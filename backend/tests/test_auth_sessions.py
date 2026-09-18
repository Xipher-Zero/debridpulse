import base64
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi import Request, Response

from api import auth_routes
from auth.csrf import LoginCsrfStore, login_csrf_store
from auth.middleware import enforce_authentication
from auth.models import AuthMechanism, Principal
from auth.passwords import hash_password, password_credential_version
from auth.policy import safe_return_path
from auth.sessions import (
    HTTP_SESSION_COOKIE,
    HTTPS_SESSION_COOKIE,
    SessionStore,
    session_store,
    set_session_cookie,
)
from auth.throttle import password_failure_throttle


ROOT = Path(__file__).resolve().parents[2]


def _settings(*, enabled=True, username="operator", password="secret", lifetime=12):
    return SimpleNamespace(
        auth_password_enabled=enabled,
        auth_username=username,
        auth_password_hash=hash_password(password) if password else "",
        auth_session_lifetime_hours=lifetime,
    )


def _request(
    method="GET",
    path="/api/stats",
    headers=None,
    query_string=b"",
    body=b"",
    *,
    scheme="http",
):
    raw_headers = []
    for key, value in (headers or {}).items():
        raw_headers.append((str(key).lower().encode("latin-1"), str(value).encode("latin-1")))
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": scheme,
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query_string,
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("debridpulse.local", 443 if scheme == "https" else 80),
    }
    return Request(scope, receive=receive)


def _form_request(path, values, *, cookie="", extra_headers=None):
    body = urlencode(values).encode()
    headers = {
        "Host": "debridpulse.local",
        "Content-Type": "application/x-www-form-urlencoded",
        "Content-Length": str(len(body)),
        **(extra_headers or {}),
    }
    if cookie:
        headers["Cookie"] = cookie
    return _request("POST", path, headers=headers, body=body)


def _basic(username, password):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


def _response_cookie(response, name):
    for key, value in response.raw_headers:
        if key.lower() != b"set-cookie":
            continue
        parsed = SimpleCookie()
        parsed.load(value.decode("latin-1"))
        if name in parsed:
            return parsed[name]
    return None


async def _ok(_request):
    return Response(content="ok", status_code=200)


@pytest.mark.asyncio
async def test_auth_bootstrap_loads_before_application_javascript():
    bundle_response = await auth_routes.application_javascript_bundle()
    bundle = bundle_response.body.decode("utf-8")
    auth_marker = "DebridPulse application-session bootstrap"
    app_marker = "DebridPulse — self-hosted multi-provider transfer manager."
    assert auth_marker in bundle
    assert app_marker in bundle
    assert bundle.index(auth_marker) < bundle.index(app_marker)
    bootstrap = bundle.split(app_marker, 1)[0]
    assert "localStorage" not in bootstrap
    assert "sessionStorage" not in bootstrap
    assert "X-CSRF-Token" in bootstrap
    assert bundle_response.headers["cache-control"] == "no-cache"


def test_login_request_body_limit_is_narrower_than_general_application_limit():
    main = (ROOT / "backend/main.py").read_text()
    assert 'scope.get("path") == "/login"' in main
    assert "64 * 1024" in main


def test_process_restart_invalidates_browser_session_by_design():
    before_restart = SessionStore()
    token, _ = before_restart.create(
        Principal.password_session("operator"),
        lifetime_seconds=3600,
    )
    assert before_restart.resolve(token) is not None
    after_restart = SessionStore()
    assert after_restart.resolve(token) is None


def test_session_store_is_bounded_absolute_and_opaque():
    now = [100.0]
    store = SessionStore(max_entries=2, clock=lambda: now[0])
    principal = Principal.password_session("operator")
    first, _ = store.create(principal, lifetime_seconds=60, credential_version="v1")
    second, _ = store.create(principal, lifetime_seconds=60, credential_version="v1")
    third, _ = store.create(principal, lifetime_seconds=60, credential_version="v1")

    assert store.size == 2
    assert store.resolve(first) is None
    assert store.resolve(second) is not None
    assert store.resolve(third) is not None
    assert all(isinstance(key, bytes) for key in store._entries)
    assert first.encode() not in store._entries

    now[0] = 161.0
    assert store.resolve(second) is None
    assert store.resolve(third) is None
    assert store.size == 0


def test_session_csrf_is_derived_and_bound_to_session():
    store = SessionStore()
    token_a, _ = store.create(Principal.password_session("a"), lifetime_seconds=60)
    token_b, _ = store.create(Principal.password_session("b"), lifetime_seconds=60)
    csrf_a = store.csrf_token(token_a)
    assert csrf_a
    assert store.verify_csrf(token_a, csrf_a) is True
    assert store.verify_csrf(token_b, csrf_a) is False
    assert store.verify_csrf(token_a, "") is False


def test_login_csrf_is_one_time_bounded_and_expires():
    now = [50.0]
    store = LoginCsrfStore(ttl_seconds=30, max_entries=2, clock=lambda: now[0])
    nonce, token = store.issue()
    assert store.consume(nonce, token) is True
    assert store.consume(nonce, token) is False

    n1, _ = store.issue()
    store.issue()
    n3, t3 = store.issue()
    assert store.size == 2
    assert store.consume(n1, "wrong") is False
    assert store.size <= 2
    now[0] = 81.0
    assert store.consume(n3, t3) is False
    assert store.size == 0


def test_session_cookie_attributes_for_https_and_lan_http():
    https_request = _request(
        "GET",
        headers={"Host": "dp.example"},
        scheme="https",
    )
    https_response = Response()
    set_session_cookie(https_response, https_request, "token", max_age=3600)
    secure = _response_cookie(https_response, HTTPS_SESSION_COOKIE)
    assert secure is not None
    assert secure["secure"] is True
    assert secure["httponly"] is True
    assert secure["samesite"].lower() == "lax"
    assert secure["path"] == "/"
    assert not secure["domain"]

    http_request = _request("GET", headers={"Host": "dp.lan"})
    http_response = Response()
    set_session_cookie(http_response, http_request, "token", max_age=3600)
    lan = _response_cookie(http_response, HTTP_SESSION_COOKIE)
    assert lan is not None
    assert lan["secure"] == ""
    assert lan["httponly"] is True
    assert lan["samesite"].lower() == "lax"


def test_untrusted_forwarded_proto_does_not_upgrade_plain_http_cookie(monkeypatch):
    import auth.sessions as sessions

    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setattr(
        "core.config.get_settings",
        lambda: SimpleNamespace(public_base_url=""),
    )
    request = _request(
        "GET",
        headers={"Host": "dp.lan", "X-Forwarded-Proto": "https"},
    )
    response = Response()
    sessions.set_session_cookie(response, request, "token", max_age=3600)
    assert _response_cookie(response, HTTPS_SESSION_COOKIE) is None
    assert _response_cookie(response, HTTP_SESSION_COOKIE) is not None


def test_safe_return_path_blocks_open_redirects():
    assert safe_return_path("/settings") == "/settings"
    assert safe_return_path("/downloads?filter=error") == "/downloads?filter=error"
    assert safe_return_path("https://evil.example/") == "/"
    assert safe_return_path("http://evil.example/") == "/"
    assert safe_return_path("//evil.example/") == "/"
    assert safe_return_path("///evil.example/") == "/"
    assert safe_return_path("////evil.example/") == "/"
    assert safe_return_path("/\\evil.example/") == "/"
    assert safe_return_path("/login") == "/"


def test_login_route_remains_behind_general_cross_site_mutation_defense():
    main = (ROOT / "backend/main.py").read_text()
    middleware = (ROOT / "backend/auth/middleware.py").read_text()
    assert 'request.url.path not in _AUTH_MUTATION_PATHS' in main
    assert 'fetch_site == "cross-site"' in middleware
    assert 'Response(content="Forbidden origin", status_code=403)' in middleware
    assert 'app.include_router(auth_router)' in main


@pytest.mark.asyncio
async def test_open_mode_session_status_is_anonymous_and_login_is_bypassed(monkeypatch):
    cfg = _settings(enabled=False)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: cfg)
    request = _request("GET", path="/login", headers={"Host": "debridpulse.local"})
    response = await auth_routes.login_page(request, next="/stats")
    assert response.status_code == 303
    assert response.headers["location"] == "/stats"

    status_request = _request("GET", path="/api/auth/session", headers={"Host": "debridpulse.local"})
    status_request.state.principal = Principal.anonymous()
    data = await auth_routes.auth_session_status(status_request)
    assert data["authenticated"] is False
    assert data["csrf_token"] == ""


@pytest.mark.asyncio
async def test_session_mutation_requires_csrf_and_correct_token_passes(monkeypatch):
    import auth.middleware as middleware

    cfg = _settings()
    monkeypatch.setattr(middleware, "get_settings", lambda: cfg)
    session_store.clear()
    token, _ = session_store.create(
        Principal.password_session("operator"),
        lifetime_seconds=3600,
        credential_version=password_credential_version(cfg.auth_password_hash),
    )
    cookie = f"{HTTP_SESSION_COOKIE}={token}"

    missing = _request("POST", headers={"Cookie": cookie, "Host": "debridpulse.local"})
    assert (await enforce_authentication(missing, _ok)).status_code == 403

    valid = _request(
        "POST",
        headers={
            "Cookie": cookie,
            "Host": "debridpulse.local",
            "X-CSRF-Token": session_store.csrf_token(token),
        },
    )
    response = await enforce_authentication(valid, _ok)
    assert response.status_code == 200
    assert valid.state.principal.mechanism is AuthMechanism.PASSWORD_SESSION


@pytest.mark.asyncio
async def test_auth_session_status_returns_csrf_only_for_server_session():
    session_store.clear()
    token, _ = session_store.create(Principal.password_session("operator"), lifetime_seconds=3600)
    request = _request("GET", path="/api/auth/session", headers={"Host": "debridpulse.local"})
    request.state.principal = Principal.password_session("operator")
    request.state.auth_session_token = token
    data = await auth_routes.auth_session_status(request)
    assert data["authenticated"] is True
    assert data["mechanism"] == "password_session"
    assert data["csrf_token"] == session_store.csrf_token(token)
    assert data["session_expires_in_seconds"] > 0

    basic = _request("GET", path="/api/auth/session", headers={"Host": "debridpulse.local"})
    basic.state.principal = Principal.http_basic("operator")
    basic_data = await auth_routes.auth_session_status(basic)
    assert basic_data["csrf_token"] == ""
    assert basic_data["session_expires_in_seconds"] is None


@pytest.mark.asyncio
async def test_password_change_invalidates_existing_browser_session(monkeypatch):
    import auth.middleware as middleware

    old_cfg = _settings(password="old-secret")
    new_cfg = _settings(password="new-secret")
    monkeypatch.setattr(middleware, "get_settings", lambda: new_cfg)
    session_store.clear()
    token, _ = session_store.create(
        Principal.password_session("operator"),
        lifetime_seconds=3600,
        credential_version=password_credential_version(old_cfg.auth_password_hash),
    )
    request = _request(
        "GET",
        path="/",
        headers={
            "Cookie": f"{HTTP_SESSION_COOKIE}={token}",
            "Host": "debridpulse.local",
            "Accept": "text/html",
        },
    )
    response = await enforce_authentication(request, _ok)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")
    assert session_store.resolve(token) is None


@pytest.mark.asyncio
async def test_login_page_clears_stale_password_session(monkeypatch):
    new_cfg = _settings(password="new-secret")
    old_cfg = _settings(password="old-secret")
    monkeypatch.setattr(auth_routes, "get_settings", lambda: new_cfg)
    session_store.clear()
    login_csrf_store.clear()
    token, _ = session_store.create(
        Principal.password_session("operator"),
        lifetime_seconds=3600,
        credential_version=password_credential_version(old_cfg.auth_password_hash),
    )
    request = _request(
        "GET",
        path="/login",
        headers={"Host": "debridpulse.local", "Cookie": f"{HTTP_SESSION_COOKIE}={token}"},
    )
    response = await auth_routes.login_page(request, next="/stats")
    assert response.status_code == 200
    assert session_store.resolve(token) is None
    expired = _response_cookie(response, HTTP_SESSION_COOKIE)
    assert expired is not None
    assert expired["max-age"] == "0"


@pytest.mark.asyncio
async def test_browser_navigation_redirects_but_api_get_is_json_401(monkeypatch):
    import auth.middleware as middleware

    cfg = _settings()
    monkeypatch.setattr(middleware, "get_settings", lambda: cfg)

    browser = _request(
        "GET",
        path="/stats",
        headers={"Host": "debridpulse.local", "Accept": "text/html"},
    )
    browser_response = await enforce_authentication(browser, _ok)
    assert browser_response.status_code == 303
    assert browser_response.headers["location"] == "/login?next=%2Fstats"

    api_request = _request(
        "GET",
        path="/api/stats",
        headers={"Host": "debridpulse.local", "Accept": "application/json"},
    )
    api_response = await enforce_authentication(api_request, _ok)
    assert api_response.status_code == 401
    assert "WWW-Authenticate" not in api_response.headers
    assert b'"detail":"Unauthorized"' in api_response.body


@pytest.mark.asyncio
async def test_machine_basic_mutation_does_not_require_browser_csrf(monkeypatch):
    import auth.middleware as middleware

    cfg = _settings()
    monkeypatch.setattr(middleware, "get_settings", lambda: cfg)
    password_failure_throttle.clear()
    request = _request(
        "POST",
        headers={
            "Host": "debridpulse.local",
            "Authorization": _basic("operator", "secret"),
        },
    )
    response = await enforce_authentication(request, _ok)
    assert response.status_code == 200
    assert request.state.principal.mechanism is AuthMechanism.HTTP_BASIC


@pytest.mark.asyncio
async def test_password_login_creates_rotated_application_session(monkeypatch):
    cfg = _settings(lifetime=1)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: cfg)
    password_failure_throttle.clear()
    session_store.clear()
    login_csrf_store.clear()

    old_token, _ = session_store.create(
        Principal.password_session("operator"),
        lifetime_seconds=3600,
        credential_version=password_credential_version(cfg.auth_password_hash),
    )
    browser_nonce, csrf = login_csrf_store.issue()
    request = _form_request(
        "/login",
        {
            "username": "operator",
            "password": "secret",
            "csrf_token": csrf,
            "next": "/stats?window=24",
        },
        cookie=(
            f"debridpulse-login-csrf={browser_nonce}; "
            f"{HTTP_SESSION_COOKIE}={old_token}"
        ),
    )
    response = await auth_routes.password_login(request)
    assert response.status_code == 303
    assert response.headers["location"] == "/stats?window=24"
    assert session_store.resolve(old_token) is None

    issued = _response_cookie(response, HTTP_SESSION_COOKIE)
    assert issued is not None
    assert issued.value != old_token
    record = session_store.resolve(issued.value)
    assert record is not None
    assert record.principal.mechanism is AuthMechanism.PASSWORD_SESSION
    assert record.credential_version == password_credential_version(cfg.auth_password_hash)


@pytest.mark.asyncio
async def test_login_csrf_reuse_is_rejected(monkeypatch):
    cfg = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: cfg)
    password_failure_throttle.clear()
    session_store.clear()
    login_csrf_store.clear()

    browser_nonce, csrf = login_csrf_store.issue()
    cookie = f"debridpulse-login-csrf={browser_nonce}"
    first = _form_request(
        "/login",
        {"username": "operator", "password": "wrong", "csrf_token": csrf, "next": "/"},
        cookie=cookie,
    )
    first_response = await auth_routes.password_login(first)
    assert first_response.status_code == 401

    reused = _form_request(
        "/login",
        {"username": "operator", "password": "secret", "csrf_token": csrf, "next": "/"},
        cookie=cookie,
    )
    reused_response = await auth_routes.password_login(reused)
    assert reused_response.status_code == 403
    assert session_store.size == 0


@pytest.mark.asyncio
async def test_logout_revokes_server_session_and_expires_cookie():
    session_store.clear()
    token, _ = session_store.create(Principal.password_session("operator"), lifetime_seconds=3600)
    request = _request("POST", path="/api/auth/logout", headers={"Host": "debridpulse.local"})
    request.state.principal = Principal.password_session("operator")
    request.state.auth_session_token = token

    response = await auth_routes.logout(request)
    assert response.status_code == 200
    assert session_store.resolve(token) is None
    expired = _response_cookie(response, HTTP_SESSION_COOKIE)
    assert expired is not None
    assert expired["max-age"] == "0"
