"""Debrid-Link-native answers terminate here.

Error codes follow Debrid-Link's published v2 error table
(``/api/v2/api_doc/errors``); a handful the API is known to answer outside that
table (JDownloader's client handles them) are mapped too. A code not listed
follows the explicit unmapped path, never a speculative retry. Provider output
is factual; recovery policy -- including whether a failure exhausts Debrid-Link
for a request -- is owned by the universal core.

Two native shapes become provider resources:

* a SEEDBOX torrent (magnet or ``.torrent``), identified by its torrent id;
* a hoster FOLDER: one submitted hoster URL that Debrid-Link answered with
  several links, identified by those link ids. Several distinct files are
  manifest members, never candidates of one artifact.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re
from uuid import NAMESPACE_URL, uuid5

import aiohttp

from providers.debridlink.client import (
    CREDENTIAL_MISSING, DebridLinkAPIError, DebridLinkProtocolError, native_file_id, native_id,
)
from services.network_safety import UnsafeDestinationError
from transfers.errors import (
    Category, Confidence, Domain, EvidenceBasis, NormalizedError, Origin,
    Permanence, Retryability, Stage, TransferError, safe_diagnostic,
)
from transfers.file_selection import ManifestInvalid, collection_member_paths
from transfers.models import (
    CachePresence, FileManifest, FileManifestEntry, Ownership, ProviderObservation,
    ProviderResource, ResourceState, TransferProgress, TransferRequest,
)

INTEGRATION_ID = "debridlink"
SEEDBOX, LINKS = "seedbox", "links"

_ERRORS = {
    CREDENTIAL_MISSING: (Category.CREDENTIAL_MISSING, Retryability.AFTER_REAUTH),
    # -- every service ---------------------------------------------------------------
    "badToken": (Category.CREDENTIAL_INVALID, Retryability.AFTER_REAUTH),
    "unknowR": (Category.PROVIDER_PROTOCOL_VIOLATION, Retryability.NEVER),
    "internalError": (Category.PROVIDER_UNAVAILABLE, Retryability.BACKOFF),
    "badArguments": (Category.INVALID_REQUEST, Retryability.NEVER),
    "floodDetected": (Category.RATE_LIMITED, Retryability.BACKOFF),
    "freeServerOverload": (Category.PROVIDER_UNAVAILABLE, Retryability.BACKOFF),
    "unverifiedEmail": (Category.ACCOUNT_LIMITED, Retryability.AFTER_RESOURCE_CHANGE),
    "badId": (Category.RESOURCE_NOT_FOUND, Retryability.AFTER_RERESOLUTION),
    "infringingFile": (Category.CONTENT_INVALID, Retryability.NEVER),
    # Answered outside the published table (JDownloader handles them): the
    # account or its address is refused, never a property of one link.
    "accountLocked": (Category.ACCOUNT_LIMITED, Retryability.NEVER),
    "serverNotAllowed": (Category.AUTHORIZATION_FAILED, Retryability.AFTER_RESOURCE_CHANGE),
    # -- downloader ------------------------------------------------------------------
    "notDebrid": (Category.RESOLUTION_TEMPORARILY_FAILED, Retryability.BACKOFF),
    "hostNotValid": (Category.UNSUPPORTED_REQUEST, Retryability.NEVER),
    "fileNotFound": (Category.SOURCE_NOT_FOUND, Retryability.NEVER),
    "fileNotAvailable": (Category.SOURCE_TEMPORARILY_UNAVAILABLE, Retryability.BACKOFF),
    "badFileUrl": (Category.INVALID_REQUEST, Retryability.NEVER),
    # The link needs a password. DebridPulse has no password-only input, so
    # it never supplies one: the source is unusable as submitted.
    "badFilePassword": (Category.SOURCE_UNAVAILABLE, Retryability.NEVER),
    # Per-hoster and per-account limits are temporary capacity, never a fact
    # that the host is unsupported.
    "notFreeHost": (Category.ACCOUNT_LIMITED, Retryability.AFTER_RESOURCE_CHANGE),
    "maintenanceHost": (Category.SOURCE_TEMPORARILY_UNAVAILABLE, Retryability.BACKOFF),
    "maxLink": (Category.QUOTA_EXCEEDED, Retryability.AFTER_RESOURCE_CHANGE),
    "maxLinkHost": (Category.QUOTA_EXCEEDED, Retryability.AFTER_RESOURCE_CHANGE),
    "maxData": (Category.QUOTA_EXCEEDED, Retryability.AFTER_RESOURCE_CHANGE),
    "maxDataHost": (Category.QUOTA_EXCEEDED, Retryability.AFTER_RESOURCE_CHANGE),
    "disabledHost": (Category.SOURCE_TEMPORARILY_UNAVAILABLE, Retryability.BACKOFF),
    "noServerHost": (Category.SOURCE_TEMPORARILY_UNAVAILABLE, Retryability.BACKOFF),
    "disabledServerHost": (Category.AUTHORIZATION_FAILED, Retryability.AFTER_RESOURCE_CHANGE),
    "maxSimultaneousFilesHost": (Category.CONCURRENCY_LIMITED, Retryability.BACKOFF),
    # -- seedbox -----------------------------------------------------------------------
    "notAddTorrent": (Category.RESOLUTION_TEMPORARILY_FAILED, Retryability.BACKOFF),
    "torrentTooBig": (Category.ACCOUNT_LIMITED, Retryability.NEVER),
    "maxTorrent": (Category.QUOTA_EXCEEDED, Retryability.AFTER_RESOURCE_CHANGE),
    "badTorrentFile": (Category.INVALID_REQUEST, Retryability.NEVER),
    "maxTransfer": (Category.CONCURRENCY_LIMITED, Retryability.BACKOFF),
}
# A refusal that carried no code is described by its HTTP status alone.
_STATUSES = {
    401: (Category.CREDENTIAL_INVALID, Retryability.AFTER_REAUTH),
    403: (Category.AUTHORIZATION_FAILED, Retryability.AFTER_RESOURCE_CHANGE),
    404: (Category.RESOURCE_NOT_FOUND, Retryability.AFTER_RERESOLUTION),
    429: (Category.RATE_LIMITED, Retryability.BACKOFF),
}
_SOURCE_CATEGORIES = frozenset({
    Category.SOURCE_NOT_FOUND, Category.SOURCE_UNAVAILABLE,
    Category.SOURCE_TEMPORARILY_UNAVAILABLE, Category.SOURCE_EXPIRED, Category.CONTENT_INVALID,
})


def _error(category: Category, retry: Retryability, native: str, diagnostic: object, *, stage: Stage,
           secrets: tuple[str, ...], known: bool, retry_after: float | None = None) -> NormalizedError:
    source = category in _SOURCE_CATEGORIES
    return NormalizedError(
        Domain.RESOLUTION if source else Domain.PROVIDER, category, stage, retryability=retry,
        origin=Origin.REMOTE_SOURCE if source else Origin.PROVIDER,
        permanence=Permanence.PERMANENT if retry == Retryability.NEVER else Permanence.UNKNOWN,
        integration_id=INTEGRATION_ID, native_code=safe_diagnostic(native, secrets=secrets, limit=128),
        diagnostic=safe_diagnostic(diagnostic, secrets=secrets),
        retry_after_seconds=retry_after if retry == Retryability.BACKOFF else None,
        confidence=Confidence.HIGH if known else Confidence.UNKNOWN,
        evidence_basis=EvidenceBasis.NATIVE_CODE if known else EvidenceBasis.UNKNOWN,
    )


def error_from_native(exc: DebridLinkAPIError, *, stage: Stage = Stage.RESOLUTION,
                      secrets: tuple[str, ...] = ()) -> NormalizedError:
    if exc.error in _ERRORS:
        category, retry = _ERRORS[exc.error]
        return _error(category, retry, exc.error, exc.error, stage=stage, secrets=secrets, known=True,
                      retry_after=exc.retry_after)
    if not exc.error and exc.status in _STATUSES:
        category, retry = _STATUSES[exc.status]
        return _error(category, retry, str(exc.status), str(exc.status), stage=stage, secrets=secrets,
                      known=True, retry_after=exc.retry_after)
    if not exc.error and exc.status >= 500:
        return _error(Category.PROVIDER_UNAVAILABLE, Retryability.BACKOFF, str(exc.status), str(exc.status),
                      stage=stage, secrets=secrets, known=True, retry_after=exc.retry_after)
    return _error(Category.UNMAPPED_PROVIDER_ERROR, Retryability.UNKNOWN, exc.error or str(exc.status),
                  exc.error or str(exc.status), stage=stage, secrets=secrets, known=False)


def protocol_error(stage: Stage, diagnostic: object = "") -> NormalizedError:
    return NormalizedError(Domain.PROVIDER, Category.PROVIDER_PROTOCOL_VIOLATION, stage,
                           Retryability.NEVER, origin=Origin.PROVIDER, permanence=Permanence.PERMANENT,
                           integration_id=INTEGRATION_ID, diagnostic=safe_diagnostic(diagnostic),
                           confidence=Confidence.HIGH, evidence_basis=EvidenceBasis.NATIVE_CODE)


def translate_error(exc: Exception, *, stage: Stage = Stage.RESOLUTION,
                    secrets: tuple[str, ...] = ()) -> NormalizedError:
    if isinstance(exc, TransferError):
        return exc.error
    if isinstance(exc, DebridLinkAPIError):
        return error_from_native(exc, stage=stage, secrets=secrets)
    if isinstance(exc, DebridLinkProtocolError):
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


def _adapter_error(stage: Stage = Stage.RESOLUTION) -> TransferError:
    return TransferError(NormalizedError(Domain.PROVIDER, Category.INVALID_ADAPTER_RESPONSE, stage,
                                         integration_id=INTEGRATION_ID))


# -- resource identity ---------------------------------------------------------------

def seedbox_resource(torrent_id: str, *, ownership: Ownership = Ownership.OBSERVED) -> ProviderResource:
    """A torrent's durable identity: its native id. Never a download link."""
    if native_id(torrent_id) is None:
        raise _adapter_error()
    return ProviderResource(INTEGRATION_ID, {"family": SEEDBOX, "id": torrent_id}, ownership,
                            uuid5(NAMESPACE_URL, f"debridlink:{SEEDBOX}:{torrent_id}").hex)


def links_resource(link_ids: tuple[str, ...], *, ownership: Ownership = Ownership.CREATED) -> ProviderResource:
    """A hoster folder's durable identity: the native ids of the links
    Debrid-Link generated for it, in its own order. Never a download link."""
    if len(link_ids) < 2 or len(set(link_ids)) != len(link_ids) or any(native_id(item) is None for item in link_ids):
        raise _adapter_error()
    joined = ",".join(link_ids)
    return ProviderResource(INTEGRATION_ID, {"family": LINKS, "ids": joined}, ownership,
                            uuid5(NAMESPACE_URL, f"debridlink:{LINKS}:{joined}").hex)


def identity(resource_value: ProviderResource) -> tuple[str, tuple[str, ...]]:
    """``(family, native ids)`` of one of this provider's resources."""
    context = resource_value.context or {}
    family = str(context.get("family") or "")
    if resource_value.provider_id == INTEGRATION_ID and family == SEEDBOX:
        torrent = native_id(context.get("id"))
        if torrent is not None:
            return SEEDBOX, (torrent,)
    elif resource_value.provider_id == INTEGRATION_ID and family == LINKS:
        ids = tuple(str(context.get("ids") or "").split(","))
        if len(ids) >= 2 and all(native_id(item) is not None for item in ids):
            return LINKS, ids
    raise _adapter_error(Stage.RECONCILIATION)


# -- members -------------------------------------------------------------------------

@dataclass(frozen=True)
class NativeMember:
    """One file of a resource, already collection-root-relative."""
    native_id: str
    name: str
    relative_path: str
    expected_bytes: int
    complete: bool


def native_name(native: dict) -> str:
    value = native.get("name")
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _size(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("file size must be a non-negative integer")
    return value


def _unique(paths) -> None:
    if len(set(paths)) != len(paths):
        raise ManifestInvalid("duplicate_path")


def seedbox_members(native: dict) -> tuple[NativeMember, ...]:
    """THE seedbox file interpreter, in native ``files[]`` order.

    The early selectable manifest and the executable manifest both derive
    member paths from here alone, so the two never drift. A file's ``name`` is
    its path within the torrent; whether a first folder is the collection
    wrapper is the neutral rule's (``collection_member_paths``), given the
    torrent's name. Raises ``ManifestInvalid`` for a path that would escape
    the root or collide, ``ValueError``/``TypeError`` for a malformed record."""
    files = native.get("files")
    if not isinstance(files, list):
        raise TypeError("files must be a list")
    records = []
    for record in files:
        if not isinstance(record, dict):
            raise TypeError("file record must be an object")
        file_id, path = native_file_id(record.get("id")), record.get("name")
        if file_id is None:
            raise ValueError("file id is malformed")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("file name must be a non-empty string")
        percent = record.get("downloadPercent")
        complete = isinstance(percent, (int, float)) and not isinstance(percent, bool) and percent >= 100
        records.append((file_id, path.replace("\\", "/").lstrip("/").split("/"), _size(record.get("size")),
                        complete))
    paths = collection_member_paths(native_name(native), [parts for _id, parts, _size, _done in records])
    _unique(paths)
    return tuple(NativeMember(file_id, path.rsplit("/", 1)[-1], path, size, complete)
                 for path, (file_id, _parts, size, complete) in zip(paths, records, strict=True))


def link_members(links: list) -> tuple[NativeMember, ...]:
    """The files of a hoster folder, one per generated link, in native order.
    Each is a flat file: a folder answer carries no directory structure."""
    records = []
    for record in links:
        if not isinstance(record, dict):
            raise TypeError("link record must be an object")
        link_id, name = native_id(record.get("id")), record.get("name")
        if link_id is None:
            raise ValueError("link id is malformed")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("link name must be a non-empty string")
        records.append((link_id, [name.strip()], _size(record.get("size"))))
    paths = collection_member_paths("", [parts for _id, parts, _size in records])
    _unique(paths)
    return tuple(NativeMember(link_id, path, path, size, True)
                 for path, (link_id, _parts, size) in zip(paths, records, strict=True))


def _manifest(members: tuple[NativeMember, ...]) -> FileManifest | None:
    if not members:
        return None
    return FileManifest(tuple(FileManifestEntry(member.name, member.relative_path, member.expected_bytes)
                              for member in members))


# -- the one status translator ----------------------------------------------------------

# Debrid-Link's documented torrent statuses: 0 paused, 1 queued, 2
# verification, 4 downloading, 8 seeding, 100 finished. Only a torrent whose
# every file Debrid-Link reports at 100 % is available; nothing that merely
# sounds finished is reported ready before its files can be requested.
_PREPARING = frozenset({0, 1, 2, 4})
_STORED = frozenset({8, 100})


def _nonnegative(value) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("native counter must be a number")
    return max(0, int(value))


def seedbox_observation(native: dict, *, resource_value: ProviderResource | None = None,
                        request: TransferRequest | None = None) -> ProviderObservation:
    """A neutral observation of one torrent.

    The manifest is published only from a complete file list: a torrent
    Debrid-Link still lists as one zip entry, or one whose metadata has not
    arrived yet, has none. Cache presence stays UNKNOWN: Debrid-Link publishes
    no read that states it, and immediate readiness is not one."""
    if not isinstance(native, dict):
        raise _adapter_error(Stage.RECONCILIATION)
    torrent = native_id(native.get("id"))
    if torrent is None:
        raise TransferError(protocol_error(Stage.RECONCILIATION, "torrent without an id"))
    resource_value = resource_value or seedbox_resource(torrent)
    try:
        total = _nonnegative(native.get("totalSize"))
        percent = min(100, _nonnegative(native.get("downloadPercent")))
        progress = TransferProgress(total, total * percent // 100, _nonnegative(native.get("downloadSpeed")))
    except ValueError as exc:
        raise _adapter_error(Stage.RECONCILIATION) from exc
    members = ()
    if native.get("isZip") is not True:
        try:
            members = seedbox_members(native)
        except (TypeError, ValueError):
            members = ()
    status = native.get("status")
    status = status if isinstance(status, int) and not isinstance(status, bool) else None
    error = None
    if status in _STORED and members and all(member.complete for member in members):
        state = ResourceState.AVAILABLE
    elif status in _PREPARING or status in _STORED:
        state = ResourceState.PREPARING
    else:
        state = ResourceState.UNKNOWN
        native_status = "unknown" if status is None else str(status)
        error = _error(Category.UNMAPPED_PROVIDER_ERROR, Retryability.UNKNOWN, native_status, native_status,
                       stage=Stage.RECONCILIATION, secrets=(), known=False)
    name = native_name(native)
    fingerprint = str(native.get("hashString") or "").casefold()
    fingerprint = fingerprint if re.fullmatch(r"[a-f0-9]{40}", fingerprint) else ""
    if request is None and fingerprint:
        request = TransferRequest("magnet", "magnet:?xt=urn:btih:" + fingerprint, name, fingerprint,
                                  INTEGRATION_ID)
    return ProviderObservation(resource_value, state, name, fingerprint, progress, error, request,
                               file_manifest=_manifest(members) if state == ResourceState.AVAILABLE else None,
                               cache_presence=CachePresence.UNKNOWN)


def links_observation(links: list, resource_value: ProviderResource,
                      request: TransferRequest | None = None) -> ProviderObservation:
    """A neutral observation of a hoster folder: available exactly when every
    one of its links is still on the account, with the folder's complete file
    list; absent once any is gone (it was retired, or Debrid-Link expired it)."""
    _family, ids = identity(resource_value)
    by_id = {str(record.get("id")): record for record in links if isinstance(record, dict)}
    if any(link_id not in by_id for link_id in ids):
        return ProviderObservation(resource_value, ResourceState.ABSENT)
    ordered = [by_id[link_id] for link_id in ids]
    try:
        members = link_members(ordered)
    except ManifestInvalid:
        raise TransferError(NormalizedError(Domain.SECURITY, Category.PATH_POLICY_VIOLATION, Stage.RECONCILIATION,
                                            integration_id=INTEGRATION_ID)) from None
    except (TypeError, ValueError) as exc:
        raise _adapter_error(Stage.RECONCILIATION) from exc
    total = sum(member.expected_bytes for member in members)
    return ProviderObservation(resource_value, ResourceState.AVAILABLE, "", "", TransferProgress(total, total, 0),
                               None, request, file_manifest=_manifest(members),
                               cache_presence=CachePresence.UNKNOWN)
