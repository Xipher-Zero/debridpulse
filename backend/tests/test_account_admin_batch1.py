"""ACL live qualification, Batch 1: account administration.

Remove works for local and OIDC people alike. A removed OIDC identity that
signs in again gets a NEW account, created disabled, with no session; a
disabled (not removed) account is never provisioned around. Merge folds two or
more accounts into one survivor in one transaction, or changes nothing. The
Admin "Link OIDC" path is retired; Verify Sign-In's binding is untouched.
"""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

import db.database as database
from auth import accounts
from auth.accounts import DEFAULT_ADMIN_ID, Role
from auth.models import Principal
from auth.permissions import ACTIONS, Capability
from auth.sessions import session_store
from transfers.models import SubmissionDemand, TransferRequest
from transfers.recovery_repository import TransferRepository
from test_access_control import served  # noqa: F401  (fixture)

pytestmark = pytest.mark.asyncio

ISSUER = "https://idp.example"
ADMIN = "admin-actor"


@pytest_asyncio.fixture
async def accounts_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "batch1.db")
    await database.init_db()


async def _login(subject: str, **claims):
    return await accounts.resolve_oidc(ISSUER, subject, {"iss": ISSUER, "sub": subject, **claims},
                                       default_role="user")


async def _binding(subject: str) -> str:
    async with database.get_db() as db:
        row = await db.fetchone("SELECT account_id FROM account_oidc_bindings WHERE issuer=? AND subject=?",
                                (ISSUER, subject))
    return row["account_id"]


async def _oidc_accounts(subject_name: str) -> list:
    return [row for row in await accounts.list_accounts() if row["display_name"] == subject_name]


async def test_a_removed_oidc_identity_returns_as_a_new_disabled_account(accounts_db):
    first = await _login("ret", preferred_username="returner", email="ret@example.com", email_verified=True)
    assert first.enabled and first.role is Role.USER
    repository = TransferRepository()
    transfer, _ = await repository.admit(
        (TransferRequest("magnet", "magnet:?xt=urn:btih:" + "a" * 40, name="x", fingerprint="a" * 40),),
        name="x", demand=SubmissionDemand(first.id))
    token, _ = session_store.create(Principal.oidc_session(f"{ISSUER}|ret").bound_to(first), lifetime_seconds=3600)

    withdrawn = await accounts.delete_account(ADMIN, first.id)                 # Remove
    assert withdrawn == [transfer.id]                                          # its outstanding demand withdrawn
    assert session_store.resolve(token) is None                                # its session ended
    assert await _binding("ret") == first.id                                   # the durable marker

    # The same identity signs in again: no session, a NEW account, disabled.
    assert await _login("ret", preferred_username="returner", email="ret@example.com", email_verified=True) is None
    fresh_id = await _binding("ret")
    fresh = await accounts.get_account(fresh_id)
    assert fresh_id != first.id and fresh.enabled is False and fresh.role is Role.USER
    assert (fresh.display_name, fresh.email, fresh.email_verified) == ("returner", "ret@example.com", True)
    # The removed account and its history are untouched.
    old = await accounts.get_account(first.id)
    assert old.deleted_at and not old.enabled
    async with database.get_db() as db:
        demand = await db.fetchone("SELECT account_id, withdrawn_at FROM transfer_demands WHERE transfer_id=?",
                                   (transfer.id,))
    assert demand["account_id"] == first.id and demand["withdrawn_at"] is not None

    # Repeated and concurrent returns create nothing more and grant nothing.
    results = await asyncio.gather(*(_login("ret") for _ in range(5)))
    assert results == [None] * 5 and await _binding("ret") == fresh_id
    assert len(await _oidc_accounts("returner")) == 2                          # the tombstone and the new one

    # Only an Admin enabling the new account makes it usable -- the same account.
    await accounts.set_enabled(ADMIN, fresh_id, True)
    assert (await _login("ret")).id == fresh_id
    # Removing it again repeats the cycle: disabled by default, every time.
    await accounts.delete_account(ADMIN, fresh_id)
    assert await _login("ret") is None
    again = await accounts.get_account(await _binding("ret"))
    assert again.id not in {first.id, fresh_id} and again.enabled is False


async def test_concurrent_returns_of_a_removed_identity_create_exactly_one_account(accounts_db):
    first = await _login("race", preferred_username="racer")
    await accounts.delete_account(ADMIN, first.id)
    assert await asyncio.gather(*(_login("race", preferred_username="racer") for _ in range(8))) == [None] * 8
    rows = await _oidc_accounts("racer")
    assert len(rows) == 2 and sum(1 for row in rows if not row["deleted_at"]) == 1
    assert not any(row["enabled"] for row in rows)


async def test_a_disabled_account_is_never_provisioned_around(accounts_db):
    person = await _login("held", preferred_username="held")
    await accounts.set_enabled(ADMIN, person.id, False)
    assert await _login("held") is None and await _login("held") is None
    assert await _binding("held") == person.id
    assert len(await _oidc_accounts("held")) == 1
    await accounts.set_enabled(ADMIN, person.id, True)                         # re-enable: the same account
    assert (await _login("held")).id == person.id


async def test_the_removed_identity_marker_survives_a_database_wipe(accounts_db):
    from services.db_maintenance import wipe_database

    person = await _login("wiped", preferred_username="wiped")
    await accounts.delete_account(ADMIN, person.id)
    await wipe_database(verified_quiesced=True)
    assert await _binding("wiped") == person.id
    assert await _login("wiped") is None
    assert (await accounts.get_account(await _binding("wiped"))).enabled is False


async def test_remove_refuses_self_protected_accounts_and_the_last_usable_admin(accounts_db, monkeypatch):
    import core.config as config

    # The last-usable-Admin invariant guards an installation that requires sign-in.
    monkeypatch.setattr(config, "_settings", config.AppSettings(auth_password_enabled=True))
    local = await accounts.create_local_account("loc", "loc-secret", role="user", display_name="Loc")
    with pytest.raises(accounts.AccountRefused):
        await accounts.delete_account(local.id, local.id)                       # never itself
    with pytest.raises(accounts.AccountRefused):
        await accounts.delete_account(ADMIN, DEFAULT_ADMIN_ID)                  # protected
    only_admin = await accounts.create_local_account("boss", "boss-secret", role="admin", display_name="Boss")
    with pytest.raises(accounts.AccountRefused, match="usable Admin"):
        await accounts.delete_account(ADMIN, only_admin.id)                     # the last usable Admin
    assert (await accounts.get_account(only_admin.id)).deleted_at == ""
    await accounts.delete_account(ADMIN, local.id)                              # a local person: removed
    assert (await accounts.get_account(local.id)).deleted_at


async def test_merge_folds_several_accounts_into_one_or_changes_nothing(accounts_db):
    keep = await accounts.create_local_account("keep", "keep-secret", role="admin", display_name="Keep")
    oidc_one = await _login("m1", preferred_username="m-one")
    oidc_two = await _login("m2", preferred_username="m-two")
    await accounts.merge_accounts(ADMIN, [oidc_one.id, oidc_two.id], keep.id)
    for source in (oidc_one, oidc_two):
        folded = await accounts.get_account(source.id)
        assert folded.merged_into == keep.id and not folded.enabled
    assert await _binding("m1") == keep.id and await _binding("m2") == keep.id
    assert (await _login("m1")).id == keep.id and (await accounts.get_account(keep.id)).role is Role.ADMIN

    # A refusal anywhere refuses the whole merge: nothing moves.
    a = await accounts.create_local_account("a-login", "a-secret", role="user", display_name="A")
    b = await accounts.create_local_account("b-login", "b-secret", role="user", display_name="B")
    c = await _login("m3", preferred_username="m-three")
    with pytest.raises(accounts.AccountRefused, match="local credential"):
        await accounts.merge_accounts(ADMIN, [a.id, c.id], b.id)                # two local passwords
    assert (await accounts.get_account(c.id)).merged_into == "" and await _binding("m3") == c.id
    for refused in ([DEFAULT_ADMIN_ID, c.id], [accounts.DEFAULT_INTEGRATION_ID]):
        with pytest.raises(accounts.AccountRefused):
            await accounts.merge_accounts(ADMIN, refused, b.id)                 # protected / Integration mix
    with pytest.raises(accounts.AccountRefused):
        await accounts.merge_accounts(b.id, [c.id], b.id)                       # never itself


async def test_admin_account_endpoints_merge_and_link_retirement(served):
    client, as_ = served
    a = await accounts.create_local_account("ha", "ha-secret", role="user", display_name="HA")
    b = await _login("hb", preferred_username="hb")
    c = await _login("hc", preferred_username="hc")
    body = {"target_id": a.id, "source_ids": [b.id, c.id]}
    for role in (Role.USER, Role.READONLY, Role.INTEGRATION):
        assert (await client.post("/api/accounts/merge", headers=as_(role), json=body)).status_code == 403
        assert (await client.delete(f"/api/accounts/{b.id}", headers=as_(role))).status_code == 403
    refused = await client.post("/api/accounts/merge", headers=as_(Role.ADMIN),
                                json={"target_id": a.id, "source_ids": [accounts.DEFAULT_INTEGRATION_ID]})
    assert refused.status_code == 409 and "cannot be merged" in refused.json()["detail"]
    assert (await client.post("/api/accounts/merge", headers=as_(Role.ADMIN), json=body)).status_code == 200
    assert {(await accounts.get_account(item.id)).merged_into for item in (b, c)} == {a.id}

    # Link OIDC is retired: no route, no action; Verify Sign-In's owner remains.
    linked = await client.post(f"/api/accounts/{a.id}/oidc-identities", headers=as_(Role.ADMIN),
                               json={"issuer": ISSUER, "subject": "typed"})
    assert linked.status_code in {404, 405}
    assert not any("link_account_identity" in name for name in ACTIONS)
    assert not hasattr(accounts, "link_oidc_identity") and hasattr(accounts, "bind_verified_oidc")


async def test_an_admin_changes_a_username_keeping_the_password(accounts_db):
    person = await accounts.create_local_account("before", "before-secret", role="user", display_name="P")
    _owner, verifier = await accounts.local_credential("before")
    session, _ = session_store.create(Principal.password_session(await accounts.get_account(person.id)),
                                      lifetime_seconds=3600)
    generation = (await accounts.get_account(person.id)).generation

    await accounts.set_username(ADMIN, person.id, "  after ")

    assert await accounts.local_credential("before") is None                   # the former name no longer signs in
    renamed, kept = await accounts.local_credential("after")
    assert (renamed.id, kept) == (person.id, verifier)                          # the same password
    assert renamed.generation == generation + 1 and session_store.resolve(session) is None   # signed out
    assert renamed.display_name == "P"
    taken = await accounts.create_local_account("taken", "taken-secret", role="user")
    oidc = await _login("named", preferred_username="named")
    for actor, target, name, words in (
            (person.id, person.id, "self", "to itself"),
            (ADMIN, person.id, "taken", "belongs to another account"),
            (ADMIN, oidc.id, "taken", "belongs to another account"),
            (ADMIN, person.id, "   ", "A username is required"),
            (ADMIN, accounts.DEFAULT_INTEGRATION_ID, "bot", "Integration accounts have no username")):
        with pytest.raises(accounts.AccountRefused, match=words):
            await accounts.set_username(actor, target, name)
    assert (await accounts.local_credential("after"))[0].id == person.id and (await accounts.get_account(taken.id)).username == "taken"

    # An account without a password has a username too, changed alone: its
    # sessions are re-bound, not ended, and it still has no password.
    oidc_session, _ = session_store.create(Principal.oidc_session("named").bound_to(oidc), lifetime_seconds=3600)
    await accounts.set_username(ADMIN, oidc.id, "renamed")
    changed = await accounts.get_account(oidc.id)
    assert (changed.username, changed.display_name, changed.has_password) == ("renamed", "named", False)
    assert session_store.resolve(oidc_session) is not None


async def test_the_username_endpoint_is_admin_only_and_reports_refusals(served):
    client, as_ = served
    person = await accounts.create_local_account("route-before", "route-secret", role="user")
    await accounts.create_local_account("route-taken", "route-taken-secret", role="user")
    assert ACTIONS["api.account_routes.rename_account_username"] is Capability.ADMINISTER
    for role in (Role.USER, Role.READONLY, Role.INTEGRATION):
        assert (await client.put(f"/api/accounts/{person.id}/username", headers=as_(role),
                                 json={"username": "x"})).status_code == 403
    assert (await client.put(f"/api/accounts/{person.id}/username", headers=as_(Role.ADMIN),
                             json={"username": "route-after"})).status_code == 200
    assert (await accounts.get_account(person.id)).username == "route-after"
    refused = await client.put(f"/api/accounts/{person.id}/username", headers=as_(Role.ADMIN), json={"username": "route-taken"})
    assert refused.status_code == 409 and "belongs to another account" in refused.json()["detail"]
    missing = await client.put("/api/accounts/no-such/username", headers=as_(Role.ADMIN), json={"username": "y"})
    assert missing.status_code == 404
    assert (await client.put(f"/api/accounts/{person.id}/username", headers=as_(Role.ADMIN),
                             json={"username": ""})).status_code == 422
