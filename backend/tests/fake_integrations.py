"""Deterministic integrations with a parcel protocol unrelated to debrid APIs."""
from dataclasses import replace
from pathlib import Path

from transfers.models import (
    Capability, CleanupDirective, Endpoint, ExecutionHandle, ExecutionObservation,
    ExecutionState, IntegrationDescriptor, OutcomeKind, Ownership, ProviderObservation,
    ProviderResource, ResolutionResult, ResourceSnapshot, ResourceState, SourceEntry,
    TransferCandidate, TransferOutcome, TransferProgress, TransferRequest,
)


class ParcelProvider:
    def __init__(self, identity="parcel-lab"):
        self.descriptor = IntegrationDescriptor(identity, "Parcel lab", frozenset({
            Capability.RESOLVE, Capability.RESOURCE_LOOKUP, Capability.METADATA,
            Capability.INVENTORY, Capability.CLEANUP, Capability.REFRESH,
        }), request_types=frozenset({"parcel", "parcel-member"}))
        self.calls = []
        self.responses = []
        self.resources = {}
        self.members = {}
        self.inventory_items = ()
        self.cleanup_response = TransferOutcome(OutcomeKind.SUCCESS)
        self.entered = None
        self.release = None

    def candidate(self, name="payload.bin", *, payload="parcel"):
        return TransferCandidate(name, (Endpoint("memory", f"memory:{payload}"),), expected_bytes=4,
                                 provider_id=self.descriptor.id, refresh_request=TransferRequest("parcel-member", payload, name=name))

    def parcel(self, payload="parcel", *, state=ResourceState.PREPARING, ownership=Ownership.CREATED):
        resource = ProviderResource(self.descriptor.id, {"box_ticket": payload}, ownership, id=f"{self.descriptor.id}:{payload}")
        observed = ProviderObservation(resource, state, "Parcel", request=TransferRequest("parcel", payload))
        self.resources[resource.id] = observed
        self.members[resource.id] = (SourceEntry("payload.bin", 4, "folder/payload.bin", TransferRequest("parcel-member", payload)),)
        return ResolutionResult(state, observation=observed)

    async def resolve(self, request):
        self.calls.append(("resolve", request.payload))
        if self.entered:
            self.entered.set()
            await self.release.wait()
        if self.responses:
            result = self.responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return ResolutionResult(ResourceState.AVAILABLE, (self.candidate(request.name or "payload.bin", payload=request.payload),))

    async def observe(self, resource):
        self.calls.append(("observe", resource.id))
        return self.resources.get(resource.id, ProviderObservation(resource, ResourceState.ABSENT))

    async def manifest(self, resource):
        self.calls.append(("manifest", resource.id))
        return self.members.get(resource.id, ())

    async def refresh(self, candidate):
        self.calls.append(("refresh", candidate.id))
        return ResolutionResult(ResourceState.AVAILABLE, (replace(candidate, expires_at=None),))

    async def inventory(self):
        self.calls.append(("inventory", None))
        return ResourceSnapshot(self.inventory_items, complete=False)

    async def cleanup(self, directive: CleanupDirective):
        self.calls.append(("cleanup", directive))
        if self.cleanup_response.kind == OutcomeKind.SUCCESS:
            self.resources.pop(directive.resource.id, None)
        return self.cleanup_response


class MemoryExecutor:
    descriptor = IntegrationDescriptor("memory-copy", "Memory copy", frozenset({Capability.PAUSE, Capability.RESUME, Capability.RECONCILE}), schemes=frozenset({"memory"}))

    def __init__(self, authorize):
        self.authorize = authorize
        self.calls = []
        self.jobs = {}
        self.start_errors = []

    def prepare(self, request):
        return ExecutionHandle(self.descriptor.id, {"copy_ticket": request.attempt_id, "destination": request.target}, request.attempt_id)

    def resumable_paths(self, target):
        return (target + ".memory-progress",)

    async def start(self, request, handle):
        assert await self.authorize(handle, "start"), "Core must persist authority before executor contact"
        self.calls.append(("start", handle))
        error = self.start_errors.pop(0) if self.start_errors else None
        result = ExecutionObservation(handle, ExecutionState.FAILED if error else ExecutionState.TRANSFERRING,
                                      TransferProgress(4, 1, 1), (request.target,), error)
        self.jobs[handle.attempt_id] = result
        return result

    async def observe(self, handle):
        assert await self.authorize(handle, "observe")
        self.calls.append(("observe", handle))
        return self.jobs.get(handle.attempt_id, ExecutionObservation(handle, ExecutionState.ABSENT))

    async def pause(self, handle):
        assert await self.authorize(handle, "pause")
        current = await self.observe(handle)
        if current.resumable:
            current = replace(current, state=ExecutionState.PAUSED)
            self.jobs[handle.attempt_id] = current
        return current

    async def resume(self, handle):
        assert await self.authorize(handle, "resume")
        current = await self.observe(handle)
        if current.resumable:
            current = replace(current, state=ExecutionState.TRANSFERRING)
            self.jobs[handle.attempt_id] = current
        return current

    async def cancel(self, handle):
        assert await self.authorize(handle, "cancel")
        self.calls.append(("cancel", handle))
        if handle.attempt_id in self.jobs:
            self.jobs[handle.attempt_id] = replace(self.jobs[handle.attempt_id], state=ExecutionState.CANCELLED)
        return TransferOutcome(OutcomeKind.CANCELLED)

    def finish(self, handle, *, materialize=True):
        current = self.jobs[handle.attempt_id]
        if materialize:
            target = Path(current.paths[0])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"done")
        self.jobs[handle.attempt_id] = replace(current, state=ExecutionState.SUCCEEDED, progress=TransferProgress(4, 4))
