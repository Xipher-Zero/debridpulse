"""Database Wipe owns its own quiescence and preserves the operator's pause intent.

The operator confirms the destructive intent (typed WIPE); DebridPulse
establishes what the wipe needs itself. Inside the maintenance admission the
wipe drains every native execution through the ONE generic drain
(``ApplicationService.drain_executions``) -- the same one a restore uses -- so
no native execution owned by the database being deleted is still alive when
it is deleted. A PAUSED native execution is parked, not released, and is not
enough. Afterwards the operator's pre-wipe global pause intent is restored
exactly: running resumes, paused stays paused.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

import api.routes as routes
import services.db_maintenance as db_maintenance
from application.service import ApplicationService
from backup_support import prepare_backup_installation
from core.config import AppSettings
from executor_fakes import LedgerExecutor, artifact_of, ledger_capabilities, ledger_core, submit_ledger
from fastapi import HTTPException
from services import backup
from transfers.models import ContinuationCapability, ExecutionState, TransferState

pytestmark = pytest.mark.asyncio

PARKING = frozenset({ContinuationCapability.FULL_RESTART, ContinuationCapability.NATIVE_PRIVATE_RESUME,
                     ContinuationCapability.NATIVE_QUIESCE})


async def _wipe_setup(tmp_path, monkeypatch, *, backup_before_wipe=True):
    core = await ledger_core(tmp_path, monkeypatch, executors=lambda authorize: (
        LedgerExecutor(authorize, capabilities=ledger_capabilities(continuation=PARKING)),))
    transfer = await submit_ledger(core, "wiped-item")
    artifact = await artifact_of(core, transfer.id)
    core.executor.run(artifact.execution)
    await core.engine.reconcile_executions()
    await prepare_backup_installation(tmp_path, monkeypatch)
    settings = AppSettings(db_wipe_enabled=True, db_backup_before_wipe=backup_before_wipe)
    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    monkeypatch.setattr(routes.scheduler_runtime, "scheduler_running", lambda: False)

    # Observe the destructive boundary itself: what is alive at the moment the
    # canonical deletion owner runs, and whether the safety backup precedes it.
    seen = SimpleNamespace(live_at_wipe=None, backups_at_wipe=None, native_at_wipe=None, calls=0)
    wipe = db_maintenance.wipe_database

    async def observed_wipe(**kwargs):
        seen.calls += 1
        seen.live_at_wipe = await core.repository.live_executions()
        seen.backups_at_wipe = len(backup.list_restore_points())
        seen.native_at_wipe = core.executor.job_for(artifact.execution).state
        return await wipe(**kwargs)

    monkeypatch.setattr(db_maintenance, "wipe_database", observed_wipe)
    return core, ApplicationService(core.engine), transfer, artifact, seen


async def test_running_wipe_pauses_drains_backs_up_wipes_and_resumes(tmp_path, monkeypatch):
    core, service, transfer, artifact, seen = await _wipe_setup(tmp_path, monkeypatch)
    assert not await core.repository.globally_paused()  # no manual Pause first

    result = await routes.wipe_database_admin({"confirm": True}, application=service)

    assert result["ok"] is True
    assert result["drain"] == {"live_before": 1, "live_after": 0}
    assert seen.live_at_wipe == ()
    assert seen.native_at_wipe == ExecutionState.CANCELLED
    assert seen.backups_at_wipe == 1  # the mandatory safety backup precedes deletion
    assert result["backup"]["id"] == backup.list_restore_points()[0].id
    assert await core.repository.get(transfer.id) is None
    # The operator was running, so the wipe's own temporary pause is undone.
    assert not await core.repository.globally_paused()


async def test_paused_wipe_releases_the_parked_native_execution_and_stays_paused(tmp_path, monkeypatch):
    core, service, transfer, artifact, seen = await _wipe_setup(tmp_path, monkeypatch)
    await service.pause_all()
    # Pause alone PARKS this executor's native job: it is paused but still alive
    # and still owned -- the state the old wipe accepted as idle.
    assert core.executor.job_for(artifact.execution).state == ExecutionState.PAUSED
    assert [item.handle for item in await core.repository.live_executions()] == [artifact.execution]

    result = await routes.wipe_database_admin({"confirm": True}, application=service)

    assert result["ok"] is True
    assert seen.live_at_wipe == ()
    assert seen.native_at_wipe == ExecutionState.CANCELLED
    assert ("cancel", artifact.execution.attempt_id) in core.executor.calls
    assert await core.repository.globally_paused()  # the operator's intent survives the wipe


async def test_refused_drain_never_wipes_and_restores_prior_running_intent(tmp_path, monkeypatch):
    core, service, transfer, artifact, seen = await _wipe_setup(tmp_path, monkeypatch)
    core.executor.cancel_mode = "unconfirmed"  # the native stop cannot be proven

    with pytest.raises(HTTPException) as refused:
        await routes.wipe_database_admin({"confirm": True}, application=service)

    assert refused.value.status_code == 409
    assert seen.calls == 0
    assert not backup.list_restore_points()  # nothing destructive or safety-side began
    current = await core.repository.get(transfer.id)
    assert current is not None and current.state != TransferState.CANCELLED
    # Native ownership is not abandoned: the unproven writer is still owned.
    assert [item.handle for item in await core.repository.live_executions()] == [artifact.execution]
    # Only the wipe's own temporary pause is unwound.
    assert not await core.repository.globally_paused()


async def test_refused_drain_leaves_an_operator_pause_in_place(tmp_path, monkeypatch):
    core, service, _transfer, _artifact, seen = await _wipe_setup(tmp_path, monkeypatch)
    await service.pause_all()
    core.executor.cancel_mode = "unconfirmed"

    with pytest.raises(HTTPException):
        await routes.wipe_database_admin({"confirm": True}, application=service)

    assert seen.calls == 0
    assert await core.repository.globally_paused()


async def test_the_safety_backup_cannot_be_turned_off(tmp_path, monkeypatch):
    # A settings document that still carries the retired opt-out changes nothing.
    core, service, transfer, _artifact, seen = await _wipe_setup(tmp_path, monkeypatch, backup_before_wipe=False)

    result = await routes.wipe_database_admin({"confirm": True}, application=service)

    assert seen.backups_at_wipe == 1
    assert result["backup"]["id"] == backup.list_restore_points()[0].id
    assert await core.repository.get(transfer.id) is None


async def test_a_failed_safety_backup_aborts_the_wipe(tmp_path, monkeypatch):
    core, service, transfer, _artifact, seen = await _wipe_setup(tmp_path, monkeypatch)

    async def refused():
        raise backup.BackupRejected("create", "Backup could not be created.")

    monkeypatch.setattr(routes.backup_store, "create_restore_point", refused)

    with pytest.raises(HTTPException) as aborted:
        await routes.wipe_database_admin({"confirm": True}, application=service)

    assert aborted.value.status_code == 500
    assert seen.calls == 0
    assert await core.repository.get(transfer.id) is not None
    assert not await core.repository.globally_paused()


async def _running(core, payload):
    transfer = await submit_ledger(core, payload)
    core.executor.run((await artifact_of(core, transfer.id)).execution)
    await core.engine.reconcile_executions()
    return transfer


def _backed_up_pause_intent(point_id):
    """The pause intent a restore point carries: (global flag, {transfer: paused})."""
    point = backup.restore_point(point_id)
    member = json.loads((point.path / ".debridpulse-backup.json").read_text(encoding="utf-8"))["database"]
    with closing(sqlite3.connect(point.path / member)) as conn:
        row = conn.execute("SELECT value FROM transfer_controls WHERE key='paused'").fetchone()
        intents = dict(conn.execute("SELECT torrent_id, paused FROM transfer_pause_intents").fetchall())
    return (row is not None and row[0] == "1"), intents


async def test_a_refused_wipe_keeps_an_individually_paused_transfer_paused(tmp_path, monkeypatch):
    core, service, running, _artifact, seen = await _wipe_setup(tmp_path, monkeypatch)
    held = await _running(core, "held-item")
    await service.pause(held.id)  # the operator paused this one transfer; processing is running
    core.executor.cancel_mode = "unconfirmed"

    with pytest.raises(HTTPException):
        await routes.wipe_database_admin({"confirm": True}, application=service)

    assert seen.calls == 0
    assert not await core.repository.globally_paused()
    assert (await core.repository.get(held.id)).paused
    assert not (await core.repository.get(running.id)).paused


async def test_a_failed_safety_backup_keeps_an_individually_paused_transfer_paused(tmp_path, monkeypatch):
    core, service, running, _artifact, seen = await _wipe_setup(tmp_path, monkeypatch)
    held = await _running(core, "held-item")
    await service.pause(held.id)

    async def refused():
        raise backup.BackupRejected("create", "Backup could not be created.")

    monkeypatch.setattr(routes.backup_store, "create_restore_point", refused)
    with pytest.raises(HTTPException):
        await routes.wipe_database_admin({"confirm": True}, application=service)

    assert seen.calls == 0
    assert not await core.repository.globally_paused()
    assert (await core.repository.get(held.id)).paused
    assert not (await core.repository.get(running.id)).paused


async def test_the_safety_backup_records_the_operators_pause_intent_not_the_wipes(tmp_path, monkeypatch):
    core, service, running, _artifact, _seen = await _wipe_setup(tmp_path, monkeypatch)
    held = await _running(core, "held-item")
    await service.pause(held.id)

    result = await routes.wipe_database_admin({"confirm": True}, application=service)

    globally_paused, intents = _backed_up_pause_intent(result["backup"]["id"])
    assert globally_paused is False
    assert intents.get(held.id) == 1
    assert intents.get(running.id, 0) == 0
    assert not await core.repository.globally_paused()


async def test_a_globally_paused_operator_is_backed_up_as_paused(tmp_path, monkeypatch):
    core, service, _running_transfer, _artifact, _seen = await _wipe_setup(tmp_path, monkeypatch)
    await service.pause_all()

    result = await routes.wipe_database_admin({"confirm": True}, application=service)

    assert _backed_up_pause_intent(result["backup"]["id"])[0] is True
    assert await core.repository.globally_paused()
