"""TorBox-native answers terminate here.

Error codes follow TorBox's documented error table; a code not listed follows
the explicit unmapped path, never a speculative retry. Provider output is
factual; recovery policy -- including whether a failure exhausts TorBox for a
request -- is owned by the universal core.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import re
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

import aiohttp

from core.presentation_safety import safe_public_host
from providers.torbox.client import (
    CREDENTIAL_MISSING, FAMILIES, TORRENT, WEBDL, TorBoxAPIError, TorBoxProtocolError,
)
from services.network_safety import UnsafeDestinationError
from transfers.errors import (
    Category, Confidence, Domain, EvidenceBasis, MutationOutcome, NormalizedError, Origin,
    Permanence, Retryability, Stage, TransferError, safe_diagnostic,
)
from transfers.file_selection import collection_member_paths
from transfers.models import (
    CachePresence, FileManifest, FileManifestEntry, Ownership, ProviderObservation,
    ProviderResource, ResourceState, TransferProgress, TransferRequest,
)

INTEGRATION_ID = "torbox"

_ERRORS = {
    CREDENTIAL_MISSING: (Category.CREDENTIAL_MISSING, Retryability.AFTER_REAUTH),
    "NO_AUTH": (Category.CREDENTIAL_MISSING, Retryability.AFTER_REAUTH),
    "BAD_TOKEN": (Category.CREDENTIAL_INVALID, Retryability.AFTER_REAUTH),
    "INVALID_DEVICE": (Category.AUTHORIZATION_FAILED, Retryability.AFTER_RESOURCE_CHANGE),
    "NOT_OWNER": (Category.AUTHORIZATION_FAILED, Retryability.NEVER),
    "VENDOR_DISABLED": (Category.ACCOUNT_LIMITED, Retryability.NEVER),
    # TorBox's own verification or storage failing ("try again later").
    "AUTH_ERROR": (Category.PROVIDER_UNAVAILABLE, Retryability.BACKOFF),
    "DATABASE_ERROR": (Category.PROVIDER_UNAVAILABLE, Retryability.BACKOFF),
    "UNKNOWN_ERROR": (Category.PROVIDER_UNAVAILABLE, Retryability.BACKOFF),
    "DOWNLOAD_SERVER_ERROR": (Category.PROVIDER_UNAVAILABLE, Retryability.BACKOFF),
    "NO_SERVERS_AVAILABLE_ERROR": (Category.PROVIDER_UNAVAILABLE, Retryability.BACKOFF),
    "TEMPORARILY_DISABLED": (Category.PROVIDER_MAINTENANCE, Retryability.BACKOFF),
    "REDIRECT_ERROR": (Category.PROVIDER_UNAVAILABLE, Retryability.BACKOFF),
    "ENDPOINT_NOT_FOUND": (Category.PROVIDER_PROTOCOL_VIOLATION, Retryability.NEVER),
    "ITEM_NOT_FOUND": (Category.RESOURCE_NOT_FOUND, Retryability.AFTER_RERESOLUTION),
    "DUPLICATE_ITEM": (Category.RESOURCE_STATE_CONFLICT, Retryability.AFTER_RESOURCE_CHANGE),
    # The account or plan cannot do this.
    "PLAN_RESTRICTED_FEATURE": (Category.ACCOUNT_LIMITED, Retryability.AFTER_RESOURCE_CHANGE),
    "DOWNLOAD_TOO_LARGE": (Category.ACCOUNT_LIMITED, Retryability.NEVER),
    "TOO_MUCH_DATA": (Category.ACCOUNT_LIMITED, Retryability.NEVER),
    "MONTHLY_LIMIT": (Category.QUOTA_EXCEEDED, Retryability.AFTER_RESOURCE_CHANGE),
    "COOLDOWN_LIMIT": (Category.QUOTA_EXCEEDED, Retryability.AFTER_RESOURCE_CHANGE),
    "ACTIVE_LIMIT": (Category.CONCURRENCY_LIMITED, Retryability.BACKOFF),
    "DIFF_ISSUE": (Category.CONCURRENCY_LIMITED, Retryability.BACKOFF),
    "RATE_LIMIT": (Category.RATE_LIMITED, Retryability.BACKOFF),
    "UNSUPPORTED_SITE": (Category.UNSUPPORTED_REQUEST, Retryability.NEVER),
    # The submitted input itself is not usable.
    "INVALID_OPTION": (Category.INVALID_REQUEST, Retryability.NEVER),
    "MISSING_REQUIRED_OPTION": (Category.INVALID_REQUEST, Retryability.NEVER),
    "TOO_MANY_OPTIONS": (Category.INVALID_REQUEST, Retryability.NEVER),
    "INVALID_LINK": (Category.INVALID_REQUEST, Retryability.NEVER),
    "BOZO_TORRENT": (Category.INVALID_REQUEST, Retryability.NEVER),
    "BOZO_NZB": (Category.INVALID_REQUEST, Retryability.NEVER),
    "BOZO_FILE": (Category.INVALID_REQUEST, Retryability.NEVER),
    # The source itself.
    "LINK_OFFLINE": (Category.SOURCE_NOT_FOUND, Retryability.NEVER),
}
# A refusal that carried no known code is described by its HTTP status alone.
_STATUSES = {
    401: (Category.CREDENTIAL_INVALID, Retryability.AFTER_REAUTH),
    403: (Category.CREDENTIAL_INVALID, Retryability.AFTER_REAUTH),
    404: (Category.RESOURCE_NOT_FOUND, Retryability.AFTER_RERESOLUTION),
    429: (Category.RATE_LIMITED, Retryability.BACKOFF),
}
_SOURCE_CATEGORIES = frozenset({
    Category.SOURCE_NOT_FOUND, Category.SOURCE_UNAVAILABLE,
    Category.SOURCE_TEMPORARILY_UNAVAILABLE, Category.SOURCE_EXPIRED, Category.CONTENT_INVALID,
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


def error_from_native(exc: TorBoxAPIError, *, stage: Stage = Stage.RESOLUTION,
                      secrets: tuple[str, ...] = ()) -> NormalizedError:
    code = exc.error.upper()
    if code in _ERRORS or exc.error in _ERRORS:
        category, retry = _ERRORS.get(code) or _ERRORS[exc.error]
        return _error(category, retry, exc.error, exc.detail, stage=stage, secrets=secrets, known=True)
    if not code and exc.status in _STATUSES:
        category, retry = _STATUSES[exc.status]
        return _error(category, retry, str(exc.status), exc.detail, stage=stage, secrets=secrets, known=True)
    if not code and exc.status >= 500:
        return _error(Category.PROVIDER_UNAVAILABLE, Retryability.BACKOFF, str(exc.status), exc.detail,
                      stage=stage, secrets=secrets, known=True)
    return _error(Category.UNMAPPED_PROVIDER_ERROR, Retryability.UNKNOWN, exc.error or str(exc.status),
                  exc.detail, stage=stage, secrets=secrets, known=False)


def protocol_error(stage: Stage, diagnostic: object = "") -> NormalizedError:
    return NormalizedError(Domain.PROVIDER, Category.PROVIDER_PROTOCOL_VIOLATION, stage,
                           Retryability.NEVER, origin=Origin.PROVIDER, permanence=Permanence.PERMANENT,
                           integration_id=INTEGRATION_ID, diagnostic=safe_diagnostic(diagnostic),
                           confidence=Confidence.HIGH, evidence_basis=EvidenceBasis.NATIVE_CODE)


def translate_error(exc: Exception, *, stage: Stage = Stage.RESOLUTION,
                    secrets: tuple[str, ...] = ()) -> NormalizedError:
    if isinstance(exc, TransferError):
        return exc.error
    if isinstance(exc, TorBoxAPIError):
        return error_from_native(exc, stage=stage, secrets=secrets)
    if isinstance(exc, TorBoxProtocolError):
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


# -- resource identity -------------------------------------------------------------

def resource(family: str, native_id: str, *, ownership: Ownership = Ownership.OBSERVED) -> ProviderResource:
    """A TorBox object's durable identity: its family AND its id, because ids
    are unique only within a family. Never a download link."""
    if family not in FAMILIES or not str(native_id or "").isdigit():
        raise TransferError(NormalizedError(Domain.PROVIDER, Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION,
                                            integration_id=INTEGRATION_ID))
    return ProviderResource(INTEGRATION_ID, {"family": family, "id": str(native_id)}, ownership,
                            uuid5(NAMESPACE_URL, f"torbox:{family}:{native_id}").hex)


def creation_error(exc: Exception, *, secrets: tuple[str, ...] = ()) -> NormalizedError:
    """A creation that yielded no usable answer, with whether TorBox may
    have created the object anyway (``MutationOutcome``).

    Only a positive fact proves nothing was created: TorBox's own refusal
    (a native code, or a client-side status), or a connection that was never
    established. Anything else -- a success answer whose body is unusable or
    names no object, a server-side failure page without a native code, a
    timeout, a connection lost once the request could have been processed --
    cannot rule the creation out."""
    error = translate_error(exc, stage=Stage.RESOLUTION, secrets=secrets)
    refused = isinstance(exc, TorBoxAPIError) and bool(exc.error or exc.status < 500)
    if refused or isinstance(exc, aiohttp.ClientConnectorError):
        return error
    return replace(error, mutation=MutationOutcome.UNCERTAIN)


def identity(resource_value: ProviderResource) -> tuple[str, str]:
    context = resource_value.context or {}
    family, native_id = str(context.get("family") or ""), str(context.get("id") or "")
    if resource_value.provider_id != INTEGRATION_ID or family not in FAMILIES or not native_id.isdigit():
        raise TransferError(NormalizedError(Domain.PROVIDER, Category.INVALID_ADAPTER_RESPONSE,
                                            Stage.RECONCILIATION, integration_id=INTEGRATION_ID))
    return family, native_id


# -- members -------------------------------------------------------------------------

@dataclass(frozen=True)
class NativeMember:
    """One file of a TorBox object, already collection-root-relative."""
    file_id: str
    name: str
    relative_path: str
    expected_bytes: int


def webdl_source_host(native: dict) -> str | None:
    """The safe hostname of the hoster a web download was submitted for, from
    TorBox's own record of it (``original_url``), or ``None`` when TorBox
    names none safely. Only the hostname leaves here: the hoster URL's path,
    query and anything secret in them never do."""
    value = native.get("original_url")
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value.strip())
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not host:
        return None
    return safe_public_host(host)


def native_name(native: dict) -> str:
    value = native.get("name")
    return value.strip() if isinstance(value, str) and value.strip() else ""


def native_members(native: dict) -> tuple[NativeMember, ...]:
    """THE TorBox file interpreter, in native ``files[]`` order.

    The early selectable manifest and the executable manifest both derive
    member paths from here alone, so the two never drift. Each file's ``name``
    is its path from the object's own folder; whether that first folder is the
    collection wrapper is the neutral rule's
    (``transfers.file_selection.collection_member_paths``), given the object's
    name. Raises ``ManifestInvalid`` for a path that would escape the root and
    ``ValueError``/``TypeError`` for a malformed record."""
    files = native.get("files")
    if not isinstance(files, list):
        raise TypeError("files must be a list")
    records = []
    for record in files:
        if not isinstance(record, dict):
            raise TypeError("file record must be an object")
        file_id, size, path = record.get("id"), record.get("size"), record.get("name")
        if isinstance(file_id, bool) or not isinstance(file_id, int) or file_id < 0:
            raise ValueError("file id must be a non-negative integer")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("file size must be a non-negative integer")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("file path must be a non-empty string")
        records.append((str(file_id), path.replace("\\", "/").lstrip("/").split("/"), size))
    paths = collection_member_paths(native_name(native), [parts for _id, parts, _size in records])
    return tuple(NativeMember(file_id, path.rsplit("/", 1)[-1], path, size)
                 for path, (file_id, _parts, size) in zip(paths, records, strict=True))


def file_manifest(native: dict) -> FileManifest | None:
    """Neutral early FileManifest, or ``None`` while TorBox has no complete,
    safely usable file list yet."""
    try:
        members = native_members(native)
    except (TypeError, ValueError):
        return None
    if not members:
        return None
    return FileManifest(tuple(FileManifestEntry(member.name, member.relative_path, member.expected_bytes)
                              for member in members))


# -- the one status translator ----------------------------------------------------------

# TorBox reports one ``download_state`` string per object -- qBittorrent's
# states for torrents and its own for web and Usenet downloads. DebridPulse
# only needs to know whether the files can be requested now, whether TorBox
# is still working, or whether it failed; this is the one place that decides.
_PREPARING_PREFIXES = ("queued", "allocating", "metadl", "checkingresumedata", "downloading", "stalled",
                       "paused", "checking", "waiting", "processing", "repair", "verifying", "direct unpack",
                       "unpack", "extract", "uploading", "moving", "completed", "cached", "forced", "pending")
# ``missing``: the NZB's articles are not all available to TorBox.
_FAILED_STATES = frozenset({"missing", "missingfiles", "error"})


def _failure(state: str) -> NormalizedError:
    """A failure of TorBox's own acquisition: the provider could not produce
    the files, which another provider of the same request may still do."""
    return _error(Category.RESOLUTION_FAILED, Retryability.NEVER, state or "failed", state or "failed",
                  stage=Stage.RECONCILIATION, secrets=(), known=True)


def _nonnegative(value) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("native counter must be a number")
    return max(0, int(value))


def observation(family: str, native: dict, *, resource_value: ProviderResource | None = None,
                request: TransferRequest | None = None) -> ProviderObservation:
    """A neutral observation of one TorBox object.

    ``download_present`` -- TorBox's statement that the files are stored and
    can be requested -- is the only thing that makes an object available; a
    state that merely sounds finished does not, so nothing is reported ready
    before its material can actually be requested. Cache presence stays
    UNKNOWN: TorBox's per-object flags are not an authoritative
    already-cached-at-submission fact."""
    if not isinstance(native, dict):
        raise TransferError(NormalizedError(Domain.PROVIDER, Category.INVALID_ADAPTER_RESPONSE,
                                            Stage.RECONCILIATION, integration_id=INTEGRATION_ID))
    native_id = native.get("id")
    if isinstance(native_id, bool) or not isinstance(native_id, (int, str)) or not str(native_id).isdigit():
        raise TransferError(protocol_error(Stage.RECONCILIATION, f"{family} object without an id"))
    resource_value = resource_value or resource(family, str(native_id))
    try:
        total = _nonnegative(native.get("size"))
        # ``progress`` is TorBox's own fraction (0..1) of its remote acquisition.
        fraction = native.get("progress")
        if native.get("download_present") is True:
            done = total
        elif isinstance(fraction, (int, float)) and not isinstance(fraction, bool):
            done = int(total * min(1.0, max(0.0, float(fraction))))
        else:
            done = 0
        progress = TransferProgress(total, done, _nonnegative(native.get("download_speed")))
    except (TypeError, ValueError) as exc:
        raise TransferError(NormalizedError(Domain.PROVIDER, Category.INVALID_ADAPTER_RESPONSE,
                                            Stage.RECONCILIATION, integration_id=INTEGRATION_ID)) from exc
    state_text = str(native.get("download_state") or "").strip().casefold()
    failure_text = native.get("error") if family == WEBDL else None
    manifest = file_manifest(native)
    error = None
    if native.get("download_present") is True and manifest is not None:
        state = ResourceState.AVAILABLE
    elif isinstance(failure_text, str) and failure_text.strip():
        state, error = ResourceState.UNAVAILABLE, _failure("error")
    elif state_text in _FAILED_STATES or state_text.startswith("failed"):
        state, error = ResourceState.UNAVAILABLE, _failure(state_text)
    elif state_text.startswith(_PREPARING_PREFIXES) or not state_text:
        state = ResourceState.PREPARING
    else:
        state = ResourceState.UNKNOWN
        error = _error(Category.UNMAPPED_PROVIDER_ERROR, Retryability.UNKNOWN, state_text, state_text,
                       stage=Stage.RECONCILIATION, secrets=(), known=False)
    name = native_name(native)
    fingerprint = ""
    if family == TORRENT:
        candidate = str(native.get("hash") or "").casefold()
        fingerprint = candidate if re.fullmatch(r"[a-f0-9]{40}", candidate) else ""
        if request is None and fingerprint:
            request = TransferRequest("magnet", "magnet:?xt=urn:btih:" + fingerprint, name, fingerprint,
                                      INTEGRATION_ID)
    return ProviderObservation(resource_value, state, name, fingerprint, progress, error, request,
                               file_manifest=manifest, cache_presence=CachePresence.UNKNOWN)
