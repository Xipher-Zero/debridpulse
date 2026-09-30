"""The application lifetime owner: the ONE startup and shutdown sequence.

Process startup (``main.lifespan``) and a restore's replacement universe both
start an application through ``start_application`` -- schema bootstrap and
startup material reconciliation, integrations, the startup recovery pass and
the scheduler -- so restored state is reconciled by exactly the machinery
that reconciles any state DebridPulse starts with.

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
    return cfg


async def start_application(application):
    await application.engine.initialize()
    await application.engine.recover_postprocessing()
    await application.start_integrations()
    try:
        await application.recover()
    except Exception as exc:
        logger.warning("Startup reconciliation deferred: %s", sanitize_exception(exc))
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


async def _reopen(application, *, stopped_integrations, paused_by_restore: bool, scheduler_was_running: bool) -> None:
    """Put back exactly what a refused restore changed on an application whose
    state was never replaced: the integrations it stopped, the pause it
    imposed, the scheduler it stopped. Nothing else was touched, so nothing
    else is started again."""
    if stopped_integrations:
        await application.start_integrations(only=stopped_integrations)
    if paused_by_restore:
        await application.resume_all()
    if scheduler_was_running:
        await scheduler.start_scheduler(application)


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
    async with _RESTORE_LOCK:
        current = state.application
        try:
            await backup.validate_restore_point(point_id)
        except backup.BackupRejected as exc:
            raise RestoreFailed(404 if exc.reason == "not_found" else 400, exc.detail) from None
        was_paused = await current.repository.globally_paused()
        scheduler_was_running = scheduler.scheduler_running()
        stopped_integrations = ()
        refused = None
        async with current.state_replacement_admission():
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
                        safety = await backup.create_restore_point()
                        staged = await backup.stage_restore(point_id)
                        await asyncio.to_thread(staged.validate)
                        if await current.repository.live_executions():
                            raise RuntimeError("a pre-restore execution is still live")
                        await asyncio.to_thread(staged.activate)
                except Exception as exc:
                    if staged is not None:
                        staged.discard()
                    logger.warning("Restore refused before activation: %s", sanitize_exception(exc))
                    refused = RestoreFailed(400, exc.detail if isinstance(exc, backup.BackupRejected) else "")
            if refused is None:
                # Swapped. The pre-restore application stays closed while the
                # restored state starts, and is never reopened.
                try:
                    restored = await _start_composed(state)
                except Exception as exc:
                    logger.error("Restored state failed to start; reverting: %s", sanitize_exception(exc))
                    async with database_maintenance():
                        await asyncio.to_thread(staged.rollback)
                    previous = await _start_composed(state)
                    if not was_paused:
                        await previous.resume_all()
                    state.application = previous
                    raise RestoreFailed(400) from None
                staged.commit()
                state.application = restored
        if refused is not None:
            # Nothing was replaced. Reopened only once admission is released,
            # so the restarted scheduler cannot bounce off the maintenance gate.
            await _reopen(current, stopped_integrations=stopped_integrations,
                          paused_by_restore=not was_paused, scheduler_was_running=scheduler_was_running)
            raise refused
        # Authentication is part of the restored state: no session issued
        # under the replaced configuration survives it.
        session_store.clear()
        logger.warning("Backup restored: %s (safety backup %s)", point_id, safety.id)
        return {"ok": True, "restored": staged.point.public(), "safety_backup": safety.public(), "drain": drain}
