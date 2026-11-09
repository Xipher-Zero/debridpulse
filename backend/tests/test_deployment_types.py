"""DP 1.0.14 deployment types: Server and Standalone.

``deployment_type`` (``core.config.AppSettings``) is ``server`` -- every
account signs in under its role -- or ``standalone``: the protected default
Admin is the installation's one person, by its own password once one is set,
and OpenID Connect is never enabled. Absent means Server.

The type changes only through the confirmed transition
(``PUT /api/auth/deployment-type``), in either direction: Password and OIDC
both disabled (the rest of their configuration kept), the default Admin's
local password removed if present, every browser session ended. No other
account, credential, binding, token, stored ``enabled`` or demand is touched.
On an open Standalone installation the default Admin's open principal sets
its first password from My Account, which enables Username & Password with it.

Everything is proven through the real application and middleware, with
expected values stated independently (stored rows read back, not the
candidate's own predicates). Synthetic accounts, passwords and OIDC
configuration only; no identity provider is contacted."""
from __future__ import annotations

import asyncio
import base64
import json
import re
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

import api.auth_config_routes as auth_config_routes
import api.auth_routes as auth_routes
import auth.manager as manager
import auth.middleware as middleware
import auth.passwords as passwords
import auth.transitions as transitions
import core.config as config
import db.database as database
from auth import accounts
from auth.accounts import DEFAULT_ADMIN_ID
from auth.manager import PasswordAttemptAdmission
from auth.models import Principal
from auth.passwords import verify_password_candidate_async
from auth.permissions import ACTIONS, Capability
from auth.sessions import HTTP_SESSION_COOKIE, session_store

pytestmark = pytest.mark.asyncio

ISSUER = "https://idp.invalid/application/o/dp"
# Synthetic secrets: test values only.
ADMIN_PW = "synthetic-default-admin-1"
SECOND_PW = "synthetic-second-admin-2"
USER_PW = "synthetic-user-pass-3"
DISABLED_PW = "synthetic-disabled-pass-4"
NEW_PW = "synthetic-standalone-5"
CLIENT_SECRET = "synthetic-client-secret"

OIDC = dict(oidc_provider_name="Synthetic IdP", oidc_issuer_url=ISSUER, oidc_client_id="dp-client",
            oidc_client_secret=CLIENT_SECRET, oidc_scopes=["openid", "profile", "email"], oidc_allow_all=True,
            public_base_url="https://pulse.invalid", auth_session_lifetime_hours=9)


def _file() -> dict:
    return json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))


async def _rows(sql: str, args=()) -> list[tuple]:
    async with database.get_db() as db:
        return [tuple(row.values()) if hasattr(row, "values") else tuple(row)
                for row in await db.fetchall(sql, args)]


async def _hash(account_id: str) -> str | None:
    rows = await _rows("SELECT password_hash FROM account_local_credentials WHERE account_id=?", (account_id,))
    return rows[0][0] if rows else None


async def _people_state() -> list[tuple]:
    """Every account's stored identity facts, its credential and bindings:
    what a transition must leave as it was (the default Admin's password
    aside)."""
    return await _rows(
        "SELECT a.id, a.role, a.username, a.enabled, a.display_name, a.deleted_at, "
        "COALESCE(c.password_hash,''), (SELECT group_concat(issuer || '|' || subject) FROM account_oidc_bindings b "
        "WHERE b.account_id=a.id) FROM accounts a LEFT JOIN account_local_credentials c ON c.account_id=a.id "
        "WHERE a.id<>? ORDER BY a.id", (DEFAULT_ADMIN_ID,))


async def _demand_and_tokens() -> tuple[list[tuple], list[tuple]]:
    demand = await _rows("SELECT id, transfer_id, account_id, credential_id, origin, withdrawn_at, withdrawn_by "
                         "FROM transfer_demands ORDER BY id")
    tokens = await _rows("SELECT id, account_id, verifier, client_id, revoked_at FROM account_tokens ORDER BY id")
    return demand, tokens


@pytest.fixture(autouse=True)
def fresh_locks(monkeypatch):
    """This test's own authentication lock and password admission: asyncio
    primitives bind to the event loop they are first contended in."""
    lock = asyncio.Lock()
    for module in (transitions, middleware, auth_routes, auth_config_routes):
        monkeypatch.setattr(module, "authentication_configuration_lock", lock)
    monkeypatch.setattr(config, "_config_write_lock", asyncio.Lock())
    monkeypatch.setattr(manager, "password_attempt_admission", PasswordAttemptAdmission())
    monkeypatch.setattr(passwords, "_PASSWORD_VERIFY_SLOTS", asyncio.Semaphore(2))


@pytest_asyncio.fixture
async def install(tmp_path, monkeypatch):
    """A Server installation with Password and OIDC configured and enabled:
    the default Admin and a second Admin with passwords, a User, a disabled
    User, an OIDC-bound User, an Integration with a token, and demand owned by
    the User and the Integration."""
    import main

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "deployment.db")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    await database.init_db()
    settings = config.AppSettings(auth_password_enabled=True, auth_oidc_enabled=True, **OIDC)
    config.save_settings(settings)
    previous = config.get_settings()
    config.apply_settings(settings)

    await accounts.set_own_local_credential(DEFAULT_ADMIN_ID, password=ADMIN_PW)
    people = {
        "admin": await accounts.get_account(DEFAULT_ADMIN_ID),
        "second": await accounts.create_local_account("second-admin", SECOND_PW, role="admin"),
        "user": await accounts.create_local_account("local-user", USER_PW, role="user"),
        "disabled": await accounts.create_local_account("disabled-user", DISABLED_PW, role="user"),
        "oidc": await accounts.resolve_oidc(ISSUER, "sub-oidc", {"iss": ISSUER, "sub": "sub-oidc",
                                                                 "preferred_username": "oidc-user"},
                                            default_role="user"),
    }
    await accounts.set_enabled(DEFAULT_ADMIN_ID, people["disabled"].id, False)
    integration = await accounts.create_integration_account("Synthetic Integration")
    token_record, bearer = await accounts.create_token(integration.id, "probe")
    async with database.get_db() as db:
        await db.execute("INSERT INTO torrents(id, hash, name) VALUES(1, 'synthetic-hash', 'synthetic transfer')")
        await db.execute("INSERT INTO transfer_demands(id,transfer_id,account_id,origin,created_at) "
                         "VALUES('demand-user',1,?,'submission',1)", (people["user"].id,))
        await db.execute("INSERT INTO transfer_demands(id,transfer_id,account_id,credential_id,origin,created_at) "
                         "VALUES('demand-integration',1,?,?,'submission',2)", (integration.id, token_record["id"]))
        await db.commit()
    for name in people:
        people[name] = await accounts.get_account(people[name].id)
    # The state helpers read stored VALUES (each row an account ID first).
    assert {row[0] for row in await _people_state()} == \
        {p.id for name, p in people.items() if name != "admin"} | {integration.id, accounts.DEFAULT_INTEGRATION_ID}
    assert len((await _demand_and_tokens())[0]) == 2

    @asynccontextmanager
    async def operation():
        yield

    monkeypatch.setattr(main.app.state, "application", SimpleNamespace(application_operation=operation), raising=False)
    session_store.clear()

    def session(name: str) -> str:
        principal = Principal.password_session(people[name])
        token, _ = session_store.create(principal, lifetime_seconds=3600, credential_version=principal.credential_version)
        return token

    def headers(token: str) -> dict:
        return {"Cookie": f"{HTTP_SESSION_COOKIE}={token}", "X-CSRF-Token": session_store.csrf_token(token)}

    transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        yield SimpleNamespace(client=client, transport=transport, people=people, session=session, headers=headers,
                              bearer={"Authorization": f"Bearer {bearer}"}, integration=integration)
    session_store.clear()
    config.apply_settings(previous)


def _browser(live) -> httpx.AsyncClient:
    """A browser of its own: its own cookie jar."""
    return httpx.AsyncClient(transport=live.transport, base_url="http://127.0.0.1")


async def _switch(live, target: str, *, headers=None, confirm=True):
    return await live.client.put("/api/auth/deployment-type", headers=headers or {},
                                 json={"deployment_type": target, "confirm": confirm})


async def _sign_in(browser, password: str, *, username: str | None = None):
    page = await browser.get("/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    form = {"password": password, "csrf_token": csrf}
    if username is not None:
        form["username"] = username
    return await browser.post("/login", data=form)


def _basic(username: str, password: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()}


async def _standalone_with_password(live) -> None:
    """Server -> Standalone, then the default Admin's first password."""
    assert (await _switch(live, "standalone", headers=live.headers(live.session("admin")))).status_code == 200
    enrolled = await live.client.post("/api/account/first-password", json={"new_password": NEW_PW})
    assert enrolled.status_code == 200, enrolled.text


# ── M01: the setting itself ────────────────────────────────────────────────

async def test_m01_an_absent_type_is_server_and_an_invalid_one_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    path.write_text(json.dumps({"auth_password_enabled": True, "auth_oidc_enabled": False}), encoding="utf-8")
    assert config.load_settings().deployment_type == "server"
    assert config.AppSettings().deployment_type == "server"

    for corrupt in ("desktop", "", None, True):
        path.write_text(json.dumps({"deployment_type": corrupt}), encoding="utf-8")
        with pytest.raises(RuntimeError):
            config.load_settings()
    # Standalone never runs with OpenID Connect: refused at load and in a backup's document.
    path.write_text(json.dumps({"deployment_type": "standalone", "auth_oidc_enabled": True, **OIDC}), encoding="utf-8")
    with pytest.raises(RuntimeError):
        config.load_settings()
    with pytest.raises(ValueError):
        config.validate_settings_document({"deployment_type": "standalone", "auth_oidc_enabled": True})
    assert config.validate_settings_document({"deployment_type": "standalone"}).deployment_type == "standalone"


async def test_the_transition_is_one_admin_action():
    assert ACTIONS["api.auth_config_routes.change_deployment_type"] is Capability.ADMINISTER


async def test_m01_server_sign_in_is_unchanged(install):
    live = install
    async with _browser(live) as browser:
        page = await browser.get("/login")
        assert 'name="username"' in page.text and "/auth/oidc/start" in page.text
        signed = await _sign_in(browser, SECOND_PW, username="second-admin")
        assert signed.status_code == 303
        me = (await browser.get("/api/auth/session")).json()
        assert me["account_id"] == live.people["second"].id and me["deployment_type"] == "server"
    assert (await live.client.get("/api/auth/session", headers=_basic("local-user", USER_PW))).json()["account_id"] \
        == live.people["user"].id


# ── M02 / M12: Server -> Standalone ────────────────────────────────────────

async def test_m02_server_to_standalone_clears_only_the_default_admins_password(install):
    live = install
    before_people = await _people_state()
    before_demand, before_tokens = await _demand_and_tokens()
    before_file = _file()
    admin, second, user = live.session("admin"), live.session("second"), live.session("user")

    # Without the destructive confirmation nothing changes.
    refused = await _switch(live, "standalone", headers=live.headers(admin), confirm=False)
    assert refused.status_code == 409 and _file() == before_file and await _hash(DEFAULT_ADMIN_ID)
    assert session_store.resolve(second) is not None

    switched = await _switch(live, "standalone", headers=live.headers(admin))
    assert switched.status_code == 200 and switched.json() == {"ok": True, "deployment_type": "standalone"}
    assert HTTP_SESSION_COOKIE in switched.headers.get("set-cookie", "")

    stored = _file()
    assert stored["deployment_type"] == "standalone"
    assert stored["auth_password_enabled"] is False and stored["auth_oidc_enabled"] is False
    # Every other authentication setting is kept, the OIDC client secret included.
    for key, value in OIDC.items():
        assert stored[key] == value, key
    assert {k: v for k, v in stored.items() if k not in {"deployment_type", "auth_password_enabled",
                                                         "auth_oidc_enabled"}} \
        == {k: v for k, v in before_file.items() if k not in {"deployment_type", "auth_password_enabled",
                                                              "auth_oidc_enabled"}}
    # Only the default Admin's password is gone; every other account fact, token and demand row is as it was.
    assert await _hash(DEFAULT_ADMIN_ID) is None
    assert await _people_state() == before_people
    assert await _demand_and_tokens() == (before_demand, before_tokens)
    # Every browser session ended.
    assert all(session_store.resolve(token) is None for token in (admin, second, user))
    assert session_store.size == 0

    # Open Standalone: the default Admin's open principal, authenticating nothing.
    me = (await live.client.get("/api/auth/session")).json()
    assert (me["authenticated"], me["mechanism"], me["account_id"], me["deployment_type"]) == \
        (False, None, DEFAULT_ADMIN_ID, "standalone")
    # The Integration token keeps its own authority.
    token_me = (await live.client.get("/api/auth/session", headers=live.bearer)).json()
    assert (token_me["account_id"], token_me["mechanism"], token_me["role"]) == \
        (live.integration.id, "api_token", "integration")


async def test_m03_from_open_server_the_type_changes_and_nothing_else(install):
    live = install
    opened = await live.client.put("/api/auth/config", headers=live.headers(live.session("admin")),
                                   json={"auth_password_enabled": False, "auth_oidc_enabled": False,
                                         "confirm_open_mode": True})
    assert opened.status_code == 200, opened.text
    # The ordinary open-mode change keeps the default Admin's password.
    assert await _hash(DEFAULT_ADMIN_ID)
    before_people = await _people_state()
    before = await _demand_and_tokens()

    switched = await _switch(live, "standalone")         # the open principal: the default Admin
    assert switched.status_code == 200, switched.text
    assert _file()["deployment_type"] == "standalone" and await _hash(DEFAULT_ADMIN_ID) is None
    assert await _people_state() == before_people and await _demand_and_tokens() == before


# ── M04: Standalone -> Server ──────────────────────────────────────────────

async def test_m04_standalone_to_server_from_open_and_from_password_protected(install):
    live = install
    before_people = await _people_state()
    before = await _demand_and_tokens()
    assert (await _switch(live, "standalone", headers=live.headers(live.session("admin")))).status_code == 200

    # From open Standalone.
    back = await _switch(live, "server")
    assert back.status_code == 200 and _file()["deployment_type"] == "server"
    assert (_file()["auth_password_enabled"], _file()["auth_oidc_enabled"]) == (False, False)
    assert _file()["oidc_issuer_url"] == ISSUER and _file()["oidc_client_secret"] == CLIENT_SECRET

    # From password-protected Standalone.
    assert (await _switch(live, "standalone")).status_code == 200
    assert (await live.client.post("/api/account/first-password", json={"new_password": NEW_PW})).status_code == 200
    assert _file()["auth_password_enabled"] is True and await _hash(DEFAULT_ADMIN_ID)
    async with _browser(live) as browser:
        assert (await _sign_in(browser, NEW_PW)).status_code == 303
        back = await browser.put("/api/auth/deployment-type", json={"deployment_type": "server", "confirm": True},
                                 headers={"X-CSRF-Token": (await browser.get("/api/auth/session")).json()["csrf_token"]})
    assert back.status_code == 200, back.text
    stored = _file()
    assert (stored["deployment_type"], stored["auth_password_enabled"], stored["auth_oidc_enabled"]) == \
        ("server", False, False)
    assert stored["oidc_client_id"] == "dp-client" and stored["auth_session_lifetime_hours"] == 9
    assert await _hash(DEFAULT_ADMIN_ID) is None and session_store.size == 0
    # Stored enablement is what it was: the disabled User is still disabled, nobody was enabled or disabled.
    assert await _people_state() == before_people and await _demand_and_tokens() == before
    assert (await accounts.get_account(live.people["disabled"].id)).enabled is False
    for name in ("second", "user", "oidc"):
        assert (await accounts.get_account(live.people[name].id)).enabled is True
    # Server open mode: the default Admin's open principal.
    me = (await live.client.get("/api/auth/session")).json()
    assert (me["authenticated"], me["account_id"], me["deployment_type"]) == (False, DEFAULT_ADMIN_ID, "server")


# ── M05: confirmation, authority, staleness, concurrency ──────────────────

async def test_m05_refusals_change_nothing(install):
    live = install
    before_file, before_hash = _file(), await _hash(DEFAULT_ADMIN_ID)
    user = live.session("user")
    assert (await _switch(live, "standalone", headers=live.headers(user))).status_code == 403
    assert (await _switch(live, "standalone", headers=live.bearer)).status_code == 403
    assert (await _switch(live, "standalone")).status_code == 401             # no credential, auth on
    assert (await _switch(live, "server", headers=live.headers(live.session("admin")))).status_code == 409
    malformed = await live.client.put("/api/auth/deployment-type", headers=live.headers(live.session("admin")),
                                      json={"deployment_type": "desktop", "confirm": True})
    assert malformed.status_code == 422
    assert _file()["deployment_type"] == "server" and config.get_settings().deployment_type == "server"
    assert await _hash(DEFAULT_ADMIN_ID) == before_hash and session_store.resolve(user) is not None
    assert {k: v for k, v in _file().items() if k != "deployment_type"} == \
        {k: v for k, v in before_file.items() if k != "deployment_type"}


async def test_m05_a_repeated_or_concurrent_transition_acts_once(install):
    live = install
    await _standalone_with_password(live)
    enrolled_hash = await _hash(DEFAULT_ADMIN_ID)
    async with _browser(live) as browser:
        assert (await _sign_in(browser, NEW_PW)).status_code == 303
        csrf = (await browser.get("/api/auth/session")).json()["csrf_token"]
        # The type already in force: no password cleared, nothing disabled, no session ended.
        same = await browser.put("/api/auth/deployment-type", headers={"X-CSRF-Token": csrf},
                                 json={"deployment_type": "standalone", "confirm": True})
        assert same.status_code == 409 and "already Standalone" in same.json()["detail"]
        assert await _hash(DEFAULT_ADMIN_ID) == enrolled_hash and _file()["auth_password_enabled"] is True
        assert (await browser.get("/api/auth/session")).json()["authenticated"] is True

        # Two concurrent requests for Server: exactly one acts.
        results = await asyncio.gather(*(browser.put("/api/auth/deployment-type", headers={"X-CSRF-Token": csrf},
                                                     json={"deployment_type": "server", "confirm": True})
                                         for _ in range(2)))
    assert sorted(result.status_code for result in results) in ([200, 401], [200, 409])
    assert _file()["deployment_type"] == "server" and await _hash(DEFAULT_ADMIN_ID) is None


# ── M06 / M07: one person in Standalone, a password-only sign-in ──────────

async def test_m06_m07_only_the_default_admin_signs_in_and_only_by_password(install):
    live = install
    stale = live.session("second")
    await _standalone_with_password(live)

    async with _browser(live) as browser:
        page = await browser.get("/login")
        assert page.status_code == 200
        inputs = re.findall(r"<input\b[^>]*>", page.text)
        assert [i for i in inputs if 'type="hidden"' not in i] == [
            next(i for i in inputs if 'type="password"' in i)]
        assert sorted(re.search(r'name="([^"]+)"', i).group(1) for i in inputs) == ["csrf_token", "next", "password"]
        assert "username" not in page.text.lower().replace("user name", "")
        assert "/auth/oidc/start" not in page.text and "Sign in with" not in page.text

        # Another Admin's valid password, even naming that Admin, does not sign in.
        refused = await _sign_in(browser, SECOND_PW, username="second-admin")
        assert refused.status_code == 401 and "Invalid password." in refused.text
        assert HTTP_SESSION_COOKIE not in refused.headers.get("set-cookie", "")
        # The default Admin's password signs in as the default Admin, whatever username is posted.
        signed = await _sign_in(browser, NEW_PW, username="second-admin")
        assert signed.status_code == 303
        me = (await browser.get("/api/auth/session")).json()
        assert (me["authenticated"], me["account_id"]) == (True, DEFAULT_ADMIN_ID)

    # HTTP Basic: the same one person.
    assert (await live.client.get("/api/auth/session", headers=_basic("second-admin", SECOND_PW))).status_code == 401
    assert (await live.client.get("/api/auth/session", headers=_basic("local-user", USER_PW))).status_code == 401
    basic = await live.client.get("/api/auth/session", headers=_basic("local-user", NEW_PW))
    assert basic.status_code == 200 and basic.json()["account_id"] == DEFAULT_ADMIN_ID
    # A session from before, or one issued to another person now, admits nothing.
    assert (await live.client.get("/api/account", headers=live.headers(stale))).status_code == 401
    other = live.session("user")
    assert (await live.client.get("/api/account", headers=live.headers(other))).status_code == 401
    # OIDC: not offered, and no OIDC-bound account is admitted.
    assert (await live.client.get("/auth/oidc/start")).status_code == 404
    assert accounts.admitted(live.people["oidc"], config.get_settings()) is False
    assert accounts.admitted(live.people["second"], config.get_settings()) is False
    assert accounts.admitted(live.people["admin"], config.get_settings()) is True
    # The Integration token keeps its ordinary authority.
    assert (await live.client.get("/api/auth/session", headers=live.bearer)).json()["account_id"] == live.integration.id
    # Stored enablement is untouched by the suspension.
    assert (await accounts.get_account(live.people["second"].id)).enabled is True


# ── M08 / M09: Standalone first-password enrollment ────────────────────────

async def test_m08_open_standalone_sets_the_first_password_and_enables_password_sign_in(install):
    live = install
    assert (await _switch(live, "standalone", headers=live.headers(live.session("admin")))).status_code == 200
    before_people = await _people_state()
    profile = (await live.client.get("/api/account")).json()
    assert profile["has_password"] is False

    enrolled = await live.client.post("/api/account/first-password", json={"new_password": NEW_PW})
    assert enrolled.status_code == 200 and enrolled.json() == {"ok": True}
    stored = await _hash(DEFAULT_ADMIN_ID)
    assert stored and await verify_password_candidate_async(stored, NEW_PW, use_configured_hash=True)
    assert (_file()["auth_password_enabled"], _file()["auth_oidc_enabled"], _file()["deployment_type"]) == \
        (True, False, "standalone")
    assert await _people_state() == before_people
    # The open principal no longer admits anything: the browser is sent to the password-only sign-in.
    page = await live.client.get("/", headers={"Accept": "text/html"})
    assert page.status_code == 303 and page.headers["location"].startswith("/login")
    assert (await live.client.get("/api/account")).status_code == 401
    # Change Password (with its proof) is the way on from here.
    async with _browser(live) as browser:
        assert (await _sign_in(browser, NEW_PW)).status_code == 303
        csrf = (await browser.get("/api/auth/session")).json()["csrf_token"]
        changed = await browser.put("/api/account/password", headers={"X-CSRF-Token": csrf},
                                    json={"current_password": NEW_PW, "new_password": ADMIN_PW})
        assert changed.status_code == 200
    assert await verify_password_candidate_async(await _hash(DEFAULT_ADMIN_ID), ADMIN_PW, use_configured_hash=True)


async def test_m08_server_first_password_rules_are_unchanged(install):
    live = install
    # Open Server: the open principal sets no password.
    opened = await live.client.put("/api/auth/config", headers=live.headers(live.session("admin")),
                                   json={"auth_password_enabled": False, "auth_oidc_enabled": False,
                                         "confirm_open_mode": True})
    assert opened.status_code == 200
    await accounts.set_own_local_credential(DEFAULT_ADMIN_ID, clear=True)
    before = _file()
    refused = await live.client.post("/api/account/first-password", json={"new_password": NEW_PW})
    assert refused.status_code == 403 and await _hash(DEFAULT_ADMIN_ID) is None and _file() == before


async def test_m09_repeated_concurrent_and_wrong_principal_enrollment_changes_nothing_twice(install):
    live = install
    assert (await _switch(live, "standalone", headers=live.headers(live.session("admin")))).status_code == 200
    # An Integration token is not the default Admin's open principal.
    assert (await live.client.post("/api/account/first-password", headers=live.bearer,
                                   json={"new_password": NEW_PW})).status_code == 403
    assert await _hash(DEFAULT_ADMIN_ID) is None and _file()["auth_password_enabled"] is False

    results = await asyncio.gather(
        live.client.post("/api/account/first-password", json={"new_password": NEW_PW}),
        live.client.post("/api/account/first-password", json={"new_password": SECOND_PW}))
    statuses = sorted(result.status_code for result in results)
    assert statuses in ([200, 409], [200, 401]), [r.text for r in results]
    winner = NEW_PW if results[0].status_code == 200 else SECOND_PW
    assert await verify_password_candidate_async(await _hash(DEFAULT_ADMIN_ID), winner, use_configured_hash=True)
    # Once set, a retry neither replaces it nor is admitted.
    again = await live.client.post("/api/account/first-password", json={"new_password": USER_PW})
    assert again.status_code == 401
    assert await verify_password_candidate_async(await _hash(DEFAULT_ADMIN_ID), winner, use_configured_hash=True)


# ── M10: no write makes a forbidden Standalone state ───────────────────────

async def test_m10_settings_writes_cannot_enable_oidc_or_a_password_another_admin_holds(install):
    live = install
    assert (await _switch(live, "standalone", headers=live.headers(live.session("admin")))).status_code == 200
    before_file, before_people = _file(), await _people_state()
    for path, body in (("/api/auth/config", {"auth_oidc_enabled": True}),
                       ("/api/auth/config", {"auth_password_enabled": True, "auth_oidc_enabled": True}),
                       ("/api/settings", {**config.get_settings().model_dump(), "auth_oidc_enabled": True}),
                       # Only the second Admin holds a password: that is not Standalone readiness.
                       ("/api/auth/config", {"auth_password_enabled": True}),
                       ("/api/settings", {**config.get_settings().model_dump(), "auth_password_enabled": True})):
        response = await live.client.put(path, json=body)
        assert response.status_code == 409, (path, body, response.text)
        assert _file() == before_file and await _people_state() == before_people
        assert await _hash(DEFAULT_ADMIN_ID) is None
    # OIDC verification cannot be started either.
    assert (await live.client.post("/api/auth/oidc/verify-config", json={})).status_code == 401

    # Password-protected Standalone: the default Admin cannot clear the one usable password.
    assert (await live.client.post("/api/account/first-password", json={"new_password": NEW_PW})).status_code == 200
    async with _browser(live) as browser:
        assert (await _sign_in(browser, NEW_PW)).status_code == 303
        csrf = {"X-CSRF-Token": (await browser.get("/api/auth/session")).json()["csrf_token"]}
        cleared = await browser.put("/api/auth/config", headers=csrf, json={"clear_password": True})
        assert cleared.status_code == 409 and await _hash(DEFAULT_ADMIN_ID)
        oidc = await browser.post("/api/auth/oidc/verify-config", headers=csrf, json={})
        assert oidc.status_code == 409
    assert _file()["auth_oidc_enabled"] is False and _file()["auth_password_enabled"] is True


# ── M11: failures leave the previous state ─────────────────────────────────

def _failing_commit(monkeypatch):
    """The account transaction fails at its commit, after the settings were written."""
    real = accounts._transaction

    @asynccontextmanager
    async def failing():
        async with real() as db:
            yield db
            raise sqlite3.OperationalError("synthetic commit failure")

    monkeypatch.setattr(accounts, "_transaction", failing)


async def test_m11_a_failed_transition_keeps_type_credentials_and_sessions(install, monkeypatch):
    live = install
    before_file, before_hash = _file(), await _hash(DEFAULT_ADMIN_ID)
    admin = live.session("admin")

    def refuse(_settings):
        raise OSError("synthetic disk full")

    with monkeypatch.context() as patch:
        patch.setattr(auth_config_routes, "save_settings", refuse)
        failed = await _switch(live, "standalone", headers=live.headers(admin))
    assert failed.status_code == 500 and "nothing was changed" in failed.json()["detail"]
    assert _file() == before_file and config.get_settings().deployment_type == "server"
    assert await _hash(DEFAULT_ADMIN_ID) == before_hash and session_store.resolve(admin) is not None

    with monkeypatch.context() as patch:
        _failing_commit(patch)
        failed = await _switch(live, "standalone", headers=live.headers(admin))
    assert failed.status_code == 500
    assert _file() == before_file and config.get_settings().deployment_type == "server"
    assert config.get_settings().auth_password_enabled is True
    assert await _hash(DEFAULT_ADMIN_ID) == before_hash and session_store.resolve(admin) is not None


async def test_m11_a_failed_enrollment_leaves_standalone_open_and_can_be_retried(install, monkeypatch):
    import api.account_routes as account_routes

    live = install
    assert (await _switch(live, "standalone", headers=live.headers(live.session("admin")))).status_code == 200
    before_file = _file()

    def refuse(_settings):
        raise OSError("synthetic disk full")

    with monkeypatch.context() as patch:
        patch.setattr(account_routes, "save_settings", refuse)
        failed = await live.client.post("/api/account/first-password", json={"new_password": NEW_PW})
    assert failed.status_code == 500 and "nothing was changed" in failed.json()["detail"]
    assert _file() == before_file and await _hash(DEFAULT_ADMIN_ID) is None

    with monkeypatch.context() as patch:
        _failing_commit(patch)
        failed = await live.client.post("/api/account/first-password", json={"new_password": NEW_PW})
    assert failed.status_code == 500
    assert _file() == before_file and await _hash(DEFAULT_ADMIN_ID) is None
    assert config.get_settings().auth_password_enabled is False
    assert (await live.client.get("/api/account")).status_code == 200            # still open

    retried = await live.client.post("/api/account/first-password", json={"new_password": NEW_PW})
    assert retried.status_code == 200 and _file()["auth_password_enabled"] is True


# ── M15: the sign-in page reads no account ─────────────────────────────────

async def test_m15_the_standalone_sign_in_page_reads_no_account_and_names_no_one(install, monkeypatch):
    live = install
    await _standalone_with_password(live)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("the sign-in page read an account")

    with monkeypatch.context() as patch:
        for name in ("get_account", "local_credential", "default_admin_credential", "list_accounts",
                     "local_credential_status", "admin_password_holders"):
            patch.setattr(accounts, name, forbidden)
        page = await live.client.get("/login")
        status = await live.client.get("/api/auth/status")
    assert page.status_code == 200 and 'type="password"' in page.text
    assert set(status.json()) == {"authentication_required", "password_enabled", "password_ready", "oidc_enabled",
                                  "oidc_ready", "oidc_provider_name"}
    for name in ("second-admin", "local-user", "Administrator", "oidc-user"):
        assert name not in page.text
    # A failed attempt says the same, whoever is named.
    async with _browser(live) as browser:
        named = await _sign_in(browser, "wrong-password", username="second-admin")
    async with _browser(live) as browser:
        anonymous = await _sign_in(browser, "wrong-password")
    assert named.status_code == anonymous.status_code == 401
    assert re.search(r'role="alert">([^<]*)<', named.text).group(1) == \
        re.search(r'role="alert">([^<]*)<', anonymous.text).group(1) == "Invalid password."


# ── M10 / M13: the broad Settings write, restore and wipe ──────────────────
# The real Settings and backup routes (``api.routes``), as the open principal
# admits them, over a real database and configuration file.

from test_backup_restore_points import PRE_DEMAND_BACKUP, _repack, installation, served  # noqa: E402,F401


async def test_m10_the_broad_settings_write_cannot_change_the_type(served):
    _app, client, _composed, _calls = served
    document = (await client.get("/api/settings")).json()
    for name in document.pop("compatibility_fields"):
        document.pop(name, None)
    for name in ("integrations", "integration_groups", "transfer_policy", "execution_runtime_limits"):
        document.pop(name, None)
    document = {k: v for k, v in document.items() if k in config.AppSettings.model_fields}
    written = await client.put("/api/settings", json={**document, "deployment_type": "standalone",
                                                      "log_level": "DEBUG"})
    assert written.status_code == 200, written.text
    assert written.json()["deployment_type"] == "server" and written.json()["log_level"] == "DEBUG"
    assert _file()["deployment_type"] == "server" and config.get_settings().deployment_type == "server"


async def test_m13_a_previous_release_backup_restores_as_server(served, tmp_path):
    """The pinned 1.0.13 backup has no deployment type: restored over a
    Standalone installation, it is a Server again, its own password sign-in
    intact. A backup holding Standalone with OIDC enabled is refused."""
    app, client, _composed, _calls = served
    standalone = config.get_settings().model_copy(update={"deployment_type": "standalone"})
    config.save_settings(standalone)
    config.apply_settings(standalone)
    with __import__("zipfile").ZipFile(PRE_DEMAND_BACKUP) as package:
        document = json.loads(package.read("config.json"))
    assert "deployment_type" not in document

    invalid = dict(document, deployment_type="standalone", auth_oidc_enabled=True, **OIDC)
    package = _repack(PRE_DEMAND_BACKUP, tmp_path / "standalone-oidc.zip",
                      replace_members={"config.json": json.dumps(invalid)})
    added = await client.post("/api/admin/backups", content=package.read_bytes(),
                              headers={"Content-Type": "application/zip"})
    if added.status_code == 200:
        refused = await client.post("/api/admin/backups/restore", json={"id": added.json()["backup"]["id"]})
        assert refused.status_code == 400, refused.text
    else:
        assert added.status_code == 400, added.text
    assert config.get_settings().deployment_type == "standalone" and _file()["deployment_type"] == "standalone"

    added = await client.post("/api/admin/backups", content=PRE_DEMAND_BACKUP.read_bytes(),
                              headers={"Content-Type": "application/zip"})
    assert added.status_code == 200, added.text
    restored = await client.post("/api/admin/backups/restore", json={"id": added.json()["backup"]["id"]})
    assert restored.status_code == 200, restored.text
    assert config.get_settings().deployment_type == "server"
    assert _file().get("deployment_type", "server") == "server"
    assert config.get_settings().auth_password_enabled is True
    assert await accounts.usable_admin_present(config.get_settings())


async def test_m13_a_database_wipe_keeps_the_standalone_type_and_its_password(installation, monkeypatch):
    from services import db_maintenance

    await accounts.set_own_local_credential(DEFAULT_ADMIN_ID, password=ADMIN_PW)
    standalone = config.get_settings().model_copy(update={"deployment_type": "standalone",
                                                         "auth_password_enabled": True})
    config.save_settings(standalone)
    config.apply_settings(standalone)
    before_file, before_hash = _file(), await _hash(DEFAULT_ADMIN_ID)

    await db_maintenance.wipe_database(verified_quiesced=True)

    assert _file() == before_file and await _hash(DEFAULT_ADMIN_ID) == before_hash
    assert config.get_settings().deployment_type == "standalone"
    assert await accounts.usable_admin_present(config.get_settings())


# ── M09 / M11: enrollment and scoped Settings writes are serialized ────────
# Every scoped Settings writer holds the configuration write lock while it
# loads, changes and saves the document (``api.routes``). Enrollment takes the
# same lock after the authentication lock, so neither can write a document the
# other has just made stale. The interleavings are forced, never timed: each
# side is held inside its critical section until the other is seen waiting on
# the lock.

async def _waiting_on(lock: asyncio.Lock) -> None:
    for _ in range(2000):
        if getattr(lock, "_waiters", None):
            return
        await asyncio.sleep(0.005)
    raise AssertionError("nothing waited on the configuration write lock")


@pytest_asyncio.fixture
async def enrolling(served):
    """The real Settings and backup routes plus the real account routes, as
    the open Standalone principal: an open Standalone installation."""
    from api.account_routes import router as account_router

    app, client, _composed, _calls = served
    app.include_router(account_router, prefix="/api")
    standalone = config.get_settings().model_copy(update={"deployment_type": "standalone"})
    config.save_settings(standalone)
    config.apply_settings(standalone)
    assert config.get_settings().auth_password_enabled is False
    return client


async def test_m11_a_scoped_settings_write_waits_for_enrollment_and_both_are_kept(enrolling, monkeypatch):
    client = enrolling
    retries = config.get_settings().transfer_policy.execution_retry_count + 1
    inside, release = asyncio.Event(), asyncio.Event()
    commit = accounts.commit_first_password

    async def held(*args, **kwargs):
        inside.set()
        await release.wait()
        return await commit(*args, **kwargs)

    monkeypatch.setattr(accounts, "commit_first_password", held)
    enrolment = asyncio.create_task(client.post("/api/account/first-password", json={"new_password": NEW_PW}))
    await asyncio.wait_for(inside.wait(), 30)
    scoped = asyncio.create_task(client.patch("/api/transfer-policy", json={"execution_retry_count": retries}))
    await _waiting_on(config.config_write_lock())
    assert not scoped.done() and _file()["transfer_policy"]["execution_retry_count"] != retries
    release.set()
    enrolled, patched = await enrolment, await scoped
    assert enrolled.status_code == 200, enrolled.text
    assert patched.status_code == 200, patched.text
    # Neither write undid the other, on disk or in force.
    for document in (_file(), config.get_settings().model_dump()):
        assert document["auth_password_enabled"] is True and document["deployment_type"] == "standalone"
        assert document["transfer_policy"]["execution_retry_count"] == retries
    assert await verify_password_candidate_async(await _hash(DEFAULT_ADMIN_ID), NEW_PW, use_configured_hash=True)


async def test_m11_enrollment_waits_for_a_scoped_settings_write_and_both_are_kept(enrolling):
    client = enrolling
    held, release = asyncio.Event(), asyncio.Event()

    async def scoped_writer():
        # The scoped writers' contract (``api.routes``): load, change and save
        # the document under the configuration write lock.
        async with config.config_write_lock():
            current = config.load_settings()
            held.set()
            await release.wait()
            current.log_level = "DEBUG"
            config.save_settings(current)
            config.apply_settings(current)

    writer = asyncio.create_task(scoped_writer())
    await held.wait()
    enrolment = asyncio.create_task(client.post("/api/account/first-password", json={"new_password": NEW_PW}))
    await _waiting_on(config.config_write_lock())      # past Argon2, waiting for the writer
    assert await _hash(DEFAULT_ADMIN_ID) is None and _file()["auth_password_enabled"] is False
    release.set()
    await writer
    enrolled = await enrolment
    assert enrolled.status_code == 200, enrolled.text
    # The writer's stale document did not reopen the installation, and its change was kept.
    for document in (_file(), config.get_settings().model_dump()):
        assert document["auth_password_enabled"] is True and document["log_level"] == "DEBUG"
    assert await _hash(DEFAULT_ADMIN_ID)


async def test_m11_a_failed_enrollment_beside_a_scoped_write_stays_recoverable(enrolling, monkeypatch):
    client = enrolling
    retries = config.get_settings().transfer_policy.execution_retry_count + 1
    inside, release = asyncio.Event(), asyncio.Event()
    real = accounts._transaction

    @asynccontextmanager
    async def failing():
        async with real() as db:
            yield db
            inside.set()
            await release.wait()
            raise sqlite3.OperationalError("synthetic commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(accounts, "_transaction", failing)
        enrolment = asyncio.create_task(client.post("/api/account/first-password", json={"new_password": NEW_PW}))
        await asyncio.wait_for(inside.wait(), 30)
        scoped = asyncio.create_task(client.patch("/api/transfer-policy", json={"execution_retry_count": retries}))
        await _waiting_on(config.config_write_lock())
        release.set()
        failed, patched = await enrolment, await scoped
    assert failed.status_code == 500 and "nothing was changed" in failed.json()["detail"]
    assert patched.status_code == 200, patched.text
    # The failure was undone before the scoped write ran: still open, its change kept.
    for document in (_file(), config.get_settings().model_dump()):
        assert document["auth_password_enabled"] is False and document["transfer_policy"]["execution_retry_count"] == retries
    assert await _hash(DEFAULT_ADMIN_ID) is None

    retried = await client.post("/api/account/first-password", json={"new_password": NEW_PW})
    assert retried.status_code == 200, retried.text
    assert _file()["auth_password_enabled"] is True and _file()["transfer_policy"]["execution_retry_count"] == retries


# ── C01-C07: clearing the Standalone password from My Account ──────────────
# A password-protected Standalone installation's default Admin, signed in by
# its password, removes that password, proving it
# (``POST /api/account/clear-password``): the credential removed and Username
# & Password disabled as one change, every browser session ended, nothing else
# touched. The installation is then open until a first password is set again.

CLEAR = "/api/account/clear-password"


@asynccontextmanager
async def _protected_standalone(live):
    """Server -> Standalone, the first password, then the default Admin's own
    password session in a browser of its own, with its CSRF header."""
    from auth.throttle import login_challenge_rate_limiter, password_failure_throttle

    # This test's own sign-in page budget and failure throttle.
    login_challenge_rate_limiter.clear()
    password_failure_throttle.clear()
    await _standalone_with_password(live)
    async with _browser(live) as browser:
        assert (await _sign_in(browser, NEW_PW)).status_code == 303
        csrf = (await browser.get("/api/auth/session")).json()["csrf_token"]
        yield browser, {"X-CSRF-Token": csrf}
    password_failure_throttle.clear()


async def _clear(browser, headers, password=NEW_PW):
    return await browser.post(CLEAR, headers=headers, json={"current_password": password})


async def _protected_state() -> tuple:
    """What a refused or failed clear must leave: the settings on disk and in
    force, the default Admin's stored password, every session."""
    return (_file(), config.get_settings().model_dump(), await _hash(DEFAULT_ADMIN_ID),
            sorted(session_store._entries))


async def test_c01_clear_removes_the_password_disables_password_sign_in_and_ends_every_session(install, monkeypatch):
    import api.account_routes as account_routes

    live = install
    journaled = []
    journal = account_routes.journal_configuration_change

    async def recording(previous, current):
        journaled.append((previous.auth_password_enabled, current.auth_password_enabled))
        await journal(previous, current)

    async with _protected_standalone(live) as (browser, headers):
        monkeypatch.setattr(account_routes, "journal_configuration_change", recording)
        before_people, before_links = await _people_state(), await _demand_and_tokens()
        before_file = _file()
        profile = (await browser.get("/api/account")).json()
        assert (profile["deployment_type"], profile["sign_in_required"], profile["has_password"]) == \
            ("standalone", True, True)

        cleared = await _clear(browser, headers)
        assert cleared.status_code == 200 and cleared.json() == {"ok": True}
        assert cleared.headers["cache-control"] == "no-store"
        assert HTTP_SESSION_COOKIE in cleared.headers.get("set-cookie", "")
        # The credential and Username & Password, together; nothing else of the settings.
        assert await _hash(DEFAULT_ADMIN_ID) is None
        after = _file()
        assert (after["deployment_type"], after["auth_password_enabled"], after["auth_oidc_enabled"]) == \
            ("standalone", False, False)
        assert {k: v for k, v in after.items() if k != "auth_password_enabled"} == \
            {k: v for k, v in before_file.items() if k != "auth_password_enabled"}
        assert after["oidc_client_id"] == "dp-client" and after["oidc_issuer_url"] == ISSUER
        assert config.get_settings().auth_password_enabled is False
        assert journaled == [(True, False)]
        # No other account, credential, binding, token or demand moved.
        assert await _people_state() == before_people and await _demand_and_tokens() == before_links
        assert (await live.client.get("/api/auth/session", headers=live.bearer)).json()["account_id"] == \
            live.integration.id
        # Every browser session ended; the browser is now the open default Admin.
        assert session_store.size == 0
        me = (await browser.get("/api/auth/session")).json()
        assert (me["authenticated"], me["mechanism"], me["account_id"]) == (False, None, DEFAULT_ADMIN_ID)
        reopened = (await browser.get("/api/account")).json()
        assert (reopened["sign_in_required"], reopened["has_password"]) == (False, False)
        page = await browser.get("/", headers={"Accept": "text/html"})
        assert page.status_code == 200

    # A first password can be set again, and then changed with its proof.
    assert (await live.client.post("/api/account/first-password", json={"new_password": SECOND_PW})).status_code == 200
    assert _file()["auth_password_enabled"] is True
    async with _browser(live) as browser:
        assert (await _sign_in(browser, SECOND_PW)).status_code == 303
        csrf = (await browser.get("/api/auth/session")).json()["csrf_token"]
        assert (await browser.get("/api/account")).json()["sign_in_required"] is True
        changed = await browser.put("/api/account/password", headers={"X-CSRF-Token": csrf},
                                    json={"current_password": SECOND_PW, "new_password": NEW_PW})
        assert changed.status_code == 200, changed.text
    assert await verify_password_candidate_async(await _hash(DEFAULT_ADMIN_ID), NEW_PW, use_configured_hash=True)


async def test_c02_a_wrong_password_and_every_other_principal_change_nothing(install):
    live = install
    async with _protected_standalone(live) as (browser, headers):
        before = await _protected_state()
        wrong = await _clear(browser, headers, password="synthetic-wrong-password")
        assert wrong.status_code == 409 and wrong.json()["detail"] == "The current password is not correct"
        assert await _protected_state() == before
        # No session; an Integration token; no CSRF header; a field naming another account.
        assert (await live.client.post(CLEAR, json={"current_password": NEW_PW})).status_code == 401
        assert (await live.client.post(CLEAR, headers=live.bearer, json={"current_password": NEW_PW})).status_code == 403
        assert (await browser.post(CLEAR, json={"current_password": NEW_PW})).status_code == 403
        extra = await browser.post(CLEAR, headers=headers,
                                   json={"current_password": NEW_PW, "account_id": live.people["second"].id})
        assert extra.status_code == 422
        assert await _protected_state() == before


async def test_c02_server_never_clears_through_my_account(install):
    live = install
    before = _file(), await _hash(DEFAULT_ADMIN_ID), await _hash(live.people["second"].id)
    for name, password in (("admin", ADMIN_PW), ("second", SECOND_PW)):
        token = live.session(name)
        refused = await live.client.post(CLEAR, headers=live.headers(token), json={"current_password": password})
        assert refused.status_code == 403, name
        assert session_store.resolve(token) is not None
    assert (_file(), await _hash(DEFAULT_ADMIN_ID), await _hash(live.people["second"].id)) == before


async def test_c03_a_repeated_or_concurrent_clear_acts_once(install):
    live = install
    async with _protected_standalone(live) as (browser, headers):
        results = await asyncio.gather(_clear(browser, headers), _clear(browser, headers))
        statuses = sorted(result.status_code for result in results)
        assert statuses[0] == 200 and statuses[1] in (401, 403, 409), [r.text for r in results]
        assert await _hash(DEFAULT_ADMIN_ID) is None and _file()["auth_password_enabled"] is False
        # Repeated from the now-open browser: refused, nothing changed.
        settled = _file()
        assert (await _clear(browser, headers)).status_code == 403
        assert _file() == settled
    # Nor does a later Server installation become open through it.
    async with _browser(live) as browser:
        assert (await _switch(live, "server")).status_code == 200
        assert (await live.client.post(CLEAR, json={"current_password": NEW_PW})).status_code == 403


async def test_c04_a_password_or_authentication_changed_after_the_proof_is_refused(install, monkeypatch):
    live = install
    prove = accounts.prove_own_password
    async with _protected_standalone(live) as (browser, headers):
        async def then_changed(*args, **kwargs):
            proven = await prove(*args, **kwargs)
            await accounts.set_own_local_credential(DEFAULT_ADMIN_ID, password=ADMIN_PW)
            return proven

        with monkeypatch.context() as patch:
            patch.setattr(accounts, "prove_own_password", then_changed)
            stale = await _clear(browser, headers)
        assert stale.status_code == 409 and "changed meanwhile" in stale.json()["detail"]
        assert await verify_password_candidate_async(await _hash(DEFAULT_ADMIN_ID), ADMIN_PW, use_configured_hash=True)
        assert _file()["auth_password_enabled"] is True and config.get_settings().auth_password_enabled is True

    async with _browser(live) as browser:
        assert (await _sign_in(browser, ADMIN_PW)).status_code == 303
        headers = {"X-CSRF-Token": (await browser.get("/api/auth/session")).json()["csrf_token"]}

        async def then_opened(*args, **kwargs):
            proven = await prove(*args, **kwargs)
            opened = config.get_settings().model_copy(update={"auth_password_enabled": False})
            config.save_settings(opened)
            config.apply_settings(opened)
            return proven

        with monkeypatch.context() as patch:
            patch.setattr(accounts, "prove_own_password", then_opened)
            moved = await _clear(browser, headers, password=ADMIN_PW)
        assert moved.status_code == 409 and "nothing was changed" in moved.json()["detail"]
        assert await _hash(DEFAULT_ADMIN_ID) is not None


async def test_c05_a_failed_clear_keeps_the_password_settings_and_sessions_and_can_be_retried(install, monkeypatch):
    import api.account_routes as account_routes

    live = install
    async with _protected_standalone(live) as (browser, headers):
        before = await _protected_state()

        def refuse(_settings):
            raise OSError("synthetic disk full")

        with monkeypatch.context() as patch:
            patch.setattr(account_routes, "save_settings", refuse)
            failed = await _clear(browser, headers)
        assert failed.status_code == 500 and "nothing was changed" in failed.json()["detail"]
        assert await _protected_state() == before

        with monkeypatch.context() as patch:
            _failing_commit(patch)
            failed = await _clear(browser, headers)
        assert failed.status_code == 500
        # The settings written inside the transaction were restored when it failed to commit.
        assert await _protected_state() == before
        assert (await browser.get("/api/account")).json()["sign_in_required"] is True

        retried = await _clear(browser, headers)
        assert retried.status_code == 200 and _file()["auth_password_enabled"] is False
        assert await _hash(DEFAULT_ADMIN_ID) is None


async def test_c06_a_scoped_settings_write_waits_for_the_clear_and_both_are_kept(install, monkeypatch):
    live = install
    async with _protected_standalone(live) as (browser, headers):
        inside, release = asyncio.Event(), asyncio.Event()
        commit = accounts.commit_password_clear

        async def held(*args, **kwargs):
            inside.set()
            await release.wait()
            return await commit(*args, **kwargs)

        async def scoped_writer():
            async with config.config_write_lock():
                current = config.load_settings()
                current.log_level = "DEBUG"
                config.save_settings(current)
                config.apply_settings(current)

        monkeypatch.setattr(accounts, "commit_password_clear", held)
        clearing = asyncio.create_task(_clear(browser, headers))
        await asyncio.wait_for(inside.wait(), 30)
        writer = asyncio.create_task(scoped_writer())
        await _waiting_on(config.config_write_lock())
        assert not writer.done()
        release.set()
        cleared = await clearing
        await writer
        assert cleared.status_code == 200, cleared.text
        for document in (_file(), config.get_settings().model_dump()):
            assert document["auth_password_enabled"] is False and document["log_level"] == "DEBUG"
        assert await _hash(DEFAULT_ADMIN_ID) is None


async def test_c06_the_clear_waits_for_a_scoped_settings_write_and_both_are_kept(install):
    live = install
    async with _protected_standalone(live) as (browser, headers):
        held, release = asyncio.Event(), asyncio.Event()

        async def scoped_writer():
            async with config.config_write_lock():
                current = config.load_settings()
                held.set()
                await release.wait()
                current.log_level = "DEBUG"
                config.save_settings(current)
                config.apply_settings(current)

        writer = asyncio.create_task(scoped_writer())
        await held.wait()
        clearing = asyncio.create_task(_clear(browser, headers))
        await _waiting_on(config.config_write_lock())      # past Argon2, waiting for the writer
        assert await _hash(DEFAULT_ADMIN_ID) is not None and _file()["auth_password_enabled"] is True
        release.set()
        await writer
        cleared = await clearing
        assert cleared.status_code == 200, cleared.text
        # The writer's document (password sign-in still on) did not survive the clear, and its change was kept.
        for document in (_file(), config.get_settings().model_dump()):
            assert document["auth_password_enabled"] is False and document["log_level"] == "DEBUG"


async def test_c07_my_account_reads_the_effective_sign_in_requirement(install):
    live = install
    server = (await live.client.get("/api/account", headers=live.headers(live.session("admin")))).json()
    assert (server["deployment_type"], server["sign_in_required"]) == ("server", True)
    assert (await _switch(live, "standalone", headers=live.headers(live.session("admin")))).status_code == 200
    opened = (await live.client.get("/api/account")).json()
    assert (opened["deployment_type"], opened["sign_in_required"]) == ("standalone", False)
    # A stored but not operational password does not make sign-in required.
    await accounts.set_own_local_credential(DEFAULT_ADMIN_ID, password=ADMIN_PW)
    stored = (await live.client.get("/api/account")).json()
    assert (stored["has_password"], stored["sign_in_required"]) == (True, False)
