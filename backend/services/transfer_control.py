"""Durable, failure-aware pause/resume control for DebridPulse v1.0.3."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from core.config import get_settings
from core.logging_utils import sanitize_exception
from db.database import get_db
from executors.aria2.client import Aria2ConnectionError, Aria2RPCError
from services.aria2_runtime import is_builtin_mode

logger = logging.getLogger("alldebrid.transfer_control")
_INTENT_TABLE = "transfer_pause_intents"
_LOST_MARKERS = ("aria2 entry lost", "aria2 entry removed", "partial/missing aria2 state")


def _missing_gid_error(exc: Exception) -> bool:
    msg = str(exc or "").casefold()
    return "gid" in msg and any(
        token in msg
        for token in ("not found", "not exist", "cannot find", "could not find")
    )


class TransferControlCoordinator:
    """Attach a narrow reliability layer to the shared TorrentManager singleton."""

    def __init__(self, manager) -> None:
        self.manager = manager
        self._initialized = False
        self._init_lock = asyncio.Lock()
        self._queue_lock = asyncio.Lock()
        self._pause_intents: Set[int] = set()
        self._queue_task: Optional[asyncio.Task] = None
        self._queue_trailing = False
        self._lost_strikes: Dict[int, int] = {}

        self._orig_dispatch = manager._engine_dispatch_pending_aria2_queue
        self._orig_schedule_ready = manager._engine_schedule_ready_parent_download
        self._orig_start = manager._engine_start_download
        self._orig_download = manager._engine_download
        self._orig_reset = manager._engine_reset_torrent_for_redownload
        self._orig_parent_progress = manager._engine_update_aria2_parent_progress
        self._orig_sync_clients = manager._engine_sync_download_clients

    def install(self) -> None:
        raise RuntimeError("Runtime method patching was removed in DebridPulse v1.0.5")

    # ---------- durable intent ----------

    async def ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return

            preseed = set(self._pause_intents)
            async with get_db() as db:
                await db.execute(
                    f"""CREATE TABLE IF NOT EXISTS {_INTENT_TABLE} (
                           torrent_id INTEGER PRIMARY KEY,
                           paused INTEGER NOT NULL DEFAULT 1,
                           updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                       )"""
                )
                await db.execute(
                    f"""DELETE FROM {_INTENT_TABLE}
                         WHERE torrent_id NOT IN (
                             SELECT id FROM torrents
                              WHERE status NOT IN ('completed','deleted','error')
                         )"""
                )

                # Upgrade v1.0.2 selective pauses once. An explicit paused=0
                # tombstone wins on later restarts, preserving Resume intent.
                if not bool(get_settings().paused):
                    rows = await db.fetchall(
                        """SELECT t.id
                             FROM torrents t
                             LEFT JOIN transfer_pause_intents p
                               ON p.torrent_id=t.id
                            WHERE t.status='paused'
                              AND p.torrent_id IS NULL"""
                    )
                    for row in rows:
                        await db.execute(
                            f"""INSERT INTO {_INTENT_TABLE}
                                   (torrent_id, paused, updated_at)
                               VALUES (?, 1, CURRENT_TIMESTAMP)
                               ON CONFLICT(torrent_id) DO UPDATE SET
                                   paused=1, updated_at=CURRENT_TIMESTAMP""",
                            (int(row["id"]),),
                        )

                rows = await db.fetchall(
                    f"SELECT torrent_id FROM {_INTENT_TABLE} WHERE paused=1"
                )
                await db.commit()

            self._pause_intents = {
                int(row["torrent_id"]) for row in rows
            } | preseed
            self._initialized = True

    async def _set_intent(self, torrent_id: int, paused: bool) -> None:
        torrent_id = int(torrent_id)
        if paused:
            self._pause_intents.add(torrent_id)
        else:
            self._pause_intents.discard(torrent_id)
        async with get_db() as db:
            await db.execute(
                f"""INSERT INTO {_INTENT_TABLE}
                       (torrent_id, paused, updated_at)
                   VALUES (?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(torrent_id) DO UPDATE SET
                       paused=excluded.paused,
                       updated_at=CURRENT_TIMESTAMP""",
                (torrent_id, 1 if paused else 0),
            )
            await db.commit()

    async def _set_many_resumed(self, torrent_ids: List[int]) -> None:
        self._pause_intents.clear()
        async with get_db() as db:
            await db.execute(
                f"UPDATE {_INTENT_TABLE} SET paused=0, updated_at=CURRENT_TIMESTAMP"
            )
            for torrent_id in torrent_ids:
                await db.execute(
                    f"""INSERT INTO {_INTENT_TABLE}
                           (torrent_id, paused, updated_at)
                       VALUES (?, 0, CURRENT_TIMESTAMP)
                       ON CONFLICT(torrent_id) DO UPDATE SET
                           paused=0, updated_at=CURRENT_TIMESTAMP""",
                    (int(torrent_id),),
                )
            await db.commit()

    # ---------- strict aria2 control ----------

    async def confirm_gid(self, gid: str, *, attempts: int = 3, delay: float = .12):
        gid = str(gid or "").strip()
        if not gid:
            return None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                return await self.manager.aria2().tell_status(gid)
            except Aria2ConnectionError:
                raise
            except Aria2RPCError as exc:
                if not _missing_gid_error(exc):
                    raise
                if attempt >= attempts:
                    return None
                await asyncio.sleep(delay * attempt)
        return None

    async def _require_owned_mutation(self, gid: str) -> str:
        normalized = str(gid or "").strip()
        if not normalized:
            raise ValueError("aria2 GID is required")
        if not is_builtin_mode() and normalized not in await self.manager._aria2_owned_gids():
            raise PermissionError(f"aria2 GID {normalized} is not owned by DebridPulse")
        return normalized

    async def _strict_pause_gid(self, gid: str):
        gid = await self._require_owned_mutation(gid)
        state = await self.confirm_gid(gid)
        if state is None:
            return None
        if state.status in {"paused", "complete"}:
            return state
        if state.status in {"removed", "error"}:
            return state
        if state.status not in {"active", "waiting"}:
            raise Aria2RPCError(
                f"aria2 GID {gid} cannot be paused from state {state.status}"
            )
        await self.manager.aria2()._call("aria2.pause", [gid])
        state = await self.confirm_gid(gid)
        if state is None:
            return None
        if state.status not in {"paused", "complete"}:
            raise Aria2RPCError(
                f"aria2 GID {gid} did not enter paused state "
                f"(reported {state.status})"
            )
        return state

    async def _strict_resume_gid(self, gid: str):
        gid = await self._require_owned_mutation(gid)
        state = await self.confirm_gid(gid)
        if state is None:
            return None
        if state.status in {"active", "waiting", "complete"}:
            return state
        if state.status in {"removed", "error"}:
            return state
        if state.status != "paused":
            raise Aria2RPCError(
                f"aria2 GID {gid} cannot be resumed from state {state.status}"
            )
        await self.manager.aria2()._call("aria2.unpause", [gid])
        state = await self.confirm_gid(gid)
        if state is None:
            return None
        if state.status not in {"active", "waiting", "complete"}:
            raise Aria2RPCError(
                f"aria2 GID {gid} did not resume (reported {state.status})"
            )
        return state

    async def _owned(self, downloads):
        if is_builtin_mode():
            return list(downloads)
        return await self.manager._aria2_owned_downloads(downloads)

    # ---------- item controls ----------

    async def pause_torrent(self, torrent_id: int):
        torrent_id = int(torrent_id)

        # Synchronous in-memory intent closes the magnet-materialization race
        # before the first await.
        self._pause_intents.add(torrent_id)
        try:
            await self.ensure_initialized()
            await self._set_intent(torrent_id, True)
            async with self.manager._aria2_state_lock:
                result = await self._pause_parent(torrent_id, strict=True)
        except ValueError:
            self._pause_intents.discard(torrent_id)
            await self._set_intent(torrent_id, False)
            raise

        await self.manager._log_event(
            torrent_id, "info", "Selective pause confirmed for aria2 transfer"
        )
        self._schedule_queue()
        return result

    async def _pause_parent(self, torrent_id: int, *, strict: bool) -> dict:
        failures: List[str] = []
        paused = completed = 0

        async with self._queue_lock:
            async with self.manager._aria2_dispatch_lock:
                async with get_db() as db:
                    parent = await db.fetchone(
                        "SELECT id, status FROM torrents WHERE id=?",
                        (torrent_id,),
                    )
                    if not parent:
                        raise ValueError("Transfer not found")
                    rows = await db.fetchall(
                        """SELECT id AS file_id, download_id, status
                             FROM download_files
                            WHERE torrent_id=? AND download_client='aria2'
                              AND blocked=0
                              AND status IN ('pending','queued','downloading')
                            ORDER BY id""",
                        (torrent_id,),
                    )

                for row in rows:
                    gid = str(row.get("download_id") or "").strip()
                    if not gid:
                        continue
                    try:
                        state = await self._strict_pause_gid(gid)
                        if state is None or state.status in {"removed", "error"}:
                            failures.append(
                                f"{gid}: "
                                + (
                                    "state could not be confirmed"
                                    if state is None
                                    else f"aria2 is {state.status}"
                                )
                            )
                            continue
                        desired = "completed" if state.status == "complete" else "paused"
                        completed += int(desired == "completed")
                        paused += int(desired == "paused")
                        async with get_db() as db:
                            await db.execute(
                                """UPDATE download_files
                                      SET status=?, updated_at=CURRENT_TIMESTAMP
                                    WHERE id=?""",
                                (desired, int(row["file_id"])),
                            )
                            await db.commit()
                    except Exception as exc:
                        failures.append(
                            f"{gid}: {sanitize_exception(exc, max_length=160)}"
                        )

                async with get_db() as db:
                    # Pending children have no daemon state to confirm and are
                    # always safe to park.
                    await db.execute(
                        """UPDATE download_files
                              SET status='paused', updated_at=CURRENT_TIMESTAMP
                            WHERE torrent_id=? AND download_client='aria2'
                              AND blocked=0 AND download_id IS NULL
                              AND status IN ('pending','queued','downloading')""",
                        (torrent_id,),
                    )
                    if not failures:
                        await db.execute(
                            """UPDATE torrents
                                  SET status='paused', updated_at=CURRENT_TIMESTAMP
                                WHERE id=?
                                  AND status NOT IN ('completed','deleted','error')""",
                            (torrent_id,),
                        )
                    await db.commit()

        if failures:
            message = (
                f"Pause intent saved, but {len(failures)} aria2 operation(s) "
                "were not confirmed"
            )
            logger.warning("%s for torrent %s: %s", message, torrent_id, "; ".join(failures))
            if strict:
                raise Aria2RPCError(message)
        return {"paused_gids": paused, "completed_gids": completed, "failed": len(failures)}

    async def resume_torrent(self, torrent_id: int):
        torrent_id = int(torrent_id)
        await self.ensure_initialized()
        if bool(get_settings().paused):
            raise ValueError(
                "Processing is globally paused; use Resume All before "
                "resuming an individual transfer"
            )

        await self._set_intent(torrent_id, False)
        async with self.manager._aria2_state_lock:
            result = await self._resume_parent(torrent_id)

        await self.manager._log_event(
            torrent_id, "info", "Selective pause cleared; transfer eligible to resume"
        )
        self._schedule_queue()
        return result

    async def _resume_parent(self, torrent_id: int) -> dict:
        failures: List[str] = []
        resumed = waiting = 0

        async with self._queue_lock:
            async with self.manager._aria2_dispatch_lock:
                current = await self.manager._engine_aria2_get_all()
                owned = await self._owned(current)
                by_gid = {str(item.gid): item for item in owned}
                limit = self.manager._aria2_slot_limit()
                in_flight = [
                    item for item in owned if item.status in {"active", "waiting"}
                ]
                available = max(0, limit - len(in_flight))

                async with get_db() as db:
                    parent = await db.fetchone(
                        """SELECT id, status, provider_status, alldebrid_id, source
                             FROM torrents WHERE id=?""",
                        (torrent_id,),
                    )
                    if not parent:
                        raise ValueError("Transfer not found")
                    rows = await db.fetchall(
                        """SELECT id AS file_id, download_id
                             FROM download_files
                            WHERE torrent_id=? AND download_client='aria2'
                              AND blocked=0 AND status='paused'
                            ORDER BY id""",
                        (torrent_id,),
                    )

                for row in rows:
                    file_id = int(row["file_id"])
                    gid = str(row.get("download_id") or "").strip()
                    if not gid:
                        async with get_db() as db:
                            await db.execute(
                                """UPDATE download_files
                                      SET status='pending', updated_at=CURRENT_TIMESTAMP
                                    WHERE id=?""",
                                (file_id,),
                            )
                            await db.commit()
                        continue

                    state = by_gid.get(gid)
                    if state is None:
                        try:
                            state = await self.confirm_gid(gid)
                        except Exception as exc:
                            failures.append(
                                f"{gid}: {sanitize_exception(exc, max_length=160)}"
                            )
                            continue

                    if state is None or state.status in {"removed", "error"}:
                        failures.append(
                            f"{gid}: "
                            + (
                                "state could not be confirmed"
                                if state is None
                                else f"aria2 is {state.status}"
                            )
                        )
                        continue
                    if state.status == "complete":
                        desired = "completed"
                    elif state.status == "active":
                        desired = "downloading"
                    elif state.status == "waiting":
                        desired = "queued"
                    elif state.status == "paused":
                        if available <= 0:
                            waiting += 1
                            continue
                        try:
                            state = await self._strict_resume_gid(gid)
                        except Exception as exc:
                            failures.append(
                                f"{gid}: {sanitize_exception(exc, max_length=160)}"
                            )
                            continue
                        if state is None or state.status in {"removed", "error"}:
                            failures.append(f"{gid}: resume could not be confirmed")
                            continue
                        if state.status == "complete":
                            desired = "completed"
                        elif state.status == "active":
                            desired = "downloading"
                            available -= 1
                            resumed += 1
                        else:
                            desired = "queued"
                            available -= 1
                            resumed += 1
                    else:
                        failures.append(f"{gid}: unexpected aria2 state {state.status}")
                        continue

                    async with get_db() as db:
                        await db.execute(
                            """UPDATE download_files
                                  SET status=?, updated_at=CURRENT_TIMESTAMP
                                WHERE id=?""",
                            (desired, file_id),
                        )
                        await db.commit()

                next_parent = "queued"
                if (
                    not rows
                    and str(parent.get("source") or "") != "direct_link"
                    and str(parent.get("provider_status") or "") == "ready"
                    and str(parent.get("alldebrid_id") or "").strip()
                ):
                    next_parent = "ready"

                async with get_db() as db:
                    await db.execute(
                        """UPDATE torrents
                              SET status=?, updated_at=CURRENT_TIMESTAMP
                            WHERE id=? AND status='paused'""",
                        (next_parent, torrent_id),
                    )
                    await db.commit()

        if failures:
            raise Aria2RPCError(
                "Resume intent saved, but "
                f"{len(failures)} aria2 operation(s) were not confirmed"
            )
        return {"resumed_gids": resumed, "waiting_for_slot": waiting, "failed": 0}

    # ---------- global controls ----------

    async def pause_all_downloads(self) -> dict:
        await self.ensure_initialized()
        if self.manager.download_client_name() != "aria2":
            return {"paused": 0, "failed": 0}

        async with get_db() as db:
            rows = await db.fetchall(
                """SELECT DISTINCT t.id
                     FROM torrents t
                     LEFT JOIN download_files f ON f.torrent_id=t.id
                    WHERE t.status IN ('queued','downloading')
                       OR (
                            f.download_client='aria2' AND f.blocked=0
                            AND f.status IN ('pending','queued','downloading')
                          )
                    ORDER BY t.id"""
            )

        paused = failed = 0
        async with self.manager._aria2_state_lock:
            for row in rows:
                try:
                    await self._pause_parent(int(row["id"]), strict=True)
                    paused += 1
                except Exception as exc:
                    failed += 1
                    logger.warning(
                        "Pause All could not confirm torrent %s: %s",
                        row["id"], sanitize_exception(exc, max_length=180)
                    )
        return {"paused": paused, "failed": failed}

    async def resume_all_downloads(self) -> dict:
        await self.ensure_initialized()
        async with self.manager._aria2_state_lock:
            async with get_db() as db:
                rows = await db.fetchall(
                    "SELECT id FROM torrents WHERE status='paused' ORDER BY id"
                )
            ids = [int(row["id"]) for row in rows]
            await self._set_many_resumed(ids)

            async with get_db() as db:
                await db.execute(
                    """UPDATE download_files
                          SET status='pending', updated_at=CURRENT_TIMESTAMP
                        WHERE status='paused' AND download_id IS NULL
                          AND download_client='aria2' AND blocked=0"""
                )
                await db.execute(
                    """UPDATE torrents
                          SET status=CASE
                                WHEN source!='direct_link'
                                 AND provider_status='ready'
                                 AND alldebrid_id IS NOT NULL
                                 AND alldebrid_id!=''
                                 AND NOT EXISTS (
                                     SELECT 1 FROM download_files f
                                      WHERE f.torrent_id=torrents.id AND f.blocked=0
                                 )
                                THEN 'ready'
                                ELSE 'queued'
                              END,
                              updated_at=CURRENT_TIMESTAMP
                        WHERE status='paused'"""
                )
                await db.commit()
        return {"resumed": 0, "queued": len(ids), "failed": 0}

    # ---------- queue / preparation ----------

    def _schedule_queue(self) -> None:
        task = self._queue_task
        if task is not None and not task.done():
            self._queue_trailing = True
            return

        async def runner() -> None:
            try:
                while True:
                    self._queue_trailing = False
                    await asyncio.sleep(0)
                    await self._queue_pass()
                    if not self._queue_trailing:
                        break
            except Exception as exc:
                logger.debug(
                    "Deferred queue advancement failed: %s",
                    sanitize_exception(exc, max_length=200),
                )

        task = asyncio.create_task(runner())
        self._queue_task = task

        def done(finished: asyncio.Task) -> None:
            if self._queue_task is finished:
                self._queue_task = None

        task.add_done_callback(done)

    async def advance_queue_locked(self) -> int:
        # Called while manager._aria2_state_lock may already be held.
        self._schedule_queue()
        return 0

    async def _queue_pass(self) -> None:
        await self.ensure_initialized()
        if (
            self.manager.download_client_name() != "aria2"
            or bool(get_settings().paused)
            or self.manager._disk_guard_active
        ):
            return
        await self._enforce_selective_pauses()
        await self._resume_unintended_paused()
        await self.dispatch_queue()
        await self.manager._schedule_ready_aria2_parents()

    async def _park_pending_intents(self) -> None:
        for torrent_id in tuple(self._pause_intents):
            async with get_db() as db:
                await db.execute(
                    """UPDATE download_files
                          SET status='paused', updated_at=CURRENT_TIMESTAMP
                        WHERE torrent_id=? AND download_client='aria2'
                          AND blocked=0 AND download_id IS NULL
                          AND status='pending'""",
                    (torrent_id,),
                )
                await db.execute(
                    """UPDATE torrents
                          SET status='paused', updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND status IN ('queued','downloading')""",
                    (torrent_id,),
                )
                await db.commit()

    async def _preserve_pending_sources(self) -> None:
        # For provider-backed magnet rows, v1.0.2 stored the provider file link
        # only in download_url and overwrote it with the generated CDN URL at
        # dispatch. Preserve it before the original dispatcher mutates the row.
        async with get_db() as db:
            await db.execute(
                """UPDATE download_files
                      SET source_url=download_url, updated_at=CURRENT_TIMESTAMP
                    WHERE id IN (
                        SELECT f.id
                          FROM download_files f
                          JOIN torrents t ON t.id=f.torrent_id
                         WHERE f.status='pending'
                           AND f.download_client='aria2'
                           AND f.blocked=0
                           AND t.source!='direct_link'
                           AND (f.source_url IS NULL OR f.source_url='')
                           AND f.download_url IS NOT NULL
                           AND f.download_url!=''
                    )"""
            )
            await db.commit()

    async def dispatch_queue(self, all_downloads=None):
        await self.ensure_initialized()
        if (
            self.manager.download_client_name() != "aria2"
            or bool(get_settings().paused)
            or self.manager._disk_guard_active
        ):
            return 0

        async with self._queue_lock:
            await self._park_pending_intents()
            await self._preserve_pending_sources()

            snapshot = (
                list(all_downloads)
                if all_downloads is not None
                else await self.manager._engine_aria2_get_all()
            )
            owned = await self._owned(snapshot)
            limit = self.manager._aria2_slot_limit()
            live = [
                item for item in owned if item.status in {"active", "waiting"}
            ]
            if len(live) > limit:
                logger.info(
                    "aria2 has %d live DebridPulse job(s) above limit %d; "
                    "grandfathering them without removing GIDs",
                    len(live), limit,
                )
                return 0

            result = await self._orig_dispatch(snapshot)

            if bool(get_settings().aria2_start_paused):
                async with get_db() as db:
                    paused_rows = await db.fetchall(
                        """SELECT DISTINCT t.id
                             FROM torrents t
                             JOIN download_files f ON f.torrent_id=t.id
                            WHERE t.status='paused'
                              AND f.download_client='aria2'
                              AND f.status='paused'"""
                    )
                for row in paused_rows:
                    await self._set_intent(int(row["id"]), True)
            return result

    async def _resume_unintended_paused(self) -> int:
        if bool(get_settings().paused):
            return 0
        resumed = 0
        async with self._queue_lock:
            current = await self.manager._engine_aria2_get_all()
            owned = await self._owned(current)
            limit = self.manager._aria2_slot_limit()
            live = [d for d in owned if d.status in {"active", "waiting"}]
            available = max(0, limit - len(live))
            by_gid = {str(d.gid): d for d in owned}

            async with get_db() as db:
                rows = await db.fetchall(
                    """SELECT f.id AS file_id, f.torrent_id, f.download_id
                         FROM download_files f
                         JOIN torrents t ON t.id=f.torrent_id
                        WHERE f.status='paused'
                          AND f.download_client='aria2' AND f.blocked=0
                          AND t.status NOT IN ('completed','deleted','error')
                        ORDER BY t.priority DESC, t.id ASC, f.id ASC"""
                )

            for row in rows:
                torrent_id = int(row["torrent_id"])
                if torrent_id in self._pause_intents:
                    continue
                file_id = int(row["file_id"])
                gid = str(row.get("download_id") or "").strip()
                if not gid:
                    async with get_db() as db:
                        await db.execute(
                            "UPDATE download_files SET status='pending' WHERE id=?",
                            (file_id,),
                        )
                        await db.commit()
                    continue
                state = by_gid.get(gid)
                if state is None:
                    try:
                        state = await self.confirm_gid(gid)
                    except Exception:
                        continue
                if state is None or state.status in {"removed", "error"}:
                    continue
                if state.status == "complete":
                    desired = "completed"
                elif state.status in {"active", "waiting"}:
                    desired = "downloading" if state.status == "active" else "queued"
                elif state.status == "paused":
                    if available <= 0:
                        continue
                    try:
                        state = await self._strict_resume_gid(gid)
                    except Exception:
                        continue
                    if state is None or state.status in {"removed", "error"}:
                        continue
                    if state.status == "complete":
                        desired = "completed"
                    else:
                        desired = "downloading" if state.status == "active" else "queued"
                        available -= 1
                        resumed += 1
                else:
                    continue

                async with get_db() as db:
                    await db.execute(
                        """UPDATE download_files
                              SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (desired, file_id),
                    )
                    await db.execute(
                        """UPDATE torrents
                              SET status=?, updated_at=CURRENT_TIMESTAMP
                            WHERE id=? AND status='paused'""",
                        (
                            "downloading" if desired == "downloading" else "queued",
                            torrent_id,
                        ),
                    )
                    await db.commit()
        return resumed

    async def _enforce_selective_pauses(self) -> None:
        for torrent_id in tuple(sorted(self._pause_intents)):
            try:
                await self._pause_parent(torrent_id, strict=False)
            except ValueError:
                await self._set_intent(torrent_id, False)
            except Exception as exc:
                logger.debug(
                    "Selective pause enforcement deferred for %s: %s",
                    torrent_id, sanitize_exception(exc, max_length=160)
                )

    def schedule_ready_parent(self, torrent_id: int, ad_id: str, name: str) -> bool:
        if int(torrent_id) in self._pause_intents:
            return False
        return self._orig_schedule_ready(torrent_id, ad_id, name)

    async def start_download(self, torrent_id: int, ad_id: str, name: str):
        await self.ensure_initialized()
        if int(torrent_id) in self._pause_intents:
            async with get_db() as db:
                await db.execute(
                    """UPDATE torrents SET status='paused', updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND status NOT IN ('completed','deleted','error')""",
                    (int(torrent_id),),
                )
                await db.commit()
            return
        return await self._orig_start(torrent_id, ad_id, name)

    async def download(self, torrent_id: int, ad_id: str, name: str):
        if int(torrent_id) in self._pause_intents:
            async with get_db() as db:
                await db.execute(
                    """UPDATE torrents SET status='paused', updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND status NOT IN ('completed','deleted','error')""",
                    (int(torrent_id),),
                )
                await db.commit()
            return
        try:
            return await self._orig_download(torrent_id, ad_id, name)
        finally:
            if int(torrent_id) in self._pause_intents:
                await self._pause_parent(int(torrent_id), strict=False)

    # ---------- serialized observation ----------

    async def sync_clients(self):
        await self.ensure_initialized()
        if self.manager.download_client_name() != "aria2":
            return await self._orig_sync_clients()

        globally_paused = bool(get_settings().paused)
        async with self.manager._aria2_state_lock:
            await self.manager.sync_aria2_downloads()
            if globally_paused:
                await self._enforce_global_pause()

        if globally_paused:
            return
        self._schedule_queue()
        try:
            await self.manager._cleanup_aria2_orphans()
        except Exception as exc:
            logger.debug(
                "aria2 orphan cleanup deferred: %s",
                sanitize_exception(exc, max_length=160),
            )

    async def _enforce_global_pause(self) -> None:
        async with get_db() as db:
            rows = await db.fetchall(
                """SELECT DISTINCT t.id
                     FROM torrents t
                     JOIN download_files f ON f.torrent_id=t.id
                    WHERE t.status NOT IN ('completed','deleted','error')
                      AND f.download_client='aria2' AND f.blocked=0
                      AND f.status IN ('pending','queued','downloading')
                    ORDER BY t.id"""
            )
        for row in rows:
            await self._pause_parent(int(row["id"]), strict=False)

    async def update_parent_progress(self, all_downloads=None):
        await self._orig_parent_progress(all_downloads)
        await self.ensure_initialized()
        if bool(get_settings().paused):
            return

        # Physically paused GIDs waiting for a free slot are queued work after
        # Resume intent is cleared. Do not present them as selectively paused.
        async with get_db() as db:
            rows = await db.fetchall(
                "SELECT id FROM torrents WHERE status='paused'"
            )
            changed = 0
            for row in rows:
                torrent_id = int(row["id"])
                if torrent_id in self._pause_intents:
                    continue
                await db.execute(
                    """UPDATE torrents
                          SET status='queued', updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND status='paused'""",
                    (torrent_id,),
                )
                changed += 1
            if changed:
                await db.commit()

    # ---------- lost-GID recovery ----------

    async def reset_for_redownload(self, torrent_id: int, reason: str):
        reason_cf = str(reason or "").casefold()
        if not any(marker in reason_cf for marker in _LOST_MARKERS):
            return await self._orig_reset(torrent_id, reason)

        torrent_id = int(torrent_id)
        await self.ensure_initialized()
        async with get_db() as db:
            rows = await db.fetchall(
                """SELECT id AS file_id, status, download_id, source_url
                     FROM download_files
                    WHERE torrent_id=? AND blocked=0 ORDER BY id""",
                (torrent_id,),
            )
        if not rows:
            return await self._orig_reset(torrent_id, reason)

        lost = []
        for row in rows:
            if str(row.get("status") or "") == "completed":
                continue
            gid = str(row.get("download_id") or "").strip()
            if not gid:
                if str(row.get("status") or "") not in {"pending", "paused"}:
                    lost.append(row)
                continue
            try:
                state = await self.confirm_gid(gid)
            except (Aria2ConnectionError, Aria2RPCError):
                # Operational failure is never disappearance evidence.
                self._lost_strikes.pop(torrent_id, None)
                return
            if state is None or state.status == "removed":
                lost.append(row)
            elif state.status == "complete":
                async with get_db() as db:
                    await db.execute(
                        """UPDATE download_files SET status='completed',
                                  updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (int(row["file_id"]),),
                    )
                    await db.commit()

        if not lost:
            self._lost_strikes.pop(torrent_id, None)
            logger.info(
                "Suppressed destructive reset for torrent %s; GIDs confirmed",
                torrent_id,
            )
            return

        # v1.0.3 preserves source_url before dispatch, so confirmed-lost files
        # can be redispatched individually while completed siblings remain.
        if all(str(row.get("source_url") or "").strip() for row in lost):
            paused = bool(get_settings().paused) or torrent_id in self._pause_intents
            async with get_db() as db:
                for row in lost:
                    source = str(row["source_url"]).strip()
                    await db.execute(
                        """UPDATE download_files
                              SET status=?, download_id=NULL, download_url=?,
                                  updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        ("paused" if paused else "pending", source, int(row["file_id"])),
                    )
                await db.execute(
                    """UPDATE torrents
                          SET status=?, error_message=NULL, updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND status NOT IN ('completed','deleted','error')""",
                    ("paused" if paused else "queued", torrent_id),
                )
                await db.execute(
                    """INSERT INTO events (torrent_id, level, message)
                       VALUES (?, 'warn', ?)""",
                    (
                        torrent_id,
                        f"{reason}; recovered {len(lost)} confirmed-lost aria2 "
                        "file(s) without rebuilding completed siblings",
                    ),
                )
                await db.commit()
            self._lost_strikes.pop(torrent_id, None)
            if not paused:
                self._schedule_queue()
            return

        # Legacy v1.0.2 active rows may not have retained source_url. Require
        # two separate confirmed-loss cycles before permitting the old whole-
        # parent rebuild; one control-transition snapshot can never destroy it.
        strikes = self._lost_strikes.get(torrent_id, 0) + 1
        self._lost_strikes[torrent_id] = strikes
        if strikes < 2:
            logger.warning(
                "Deferred legacy parent reset for torrent %s after first "
                "confirmed missing-GID cycle",
                torrent_id,
            )
            return
        self._lost_strikes.pop(torrent_id, None)
        return await self._orig_reset(torrent_id, reason)

    # ---------- low-level settings queue ----------

    async def control_aria2_gid(self, gid: str, action: str) -> dict:
        gid = str(gid or "").strip()
        if action not in {"pause", "resume", "remove"}:
            raise ValueError("Unsupported aria2 action")
        if not is_builtin_mode() and gid not in await self.manager._aria2_owned_gids():
            raise PermissionError(f"aria2 GID {gid} is not owned by DebridPulse")
        if action == "remove":
            mutated = await self.manager._remove_owned_aria2_gid(gid)
            return {"mutated": mutated, "result_preserved": not is_builtin_mode()}

        async with self.manager._aria2_state_lock:
            async with self._queue_lock:
                state = await self.confirm_gid(gid)
                if state is None:
                    return {"mutated": False, "reason": "aria2 GID no longer exists"}
                if state.status not in {"active", "waiting", "paused"}:
                    return {
                        "mutated": False,
                        "reason": f"aria2 GID is already {state.status}",
                    }
                state = (
                    await self._strict_pause_gid(gid)
                    if action == "pause"
                    else await self._strict_resume_gid(gid)
                )
        return {
            "mutated": state is not None,
            "status": getattr(state, "status", "missing") if state else "missing",
        }

    # ---------- recovery-loop hardening ----------

    def _install_recovery_guard(self) -> None:
        # Recovery is owned by ReconciliationService in v1.0.5.
        return None
