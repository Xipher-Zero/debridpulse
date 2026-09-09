"""AllDebrid-native responses terminate here.

Mappings are based on the existing integration and https://docs.alldebrid.com/.
Explicit expired/no-peer descriptions preserve the repository's existing
regression fixtures, whose numeric assignments differ from the documented table.
Provider output is factual; recovery policy is owned by the universal core.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import re
from uuid import NAMESPACE_URL, uuid5

import aiohttp

from providers.alldebrid.client import AllDebridAPIError
from transfers.errors import (
    Category, Confidence, Domain, EvidenceBasis, NormalizedError, Origin,
    Permanence, Retryability, Stage, TransferError, safe_diagnostic,
)
from transfers.models import (
    FileManifest, FileManifestEntry, Ownership, ProviderObservation, ProviderResource,
    ResourceState, TransferProgress, TransferRequest,
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


def _manifest_entries(nodes, prefix: str = "") -> list[FileManifestEntry]:
    """Flatten AllDebrid's nested file representation into neutral entries.

    Reads only ``n`` (name), ``s`` (size) and ``e`` (child list). Any download
    link (``l``) or capability URL on a native node is discarded here.
    """
    entries: list[FileManifestEntry] = []
    for node in nodes or ():
        if not isinstance(node, dict):
            continue
        name = str(node.get("n") or node.get("name") or "").strip()
        children = node.get("e")
        current = f"{prefix}/{name}".strip("/") if name else prefix
        if isinstance(children, list):
            entries.extend(_manifest_entries(children, current))
            continue
        if not name:
            continue
        try:
            size = max(0, int(node.get("s") or node.get("size") or 0))
        except (TypeError, ValueError, OverflowError):
            size = 0
        entries.append(FileManifestEntry(name, current or name, size))
    return entries


def file_manifest_from_native(native: dict) -> FileManifest | None:
    """Neutral early FileManifest from a status record's file tree, or ``None``.

    Absent/empty tree yields ``None``: the provider reports no selectable
    manifest until it has a complete authoritative tree.
    """
    entries = _manifest_entries(native.get("files"))
    return FileManifest(tuple(entries)) if entries else None


def file_manifest_from_files_response(records, native_id: str) -> FileManifest | None:
    """Neutral FileManifest from a /magnet/files response (links discarded)."""
    for record in records or ():
        if isinstance(record, dict) and str(record.get("id")) == str(native_id):
            entries = _manifest_entries(record.get("files"))
            if entries:
                return FileManifest(tuple(entries))
    return None


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
    if request is None and re.fullmatch(r"[a-f0-9]{40}", fingerprint):
        request = TransferRequest("magnet", "magnet:?xt=urn:btih:" + fingerprint,
                                  str(native.get("filename") or native.get("name") or ""),
                                  fingerprint, "alldebrid")
    return ProviderObservation(resource, state,
                               str(native.get("filename") or native.get("name") or ""),
                               fingerprint, progress, error, request,
                               file_manifest=file_manifest_from_native(native))