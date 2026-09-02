"""Small capability contracts; integration implementations own no core policy."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from transfers.applicability import ProviderApplicability
from transfers.input_required import SubmittedInput
from transfers.models import (
    CleanupDirective, ExecutionHandle, ExecutionObservation, ExecutionRequest, ExecutionSnapshot,
    HealthObservation, InputRequirement, IntegrationDescriptor, ProviderObservation,
    ProviderResource, ResolutionResult, ResourceSnapshot, TransferCandidate,
    TransferOutcome, TransferRequest, SourceEntry, ArtifactFingerprint,
)


@runtime_checkable
class Provider(Protocol):
    descriptor: IntegrationDescriptor

    async def resolve(self, request: TransferRequest) -> ResolutionResult: ...


@runtime_checkable
class ApplicabilitySource(Protocol):
    """Provider-owned canonical applicability snapshot; no native state crosses this boundary."""

    applicability: ProviderApplicability


@runtime_checkable
class ProviderInputContinuation(Protocol):
    async def resolve_with_input(self, request: TransferRequest, submitted: SubmittedInput) -> ResolutionResult: ...


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

    def prepare(self, request: ExecutionRequest) -> ExecutionHandle | InputRequirement:
        """Allocate a handle or request transient input without remote mutation."""
        ...

    async def start(self, request: ExecutionRequest, handle: ExecutionHandle) -> ExecutionObservation: ...
    async def observe(self, handle: ExecutionHandle) -> ExecutionObservation: ...
    async def cancel(self, handle: ExecutionHandle) -> TransferOutcome: ...

    def resumable_paths(self, target: str) -> tuple[str, ...]:
        """Executor-owned sidecars which prevent adoption as a complete payload."""
        ...


@runtime_checkable
class ExecutorInputContinuation(Protocol):
    def prepare_with_input(self, request: ExecutionRequest, submitted: SubmittedInput) -> ExecutionHandle | InputRequirement: ...


@runtime_checkable
class ExecutorInputRecovery(Protocol):
    """Continue an already-started execution after a definitive input challenge."""

    def input_requirement(self, candidate: TransferCandidate, observation: ExecutionObservation) -> InputRequirement | None: ...
    async def start_with_input(self, request: ExecutionRequest, handle: ExecutionHandle,
                               submitted: SubmittedInput) -> ExecutionObservation: ...


@runtime_checkable
class PauseResume(Protocol):
    async def pause(self, handle: ExecutionHandle) -> ExecutionObservation: ...
    async def resume(self, handle: ExecutionHandle) -> ExecutionObservation: ...


@runtime_checkable
class BatchObservation(Protocol):
    async def observe_many(self, handles: tuple[ExecutionHandle, ...]) -> ExecutionSnapshot: ...


@runtime_checkable
class CandidateSampling(Protocol):
    async def fingerprint(self, candidate: TransferCandidate) -> ArtifactFingerprint | None: ...


@runtime_checkable
class PostProcessor(Protocol):
    descriptor: IntegrationDescriptor

    async def process(self, transfer_id: int, paths: tuple[str, ...]) -> TransferOutcome: ...
