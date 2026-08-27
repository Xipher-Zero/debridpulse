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


# Exact raster derivative of the reviewed DebridPulse logo. Authentication
# screens are intentionally self-contained, so the mark is embedded rather than
# weakening the unauthenticated static-asset boundary just for branding.
_AUTH_MARK_HTML = """<img class="brand-mark" alt="" aria-hidden="true" src="data:image/png;base64,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"/>"""


_AUTH_PAGE_STYLE = """
:root { --bg:#090812;--bg2:#100e1c;--surface:#171526;--surface2:#211e34;--border:#302c49;--border2:#484268;--text:#f4f1ff;--text2:#c2bdd6;--text3:#89839f;--accent:#a67cff;--accent2:#66a8ff;--accent-rgb:166,124,255;--accent-contrast:#120d1d;--danger:#ff6b6b;--primary-gradient:linear-gradient(135deg,#a67cff,#4f8cff);--primary-gradient-hover:linear-gradient(135deg,#b991ff,#66a8ff); }
* { box-sizing:border-box; }
body { margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:radial-gradient(circle at 16% 12%,rgba(var(--accent-rgb),.13),transparent 25%),radial-gradient(circle at 88% 8%,rgba(99,164,255,.10),transparent 20%),linear-gradient(180deg,var(--bg2),var(--bg));color:var(--text);font-family:Outfit,Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
.card { width:min(460px,100%);padding:30px;border:1px solid var(--border);border-radius:12px;background:linear-gradient(160deg,rgba(33,30,52,.97),rgba(15,13,27,.94));box-shadow:0 18px 40px rgba(0,0,8,.34); }
.brand-lockup { display:flex;align-items:center;gap:14px;margin-bottom:18px; }.brand-mark { width:62px;height:62px;flex:0 0 62px;display:block;object-fit:contain; }.brand { font-size:28px;font-weight:800;letter-spacing:-.7px;line-height:1;margin:0 0 5px; }.brand span { color:var(--accent); }.brand-sub { color:var(--text3);font-size:11px;letter-spacing:.08em;text-transform:uppercase;font-weight:600; }h1 { font-size:17px;margin:0 0 24px;color:var(--text2);font-weight:500; }
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
  <div class="brand-lockup">{_AUTH_MARK_HTML}<div><div class="brand">Debrid<span>Pulse</span></div><div class="brand-sub">Secure access</div></div></div>
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
        "default-src 'none'; img-src data:; style-src 'unsafe-inline'; form-action 'self'; "
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
<body><main class="card"><div class="brand-lockup">{_AUTH_MARK_HTML}<div><div class="brand">Debrid<span>Pulse</span></div><div class="brand-sub">Secure access</div></div></div><h1>Sign in unavailable</h1><div class="error" role="alert">{html.escape(message)}</div><a class="auth-action secondary" href="/login">Return to sign in</a></main></body>
</html>"""
    response = HTMLResponse(content=body, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"
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
