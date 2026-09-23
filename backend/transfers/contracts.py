"""Small capability contracts; integration implementations own no core policy."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from transfers.applicability import ProviderApplicability
from transfers.input_required import SubmittedInput
from transfers.models import (
    CleanupDirective, ExecutionFootprint, ExecutionHandle, ExecutionObservation, ExecutionRequest,
    ExecutionSnapshot, ExecutionSubject, ExecutionWork, ExecutorCapabilities, ExecutorClaim, ExecutorGateResult,
    ExecutorHealth, ExecutorRuntimeControlResult, ExecutorThroughput, HealthObservation, InputRequirement,
    IntegrationDescriptor,
    ProviderObservation, ProviderResource, ResolutionResult, ResourceSnapshot, TransferCandidate,
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
    """The one generalized executor contract.

    Core speaks only these neutral semantics; everything native terminates in
    the implementation. Optional semantic operations below are used only when
    ``capabilities`` declares them (validated at registration)."""

    descriptor: IntegrationDescriptor
    capabilities: ExecutorCapabilities

    def claim(self, subject: ExecutionSubject) -> ExecutorClaim:
        """Pure, fast, I/O-free applicability over canonical subject facts."""
        ...

    def footprint(self, work: ExecutionWork) -> ExecutionFootprint:
        """Pure: native transient paths this work may create beside its plan."""
        ...

    def prepare(self, request: ExecutionRequest) -> ExecutionHandle | InputRequirement:
        """Allocate a durable correlation or request transient input; no native mutation."""
        ...

    async def start(self, request: ExecutionRequest, handle: ExecutionHandle) -> ExecutionObservation:
        """May return the prepared handle or its one legal native binding.
        A lost acknowledgement is ``UNKNOWN``, never ``FAILED``."""
        ...

    async def observe_many(self, handles: tuple[ExecutionHandle, ...]) -> ExecutionSnapshot:
        """One neutral snapshot; failure is a snapshot error, never an empty success."""
        ...

    async def cancel(self, handle: ExecutionHandle) -> ExecutionObservation:
        """Request native stop and report observed truth: only CANCELLED/ABSENT
        (or another terminal state) proves the writer stopped; an unconfirmed
        or lost acknowledgement is ``UNKNOWN``."""
        ...

    async def health(self) -> ExecutorHealth: ...


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
    """Per-execution controls (``capabilities.per_execution_pause``). Core
    invokes one only while the current observation advertises it."""

    async def pause(self, handle: ExecutionHandle) -> ExecutionObservation: ...
    async def resume(self, handle: ExecutionHandle) -> ExecutionObservation: ...


@runtime_checkable
class ExecutorAcquisitionGate(Protocol):
    """``capabilities.acquisition_gate``: while paused, this executor begins or
    continues no DP-owned network acquisition; executor-local non-network work
    may continue. Never proof of full application quiescence."""

    async def set_acquisition_paused(self, paused: bool) -> ExecutorGateResult: ...


@runtime_checkable
class ExecutorBandwidthControl(Protocol):
    """``capabilities.aggregate_bandwidth_ceiling``: ``bytes_per_second`` (0 =
    unlimited) bounds the aggregate DP-owned acquisition of this executor."""

    async def set_bandwidth_ceiling(self, bytes_per_second: int) -> ExecutorRuntimeControlResult: ...


@runtime_checkable
class ExecutorAggregateThroughput(Protocol):
    """``capabilities.aggregate_throughput``: this executor measures download
    throughput only for itself as a whole.

    Declared only when the implementation genuinely cannot report a truthful
    rate per execution. Core then counts this ONE value for the executor and
    never adds any per-execution progress rate from it, so the same throughput
    can never be counted twice. Every other executor contributes the sum of the
    per-execution rates it already reports through ``TransferProgress``.
    """

    async def aggregate_download_throughput(self) -> ExecutorThroughput: ...


@runtime_checkable
class ExecutorNativeRetry(Protocol):
    """``capabilities.native_assisted_retry``: after core decided a same-
    candidate retry and fenced ``previous``, continue its native state under
    the new durably prepared attempt ``prepared``."""

    async def retry_from(self, request: ExecutionRequest, prepared: ExecutionHandle,
                         previous: ExecutionHandle) -> ExecutionObservation: ...


@runtime_checkable
class CandidateSampling(Protocol):
    """Bounded neutral content evidence for one subject, acquired before any writer.

    ``None`` means no evidence capability for this subject. An
    ``InputRequirement`` means the evidence exists but acquiring it definitively
    requires transient operator input; core carries it through the one
    INPUT_REQUIRED lifecycle and continues through
    ``CandidateSamplingContinuation``. The sample is a fact; what it means for
    equivalence is decided by core evidence policy only.
    """

    async def fingerprint(self, subject: ExecutionSubject) -> ArtifactFingerprint | InputRequirement | None: ...


@runtime_checkable
class CandidateSamplingContinuation(Protocol):
    """Continue the same evidence acquisition with submitted transient input."""

    async def fingerprint_with_input(self, subject: ExecutionSubject,
                                     submitted: SubmittedInput) -> ArtifactFingerprint | InputRequirement | None: ...


@runtime_checkable
class PostProcessor(Protocol):
    descriptor: IntegrationDescriptor

    async def process(self, transfer_id: int, paths: tuple[str, ...]) -> TransferOutcome: ...
