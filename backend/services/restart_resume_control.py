"""Built-in aria2 restart recovery without changing canonical download paths.

The built-in daemon intentionally starts with a clean RPC/session state.  A
DebridPulse download may therefore have a durable database row and an on-disk
payload/control sidecar even though its old aria2 GID no longer exists.  This
coordinator treats that condition as a redispatch of the *same* physical target,
never as a fresh manifest allocation.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from core.config import get_settings
from db.database import get_db
from services.aria2 import Aria2ConnectionError, Aria2RPCError
from services.aria2_runtime import is_builtin_mode
from services.dispatch_coordinator import MirrorAwareTransferControlCoordinator


logger = logging.getLogger("debridpulse.restart_resume")
_LOST_MARKERS = ("aria2 entry lost", "aria2 entry removed", "partial/missing aria2 state")


def resume_artifact_state(local_path: object) -> tuple[bool, bool]:
    """Return whether the exact payload and its aria2 control sidecar exist."""
    value = str(local_path or "").strip()
    if not value:
        return False, False
    target = Path(value)
    return target.exists(), Path(f"{target}.aria2").exists()


def _redispatch_source(row: dict) -> str:
    """Prefer the durable provider/source identity over an expiring generated URL."""
    return str(row.get("source_url") or row.get("download_url") or "").strip()


def _redispatch_download_url(row: dict, source: str) -> Optional[str]:
    # Direct-link rows are unlocked again from source_url by the dispatcher.
    # Keeping an old generated CDN capability here is unnecessary and can be
    # actively harmful after a restart because those capabilities expire.
    if str(row.get("transfer_source") or "") == "direct_link":
        return None
    return source


class RestartResumableTransferControlCoordinator(MirrorAwareTransferControlCoordinator):
    """Recover vanished built-in GIDs in place at their persisted exact path."""

    async def _stage_missing_paused_gids(self, torrent_id: Optional[int] = None) -> dict:
        """Turn stale paused GIDs into parked exact-path redispatch work.

        The row deliberately remains ``paused`` here.  The inherited resume path
        changes a blank-GID paused row to ``pending`` while holding its normal
        queue/dispatch locks.  That keeps this preflight from making work runnable
        before Resume owns the control transition.
        """
        if not is_builtin_mode():
            return {"recovered": 0, "resumable": 0}

        params: tuple = ()
        predicate = ""
        if torrent_id is not None:
            predicate = " AND f.torrent_id=?"
            params = (int(torrent_id),)

        async with get_db() as db:
            rows = await db.fetchall(
                f"""SELECT f.id AS file_id, f.torrent_id, f.download_id,
                           f.local_path, f.source_url, f.download_url,
                           t.source AS transfer_source
                      FROM download_files f
                      JOIN torrents t ON t.id=f.torrent_id
                     WHERE f.download_client='aria2'
                       AND f.blocked=0 AND f.status='paused'
                       AND f.download_id IS NOT NULL{predicate}
                     ORDER BY f.torrent_id, f.id""",
                params,
            )

        recovered = 0
        resumable = 0
        parents: dict[int, list[bool]] = {}
        for row in rows:
            gid = str(row.get("download_id") or "").strip()
            if not gid:
                continue
            try:
                state = await self.confirm_gid(gid)
            except (Aria2ConnectionError, Aria2RPCError):
                # Connectivity/auth/RPC failure is not disappearance evidence.
                continue
            if state is not None:
                continue

            source = _redispatch_source(row)
            local_path = str(row.get("local_path") or "").strip()
            if not source or not local_path:
                # Without both durable source identity and the canonical target
                # there is no safe way to recreate this job in place.
                continue

            payload_exists, sidecar_exists = resume_artifact_state(local_path)
            exact_resume = bool(payload_exists and sidecar_exists)
            new_download_url = _redispatch_download_url(row, source)
            async with get_db() as db:
                cursor = await db.execute(
                    """UPDATE download_files
                          SET download_id=NULL, download_url=?,
                              updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND status='paused' AND download_id=?""",
                    (new_download_url, int(row["file_id"]), gid),
                )
                await db.commit()
                changed = int(getattr(cursor, "rowcount", 0) or 0) != 0
            if not changed:
                continue

            recovered += 1
            resumable += int(exact_resume)
            parents.setdefault(int(row["torrent_id"]), []).append(exact_resume)
            logger.info(
                "Recovered stale built-in aria2 GID %s for transfer %s at exact path %s "
                "(payload=%s, control=%s)",
                gid,
                row["torrent_id"],
                local_path,
                payload_exists,
                sidecar_exists,
            )

        for parent_id, states in parents.items():
            exact_count = sum(1 for state in states if state)
            await self.manager._log_event(
                parent_id,
                "info",
                "Built-in aria2 restarted; preserved canonical target path for "
                f"{len(states)} paused file(s), including {exact_count} with an "
                "exact payload + .aria2 control file ready to resume",
            )
        return {"recovered": recovered, "resumable": resumable}

    async def _resume_parent(self, torrent_id: int) -> dict:
        # A clean built-in daemon has no old GID to unpause.  Convert only
        # explicitly-confirmed missing jobs to parked blank-GID rows first; the
        # inherited resume path then redispatches their persisted local_path.
        await self._stage_missing_paused_gids(int(torrent_id))
        return await super()._resume_parent(int(torrent_id))

    async def _resume_unintended_paused(self) -> int:
        # Resume All / reconciliation uses this path rather than _resume_parent.
        await self._stage_missing_paused_gids()
        return await super()._resume_unintended_paused()

    async def reset_for_redownload(self, torrent_id: int, reason: str):
        """Recover confirmed-lost built-in jobs without rebuilding their manifest.

        A whole-parent rebuild is exactly what can route a direct-link child back
        through filename collision allocation and create ``(2)``, ``(3)``, ...
        copies.  Once a physical manifest has a canonical ``local_path`` and a
        durable source, loss of the daemon GID only clears that GID.  aria2 is
        subsequently re-added with the same dir/out; an existing ``.aria2``
        sidecar makes the addUri operation a true byte-range resume.
        """
        reason_cf = str(reason or "").casefold()
        if (
            not is_builtin_mode()
            or not any(marker in reason_cf for marker in _LOST_MARKERS)
        ):
            return await super().reset_for_redownload(torrent_id, reason)

        torrent_id = int(torrent_id)
        await self.ensure_initialized()
        async with get_db() as db:
            rows = await db.fetchall(
                """SELECT f.id AS file_id, f.status, f.download_id,
                          f.local_path, f.source_url, f.download_url,
                          t.source AS transfer_source
                     FROM download_files f
                     JOIN torrents t ON t.id=f.torrent_id
                    WHERE f.torrent_id=? AND f.blocked=0
                    ORDER BY f.id""",
                (torrent_id,),
            )
        if not rows:
            return await super().reset_for_redownload(torrent_id, reason)

        paused = bool(get_settings().paused) or torrent_id in self._pause_intents
        recovered: list[tuple[dict, bool]] = []
        unrecoverable: list[dict] = []
        observed_live = False

        for row in rows:
            status = str(row.get("status") or "")
            if status == "completed":
                continue

            gid = str(row.get("download_id") or "").strip()
            if not gid:
                # Existing pending/paused rows are already parked correctly.
                if status in {"pending", "paused"}:
                    continue
                state = None
            else:
                try:
                    state = await self.confirm_gid(gid)
                except (Aria2ConnectionError, Aria2RPCError):
                    # Never mutate filesystem recovery state on an operational
                    # RPC failure; another reconciliation cycle can try again.
                    self._lost_strikes.pop(torrent_id, None)
                    return

            if state is not None:
                state_name = str(getattr(state, "status", "") or "")
                if state_name == "complete":
                    async with get_db() as db:
                        await db.execute(
                            """UPDATE download_files SET status='completed',
                                      updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                            (int(row["file_id"]),),
                        )
                        await db.commit()
                    continue
                if state_name not in {"removed"}:
                    observed_live = True
                    continue

            source = _redispatch_source(row)
            local_path = str(row.get("local_path") or "").strip()
            if not source or not local_path:
                unrecoverable.append(row)
                continue

            payload_exists, sidecar_exists = resume_artifact_state(local_path)
            recovered.append((row, bool(payload_exists and sidecar_exists)))

        if not recovered:
            if observed_live and not unrecoverable:
                self._lost_strikes.pop(torrent_id, None)
                logger.info(
                    "Suppressed destructive reset for transfer %s; aria2 state is still live",
                    torrent_id,
                )
                return
            return await super().reset_for_redownload(torrent_id, reason)

        target_status = "paused" if paused else "pending"
        async with get_db() as db:
            for row, _exact_resume in recovered:
                source = _redispatch_source(row)
                await db.execute(
                    """UPDATE download_files
                          SET status=?, download_id=NULL, download_url=?,
                              block_reason=NULL, updated_at=CURRENT_TIMESTAMP
                        WHERE id=?""",
                    (
                        target_status,
                        _redispatch_download_url(row, source),
                        int(row["file_id"]),
                    ),
                )

            # Never destroy already-canonical siblings merely because one legacy
            # row lacks enough identity to be safely redispatched.  Surface that
            # row as an error instead of rebuilding the entire parent.
            for row in unrecoverable:
                await db.execute(
                    """UPDATE download_files
                          SET status='error',
                              block_reason='Cannot recover missing aria2 job without durable source and canonical path',
                              updated_at=CURRENT_TIMESTAMP
                        WHERE id=?""",
                    (int(row["file_id"]),),
                )

            await db.execute(
                """UPDATE torrents
                      SET status=?, error_message=?, updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND status NOT IN ('completed','deleted','error')""",
                (
                    "paused" if paused else "queued",
                    (
                        f"{len(unrecoverable)} file(s) could not be recovered safely after aria2 restart"
                        if unrecoverable
                        else None
                    ),
                    torrent_id,
                ),
            )
            exact_count = sum(1 for _row, exact in recovered if exact)
            await db.execute(
                "INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)",
                (
                    torrent_id,
                    "warn" if unrecoverable else "info",
                    f"{reason}; recovered {len(recovered)} file(s) at their exact "
                    f"persisted path ({exact_count} with payload + .aria2 control "
                    "state); canonical manifest retained, no filename reallocation"
                    + (
                        f"; {len(unrecoverable)} legacy file(s) lacked safe recovery identity"
                        if unrecoverable
                        else ""
                    ),
                ),
            )
            await db.commit()

        self._lost_strikes.pop(torrent_id, None)
        if not paused:
            self._schedule_queue()
        return
