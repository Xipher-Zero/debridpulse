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
class RequestApplicabilitySource(Protocol):
    """Provider-local request interpretation returning only neutral applicability facts.

    Use this only when a provider's validated native semantics cannot be safely
    flattened into the host-only applicability snapshot. The provider performs
    that interpretation without I/O and returns the neutral Item 6 value for
    the current request; native schemas and pattern syntax never cross here.
    """

    def applicability_for(self, request: TransferRequest) -> ProviderApplicability: ...


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
    """Execute with transient input the executor did not have to ask for itself.

    ``start_with_input`` continues an already-started execution after a
    definitive input challenge, or starts a freshly prepared attempt whose input
    was already proven by pre-writer evidence acquisition for that candidate.
    """

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
    """Bounded neutral content evidence for one candidate, acquired before any writer.

    ``None`` means no evidence capability for this candidate. An
    ``InputRequirement`` means the evidence exists but acquiring it definitively
    requires transient operator input; core carries it through the one
    INPUT_REQUIRED lifecycle and continues through
    ``CandidateSamplingContinuation``.
    """

    async def fingerprint(self, candidate: TransferCandidate) -> ArtifactFingerprint | InputRequirement | None: ...


@runtime_checkable
class CandidateSamplingContinuation(Protocol):
    """Continue the same evidence acquisition with submitted transient input."""

    async def fingerprint_with_input(self, candidate: TransferCandidate,
                                     submitted: SubmittedInput) -> ArtifactFingerprint | InputRequirement | None: ...


@runtime_checkable
class PostProcessor(Protocol):
    descriptor: IntegrationDescriptor

    async def process(self, transfer_id: int, paths: tuple[str, ...]) -> TransferOutcome: ...
