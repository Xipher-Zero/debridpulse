"""Small capability contracts; integration implementations own no core policy."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from transfers.applicability import ProviderApplicability
from transfers.input_required import SubmittedInput
from transfers.models import (
    CleanupDirective, DiscoveryDepth, DiscoveryLimits, DiscoveryResult, ExecutionFootprint, ExecutionHandle, ExecutionObservation, ExecutionRequest,
    ExecutionSnapshot, ExecutionSubject, ExecutionWork, ExecutorCapabilities, ExecutorClaim, ExecutorGateResult,
    ExecutorHealth, ExecutorRuntimeControlResult, ExecutorThroughput, HealthObservation, InputRequirement,
    IntegrationDescriptor,
    ProviderObservation, ProviderResource, ResolutionResult, ResourceSnapshot, RetargetTruth, TransferCandidate,
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
class DiscoveryResolution(Protocol):
    """A provider that asked core for remote discovery (``ResolutionResult.discovery``)
    turns the neutral result into its ordinary resolution. It never lists,
    connects or authenticates itself."""

    async def resolve_discovered(self, request: TransferRequest, discovered: DiscoveryResult) -> ResolutionResult: ...


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
class ExecutorSourceRetarget(Protocol):
    """``ContinuationCapability.NATIVE_SOURCE_RETARGET``: hand a quiesced
    native job to a NEW DebridPulse execution attempt whose request names a
    different, already-equivalent source for the same artifact and target.

    Core decides that a retarget is appropriate and fences the previous
    attempt; the executor only answers whether this concrete source pair can
    be retargeted safely, and performs it. Native object continuity is never
    writer-authority continuity: ``previous`` keeps its own identity and loses
    all authority, and the inherited native object is reached only through
    the new attempt's handle from then on."""

    async def prepare_retarget(self, request: ExecutionRequest,
                               previous: ExecutionHandle) -> ExecutionHandle | None:
        """No native mutation. The new attempt's handle adopting
        ``previous``'s native object, when this pair is retargetable --
        including every security and preparation check a fresh start of
        ``request`` would apply -- else ``None`` (core continues portably)."""
        ...

    async def retarget_from(self, request: ExecutionRequest, prepared: ExecutionHandle,
                            previous: ExecutionHandle) -> ExecutionObservation:
        """After core durably admitted ``prepared`` and fenced ``previous``:
        prove the inherited native job still exists and is quiesced, re-check
        the replacement source, replace its source and report observed truth
        for ``prepared``. The job stays quiesced; only core resumes it. A
        definitive refusal before any native mutation is ``FAILED``; an
        uncertain mutation is ``UNKNOWN``."""
        ...

    async def retarget_truth(self, request: ExecutionRequest, prepared: ExecutionHandle,
                             original: ExecutionRequest) -> RetargetTruth:
        """No native mutation. Which source the quiesced job inherited by
        ``prepared`` positively serves now -- ``request``'s (the replacement),
        ``original``'s (the previous attempt's), or neither provably -- with
        everything a start of that source would configure. Core resolves an
        unproven retarget from this answer alone; while it is unproven the
        new attempt has no acquisition authority."""
        ...


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
class RemoteDiscovery(Protocol):
    """``capabilities.remote_discovery``: read-only listing of one directory
    subject, before any candidate exists, behind exactly the server-identity
    and authentication decisions execution applies.

    Answers the neutral ``InputRequirement`` when access input is definitively
    needed (the one INPUT_REQUIRED lifecycle and the authentication-input owner
    carry it; ``submitted`` is that owner's answer), a ``DiscoveryResult`` of the
    directory's regular files within ``depth`` (``DiscoveryDepth``: its
    immediate files, N subdirectory levels, or its whole tree), or raises a
    normalized ``TransferError`` for a definitive failure. The executor
    translates the neutral depth into its transport and enforces it itself; one
    that cannot enumerate exactly that depth raises ``UNSUPPORTED_CAPABILITY``
    rather than answer with more or less. ``limits`` (``DiscoveryLimits``) are
    enforced the same way: a directory past its file limit, or an enumeration
    past its time limit, fails -- never answers with a partial listing -- and
    an executor that cannot enforce a requested limit refuses it.
    ``content_limit`` asks for the complete content of the regular file the
    subject names instead of a listing (``DiscoveryRequest.content_limit``):
    a FILE answer carrying at most that many bytes, a larger file failing the
    discovery; an executor that cannot read it refuses."""

    async def discover(self, subject: ExecutionSubject, submitted: SubmittedInput | None = None, *,
                       depth: DiscoveryDepth = DiscoveryDepth.CURRENT,
                       limits: DiscoveryLimits = DiscoveryLimits(),
                       content_limit: int | None = None) -> DiscoveryResult | InputRequirement: ...


@runtime_checkable
class ContinuationBoundaryDiscovery(Protocol):
    """``ContinuationCapability.BOUNDARY_DISCOVERY``: where this executor can
    continue ``subject`` exactly, for concrete source data.

    ``member`` is ``""`` for a FILE artifact, else a collection member's
    relative path. The answer is the largest offset ``<= prefix`` (the
    DP-valid contiguous prefix) at which the executor can continue writing
    final-file bytes exactly -- e.g. the start of the first source segment
    whose decoded range begins there. Core bounds and validates it; the
    executor never decides how much material is retained."""

    async def continuation_boundary(self, subject: ExecutionSubject, member: str, prefix: int) -> int: ...


@runtime_checkable
class PostProcessor(Protocol):
    descriptor: IntegrationDescriptor

    async def process(self, transfer_id: int, paths: tuple[str, ...]) -> TransferOutcome: ...
