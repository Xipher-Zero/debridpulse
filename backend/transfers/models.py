"""Canonical values crossing source, executor and post-processing boundaries."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping
from uuid import uuid4

from transfers.errors import NormalizedError
from transfers.size_evidence import positive_size
from transfers.staged_input import StagedPayload


def new_identity() -> str:
    return uuid4().hex


class Capability(StrEnum):
    RESOLVE = "resolve"
    AVAILABILITY = "availability"
    METADATA = "metadata"
    FILE_MANIFEST = "file_manifest"
    REFRESH = "refresh"
    ALTERNATES = "alternates"
    RESOURCE_CREATION = "resource_creation"
    RESOURCE_LOOKUP = "resource_lookup"
    INVENTORY = "inventory"
    CLEANUP = "cleanup"
    HEALTH = "health"
    INTEGRITY = "integrity"


class InputReason(StrEnum):
    AUTH_REQUIRED = "auth_required"
    # The operator must confirm an observed server identity before the
    # execution may continue. A security interaction, not a protocol.
    SERVER_IDENTITY_REQUIRED = "server_identity_required"


class InputOrigin(StrEnum):
    PROVIDER = "provider"
    # Pre-writer evidence acquisition for one resolved candidate: no artifact
    # exists yet, and the sampling capability (never a provider) owns the input.
    EVIDENCE = "evidence"
    EXECUTOR = "executor"


class InputMethod(StrEnum):
    USERNAME_PASSWORD = "username_password"
    USERNAME_PRIVATE_KEY = "username_private_key"
    # Confirm an observed server identity and supply nothing else: offered
    # only when the authentication-input owner already holds usable
    # credentials for that scope. Identity and credentials stay separate facts.
    SERVER_IDENTITY = "server_identity"


class InputField(StrEnum):
    USERNAME = "username"
    PASSWORD = "password"
    PRIVATE_KEY = "private_key"
    PASSPHRASE = "passphrase"


@dataclass(frozen=True)
class InputFieldDescriptor:
    name: InputField
    required: bool


@dataclass(frozen=True)
class InputMethodDescriptor:
    method: InputMethod
    fields: tuple[InputFieldDescriptor, ...]

    def __post_init__(self):
        actual = {item.name: item.required for item in self.fields}
        if len(actual) != len(self.fields):
            raise ValueError("Authentication field descriptors must be unique")
        if self.method == InputMethod.USERNAME_PASSWORD:
            expected = {InputField.USERNAME: True, InputField.PASSWORD: True}
        elif self.method == InputMethod.USERNAME_PRIVATE_KEY:
            expected = {InputField.USERNAME: True, InputField.PRIVATE_KEY: True, InputField.PASSPHRASE: False}
        elif self.method == InputMethod.SERVER_IDENTITY:
            expected = {}
        else:
            raise ValueError("Unsupported authentication method")
        if actual != expected:
            raise ValueError("Authentication method fields do not match the canonical contract")


class InputFactName(StrEnum):
    SERVER_HOST = "server_host"
    SERVER_IDENTITY_ALGORITHM = "server_identity_algorithm"
    SERVER_IDENTITY_FINGERPRINT = "server_identity_fingerprint"


@dataclass(frozen=True)
class InputFact:
    """One durable, public, non-secret fact the operator needs to decide."""
    name: InputFactName
    value: str

    def __post_init__(self):
        if not isinstance(self.name, InputFactName):
            raise TypeError("Input fact names must be canonical")
        if (not isinstance(self.value, str) or not self.value or len(self.value) > 1024
                or any(ord(char) < 32 or ord(char) == 127 for char in self.value)):
            raise ValueError("Input fact values must be bounded printable text")


# Exactly the facts each neutral reason carries.
_REASON_FACTS = {
    InputReason.AUTH_REQUIRED: frozenset(),
    InputReason.SERVER_IDENTITY_REQUIRED: frozenset({
        InputFactName.SERVER_HOST,
        InputFactName.SERVER_IDENTITY_ALGORITHM,
        InputFactName.SERVER_IDENTITY_FINGERPRINT,
    }),
}


@dataclass(frozen=True)
class InputRequirement:
    reason: InputReason
    methods: tuple[InputMethodDescriptor, ...]
    facts: tuple[InputFact, ...] = ()

    def __post_init__(self):
        if self.reason not in _REASON_FACTS:
            raise ValueError("Unsupported input-required reason")
        if not self.methods or len({item.method for item in self.methods}) != len(self.methods):
            raise ValueError("Authentication challenges require unique accepted methods")
        names = [fact.name for fact in self.facts]
        if len(set(names)) != len(names) or set(names) != _REASON_FACTS[self.reason]:
            raise ValueError("Input challenge facts do not match the canonical reason contract")


@dataclass(frozen=True)
class InputChallenge:
    id: str
    transfer_id: int
    generation: int
    reason: InputReason
    origin: InputOrigin
    integration_id: str
    operation_id: str
    methods: tuple[InputMethodDescriptor, ...]
    request_id: str | None = None
    artifact_id: int | None = None
    facts: tuple[InputFact, ...] = ()

    @property
    def requirement(self) -> InputRequirement:
        return InputRequirement(self.reason, self.methods, self.facts)


class ResourceState(StrEnum):
    PREPARING = "preparing"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    ABSENT = "absent"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class CachePresence(StrEnum):
    """Provider-neutral torrent-cache presence, as observed when a provider
    accepted the request (DP 1.0.12 canonical torrent cache fact).

    Orthogonal to ``ResourceState``: ``AVAILABLE`` says a resource is usable
    now, never that it was already cached when first asked for. ``HIT`` /
    ``MISS`` are set only from a provider's own authoritative statement at the
    observation itself; they are never derived from later readiness, status
    codes, speed, completion time, or endpoint identity. ``UNKNOWN`` means the
    provider gave no trustworthy fact, and is what every observation persisted
    before this fact existed decodes to.
    """
    HIT = "hit"
    MISS = "miss"
    UNKNOWN = "unknown"


class DeliveryKind(StrEnum):
    """Whether a candidate's endpoint IS the requested source or a
    provider-issued delivery capability for it.

    A provider-issued endpoint is an execution capability, not the logical
    source route. ``DIRECT`` is also what candidates persisted before this
    fact existed decode to: their presentation is left unchanged rather than
    guessed from the endpoint or provider.
    """
    DIRECT = "direct"
    PROVIDER_ISSUED = "provider_issued"


class TransferState(StrEnum):
    ACCEPTED = "pending"
    RESOLVING = "processing"
    INPUT_REQUIRED = "input_required"
    READY = "ready"
    QUEUED = "queued"
    TRANSFERRING = "downloading"
    PAUSED = "paused"
    VERIFYING = "verifying"
    POST_PROCESSING = "extracting"
    COMPLETED = "completed"
    CONSOLIDATED = "consolidated"
    FAILED = "error"
    CANCELLED = "cancelled"
    DELETED = "deleted"


class ExecutionState(StrEnum):
    """Neutral executor lifecycle. ``RUNNING`` means only that executor work is
    currently active; network activity, bandwidth need and stall expectations
    are separate ``ExecutionActivity`` facts."""
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class Ownership(StrEnum):
    CREATED = "created"
    OBSERVED = "observed"
    ADOPTED = "adopted"


class CleanupAuthority(StrEnum):
    OWNED = "owned"
    USER_REQUEST = "user_request"


class OutcomeKind(StrEnum):
    OBSERVATION = "observation"
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class CancellationInitiator(StrEnum):
    USER = "user"
    POLICY = "policy"
    PROVIDER = "provider"
    EXECUTOR = "executor"


@dataclass(frozen=True)
class TransferRequest:
    kind: str
    # A request's input. Small inputs are carried inline. A large one is carried
    # as a ``StagedPayload`` -- a durable reference owned by
    # ``transfers.staged_input`` -- so the bytes never enter request or
    # candidate JSON. Core neither interprets nor dereferences it; the edge that
    # produced the input and the edge that consumes it do.
    payload: "str | bytes | StagedPayload" = field(repr=False)
    name: str = ""
    fingerprint: str = ""
    preferred_provider: str | None = None
    # Neutral per-submission policy: "all" (default) or "interactive". Governs
    # only whether the interactive file-selection lifecycle is entered; it is
    # NOT part of source identity and is excluded from the dedupe fingerprint
    # (which keys on ``fingerprint``, the BitTorrent infohash).
    selection_mode: str = "all"
    # The operator's per-submission consent to connect THIS explicitly entered
    # source to a private-LAN (RFC1918) address, recorded at admission (the
    # per-transfer confirmation, or the global skip-confirmation setting). Not
    # identity; never a global permission; never inherited by anything the
    # source's provider returns for another host.
    local_network_consent: bool = False


# BitTorrent-class request kinds: a magnet URI, or a torrent metainfo upload
# ("torrent"; "torrent_file"/"file" are stored/legacy spellings). A provider
# may materialize either into HTTP(S) descendants, but the root's class wins.
TORRENT_FILE_REQUEST_KINDS = frozenset({"torrent", "torrent_file", "file"})
BITTORRENT_REQUEST_KINDS = frozenset({"magnet", *TORRENT_FILE_REQUEST_KINDS})


@dataclass(frozen=True)
class IntegrationDescriptor:
    id: str
    name: str
    capabilities: frozenset[Capability]
    request_types: frozenset[str] = frozenset()
    enabled: bool = True
    priority: int = 0


@dataclass(frozen=True)
class ProviderResource:
    provider_id: str
    context: Mapping[str, object] = field(repr=False)
    ownership: Ownership = Ownership.OBSERVED
    id: str = field(default_factory=new_identity)


@dataclass(frozen=True)
class Endpoint:
    scheme: str
    address: str = field(repr=False)
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class IntegrityMetadata:
    algorithm: str
    digest: str


@dataclass(frozen=True)
class SourceIdentity:
    """Comparable source scope supplied by the resolver, without source secrets."""
    scope: str
    key: str


class MaterializationKind(StrEnum):
    """Neutral shape of one logical acquisition: one final file, or a
    collection of final files beneath one dedicated directory boundary."""
    FILE = "file"
    COLLECTION = "collection"


class FingerprintKind(StrEnum):
    """Provider-neutral strength of bounded remote content evidence."""
    FULL_CONTENT_SAMPLE = "full_content_sample"
    PREFIX_CONTENT_SAMPLE = "prefix_content_sample"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ArtifactFingerprint:
    total_bytes: int
    signature: str
    kind: FingerprintKind = FingerprintKind.FULL_CONTENT_SAMPLE
    reason: str = ""
    prefix_signature: str = ""


@dataclass(frozen=True)
class TransferCandidate:
    """One executable acquisition option.

    ``relative_path`` is the collection-root-relative member path a manifest
    child was stamped with (see :class:`SourceEntry`); core prepends the durable
    transfer root exactly once in
    ``transfers._engine_base.TransferEngine._materialize``.
    """
    name: str
    endpoints: tuple[Endpoint, ...]
    expected_bytes: int = 0
    relative_path: str = ""
    provider_id: str = ""
    resource: ProviderResource | None = None
    refresh_request: TransferRequest | None = field(default=None, repr=False)
    context: Mapping[str, object] = field(default_factory=dict, repr=False)
    expires_at: float | None = None
    integrity: tuple[IntegrityMetadata, ...] = ()
    priority: int = 0
    id: str = field(default_factory=new_identity)
    source_identity: SourceIdentity | None = None
    resolver_identity_evidence: ResolverArtifactIdentityEvidence | None = None
    delivery: DeliveryKind = DeliveryKind.DIRECT
    # Neutral transient-input methods an executor may request for this
    # candidate. Never carries a secret, an executor identity or a native name.
    accepted_input_methods: tuple[InputMethod, ...] = ()
    # Non-secret content evidence retained because transient operator input
    # proved this candidate when it joined a canonical artifact. It stands in
    # for a live acquisition only when that acquisition would need input the
    # deciding request does not hold (the proving input itself is never kept).
    content_evidence: ArtifactFingerprint | None = None
    # The canonical request class this candidate was resolved for. Stamped by
    # core from the owning request (never chosen by a provider); an executor
    # may claim a subject from it without any URL endpoint.
    request_kind: str = ""
    # Provider-declared neutral acquisition shape; core derives the
    # materialization plan from it without knowing the eventual executor.
    materialization: MaterializationKind = MaterializationKind.FILE
    # Core-stamped lineage fact (never provider-chosen): every endpoint of this
    # candidate is exactly the host the operator explicitly submitted with
    # local-network consent. Only such a candidate may reach an RFC1918
    # address, and only while the global Local Network Connections policy is
    # on -- enforced at the connection boundary, never by this flag alone.
    private_network_grant: bool = False

    def __post_init__(self):
        if not isinstance(self.materialization, MaterializationKind):
            raise ValueError("Candidate materialization must be a canonical kind")
        methods = self.accepted_input_methods
        if (not isinstance(methods, tuple) or any(not isinstance(item, InputMethod) for item in methods)
                or len(set(methods)) != len(methods)):
            raise ValueError("Accepted input methods must be unique canonical input methods")


@dataclass(frozen=True)
class TransferProgress:
    total_bytes: int = 0
    completed_bytes: int = 0
    bytes_per_second: int = 0

    @property
    def percentage(self) -> float:
        return min(100.0, max(0.0, self.completed_bytes / self.total_bytes * 100)) if self.total_bytes > 0 else 0.0


@dataclass(frozen=True)
class FileManifestEntry:
    """One neutral file fact for human selection: safe name, relative path, size.

    Carries no URL, endpoint, signed token, header, credential, provider-native
    decision, selection flag, timeout, or executor information.

    ``relative_path`` is the member path INSIDE the transfer collection root and
    never contains the root itself (``Disc 1/track01.flac``, never
    ``Album/Disc 1/track01.flac``). Interpreting a provider-native collection
    wrapper is the provider translation boundary's job; core owns the canonical
    root (``torrents.name``) and applies it exactly once when it allocates a
    child FILE target.
    """
    name: str
    relative_path: str
    expected_bytes: int = 0


@dataclass(frozen=True)
class FileManifest:
    """A complete provider-neutral file-level tree observed for one resource.

    A provider reports ``None`` until it has a complete authoritative tree;
    partial trees are never exposed as a selectable manifest.
    """
    entries: tuple[FileManifestEntry, ...]


@dataclass(frozen=True)
class ProviderObservation:
    resource: ProviderResource
    state: ResourceState
    name: str = ""
    fingerprint: str = ""
    progress: TransferProgress = field(default_factory=TransferProgress)
    error: NormalizedError | None = None
    request: TransferRequest | None = field(default=None, repr=False)
    file_manifest: FileManifest | None = None
    cache_presence: CachePresence = CachePresence.UNKNOWN


@dataclass(frozen=True)
class DiscoveryRequest:
    """A provider's request that core list ONE remote directory for it.

    ``endpoint`` names the directory in the transport that would execute its
    members; core runs the listing read-only through the executor that claims
    it, under the one authentication-input and server-identity owner, and
    hands the neutral result back to the provider. A provider never opens the
    connection itself.

    ``recursive``: the provider's source semantics select the whole tree
    beneath a directory, not only its immediate files. An executor that cannot
    list a tree refuses the discovery; it never answers with a flat listing.

    ``alternate``: the SAME operator request read another way, when the
    provider's source semantics admit one (never a second candidate). Core
    tries it only after discovering ``endpoint`` positively establishes that
    this interpretation does not provide the resource
    (``transfers.policy.interpretation_absent``); once reached, it durably
    becomes the request's interpretation (``RequestRecord.interpretation``).
    An alternate's own resolution never names a further alternate."""
    endpoint: Endpoint
    accepted_input_methods: tuple[InputMethod, ...] = ()
    recursive: bool = False
    alternate: TransferRequest | None = None


@dataclass(frozen=True)
class DiscoveredEntry:
    """One regular file found inside a discovered directory. ``relative_path``
    is its path below that directory for a recursive discovery (``""`` means
    the file lies directly inside it, at ``name``)."""
    name: str
    expected_bytes: int = 0
    relative_path: str = ""


class RemoteObjectKind(StrEnum):
    """What authoritative remote evidence proved a discovered path to be."""
    FILE = "file"
    DIRECTORY = "directory"


@dataclass(frozen=True)
class DiscoveryResult:
    """Authoritative remote facts about one discovered path.

    A DIRECTORY carries its immediate regular-file members -- or, for a
    recursive discovery, every regular file of its tree, each at its
    ``relative_path`` (a server whose top level is a set of named roots lists
    each root as the first path segment); never directories, symbolic links or
    special files, and never remote-browser state. Only a complete listing is a
    result: anything the server refused to list fails the discovery. ``directory``
    is the concrete absolute path they were listed in, as the server resolved
    it -- so a home-relative request reaches execution as a canonical path. A
    regular FILE carries no members, only its size when the server reports it
    (``expected_bytes``)."""
    entries: tuple[DiscoveredEntry, ...] = ()
    directory: str = ""
    kind: RemoteObjectKind = RemoteObjectKind.DIRECTORY
    expected_bytes: int = 0


@dataclass(frozen=True)
class ResolutionResult:
    """Candidates are alternatives for one request; manifests describe members."""
    state: ResourceState
    candidates: tuple[TransferCandidate, ...] = ()
    observation: ProviderObservation | None = None
    error: NormalizedError | None = None
    input_required: InputRequirement | None = None
    # Core-run remote discovery the provider needs before it can describe the
    # resource (``DiscoveryResolution.resolve_discovered`` receives the result).
    discovery: DiscoveryRequest | None = None


@dataclass(frozen=True)
class SourceEntry:
    """Metadata for an unresolved manifest member; never dispatched as a candidate.

    ``relative_path`` uses the same collection-root-relative coordinate system as
    :class:`FileManifestEntry` -- the two must agree exactly, or explicit file
    selection cannot reconcile an early choice against the executable manifest
    (``transfers.file_selection.reconcile_executable_subset``).
    """
    name: str
    expected_bytes: int
    relative_path: str
    request: TransferRequest = field(repr=False)


@dataclass(frozen=True)
class ResourceSnapshot:
    observations: tuple[ProviderObservation, ...]
    complete: bool = False
    error: NormalizedError | None = None


@dataclass(frozen=True)
class ExecutionSubject:
    """What an executor is asked to claim, before any output target exists.

    Canonical request/candidate facts only: no executor choice, no native
    option or identity, no core routing/recovery decision, no transient secret.
    """
    request_kind: str
    candidate: TransferCandidate

    @classmethod
    def of(cls, candidate: TransferCandidate) -> "ExecutionSubject":
        return cls(candidate.request_kind, candidate)


@dataclass(frozen=True)
class MaterializationPlan:
    """Core-owned output policy. ``FILE``: ``target`` is the exact final file
    and ``root`` the allowed boundary. ``COLLECTION``: ``root`` is a dedicated
    core-authorized directory and ``target`` is ``None``."""
    kind: MaterializationKind
    root: str
    target: str | None = None

    def __post_init__(self):
        if not isinstance(self.kind, MaterializationKind) or not self.root:
            raise ValueError("Materialization plans require a canonical kind and a root")
        if (self.kind == MaterializationKind.FILE) != (self.target is not None):
            raise ValueError("Only a FILE plan names an exact target")


@dataclass(frozen=True)
class ExecutionWork:
    """A subject after core allocated its materialization boundary.

    ``attempt_id`` is the durable execution attempt this work belongs to, when
    core has allocated one. It exists so an executor whose native transient
    material is scoped to a single attempt can name that material accurately in
    ``footprint()``; without it such an executor could only report material
    shared between attempts, and cleaning one attempt would destroy another's.

    ``None`` means core has not allocated an attempt yet, which is not a gap:
    before an attempt exists, that attempt's transient material cannot exist
    either, so there is nothing for it to report.
    """
    subject: ExecutionSubject
    materialization: MaterializationPlan
    attempt_id: str | None = None


@dataclass(frozen=True)
class ExecutorClaim:
    """An executor's pure applicability answer; ordering stays core-owned."""
    supported: bool


class ContinuationCapability(StrEnum):
    """What an executor can honor when core hands it existing artifact
    material. Declarations only: which of them a replacement uses is decided
    by the one core planner (``transfers.continuation``), never here."""
    FULL_RESTART = "full_restart"
    CONTIGUOUS_FROM_OFFSET = "contiguous_from_offset"
    ARBITRARY_RANGE_FETCH = "arbitrary_range_fetch"
    IMPORT_EXISTING_MATERIAL = "import_existing_material"
    # Observations carry the exact final-file ranges written so far
    # (``ExecutionObservation.material``); the only way uncommitted work can
    # ever become DebridPulse-valid before completion.
    EXPORT_MATERIAL_RANGES = "export_material_ranges"
    SOURCE_SEGMENT_TO_FINAL_RANGE = "source_segment_to_final_range"
    DESTINATION_AWARE_CONTINUATION = "destination_aware_continuation"
    # A natively quiesced job keeps its own private acquisition state and
    # continues exactly where it stopped when resumed (``PauseResume.resume``).
    # Disposable acceleration only: never DebridPulse material truth.
    NATIVE_PRIVATE_RESUME = "native_private_resume"
    # ``transfers.contracts.ExecutorSourceRetarget``: a quiesced native job may
    # keep its private acquisition state while the source supplying the same
    # logical artifact is replaced. A declaration only -- core decides whether
    # a retarget is appropriate; the executor answers whether one concrete
    # source pair can be retargeted safely.
    NATIVE_SOURCE_RETARGET = "native_source_retarget"
    # ``PauseResume.pause`` stops acquisition gracefully so the final completed
    # work can be checkpointed before the writer is fenced. An optimization:
    # lacking it never prevents a DebridPulse Pause.
    NATIVE_QUIESCE = "native_quiesce"
    # ``transfers.contracts.ContinuationBoundaryDiscovery``: the executor can
    # answer, for concrete source data, the largest offset at or below the
    # DP-valid prefix where it can continue exactly (e.g. where decoded source
    # segments begin). Data-dependent, unlike ``continuation_alignment``.
    BOUNDARY_DISCOVERY = "boundary_discovery"


class RetargetTruth(StrEnum):
    """What a native job inherited through a handoff is observed to serve now
    (``ExecutorSourceRetarget.retarget_truth``): only the replacement source
    (``RETARGETED``), still only the previous source (``ORIGINAL``), or
    anything that is not positively one of those (``UNKNOWN``)."""
    RETARGETED = "retargeted"
    ORIGINAL = "original"
    UNKNOWN = "unknown"


class ContinuationStrategy(StrEnum):
    FULL_RESTART = "full_restart"
    CONTIGUOUS_FROM_OFFSET = "contiguous_from_offset"
    # The new writer inherits the previous writer's quiesced native object of
    # the same executor and target; every DP-valid range is retained.
    NATIVE_STATE_HANDOFF = "native_state_handoff"


@dataclass(frozen=True)
class ContinuationPlan:
    """The one authorization a writer receives for an artifact's material.

    Produced only by ``transfers.continuation.plan_continuation`` and bound to
    the material generation it was computed against: an executor may reject a
    plan it cannot honor, but never retains more than ``retained`` nor writes
    outside ``authorized``. Ranges are ``[start, end)`` final-file offsets."""
    artifact_id: int
    material_generation: int
    geometry_version: int
    candidate_id: str
    executor_id: str
    strategy: ContinuationStrategy
    boundary: int
    retained: tuple[tuple[int, int], ...]
    discarded: tuple[tuple[int, int], ...]
    authorized: tuple[tuple[int, int], ...]
    expected_size: int | None
    reason: str
    # Provenance: the selected executor's declared continuation capabilities
    # (and alignment) this decision was made from.
    capabilities: tuple[str, ...] = ()
    alignment: int = 1
    # COLLECTION artifacts: per member file (relative path), the retained
    # contiguous boundary and what is reclassified. A member not listed keeps
    # nothing and may be written from 0; a listed member from its boundary.
    member_boundaries: tuple[tuple[str, int], ...] = ()
    member_discarded: tuple[tuple[str, tuple[tuple[int, int], ...]], ...] = ()

    def member_boundary(self, member: str) -> int:
        return dict(self.member_boundaries).get(member, 0)

    @property
    def retained_bytes(self) -> int:
        return (sum(end - start for start, end in self.retained)
                + sum(boundary for _member, boundary in self.member_boundaries))

    @property
    def discarded_bytes(self) -> int:
        return (sum(end - start for start, end in self.discarded)
                + sum(end - start for _member, ranges in self.member_discarded for start, end in ranges))

    def as_dict(self) -> dict:
        return {
            "artifact_id": self.artifact_id, "material_generation": self.material_generation,
            "geometry_version": self.geometry_version, "candidate_id": self.candidate_id,
            "executor_id": self.executor_id, "strategy": self.strategy.value, "boundary": self.boundary,
            "retained": [list(item) for item in self.retained], "discarded": [list(item) for item in self.discarded],
            "authorized": [list(item) for item in self.authorized], "expected_size": self.expected_size,
            "reason": self.reason, "capabilities": list(self.capabilities), "alignment": self.alignment,
            "member_boundaries": [[member, boundary] for member, boundary in self.member_boundaries],
            "member_discarded": [[member, [list(item) for item in ranges]] for member, ranges in self.member_discarded],
        }

    @classmethod
    def from_dict(cls, value: Mapping) -> "ContinuationPlan":
        def ranges(key):
            return tuple((int(start), int(end)) for start, end in value.get(key) or ())
        return cls(int(value["artifact_id"]), int(value["material_generation"]), int(value["geometry_version"]),
                   str(value["candidate_id"]), str(value["executor_id"]), ContinuationStrategy(value["strategy"]),
                   int(value["boundary"]), ranges("retained"), ranges("discarded"), ranges("authorized"),
                   None if value.get("expected_size") is None else int(value["expected_size"]), str(value["reason"]),
                   tuple(str(item) for item in value.get("capabilities") or ()), int(value.get("alignment") or 1),
                   tuple((str(member), int(boundary)) for member, boundary in value.get("member_boundaries") or ()),
                   tuple((str(member), tuple((int(start), int(end)) for start, end in ranges))
                         for member, ranges in value.get("member_discarded") or ()))


@dataclass(frozen=True)
class ExecutorCapabilities:
    """Static semantic guarantees an executor implementation declares.

    Each flag promises the matching neutral operation exists (validated at
    registration); none of them states that it is available right now --
    that is ``ExecutorHealth.available_runtime_capabilities``."""
    candidate_sampling: bool = False
    per_execution_pause: bool = False
    acquisition_gate: bool = False
    aggregate_bandwidth_ceiling: bool = False
    aggregate_throughput: bool = False
    native_assisted_retry: bool = False
    transient_input: bool = False
    # Read-only listing of one remote directory before any candidate exists,
    # behind the same trust and authentication the executor applies to
    # execution (``transfers.contracts.RemoteDiscovery``).
    remote_discovery: bool = False
    materialization_kinds: frozenset[MaterializationKind] = frozenset({MaterializationKind.FILE})
    # Material-continuation declarations (``ContinuationCapability``). Every
    # executor can at least restart from zero. ``continuation_alignment`` is
    # the byte grain at which this executor can begin continuing (1 = any
    # offset); the planner derives the maximal mutually safe boundary from it.
    continuation: frozenset[ContinuationCapability] = frozenset({ContinuationCapability.FULL_RESTART})
    continuation_alignment: int = 1

    def __post_init__(self):
        kinds = self.materialization_kinds
        if (not isinstance(kinds, frozenset) or not kinds
                or any(not isinstance(item, MaterializationKind) for item in kinds)):
            raise ValueError("Executors must declare canonical materialization kinds")
        continuation = self.continuation
        if (not isinstance(continuation, frozenset)
                or any(not isinstance(item, ContinuationCapability) for item in continuation)
                or ContinuationCapability.FULL_RESTART not in continuation):
            raise ValueError("Executors must declare canonical continuation capabilities, FULL_RESTART included")
        if isinstance(self.continuation_alignment, bool) or int(self.continuation_alignment) < 1:
            raise ValueError("Continuation alignment is a positive byte count")


class ExecutorRuntimeCapability(StrEnum):
    ACQUISITION_GATE = "acquisition_gate"
    AGGREGATE_BANDWIDTH_CEILING = "aggregate_bandwidth_ceiling"
    NATIVE_ASSISTED_RETRY = "native_assisted_retry"


@dataclass(frozen=True)
class ExecutorHealth:
    """Current executor truth. Runtime availability may only narrow the
    statically declared capabilities, never extend them."""
    reachable: bool
    ready: bool
    available_runtime_capabilities: frozenset[ExecutorRuntimeCapability] = frozenset()
    error: NormalizedError | None = None


@dataclass(frozen=True)
class ExecutorThroughput:
    """One instantaneous acquisition rate measured for a whole executor.

    Reported only by an executor whose measurement has no finer granularity
    than itself, so core can never be tempted to split one figure across
    executions or to add it to per-execution rates. ``observed`` is False
    whenever no current measurement exists -- an unreachable executor is
    unknown, never its last value.
    """
    bytes_per_second: int = 0
    observed: bool = False


@dataclass(frozen=True)
class ExecutorRuntimeControlResult:
    """Outcome of an assigned aggregate ceiling: ``effective`` is confirmed
    executor truth, ``None`` when it could not be proven."""
    requested_bytes_per_second: int
    effective_bytes_per_second: int | None
    error: NormalizedError | None = None


@dataclass(frozen=True)
class ExecutorGateResult:
    """Outcome of an executor-wide acquisition gate; ``effective_paused`` is
    confirmed truth, ``None`` when it could not be proven."""
    requested_paused: bool
    effective_paused: bool | None
    error: NormalizedError | None = None


@dataclass(frozen=True)
class ExecutionHandle:
    """Durable execution identity.

    ``executor_id`` and ``attempt_id`` are core identities. ``correlation`` is
    executor-owned, persisted before any native mutation and immutable;
    ``native`` is executor-owned, may be bound once (``None`` -> value) after
    native acceptance and is immutable afterwards. Core copies and compares
    both maps and never interprets them. Both are durable non-secret facts."""
    executor_id: str
    attempt_id: str
    correlation: Mapping[str, object] = field(repr=False)
    native: Mapping[str, object] | None = field(default=None, repr=False)

    def binds(self, bound: "ExecutionHandle") -> bool:
        """Whether ``bound`` is this prepared handle's one legal native binding."""
        return (self.native is None and bound.native is not None and bound.executor_id == self.executor_id
                and bound.attempt_id == self.attempt_id and bound.correlation == self.correlation)


@dataclass(frozen=True)
class ExecutionRequest:
    work: ExecutionWork
    attempt_id: str
    paused: bool = False
    # The core continuation plan this writer is authorized under. ``None``
    # authorizes no existing material at all (a restart from zero).
    continuation: ContinuationPlan | None = None

    def __post_init__(self):
        # One attempt identity, never two. Once a request exists its attempt is
        # allocated, so work that names a different attempt -- or none at all --
        # would make ``footprint()`` and ``start()`` disagree about which
        # attempt's native material is being planned.
        if self.work.attempt_id != self.attempt_id:
            raise ValueError("Execution work must belong to this execution attempt")


@dataclass(frozen=True)
class ExecutionFootprint:
    """Native transient material an executor may create beside or inside the
    core plan (and later resume from or clean). Never final material.

    ``transient_paths`` are single native files (a resume/control file);
    ``transient_trees`` are native directories whose whole subtree is
    transient (a partial-transfer directory). Core validates both inside
    download storage, excludes both from materialization, and removes them
    only under the execution's durable material ownership."""
    transient_paths: tuple[str, ...] = ()
    transient_trees: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionActivity:
    """Facts independent of lifecycle state.

    ``network_active``: network acquisition is observed now.
    ``bandwidth_reservation_required``: the executor may consume download
    bandwidth for this execution without another core admission transition.
    ``progress_expected``: acquisition progress is expected (stall detection)."""
    network_active: bool = False
    bandwidth_reservation_required: bool = False
    progress_expected: bool = False


class ExecutionControl(StrEnum):
    PAUSE = "pause"
    RESUME = "resume"


@dataclass(frozen=True)
class MaterializedEntry:
    relative_path: str
    bytes: int | None = None


@dataclass(frozen=True)
class MaterializationResult:
    """What a succeeded execution reports it produced, relative to its plan."""
    kind: MaterializationKind
    entries: tuple[MaterializedEntry, ...]


@dataclass(frozen=True)
class ExecutionObservation:
    handle: ExecutionHandle
    state: ExecutionState
    progress: TransferProgress = field(default_factory=TransferProgress)
    error: NormalizedError | None = None
    activity: ExecutionActivity = field(default_factory=ExecutionActivity)
    # Controls valid for this execution now (static capability permitting).
    controls: frozenset[ExecutionControl] = frozenset()
    # Supplied only with SUCCEEDED; core verifies it before trusting it.
    materialization: MaterializationResult | None = None
    # ``EXPORT_MATERIAL_RANGES``: exact ``[start, end)`` final-file ranges this
    # writer reports completely written. Evidence for a core checkpoint only --
    # it never becomes DebridPulse-valid by being reported (``None`` = the
    # executor reports none).
    material: tuple[tuple[int, int], ...] | None = None
    # The same evidence for a COLLECTION: per member file (relative path to
    # the collection root), the exact ranges completely written.
    member_material: tuple[tuple[str, tuple[tuple[int, int], ...]], ...] | None = None

    @property
    def reports_material(self) -> bool:
        return self.material is not None or self.member_material is not None

    @property
    def resumable(self) -> bool:
        """Native work exists and has not reached a terminal state."""
        return self.state in {ExecutionState.QUEUED, ExecutionState.RUNNING, ExecutionState.PAUSED}

    @property
    def stopped(self) -> bool:
        """Positively observed truth that the native writer is not running."""
        return self.state in {ExecutionState.SUCCEEDED, ExecutionState.FAILED, ExecutionState.CANCELLED,
                              ExecutionState.ABSENT}


@dataclass(frozen=True)
class ExecutionSnapshot:
    observations: tuple[ExecutionObservation, ...]
    error: NormalizedError | None = None


@dataclass(frozen=True)
class TransferOutcome:
    kind: OutcomeKind
    error: NormalizedError | None = None
    cancellation_initiator: CancellationInitiator | None = None
    detail: str = ""


@dataclass(frozen=True)
class CleanupDirective:
    resource: ProviderResource
    authority: CleanupAuthority = CleanupAuthority.OWNED


@dataclass(frozen=True)
class HealthObservation:
    healthy: bool
    error: NormalizedError | None = None


@dataclass(frozen=True)
class Transfer:
    id: int
    name: str
    state: TransferState
    fingerprint: str = ""
    source: str = ""
    priority: int = 0
    paused: bool = False
    # DP-valid completion percentage; ``None`` while any unfinished artifact's
    # size is unknown (unavailable, never a fabricated 0%).
    progress: float | None = 0.0
    error: NormalizedError | None = None
    epoch: int = 0


@dataclass(frozen=True)
class RequestRecord:
    id: str
    transfer_id: int
    request: TransferRequest
    state: str
    parent_id: str | None = None
    resource: ProviderResource | None = None
    attempts: int = 0
    retry_at: float = 0
    error: NormalizedError | None = None
    entry: SourceEntry | None = None
    # The provider's alternate reading of ``request`` that discovery
    # established (``DiscoveryRequest.alternate``); ``request`` stays exactly
    # what the operator submitted.
    interpretation: TransferRequest | None = None

    @property
    def resolvable(self) -> TransferRequest:
        """The request as the provider resolves it: its established
        interpretation when it has one, otherwise the request itself."""
        return self.interpretation if self.interpretation is not None else self.request


class SizeKnowledge(StrEnum):
    """Canonical payload-size-knowledge fact (DP 1.0.12 canonical
    lifecycle/recovery/completion rework, Section 3.3): ``SIZE_UNKNOWN``,
    ``SIZE_KNOWN(0)`` and ``SIZE_KNOWN(N>0)`` are three distinct facts. ``0``
    must never simultaneously mean "no size evidence" and "affirmatively
    zero bytes" -- see ``transfers.filesystem.size_knowledge`` for the one
    canonical resolver that produces this fact from candidate/executor
    evidence.
    """
    UNKNOWN = "unknown"
    KNOWN_ZERO = "known_zero"
    KNOWN_POSITIVE = "known_positive"

    @classmethod
    def durable(cls, expected_bytes, stored=None) -> "SizeKnowledge":
        """The ONE interpretation of an artifact's durable size storage.

        Durable size truth is stored as a byte count plus a nullable
        size-knowledge column, and this is the only function that turns that
        pair back into the canonical fact. The two are not independent
        authorities: the byte count is the authority whenever it is positive,
        and the column is consulted only for the case a number genuinely
        cannot express -- whether a non-positive value means "no size
        evidence" or "affirmatively zero bytes".

        * positive byte count => ``KNOWN_POSITIVE``, whatever the column says,
          so positive evidence can never be downgraded by stale or defaulted
          bookkeeping;
        * otherwise an explicit durable ``known_zero`` => ``KNOWN_ZERO``. Only
          a verified completion whose trusted affirmative-zero evidence the
          stable local payload proved ever writes that value;
        * otherwise ``UNKNOWN`` -- which is exactly what every row written
          before this column existed reads as, since they are all ``NULL``.
          A historical numeric ``0`` is therefore never reinterpreted as a
          legitimate empty payload, and nothing backfills one.
        """
        if positive_size(expected_bytes) is not None:
            return cls.KNOWN_POSITIVE
        return cls.KNOWN_ZERO if stored == cls.KNOWN_ZERO.value else cls.UNKNOWN


class MaterializationAdmissionKind(StrEnum):
    """Universal execution-admission decision for one artifact's dispatch.

    Derived from durable file-selection generation/commitment/membership facts
    (``transfers.file_selection`` / ``transfers.repository
    .materialization_authorization``). Never a transfer-global mutable
    ``selection_authorized`` flag -- always recomputed from the same durable
    facts the selection lifecycle already owns.
    """
    PROCEED = "proceed"
    HOLD = "hold"
    STALE = "stale"


@dataclass(frozen=True)
class MaterializationAdmission:
    kind: MaterializationAdmissionKind
    # The selection-generation id (``transfer_file_selections.id``) this
    # decision was evaluated against, when a generation applies. ``None`` for
    # PROCEED decisions with no applicable generation (never interactive).
    authority_generation: str | None = None


@dataclass(frozen=True)
class ResolverArtifactIdentityEvidence:
    """Neutral resolver/provider-asserted identity fact about a candidate.

    States only what a resolver reported -- never a duplicate/equivalence
    decision (``transfers.mirrors`` owns that). ``resolved_name`` must be the
    name the provider's resolution response itself asserted, never a
    submitted-URL basename or other non-resolver fallback; ``""`` when the
    provider asserts none.

    ``object_coordinate`` is the authoritative remote coordinate of the
    candidate within its server scope (``transfers.requests
    .remote_object_coordinate``), stated by the provider from the facts its
    classification established. It is an address, not immutable identity (a
    server may replace the contents at one path): equal coordinates only make
    two SAME-source candidates pairable for ordinary material proof.
    """
    resolved_name: str
    exact_bytes: int
    object_coordinate: str = ""


@dataclass(frozen=True)
class Artifact:
    id: int
    transfer_id: int
    request_id: str
    name: str
    target: str
    expected_bytes: int
    state: str
    candidates: tuple[TransferCandidate, ...] = field(repr=False)
    selected: int = 0
    execution: ExecutionHandle | None = None
    retries: int = 0
    retry_at: float = 0
    error: NormalizedError | None = None
    # The canonical durable size fact this artifact's persisted size means,
    # reconstructed on load by ``SizeKnowledge.durable`` from the byte count and
    # the nullable durable column. Never set by hand: ``UNKNOWN`` is the correct
    # value for an artifact whose size nothing has affirmatively established.
    size_knowledge: SizeKnowledge = SizeKnowledge.UNKNOWN


@dataclass(frozen=True)
class ResolutionAttempt:
    id: str
    request_id: str
    provider_id: str
    state: str
    error: NormalizedError | None = None


@dataclass(frozen=True)
class ExecutionAttempt:
    handle: ExecutionHandle
    transfer_id: int
    artifact_id: int
    state: str
    progress: TransferProgress = field(default_factory=TransferProgress)
    error: NormalizedError | None = None
    candidate: TransferCandidate | None = field(default=None, repr=False)
