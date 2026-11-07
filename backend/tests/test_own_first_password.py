"""DP 1.0.14 My Account: an OIDC account's first local password.

A signed-in human account with no local password sets its FIRST one from My
Account (``POST /api/account/first-password``): its signed-in session is the
authority, there is no current password to prove, and the account is always
the signed-in one -- the request names none. It is refused, and the stored
credential kept, once ANY password exists (set by an Admin, or by an earlier
submission), decided under the authentication lock in the write's own
transaction. The account keeps its OIDC identity, username, role and profile;
like every credential change its sessions end. ``PUT /api/account/password``
(Change Password, with its proof) is unchanged.

Refusals are proven through the real application and middleware: sessions
issued by the application's own session store (an OIDC session under a real
OIDC configuration, as the callback issues it), a real Integration bearer
token, and requests with no credential. Synthetic accounts and passwords
only; no identity provider is contacted."""
from __future__ import annotations

import asyncio
import re
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

import core.config as config
import db.database as database
import auth.manager as manager
import auth.passwords as passwords
import auth.transitions as transitions
from auth import accounts
from auth.accounts import AccountRefused, Role
from auth.manager import PasswordAttemptAdmission, PasswordAuthenticationBusy
from auth.models import Principal
from auth.oidc_version import oidc_configuration_version
from auth.passwords import verify_password_candidate_async
from auth.permissions import ACTIONS, Capability
from auth.sessions import HTTP_SESSION_COOKIE, session_store

pytestmark = pytest.mark.asyncio

ISSUER = "https://idp.invalid/application/o/dp"
# Synthetic secrets: test values only.
FIRST = "synthetic-first-pass-1"
SECOND = "synthetic-second-pass-2"
ADMIN_SET = "synthetic-admin-set-3"
LOCAL = "synthetic-local-pass-4"


def _settings(**updates) -> config.AppSettings:
    values = dict(auth_password_enabled=True, auth_oidc_enabled=True, oidc_provider_name="Synthetic IdP",
                  oidc_issuer_url=ISSUER, oidc_client_id="dp-client", oidc_client_secret="synthetic-client-secret",
                  oidc_scopes=["openid", "profile", "email"], oidc_allow_all=True, public_base_url="https://pulse.invalid")
    values.update(updates)
    return config.AppSettings(**values)


async def _hash(account_id: str) -> str | None:
    async with database.get_db() as db:
        row = await db.fetchone("SELECT password_hash FROM account_local_credentials WHERE account_id=?", (account_id,))
    return str(row["password_hash"]) if row else None


async def _bindings(account_id: str) -> list[tuple[str, str]]:
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT issuer, subject FROM account_oidc_bindings WHERE account_id=?", (account_id,))
    return [(row["issuer"], row["subject"]) for row in rows]


async def _oidc_person(subject: str, username: str):
    return await accounts.resolve_oidc(ISSUER, subject, {"iss": ISSUER, "sub": subject, "preferred_username": username},
                                       default_role="user")


@pytest.fixture(autouse=True)
def fresh_admission(monkeypatch):
    """This test's own password-work admission, Argon2 worker slots and
    authentication lock: each is process-wide state, and an asyncio primitive
    binds to the event loop it is first contended in."""
    admission = PasswordAttemptAdmission()
    monkeypatch.setattr(manager, "password_attempt_admission", admission)
    monkeypatch.setattr(passwords, "_PASSWORD_VERIFY_SLOTS", asyncio.Semaphore(2))
    monkeypatch.setattr(transitions, "authentication_configuration_lock", asyncio.Lock())
    return admission


class BlockingHash:
    """A stand-in for Argon2 that holds its worker thread until released."""

    def __init__(self):
        self.release = threading.Event()
        self.started = 0
        self._lock = threading.Lock()

    def __call__(self, password):
        with self._lock:
            self.started += 1
        assert self.release.wait(timeout=10)
        return f"$synthetic-verifier${len(password)}"


async def _until(condition, attempts=400):
    for _ in range(attempts):
        if condition():
            return True
        await asyncio.sleep(0.01)
    return condition()


@pytest_asyncio.fixture
async def accounts_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "first-password.db")
    await database.init_db()
    monkeypatch.setattr(config, "_settings", _settings())


@pytest_asyncio.fixture
async def live(accounts_db, monkeypatch):
    """The real application and middleware, password and OIDC sign-in enabled."""
    import main

    @asynccontextmanager
    async def operation():
        yield

    monkeypatch.setattr(main.app.state, "application", SimpleNamespace(application_operation=operation), raising=False)
    session_store.clear()
    people = {"oidc": await _oidc_person("sub-oidc", "oidc-person"),
              "other": await _oidc_person("sub-other", "other-person"),
              "local": await accounts.create_local_account("local-person", LOCAL, role="user")}
    integration = await accounts.create_integration_account("Synthetic Integration")
    _record, bearer = await accounts.create_token(integration.id, "probe")
    version = oidc_configuration_version(config.get_settings())

    def session(name: str) -> str:
        account = people[name]
        principal = (Principal.password_session(account) if name == "local" else
                     Principal.oidc_session(f"{ISSUER}|sub-{name}", display_name=account.username,
                                            credential_version=version).bound_to(account))
        token, _ = session_store.create(principal, lifetime_seconds=3600, credential_version=principal.credential_version)
        return token

    def headers(token: str, csrf: str | None = None) -> dict:
        return {"Cookie": f"{HTTP_SESSION_COOKIE}={token}",
                "X-CSRF-Token": session_store.csrf_token(token) if csrf is None else csrf}

    transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        yield SimpleNamespace(client=client, people=people, session=session, headers=headers,
                              bearer={"Authorization": f"Bearer {bearer}"}, integration=integration)
    session_store.clear()


# ── the route and its classification ───────────────────────────────────────

async def test_the_route_is_the_callers_own_session_action():
    assert ACTIONS["api.account_routes.set_own_first_password"] is Capability.SESSION
    assert ACTIONS["api.account_routes.change_own_password"] is Capability.SESSION


# ── through the real application ───────────────────────────────────────────

async def test_an_oidc_account_sets_its_first_password_and_keeps_its_identity(live):
    person = live.people["oidc"]
    token = live.session("oidc")
    profile = await live.client.get("/api/account", headers=live.headers(token))
    assert profile.status_code == 200 and profile.json()["has_password"] is False
    assert profile.json()["username"] == "oidc-person"
    before = await accounts.get_account(person.id)
    bindings = await _bindings(person.id)

    response = await live.client.post("/api/account/first-password", headers=live.headers(token),
                                      json={"new_password": FIRST})
    assert response.status_code == 200 and response.json() == {"ok": True}
    stored = await _hash(person.id)
    assert stored and FIRST not in stored and await verify_password_candidate_async(stored, FIRST, use_configured_hash=True)

    # The same account: identity, username, role, state and profile untouched; its OIDC identity kept.
    after = await accounts.get_account(person.id)
    assert after.has_password
    assert (after.username, after.role, after.enabled, after.display_name, after.email, after.protected) == \
        (before.username, before.role, before.enabled, before.display_name, before.email, before.protected)
    assert await _bindings(person.id) == bindings == [(ISSUER, "sub-oidc")]
    assert (await _oidc_person("sub-oidc", "oidc-person")).id == person.id
    # The installation's sign-in settings are not touched.
    assert config.get_settings().auth_password_enabled is True and config.get_settings().auth_oidc_enabled is True

    # Like every credential change, the account's sessions end: sign in again.
    assert session_store.resolve(token) is None
    assert (await live.client.get("/api/account", headers=live.headers(token))).status_code == 401

    # The new local password signs in through the real sign-in form, as the same account.
    login = await live.client.get("/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', login.text).group(1)
    signed = await live.client.post("/login", data={"username": "oidc-person", "password": FIRST, "csrf_token": csrf})
    assert signed.status_code == 303
    cookie = signed.cookies.get(HTTP_SESSION_COOKIE) or next(iter(signed.cookies.values()))
    record = session_store.resolve(cookie)
    assert record is not None and record.principal.account_id == person.id

    # Afterwards it is Change Password's: Set is refused for it.
    fresh = live.session("oidc")
    again = await live.client.post("/api/account/first-password", headers=live.headers(fresh), json={"new_password": SECOND})
    assert again.status_code == 409 and "already has a password" in again.json()["detail"]
    assert await _hash(person.id) == stored


async def test_an_account_with_a_password_keeps_change_password_and_its_proof(live):
    person = live.people["local"]
    stored = await _hash(person.id)
    token = live.session("local")
    assert (await live.client.get("/api/account", headers=live.headers(token))).json()["has_password"] is True
    refused = await live.client.post("/api/account/first-password", headers=live.headers(token), json={"new_password": FIRST})
    assert refused.status_code == 409 and "use Change Password" in refused.json()["detail"]
    assert await _hash(person.id) == stored
    # Change Password still proves the current password.
    wrong = await live.client.put("/api/account/password", headers=live.headers(token),
                                  json={"current_password": "synthetic-wrong-pass", "new_password": SECOND})
    assert wrong.status_code == 409 and wrong.json()["detail"] == "The current password is not correct"
    assert await _hash(person.id) == stored


async def test_no_credential_no_session_and_no_human_session_are_refused(live):
    person = live.people["oidc"]
    token = live.session("oidc")
    body = {"new_password": FIRST}
    # No credential at all.
    assert (await live.client.post("/api/account/first-password", json=body)).status_code == 401
    # A real Integration bearer token: an Integration has no password.
    assert (await live.client.post("/api/account/first-password", headers=live.bearer, json=body)).status_code == 403
    # The session's CSRF token is required, and must be its own.
    assert (await live.client.post("/api/account/first-password", headers=live.headers(token, csrf=""), json=body)).status_code == 403
    assert (await live.client.post("/api/account/first-password", headers=live.headers(token, csrf="synthetic-wrong"),
                                   json=body)).status_code == 403
    # Nothing was written by any of them, and the session still stands.
    assert await _hash(person.id) is None and await _hash(live.integration.id) is None
    assert session_store.resolve(token) is not None


async def test_the_request_names_no_account_and_a_named_one_is_refused(live):
    caller, other = live.people["oidc"], live.people["other"]
    token = live.session("oidc")
    for extra in ({"account_id": other.id}, {"username": "other-person"}, {"current_password": ""}):
        injected = await live.client.post("/api/account/first-password", headers=live.headers(token),
                                          json={"new_password": FIRST, **extra})
        assert injected.status_code == 422, extra
        assert FIRST not in injected.text, extra
    assert await _hash(caller.id) is None and await _hash(other.id) is None
    # Without one it is the caller's own, and only the caller's.
    other_token = live.session("other")
    assert (await live.client.post("/api/account/first-password", headers=live.headers(token),
                                   json={"new_password": FIRST})).status_code == 200
    assert await _hash(caller.id) and await _hash(other.id) is None
    assert session_store.resolve(other_token) is not None


async def test_policy_failures_write_nothing(live):
    person = live.people["oidc"]
    token = live.session("oidc")
    short = await live.client.post("/api/account/first-password", headers=live.headers(token), json={"new_password": "short7!"})
    assert short.status_code == 409 and short.json()["detail"] == "A password must be at least 8 characters"
    assert "short7!" not in short.text
    empty = await live.client.post("/api/account/first-password", headers=live.headers(token), json={"new_password": ""})
    assert empty.status_code == 422 and "new_password" in empty.text and '"input":""' not in empty.text.replace(" ", "")
    typed = await live.client.post("/api/account/first-password", headers=live.headers(token), json={"new_password": 12345678})
    assert typed.status_code == 422
    assert await _hash(person.id) is None and session_store.resolve(token) is not None


async def test_a_stale_set_after_an_admins_reset_is_refused_and_the_admins_password_kept(live):
    person = live.people["oidc"]
    token = live.session("oidc")
    # The dialog was opened when the account had no password ...
    assert (await live.client.get("/api/account", headers=live.headers(token))).json()["has_password"] is False
    # ... then an Admin set one: the account's sessions ended with it.
    await accounts.reset_password(accounts.DEFAULT_ADMIN_ID, person.id, ADMIN_SET)
    admin_hash = await _hash(person.id)
    stale = await live.client.post("/api/account/first-password", headers=live.headers(token), json={"new_password": FIRST})
    assert stale.status_code == 401
    # A session issued after the reset is refused by the precondition itself.
    fresh = live.session("oidc")
    refused = await live.client.post("/api/account/first-password", headers=live.headers(fresh), json={"new_password": FIRST})
    assert refused.status_code == 409
    assert await _hash(person.id) == admin_hash
    assert await verify_password_candidate_async(admin_hash, ADMIN_SET, use_configured_hash=True)


async def test_a_duplicate_submission_keeps_the_first(live):
    person = live.people["oidc"]
    token = live.session("oidc")
    first = await live.client.post("/api/account/first-password", headers=live.headers(token), json={"new_password": FIRST})
    assert first.status_code == 200
    stored = await _hash(person.id)
    # The same form submitted again: its session ended with the first.
    duplicate = await live.client.post("/api/account/first-password", headers=live.headers(token), json={"new_password": SECOND})
    assert duplicate.status_code == 401
    assert await _hash(person.id) == stored


# ── the account owner, directly ────────────────────────────────────────────

async def test_racing_first_passwords_create_exactly_one(accounts_db):
    person = await _oidc_person("sub-race", "race-person")
    outcomes = await asyncio.gather(*(accounts.set_own_first_password(person.id, password) for password in (FIRST, SECOND)),
                                    return_exceptions=True)
    won = [password for password, outcome in zip((FIRST, SECOND), outcomes) if outcome is None]
    lost = [outcome for outcome in outcomes if isinstance(outcome, AccountRefused)]
    assert len(won) == 1 and len(lost) == 1 and "already has a password" in str(lost[0])
    stored = await _hash(person.id)
    assert await verify_password_candidate_async(stored, won[0], use_configured_hash=True)
    loser = SECOND if won[0] == FIRST else FIRST
    assert not await verify_password_candidate_async(stored, loser, use_configured_hash=True)


async def test_an_admin_first_then_set_refuses_and_keeps_the_admins(accounts_db):
    person = await _oidc_person("sub-admin-first", "admin-first-person")
    await accounts.reset_password(accounts.DEFAULT_ADMIN_ID, person.id, ADMIN_SET)
    admin_hash = await _hash(person.id)
    with pytest.raises(AccountRefused, match="already has a password"):
        await accounts.set_own_first_password(person.id, FIRST)
    assert await _hash(person.id) == admin_hash


async def test_set_then_set_again_keeps_the_first(accounts_db):
    person = await _oidc_person("sub-twice", "twice-person")
    await accounts.set_own_first_password(person.id, FIRST)
    stored = await _hash(person.id)
    with pytest.raises(AccountRefused):
        await accounts.set_own_first_password(person.id, SECOND)
    assert await _hash(person.id) == stored


async def test_integration_and_disabled_accounts_are_refused(accounts_db):
    integration = await accounts.create_integration_account("Synthetic Integration")
    with pytest.raises(AccountRefused, match="Integration"):
        await accounts.set_own_first_password(integration.id, FIRST)
    person = await _oidc_person("sub-disabled", "disabled-person")
    await accounts.set_enabled(accounts.DEFAULT_ADMIN_ID, person.id, False)
    with pytest.raises(AccountRefused, match="disabled"):
        await accounts.set_own_first_password(person.id, FIRST)
    assert await _hash(integration.id) is None and await _hash(person.id) is None
    assert (await accounts.get_account(person.id)).role is Role.USER


async def test_with_local_sign_in_off_the_password_is_kept_and_the_setting_untouched(accounts_db, monkeypatch):
    monkeypatch.setattr(config, "_settings", _settings(auth_password_enabled=False))
    person = await _oidc_person("sub-off", "off-person")
    await accounts.set_own_first_password(person.id, FIRST)
    assert await _hash(person.id)
    assert config.get_settings().auth_password_enabled is False
    # Local sign-in follows the installation's setting, as before: not available while it is off.
    from auth.policy import password_auth_enabled

    assert password_auth_enabled(config.get_settings()) is False


# ── bounded password work ──────────────────────────────────────────────────

async def test_a_known_ineligible_request_is_refused_before_any_hashing(accounts_db, monkeypatch):
    hashed = []
    monkeypatch.setattr(passwords, "hash_password", lambda password: hashed.append(1) or "unused")
    local = await accounts.create_local_account("local-person", LOCAL, role="user")
    stored = await _hash(local.id)
    integration = await accounts.create_integration_account("Synthetic Integration")
    for account_id, reason in ((local.id, "already has a password"), (integration.id, "Integration"), ("no-such-account", "does not exist")):
        with pytest.raises(AccountRefused, match=reason):
            await accounts.set_own_first_password(account_id, FIRST, peer="peer-a")
    with pytest.raises(AccountRefused, match="at least 8"):
        await accounts.set_own_first_password(local.id, "short7!", peer="peer-a")
    assert hashed == [] and manager.password_attempt_admission.pending == 0
    assert await _hash(local.id) == stored


@pytest.mark.parametrize("peers", [("peer-a",) * 6, tuple(f"peer-{n}" for n in range(6))], ids=["one-peer", "many-peers"])
async def test_excess_concurrent_attempts_are_refused_at_once_and_work_stays_bounded(accounts_db, monkeypatch, peers):
    # Per peer at most 4 pending; in all, here, at most 4 (16 in the application).
    monkeypatch.setattr(manager, "password_attempt_admission", PasswordAttemptAdmission(max_pending=4, max_per_peer=4))
    blocking = BlockingHash()
    monkeypatch.setattr(passwords, "hash_password", blocking)
    person = await _oidc_person("sub-burst", "burst-person")
    attempts = [asyncio.create_task(accounts.set_own_first_password(person.id, f"{FIRST}-{n}", peer=peer))
                for n, peer in enumerate(peers)]
    assert await _until(lambda: blocking.started == 2)
    await asyncio.sleep(0.1)
    # Two refused at once (429 at the route), four admitted, only two hashing.
    refused = [task for task in attempts if task.done()]
    assert len(refused) == 2 and all(isinstance(task.exception(), PasswordAuthenticationBusy) for task in refused)
    assert manager.password_attempt_admission.pending == 4 and blocking.started == 2
    blocking.release.set()
    outcomes = await asyncio.gather(*attempts, return_exceptions=True)
    admitted = [outcome for task, outcome in zip(attempts, outcomes) if task not in refused]
    # Of the admitted, exactly one sets the password; the rest find it set.
    assert sum(outcome is None for outcome in admitted) == 1
    assert all(isinstance(outcome, AccountRefused) and "already has a password" in str(outcome)
               for outcome in admitted if outcome is not None)
    assert blocking.started == 4 and manager.password_attempt_admission.pending == 0
    assert passwords._PASSWORD_VERIFY_SLOTS._value == 2


async def test_a_cancelled_request_holds_its_admission_until_its_hash_finishes(accounts_db, monkeypatch):
    blocking = BlockingHash()
    monkeypatch.setattr(passwords, "hash_password", blocking)
    person = await _oidc_person("sub-cancel", "cancel-person")
    request = asyncio.create_task(accounts.set_own_first_password(person.id, FIRST, peer="peer-a"))
    assert await _until(lambda: blocking.started == 1)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    request.cancel()
    # The caller is gone; its hashing thread is not: its admission and worker slot stay taken.
    await asyncio.sleep(0.1)
    assert manager.password_attempt_admission.pending == 1
    assert passwords._PASSWORD_VERIFY_SLOTS._value == 1
    blocking.release.set()
    assert await _until(lambda: manager.password_attempt_admission.pending == 0)
    assert passwords._PASSWORD_VERIFY_SLOTS._value == 2
    # A cancelled request writes nothing.
    assert await _hash(person.id) is None


async def test_a_saturated_admission_answers_429_and_writes_nothing(live):
    person = live.people["oidc"]
    token = live.session("oidc")
    admission = manager.password_attempt_admission
    # The ASGI transport's peer is 127.0.0.1: fill its share.
    while admission.acquire("127.0.0.1"):
        pass
    busy = await live.client.post("/api/account/first-password", headers=live.headers(token), json={"new_password": FIRST})
    assert busy.status_code == 429 and busy.headers["retry-after"] == "2"
    assert await _hash(person.id) is None and session_store.resolve(token) is not None
    for _ in range(admission.max_per_peer):
        admission.release("127.0.0.1")
    done = await live.client.post("/api/account/first-password", headers=live.headers(token), json={"new_password": FIRST})
    assert done.status_code == 200 and await _hash(person.id)
    assert admission.pending == 0
