"""Stable failure semantics shared by policy, persistence and presentation.

Native diagnostics are opaque troubleshooting data. They are sanitized when an
envelope is constructed, before they can reach persistence or an event consumer.
Recovery fields remain in the canonical envelope for compatibility, but factual
normalization never selects a recovery action or operator escalation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import json
import re
import math
from typing import Mapping
from types import MappingProxyType


class Domain(StrEnum):
    REQUEST = "request"
    PROVIDER = "provider"
    RESOLUTION = "resolution"
    EXECUTOR = "executor"
    NETWORK = "network"
    SECURITY = "security"
    LOCAL_RESOURCE = "local_resource"
    INTEGRITY = "integrity"
    LIFECYCLE = "lifecycle"
    RECONCILIATION = "reconciliation"
    CLEANUP = "cleanup"
    POST_PROCESSING = "post_processing"
    INTERNAL = "internal"


class Category(StrEnum):
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_REQUEST = "unsupported_request"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    INVALID_CONFIGURATION = "invalid_configuration"
    AUTHENTICATION_FAILED = "authentication_failed"
    AUTHORIZATION_FAILED = "authorization_failed"
    CREDENTIAL_MISSING = "credential_missing"
    CREDENTIAL_EXPIRED = "credential_expired"
    CREDENTIAL_INVALID = "credential_invalid"
    SOURCE_NOT_FOUND = "source_not_found"
    SOURCE_UNAVAILABLE = "source_unavailable"
    SOURCE_TEMPORARILY_UNAVAILABLE = "source_temporarily_unavailable"
    SOURCE_EXPIRED = "source_expired"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_DEGRADED = "provider_degraded"
    PROVIDER_MAINTENANCE = "provider_maintenance"
    RESOURCE_NOT_FOUND = "resource_not_found"
    RESOURCE_EXPIRED = "resource_expired"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXCEEDED = "quota_exceeded"
    CONCURRENCY_LIMITED = "concurrency_limited"
    ACCOUNT_LIMITED = "account_limited"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    RESOLUTION_FAILED = "resolution_failed"
    RESOLUTION_TEMPORARILY_FAILED = "resolution_temporarily_failed"
    NO_TRANSFER_CANDIDATE = "no_transfer_candidate"
    CANDIDATE_EXPIRED = "candidate_expired"
    CANDIDATE_REJECTED = "candidate_rejected"
    DNS_FAILURE = "dns_failure"
    CONNECTION_FAILED = "connection_failed"
    # The endpoint actively refused the connection: nothing serves that port.
    CONNECTION_REFUSED = "connection_refused"
    CONNECTION_TIMEOUT = "connection_timeout"
    READ_TIMEOUT = "read_timeout"
    REMOTE_RESET = "remote_reset"
    PROTOCOL_ERROR = "protocol_error"
    TLS_FAILURE = "tls_failure"
    HOST_KEY_FAILURE = "host_key_failure"
    DESTINATION_BLOCKED = "destination_blocked"
    EGRESS_POLICY_VIOLATION = "egress_policy_violation"
    UNSAFE_REDIRECT = "unsafe_redirect"
    TLS_IDENTITY_FAILURE = "tls_identity_failure"
    PATH_POLICY_VIOLATION = "path_policy_violation"
    SECURITY_POLICY_REJECTED = "security_policy_rejected"
    EXECUTOR_UNAVAILABLE = "executor_unavailable"
    EXECUTOR_REJECTED = "executor_rejected"
    TRANSFER_FAILED = "transfer_failed"
    TRANSFER_STALLED = "transfer_stalled"
    TRANSFER_INTERRUPTED = "transfer_interrupted"
    REMOTE_READ_FAILED = "remote_read_failed"
    REMOTE_WRITE_FAILED = "remote_write_failed"
    SIZE_MISMATCH = "size_mismatch"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    CONTENT_INVALID = "content_invalid"
    MATERIALIZATION_FAILED = "materialization_failed"
    LOCAL_PATH_CONFLICT = "local_path_conflict"
    DISK_FULL = "disk_full"
    PERMISSION_DENIED = "permission_denied"
    LOCAL_IO_FAILURE = "local_io_failure"
    PATH_UNAVAILABLE = "path_unavailable"
    LOCAL_RESOURCE_EXHAUSTED = "local_resource_exhausted"
    APPLICATION_STORAGE_FULL = "application_storage_full"
    APPLICATION_STORAGE_READ_ONLY = "application_storage_read_only"
    APPLICATION_STORAGE_UNAVAILABLE = "application_storage_unavailable"
    DOWNLOAD_STORAGE_FULL = "download_storage_full"
    DOWNLOAD_STORAGE_READ_ONLY = "download_storage_read_only"
    DOWNLOAD_STORAGE_UNAVAILABLE = "download_storage_unavailable"
    OWNERSHIP_CONFLICT = "ownership_conflict"
    RESOURCE_STATE_CONFLICT = "resource_state_conflict"
    RECONCILIATION_FAILED = "reconciliation_failed"
    ORPHANED_RESOURCE = "orphaned_resource"
    RECOVERY_FAILED = "recovery_failed"
    STATE_INCONSISTENT = "state_inconsistent"
    REMOTE_CLEANUP_FAILED = "remote_cleanup_failed"
    LOCAL_CLEANUP_FAILED = "local_cleanup_failed"
    POST_PROCESSING_FAILED = "post_processing_failed"
    EXTRACTION_FAILED = "extraction_failed"
    PROVIDER_PROTOCOL_VIOLATION = "provider_protocol_violation"
    EXECUTOR_PROTOCOL_VIOLATION = "executor_protocol_violation"
    INVALID_ADAPTER_RESPONSE = "invalid_adapter_response"
    UNMAPPED_PROVIDER_ERROR = "unmapped_provider_error"
    UNMAPPED_EXECUTOR_ERROR = "unmapped_executor_error"
    INTERNAL_ERROR = "internal_error"


class Stage(StrEnum):
    SUBMISSION = "submission"
    RESOLUTION = "resolution"
    CANDIDATE_PREPARATION = "candidate_preparation"
    QUEUE = "queue"
    EXECUTION = "execution"
    VERIFICATION = "verification"
    POST_PROCESSING = "post_processing"
    RECONCILIATION = "reconciliation"
    CLEANUP = "cleanup"


class Retryability(StrEnum):
    NEVER = "never"
    IMMEDIATE = "immediate"
    BACKOFF = "backoff"
    AFTER_REAUTH = "after_reauth"
    AFTER_RERESOLUTION = "after_reresolution"
    AFTER_RESOURCE_CHANGE = "after_resource_change"
    UNKNOWN = "unknown"


class Recovery(StrEnum):
    NONE = "none"
    RETRY = "retry"
    BACKOFF = "backoff"
    RERESOLVE = "reresolve"
    TRY_ALTERNATE_CANDIDATE = "try_alternate_candidate"
    TRY_ALTERNATE_PROVIDER = "try_alternate_provider"
    REAUTHENTICATE = "reauthenticate"
    RECONCILE = "reconcile"
    REQUIRE_OPERATOR = "require_operator"
    FAIL = "fail"


class Origin(StrEnum):
    USER = "user"
    CORE = "core"
    PROVIDER = "provider"
    EXECUTOR = "executor"
    REMOTE_SOURCE = "remote_source"
    LOCAL_SYSTEM = "local_system"
    SECURITY_POLICY = "security_policy"
    POST_PROCESSOR = "post_processor"


class Permanence(StrEnum):
    TEMPORARY = "temporary"
    PERMANENT = "permanent"
    UNKNOWN = "unknown"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class MutationOutcome(StrEnum):
    """Whether a failed operation may still have changed remote state.

    Only an operation that creates something remotely can be uncertain, and
    only its emitter knows: a structured refusal, or a request that provably
    never left, is ``NOT_COMMITTED``; an answer that arrived but cannot be
    read, or a request lost once it could have been processed, cannot rule the
    creation out and is ``UNCERTAIN``. An exception is never by itself proof
    that nothing was created."""
    NOT_COMMITTED = "not_committed"
    UNCERTAIN = "uncertain"


class EvidenceBasis(StrEnum):
    STRUCTURED = "structured"
    TYPED_EXCEPTION = "typed_exception"
    NATIVE_CODE = "native_code"
    DIAGNOSTIC = "diagnostic"
    UNKNOWN = "unknown"


_SECURITY_CATEGORIES = frozenset({
    Category.DESTINATION_BLOCKED, Category.EGRESS_POLICY_VIOLATION,
    Category.UNSAFE_REDIRECT, Category.TLS_IDENTITY_FAILURE,
    Category.HOST_KEY_FAILURE, Category.PATH_POLICY_VIOLATION,
    Category.SECURITY_POLICY_REJECTED,
})
_UNKNOWN_CATEGORIES = frozenset({
    Category.UNMAPPED_PROVIDER_ERROR, Category.UNMAPPED_EXECUTOR_ERROR,
    Category.INVALID_ADAPTER_RESPONSE, Category.PROVIDER_PROTOCOL_VIOLATION,
    Category.EXECUTOR_PROTOCOL_VIOLATION, Category.INTERNAL_ERROR,
})
_SECRET_KEY = re.compile(
    r"(?i)(password|passwd|secret|token|cookie|authorization|credential|api.?key|"
    r"private.?key|signed|signature|headers?|uri|url|payload|body)"
)
_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*:(?://|\?)[^\s<>\"']*")
_KEY_BLOCK = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)", re.S)
_HEADER = re.compile(r"(?im)\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*[:=][^\r\n]*")
_BEARER = re.compile(r"(?i)\b(?:bearer|basic)\s+[a-z0-9_+/=.\-]+")
_PAIR = re.compile(
    r"(?i)\b(?:password|passwd|secret|token|api[_-]?key|apikey|credential|signature)"
    r"[\s\"']*[:=][\s\"']*[^\s,;&}\]\"']+"
)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def safe_diagnostic(value: object, *, secrets: tuple[str, ...] = (), limit: int = 500) -> str:
    text = str(value or "")
    for secret in sorted((str(x) for x in secrets if x), key=len, reverse=True):
        text = text.replace(secret, "<redacted>")
    text = _KEY_BLOCK.sub("<private-key>", text)
    text = _HEADER.sub("<credential-header>", text)
    text = _BEARER.sub("<credential>", text)
    text = _URL.sub("<capability-url>", text)
    text = _PAIR.sub("<redacted>", text)
    text = _CONTROL.sub(" ", text)
    return text[:limit]


def safe_context(value: Mapping | None, *, secrets: tuple[str, ...] = ()) -> dict:
    """Only bounded scalar diagnostics cross this boundary; no native payloads."""
    result = {}
    for key, item in list((value or {}).items())[:24]:
        key = str(key)
        if _SECRET_KEY.search(key) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", key):
            continue
        if item is None or isinstance(item, (bool, int, float)):
            result[key] = item
        elif isinstance(item, str):
            result[key] = safe_diagnostic(item, secrets=secrets, limit=200)
    return result


# Bounds of ``NormalizedError.diagnostic_evidence``. The node budget bounds the
# work and leaves room for two full 64-record lists; the byte ceiling is what
# bounds the durable size. It is measured on the compact JSON encoding as it is
# persisted (``codec.dump``: non-ASCII escaped), which is never shorter than
# its UTF-8 form, so both stay within it.
EVIDENCE_MAX_NODES = 1024
EVIDENCE_MAX_DEPTH = 5
EVIDENCE_MAX_ENTRIES = 64
EVIDENCE_MAX_TEXT = 256
EVIDENCE_MAX_BYTES = 16384
# The one evidence-wide fact that something was cut.
EVIDENCE_TRUNCATED = "_truncated"
_EVIDENCE_KEY = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_EVIDENCE_SECRET_KEY = re.compile(r"(?i)(passphrase|session|bearer|access.?key)")
_EVIDENCE_INT = 2 ** 63


def _evidence_bytes(value) -> int:
    return len(json.dumps(value, separators=(",", ":"), sort_keys=True))


class _EvidenceBudget:
    def __init__(self):
        self.nodes = EVIDENCE_MAX_NODES
        self.truncated = False


def _evidence_value(value, depth: int, budget: _EvidenceBudget, secrets: tuple[str, ...]):
    budget.nodes -= 1
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if -_EVIDENCE_INT < value < _EVIDENCE_INT else "<large-int>"
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (Mapping, list, tuple)):
        if depth >= EVIDENCE_MAX_DEPTH:
            budget.truncated = True
            return "<truncated>"
        if isinstance(value, Mapping):
            return _evidence_mapping(list(value.items()), depth, budget, secrets)
        result = []
        for index, item in enumerate(value):
            if index >= EVIDENCE_MAX_ENTRIES or budget.nodes <= 0:
                budget.truncated = True
                break
            result.append(_evidence_value(item, depth + 1, budget, secrets))
        return result
    # Free text, and only the type of anything that is not plain data: never
    # a repr, never bytes.
    text = value if isinstance(value, str) else f"<{type(value).__name__}>"
    text = text.encode("utf-8", "replace").decode("utf-8")
    return safe_diagnostic(text, secrets=secrets, limit=EVIDENCE_MAX_TEXT)


def _evidence_mapping(items: list, depth: int, budget: _EvidenceBudget, secrets: tuple[str, ...]) -> dict:
    result = {}
    for index, (key, item) in enumerate(items):
        if index >= EVIDENCE_MAX_ENTRIES or budget.nodes <= 0:
            budget.truncated = True
            break
        if not isinstance(key, str) or not _EVIDENCE_KEY.fullmatch(key):
            continue
        if _SECRET_KEY.search(key) or _EVIDENCE_SECRET_KEY.search(key):
            budget.nodes -= 1
            result[key] = "<redacted>"
            continue
        result[key] = _evidence_value(item, depth + 1, budget, secrets)
    return result


def _evidence_within_bytes(evidence: dict) -> dict:
    """Deterministically prune whole list entries and mapping values -- the
    largest top-level container first, from its end, so native order keeps
    its prefix -- and only then top-level scalars, largest first, until the
    compact encoding fits. Sizes are tracked exactly, never re-encoded."""
    if _evidence_bytes(evidence) <= EVIDENCE_MAX_BYTES:
        return evidence
    evidence[EVIDENCE_TRUNCATED] = True
    size = _evidence_bytes(evidence)
    sizes = {key: _evidence_bytes(item) for key, item in evidence.items() if isinstance(item, (dict, list))}
    while size > EVIDENCE_MAX_BYTES:
        containers = [key for key in sorted(sizes) if evidence[key]]
        if containers:
            key = max(containers, key=lambda name: sizes[name])
            container = evidence[key]
            if isinstance(container, list):
                delta = _evidence_bytes(container.pop())
            else:
                last = sorted(container)[-1]
                delta = _evidence_bytes(last) + 1 + _evidence_bytes(container.pop(last))
            delta += 1 if container else 0
            sizes[key] -= delta
        else:
            scalars = [key for key in sorted(evidence) if key != EVIDENCE_TRUNCATED and key not in sizes]
            if not scalars:
                return {EVIDENCE_TRUNCATED: True}
            key = max(scalars, key=lambda name: _evidence_bytes(name) + _evidence_bytes(evidence[name]))
            delta = _evidence_bytes(key) + 1 + _evidence_bytes(evidence.pop(key)) + 1
        size -= delta
    return evidence


def safe_diagnostic_evidence(value: Mapping | None, *, secrets: tuple[str, ...] = ()) -> dict:
    """THE durable-safety boundary for nested forensic evidence, as plain JSON
    data. Only mappings, lists, ``None``, booleans, integers, finite floats
    and bounded free text cross it; a credential- or capability-named key
    keeps only ``<redacted>``, free text gets ``safe_diagnostic`` (a URL never
    survives), and the whole value is bounded (nodes, depth, entries per
    level, text, and ``EVIDENCE_MAX_BYTES`` of compact JSON), stating
    ``_truncated`` when cut. Top-level scalars -- the cardinality facts -- are
    kept before any container. Never raises for size; idempotent.

    ``safe_context`` stays the separate scalar-only contract."""
    if not isinstance(value, Mapping):
        return {}
    items = list(value.items())
    truncated = value.get(EVIDENCE_TRUNCATED) is True
    items = [item for item in items if item[0] != EVIDENCE_TRUNCATED]
    containers = [item for item in items if isinstance(item[1], (Mapping, list, tuple))]
    scalars = [item for item in items if not isinstance(item[1], (Mapping, list, tuple))]
    budget = _EvidenceBudget()
    budget.nodes -= 1
    evidence = _evidence_mapping(scalars, 0, budget, secrets)
    if len(scalars) > EVIDENCE_MAX_ENTRIES:
        containers = []
        budget.truncated = True
    containers = containers[:EVIDENCE_MAX_ENTRIES - len(evidence)] if containers else []
    if len(evidence) + len(containers) < len(items):
        budget.truncated = True
    evidence.update(_evidence_mapping(containers, 0, budget, secrets))
    if truncated or budget.truncated:
        evidence[EVIDENCE_TRUNCATED] = True
    return _evidence_within_bytes(evidence)


def _frozen_evidence(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _frozen_evidence(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_frozen_evidence(item) for item in value)
    return value


def _thawed_evidence(value):
    if isinstance(value, Mapping):
        return {key: _thawed_evidence(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thawed_evidence(item) for item in value]
    return value


@dataclass(frozen=True)
class NormalizedError:
    domain: Domain
    category: Category
    stage: Stage
    retryability: Retryability = Retryability.UNKNOWN
    recovery: Recovery = Recovery.NONE
    origin: Origin = Origin.CORE
    permanence: Permanence = Permanence.UNKNOWN
    severity: str = "error"
    operator_action_required: bool = False
    integration_id: str = ""
    native_code: str = ""
    diagnostic: str = ""
    context: Mapping = field(default_factory=dict)
    retry_after_seconds: float | None = None
    confidence: Confidence = Confidence.UNKNOWN
    evidence_basis: EvidenceBasis = EvidenceBasis.UNKNOWN
    mutation: MutationOutcome = MutationOutcome.NOT_COMMITTED
    # Bounded forensic facts that were in memory when this failure was
    # created: diagnostics only. Never compared, never part of a failure
    # signature, never read by policy, recovery, routing or presentation; only
    # the durable (``diagnostics=True``) encoding carries it.
    diagnostic_evidence: Mapping = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self):
        # Reconstructed/persisted values are canonicalized here. This layer may
        # harden semantic facts, but it deliberately does not select recovery or
        # operator action; those compatibility/policy decisions are core-owned.
        for name, enum in (("domain", Domain), ("category", Category), ("stage", Stage),
                           ("retryability", Retryability), ("recovery", Recovery),
                           ("origin", Origin), ("permanence", Permanence),
                           ("confidence", Confidence), ("evidence_basis", EvidenceBasis),
                           ("mutation", MutationOutcome)):
            object.__setattr__(self, name, enum(getattr(self, name)))
        if self.domain == Domain.SECURITY or self.category in _SECURITY_CATEGORIES:
            object.__setattr__(self, "domain", Domain.SECURITY)
            object.__setattr__(self, "origin", Origin.SECURITY_POLICY)
            object.__setattr__(self, "retryability", Retryability.NEVER)
        elif self.category in _UNKNOWN_CATEGORIES:
            object.__setattr__(self, "retryability", Retryability.UNKNOWN)
            object.__setattr__(self, "permanence", Permanence.UNKNOWN)
        for name, limit in (("integration_id", 128), ("native_code", 128), ("diagnostic", 500)):
            object.__setattr__(self, name, safe_diagnostic(getattr(self, name), limit=limit))
        object.__setattr__(self, "context", MappingProxyType(safe_context(self.context)))
        object.__setattr__(self, "diagnostic_evidence",
                           _frozen_evidence(safe_diagnostic_evidence(self.diagnostic_evidence)))
        if self.severity not in {"info", "warning", "error", "critical"}:
            object.__setattr__(self, "severity", "error")
        if self.retry_after_seconds is not None:
            delay = float(self.retry_after_seconds)
            if not math.isfinite(delay):
                raise ValueError("Retry delay must be finite")
            object.__setattr__(self, "retry_after_seconds", max(0.0, delay))

    @property
    def message(self) -> str:
        if self.category == Category.UNMAPPED_EXECUTOR_ERROR:
            return "Download failed"
        return self.category.value.replace("_", " ").capitalize()

    def as_dict(self, *, diagnostics: bool = False) -> dict:
        payload = {name: getattr(self, name) for name in self.__dataclass_fields__}
        payload["context"] = dict(self.context)
        payload["message"] = self.message
        # Present only when there is evidence: an error without any keeps the
        # exact durable encoding it had before the field existed.
        evidence = payload.pop("diagnostic_evidence")
        if diagnostics and evidence:
            payload["diagnostic_evidence"] = _thawed_evidence(evidence)
        if not diagnostics:
            for field_name in ("native_code", "diagnostic", "context"):
                payload.pop(field_name)
        return payload

    @classmethod
    def from_dict(cls, value: Mapping) -> NormalizedError:
        fields = cls.__dataclass_fields__
        return cls(**{key: item for key, item in value.items() if key in fields})


class TransferError(Exception):
    """Only normalized semantic failures are raised across integration boundaries."""
    def __init__(self, error: NormalizedError):
        self.error = error
        super().__init__(error.message)


def unknown_failure(exc: Exception, *, integration_id: str, domain: Domain,
                    stage: Stage, secrets: tuple[str, ...] = ()) -> NormalizedError:
    category = (Category.UNMAPPED_PROVIDER_ERROR if domain == Domain.PROVIDER else
                Category.UNMAPPED_EXECUTOR_ERROR if domain == Domain.EXECUTOR else
                Category.INTERNAL_ERROR)
    origin = Origin.PROVIDER if domain == Domain.PROVIDER else Origin.EXECUTOR if domain == Domain.EXECUTOR else Origin.CORE
    return NormalizedError(domain, category, stage, origin=origin,
                           integration_id=integration_id,
                           diagnostic=safe_diagnostic(exc, secrets=secrets),
                           confidence=Confidence.LOW,
                           evidence_basis=EvidenceBasis.TYPED_EXCEPTION)