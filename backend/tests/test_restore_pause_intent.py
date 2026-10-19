"""1.0.13 WS R (FUNC-A03): a restore never changes the pause intent it did not own.

A restore drains every execution by pausing everything. A refused restore
then resumed every transfer, a rolled-back one did the same, and the safety
backup was taken after the drain -- so a transfer the operator paused, or an
external client's advisory hold, was lost three ways. The drain's pause is the
restore's own: the operator's and every external client's intent is recorded
before it, written back before the safety backup is taken, and reinstated
exactly -- an external hold with its own holder and generation, so that
client's fenced release still matches it.

Real restore route, real drain, staging, swap and rollback (test_backup_restore_points).
"""
from __future__ import annotations

import sqlite3

import pytest

import db.database as database
from services import backup
from test_backup_restore_points import _managed_database, _submit, installation, served  # noqa: F401
from transfers.models import ExecutionObservation, ExecutionState

pytestmark = pytest.mark.asyncio

HOLDER = "external:radarr"


async def _three(app, client):
    """One transfer running, one paused by the operator, one held by an external client."""
    running = await _submit(app, client, "running")
    paused = await _submit(app, client, "paused")
    held = await _submit(app, client, "held")
    assert (await client.post(f"/api/torrents/{paused}/pause")).status_code == 200
    await app.state.application.engine.pause(held, holder=HOLDER)
    hold = await app.state.application.repository.pause_hold(held)
    return running, paused, held, (hold["holder"], int(hold["generation"]))


async def _intent(service, ids):
    states = {}
    for transfer_id in ids:
        hold = await service.repository.pause_hold(transfer_id)
        transfer = await service.repository.get(transfer_id)
        states[transfer_id] = (transfer.paused, hold["holder"] if hold else None,
                               int(hold["generation"]) if hold and hold["holder"] else None)
    return states


def _assert_kept(states, running, paused, held, grant):
    assert states[running][0] is False
    assert states[paused][:2] == (True, None)
    assert states[held] == (True, *grant)


async def _released_by_its_holder(service, held, grant) -> bool:
    return await service.engine.resume(held, held_by=grant) is not None and not (
        await service.repository.get(held)).paused


async def test_a_refused_restore_reinstates_every_pause_exactly_and_the_holder_can_still_release(served, monkeypatch):  # noqa: F811
    app, client, _composed, _calls = served
    running, paused, held, grant = await _three(app, client)
    point = (await client.post("/api/admin/backup")).json()["restore_point"]["id"]

    def refuse(_staged):
        raise backup.BackupRejected("restore", "staged database failed validation")

    monkeypatch.setattr(backup.StagedRestore, "validate", refuse)
    assert (await client.post("/api/admin/backups/restore", json={"id": point})).status_code == 400

    service = app.state.application
    _assert_kept(await _intent(service, (running, paused, held)), running, paused, held, grant)
    assert not await service.repository.globally_paused()
    assert await _released_by_its_holder(service, held, grant)


async def test_a_drain_refusal_reinstates_every_pause_exactly(served):  # noqa: F811
    app, client, _composed, _calls = served
    running, paused, held, grant = await _three(app, client)
    point = (await client.post("/api/admin/backup")).json()["restore_point"]["id"]
    service = app.state.application

    async def unconfirmed(handle):
        return ExecutionObservation(handle, ExecutionState.UNKNOWN)

    service.test_executor.cancel = unconfirmed
    assert (await client.post("/api/admin/backups/restore", json={"id": point})).status_code == 409

    _assert_kept(await _intent(service, (running, paused, held)), running, paused, held, grant)
    assert await _released_by_its_holder(service, held, grant)


async def test_a_global_pause_survives_a_refused_restore(served, monkeypatch):  # noqa: F811
    app, client, _composed, _calls = served
    await _submit(app, client, "one")
    point = (await client.post("/api/admin/backup")).json()["restore_point"]["id"]
    await app.state.application.pause_all()

    def refuse(_staged):
        raise backup.BackupRejected("restore", "staged database failed validation")

    monkeypatch.setattr(backup.StagedRestore, "validate", refuse)
    assert (await client.post("/api/admin/backups/restore", json={"id": point})).status_code == 400
    assert await app.state.application.repository.globally_paused()


async def test_a_rolled_back_restore_reinstates_every_pause_exactly(served, monkeypatch):  # noqa: F811
    app, client, _composed, _calls = served
    running, paused, held, grant = await _three(app, client)
    point = (await client.post("/api/admin/backup")).json()["restore_point"]["id"]

    import application.lifetime as lifetime
    original, attempts = lifetime.start_application, []

    async def failing_once(service):
        attempts.append(service)
        if len(attempts) == 1:
            raise RuntimeError("restored startup failed")
        return await original(service)

    monkeypatch.setattr(lifetime, "start_application", failing_once)
    assert (await client.post("/api/admin/backups/restore", json={"id": point})).status_code == 400

    service = app.state.application
    assert service is attempts[-1]
    _assert_kept(await _intent(service, (running, paused, held)), running, paused, held, grant)
    assert await _released_by_its_holder(service, held, grant)


async def test_the_safety_backup_holds_the_intent_the_restore_replaced_not_the_drains(served):  # noqa: F811
    app, client, _composed, _calls = served
    running, paused, held, grant = await _three(app, client)
    point = (await client.post("/api/admin/backup")).json()["restore_point"]["id"]

    response = await client.post("/api/admin/backups/restore", json={"id": point})
    assert response.status_code == 200, response.text
    safety = backup.restore_point(response.json()["safety_backup"]["id"])
    with sqlite3.connect(f"file:{_managed_database(safety)}?mode=ro", uri=True) as conn:
        rows = {row[0]: (bool(row[1]), row[2], row[3]) for row in conn.execute(
            "SELECT torrent_id, paused, holder, generation FROM transfer_pause_intents")}
        globally = conn.execute("SELECT value FROM transfer_controls WHERE key='paused'").fetchone()

    assert globally is None or globally[0] != "1"
    assert not rows.get(running, (False,))[0]
    assert rows[paused][:2] == (True, None)
    assert rows[held] == (True, *grant)
    assert database.DB_PATH.exists()


async def test_work_that_lands_while_the_restore_waits_for_admission_is_never_reverted(served, monkeypatch):  # noqa: F811
    """The intent is recorded once admitted work has drained: an operator's
    Resume or a new transfer that completes while the restore waits for
    admission is the intent a refusal reinstates, not one it reverts."""
    import asyncio

    from transfers.models import TransferRequest

    app, client, _composed, _calls = served
    running, paused, held, grant = await _three(app, client)
    point = (await client.post("/api/admin/backup")).json()["restore_point"]["id"]
    service = app.state.application

    def refuse(_staged):
        raise backup.BackupRejected("restore", "staged database failed validation")

    monkeypatch.setattr(backup.StagedRestore, "validate", refuse)
    release, admitted, late = asyncio.Event(), asyncio.Event(), []

    async def operator_work():
        async with service.application_operation():
            admitted.set()
            await release.wait()
            await service.engine.resume(paused)
            created = await service.engine.submit(
                (TransferRequest("parcel", "https://fake.example/late", name="late"),), deduplicate=False)
            late.append(created.id)

    work = asyncio.create_task(operator_work())
    await admitted.wait()
    restore = asyncio.create_task(client.post("/api/admin/backups/restore", json={"id": point}))
    await asyncio.sleep(0.05)
    assert not restore.done(), "the restore must wait for admitted work to drain"
    release.set()
    await work
    assert (await restore).status_code == 400

    states = await _intent(service, (running, paused, held, late[0]))
    assert states[paused][0] is False, "the operator's resume was reverted"
    assert states[late[0]][0] is False, "a transfer added meanwhile was left paused by the drain"
    assert states[held] == (True, *grant)
