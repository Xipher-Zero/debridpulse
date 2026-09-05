"""Canonical values crossing source, executor and post-processing boundaries."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping
from uuid import uuid4

from transfers.errors import NormalizedError


def new_identity() -> str:
    return uuid4().hex


class Capability(StrEnum):
    RESOLVE = "resolve"
    AVAILABILITY = "availability"
    METADATA = "metadata"
    REFRESH = "refresh"
    ALTERNATES = "alternates"
    RESOURCE_CREATION = "resource_creation"
    RESOURCE_LOOKUP = "resource_lookup"
    INVENTORY = "inventory"
    CLEANUP = "cleanup"
    HEALTH = "health"
    INTEGRITY = "integrity"
    PAUSE = "pause"
    RESUME = "resume"
    RECONCILE = "reconcile"


class InputReason(StrEnum):
    AUTH_REQUIRED = "auth_required"


class InputOrigin(StrEnum):
    PROVIDER = "provider"
    EXECUTOR = "executor"


class InputMethod(StrEnum):
    USERNAME_PASSWORD = "username_password"
    USERNAME_PRIVATE_KEY = "username_private_key"


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
        else:
            raise ValueError("Unsupported authentication method")
        if actual != expected:
            raise ValueError("Authentication method fields do not match the canonical contract")


@dataclass(frozen=True)
class InputRequirement:
    reason: InputReason
    methods: tuple[InputMethodDescriptor, ...]

    def __post_init__(self):
        if self.reason != InputReason.AUTH_REQUIRED:
            raise ValueError("Unsupported input-required reason")
        if not self.methods or len({item.method for item in self.methods}) != len(self.methods):
            raise ValueError("Authentication challenges require unique accepted methods")


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

    @property
    def requirement(self) -> InputRequirement:
        return InputRequirement(self.reason, self.methods)


class ResourceState(StrEnum):
    PREPARING = "preparing"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    ABSENT = "absent"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


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
    QUEUED = "queued"
    TRANSFERRING = "transferring"
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
    payload: str | bytes = field(repr=False)
    name: str = ""
    fingerprint: str = ""
    preferred_provider: str | None = None


@dataclass(frozen=True)
class IntegrationDescriptor:
    id: str
    name: str
    capabilities: frozenset[Capability]
    request_types: frozenset[str] = frozenset()
    schemes: frozenset[str] = frozenset()
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


@dataclass(frozen=True)
class TransferProgress:
    total_bytes: int = 0
    completed_bytes: int = 0
    bytes_per_second: int = 0

    @property
    def percentage(self) -> float:
        return min(100.0, max(0.0, self.completed_bytes / self.total_bytes * 100)) if self.total_bytes > 0 else 0.0


@dataclass(frozen=True)
class ProviderObservation:
    resource: ProviderResource
    state: ResourceState
    name: str = ""
    fingerprint: str = ""
    progress: TransferProgress = field(default_factory=TransferProgress)
    error: NormalizedError | None = None
    request: TransferRequest | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ResolutionResult:
    """Candidates are alternatives for one request; manifests describe members."""
    state: ResourceState
    candidates: tuple[TransferCandidate, ...] = ()
    observation: ProviderObservation | None = None
    error: NormalizedError | None = None
    input_required: InputRequirement | None = None


@dataclass(frozen=True)
class SourceEntry:
    """Metadata for an unresolved manifest member; never dispatched as a candidate."""
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
class ExecutionHandle:
    executor_id: str
    context: Mapping[str, object] = field(repr=False)
    attempt_id: str = field(default_factory=new_identity)


@dataclass(frozen=True)
class ExecutionRequest:
    candidate: TransferCandidate
    target: str
    attempt_id: str
    paused: bool = False


@dataclass(frozen=True)
class ExecutionObservation:
    handle: ExecutionHandle
    state: ExecutionState
    progress: TransferProgress = field(default_factory=TransferProgress)
    paths: tuple[str, ...] = ()
    error: NormalizedError | None = None

    @property
    def occupies_slot(self) -> bool:
        return self.state in {ExecutionState.QUEUED, ExecutionState.TRANSFERRING}

    @property
    def resumable(self) -> bool:
        return self.state in {ExecutionState.QUEUED, ExecutionState.TRANSFERRING, ExecutionState.PAUSED}


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
    progress: float = 0.0
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
