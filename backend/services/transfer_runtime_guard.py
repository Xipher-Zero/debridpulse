"""Final V1 transfer-boundary guards.

This layer deliberately wraps the validated transfer-integrity engine instead of
editing its manifest reconciliation algorithm. It closes boundaries that need
cross-operation authority:

* a historical completed row never blocks an explicit same-hash re-submit;
* Delete is terminal across provider work, materialization, direct-link
  preparation, aria2 dispatch, and finalization;
* distinct provider files must map to distinct sanitized local destinations;
* provider-issued download hostnames are resolved immediately before aria2
  dispatch and rejected when any current answer is non-public;
* an explicit operator request to delete from AllDebrid is honored independently
  of automatic ownership-based completion cleanup.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path, PurePosixPath

from core.config import get_settings
from db.database import get_db
from services.aria2_runtime import effective_rpc_config, is_builtin_mode
from services.downloader_egress_guard import downloader_egress_guard
from services.network_safety import (
    reject_non_public_resolution,
    validate_resolved_public_destination,
)
from services.manager_v2 import (
    DIRECT_LINK_SOURCE,
    READY_CODE,
    extract_hash,
    safe_name,
    safe_rel_path,
)
from services.transfer_integrity import (
    TransferIntegrityAria2Service,
    TransferIntegrityManager,
)

logger = logging.getLogger("debridpulse.transfer_runtime_guard")

_MATERIALIZATION_CONTEXT: ContextVar[tuple[int, str] | None] = ContextVar(
    "debridpulse_materialization_context", default=None
)
_PROVIDER_STATE_OWNER: ContextVar[object | None] = ContextVar(
    "debridpulse_provider_state_owner", default=None
)


class GuardedTransferIntegrityAria2Service(TransferIntegrityAria2Service):
    """Bind every owned aria2 connection to the guarded egress boundary."""

    async def ensure_download(self, uri: str, options=None, *args, **kwargs) -> str:
        # Keep the early resolution check as defense in depth, but do not rely
        # on it for connection authorization: aria2 would otherwise resolve the
        # hostname again later and re-open the DNS-rebinding race.
        validated = await validate_resolved_public_destination(uri)
        await downloader_egress_guard.ensure_started()
        guarded_options = dict(options or {})
        guarded_options.update(
            downloader_egress_guard.job_options(
                validated,
                external=not is_builtin_mode(),
            )
        )
        return await super().ensure_download(
            validated,
            guarded_options,
            *args,
            **kwargs,
        )


class GuardedTransferIntegrityManager(TransferIntegrityManager):
    """Transfer-integrity engine with explicit cross-operation lifecycle guards."""

    def __init__(self):
        super().__init__()
        self._transfer_lifecycle_locks: dict[int, asyncio.Lock] = {}
        self._provider_state_lock = asyncio.Lock()
        self._deleted_transfer_ids: set[int] = set()
        self._delete_events: dict[int, asyncio.Event] = {}
        self._transfer_background_tasks: dict[int, set[asyncio.Task]] = {}
        self._pending_provider_cleanup: dict[int, set[str]] = {}

    def aria2(self):
        if self._aria2 is None:
            cfg = get_settings()
            url, secret = effective_rpc_config(cfg)
            self._aria2 = GuardedTransferIntegrityAria2Service(
                url,
                secret,
                cfg.aria2_operation_timeout_seconds,
            )
        return self._aria2

    def _lifecycle_lock(self, torrent_id: int) -> asyncio.Lock:
        torrent_id = int(torrent_id)
        lock = self._transfer_lifecycle_locks.get(torrent_id)
        if lock is None:
            lock = asyncio.Lock()
            self._transfer_lifecycle_locks[torrent_id] = lock
        return lock

    def _delete_event(self, torrent_id: int) -> asyncio.Event:
        torrent_id = int(torrent_id)
        event = self._delete_events.get(torrent_id)
        if event is None:
            event = asyncio.Event()
            self._delete_events[torrent_id] = event
        return event

    def _delete_requested(self, torrent_id: int) -> bool:
        torrent_id = int(torrent_id)
        return (
            torrent_id in self._deleted_transfer_ids
            or self._delete_event(torrent_id).is_set()
        )

    def _begin_delete_intent(self, torrent_id: int) -> None:
        torrent_id = int(torrent_id)
        self._deleted_transfer_ids.add(torrent_id)
        self._delete_event(torrent_id).set()

    def _clear_delete_intent(self, torrent_id: int) -> None:
        torrent_id = int(torrent_id)
        self._deleted_transfer_ids.discard(torrent_id)
        self._delete_events[torrent_id] = asyncio.Event()

    @asynccontextmanager
    async def _provider_state_guard(self):
        if _PROVIDER_STATE_OWNER.get() is self:
            yield
            return
        async with self._provider_state_lock:
            token = _PROVIDER_STATE_OWNER.set(self)
            try:
                yield
            finally:
                _PROVIDER_STATE_OWNER.reset(token)

    def _register_transfer_background_task(self, torrent_id: int):
        task = asyncio.current_task()
        if task is None:
            return None
        torrent_id = int(torrent_id)
        self._transfer_background_tasks.setdefault(torrent_id, set()).add(task)
        return task

    def _unregister_transfer_background_task(
        self, torrent_id: int, task: asyncio.Task | None
    ) -> None:
        if task is None:
            return
        torrent_id = int(torrent_id)
        tasks = self._transfer_background_tasks.get(torrent_id)
        if not tasks:
            return
        tasks.discard(task)
        if not tasks:
            self._transfer_background_tasks.pop(torrent_id, None)

    async def _cancel_transfer_background_tasks(self, torrent_id: int) -> None:
        torrent_id = int(torrent_id)
        current = asyncio.current_task()
        tasks = [
            task
            for task in self._transfer_background_tasks.get(torrent_id, set())
            if task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _wait_for_delete_or_timeout(
        self, torrent_id: int, seconds: float
    ) -> bool:
        """Return True when Delete wins before the timeout expires."""
        if self._delete_requested(torrent_id):
            return True
        if seconds <= 0:
            return False
        try:
            await asyncio.wait_for(
                self._delete_event(torrent_id).wait(),
                timeout=float(seconds),
            )
            return True
        except TimeoutError:
            return self._delete_requested(torrent_id)

    async def _load_transfer_row(self, torrent_id: int):
        async with get_db() as db:
            return await db.fetchone(
                "SELECT id, hash, name, status, source, alldebrid_id "
                "FROM torrents WHERE id=?",
                (int(torrent_id),),
            )

    async def _completed_transfer_by_hash(self, hash_value: str):
        if not hash_value:
            return None
        async with get_db() as db:
            return await db.fetchone(
                "SELECT id, hash, name, status, source, alldebrid_id "
                "FROM torrents WHERE hash=? AND status='completed' LIMIT 1",
                (str(hash_value).lower(),),
            )

    async def _deleted_transfer_by_hash(self, hash_value: str):
        if not hash_value:
            return None
        async with get_db() as db:
            return await db.fetchone(
                "SELECT id, hash, status FROM torrents "
                "WHERE hash=? AND status='deleted' LIMIT 1",
                (str(hash_value).lower(),),
            )

    @staticmethod
    def _history_duplicate_payload(row) -> dict:
        return {
            "is_duplicate": True,
            "confidence": 1.0,
            "action": "warn",
            "reason": "same_infohash_completed_history",
            "matches": [
                {
                    "torrent_id": int(row["id"]),
                    "name": str(row["name"] or ""),
                    "status": "completed",
                    "hash": str(row["hash"] or ""),
                    "reason": "same_infohash",
                    "confidence": 1.0,
                }
            ],
        }

    async def _clear_historical_completion(self, torrent_id: int) -> None:
        async with get_db() as db:
            await db.execute(
                """UPDATE torrents
                   SET completed_at=NULL, progress=0, error_message=NULL,
                       polling_failures=0, updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status!='deleted'""",
                (int(torrent_id),),
            )
            await db.commit()

    async def _cleanup_provider_after_delete(
        self, torrent_id: int, ad_id: str
    ) -> bool:
        """Remove a provider object created after Delete already won."""
        torrent_id = int(torrent_id)
        ad_id = str(ad_id or "").strip()
        if not ad_id:
            return True
        try:
            deleted = bool(await self.ad().delete_magnet(ad_id))
        except Exception as exc:
            logger.warning(
                "Could not clean provider object %s created after delete of transfer %s: %s",
                ad_id,
                torrent_id,
                exc,
            )
            deleted = False
        if deleted:
            pending = self._pending_provider_cleanup.get(torrent_id)
            if pending:
                pending.discard(ad_id)
                if not pending:
                    self._pending_provider_cleanup.pop(torrent_id, None)
            return True
        self._pending_provider_cleanup.setdefault(torrent_id, set()).add(ad_id)
        return False

    async def _retry_pending_provider_cleanup(self, torrent_id: int) -> bool:
        torrent_id = int(torrent_id)
        pending = list(self._pending_provider_cleanup.get(torrent_id, set()))
        ok = True
        for ad_id in pending:
            if not await self._cleanup_provider_after_delete(torrent_id, ad_id):
                ok = False
        return ok

    async def _reacquire_completed_magnet(
        self,
        magnet: str,
        hash_value: str,
        source: str,
        history_row,
    ) -> dict:
        """Explicit re-submit revives completed history instead of treating it as possession."""
        if self.is_paused():
            result = await self._persist_deferred_magnet(magnet, hash_value, source)
            result["_duplicate"] = self._history_duplicate_payload(history_row)
            return result

        async with self._upload_sem:
            if self.is_paused():
                result = await self._persist_deferred_magnet(magnet, hash_value, source)
                result["_duplicate"] = self._history_duplicate_payload(history_row)
                return result
            provider_result = await self.ad().upload_magnet(magnet)

        ad_id = str(provider_result.get("id", ""))
        name = (
            provider_result.get("name")
            or provider_result.get("filename")
            or hash_value[:16]
        )
        normalized_hash = str(provider_result.get("hash") or hash_value).lower()
        row = await self._upsert(normalized_hash, magnet, name, ad_id, source)
        torrent_id = int(row["id"])
        await self._clear_historical_completion(torrent_id)
        row = {**row, "completed_at": None, "progress": 0, "error_message": None}
        row["_duplicate"] = self._history_duplicate_payload(history_row)

        if get_settings().discord_notify_added:
            await self.notify().send_added(name, source=source, alldebrid_id=ad_id)

        status_code = int(
            provider_result.get("statusCode")
            or provider_result.get("status_code")
            or 0
        )
        if status_code == READY_CODE:
            self._schedule_ready_parent_download(torrent_id, ad_id, str(name))
        return row

    async def add_magnet_direct(self, magnet: str, source: str = "manual") -> dict:
        hash_value = extract_hash(magnet)
        if not hash_value:
            raise ValueError("Invalid magnet: no btih hash found")

        deleted_row = await self._deleted_transfer_by_hash(hash_value)
        if deleted_row is not None:
            self._clear_delete_intent(int(deleted_row["id"]))

        history_row = await self._completed_transfer_by_hash(hash_value)
        if history_row is not None:
            logger.info(
                "Explicit same-hash re-submit reviving completed transfer %s",
                history_row["id"],
            )
            return await self._reacquire_completed_magnet(
                magnet,
                hash_value,
                source,
                history_row,
            )
        return await super().add_magnet_direct(magnet, source=source)

    async def add_torrent_file_direct(
        self,
        file_bytes: bytes,
        filename: str,
        source: str = "manual",
        preferred_hash: str | None = None,
    ) -> dict:
        if not file_bytes or not get_settings().alldebrid_api_key:
            return await super().add_torrent_file_direct(
                file_bytes,
                filename,
                source=source,
                preferred_hash=preferred_hash,
            )

        local_hash = str(preferred_hash or "").strip().lower()
        if not local_hash:
            try:
                from transfers.requests import extract_hash_from_torrent

                local_hash = str(extract_hash_from_torrent(file_bytes) or "").lower()
            except Exception:
                local_hash = ""

        deleted_row = await self._deleted_transfer_by_hash(local_hash)
        if deleted_row is not None:
            self._clear_delete_intent(int(deleted_row["id"]))

        history_row = await self._completed_transfer_by_hash(local_hash)
        if history_row is None:
            return await super().add_torrent_file_direct(
                file_bytes,
                filename,
                source=source,
                preferred_hash=preferred_hash,
            )

        logger.info(
            "Explicit same-hash .torrent re-submit reviving completed transfer %s",
            history_row["id"],
        )
        row = await self._upload_torrent_file_provider(
            file_bytes,
            filename,
            source,
            local_hash,
        )
        if row.get("id"):
            await self._clear_historical_completion(int(row["id"]))
            row = {
                **row,
                "completed_at": None,
                "progress": 0,
                "error_message": None,
            }
        row["_duplicate"] = self._history_duplicate_payload(history_row)
        return row

    @staticmethod
    def _validate_manifest_destinations(name: str, flat_files: list[dict]) -> None:
        """Require a one-to-one mapping from required provider files to local paths."""
        cfg = get_settings()
        destination_root = Path(cfg.download_folder) / safe_name(name)
        torrent_root = safe_name(name)
        claimed: dict[str, tuple[str, str, int]] = {}

        for file_info in flat_files:
            relative_path = (
                file_info.get("path")
                or file_info.get("name")
                or "download.bin"
            )
            display_name = str(
                PurePosixPath(str(relative_path).replace("\\", "/"))
            )
            file_size = int(file_info.get("size", 0) or 0)

            relative_target = safe_rel_path(display_name)
            if relative_target.parts and relative_target.parts[0] == torrent_root:
                remaining = relative_target.parts[1:]
                relative_target = (
                    Path(*remaining) if remaining else Path("download.bin")
                )
            local_path = destination_root / relative_target
            path_key = str(
                PurePosixPath(str(local_path).replace("\\", "/"))
            )
            identity = (
                display_name,
                str(file_info.get("link") or "").strip(),
                file_size,
            )
            previous = claimed.get(path_key)
            if previous is not None and previous != identity:
                raise ValueError(
                    "Provider manifest path collision after sanitization: "
                    f"{previous[0]!r} and {display_name!r} both map to {path_key!r}"
                )
            claimed[path_key] = identity

    async def _fetch_ready_files(self, ad_id: str):
        flat_files = await super()._fetch_ready_files(ad_id)
        context = _MATERIALIZATION_CONTEXT.get()
        if context is not None:
            self._validate_manifest_destinations(context[1], flat_files)
        return flat_files

    async def _prepare_direct_link_collection(
        self, torrent_id: int, links: list[str]
    ) -> None:
        """Track the dedicated preparation task so Delete can cancel it."""
        torrent_id = int(torrent_id)
        if self._delete_requested(torrent_id):
            return
        task = self._register_transfer_background_task(torrent_id)
        try:
            if self._delete_requested(torrent_id):
                return
            await super()._prepare_direct_link_collection(torrent_id, links)
        finally:
            self._unregister_transfer_background_task(torrent_id, task)

    async def _engine_start_download(
        self, torrent_id: int, ad_id: str, name: str
    ):
        """Track provider-ready worker tasks and refuse work after Delete intent."""
        torrent_id = int(torrent_id)
        if self._delete_requested(torrent_id):
            return
        task = self._register_transfer_background_task(torrent_id)
        try:
            if self._delete_requested(torrent_id):
                return
            return await super()._engine_start_download(torrent_id, ad_id, name)
        finally:
            self._unregister_transfer_background_task(torrent_id, task)

    async def _engine_download(self, torrent_id: int, ad_id: str, name: str):
        """Serialize materialization against Delete without changing engine ordering."""
        torrent_id = int(torrent_id)
        async with self._lifecycle_lock(torrent_id):
            if self._delete_requested(torrent_id):
                return
            row = await self._load_transfer_row(torrent_id)
            if row is None or str(row["status"] or "") == "deleted":
                return

            token = _MATERIALIZATION_CONTEXT.set((torrent_id, str(name or "")))
            try:
                return await super()._engine_download(torrent_id, ad_id, name)
            finally:
                _MATERIALIZATION_CONTEXT.reset(token)

    async def _finalize_aria2_torrent(self, torrent_id: int):
        torrent_id = int(torrent_id)
        async with self._lifecycle_lock(torrent_id):
            if self._delete_requested(torrent_id):
                return
            row = await self._load_transfer_row(torrent_id)
            if row is None or str(row["status"] or "") == "deleted":
                return
            return await super()._finalize_aria2_torrent(torrent_id)

    async def _fail_torrent(
        self, torrent_id: int, message: str, notify: bool = False
    ):
        torrent_id = int(torrent_id)
        async with self._lifecycle_lock(torrent_id):
            if self._delete_requested(torrent_id):
                return
            row = await self._load_transfer_row(torrent_id)
            if row is None or str(row["status"] or "") == "deleted":
                return
            return await super()._fail_torrent(torrent_id, message, notify=notify)

    async def _set_provider_missing(self, torrent_id: int, message: str):
        torrent_id = int(torrent_id)
        async with self._lifecycle_lock(torrent_id):
            if self._delete_requested(torrent_id):
                return
            return await super()._set_provider_missing(torrent_id, message)

    async def _handle_expired_reimport(self, row: dict, magnet_link: str) -> None:
        """Reimport an expired magnet only while the transfer remains live."""
        torrent_id = int(row.get("id") or 0)
        name = str(row.get("name") or f"torrent {torrent_id}")
        if self._delete_requested(torrent_id):
            return

        new_ad_id = ""
        try:
            async with get_db() as db:
                current = await db.fetchone(
                    "SELECT status, alldebrid_id FROM torrents WHERE id=?",
                    (torrent_id,),
                )
            if (
                not current
                or current["status"] != "pending"
                or current.get("alldebrid_id")
                or self._delete_requested(torrent_id)
            ):
                logger.debug(
                    "expired_reimport: torrent %s no longer pending — skip",
                    torrent_id,
                )
                return

            async with self._upload_sem:
                if self._delete_requested(torrent_id):
                    return
                result = await self.ad().upload_magnet(magnet_link)

            new_ad_id = str(result.get("id", ""))
            new_hash = str(
                result.get("hash", row.get("hash", "")) or ""
            ).lower()
            if not new_ad_id:
                raise ValueError("AllDebrid returned no ID for re-uploaded magnet")

            if self._delete_requested(torrent_id):
                await self._cleanup_provider_after_delete(torrent_id, new_ad_id)
                return

            async with get_db() as db:
                update = await db.execute(
                    """UPDATE torrents
                          SET alldebrid_id = ?,
                              hash = COALESCE(NULLIF(?, ''), hash),
                              status = 'uploading',
                              error_message = NULL,
                              provider_status = NULL,
                              provider_status_code = NULL,
                              updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND status != 'deleted'""",
                    (new_ad_id, new_hash, torrent_id),
                )
                await db.commit()

            if update.rowcount == 0 or self._delete_requested(torrent_id):
                await self._cleanup_provider_after_delete(torrent_id, new_ad_id)
                return

            logger.info(
                "expired_reimport: torrent %s '%s' re-uploaded → new ad_id=%s",
                torrent_id,
                name[:60],
                new_ad_id,
            )
        except Exception as exc:
            if self._delete_requested(torrent_id):
                if new_ad_id:
                    await self._cleanup_provider_after_delete(torrent_id, new_ad_id)
                return
            logger.error(
                "expired_reimport: torrent %s '%s' re-upload failed: %s",
                torrent_id,
                name[:60],
                exc,
            )
            async with get_db() as db:
                await db.execute(
                    """UPDATE torrents
                          SET status = 'error',
                              error_message = 'Expired reimport failed: ' || ?,
                              updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND status != 'deleted'""",
                    (str(exc)[:200], torrent_id),
                )
                await db.commit()

    async def _handle_upload_failed(self, row: dict, error_message: str) -> None:
        """Retry upload failure without allowing a delayed retry to outrun Delete."""
        cfg = get_settings()
        max_retries = max(
            0, int(getattr(cfg, "upload_fail_retry_count", 3) or 3)
        )
        delay_minutes = max(
            0, int(getattr(cfg, "upload_fail_retry_delay_minutes", 5) or 5)
        )

        torrent_id = int(row["id"])
        name = str(row["name"] or f"torrent {torrent_id}")
        ad_id = str(row.get("alldebrid_id") or "")
        magnet_link = str(row.get("magnet") or "")
        source = str(row.get("source") or "manual")

        if self._delete_requested(torrent_id):
            return

        async with get_db() as db:
            retry_row = await (
                await db.execute(
                    "SELECT upload_retry_count, status FROM torrents WHERE id=?",
                    (torrent_id,),
                )
            ).fetchone()
        if (
            not retry_row
            or str(retry_row.get("status") or "") == "deleted"
            or self._delete_requested(torrent_id)
        ):
            return

        current_retry = int(retry_row.get("upload_retry_count") or 0)
        attempt = current_retry + 1

        logger.warning(
            "Upload failed (code 5) for torrent %s (id=%s) — attempt %s/%s",
            name,
            torrent_id,
            attempt,
            max_retries,
        )

        if attempt > max_retries or not magnet_link:
            msg = (
                f"Upload failed permanently after {max_retries} retries"
                if attempt > max_retries
                else "Upload failed: no magnet link stored for re-upload"
            )
            logger.error(
                "Upload failed permanently for torrent %s (source=%s): %s",
                torrent_id,
                source,
                msg,
            )
            if self._delete_requested(torrent_id):
                return
            await self._log_event(
                torrent_id,
                "error",
                "Upload failed permanently "
                f"(code 5, {attempt-1} retries exhausted): {error_message}",
            )
            async with get_db() as db:
                await db.execute(
                    """UPDATE torrents
                       SET alldebrid_id=NULL, upload_retry_count=0,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status!='deleted'""",
                    (torrent_id,),
                )
                await db.commit()
            await self._fail_torrent(torrent_id, msg, notify=False)
            if (
                not self._delete_requested(torrent_id)
                and getattr(cfg, "discord_notify_error", False)
            ):
                await self.notify().send_upload_failed_permanent(
                    name,
                    max_attempts=max_retries,
                    reason=error_message,
                    alldebrid_id=ad_id,
                )
            return

        async with get_db() as db:
            update = await db.execute(
                """UPDATE torrents
                   SET upload_retry_count=?, status='uploading',
                       provider_status='queued', provider_status_code=NULL,
                       error_message=NULL, updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status!='deleted'""",
                (attempt, torrent_id),
            )
            if update.rowcount:
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) "
                    "VALUES (?, ?, ?)",
                    (
                        torrent_id,
                        "warn",
                        "Upload failed (code 5) — scheduling retry "
                        f"{attempt}/{max_retries} in {delay_minutes} min",
                    ),
                )
            await db.commit()

        if update.rowcount == 0 or self._delete_requested(torrent_id):
            return

        if getattr(cfg, "discord_notify_error", False):
            await self.notify().send_requeue(
                name,
                attempt=attempt,
                max_attempts=max_retries,
                reason=error_message,
                alldebrid_id=ad_id,
            )

        if ad_id and self._provider_delete_authorized(row.get("source")):
            try:
                removed_old = bool(await self.ad().delete_magnet(ad_id))
                if removed_old:
                    async with get_db() as db:
                        await db.execute(
                            """UPDATE torrents
                               SET alldebrid_id=NULL, updated_at=CURRENT_TIMESTAMP
                               WHERE id=? AND status!='deleted' AND alldebrid_id=?""",
                            (torrent_id, ad_id),
                        )
                        await db.commit()
                    logger.info(
                        "Deleted failed magnet %s from AllDebrid before re-upload",
                        ad_id,
                    )
                else:
                    logger.warning(
                        "Could not confirm deletion of failed magnet %s before re-upload",
                        ad_id,
                    )
            except Exception as exc:
                logger.debug(
                    "Could not delete failed magnet %s: %s",
                    ad_id,
                    exc,
                )

        if self._delete_requested(torrent_id):
            return

        if delay_minutes > 0:
            logger.info(
                "Waiting %s min before re-uploading torrent %s",
                delay_minutes,
                torrent_id,
            )
            if await self._wait_for_delete_or_timeout(
                torrent_id, delay_minutes * 60
            ):
                return

        new_ad_id = ""
        try:
            async with self._upload_sem:
                if self._delete_requested(torrent_id):
                    return
                result = await self.ad().upload_magnet(magnet_link)

            new_ad_id = str(result.get("id", ""))
            new_name = result.get("name") or result.get("filename") or name
            if self._delete_requested(torrent_id):
                await self._cleanup_provider_after_delete(torrent_id, new_ad_id)
                return

            logger.info(
                "Re-upload successful for torrent %s: new ad_id=%s",
                torrent_id,
                new_ad_id,
            )
            async with get_db() as db:
                update = await db.execute(
                    """UPDATE torrents
                       SET alldebrid_id=?, name=?, status='uploading',
                           provider_status='queued', provider_status_code=NULL,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status!='deleted'""",
                    (new_ad_id, new_name, torrent_id),
                )
                if update.rowcount:
                    await db.execute(
                        "INSERT INTO events (torrent_id, level, message) "
                        "VALUES (?, ?, ?)",
                        (
                            torrent_id,
                            "info",
                            "Re-upload attempt "
                            f"{attempt}/{max_retries} succeeded "
                            f"(new ad_id={new_ad_id})",
                        ),
                    )
                await db.commit()

            if update.rowcount == 0 or self._delete_requested(torrent_id):
                await self._cleanup_provider_after_delete(torrent_id, new_ad_id)
        except Exception as exc:
            if self._delete_requested(torrent_id):
                if new_ad_id:
                    await self._cleanup_provider_after_delete(
                        torrent_id, new_ad_id
                    )
                return
            logger.error(
                "Re-upload attempt %s failed for torrent %s: %s",
                attempt,
                torrent_id,
                exc,
            )
            async with get_db() as db:
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) "
                    "VALUES (?, ?, ?)",
                    (
                        torrent_id,
                        "error",
                        f"Re-upload attempt {attempt} failed: {exc}",
                    ),
                )
                await db.commit()

    async def sync_alldebrid_status(self):
        async with self._provider_state_guard():
            return await super().sync_alldebrid_status()

    async def reconcile_provider_inventory(self) -> dict:
        async with self._provider_state_guard():
            return await super().reconcile_provider_inventory()

    async def import_existing_magnets(self, all_magnets=None):
        async with self._provider_state_guard():
            result = await super().import_existing_magnets(all_magnets=all_magnets)
            for torrent_id in tuple(self._deleted_transfer_ids):
                async with get_db() as db:
                    await db.execute(
                        "UPDATE torrents SET status='deleted', "
                        "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (torrent_id,),
                    )
                    await db.commit()
            return result

    async def full_alldebrid_sync(self, all_magnets=None):
        async with self._provider_state_guard():
            return await super().full_alldebrid_sync(all_magnets=all_magnets)

    async def cleanup_no_peer_errors(self):
        async with self._provider_state_guard():
            return await super().cleanup_no_peer_errors()

    async def cleanup_alldebrid_orphans(self):
        async with self._provider_state_guard():
            return await super().cleanup_alldebrid_orphans()

    async def cleanup_stuck_downloads(self):
        async with self._provider_state_guard():
            return await super().cleanup_stuck_downloads()

    async def delete_torrent(
        self, torrent_id: int, delete_from_ad: bool = True
    ):
        """Make explicit operator deletion the final authority for this transfer."""
        torrent_id = int(torrent_id)
        self._begin_delete_intent(torrent_id)

        try:
            await self._cancel_transfer_background_tasks(torrent_id)

            async with self._provider_state_guard():
                async with self._lifecycle_lock(torrent_id):
                    # The dispatch lock is the mutation boundary needed here.
                    # Do not also acquire _aria2_state_lock: reconciliation owns
                    # that lock while finalization may enter the lifecycle guard,
                    # so taking lifecycle -> state here would create an AB/BA
                    # deadlock.  Holding dispatch prevents a pending child from
                    # acquiring a new GID while base Delete snapshots/removes all
                    # currently recorded DebridPulse-owned GIDs.
                    async with self._aria2_dispatch_lock:
                        row = await self._load_transfer_row(torrent_id)
                        if row is None:
                            raise ValueError("Torrent not found")

                        cleanup_ok = await self._retry_pending_provider_cleanup(
                            torrent_id
                        )
                        if delete_from_ad and not cleanup_ok:
                            raise RuntimeError(
                                "Provider cleanup created during Delete "
                                f"was not confirmed for transfer {torrent_id}"
                            )

                        ad_id = str(row["alldebrid_id"] or "").strip()
                        source = str(row["source"] or "").strip()
                        status = str(row["status"] or "").strip()
                        if (
                            delete_from_ad
                            and ad_id
                            and source != DIRECT_LINK_SOURCE
                        ):
                            deleted = await self.ad().delete_magnet(ad_id)
                            already_cleaned_owned_completion = (
                                status == "completed"
                                and self._provider_delete_authorized(source)
                            )
                            if (
                                not deleted
                                and not already_cleaned_owned_completion
                            ):
                                raise RuntimeError(
                                    "AllDebrid deletion was not confirmed "
                                    f"for transfer {torrent_id}"
                                )

                        result = await super().delete_torrent(
                            torrent_id,
                            delete_from_ad=False,
                        )
                        await self._retry_pending_provider_cleanup(torrent_id)
                        return result
        except BaseException:
            self._clear_delete_intent(torrent_id)
            raise


manager = GuardedTransferIntegrityManager()
