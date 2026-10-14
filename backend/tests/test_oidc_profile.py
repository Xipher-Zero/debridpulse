"""OIDC profile population (1.0.13, ACL live-qualification follow-on).

An OIDC identity's claims give a new account its Display Name, Actual Name and
Email (with the verification the provider stated); later sign-ins only
complete what is missing and never overwrite the account's own names. Only
``(issuer, subject)`` identifies: profile claims never link, enable or change
the authority of anything. My Account reads the sanitized profile from
``GET /api/account``.
"""
from __future__ import annotations

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
    # There is no way to edit the email here, and an Integration has no profile.
    assert (await client.put("/api/account/email", headers=own, json={"email": "x@example.com"})).status_code in {403, 404, 405}
    assert (await client.get("/api/account", headers=as_(Role.INTEGRATION))).status_code == 403
