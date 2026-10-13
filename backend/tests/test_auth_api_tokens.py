import base64
import json
import os
from http.cookies import SimpleCookie
from types import SimpleNamespace

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse

from account_support import account, account_database, admin_with_password
from api import auth_config_routes
from auth import middleware
from auth.api_tokens import API_TOKEN_PREFIX, ApiTokenStore
from auth.models import AuthMechanism, Principal
from auth.sessions import session_store


def _settings(**updates):
    values = {
        "auth_password_enabled": True,
        "auth_oidc_enabled": False,
        "oidc_issuer_url": "https://id.example/application/o/debridpulse",
        "oidc_client_id": "client",
        "oidc_client_secret": "secret",
        "oidc_scopes": ["openid", "profile", "email"],
        "oidc_allow_all": True,
        "oidc_allowed_subjects": [],
        "oidc_allowed_emails": [],
        "oidc_allowed_groups": [],
        "oidc_group_claim": "groups",
        "public_base_url": "https://pulse.example",
    }
    values.update(updates)
    return SimpleNamespace(**values)


def _request(*, path="/api/stats", headers=None):
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
            "query_string": b"",
            "headers": raw_headers,
            "client": ("127.0.0.1", 12345),
            "server": ("pulse.example", 80),
        }
    )


def _json(response):
    return json.loads(response.body.decode())


def test_api_token_store_persists_only_verifier_and_rotation_invalidates_old_token(tmp_path):
    path = tmp_path / "api-token.json"
    store = ApiTokenStore(path)

    first = store.generate()
    assert first.startswith(API_TOKEN_PREFIX)
    assert store.enabled is True
    assert store.configured is True
    assert store.verify(first) is True
    assert store.verify(first + "x") is False

    persisted = path.read_text(encoding="utf-8")
    assert first not in persisted
    data = json.loads(persisted)
    assert data["verifier"] == store.verifier_for(first)
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600

    second = store.generate()
    assert second != first
    assert store.verify(first) is False
    assert store.verify(second) is True

    store.set_enabled(False)
    assert store.configured is True
    assert store.verify(second) is False
    store.set_enabled(True)
    assert store.verify(second) is True

    store.clear()
    assert store.enabled is False
    assert store.configured is False
    assert store.verify(second) is False
    assert path.exists() is False


def test_api_token_store_cannot_enable_without_generated_verifier(tmp_path):
    store = ApiTokenStore(tmp_path / "api-token.json")
    with pytest.raises(ValueError):
        store.set_enabled(True)


@pytest.mark.asyncio
async def test_valid_bearer_admits_oidc_only_machine_request(monkeypatch, tmp_path):
    store = ApiTokenStore(tmp_path / "api-token.json")
    token = store.generate()
    cfg = _settings(auth_password_enabled=False, auth_oidc_enabled=True)
    monkeypatch.setattr(middleware, "get_settings", lambda: cfg)
    monkeypatch.setattr(middleware, "api_token_store", store)
    monkeypatch.setattr(middleware, "_oidc_ready", lambda _cfg: True)
    seen = []

    async def call_next(request):
        seen.append(request.state.principal)
        return JSONResponse({"ok": True})

    response = await middleware.enforce_authentication(
        _request(headers={"Authorization": f"Bearer {token}"}),
        call_next,
    )
    assert response.status_code == 200
    assert seen[0].mechanism is AuthMechanism.API_TOKEN
    assert seen[0].subject == "api-token"


@pytest.mark.asyncio
async def test_invalid_explicit_bearer_is_rejected_when_authentication_is_required(monkeypatch, tmp_path):
    store = ApiTokenStore(tmp_path / "api-token.json")
    store.generate()
    cfg = _settings(auth_password_enabled=False, auth_oidc_enabled=True)
    monkeypatch.setattr(middleware, "get_settings", lambda: cfg)
    monkeypatch.setattr(middleware, "api_token_store", store)
    monkeypatch.setattr(middleware, "_oidc_ready", lambda _cfg: True)

    async def call_next(_request):
        raise AssertionError("invalid bearer must not reach application")

    response = await middleware.enforce_authentication(
        _request(headers={"Authorization": "Bearer definitely-wrong"}),
        call_next,
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_api_token_does_not_turn_open_mode_into_token_only_mode(monkeypatch, tmp_path):
    store = ApiTokenStore(tmp_path / "api-token.json")
    store.generate()
    cfg = _settings(auth_password_enabled=False, auth_oidc_enabled=False)
    monkeypatch.setattr(middleware, "get_settings", lambda: cfg)
    monkeypatch.setattr(middleware, "api_token_store", store)
    seen = []

    async def call_next(request):
        seen.append(request.state.principal)
        return JSONResponse({"ok": True})

    response = await middleware.enforce_authentication(
        _request(headers={"Authorization": "Bearer wrong-but-open"}),
        call_next,
    )
    assert response.status_code == 200
    assert seen[0].authenticated is False


@pytest.mark.asyncio
async def test_application_session_precedes_unexpected_bearer_header(monkeypatch, tmp_path):
    store = ApiTokenStore(tmp_path / "api-token.json")
    store.generate()
    cfg = _settings()
    monkeypatch.setattr(middleware, "get_settings", lambda: cfg)
    monkeypatch.setattr(middleware, "api_token_store", store)
    session_store.clear()
    token, _record = session_store.create(
        Principal.password_session(account("operator")),
        lifetime_seconds=3600,
    )
    seen = []

    async def call_next(request):
        seen.append(request.state.principal)
        return JSONResponse({"ok": True})

    response = await middleware.enforce_authentication(
        _request(
            headers={
                "Cookie": f"debridpulse-session={token}",
                "Authorization": "Bearer invalid-and-must-be-ignored",
            }
        ),
        call_next,
    )
    assert response.status_code == 200
    assert seen[0].mechanism is AuthMechanism.PASSWORD_SESSION


@pytest.mark.asyncio
async def test_http_basic_remains_available_with_api_token_configured(monkeypatch, tmp_path):
    store = ApiTokenStore(tmp_path / "api-token.json")
    store.generate()
    cfg = _settings()
    monkeypatch.setattr(middleware, "get_settings", lambda: cfg)
    monkeypatch.setattr(middleware, "api_token_store", store)
    await account_database(tmp_path, monkeypatch)
    await admin_with_password("operator", "secret")
    seen = []

    async def call_next(request):
        seen.append(request.state.principal)
        return JSONResponse({"ok": True})

    encoded = base64.b64encode(b"operator:secret").decode()
    response = await middleware.enforce_authentication(
        _request(headers={"Authorization": f"Basic {encoded}"}),
        call_next,
    )
    assert response.status_code == 200
    assert seen[0].mechanism is AuthMechanism.HTTP_BASIC


@pytest.mark.asyncio
async def test_api_token_management_returns_raw_token_once_and_never_from_status(monkeypatch, tmp_path):
    store = ApiTokenStore(tmp_path / "api-token.json")
    monkeypatch.setattr(auth_config_routes, "api_token_store", store)

    generated = await auth_config_routes.generate_or_rotate_api_token()
    generated_body = _json(generated)
    first = generated_body["token"]
    assert first.startswith(API_TOKEN_PREFIX)
    assert generated_body["rotated"] is False
    assert generated.headers["cache-control"] == "no-store"

    status = await auth_config_routes.api_token_status()
    status_body = _json(status)
    assert status_body == {"enabled": True, "configured": True}
    assert first not in status.body.decode()
    assert "verifier" not in status.body.decode()

    rotated = await auth_config_routes.generate_or_rotate_api_token()
    rotated_body = _json(rotated)
    second = rotated_body["token"]
    assert rotated_body["rotated"] is True
    assert second != first
    assert store.verify(first) is False
    assert store.verify(second) is True

    disabled = await auth_config_routes.set_api_token_enabled(
        auth_config_routes.ApiTokenEnableRequest(enabled=False)
    )
    assert _json(disabled)["enabled"] is False
    assert store.configured is True

    enabled = await auth_config_routes.set_api_token_enabled(
        auth_config_routes.ApiTokenEnableRequest(enabled=True)
    )
    assert _json(enabled)["enabled"] is True
    assert store.verify(second) is True

    cleared = await auth_config_routes.clear_api_token()
    assert _json(cleared) == {"ok": True, "enabled": False, "configured": False}
    assert store.verify(second) is False
