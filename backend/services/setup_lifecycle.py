"""The setup wizard's lifecycle: the ONE owner of whether this installation's
initial setup has finished, where an unfinished one resumes, the one named
Administrator a new Server installation's setup creates, and each Admin's
own unfinished rerun.

Three facts stay apart:

* **The installation's initial setup** (``installation_setup``, one row). The
  database bootstrap writes it, incomplete, only when it creates a genuinely
  new installation (``db.migrations.v112``: a fresh database and no
  configuration file), in the transaction that declares the new database
  current -- so it is durable before anything is served. An installation with
  no row predates the wizard and is complete; nothing else -- accounts,
  credentials, providers or any setting -- is read to decide that. Only
  Finish completes it, and nothing makes it incomplete again.
* **An Admin's own rerun** (``account_setup_rerun``, keyed by the account). It
  never touches the installation's setup and is never another Admin's.
* **Everything the wizard configures** stays with its own owner (the default
  language, the deployment type, the download folder, providers): the phase
  recorded here is only where the wizard resumes, never a copy of a value.

The named Administrator (``establish_administrator``) is the one high-risk
transition. SQLite (the account) and ``config.json`` (Username & Password)
cannot commit together, so they commit in the order whose interruption is
recoverable: the account, with this installation's record of it, first; then
the setting. Interrupted between the two, the installation is still open, an
Administrator exists, and the record names it: the same username and password
submitted again, or the next start (``settle_interrupted_bootstrap``),
finishes the change -- no second Administrator is ever created, and Username
& Password is never enabled without an Administrator who can sign in with it.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import NoReturn
from datetime import datetime, timezone

from core.localization import Coded

logger = logging.getLogger("debridpulse.setup")

INITIAL_PHASES = ("welcome", "deployment", "download_directory", "administrator",
                  "premium_services", "getting_started")
RERUN_PHASES = ("welcome", "download_directory", "administrator", "premium_services", "getting_started")
# The pages after a Server installation's Administrator exists: the only ones
# its initial setup resumes on once that Administrator is established.
_AFTER_ADMINISTRATOR = frozenset({"premium_services", "getting_started"})


class SetupRefused(Coded, Exception):
    """A setup step the installation's current state does not allow; nothing
    was changed. ``status`` is the HTTP status it is answered with."""

    def __init__(self, message: str, *, code: str, status: int = 409):
        super().__init__(message, code=code)
        self.status = status


@dataclass(frozen=True, slots=True)
class InitialSetup:
    complete: bool
    phase: str | None
    administrator_id: str
    administrator_configured: bool


_COMPLETE = InitialSetup(True, None, "", False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _record(row) -> InitialSetup:
    if row is None:
        return _COMPLETE
    return InitialSetup(bool(row["initial_setup_complete"]), row["initial_phase"],
                        str(row["bootstrap_account_id"] or ""), bool(row["bootstrap_configured_at"]))


async def _initial(db) -> InitialSetup:
    return _record(await db.fetchone(
        "SELECT initial_setup_complete, initial_phase, bootstrap_account_id, bootstrap_configured_at "
        "FROM installation_setup WHERE id=1"))


@asynccontextmanager
async def _transaction():
    from db.database import get_db

    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            await db.rollback()
            raise
        await db.commit()


def _lock():
    import auth.transitions as transitions

    return transitions.authentication_configuration_lock


# ── the new installation ────────────────────────────────────────────────────

async def record_new_installation(db) -> None:
    """Mark a genuinely new installation's initial setup as not yet done, at
    the Welcome page. Called once, by the database bootstrap, inside the
    transaction that declares the new database current."""
    await db.execute(
        "INSERT OR IGNORE INTO installation_setup(id, initial_setup_complete, initial_phase, updated_at) "
        "VALUES(1, 0, 'welcome', ?)", (_now(),))


# ── reads ───────────────────────────────────────────────────────────────────

async def initial_setup() -> InitialSetup:
    from db.database import get_db

    async with get_db() as db:
        return await _initial(db)


async def state(account_id: str) -> dict:
    """What the wizard shows the signed-in Admin: the installation's initial
    setup and this account's own rerun. Read from the two setup records
    alone."""
    from db.database import get_db

    async with get_db() as db:
        initial = await _initial(db)
        rerun = await db.fetchone("SELECT phase FROM account_setup_rerun WHERE account_id=?", (str(account_id or ""),))
    administrator = ("created" if initial.administrator_configured
                     else "pending" if initial.administrator_id else "none")
    return {
        "initial": {"complete": initial.complete, "phase": initial.phase, "administrator": administrator},
        "rerun": {"phase": rerun["phase"]} if rerun is not None else None,
    }


# ── the initial setup ───────────────────────────────────────────────────────

def _incomplete(initial: InitialSetup) -> None:
    if initial.complete:
        raise SetupRefused("Initial setup is already complete", code="setup.initial_complete")


async def _settle(db) -> InitialSetup:
    """Record that the Administrator's Username & Password is enabled: the
    setup then resumes after the Administrator page."""
    await db.execute(
        "UPDATE installation_setup SET bootstrap_configured_at=?, updated_at=?, initial_phase=CASE "
        "WHEN initial_phase IN ('premium_services','getting_started') THEN initial_phase "
        "ELSE 'premium_services' END WHERE id=1", (_now(), _now()))
    return await _initial(db)


async def _settled(db, settings) -> InitialSetup:
    """The initial setup, with an Administrator whose Username & Password is
    in force recorded as established (a confirmation interrupted after the
    setting was written)."""
    from auth.policy import password_auth_enabled

    initial = await _initial(db)
    if initial.administrator_id and not initial.administrator_configured and password_auth_enabled(settings):
        initial = await _settle(db)
    return initial


def _administrator_required(initial: InitialSetup, settings) -> None:
    from auth.policy import standalone_deployment

    if not standalone_deployment(settings) and not initial.administrator_configured:
        raise SetupRefused("Create the Administrator before continuing", code="setup.administrator_pending")


async def advance_initial(phase: str) -> None:
    """Record the page the initial setup resumes on, once the page before it
    has committed its own configuration through that configuration's owner."""
    from auth.policy import standalone_deployment
    from core.config import get_settings

    async with _lock():
        settings = get_settings()
        async with _transaction() as db:
            initial = await _settled(db, settings)
            _incomplete(initial)
            if phase == "administrator" and standalone_deployment(settings):
                raise SetupRefused("That setup step is not available for this installation",
                                   code="setup.phase_unavailable")
            if phase in _AFTER_ADMINISTRATOR:
                _administrator_required(initial, settings)
            elif initial.administrator_configured:
                # The Administrator exists: the pages that chose and created it
                # are never where its setup resumes again.
                raise SetupRefused("That setup step is not available for this installation",
                                   code="setup.phase_unavailable")
            await db.execute("UPDATE installation_setup SET initial_phase=?, updated_at=? WHERE id=1",
                             (phase, _now()))


# The sessions a Server installation's Finish ends: a browser session.
_SESSION_MECHANISMS = frozenset({"password_session", "oidc_session"})


async def finish_initial(principal) -> None:
    """Finish: the initial setup is complete, written before the caller ends
    the wizard's session. A Server installation finishes only once its named
    Administrator is established, while Username & Password is still enabled,
    and only for a caller signed in with a browser session -- the session
    Finish then ends. Anything else is refused and the setup stays
    incomplete."""
    from auth.policy import password_auth_enabled, standalone_deployment
    from core.config import get_settings

    async with _lock():
        settings = get_settings()
        async with _transaction() as db:
            initial = await _settled(db, settings)
            _incomplete(initial)
            _administrator_required(initial, settings)
            if not standalone_deployment(settings):
                if not password_auth_enabled(settings):
                    raise SetupRefused("Username & Password must be enabled before the setup of a Server installation can finish", code="setup.finish_needs_password")
                mechanism = str(getattr(principal, "mechanism", None) or "")
                if not (getattr(principal, "authenticated", False) and mechanism in _SESSION_MECHANISMS):
                    raise SetupRefused("Forbidden", code="auth.forbidden", status=403)
            await db.execute(
                "UPDATE installation_setup SET initial_setup_complete=1, initial_phase=NULL, completed_at=?, "
                "updated_at=? WHERE id=1", (_now(), _now()))


# ── the named Administrator ─────────────────────────────────────────────────

def _open_server(settings) -> bool:
    from auth.policy import interactive_auth_enabled, standalone_deployment

    return not standalone_deployment(settings) and not interactive_auth_enabled(settings)


def _already_created() -> NoReturn:
    raise SetupRefused("The Administrator for this installation has already been created; sign in with its username and password", code="setup.administrator_exists")


def _unavailable() -> NoReturn:
    raise SetupRefused("The Administrator can only be created during the initial setup of a Server installation without sign-in", code="setup.bootstrap_unavailable")


async def establish_administrator(request, username: str, password: str):
    """Create a new Server installation's named Administrator and enable
    Username & Password for it, as one recoverable transition; return the
    Administrator's account once both are in force.

    Admitted only while the installation's initial setup is incomplete, it is
    a Server, and no sign-in is enabled -- re-checked under the authentication
    configuration lock, then the configuration write lock (the established
    order). Argon2 runs first, outside both, in the bounded password-work
    admission. The account and this installation's record of it commit in one
    transaction, refused if another submission created an Administrator first;
    only then is the setting written. An Administrator already recorded but
    not yet enabled is finished by its own username and password, proven as a
    sign-in proves them; any other submission is refused."""
    from auth import accounts
    from auth.manager import peer_key, verify_local_credentials
    from core.config import apply_settings, config_write_lock, get_settings, journal_configuration_change, save_settings

    current = get_settings()
    initial = await initial_setup()
    _incomplete(initial)
    if initial.administrator_id and initial.administrator_configured:
        _already_created()
    if not _open_server(current):
        _unavailable()
    enabled = current.model_copy(update={"auth_password_enabled": True}, deep=True)
    claimed = initial.administrator_id
    verifier = None
    if claimed:
        proven = await verify_local_credentials(request, username, password, settings=enabled)
        if proven is None or proven.id != claimed:
            _already_created()
    else:
        accounts.new_password_rule(password)
        verifier = await accounts.admitted_verifier(password, peer=peer_key(request))

    async with _lock(), config_write_lock():
        current = get_settings()
        initial = await initial_setup()
        _incomplete(initial)
        if initial.administrator_id != claimed or initial.administrator_configured:
            _already_created()
        if not _open_server(current):
            _unavailable()
        enabled = current.model_copy(update={"auth_password_enabled": True}, deep=True)
        if verifier is not None:
            async def claim(db, account_id: str) -> None:
                # The decision and its record share the account's transaction.
                recorded = await _initial(db)
                _incomplete(recorded)
                if recorded.administrator_id:
                    _already_created()
                await db.execute("UPDATE installation_setup SET bootstrap_account_id=?, updated_at=? WHERE id=1",
                                 (account_id, _now()))

            administrator = await accounts.create_initial_admin(username, verifier, settings=enabled, claim=claim)
        else:
            administrator = await accounts.get_account(claimed)
            if administrator is None or administrator.role is not accounts.Role.ADMIN \
                    or not accounts.admitted(administrator, enabled):
                _already_created()
        try:
            save_settings(enabled)
        except Exception as exc:  # noqa: BLE001 - reported; the record lets the same submission finish it
            logger.error("Username & Password could not be enabled for the new Administrator: %s",
                         type(exc).__name__)
            raise SetupRefused("The Administrator was created, but Username & Password could not be enabled yet. Submit the same username and password again.",
                               code="setup.administrator_not_enabled", status=500) from None
        apply_settings(enabled)
        recorded = True
        try:
            async with _transaction() as db:
                await _settle(db)
        except Exception as exc:  # noqa: BLE001 - reported below; both halves are in force
            recorded = False
            logger.error("The new Administrator's setup record could not be updated: %s", type(exc).__name__)
    await journal_configuration_change(current, enabled)
    logger.warning("Initial setup created the named Administrator; Username & Password is enabled")
    if not recorded:
        # The Administrator and its sign-in are in force, but setup's record
        # of them is not: no session is issued from a state the wizard would
        # read as unfinished. Ordinary sign-in is the way on; the next setup
        # step, or the next start, records it (``_settled``,
        # ``settle_interrupted_bootstrap``), and no submission can create
        # another Administrator now that sign-in is enabled.
        raise SetupRefused("The Administrator was created and Username & Password is enabled, but setup could not record it. Sign in with the new username and password to continue.", code="setup.administrator_not_recorded", status=500)
    return administrator


async def settle_interrupted_bootstrap() -> None:
    """At start: finish a named Administrator's transition that was
    interrupted after its account committed. Username & Password is enabled
    for it when it is still an Administrator who can sign in with it on a
    Server installation; otherwise nothing is changed."""
    from auth import accounts
    from auth.policy import password_auth_enabled, standalone_deployment
    from core.config import apply_settings, config_write_lock, get_settings, save_settings

    initial = await initial_setup()
    if initial.complete or not initial.administrator_id or initial.administrator_configured:
        return
    async with _lock(), config_write_lock():
        current = get_settings()
        if not password_auth_enabled(current):
            enabled = current.model_copy(update={"auth_password_enabled": True}, deep=True)
            administrator = await accounts.get_account(initial.administrator_id)
            usable = (administrator is not None and administrator.role is accounts.Role.ADMIN
                      and administrator.has_password and accounts.admitted(administrator, enabled))
            if standalone_deployment(current) or not usable:
                logger.warning("An interrupted initial Administrator setup could not be finished at start")
                return
            save_settings(enabled)
            apply_settings(enabled)
            logger.warning("An interrupted initial Administrator setup was finished: Username & Password is enabled")
        async with _transaction() as db:
            await _settle(db)


# ── an Admin's rerun ────────────────────────────────────────────────────────

async def set_rerun(account_id: str, phase: str) -> None:
    """Start or continue this Admin's own rerun at ``phase``. A rerun belongs
    to the installation's finished setup: it never changes it."""
    from auth.policy import standalone_deployment
    from core.config import get_settings

    async with _transaction() as db:
        if not (await _initial(db)).complete:
            raise SetupRefused("Initial setup is not complete yet", code="setup.initial_incomplete")
        if phase == "administrator" and standalone_deployment(get_settings()):
            raise SetupRefused("That setup step is not available for this installation",
                               code="setup.phase_unavailable")
        await db.execute(
            "INSERT INTO account_setup_rerun(account_id, phase, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(account_id) DO UPDATE SET phase=excluded.phase, updated_at=excluded.updated_at",
            (str(account_id), phase, _now()))


async def end_rerun(account_id: str) -> None:
    """End this Admin's rerun, finished or dismissed: nothing it committed is
    reverted, and the installation's setup is untouched."""
    async with _transaction() as db:
        await db.execute("DELETE FROM account_setup_rerun WHERE account_id=?", (str(account_id),))
