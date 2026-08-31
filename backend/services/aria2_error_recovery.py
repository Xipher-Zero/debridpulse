"""Bounded aria2 error recovery owned by the canonical reconciliation path.

Retries are driven by persisted download-file state rather than by sleeping inside
an aria2 state lock. ``download_files.updated_at`` is the retry clock, and the
retry counter is atomically claimed before any network mutation so a failed RPC
attempt still consumes budget.
"""
from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath

from core.config import get_settings
from core.logging_utils import sanitize_exception
from db.database import get_db
from services.aria2_runtime import is_builtin_mode
from services.event_bus import publish

logger = logging.getLogger("debridpulse.aria2_error_recovery")


class Aria2ErrorRecovery:
    def __init__(self, engine, ownership):
        self.engine = engine
        self.ownership = ownership

    async def _event(self, transfer_id: int, level: str, message: str) -> None:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)",
                (int(transfer_id), str(level), str(message)[:1200]),
            )
            await db.commit()

    async def _publish(self, transfer_id: int, status: str, name: str) -> None:
        try:
            await publish(
                "torrent_updated",
                {
                    "id": int(transfer_id),
                    "status": str(status),
                    "name": str(name or ""),
                },
            )
            await publish("stats_changed", {})
        except Exception:
            pass

    async def run(self) -> dict:
        cfg = get_settings()
        if self.engine.download_client_name() != "aria2" or bool(cfg.paused):
            return {"checked": 0, "retried": 0, "deferred": 0, "failed": 0}

        max_retries = max(0, int(getattr(cfg, "aria2_error_retry_count", 0) or 0))
        delay_seconds = max(
            0,
            int(getattr(cfg, "aria2_error_retry_delay_seconds", 0) or 0),
        )
        if max_retries <= 0:
            return {"checked": 0, "retried": 0, "deferred": 0, "failed": 0}

        builtin = is_builtin_mode(cfg)
        snapshot = await self.engine._engine_aria2_get_all()
        by_gid = {
            str(getattr(item, "gid", "") or ""): item
            for item in snapshot
            if str(getattr(item, "gid", "") or "")
        }
        owned_gids = set(by_gid) if builtin else await self.ownership.owned_gids()
        live = [
            item for item in snapshot
            if str(getattr(item, "gid", "") or "") in owned_gids
            and str(getattr(item, "status", "") or "") in {"active", "waiting"}
        ]
        available = max(0, self.engine._aria2_slot_limit() - len(live))

        async with get_db() as db:
            rows = await db.fetchall(
                """SELECT f.id AS file_id, f.torrent_id, f.local_path,
                          f.download_id, f.download_url, f.filename,
                          f.block_reason, COALESCE(f.retry_count, 0) AS retry_count,
                          CAST(COALESCE(
                              (julianday('now') - julianday(f.updated_at)) * 86400.0,
                              1000000000
                          ) AS INTEGER) AS retry_age_seconds,
                          t.name AS torrent_name
                   FROM download_files f
                   JOIN torrents t ON t.id=f.torrent_id
                   WHERE f.download_client='aria2'
                     AND f.blocked=0
                     AND f.status='error'
                     AND t.status NOT IN ('completed','deleted','paused')
                   ORDER BY t.priority DESC, f.id ASC"""
            )

        checked = len(rows)
        retried = 0
        deferred = 0
        failed = 0

        for row in rows:
            old_gid = str(row.get("download_id") or "").strip()
            old_state = by_gid.get(old_gid) if old_gid else None
            old_status = str(getattr(old_state, "status", "") or "").lower()
            if old_status in {"active", "waiting", "paused", "complete"}:
                # A newer reconciliation observation already contradicts the
                # persisted error row. Never create a second physical job here.
                continue

            current_retry = max(0, int(row.get("retry_count") or 0))
            if current_retry >= max_retries:
                continue

            age = max(0, int(row.get("retry_age_seconds") or 0))
            if age < delay_seconds or available <= 0:
                deferred += 1
                continue

            if not builtin and old_gid and old_gid not in owned_gids:
                logger.warning(
                    "aria2 retry skipped unowned external GID for transfer %s file %s",
                    row["torrent_id"],
                    row["file_id"],
                )
                continue

            url = str(row.get("download_url") or "").strip()
            local_path = str(row.get("local_path") or "").strip()
            if not url or not local_path:
                logger.warning(
                    "aria2 retry cannot reconstruct transfer %s file %s: missing URL/path",
                    row["torrent_id"],
                    row["file_id"],
                )
                continue

            attempt = current_retry + 1
            async with get_db() as db:
                claim = await db.execute(
                    """UPDATE download_files
                       SET retry_count=?, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status='error'
                         AND COALESCE(retry_count,0)=?""",
                    (attempt, int(row["file_id"]), current_retry),
                )
                await db.commit()
            if int(getattr(claim, "rowcount", 0) or 0) <= 0:
                continue

            reason = str(row.get("block_reason") or "aria2 reported an error").strip()
            await self._event(
                int(row["torrent_id"]),
                "warn",
                f"aria2 error retry {attempt}/{max_retries} for {row['filename']!r}: {reason}",
            )

            new_gid = ""
            try:
                # The failed result must leave aria2 before ensure_download() is
                # allowed to deduplicate by URI/path. Otherwise built-in aria2 can
                # hand the same stopped/error GID back and no physical restart occurs.
                if old_gid:
                    await self.engine._remove_owned_aria2_gid(old_gid)

                target = Path(local_path)
                remote_path = self.engine._remote_aria2_path(target)
                remote = PurePosixPath(str(remote_path).replace("\\", "/"))
                options = self.engine._aria2_job_options(
                    {"dir": str(remote.parent), "out": remote.name}
                )
                retry_snapshot = (
                    [
                        item for item in snapshot
                        if str(getattr(item, "gid", "") or "") != old_gid
                    ]
                    if builtin
                    else []
                )
                # One persisted recovery attempt maps to one aria2.addUri attempt.
                # Transport-level backoff is deliberately disabled here because
                # reconciliation owns the state lock; later cycles provide the
                # configured persisted retry delay without sleeping under that lock.
                new_gid = await self.engine.aria2().ensure_download(
                    url,
                    options,
                    start_paused=False,
                    max_retries=1,
                    cached_downloads=retry_snapshot,
                )
                await self.ownership.record(
                    new_gid,
                    download_file_id=int(row["file_id"]),
                    transfer_id=int(row["torrent_id"]),
                )

                async with get_db() as db:
                    installed = await db.execute(
                        """UPDATE download_files
                           SET download_id=?, status='queued', block_reason=NULL,
                               updated_at=CURRENT_TIMESTAMP
                           WHERE id=? AND status='error' AND retry_count=?""",
                        (new_gid, int(row["file_id"]), attempt),
                    )
                    if int(getattr(installed, "rowcount", 0) or 0) > 0:
                        await db.execute(
                            """UPDATE torrents
                               SET status='queued', updated_at=CURRENT_TIMESTAMP
                               WHERE id=? AND status='error'""",
                            (int(row["torrent_id"]),),
                        )
                    await db.commit()

                if int(getattr(installed, "rowcount", 0) or 0) <= 0:
                    await self.engine._remove_owned_aria2_gid(new_gid)
                    continue

                retried += 1
                if new_gid not in by_gid:
                    available = max(0, available - 1)
                await self._publish(
                    int(row["torrent_id"]),
                    "queued",
                    str(row.get("torrent_name") or ""),
                )
            except Exception as exc:
                failed += 1
                safe_error = sanitize_exception(exc, max_length=240)
                logger.warning(
                    "aria2 retry attempt %s/%s could not start transfer %s file %s: %s",
                    attempt,
                    max_retries,
                    row["torrent_id"],
                    row["file_id"],
                    safe_error,
                )
                await self._event(
                    int(row["torrent_id"]),
                    "warn" if attempt < max_retries else "error",
                    f"aria2 retry {attempt}/{max_retries} could not start for {row['filename']!r}: {safe_error}",
                )

        return {
            "checked": checked,
            "retried": retried,
            "deferred": deferred,
            "failed": failed,
        }
