"""Real-Debrid-native responses terminate here.

Error meanings follow the official numeric table at https://api.real-debrid.com/.
A new native code is not a new universal semantic category: an unlisted code
follows the explicit unmapped path, never a speculative retry. Provider output
is factual; recovery policy is owned by the universal core.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import re
import unicodedata
from uuid import NAMESPACE_URL, uuid5

import aiohttp

from providers.realdebrid.client import (
    CREDENTIAL_MISSING, OAUTH_GRANT_REJECTED, RealDebridAPIError, RealDebridProtocolError,
)
from services.network_safety import UnsafeDestinationError
from transfers.errors import (
    Category, Confidence, Domain, EvidenceBasis, NormalizedError, Origin,
    Permanence, Retryability, Stage, TransferError, safe_diagnostic,
)
from transfers.file_selection import collection_member_paths
from transfers.models import (
    CachePresence, FileManifest, FileManifestEntry, Ownership, ProviderObservation,
    ProviderResource, ResourceState, TransferProgress, TransferRequest,
)

INTEGRATION_ID = "realdebrid"

_ERRORS = {
    -1: (Category.PROVIDER_UNAVAILABLE, Retryability.BACKOFF),          # Internal error
    1: (Category.INVALID_REQUEST, Retryability.NEVER),                  # Missing parameter
    2: (Category.INVALID_REQUEST, Retryability.NEVER),                  # Bad parameter value
    3: (Category.PROVIDER_PROTOCOL_VIOLATION, Retryability.NEVER),      # Unknown method
    4: (Category.PROVIDER_PROTOCOL_VIOLATION, Retryability.NEVER),      # Method not allowed
    5: (Category.RATE_LIMITED, Retryability.BACKOFF),                   # Slow down
    6: (Category.SOURCE_TEMPORARILY_UNAVAILABLE, Retryability.BACKOFF), # Ressource unreachable
    7: (Category.RESOURCE_NOT_FOUND, Retryability.AFTER_RERESOLUTION),  # Resource not found
    8: (Category.CREDENTIAL_INVALID, Retryability.AFTER_REAUTH),        # Bad token
    9: (Category.AUTHORIZATION_FAILED, Retryability.AFTER_RESOURCE_CHANGE),  # Permission denied
    10: (Category.AUTHENTICATION_FAILED, Retryability.AFTER_REAUTH),    # Two-Factor authentication needed
    11: (Category.AUTHENTICATION_FAILED, Retryability.AFTER_REAUTH),    # Two-Factor authentication pending
    12: (Category.CREDENTIAL_INVALID, Retryability.AFTER_REAUTH),       # Invalid login
    13: (Category.CREDENTIAL_INVALID, Retryability.AFTER_REAUTH),       # Invalid password
    14: (Category.ACCOUNT_LIMITED, Retryability.NEVER),                 # Account locked
    15: (Category.ACCOUNT_LIMITED, Retryability.AFTER_RESOURCE_CHANGE), # Account not activated
    16: (Category.UNSUPPORTED_REQUEST, Retryability.NEVER),             # Unsupported hoster
    17: (Category.SOURCE_TEMPORARILY_UNAVAILABLE, Retryability.BACKOFF),  # Hoster in maintenance
    18: (Category.QUOTA_EXCEEDED, Retryability.AFTER_RESOURCE_CHANGE),  # Hoster limit reached
    19: (Category.SOURCE_TEMPORARILY_UNAVAILABLE, Retryability.BACKOFF),  # Hoster temporarily unavailable
    20: (Category.ACCOUNT_LIMITED, Retryability.AFTER_RESOURCE_CHANGE), # Hoster not available for free users
    21: (Category.CONCURRENCY_LIMITED, Retryability.BACKOFF),           # Too many active downloads
    22: (Category.AUTHORIZATION_FAILED, Retryability.AFTER_RESOURCE_CHANGE),  # IP Address not allowed
    23: (Category.QUOTA_EXCEEDED, Retryability.AFTER_RESOURCE_CHANGE),  # Traffic exhausted
    24: (Category.SOURCE_NOT_FOUND, Retryability.NEVER),                # File unavailable
    25: (Category.PROVIDER_UNAVAILABLE, Retryability.BACKOFF),          # Service unavailable
    26: (Category.ACCOUNT_LIMITED, Retryability.NEVER),                 # Upload too big
    27: (Category.RESOLUTION_TEMPORARILY_FAILED, Retryability.BACKOFF), # Upload error
    28: (Category.CONTENT_INVALID, Retryability.NEVER),                 # File not allowed
    29: (Category.ACCOUNT_LIMITED, Retryability.NEVER),                 # Torrent too big
    30: (Category.INVALID_REQUEST, Retryability.NEVER),                 # Torrent file invalid
    33: (Category.RESOURCE_STATE_CONFLICT, Retryability.AFTER_RESOURCE_CHANGE),  # Torrent already active
    34: (Category.RATE_LIMITED, Retryability.BACKOFF),                  # Too many requests
    # Infringing file: Real-Debrid's own refusal to serve this content, not a
    # fact about the content -- another provider may still serve it.
    35: (Category.CANDIDATE_REJECTED, Retryability.NEVER),
    36: (Category.QUOTA_EXCEEDED, Retryability.AFTER_RESOURCE_CHANGE),  # Fair Usage Limit
    37: (Category.UNSUPPORTED_CAPABILITY, Retryability.NEVER),          # Disabled endpoint
}
# Refusals that carry no numeric code: the token endpoint's refusal of the
# stored grant, and the absence of any credential.
_NAMED = {
    OAUTH_GRANT_REJECTED: (Category.CREDENTIAL_INVALID, Retryability.AFTER_REAUTH),
    CREDENTIAL_MISSING: (Category.CREDENTIAL_MISSING, Retryability.AFTER_REAUTH),
}
# The native torrent statuses that describe a failed resource.
_FAILED_STATUSES = {
    "magnet_error": (Category.SOURCE_UNAVAILABLE, Retryability.AFTER_RERESOLUTION),
    "error": (Category.RESOLUTION_FAILED, Retryability.UNKNOWN),
    "virus": (Category.CONTENT_INVALID, Retryability.NEVER),
    "dead": (Category.SOURCE_UNAVAILABLE, Retryability.AFTER_RERESOLUTION),
}
# A refusal that carried no numeric code is described by its HTTP status alone.
_STATUSES = {
    401: (Category.CREDENTIAL_INVALID, Retryability.AFTER_REAUTH),
    403: (Category.AUTHORIZATION_FAILED, Retryability.AFTER_RESOURCE_CHANGE),
    404: (Category.RESOURCE_NOT_FOUND, Retryability.AFTER_RERESOLUTION),
    429: (Category.RATE_LIMITED, Retryability.BACKOFF),
}
_SOURCE_CATEGORIES = frozenset({
    Category.SOURCE_NOT_FOUND, Category.SOURCE_UNAVAILABLE,
    Category.SOURCE_TEMPORARILY_UNAVAILABLE, Category.SOURCE_EXPIRED,
    Category.CONTENT_INVALID,
})


def _error(category: Category, retry: Retryability, native: str, diagnostic: object, *, stage: Stage,
           secrets: tuple[str, ...], known: bool) -> NormalizedError:
    source = category in _SOURCE_CATEGORIES
    return NormalizedError(
        Domain.RESOLUTION if source else Domain.PROVIDER, category, stage, retryability=retry,
        origin=Origin.REMOTE_SOURCE if source else Origin.PROVIDER,
        permanence=Permanence.PERMANENT if retry == Retryability.NEVER else Permanence.UNKNOWN,
        integration_id=INTEGRATION_ID, native_code=safe_diagnostic(native, secrets=secrets, limit=128),
        diagnostic=safe_diagnostic(diagnostic, secrets=secrets),
        confidence=Confidence.HIGH if known else Confidence.UNKNOWN,
        evidence_basis=EvidenceBasis.NATIVE_CODE if known else EvidenceBasis.UNKNOWN,
    )


def error_from_native(exc: RealDebridAPIError, *, stage: Stage = Stage.RESOLUTION,
                      secrets: tuple[str, ...] = ()) -> NormalizedError:
    if exc.error_code is not None and exc.error_code in _ERRORS:
        category, retry = _ERRORS[exc.error_code]
        return _error(category, retry, str(exc.error_code), exc.error, stage=stage, secrets=secrets, known=True)
    if exc.error_code is None and exc.error in _NAMED:
        category, retry = _NAMED[exc.error]
        return _error(category, retry, exc.error, exc.error, stage=stage, secrets=secrets, known=True)
    if exc.error_code is None and exc.status >= 500:
        return _error(Category.PROVIDER_UNAVAILABLE, Retryability.BACKOFF, str(exc.status), exc.error,
                      stage=stage, secrets=secrets, known=True)
    if exc.error_code is None and exc.status in _STATUSES:
        category, retry = _STATUSES[exc.status]
        return _error(category, retry, str(exc.status), exc.error, stage=stage, secrets=secrets, known=True)
    native = str(exc.error_code) if exc.error_code is not None else (exc.error or str(exc.status))
    return _error(Category.UNMAPPED_PROVIDER_ERROR, Retryability.UNKNOWN, native, exc.error,
                  stage=stage, secrets=secrets, known=False)


def status_error(status: str, *, stage: Stage = Stage.RECONCILIATION) -> NormalizedError:
    """The neutral failure a native failed-torrent status describes."""
    category, retry = _FAILED_STATUSES.get(status, (Category.UNMAPPED_PROVIDER_ERROR, Retryability.UNKNOWN))
    return _error(category, retry, status, status, stage=stage, secrets=(), known=status in _FAILED_STATUSES)


def protocol_error(stage: Stage, diagnostic: object = "") -> NormalizedError:
    return NormalizedError(Domain.PROVIDER, Category.PROVIDER_PROTOCOL_VIOLATION, stage,
                           Retryability.NEVER, origin=Origin.PROVIDER, permanence=Permanence.PERMANENT,
                           integration_id=INTEGRATION_ID, diagnostic=safe_diagnostic(diagnostic),
                           confidence=Confidence.HIGH, evidence_basis=EvidenceBasis.NATIVE_CODE)


def translate_error(exc: Exception, *, stage: Stage = Stage.RESOLUTION,
                    secrets: tuple[str, ...] = ()) -> NormalizedError:
    if isinstance(exc, TransferError):
        return exc.error
    if isinstance(exc, RealDebridAPIError):
        return error_from_native(exc, stage=stage, secrets=secrets)
    if isinstance(exc, RealDebridProtocolError):
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


def resource_from_native(native: dict, *, ownership: Ownership = Ownership.OBSERVED) -> ProviderResource:
    native_id = str(native.get("id") or "").strip() if isinstance(native, dict) else ""
    if not native_id:
        raise TransferError(NormalizedError(Domain.PROVIDER, Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION,
                                            integration_id=INTEGRATION_ID))
    return ProviderResource(INTEGRATION_ID, {"id": native_id}, ownership,
                            uuid5(NAMESPACE_URL, f"realdebrid:resource:{native_id}").hex)


# The canonical neutral manifest contract this boundary must satisfy:
# ``FileManifestEntry.relative_path`` / ``SourceEntry.relative_path`` describe the
# member path INSIDE the collection root and never contain the root itself --
# core applies the durable transfer root exactly once when it materializes.


@dataclass(frozen=True)
class NativeMember:
    """One Real-Debrid torrent file, already collection-root-relative."""
    name: str
    relative_path: str
    expected_bytes: int
    selected: bool


def _segments(path: object) -> list[str]:
    text = str(path or "").replace("\\", "/")
    # Real-Debrid reports every path from the torrent's own root with a leading
    # separator ("/Season 1/e01.mkv"); that separator is a convention, not a
    # directory. Every other segment is judged by the neutral member-path rule.
    return text[1:].split("/") if text.startswith("/") else text.split("/")


def native_members(files, *, root_name: str = "") -> tuple[NativeMember, ...]:
    """THE Real-Debrid native file interpreter, in native ``files[]`` order.

    One owner for both neutral surfaces: the early selectable ``FileManifest``
    and the executable ``SourceEntry`` manifest derive member paths from this
    function alone, so the two can never drift onto different coordinate
    systems and explicit file selection keeps reconciling.

    Reading ``files[]`` is Real-Debrid's; whether a first directory is the
    collection wrapper is the neutral rule's
    (``transfers.file_selection.collection_member_paths``), given the torrent's
    authoritative name ``root_name``. Order is never changed -- the executable
    manifest pairs ``links[]`` by native selected-file ordinal.

    Raises ``ManifestInvalid`` for any path that would escape the root and
    ``ValueError``/``TypeError`` for a malformed native record.
    """
    if not isinstance(files, list):
        raise TypeError("files must be a list")
    records = []
    for record in files:
        if not isinstance(record, dict):
            raise TypeError("file record must be an object")
        size = record.get("bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("file size must be a non-negative integer")
        records.append((_segments(record.get("path")), size, record.get("selected") == 1))
    paths = collection_member_paths(root_name, [parts for parts, _size, _selected in records])
    return tuple(NativeMember(path.rsplit("/", 1)[-1], path, size, selected)
                 for path, (_parts, size, selected) in zip(paths, records, strict=True))


def file_manifest_from_native(native: dict, *, root_name: str | None = None) -> FileManifest | None:
    """Neutral early FileManifest, or ``None`` while Real-Debrid has no complete
    file list (a magnet still converting) or the list is not safely usable.
    Carries no link and no native file id."""
    files = native.get("files")
    if not isinstance(files, list) or not files:
        return None
    name = native_name(native) if root_name is None else root_name
    try:
        members = native_members(files, root_name=name)
    except (TypeError, ValueError):
        return None
    return FileManifest(tuple(FileManifestEntry(member.name, member.relative_path, member.expected_bytes)
                              for member in members))


def native_name(native: dict) -> str:
    """The torrent's authoritative name, or ``""`` before Real-Debrid has one.

    ``original_filename`` is the torrent's own name; ``filename`` is the name
    Real-Debrid displays, which is the same until it renames it."""
    for field in ("original_filename", "filename"):
        value = native.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# Provider-owned native state on the provider's own resource: core treats
# ``ProviderResource.context`` as opaque and never reads this key.
_ROOT_NAME_CONTEXT = "root_name"


def with_root_name(resource: ProviderResource, name: str) -> ProviderResource:
    if not name or str(resource.context.get(_ROOT_NAME_CONTEXT) or "") == name:
        return resource
    return replace(resource, context={**dict(resource.context), _ROOT_NAME_CONTEXT: name})


# Native torrent status -> neutral resource state. Every preparing status is the
# provider still preparing; ``downloaded`` is the only available one.
_PREPARING = frozenset({"magnet_conversion", "waiting_files_selection", "queued", "downloading",
                        "compressing", "uploading"})
AWAITING_SELECTION = "waiting_files_selection"
CONVERTING = "magnet_conversion"


def _nonnegative(value) -> int:
    if value is None:
        return 0
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("native counter must be a number")
    return max(0, int(value))


def observation_from_native(native: dict, *, resource: ProviderResource | None = None,
                            request: TransferRequest | None = None) -> ProviderObservation:
    """A neutral observation of one native torrent record (info or list entry).

    Cache presence is always UNKNOWN: Real-Debrid publishes no authoritative
    cache-presence fact, and immediate availability, 100 % progress or present
    links are not one."""
    if not isinstance(native, dict):
        raise TransferError(NormalizedError(Domain.PROVIDER, Category.INVALID_ADAPTER_RESPONSE,
                                            Stage.RECONCILIATION, integration_id=INTEGRATION_ID))
    resource = resource or resource_from_native(native)
    try:
        total = _nonnegative(native.get("bytes"))
        percent = min(100, _nonnegative(native.get("progress")))
        progress = TransferProgress(total, total * percent // 100, _nonnegative(native.get("speed")))
    except ValueError as exc:
        raise TransferError(NormalizedError(Domain.PROVIDER, Category.INVALID_ADAPTER_RESPONSE,
                                            Stage.RECONCILIATION, integration_id=INTEGRATION_ID)) from exc
    status = str(native.get("status") or "")
    error = None
    if status == "downloaded":
        state = ResourceState.AVAILABLE
    elif status in _PREPARING:
        state = ResourceState.PREPARING
    elif status in _FAILED_STATUSES:
        state = ResourceState.UNAVAILABLE
        error = status_error(status)
    else:
        state = ResourceState.UNKNOWN
        error = status_error(status or "unknown")
    fingerprint = str(native.get("hash") or "").lower()
    if not re.fullmatch(r"[a-f0-9]{40}", fingerprint):
        fingerprint = ""
    name = native_name(native)
    if request is None and fingerprint:
        request = TransferRequest("magnet", "magnet:?xt=urn:btih:" + fingerprint, name, fingerprint,
                                  INTEGRATION_ID)
    return ProviderObservation(with_root_name(resource, name), state, name, fingerprint, progress, error,
                               request, file_manifest=file_manifest_from_native(native, root_name=name),
                               cache_presence=CachePresence.UNKNOWN)


def _comparable(name: object) -> str:
    return unicodedata.normalize("NFC", str(name or "")).strip().casefold()


def unrestricted_matches(member: NativeMember, native: dict) -> bool:
    """Whether an ``/unrestrict/link`` response is the expected member.

    Exact size is the authority whenever both sides state a positive one --
    it is what tells two members with the same basename apart. The filename is
    supporting evidence: a stated name that contradicts the member's refutes
    the pairing, but a matching name never overrides a size contradiction.
    Something must positively confirm the pairing; nothing unconfirmed is
    accepted."""
    returned = native.get("filesize")
    returned_size = returned if isinstance(returned, int) and not isinstance(returned, bool) else 0
    sizes_known = returned_size > 0 and member.expected_bytes > 0
    if sizes_known and returned_size != member.expected_bytes:
        return False
    returned_name = native.get("filename")
    if isinstance(returned_name, str) and returned_name.strip():
        if _comparable(returned_name) != _comparable(member.name):
            return False
        return True
    return sizes_known
