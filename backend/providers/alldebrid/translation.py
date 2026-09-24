"""AllDebrid-native responses terminate here.

Mappings are based on the existing integration and https://docs.alldebrid.com/.
Explicit expired/no-peer descriptions preserve the repository's existing
regression fixtures, whose numeric assignments differ from the documented table.
Provider output is factual; recovery policy is owned by the universal core.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import re
from uuid import NAMESPACE_URL, uuid5

import aiohttp

from providers.alldebrid.client import AllDebridAPIError
from services.network_safety import validate_provider_download_url
from transfers.errors import (
    Category, Confidence, Domain, EvidenceBasis, NormalizedError, Origin,
    Permanence, Retryability, Stage, TransferError, safe_diagnostic,
)
from transfers.models import (
    CachePresence, FileManifest, FileManifestEntry, Ownership, ProviderObservation,
    ProviderResource, ResourceState, TransferProgress, TransferRequest,
)


# A new native code is not a new universal semantic category. Unlisted codes
# follow the explicit unknown path, never a speculative transient retry.
_ERRORS = {
    "AUTH_MISSING_APIKEY": (Category.CREDENTIAL_MISSING, Retryability.AFTER_REAUTH),
    "AUTH_BAD_APIKEY": (Category.CREDENTIAL_INVALID, Retryability.AFTER_REAUTH),
    "AUTH_BLOCKED": (Category.AUTHORIZATION_FAILED, Retryability.AFTER_RESOURCE_CHANGE),
    "AUTH_USER_BANNED": (Category.ACCOUNT_LIMITED, Retryability.NEVER),
    "NO_SERVER": (Category.AUTHORIZATION_FAILED, Retryability.AFTER_RESOURCE_CHANGE),
    "ACCOUNT_INVALID": (Category.AUTHORIZATION_FAILED, Retryability.AFTER_RESOURCE_CHANGE),
    "LINK_DOWN": (Category.SOURCE_NOT_FOUND, Retryability.NEVER),
    "LINK_HOST_NOT_SUPPORTED": (Category.UNSUPPORTED_REQUEST, Retryability.NEVER),
    "LINK_NOT_SUPPORTED": (Category.UNSUPPORTED_REQUEST, Retryability.NEVER),
    "BAD_LINK": (Category.INVALID_REQUEST, Retryability.NEVER),
    "LINK_IS_MISSING": (Category.INVALID_REQUEST, Retryability.NEVER),
    "LINK_PASS_PROTECTED": (Category.CREDENTIAL_MISSING, Retryability.AFTER_REAUTH),
    "LINK_HOST_UNAVAILABLE": (Category.SOURCE_TEMPORARILY_UNAVAILABLE, Retryability.BACKOFF),
    "LINK_TEMPORARY_UNAVAILABLE": (Category.SOURCE_TEMPORARILY_UNAVAILABLE, Retryability.BACKOFF),
    "LINK_TOO_MANY_DOWNLOADS": (Category.CONCURRENCY_LIMITED, Retryability.BACKOFF),
    "LINK_HOST_FULL": (Category.RESOURCE_EXHAUSTED, Retryability.BACKOFF),
    "LINK_HOST_LIMIT_REACHED": (Category.QUOTA_EXCEEDED, Retryability.AFTER_RESOURCE_CHANGE),
    "LINK_ERROR": (Category.RESOLUTION_FAILED, Retryability.UNKNOWN),
    "DELAYED_INVALID_ID": (Category.CANDIDATE_EXPIRED, Retryability.AFTER_RERESOLUTION),
    "MAINTENANCE": (Category.PROVIDER_MAINTENANCE, Retryability.BACKOFF),
    "FREE_TRIAL_LIMIT_REACHED": (Category.QUOTA_EXCEEDED, Retryability.AFTER_RESOURCE_CHANGE),
    "MUST_BE_PREMIUM": (Category.ACCOUNT_LIMITED, Retryability.AFTER_RESOURCE_CHANGE),
    "MAGNET_MUST_BE_PREMIUM": (Category.ACCOUNT_LIMITED, Retryability.AFTER_RESOURCE_CHANGE),
    "MAGNET_INVALID_ID": (Category.RESOURCE_NOT_FOUND, Retryability.AFTER_RERESOLUTION),
    "MAGNET_INVALID_URI": (Category.INVALID_REQUEST, Retryability.NEVER),
    "MAGNET_INVALID_FILE": (Category.INVALID_REQUEST, Retryability.NEVER),
    "MAGNET_NO_URI": (Category.INVALID_REQUEST, Retryability.NEVER),
    "MAGNET_TOO_MANY_ACTIVE": (Category.CONCURRENCY_LIMITED, Retryability.BACKOFF),
    "MAGNET_TOO_MANY": (Category.QUOTA_EXCEEDED, Retryability.AFTER_RESOURCE_CHANGE),
    "MAGNET_TOO_LARGE": (Category.ACCOUNT_LIMITED, Retryability.NEVER),
    "MAGNET_MAGNET_TOO_BIG": (Category.ACCOUNT_LIMITED, Retryability.NEVER),
    "MAGNET_UPLOAD_FAILED": (Category.RESOLUTION_TEMPORARILY_FAILED, Retryability.AFTER_RERESOLUTION),
    "MAGNET_FILE_UPLOAD_FAILED": (Category.RESOLUTION_TEMPORARILY_FAILED, Retryability.BACKOFF),
    "MAGNET_CANT_BOOTSTRAP": (Category.SOURCE_TEMPORARILY_UNAVAILABLE, Retryability.AFTER_RERESOLUTION),
    "MAGNET_TOOK_TOO_LONG": (Category.SOURCE_TEMPORARILY_UNAVAILABLE, Retryability.AFTER_RERESOLUTION),
    "MAGNET_LINKS_REMOVED": (Category.RESOURCE_EXPIRED, Retryability.AFTER_RERESOLUTION),
    "MAGNET_PROCESSING_FAILED": (Category.CONTENT_INVALID, Retryability.NEVER),
}
_SOURCE_CATEGORIES = frozenset({
    Category.SOURCE_NOT_FOUND, Category.SOURCE_UNAVAILABLE,
    Category.SOURCE_TEMPORARILY_UNAVAILABLE, Category.SOURCE_EXPIRED,
    Category.CONTENT_INVALID,
})


def error_from_code(code: str, diagnostic: object = "", *, stage: Stage = Stage.RESOLUTION,
                    secrets: tuple[str, ...] = ()) -> NormalizedError:
    category, retry = _ERRORS.get(code, (Category.UNMAPPED_PROVIDER_ERROR, Retryability.UNKNOWN))
    return NormalizedError(
        Domain.RESOLUTION if category in _SOURCE_CATEGORIES else Domain.PROVIDER,
        category, stage, retryability=retry,
        origin=Origin.REMOTE_SOURCE if category in _SOURCE_CATEGORIES else Origin.PROVIDER,
        permanence=Permanence.PERMANENT if retry == Retryability.NEVER else Permanence.UNKNOWN,
        integration_id="alldebrid", native_code=safe_diagnostic(code, secrets=secrets, limit=128),
        diagnostic=safe_diagnostic(diagnostic, secrets=secrets),
        confidence=Confidence.HIGH if code in _ERRORS else Confidence.UNKNOWN,
        evidence_basis=EvidenceBasis.NATIVE_CODE if code in _ERRORS else EvidenceBasis.UNKNOWN,
    )


def translate_error(exc: Exception, *, stage: Stage = Stage.RESOLUTION,
                    secrets: tuple[str, ...] = ()) -> NormalizedError:
    if isinstance(exc, TransferError):
        return exc.error
    if isinstance(exc, AllDebridAPIError):
        return error_from_code(exc.code, exc.message, stage=stage, secrets=secrets)
    if isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError)):
        return NormalizedError(
            Domain.NETWORK,
            Category.CONNECTION_TIMEOUT if isinstance(exc, asyncio.TimeoutError) else Category.CONNECTION_FAILED,
            stage, retryability=Retryability.BACKOFF,
            origin=Origin.PROVIDER, permanence=Permanence.TEMPORARY,
            integration_id="alldebrid", diagnostic=safe_diagnostic(exc, secrets=secrets),
            confidence=Confidence.HIGH, evidence_basis=EvidenceBasis.TYPED_EXCEPTION,
        )
    # Legacy client failures are interpreted only in this provider-local adapter.
    # Exact structural patterns preserve transport diagnostics until the native
    # client raises typed exceptions for every path.
    text = str(exc)
    code = re.search(r"AllDebrid \[([A-Z0-9_]+)\]", text)
    if code:
        return replace(error_from_code(code.group(1), text, stage=stage, secrets=secrets),
                       confidence=Confidence.MEDIUM, evidence_basis=EvidenceBasis.DIAGNOSTIC)
    http = re.search(r"AllDebrid HTTP (\d{3})", text)
    if http and int(http.group(1)) >= 500:
        return replace(error_from_code("MAINTENANCE", text, stage=stage, secrets=secrets),
                       category=Category.PROVIDER_UNAVAILABLE, native_code=http.group(1),
                       confidence=Confidence.MEDIUM, evidence_basis=EvidenceBasis.DIAGNOSTIC)
    if text.startswith("Network error"):
        return NormalizedError(Domain.NETWORK, Category.CONNECTION_FAILED, stage,
                               Retryability.BACKOFF,
                               origin=Origin.PROVIDER, permanence=Permanence.TEMPORARY,
                               integration_id="alldebrid", diagnostic=safe_diagnostic(text, secrets=secrets),
                               confidence=Confidence.MEDIUM, evidence_basis=EvidenceBasis.DIAGNOSTIC)
    if any(marker in text for marker in ("non-public", "local download", "local unlocked", "credential-bearing", "non-HTTP(S)")):
        return NormalizedError(Domain.SECURITY, Category.DESTINATION_BLOCKED, stage,
                               integration_id="alldebrid", diagnostic=safe_diagnostic(text, secrets=secrets),
                               confidence=Confidence.MEDIUM, evidence_basis=EvidenceBasis.DIAGNOSTIC)
    if any(marker in text for marker in ("invalid JSON", "empty response", "unexpected payload", "without an ID", "unexpected magnet response", "unexpected file response")):
        return NormalizedError(Domain.PROVIDER, Category.PROVIDER_PROTOCOL_VIOLATION, stage,
                               origin=Origin.PROVIDER, integration_id="alldebrid",
                               diagnostic=safe_diagnostic(text, secrets=secrets),
                               confidence=Confidence.MEDIUM, evidence_basis=EvidenceBasis.DIAGNOSTIC)
    return error_from_code("UNMAPPED", text, stage=stage, secrets=secrets)


def resource_from_native(native: dict, *, ownership: Ownership = Ownership.OBSERVED) -> ProviderResource:
    native_id = str(native.get("id") or "").strip()
    if not native_id:
        raise TransferError(NormalizedError(Domain.PROVIDER, Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION,
                                            integration_id="alldebrid"))
    return ProviderResource("alldebrid", {"id": native_id}, ownership,
                            uuid5(NAMESPACE_URL, f"alldebrid:resource:{native_id}").hex)


# The canonical neutral manifest contract this boundary must satisfy:
# ``FileManifestEntry.relative_path`` / ``SourceEntry.relative_path`` describe the
# member path INSIDE the collection root and never contain the root itself --
# ``transfers._engine_base.TransferEngine._materialize`` applies the durable
# transfer root exactly once when it allocates a child FILE target.


@dataclass(frozen=True)
class NativeMember:
    """One leaf of an AllDebrid native file tree, already collection-root-relative."""
    name: str
    relative_path: str
    expected_bytes: int
    link: str = ""


def _unwrap_collection_root(nodes: list[dict], root_name: str) -> list[dict]:
    """Terminate AllDebrid's own collection wrapper at this boundary.

    A BitTorrent multi-file torrent declares ``info.name`` as the single
    directory every member path is stored under. AllDebrid reports that same
    string as the magnet's authoritative ``filename`` and reports that same
    directory as the top-level node of its file tree, so the wrapper is
    identified by two of the provider's OWN authoritative facts and nothing
    else: the tree has exactly one top-level node, that node is a directory
    (it carries a child list ``e``), and its native name is exactly the
    provider's authoritative name for this resource.

    Exactly one level is ever removed, and every case these native facts
    cannot decide leaves the tree untouched: no authoritative name (the
    provider's ``noname`` placeholder is not one), more than one top-level
    node, a top-level leaf (a single-file torrent, whose ``info.name`` IS the
    file), or any inexact match. Nothing here infers a wrapper from the core
    transfer name, from a prefix shared by every member, or from a first
    directory merely because there is only one -- a real member directory
    such as ``Disc 1`` is removed only when ``Disc 1`` genuinely is this
    resource's own name, in which case it genuinely is the collection root.
    """
    if not root_name or len(nodes) != 1:
        return nodes
    only = nodes[0]
    children = only.get("e")
    if not isinstance(children, list):
        return nodes
    if str(only.get("n") or only.get("name") or "").strip() != root_name:
        return nodes
    return [node for node in children if isinstance(node, dict)]


def _flatten(nodes: list[dict], prefix: str, require_link: bool) -> list[NativeMember]:
    members: list[NativeMember] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        name = str(node.get("n") or node.get("name") or "").strip()
        children = node.get("e")
        current = f"{prefix}/{name}".strip("/") if name else prefix
        if isinstance(children, list):
            members.extend(_flatten(children, current, require_link))
            continue
        if not name:
            continue
        link = ""
        if require_link:
            if "l" not in node:
                continue
            # The provider-issued download capability is validated here, at the
            # one native boundary, before it can reach any consumer. A node that
            # DOES claim a link but carries an unusable one is a malformed native
            # payload and fails loudly, exactly as it did before this boundary had
            # a single owner -- it is never silently dropped from the manifest.
            link = validate_provider_download_url(node["l"], context="magnet file download link")
        try:
            size = max(0, int(node.get("s") or node.get("size") or 0))
        except (TypeError, ValueError, OverflowError):
            size = 0
        members.append(NativeMember(name, current or name, size, link))
    return members


def native_members(nodes, *, root_name: str = "", require_link: bool = False) -> tuple[NativeMember, ...]:
    """THE AllDebrid native file-tree interpreter.

    One owner for both neutral surfaces: the early selectable ``FileManifest``
    and the executable ``SourceEntry`` manifest derive their member paths from
    this function alone, so the two can never drift onto different coordinate
    systems and explicit file selection keeps reconciling
    (``transfers.file_selection.reconcile_executable_subset``).

    ``require_link`` selects the capability-bearing surface: a leaf that claims
    no native download link at all is skipped, and one that claims an unusable
    link fails. With it off the native ``l`` value is never even read, so no
    capability URL can leak into the neutral early manifest. The member PATH is
    computed identically either way.

    A node carrying a child list ``e`` is a directory on BOTH surfaces. The two
    superseded flatteners disagreed here -- the executable one tested ``l``
    first and would have called such a node a file -- and that disagreement is
    resolved in favour of the early surface's rule, because early and executable
    member paths must be identical for explicit selection to reconcile.
    """
    prepared = [node for node in (nodes or ()) if isinstance(node, dict)]
    return tuple(_flatten(_unwrap_collection_root(prepared, root_name), "", require_link))


def file_manifest_from_native(native: dict, *, root_name: str | None = None) -> FileManifest | None:
    """Neutral early FileManifest from a status record's file tree, or ``None``.

    Absent/empty tree yields ``None``: the provider reports no selectable
    manifest until it has a complete authoritative tree. ``root_name`` defaults
    to this same record's own authoritative name.
    """
    name = _native_name(native) if root_name is None else root_name
    entries = [FileManifestEntry(member.name, member.relative_path, member.expected_bytes)
               for member in native_members(native.get("files"), root_name=name)]
    return FileManifest(tuple(entries)) if entries else None


def file_manifest_from_files_response(records, native_id: str, *, root_name: str = "") -> FileManifest | None:
    """Neutral FileManifest from a /magnet/files response (links discarded).

    That endpoint carries no name fact of its own, so the caller supplies the
    authoritative root name it already observed for the same resource.
    """
    for record in records or ():
        if isinstance(record, dict) and str(record.get("id")) == str(native_id):
            entries = [FileManifestEntry(member.name, member.relative_path, member.expected_bytes)
                       for member in native_members(record.get("files"), root_name=root_name)]
            if entries:
                return FileManifest(tuple(entries))
    return None


def cache_presence_from_upload(native: dict) -> CachePresence:
    """Neutral cache presence from a magnet/torrent upload response.

    ``POST /v4/magnet/upload`` and ``/v4/magnet/upload/file`` document the
    response ``ready`` boolean as whether the magnet/torrent is *already
    available*: that is the provider's own cache-presence statement, so exactly
    ``True`` is a HIT and exactly ``False`` a MISS. Anything else (absent,
    non-boolean) is UNKNOWN. This is the only place native ``ready`` becomes a
    cache fact, and it is applied to upload responses alone: a later status
    (``statusCode``, ``ResourceState.AVAILABLE``, speed, files/links) never
    turns a MISS/UNKNOWN into a HIT. Resource readiness is translated
    independently by ``observation_from_native``.
    """
    ready = native.get("ready")
    if ready is True:
        return CachePresence.HIT
    if ready is False:
        return CachePresence.MISS
    return CachePresence.UNKNOWN


# AllDebrid's own vocabulary for "this torrent's metadata is not resolved yet":
# the native filename it reports until the real name is known. It is a
# provider-native placeholder, not a name, so it is neutralized here at the
# translation boundary and core never learns the string.
_UNRESOLVED_NATIVE_NAME = "noname"


def _native_name(native: dict) -> str:
    """The authoritative native name, or ``""`` when the provider has none yet."""
    name = str(native.get("filename") or native.get("name") or "").strip()
    return "" if name.casefold() == _UNRESOLVED_NATIVE_NAME else name


# Provider-owned native state on the provider's own resource. Core treats
# ``ProviderResource.context`` as opaque and never reads this key; it exists so
# that ``/v4/magnet/files`` -- which carries no name fact of its own -- can be
# interpreted with the authoritative torrent name the status record did carry.
_ROOT_NAME_CONTEXT = "root_name"


def with_root_name(resource: ProviderResource, name: str) -> ProviderResource:
    """Enrich a provider-owned resource with AllDebrid's authoritative root name.

    Canonical resource identity is untouched: ``resource.id`` is
    ``uuid5("alldebrid:resource:<native id>")`` and does not depend on
    ``context``, so a later observation enriches the SAME resource rather than
    creating another one. An absent name -- including the neutralized ``noname``
    placeholder -- is the absence of a fact and never overwrites a known one.
    """
    if not name or str(resource.context.get(_ROOT_NAME_CONTEXT) or "") == name:
        return resource
    return replace(resource, context={**dict(resource.context), _ROOT_NAME_CONTEXT: name})


def collection_root_name(resource: ProviderResource) -> str:
    """The authoritative root name carried on a provider-owned resource, if any."""
    return str(resource.context.get(_ROOT_NAME_CONTEXT) or "").strip()


def observation_from_native(native: dict, *, resource: ProviderResource | None = None,
                            request: TransferRequest | None = None) -> ProviderObservation:
    resource = resource or resource_from_native(native)
    try:
        code = int(native.get("statusCode", native.get("status_code", 0)) or 0)
        progress = TransferProgress(max(0, int(native.get("size") or 0)),
                                    max(0, int(native.get("downloaded") or 0)),
                                    max(0, int(native.get("downloadSpeed") or 0)))
    except (ValueError, TypeError, OverflowError) as exc:
        raise TransferError(NormalizedError(Domain.PROVIDER, Category.INVALID_ADAPTER_RESPONSE,
                                            Stage.RECONCILIATION, integration_id="alldebrid")) from exc
    description = str(native.get("status") or "").casefold()
    # An upload response can expose an explicit readiness fact before any status
    # poll. Precedence: explicit statusCode/status_code -> existing translation;
    # else native ready boolean; else conservative existing behavior.
    has_status_code = "statusCode" in native or "status_code" in native
    ready = native.get("ready")
    error = None
    if "expired" in description or "files removed from cache" in description:
        state = ResourceState.EXPIRED
        error = error_from_code("MAGNET_LINKS_REMOVED", native.get("status"), stage=Stage.RECONCILIATION)
    elif not has_status_code and ready is True:
        state = ResourceState.AVAILABLE
    elif not has_status_code and ready is False:
        state = ResourceState.PREPARING
    elif code == 4:
        state = ResourceState.AVAILABLE
    elif code in (0, 1, 2, 3):
        state = ResourceState.PREPARING
    else:
        state = ResourceState.UNAVAILABLE
        if "no peer" in description:
            native_code = "MAGNET_CANT_BOOTSTRAP"
        else:
            native_code = {
                5: "MAGNET_UPLOAD_FAILED", 6: "MAGNET_PROCESSING_FAILED",
                7: "MAGNET_CANT_BOOTSTRAP", 8: "MAGNET_TOO_LARGE",
                9: "MAGNET_INTERNAL_ERROR", 10: "MAGNET_TOOK_TOO_LONG",
                11: "MAGNET_LINKS_REMOVED", 12: "MAGNET_PROCESSING_FAILED",
                13: "MAGNET_PROCESSING_FAILED", 14: "MAGNET_CANT_BOOTSTRAP",
                15: "MAGNET_CANT_BOOTSTRAP",
            }.get(code, f"STATUS_{code}")
        error = error_from_code(native_code, native.get("status"), stage=Stage.RECONCILIATION)
        if error.category == Category.UNMAPPED_PROVIDER_ERROR:
            state = ResourceState.UNKNOWN
    fingerprint = str(native.get("hash") or "").lower()
    name = _native_name(native)
    if request is None and re.fullmatch(r"[a-f0-9]{40}", fingerprint):
        request = TransferRequest("magnet", "magnet:?xt=urn:btih:" + fingerprint,
                                  name, fingerprint, "alldebrid")
    return ProviderObservation(with_root_name(resource, name), state, name,
                               fingerprint, progress, error, request,
                               file_manifest=file_manifest_from_native(native, root_name=name))