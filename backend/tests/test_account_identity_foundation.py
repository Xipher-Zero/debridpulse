"""1.0.13 ACL Stage 1: durable accounts are the one identity authority.

Narrow proofs of the account owner's invariants (``auth.accounts``): first
OIDC sign-in provisions exactly one account per validated ``(issuer,
subject)`` and never links by email; the last usable Admin survives
concurrent demotion; disabling an account revokes its sessions and refuses
its credential; a merge redirects history without rewriting it; and the
configuration file's credential remains the offline recovery for the default
Admin.
"""
from __future__ import annotations

import asyncio
import base64
import json

import pytest
import pytest_asyncio
from fastapi import Request, Response

import core.config as config
from account_support import account_database, admin_with_password
from auth import accounts
from auth.accounts import DEFAULT_ADMIN_ID, DEFAULT_INTEGRATION_ID, AccountRefused, Role
from auth.models import Principal
from auth.passwords import hash_password
from auth.sessions import session_store
from auth.throttle import password_failure_throttle

ISSUER = "https://id.example/application/o/debridpulse/"


@pytest_asyncio.fixture
async def installation(tmp_path, monkeypatch):
    await account_database(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "_settings", config.AppSettings(auth_password_enabled=True))
    session_store.clear()
    password_failure_throttle.clear()
    return tmp_path


async def _count(sql: str, params=()) -> int:
    from db.database import get_db

    async with get_db() as db:
        return int((await db.fetchone(sql, params))["n"])


def _basic_request(username: str, password: str) -> Request:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return Request({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": "/api/stats", "raw_path": b"/api/stats", "query_string": b"",
        "headers": [(b"host", b"debridpulse.local"), (b"authorization", f"Basic {token}".encode())],
        "client": ("127.0.0.1", 12345), "server": ("debridpulse.local", 80),
    })


async def _basic_status(username: str, password: str) -> int:
    from auth.middleware import enforce_authentication

    async def ok(_request):
        return Response(status_code=204)

    return (await enforce_authentication(_basic_request(username, password), ok)).status_code


@pytest.mark.asyncio
async def test_concurrent_first_sign_ins_of_one_identity_provision_exactly_one_account(installation):
    claims = {"iss": ISSUER, "sub": "user-1", "email": "user@example.com", "email_verified": True}
    resolved = await asyncio.gather(*(
        accounts.resolve_oidc(ISSUER, "user-1", claims, default_role="readonly") for _ in range(8)))

    assert len({account.id for account in resolved}) == 1
    assert await _count("SELECT COUNT(*) AS n FROM accounts WHERE origin='oidc'") == 1
    assert await _count("SELECT COUNT(*) AS n FROM account_oidc_bindings") == 1
    assert resolved[0].role is Role.READONLY and resolved[0].email == "user@example.com"


@pytest.mark.asyncio
async def test_identity_is_issuer_and_subject_never_email(installation):
    shared = {"email": "same@example.com", "email_verified": True}
    first = await accounts.resolve_oidc(ISSUER, "user-1", {**shared}, default_role="user")
    other_issuer = await accounts.resolve_oidc("https://other.example/", "user-1", {**shared}, default_role="user")
    other_subject = await accounts.resolve_oidc(ISSUER, "user-2", {**shared}, default_role="user")

    assert len({first.id, other_issuer.id, other_subject.id}) == 3
    # An unverified email is never recorded as the account's email.
    unverified = await accounts.resolve_oidc(ISSUER, "user-3", {"email": "x@example.com"}, default_role="user")
    assert unverified.email == "" and unverified.email_verified is False


@pytest.mark.asyncio
async def test_a_provisioned_account_keeps_its_role_and_signs_in_with_it(installation):
    provisioned = await accounts.resolve_oidc(ISSUER, "user-1", {}, default_role="user")
    assert provisioned.role is Role.USER and accounts.admitted(provisioned) is True

    await accounts.set_role(DEFAULT_ADMIN_ID, provisioned.id, Role.READONLY)
    again = await accounts.resolve_oidc(ISSUER, "user-1", {"groups": ["admins"]}, default_role="user")
    assert again.id == provisioned.id and again.role is Role.READONLY

    # A disabled identity is neither re-created nor re-enabled by signing in.
    await accounts.set_enabled(DEFAULT_ADMIN_ID, provisioned.id, False)
    assert await accounts.resolve_oidc(ISSUER, "user-1", {}, default_role="user") is None
    assert await _count("SELECT COUNT(*) AS n FROM accounts WHERE origin='oidc'") == 1
    # Verify Sign-In cannot hand that identity to an Admin either: it is bound.
    assert await accounts.bind_verified_oidc(ISSUER, "user-1", DEFAULT_ADMIN_ID) is None


@pytest.mark.asyncio
async def test_concurrent_demotion_of_the_last_two_usable_admins_keeps_one(installation, monkeypatch):
    import auth.transitions as transitions

    # The process-wide lock binds to the loop it is first contended in; give
    # this test's loop its own.
    monkeypatch.setattr(transitions, "authentication_configuration_lock", asyncio.Lock())
    first = await accounts.create_local_account("first", "secret-1", role=Role.ADMIN)
    second = await accounts.create_local_account("second", "secret-2", role=Role.ADMIN)
    token, _ = session_store.create(Principal.password_session(first), lifetime_seconds=3600)

    outcomes = await asyncio.gather(
        accounts.set_role(DEFAULT_ADMIN_ID, first.id, Role.USER),
        accounts.set_role(DEFAULT_ADMIN_ID, second.id, Role.USER),
        return_exceptions=True,
    )

    assert sum(isinstance(outcome, AccountRefused) for outcome in outcomes) == 1
    roles = {(await accounts.get_account(account_id)).role for account_id in (first.id, second.id)}
    assert roles == {Role.ADMIN, Role.USER}
    if outcomes[0] is None:
        assert session_store.resolve(token) is None  # the demoted account's session ended with it


@pytest.mark.asyncio
async def test_disabling_an_account_revokes_its_sessions_and_refuses_its_password(installation):
    holder = await accounts.create_local_account("holder", "secret", role=Role.ADMIN)
    other = await accounts.create_local_account("other", "other-secret", role=Role.ADMIN)
    token, _ = session_store.create(Principal.password_session(other), lifetime_seconds=3600)
    assert await _basic_status("other", "other-secret") == 204

    await accounts.set_enabled(holder.id, other.id, False)

    assert session_store.resolve(token) is None
    assert await _basic_status("other", "other-secret") == 401
    with pytest.raises(AccountRefused):
        await accounts.set_enabled(holder.id, holder.id, False)  # never oneself
    with pytest.raises(AccountRefused):
        await accounts.set_enabled(holder.id, DEFAULT_ADMIN_ID, False)  # nor the default Admin

    await accounts.set_enabled(holder.id, other.id, True)
    assert await _basic_status("other", "other-secret") == 204


@pytest.mark.asyncio
async def test_a_merge_moves_live_identities_and_redirects_history_without_rewriting_it(installation):
    target = await accounts.create_local_account("target", "secret", role=Role.ADMIN)
    duplicate = await accounts.resolve_oidc(ISSUER, "user-1", {"email": "t@example.com", "email_verified": True},
                                            default_role="user")

    await accounts.merge_accounts(DEFAULT_ADMIN_ID, duplicate.id, target.id)

    assert (await accounts.resolve_oidc(ISSUER, "user-1", {}, default_role="user")).id == target.id
    assert await accounts.canonical_account_id(duplicate.id) == target.id
    tombstone = await accounts.get_account(duplicate.id)
    assert tombstone is not None and not tombstone.active and tombstone.merged_into == target.id
    assert (await accounts.get_account(target.id)).role is Role.ADMIN

    holder = await accounts.create_local_account("holder", "secret", role=Role.USER)
    with pytest.raises(AccountRefused):
        await accounts.merge_accounts(DEFAULT_ADMIN_ID, holder.id, target.id)  # two local credentials
    with pytest.raises(AccountRefused):
        await accounts.merge_accounts(target.id, DEFAULT_INTEGRATION_ID, target.id)  # protected


@pytest.mark.asyncio
async def test_default_accounts_exist_and_the_integration_account_holds_no_credential(installation):
    admin = await accounts.get_account(DEFAULT_ADMIN_ID)
    automation = await accounts.get_account(DEFAULT_INTEGRATION_ID)

    assert (admin.role, admin.protected, admin.has_password) == (Role.ADMIN, "default_admin", False)
    assert (automation.role, automation.protected, automation.has_password) == (
        Role.INTEGRATION, "default_integration", False)
    assert await _count("SELECT COUNT(*) AS n FROM account_local_credentials") == 0
    with pytest.raises(AccountRefused):
        await accounts.reset_password(DEFAULT_ADMIN_ID, DEFAULT_INTEGRATION_ID, "secret")
    assert await accounts.list_tokens(DEFAULT_INTEGRATION_ID) == []


@pytest.mark.asyncio
async def test_the_configuration_file_credential_recovers_the_default_admin(installation, monkeypatch):
    from application.lifetime import prepare_settings_and_migrate
    from db.migrations.v112 import _mark_current

    await _mark_current()  # the installation's database, as startup leaves it
    await admin_with_password("operator", "forgotten")
    path = installation / "config.json"
    path.write_text(json.dumps({"auth_password_enabled": True, "auth_username": "operator",
                                "auth_password": "recovered"}), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.setitem(config._legacy_startup_inputs, "credential", None)
    config.apply_settings(config.load_settings())

    await prepare_settings_and_migrate()

    assert await _basic_status("operator", "recovered") == 204
    assert await _basic_status("operator", "forgotten") == 401
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert not {"auth_username", "auth_password", "auth_password_hash"} & set(persisted)
    assert config.legacy_credential_input() is None

    # A recovery username owned by another account is refused, not reassigned.
    await accounts.create_local_account("someone", "secret", role=Role.USER)
    with pytest.raises(RuntimeError):
        await accounts.import_legacy_credential("someone", hash_password("x"))


def _clear_last_password_into_open_mode(principal) -> Request:
    request = Request({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "PUT",
        "scheme": "http", "path": "/api/auth/config", "raw_path": b"/api/auth/config", "query_string": b"",
        "headers": [(b"host", b"debridpulse.local")],
        "client": ("127.0.0.1", 12345), "server": ("debridpulse.local", 80),
    })
    request.state.principal = principal
    return request


_OPEN_MODE = {"auth_password_enabled": False, "clear_password": True, "confirm_open_mode": True}


async def _open_mode_transition(installation, monkeypatch, *, fail_save=False, fail_commit=False):
    """Clear the only Admin password while switching to no-auth, through the
    one Authentication update path, with an optional persistence fault."""
    from api import auth_config_routes
    from db import database

    operator = await admin_with_password()
    path = installation / "config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    config.save_settings(config.get_settings())
    before = path.read_text(encoding="utf-8")
    token, _ = session_store.create(Principal.password_session(operator), lifetime_seconds=3600)
    writes = []
    real_save = config.save_settings

    def save(settings):
        writes.append(settings.auth_password_enabled)
        if fail_save:
            raise OSError("read-only configuration volume")
        real_save(settings)

    async def refuse_commit(_self):
        raise OSError("database is full")

    monkeypatch.setattr(auth_config_routes, "save_settings", save)
    update = auth_config_routes.AuthenticationConfigUpdate(**_OPEN_MODE)
    with monkeypatch.context() as fault:
        if fail_commit:
            fault.setattr(database._DbConnection, "commit", refuse_commit)
        response = await auth_config_routes.update_authentication_config(
            _clear_last_password_into_open_mode(Principal.password_session(operator)), update)
    return response, token, before, path, writes


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["fail_save", "fail_commit"])
async def test_a_failed_open_mode_transition_keeps_the_last_password_and_reports_failure(
        installation, monkeypatch, fault):
    """Either persistence boundary failing leaves the password, its mechanism
    and its session exactly as they were, and the request reports failure."""
    response, token, before, path, writes = await _open_mode_transition(
        installation, monkeypatch, **{fault: True})

    assert response.status_code == 500
    assert b"nothing was changed" in response.body
    assert config.get_settings().auth_password_enabled is True
    assert path.read_text(encoding="utf-8") == before
    assert await accounts.local_credential("operator") is not None
    assert session_store.resolve(token) is not None
    if fault == "fail_commit":
        assert writes == [False, True]  # written, then put back


@pytest.mark.asyncio
async def test_the_open_mode_transition_clears_the_last_password_when_both_persist(installation, monkeypatch):
    response, token, _before, path, writes = await _open_mode_transition(installation, monkeypatch)

    assert response.status_code == 200
    assert writes == [False]
    assert config.get_settings().auth_password_enabled is False
    assert json.loads(path.read_text(encoding="utf-8"))["auth_password_enabled"] is False
    assert await accounts.local_credential("operator") is None
    assert session_store.resolve(token) is None
