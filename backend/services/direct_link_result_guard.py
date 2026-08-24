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

import logging
from pathlib import Path

from core.config import get_settings
from db.database import get_db
from services.aria2_runtime import is_builtin_mode
from services.manager_v2 import DIRECT_LINK_SOURCE
from services.transfer_runtime_guard import GuardedTransferIntegrityManager


logger = logging.getLogger("debridpulse.direct_link_result_guard")
_FALSE_ARIA2_FAILURE = "One or more aria2 transfers failed"


class DirectLinkResultGuardManager(GuardedTransferIntegrityManager):
    """Keep provider-source outcomes out of physical aria2 accounting."""

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
                       SUM(CASE WHEN local_path IS NULL AND download_id IS NULL AND status IN ('error','missing') THEN 1 ELSE 0 END) AS source_failure_count,
                       SUM(CASE WHEN status='duplicate' THEN 1 ELSE 0 END) AS duplicate_count
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

                details = [f"aria2 completed {completed_count} logical file(s)"]
                if source_failures:
                    details.append(f"{source_failures} source link(s) unavailable")
                if duplicates:
                    details.append(f"{duplicates} duplicate mirror(s) suppressed")
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

    async def _finalize_aria2_torrent(self, torrent_id: int):
        torrent_id = int(torrent_id)
        await self._normalize_direct_link_source_outcomes(torrent_id)

        # Own successful direct-link completion so retained Duplicate rows do not
        # inflate the inherited finalizer's all-row size SUM.  All other cases
        # (ordinary torrents, active work, and real physical aria2 failures)
        # remain delegated to the previously audited finalizer.
        if await self._complete_direct_link_result(torrent_id):
            return
        return await super()._finalize_aria2_torrent(torrent_id)

    async def reconcile_aria2_on_startup(self):
        # Normalize before inherited startup reconciliation, then repair any
        # terminal Error row created by the old source/aria2 conflation when the
        # physical payload is in fact complete.
        await self._normalize_direct_link_source_outcomes()
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
