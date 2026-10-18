"""Integration-account API tokens: the only bearer credential (1.0.13 ACL).

The pre-account global token is retired; a token belongs to one Integration
account, authenticates it with its own role, is shown once and is revoked at
once."""
import base64
import json
import sqlite3
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import Request
from fastapi.responses import JSONResponse

from account_support import account, account_database, admin_with_password
from auth import accounts, middleware
from auth.accounts import DEFAULT_INTEGRATION_ID, TOKEN_PREFIX, AccountRefused, Role
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
    # Every HTTP/1.1 request names its authority: the one this scope serves.
    raw_headers = [(b"host", b"pulse.example")] + [
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


@pytest_asyncio.fixture
async def installation(tmp_path, monkeypatch):
    path = await account_database(tmp_path, monkeypatch)
    monkeypatch.setattr(middleware, "get_settings", lambda: _settings())
    return path


async def _admit(headers):
    seen = []

    async def call_next(request):
        seen.append(request.state.principal)
        return JSONResponse({"ok": True})

    response = await middleware.enforce_authentication(_request(headers=headers), call_next)
    return response, seen


@pytest.mark.asyncio
async def test_a_token_is_shown_once_stored_as_a_verifier_and_authenticates_its_integration(installation):
    record, raw = await accounts.create_token(DEFAULT_INTEGRATION_ID, "scraper")

    assert raw.startswith(TOKEN_PREFIX)
    with sqlite3.connect(installation) as conn:
        stored = conn.execute("SELECT * FROM account_tokens").fetchall()
    assert raw not in repr(stored)
    assert "token" not in json.dumps(await accounts.list_tokens(DEFAULT_INTEGRATION_ID))
    response, seen = await _admit({"Authorization": f"Bearer {raw}"})
    assert response.status_code == 200
    assert seen[0].mechanism is AuthMechanism.API_TOKEN
    assert (seen[0].account_id, seen[0].role, seen[0].credential_id) == (
        DEFAULT_INTEGRATION_ID, Role.INTEGRATION.value, record["id"])


@pytest.mark.asyncio
async def test_revocation_and_account_disable_end_a_token_at_once(installation):
    _record, first = await accounts.create_token(DEFAULT_INTEGRATION_ID, "one")
    record, second = await accounts.create_token(DEFAULT_INTEGRATION_ID, "two")
    assert await accounts.revoke_token(DEFAULT_INTEGRATION_ID, record["id"]) is True
    assert (await _admit({"Authorization": f"Bearer {second}"}))[0].status_code == 401
    assert (await _admit({"Authorization": f"Bearer {first}"}))[0].status_code == 200

    await accounts.set_enabled("someone-else", DEFAULT_INTEGRATION_ID, False)
    assert (await _admit({"Authorization": f"Bearer {first}"}))[0].status_code == 401


@pytest.mark.asyncio
async def test_a_revoked_token_leaves_the_active_list_and_keeps_its_row_and_attribution(installation):
    kept, _ = await accounts.create_token(DEFAULT_INTEGRATION_ID, "kept")
    retired, raw = await accounts.create_token(DEFAULT_INTEGRATION_ID, "retired")
    assert [item["id"] for item in await accounts.list_tokens(DEFAULT_INTEGRATION_ID)] == [kept["id"], retired["id"]]

    assert await accounts.revoke_token(DEFAULT_INTEGRATION_ID, retired["id"]) is True

    assert [item["id"] for item in await accounts.list_tokens(DEFAULT_INTEGRATION_ID)] == [kept["id"]]
    assert (await _admit({"Authorization": f"Bearer {raw}"}))[0].status_code == 401
    assert await accounts.revoke_token(DEFAULT_INTEGRATION_ID, retired["id"]) is False   # final, once
    with sqlite3.connect(installation) as conn:
        assert conn.execute("SELECT label, revoked_at IS NOT NULL FROM account_tokens WHERE id=?",
                            (retired["id"],)).fetchone() == ("retired", 1)
    assert (await accounts.requester_identities([], [retired["id"]]))[1] == {retired["id"]: "retired"}


@pytest.mark.asyncio
async def test_only_integration_accounts_hold_tokens(installation):
    operator = await admin_with_password()
    with pytest.raises(AccountRefused):
        await accounts.create_token(operator.id, "human")
    response, _seen = await _admit({"Authorization": "Bearer dp_not-a-token"})
    assert response.status_code == 401 and response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_the_global_token_is_retired_and_never_authenticates(installation, tmp_path, monkeypatch):
    from db import event_journal

    legacy = tmp_path / "api-token.json"
    legacy.write_text(json.dumps({"enabled": True, "verifier": "a" * 64}), encoding="utf-8")
    monkeypatch.setattr(event_journal, "record_now", _recorded := _Recorder())

    assert await accounts.retire_legacy_api_token(legacy) is True
    assert not legacy.exists()
    assert _recorded.events[0].event_type == "administration.api_token_retired"
    assert await accounts.retire_legacy_api_token(legacy) is False


class _Recorder:
    def __init__(self):
        self.events = []

    async def __call__(self, event):
        self.events.append(event)
        return True


@pytest.mark.asyncio
async def test_application_session_precedes_unexpected_bearer_header(installation):
    session_store.clear()
    token, _record = session_store.create(Principal.password_session(account("operator")), lifetime_seconds=3600)
    response, seen = await _admit({
        "Cookie": f"debridpulse-session={token}",
        "Authorization": "Bearer invalid-and-must-be-ignored",
    })
    assert response.status_code == 200
    assert seen[0].mechanism is AuthMechanism.PASSWORD_SESSION


@pytest.mark.asyncio
async def test_http_basic_remains_available_beside_integration_tokens(installation):
    await accounts.create_token(DEFAULT_INTEGRATION_ID, "scraper")
    await admin_with_password("operator", "secret")
    encoded = base64.b64encode(b"operator:secret").decode()
    response, seen = await _admit({"Authorization": f"Basic {encoded}"})
    assert response.status_code == 200
    assert seen[0].mechanism is AuthMechanism.HTTP_BASIC


@pytest.mark.asyncio
async def test_open_mode_admits_a_valid_token_or_no_credential_but_never_an_invalid_bearer(installation, monkeypatch):
    monkeypatch.setattr(middleware, "get_settings", lambda: _settings(auth_password_enabled=False))
    record, raw = await accounts.create_token(DEFAULT_INTEGRATION_ID, "scraper")
    await accounts.revoke_token(DEFAULT_INTEGRATION_ID, record["id"])
    _record, valid = await accounts.create_token(DEFAULT_INTEGRATION_ID, "live")

    response, seen = await _admit({"Authorization": f"Bearer {valid}"})
    assert response.status_code == 200 and seen[0].role == Role.INTEGRATION.value
    for invalid in ("dp_never-issued", raw, "not-even-a-dp-token"):
        response, seen = await _admit({"Authorization": f"Bearer {invalid}"})
        assert response.status_code == 401 and seen == []
    response, seen = await _admit({})
    assert response.status_code == 200 and seen[0] == Principal.standalone()
