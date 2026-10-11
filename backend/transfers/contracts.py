"""Small capability contracts; integration implementations own no core policy."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Protocol, runtime_checkable

from transfers.applicability import ProviderApplicability
from transfers.entitlement import ProviderEntitlements
from transfers.input_required import SubmittedInput
from transfers.models import (
    ActiveCapacity, AvailabilityState, CachePresence, CleanupDirective, DiscoveryDepth, DiscoveryLimits, DiscoveryResult, ExecutionFootprint, ExecutionHandle, ExecutionObservation, ExecutionRequest,
    ExecutionSnapshot, ExecutionSubject, ExecutionWork, ExecutorCapabilities, ExecutorClaim, ExecutorGateResult,
    ExecutorHealth, ExecutorRuntimeControlResult, FileManifestEntry, HealthObservation, InputRequirement,
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
class EntitlementSource(Protocol):
    """Optional: a provider whose current ACCOUNT decides what it may begin.

    The provider's own lifecycle keeps this neutral value current from its
    account truth (translation, refresh, last-known-good, known expiry,
    definitive refusals); reading it performs no I/O. A provider that does not
    implement it -- or whose value is ``None`` -- has no account entitlement
    dimension at all: nothing is synthesized for it."""

    entitlements: ProviderEntitlements | None


@runtime_checkable
class RequestEntitlementSource(Protocol):
    """Optional narrowing of ``EntitlementSource`` for one request, when the
    account's entitlement depends on request facts only the provider can read
    (for example which of its hosts a link belongs to). ``None`` is unknown."""

    def entitlement_for(self, request: TransferRequest) -> bool | None: ...


@runtime_checkable
class CachedResolution(Protocol):
    """Optional: a provider whose ordinary ``resolve`` may begin productive
    remote acquisition, and that can also resolve WITHOUT beginning any.

    ``cache_presence`` states, for each request in order, whether the provider
    already holds it so that resolving it needs no new acquisition -- a pure
    read that creates nothing (``UNKNOWN`` when it cannot tell).
    ``resolve_cached`` resolves a request exactly as ``resolve`` would only
    when doing so begins no productive acquisition; ``None`` means it is not
    held now and nothing was created.

    Core asks only while choosing which alternative of an explicit
    alternative-source group to resolve first; ``resolve`` remains the one
    productive resolution, used once core has admitted that alternative."""

    async def cache_presence(self, requests: tuple[TransferRequest, ...]) -> tuple[CachePresence, ...]: ...

    async def resolve_cached(self, request: TransferRequest) -> ResolutionResult | None: ...


@runtime_checkable
class AvailabilitySource(Protocol):
    """Optional, declared by ``Capability.AVAILABILITY``: for each request in
    order, whether the provider can deliver it now without beginning
    productive acquisition (``AvailabilityState``). One bounded read that
    creates nothing, batched where the provider can; ``UNKNOWN`` when it
    cannot tell. Core asks only the providers already competing for a root,
    and only to prefer a ``READY`` one -- never to add, remove or exhaust one."""

    async def availability(self, requests: tuple[TransferRequest, ...]) -> tuple[AvailabilityState, ...]: ...


# Set by core for exactly the duration of a speculative backup preparation's
# ``resolve`` (never for any other provider call), so a provider's own refusal
# boundary can tell a backup that hit scarce capacity from a primary refusal.
_SPECULATIVE_ATTEMPT: ContextVar[bool] = ContextVar("speculative_attempt", default=False)


def speculative_attempt() -> bool:
    """Whether the provider call in progress is a speculative backup
    preparation. Its refusals say only that the backup could not be made now:
    a provider must not contract the account's entitlement, or record any
    other lasting account fact, from them."""
    return _SPECULATIVE_ATTEMPT.get()


@contextmanager
def speculative_preparation():
    """The scope of one speculative backup preparation's provider call."""
    token = _SPECULATIVE_ATTEMPT.set(True)
    try:
        yield
    finally:
        _SPECULATIVE_ATTEMPT.reset(token)


@runtime_checkable
class SpeculativePreparation(Protocol):
    """Optional: whether the provider allows a request to be prepared
    speculatively -- added to its account as a backup while another provider
    delivers it. A pure, provider-owned answer that performs no I/O and may
    follow the provider's current options. A provider without it is never
    eligible (``IntegrationRegistry.speculative_preparation_allowed``)."""

    def speculative_preparation_allowed(self, request: TransferRequest) -> bool: ...


@runtime_checkable
class ActiveCapacitySource(Protocol):
    """Optional: the provider's concurrent active-resource capacity for
    ``request``'s class (``ActiveCapacity``), read without creating anything.
    ``None`` says the class has no such capacity at this provider. Core uses
    it only to keep backups out of a full provider and to let primary work
    reclaim the slots its own backups hold -- never to route."""

    async def active_capacity(self, request: TransferRequest) -> ActiveCapacity | None: ...


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
class UpstreamSelection(Protocol):
    """A manifest provider that executes only the files selected on its own
    resource: until DebridPulse's authorized selection is synchronized there,
    the resource is observable and its file list selectable, but none of its
    members is executable.

    Core hands it, for a root's still-uncommitted generation whose decision is
    settled, exactly the members that generation's proof authorizes, at the
    provider's own manifest coordinates
    (``TransferRepository.upstream_selection``) -- never a choice of its own.
    The provider expresses them natively -- answering ``True``: its resource
    changed and is read back once, at once -- or verifies a selection already
    made (``False``), and raises a normalized error when its resource does not
    or cannot reflect them. ``submit=False`` is that read-back: the provider
    only reads and verifies, never sends, and answers ``True`` when its
    resource still waits for a selection. A provider without it keeps every
    member executable as before."""

    async def synchronize_selection(self, resource: ProviderResource,
                                    members: tuple[FileManifestEntry, ...], *, submit: bool = True) -> bool: ...


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
