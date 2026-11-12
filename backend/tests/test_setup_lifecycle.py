"""DP 1.0.14 setup wizard, WS1: the installation's setup lifecycle and a new
Server installation's named Administrator (``services.setup_lifecycle``,
``api.setup_routes``).

Proven through the real database bootstrap (``db.migrations.v112``), the
real application and its middleware, the real sign-in form and the
application's own session store. Synthetic accounts and passwords only; every
installation is a scratch directory."""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from contextlib import asynccontextmanager, closing
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

import auth.manager as manager
import auth.passwords as passwords
import auth.transitions as transitions
import core.config as config
import db.database as database
from auth import accounts
from auth.manager import PasswordAttemptAdmission
from auth.models import Principal
from auth.permissions import ACTIONS, Capability
from auth.sessions import HTTP_SESSION_COOKIE, session_store
from db.migrations import v112
from services import db_maintenance, setup_lifecycle

pytestmark = pytest.mark.asyncio

# Synthetic secrets: test values only.
ADMIN_PASSWORD = "synthetic-setup-admin-1"
OTHER_PASSWORD = "synthetic-setup-other-2"
USER_PASSWORD = "synthetic-setup-user-3"


@pytest.fixture(autouse=True)
def fresh_admission(monkeypatch):
    """This test's own password-work admission, Argon2 slots and locks:
    process-wide asyncio primitives bind to the loop they first wait in."""
    monkeypatch.setattr(manager, "password_attempt_admission", PasswordAttemptAdmission())
    monkeypatch.setattr(passwords, "_PASSWORD_VERIFY_SLOTS", asyncio.Semaphore(2))
    monkeypatch.setattr(transitions, "authentication_configuration_lock", asyncio.Lock())
    monkeypatch.setattr(config, "_config_write_lock", asyncio.Lock())


@pytest.fixture(autouse=True)
def own_sign_in_budget():
    """This test's own sign-in page budget and failure throttle, returned
    unused: both are process-wide."""
    from auth.throttle import login_challenge_rate_limiter, password_failure_throttle

    login_challenge_rate_limiter.clear()
    password_failure_throttle.clear()
    yield
    login_challenge_rate_limiter.clear()
    password_failure_throttle.clear()


def _paths(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "data" / "debridpulse.db")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config" / "config.json")
    monkeypatch.setattr(config, "_settings", config.AppSettings())


def _row(path=None):
    with closing(sqlite3.connect(path or database.DB_PATH)) as conn:
        return conn.execute("SELECT initial_setup_complete, initial_phase, bootstrap_account_id, "
                            "bootstrap_configured_at IS NOT NULL FROM installation_setup").fetchall()


def _named_admins():
    with closing(sqlite3.connect(database.DB_PATH)) as conn:
        return conn.execute("SELECT id, username FROM accounts WHERE role='admin' AND protected IS NULL "
                            "AND deleted_at IS NULL").fetchall()


def _saved() -> dict:
    return json.loads(config.CONFIG_PATH.read_text(encoding="utf-8")) if config.CONFIG_PATH.exists() else {}


@pytest_asyncio.fixture
async def installation(tmp_path, monkeypatch):
    """A genuinely new installation: no database, no configuration file."""
    _paths(tmp_path, monkeypatch)
    await v112.migrate()
    return tmp_path


@pytest_asyncio.fixture
async def live(installation, monkeypatch):
    import main

    @asynccontextmanager
    async def operation():
        yield

    monkeypatch.setattr(main.app.state, "application", SimpleNamespace(application_operation=operation), raising=False)
    session_store.clear()

    def headers(token: str) -> dict:
        return {"Cookie": f"{HTTP_SESSION_COOKIE}={token}", "X-CSRF-Token": session_store.csrf_token(token)}

    transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        yield SimpleNamespace(client=client, headers=headers)
    session_store.clear()


async def _bootstrap(live, username="operator-one", password=ADMIN_PASSWORD, **kwargs):
    return await live.client.post("/api/setup/initial/administrator",
                                  json={"username": username, "password": password}, **kwargs)


def _cookie(response) -> str:
    match = re.search(rf"{HTTP_SESSION_COOKIE}=([^;]*)", response.headers.get("set-cookie", ""))
    return match.group(1) if match else ""


async def _sign_in(live, username, password):
    login = await live.client.get("/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', login.text).group(1)
    return await live.client.post("/login", data={"username": username, "password": password, "csrf_token": csrf})


# ── a new installation versus an existing one ──────────────────────────────

async def test_a_genuinely_new_installation_starts_its_setup_at_welcome(installation):
    assert _row() == [(0, "welcome", None, 0)]
    assert (await setup_lifecycle.initial_setup()).complete is False
    # Starting again changes nothing: the setup is never reset by a restart.
    await v112.migrate()
    assert _row() == [(0, "welcome", None, 0)]


async def test_a_fresh_database_beside_an_existing_configuration_is_not_a_new_installation(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    config.CONFIG_PATH.parent.mkdir(parents=True)
    config.CONFIG_PATH.write_text(json.dumps({"deployment_type": "server"}), encoding="utf-8")
    await v112.migrate()
    assert _row() == []
    assert (await setup_lifecycle.initial_setup()).complete is True


async def test_an_existing_database_without_the_record_is_complete(tmp_path, monkeypatch):
    # An installation from before the setup wizard: its database is already
    # current and holds no setup record.
    _paths(tmp_path, monkeypatch)
    await database.init_db()
    await v112._mark_current()
    await v112.migrate()
    assert _row() == []
    assert (await setup_lifecycle.state(accounts.DEFAULT_ADMIN_ID))["initial"] == {
        "complete": True, "phase": None, "administrator": "none"}


async def test_the_new_installation_decision_reads_the_setup_records_alone(installation, monkeypatch):
    """The pre-sign-in data boundary: what the wizard state reads is the two
    setup tables -- never an account, credential or setting."""
    statements: list[str] = []
    real = database.get_db

    @asynccontextmanager
    async def traced():
        async with real() as db:
            await db._raw.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(database, "get_db", traced)

    def no_settings():
        raise AssertionError("the setup state read a setting")

    monkeypatch.setattr(config, "get_settings", no_settings)
    assert (await setup_lifecycle.initial_setup()).complete is False
    await setup_lifecycle.state(accounts.DEFAULT_ADMIN_ID)
    tables = {name for sql in statements for name in re.findall(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+(\w+)", sql, re.I)}
    assert statements and tables == {"installation_setup", "account_setup_rerun"}


async def test_no_sign_in_page_or_public_path_reads_the_setup_state(live, monkeypatch):
    reads = []
    real = setup_lifecycle._initial

    async def counted(db):
        reads.append(True)
        return await real(db)

    monkeypatch.setattr(setup_lifecycle, "_initial", counted)
    config._settings = config.AppSettings(auth_password_enabled=True)
    await accounts.create_local_account("someone", USER_PASSWORD, role="admin")
    for path in ("/login", "/api/auth/status", "/api/health", "/api/version", "/manifest.webmanifest"):
        await live.client.get(path)
    await _sign_in(live, "someone", "synthetic-wrong-password")
    assert reads == []
    # And the setup state is no public path: without a session it is refused.
    assert (await live.client.get("/api/setup/state")).status_code == 401
    assert reads == []


# ── classification ──────────────────────────────────────────────────────────

async def test_every_setup_route_is_an_admin_action():
    names = [name for name in ACTIONS if name.startswith("api.setup_routes.")]
    assert len(names) == 6 and all(ACTIONS[name] is Capability.ADMINISTER for name in names)


# ── Standalone ──────────────────────────────────────────────────────────────

async def test_a_standalone_setup_resumes_where_it_stopped_and_finishes_without_an_administrator(live):
    config._settings = config.AppSettings(deployment_type="standalone")
    for phase in ("deployment", "download_directory", "premium_services", "getting_started"):
        moved = await live.client.put("/api/setup/initial/phase", json={"phase": phase})
        assert moved.status_code == 200 and moved.json()["initial"]["phase"] == phase
    refused = await live.client.put("/api/setup/initial/phase", json={"phase": "administrator"})
    assert refused.status_code == 409 and refused.json()["code"] == "setup.phase_unavailable"
    assert (await live.client.get("/api/setup/state")).json()["initial"]["phase"] == "getting_started"
    refused_admin = await _bootstrap(live)
    assert refused_admin.status_code == 409 and refused_admin.json()["code"] == "setup.bootstrap_unavailable"

    finished = await live.client.post("/api/setup/initial/finish")
    assert finished.status_code == 200 and finished.json() == {"complete": True, "sign_in_required": False}
    assert _row() == [(1, None, None, 0)] and _named_admins() == []
    assert config.get_settings().auth_password_enabled is False
    again = await live.client.post("/api/setup/initial/finish")
    assert again.status_code == 409 and again.json()["code"] == "setup.initial_complete"
    assert (await live.client.put("/api/setup/initial/phase", json={"phase": "welcome"})).status_code == 409


# ── Server: the named Administrator ─────────────────────────────────────────

async def test_a_server_setup_creates_a_distinct_administrator_and_continues_as_it(live):
    default_before = await accounts.get_account(accounts.DEFAULT_ADMIN_ID)
    pending = await live.client.put("/api/setup/initial/phase", json={"phase": "premium_services"})
    assert pending.status_code == 409 and pending.json()["code"] == "setup.administrator_pending"
    assert (await live.client.post("/api/setup/initial/finish")).json()["code"] == "setup.administrator_pending"
    assert (await live.client.put("/api/setup/initial/phase", json={"phase": "administrator"})).status_code == 200

    created = await _bootstrap(live)

    assert created.status_code == 200, created.text
    assert created.json()["initial"] == {"complete": False, "phase": "premium_services", "administrator": "created"}
    assert ADMIN_PASSWORD not in created.text
    [(admin_id, username)] = _named_admins()
    assert username == "operator-one" and admin_id != accounts.DEFAULT_ADMIN_ID
    # Username & Password is in force, in memory and on disk.
    assert config.get_settings().auth_password_enabled is True and _saved()["auth_password_enabled"] is True
    assert _row() == [(0, "premium_services", admin_id, 1)]
    # The protected default Admin is exactly as it was: no password, same name.
    default_after = await accounts.get_account(accounts.DEFAULT_ADMIN_ID)
    assert (default_after.username, default_after.has_password, default_after.protected, default_after.role) == \
        (default_before.username, False, "default_admin", accounts.Role.ADMIN)

    # The browser holds an ordinary password session for the new Administrator.
    token = _cookie(created)
    record = session_store.resolve(token)
    assert record is not None and record.principal.account_id == admin_id
    assert record.principal.authenticated and record.principal.role == "admin"
    # Without the CSRF token its session cannot change anything.
    no_csrf = await live.client.put("/api/setup/initial/phase", json={"phase": "getting_started"},
                                    headers={"Cookie": f"{HTTP_SESSION_COOKIE}={token}"})
    assert no_csrf.status_code == 403
    # The pages that created it are not where setup resumes again.
    back = await live.client.put("/api/setup/initial/phase", json={"phase": "deployment"}, headers=live.headers(token))
    assert back.status_code == 409 and back.json()["code"] == "setup.phase_unavailable"
    onward = await live.client.put("/api/setup/initial/phase", json={"phase": "getting_started"},
                                   headers=live.headers(token))
    assert onward.status_code == 200 and onward.json()["initial"]["phase"] == "getting_started"

    finished = await live.client.post("/api/setup/initial/finish", headers=live.headers(token))

    assert finished.status_code == 200 and finished.json() == {"complete": True, "sign_in_required": True}
    assert "Max-Age=0" in finished.headers.get("set-cookie", "") or 'expires=' in finished.headers.get("set-cookie", "").lower()
    assert _row() == [(1, None, admin_id, 1)]
    # The wizard's session is over: it no longer admits anything.
    assert session_store.resolve(token) is None
    assert (await live.client.get("/api/setup/state", headers=live.headers(token))).status_code == 401
    # Only the new Administrator's real credentials sign in again.
    assert (await _sign_in(live, "operator-one", OTHER_PASSWORD)).status_code == 401
    signed = await _sign_in(live, "operator-one", ADMIN_PASSWORD)
    assert signed.status_code == 303
    assert session_store.resolve(_cookie(signed)).principal.account_id == admin_id


async def test_an_invalid_administrator_is_refused_and_changes_nothing(live):
    for username, password, code in (("operator-one", "short7!", "account.password_too_short"),
                                     ("operator-one", "", "account.password_required"),
                                     ("   ", ADMIN_PASSWORD, "account.username_required"),
                                     ("Administrator", ADMIN_PASSWORD, "account.username_taken")):
        refused = await _bootstrap(live, username, password)
        assert refused.status_code == 409 and refused.json()["code"] == code, (username, refused.text)
        assert password not in refused.text or not password
    typed = await live.client.post("/api/setup/initial/administrator", json={"username": "x", "password": 12345678})
    assert typed.status_code == 422
    assert _named_admins() == [] and _row() == [(0, "welcome", None, 0)]
    assert config.get_settings().auth_password_enabled is False and _saved() == {}


async def test_two_concurrent_submissions_create_one_administrator(live):
    first, second = await asyncio.gather(_bootstrap(live, "operator-one", ADMIN_PASSWORD),
                                         _bootstrap(live, "operator-two", OTHER_PASSWORD))
    statuses = sorted((first.status_code, second.status_code))
    assert statuses == [200, 409], (first.text, second.text)
    loser = first if first.status_code == 409 else second
    assert loser.json()["code"] in {"setup.administrator_exists", "setup.bootstrap_unavailable"}
    assert len(_named_admins()) == 1 and _cookie(loser) == ""


async def test_after_the_administrator_exists_no_open_or_signed_in_caller_can_create_another(live):
    created = await _bootstrap(live)
    token = _cookie(created)
    live.client.cookies.clear()
    # A stale browser with no session: sign-in is enabled now, so it is refused.
    assert (await _bootstrap(live, "operator-two", OTHER_PASSWORD)).status_code == 401
    # A lost response replayed: the same submission is not a second account.
    assert (await _bootstrap(live)).status_code == 401
    # The signed-in Administrator is not the open principal.
    signed = await _bootstrap(live, "operator-two", OTHER_PASSWORD, headers=live.headers(token))
    assert signed.status_code == 403
    assert len(_named_admins()) == 1
    # A lost response is recovered by ordinary sign-in, and setup resumes after the Administrator.
    signed_in = await _sign_in(live, "operator-one", ADMIN_PASSWORD)
    state = await live.client.get("/api/setup/state", headers=live.headers(_cookie(signed_in)))
    assert state.json()["initial"] == {"complete": False, "phase": "premium_services", "administrator": "created"}


async def test_a_failed_setting_write_after_the_account_commits_is_finished_by_the_same_submission(live, monkeypatch):
    real = config.save_settings
    failures = iter([True])

    def failing(settings):
        if next(failures, False):
            raise OSError("synthetic write failure")
        return real(settings)

    monkeypatch.setattr(config, "save_settings", failing)

    failed = await _bootstrap(live)

    assert failed.status_code == 500 and failed.json()["code"] == "setup.administrator_not_enabled"
    assert _cookie(failed) == "" and ADMIN_PASSWORD not in failed.text
    [(admin_id, _name)] = _named_admins()
    # Never enabled without its Administrator, and still open: recoverable.
    assert config.get_settings().auth_password_enabled is False and _saved() == {}
    assert _row() == [(0, "welcome", admin_id, 0)]
    assert (await live.client.get("/api/setup/state")).json()["initial"]["administrator"] == "pending"
    # Another submission is refused, never a second Administrator.
    other = await _bootstrap(live, "operator-two", OTHER_PASSWORD)
    assert other.status_code == 409 and other.json()["code"] == "setup.administrator_exists"
    wrong = await _bootstrap(live, "operator-one", OTHER_PASSWORD)
    assert wrong.status_code == 409 and wrong.json()["code"] == "setup.administrator_exists"

    finished = await _bootstrap(live)

    assert finished.status_code == 200, finished.text
    assert _named_admins() == [(admin_id, "operator-one")]
    assert config.get_settings().auth_password_enabled is True and _saved()["auth_password_enabled"] is True
    assert _row() == [(0, "premium_services", admin_id, 1)]
    assert session_store.resolve(_cookie(finished)).principal.account_id == admin_id


async def test_an_interrupted_administrator_is_finished_at_the_next_start(installation, monkeypatch):
    # The crash window: the account committed, the process stopped before the setting.
    monkeypatch.setattr(config, "save_settings", lambda settings: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(setup_lifecycle.SetupRefused):
        await setup_lifecycle.establish_administrator(
            SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")), "operator-one", ADMIN_PASSWORD)
    [(admin_id, _name)] = _named_admins()
    monkeypatch.undo()
    _paths(installation, monkeypatch)  # the restarted process: settings as stored
    monkeypatch.setattr(transitions, "authentication_configuration_lock", asyncio.Lock())
    monkeypatch.setattr(config, "_config_write_lock", asyncio.Lock())
    assert config.get_settings().auth_password_enabled is False

    await setup_lifecycle.settle_interrupted_bootstrap()

    assert config.get_settings().auth_password_enabled is True and _saved()["auth_password_enabled"] is True
    assert _row() == [(0, "premium_services", admin_id, 1)] and len(_named_admins()) == 1
    # Settling again changes nothing.
    await setup_lifecycle.settle_interrupted_bootstrap()
    assert _row() == [(0, "premium_services", admin_id, 1)]


async def test_a_failed_record_after_sign_in_is_enabled_answers_truthfully_and_creates_no_second_admin(live, monkeypatch):
    """The failure is injected into the ``_settle`` transaction that runs after
    Username & Password was written: the answer is a failure with no session,
    never a 200 carrying a stale state."""
    real = setup_lifecycle._settle
    calls = []

    async def failing(db):
        calls.append(_saved().get("auth_password_enabled"))
        if len(calls) == 1:
            raise sqlite3.OperationalError("synthetic settle failure")
        return await real(db)

    monkeypatch.setattr(setup_lifecycle, "_settle", failing)

    failed = await _bootstrap(live)

    assert calls == [True]  # it ran after the setting was on disk
    assert failed.status_code == 500 and failed.json()["code"] == "setup.administrator_not_recorded"
    assert _cookie(failed) == "" and ADMIN_PASSWORD not in failed.text
    [(admin_id, _name)] = _named_admins()
    assert config.get_settings().auth_password_enabled is True and _saved()["auth_password_enabled"] is True
    assert _row() == [(0, "welcome", admin_id, 0)]
    # Sign-in is enabled now: no further submission is admitted, so none can create another.
    live.client.cookies.clear()
    assert (await _bootstrap(live)).status_code == 401
    assert (await _bootstrap(live, "operator-two", OTHER_PASSWORD)).status_code == 401
    # Ordinary sign-in is the way on; the next setup step records the Administrator.
    token = _cookie(await _sign_in(live, "operator-one", ADMIN_PASSWORD))
    state = await live.client.get("/api/setup/state", headers=live.headers(token))
    assert state.json()["initial"] == {"complete": False, "phase": "welcome", "administrator": "pending"}
    onward = await live.client.put("/api/setup/initial/phase", json={"phase": "premium_services"},
                                   headers=live.headers(token))
    assert onward.status_code == 200, onward.text
    assert onward.json()["initial"] == {"complete": False, "phase": "premium_services", "administrator": "created"}
    assert _row() == [(0, "premium_services", admin_id, 1)] and _named_admins() == [(admin_id, "operator-one")]


async def test_a_server_finish_is_refused_once_sign_in_by_password_is_disabled(live):
    created = await _bootstrap(live)
    [(admin_id, _name)] = _named_admins()
    # Username & Password disabled afterwards (its ordinary authority's effect):
    # the installation is open again, and its caller the open principal.
    config._settings = config.get_settings().model_copy(update={"auth_password_enabled": False})
    live.client.cookies.clear()

    refused = await live.client.post("/api/setup/initial/finish")

    assert refused.status_code == 409 and refused.json()["code"] == "setup.finish_needs_password"
    assert _row() == [(0, "premium_services", admin_id, 1)]
    assert (await setup_lifecycle.initial_setup()).complete is False
    assert _cookie(created)


async def test_a_server_finish_is_refused_to_a_caller_without_a_browser_session(live):
    import base64

    await _bootstrap(live)
    [(admin_id, _name)] = _named_admins()
    live.client.cookies.clear()
    basic = base64.b64encode(f"operator-one:{ADMIN_PASSWORD}".encode()).decode()

    refused = await live.client.post("/api/setup/initial/finish", headers={"Authorization": f"Basic {basic}"})

    assert refused.status_code == 403 and refused.json()["code"] == "auth.forbidden"
    assert _row() == [(0, "premium_services", admin_id, 1)]
    # No credential at all: the boundary refuses it before Finish is reached.
    assert (await live.client.post("/api/setup/initial/finish")).status_code == 401
    assert (await setup_lifecycle.initial_setup()).complete is False


async def test_a_finished_installation_never_reopens_the_administrator(live):
    config._settings = config.AppSettings(deployment_type="standalone")
    await live.client.post("/api/setup/initial/finish")
    config._settings = config.AppSettings()
    refused = await _bootstrap(live)
    assert refused.status_code == 409 and refused.json()["code"] == "setup.initial_complete"
    assert _named_admins() == []


# ── an Admin's rerun ────────────────────────────────────────────────────────

async def test_a_rerun_is_its_own_admins_and_never_reopens_the_initial_setup(live):
    early = await live.client.put("/api/setup/rerun", json={"phase": "welcome"})
    assert early.status_code == 409 and early.json()["code"] == "setup.initial_incomplete"
    created = await _bootstrap(live)
    ann_token = _cookie(created)
    await live.client.post("/api/setup/initial/finish", headers=live.headers(ann_token))
    ann = await accounts.local_credential("operator-one")
    bea = await accounts.create_local_account("bea", OTHER_PASSWORD, role="admin")
    una = await accounts.create_local_account("una", USER_PASSWORD, role="user")

    def session(account):
        principal = Principal.password_session(account)
        token, _ = session_store.create(principal, lifetime_seconds=3600, credential_version=principal.credential_version)
        return token

    ann_token, bea_token, una_token = session(ann[0]), session(bea), session(una)
    started = await live.client.put("/api/setup/rerun", json={"phase": "administrator"}, headers=live.headers(ann_token))
    assert started.status_code == 200 and started.json()["rerun"] == {"phase": "administrator"}
    assert started.json()["initial"]["complete"] is True
    # Admin B does not inherit Admin A's rerun.
    assert (await live.client.get("/api/setup/state", headers=live.headers(bea_token))).json()["rerun"] is None
    # A User may neither read nor change a setup.
    for method, path, body in (("GET", "/api/setup/state", None), ("PUT", "/api/setup/rerun", {"phase": "welcome"}),
                               ("DELETE", "/api/setup/rerun", None), ("POST", "/api/setup/initial/finish", None)):
        refused = await live.client.request(method, path, json=body, headers=live.headers(una_token))
        assert refused.status_code == 403, (method, path)
    # A rerun never creates an Administrator, even mid-rerun.
    assert (await _bootstrap(live, "operator-two", OTHER_PASSWORD, headers=live.headers(ann_token))).status_code == 403
    # Dismissed: nothing reverts and the initial setup stays complete.
    ended = await live.client.delete("/api/setup/rerun", headers=live.headers(ann_token))
    assert ended.status_code == 200 and ended.json()["rerun"] is None and ended.json()["initial"]["complete"] is True
    assert _row()[0][0] == 1


async def test_a_standalone_rerun_has_no_administrator_page(live):
    config._settings = config.AppSettings(deployment_type="standalone")
    await live.client.post("/api/setup/initial/finish")
    refused = await live.client.put("/api/setup/rerun", json={"phase": "administrator"})
    assert refused.status_code == 409 and refused.json()["code"] == "setup.phase_unavailable"
    kept = await live.client.put("/api/setup/rerun", json={"phase": "premium_services"})
    assert kept.status_code == 200 and kept.json()["rerun"] == {"phase": "premium_services"}


async def test_a_removed_admins_rerun_is_retired_with_it(installation):
    with closing(sqlite3.connect(database.DB_PATH)) as conn:
        conn.execute("UPDATE installation_setup SET initial_setup_complete=1")
        conn.commit()
    bea = await accounts.create_local_account("bea", OTHER_PASSWORD, role="admin")
    await setup_lifecycle.set_rerun(bea.id, "welcome")
    await accounts.delete_account(accounts.DEFAULT_ADMIN_ID, bea.id)
    assert (await setup_lifecycle.state(bea.id))["rerun"] is None


# ── wipe ────────────────────────────────────────────────────────────────────

async def test_a_wipe_keeps_the_setup_and_every_rerun(installation):
    bea = await accounts.create_local_account("bea", OTHER_PASSWORD, role="admin")
    with closing(sqlite3.connect(database.DB_PATH)) as conn:
        conn.execute("UPDATE installation_setup SET initial_setup_complete=1, initial_phase=NULL")
        conn.commit()
    await setup_lifecycle.set_rerun(bea.id, "premium_services")
    before = _row()
    result = await db_maintenance.wipe_database(verified_quiesced=True)
    assert not {"installation_setup", "account_setup_rerun"} & set(result["wiped_tables"])
    assert _row() == before
    assert (await setup_lifecycle.state(bea.id))["rerun"] == {"phase": "premium_services"}
