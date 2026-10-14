"""OIDC profile population (1.0.13, ACL live-qualification follow-on).

An OIDC identity's claims give a new account its Display Name, Actual Name and
Email (with the verification the provider stated); later sign-ins only
complete what is missing and never overwrite the account's own names. Only
``(issuer, subject)`` identifies: profile claims never link, enable or change
the authority of anything. My Account reads the sanitized profile from
``GET /api/account``.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest
import pytest_asyncio

import core.config as config
import db.database as database
from auth import accounts, oidc
from auth.accounts import Role
from auth.models import Principal
from auth.sessions import HTTP_SESSION_COOKIE, session_store
from test_access_control import served  # noqa: F401  (fixture)
from test_auth_oidc import _config

pytestmark = pytest.mark.asyncio

ISSUER = "https://idp.example"


@pytest_asyncio.fixture
async def accounts_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "profile.db")
    await database.init_db()


async def _login(subject: str, **claims):
    return await accounts.resolve_oidc(ISSUER, subject, {"iss": ISSUER, "sub": subject, **claims},
                                       default_role="user")


async def test_first_sign_in_initializes_the_profile_by_claim_precedence(accounts_db):
    full = await _login("full", preferred_username="jdoe", nickname="Johnny", name="John Doe",
                        email="john@example.com", email_verified=True)
    assert (full.display_name, full.actual_name, full.email, full.email_verified, full.email_verified_known) == (
        "jdoe", "John Doe", "john@example.com", True, True)

    partial = await _login("partial", nickname="Jo", given_name="Jo", family_name="Ann", email="jo@example.com")
    assert (partial.display_name, partial.actual_name) == ("Jo", "Jo Ann")
    # No verification claim: the email is kept, and nothing says it is verified.
    assert (partial.email, partial.email_verified, partial.email_verified_known) == ("jo@example.com", False, False)

    stated_false = await _login("unverified", name="Una Verified", email="una@example.com", email_verified=False)
    assert (stated_false.display_name, stated_false.actual_name) == ("Una Verified", "Una Verified")
    assert (stated_false.email_verified, stated_false.email_verified_known) == (False, True)

    # A non-boolean "verification" is not a statement; only given name present.
    odd = await _login("odd", given_name="Solo", email="odd@example.com", email_verified="true")
    assert (odd.actual_name, odd.email_verified, odd.email_verified_known) == ("Solo", False, False)

    bare = await _login("bare-subject")
    assert (bare.display_name, bare.actual_name, bare.email, bare.email_verified_known) == ("bare-subject", "", "", False)


async def test_later_sign_ins_complete_missing_fields_and_keep_the_accounts_own_names(accounts_db):
    # As a pre-follow-on account was provisioned: display from name, no actual
    # name, and no email because it was not verified.
    first = await _login("returning", name="Ret Urner")
    async with database.get_db() as db:
        await db.execute("UPDATE accounts SET display_name='Ret Urner', actual_name=NULL, email='' WHERE id=?",
                         (first.id,))
        await db.commit()

    again = await _login("returning", preferred_username="ret", name="Ret Urner", email="ret@example.com")
    assert again.id == first.id and again.role is Role.USER
    assert (again.display_name, again.actual_name, again.email) == ("Ret Urner", "Ret Urner", "ret@example.com")
    assert again.email_verified_known is False

    # The provider later states the SAME address's verification: recorded.
    stated = await _login("returning", email="RET@example.com", email_verified=True)
    assert (stated.email, stated.email_verified, stated.email_verified_known) == ("ret@example.com", True, True)

    # Edited names are the account's own; a different email does not replace it.
    await accounts.set_display_name(first.id, "My Choice")
    await accounts.set_actual_name(first.id, "Chosen Name")
    later = await _login("returning", preferred_username="other", name="Other Name",
                         email="new@example.com", email_verified=True)
    assert (later.display_name, later.actual_name, later.email, later.email_verified) == (
        "My Choice", "Chosen Name", "ret@example.com", True)

    # Clearing the name is a choice too: a later sign-in leaves it empty.
    await accounts.set_actual_name(first.id, "")
    cleared = await _login("returning", name="Ret Urner", given_name="Ret", family_name="Urner")
    assert cleared.actual_name == ""
    async with database.get_db() as db:
        stored = await db.fetchone("SELECT actual_name FROM accounts WHERE id=?", (first.id,))
    assert stored["actual_name"] == ""                      # cleared, not "never populated"


async def test_an_identity_with_no_name_claim_leaves_the_name_unpopulated_until_one_arrives(accounts_db):
    nameless = await _login("nameless", preferred_username="nl")
    assert nameless.actual_name == ""
    named = await _login("nameless", given_name="Nora", family_name="Lee")
    assert named.actual_name == "Nora Lee"                  # never populated, so completed


async def test_profile_claims_never_link_enable_or_change_authority(accounts_db):
    one = await _login("one", name="Same Person", email="same@example.com", email_verified=True)
    two = await _login("two", name="Same Person", email="same@example.com", email_verified=True)
    assert one.id != two.id                                      # a shared verified email links nothing
    async with database.get_db() as db:
        bindings = await db.fetchall("SELECT subject, account_id FROM account_oidc_bindings ORDER BY subject")
    assert [(row["subject"], row["account_id"]) for row in bindings] == [("one", one.id), ("two", two.id)]

    await accounts.set_role("admin-actor", one.id, Role.READONLY)
    assert (await _login("one", name="Changed")).role is Role.READONLY   # a sign-in never changes a role

    await accounts.set_enabled("admin-actor", two.id, False)
    assert await _login("two", name="Changed", email="other@example.com", email_verified=True) is None
    disabled = await accounts.get_account(two.id)
    assert (disabled.enabled, disabled.actual_name, disabled.email) == (False, "Same Person", "same@example.com")


async def test_existing_databases_gain_the_columns_and_known_verification(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:              # the accounts table as Stage 1 shipped it
        conn.execute("""CREATE TABLE accounts (
            id TEXT PRIMARY KEY, role TEXT NOT NULL, origin TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
            email_verified INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1,
            protected TEXT UNIQUE, generation INTEGER NOT NULL DEFAULT 1, merged_into TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT)""")
        conn.execute("INSERT INTO accounts(id,role,origin,display_name,email,email_verified,created_at,updated_at) "
                     "VALUES('old','user','oidc','Old','old@example.com',1,'t','t')")
    monkeypatch.setattr(database, "DB_PATH", path)
    await database.init_db()
    old = await accounts.get_account("old")
    assert (old.actual_name, old.email, old.email_verified, old.email_verified_known) == (
        "", "old@example.com", True, True)
    async with database.get_db() as db:
        assert (await db.fetchone("SELECT actual_name FROM accounts WHERE id='old'"))["actual_name"] is None


async def test_the_session_keeps_the_profile_claims_it_is_given():
    cfg = _config(oidc_allow_all=True)
    principal = oidc.authorize_oidc_claims(cfg, {
        "iss": cfg.issuer, "sub": "user-1", "nickname": "Nick", "given_name": "Giv", "family_name": "Fam",
        "address": {"street": "never kept"}, "phone_number": "never kept"})
    assert {key: principal.claims[key] for key in ("nickname", "given_name", "family_name")} == {
        "nickname": "Nick", "given_name": "Giv", "family_name": "Fam"}
    assert "address" not in principal.claims and "phone_number" not in principal.claims


async def test_my_account_reads_and_edits_the_sanitized_profile(served, monkeypatch):
    from auth.oidc_version import oidc_configuration_version

    client, as_ = served
    ready = config.AppSettings(
        auth_password_enabled=True, auth_oidc_enabled=True, oidc_provider_name="OIDC",
        oidc_issuer_url=ISSUER, oidc_client_id="client", oidc_client_secret="secret",
        oidc_scopes=["openid"], oidc_allow_all=True, public_base_url="https://pulse.example")
    monkeypatch.setattr(config, "_settings", ready)
    person = await _login("me", preferred_username="me", name="Me Myself", email="me@example.com")
    token, _ = session_store.create(Principal.oidc_session(f"{ISSUER}|me").bound_to(person), lifetime_seconds=3600,
                                    credential_version=oidc_configuration_version(ready))
    own = {"Cookie": f"{HTTP_SESSION_COOKIE}={token}", "X-CSRF-Token": session_store.csrf_token(token)}

    profile = (await client.get("/api/account", headers=own)).json()
    assert profile == {"display_name": "me", "actual_name": "Me Myself", "email": "me@example.com",
                       "email_verification": "unknown", "origin": "oidc", "role": "user", "has_password": False}

    assert (await client.put("/api/account/actual-name", headers=own, json={"actual_name": "Me Edited"})).status_code == 200
    assert (await client.put("/api/account/display-name", headers=own, json={"display_name": "Edited"})).status_code == 200
    edited = (await client.get("/api/account", headers=own)).json()
    assert (edited["display_name"], edited["actual_name"], edited["email"]) == ("Edited", "Me Edited", "me@example.com")
    # A populated email is never replaced here, and an Integration has no profile.
    assert (await client.put("/api/account/email", headers=own, json={"email": "x@example.com"})).status_code == 409
    assert (await client.get("/api/account", headers=own)).json()["email"] == "me@example.com"
    assert (await client.get("/api/account", headers=as_(Role.INTEGRATION))).status_code == 403


# ── an account's own, self-entered email (ACL live UX Batch 2) ──────────────

async def test_an_account_without_an_email_records_one_once_unverified_and_self_entered(accounts_db):
    person = await accounts.create_local_account("selfie", "secret", role=Role.USER)
    assert await accounts.set_own_email(person.id, "  Me@Example.COM ") == "Me@example.com"
    stored = await accounts.get_account(person.id)
    assert (stored.email, stored.email_verified, stored.email_verified_known, stored.email_self_entered) == (
        "Me@example.com", False, True, True)
    [listed] = [item for item in await accounts.list_accounts() if item["id"] == person.id]
    assert (listed["email_verified"], listed["email_verified_known"], listed["email_self_entered"]) == (
        False, True, True)

    # Once present it is never replaced -- not even by the same account.
    with pytest.raises(accounts.AccountRefused, match="already has an email"):
        await accounts.set_own_email(person.id, "other@example.com")
    assert (await accounts.get_account(person.id)).email == "Me@example.com"

    blank = await accounts.create_local_account("blank", "secret", role=Role.USER)
    for invalid in ("", "   ", "no-at-sign", "a@b", "two@@example.com", "sp ace@example.com", "x@-bad-.com",
                    "<x>@example.com", "x@example.com\nBcc: y@example.com", "a" * 65 + "@example.com",
                    "x@" + "a" * 250 + ".com"):
        with pytest.raises(accounts.InvalidEmail):
            await accounts.set_own_email(blank.id, invalid)
    assert (await accounts.get_account(blank.id)).email == ""            # nothing invented

    with pytest.raises(accounts.AccountRefused):
        await accounts.set_own_email(accounts.DEFAULT_INTEGRATION_ID, "bot@example.com")


async def test_concurrent_first_emails_record_exactly_one_and_refuse_the_other(accounts_db):
    person = await accounts.create_local_account("racer", "secret", role=Role.USER)
    results = await asyncio.gather(accounts.set_own_email(person.id, "first@example.com"),
                                   accounts.set_own_email(person.id, "second@example.com"), return_exceptions=True)
    won = [item for item in results if isinstance(item, str)]
    refused = [item for item in results if isinstance(item, accounts.AccountRefused)]
    assert len(won) == 1 and len(refused) == 1
    assert (await accounts.get_account(person.id)).email == won[0]


async def test_a_self_entered_email_is_never_verified_replaced_or_used_as_identity(accounts_db):
    # An OIDC identity whose provider supplied no email enters one itself.
    first = await _login("quiet", preferred_username="quiet")
    await accounts.set_own_email(first.id, "quiet@example.com")

    # Its provider later states that same address verified: still unverified,
    # still self-entered. A different address does not replace it either.
    again = await _login("quiet", email="QUIET@example.com", email_verified=True)
    assert again.id == first.id
    assert (again.email, again.email_verified, again.email_verified_known, again.email_self_entered) == (
        "quiet@example.com", False, True, True)
    other = await _login("quiet", email="new@example.com", email_verified=True)
    assert (other.email, other.email_verified) == ("quiet@example.com", False)

    # Another identity presenting that address, verified, is its own account:
    # the stored address links, binds and merges nothing.
    stranger = await _login("stranger", email="quiet@example.com", email_verified=True)
    assert stranger.id != first.id and stranger.role is Role.USER
    async with database.get_db() as db:
        bindings = await db.fetchall("SELECT subject, account_id FROM account_oidc_bindings ORDER BY subject")
    assert [(row["subject"], row["account_id"]) for row in bindings] == [("quiet", first.id), ("stranger", stranger.id)]
    assert (await accounts.get_account(first.id)).role is Role.USER


async def test_my_account_records_its_own_email_once_and_only_its_own(served):
    client, as_ = served
    alice = await accounts.create_local_account("alice", "secret", role=Role.USER)
    bob = await accounts.create_local_account("bob", "secret", role=Role.USER)
    token, _ = session_store.create(Principal.password_session(alice), lifetime_seconds=3600)
    own = {"Cookie": f"{HTTP_SESSION_COOKIE}={token}", "X-CSRF-Token": session_store.csrf_token(token)}

    assert (await client.get("/api/account", headers=own)).json()["email"] == ""
    assert (await client.put("/api/account/email", headers=own, json={"email": "not an email"})).status_code == 422
    # The account is the session's: a body naming another account changes only the caller's.
    saved = await client.put("/api/account/email", headers=own,
                             json={"email": "alice@example.com", "account_id": bob.id, "email_verified": True})
    assert saved.status_code == 200, saved.text
    profile = (await client.get("/api/account", headers=own)).json()
    assert (profile["email"], profile["email_verification"]) == ("alice@example.com", "unverified")
    assert (await accounts.get_account(bob.id)).email == ""
    # A stale second save is refused, never a silent replacement.
    stale = await client.put("/api/account/email", headers=own, json={"email": "alice2@example.com"})
    assert stale.status_code == 409 and "already has an email" in stale.json()["detail"]
    assert (await accounts.get_account(alice.id)).email == "alice@example.com"
    # The session survives: recording an email changes no authority.
    assert session_store.resolve(token) is not None
    assert (await client.put("/api/account/email", headers=as_(Role.INTEGRATION),
                             json={"email": "bot@example.com"})).status_code == 403
