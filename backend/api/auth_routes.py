from __future__ import annotations

import html
import os
import secrets
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from auth.csrf import (
    clear_login_csrf_cookie,
    login_csrf_cookie_name,
    login_csrf_store,
    set_login_csrf_cookie,
)
from auth.manager import (
    PasswordAuthenticationBusy,
    password_authentication_snapshot_current,
    peer_key,
    verify_local_credentials,
)
from auth.models import AuthMechanism, Principal
from auth.oidc import (
    OIDC_CORRELATION_COOKIE,
    OIDC_TRANSACTION_TTL_SECONDS,
    OidcError,
    begin_oidc_login,
    complete_oidc_login,
    oidc_auth_ready,
    oidc_transaction_store,
)
from auth.oidc_version import oidc_configuration_version
from auth.passwords import password_credential_version
from auth.policy import (
    interactive_auth_enabled,
    oidc_auth_enabled,
    password_auth_enabled,
    password_auth_ready,
    safe_return_path,
)
from auth.sessions import (
    clear_session_cookie,
    session_cookie_token,
    session_store,
    set_session_cookie,
)
from auth.throttle import login_challenge_rate_limiter, oidc_start_rate_limiter
from auth.transitions import authentication_configuration_lock
from core.config import get_settings


router = APIRouter()


_AUTH_MARK_SVG = """<svg class="brand-mark" viewBox="0 0 128 128" aria-hidden="true" focusable="false">
<defs><linearGradient id="auth-outline" x1="18" y1="18" x2="110" y2="110" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#a62cff"/><stop offset="1" stop-color="#208cff"/></linearGradient><linearGradient id="auth-arrow" x1="64" y1="24" x2="64" y2="86" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#b532ff"/><stop offset=".48" stop-color="#7855ff"/><stop offset="1" stop-color="#08a8ff"/></linearGradient><linearGradient id="auth-tray" x1="28" y1="86" x2="101" y2="98" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#9b32ff"/><stop offset="1" stop-color="#149fff"/></linearGradient><filter id="auth-glow" x="-35%" y="-35%" width="170%" height="170%"><feGaussianBlur stdDeviation="2.4" result="blur"/><feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>
<rect x="14" y="13" width="100" height="102" rx="23" fill="#080b18" stroke="url(#auth-outline)" stroke-width="2.5"/><path d="M55 30c0-2.8 2.2-5 5-5h8c2.8 0 5 2.2 5 5v27h10.2c3.8 0 5.8 4.5 3.2 7.2L67.2 84.8a4.4 4.4 0 0 1-6.4 0L41.6 64.2c-2.6-2.7-.6-7.2 3.2-7.2H55V30z" fill="url(#auth-arrow)" filter="url(#auth-glow)"/><path d="M28 79v12.5c0 5 4 9 9 9h54c5 0 9-4 9-9V79" fill="none" stroke="url(#auth-tray)" stroke-width="7" stroke-linecap="round" stroke-linejoin="round" filter="url(#auth-glow)"/></svg>"""


_AUTH_PAGE_STYLE = """
:root { --bg:#090812;--bg2:#100e1c;--surface:#171526;--surface2:#211e34;--border:#302c49;--border2:#484268;--text:#f4f1ff;--text2:#c2bdd6;--text3:#89839f;--accent:#a67cff;--accent2:#66a8ff;--accent-rgb:166,124,255;--accent-contrast:#120d1d;--danger:#ff6b6b;--primary-gradient:linear-gradient(135deg,#a67cff,#4f8cff);--primary-gradient-hover:linear-gradient(135deg,#b991ff,#66a8ff); }
* { box-sizing:border-box; }
body { margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:radial-gradient(circle at 16% 12%,rgba(var(--accent-rgb),.13),transparent 25%),radial-gradient(circle at 88% 8%,rgba(99,164,255,.10),transparent 20%),linear-gradient(180deg,var(--bg2),var(--bg));color:var(--text);font-family:Outfit,Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
.card { width:min(460px,100%);padding:30px;border:1px solid var(--border);border-radius:12px;background:linear-gradient(160deg,rgba(33,30,52,.97),rgba(15,13,27,.94));box-shadow:0 18px 40px rgba(0,0,8,.34); }
.brand-lockup { display:flex;align-items:center;gap:14px;margin-bottom:18px; }.brand-mark { width:62px;height:62px;flex:0 0 62px;display:block; }.brand { font-size:28px;font-weight:800;letter-spacing:-.7px;line-height:1;margin:0 0 5px; }.brand span { color:var(--accent); }.brand-sub { color:var(--text3);font-size:11px;letter-spacing:.08em;text-transform:uppercase;font-weight:600; }h1 { font-size:17px;margin:0 0 24px;color:var(--text2);font-weight:500; }
label { display:block;font-size:12px;font-weight:700;color:var(--text2);margin:14px 0 7px; }input { width:100%;border:1px solid var(--border);background:var(--surface2);color:var(--text);border-radius:8px;padding:11px 12px;font:inherit;outline:none; }input:focus { border-color:var(--accent);box-shadow:0 0 0 3px rgba(var(--accent-rgb),.24); }
.auth-action { width:100%;margin-top:20px;border-radius:8px;padding:11px 14px;font-weight:800;font-size:14px;cursor:pointer;text-align:center;text-decoration:none;display:block; }button.auth-action { font-family:inherit; }.auth-action:hover { filter:brightness(1.06); }.primary { border:0;background:var(--primary-gradient);color:var(--accent-contrast); }.primary:hover { background:var(--primary-gradient-hover); }.secondary { background:var(--surface2);color:var(--text);border:1px solid var(--border); }
.divider { display:flex;align-items:center;gap:12px;color:var(--text3);font-size:11px;margin:22px 0 0; }.divider:before,.divider:after { content:"";height:1px;background:var(--border);flex:1; }.error { border:1px solid rgba(255,107,107,.4);background:rgba(255,107,107,.08);color:#ffc0c0;padding:10px 12px;border-radius:8px;font-size:12px;line-height:1.45;margin:0 0 15px; }.muted { color:var(--text3);font-size:13px;line-height:1.55; }.foot { margin-top:22px;padding-top:16px;border-top:1px solid var(--border);color:var(--text3);font-size:11px;line-height:1.5; }
"""


def _session_lifetime_seconds(cfg) -> int:
    hours = int(getattr(cfg, "auth_session_lifetime_hours", 12) or 12)
    return max(3600, min(168 * 3600, hours * 3600))


def _session_record_current(record, cfg) -> bool:
    if record is None:
        return False
    mechanism = record.principal.mechanism
    if mechanism is AuthMechanism.PASSWORD_SESSION:
        if not password_auth_ready(cfg):
            return False
        current_username = str(getattr(cfg, "auth_username", "") or "").strip()
        if not current_username or record.principal.subject != current_username:
            return False
        current_version = password_credential_version(getattr(cfg, "auth_password_hash", ""))
        return bool(current_version and record.credential_version == current_version)
    if mechanism is AuthMechanism.OIDC_SESSION:
        if not oidc_auth_ready(cfg):
            return False
        current_version = oidc_configuration_version(cfg)
        return bool(current_version and record.credential_version == current_version)
    return False


def _static_asset(name: str) -> Path:
    candidates: list[Path] = []
    configured = os.getenv("STATIC_DIR", "").strip()
    if configured:
        candidates.append(Path(configured) / name)
    candidates.extend(
        (
            Path(__file__).resolve().parents[2] / "frontend" / "static" / name,
            Path("/app/frontend/static") / name,
            Path("/app/static") / name,
        )
    )
    asset = next((candidate for candidate in candidates if candidate.is_file()), None)
    if asset is None:
        raise RuntimeError(f"Frontend asset not found: {name}")
    return asset


def _login_page(
    request: Request,
    *,
    csrf_token: str,
    return_to: str,
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    cfg = get_settings()
    password_enabled = password_auth_enabled(cfg)
    password_ready = password_auth_ready(cfg)
    oidc_enabled = oidc_auth_enabled(cfg)
    oidc_ready = oidc_auth_ready(cfg) if oidc_enabled else False
    provider_name = html.escape(
        str(getattr(cfg, "oidc_provider_name", "") or "OpenID Connect").strip()
        or "OpenID Connect"
    )
    error_html = (
        f'<div class="error" role="alert">{html.escape(error)}</div>' if error else ""
    )

    controls: list[str] = []
    if oidc_enabled and oidc_ready:
        controls.append(
            f'<a class="auth-action primary oidc" href="/auth/oidc/start?next={quote(return_to, safe="")}">'
            f"Continue with {provider_name}</a>"
        )
    elif oidc_enabled:
        controls.append(
            '<div class="error" role="alert">OpenID Connect is enabled but its local '
            "configuration is incomplete or invalid.</div>"
        )

    if password_enabled and password_ready:
        if oidc_ready:
            controls.append('<div class="divider"><span>or use local password</span></div>')
        password_button_class = "secondary" if oidc_ready else "primary"
        controls.append(
            f"""
            <form method="post" action="/login" autocomplete="on">
              <input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}">
              <input type="hidden" name="next" value="{html.escape(return_to, quote=True)}">
              <label for="username">Username</label>
              <input id="username" name="username" type="text" maxlength="256" autocomplete="username" required>
              <label for="password">Password</label>
              <input id="password" name="password" type="password" maxlength="4096" autocomplete="current-password" required>
              <button class="auth-action {password_button_class}" type="submit">Sign In</button>
            </form>
            """
        )
    elif password_enabled:
        controls.append(
            '<div class="error" role="alert">Username &amp; Password authentication is enabled '
            "but is not fully configured. That mechanism is unavailable.</div>"
        )

    if not password_enabled and not oidc_enabled:
        controls.append('<p class="muted">Authentication is not currently required.</p>')

    interactive_controls = "\n".join(controls)
    body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in · DebridPulse</title>
<style>{_AUTH_PAGE_STYLE}</style>
</head>
<body>
<main class="card">
  <div class="brand-lockup">{_AUTH_MARK_SVG}<div><div class="brand">Debrid<span>Pulse</span></div><div class="brand-sub">Secure access</div></div></div>
  <h1>Sign in to continue</h1>
  {error_html}
  {interactive_controls}
  <div class="foot">Password-only LAN deployments may operate over HTTP. OpenID Connect requires a canonical HTTPS external URL.</div>
</main>
</body>
</html>"""
    response = HTMLResponse(content=body, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    return response


def _state_free_auth_page(
    *,
    message: str,
    status_code: int,
    retry_after: int | None = None,
) -> HTMLResponse:
    """Render an authentication error without allocating browser challenge state."""
    body = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in · DebridPulse</title><style>{_AUTH_PAGE_STYLE}</style></head>
<body><main class="card"><div class="brand-lockup">{_AUTH_MARK_SVG}<div><div class="brand">Debrid<span>Pulse</span></div><div class="brand-sub">Secure access</div></div></div><h1>Sign in unavailable</h1><div class="error" role="alert">{html.escape(message)}</div><a class="auth-action secondary" href="/login">Return to sign in</a></main></body>
</html>"""
    response = HTMLResponse(content=body, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"
    if retry_after is not None:
        response.headers["Retry-After"] = str(max(1, int(retry_after)))
    return response


def _issue_login_page(
    request: Request,
    *,
    return_to: str,
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    if not login_challenge_rate_limiter.allow(peer_key(request)):
        return _state_free_auth_page(
            message="Too many sign-in challenges have been requested. Try again shortly.",
            status_code=429,
            retry_after=60,
        )
    browser_nonce, form_token = login_csrf_store.issue()
    response = _login_page(
        request,
        csrf_token=form_token,
        return_to=safe_return_path(return_to),
        error=error,
        status_code=status_code,
    )
    set_login_csrf_cookie(response, request, browser_nonce)
    return response


def _set_oidc_correlation_cookie(response: Response, value: str) -> None:
    response.set_cookie(
        key=OIDC_CORRELATION_COOKIE,
        value=str(value),
        max_age=OIDC_TRANSACTION_TTL_SECONDS,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def _clear_oidc_correlation_cookie(response: Response) -> None:
    response.delete_cookie(
        key=OIDC_CORRELATION_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


@router.get("/api/auth/status")
async def public_auth_status():
    """Minimal public bootstrap state needed to render the login experience."""
    cfg = get_settings()
    password_enabled = password_auth_enabled(cfg)
    oidc_enabled = oidc_auth_enabled(cfg)
    return {
        "authentication_required": interactive_auth_enabled(cfg),
        "password_enabled": password_enabled,
        "password_ready": password_auth_ready(cfg) if password_enabled else False,
        "oidc_enabled": oidc_enabled,
        "oidc_ready": oidc_auth_ready(cfg) if oidc_enabled else False,
        "oidc_provider_name": (
            str(getattr(cfg, "oidc_provider_name", "") or "OpenID Connect").strip()
            or "OpenID Connect"
        ),
    }


@router.get("/app.js", include_in_schema=False)
async def application_javascript_bundle():
    """Serve the protected browser bootstrap before the existing app script."""
    auth_js = _static_asset("auth.js").read_text(encoding="utf-8")
    app_js = _static_asset("app.js").read_text(encoding="utf-8")
    response = Response(
        content=f"{auth_js}\n;\n{app_js}",
        media_type="application/javascript",
    )
    response.headers["Cache-Control"] = "no-cache"
    return response


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    cfg = get_settings()
    return_to = safe_return_path(next)
    if not interactive_auth_enabled(cfg):
        return RedirectResponse(url=return_to, status_code=303)

    existing_token = session_cookie_token(request)
    existing = session_store.resolve(existing_token) if existing_token else None
    if _session_record_current(existing, cfg):
        return RedirectResponse(url=return_to, status_code=303)
    if existing_token:
        session_store.revoke(existing_token)

    response = _issue_login_page(request, return_to=return_to)
    if existing_token:
        clear_session_cookie(response, request)
    return response


@router.post("/login")
async def password_login(request: Request):
    cfg = get_settings()
    if not password_auth_enabled(cfg):
        return _issue_login_page(
            request,
            return_to="/",
            error="Username & Password authentication is disabled.",
            status_code=403,
        )
    if not password_auth_ready(cfg):
        return _issue_login_page(
            request,
            return_to="/",
            error="Username & Password authentication is unavailable because its configuration is incomplete.",
            status_code=503,
        )

    form = await request.form()
    username = str(form.get("username") or "")
    password = str(form.get("password") or "")
    csrf_token = str(form.get("csrf_token") or "")
    return_to = safe_return_path(str(form.get("next") or "/"))

    if len(username) > 256 or len(password) > 4096 or len(csrf_token) > 256:
        return _issue_login_page(
            request,
            return_to=return_to,
            error="Invalid sign-in request.",
            status_code=400,
        )

    browser_nonce = str(request.cookies.get(login_csrf_cookie_name(request), "") or "")
    if not login_csrf_store.consume(browser_nonce, csrf_token):
        return _issue_login_page(
            request,
            return_to=return_to,
            error="The sign-in form expired. Try again.",
            status_code=403,
        )

    try:
        verified = await verify_local_credentials(
            request,
            username,
            password,
            settings=cfg,
        )
    except PasswordAuthenticationBusy:
        response = _issue_login_page(
            request,
            return_to=return_to,
            error="Too many sign-in attempts are already being processed. Try again shortly.",
            status_code=429,
        )
        response.headers["Retry-After"] = "2"
        return response
    if not verified:
        return _issue_login_page(
            request,
            return_to=return_to,
            error="Invalid username or password.",
            status_code=401,
        )

    async with authentication_configuration_lock:
        current = get_settings()
        if not password_authentication_snapshot_current(cfg, current):
            return _issue_login_page(
                request,
                return_to=return_to,
                error="Authentication configuration changed while sign-in was in progress. Try again.",
                status_code=409,
            )

        old_token = session_cookie_token(request)
        if old_token:
            session_store.revoke(old_token)

        configured_username = str(getattr(current, "auth_username", "") or "").strip()
        lifetime = _session_lifetime_seconds(current)
        version = password_credential_version(getattr(current, "auth_password_hash", ""))
        token, _record = session_store.create(
            Principal.password_session(configured_username, credential_version=version),
            lifetime_seconds=lifetime,
            credential_version=version,
        )
    response = RedirectResponse(url=return_to, status_code=303)
    set_session_cookie(response, request, token, max_age=lifetime)
    clear_login_csrf_cookie(response, request)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/auth/oidc/start")
async def oidc_start(request: Request, next: str = "/"):
    cfg = get_settings()
    return_to = safe_return_path(next)
    if not oidc_auth_enabled(cfg):
        return _issue_login_page(
            request,
            return_to=return_to,
            error="OpenID Connect authentication is disabled.",
            status_code=404,
        )
    if not oidc_start_rate_limiter.allow(peer_key(request)):
        return _state_free_auth_page(
            message="Too many OpenID Connect sign-in attempts have been started. Try again shortly.",
            status_code=429,
            retry_after=60,
        )
    try:
        authorization_url, correlation = await begin_oidc_login(cfg, return_to=return_to)
    except OidcError:
        return _issue_login_page(
            request,
            return_to=return_to,
            error="OpenID Connect is currently unavailable or misconfigured.",
            status_code=503,
        )
    response = RedirectResponse(url=authorization_url, status_code=303)
    _set_oidc_correlation_cookie(response, correlation)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/auth/oidc/callback")
async def oidc_callback(
    request: Request,
    state: str = "",
    code: str = "",
    error: str = "",
):
    correlation = str(request.cookies.get(OIDC_CORRELATION_COOKIE, "") or "")
    if error:
        oidc_transaction_store.consume(state, correlation)
        response = _issue_login_page(
            request,
            return_to="/",
            error="OpenID Connect sign-in was not completed.",
            status_code=401,
        )
        _clear_oidc_correlation_cookie(response)
        return response

    try:
        principal, return_to = await complete_oidc_login(
            state=state,
            code=code,
            correlation=correlation,
        )
    except OidcError:
        response = _issue_login_page(
            request,
            return_to="/",
            error="OpenID Connect sign-in could not be validated or authorized.",
            status_code=401,
        )
        _clear_oidc_correlation_cookie(response)
        return response

    async with authentication_configuration_lock:
        cfg = get_settings()
        current_version = oidc_configuration_version(cfg)
        proof_version = str(principal.credential_version or "")
        if not proof_version or not current_version or not secrets.compare_digest(
            proof_version,
            current_version,
        ):
            response = _issue_login_page(
                request,
                return_to="/",
                error="Authentication configuration changed while sign-in was in progress. Start a new sign-in.",
                status_code=409,
            )
            _clear_oidc_correlation_cookie(response)
            return response

        old_token = session_cookie_token(request)
        if old_token:
            session_store.revoke(old_token)
        lifetime = _session_lifetime_seconds(cfg)
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
    _clear_oidc_correlation_cookie(response)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/api/auth/session")
async def auth_session_status(request: Request, response: Response = None):
    principal = getattr(request.state, "principal", Principal.anonymous())
    session_token = str(getattr(request.state, "auth_session_token", "") or "")
    record = session_store.resolve(session_token) if session_token else None
    if response is not None:
        response.headers["Cache-Control"] = "no-store"
    return {
        "authenticated": bool(principal.authenticated),
        "mechanism": principal.mechanism.value if principal.mechanism else None,
        "subject": principal.subject,
        "display_name": principal.display_name,
        "csrf_token": session_store.csrf_token(session_token) if record is not None else "",
        "session_expires_in_seconds": (
            max(0, int(record.expires_at - time.monotonic())) if record is not None else None
        ),
    }


@router.post("/api/auth/logout")
async def logout(request: Request):
    principal = getattr(request.state, "principal", Principal.anonymous())
    if principal.mechanism not in {AuthMechanism.PASSWORD_SESSION, AuthMechanism.OIDC_SESSION}:
        return JSONResponse(
            content={"detail": "No browser application session"},
            status_code=400,
        )

    session_token = str(getattr(request.state, "auth_session_token", "") or "")
    if session_token:
        session_store.revoke(session_token)
    response = JSONResponse(content={"ok": True})
    clear_session_cookie(response, request)
    response.headers["Cache-Control"] = "no-store"
    return response
