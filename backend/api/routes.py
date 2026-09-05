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
from typing import Optional, AsyncGenerator, Literal
from urllib.parse import urlparse

from fastapi import Depends, APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse, Response
from pydantic import BaseModel, Field

from core.branding import APP_SHORT_NAME, REPOSITORY_API_URL
from core.config import (
    AppSettings,
    apply_settings,
    get_settings,
    load_settings,
    save_settings,
)
from core.config_validator import validate_and_sanitise
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

from application.dependencies import get_application
from transfers import codec
from application.service import ApplicationService
from executors.aria2.runtime import runtime as aria2_runtime
from services.event_bus import bind_publisher
from services.notification_service import NotificationService
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
_SECRET_SETTINGS = {
    "alldebrid_api_key", "aria2_secret", "discord_webhook_url",
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
    from integrations.configuration import public_integrations
    data["integrations"] = public_integrations(settings, definitions)
    for field in _SECRET_SETTINGS:
        if field in data:
            data[f"{field}_configured"] = bool(str(data.get(field) or "").strip())
            data[field] = ""
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
        if field in requested_clears:
            merged[field] = ""
        elif not str(merged.get(field) or "").strip():
            merged[field] = getattr(previous, field, "")
    return merged


@router.put("/settings")
async def update_settings(new: SettingsUpdate, application: ApplicationService = Depends(get_application)):
    async with application.configuration_admission():
        previous = get_settings()
        merged = _merge_secret_settings(new, previous)
        definitions = application.definitions
        from integrations.configuration import normalize_settings
        merged["integrations"] = new.integrations
        clean = normalize_settings(AppSettings(**merged), definitions, previous=previous,
            supplied_fields=new.model_fields_set, clear_legacy_secrets=new.clear_secrets)
        clean = validate_and_sanitise(clean)
        if getattr(clean, "max_concurrent_downloads", None) is not None:
            clean = clean.model_copy(update={"aria2_max_active_downloads": clean.max_concurrent_downloads})
        try:
            await application.validate_configuration(previous, clean)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        save_settings(clean)
        apply_settings(clean)
        _revoke_stale_authentication_state(previous, clean)
        application.configure()
        if getattr(clean, "aria2_mode", "external") == "builtin":
            if (getattr(previous, "aria2_mode", "external") == "builtin"
                    and getattr(previous, "aria2_builtin_port", 6800) != getattr(clean, "aria2_builtin_port", 6800)):
                await aria2_runtime.restart()
            else:
                await aria2_runtime.ensure_started()
            try:
                await application.integration_admin("aria2").apply_memory_tuning()
            except Exception as exc:
                logger.warning("Could not apply aria2 memory settings immediately: %s", sanitize_exception(exc))
        elif getattr(previous, "aria2_mode", "external") == "builtin":
            await aria2_runtime.stop()
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

@router.post("/settings/test-discord")
async def test_discord():
    cfg = get_settings()
    if not cfg.discord_webhook_url:
        raise HTTPException(400, "No Discord webhook configured")
    from services.notifications import NotificationService
    svc = NotificationService(cfg.discord_webhook_url)
    ok = await svc.test()
    if not ok:
        raise HTTPException(502, "Discord test failed — check webhook URL")
    return {"ok": True}


@router.post("/settings/test-alldebrid")
async def test_alldebrid():
    from providers.alldebrid.admin import account_status
    cfg = get_settings()
    if not cfg.alldebrid_api_key:
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
    """Return ownership-safe live counters used by the topbar indicator."""
    cfg = get_settings()
    external = getattr(cfg, "aria2_mode", "external") != "builtin"

    if not external:
        return {
            "ok": True,
            "mode": "builtin",
            "external_control": False,
            **await application.integration_admin("aria2").get_global_stat(),
        }

    # External aria2 may be shared with unrelated applications. Observe only
    # jobs whose GIDs DebridPulse has recorded as its own.
    active_downloads = await application.integration_admin("aria2").get_active()
    owned_active = await application.integration_admin("aria2").filter_owned(active_downloads)

    return {
        "ok": True,
        "mode": "external",
        "external_control": True,
        "download_speed": sum(
            int(getattr(download, "download_speed", 0) or 0)
            for download in owned_active
        ),
        "upload_speed": 0,
        "active": len(owned_active),
        "waiting": 0,
    }


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
    cfg = get_settings()
    try:
        downloads = await application.integration_admin("aria2").get_all(
            getattr(cfg, "aria2_waiting_window", 100),
            getattr(cfg, "aria2_stopped_window", 100),
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

@router.get("/torrents")
async def list_torrents(
    status: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(0, ge=0, le=5000),
    offset: int = 0, application: ApplicationService = Depends(get_application)):
    async with get_db() as db:
        clauses = []
        params = []

        if status:
            clauses.append("t.status = ?")
            params.append(status)
        else:
            # Deletion is intentionally a soft delete so the torrent hash and
            # prior ownership state remain available for duplicate detection
            # and controlled revival.  Soft-deleted rows must not remain in
            # the normal "All Downloads" view, however.
            clauses.append("t.status != 'deleted'")

        if search:
            clauses.append(
                """(
                    LOWER(COALESCE(t.name, '')) LIKE ?
                    OR LOWER(COALESCE(t.hash, '')) LIKE ?
                    OR LOWER(COALESCE(t.source, '')) LIKE ?
                    OR LOWER(COALESCE(t.label, '')) LIKE ?
                    OR LOWER(COALESCE(t.error_message, '')) LIKE ?
                )"""
            )
            needle = f"%{search.strip().lower()}%"
            params.extend([needle, needle, needle, needle, needle])

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"""SELECT t.*,
                (SELECT COUNT(*) FROM download_files WHERE torrent_id=t.id) as file_count,
                (SELECT COUNT(*) FROM download_files WHERE torrent_id=t.id AND blocked=1) as blocked_count
                FROM torrents t {where}
                ORDER BY t.created_at DESC"""
        query_params = list(params)
        if limit > 0:
            query += " LIMIT ? OFFSET ?"
            query_params.extend([limit, offset])

        rows = await db.fetchall(query, query_params)
        total_row = await db.fetchone(
            f"SELECT COUNT(*) AS cnt FROM torrents t {where}", params
        )
        total = total_row["cnt"] if total_row else 0
        return {"items": [_public_transfer_presentation(
            await application.repository.presentation(row["id"]), application.definitions
        ) for row in rows], "total": total}


@router.post("/torrents/add-magnet")
async def add_magnet(body: dict, application: ApplicationService = Depends(get_application)):
    magnet = (body.get("magnet") or "").strip()
    if not magnet:
        raise HTTPException(400, "magnet is required")
    try:
        row = await application.submit_magnet(magnet, source="manual")
        return public_payload(row)
    except ValueError as exc:
        raise HTTPException(400, _sanitize_error(exc))
    except Exception as exc:
        logger.exception("add_magnet failed: %s", _sanitize_error(exc))
        raise HTTPException(502, _sanitize_error(exc))


@router.post("/torrents/add-file")
async def add_torrent_file(file: UploadFile = File(...), application: ApplicationService = Depends(get_application)):
    """Upload a .torrent metafile directly to AllDebrid.

    The local aria2 daemon never receives the torrent metafile.  AllDebrid
    processes it and ADC later dispatches only the unlocked HTTPS file URLs.
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
        )
        return public_payload(result)
    except ValueError as exc:
        raise HTTPException(400, _sanitize_error(exc))
    except Exception as exc:
        raise HTTPException(502, _sanitize_error(exc))


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
    item = await application.repository.presentation(torrent_id, details=True)
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
        return {"ok": True, "paused": bool(get_settings().paused)}
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

@router.get("/events")
async def get_events(limit: int = Query(200, le=500)):
    async with get_db() as db:
        rows = await db.fetchall(
            """SELECT e.*, t.name AS torrent_name
               FROM events e
               LEFT JOIN torrents t ON t.id = e.torrent_id
               ORDER BY e.created_at DESC LIMIT ?""",
            (limit,),
        )
    return public_payload(rows)


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
async def get_stats():
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
        "paused": bool(get_settings().paused),
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
    return {"ok": True, "paused": bool(get_settings().paused), **result}

@router.post("/processing/resume")
async def resume_processing( application: ApplicationService = Depends(get_application)):
    result = await application.resume_all()
    return {"ok": True, "paused": bool(get_settings().paused), **result}

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
    if not getattr(cfg, "paused", False):
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
                if not getattr(cfg, "paused", False):
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
        cfg = get_settings()
        external = getattr(cfg, "aria2_mode", "external") != "builtin"
        opts = await application.integration_admin("aria2").get_global_options()
        return {
            "ok": True,
            "mode": "external" if external else "builtin",
            "global_options_read_only": external,
            "max_download_speed": int(opts.get("max-overall-download-limit") or 0),
            "max_upload_speed":   int(opts.get("max-overall-upload-limit")   or 0),
            "max_concurrent_downloads": (
                int(getattr(cfg, "max_concurrent_downloads", 1) or 1)
                if external
                else int(opts.get("max-concurrent-downloads") or 0)
            ),
            "raw": {k: v for k, v in opts.items() if "limit" in k or "speed" in k or "concurrent" in k},
        }
    except Exception as e:
        raise HTTPException(502, _sanitize_error(e))


@router.post("/aria2/global-options")
async def aria2_set_global_options(body: dict, application: ApplicationService = Depends(get_application)):
    """
    Apply global aria2 options at runtime.
    Accepts: max_download_speed (bytes/s, 0=unlimited), max_upload_speed.
    """
    async with application.configuration_admission():
        cfg = get_settings()
        external = getattr(cfg, "aria2_mode", "external") != "builtin"
        options: dict = {}
        cfg_updates: dict = {}
        if "max_download_speed" in body:
            val = int(body["max_download_speed"])
            options["max-overall-download-limit"] = str(val)
            cfg_updates["aria2_max_download_limit"] = val
        if "max_upload_speed" in body:
            val = int(body["max_upload_speed"])
            options["max-overall-upload-limit"] = str(val)
            cfg_updates["aria2_max_upload_limit"] = val
        if "max_concurrent_downloads" in body:
            val = max(1, int(body["max_concurrent_downloads"]))
            if not external:
                options["max-concurrent-downloads"] = str(val)
            # Keep the legacy input and universal execution limit consistent.
            cfg_updates["aria2_max_active_downloads"] = val
            cfg_updates["max_concurrent_downloads"] = val
        if external and any(
            key in body for key in ("max_download_speed", "max_upload_speed")
        ):
            raise HTTPException(
                409,
                "Global bandwidth limits are read-only for an external shared aria2 daemon",
            )
        if not options and not cfg_updates:
            raise HTTPException(400, "No valid options provided")
        try:
            if not external:
                await application.integration_admin("aria2").change_global_options(options)
            # Persist so the limits survive an aria2 restart
            if cfg_updates:
                current = load_settings()
                for k, v in cfg_updates.items():
                    setattr(current, k, v)
                from integrations.configuration import normalize_settings
                current = normalize_settings(current, application.definitions, supplied_fields=set(cfg_updates))
                save_settings(current)
                apply_settings(current)
            # Reconfigure only after active application operations have drained.
            if "max_concurrent_downloads" in cfg_updates:
                application.configure()
                try:
                    await application.reconcile_executions()
                except Exception as exc:
                    logger.debug("aria2 quick slot dispatch skipped: %s", sanitize_exception(exc))
            return {
                "ok": True,
                "mode": "external" if external else "builtin",
                "applied": (
                    {"adc-max-concurrent-downloads": cfg_updates["max_concurrent_downloads"]}
                    if external
                    else options
                ),
            }
        except Exception as e:
            raise HTTPException(502, _sanitize_error(e))





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

    Scrape with: `- job_name: alldebrid  static_configs: [{targets: [host:8080]}]`
    and set `metrics_path: /api/metrics`.
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

    _gauge("alldebrid_torrents_total",
           "Number of torrents by status",
           sum(by_status.values()))

    for status, count in by_status.items():
        lines.append(f'alldebrid_torrents_by_status{{status="{status}"}} {count}')

    _gauge("alldebrid_active_downloads",
           "Torrents currently in queued or downloading state",
           by_status.get("queued", 0) + by_status.get("downloading", 0))

    _gauge("alldebrid_completed_downloads",
           "Total torrents completed",
           by_status.get("completed", 0))

    _gauge("alldebrid_error_torrents",
           "Torrents in error state",
           by_status.get("error", 0))

    _gauge("alldebrid_pending_files",
           "download_files rows in pending state (waiting for aria2 slot)",
           by_file_status.get("pending", 0))

    _gauge("alldebrid_sse_subscribers",
           "Number of SSE connections",
           len(_sse_subscribers))

    _gauge("alldebrid_downloaded_bytes_total",
           "Total bytes downloaded (completed torrents)",
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
