"""Application commands over the single universal lifecycle owner.

This module is usable with any registry. It knows no concrete integrations,
native job identifiers, response formats, or integration error codes.
"""
from __future__ import annotations

import asyncio
import errno
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

from services.event_bus import publish
from services.maintenance_gate import ApplicationMaintenanceGate
from transfers.contracts import Manifest
from transfers.errors import Category, Domain, NormalizedError, Stage, TransferError
from transfers.models import ExecutionState, TransferRequest, TransferState
from transfers.requests import (
    direct_link_collection_name, direct_link_filename, extract_hash,
    extract_hash_from_torrent, normalize_direct_links,
)
from transfers.storage import StorageDomain


class ApplicationService:
    def __init__(self, engine, *, configure=None, lifecycle=(), admins=None, pause_changed=None, capacity=None):
        self.engine = engine
        self.repository = engine.repository
        self._configure = configure
        self.lifecycle = tuple(lifecycle)
        self.admins = admins or {}
        self._admission = ApplicationMaintenanceGate()
        self.pause_changed = pause_changed
        self.capacity = capacity
        self.observability = None
        self.resolution_wakeup = asyncio.Event()
        self.integration_wakeup = asyncio.Event()
        self.execution_wakeup = asyncio.Event()
        self.execution_poll_interval = 2
        self.definitions = ()

    def notify_applicability_changed(self, _integration_id: str) -> None:
        """Wake canonical maintenance and route resolution after neutral fact changes."""
        self.resolution_wakeup.set()
        self.integration_wakeup.set()

    def application_storage_permitted(self) -> bool:
        capacity = self.capacity
        return capacity is None or bool(getattr(capacity, "application_storage_permitted", True))

    def download_storage_permitted(self) -> bool:
        capacity = self.capacity
        return capacity is None or bool(getattr(capacity, "download_work_permitted", True))

    def _require_application_storage(self) -> None:
        if self.capacity is not None and hasattr(self.capacity, "require_application_storage"):
            self.capacity.require_application_storage()

    def _record_application_storage_fault(self, exc: BaseException):
        if self.capacity is None or not hasattr(self.capacity, "report_application_exception"):
            return None
        return self.capacity.report_application_exception(exc)

    def _record_download_storage_fault(self, error: NormalizedError | None):
        """Feed neutral local-resource failures into the canonical storage owner."""
        if (
            self.capacity is None
            or not hasattr(self.capacity, "report_fault")
            or error is None
            or error.domain != Domain.LOCAL_RESOURCE
        ):
            return None
        category = error.category
        if category in {Category.DISK_FULL, Category.LOCAL_RESOURCE_EXHAUSTED, Category.DOWNLOAD_STORAGE_FULL}:
            code = errno.ENOSPC
        elif category == Category.QUOTA_EXCEEDED:
            code = getattr(errno, "EDQUOT", errno.ENOSPC)
        elif category == Category.DOWNLOAD_STORAGE_READ_ONLY:
            code = errno.EROFS
        elif category in {Category.LOCAL_IO_FAILURE, Category.DOWNLOAD_STORAGE_UNAVAILABLE}:
            code = errno.EIO
        elif category == Category.PERMISSION_DENIED:
            code = errno.EACCES
        elif category == Category.PATH_UNAVAILABLE:
            code = errno.ENOENT
        else:
            return None
        return self.capacity.report_fault(StorageDomain.DOWNLOAD, OSError(code, category.value))

    async def _contain_download_storage_faults(self, transfers) -> None:
        """Close dispatch and retain the same logical transfer after executor storage failure."""
        for transfer in transfers:
            for artifact in await self.repository.artifacts(transfer.id):
                if artifact.state != "queued" or artifact.error is None:
                    continue
                fault = self._record_download_storage_fault(artifact.error)
                if fault is None:
                    continue
                self.engine.dispatch_permitted = False
                # The universal retry policy already kept this artifact
                # nonterminal. Replace its generic LOCAL_RESOURCE diagnostic with
                # the stable download-storage semantic while preserving retry_at.
                if artifact.error.category != fault.error.category:
                    await self.repository.artifact_state(
                        artifact.id,
                        "queued",
                        error=fault.error,
                        retry_at=artifact.retry_at,
                    )

    @asynccontextmanager
    async def _storage_checked_admission(self, *, maintenance: bool):
        """Contain DB-backed work without consulting the failed database itself."""
        self._require_application_storage()
        admission = self._admission.maintenance() if maintenance else self._admission.operation()
        async with admission:
            self._require_application_storage()
            try:
                yield
            except Exception as exc:
                fault = self._record_application_storage_fault(exc)
                if fault is not None:
                    raise fault from exc
                raise

    def configuration_admission(self):
        return self._storage_checked_admission(maintenance=True)

    async def validate_configuration(self, previous, current):
        from integrations.configuration import normalize_settings
        previous = normalize_settings(previous, self.definitions)
        for definition in self.definitions:
            old = previous.integrations[definition.id].options
            new = current.integrations[definition.id].options
            if any(old.get(key) != new.get(key) for key in definition.ownership_fields):
                if await self.repository.has_integration_references(definition.id):
                    raise ValueError(f"Finish or remove existing {definition.name} resources before changing its connection")
        download_folder_changed = previous.download_folder != current.download_folder
        if download_folder_changed and await self.repository.has_integration_references():
            raise ValueError("Finish or remove existing resources before changing the download folder")
        # Only a Download Folder change is a candidate-save operation.  Runtime
        # recovery owns active-path re-probing, so a degraded current Download
        # Folder cannot block unrelated Settings changes.
        if (
            download_folder_changed
            and self.capacity is not None
            and hasattr(self.capacity, "require_download_path")
        ):
            await asyncio.to_thread(
                self.capacity.require_download_path,
                current.download_folder,
                apply_if_active=True,
            )

    async def deliver_events(self):
        if self.observability:
            await self.observability.deliver()

    async def _pause_changed(self):
        if self.pause_changed:
            self.pause_changed(await self.repository.globally_paused())

    async def check_resources(self):
        if self.capacity is None:
            result = {"enabled": False, "active": False}
        else:
            # Filesystem probes may block on a degraded remote mount. Keep them
            # off the event loop while retaining one synchronous canonical owner.
            result = await asyncio.to_thread(self.capacity.check)
        # Dispatch requires both safe durable application state and usable
        # download storage. This runtime gate is independent of global Pause.
        self.engine.dispatch_permitted = self.application_storage_permitted() and not result["active"]
        return result

    async def storage_health(self):
        """Return a fresh, SQLite-independent storage-health snapshot."""
        return await self.check_resources()

    def application_operation(self):
        return self._storage_checked_admission(maintenance=False)

    def database_wipe_admission(self):
        return self._storage_checked_admission(maintenance=True)

    def integration_admin(self, identity):
        try:
            return self.admins[identity]
        except KeyError:
            raise ValueError("Integration administration is unavailable") from None

    async def require(self, transfer_id):
        transfer = await self.repository.get(transfer_id)
        if transfer is None:
            raise KeyError(transfer_id)
        return transfer

    async def _publish(self, transfer_id):
        item = await self.repository.presentation(transfer_id)
        if item:
            await publish("torrent_updated", item)
        await publish("stats_changed", {})
        return item

    async def submit(self, requests, **options):
        async with self.application_operation():
            transfer = await self.engine.submit(tuple(requests), **options)
            self.resolution_wakeup.set()
            return await self._publish(transfer.id)

    async def submit_magnet(self, magnet, *, source="manual"):
        fingerprint = extract_hash(magnet)
        if not fingerprint or urlsplit(magnet).scheme != "magnet":
            raise ValueError("A valid BitTorrent magnet is required")
        name = parse_qs(urlsplit(magnet).query).get("dn", [fingerprint])[0]
        return await self.submit((TransferRequest("magnet", magnet, name=name, fingerprint=fingerprint),), name=name, source=source)

    async def submit_torrent(self, data, filename, *, source="manual_file"):
        fingerprint = extract_hash_from_torrent(data)
        if not fingerprint:
            raise ValueError("Invalid torrent metainfo")
        name = filename.rsplit(".", 1)[0]
        return await self.submit((TransferRequest("torrent", data, name=filename, fingerprint=fingerprint),), name=name, source=source)

    async def submit_links(self, links):
        urls = normalize_direct_links(links)
        requests = tuple(TransferRequest(urlsplit(url).scheme.lower(), url, name=direct_link_filename(url, index)) for index, url in enumerate(urls, 1))
        item = await self.submit(requests, name=direct_link_collection_name([], urls), source="direct_link", deduplicate=False)
        return {"ok": True, "id": item["id"], "torrent_id": item["id"], "accepted": len(urls), "items": [item], **item}


    async def submit_input(self, transfer_id, *, challenge_id, method, values):
        async with self.application_operation():
            challenge = await self.engine.submit_input(transfer_id, challenge_id, method, values)
            self.resolution_wakeup.set()
            self.execution_wakeup.set()
            await self._publish(transfer_id)
            return {"ok": True, "accepted": True, "id": transfer_id, "challenge_id": challenge.id}

    async def cancel(self, transfer_id):
        async with self.application_operation():
            await self.require(transfer_id)
            errors = await self.engine.cancel(transfer_id)
            await self._publish(transfer_id)
            return {
                "ok": not errors,
                "cancelled": True,
                "cleanup_errors": [error.as_dict() for error in errors],
            }

    async def pause(self, transfer_id):
        async with self.application_operation():
            await self.require(transfer_id)
            errors = await self.engine.pause(transfer_id)
            self.execution_wakeup.set()
            await self._publish(transfer_id)
            return self._control_result(errors)

    async def resume(self, transfer_id):
        async with self.application_operation():
            await self.require(transfer_id)
            errors = await self.engine.resume(transfer_id)
            self.resolution_wakeup.set()
            self.execution_wakeup.set()
            await self._pause_changed()
            await self._publish(transfer_id)
            return self._control_result(errors)

    @staticmethod
    def _control_result(errors):
        if errors:
            raise TransferError(errors[0])
        return {"ok": True}

    async def pause_all(self):
        async with self.application_operation():
            results = await self.engine.pause_all()
            await self._pause_changed()
            await publish("stats_changed", {})
            return {"ok": not any(results.values()), "paused": True, "count": len(results), "failed": sum(bool(errors) for errors in results.values())}

    async def resume_all(self):
        async with self.application_operation():
            results = await self.engine.resume_all()
            self.resolution_wakeup.set()
            self.execution_wakeup.set()
            await self._pause_changed()
            await publish("stats_changed", {})
            return {"ok": not any(results.values()), "paused": False, "count": len(results), "failed": sum(bool(errors) for errors in results.values())}

    async def retry(self, transfer_id):
        async with self.application_operation():
            transfer = await self.require(transfer_id)
            accepted = await self.engine.retry(transfer_id, reacquire=transfer.state in {TransferState.COMPLETED, TransferState.DELETED})
            if not accepted:
                raise TransferError(NormalizedError(Domain.RECONCILIATION, Category.RECOVERY_FAILED, Stage.RECONCILIATION))
            self.resolution_wakeup.set()
            self.execution_wakeup.set()
            return {"ok": True, **await self._publish(transfer_id)}

    async def delete(self, transfer_id, *, remote=True):
        async with self.application_operation():
            await self.require(transfer_id)
            await self.engine.delete(transfer_id, remote=remote)
            await self._publish(transfer_id)
            return {"ok": True}

    async def select_artifact(self, transfer_id, artifact_id, *, selected):
        async with self.application_operation():
            await self.engine.select_artifact(transfer_id, artifact_id, selected=selected)
            self.execution_wakeup.set()
            await self._publish(transfer_id)
            return {"ok": True, "file_id": artifact_id, "blocked": not selected}

    async def preview(self, transfer_id):
        await self.require(transfer_id)
        item = await self.repository.presentation(transfer_id, details=True)
        if item["files"]:
            return {"source": "local", "files": item["files"]}
        files = []
        for resource, _state, _pending in await self.repository.resources(transfer_id):
            provider = self.engine.registry.providers.get(resource.provider_id)
            if isinstance(provider, Manifest):
                entries = await provider.manifest(resource)
                files.extend({"filename": entry.relative_path or entry.name, "size_bytes": entry.expected_bytes} for entry in entries)
        return {"source": "provider", "files": files}

    async def resolve_pending(self):
        async with self.application_operation():
            # Materialization occurs inside the universal resolution cycle. A
            # download-storage guard therefore defers this cycle without failing
            # the logical transfer; recovery resumes the same durable request.
            if not self.download_storage_permitted():
                return
            await self.engine.resolve_pending()
            self.execution_wakeup.set()

    async def reconcile_executions(self):
        async with self.application_operation():
            # Execution reconciliation consumes the in-memory storage state; the
            # dedicated disk guard owns periodic recovery probes. This prevents a
            # fast executor loop from erasing a just-observed ENOSPC/EDQUOT/EROFS
            # transition before the bounded recovery cadence.
            self.engine.dispatch_permitted = self.application_storage_permitted() and self.download_storage_permitted()
            before = await self.repository.active()
            await self.engine.reconcile_executions()
            await self._contain_download_storage_faults(before)
            for transfer in before:
                await self._publish(transfer.id)

    async def process_postprocessors(self):
        async with self.application_operation():
            if not self.download_storage_permitted():
                return
            await self.engine.process_postprocessors()

    async def reconcile_inventory(self):
        async with self.application_operation():
            before = {item.id for item in await self.repository.active()}
            errors = await self.engine.reconcile_inventory()
            after = await self.repository.active()
            return {"imported": sum(item.id not in before for item in after), "updated": len(after), "errors": [error.as_dict() for error in errors]}

    async def recover(self):
        async with self.application_operation():
            report = await self.reconcile_inventory()
            await self.resolve_pending()
            await self.reconcile_executions()
            return {"ok": not report["errors"], **report}

    async def quiesce_for_database_wipe(self):
        # The maintenance admission owner has already drained all commands and
        # scheduler cycles, including in-flight provider submissions.
        result = await self.pause_all()
        if result["failed"]:
            raise RuntimeError("Could not confirm every owned execution is paused")
        checked = 0
        for attempt in await self.repository.live_executions():
            executor = self.engine.registry.executors.get(attempt.handle.executor_id)
            if executor is None:
                raise RuntimeError("An execution integration is unavailable")
            observation = await executor.observe(attempt.handle)
            if observation.state not in {ExecutionState.PAUSED, ExecutionState.SUCCEEDED, ExecutionState.FAILED, ExecutionState.CANCELLED, ExecutionState.ABSENT}:
                raise RuntimeError("An owned execution could not be confirmed idle")
            checked += 1
        return {"pause": result, "owned_checked": checked, "provider_operations_drained": True, "materialization_drained": True}

    async def release_database_wipe_quiescence(self):
        # Admission is released by the surrounding maintenance context.
        return None

    def configure(self):
        if self._configure:
            self._configure(self)

    async def start_integrations(self):
        for integration in self.lifecycle:
            await integration.start()

    async def stop_integrations(self):
        for integration in reversed(self.lifecycle):
            await integration.stop()

    async def maintain_integrations(self):
        async with self.application_operation():
            for integration in self.lifecycle:
                await integration.maintain()
