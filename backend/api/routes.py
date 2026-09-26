"""
REST API routes for DebridPulse.

Conventions:
- All DB access uses get_db() against the authoritative SQLite store.
- Pydantic models for request bodies are defined inline.
- No inline `import` statements — all imports are at module level.
"""
import asyncio
import ipaddress
import json as _json
import logging
import os
import time
from pathlib import Path
from typing import AsyncGenerator, Literal
from urllib.parse import urlparse

from fastapi import Depends, APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse, Response
from pydantic import BaseModel, Field

from core.branding import APP_SHORT_NAME, REPOSITORY_API_URL
from core.config import (
    AppSettings,
    apply_settings,
    config_write_lock,
    get_settings,
    load_settings,
    save_settings,
)
from api.legacy_settings_view import legacy_settings_projection
from providers.alldebrid.definition import canonical_options as alldebrid_canonical_options
from core.config_validator import validate_and_sanitise
from integrations.definition import IntegrationSettings
from transfers.runtime_limits import ExecutionRuntimeLimits
from transfers.settings import TransferSettings
from core.logging_utils import sanitize_exception, sanitize_log_value
from core.presentation_safety import safe_original_http_resource
from core.version import is_version_newer, normalize_version_tag, read_version
from auth.models import AuthMechanism
from auth.oidc_version import oidc_configuration_version
from auth.passwords import basic_verification_cache, password_credential_version
from auth.sessions import session_store
from core import scheduler as scheduler_runtime
from db.database import DB_PATH, database_maintenance, get_db


def _sanitize_error(exc: Exception) -> str:
    """Return a safe, short error message suitable for API responses.

    Strips raw magnet links and very long URLs that may appear in exception
    strings — e.g. when AllDebrid echoes back the submitted magnet in an
    error payload, or when a download_torrent_file exception includes the URL.
    Truncates the result to 200 characters.
    """
    return sanitize_exception(exc, max_length=200)


# ── SQL dialect helpers ────────────────────────────────────────────────────────
def _sql_now_minus(interval: str) -> str:
    parts = interval.split()
    n, unit = parts[0], parts[1]
    return f"datetime('now','-{n} {unit}')"


def _sql_strftime(fmt: str, field: str) -> str:
    # SQLite stores canonical UTC clock values; calendar buckets are operator-local.
    return f"strftime('{fmt}', {field}, 'localtime')"


def _sql_date(field: str) -> str:
    return f"DATE({field}, 'localtime')"

from application import dispatch_admission
from application.dependencies import get_application
from transfers import codec
from application.service import ApplicationService
from executors.aria2.runtime import runtime as aria2_runtime, _canonical_aria2_options
from services.event_bus import bind_publisher
from api.serializers import (
    public_download_file,
    public_payload,
    public_torrent,
)
from executors.aria2.presentation import public_aria2_download

logger = logging.getLogger("debridpulse.routes")
router = APIRouter()


def _duplicate_candidate_from_payload(payload: dict, source: str = "preview"):
    """Build a read-only duplicate-check candidate from API/search payload data."""
    from services.duplicates import DuplicateCandidate

    return DuplicateCandidate(
        source=source,
        title=str(payload.get("title") or payload.get("name") or "").strip(),
        magnet=str(payload.get("magnet") or "").strip(),
        torrent_url=str(payload.get("torrent_url") or "").strip(),
        infohash=str(payload.get("hash") or payload.get("infohash") or "").strip().lower(),
        resource_id=str(payload.get("resource_id") or "").strip(),
        size_bytes=int(payload.get("size_bytes") or payload.get("size") or 0),
        indexer=str(payload.get("indexer") or "").strip(),
        category=str(payload.get("category") or "").strip(),
        imdb_id=str(payload.get("imdb_id") or payload.get("imdbid") or "").strip(),
        tmdb_id=str(payload.get("tmdb_id") or payload.get("tmdbid") or "").strip(),
    )


def _public_base_url(request: Request) -> str:
    """Return the externally reachable base URL for generated links."""
    configured = (os.getenv("PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if configured:
        return configured
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost:8080"
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    return f"{scheme}://{host}".rstrip("/")


def _avatar_reachability_warning(public_url: str) -> str:
    """Return a warning when Discord likely cannot fetch the generated avatar URL."""
    if _is_public_url(public_url):
        return ""
    return (
        "Avatar uploaded, but the generated URL is private or loopback and may not be reachable by Discord. "
        "Set PUBLIC_BASE_URL to a public HTTP(S) address or use a public avatar URL directly."
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_public_url(url: str) -> bool:
    """Returns True when url is reachable from outside the container."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if not host or host in ("localhost", "127.0.0.1", "::1"):
            return False
        addr = ipaddress.ip_address(host)
        return not (addr.is_loopback or addr.is_private or addr.is_link_local)
    except ValueError:
        # hostname — not an IP, assume public
        return True


# ── Settings ───────────────────────────────────────────────────────────────────
# Secrets of the broad settings document only. Integration-owned secrets
# (AllDebrid ``api_key``, aria2 ``secret``) live in their ``integrations.<id>``
# namespace and are written, cleared and redacted through it.
_SECRET_SETTINGS = {
    "discord_webhook_url",
    "discord_webhook_added", "stats_report_webhook_url",
    "auth_password", "extraction_password",
}

# SettingsUpdate inherits AppSettings, so omitted values are otherwise populated
# with model defaults before the route sees them. Authentication transition
# enforcement reasons from the raw request and deliberately treats omitted auth
# fields as unchanged. Preserve those fields here too so a partial legacy PUT
# cannot silently reset authentication behind the transition state machine.
_AUTH_COMPAT_SETTINGS_FIELDS = (
    "auth_password_enabled",
    "auth_username",
    "auth_session_lifetime_hours",
    "auth_oidc_enabled",
    "oidc_provider_name",
    "oidc_issuer_url",
    "oidc_client_id",
    "oidc_scopes",
    "oidc_allow_all",
    "oidc_allowed_subjects",
    "oidc_allowed_emails",
    "oidc_allowed_groups",
    "oidc_group_claim",
    "public_base_url",
)


def _public_settings(settings: AppSettings, definitions=()) -> dict:
    data = settings.model_dump()
    from integrations.configuration import public_integration_groups, public_integrations
    data["integrations"] = public_integrations(settings, definitions)
    data["integration_groups"] = public_integration_groups(settings, definitions)
    # Compatibility output for pre-canonical readers, derived from the
    # canonical namespaces above at response time; never persisted or consumed.
    compatibility = legacy_settings_projection(settings, definitions)
    data.update(compatibility)
    # Self-describing: a client that re-submits this document strips exactly
    # these names, so no client has to keep its own list of them.
    data["compatibility_fields"] = sorted(compatibility)
    for field in _SECRET_SETTINGS:
        if field in data:
            data[f"{field}_configured"] = bool(str(data.get(field) or "").strip())
            data[field] = ""
    # Verification evidence is internal: a fingerprint is never served. What is
    # published is the DERIVED notification state -- whether each section has an
    # effective destination, and whether a Test has covered exactly what is
    # saved -- computed by the one notification-boundary owner.
    data.pop("notification_verification", None)
    from services.notification_service import notification_state
    data.update(notification_state(settings))
    data["database_backend"] = "sqlite"
    data["timezone"] = (os.getenv("TZ", "UTC") or "UTC").strip() or "UTC"
    return data


def _provider_display_name(identity: str | None, definitions) -> str | None:
    if not identity:
        return None
    return next((definition.name for definition in definitions if definition.id == identity), None)


def _safe_original_resource(request_payload) -> str | None:
    """Return a normal-user source label without returning capability-bearing data."""
    if not request_payload:
        return None
    try:
        request = codec.request(request_payload)
    except (TypeError, ValueError, KeyError):
        return None

    kind = str(request.kind or "").strip().lower()
    raw = request.payload.decode("utf-8", "replace") if isinstance(request.payload, bytes) else str(request.payload or "")

    if kind in {"http", "https"}:
        return safe_original_http_resource(raw, max_length=180) or request.name or "HTTP/HTTPS resource"

    if kind == "magnet" or raw.lower().startswith("magnet:?"):
        return sanitize_log_value(raw, max_length=180)

    if isinstance(request.payload, bytes) or kind in {"torrent", "torrent_file", "file"}:
        return request.name or "Torrent file"

    return request.name or (f"{kind.upper()} resource" if kind else "Source resource")


def _public_transfer_presentation(value, definitions) -> dict:
    """Decorate durable provenance with neutral display metadata before stripping capabilities."""
    raw_request = value.get("request") if isinstance(value, dict) else None
    result = public_payload(value)
    if "current_provider_id" in result:
        result["current_provider_name"] = _provider_display_name(result.get("current_provider_id"), definitions)
    if "delivering_provider_id" in result:
        result["delivering_provider_name"] = _provider_display_name(result.get("delivering_provider_id"), definitions)

    for attempt in result.get("route_attempts", []) or []:
        attempt["provider_name"] = _provider_display_name(attempt.get("provider_id"), definitions)
    for attempt in result.get("execution_attempts", []) or []:
        attempt["provider_name"] = _provider_display_name(attempt.get("provider_id"), definitions)

    if raw_request is not None:
        result["original_resource"] = _safe_original_resource(raw_request)
    return result


def _password_auth_binding(settings: AppSettings) -> tuple[bool, str, str]:
    return (
        bool(getattr(settings, "auth_password_enabled", False)),
        str(getattr(settings, "auth_username", "") or "").strip(),
        password_credential_version(getattr(settings, "auth_password_hash", "")),
    )


def _oidc_auth_binding(settings: AppSettings) -> tuple[bool, str]:
    return (
        bool(getattr(settings, "auth_oidc_enabled", False)),
        oidc_configuration_version(settings),
    )


def _revoke_stale_authentication_state(previous: AppSettings, current: AppSettings) -> None:
    """Give the legacy broad Settings route the same revocation semantics as the dedicated auth route."""
    if _password_auth_binding(previous) != _password_auth_binding(current):
        basic_verification_cache.clear()
        session_store.revoke_mechanism(AuthMechanism.PASSWORD_SESSION)
    if _oidc_auth_binding(previous) != _oidc_auth_binding(current):
        session_store.revoke_mechanism(AuthMechanism.OIDC_SESSION)


@router.get("/settings")
async def get_settings_ep(application: ApplicationService = Depends(get_application)):
    return _public_settings(get_settings(), application.definitions)


@router.get("/health")
async def health_check():
    """
    Lightweight liveness probe for Docker HEALTHCHECK and uptime monitors.

    Returns HTTP 200 as long as the process is running. Does not check
    AllDebrid or aria2 — those are external and their absence should not
    restart the container. Use GET /api/stats for full service health.
    """
    return {"status": "ok", "version": read_version()}


@router.get("/version")
async def get_version_ep():
    return {"version": read_version()}


_update_check_cache: dict = {}


def _version_gt(a: str, b: str) -> bool:
    """True when candidate release ``a`` is newer than running release ``b``."""
    return is_version_newer(a, b)


@router.get("/version/check")
async def version_check():
    """Compare running version with latest GitHub release. Cached 30 min."""
    import time, aiohttp as _aiohttp
    cache, now, current = _update_check_cache, time.time(), read_version()
    if cache.get("ts", 0) + 1800 > now:
        return cache.get("result", {"current": current, "latest": None, "update_available": False})
    try:
        async with _aiohttp.ClientSession(timeout=_aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(
                f"{REPOSITORY_API_URL}/releases/latest",
                headers={"Accept": "application/vnd.github.v3+json"},
            ) as r:
                if r.status != 200: raise RuntimeError("GitHub API " + str(r.status))
                rel = await r.json()
        latest = normalize_version_tag(rel.get("tag_name") or "")
        result = {
            "current": current, "latest": latest,
            "update_available": _version_gt(latest, current),
            "release_url":   rel.get("html_url", ""),
            "release_notes": (rel.get("body") or "").strip(),
            "published_at":  (rel.get("published_at") or "")[:10],
        }
        cache["result"] = result
        cache["ts"] = now
        return result
    except Exception as exc:
        logger.warning("Version check failed: %s", sanitize_exception(exc))
        return {"current": current, "latest": None, "update_available": False, "error": sanitize_exception(exc)}


class SettingsUpdate(AppSettings):
    clear_secrets: list[str] = Field(default_factory=list)


def _merge_secret_settings(new: SettingsUpdate, previous: AppSettings) -> dict:
    requested_clears = {str(field) for field in getattr(new, "clear_secrets", [])}
    unknown = requested_clears - _SECRET_SETTINGS
    if unknown:
        raise HTTPException(400, f"Unsupported secret field(s): {', '.join(sorted(unknown))}")
    merged = new.model_dump(exclude={"clear_secrets"})
    explicitly_set = set(new.model_fields_set)
    for field in _AUTH_COMPAT_SETTINGS_FIELDS:
        if field not in explicitly_set:
            merged[field] = getattr(previous, field)
    for field in _SECRET_SETTINGS:
        if str(merged.get(field) or "").strip():
            # An explicit non-empty value is authoritative and overrides a
            # contradictory clear request: a payload that both supplies a
            # secret and asks to clear it must not destroy the supplied value.
            continue
        if field in requested_clears:
            merged[field] = ""
        else:
            merged[field] = getattr(previous, field, "")
    return merged


async def _apply_aria2_settings(application: ApplicationService) -> None:
    """Ensure the aria2 daemon runs and reapply its native tuning after a
    settings change. Shared by the whole-settings route and the scoped
    integration-configuration route (specification section 9.5) so both
    surfaces give aria2 the same targeted, non-blanket handling -- never
    ``ApplicationMaintenanceGate`` (specification section 2.6, 6)."""
    await aria2_runtime.ensure_started()
    try:
        await application.integration_admin("aria2").apply_memory_tuning()
    except Exception as exc:
        logger.warning("Could not apply aria2 memory settings immediately: %s", sanitize_exception(exc))


@router.put("/settings")
async def update_settings(new: SettingsUpdate, application: ApplicationService = Depends(get_application)):
    async with application.configuration_admission():
        # The narrow config-write lock (specification sections 9.5, 13.8)
        # serializes this load-modify-save critical section against every
        # OTHER settings-mutation route -- including the scoped PATCH
        # surfaces below, which deliberately do NOT hold
        # ``configuration_admission()`` and could otherwise run genuinely
        # concurrently with this whole-settings write.
        async with config_write_lock():
            previous = get_settings()
            merged = _merge_secret_settings(new, previous)
            definitions = application.definitions
            from integrations.configuration import normalize_settings
            # The broad document never writes a canonical namespace: those have
            # their own scoped surfaces (``/integrations/{id}/configuration``,
            # ``/transfer-policy``, ``/execution/runtime-limits``). Whatever a
            # stale snapshot echoes for them is ignored, so it cannot undo a
            # concurrently applied scoped write.
            merged["integrations"] = previous.integrations
            merged["integration_groups"] = previous.integration_groups
            merged["transfer_policy"] = previous.transfer_policy
            merged["execution_runtime_limits"] = previous.execution_runtime_limits
            # Notification verification evidence is metadata ABOUT canonical
            # configuration and is never accepted from a request, so it is
            # carried forward here exactly like the canonical namespaces above.
            merged["notification_verification"] = previous.notification_verification
            clean = normalize_settings(AppSettings(**merged), definitions, previous=previous)
            clean = validate_and_sanitise(clean)
            # ... and then retired where it no longer describes what is about to
            # be saved. Sanitisation can itself change material (a rejected
            # avatar URL is cleared), so this is the last word before the write.
            from services.notification_service import carried_verification
            clean = carried_verification(clean)
            try:
                await application.validate_configuration(previous, clean)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            save_settings(clean)
            apply_settings(clean)
        _revoke_stale_authentication_state(previous, clean)
        application.configure()
        await _apply_aria2_settings(application)
        data = _public_settings(clean, application.definitions)
        data["ok"] = True
        return data


# ── Avatar ─────────────────────────────────────────────────────────────────────

@router.post("/settings/upload-avatar")
async def upload_avatar(request: Request, file: UploadFile = File(...)):
    """
    Saves the avatar image to CONFIG_DIR/avatar.<ext> and returns the
    public HTTP URL so Discord can fetch it.
    Discord requires a real HTTPS/HTTP URL — data URIs are rejected.
    """
    ALLOWED = {"image/png": "png", "image/jpeg": "jpg",
                "image/gif": "gif", "image/webp": "webp"}
    MAX_BYTES = 4 * 1024 * 1024

    ct = (file.content_type or "").lower().split(";")[0].strip()
    if ct not in ALLOWED:
        raise HTTPException(400, f"Unsupported type '{ct}'. Allowed: PNG, JPG, GIF, WebP")

    data = await file.read(MAX_BYTES + 1)
    await file.close()
    if len(data) > MAX_BYTES:
        raise HTTPException(413, f"File too large ({len(data)//1024} KB). Limit: 4 MB")

    ext = ALLOWED[ct]
    config_dir = Path(os.getenv("CONFIG_PATH", "/app/config/config.json")).parent
    config_dir.mkdir(parents=True, exist_ok=True)

    for old in config_dir.glob("avatar.*"):
        old.unlink(missing_ok=True)
    (config_dir / f"avatar.{ext}").write_bytes(data)

    public_url = f"{_public_base_url(request)}/api/avatar"
    warning = _avatar_reachability_warning(public_url)

    if warning:
        logger.warning(
            "Avatar uploaded, but URL %s may not be reachable by Discord",
            public_url,
        )

    payload = {"ok": True, "url": public_url, "size_bytes": len(data), "content_type": ct}
    if warning:
        payload["warning"] = warning
    return payload


@router.get("/avatar")
async def serve_avatar():
    """Serves the stored avatar image for Discord to fetch."""
    config_dir = Path(os.getenv("CONFIG_PATH", "/app/config/config.json")).parent
    media_types = {"png": "image/png", "jpg": "image/jpeg",
                   "gif": "image/gif", "webp": "image/webp"}
    for ext, media_type in media_types.items():
        p = config_dir / f"avatar.{ext}"
        if p.exists():
            return FileResponse(str(p), media_type=media_type,
                                headers={"Cache-Control": "public, max-age=3600"})
    raise HTTPException(404, "No avatar uploaded")


# ── Connection tests ───────────────────────────────────────────────────────────

@router.post("/settings/test-alldebrid")
async def test_alldebrid():
    from providers.alldebrid.admin import account_status
    cfg = get_settings()
    if not alldebrid_canonical_options(cfg).api_key:
        raise HTTPException(400, "No API key configured")
    try:
        return await account_status(cfg)
    except Exception as exc:
        raise HTTPException(502, _sanitize_error(exc))


@router.post("/settings/test-aria2")
async def test_aria2( application: ApplicationService = Depends(get_application)):
    try:
        result = await application.integration_admin("aria2").test()
        return {"ok": True, **result}
    except Exception as e:
        raise HTTPException(502, _sanitize_error(e))


@router.post("/settings/aria2-housekeeping")
async def run_aria2_housekeeping_ep( application: ApplicationService = Depends(get_application)):
    try:
        return await application.integration_admin("aria2").housekeeping()
    except Exception as e:
        raise HTTPException(502, _sanitize_error(e))


@router.get("/aria2/runtime")
async def aria2_runtime_status( application: ApplicationService = Depends(get_application)):
    status = await aria2_runtime.status()
    diagnostics = {}
    speed_stat = {"download_speed": 0, "upload_speed": 0, "active": 0}
    try:
        if status.get("running"):
            diagnostics = await application.integration_admin("aria2").memory_diagnostics()
            speed_stat  = await application.integration_admin("aria2").get_global_stat()
    except Exception as exc:
        diagnostics = {"error": sanitize_exception(exc)}
    return {**status, "diagnostics": diagnostics, **speed_stat}


@router.get("/aria2/global-stat")
async def aria2_global_stat( application: ApplicationService = Depends(get_application)):
    """Return live counters used by the topbar indicator."""
    return {"ok": True, **await application.integration_admin("aria2").get_global_stat()}


@router.post("/aria2/runtime/start")
async def aria2_runtime_start( application: ApplicationService = Depends(get_application)):
    status = await aria2_runtime.start()
    application.configure()
    return status


@router.post("/aria2/runtime/stop")
async def aria2_runtime_stop( application: ApplicationService = Depends(get_application)):
    status = await aria2_runtime.stop()
    application.configure()
    return status


@router.post("/aria2/runtime/restart")
async def aria2_runtime_restart( application: ApplicationService = Depends(get_application)):
    status = await aria2_runtime.restart()
    application.configure()
    return status


@router.post("/aria2/runtime/apply")
async def aria2_runtime_apply( application: ApplicationService = Depends(get_application)):
    try:
        await aria2_runtime.apply_options()
        result = await application.integration_admin("aria2").housekeeping()
        return {"ok": True, **result}
    except Exception as e:
        raise HTTPException(502, _sanitize_error(e))


@router.get("/aria2/downloads")
async def aria2_downloads( application: ApplicationService = Depends(get_application)):
    aria2 = _canonical_aria2_options(get_settings())
    try:
        downloads = await application.integration_admin("aria2").get_all(
            aria2.waiting_window,
            aria2.stopped_window,
        )
        downloads = await application.integration_admin("aria2").filter_owned(downloads)
    except Exception as e:
        raise HTTPException(502, _sanitize_error(e))
    items = [public_aria2_download(download) for download in downloads]
    groups = {
        "active": [item for item in items if item["status"] == "active"],
        "waiting": [item for item in items if item["status"] in {"waiting", "paused"}],
        "stopped": [item for item in items if item["status"] not in {"active", "waiting", "paused"}],
    }
    return {
        "ok": True,
        "items": items,
        "groups": groups,
        "summary": {
            "active": len(groups["active"]),
            "waiting": len(groups["waiting"]),
            "stopped": len(groups["stopped"]),
            "download_speed": sum(item["download_speed"] for item in groups["active"]),
            "remaining_length": sum(item["remaining_length"] for item in items),
        },
    }


@router.post("/aria2/downloads/{gid}/{action}")
async def aria2_download_action(gid: str, action: str, application: ApplicationService = Depends(get_application)):
    if action not in {"pause", "resume", "remove"}:
        raise HTTPException(400, "Unsupported aria2 action")
    try:
        result = await application.integration_admin("aria2").control(gid, action)
        return {"ok": True, "gid": gid, "action": action, **result}
    except PermissionError as e:
        raise HTTPException(403, _sanitize_error(e))
    except Exception as e:
        raise HTTPException(502, _sanitize_error(e))


# ── Torrents ───────────────────────────────────────────────────────────────────
#
# The operational torrents collection (GET /api/torrents) is owned solely by
# api.operational_downloads.list_operational_torrents. Its canonical lifecycle
# rule (soft-deleted *and* fully consolidated rows excluded from the default
# view) and its bounded single-read projection differ from the historical
# per-row presentation rebuild, so it is declared there rather than here.

@router.post("/torrents/add-magnet")
async def add_magnet(body: dict, application: ApplicationService = Depends(get_application)):
    magnet = (body.get("magnet") or "").strip()
    if not magnet:
        raise HTTPException(400, "magnet is required")
    # Optional neutral submission intent. Omitted -> ALL (correction section 6):
    # a historical/headless caller sending the unchanged body shape never enters
    # the interactive file-selection lifecycle.
    try:
        row = await application.submit_magnet(
            magnet, source="manual", selection_mode=body.get("selection_mode"),
        )
        return public_payload(row)
    except ValueError as exc:
        raise HTTPException(400, _sanitize_error(exc))
    except Exception as exc:
        logger.exception("add_magnet failed: %s", _sanitize_error(exc))
        raise HTTPException(502, _sanitize_error(exc))


# How much of a streamed upload is read at a time. Bounded, so a large upload
# costs one chunk of memory rather than its whole size.
_UPLOAD_CHUNK_BYTES = 1024 * 1024


@router.post("/torrents/add-file")
async def add_torrent_file(
    file: UploadFile = File(...),
    selection_mode: str | None = Form(default=None),
    application: ApplicationService = Depends(get_application),
):
    """Upload a .torrent metafile directly to AllDebrid.

    The local aria2 daemon never receives the torrent metafile.  AllDebrid
    processes it and ADC later dispatches only the unlocked HTTPS file URLs.

    ``selection_mode`` is an optional multipart form field; omitted -> ALL. The
    built-in browser sends ``interactive``; a historical multipart upload with
    only ``file=<torrent>`` keeps the ALL default (correction section 6).
    """
    max_bytes = 16 * 1024 * 1024
    filename = Path(file.filename or "upload.torrent").name

    if not filename.lower().endswith(".torrent"):
        raise HTTPException(400, "A .torrent file is required")

    try:
        data = await file.read(max_bytes + 1)
    finally:
        await file.close()

    if not data:
        raise HTTPException(400, "Torrent file is empty")
    if len(data) > max_bytes:
        raise HTTPException(413, "Torrent file exceeds the 16 MB upload limit")

    try:
        result = await application.submit_torrent(
            data,
            filename,
            source="manual_file",
            selection_mode=selection_mode,
        )
        return public_payload(result)
    except ValueError as exc:
        raise HTTPException(400, _sanitize_error(exc))
    except Exception as exc:
        raise HTTPException(502, _sanitize_error(exc))


@router.post("/usenet/add-file")
async def add_usenet_file(
    file: UploadFile = File(...),
    application: ApplicationService = Depends(get_application),
):
    """Upload an .nzb posting for DebridPulse to acquire.

    The route does nothing but hand the upload to the canonical submission
    seam: the Usenet provider validates and normalizes it and universal core
    chooses the executor. Nothing here knows how acquisition is performed.

    The bytes are handed over as a STREAM, not a buffer. A real posting's
    manifest is routinely tens or hundreds of megabytes -- reading one into a
    single object here would cost multiples of its size in memory before the
    application had even seen it -- so the route reads it in chunks and the
    staged-input owner writes it through. The route is not the persistence
    owner and holds no ceiling of its own: the one ceiling belongs to that
    owner and is enforced as the stream is written.
    """
    filename = Path(file.filename or "upload.nzb").name
    if not filename.lower().endswith(".nzb"):
        raise HTTPException(400, "An .nzb file is required")

    async def chunks():
        while True:
            chunk = await file.read(_UPLOAD_CHUNK_BYTES)
            if not chunk:
                return
            yield chunk

    try:
        return public_payload(await application.submit_nzb(chunks(), filename, source="manual_file"))
    except ValueError as exc:
        raise HTTPException(400, _sanitize_error(exc)) from None
    except Exception as exc:
        logger.exception("add_usenet_file failed: %s", _sanitize_error(exc))
        raise HTTPException(502, _sanitize_error(exc))
    finally:
        await file.close()


@router.post("/links/add")
async def add_debrid_links(body: dict, application: ApplicationService = Depends(get_application)):
    """Submit one or more ordinary hoster URLs as a tracked transfer batch."""
    raw_links = body.get("links", [])
    if isinstance(raw_links, str):
        links = [line.strip() for line in raw_links.splitlines() if line.strip()]
    elif isinstance(raw_links, list):
        links = [str(value).strip() for value in raw_links if str(value).strip()]
    else:
        raise HTTPException(400, "links must be a list or newline-separated string")
    try:
        return public_payload(await application.submit_links(links))
    except ValueError as exc:
        raise HTTPException(400, _sanitize_error(exc))
    except Exception as exc:
        logger.exception("add_debrid_links failed: %s", _sanitize_error(exc))
        raise HTTPException(502, _sanitize_error(exc))


@router.post("/torrents/check-duplicate")
async def check_torrent_duplicate(body: dict):
    """Read-only duplicate preview. Never uploads/imports anything to AllDebrid."""
    from services.duplicates import check_before_add

    candidate = _duplicate_candidate_from_payload(body, source=str(body.get("source") or "preview"))
    if not (candidate.infohash or candidate.magnet or candidate.title or candidate.resource_id):
        raise HTTPException(400, "title, magnet, hash, infohash, or resource_id is required")
    decision = await check_before_add(candidate)
    return {"ok": True, "duplicate": decision.as_dict()}


@router.post("/torrents/import-existing")
async def import_existing( application: ApplicationService = Depends(get_application)):
    return {"ok": True, **await application.reconcile_inventory()}


@router.get("/torrents/diagnose")
async def diagnose_torrents():
    """Return a full count breakdown of all local torrent statuses."""
    async with get_db() as db:
        all_counts = await (await db.execute(
            """SELECT status, COUNT(*) AS cnt FROM torrents
               GROUP BY status ORDER BY cnt DESC"""
        )).fetchall()
        non_terminal = await (await db.execute(
            """SELECT t.id, t.name, t.status,
                      (SELECT COUNT(*) FROM download_files f WHERE f.torrent_id=t.id AND f.blocked=0) AS file_count
               FROM torrents t
               WHERE t.status NOT IN ('completed', 'deleted')
               ORDER BY t.id DESC LIMIT 20"""
        )).fetchall()
    return {
        "status_counts": [dict(r) for r in all_counts],
        "sample_non_terminal": [dict(r) for r in non_terminal],
    }


@router.post("/torrents/recover-all")
async def recover_all_ready( application: ApplicationService = Depends(get_application)):
    """Reconcile durable requests and owned executions through the core."""
    return await application.recover()


@router.get("/torrents/{torrent_id}/files-preview")
async def torrent_files_preview(torrent_id: int, application: ApplicationService = Depends(get_application)):
    try:
        return await application.preview(torrent_id)
    except KeyError:
        raise HTTPException(404, "Transfer not found")


@router.post("/torrents/{torrent_id}/files/{file_id}/block")
async def block_file(torrent_id: int, file_id: int, blocked: bool = True, application: ApplicationService = Depends(get_application)):
    try:
        return await application.select_artifact(torrent_id, file_id, selected=not blocked)
    except KeyError:
        raise HTTPException(404, "File not found")
    except ValueError as exc:
        raise HTTPException(409, _sanitize_error(exc))


@router.get("/torrents/{torrent_id}")
async def get_torrent(torrent_id: int, application: ApplicationService = Depends(get_application)):
    engine = getattr(application, "engine", None)
    item = await application.repository.presentation(
        torrent_id, details=True,
        capacity_only_blocked_ids=dispatch_admission.capacity_only_blocked_ids(engine),
    )
    if item is None:
        raise HTTPException(404, "Transfer not found")
    return _public_transfer_presentation(item, application.definitions)


@router.delete("/torrents/{torrent_id}")
async def delete_torrent(torrent_id: int, from_alldebrid: bool = True, application: ApplicationService = Depends(get_application)):
    # The old query parameter is retained as an external API compatibility alias.
    try:
        return await application.delete(torrent_id, remote=from_alldebrid)
    except KeyError:
        raise HTTPException(404, "Transfer not found")


@router.post("/torrents/{torrent_id}/input")
async def submit_transfer_input(torrent_id: int, request: Request, application: ApplicationService = Depends(get_application)):
    raw = await request.body()
    if len(raw) > 512 * 1024:
        raise HTTPException(413, "Authentication input is too large")
    try:
        body = _json.loads(raw)
    except (TypeError, ValueError):
        raise HTTPException(400, "Authentication input must be a JSON object") from None
    finally:
        raw = b""
    if not isinstance(body, dict):
        raise HTTPException(400, "Authentication input must be a JSON object")
    allowed_fields = {"challenge_id", "method", "username", "password", "private_key", "passphrase"}
    if set(body) - allowed_fields:
        body.clear()
        raise HTTPException(400, "Authentication input contains unsupported fields")
    challenge_id = body.get("challenge_id")
    method = body.get("method")
    if not isinstance(challenge_id, str) or not challenge_id or not isinstance(method, str) or not method:
        body.clear()
        raise HTTPException(400, "challenge_id and method are required")
    values = {name: body[name] for name in ("username", "password", "private_key", "passphrase") if name in body}
    try:
        return await application.submit_input(torrent_id, challenge_id=challenge_id, method=method, values=values)
    except KeyError:
        raise HTTPException(404, "Transfer not found") from None
    except ValueError:
        raise HTTPException(409, "Authentication input was not accepted") from None
    finally:
        values.clear()
        body.clear()


@router.post("/torrents/{torrent_id}/cancel")
async def cancel_torrent(torrent_id: int, application: ApplicationService = Depends(get_application)):
    try:
        return await application.cancel(torrent_id)
    except KeyError:
        raise HTTPException(404, "Transfer not found") from None


@router.post("/torrents/{torrent_id}/retry")
async def retry_torrent(torrent_id: int, application: ApplicationService = Depends(get_application)):
    try:
        return await application.retry(torrent_id)
    except KeyError:
        raise HTTPException(404, "Transfer not found")


@router.post("/torrents/{torrent_id}/pause")
async def pause_torrent(torrent_id: int, application: ApplicationService = Depends(get_application)):
    try:
        await application.pause(torrent_id)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(400, _sanitize_error(e))


@router.post("/torrents/{torrent_id}/resume")
async def resume_torrent(torrent_id: int, application: ApplicationService = Depends(get_application)):
    try:
        await application.resume(torrent_id)
        return {"ok": True, "paused": await application.repository.globally_paused()}
    except Exception as e:
        raise HTTPException(400, _sanitize_error(e))

class LabelUpdate(BaseModel):
    label: str = ""
    priority: int = 0


@router.put("/torrents/{torrent_id}/label")
async def set_torrent_label(torrent_id: int, body: LabelUpdate, application: ApplicationService = Depends(get_application)):
    await application.repository.update_metadata(torrent_id, label=body.label.strip(), priority=body.priority)
    return {"ok": True}


class BulkAction(BaseModel):
    ids: list
    action: Literal["delete", "retry", "reset", "pause", "resume", "remove_label"]


@router.post("/torrents/bulk")
async def bulk_action(body: BulkAction, application: ApplicationService = Depends(get_application)):
    if not body.ids:
        raise HTTPException(400, "No IDs provided")
    ok = failed = 0
    for value in body.ids:
        try:
            tid = int(value)
            if body.action == "delete":
                await application.delete(tid, remote=True)
            elif body.action in {"retry", "reset"}:
                await application.retry(tid)
            elif body.action == "pause":
                await application.pause(tid)
            elif body.action == "resume":
                await application.resume(tid)
            elif body.action == "remove_label":
                await application.repository.update_metadata(tid, label="")
            ok += 1
        except Exception:
            failed += 1
    return {"ok": ok, "failed": failed}


# ── Events ─────────────────────────────────────────────────────────────────────
#
# The activity events collection (GET /api/events) is owned solely by
# api.operational_downloads.list_activity_events, which applies the optional
# search / severity / timeframe predicates before the result ceiling. The
# streaming and subscriber-count event routes below remain here.

@router.get("/admin/performance")
async def performance_diagnostics( application: ApplicationService = Depends(get_application)):
    from core.performance import snapshot as performance_snapshot
    from db.database import db_runtime_metrics

    return {
        "timers": performance_snapshot(),
        "database": db_runtime_metrics(),
        "aria2": application.integration_admin("aria2").rpc_metrics(),
    }


# ── Statistics ─────────────────────────────────────────────────────────────────

@router.get("/stats")
async def get_stats(application: ApplicationService = Depends(get_application)):
    started = time.monotonic()
    async with get_db() as db:
        by_status_rows = await db.fetchall(
            "SELECT status, COUNT(*) as count FROM torrents GROUP BY status"
        )
        by_status = {r["status"]: r["count"] for r in by_status_rows}

        last_24h_expr = _sql_now_minus("1 day")
        last_7d_expr = _sql_now_minus("7 days")
        aggregate = await db.fetchone(
            f"""SELECT
                   COALESCE(SUM(CASE WHEN status='completed' THEN size_bytes ELSE 0 END), 0)
                       AS total_completed_bytes,
                   SUM(CASE WHEN status IN ('downloading','processing','uploading','paused')
                            THEN 1 ELSE 0 END) AS active_downloads,
                   SUM(CASE WHEN COALESCE(extraction_status,'')='extracting'
                            THEN 1 ELSE 0 END) AS extracting_count,
                   SUM(CASE WHEN status IN ('ready','queued') THEN 1 ELSE 0 END)
                       AS queued_downloads,
                   SUM(CASE WHEN status='downloading' THEN 1 ELSE 0 END)
                       AS operator_active_downloads,
                   AVG(CASE WHEN status='downloading' THEN COALESCE(progress, 0)
                            ELSE NULL END) AS operator_active_progress_pct,
                   SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS error_count,
                   SUM(CASE WHEN status='completed' AND COALESCE(extraction_status,'')!='extracting' THEN 1 ELSE 0 END) AS completed_count,
                   SUM(CASE WHEN completed_at >= {last_24h_expr}
                                AND COALESCE(extraction_status,'')!='extracting' THEN 1 ELSE 0 END)
                       AS completed_last_24h,
                   SUM(CASE WHEN completed_at >= {last_7d_expr}
                                AND COALESCE(extraction_status,'')!='extracting' THEN 1 ELSE 0 END)
                       AS completed_last_7d,
                   AVG(CASE
                       WHEN completed_at IS NOT NULL AND created_at IS NOT NULL
                       THEN CAST((julianday(completed_at)-julianday(created_at))*86400 AS INTEGER)
                       ELSE NULL END) AS avg_download_duration_seconds,
                   AVG(CASE WHEN status='completed' AND size_bytes>0 THEN size_bytes
                            ELSE NULL END) AS avg_torrent_size_bytes,
                   (SELECT COUNT(*) FROM download_files WHERE blocked=1)
                       AS total_blocked_files
               FROM torrents"""
        ) or {}

    operator_active = int(aggregate.get("operator_active_downloads") or 0)
    operator_progress = None
    if operator_active > 0:
        average = float(aggregate.get("operator_active_progress_pct") or 0)
        operator_progress = max(0, min(100, round(average)))

    error_count = int(aggregate.get("error_count") or 0)
    completed_count = int(aggregate.get("completed_count") or 0)
    active_downloads = int(aggregate.get("active_downloads") or 0)
    extracting_count = int(aggregate.get("extracting_count") or 0)
    terminal = completed_count + error_count
    success_rate = (
        round(completed_count / terminal * 100, 1)
        if terminal > 0
        else None
    )

    db_type = "sqlite"

    result = {
        "version": read_version(),
        "by_status": by_status,
        "total_completed_bytes": int(aggregate.get("total_completed_bytes") or 0),
        "db_type": db_type,
        "total_blocked_files": int(aggregate.get("total_blocked_files") or 0),
        "active_downloads": active_downloads,
        "active_operations": active_downloads + extracting_count,
        "extracting_count": extracting_count,
        "queued_downloads": int(aggregate.get("queued_downloads") or 0),
        "operator_active_downloads": operator_active,
        "operator_active_progress_pct": operator_progress,
        "error_count": error_count,
        "completed_count": completed_count,
        "success_rate_pct": success_rate,
        "completed_last_24h": int(aggregate.get("completed_last_24h") or 0),
        "completed_last_7d": int(aggregate.get("completed_last_7d") or 0),
        "avg_download_duration_seconds": int(
            aggregate.get("avg_download_duration_seconds") or 0
        ),
        "avg_torrent_size_bytes": int(aggregate.get("avg_torrent_size_bytes") or 0),
        "paused": await application.repository.globally_paused(),
    }
    from core.performance import observe
    observe("api.stats", time.monotonic() - started)
    return result


@router.get("/stats/detail")
async def get_stats_detail(period: str = "all"):
    """
    period: "1h" | "24h" | "7d" | "30d" | "1y" | "all"
    All metrics (including totals) are filtered to the selected period.
    """
    period_map = {
        "1h":  (_sql_now_minus("1 hour"),  "1h",  _sql_strftime("%H:%M", "completed_at"), 60),
        "24h": (_sql_now_minus("1 day"),   "24h", _sql_strftime("%H:00", "completed_at"), 24),
        "7d":  (_sql_now_minus("7 days"),  "7d",  _sql_date("completed_at"),              7),
        "30d": (_sql_now_minus("30 days"), "30d", _sql_date("completed_at"),              30),
        "1y":  (_sql_now_minus("1 year"),  "1y",  _sql_strftime("%Y-%m", "completed_at"), 12),
        "all": (None,                      "all", _sql_date("completed_at"),              None),
    }
    entry = period_map.get(period, period_map["all"])
    cutoff, period_label, date_fmt, _ = entry
    where_ts   = f"WHERE created_at >= {cutoff}"    if cutoff else ""
    where_done = f"WHERE completed_at >= {cutoff}"   if cutoff else ""
    where_comp = (
        f"WHERE status='completed' AND COALESCE(extraction_status,'')!='extracting' AND completed_at >= {cutoff}"
        if cutoff
        else "WHERE status='completed' AND COALESCE(extraction_status,'')!='extracting'"
    )

    async with get_db() as db:
        # ── Totals (period-filtered) ─────────────────────────────────────────
        totals_row = await db.fetchone(
            f"SELECT COUNT(*) as torrent_total, COALESCE(SUM(size_bytes),0) as torrent_size_total "
            f"FROM torrents {where_ts}"
        ) or {}
        totals = dict(totals_row)

        completed_count = (await db.fetchone(
            f"SELECT COUNT(*) as c FROM torrents {where_comp}") or {}).get("c", 0)
        error_count = (await db.fetchone(
            f"SELECT COUNT(*) as c FROM torrents WHERE status='error'"
            + (f" AND created_at >= {cutoff}" if cutoff else "")) or {}).get("c", 0)
        terminal = completed_count + error_count
        totals["success_rate_pct"] = round(completed_count / terminal * 100, 1) if terminal > 0 else None

        completed_size_row = await db.fetchone(
            f"SELECT COALESCE(SUM(size_bytes),0) as v FROM torrents {where_comp}")
        totals["completed_size"]  = completed_size_row["v"] if completed_size_row else 0
        totals["completed_count"] = completed_count

        partial_row = await db.fetchone(
            "SELECT COUNT(*) as c FROM torrents "
            "WHERE (status IN ('processing','downloading','dispatched','partial') OR COALESCE(extraction_status,'')='extracting')"
            + (f" AND created_at >= {cutoff}" if cutoff else ""))
        totals["partial_total"] = partial_row["c"] if partial_row else 0

        # ── Breakdowns ───────────────────────────────────────────────────────
        torrent_status = await db.fetchall(
            f"SELECT status, COUNT(*) as count FROM torrents {where_ts} "
            f"GROUP BY status ORDER BY count DESC")
        where_files = (f"WHERE updated_at >= {cutoff}" if cutoff else "")
        file_status = await db.fetchall(
            f"SELECT status, COUNT(*) as count, COALESCE(SUM(size_bytes),0) as size_bytes "
            f"FROM download_files {where_files} GROUP BY status ORDER BY count DESC")
        event_levels = await db.fetchall(
            f"SELECT level, COUNT(*) as count FROM events {where_ts} GROUP BY level")
        sources = await db.fetchall(
            f"SELECT source, COUNT(*) as count FROM torrents {where_ts} "
            f"GROUP BY source ORDER BY count DESC LIMIT 10")

        # ── Chart data (period-aware grouping) ───────────────────────────────
        _cutoff_90d = _sql_now_minus("90 days")
        if period == "1h":
            _grp = _sql_strftime("%H:%M", "completed_at")
            daily_completions = await db.fetchall(
                f"SELECT {_grp} as date, COUNT(*) as count "
                f"FROM torrents WHERE completed_at >= {cutoff} AND status='completed' "
                f"GROUP BY {_grp} ORDER BY date ASC")
        elif period == "24h":
            # Group and label by hour — both SELECT and GROUP BY use the same expression
            _grp = _sql_strftime("%H:00", "completed_at")
            daily_completions = await db.fetchall(
                f"SELECT {_grp} as date, COUNT(*) as count "
                f"FROM torrents WHERE completed_at >= {cutoff} AND status='completed' "
                f"GROUP BY {_grp} ORDER BY {_grp} ASC")
        elif period in ("7d", "30d"):
            _grp = _sql_date("completed_at")
            daily_completions = await db.fetchall(
                f"SELECT {_grp} as date, COUNT(*) as count "
                f"FROM torrents WHERE completed_at >= {cutoff} AND status='completed' "
                f"GROUP BY {_grp} ORDER BY date ASC")
        elif period == "1y":
            _grp = _sql_strftime("%Y-%m", "completed_at")
            daily_completions = await db.fetchall(
                f"SELECT {_grp} as date, COUNT(*) as count "
                f"FROM torrents WHERE completed_at >= {cutoff} AND status='completed' "
                f"GROUP BY {_grp} ORDER BY date ASC")
        else:  # all — last 90 days grouped by day
            _grp = _sql_date("completed_at")
            daily_completions = await db.fetchall(
                f"SELECT {_grp} as date, COUNT(*) as count "
                f"FROM torrents WHERE completed_at >= {_cutoff_90d} AND status='completed' "
                f"GROUP BY {_grp} ORDER BY date ASC")

        return {
            "period":             period_label,
            "totals":             totals,
            "torrent_status":     torrent_status,
            "file_status":        file_status,
            "event_levels":       event_levels,
            "daily_completions":  daily_completions,
            "sources":            sources,
        }


# ── Processing control ─────────────────────────────────────────────────────────

@router.post("/processing/pause")
async def pause_processing( application: ApplicationService = Depends(get_application)):
    result = await application.pause_all()
    return {"ok": True, **result}

@router.post("/processing/resume")
async def resume_processing( application: ApplicationService = Depends(get_application)):
    result = await application.resume_all()
    return {"ok": True, **result}

# ── Changelog ──────────────────────────────────────────────────────────────────

_changelog_cache: dict = {}


@router.get("/changelog")
async def get_changelog():
    """Return CHANGELOG.md.
    Uses local file when it contains the running version entry.
    Falls back to GitHub Releases API (1h cache) for stale images."""
    import time, aiohttp as _aiohttp
    local: str | None = None
    for c in (Path("/app/CHANGELOG.md"),
              Path(__file__).resolve().parents[2] / "CHANGELOG.md"):
        if c.exists():
            local = c.read_text(encoding="utf-8"); break
    running = read_version()
    if local and ("[" + running + "]") in local:
        return {"content": local, "source": "local"}
    cache, now = _changelog_cache, time.time()
    if cache.get("ts", 0) + 3600 > now:
        return {"content": cache.get("content", local or ""), "source": "github_cache"}
    sep = "\n\n---\n\n"
    try:
        async with _aiohttp.ClientSession(timeout=_aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(
                f"{REPOSITORY_API_URL}/releases?per_page=25",
                headers={"Accept": "application/vnd.github.v3+json"},
            ) as r:
                if r.status == 200:
                    rels = await r.json()
                    parts = []
                    for rel in rels:
                        body = (rel.get("body") or "").strip()
                        tag  = rel.get("tag_name", "")
                        date = (rel.get("published_at") or "")[:10]
                        parts.append(body or "## " + tag + " \u2014 " + date)
                    combined = sep.join(parts)
                    cache["content"] = combined
                    cache["ts"] = now
                    return {"content": combined, "source": "github"}
    except Exception as exc:
        logger.warning("Changelog GitHub fetch failed: %s", exc)
    return {"content": local or "", "source": "local_fallback"}


# ── Admin ──────────────────────────────────────────────────────────────────────

@router.post("/admin/backup")
async def trigger_backup():
    from services.backup import run_backup
    result = await run_backup()
    return result


@router.get("/admin/backups")
async def list_backups():
    from services.backup import list_backups as _list
    return {"backups": _list()}


@router.post("/admin/database/backup")
async def trigger_database_backup():
    from services.db_maintenance import run_database_backup
    return await run_database_backup()


@router.get("/admin/database/backups")
async def list_database_backups():
    from services.db_maintenance import list_database_backups as _list
    return {"backups": _list()}


@router.post("/admin/drop-page-cache")
async def drop_page_cache_ep():
    """
    Release the Linux kernel page cache for all completed download files.
    This frees RAM that Linux holds as file cache after downloads finish.
    Safe to call at any time — files on disk are not affected.
    """
    from services.page_cache import drop_page_cache_for_file
    from pathlib import Path

    try:
        async with get_db() as db:
            rows = await (await db.execute(
                "SELECT local_path FROM download_files "
                "WHERE status='completed' AND local_path IS NOT NULL"
            )).fetchall()
        paths = [r["local_path"] for r in rows if r["local_path"]]
        dropped = sum(1 for p in paths if drop_page_cache_for_file(p))
        return {
            "ok": True,
            "files_processed": len(paths),
            "cache_released": dropped,
            "message": f"Page cache released for {dropped}/{len(paths)} files",
        }
    except Exception as e:
        raise HTTPException(500, _sanitize_error(e))


@router.get("/admin/memory-info")
async def memory_info_ep():
    """
    Read /proc/meminfo to show the difference between total RAM usage
    and actual used RAM vs kernel page cache.
    This helps diagnose whether high RAM usage is a real leak or
    normal kernel page-cache behaviour.
    """
    import re as _re
    from pathlib import Path as _Path

    info = {}
    try:
        text = _Path("/proc/meminfo").read_text()
        for line in text.splitlines():
            m = _re.match(r"^(\w+):\s+(\d+)\s+kB$", line)
            if m:
                info[m.group(1)] = int(m.group(2)) * 1024

        def fmt(b: int) -> str:
            if b >= 1 << 30:
                return f"{b / (1 << 30):.1f} GB"
            if b >= 1 << 20:
                return f"{b / (1 << 20):.1f} MB"
            return f"{b / (1 << 10):.0f} KB"

        total       = info.get("MemTotal", 0)
        free        = info.get("MemFree", 0)
        available   = info.get("MemAvailable", 0)
        cached      = info.get("Cached", 0) + info.get("SwapCached", 0)
        buffers     = info.get("Buffers", 0)
        used        = total - free - cached - buffers
        page_cache  = cached + buffers

        return {
            "total":           fmt(total),
            "really_used":     fmt(used),
            "page_cache":      fmt(page_cache),
            "available":       fmt(available),
            "free":            fmt(free),
            "note": (
                "really_used is actual process RAM. "
                "page_cache is kernel file cache (shown as 'used' in Unraid dashboard "
                "but reclaimed automatically when needed). "
                "If page_cache is large, run POST /admin/drop-page-cache to release it."
            ),
            "raw_kb": {k: v // 1024 for k, v in info.items()
                       if k in ("MemTotal","MemFree","MemAvailable","Cached","Buffers","SwapTotal","SwapFree")},
        }
    except Exception as e:
        raise HTTPException(500, _sanitize_error(e))



_database_wipe_lock = asyncio.Lock()


@router.post("/admin/database/wipe")
async def wipe_database_admin(body: dict | None = None, application: ApplicationService = Depends(get_application)):
    cfg = get_settings()
    if not getattr(cfg, "db_wipe_enabled", False):
        raise HTTPException(400, "Database wipe is disabled in settings")
    if not await application.repository.globally_paused():
        raise HTTPException(409, "Pause processing before wiping the database")
    if not (body or {}).get("confirm"):
        raise HTTPException(400, "Wipe confirmation required")

    if _database_wipe_lock.locked():
        raise HTTPException(409, "Database wipe is already in progress")

    async with _database_wipe_lock:
        scheduler_was_running = scheduler_runtime.scheduler_running()
        scheduler_stopped = False
        quiesced = False
        try:
            async with application.database_wipe_admission():
                # A state-changing request could have been admitted immediately
                # before maintenance closed admission. The gate drains it first;
                # refresh every destructive setting only after that drain.
                cfg = get_settings()
                if not getattr(cfg, "db_wipe_enabled", False):
                    raise HTTPException(400, "Database wipe is disabled in settings")
                if not await application.repository.globally_paused():
                    raise HTTPException(409, "Pause processing before wiping the database")

                if scheduler_was_running:
                    # Claim restart responsibility before the interruptible stop.
                    scheduler_stopped = True
                    await scheduler_runtime.stop_scheduler()

                try:
                    quiesce_result = await application.quiesce_for_database_wipe()
                    quiesced = True
                except Exception as exc:
                    raise HTTPException(409, _sanitize_error(exc))

                try:
                    # Application execution admission, scheduler activity, provider
                    # work, materialization work and owned aria2 execution are all
                    # closed/drained before this database writer gate is acquired.
                    async with database_maintenance():
                        backup_result = None
                        if getattr(cfg, "db_backup_before_wipe", True):
                            from services.db_maintenance import run_database_backup
                            backup_result = await run_database_backup()
                            if backup_result.get("skipped"):
                                raise HTTPException(409, "Pre-wipe database backup is required but disabled")
                            if backup_result.get("errors"):
                                raise HTTPException(500, "Pre-wipe database backup failed; wipe aborted")

                        from services.db_maintenance import wipe_database
                        result = await wipe_database(verified_quiesced=True)

                    return {**result, "backup": backup_result, "quiesced": quiesce_result}
                finally:
                    if quiesced:
                        await application.release_database_wipe_quiescence()
                        quiesced = False
        finally:
            # Restart only after application admission has reopened so new
            # scheduler tasks cannot immediately bounce off the maintenance gate.
            if scheduler_stopped:
                await scheduler_runtime.start_scheduler(application)



# ── Statistics & Reporting ──────────────────────────────────────────────────────



@router.get("/aria2/global-options")
async def aria2_get_global_options( application: ApplicationService = Depends(get_application)):
    """Return current aria2 global options (includes speed limits)."""
    try:
        opts = await application.integration_admin("aria2").get_global_options()
        return {
            "ok": True,
            "max_download_speed": int(opts.get("max-overall-download-limit") or 0),
            "max_upload_speed":   int(opts.get("max-overall-upload-limit")   or 0),
            "max_concurrent_downloads": int(
                application.engine.policy.max_active_executions
            ),
            "raw": {k: v for k, v in opts.items() if "limit" in k or "speed" in k or "concurrent" in k},
        }
    except Exception as e:
        raise HTTPException(502, _sanitize_error(e))


@router.post("/aria2/global-options")
async def aria2_set_global_options(body: dict, application: ApplicationService = Depends(get_application)):
    """
    Legacy compatibility edge only (specification section 9.6). Forwards
    download-bandwidth and concurrency mutations to the SAME canonical scoped
    routes the neutral UI now calls directly -- ``patch_execution_runtime_limits``
    and ``patch_transfer_policy`` -- so exactly one implementation ever
    applies or persists either value; this route holds no independent
    native-apply or persistence logic for them (Gate 9 revision-4 rejection
    finding 5: two implementations were previously able to change the same
    underlying state). ``max_upload_speed`` has no canonical neutral surface
    (specification section 4.4: not a release-driving UI requirement for
    1.0.12) and is applied/persisted narrowly below rather than reintroducing
    a second copy of either canonical pipeline.

    Accepts: max_download_speed (bytes/s, 0=unlimited), max_upload_speed,
    max_concurrent_downloads. The UI has migrated to the neutral routes and
    sends at most one of these per request; no external dependency requires
    combined multi-field requests to keep working atomically.
    """
    async with application.application_operation():
        requested = set(body)
        if not requested & {"max_download_speed", "max_upload_speed", "max_concurrent_downloads"}:
            raise HTTPException(400, "No valid options provided")

        applied: dict = {}
        last_apply_error = None
        try:
            if "max_download_speed" in body:
                result = await patch_execution_runtime_limits(
                    {"max_download_bytes_per_second": body["max_download_speed"]}, application=application,
                )
                last_apply_error = result.get("last_apply_error")
                applied["max-overall-download-limit"] = str(result["configured"]["max_download_bytes_per_second"])

            if "max_upload_speed" in body:
                val = int(body["max_upload_speed"])
                # Upload bandwidth has no neutral surface (specification
                # section 4.4), so it is an aria2-owned option. It is written
                # only through the canonical scoped integration surface -- this
                # compatibility edge holds no persistence logic of its own --
                # and the native apply is then attempted so a failure is still
                # reported truthfully.
                await patch_integration_configuration(
                    "aria2", IntegrationConfigurationUpdate(options={"max_upload_limit": val}),
                    application=application,
                )
                try:
                    await application.integration_admin("aria2").change_global_options(
                        {"max-overall-upload-limit": str(val)},
                    )
                except Exception as exc:
                    last_apply_error = _sanitize_error(exc)
                applied["max-overall-upload-limit"] = str(val)

            if "max_concurrent_downloads" in body:
                try:
                    update = TransferPolicyUpdate(max_concurrent_executions=body["max_concurrent_downloads"])
                except Exception as exc:
                    raise HTTPException(400, _sanitize_error(exc)) from None
                result = await patch_transfer_policy(update, application=application)
                # Gate 9 revision-6 rejection finding 3: propagate the
                # canonical route's own native-apply truth through this
                # compatibility forwarder -- an ``ok: true`` response here
                # must never imply a native concurrency apply that
                # ``patch_transfer_policy`` itself reported as failed.
                last_apply_error = last_apply_error or result.get("last_apply_error")
                concurrency = result["max_concurrent_executions"]
                applied["max-concurrent-downloads"] = str(concurrency)

            return {
                "ok": last_apply_error is None,
                "applied": applied,
            }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, _sanitize_error(e))


@router.get("/execution/runtime-limits")
async def get_execution_runtime_limits(application: ApplicationService = Depends(get_application)):
    """Neutral live executor-runtime limits (specification section 4.4).
    ``configured`` is the durable desired value; ``effective`` is the value
    the core runtime owner has proven enforced across every executor that
    currently holds a bandwidth reservation -- ``None`` (with
    ``last_apply_error``) whenever that cannot be proven (section 2.7)."""
    return await application.execution_runtime_limits()


@router.get("/execution/runtime-status")
async def get_execution_runtime_status(application: ApplicationService = Depends(get_application)):
    """Neutral live runtime status for the operator-facing shell.

    The ONE fact source for the topbar indicator and the browser-tab title:
    current aggregate download throughput across every acquiring executor,
    DebridPulse's own execution-admission occupancy, and the configured global
    download cap. Executor-specific routes remain available as diagnostics;
    they no longer own generic presentation truth, so a future executor joins
    by implementing the neutral contracts rather than by adding a branch here.
    """
    return {"ok": True, **await application.execution_runtime_status()}


@router.patch("/execution/runtime-limits")
async def patch_execution_runtime_limits(body: dict, application: ApplicationService = Depends(get_application)):
    """Scoped neutral runtime-limit mutation (specification sections 4.4, 9.5,
    9.6). Reloads only this namespace, preserves every unrelated setting, and
    uses the SAME ordinary application-operation admission every other
    command already uses -- never application-wide maintenance merely because
    the value is persisted (specification section 2.6, 13.3)."""
    if "max_download_bytes_per_second" not in body:
        raise HTTPException(400, "max_download_bytes_per_second is required")
    try:
        value = max(0, int(body["max_download_bytes_per_second"]))
    except (TypeError, ValueError):
        raise HTTPException(400, "max_download_bytes_per_second must be an integer") from None

    async with application.application_operation():
        # The config-write lock serializes the full desired-write ->
        # reinjection -> convergence pipeline as one critical section, so the
        # last writer under the lock wins for the durable value and the
        # enforced executor ceilings together. Durable desired state is
        # persisted FIRST: a convergence failure afterwards is an explicit,
        # observable configured != effective divergence (section 2.7), never
        # a silent split between executors and disk.
        async with config_write_lock():
            current = load_settings()
            current.execution_runtime_limits = ExecutionRuntimeLimits(max_download_bytes_per_second=value)
            from integrations.configuration import normalize_settings
            current = normalize_settings(current, application.definitions)
            save_settings(current)
            apply_settings(current)
            application.configure()
            return await application.execution_runtime_limits()


# ── Scoped namespace mutation surfaces (DP 1.0.12 canonical architecture ──
# correction, Workstream C, specification section 9.5): each surface reloads
# only its own namespace, preserves every unrelated setting, and never
# replaces the whole settings document from a stale UI snapshot. Neither
# acquires ``configuration_admission()`` -- concurrency/retry policy and
# executor tuning are not application-wide invariants (specification
# sections 2.6, 6).

class TransferPolicyUpdate(BaseModel):
    """Partial update of ``transfer_policy``: every operator-tunable field of
    the canonical namespace, and only through this surface."""
    max_concurrent_executions: int | None = None
    execution_retry_count: int | None = None
    execution_retry_delay_seconds: int | None = None
    resolution_retry_count: int | None = None
    resolution_retry_delay_minutes: int | None = None
    execution_poll_interval_seconds: int | None = None
    provider_poll_interval_seconds: int | None = None
    stalled_timeout_hours: int | None = None


@router.get("/transfer-policy")
async def get_transfer_policy_ep(application: ApplicationService = Depends(get_application)):
    """Universal transfer-policy namespace (specification section 4.1): the
    single canonical authority for execution concurrency, retry, polling and
    stall policy, never aria2-named."""
    policy = get_settings().transfer_policy or TransferSettings()
    return {"ok": True, **policy.model_dump()}


@router.patch("/transfer-policy")
async def patch_transfer_policy(body: TransferPolicyUpdate, application: ApplicationService = Depends(get_application)):
    """Scoped universal transfer-policy mutation. The canonical UI writes only
    this surface for every ``transfer_policy`` field; no flat alias is a second
    writable authority (specification sections 4.1, 9.7, 9.8)."""
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(400, "No valid transfer-policy fields provided")
    async with application.application_operation():
        # The narrow config-write lock (specification sections 9.5, 13.8)
        # serializes this load-modify-save critical section against every
        # other settings-mutation route: no namespace may lose another
        # namespace's newer value to a concurrent read-modify-write.
        async with config_write_lock():
            current = load_settings()
            base = current.transfer_policy or TransferSettings()
            try:
                policy = base.model_copy(update=updates)
                policy = TransferSettings(**policy.model_dump())  # re-validate bounds
            except Exception as exc:
                raise HTTPException(400, _sanitize_error(exc)) from None
            current.transfer_policy = policy
            from integrations.configuration import normalize_settings
            current = normalize_settings(current, application.definitions)
            save_settings(current)
            apply_settings(current)
            # Unconditional (Gate 9 revision-3 rejection finding 4):
            # ``application.configure()`` (``composition.configure``) is the
            # ONE place ``execution_retry_count``/``execution_retry_delay_seconds``
            # become the running universal recovery engine's live
            # ``max_attempts``/``retry_delay`` -- exactly as it is also the
            # one place ``max_concurrent_executions`` becomes the live
            # scheduler capacity. Gating this call on "did concurrency
            # change" left a retry-only PATCH silently persisting one retry
            # policy while the running engine kept consuming the old one
            # until restart or an unrelated reconfigure. Held inside the
            # SAME config-write lock so the engine's live policy always
            # corresponds to the just-persisted revision, never a stale
            # interleaving (specification section 13.8).
            application.configure()

            # ``max_concurrent_executions`` is the one global concurrency
            # policy and core admission (``occupied_execution_slots``) its only
            # enforcement: no executor receives it as a native policy copy.
            last_apply_error = None
        if "max_concurrent_executions" in updates:
            # Only the capacity-triggered dispatch nudge stays conditional:
            # it exists solely to immediately use newly available
            # concurrency slots, which a retry-only change does not create,
            # and is an async dispatch operation that must not run while
            # holding the config-write lock.
            try:
                await application.reconcile_executions()
            except Exception as exc:
                logger.debug("transfer-policy concurrency reconfigure skipped: %s", sanitize_exception(exc))
    return {
        "ok": last_apply_error is None,
        "last_apply_error": last_apply_error,
        **current.transfer_policy.model_dump(),
    }


class IntegrationConfigurationUpdate(BaseModel):
    options: dict = Field(default_factory=dict)
    enabled: bool | None = None
    priority: int | None = None
    clear_secrets: list[str] = Field(default_factory=list)
    # Opaque proofs a successful Test handed back for what it actually tested.
    # NEVER a claim that something is verified: the fingerprint is re-derived
    # server-side from the configuration this request saves, and a proof is
    # accepted only for that. A forged or asserted token matches nothing.
    verification: list[str] = Field(default_factory=list)


def _integration_definition(application: ApplicationService, integration_id: str):
    definition = next((d for d in application.definitions if d.id == integration_id), None)
    if definition is None:
        raise HTTPException(404, "Unknown integration")
    return definition


@router.get("/integrations/{integration_id}/configuration")
async def get_integration_configuration(integration_id: str, application: ApplicationService = Depends(get_application)):
    """Integration/executor-owned configuration namespace (specification
    section 4.3): concrete executor tuning is never read back through
    universal transfer policy."""
    _integration_definition(application, integration_id)
    from integrations.configuration import public_integrations
    public = public_integrations(get_settings(), application.definitions).get(integration_id, {})
    return {"ok": True, **public}


@router.patch("/integrations/{integration_id}/configuration")
async def patch_integration_configuration(
    integration_id: str, body: IntegrationConfigurationUpdate, application: ApplicationService = Depends(get_application),
):
    """Scoped integration/executor configuration mutation (specification
    sections 4.3, 9.1, 9.5). Merges only the supplied option keys into the
    existing namespace -- every unrelated integration and every unrelated
    option is preserved untouched. A proven lifecycle/binding invariant
    (``ApplicationService.validate_configuration`` -- has this integration
    ever been used) is still enforced for an integration's ownership fields;
    it is not exempt merely because this is a scoped route."""
    definition = _integration_definition(application, integration_id)
    from integrations.configuration import accept_verification
    async with application.application_operation():
        # The narrow config-write lock (specification sections 9.5, 13.8)
        # serializes this load-modify-save critical section -- including the
        # ``previous`` baseline read used below -- against every other
        # settings-mutation route.
        async with config_write_lock():
            previous = get_settings()
            current = load_settings()
            existing = current.integrations.get(integration_id)
            existing_options = existing.options if isinstance(existing, IntegrationSettings) else {}
            merged_options = {**existing_options, **body.options}
            try:
                validated_options = definition.options_model(**merged_options).model_dump()
            except Exception as exc:
                raise HTTPException(400, _sanitize_error(exc)) from None
            entry = IntegrationSettings(
                enabled=(existing.enabled if isinstance(existing, IntegrationSettings) and body.enabled is None else bool(body.enabled)),
                priority=(existing.priority if isinstance(existing, IntegrationSettings) and body.priority is None else int(body.priority or 0)),
                options=validated_options,
                clear_secrets=body.clear_secrets,
            )
            current.integrations = {**current.integrations, integration_id: entry}
            from integrations.configuration import normalize_settings
            # ``previous=previous`` (Gate 9 revision-3 rejection finding 2):
            # without it, ``normalize_settings``'s generic secret-preservation
            # branch (``old_options.get(secret)``) has no prior namespace to
            # restore a blank/omitted secret from, so an ordinary Save whose
            # already-configured-secret UI control is intentionally blank
            # (the existing UI contract: blank means "keep current") would
            # erase the stored secret. This is the SAME ``previous`` the
            # whole-settings route already threads through for exactly this
            # reason -- a scoped route is not exempt from it.
            clean = normalize_settings(current, application.definitions, previous=previous)
            # A draft the operator tested before saving it may be verified by
            # the Save that promotes it -- but only after the generic owner has
            # proven the proof describes the configuration just saved.
            clean = accept_verification(clean, definition, body.verification)
            try:
                await application.validate_configuration(previous, clean)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            save_settings(clean)
            apply_settings(clean)
            # Reconfigure and the aria2 lifecycle apply now happen INSIDE
            # the config-write lock (Gate 9 revision-3 rejection finding 7):
            # previously the lock was released before this apply phase, so
            # two concurrent integration-configuration writes could apply
            # their native/lifecycle effects out of order relative to their
            # persisted revisions. Holding the lock across validate -> save
            # -> apply -> reconfigure -> lifecycle makes the last writer
            # under the lock win coherently for the durable revision AND
            # the resulting live/native state together.
            application.configure()
            if integration_id == "aria2":
                await _apply_aria2_settings(application)
            # An integration that owns external configuration applies it here,
            # inside the same lock, through the generic seam. No integration is
            # named: composition discovered which namespaces have appliers.
            applied = await application.apply_integration_configuration(integration_id)
    # A canonical configuration change can alter which sources are routable and
    # whether an integration's managed lifecycle component is still required.
    # Waking the neutral maintenance/resolution signals here is what makes an
    # operator-visible control IMMEDIATE rather than cadence-bound: before this,
    # an integration whose enable state had just changed converged only on the
    # 60 s integration-maintenance tick, so an operator who enabled one saw an
    # unreachable service for up to a minute. Issued AFTER the
    # application-operation block so maintenance is never woken into an
    # admission this request still holds. Neutral: it names no integration and
    # applies to every namespace.
    application.notify_applicability_changed(integration_id)
    from integrations.configuration import public_integrations
    public = public_integrations(clean, application.definitions).get(integration_id, {})
    # A save whose native application failed is reported truthfully: the
    # canonical namespace is saved (it is the desired state) but the operator is
    # never told the service is configured when it is not.
    return {"ok": True, **public, **({"native": applied.public()} if applied is not None else {})}


class IntegrationGroupConfigurationUpdate(BaseModel):
    enabled: bool


@router.get("/integration-groups")
async def list_integration_groups(application: ApplicationService = Depends(get_application)):
    """Every declared integration group and its aggregate participation gate."""
    from integrations.configuration import public_integration_groups
    return {"ok": True, "groups": public_integration_groups(get_settings(), application.definitions)}


@router.patch("/integration-groups/{group_id}/configuration")
async def patch_integration_group_configuration(
    group_id: str, body: IntegrationGroupConfigurationUpdate,
    application: ApplicationService = Depends(get_application),
):
    """Scoped mutation of ONE aggregate participation gate.

    Deliberately the same discipline as ``/integrations/{id}/configuration``
    -- the same admission, the same narrow config-write lock held across
    validate -> save -> apply -> reconfigure, and the same neutral applicability
    wake afterwards -- because it is the same kind of thing: canonical operator
    intent about whether something participates. It is not a second settings
    framework, and it writes no member namespace: a gate gates, and the
    members' own preferences are exactly as they were on both sides of it.
    """
    from integrations.configuration import (
        known_groups, normalize_settings, public_integration_groups, set_group_enabled,
    )
    if group_id not in known_groups(application.definitions):
        raise HTTPException(404, "Unknown integration group")
    async with application.application_operation():
        async with config_write_lock():
            previous = get_settings()
            current = set_group_enabled(load_settings(), group_id, body.enabled,
                                        definitions=application.definitions)
            clean = normalize_settings(current, application.definitions, previous=previous)
            try:
                await application.validate_configuration(previous, clean)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            save_settings(clean)
            apply_settings(clean)
            application.configure()
    # Which sources are routable just changed for every member of this group.
    for member in public_integration_groups(clean, application.definitions)[group_id]["members"]:
        application.notify_applicability_changed(member)
    return {"ok": True, "group_id": group_id,
            **public_integration_groups(clean, application.definitions)[group_id]}


@router.get("/stats/comprehensive")
async def get_comprehensive_stats(hours: int = Query(24, ge=1, le=8760)):
    """Comprehensive stats for a given time window (hours)."""
    from services.stats import collect_all_metrics
    return await collect_all_metrics(hours=hours)


@router.get("/stats/report")
@router.get("/stats/report-data")
async def get_stats_report(hours: int = Query(24, ge=1, le=8760)):
    """Formatted report for a given time window."""
    from services.stats import generate_report
    return await generate_report(hours=hours)


@router.post("/stats/report/send")
async def send_stats_report_ep(hours: int = Query(24, ge=1, le=8760)):
    """Send the current report to the configured reporting webhook."""
    from services.stats import send_stats_report
    return await send_stats_report(hours=hours, triggered_by="manual")


@router.post("/stats/snapshot")
async def trigger_stats_snapshot():
    """Manually trigger a stats snapshot."""
    from services.stats import take_stats_snapshot
    await take_stats_snapshot()
    return {"ok": True, "message": "Snapshot taken"}


@router.get("/stats/snapshots")
async def list_stats_snapshots(limit: int = Query(30, le=100)):
    """Return recent stats snapshots."""
    async with get_db() as db:
        rows = await db.fetchall(
            "SELECT id, created_at FROM stats_snapshots ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    return {"snapshots": public_payload(rows)}


@router.get("/stats/export")
async def export_stats(hours: int = Query(24, ge=1, le=8760)):
    """Export comprehensive stats as JSON."""
    from services.stats import collect_all_metrics
    from fastapi.encoders import jsonable_encoder
    from fastapi.responses import JSONResponse
    data = await collect_all_metrics(hours=hours)
    return JSONResponse(
        content=jsonable_encoder(data),
        headers={"Content-Disposition": f"attachment; filename=stats_{hours}h.json"},
    )


@router.post("/admin/full-sync")
async def trigger_full_sync( application: ApplicationService = Depends(get_application)):
    return {"ok": True, **await application.reconcile_inventory()}


@router.post("/admin/deep-sync")
async def trigger_deep_sync( application: ApplicationService = Depends(get_application)):
    t0 = time.monotonic()
    await application.reconcile_executions()
    return {"ok": True, "elapsed_seconds": round(time.monotonic() - t0, 2)}


# ── Server-Sent Events (SSE) ──────────────────────────────────────────────────
# Lightweight pub/sub: a set of asyncio.Queue instances, one per connected client.
# The backend pushes events when significant state changes occur; the frontend
# listens via EventSource and drops its 15-second polling interval.
#
# Event types:
#   ping          — heartbeat every 30 s (keeps the connection alive through proxies)
#   stats_changed — basic stats object; frontend re-renders stats bar
#   torrent_updated — {id, status, name}; frontend refreshes the affected row
#
# This requires NO external dependencies (no Redis, no WebSocket library).

_sse_subscribers: set[asyncio.Queue] = set()
_sse_lock = asyncio.Lock()


async def _sse_broadcast(event_type: str, data: dict) -> None:
    """Push an SSE event to all connected clients (fire-and-forget)."""
    payload = f"event: {event_type}\ndata: {_json.dumps(data)}\n\n"
    dead: list[asyncio.Queue] = []
    async with _sse_lock:
        for q in _sse_subscribers:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            _sse_subscribers.discard(q)


bind_publisher(_sse_broadcast)


async def _sse_generator(request: Request) -> AsyncGenerator[str, None]:
    """Yield SSE frames until the client disconnects."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    async with _sse_lock:
        _sse_subscribers.add(queue)
    try:
        yield "event: connected\ndata: {}\n\n"
        while True:
            if await request.is_disconnected():
                break
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=30)
                yield frame
            except asyncio.TimeoutError:
                # Send heartbeat so proxies don't close the connection
                yield "event: ping\ndata: {}\n\n"
    finally:
        async with _sse_lock:
            _sse_subscribers.discard(queue)


@router.get("/events/stream")
async def events_stream(request: Request):
    """Server-Sent Events stream for live UI updates.

    Connect via:  const es = new EventSource('/api/events/stream');
    Events:  connected, ping, stats_changed, torrent_updated
    """
    return StreamingResponse(
        _sse_generator(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )


@router.get("/events/subscriber-count")
async def sse_subscriber_count():
    """Diagnostic: how many SSE clients are currently connected."""
    return {"subscribers": len(_sse_subscribers)}


# ── Prometheus metrics ────────────────────────────────────────────────────────


@router.get("/disk-guard")
async def disk_guard_status( application: ApplicationService = Depends(get_application)):
    """
    Current disk-space guard state.

    Returns free_gb, min_free_gb, and whether the guard is active
    (new dispatches currently deferred due to low disk space).
    """
    return await application.check_resources()


@router.get("/metrics")
async def prometheus_metrics():
    """Prometheus-compatible metrics endpoint.

    Scrape with: `- job_name: debridpulse  static_configs: [{targets: [host:8080]}]`
    and set `metrics_path: /api/metrics`.

    Every metric describes universal transfer/scheduler state under the
    ``debridpulse_`` namespace; none is named for a provider or an executor.
    """
    try:
        from prometheus_client import (
            Counter, Gauge, Histogram, CollectorRegistry, generate_latest, CONTENT_TYPE_LATEST,
            REGISTRY,
        )
    except ImportError:
        raise HTTPException(
            503,
            "prometheus-client is not installed. Add it to requirements.txt and rebuild.",
        )

    async with get_db() as db:
        # Torrent counts by status
        rows = await db.fetchall(
            "SELECT status, COUNT(*) AS c FROM torrents GROUP BY status"
        )
        by_status = {r["status"]: int(r["c"]) for r in rows}

        # Download file counts by status
        frows = await db.fetchall(
            "SELECT status, COUNT(*) AS c FROM download_files GROUP BY status"
        )
        by_file_status = {r["status"]: int(r["c"]) for r in frows}

        # Total size downloaded (bytes)
        size_row = await db.fetchone(
            "SELECT COALESCE(SUM(size_bytes),0) AS total FROM torrents WHERE status='completed'"
        )
        total_bytes = int((size_row["total"] if size_row else 0) or 0)

    # Build output manually to avoid global registry side-effects on repeated scrapes
    lines: list[str] = []

    def _gauge(name: str, help_text: str, value: float, labels: dict | None = None) -> None:
        lstr = ""
        if labels:
            lstr = "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}"
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name}{lstr} {value}")

    _gauge("debridpulse_transfers_total",
           "Number of transfers across all statuses",
           sum(by_status.values()))

    for status, count in by_status.items():
        lines.append(f'debridpulse_transfers_by_status{{status="{status}"}} {count}')

    _gauge("debridpulse_active_downloads",
           "Transfers currently in queued or downloading state",
           by_status.get("queued", 0) + by_status.get("downloading", 0))

    _gauge("debridpulse_completed_downloads",
           "Total transfers completed",
           by_status.get("completed", 0))

    _gauge("debridpulse_error_transfers",
           "Transfers in error state",
           by_status.get("error", 0))

    _gauge("debridpulse_pending_files",
           "Download files in pending state (waiting for an available execution slot)",
           by_file_status.get("pending", 0))

    _gauge("debridpulse_sse_subscribers",
           "Number of SSE connections",
           len(_sse_subscribers))

    _gauge("debridpulse_downloaded_bytes_total",
           "Total bytes downloaded (completed transfers)",
           total_bytes)

    return Response(
        content="\n".join(lines) + "\n",
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )

# ── Priority Queue ────────────────────────────────────────────────────────────

@router.patch("/torrents/{torrent_id}/priority")
async def set_torrent_priority(torrent_id: int, body: dict, application: ApplicationService = Depends(get_application)):
    """Set the dispatch priority for a torrent.
    Higher priority = dispatched sooner.  Default: 0.
    Body: {"priority": <int>}
    """
    priority = int(body.get("priority") or 0)
    if not await application.repository.get(torrent_id):
        raise HTTPException(404, "Transfer not found")
    await application.repository.update_metadata(torrent_id, priority=priority)
    await _sse_broadcast("torrent_updated", {"torrent_id": torrent_id, "priority": priority})
    return {"ok": True, "torrent_id": torrent_id, "priority": priority}


# ── Recovery ──────────────────────────────────────────────────────────────────

@router.post("/recovery/run")
async def run_recovery( application: ApplicationService = Depends(get_application)):
    """Manually trigger an auto-recovery pass."""
    result = await application.recover()
    return {"ok": True, "result": result}


# ── AllDebrid orphan cleanup ───────────────────────────────────────────────────

@router.post("/admin/cleanup-alldebrid-orphans")
async def cleanup_alldebrid_orphans_endpoint( application: ApplicationService = Depends(get_application)):
    """Compatibility URL for retrying already-authorized canonical cleanup."""
    async with application.application_operation():
        await application.engine.cleanup_pending()
    return {"ok": True}


# --- Usenet news-server mutation ---------------------------------------------
#
# Per-server Save/Add/Remove against the ONE canonical `integrations.usenet`
# namespace. These are settings MUTATIONS, so they live here beside the other
# scoped configuration writes and run under the same configuration write lock;
# the transient (non-persisting) Test route stays in the validation module.

USENET_NAMESPACE = "usenet"


# DebridPulse's own connection bounds; the floor is 1, not SAB's 0 (see
# integrations/usenet/definition.py).
from integrations.usenet.definition import (
    MAX_ARTICLES_PER_REQUEST, MAX_CONNECTIONS, MAX_SERVER_TIMEOUT_SECONDS,
    MIN_ARTICLES_PER_REQUEST, MIN_CONNECTIONS, MIN_SERVER_TIMEOUT_SECONDS,
)


class UsenetServerUpdate(BaseModel):
    """One server card's own values. Absent fields are left untouched, and a
    blank password keeps the stored one unless ``clear_password`` is set."""
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    ssl: bool | None = None
    username: str | None = None
    password: str | None = None
    connections: int | None = Field(default=None, ge=MIN_CONNECTIONS, le=MAX_CONNECTIONS)
    priority: int | None = Field(default=None, ge=0, le=99)
    articles_per_request: int | None = Field(default=None, ge=MIN_ARTICLES_PER_REQUEST,
                                             le=MAX_ARTICLES_PER_REQUEST)
    timeout_seconds: int | None = Field(default=None, ge=MIN_SERVER_TIMEOUT_SECONDS,
                                        le=MAX_SERVER_TIMEOUT_SECONDS)
    enabled: bool | None = None
    display_name: str | None = None
    clear_password: bool = False
    # See ``IntegrationConfigurationUpdate.verification``: the same opaque
    # Test -> Save carriage, through the same generic acceptance owner.
    verification: list[str] = Field(default_factory=list)

    def values(self) -> dict:
        return self.model_dump(exclude_none=True,
                               exclude={"clear_password", "verification"})


async def _mutate_usenet_servers(application: ApplicationService, mutate, verification=()):
    """Apply one per-server mutation to the ONE canonical namespace.

    Load -> mutate -> validate -> save -> reconfigure -> apply natively, all
    inside the existing configuration write lock, exactly like every other
    settings mutation. ``mutate`` receives the current options and returns
    ``(new_options, payload)``.
    """
    from core.config import config_write_lock, get_settings, load_settings, save_settings, apply_settings
    from integrations.configuration import accept_verification, normalize_settings, public_integrations
    from integrations.definition import IntegrationSettings
    from integrations.usenet.definition import UsenetOptions
    from integrations.usenet.servers import ServerMutationError

    async with application.application_operation():
        async with config_write_lock():
            previous = get_settings()
            current = load_settings()
            entry = current.integrations.get(USENET_NAMESPACE)
            stored = entry.options if isinstance(entry, IntegrationSettings) else {}
            try:
                updated, payload = mutate(UsenetOptions(**(stored or {})))
            except ServerMutationError as exc:
                raise HTTPException(404, "Unknown Usenet server") from None
            except ValueError as exc:
                raise HTTPException(400, _sanitize_error(exc)) from None
            current.integrations = {**current.integrations, USENET_NAMESPACE: IntegrationSettings(
                enabled=(entry.enabled if isinstance(entry, IntegrationSettings) else False),
                priority=(entry.priority if isinstance(entry, IntegrationSettings) else 0),
                options=updated.model_dump(),
            )}
            clean = normalize_settings(current, application.definitions, previous=previous)
            clean = accept_verification(
                clean, _integration_definition(application, USENET_NAMESPACE), verification)
            # The SAME canonical ownership fence every other settings mutation
            # passes through: a server change that could abandon owned native
            # work is refused here, not re-implemented in Usenet code.
            try:
                await application.validate_configuration(previous, clean)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            save_settings(clean)
            apply_settings(clean)
            application.configure()
            applied = await application.apply_integration_configuration(USENET_NAMESPACE)
    public = public_integrations(clean, application.definitions).get(USENET_NAMESPACE, {})
    # The accepted canonical public projection of this integration, published
    # once. A server write can change the provider's derived ``configured`` and
    # ``verified`` state, and this module is not the only owner of how that is
    # presented -- so the response states the identity and the projection, and
    # the neutral acceptance seam carries them to whoever else renders them.
    # ``servers`` remains the same projection's own server list, not a second
    # copy of it.
    return {"ok": True, **payload, "integration_id": USENET_NAMESPACE, "integration": public,
            "servers": public.get("options", {}).get("servers", []),
            **({"native": applied.public()} if applied is not None else {})}


@router.post("/usenet/servers")
async def create_usenet_server(payload: UsenetServerUpdate,
                               application: ApplicationService = Depends(get_application)):
    """Add one news server. Every existing server is left byte-identical."""
    from integrations.usenet.servers import create_server

    def mutate(options):
        updated, created = create_server(options, payload.values(),
                                         clear_password=payload.clear_password)
        return updated, {"server_id": created.id}

    return await _mutate_usenet_servers(application, mutate, payload.verification)


@router.put("/usenet/servers/{server_id}")
async def update_usenet_server(server_id: str, payload: UsenetServerUpdate,
                               application: ApplicationService = Depends(get_application)):
    """Save exactly one server card.

    Only this record changes: unsaved edits on other cards are never persisted,
    and a blank password keeps this server's stored credential unless the
    operator explicitly cleared it.
    """
    from integrations.usenet.servers import merge_server

    def mutate(options):
        return (merge_server(options, server_id, payload.values(),
                             clear_password=payload.clear_password),
                {"server_id": server_id})

    return await _mutate_usenet_servers(application, mutate, payload.verification)


@router.delete("/usenet/servers/{server_id}")
async def delete_usenet_server(server_id: str,
                               application: ApplicationService = Depends(get_application)):
    """Remove exactly one server, preserving every survivor's credential."""
    from integrations.usenet.servers import remove_server

    def mutate(options):
        return remove_server(options, server_id), {"server_id": server_id, "removed": True}

    return await _mutate_usenet_servers(application, mutate)
