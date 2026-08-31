from http.cookies import SimpleCookie
from types import SimpleNamespace

import pytest
from fastapi import Request

from api import auth_routes
from auth.models import AuthMechanism, Principal
from auth.oidc import OIDC_CORRELATION_COOKIE
from auth.oidc_version import oidc_configuration_version
from auth.passwords import hash_password
from auth.sessions import HTTPS_SESSION_COOKIE, session_store


def _settings(**updates):
    values = {
        "auth_password_enabled": True,
        "auth_username": "operator",
        "auth_password_hash": hash_password("secret"),
        "auth_oidc_enabled": True,
        "oidc_provider_name": "Authentik",
        "oidc_issuer_url": "https://id.example/application/o/debridpulse",
        "oidc_client_id": "debridpulse-client",
        "oidc_client_secret": "secret",
        "oidc_scopes": ["openid", "profile", "email"],
        "oidc_allow_all": True,
        "oidc_allowed_subjects": [],
        "oidc_allowed_emails": [],
        "oidc_allowed_groups": [],
        "oidc_group_claim": "groups",
        "public_base_url": "https://pulse.example",
        "auth_session_lifetime_hours": 12,
    }
    values.update(updates)
    return SimpleNamespace(**values)


def _request(path="/login", *, headers=None, query_string=b""):
    raw_headers = [
        (str(key).lower().encode("latin-1"), str(value).encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query_string,
            "headers": raw_headers,
            "client": ("127.0.0.1", 12345),
            "server": ("pulse.example", 80),
        }
    )


def _response_cookie(response, name):
    for key, value in response.raw_headers:
        if key.lower() != b"set-cookie":
            continue
        parsed = SimpleCookie()
        parsed.load(value.decode("latin-1"))
        if name in parsed:
            return parsed[name]
    return None


@pytest.mark.asyncio
async def test_unified_login_shows_password_and_oidc_when_both_enabled(monkeypatch):
    cfg = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: cfg)
    request = _request(headers={"Host": "pulse.example"})
    response = await auth_routes.login_page(request, next="/stats")
    body = response.body.decode()
    assert response.status_code == 200
    assert 'name="username"' in body
    assert 'name="password"' in body
    assert "Sign in with Authentik" in body
    assert "/auth/oidc/start?next=%2Fstats" in body


@pytest.mark.asyncio
async def test_oidc_only_uses_local_landing_page_without_password_or_auto_redirect(monkeypatch):
    cfg = _settings(auth_password_enabled=False, auth_username="", auth_password_hash="")
    monkeypatch.setattr(auth_routes, "get_settings", lambda: cfg)
    request = _request(headers={"Host": "pulse.example"})
    response = await auth_routes.login_page(request, next="/")
    body = response.body.decode()
    assert response.status_code == 200
    assert "Sign in with Authentik" in body
    assert 'name="username"' not in body
    assert 'name="password"' not in body
    assert "location" not in response.headers


@pytest.mark.asyncio
async def test_oidc_start_sets_secure_bound_correlation_cookie(monkeypatch):
    cfg = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: cfg)

    async def fake_begin(_cfg, *, return_to):
        assert _cfg is cfg
        assert return_to == "/downloads?filter=ready"
        return "https://id.example/authorize?state=opaque", "browser-correlation"

    monkeypatch.setattr(auth_routes, "begin_oidc_login", fake_begin)
    request = _request("/auth/oidc/start", headers={"Host": "pulse.example"})
    response = await auth_routes.oidc_start(request, next="/downloads?filter=ready")
    assert response.status_code == 303
    assert response.headers["location"].startswith("https://id.example/authorize")
    cookie = _response_cookie(response, OIDC_CORRELATION_COOKIE)
    assert cookie is not None
    assert cookie.value == "browser-correlation"
    assert cookie["secure"] is True
    assert cookie["httponly"] is True
    assert cookie["samesite"].lower() == "lax"
    assert cookie["path"] == "/"
    assert not cookie["domain"]


@pytest.mark.asyncio
async def test_oidc_callback_rotates_old_session_and_forces_secure_application_cookie(monkeypatch):
    cfg = _settings()
    config_version = oidc_configuration_version(cfg)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: cfg)
    session_store.clear()
    old_token, _ = session_store.create(
        Principal.password_session("operator"),
        lifetime_seconds=3600,
        credential_version="old",
    )

    async def fake_complete(*, state, code, correlation):
        assert state == "state"
        assert code == "code"
        assert correlation == "browser-correlation"
        return (
            Principal.oidc_session(
                "https://id.example/application/o/debridpulse|user-1",
                display_name="Operator",
                credential_version=config_version,
            ),
            "/stats",
        )

    monkeypatch.setattr(auth_routes, "complete_oidc_login", fake_complete)
    request = _request(
        "/auth/oidc/callback",
        headers={
            "Host": "pulse.example",
            "Cookie": (
                f"debridpulse-session={old_token}; "
                f"{OIDC_CORRELATION_COOKIE}=browser-correlation"
            ),
        },
    )
    response = await auth_routes.oidc_callback(request, state="state", code="code")
    assert response.status_code == 303
    assert response.headers["location"] == "/stats"
    assert session_store.resolve(old_token) is None

    cookie = _response_cookie(response, HTTPS_SESSION_COOKIE)
    assert cookie is not None
    assert cookie.value != old_token
    assert cookie["secure"] is True
    assert cookie["httponly"] is True
    assert cookie["samesite"].lower() == "lax"
    record = session_store.resolve(cookie.value)
    assert record is not None
    assert record.principal.mechanism is AuthMechanism.OIDC_SESSION
    assert record.credential_version == config_version

    correlation = _response_cookie(response, OIDC_CORRELATION_COOKIE)
    assert correlation is not None
    assert correlation["max-age"] == "0"


@pytest.mark.asyncio
async def test_oidc_callback_rejects_proof_from_stale_configuration(monkeypatch):
    current = _settings(oidc_allowed_groups=["new-policy"])
    old = _settings(oidc_allowed_groups=["old-policy"])
    monkeypatch.setattr(auth_routes, "get_settings", lambda: current)
    session_store.clear()

    async def fake_complete(**_kwargs):
        return (
            Principal.oidc_session(
                "https://id.example/application/o/debridpulse|user-1",
                credential_version=oidc_configuration_version(old),
            ),
            "/stats",
        )

    monkeypatch.setattr(auth_routes, "complete_oidc_login", fake_complete)
    response = await auth_routes.oidc_callback(
        _request(
            "/auth/oidc/callback",
            headers={
                "Host": "pulse.example",
                "Cookie": f"{OIDC_CORRELATION_COOKIE}=browser-correlation",
            },
        ),
        state="state",
        code="code",
    )
    assert response.status_code == 409
    assert session_store.size == 0
    assert b"configuration changed" in response.body


@pytest.mark.asyncio
async def test_oidc_callback_failure_does_not_issue_application_session(monkeypatch):
    cfg = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: cfg)
    session_store.clear()

    async def fail_complete(**_kwargs):
        from auth.oidc import OidcProtocolError

        raise OidcProtocolError("private detail must not escape")

    monkeypatch.setattr(auth_routes, "complete_oidc_login", fail_complete)
    request = _request(
        "/auth/oidc/callback",
        headers={
            "Host": "pulse.example",
            "Cookie": f"{OIDC_CORRELATION_COOKIE}=browser-correlation",
        },
    )
    response = await auth_routes.oidc_callback(request, state="state", code="code")
    body = response.body.decode()
    assert response.status_code == 401
    assert "private detail" not in body
    assert "could not be validated or authorized" in body
    assert _response_cookie(response, HTTPS_SESSION_COOKIE) is None
    correlation = _response_cookie(response, OIDC_CORRELATION_COOKIE)
    assert correlation is not None
    assert correlation["max-age"] == "0"
