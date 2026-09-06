from __future__ import annotations

import base64
import binascii
from collections.abc import Awaitable, Callable, Iterable
from urllib.parse import quote

from fastapi import Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from auth.api_tokens import api_token_store
from auth.manager import (
    PasswordAuthenticationBusy,
    password_authentication_snapshot_current,
    verify_local_credentials,
)
from auth.models import AuthMechanism, Principal
from auth.passwords import password_credential_version
from auth.policy import (
    MUTATING_HTTP_METHODS,
    interactive_auth_enabled,
    is_public_path,
    normalized_origin,
    oidc_auth_enabled,
    password_auth_enabled,
    password_auth_ready,
    safe_return_path,
    trusted_request_origin,
)
from auth.sessions import CSRF_HEADER, session_cookie_token, session_store
from auth.transitions import (
    authentication_configuration_lock,
    is_auth_settings_mutation,
    settings_transition_rejection,
)
from core.branding import APP_SHORT_NAME
from core.config import get_settings


CallNext = Callable[[Request], Awaitable[Response]]


def _attach_principal(request: Request, principal: Principal) -> None:
    request.state.principal = principal


def _attach_session(request: Request, token: str) -> None:
    request.state.auth_session_token = token


def _has_basic_scheme(header: str) -> bool:
    scheme, separator, _token = str(header or "").strip().partition(" ")
    return bool(separator and scheme.casefold() == "basic")


def _has_bearer_scheme(header: str) -> bool:
    scheme, separator, _token = str(header or "").strip().partition(" ")
    return bool(separator and scheme.casefold() == "bearer")


def _decode_bearer_token(header: str) -> str | None:
    scheme, separator, token = str(header or "").strip().partition(" ")
    if not separator or scheme.casefold() != "bearer":
        return None
    token = token.strip()
    if not token or len(token) > 4096 or any(ch.isspace() for ch in token):
        return None
    return token


def _decode_basic_credentials(header: str) -> tuple[str, str] | None:
    scheme, separator, token = str(header or "").strip().partition(" ")
    if not separator or scheme.casefold() != "basic":
        return None
    token = token.strip()
    if not token:
        return None
    try:
        decoded = base64.b64decode(token, validate=True).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError, UnicodeError):
        return None
    username, credential_separator, password = decoded.partition(":")
    if not credential_separator:
        return None
    return username, password


def _unauthorized(*, basic_challenge: bool = False, bearer_challenge: bool = False) -> Response:
    headers = None
    if bearer_challenge:
        headers = {"WWW-Authenticate": "Bearer"}
    elif basic_challenge:
        headers = {"WWW-Authenticate": f'Basic realm="{APP_SHORT_NAME}"'}
    return JSONResponse(content={"detail": "Unauthorized"}, status_code=401, headers=headers)


def _authentication_busy() -> Response:
    return JSONResponse(
        content={"detail": "Too many authentication attempts are already being processed"},
        status_code=429,
        headers={"Retry-After": "2"},
    )


def _is_browser_navigation(request: Request) -> bool:
    if request.method.upper() != "GET" or request.url.path.startswith("/api/"):
        return False
    accept = str(request.headers.get("Accept", "") or "").casefold()
    return "text/html" in accept


def _browser_login_redirect(request: Request) -> Response:
    target = request.url.path or "/"
    if request.url.query:
        target += "?" + request.url.query
    target = safe_return_path(target)
    return RedirectResponse(url=f"/login?next={quote(target, safe='')}", status_code=303)


def _oidc_ready(cfg) -> bool:
    if not oidc_auth_enabled(cfg):
        return False
    from auth.oidc import oidc_auth_ready

    return oidc_auth_ready(cfg)


def _session_record_still_valid(record, cfg) -> bool:
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
        if not _oidc_ready(cfg):
            return False
        from auth.oidc_version import oidc_configuration_version

        current_version = oidc_configuration_version(cfg)
        return bool(current_version and record.credential_version == current_version)
    return False


async def _admit_authenticated(
    request: Request,
    call_next: CallNext,
    principal: Principal,
    cfg,
) -> Response:
    rejection = await settings_transition_rejection(request, principal, cfg)
    if rejection is not None:
        return rejection
    return await call_next(request)


async def enforce_general_web_security(
    request: Request,
    call_next: CallNext,
    *,
    allowed_origins: Iterable[str] = (),
) -> Response:
    """Reject untrusted browser mutations independently of authentication."""
    if request.method.upper() not in MUTATING_HTTP_METHODS:
        return await call_next(request)

    origin = str(request.headers.get("Origin", "") or "").strip()
    fetch_site = str(request.headers.get("Sec-Fetch-Site", "") or "").strip().casefold()
    if not origin:
        if fetch_site == "cross-site":
            return Response(content="Forbidden request context", status_code=403)
        return await call_next(request)

    # Host is client-controlled browser input. Establish an intentionally trusted
    # application authority before comparing Origin with it. The operator-owned
    # public base URL covers reverse-proxy deployments; direct localhost/IP and
    # the transport-owned ASGI server authority preserve ordinary local access.
    request_identity = trusted_request_origin(request, settings=get_settings())
    if request_identity is None:
        return Response(content="Forbidden authority", status_code=403)

    origin_identity = normalized_origin(origin)
    configured_identities = {
        identity
        for item in allowed_origins
        if (identity := normalized_origin(str(item or "").strip())) is not None
    }
    configured_cross_origin = bool(
        origin_identity is not None and origin_identity in configured_identities
    )

    if fetch_site == "cross-site" and not configured_cross_origin:
        return Response(content="Forbidden request context", status_code=403)

    if (
        origin_identity is None
        or (origin_identity != request_identity and not configured_cross_origin)
    ):
        return Response(content="Forbidden origin", status_code=403)

    return await call_next(request)


async def _enforce_authentication_unlocked(request: Request, call_next: CallNext) -> Response:
    """Authentication implementation; caller serializes auth-config writes."""
    _attach_principal(request, Principal.anonymous())
    cfg = get_settings()

    # CORS middleware is authoritative for preflight. OPTIONS carries no
    # application mutation and must reach it before interactive authentication.
    if request.method.upper() == "OPTIONS":
        return await call_next(request)

    if is_public_path(request.url.path):
        return await call_next(request)

    session_token = session_cookie_token(request)
    if session_token:
        record = session_store.resolve(session_token)
        if record is not None and _session_record_still_valid(record, cfg):
            _attach_principal(request, record.principal)
            _attach_session(request, session_token)
            if request.method.upper() in MUTATING_HTTP_METHODS:
                csrf = str(request.headers.get(CSRF_HEADER, "") or "")
                if not session_store.verify_csrf(session_token, csrf):
                    return JSONResponse(
                        content={"detail": "CSRF validation failed"},
                        status_code=403,
                    )
            return await _admit_authenticated(request, call_next, record.principal, cfg)
        session_store.revoke(session_token)

    auth_header = str(request.headers.get("Authorization", "") or "")

    if _has_bearer_scheme(auth_header):
        if not interactive_auth_enabled(cfg):
            return await _admit_authenticated(request, call_next, Principal.anonymous(), cfg)
        provided_token = _decode_bearer_token(auth_header)
        if provided_token is not None and api_token_store.verify(provided_token):
            principal = Principal.api_token()
            _attach_principal(request, principal)
            return await _admit_authenticated(request, call_next, principal, cfg)
        return _unauthorized(bearer_challenge=True)

    if _has_basic_scheme(auth_header):
        if not password_auth_enabled(cfg):
            if not interactive_auth_enabled(cfg):
                return await _admit_authenticated(request, call_next, Principal.anonymous(), cfg)
            return _unauthorized(basic_challenge=True)
        if not password_auth_ready(cfg):
            return JSONResponse(
                content={"detail": "Password authentication unavailable"},
                status_code=503,
            )
        credentials = _decode_basic_credentials(auth_header)
        try:
            if credentials is None:
                await verify_local_credentials(request, "", "", settings=cfg)
                return _unauthorized(basic_challenge=True)
            provided_user, provided_pass = credentials
            if await verify_local_credentials(
                request,
                provided_user,
                provided_pass,
                allow_basic_success_cache=True,
                settings=cfg,
            ):
                if is_auth_settings_mutation(request):
                    # The settings-mutation path already owns the non-reentrant
                    # configuration lock in enforce_authentication().
                    current = get_settings()
                    if not password_authentication_snapshot_current(cfg, current):
                        return _unauthorized(basic_challenge=True)
                    username = str(getattr(current, "auth_username", "") or "").strip()
                    principal = Principal.http_basic(username)
                    _attach_principal(request, principal)
                    return await _admit_authenticated(request, call_next, principal, current)

                async with authentication_configuration_lock:
                    current = get_settings()
                    if not password_authentication_snapshot_current(cfg, current):
                        return _unauthorized(basic_challenge=True)
                    username = str(getattr(current, "auth_username", "") or "").strip()
                    principal = Principal.http_basic(username)
                    _attach_principal(request, principal)
                return await _admit_authenticated(request, call_next, principal, current)
            return _unauthorized(basic_challenge=True)
        except PasswordAuthenticationBusy:
            return _authentication_busy()

    if not interactive_auth_enabled(cfg):
        # Open mode still runs the auth-transition state machine. This is
        # critical when an anonymous operator enables the first mechanism.
        return await _admit_authenticated(request, call_next, Principal.anonymous(), cfg)

    if not (
        password_auth_ready(cfg)
        or _oidc_ready(cfg)
        or (api_token_store.enabled and api_token_store.configured)
    ):
        return JSONResponse(
            content={"detail": "Configured authentication is unavailable"},
            status_code=503,
        )

    if _is_browser_navigation(request):
        return _browser_login_redirect(request)

    return _unauthorized()


async def enforce_authentication(request: Request, call_next: CallNext) -> Response:
    """Outer authentication boundary for open, session, Bearer and Basic access."""
    if is_auth_settings_mutation(request):
        # Hold the lock from credential validation through FastAPI route
        # persistence. This closes the middleware-check/route-commit TOCTOU
        # window for both broad and dedicated settings APIs.
        async with authentication_configuration_lock:
            return await _enforce_authentication_unlocked(request, call_next)
    return await _enforce_authentication_unlocked(request, call_next)


# Compatibility name for phase-2 tests/downstream imports while the manager API
# settles. All requests now use the application-session-aware implementation.
enforce_password_http_auth = enforce_authentication
