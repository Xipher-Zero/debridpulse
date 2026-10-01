"""Application commands over the single universal lifecycle owner.

This module is usable with any registry. It knows no concrete integrations,
native job identifiers, response formats, or integration error codes.
"""
from __future__ import annotations

import asyncio
import errno
import logging
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

from application import dispatch_admission
from services.event_bus import publish
from services.maintenance_gate import ApplicationMaintenanceGate
from transfers import file_selection
from transfers.contracts import Manifest
from transfers.errors import Category, Domain, NormalizedError, Stage, TransferError
from transfers.models import TransferRequest, TransferState
from transfers.requests import (
    direct_link_collection_name, direct_link_filename, extract_hash,
    direct_link_host, extract_hash_from_torrent, normalize_direct_links,
)
from services.network_safety import names_private_lan
from transfers.staged_input import StagedInputError
from transfers.storage import StorageDomain





logger = logging.getLogger("debridpulse.application")
class LocalNetworkConfirmationRequired(Exception):
    """A submission names private-LAN hosts and the operator has not allowed
    this submission to reach them yet. Nothing was admitted."""

    def __init__(self, hosts: tuple[str, ...]):
        super().__init__("local network connection requires confirmation")
        self.hosts = tuple(hosts)

class IntegrationStopFailed(RuntimeError):
    """An integration failed to stop; ``stopped`` were stopped before it."""

    def __init__(self, stopped):
        super().__init__("an integration failed to stop")
        self.stopped = tuple(stopped)


class ApplicationService:
    def __init__(self, engine, *, configure=None, lifecycle=(), admins=None, capacity=None,
                 staged_input=None):
        self.engine = engine
        # The one owner of durable, large submitted request input. Held here
        # because it is an application resource, not a provider's or an
        # executor's: the edge stages into it, both of them only read from it,
        # and its reclamation is driven from application maintenance.
        self.staged_input = staged_input
        self.repository = engine.repository
        self._configure = configure
        self.lifecycle = tuple(lifecycle)
        self.admins = admins or {}
        # Canonical-namespace -> integration-owned configuration appliers,
        # discovered generically by composition.
        self.configuration_appliers = {}
        self._admission = ApplicationMaintenanceGate()
        self.capacity = capacity
        self.observability = None
        self.resolution_wakeup = asyncio.Event()
        self.integration_wakeup = asyncio.Event()
        self.execution_wakeup = asyncio.Event()
        self.execution_poll_interval = 1
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
                if await self.repository.has_integration_references(definition.owned_identities):
                    raise ValueError(f"Finish or remove existing {definition.name} resources before changing its connection")
        download_folder_changed = previous.download_folder != current.download_folder
        if download_folder_changed and await self.repository.has_integration_references():
            raise ValueError("Finish or remove existing resources before changing the download folder")
        # Only a Download Folder change is a candidate-save operation. Runtime
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

    def state_replacement_admission(self):
        return self._storage_checked_admission(maintenance=True)

    async def execution_runtime_limits(self) -> dict:
        """Neutral configured/effective global runtime limits, converged by the
        core runtime owner; ``effective`` is what is proven enforced across the
        reserved executor set, ``None`` when it cannot be proven."""
        status = await self.engine.converge_runtime_limits()
        return {
            "ok": status.ok,
            "configured": {"max_download_bytes_per_second": status.configured},
            "effective": {"max_download_bytes_per_second": status.effective},
            "last_apply_error": status.last_apply_error,
        }

    async def execution_throughput(self) -> dict:
        """The volatile speed facts alone, read from memory.

        ``download_bytes_per_second`` is the core throughput meter (sampled
        at presentation cadence by its owner) and ``max_download_bytes_per_second``
        the configured runtime limit. Neither awaits anything, so the speed an
        operator sees is never serialized behind a slower repository-backed
        runtime fact (``execution_runtime_status``'s occupancy)."""
        engine = self.engine
        return {
            "download_bytes_per_second": int(engine.throughput.current()),
            "max_download_bytes_per_second": int(engine.runtime.configured),
        }

    async def execution_runtime_status(self) -> dict:
        """The neutral live runtime facts the operator-facing shell consumes.

        Every value is a read PROJECTION of exactly one existing owner -- none
        of them is a second authority, and none of them is executor-specific:

        * ``download_bytes_per_second`` -- the core throughput meter, which
          aggregates every currently acquiring executor through one counting
          rule and settles to 0 when nothing is measurable;
        * ``active_execution_slots`` -- the canonical execution-admission
          occupancy, never a native queue count;
        * ``max_download_bytes_per_second`` -- the configured value held by the
          core runtime-limit owner that ``PATCH /execution/runtime-limits``
          writes.
        """
        engine = self.engine
        return {
            "download_bytes_per_second": int(engine.throughput.current()),
            "active_execution_slots": int(await self.repository.occupied_execution_slots(engine.clock())),
            "max_download_bytes_per_second": int(engine.runtime.configured),
        }

    async def apply_integration_configuration(self, namespace: str):
        """Drive the integration-owned application of one canonical namespace.

        The ONE path from a persisted configuration mutation to the native
        integration service's own configuration transaction. Such a service is
        external to the transfer core's implementation, yet inside DebridPulse
        product ownership -- DebridPulse may run it, own its whole
        configuration and never expose it -- so it is named for what it is
        rather than as somebody else's endpoint. Returns ``None`` when the
        namespace has no applier, so callers can stay integration-neutral.
        """
        appliers = (self.configuration_appliers or {}).get(str(namespace)) or ()
        if not appliers:
            return None
        from integrations.definition import ConfigurationApplication
        results = [await applier.apply_configuration() for applier in appliers]
        failed = [item for item in results if not item.ok]
        if not failed:
            return ConfigurationApplication(True, "configuration applied")
        return ConfigurationApplication(
            False, "; ".join(item.detail for item in failed if item.detail),
            tuple(name for item in failed for name in item.failures))

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
        item = await self.repository.presentation(
            transfer_id,
            capacity_only_blocked_ids=dispatch_admission.capacity_only_blocked_ids(self.engine),
        )
        if item:
            await publish("torrent_updated", item)
        await publish("stats_changed", {})
        return item

    @staticmethod
    def _active_overlay_item(transfer, *, status_changed=False):
        """Project only mutable list fields needed by live browser updates."""
        state = getattr(transfer.state, "value", transfer.state)
        return {
            "id": int(transfer.id),
            "status": str(state),
            "progress": None if transfer.progress is None else float(transfer.progress),
            # In-flight execution activity, never completion (``Transfer``).
            "active_execution_progress": (None if transfer.active_execution_progress is None
                                          else float(transfer.active_execution_progress)),
            "status_changed": bool(status_changed),
        }

    async def submit(self, requests, **options):
        async with self.application_operation():
            transfer = await self.engine.submit(tuple(requests), **options)
            self.resolution_wakeup.set()
            return await self._publish(transfer.id)

    @staticmethod
    def magnet_request(magnet, *, selection_mode="all") -> TransferRequest:
        """The request one magnet submits, or ``ValueError``: the magnet
        owner's whole admissibility rule, usable before anything is admitted."""
        selection_mode = file_selection.normalize_selection_mode(selection_mode)
        fingerprint = extract_hash(magnet)
        if not fingerprint or urlsplit(magnet).scheme != "magnet":
            raise ValueError("A valid BitTorrent magnet is required")
        name = parse_qs(urlsplit(magnet).query).get("dn", [fingerprint])[0]
        return TransferRequest("magnet", magnet, name=name, fingerprint=fingerprint,
                               selection_mode=selection_mode)

    async def submit_magnet(self, magnet, *, source="manual", selection_mode="all"):
        request = self.magnet_request(magnet, selection_mode=selection_mode)
        return await self.submit((request,), name=request.name, source=source)

    async def submit_torrent(self, data, filename, *, source="manual_file", selection_mode="all"):
        selection_mode = file_selection.normalize_selection_mode(selection_mode)
        fingerprint = extract_hash_from_torrent(data)
        if not fingerprint:
            raise ValueError("Invalid torrent metainfo")
        name = filename.rsplit(".", 1)[0]
        return await self.submit(
            (TransferRequest("torrent", data, name=filename, fingerprint=fingerprint,
                             selection_mode=selection_mode),),
            name=name, source=source)

    async def submit_nzb(self, data, filename, *, source="manual_file"):
        """Admit one uploaded NZB through the canonical submission seam.

        The application layer deliberately does not interpret the posting: the
        Usenet provider owns NZB validation and normalization, and does it
        during resolution like every other provider. This only refuses an empty
        upload, which needs no format knowledge. Routing stays with core.

        ``data`` is either the manifest bytes or an async iterable of byte
        chunks. A stream is staged straight through to durable storage and the
        request carries the reference, so a large manifest is never assembled
        in memory here or persisted into the request row.
        """
        name = str(filename or "").rsplit(".", 1)[0] or "usenet-download"
        if isinstance(data, (bytes, bytearray)):
            payload = bytes(data)
            if not payload:
                raise ValueError("NZB file is empty")
        else:
            if self.staged_input is None:
                raise ValueError("Durable input storage is unavailable")
            try:
                payload = await self.staged_input.stage(data)
            except StagedInputError as exc:
                raise ValueError(str(exc)) from None
        return await self.submit(
            (TransferRequest("nzb", payload, name=filename or f"{name}.nzb"),),
            name=name, source=source)

    async def submit_meta4(self, data, filename, *, source="manual_file", selection_mode="all"):
        """Admit one uploaded Metalink4 descriptor through the canonical
        submission seam. As for an NZB, the application does not interpret
        it: the Multimeta provider reads it during resolution. The stream is
        staged straight through the neutral durable-input owner."""
        selection_mode = file_selection.normalize_selection_mode(selection_mode)
        name = str(filename or "").rsplit(".", 1)[0] or "Multimeta"
        if self.staged_input is None:
            raise ValueError("Durable input storage is unavailable")
        try:
            payload = await self.staged_input.stage(data)
        except StagedInputError as exc:
            raise ValueError(str(exc)) from None
        if not payload.byte_length:
            self.staged_input.discard(payload)
            raise ValueError("Metalink file is empty")
        return await self.submit(
            (TransferRequest("meta4", payload, name=filename or f"{name}.meta4", selection_mode=selection_mode),),
            name=name, source=source)

    async def reclaim_staged_input(self) -> int:
        """Reclaim every staged input no live request still references.

        The single cleanup owner for every terminal path -- success, permanent
        failure, cancellation, deletion, retry -- and for a crash that staged an
        input before any transfer owned it. Survivors are derived from the
        durable reference set rather than from per-path callbacks, which is what
        makes it impossible either to miss a path or to reclaim an input that is
        still needed.
        """
        if self.staged_input is None:
            return 0
        referenced = await self.repository.referenced_staged_inputs()
        return self.staged_input.sweep(referenced)

    async def submit_links(self, links, *, selection_mode="all", allow_local_network=False):
        # DP 1.0.12 corrective: one Quick Add batch is one user submission
        # and admits as ONE durable transfer owning N independent root
        # requests -- submission scope is not the same thing as equivalence
        # scope. Each request still keeps its own durable lineage, route/
        # resolution attempts, candidate/source identity and provenance,
        # and sibling requests within this one transfer may still safely
        # converge onto one canonical artifact/candidate set through the
        # existing intra-transfer machinery (transfers/cohorts.py
        # coordinate_collection(), transfers/canonical.py
        # CanonicalOwnership.attach()) -- that machinery is keyed on request
        # identity, not transfer identity, so it already treats same-transfer
        # siblings and cross-transfer contributors uniformly. A later,
        # genuinely separately admitted transfer proven equivalent still
        # converges through the same cross-transfer path, unaffected by this.
        selection_mode = file_selection.normalize_selection_mode(selection_mode)
        urls = normalize_direct_links(links)
        consented = await self._local_network_consent(urls, allow_local_network=allow_local_network)
        requests = tuple(TransferRequest(urlsplit(url).scheme.lower(), url, name=direct_link_filename(url, index),
                                         selection_mode=selection_mode,
                                         local_network_consent=direct_link_host(url) in consented)
                         for index, url in enumerate(urls, 1))
        item = await self.submit(requests, name=direct_link_collection_name([], urls), source="direct_link", deduplicate=False)
        return {"ok": True, "id": item["id"], "torrent_id": item["id"], "accepted": len(urls), "items": [item], **item}


    async def _local_network_consent(self, urls, *, allow_local_network: bool) -> frozenset[str]:
        """Admission's private-LAN decision for one submission: the hosts it
        consents to connect to on the operator's LAN.

        Local Network Connections off: nothing is consented (the connection
        boundary keeps refusing private destinations, as before). On, with
        Skip Local Connection Confirmation on: the explicitly entered LAN hosts
        are consented. On without it: nothing is admitted until the operator
        allows THIS submission; that answer is recorded on its requests only
        and never changes a setting."""
        policy = self.engine.policy
        if not policy.private_lan_connections:
            return frozenset()
        hosts = {host for host in (direct_link_host(url) for url in urls) if host}
        lan = frozenset([host for host in sorted(hosts) if await names_private_lan(host)])
        if lan and not (policy.skip_private_lan_confirmation or allow_local_network):
            raise LocalNetworkConfirmationRequired(tuple(sorted(lan)))
        return lan

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

    async def cancel_artifact(self, transfer_id, artifact_id):
        """Terminate ONE artifact's execution through the canonical owner.

        The engine owns the whole act: cancelling the native writer, requiring
        OBSERVED stop truth before the attempt is released, recording the
        cancellation outcome against that attempt and re-aggregating the parent.
        This is the application-level command for it, so every caller -- an
        operator action, an administration surface -- performs the same one
        act under the same admission and publishes the same way.
        """
        async with self.application_operation():
            await self.require(transfer_id)
            await self.engine.cancel_artifact(transfer_id, artifact_id)
            self.execution_wakeup.set()
            await self._publish(transfer_id)
            return {"ok": True, "transfer_id": transfer_id, "artifact_id": artifact_id}

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
            await publish("stats_changed", {})
            return {"ok": not any(results.values()), "paused": await self.repository.globally_paused(), "count": len(results), "failed": sum(bool(errors) for errors in results.values())}

    async def resume_all(self):
        async with self.application_operation():
            results = await self.engine.resume_all()
            self.resolution_wakeup.set()
            self.execution_wakeup.set()
            await publish("stats_changed", {})
            return {"ok": not any(results.values()), "paused": await self.repository.globally_paused(), "count": len(results), "failed": sum(bool(errors) for errors in results.values())}

    async def pause_intent(self):
        return await self.engine.pause_intent()

    async def record_pause_intent(self, intent):
        async with self.application_operation():
            await self.engine.record_pause_intent(intent)

    async def restore_pause_intent(self, intent):
        async with self.application_operation():
            results = await self.engine.restore_pause_intent(intent)
            self.resolution_wakeup.set()
            self.execution_wakeup.set()
            await publish("stats_changed", {})
            return results

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

    # -- Universal file selection (specification sections 34-40) ---------------
    # Neutral commands over the durable core selection state. No provider or
    # executor is ever contacted here; the engine already owns provider I/O and
    # the 60s/120s timing through its injected clock.

    async def file_selection(self, transfer_id):
        await self.require(transfer_id)
        return await self.repository.file_selection_presentation(
            transfer_id, now=self.engine.clock(),
        )

    async def file_selection_offers(self):
        return await self.repository.active_file_selection_offers(now=self.engine.clock())

    async def confirm_file_selection(self, transfer_id, manifest_id, entry_ids):
        async with self.application_operation():
            await self.require(transfer_id)
            result = await self.repository.confirm_file_selection(
                transfer_id, manifest_id, entry_ids, now=self.engine.clock(),
            )
            if result.outcome == "confirmed":
                # The repository transaction settled the decision and, in the same
                # BEGIN IMMEDIATE, released the file-selection gate's scheduler
                # wait. Re-drive the ordinary resolution wakeup so the confirmed
                # subset materialises on the next cycle rather than after the old
                # decision deadline or provider-poll timestamp.
                self.resolution_wakeup.set()
                await self._publish(transfer_id)
            return result

    async def dismiss_file_selection(self, transfer_id, manifest_id):
        async with self.application_operation():
            await self.require(transfer_id)
            result = await self.repository.dismiss_file_selection(
                transfer_id, manifest_id, now=self.engine.clock(),
            )
            if result.outcome == "dismissed" and result.detail == "closed_hold":
                # Close/X settled the decision to default ALL and the repository
                # released the gate's scheduler wait in the same transaction;
                # wake the resolution loop so ALL materialises immediately.
                self.resolution_wakeup.set()
                await self._publish(transfer_id)
            return result

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
            # Route resolution/readiness is independent of Download Storage.
            # The universal execution gate owns storage-consuming dispatch, so
            # readiness may bind truthful provider provenance while dispatch is
            # contained and the same durable request can execute after recovery.
            affected_ids = await self.engine.resolve_pending()
            self.execution_wakeup.set()
            # A selected-manifest commitment (e.g. the file-selection Choose
            # File affordance becoming permanently unavailable) is a semantic
            # transition the browser cannot infer from progress polling alone.
            # Reuse the existing targeted semantic publisher for exactly the
            # transfers the engine reports crossed that boundary this cycle --
            # never every active transfer, never a repository-layer publish.
            for transfer_id in affected_ids or ():
                await self._publish(transfer_id)

    async def reconcile_executions(self):
        async with self.application_operation():
            # Execution reconciliation consumes the in-memory storage state; the
            # dedicated disk guard owns periodic recovery probes. This prevents a
            # fast executor loop from erasing a just-observed ENOSPC/EDQUOT/EROFS
            # transition before the bounded recovery cadence.
            self.engine.dispatch_permitted = self.application_storage_permitted() and self.download_storage_permitted()
            before = await self.repository.active()
            # The canonical convergence owner reports what THIS cycle's recovery
            # wake actually applied. The ordinary scheduler cadence ignores it;
            # an operator-triggered broad recovery pass reports it.
            recovery = await self.engine.reconcile_executions()
            await self._contain_download_storage_faults(before)

            # Periodic progress publication is a list/read concern, not a reason
            # to reconstruct canonical transfer truth once per active transfer.
            # Re-read the small durable active projection once, publish one batch,
            # and let status transitions request one authoritative lightweight
            # collection refresh in the browser.
            after = await self.repository.active()
            after_by_id = {transfer.id: transfer for transfer in after}
            updates = []
            for previous in before:
                current = after_by_id.get(previous.id)
                if current is None:
                    updates.append(self._active_overlay_item(previous, status_changed=True))
                    continue
                previous_state = str(getattr(previous.state, "value", previous.state))
                current_state = str(getattr(current.state, "value", current.state))
                previous_progress = (previous.progress, previous.active_execution_progress)
                current_progress = (current.progress, current.active_execution_progress)
                if current_state != previous_state or current_progress != previous_progress:
                    updates.append(
                        self._active_overlay_item(
                            current,
                            status_changed=current_state != previous_state,
                        )
                    )

            if updates:
                await publish("torrent_updated", {"progress_only": True, "items": updates})
                await publish("stats_changed", {})
            return recovery

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
        """The broad operator-triggered recovery effort.

        It examines all currently non-terminal/recoverable work and asks the
        CANONICAL owners to make whatever progress is legal right now: the
        inventory reconciler, the pending-resolution pass and the convergence
        engine's own recovery wake. It is an effort, not an override -- every
        one of them applies exactly the policy it always applies, so pause
        intent, INPUT_REQUIRED, retry backoff, exhaustion, storage admission,
        materialization authority, recovery claims and provider/executor
        availability all hold. No trigger is upgraded, no authority is granted
        and no streak or budget is reset by the act of asking.

        The result states real canonical outcomes -- how many recoveries were
        APPLIED and what actually failed -- never how much work exists.
        """
        async with self.application_operation():
            inventory = await self.reconcile_inventory()
            await self.resolve_pending()
            recovery = await self.reconcile_executions()
            errors = [*inventory["errors"], *[error.as_dict() for error in getattr(recovery, "errors", ())]]
            return {
                "ok": not errors,
                # Canonical recovery outcomes: what the recovery owner applied,
                # and what inventory reconciliation adopted.
                "recovered": int(getattr(recovery, "applied", 0)),
                "imported": inventory["imported"],
                "actions": int(getattr(recovery, "applied", 0)) + int(inventory["imported"]),
                "errors": errors,
            }

    async def drain_executions(self):
        """Whole-state quiescence (restore, database wipe): no native execution
        owned by the current state may outlive it. Durable global pause stops
        new admission and quiesces and checkpoints every writer; the engine then
        releases whatever Pause left parked through the same writer retirement.
        Nothing is logically cancelled. Raises unless zero executions of the
        current state remain live."""
        live_before = len(await self.repository.live_executions())
        await self.pause_all()
        residue = await self.engine.release_writers("state_replacement")
        if residue:
            raise RuntimeError("Could not prove every native execution stopped")
        return {"live_before": live_before, "live_after": len(await self.repository.live_executions())}

    def configure(self):
        if self._configure:
            self._configure(self)

    async def start_integrations(self, only=None):
        """Start every integration, or -- given ``only`` -- exactly those, in
        lifecycle order (what a refused whole-state replacement stopped)."""
        for integration in self.lifecycle:
            if only is None or integration in only:
                await integration.start()

    async def stop_integrations(self):
        """Stop integrations in reverse order and return the ones stopped. A
        stop that fails ends the sequence with ``IntegrationStopFailed``,
        which names the integrations that were stopped before it."""
        # Clean executor shutdown is a forced material checkpoint boundary:
        # what live writers proved written becomes durable before they stop.
        try:
            await self.engine.checkpoint_live_material("executor_shutdown")
        except Exception as exc:
            logger.warning("Material checkpoint before shutdown failed: %s", type(exc).__name__)
        stopped = []
        for integration in reversed(self.lifecycle):
            try:
                await integration.stop()
            except Exception as exc:
                raise IntegrationStopFailed(stopped) from exc
            stopped.append(integration)
        return tuple(stopped)

    async def maintain_integrations(self):
        async with self.application_operation():
            for integration in self.lifecycle:
                await integration.maintain()