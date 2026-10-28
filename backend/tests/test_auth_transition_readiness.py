import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import Request

from account_support import account_database, admin_with_password
from auth.accounts import DEFAULT_ADMIN_ID
from auth.models import Principal
from auth.transitions import settings_transition_rejection


def _settings(**updates):
    values = {
        "auth_password_enabled": False,
        "auth_oidc_enabled": False,
        "oidc_provider_name": "OpenID Connect",
        "oidc_issuer_url": "",
        "oidc_client_id": "",
        "oidc_client_secret": "",
        "oidc_scopes": ["openid", "profile", "email"],
        "oidc_allow_all": False,
        "oidc_allowed_subjects": [],
        "oidc_allowed_emails": [],
        "oidc_allowed_groups": [],
        "oidc_group_claim": "groups",
        "public_base_url": "",
    }
    values.update(updates)
    return SimpleNamespace(**values)


def _request(payload, path="/api/settings"):
    body = json.dumps(payload).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "PUT",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("pulse.example", 443),
        },
        receive=receive,
    )


@pytest_asyncio.fixture(autouse=True)
async def _accounts(tmp_path, monkeypatch):
    """A fresh installation: the default accounts, and no password anywhere."""
    await account_database(tmp_path, monkeypatch)


@pytest.mark.asyncio
async def test_legacy_settings_cannot_enable_incomplete_password_as_only_auth():
    response = await settings_transition_rejection(
        _request({"auth_password_enabled": True, "auth_username": "operator"}),
        Principal.standalone(),
        _settings(),
    )
    assert response is not None
    assert response.status_code == 409
    assert b"locally usable" in response.body


@pytest.mark.asyncio
async def test_malformed_stored_password_hash_cannot_become_sole_auth():
    from db.database import get_db

    async with get_db() as db:
        await db.execute(
            "INSERT INTO account_local_credentials(account_id,password_hash,updated_at) "
            "VALUES(?,?,?)", (DEFAULT_ADMIN_ID, "$argon2id$configured-but-not-valid", "now"))
        await db.commit()
    response = await settings_transition_rejection(
        _request({"auth_password_enabled": True}),
        Principal.standalone(),
        _settings(),
    )
    assert response is not None
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_password_clear_intent_cannot_count_simultaneous_plaintext_as_fallback():
    operator = await admin_with_password("operator", "old-secret")
    current = _settings(
        auth_password_enabled=True,
        auth_oidc_enabled=True,
        oidc_issuer_url="https://id.example/application/o/debridpulse",
        oidc_client_id="client",
        oidc_allow_all=True,
        public_base_url="https://pulse.example",
    )
    response = await settings_transition_rejection(
        _request(
            {
                "auth_password_enabled": True,
                "auth_password": "new-but-discarded",
                "clear_password": True,
                "auth_oidc_enabled": False,
            },
            path="/api/auth/config",
        ),
        Principal.password_session(operator),
        current,
    )
    assert response is not None
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_dedicated_settings_cannot_enable_deny_everyone_oidc_as_only_auth():
    payload = {
        "auth_oidc_enabled": True,
        "oidc_issuer_url": "https://id.example/application/o/debridpulse",
        "oidc_client_id": "debridpulse-client",
        "public_base_url": "https://pulse.example",
        "oidc_allow_all": False,
        "oidc_allowed_subjects": [],
        "oidc_allowed_emails": [],
        "oidc_allowed_groups": [],
    }
    response = await settings_transition_rejection(
        _request(payload, path="/api/auth/config"),
        Principal.standalone(),
        _settings(),
    )
    assert response is not None
    assert response.status_code == 409
    assert b"locally usable" in response.body


@pytest.mark.asyncio
async def test_open_mode_requires_password_fallback_before_first_oidc_only_mode():
    payload = {
        "auth_oidc_enabled": True,
        "oidc_issuer_url": "https://id.example/application/o/debridpulse",
        "oidc_client_id": "debridpulse-client",
        "public_base_url": "https://pulse.example",
        "oidc_allowed_groups": ["debridpulse-operators"],
    }
    response = await settings_transition_rejection(
        _request(payload),
        Principal.standalone(),
        _settings(),
    )
    assert response is not None
    assert response.status_code == 409
    assert b"verified fallback" in response.body


@pytest.mark.asyncio
async def test_open_mode_can_enable_password_fallback_and_oidc_together():
    payload = {
        "auth_password_enabled": True,
        "auth_username": "operator",
        "auth_password": "temporary-secret",
        "auth_oidc_enabled": True,
        "oidc_issuer_url": "https://id.example/application/o/debridpulse",
        "oidc_client_id": "debridpulse-client",
        "public_base_url": "https://pulse.example",
        "oidc_allowed_groups": ["debridpulse-operators"],
    }
    # In open mode the request acts as the standalone default Admin, whose own
    # password this same request sets.
    response = await settings_transition_rejection(
        _request(payload, path="/api/auth/config"),
        Principal.standalone(),
        _settings(),
    )
    assert response is None


@pytest.mark.asyncio
async def test_broken_supplemental_oidc_does_not_disable_working_password():
    operator = await admin_with_password()
    current = _settings(auth_password_enabled=True)
    response = await settings_transition_rejection(
        _request({"auth_oidc_enabled": True, "oidc_issuer_url": "not-https"}),
        Principal.password_session(operator),
        current,
    )
    assert response is None
