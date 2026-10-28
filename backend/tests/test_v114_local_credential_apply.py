"""DP 1.0.14: the signed-in account's local credential is changed WHOLE.

Settings -> Authentication -> Username & Password applies the username and the
password together, by one explicit Apply, through the one PUT /api/auth/config
writer. These prove the server's side of that contract with synthetic
credentials only:

* a username change without a new password is refused before anything is
  written -- the name is never re-bound to a stored password (and the
  account's sessions are not revoked by a refused request);
* a first credential that is a username alone is refused the same way;
* a username and password together commit as one change;
* a password alone replaces the password under the existing username;
* a persistence failure leaves the previous credential and settings intact.
"""
import json

import pytest
import pytest_asyncio

from account_support import account, account_database, admin_with_password
from api import auth_config_routes
from auth.accounts import DEFAULT_ADMIN_ID, local_credential, local_credential_status
from auth.models import Principal
from auth.passwords import verify_password
from core.config import AppSettings
from test_auth_settings_oidc_failure_containment import _install_persistence_stubs, _request

SENTINEL = "dp-synthetic-sentinel-7f3a91"


def _settings(**updates):
    return AppSettings(**{"auth_password_enabled": True, "auth_oidc_enabled": False,
                          "auth_session_lifetime_hours": 12, **updates})


async def _apply(monkeypatch, current, principal=None, **fields):
    saved, applied = _install_persistence_stubs(monkeypatch, current)
    revoked = []
    monkeypatch.setattr("auth.accounts._revoke", lambda ids: revoked.extend(ids))
    update = auth_config_routes.AuthenticationConfigUpdate(**fields)
    response = await auth_config_routes.update_authentication_config(
        _request(payload=update.model_dump(exclude_none=True), principal=principal), update)
    return response, saved, applied, revoked


async def _signs_in(username: str, password: str) -> bool:
    found = await local_credential(username)
    return found is not None and verify_password(found[1], password)


@pytest_asyncio.fixture(autouse=True)
async def _database(tmp_path, monkeypatch):
    await account_database(tmp_path, monkeypatch)


@pytest.mark.asyncio
async def test_a_username_change_without_a_new_password_is_refused_and_changes_nothing(monkeypatch):
    await admin_with_password("operator", f"{SENTINEL}-old")
    response, saved, applied, revoked = await _apply(monkeypatch, _settings(), auth_username="renamed")
    assert response.status_code == 400
    assert json.loads(response.body)["detail"] == "Enter a new password to change the username"
    assert (saved, applied, revoked) == ([], [], [])
    assert await local_credential_status(DEFAULT_ADMIN_ID) == ("operator", True)
    assert await _signs_in("operator", f"{SENTINEL}-old")
    assert await local_credential("renamed") is None


@pytest.mark.asyncio
async def test_a_first_credential_that_is_a_username_alone_is_refused(monkeypatch):
    # A fresh installation: the default Admin, no local credential, open mode.
    response, saved, _applied, _revoked = await _apply(
        monkeypatch, _settings(auth_password_enabled=False), principal=Principal.standalone(), auth_username="operator")
    assert response.status_code == 400
    assert saved == []
    # The default Admin's username is its own from the start; it stays without a password.
    assert await local_credential_status(DEFAULT_ADMIN_ID) == ("Administrator", False)


@pytest.mark.asyncio
async def test_a_username_and_password_commit_together(monkeypatch):
    await admin_with_password("operator", f"{SENTINEL}-old")
    response, saved, _applied, revoked = await _apply(
        monkeypatch, _settings(), auth_username="renamed", auth_password=f"{SENTINEL}-new")
    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["username"] == "renamed" and body["password_configured"] is True
    assert SENTINEL not in response.body.decode()
    assert len(saved) == 1 and revoked == [DEFAULT_ADMIN_ID]
    assert await _signs_in("renamed", f"{SENTINEL}-new")
    assert not await _signs_in("renamed", f"{SENTINEL}-old")
    assert await local_credential("operator") is None


@pytest.mark.asyncio
async def test_a_first_credential_of_both_is_set_in_open_mode(monkeypatch):
    response, _saved, _applied, _revoked = await _apply(
        monkeypatch, _settings(auth_password_enabled=False), principal=Principal.standalone(),
        auth_username="operator", auth_password=f"{SENTINEL}-first")
    assert response.status_code == 200
    assert await _signs_in("operator", f"{SENTINEL}-first")


@pytest.mark.asyncio
async def test_a_password_alone_replaces_it_under_the_same_username(monkeypatch):
    await admin_with_password("operator", f"{SENTINEL}-old")
    response, _saved, _applied, _revoked = await _apply(monkeypatch, _settings(), auth_password=f"{SENTINEL}-new")
    assert response.status_code == 200
    assert await _signs_in("operator", f"{SENTINEL}-new")
    # The unchanged username sent with it is no rename.
    response, *_ = await _apply(monkeypatch, _settings(), auth_username="operator", auth_password=f"{SENTINEL}-newer")
    assert response.status_code == 200
    assert await _signs_in("operator", f"{SENTINEL}-newer")


@pytest.mark.asyncio
async def test_a_persistence_failure_keeps_the_previous_credential_and_settings(monkeypatch):
    await admin_with_password("operator", f"{SENTINEL}-old")
    current = _settings()
    _install_persistence_stubs(monkeypatch, current)
    writes = []

    def failing_save(cfg):
        writes.append(cfg.model_copy(deep=True))
        if len(writes) == 1:
            raise OSError("disk full")

    monkeypatch.setattr(auth_config_routes, "save_settings", failing_save)
    update = auth_config_routes.AuthenticationConfigUpdate(auth_username="renamed", auth_password=f"{SENTINEL}-new")
    response = await auth_config_routes.update_authentication_config(
        _request(payload=update.model_dump(exclude_none=True)), update)
    assert response.status_code == 500
    assert json.loads(response.body)["detail"] == "Authentication configuration could not be saved; nothing was changed"
    assert SENTINEL not in response.body.decode()
    # The credential transaction rolled back; the previous settings were put back.
    assert await local_credential_status(DEFAULT_ADMIN_ID) == ("operator", True)
    assert await _signs_in("operator", f"{SENTINEL}-old")
    assert await local_credential("renamed") is None
    assert writes[-1] == current


@pytest.mark.asyncio
async def test_clear_still_needs_no_password_and_keeps_its_own_guard(monkeypatch):
    await admin_with_password("operator", f"{SENTINEL}-old")
    # The only interactive mechanism: clearing it without the open-mode proof is refused upstream
    # (settings_transition_rejection); here the writer itself clears with the password disabled.
    response, *_ = await _apply(
        monkeypatch, _settings(), principal=Principal.password_session(account("operator")),
        auth_password_enabled=False, clear_password=True)
    assert response.status_code == 200
    assert await local_credential_status(DEFAULT_ADMIN_ID) == ("operator", False)
