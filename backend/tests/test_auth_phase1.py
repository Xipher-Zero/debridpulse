import base64
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import Request, Response

from account_support import account, account_database, admin_with_password
from auth.middleware import enforce_general_web_security, enforce_password_http_auth
from auth.models import AuthMechanism, Principal
from auth.policy import is_public_path
from auth.throttle import password_failure_throttle


def _request(method="GET", path="/api/stats", headers=None):
    raw_headers = []
    for key, value in (headers or {}).items():
        raw_headers.append((str(key).lower().encode("latin-1"), str(value).encode("latin-1")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("debridpulse.local", 80),
    }
    return Request(scope)


async def _ok(_request):
    return Response(content="ok", status_code=200)


def _basic(username, password):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


def _password_settings(*, enabled=True):
    return SimpleNamespace(auth_password_enabled=enabled)


@pytest_asyncio.fixture
async def operator(tmp_path, monkeypatch):
    """The default Admin, signing in as operator/secret."""
    await account_database(tmp_path, monkeypatch)
    return await admin_with_password()


def test_principal_model_and_phase1_route_policy():
    anonymous = Principal.anonymous()
    assert anonymous.authenticated is False
    assert anonymous.mechanism is None

    basic = Principal.http_basic(account("operator"))
    assert basic.authenticated is True
    assert basic.mechanism is AuthMechanism.HTTP_BASIC
    assert basic.subject == "operator"
    assert basic.account_id and basic.role == "admin"

    assert is_public_path("/api/health") is True
    assert is_public_path("/api/version") is True
    assert is_public_path("/api/avatar") is True
    assert is_public_path("/api/stats") is False


@pytest.mark.asyncio
async def test_general_browser_security_rejects_cross_site_mutation_in_open_mode():
    request = _request(
        "POST",
        headers={
            "Host": "debridpulse.local",
            "Origin": "https://evil.example",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    response = await enforce_general_web_security(request, _ok)
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_general_browser_security_allows_machine_and_same_origin_mutations():
    machine = _request("POST", headers={"Host": "debridpulse.local"})
    assert (await enforce_general_web_security(machine, _ok)).status_code == 200

    same_origin = _request(
        "POST",
        headers={
            "Host": "debridpulse.local",
            "Origin": "http://debridpulse.local",
            "Sec-Fetch-Site": "same-origin",
        },
    )
    assert (await enforce_general_web_security(same_origin, _ok)).status_code == 200


@pytest.mark.asyncio
async def test_general_browser_security_preserves_explicit_cors_origin():
    request = _request(
        "POST",
        headers={
            "Host": "debridpulse.local",
            "Origin": "https://automation.example",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    response = await enforce_general_web_security(
        request,
        _ok,
        allowed_origins=["https://automation.example"],
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_general_browser_security_requires_exact_configured_cors_origin():
    request = _request(
        "POST",
        headers={
            "Host": "debridpulse.local",
            "Origin": "http://automation.example",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    response = await enforce_general_web_security(
        request,
        _ok,
        allowed_origins=["https://automation.example"],
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_password_http_auth_sets_common_principal(operator, monkeypatch):
    import auth.middleware as middleware

    cfg = _password_settings()
    monkeypatch.setattr(middleware, "get_settings", lambda: cfg)
    request = _request(
        "GET",
        headers={
            "Host": "debridpulse.local",
            "Authorization": _basic("operator", "secret"),
        },
    )
    response = await enforce_password_http_auth(request, _ok)
    assert response.status_code == 200
    assert request.state.principal.authenticated is True
    assert request.state.principal.mechanism is AuthMechanism.HTTP_BASIC
    assert request.state.principal.subject == "operator"
    assert request.state.principal.account_id == operator.id


@pytest.mark.asyncio
async def test_password_http_auth_rejects_bad_and_malformed_credentials(operator, monkeypatch):
    import auth.middleware as middleware

    monkeypatch.setattr(middleware, "get_settings", lambda: _password_settings())
    password_failure_throttle.clear()

    wrong = _request("GET", headers={"Authorization": _basic("operator", "wrong")})
    wrong_response = await enforce_password_http_auth(wrong, _ok)
    assert wrong_response.status_code == 401
    assert wrong_response.headers["WWW-Authenticate"].startswith("Basic realm=")

    malformed = _request("GET", headers={"Authorization": "Basic !!!not-base64!!!"})
    malformed_response = await enforce_password_http_auth(malformed, _ok)
    assert malformed_response.status_code == 401


@pytest.mark.asyncio
async def test_enabled_password_without_any_credential_fails_closed(tmp_path, monkeypatch):
    import auth.middleware as middleware

    await account_database(tmp_path, monkeypatch)
    monkeypatch.setattr(middleware, "get_settings", lambda: _password_settings(enabled=True))
    request = _request("GET", path="/api/stats")
    response = await enforce_password_http_auth(request, _ok)
    assert response.status_code == 401
    basic = _request("GET", path="/api/stats", headers={"Authorization": _basic("operator", "")})
    assert (await enforce_password_http_auth(basic, _ok)).status_code == 401


@pytest.mark.asyncio
async def test_public_routes_and_open_mode_remain_admitted(monkeypatch):
    import auth.middleware as middleware

    monkeypatch.setattr(middleware, "get_settings", lambda: _password_settings())
    public = _request("GET", path="/api/health")
    assert (await enforce_password_http_auth(public, _ok)).status_code == 200
    assert public.state.principal.authenticated is False

    monkeypatch.setattr(
        middleware,
        "get_settings",
        lambda: _password_settings(enabled=False),
    )
    open_request = _request("GET", path="/api/stats", headers={"Host": "debridpulse.local"})
    assert (await enforce_password_http_auth(open_request, _ok)).status_code == 200
    # Open mode is the standalone installation's one Admin, not an anonymous tenant.
    assert open_request.state.principal.authenticated is False
    assert open_request.state.principal == Principal.standalone()
    assert open_request.state.principal.role == "admin"


def test_main_only_installs_auth_boundary():
    main = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    assert "enforce_authentication" in main
    assert "enforce_general_web_security" in main
    assert "base64.b64decode" not in main
    assert "WWW-Authenticate" not in main
    assert "Forbidden origin" not in main
