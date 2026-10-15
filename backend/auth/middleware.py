from __future__ import annotations

import base64
import binascii
from collections.abc import Awaitable, Callable, Iterable
from urllib.parse import quote

from fastapi import Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from auth.accounts import admitted, resolve_token
from auth.manager import (
    PasswordAuthenticationBusy,
    password_proof_current,
    verify_local_credentials,
)
from auth.models import AuthMechanism, Principal
from auth.permissions import non_route_capability, permits
from auth.policy import (
    MUTATING_HTTP_METHODS,
    interactive_auth_enabled,
    is_public_path,
    normalized_origin,
    public_base_url_bootstrap_origin,
    oidc_auth_enabled,
    password_auth_enabled,
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
from external.arr import QUERY_KEY_PATHS, adapter_of, available


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


def session_record_valid(record, cfg) -> bool:
    """Whether a stored application session still admits its request.

    The mechanism that issued it must still be enabled under the current
    configuration (and, for OIDC, the exact configuration that proved it).
    Its account is current by construction: every account change revokes the
    account's sessions (``auth.accounts``) in the same critical section."""
    if record is None or not record.principal.account_id:
        return False
    mechanism = record.principal.mechanism
    if mechanism is AuthMechanism.PASSWORD_SESSION:
        return password_auth_enabled(cfg)
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
    # A path outside ``/api/`` is the interface, unless it is the one routed
    # protocol endpoint there: SABnzbd's exact ``/api``, which its routed
    # action authorizes like every other.
    routed = request.url.path.startswith("/api/") or request.url.path in QUERY_KEY_PATHS
    if not routed and not permits(
        principal, non_route_capability(request.url.path),
    ):
        # The interface and the API documentation are not routed actions, so
        # the one policy is applied to them here; routed actions are
        # authorized by the application dependency (``authorize_request``).
        return JSONResponse(content={"detail": "Forbidden"}, status_code=403)
    rejection = await settings_transition_rejection(request, principal, cfg)
    if rejection is not None:
        return rejection
    return await call_next(request)


async def _admit_standalone(request: Request, call_next: CallNext, cfg) -> Response:
    """No interactive authentication: the request acts as the standalone
    installation's one Admin-equivalent principal, never as an anonymous
    tenant of a multi-user installation."""
    principal = Principal.standalone()
    _attach_principal(request, principal)
    return await _admit_authenticated(request, call_next, principal, cfg)


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
    settings = get_settings()
    request_identity = trusted_request_origin(request, settings=settings)
    origin_identity = normalized_origin(origin)
    if request_identity is None:
        # A reverse-proxy deployment has no trusted authority until the operator
        # establishes one, and the only way to establish it is a mutation --
        # which this boundary refuses. That is a bootstrap deadlock, not a
        # policy. The ONE way out is admitting the single mutation that creates
        # the authority, and only while no authority exists; the route below is
        # what enforces that the mutation really is only that. Nothing else is
        # relaxed, and the allowance disappears the moment the value is stored.
        if public_base_url_bootstrap_origin(request, settings) is not None:
            return await call_next(request)
        return Response(content="Forbidden authority", status_code=403)

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


def _protocol_key_refusal(message: str) -> Response:
    """The download-client protocol's own refusal: *arr reports a rejected key
    only from this body (HTTP 200), never from a status code."""
    return JSONResponse(content={"status": False, "error": message}, status_code=200)


async def _authenticate_query_key(request: Request, call_next: CallNext, cfg) -> Response:
    """A download-client protocol endpoint (``QUERY_KEY_PATHS``) whose client
    can send the Integration token only as an ``apikey`` query parameter (or
    as a Bearer header). The key is verified exactly as a Bearer token is --
    the same ``resolve_token``, the same Integration principal -- in every
    authentication mode. Nothing else authenticates here: no session cookie
    (so no browser can be made to act through it), no password, and never the
    open-mode standalone principal."""
    auth_header = str(request.headers.get("Authorization", "") or "")
    provided = (_decode_bearer_token(auth_header) if _has_bearer_scheme(auth_header)
                else str(request.query_params.get("apikey") or "").strip() or None)
    if provided is None:
        return _protocol_key_refusal("API Key Required")
    if len(provided) > 4096 or any(ch.isspace() for ch in provided):
        return _protocol_key_refusal("API Key Incorrect")
    resolved = await resolve_token(provided)
    if resolved is None:
        return _protocol_key_refusal("API Key Incorrect")
    principal = Principal.integration_token(*resolved)
    _attach_principal(request, principal)
    return await _admit_authenticated(request, call_next, principal, cfg)


async def _enforce_authentication_unlocked(request: Request, call_next: CallNext) -> Response:
    """Authentication implementation; caller serializes auth-config writes."""
    _attach_principal(request, Principal.anonymous())
    cfg = get_settings()

    # A download-client dialect no current delivery profile and token make
    # usable is not exposed: every path of it is the ordinary Not Found, for
    # every caller and method, before any credential is read or any refusal
    # names the protocol (``external.arr.available``).
    adapter = adapter_of(request.url.path)
    if adapter is not None and not await available(adapter):
        return JSONResponse(content={"detail": "Not Found"}, status_code=404)

    # CORS middleware is authoritative for preflight. OPTIONS carries no
    # application mutation and must reach it before interactive authentication.
    if request.method.upper() == "OPTIONS":
        return await call_next(request)

    if is_public_path(request.url.path):
        return await call_next(request)

    if request.url.path in QUERY_KEY_PATHS:
        return await _authenticate_query_key(request, call_next, cfg)

    session_token = session_cookie_token(request)
    if session_token:
        record = session_store.resolve(session_token)
        if record is not None and session_record_valid(record, cfg):
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
        # Only an active Integration account's own token authenticates a
        # bearer request, as that account, with its role -- even in open
        # mode, so its work stays attributed to it. A bearer credential that
        # names no active token is refused in every mode: an explicit,
        # invalid credential never falls back to the standalone Admin.
        provided_token = _decode_bearer_token(auth_header)
        resolved = await resolve_token(provided_token) if provided_token is not None else None
        if resolved is not None:
            principal = Principal.integration_token(*resolved)
            _attach_principal(request, principal)
            return await _admit_authenticated(request, call_next, principal, cfg)
        return _unauthorized(bearer_challenge=True)

    if _has_basic_scheme(auth_header):
        if not password_auth_enabled(cfg):
            if not interactive_auth_enabled(cfg):
                return await _admit_standalone(request, call_next, cfg)
            return _unauthorized(basic_challenge=True)
        credentials = _decode_basic_credentials(auth_header)
        try:
            if credentials is None:
                await verify_local_credentials(request, "", "", settings=cfg)
                return _unauthorized(basic_challenge=True)
            provided_user, provided_pass = credentials
            proven = await verify_local_credentials(
                request,
                provided_user,
                provided_pass,
                allow_basic_success_cache=True,
                settings=cfg,
            )
        except PasswordAuthenticationBusy:
            return _authentication_busy()
        if proven is None:
            return _unauthorized(basic_challenge=True)
        if is_auth_settings_mutation(request):
            # The settings-mutation path already owns the non-reentrant
            # configuration lock in enforce_authentication().
            current = get_settings()
            account = await password_proof_current(proven, current)
        else:
            async with authentication_configuration_lock:
                current = get_settings()
                account = await password_proof_current(proven, current)
        # HTTP Basic authenticates exactly the account whose credential
        # matched -- never a role by username -- and only an active one.
        if not admitted(account):
            return _unauthorized(basic_challenge=True)
        principal = Principal.http_basic(account)
        _attach_principal(request, principal)
        return await _admit_authenticated(request, call_next, principal, current)

    if not interactive_auth_enabled(cfg):
        # Open mode still runs the auth-transition state machine. This is
        # critical when the standalone operator enables the first mechanism.
        return await _admit_standalone(request, call_next, cfg)

    if not (
        password_auth_enabled(cfg)
        or _oidc_ready(cfg)
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
