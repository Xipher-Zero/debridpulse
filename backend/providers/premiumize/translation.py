"""Premiumize-native answers terminate here.

Error meanings follow Premiumize's stable error ``code`` table; a code not
listed follows the explicit unmapped path, never a speculative retry, and a
human ``message`` is never read for meaning. Provider output is factual;
recovery policy -- including whether a failure exhausts Premiumize for a
request -- is owned by the universal core.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from uuid import NAMESPACE_URL, uuid5

import aiohttp

from providers.premiumize.client import (
    CREDENTIAL_MISSING, PremiumizeAPIError, PremiumizeProtocolError, native_id,
)
from services.network_safety import UnsafeDestinationError
from transfers.errors import (
    Category, Confidence, Domain, EvidenceBasis, MutationOutcome, NormalizedError, Origin,
    Permanence, Retryability, Stage, TransferError, safe_diagnostic,
)
from transfers.file_selection import collection_member_paths
from transfers.models import (
    CachePresence, FileManifest, FileManifestEntry, Ownership, ProviderObservation, ProviderResource,
    ResourceState, TransferRequest,
)

INTEGRATION_ID = "premiumize"

# Premiumize's stable error codes, by its own classes: transient, semi-permanent
# (wait or capacity), permanent, unknown.
_ERRORS = {
    CREDENTIAL_MISSING: (Category.CREDENTIAL_MISSING, Retryability.AFTER_REAUTH),
    # transient
    "link_generation_failed": (Category.RESOLUTION_TEMPORARILY_FAILED, Retryability.BACKOFF),
    "transient_error": (Category.PROVIDER_UNAVAILABLE, Retryability.BACKOFF),
    # semi-permanent: a wait, or a capacity the account or service has spent.
    # ``account_limit_reached`` may be fair use, booster points or active jobs:
    # never a permanent statement about what the account may do.
    "service_down": (Category.PROVIDER_UNAVAILABLE, Retryability.BACKOFF),
    "service_limit_reached": (Category.QUOTA_EXCEEDED, Retryability.AFTER_RESOURCE_CHANGE),
    "account_limit_reached": (Category.QUOTA_EXCEEDED, Retryability.AFTER_RESOURCE_CHANGE),
    "rate_limit_reached": (Category.RATE_LIMITED, Retryability.BACKOFF),
    "semi_permanent_error": (Category.PROVIDER_UNAVAILABLE, Retryability.AFTER_RESOURCE_CHANGE),
    # permanent
    "service_unsupported": (Category.UNSUPPORTED_REQUEST, Retryability.NEVER),
    "not_found": (Category.RESOURCE_NOT_FOUND, Retryability.AFTER_RERESOLUTION),
    "authentication_failed": (Category.CREDENTIAL_INVALID, Retryability.AFTER_REAUTH),
    "permission_denied": (Category.AUTHORIZATION_FAILED, Retryability.AFTER_RESOURCE_CHANGE),
    "invalid_request": (Category.INVALID_REQUEST, Retryability.NEVER),
    "permanent_error": (Category.RESOLUTION_FAILED, Retryability.NEVER),
    # unknown / catastrophic
    "unknown_error": (Category.UNMAPPED_PROVIDER_ERROR, Retryability.UNKNOWN),
}
# A refusal that carried no code is described by its HTTP status alone.
_STATUSES = {
    401: (Category.CREDENTIAL_INVALID, Retryability.AFTER_REAUTH),
    403: (Category.AUTHORIZATION_FAILED, Retryability.AFTER_RESOURCE_CHANGE),
    404: (Category.RESOURCE_NOT_FOUND, Retryability.AFTER_RERESOLUTION),
    429: (Category.RATE_LIMITED, Retryability.BACKOFF),
}


def _error(category: Category, retry: Retryability, native: str, diagnostic: object, *, stage: Stage,
           secrets: tuple[str, ...], known: bool) -> NormalizedError:
    return NormalizedError(
        Domain.PROVIDER, category, stage, retryability=retry, origin=Origin.PROVIDER,
        permanence=Permanence.PERMANENT if retry == Retryability.NEVER else Permanence.UNKNOWN,
        integration_id=INTEGRATION_ID, native_code=safe_diagnostic(native, secrets=secrets, limit=128),
        diagnostic=safe_diagnostic(diagnostic, secrets=secrets),
        confidence=Confidence.HIGH if known else Confidence.UNKNOWN,
        evidence_basis=EvidenceBasis.NATIVE_CODE if known else EvidenceBasis.UNKNOWN,
    )


def error_from_native(exc: PremiumizeAPIError, *, stage: Stage = Stage.RESOLUTION,
                      secrets: tuple[str, ...] = ()) -> NormalizedError:
    code = exc.code.strip().casefold()
    if code in _ERRORS:
        category, retry = _ERRORS[code]
        return _error(category, retry, code, exc.message, stage=stage, secrets=secrets, known=True)
    if not code and exc.status in _STATUSES:
        category, retry = _STATUSES[exc.status]
        return _error(category, retry, str(exc.status), exc.message, stage=stage, secrets=secrets, known=True)
    if not code and exc.status >= 500:
        return _error(Category.PROVIDER_UNAVAILABLE, Retryability.BACKOFF, str(exc.status), exc.message,
                      stage=stage, secrets=secrets, known=True)
    return _error(Category.UNMAPPED_PROVIDER_ERROR, Retryability.UNKNOWN, code or str(exc.status), exc.message,
                  stage=stage, secrets=secrets, known=False)


def protocol_error(stage: Stage, diagnostic: object = "") -> NormalizedError:
    return NormalizedError(Domain.PROVIDER, Category.PROVIDER_PROTOCOL_VIOLATION, stage,
                           Retryability.NEVER, origin=Origin.PROVIDER, permanence=Permanence.PERMANENT,
                           integration_id=INTEGRATION_ID, diagnostic=safe_diagnostic(diagnostic),
                           confidence=Confidence.HIGH, evidence_basis=EvidenceBasis.NATIVE_CODE)


def translate_error(exc: Exception, *, stage: Stage = Stage.RESOLUTION,
                    secrets: tuple[str, ...] = ()) -> NormalizedError:
    if isinstance(exc, TransferError):
        return exc.error
    if isinstance(exc, PremiumizeAPIError):
        return error_from_native(exc, stage=stage, secrets=secrets)
    if isinstance(exc, PremiumizeProtocolError):
        return protocol_error(stage, safe_diagnostic(exc, secrets=secrets))
    if isinstance(exc, UnsafeDestinationError):
        return NormalizedError(Domain.SECURITY, Category.DESTINATION_BLOCKED, stage,
                               integration_id=INTEGRATION_ID, diagnostic=safe_diagnostic(exc, secrets=secrets),
                               confidence=Confidence.HIGH, evidence_basis=EvidenceBasis.TYPED_EXCEPTION)
    if isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError)):
        return NormalizedError(
            Domain.NETWORK,
            Category.CONNECTION_TIMEOUT if isinstance(exc, asyncio.TimeoutError) else Category.CONNECTION_FAILED,
            stage, retryability=Retryability.BACKOFF, origin=Origin.PROVIDER, permanence=Permanence.TEMPORARY,
            integration_id=INTEGRATION_ID, diagnostic=safe_diagnostic(exc, secrets=secrets),
            confidence=Confidence.HIGH, evidence_basis=EvidenceBasis.TYPED_EXCEPTION,
        )
    return _error(Category.UNMAPPED_PROVIDER_ERROR, Retryability.UNKNOWN, "unmapped", exc,
                  stage=stage, secrets=secrets, known=False)


# The documented refusals of ``transfer/create`` that state the request itself
# was refused -- its credential, permission, form, source service, or a limit
# the account or service is at -- so nothing was created. Premiumize's generic
# classes (``transient_error``, ``semi_permanent_error``, ``permanent_error``,
# ``unknown_error``), ``link_generation_failed`` and any code not listed here
# (a future one) prove nothing about the creation.
_CREATE_REFUSALS = frozenset({
    CREDENTIAL_MISSING, "authentication_failed", "permission_denied", "invalid_request", "service_unsupported",
    "not_found", "service_down", "service_limit_reached", "account_limit_reached", "rate_limit_reached",
})


def creation_error(exc: Exception, *, secrets: tuple[str, ...] = ()) -> NormalizedError:
    """A ``transfer/create`` that yielded no transfer id, with whether
    Premiumize may have created the transfer anyway (``MutationOutcome``).

    Only a positive fact proves nothing was created: Premiumize's own
    complete, structurally valid refusal (``status: "error"``) whose code is
    one of the documented refusals of the request itself
    (``_CREATE_REFUSALS``), a request never sent, or a connection that was
    never established. Anything else -- a generic, unknown or future code, an
    error status whose body is not that refusal (empty, truncated, HTML,
    other JSON), a success answer with no transfer id, a timeout, a
    connection lost once the request could have been processed -- cannot
    rule the creation out."""
    error = translate_error(exc, stage=Stage.RESOLUTION, secrets=secrets)
    refused = (isinstance(exc, PremiumizeAPIError) and exc.structured
               and exc.code.strip().casefold() in _CREATE_REFUSALS)
    if refused or isinstance(exc, aiohttp.ClientConnectorError):
        return error
    return replace(error, mutation=MutationOutcome.UNCERTAIN)


# -- resource identity -------------------------------------------------------------
#
# Two kinds of resource: a cloud TRANSFER Premiumize created for DebridPulse
# (its transfer id is the durable identity), and an IMMEDIATE result -- a
# frozen record of the members ``directdl`` stated for a source, which owns
# nothing on Premiumize and is never cleaned up.
CLOUD, IMMEDIATE = "cloud", "immediate"


def cloud_resource(transfer_id: str, *, ownership: Ownership = Ownership.CREATED) -> ProviderResource:
    if native_id(transfer_id) != transfer_id:
        raise TransferError(NormalizedError(Domain.PROVIDER, Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION,
                                            integration_id=INTEGRATION_ID))
    return ProviderResource(INTEGRATION_ID, {"mode": CLOUD, "transfer_id": transfer_id}, ownership,
                            uuid5(NAMESPACE_URL, f"premiumize:transfer:{transfer_id}").hex)


def immediate_resource(source: str, name: str, members) -> ProviderResource:
    """The immediate result: its source (never a link) and each member's
    exact path and size."""
    return ProviderResource(INTEGRATION_ID, {
        "mode": IMMEDIATE, "source": source, "name": name,
        "members": [[member.relative_path, member.expected_bytes, member.native_path] for member in members],
    }, Ownership.OBSERVED)


def resource_mode(resource_value: ProviderResource) -> str:
    context = resource_value.context or {}
    mode = context.get("mode")
    if resource_value.provider_id != INTEGRATION_ID or mode not in {CLOUD, IMMEDIATE} or (
            mode == CLOUD and native_id(context.get("transfer_id")) != context.get("transfer_id")):
        raise TransferError(NormalizedError(Domain.PROVIDER, Category.INVALID_ADAPTER_RESPONSE,
                                            Stage.RECONCILIATION, integration_id=INTEGRATION_ID))
    return mode


# -- members -------------------------------------------------------------------------

@dataclass(frozen=True)
class NativeMember:
    """One Premiumize file, already collection-root-relative. ``file_id`` is
    Premiumize's stable cloud file id, ``""`` for an immediate member, whose
    identity is instead its ``native_path`` -- the path exactly as ``directdl``
    stated it -- and its exact size."""
    name: str
    relative_path: str
    expected_bytes: int
    file_id: str = ""
    native_path: str = ""


def _size(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("file size must be a non-negative integer")
    return value


def _segments(path) -> list[str]:
    if not isinstance(path, str) or not path.strip():
        raise ValueError("file path must be a non-empty string")
    return path.replace("\\", "/").lstrip("/").split("/")


def immediate_members(content, *, root_name: str = "") -> tuple[NativeMember, ...]:
    """THE interpreter of ``directdl`` ``content[]``, in native order: every
    member's ``path`` and ``size`` -- its ``link`` is never read here. Empty
    content is no result. Raises ``ManifestInvalid`` for a path that would
    escape the root and ``ValueError``/``TypeError`` for a malformed member."""
    if not isinstance(content, list) or not content:
        raise ValueError("an immediate result has members")
    records = []
    for record in content:
        if not isinstance(record, dict):
            raise TypeError("member must be an object")
        records.append((_segments(record.get("path")), _size(record.get("size"))))
    paths = collection_member_paths(root_name, [parts for parts, _size in records])
    return tuple(NativeMember(path.rsplit("/", 1)[-1], path, size, native_path="/".join(parts))
                 for path, (parts, size) in zip(paths, records, strict=True))


def cloud_members(files, *, root_name: str = "") -> tuple[NativeMember, ...]:
    """THE interpreter of a cloud transfer's files: ``(segments, size, file
    id)`` from its one file or its folder tree, in traversal order. A stable
    file id is one executable member: an id stated for two files, whatever
    their paths, is refused."""
    records = []
    for parts, size, file_id in files:
        if native_id(file_id) != file_id:
            raise ValueError("file id is not a Premiumize id")
        records.append((list(parts), _size(size), file_id))
    if len({file_id for _parts, _size, file_id in records}) != len(records):
        raise ValueError("a Premiumize file id names more than one file")
    if not records:
        raise ValueError("a cloud transfer has files")
    paths = collection_member_paths(root_name, [parts for parts, _size, _id in records])
    return tuple(NativeMember(path.rsplit("/", 1)[-1], path, size, file_id)
                 for path, (_parts, size, file_id) in zip(paths, records, strict=True))


def file_manifest(members: tuple[NativeMember, ...]) -> FileManifest:
    return FileManifest(tuple(FileManifestEntry(member.name, member.relative_path, member.expected_bytes)
                              for member in members))


# -- the one transfer status translator ------------------------------------------------

_PREPARING = frozenset({"queued", "running"})
# ``seeding``: Premiumize holds every file and still seeds.
_AVAILABLE = frozenset({"finished", "seeding"})


def native_name(native: dict) -> str:
    value = native.get("name")
    return value.strip() if isinstance(value, str) and value.strip() else ""


def transfer_state(native: dict) -> tuple[ResourceState, NormalizedError | None]:
    """A transfer's neutral state. Available only when finished and naming
    its file or folder; an ``error`` is Premiumize's own failure to produce
    it, which another provider may still satisfy."""
    status = native.get("status")
    status = status.strip().casefold() if isinstance(status, str) else ""
    if status in _AVAILABLE:
        if native_id(native.get("file_id")) or native_id(native.get("folder_id")):
            return ResourceState.AVAILABLE, None
        return ResourceState.UNKNOWN, protocol_error(Stage.RECONCILIATION, "a finished transfer names no files")
    if status in _PREPARING:
        return ResourceState.PREPARING, None
    if status == "error":
        return ResourceState.UNAVAILABLE, _error(Category.RESOLUTION_FAILED, Retryability.NEVER, "error",
                                                 native.get("message"), stage=Stage.RECONCILIATION, secrets=(),
                                                 known=True)
    return ResourceState.UNKNOWN, _error(Category.UNMAPPED_PROVIDER_ERROR, Retryability.UNKNOWN, status or "none",
                                         status or "none", stage=Stage.RECONCILIATION, secrets=(), known=False)


def observation(native: dict, resource_value: ProviderResource, *, request: TransferRequest | None = None,
                manifest: FileManifest | None = None) -> ProviderObservation:
    """A neutral observation of one transfer. Premiumize states its progress
    only as a fraction with no size, so no byte progress is reported, and
    nothing else Premiumize does not state (speed, peers, bytes) is invented."""
    state, error = transfer_state(native)
    return ProviderObservation(resource_value, state, native_name(native), error=error, request=request,
                               file_manifest=manifest if state == ResourceState.AVAILABLE else None,
                               cache_presence=CachePresence.UNKNOWN)
