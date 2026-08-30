from __future__ import annotations

import asyncio
import logging
import os
import secrets
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from api import auth_routes as interactive_routes
from auth.api_tokens import api_token_store
from auth.csrf import clear_login_csrf_cookie
from auth.manager import peer_key
from auth.models import AuthMechanism, Principal
from auth.oidc import (
    OIDC_CORRELATION_COOKIE,
    OIDC_TRANSACTION_TTL_SECONDS,
    OidcConfigurationError,
    OidcError,
    begin_oidc_login,
    complete_oidc_login,
    discover_oidc,
    oidc_auth_ready,
    oidc_callback_url,
    oidc_configuration,
    oidc_transaction_store,
)
from auth.oidc_verification import oidc_verification_store
from auth.oidc_version import (
    authentication_configuration_baseline_version,
    oidc_configuration_version,
)
from auth.passwords import basic_verification_cache, is_usable_password_hash
from auth.pending_oidc import commit_verified_pending_oidc, pending_oidc_store
from auth.policy import (
    interactive_auth_enabled,
    normalized_origin,
    oidc_auth_enabled,
    password_auth_enabled,
    password_auth_ready,
    safe_return_path,
)
from auth.sessions import session_cookie_token, session_store, set_session_cookie
from auth.throttle import oidc_verify_rate_limiter
from auth.transitions import authentication_configuration_lock, oidc_critical_change
from core.config import apply_settings, get_settings, save_settings
from core.config_validator import validate_and_sanitise


router = APIRouter()
logger = logging.getLogger("alldebrid.auth")


class OidcVerificationRequest(BaseModel):
    """Proposed OIDC settings staged only until a real login proves them."""

    auth_password_enabled: bool | None = None
    oidc_provider_name: str | None = None
    oidc_issuer_url: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = Field(default=None, max_length=8192)
    clear_oidc_client_secret: bool = False
    oidc_scopes: list[str] | None = None
    oidc_allow_all: bool | None = None
    oidc_allowed_subjects: list[str] | None = None
    oidc_allowed_emails: list[str] | None = None
    oidc_allowed_groups: list[str] | None = None
    oidc_group_claim: str | None = None
    public_base_url: str | None = None
    return_to: str = "/settings"


class AuthenticationConfigUpdate(BaseModel):
    auth_password_enabled: bool | None = None
    auth_username: str | None = Field(default=None, max_length=256)
    auth_password: str | None = Field(default=None, max_length=4096)
    clear_password: bool = False
    auth_session_lifetime_hours: int | None = Field(default=None, ge=1, le=168)

    auth_oidc_enabled: bool | None = None
    oidc_provider_name: str | None = Field(default=None, max_length=256)
    oidc_issuer_url: str | None = Field(default=None, max_length=2048)
    oidc_client_id: str | None = Field(default=None, max_length=2048)
    oidc_client_secret: str | None = Field(default=None, max_length=8192)
    clear_oidc_client_secret: bool = False
    oidc_scopes: list[str] | None = None
    oidc_allow_all: bool | None = None
    oidc_allowed_subjects: list[str] | None = None
    oidc_allowed_emails: list[str] | None = None
    oidc_allowed_groups: list[str] | None = None
    oidc_group_claim: str | None = Field(default=None, max_length=256)
    public_base_url: str | None = Field(default=None, max_length=2048)
    confirm_open_mode: bool = False


class ApiTokenEnableRequest(BaseModel):
    enabled: bool


def _valid_public_base_url(value: str) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return True
    identity = normalized_origin(raw)
    return bool(identity is not None and identity[0] == "https")


def _authentication_mode(cfg) -> str:
    password = password_auth_enabled(cfg)
    oidc = oidc_auth_enabled(cfg)
    if password and oidc:
        return "Username & Password + OIDC"
    if password:
        return "Username & Password"
    if oidc:
        return "OIDC"
    return "No authentication"


def _local_oidc_state(cfg) -> tuple[bool, str]:
    candidate = cfg.model_copy(update={"auth_oidc_enabled": True}, deep=True)
    try:
        oidc_configuration(candidate)
        return True, oidc_callback_url(candidate)
    except OidcError:
        return False, ""


def _oidc_configuration_version_for_status(cfg) -> str:
    """Fingerprint configured OIDC even while its enable toggle is off."""
    candidate = cfg.model_copy(update={"auth_oidc_enabled": True}, deep=True)
    return oidc_configuration_version(candidate)


async def _oidc_runtime_available(cfg, configured: bool) -> bool | None:
    """Probe live provider discovery outside the configuration read/write path."""
    if not oidc_auth_enabled(cfg) or not configured:
        return None
    try:
        config = oidc_configuration(cfg)
        await asyncio.wait_for(discover_oidc(config), timeout=3.0)
        return True
    except (OidcError, TimeoutError):
        return False


async def _authentication_payload(request: Request, *, cfg=None) -> dict:
    """Build the authentication configuration/status payload from local state only.

    This function intentionally performs no provider discovery or other network
    I/O. GET/PUT /api/auth/config are configuration boundaries and must remain
    available even when the configured identity provider is offline or deleted.
    Live OIDC reachability is exposed separately by the runtime-status endpoint.
    """
    cfg = cfg if cfg is not None else get_settings()
    oidc_configured, callback_url = _local_oidc_state(cfg)
    oidc_version = _oidc_configuration_version_for_status(cfg) if oidc_configured else ""
    oidc_verification = oidc_verification_store.status(oidc_version)
    principal = getattr(request.state, "principal", Principal.anonymous())
    configured_public_base = str(getattr(cfg, "public_base_url", "") or "").strip()
    env_public_base = str(os.getenv("PUBLIC_BASE_URL", "") or "").strip()
    effective_public_base = env_public_base or configured_public_base
    return {
        "mode": _authentication_mode(cfg),
        "authentication_required": interactive_auth_enabled(cfg),
        "password_enabled": password_auth_enabled(cfg),
        "password_ready": password_auth_ready(cfg),
        "password_configured": bool(str(getattr(cfg, "auth_password_hash", "") or "").strip()),
        "username": str(getattr(cfg, "auth_username", "") or ""),
        "session_lifetime_hours": int(getattr(cfg, "auth_session_lifetime_hours", 12) or 12),
        "oidc_enabled": oidc_auth_enabled(cfg),
        "oidc_configured": oidc_configured,
        "oidc_ready": oidc_auth_ready(cfg) if oidc_auth_enabled(cfg) else False,
        "oidc_available": None,
        "oidc_verified": oidc_verification.verified,
        "oidc_verified_at": oidc_verification.verified_at,
        "oidc_provider_name": str(getattr(cfg, "oidc_provider_name", "") or "OpenID Connect"),
        "oidc_issuer_url": str(getattr(cfg, "oidc_issuer_url", "") or ""),
        "oidc_client_id": str(getattr(cfg, "oidc_client_id", "") or ""),
        "oidc_client_secret_configured": bool(str(getattr(cfg, "oidc_client_secret", "") or "")),
        "oidc_scopes": list(getattr(cfg, "oidc_scopes", []) or []),
        "oidc_allow_all": bool(getattr(cfg, "oidc_allow_all", False)),
        "oidc_allowed_subjects": list(getattr(cfg, "oidc_allowed_subjects", []) or []),
        "oidc_allowed_emails": list(getattr(cfg, "oidc_allowed_emails", []) or []),
        "oidc_allowed_groups": list(getattr(cfg, "oidc_allowed_groups", []) or []),
        "oidc_group_claim": str(getattr(cfg, "oidc_group_claim", "groups") or "groups"),
        "public_base_url": configured_public_base,
        "public_base_url_effective": effective_public_base,
        "public_base_url_env_override": bool(env_public_base),
        "oidc_callback_url": callback_url,
        "api_token_enabled": api_token_store.enabled,
        "api_token_configured": api_token_store.configured,
        "current_session_mechanism": principal.mechanism.value if principal.mechanism else None,
        "session_count": session_store.size,
    }


def _build_proposed_settings(request: OidcVerificationRequest, *, current=None):
    current = current if current is not None else get_settings()
    updates: dict[str, object] = {"auth_oidc_enabled": True}
    ordinary_fields = (
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
    for field in ordinary_fields:
        value = getattr(request, field)
        if value is not None:
            updates[field] = value

    if request.auth_password_enabled is not None:
        updates["auth_password_enabled"] = request.auth_password_enabled

    if request.clear_oidc_client_secret:
        updates["oidc_client_secret"] = ""
        updates["oidc_client_secret_clear"] = True
    elif request.oidc_client_secret is not None and request.oidc_client_secret.strip():
        updates["oidc_client_secret"] = request.oidc_client_secret
        updates["oidc_client_secret_clear"] = False
    else:
        updates["oidc_client_secret"] = str(getattr(current, "oidc_client_secret", "") or "")
        updates["oidc_client_secret_clear"] = False

    return current.model_copy(update=updates, deep=True)


def _build_authentication_update(update: AuthenticationConfigUpdate):
    current = get_settings()
    changes: dict[str, object] = {}
    ordinary_fields = (
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
    for field in ordinary_fields:
        value = getattr(update, field)
        if value is not None:
            changes[field] = value

    password = str(update.auth_password or "")
    if update.clear_password:
        changes["auth_password"] = ""
        changes["auth_password_hash_clear"] = True
    elif password:
        changes["auth_password"] = password
        changes["auth_password_hash_clear"] = False

    secret = str(update.oidc_client_secret or "")
    if update.clear_oidc_client_secret:
        changes["oidc_client_secret"] = ""
        changes["oidc_client_secret_clear"] = True
    elif secret:
        changes["oidc_client_secret"] = secret
        changes["oidc_client_secret_clear"] = False

    return current.model_copy(update=changes, deep=True)


def _prospective_password_ready(candidate, update: AuthenticationConfigUpdate) -> bool:
    if not bool(getattr(candidate, "auth_password_enabled", False)):
        return False
    username = str(getattr(candidate, "auth_username", "") or "").strip()
    if not username:
        return False
    if update.clear_password:
        # Clear intent wins over simultaneous plaintext in _build_authentication_update
        # and save_settings(), so it cannot be counted as a replacement credential.
        return False
    return bool(
        str(update.auth_password or "")
        or is_usable_password_hash(getattr(candidate, "auth_password_hash", ""))
    )


def _set_pending_correlation_cookie(response: JSONResponse, correlation: str) -> None:
    response.set_cookie(
        key=OIDC_CORRELATION_COOKIE,
        value=str(correlation),
        max_age=OIDC_TRANSACTION_TTL_SECONDS,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def _clear_pending_correlation_cookie(response) -> None:
    response.delete_cookie(
        key=OIDC_CORRELATION_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def _oidc_verification_failure_page(*, status_code: int) -> HTMLResponse:
    """Return a popup-safe failure result without navigating the operator SPA."""
    body = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OIDC verification · DebridPulse</title>
<style>
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:#090812;color:#f4f1ff;font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
main { width:min(420px,100%);padding:28px;border:1px solid #302c49;border-radius:12px;background:#171526;text-align:center; }
h1 { margin:0 0 10px;font-size:19px; }
p { margin:0;color:#c2bdd6;font-size:13px;line-height:1.55; }
.status { color:#ff6b6b;font-weight:700; }
</style>
</head>
<body>
<main>
  <h1>OIDC verification failed</h1>
  <p class="status">Provider sign-in or authorization did not complete successfully.</p>
  <p>This window should close automatically.</p>
</main>
<script>
(() => {
  'use strict';
  const payload = {
    type: 'debridpulse-oidc-verification',
    ok: false,
    message: 'OIDC verification failed — provider sign-in or authorization did not complete successfully.'
  };
  try {
    if ('BroadcastChannel' in window) {
      const channel = new BroadcastChannel('debridpulse-oidc-verification');
      channel.postMessage(payload);
      window.setTimeout(() => channel.close(), 250);
    }
  } catch (_) {}
  try {
    if (window.opener && !window.opener.closed) {
      window.opener.postMessage(payload, window.location.origin);
    }
  } catch (_) {}
  window.setTimeout(() => window.close(), 180);
})();
</script>
</body>
</html>"""
    response = HTMLResponse(content=body, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    return response


@router.get("/api/auth/config")
async def get_authentication_config(request: Request):
    response = JSONResponse(await _authentication_payload(request))
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/api/auth/oidc/runtime-status")
async def get_oidc_runtime_status():
    """Return live provider reachability without blocking configuration access."""
    cfg = get_settings()
    oidc_configured, _callback_url = _local_oidc_state(cfg)
    response = JSONResponse(
        {
            "oidc_enabled": oidc_auth_enabled(cfg),
            "oidc_configured": oidc_configured,
            "oidc_available": await _oidc_runtime_available(cfg, oidc_configured),
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.put("/api/auth/config")
async def update_authentication_config(request: Request, update: AuthenticationConfigUpdate):
    current = get_settings()
    candidate = _build_authentication_update(update)

    if update.public_base_url is not None and not _valid_public_base_url(update.public_base_url):
        return JSONResponse(
            {"detail": "External Base URL must be an HTTPS origin in the form https://host[:port]"},
            status_code=400,
        )

    if bool(getattr(candidate, "auth_password_enabled", False)) and not _prospective_password_ready(candidate, update):
        return JSONResponse(
            {"detail": "Username & Password cannot be enabled until a username and stored or new password are configured"},
            status_code=400,
        )

    if bool(getattr(candidate, "auth_oidc_enabled", False)):
        try:
            oidc_configuration(candidate)
        except OidcConfigurationError:
            return JSONResponse(
                {"detail": "OpenID Connect cannot be enabled until its local configuration is complete"},
                status_code=400,
            )

    clean = validate_and_sanitise(candidate)
    password_changed = bool(
        update.clear_password
        or str(update.auth_password or "")
        or (update.auth_username is not None and str(update.auth_username) != str(getattr(current, "auth_username", "")))
        or (
            update.auth_password_enabled is not None
            and bool(update.auth_password_enabled) != password_auth_enabled(current)
        )
    )
    payload = update.model_dump(exclude_none=True)
    critical_oidc_changed = oidc_critical_change(payload, current)
    oidc_disabled = bool(
        update.auth_oidc_enabled is False and oidc_auth_enabled(current)
    )

    save_settings(clean)
    apply_settings(clean)

    if password_changed:
        basic_verification_cache.clear()
        session_store.revoke_mechanism(AuthMechanism.PASSWORD_SESSION)
    if critical_oidc_changed or oidc_disabled:
        session_store.revoke_mechanism(AuthMechanism.OIDC_SESSION)

    logger.info(
        "Authentication configuration updated: password=%s oidc=%s",
        "enabled" if password_auth_enabled(clean) else "disabled",
        "enabled" if oidc_auth_enabled(clean) else "disabled",
    )
    response = JSONResponse({"ok": True, **(await _authentication_payload(request, cfg=clean))})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/api/auth/api-token")
async def api_token_status():
    response = JSONResponse(api_token_store.status())
    response.headers["Cache-Control"] = "no-store"
    return response


@router.put("/api/auth/api-token")
async def set_api_token_enabled(update: ApiTokenEnableRequest):
    try:
        api_token_store.set_enabled(update.enabled)
    except ValueError:
        return JSONResponse(
            {"detail": "Generate an API token before enabling bearer authentication"},
            status_code=409,
            headers={"Cache-Control": "no-store"},
        )
    logger.info("API token authentication %s", "enabled" if update.enabled else "disabled")
    payload = {"ok": True, **api_token_store.status()}
    response = JSONResponse(payload)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/api/auth/api-token")
async def generate_or_rotate_api_token():
    rotated = api_token_store.configured
    token = api_token_store.generate()
    logger.info("API token %s", "rotated" if rotated else "generated")
    response = JSONResponse(
        {
            "ok": True,
            "enabled": True,
            "configured": True,
            "rotated": rotated,
            "token": token,
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.delete("/api/auth/api-token")
async def clear_api_token():
    api_token_store.clear()
    logger.info("API token cleared")
    response = JSONResponse({"ok": True, "enabled": False, "configured": False})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/api/auth/oidc/verify-config")
async def verify_pending_oidc_configuration(
    request: Request,
    proposed: OidcVerificationRequest,
):
    """Stage proposed OIDC settings and require a complete provider login."""
    principal = getattr(request.state, "principal", None)
    if principal is None or not getattr(principal, "authenticated", False):
        return JSONResponse({"detail": "Authentication required"}, status_code=401)
    if not oidc_verify_rate_limiter.allow(peer_key(request)):
        return JSONResponse(
            {"detail": "Too many OpenID Connect configuration verifications have been started"},
            status_code=429,
            headers={"Retry-After": "60", "Cache-Control": "no-store"},
        )

    current = get_settings()
    baseline_version = authentication_configuration_baseline_version(current)
    candidate = _build_proposed_settings(proposed, current=current)
    return_to = safe_return_path(proposed.return_to, default="/settings")
    try:
        authorization_url, correlation = await begin_oidc_login(
            candidate,
            return_to=return_to,
        )
        version = oidc_configuration_version(candidate)
        if not version:
            raise ValueError("OIDC configuration is not usable")
        state_values = parse_qs(urlsplit(authorization_url).query).get("state", [])
        state = str(state_values[0]) if state_values else ""
        if not state:
            raise ValueError("OIDC authorization state is missing")
    except (OidcError, ValueError):
        return JSONResponse(
            {"detail": "Proposed OpenID Connect configuration could not start a verification login"},
            status_code=400,
        )

    pending_oidc_store.stage(
        state,
        candidate,
        configuration_version=version,
        baseline_configuration_version=baseline_version,
        apply_password_enabled=proposed.auth_password_enabled is not None,
    )
    response = JSONResponse(
        {
            "ok": True,
            "authorization_url": authorization_url,
            "callback_url": oidc_callback_url(candidate),
            "pending": True,
        }
    )
    response.headers["Cache-Control"] = "no-store"
    _set_pending_correlation_cookie(response, correlation)
    return response


@router.get("/auth/oidc/callback", include_in_schema=False)
async def pending_aware_oidc_callback(
    request: Request,
    state: str = "",
    code: str = "",
    error: str = "",
):
    """Commit pending settings only after the matching complete OIDC flow passes."""
    if not pending_oidc_store.has(state):
        return await interactive_routes.oidc_callback(
            request,
            state=state,
            code=code,
            error=error,
        )

    correlation = str(request.cookies.get(OIDC_CORRELATION_COOKIE, "") or "")
    if error:
        pending_oidc_store.discard(state)
        oidc_transaction_store.consume(state, correlation)
        response = _oidc_verification_failure_page(status_code=401)
        _clear_pending_correlation_cookie(response)
        return response

    try:
        principal, return_to = await complete_oidc_login(
            state=state,
            code=code,
            correlation=correlation,
        )
    except OidcError:
        pending_oidc_store.discard(state)
        response = _oidc_verification_failure_page(status_code=401)
        _clear_pending_correlation_cookie(response)
        return response

    proof_version = str(principal.credential_version or "")
    if not proof_version:
        pending_oidc_store.discard(state)
        response = _oidc_verification_failure_page(status_code=409)
        _clear_pending_correlation_cookie(response)
        return response

    # Serialize baseline validation, proof binding, persistence, old-session
    # revocation, and replacement-session creation as one auth configuration event.
    async with authentication_configuration_lock:
        try:
            committed = commit_verified_pending_oidc(
                state,
                expected_configuration_version=proof_version,
            )
        except Exception:  # noqa: BLE001 - never expose persistence/config details at callback boundary
            committed = False
        if not committed:
            response = _oidc_verification_failure_page(status_code=409)
            _clear_pending_correlation_cookie(response)
            return response

        cfg = get_settings()
        current_version = oidc_configuration_version(cfg)
        if not current_version or not secrets.compare_digest(
            proof_version,
            current_version,
        ):
            response = _oidc_verification_failure_page(status_code=409)
            _clear_pending_correlation_cookie(response)
            return response

        old_token = session_cookie_token(request)
        if old_token:
            session_store.revoke(old_token)
        lifetime = interactive_routes._session_lifetime_seconds(cfg)
        token, _record = session_store.create(
            principal,
            lifetime_seconds=lifetime,
            credential_version=proof_version,
        )

    response = RedirectResponse(url=safe_return_path(return_to), status_code=303)
    set_session_cookie(
        response,
        request,
        token,
        max_age=lifetime,
        force_secure=True,
    )
    clear_login_csrf_cookie(response, request)
    _clear_pending_correlation_cookie(response)
    response.headers["Cache-Control"] = "no-store"
    return response
