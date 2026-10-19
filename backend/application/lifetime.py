"""The application lifetime owner: the ONE startup and shutdown sequence.

Process startup (``main.lifespan``) and a restore's replacement universe both
start an application through ``start_application`` -- schema bootstrap and
startup material reconciliation, then integrations and the scheduler, whose
first cycles are the startup recovery -- so restored state is reconciled by
exactly the machinery that reconciles any state DebridPulse starts with.
Provider and executor readiness are never part of what makes an application
available: they converge after it is.

``restore_backup`` is the whole-state replacement: validate, quiesce and
drain, safety backup, stage and validate, journaled swap, then a freshly
composed application over the restored state. The pre-restore application
object -- engine, registry, executors and every in-memory native handle -- is
dropped, never reused.
"""
from __future__ import annotations

import asyncio
import logging

from core import scheduler
from core.logging_utils import sanitize_exception

logger = logging.getLogger("debridpulse.lifetime")

_RESTORE_LOCK = asyncio.Lock()
_UNCHANGED = "The current DebridPulse state was left unchanged."


class RestoreFailed(Exception):
    """A restore that did not replace the current state."""

    def __init__(self, status_code: int, reason: str = ""):
        self.status_code = status_code
        self.message = " ".join(part for part in ("Backup could not be restored.", reason, _UNCHANGED) if part)
        super().__init__(self.message)


class RestoreUnresolved(RestoreFailed):
    """A restore whose swap failed and could not be undone: the state was NOT
    left unchanged. Storage is sealed until the next start reverses it."""

    def __init__(self):
        self.status_code = 503
        self.message = ("Backup could not be restored, and the previous state could not be put back yet. "
                        "DebridPulse has stopped using its stored state; restart it to put the previous state back.")
        Exception.__init__(self, self.message)


async def prepare_settings_and_migrate():
    """Establish one sanitized settings authority before migration decisions.

    v1.0.12 migration can mint durable executor mutation authority, so it must
    bind that authority from the sanitized settings and nothing else.  Keep the tolerant load/repair behavior, but fail
    closed if a safe effective settings object cannot be established before the
    ownership-sensitive migration.
    """
    try:
        from core.config import get_settings, apply_settings, save_settings, legacy_paused_input
        from core.config_validator import validate_and_sanitise

        raw = get_settings()
        cfg = validate_and_sanitise(raw)
        if cfg is not raw:
            save_settings(cfg)
            apply_settings(cfg)
    except Exception as exc:
        detail = sanitize_exception(exc)
        logger.error(
            "Configuration validation failed before ownership-sensitive migration: %s",
            detail,
        )
        raise RuntimeError(
            "Configuration validation failed before ownership-sensitive migration"
        ) from exc

    from db.migrations.v112 import migrate

    await migrate(globally_paused=legacy_paused_input())
    # A local credential still in the configuration file (a pre-account
    # installation's, or the operator's offline recovery input) now belongs to
    # the default Admin account; only then does it leave the file.
    from auth.accounts import import_legacy_credential
    from core.config import legacy_credential_input, retire_legacy_credential_input

    credential = legacy_credential_input()
    if credential is not None:
        if await import_legacy_credential(*credential):
            logger.warning("The configuration file's local credential was installed on the default Admin account")
        retire_legacy_credential_input()
    # The pre-account global API token never becomes any account's credential.
    from auth.accounts import retire_legacy_api_token
    from core.config import CONFIG_PATH

    if await retire_legacy_api_token(CONFIG_PATH.parent / "api-token.json"):
        logger.warning("The global API token was retired; issue a token for an Integration account")
    return cfg


async def start_application(application):
    """Start one application; it is servable when this returns.

    Only DebridPulse-owned core state blocks: the repository and engine with
    startup material reconciliation, and interrupted post-processing. Every
    integration is then begun off this path, and the scheduler started. The
    scheduler loops that need started integrations wait for them, and their
    first cycles -- provider inventory, pending resolution, execution
    reconciliation, integration maintenance -- are the startup recovery, so no
    separate recovery pass runs here. Nothing in this decision names an
    integration.
    """
    await application.engine.initialize()
    await application.engine.recover_postprocessing()
    application.begin_integrations()
    await scheduler.start_scheduler(application)


async def stop_application(application):
    try:
        await scheduler.stop_scheduler()
    finally:
        try:
            await application.stop_integrations()
        except Exception as exc:
            logger.warning("Integration shutdown failed: %s", sanitize_exception(exc))


def _composer(state):
    compose = getattr(state, "compose", None)
    if compose is None:
        from application.composition import compose
    return compose


async def _start_composed(state):
    """A fresh application over whatever state is on disk now."""
    from core.config import apply_settings, load_settings

    apply_settings(load_settings())
    await prepare_settings_and_migrate()
    application = _composer(state)()
    try:
        await start_application(application)
    except BaseException:
        await stop_application(application)
        raise
    return application


async def _reopen(application, *, stopped_integrations, intent, scheduler_was_running: bool) -> None:
    """Put back exactly what a refused restore changed on an application whose
    state was never replaced: the integrations it stopped, the operator's and
    external clients' pause intent its drain overrode, the scheduler it
    stopped. Nothing else was touched, so nothing else is started again."""
    if stopped_integrations:
        await application.start_integrations(only=stopped_integrations)
    await application.restore_pause_intent(intent)
    if scheduler_was_running:
        await scheduler.start_scheduler(application)


def _seal_unresolved(application, why: str) -> None:
    """The stored state is neither restored nor reversed: until the next start
    reverses it from the journal, no session may open (and so recreate) the
    database and no work -- a settings write, another restore -- may act on
    it. Called while the restore still holds database maintenance."""
    from db.database import database_maintenance_gate

    database_maintenance_gate.seal()
    application.seal_admission()
    logger.critical("%s; DebridPulse has stopped using its stored state. Restart it to put the previous state "
                    "back.", why)


async def _to_completion(step) -> bool:
    """Run one swap step (activate, rollback) in its thread and wait for it
    however this task is cancelled: files are never left moving while the
    restore unwinds. The step's own failure is raised; returns whether a
    cancellation arrived meanwhile."""
    task = asyncio.ensure_future(asyncio.to_thread(step))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    return cancelled


async def _journal_restore(event_type: str, severity: str, message: str, point_id: str, *,
                           detail: str | None = None) -> bool:
    from db.event_journal import JournalEvent, record_now

    return await record_now(JournalEvent("administration", event_type, severity, message, "backup",
                                         subject_id=point_id, detail=detail))


async def restore_backup(state, point_id: str) -> dict:
    """Replace the current DebridPulse state with one restore point.

    ``state`` is the ASGI application state holding the live ``application``
    (and, for tests, an alternative ``compose``)."""
    from application.service import IntegrationStopFailed
    from auth.sessions import session_store
    from db.database import database_maintenance
    from services import backup

    if _RESTORE_LOCK.locked():
        raise RestoreFailed(409, "A restore is already in progress.")
    from db.database import database_maintenance_gate

    if database_maintenance_gate.sealed:
        # An earlier restore is unresolved: nothing may be validated, staged
        # or swapped until the next start reverses it.
        raise RestoreUnresolved()
    # The restore point being restored and the safety backup holding the
    # replaced state outlive every retention pass until the restore ends.
    async with _RESTORE_LOCK, backup.holding() as hold:
        hold(point_id)
        current = state.application
        try:
            await backup.validate_restore_point(point_id)
        except backup.BackupRejected as exc:
            raise RestoreFailed(404 if exc.reason == "not_found" else 400, exc.detail) from None
        stopped_integrations = ()
        refused = None
        async with current.state_replacement_admission():
            # Recorded once admitted work has drained, before the drain pauses
            # everything: the operator's own pauses and every external
            # client's hold, which the drain overrides.
            intent = await current.pause_intent()
            scheduler_was_running = scheduler.scheduler_running()
            await scheduler.stop_scheduler()
            try:
                drain = await current.drain_executions()
            except Exception as exc:
                logger.warning("Restore refused: executions could not be drained: %s", sanitize_exception(exc))
                refused = RestoreFailed(409, "Active transfers could not be stopped safely.")
            if refused is None:
                try:
                    stopped_integrations = await current.stop_integrations()
                except IntegrationStopFailed as exc:
                    stopped_integrations = exc.stopped
                    logger.warning("Restore refused: an integration could not be stopped: %s",
                                   sanitize_exception(exc.__cause__ or exc))
                    refused = RestoreFailed(409, "Active transfers could not be stopped safely.")
            if refused is None:
                staged = None
                try:
                    async with database_maintenance():
                        # The drain's pause is the restore's own: the safety
                        # backup, and a reversal, keep the recorded intent.
                        await current.record_pause_intent(intent)
                        safety = await backup.create_restore_point()
                        hold(safety.id)
                        staged = await backup.stage_restore(point_id)
                        await asyncio.to_thread(staged.validate)
                        if await current.repository.live_executions():
                            raise RuntimeError("a pre-restore execution is still live")
                        try:
                            cancelled = await _to_completion(staged.activate)
                        except Exception as exc:
                            if staged.unresolved:
                                _seal_unresolved(current, "Restore failed and could not be reversed: "
                                                 + sanitize_exception(exc))
                            raise
                        if cancelled:
                            # Swapped, but this restore will never start or
                            # commit it: the journal reverses it at the next start.
                            _seal_unresolved(current, "Restore was interrupted after its swap")
                            raise asyncio.CancelledError
                except Exception as exc:
                    if staged is not None and staged.unresolved:
                        # Part of the restored state is in place and the
                        # previous state could not be put back: nothing is
                        # reopened on it or reported as unchanged. The journal
                        # kept beside the database reverses it at the next start.
                        raise RestoreUnresolved() from None
                    if staged is not None:
                        staged.discard()
                    logger.warning("Restore refused before activation: %s", sanitize_exception(exc))
                    refused = RestoreFailed(400, exc.detail if isinstance(exc, backup.BackupRejected) else "")
            if refused is None:
                # Swapped. The pre-restore application stays closed while the
                # restored state starts, and is never reopened.
                try:
                    restored = await _start_composed(state)
                except asyncio.CancelledError:
                    # Swapped, and the restored state will never finish
                    # starting: the replaced application must not serve the
                    # swapped files. The journal reverses them at the next start.
                    async with database_maintenance():
                        _seal_unresolved(current, "Restore was interrupted while the restored state started")
                    raise
                except Exception as exc:
                    logger.error("Restored state failed to start; reverting: %s", sanitize_exception(exc))
                    async with database_maintenance():
                        try:
                            cancelled = await _to_completion(staged.rollback)
                        except Exception as rollback_exc:
                            # The restored state could not start and the
                            # previous one could not be put back: unresolved,
                            # exactly as a swap whose reversal failed.
                            staged.unresolved = True
                            _seal_unresolved(current, "Restore failed after activation and could not be reversed: "
                                             + sanitize_exception(rollback_exc))
                            raise RestoreUnresolved() from None
                    if cancelled:
                        # The previous state is whole again (nothing to seal),
                        # but its application stays stopped until a restart.
                        logger.critical("Restore was interrupted after its rollback; the previous state is back. "
                                        "Restart DebridPulse to resume processing.")
                        raise asyncio.CancelledError
                    previous = await _start_composed(state)
                    await previous.restore_pause_intent(intent)
                    state.application = previous
                    # The pre-restore database is active again: the failure is
                    # recorded there, the only journal that now exists.
                    await _journal_restore("administration.restore_failed", "error",
                                           "Restore failed after activation; the previous state was reinstated",
                                           point_id)
                    raise RestoreFailed(400) from None
                staged.commit()
                state.application = restored
        if refused is not None:
            # Nothing was replaced. Reopened only once admission is released,
            # so the restarted scheduler cannot bounce off the maintenance gate.
            await _reopen(current, stopped_integrations=stopped_integrations,
                          intent=intent, scheduler_was_running=scheduler_was_running)
            await _journal_restore("administration.restore_refused", "warning",
                                   "Restore refused; nothing was replaced", point_id)
            raise refused
        # Authentication is part of the restored state: no session issued
        # under the replaced configuration survives it.
        session_store.clear()
        logger.warning("Backup restored: %s (safety backup %s)", point_id, safety.id)
        # Written only now, into the restored database that is active: the
        # replaced database is gone, and the restored journal is exactly the
        # one the backup held (nothing recorded after it was taken survives).
        recorded = await _journal_restore("administration.backup_restored", "warning", "Backup restored", point_id,
                                          detail=f"Safety backup {safety.id} holds the replaced state")
        return {"ok": True, "restored": staged.point.public(), "safety_backup": safety.public(), "drain": drain,
                "journal_recorded": recorded}
