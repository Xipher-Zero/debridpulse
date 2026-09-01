"""Small capability contracts; integration implementations own no core policy."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from transfers.models import (
    CleanupDirective, ExecutionHandle, ExecutionObservation, ExecutionRequest,
    HealthObservation, IntegrationDescriptor, ProviderObservation,
    ProviderResource, ResolutionResult, ResourceSnapshot, TransferCandidate,
    TransferOutcome, TransferRequest, SourceEntry,
)


@runtime_checkable
class Provider(Protocol):
    descriptor: IntegrationDescriptor

    async def resolve(self, request: TransferRequest) -> ResolutionResult: ...


@runtime_checkable
class ResourceLookup(Protocol):
    async def observe(self, resource: ProviderResource) -> ProviderObservation: ...


@runtime_checkable
class Manifest(Protocol):
    async def manifest(self, resource: ProviderResource) -> tuple[SourceEntry, ...]: ...


@runtime_checkable
class CandidateRefresh(Protocol):
    async def refresh(self, candidate: TransferCandidate) -> ResolutionResult: ...


@runtime_checkable
class Inventory(Protocol):
    async def inventory(self) -> ResourceSnapshot: ...


@runtime_checkable
class Cleanup(Protocol):
    async def cleanup(self, directive: CleanupDirective) -> TransferOutcome: ...


@runtime_checkable
class Health(Protocol):
    async def health(self) -> HealthObservation: ...


@runtime_checkable
class Executor(Protocol):
    descriptor: IntegrationDescriptor

    async def start(self, request: ExecutionRequest) -> ExecutionHandle: ...
    async def observe(self, handle: ExecutionHandle) -> ExecutionObservation: ...
    async def cancel(self, handle: ExecutionHandle) -> TransferOutcome: ...


@runtime_checkable
class PauseResume(Protocol):
    async def pause(self, handle: ExecutionHandle) -> ExecutionObservation: ...
    async def resume(self, handle: ExecutionHandle) -> ExecutionObservation: ...


@runtime_checkable
class PostProcessor(Protocol):
    descriptor: IntegrationDescriptor

    async def process(self, transfer_id: int, paths: tuple[str, ...]) -> TransferOutcome: ...
