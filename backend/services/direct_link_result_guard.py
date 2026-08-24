"""Direct-link result authority for the v1.0.6 stabilization line.

Direct-link batches contain two different kinds of child rows:

* physical delivery rows that successfully unlocked and entered local aria2
  planning; and
* source outcomes (unsupported/missing host links and mirror duplicates) that
  must remain visible to the operator but are not aria2 work.

The inherited aria2 aggregator historically treated every ``blocked=0`` error
row as a failed physical transfer.  This guard makes the distinction explicit
without hiding source outcomes from the UI.
"""
from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from pathlib import Path

from core.config import get_settings
from core.logging_utils import sanitize_log_value
from db.database import get_db
from services.aria2_runtime import is_builtin_mode
from services.manager_v2 import DIRECT_LINK_SOURCE, safe_name
from services.transfer_runtime_guard import GuardedTransferIntegrityManager


logger = logging.getLogger("debridpulse.direct_link_result_guard")
_FALSE_ARIA2_FAILURE = "One or more aria2 transfers failed"
_FAILOVER_ARIA2_ERROR_CODES = frozenset({
    "2",   # timeout
    "3",   # resource not found
    "4",   # max resource-not-found threshold
    "5",   # remote source too slow
    "6",   # network problem
    "8",   # remote server cannot resume
    "19",  # name resolution failure
    "21",  # FTP command failure
    "22",  # bad/unexpected HTTP response header
    "23",  # redirect loop
    "24",  # HTTP authorization failure / expired capability
    "29",  # remote overload / maintenance
    "32",  # checksum validation failure
})
_ACTIVE_DIRECT_LINK_PATHS: ContextVar[frozenset[str]] = ContextVar(
    "debridpulse_active_direct_link_paths",
    default=frozenset(),
)
_BATCH_DIRECT_LINK_PATHS: ContextVar[set[str] | None] = ContextVar(
    "debridpulse_batch_direct_link_paths",
    default=None,
)


class DirectLinkResultGuardManager(GuardedTransferIntegrityManager):
    """Keep provider-source outcomes out of physical aria2 accounting."""

    def __init__(self):
        super().__init__()
        # Filename assignment is a very short logical critical section relative
        # to an entire transfer lifetime, but the inherited preparation routine
        # unlocks and assigns names in one operation. Serializing direct-link
        # preparation prevents two simultaneous batches from claiming the same
        # absent pathname before either has persisted its child rows.
        self._direct_link_path_planning_lock = asyncio.Lock()

    async def _prepare_direct_link_collection(
        self, torrent_id: int, links: list[str]
    ) -> None:
        """Plan direct-link paths from operational occupancy, never history.

        The legacy materializer seeds its ``reserved`` set from every non-deleted
        transfer.  Completed/error history is useful to the operator but is not
        an active filesystem claim.  Snapshot only non-terminal local paths here
        and give the allocator a separate same-batch reservation set.
        """
        torrent_id = int(torrent_id)
        async with self._direct_link_path_planning_lock:
            async with get_db() as db:
                rows = await db.fetchall(
                    """SELECT DISTINCT f.local_path
                         FROM download_files f
                         JOIN torrents t ON t.id=f.torrent_id
                        WHERE t.status NOT IN ('completed','deleted','error')
                          AND t.id!=?
                          AND f.local_path IS NOT NULL
                          AND f.local_path!=''""",
                    (torrent_id,),
                )
            active_paths = frozenset(
                str(row.get("local_path") or "").strip().casefold()
                for row in rows
                if str(row.get("local_path") or "").strip()
            )
            active_token = _ACTIVE_DIRECT_LINK_PATHS.set(active_paths)
            batch_token = _BATCH_DIRECT_LINK_PATHS.set(set())
            try:
                return await super()._prepare_direct_link_collection(
                    torrent_id, links
                )
            finally:
                _BATCH_DIRECT_LINK_PATHS.reset(batch_token)
                _ACTIVE_DIRECT_LINK_PATHS.reset(active_token)

    def _unique_direct_link_path(
        self,
        root: Path,
        filename: str,
        reserved: set[str],
        *,
        reuse_existing: bool = False,
    ) -> Path:
        """Choose a target from actual disk and live ownership state.

        ``reuse_existing`` is intentionally ignored.  A real file on disk is an
        occupancy fact and must never be silently overwritten because historical
        metadata says the source was once deleted.  During authoritative batch
        planning, completed/error/deleted DB history is ignored; only paths owned
        by non-terminal transfers and names already assigned in this batch count.
        Outside that planning context, retain the caller-supplied reservation set
        for compatibility with focused allocator callers/tests.
        """
        del reuse_existing
        candidate = root / safe_name(filename)
        stem = candidate.stem or "download"
        suffix = candidate.suffix
        counter = 2

        active_paths = _ACTIVE_DIRECT_LINK_PATHS.get()
        batch_paths = _BATCH_DIRECT_LINK_PATHS.get()
        effective_reserved = (
            set(reserved)
            if batch_paths is None
            else set(active_paths) | set(batch_paths)
        )

        while (
            str(candidate).casefold() in effective_reserved
            or candidate.exists()
        ):
            candidate = root / f"{stem} ({counter}){suffix}"
            counter += 1

        normalized = str(candidate).casefold()
        if batch_paths is not None:
            batch_paths.add(normalized)
        else:
            reserved.add(normalized)
        return candidate

    async def _normalize_direct_link_source_outcomes(
        self, torrent_id: int | None = None
    ) -> int:
        """Mark pre-dispatch source failures as non-physical result rows.

        ``blocked=NULL`` is already the persisted convention used by retained
        Duplicate mirrors: physical-transfer queries require ``blocked=0`` while
        the UI still renders the row and its status/reason.  A real aria2 error
        has a local path because it reached delivery planning and is therefore
        deliberately left at ``blocked=0``.
        """
        params: list[object] = [DIRECT_LINK_SOURCE]
        id_clause = ""
        if torrent_id is not None:
            id_clause = " AND t.id=?"
            params.append(int(torrent_id))

        async with get_db() as db:
            cursor = await db.execute(
                f"""UPDATE download_files
                       SET blocked=NULL, updated_at=CURRENT_TIMESTAMP
                     WHERE blocked=0
                       AND local_path IS NULL
                       AND download_id IS NULL
                       AND status IN ('error','missing')
                       AND torrent_id IN (
                           SELECT t.id FROM torrents t
                            WHERE t.source=?
                              AND t.status!='deleted'{id_clause}
                       )""",
                tuple(params),
            )
            await db.commit()
            return max(0, int(getattr(cursor, "rowcount", 0) or 0))

    async def _engine_update_aria2_parent_progress(self, all_downloads=None):
        # Normalize before the inherited aggregator snapshots children, so an
        # unavailable source cannot hold an otherwise complete download at 99.x%.
        await self._normalize_direct_link_source_outcomes()
        return await super()._engine_update_aria2_parent_progress(all_downloads)

    async def _get_torrent_completion_snapshot(self, torrent_id: int):
        # Startup/recovery snapshot semantics must use the same physical set.
        await self._normalize_direct_link_source_outcomes(int(torrent_id))
        return await super()._get_torrent_completion_snapshot(torrent_id)

    @staticmethod
    def _persisted_mirror_failure_code(reason: object) -> str:
        prefix = str(reason or "").strip().split(":", 1)[0].strip()
        return prefix if prefix.isdigit() else ""

    async def _mirror_failure_is_failover_eligible(
        self, row: dict
    ) -> tuple[bool, str]:
        """Classify only failures that another upstream source can plausibly fix."""
        persisted = str(row.get("block_reason") or "").strip()
        if persisted.startswith("source-unlock:"):
            reason = persisted.split(":", 1)[1].strip() or "source unlock failed"
            return True, reason
        if persisted.startswith("aria2-dispatch:"):
            reason = persisted.split(":", 1)[1].strip() or "local aria2 dispatch failed"
            return False, reason

        code = self._persisted_mirror_failure_code(persisted)
        if code:
            return code in _FAILOVER_ARIA2_ERROR_CODES, persisted

        gid = str(row.get("download_id") or "").strip()
        if not gid:
            return False, persisted or "unclassified pre-dispatch failure"
        try:
            state = await self.aria2().tell_status(gid)
        except Exception as exc:
            return False, sanitize_log_value(
                f"Could not confirm aria2 source failure: {exc}",
                max_length=220,
            )
        if str(getattr(state, "status", "") or "") != "error":
            return False, persisted or f"aria2 state is {getattr(state, 'status', '')}"

        code = str(getattr(state, "error_code", "") or "").strip()
        raw_reason = f"{code}: {getattr(state, 'error_message', '')}".strip(": ")
        capability = str(row.get("download_url") or "").strip()
        if capability:
            raw_reason = raw_reason.replace(capability, "<capability-url>")
        safe_reason = sanitize_log_value(raw_reason, max_length=220)
        return code in _FAILOVER_ARIA2_ERROR_CODES, safe_reason

    async def _clear_failed_mirror_target(self, row: dict) -> tuple[bool, str]:
        """Discard partial state before switching hosters; never mix source bytes."""
        gid = str(row.get("download_id") or "").strip()
        if gid and is_builtin_mode():
            try:
                await self._remove_owned_aria2_gid(gid)
            except Exception as exc:
                return False, sanitize_log_value(
                    f"Could not retire failed aria2 job {gid}: {exc}",
                    max_length=220,
                )

        local_path = str(row.get("local_path") or "").strip()
        if not local_path:
            return True, ""
        target = Path(local_path)
        for candidate in (Path(f"{target}.aria2"), target):
            try:
                if not candidate.exists() and not candidate.is_symlink():
                    continue
                if candidate.is_dir() and not candidate.is_symlink():
                    return False, f"Refusing to remove directory during mirror failover: {candidate}"
                candidate.unlink()
            except OSError as exc:
                return False, sanitize_log_value(
                    f"Could not clear failed mirror target {candidate}: {exc}",
                    max_length=220,
                )
        return True, ""

    async def _promote_direct_link_mirror_failover(self, torrent_id: int) -> bool:
        """Promote one standby per failed logical artifact before parent failure."""
        torrent_id = int(torrent_id)
        async with get_db() as db:
            failures = await db.fetchall(
                """SELECT f.id AS file_id, f.torrent_id, f.filename, f.size_bytes,
                          f.source_url, f.download_url, f.local_path, f.download_id,
                          f.block_reason, f.mirror_group_id, f.mirror_state,
                          t.name AS torrent_name, t.progress AS torrent_progress
                     FROM download_files f
                     JOIN torrents t ON t.id=f.torrent_id
                    WHERE t.id=? AND t.source=?
                      AND f.blocked=0 AND f.status='error'
                      AND f.mirror_group_id IS NOT NULL
                      AND f.mirror_state='active'
                    ORDER BY f.id ASC""",
                (torrent_id, DIRECT_LINK_SOURCE),
            )

        if not failures:
            return False

        from services.dispatch_coordinator import _source_host

        promoted_any = False
        for failed in failures:
            group_id = int(failed.get("mirror_group_id") or failed["file_id"])
            async with get_db() as db:
                standbys = await db.fetchall(
                    """SELECT id AS file_id, filename, size_bytes, source_url,
                              mirror_group_id, mirror_state
                         FROM download_files
                        WHERE torrent_id=? AND mirror_group_id=?
                          AND mirror_state='standby' AND status='duplicate'
                        ORDER BY id ASC""",
                    (torrent_id, group_id),
                )
            if not standbys:
                await self._log_event(
                    torrent_id,
                    "error",
                    f"Mirror failover exhausted for {failed['filename']!r}; no standby source remains",
                )
                continue

            eligible, failure_reason = await self._mirror_failure_is_failover_eligible(failed)
            failed_host = _source_host(failed.get("source_url")) or "active source"
            if not eligible:
                await self._log_event(
                    torrent_id,
                    "warn",
                    f"Mirror failover not attempted for {failed_host}: local/system failure · {failure_reason}",
                )
                continue

            cleared, clear_reason = await self._clear_failed_mirror_target(failed)
            if not cleared:
                await self._log_event(
                    torrent_id,
                    "error",
                    f"Mirror failover could not prepare a clean target for {failed['filename']!r}: {clear_reason}",
                )
                continue

            promoted = standbys[0]
            promoted_host = _source_host(promoted.get("source_url")) or "standby source"
            async with get_db() as db:
                candidate_update = await db.execute(
                    """UPDATE download_files
                          SET status='pending', blocked=0, block_reason=NULL,
                              download_url=NULL, local_path=?, download_id=NULL,
                              retry_count=0, mirror_state='active',
                              updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND torrent_id=? AND mirror_group_id=?
                          AND mirror_state='standby' AND status='duplicate'""",
                    (
                        str(failed.get("local_path") or ""),
                        int(promoted["file_id"]),
                        torrent_id,
                        group_id,
                    ),
                )
                if int(getattr(candidate_update, "rowcount", 0) or 0) <= 0:
                    await db.commit()
                    continue

                await db.execute(
                    """UPDATE download_files
                          SET blocked=NULL, mirror_state='exhausted',
                              download_url=NULL, block_reason=?,
                              updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND torrent_id=?
                          AND mirror_group_id=? AND mirror_state='active'
                          AND status='error'""",
                    (failure_reason, int(failed["file_id"]), torrent_id, group_id),
                )
                await db.execute(
                    """UPDATE torrents
                          SET status='queued',
                              error_message=CASE
                                  WHEN error_message=? THEN NULL
                                  ELSE error_message
                              END,
                              updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND status!='deleted'""",
                    (_FALSE_ARIA2_FAILURE, torrent_id),
                )
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) VALUES (?, 'warn', ?)",
                    (
                        torrent_id,
                        f"Mirror source exhausted: {failed_host} · {failure_reason}",
                    ),
                )
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) VALUES (?, 'info', ?)",
                    (
                        torrent_id,
                        f"Mirror failover: promoted {promoted_host} standby for {failed['filename']}",
                    ),
                )
                await db.commit()

            logger.info(
                "direct-link mirror failover: transfer=%s file=%s failed_host=%s promoted_host=%s",
                torrent_id,
                str(failed.get("filename") or "")[:120],
                failed_host,
                promoted_host,
            )
            await self._broadcast_direct_link_update(
                torrent_id,
                "queued",
                str(failed.get("torrent_name") or "Debrid links"),
                float(failed.get("torrent_progress") or 0.0),
            )
            promoted_any = True

        if promoted_any:
            self._track_maintenance_task(
                self.advance_aria2_queue(),
                label=f"mirror-failover-dispatch-{torrent_id}",
            )
        return promoted_any

    async def _direct_link_completion_state(self, torrent_id: int):
        async with get_db() as db:
            parent = await db.fetchone(
                "SELECT * FROM torrents WHERE id=?",
                (int(torrent_id),),
            )
            if not parent or str(parent.get("source") or "") != DIRECT_LINK_SOURCE:
                return None

            physical = await db.fetchone(
                """SELECT
                       SUM(CASE WHEN blocked=0 AND local_path IS NOT NULL THEN 1 ELSE 0 END) AS required_count,
                       SUM(CASE WHEN blocked=0 AND local_path IS NOT NULL AND status='completed' THEN 1 ELSE 0 END) AS completed_count,
                       SUM(CASE WHEN blocked=0 AND local_path IS NOT NULL AND status='error' THEN 1 ELSE 0 END) AS error_count,
                       SUM(CASE WHEN blocked=0 AND local_path IS NOT NULL AND status IN ('pending','queued','downloading','paused') THEN 1 ELSE 0 END) AS active_count,
                       COALESCE(SUM(CASE WHEN blocked=0 AND local_path IS NOT NULL AND status='completed' THEN size_bytes ELSE 0 END), 0) AS completed_bytes,
                       COUNT(*) AS total_rows,
                       SUM(CASE WHEN local_path IS NULL AND download_id IS NULL
                                           AND status IN ('error','missing')
                                           AND COALESCE(mirror_state, '')!='exhausted'
                                      THEN 1 ELSE 0 END) AS source_failure_count,
                       SUM(CASE WHEN status='duplicate' THEN 1 ELSE 0 END) AS duplicate_count,
                       SUM(CASE WHEN mirror_state='exhausted' THEN 1 ELSE 0 END) AS mirror_exhausted_count,
                       SUM(CASE WHEN mirror_state='standby' AND status='duplicate' THEN 1 ELSE 0 END) AS mirror_standby_count
                   FROM download_files
                  WHERE torrent_id=?""",
                (int(torrent_id),),
            )
            return parent, physical or {}

    @staticmethod
    def _successful_physical_completion(counts: dict) -> bool:
        required = int(counts.get("required_count") or 0)
        completed = int(counts.get("completed_count") or 0)
        errors = int(counts.get("error_count") or 0)
        active = int(counts.get("active_count") or 0)
        return required > 0 and completed == required and errors == 0 and active == 0

    async def _complete_direct_link_result(self, torrent_id: int) -> bool:
        """Finalize one direct-link parent from physical delivery truth.

        Returns True when this method owned finalization.  Source failures and
        retained Duplicate mirrors stay visible, but neither contributes to the
        logical payload size or aria2 success/failure decision.
        """
        torrent_id = int(torrent_id)
        async with self._lifecycle_lock(torrent_id):
            if self._delete_requested(torrent_id) or torrent_id in self._active:
                return False

            await self._normalize_direct_link_source_outcomes(torrent_id)
            state = await self._direct_link_completion_state(torrent_id)
            if state is None:
                return False
            parent, counts = state
            parent_status = str(parent.get("status") or "")
            if parent_status in {"completed", "deleted"}:
                return False
            if not self._successful_physical_completion(counts):
                return False

            completed_count = int(counts.get("completed_count") or 0)
            logical_size = int(counts.get("completed_bytes") or 0)
            total_rows = int(counts.get("total_rows") or 0)
            source_failures = int(counts.get("source_failure_count") or 0)
            duplicates = int(counts.get("duplicate_count") or 0)
            mirror_exhausted = int(counts.get("mirror_exhausted_count") or 0)
            mirror_standbys = int(counts.get("mirror_standby_count") or 0)

            current_error = str(parent.get("error_message") or "").strip()
            if source_failures:
                reconstructed_warning = (
                    f"{source_failures} of {total_rows} links could not be generated"
                )
                error_message = (
                    reconstructed_warning
                    if not current_error or current_error == _FALSE_ARIA2_FAILURE
                    else current_error
                )
            else:
                error_message = None if current_error == _FALSE_ARIA2_FAILURE else current_error or None

            async with get_db() as db:
                update = await db.execute(
                    """UPDATE torrents
                           SET status='completed', completed_at=CURRENT_TIMESTAMP,
                               size_bytes=?, progress=100.0, error_message=?,
                               updated_at=CURRENT_TIMESTAMP
                         WHERE id=? AND status NOT IN ('completed','deleted')""",
                    (logical_size, error_message, torrent_id),
                )
                if int(getattr(update, "rowcount", 0) or 0) <= 0:
                    await db.commit()
                    return False

                await db.execute(
                    """UPDATE download_files
                          SET mirror_state='unused', updated_at=CURRENT_TIMESTAMP
                        WHERE torrent_id=? AND mirror_state='standby'
                          AND status='duplicate'""",
                    (torrent_id,),
                )
                details = [f"aria2 completed {completed_count} logical file(s)"]
                if source_failures:
                    details.append(f"{source_failures} source link(s) unavailable")
                if mirror_exhausted:
                    details.append(
                        f"{mirror_exhausted} mirror source(s) exhausted during failover"
                    )
                if duplicates:
                    details.append(f"{duplicates} duplicate mirror(s) suppressed")
                if mirror_standbys:
                    details.append(f"{mirror_standbys} unused standby mirror(s)")
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) VALUES (?, 'info', ?)",
                    (torrent_id, "; ".join(details)),
                )
                await db.commit()

            await self._log_event(
                torrent_id,
                "info",
                "Direct-link transaction completed; no AllDebrid magnet cleanup required",
            )
            await self._mark_finished(torrent_id, name=str(parent.get("name") or ""))
            self._track_maintenance_task(
                self._extract_torrent(torrent_id, dict(parent)),
                label=f"extract-{torrent_id}",
            )
            if get_settings().discord_notify_finished:
                await self.notify().send_complete(
                    str(parent.get("name") or ""),
                    file_count=completed_count,
                    size_bytes=logical_size,
                    download_client="aria2",
                )

            if is_builtin_mode():
                try:
                    await self.aria2().purge_download_results()
                except Exception:
                    pass

            try:
                from services.page_cache import drop_page_cache_for_file

                async with get_db() as db:
                    completed_rows = await db.fetchall(
                        """SELECT local_path FROM download_files
                            WHERE torrent_id=? AND blocked=0
                              AND status='completed' AND local_path IS NOT NULL""",
                        (torrent_id,),
                    )
                dropped = 0
                for row in completed_rows:
                    local_path = row.get("local_path")
                    if local_path and drop_page_cache_for_file(local_path):
                        dropped += 1
                if dropped:
                    logger.debug(
                        "Page cache released for %d direct-link file(s) of torrent %s",
                        dropped,
                        torrent_id,
                    )
            except Exception as exc:
                logger.debug("Direct-link page cache drop skipped: %s", exc)

            return True

    async def _extract_torrent(self, torrent_id: int, torrent_dict: dict) -> None:
        if self._architecture is not None:
            await self._architecture.extraction.extract_completed_transfer(
                int(torrent_id), dict(torrent_dict or {})
            )
            return
        await super()._extract_torrent(torrent_id, torrent_dict)

    async def _finalize_aria2_torrent(self, torrent_id: int):
        torrent_id = int(torrent_id)
        await self._normalize_direct_link_source_outcomes(torrent_id)

        # Own successful direct-link completion so retained Duplicate rows do not
        # inflate the inherited finalizer's all-row size SUM.  All other cases
        # (ordinary torrents, active work, and real physical aria2 failures)
        # remain delegated to the previously audited finalizer.
        if await self._complete_direct_link_result(torrent_id):
            return

        # A failed active mirror is a source attempt, not yet a failed artifact.
        # Promote a validated standby before the inherited parent-error path runs.
        if await self._promote_direct_link_mirror_failover(torrent_id):
            return

        async with get_db() as db:
            before = await db.fetchone(
                "SELECT status, source FROM torrents WHERE id=?",
                (torrent_id,),
            )

        result = await super()._finalize_aria2_torrent(torrent_id)

        # The inherited physical-error finalizer persists status='error' but does
        # not publish the terminal transition. Without this notification the UI
        # can remain on its last pushed Downloading state until another operator
        # action forces a refresh. Direct-link result authority closes that gap.
        if (
            before
            and str(before.get("source") or "") == DIRECT_LINK_SOURCE
            and str(before.get("status") or "") != "error"
        ):
            async with get_db() as db:
                after = await db.fetchone(
                    "SELECT name, status, progress FROM torrents WHERE id=?",
                    (torrent_id,),
                )
            if after and str(after.get("status") or "") == "error":
                await self._broadcast_direct_link_update(
                    torrent_id,
                    "error",
                    str(after.get("name") or "Debrid links"),
                    float(after.get("progress") or 0.0),
                )

        return result

    async def reconcile_aria2_on_startup(self):
        # Normalize before inherited startup reconciliation, then repair any
        # terminal Error row created by the old source/aria2 conflation when the
        # physical payload is in fact complete.
        await self._normalize_direct_link_source_outcomes()
        async with get_db() as db:
            failover_candidates = await db.fetchall(
                """SELECT DISTINCT t.id
                     FROM torrents t
                     JOIN download_files f ON f.torrent_id=t.id
                    WHERE t.source=? AND t.status!='deleted'
                      AND f.blocked=0 AND f.status='error'
                      AND f.mirror_group_id IS NOT NULL
                      AND f.mirror_state='active'""",
                (DIRECT_LINK_SOURCE,),
            )
        for row in failover_candidates:
            await self._promote_direct_link_mirror_failover(int(row["id"]))
        result = await super().reconcile_aria2_on_startup()
        async with get_db() as db:
            candidates = await db.fetchall(
                """SELECT DISTINCT t.id
                     FROM torrents t
                     JOIN download_files f ON f.torrent_id=t.id
                    WHERE t.source=? AND t.status='error'
                      AND f.blocked=0 AND f.local_path IS NOT NULL
                      AND f.status='completed'""",
                (DIRECT_LINK_SOURCE,),
            )
        for row in candidates:
            await self._complete_direct_link_result(int(row["id"]))
        return result


manager = DirectLinkResultGuardManager()
