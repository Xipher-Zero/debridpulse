"""Downloads operator queue: durable priority, ordering and safe preemption.

The queue is the existing ``torrents.priority`` order (higher first, then the
older transfer): one entry per logical transfer, unfinished work first, then
settled history by completion. An operator move goes through the one priority
endpoint (``PATCH /torrents/{id}/priority``) and may displace lower-ranked
running work once the real admission proves the promoted transfer waits only
for a slot -- whenever that happens, from the move itself onwards: a
writer whose progress is DebridPulse material it can continue from is retired
through the one writer retirement (quiesce, checkpoint, fence) and readmitted
later from that material; a writer that would lose its acquired progress is
interrupted only after the operator confirmed exactly that attempt. A new
submission joins the tail and never preempts. Real engine and repository;
the ``SpoolExecutor`` moves real bytes and exports them, the
``LedgerExecutor`` can only restart.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import api.operational_downloads as downloads
import api.routes as routes
import db.database as database
from application.service import ApplicationService
from continuation_fakes import MIB, SpoolExecutor, SpoolProvider, payload
from executor_fakes import LedgerExecutor, LedgerProvider, ledger_core
from test_operational_downloads_projection import _ExplodingRepository
from transfers.models import ContinuationStrategy, ExecutionState, TransferRequest
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from account_support import standalone_boundary

pytestmark = pytest.mark.asyncio

# Material geometry v1 commits whole 1 MiB chunks before the end of a file.
SOURCES = {name: payload(3 * MIB, name) for name in ("first", "second", "third", "one", "two", "three", "four")}


async def queue_lab(tmp_path, monkeypatch, *, slots=1):
    """One slot; ``spool`` requests run on an executor that exports and
    continues its material, ``ledger`` requests on one that can only restart."""
    def executors(authorize):
        return (LedgerExecutor(authorize, identity="restart-copy", kinds=("ledger",)),
                SpoolExecutor(authorize, SOURCES, identity="spool", scheme="spoola"))
    core = await ledger_core(tmp_path, monkeypatch, executors=executors,
                             providers=(LedgerProvider(), SpoolProvider("spool-src", "spoola", SOURCES)),
                             policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0,
                                                   max_active_executions=slots))
    core.restart, core.spool = core.executors
    core.application = ApplicationService(core.engine)
    return core


async def submit(core, name, kind="spool"):
    transfer = await core.engine.submit((TransferRequest(kind, name, name=name),))
    await core.engine.tick()
    return transfer.id


async def writer(core, transfer_id):
    return (await core.repository.artifacts(transfer_id))[0].execution


async def running(core, transfer_id, *, completed=None):
    """Drive the writer of ``transfer_id`` to acquiring (``completed`` bytes)."""
    handle = await writer(core, transfer_id)
    if handle.executor_id == "spool":
        core.spool.step(handle.attempt_id, completed or 0)
    else:
        core.restart.run(handle, completed=completed)
    await core.engine.tick()
    return handle


async def tick(core, rounds=2):
    for _ in range(rounds):
        await core.engine.tick()


async def move(core, transfer_id, **body):
    return await routes.set_torrent_priority(transfer_id, body, application=core.application)


async def consent_rows(core):
    """The Downloads rows offering the operator a reaffirmation."""
    page = await downloads.list_operational_torrents(status=None, search=None, limit=0, offset=0, order="queue",
                                                     application=core.application)
    return [item["id"] for item in page["items"] if item["queue_restart_consent"]]


async def refused(core, transfer_id, **body):
    with pytest.raises(HTTPException) as caught:
        await move(core, transfer_id, **body)
    return caught.value


async def order(repository):
    return [transfer.id for transfer in await repository.active()]


async def promoted():
    """The transfers holding an operator queue promotion."""
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT id FROM torrents WHERE queue_promotion IS NOT NULL ORDER BY id")
    return [row["id"] for row in rows]


def stale_after_admission(core, transfer_id, change):
    """Run ``change`` right after the real admission made ``transfer_id``'s
    artifact wait for a slot: between the selection of the writers that yield
    to it and their interruption later in the same pass."""
    admit = core.engine._dispatch

    async def dispatch(artifact, **kwargs):
        await admit(artifact, **kwargs)
        if artifact.transfer_id == transfer_id and artifact.id in core.engine.capacity_only_blocked_ids():
            await change(artifact)
    core.engine._dispatch = dispatch
    return lambda: setattr(core.engine, "_dispatch", admit)


def calls(executor, name):
    return [attempt for call, attempt in executor.calls if call == name]


async def titled(transfer_id, title):
    """The artifact resolved to ``title`` (a Media Downloads ``/watch``
    transfer keeps its submitted name); the transfer's own name is untouched."""
    async with database.get_db() as db:
        await db.execute("UPDATE download_files SET filename=? WHERE torrent_id=?", (title, transfer_id))
        await db.commit()


# -- Q1: server-side order before pagination ---------------------------------------------------------

def _seed(db_path: Path, rows) -> None:
    conn = sqlite3.connect(db_path)
    try:
        for tid, status, priority, created_at, completed_at in rows:
            conn.execute(
                "INSERT INTO torrents(id,hash,name,status,source,progress,priority,created_at,completed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (tid, f"hash-{tid}", f"Transfer {tid}", status, "magnet", 0.0, priority, created_at, completed_at))
        conn.commit()
    finally:
        conn.close()


async def _page(**kwargs):
    application = SimpleNamespace(repository=_ExplodingRepository(), definitions=[])
    result = await downloads.list_operational_torrents(
        status=kwargs.pop("status", None), search=kwargs.pop("search", None),
        limit=kwargs.pop("limit", 0), offset=kwargs.pop("offset", 0), application=application, **kwargs)
    return [item["id"] for item in result["items"]], result["total"], result["items"]


async def test_queue_order_is_established_on_the_whole_collection_before_the_page(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    await database.init_db()
    _seed(db_path, [
        (1, "downloading", 0, "2026-05-01 10:00:00", None),
        (2, "queued", 5, "2026-05-02 10:00:00", None),
        (3, "paused", 0, "2026-05-03 10:00:00", None),
        (4, "completed", 9, "2026-05-04 10:00:00", "2026-06-01 10:00:00"),
        (5, "pending", 2, "2026-05-05 10:00:00", None),
        (6, "completed", 0, "2026-05-06 10:00:00", "2026-06-03 10:00:00"),
        (7, "cancelled", 0, "2026-04-01 10:00:00", None),
        (8, "failed", 0, "2026-05-08 10:00:00", None),
        (9, "deleted", 0, "2026-05-09 10:00:00", None),
        (10, "consolidated", 0, "2026-05-10 10:00:00", None),
    ])
    # Unfinished work by durable priority (then the older transfer), whatever
    # its status; then settled history by completion, newest first.
    pages = [await _page(order="queue", limit=3, offset=offset) for offset in (0, 3, 6)]
    assert [ids for ids, _total, _items in pages] == [[2, 5, 1], [3, 8, 6], [4, 7]]
    assert {total for _ids, total, _items in pages} == {8}
    members = {item["id"]: item["queue_member"] for _ids, _total, items in pages for item in items}
    assert members == {2: True, 5: True, 1: True, 3: True, 8: True, 6: False, 4: False, 7: False}
    # Newest stays the chronological view, unchanged.
    assert (await _page(limit=0))[0] == [8, 6, 5, 4, 3, 2, 1, 7]
    # A filtered or searched view keeps the same deterministic order.
    assert (await _page(order="queue", status="completed"))[0] == [6, 4]
    assert (await _page(order="queue", search="transfer 1"))[0] == [1]


# -- Q2: durable reorder, real admission, tail arrival never preempts --------------------------------

async def test_a_move_changes_who_gets_the_slot_and_survives_a_restart(tmp_path, monkeypatch):
    core = await queue_lab(tmp_path, monkeypatch)
    first = await submit(core, "first")
    a = await running(core, first, completed=10)
    second = await submit(core, "second")
    # An ordinary submission joins the tail and preempts nothing.
    assert await order(core.repository) == [first, second]
    assert await writer(core, second) is None and calls(core.spool, "cancel") == []

    result = await move(core, second, before_id=first)
    assert result["ok"] is True and result["outcome"] == "preempting"
    await tick(core)

    assert await order(core.repository) == [second, first]
    b = await writer(core, second)
    assert b is not None and b.attempt_id != a.attempt_id
    # The displaced writer was quiesced, checkpointed and fenced -- never
    # operator-paused -- and its artifact waits for its turn without a writer.
    assert calls(core.spool, "pause") == [a.attempt_id] and calls(core.spool, "cancel") == [a.attempt_id]
    assert await writer(core, first) is None
    assert (await core.repository.get(first)).paused is False
    assert await promoted() == [second]
    # The order is durable: a fresh repository on the same database reads it.
    assert await order(TransferRepository()) == [second, first]


async def test_a_new_submission_during_full_capacity_never_preempts(tmp_path, monkeypatch):
    core = await queue_lab(tmp_path, monkeypatch)
    first = await submit(core, "first")
    a = await running(core, first, completed=10)
    second = await submit(core, "second")
    await move(core, second, after_id=first)                    # a no-op move: nothing happens
    third = await submit(core, "third")
    await tick(core)
    assert await order(core.repository) == [first, second, third]
    assert core.spool.jobs[a.attempt_id].state == ExecutionState.RUNNING
    assert calls(core.spool, "pause") == [] and calls(core.spool, "cancel") == []
    assert await writer(core, third) is None and await promoted() == []


# -- Q3: resumable preemption readmits from DP material; manual and global Pause win ------------------

async def test_a_displaced_writer_is_readmitted_from_its_material_and_manual_pause_wins(tmp_path, monkeypatch):
    core = await queue_lab(tmp_path, monkeypatch)
    first = await submit(core, "first")
    a = await running(core, first, completed=2 * MIB + 100)
    second = await submit(core, "second")
    await move(core, second, before_id=first)
    await tick(core)
    b = await writer(core, second)
    assert b is not None and await writer(core, first) is None
    # The forced checkpoint of the retirement committed what was written.
    assert (await core.repository.material_state((await core.repository.artifacts(first))[0].id)).valid == (
        (0, 2 * MIB),)

    # The operator pauses the displaced transfer while it waits: the queue
    # never resumes it, even once the slot is free again.
    await core.application.pause(first)
    core.spool.step(b.attempt_id, 3 * MIB)
    await tick(core, 3)
    assert (await core.repository.get(first)).paused is True and await writer(core, first) is None

    await core.application.resume(first)
    await tick(core)
    readmitted = await writer(core, first)
    assert readmitted is not None and readmitted.attempt_id != a.attempt_id
    # Exactly one new writer, continuing from the checkpointed material --
    # never a native resume of the retired job.
    plan = core.spool.plans[-1]
    assert plan.strategy == ContinuationStrategy.CONTIGUOUS_FROM_OFFSET and plan.boundary == 2 * MIB
    assert calls(core.spool, "start").count(readmitted.attempt_id) == 1


async def test_no_preemption_while_processing_is_globally_paused(tmp_path, monkeypatch):
    core = await queue_lab(tmp_path, monkeypatch)
    first = await submit(core, "first")
    a = await running(core, first, completed=10)
    second = await submit(core, "second")
    await core.repository.global_pause(True)
    result = await move(core, second, before_id=first)
    assert result["outcome"] != "preempting" and await order(core.repository) == [second, first]
    assert calls(core.spool, "pause") == [] and await core.repository.globally_paused() is True
    assert core.spool.jobs[a.attempt_id].state == ExecutionState.RUNNING


# -- Q4: destructive preemption needs the operator's confirmation of that attempt ---------------------

async def test_a_restart_only_victim_is_listed_and_cancel_changes_nothing(tmp_path, monkeypatch):
    core = await queue_lab(tmp_path, monkeypatch)
    first = await submit(core, "first", kind="ledger")
    a = await running(core, first, completed=2)
    second = await submit(core, "second", kind="ledger")
    await titled(first, "The Victim's Real Title.webm")
    priorities = [(await core.repository.get(tid)).priority for tid in (first, second)]

    refusal = await refused(core, second, before_id=first)
    assert refusal.status_code == 409
    detail = refusal.detail
    assert detail["confirmation"] == "restart_required" and detail["changed"] is False
    # Each affected logical transfer once, with every attempt to confirm.
    assert [(item["transfer_id"], item["attempt_ids"]) for item in detail["transfers"]] == [(first, [a.attempt_id])]
    # Named as the Downloads list names it -- the victim's own title, never
    # the moved transfer's; the submitted name stays as it was.
    assert [(item.get("display_name"), item["name"]) for item in detail["transfers"]] == [
        ("The Victim's Real Title.webm", "first")]
    assert detail["transfers"][0]["completed_bytes"] == 2
    # The proposal changed nothing: no order, intent or native call.
    assert [(await core.repository.get(tid)).priority for tid in (first, second)] == priorities
    assert await order(core.repository) == [first, second] and await promoted() == []
    await core.engine.tick()
    assert calls(core.restart, "cancel") == [] and core.restart.job_for(a).state == ExecutionState.RUNNING


async def test_a_confirmed_restart_retires_that_attempt_once_and_readmits_the_same_transfer(tmp_path, monkeypatch):
    core = await queue_lab(tmp_path, monkeypatch)
    first = await submit(core, "first", kind="ledger")
    a = await running(core, first, completed=2)
    second = await submit(core, "second", kind="ledger")

    result = await move(core, second, before_id=first, confirm_restart=[a.attempt_id])
    assert result["outcome"] == "preempting"
    await core.engine.tick()
    await core.engine.tick()
    assert calls(core.restart, "cancel") == [a.attempt_id]
    b = await writer(core, second)
    assert b is not None
    # Ephemeral bytes of the retired attempt are never DP material.
    assert float((await core.repository.get(first)).progress or 0) == 0.0
    core.restart.finish_file(b, str(tmp_path / "payloads" / "second"))
    await core.engine.tick()
    await core.engine.tick()
    readmitted = await writer(core, first)
    assert readmitted is not None and readmitted.attempt_id != a.attempt_id
    assert calls(core.restart, "cancel") == [a.attempt_id]


async def test_a_confirmation_for_another_attempt_is_refused_before_any_mutation(tmp_path, monkeypatch):
    core = await queue_lab(tmp_path, monkeypatch)
    first = await submit(core, "first", kind="ledger")
    a = await running(core, first, completed=2)
    second = await submit(core, "second", kind="ledger")
    refusal = await refused(core, second, before_id=first, confirm_restart=["not-the-current-attempt"])
    assert refusal.status_code == 409 and refusal.detail["changed"] is True
    assert [item["attempt_ids"] for item in refusal.detail["transfers"]] == [[a.attempt_id]]
    assert await order(core.repository) == [first, second] and await promoted() == []


# -- Q5: races -- unknown stop, completion, stale anchors, restart ----------------------------------

async def test_an_unproven_stop_never_frees_the_slot_and_the_promoted_starts_exactly_once(tmp_path, monkeypatch):
    core = await queue_lab(tmp_path, monkeypatch)
    first = await submit(core, "first", kind="ledger")
    a = await running(core, first, completed=1)
    second = await submit(core, "second", kind="ledger")
    await move(core, second, before_id=first, confirm_restart=[a.attempt_id])
    core.restart.cancel_mode = "unconfirmed"
    await core.engine.tick()
    await core.engine.tick()
    assert await writer(core, second) is None
    assert await core.repository.occupied_execution_slots(core.now[0]) == 1
    assert await promoted() == [second]                   # still owed, never assumed done
    core.restart.cancel_mode = "confirm"
    await core.engine.tick()
    await core.engine.tick()
    b = await writer(core, second)
    assert b is not None and calls(core.restart, "start").count(b.attempt_id) == 1


async def test_a_victim_that_completes_first_is_simply_not_interrupted(tmp_path, monkeypatch):
    core = await queue_lab(tmp_path, monkeypatch)
    first = await submit(core, "first", kind="ledger")
    a = await running(core, first, completed=3)
    second = await submit(core, "second", kind="ledger")
    await move(core, second, before_id=first, confirm_restart=[a.attempt_id])
    core.restart.finish_file(a, str(tmp_path / "payloads" / "first"))
    await core.engine.tick()
    await core.engine.tick()
    assert calls(core.restart, "cancel") == []
    assert (await writer(core, second)) is not None


async def test_a_pause_landing_during_the_handoff_wins_on_either_side(tmp_path, monkeypatch):
    core = await queue_lab(tmp_path, monkeypatch)
    # The promoted transfer is paused before the displaced writer converges:
    # nothing is interrupted any more, and the owed move is dropped.
    first = await submit(core, "first", kind="ledger")
    a = await running(core, first, completed=1)
    second = await submit(core, "second", kind="ledger")
    await move(core, second, before_id=first, confirm_restart=[a.attempt_id])
    await core.application.pause(second)
    await tick(core)
    assert calls(core.restart, "cancel") == []
    assert core.restart.job_for(a).state == ExecutionState.RUNNING
    assert (await core.repository.get(second)).paused is True

    # The displaced transfer is paused during the handoff: its own Pause
    # retires it and nothing ever resumes it on the queue's behalf.
    third = await submit(core, "third")
    await core.application.resume(second)
    await core.application.pause(first)
    await tick(core)
    b = await writer(core, second)
    assert b is not None
    core.restart.finish_file(b, str(tmp_path / "payloads" / "second"))
    await tick(core, 3)
    assert (await core.repository.get(first)).paused is True and await writer(core, first) is None
    assert await writer(core, third) is not None


async def test_a_stale_anchor_is_refused_without_moving_anything(tmp_path, monkeypatch):
    core = await queue_lab(tmp_path, monkeypatch)
    ids = [await submit(core, name) for name in ("one", "two", "three", "four")]
    await move(core, ids[3], before_id=ids[1])                  # one, four, two, three
    assert await order(core.repository) == [ids[0], ids[3], ids[1], ids[2]]
    # A second operator still sees one|two adjacent: refused, nothing moves.
    refusal = await refused(core, ids[2], after_id=ids[0], before_id=ids[1])
    assert refusal.status_code == 409 and refusal.detail["reason"] == "queue_changed"
    assert await order(core.repository) == [ids[0], ids[3], ids[1], ids[2]]
    # A repeated identical move is a no-op.
    await move(core, ids[3], before_id=ids[1])
    assert await order(core.repository) == [ids[0], ids[3], ids[1], ids[2]]


async def test_a_promotion_before_any_capacity_observation_still_gets_the_slot(tmp_path, monkeypatch):
    """Moved up before the admission ever saw it wait: the promotion is the
    operator's, and the first pass that proves it waits for a slot acts on it."""
    core = await queue_lab(tmp_path, monkeypatch)
    first = await submit(core, "first")
    a = await running(core, first, completed=10)
    second = (await core.engine.submit((TransferRequest("spool", "second", name="second"),))).id
    assert not await core.repository.artifacts(second) and not core.engine.capacity_only_blocked_ids()
    result = await move(core, second, before_id=first)
    assert result["outcome"] == "preempting"
    await tick(core, 3)
    assert calls(core.spool, "cancel") == [a.attempt_id]
    b = await writer(core, second)
    assert b is not None and calls(core.spool, "start").count(b.attempt_id) == 1

    # The same immediate move over a restart-only writer still needs that
    # attempt confirmed, before anything changes.
    core = await queue_lab(tmp_path / "restart", monkeypatch)
    first = await submit(core, "first", kind="ledger")
    a = await running(core, first, completed=2)
    second = (await core.engine.submit((TransferRequest("ledger", "second", name="second"),))).id
    refusal = await refused(core, second, before_id=first)
    assert refusal.status_code == 409 and [item["attempt_ids"] for item in refusal.detail["transfers"]] == [
        [a.attempt_id]]
    assert await order(core.repository) == [first, second] and await promoted() == []


async def test_a_promoted_transfer_no_longer_eligible_at_the_interruption_displaces_nothing(tmp_path, monkeypatch):
    """Proven waiting when the writers to yield were selected, then no longer
    dispatchable when the first of them would be interrupted: it keeps
    running, and the promotion converges once the transfer is eligible."""
    core = await queue_lab(tmp_path, monkeypatch)
    first = await submit(core, "first")
    a = await running(core, first, completed=10)
    second = await submit(core, "second")
    await move(core, second, before_id=first)

    async def back_off(artifact):
        async with database.get_db() as db:
            await db.execute("UPDATE download_files SET retry_at=? WHERE id=?", (core.now[0] + 3600, artifact.id))
            await db.commit()
    restore = stale_after_admission(core, second, back_off)
    await tick(core, 3)
    restore()
    assert calls(core.spool, "pause") == [] and calls(core.spool, "cancel") == []
    assert core.spool.jobs[a.attempt_id].state == ExecutionState.RUNNING and await writer(core, second) is None
    assert await promoted() == [second]

    async with database.get_db() as db:
        await db.execute("UPDATE download_files SET retry_at=0 WHERE torrent_id=?", (second,))
        await db.commit()
    await tick(core)
    assert calls(core.spool, "cancel") == [a.attempt_id] and await writer(core, second) is not None


async def test_a_promotion_deferred_while_ineligible_converges_later_with_consent_asked_when_needed(
        tmp_path, monkeypatch):
    """Promoted while paused: nothing is displaced while it cannot run; once
    resumed, the first pass that proves it waits for a slot displaces the
    resumable writer -- but never a restart the operator did not confirm."""
    core = await queue_lab(tmp_path, monkeypatch)
    first = await submit(core, "first")
    a = await running(core, first, completed=10)
    second = await submit(core, "second")
    await core.application.pause(second)
    result = await move(core, second, before_id=first)
    assert result["outcome"] == "queued"
    await tick(core, 3)
    assert calls(core.spool, "cancel") == [] and core.spool.jobs[a.attempt_id].state == ExecutionState.RUNNING
    await core.application.resume(second)
    await tick(core, 3)
    assert calls(core.spool, "cancel") == [a.attempt_id] and await writer(core, second) is not None
    assert (await core.repository.get(first)).paused is False and await promoted() == [second]

    core = await queue_lab(tmp_path / "restart", monkeypatch)
    first = await submit(core, "first", kind="ledger")
    a = await running(core, first, completed=2)
    second = await submit(core, "second", kind="ledger")
    await core.application.pause(second)
    assert (await move(core, second, before_id=first))["outcome"] == "queued"
    await core.application.resume(second)
    await tick(core, 3)
    assert calls(core.restart, "cancel") == [] and core.restart.job_for(a).state == ExecutionState.RUNNING
    assert await writer(core, second) is None
    # Admission says so, and the row offers the reaffirmation: Cancel keeps
    # everything as it is, Confirm authorizes exactly the attempt it named.
    assert core.engine.restart_consent_ids() == {second} and await consent_rows(core) == [second]
    refusal = await refused(core, second, reaffirm=True)
    assert refusal.detail["confirmation"] == "restart_required" and refusal.detail["changed"] is False
    assert [item["attempt_ids"] for item in refusal.detail["transfers"]] == [[a.attempt_id]]
    await tick(core)
    assert calls(core.restart, "cancel") == [] and await order(core.repository) == [second, first]
    assert (await move(core, second, reaffirm=True, confirm_restart=[a.attempt_id]))["outcome"] == "preempting"
    await tick(core)
    assert calls(core.restart, "cancel") == [a.attempt_id] and await writer(core, second) is not None
    assert (await core.repository.get(first)).paused is False and await consent_rows(core) == []


async def test_dragging_the_running_transfer_down_overtakes_like_dragging_the_waiting_one_up(tmp_path, monkeypatch):
    """One slot: A downloading, B queued. Moving A below B is the same
    operator decision as moving B above A."""
    core = await queue_lab(tmp_path, monkeypatch)
    first = await submit(core, "first")
    a = await running(core, first, completed=10)
    second = await submit(core, "second")
    result = await move(core, first, after_id=second)
    assert result["outcome"] == "preempting" and await promoted() == [second]
    await tick(core)
    assert await order(core.repository) == [second, first]
    assert calls(core.spool, "cancel") == [a.attempt_id]
    b = await writer(core, second)
    assert b is not None and calls(core.spool, "start").count(b.attempt_id) == 1
    assert (await core.repository.get(first)).paused is False


async def test_dragging_a_restart_only_transfer_down_needs_its_attempt_confirmed(tmp_path, monkeypatch):
    core = await queue_lab(tmp_path, monkeypatch)
    first = await submit(core, "first", kind="ledger")
    a = await running(core, first, completed=2)
    second = await submit(core, "second", kind="ledger")
    priorities = [(await core.repository.get(tid)).priority for tid in (first, second)]
    refusal = await refused(core, first, after_id=second)
    assert refusal.status_code == 409 and refusal.detail["confirmation"] == "restart_required"
    assert [(item["transfer_id"], item["attempt_ids"]) for item in refusal.detail["transfers"]] == [
        (first, [a.attempt_id])]
    # Cancel: nothing moved, nothing promoted, nothing interrupted.
    assert [(await core.repository.get(tid)).priority for tid in (first, second)] == priorities
    assert await promoted() == []
    await tick(core)
    assert calls(core.restart, "cancel") == [] and core.restart.job_for(a).state == ExecutionState.RUNNING
    # Confirm: exactly that attempt is interrupted, and the overtaker starts.
    result = await move(core, first, after_id=second, confirm_restart=[a.attempt_id])
    assert result["outcome"] == "preempting"
    await tick(core)
    assert calls(core.restart, "cancel") == [a.attempt_id] and await writer(core, second) is not None
    assert await order(core.repository) == [second, first]


async def test_a_demotion_displaces_the_lowest_ranked_resumable_writer_not_the_one_dragged(tmp_path, monkeypatch):
    """Two slots: restart-only A and resumable C download, B waits; A is
    dragged below B (B > A > C). The slot B needs comes from the lowest-ranked
    writer, C -- resumable, so no confirmation -- never from restarting A
    merely because the drag crossed it."""
    core = await queue_lab(tmp_path, monkeypatch, slots=2)
    first = await submit(core, "first", kind="ledger")
    a = await running(core, first, completed=2)
    second = await submit(core, "second")
    await core.application.pause(second)
    third = await submit(core, "third")
    c = await running(core, third, completed=10)
    await core.application.resume(second)
    await tick(core)
    assert await writer(core, second) is None and await order(core.repository) == [first, second, third]

    stopped = calls(core.spool, "cancel")                       # the setup's own Pause of B
    result = await move(core, first, after_id=second)
    assert result["outcome"] == "preempting"
    assert [(item["transfer_id"], item["restart"]) for item in result["preempting"]] == [(third, False)]
    await tick(core)
    assert await order(core.repository) == [second, first, third]
    assert calls(core.spool, "cancel") == [*stopped, c.attempt_id] and calls(core.restart, "cancel") == []
    assert core.restart.job_for(a).state == ExecutionState.RUNNING and await writer(core, second) is not None


async def test_a_promotion_displaces_a_lower_writer_the_drag_never_crossed(tmp_path, monkeypatch):
    """One slot held by C; B is promoted over a paused A only, while B itself
    is paused. Once B can run, the slot it needs comes from C, ranked below
    it, although the drag never crossed C."""
    core = await queue_lab(tmp_path, monkeypatch)
    first = await submit(core, "first")
    await running(core, first, completed=10)
    await core.application.pause(first)
    second = (await core.engine.submit((TransferRequest("spool", "second", name="second"),))).id
    await core.application.pause(second)
    third = await submit(core, "third")
    c = await running(core, third, completed=10)
    assert await order(core.repository) == [first, second, third]

    assert (await move(core, second, before_id=first))["outcome"] == "queued"
    await tick(core)
    assert c.attempt_id not in calls(core.spool, "cancel")
    await core.application.resume(second)
    await tick(core, 3)
    assert c.attempt_id in calls(core.spool, "cancel") and await writer(core, second) is not None
    assert (await core.repository.get(first)).paused is True and await writer(core, first) is None
    assert (await core.repository.get(third)).paused is False


async def test_a_confirmation_never_covers_the_attempt_that_replaced_it(tmp_path, monkeypatch):
    """Confirmed for one attempt, which was replaced before admission acted:
    the new attempt is never interrupted on that confirmation, and the
    operator can confirm the new consequence."""
    core = await queue_lab(tmp_path, monkeypatch)
    first = await submit(core, "first", kind="ledger")
    a1 = await running(core, first, completed=2)
    second = await submit(core, "second", kind="ledger")
    await move(core, second, before_id=first, confirm_restart=[a1.attempt_id])
    await core.application.pause(second)                         # before any pass acts on it
    await core.application.pause(first)
    await core.application.resume(first)
    await tick(core)
    a2 = await running(core, first, completed=2)
    assert a2.attempt_id != a1.attempt_id
    stopped = calls(core.restart, "cancel")                     # the operator's own Pause of A
    await core.application.resume(second)
    await tick(core, 3)
    assert calls(core.restart, "cancel") == stopped and core.restart.job_for(a2).state == ExecutionState.RUNNING
    assert await writer(core, second) is None and await consent_rows(core) == [second]
    # The earlier confirmation names another attempt: refused as changed.
    refusal = await refused(core, second, reaffirm=True, confirm_restart=[a1.attempt_id])
    assert refusal.detail["changed"] is True
    assert [item["attempt_ids"] for item in refusal.detail["transfers"]] == [[a2.attempt_id]]
    await move(core, second, reaffirm=True, confirm_restart=[a2.attempt_id])
    await tick(core)
    assert calls(core.restart, "cancel") == [*stopped, a2.attempt_id] and await writer(core, second) is not None


async def test_an_authorized_preemption_converges_after_a_restart(tmp_path, monkeypatch):
    core = await queue_lab(tmp_path, monkeypatch)
    first = await submit(core, "first", kind="ledger")
    a = await running(core, first, completed=1)
    second = await submit(core, "second", kind="ledger")
    await move(core, second, before_id=first, confirm_restart=[a.attempt_id])
    assert await promoted() == [second]
    # A new engine over the same database and the same native executor.
    from transfers.convergence_engine import TransferEngine
    engine = TransferEngine(TransferRepository(), core.registry, download_root=str(core.root),
                            policy=core.engine.policy, clock=lambda: core.now[0])
    await engine.initialize()
    await engine.tick()
    await engine.tick()
    assert calls(core.restart, "cancel") == [a.attempt_id]
    assert (await writer(core, second)) is not None


# -- Q6: identity and compatibility ---------------------------------------------------------------

async def test_a_label_edit_keeps_the_queue_position_and_legacy_priority_still_applies(tmp_path, monkeypatch):
    core = await queue_lab(tmp_path, monkeypatch)
    first = await submit(core, "first")
    second = await submit(core, "second")
    await move(core, second, priority=7)
    before = (await core.repository.get(second)).priority
    await routes.set_torrent_label(second, routes.LabelUpdate(label="keep"), application=core.application)
    async with database.get_db() as db:
        row = await db.fetchone("SELECT label, priority FROM torrents WHERE id=?", (second,))
    assert (row["label"], row["priority"]) == ("keep", before)
    assert await order(core.repository) == [second, first]
    # The explicit absolute priority of older clients is still honoured, and
    # it never preempts.
    legacy = await move(core, first, priority=before + 5)
    assert legacy["priority"] == before + 5 and await order(core.repository) == [first, second]
    assert await promoted() == []
    # An absolute priority withdraws an earlier promotion: it never preempts.
    await move(core, second, before_id=first)
    assert await promoted() == [second]
    await move(core, second, priority=1)
    assert await promoted() == []


async def test_history_and_absorbed_transfers_are_not_queue_entries(tmp_path, monkeypatch):
    core = await queue_lab(tmp_path, monkeypatch)
    first = await submit(core, "first")
    second = await submit(core, "second")
    async with database.get_db() as db:
        await db.execute("UPDATE torrents SET status='consolidated' WHERE id=?", (second,))
        await db.commit()
    assert (await refused(core, second, before_id=first)).status_code == 409
    assert (await refused(core, first, after_id=second)).status_code == 409


async def test_a_slot_freed_mid_pass_goes_to_the_higher_waiting_transfer(tmp_path, monkeypatch):
    """A writer that finishes during a pass frees its slot after a higher
    transfer already waited this pass: the lower one behind it does not take it."""
    core = await queue_lab(tmp_path, monkeypatch)
    low_runner = await submit(core, "runner", kind="ledger")
    a = await running(core, low_runner)
    waiting = await submit(core, "waiting", kind="ledger")
    tail = await submit(core, "tail", kind="ledger")
    await move(core, waiting, priority=50)                     # legacy: no preemption
    assert await order(core.repository) == [waiting, low_runner, tail]
    core.restart.finish_file(a, str(tmp_path / "payloads" / "runner"))
    await core.engine.tick()
    assert await writer(core, tail) is None
    await core.engine.tick()
    assert await writer(core, waiting) is not None and await writer(core, tail) is None


# -- Move to Top: one command for one transfer or a selection, one atomic commit ----------------------

async def top(core, *ids, confirm_restart=None):
    """``POST /torrents/move-to-top`` through the real route, its validation
    and the application's operator-control boundary: ``(status, body)``."""
    import httpx
    from fastapi import FastAPI

    app = FastAPI()

    standalone_boundary(app)
    app.state.application = core.application
    app.include_router(routes.router, prefix="/api")
    body = {"ids": list(ids)}
    if confirm_restart is not None:
        body["confirm_restart"] = list(confirm_restart)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post("/api/torrents/move-to-top", json=body)
    return response.status_code, response.json()


async def priorities(core, ids):
    return [(await core.repository.get(tid)).priority for tid in ids]


async def stored_status(transfer_id):
    async with database.get_db() as db:
        return (await db.fetchone("SELECT status FROM torrents WHERE id=?", (transfer_id,)))["status"]


async def journaled_moves():
    async with database.get_db() as db:
        row = await db.fetchone("SELECT COUNT(*) AS n FROM event_journal WHERE event_type='transfer.queue_moved'")
    return int(row["n"])


def committed(core):
    """Every queue commit the repository makes, by the transfers it renumbers to."""
    commits, commit = [], core.repository.commit_queue_move

    async def recording(*args, **kwargs):
        commits.append(tuple(kwargs["proposed"]))
        return await commit(*args, **kwargs)
    core.repository.commit_queue_move = recording
    return commits


async def test_move_to_top_puts_the_selection_first_in_its_queue_order_in_one_commit(tmp_path, monkeypatch):
    """Q = [A, B, C, D, E, F], selection {F, B, D, X} with X settled: one
    commit of [B, D, F, A, C, E]; X is ignored and never resurrected."""
    core = await queue_lab(tmp_path, monkeypatch)
    a, b, c, d, e, f, x = [await submit(core, name) for name in ("one", "two", "three", "four", "first", "second",
                                                                  "third")]
    async with database.get_db() as db:
        await db.execute("UPDATE torrents SET status='completed' WHERE id=?", (x,))
        await db.commit()
    assert await order(core.repository) == [a, b, c, d, e, f]
    settled = (await priorities(core, [x]), await stored_status(x))
    commits = committed(core)

    status, result = await top(core, f, b, d, x)
    assert status == 200, result
    assert await order(core.repository) == [b, d, f, a, c, e]
    assert commits == [(b, d, f, a, c, e)]
    assert (result["moved"], result["ignored"]) == (3, 1)
    # Each selected transfer that overtook work holds the operator promotion;
    # the settled one is untouched.
    assert await promoted() == sorted([b, d, f])
    assert (await priorities(core, [x]), await stored_status(x)) == settled
    assert await journaled_moves() == 3
    # A submission afterwards joins the tail and preempts nothing.
    g = (await core.engine.submit((TransferRequest("spool", "first", name="late"),))).id
    assert (await order(core.repository))[-1] == g and await promoted() == sorted([b, d, f])


async def test_move_to_top_of_the_top_block_is_a_true_no_op_and_promotes_only_real_overtakes(tmp_path, monkeypatch):
    core = await queue_lab(tmp_path, monkeypatch)
    a, b, c = [await submit(core, name) for name in ("one", "two", "three")]
    before = await priorities(core, [a, b, c])
    commits = committed(core)
    status, result = await top(core, b, a)
    assert status == 200 and result["moved"] == 0 and result["outcome"] == "unchanged"
    assert commits == [] and await priorities(core, [a, b, c]) == before
    assert await promoted() == [] and await journaled_moves() == 0
    # A is already ahead of every unselected member: only C overtakes.
    status, result = await top(core, c, a)
    assert status == 200 and await order(core.repository) == [a, c, b]
    assert await promoted() == [c]


async def test_a_selection_larger_than_capacity_displaces_only_what_the_slots_need(tmp_path, monkeypatch):
    """One slot held by resumable A; B and C promoted together: A yields once,
    B takes the slot, C waits in its new place."""
    core = await queue_lab(tmp_path, monkeypatch)
    first = await submit(core, "first")
    a = await running(core, first, completed=2 * MIB + 100)
    second, third = await submit(core, "second"), await submit(core, "third")
    status, result = await top(core, third, second)
    assert status == 200 and result["outcome"] == "preempting"
    assert [(item["transfer_id"], item["restart"]) for item in result["preempting"]] == [(first, False)]
    await tick(core, 3)
    assert await order(core.repository) == [second, third, first]
    assert calls(core.spool, "cancel") == [a.attempt_id]
    assert await writer(core, second) is not None and await writer(core, third) is None
    # The yielded writer kept its material and was never operator-paused.
    assert (await core.repository.get(first)).paused is False
    assert (await core.repository.material_state((await core.repository.artifacts(first))[0].id)).valid == (
        (0, 2 * MIB),)


async def test_a_paused_selection_keeps_its_pause_and_evicts_nothing_on_its_own(tmp_path, monkeypatch):
    core = await queue_lab(tmp_path, monkeypatch)
    first = await submit(core, "first")
    a = await running(core, first, completed=10)
    second = await submit(core, "second")
    await core.application.pause(second)
    third = await submit(core, "third")
    stopped = calls(core.spool, "cancel")
    status, result = await top(core, second)
    assert status == 200 and result["outcome"] == "queued"
    await tick(core, 3)
    assert calls(core.spool, "cancel") == stopped and core.spool.jobs[a.attempt_id].state == ExecutionState.RUNNING
    # B already leads; with C the selection evicts A for C alone.
    status, result = await top(core, second, third)
    assert status == 200 and await order(core.repository) == [second, third, first]
    await tick(core, 3)
    assert calls(core.spool, "cancel") == [*stopped, a.attempt_id]
    assert (await core.repository.get(second)).paused is True and await writer(core, second) is None
    assert await writer(core, third) is not None


async def test_one_confirmation_covers_every_restart_a_selection_needs_and_cancel_changes_nothing(
        tmp_path, monkeypatch):
    """Two slots held by restart-only A and B; C and D promoted together need
    both: one refusal lists each once; a partial or stale confirmation moves
    nothing; the confirmation of both commits once and retires exactly those."""
    core = await queue_lab(tmp_path, monkeypatch, slots=2)
    first = await submit(core, "first", kind="ledger")
    a = await running(core, first, completed=2)
    second = await submit(core, "second", kind="ledger")
    b = await running(core, second, completed=3)
    third, fourth = await submit(core, "third", kind="ledger"), await submit(core, "four", kind="ledger")
    await titled(first, "First Title.webm")
    await titled(second, "Second Title.webm")
    before = await priorities(core, [first, second, third, fourth])

    status, refusal = await top(core, fourth, third)
    assert status == 409 and refusal["detail"]["confirmation"] == "restart_required"
    assert refusal["detail"]["changed"] is False
    assert sorted((item["transfer_id"], tuple(item["attempt_ids"]), item.get("display_name"))
                  for item in refusal["detail"]["transfers"]) == [
        (first, (a.attempt_id,), "First Title.webm"), (second, (b.attempt_id,), "Second Title.webm")]
    for confirmation in ([a.attempt_id], ["not-the-current-attempt", b.attempt_id]):
        status, stale = await top(core, fourth, third, confirm_restart=confirmation)
        assert status == 409 and stale["detail"]["changed"] is True
    assert await priorities(core, [first, second, third, fourth]) == before and await promoted() == []
    await tick(core)
    assert calls(core.restart, "cancel") == []

    status, result = await top(core, fourth, third, confirm_restart=[a.attempt_id, b.attempt_id])
    assert status == 200 and result["outcome"] == "preempting"
    assert await order(core.repository) == [third, fourth, first, second]
    await tick(core, 3)
    assert sorted(calls(core.restart, "cancel")) == sorted([a.attempt_id, b.attempt_id])
    assert await writer(core, third) is not None and await writer(core, fourth) is not None


async def test_move_to_top_refuses_a_selection_with_no_queue_member_and_bounds_its_input(tmp_path, monkeypatch):
    core = await queue_lab(tmp_path, monkeypatch)
    first, second = await submit(core, "first"), await submit(core, "second")
    async with database.get_db() as db:
        await db.execute("UPDATE torrents SET status='completed' WHERE id=?", (second,))
        await db.commit()
    status, refusal = await top(core, second, 9999)
    assert status == 409 and refusal["detail"]["reason"] == "not_queued"
    assert await stored_status(second) == "completed" and await promoted() == []
    assert (await top(core))[0] == 422
    assert (await top(core, *range(1, 102)))[0] == 422
    # Duplicates are one selection, never a reason to refuse or repeat.
    status, result = await top(core, first, first)
    assert status == 200 and result["outcome"] == "unchanged"


# -- A promoted transfer's automatic retry keeps one slot ---------------------------------------------

def retryable(core, handle, *, after=60):
    """The writer fails the way a refused download does: retryable, later."""
    from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage
    core.restart.fail(handle, NormalizedError(Domain.NETWORK, Category.REMOTE_READ_FAILED, Stage.EXECUTION,
                                              retryability=Retryability.BACKOFF, integration_id="restart-copy",
                                              retry_after_seconds=after))


async def artifact_state(core, transfer_id):
    return (await core.repository.artifacts(transfer_id))[0].state


async def promoted_in_retry(core, *, slots):
    """``slots`` slots: B, C and A downloading, A promoted to the top, D
    queued behind them; then A fails retryably and waits for its retry."""
    core = await queue_lab(*core, slots=slots)
    names = iter(("first", "second"))
    others = []
    for _ in range(slots - 1):
        tid = await submit(core, next(names), kind="ledger")
        await running(core, tid, completed=5)
        others.append(tid)
    if not others:
        # One slot: A overtakes a transfer the operator paused.
        held = await submit(core, "first", kind="ledger")
        await core.application.pause(held)
        others.append(held)
    a = await submit(core, "third", kind="ledger")
    handle = await running(core, a, completed=5)
    assert (await move(core, a, before_id=others[0]))["outcome"] == "queued"
    assert await promoted() == [a]
    d = await submit(core, "four", kind="ledger")
    await tick(core)
    assert await writer(core, d) is None
    retryable(core, handle)
    await tick(core, 2)
    assert await artifact_state(core, a) == "recovery_wait"
    return core, a, d, others, handle


async def test_a_promoted_transfer_in_retry_backoff_keeps_its_slot_and_retries_before_lower_work(
        tmp_path, monkeypatch):
    """Three slots: B and C download, promoted A waits for its retry, D is
    runnable. The slot A freed stays A's -- nothing is interrupted to keep it
    -- and A's due retry takes it before D; a second retryable failure keeps
    it again, and only A's completion hands it to D."""
    core, a, d, (b, c), first = await promoted_in_retry((tmp_path, monkeypatch), slots=3)
    assert await writer(core, d) is None
    await tick(core, 3)
    assert await writer(core, d) is None and calls(core.restart, "cancel") == []

    core.now[0] += 61
    await tick(core, 2)
    retry = await writer(core, a)
    assert retry is not None and retry.attempt_id != first.attempt_id and await writer(core, d) is None

    core.restart.run(retry, completed=50)                     # it progresses: a fresh retry budget
    await tick(core)
    retryable(core, retry)
    await tick(core, 2)
    assert await artifact_state(core, a) == "recovery_wait" and await writer(core, d) is None
    core.now[0] += 300
    await tick(core, 2)
    last = await writer(core, a)
    assert last is not None and await writer(core, d) is None
    core.restart.finish_file(last, str(tmp_path / "payloads" / "third"))
    await tick(core, 3)
    assert await writer(core, d) is not None
    # Kept by admission alone: no writer was ever retired for it.
    assert calls(core.restart, "cancel") == []


async def test_capacity_one_promoted_retry_keeps_the_only_slot_and_an_ordinary_one_keeps_none(
        tmp_path, monkeypatch):
    core, a, d, _others, _first = await promoted_in_retry((tmp_path / "promoted", monkeypatch), slots=1)
    await tick(core, 2)
    assert await writer(core, d) is None
    core.now[0] += 61
    await tick(core, 2)
    assert await writer(core, a) is not None and await writer(core, d) is None

    # An ordinary submission's retry reserves nothing: the slot goes on.
    core = await queue_lab(tmp_path / "ordinary", monkeypatch, slots=1)
    first = await submit(core, "first", kind="ledger")
    handle = await running(core, first, completed=5)
    second = await submit(core, "second", kind="ledger")
    retryable(core, handle)
    await tick(core, 2)
    assert await promoted() == [] and await writer(core, second) is not None


async def test_a_full_pool_is_never_interrupted_and_the_next_freed_slot_is_kept(tmp_path, monkeypatch):
    """Two slots: A failed and waits for its retry while B and C hold both
    slots; then the operator promotes A. Nothing is interrupted for it; the
    slot C frees on completion is kept for A, not taken by D."""
    core = await queue_lab(tmp_path, monkeypatch, slots=2)
    b = await submit(core, "first", kind="ledger")
    await running(core, b, completed=5)
    a = await submit(core, "third", kind="ledger")
    handle = await running(core, a, completed=5)
    c = await submit(core, "second", kind="ledger")
    retryable(core, handle)
    await tick(core, 2)
    c_writer = await writer(core, c)
    assert c_writer is not None                       # not yet promoted: an ordinary retry keeps nothing
    core.restart.run(c_writer, completed=5)
    d = await submit(core, "four", kind="ledger")
    result = await move(core, a, before_id=b)
    assert result["outcome"] != "preempting" and result["preempting"] == []
    await tick(core, 2)
    assert calls(core.restart, "cancel") == [] and await writer(core, d) is None
    core.restart.finish_file(c_writer, str(tmp_path / "payloads" / "second"))
    await tick(core, 3)
    assert await writer(core, d) is None and calls(core.restart, "cancel") == []
    core.now[0] += 61
    await tick(core, 2)
    assert await writer(core, a) is not None and await writer(core, d) is None


async def test_the_kept_slot_is_released_by_pause_demotion_or_a_final_failure(tmp_path, monkeypatch):
    from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage

    # Manual Pause: the slot goes to D, and A is never resumed by its retry.
    core, a, d, _others, _first = await promoted_in_retry((tmp_path / "pause", monkeypatch), slots=2)
    await core.application.pause(a)
    await tick(core, 2)
    assert await writer(core, d) is not None
    core.now[0] += 61
    await tick(core, 2)
    assert (await core.repository.get(a)).paused is True and await writer(core, a) is None

    # The operator moves A below D: D now outranks it and takes the slot.
    core, a, d, _others, _first = await promoted_in_retry((tmp_path / "demoted", monkeypatch), slots=2)
    await move(core, a, after_id=d)
    await tick(core, 2)
    assert await writer(core, d) is not None

    # A failure that ends A's retries returns the slot at once.
    core, a, d, _others, _first = await promoted_in_retry((tmp_path / "final", monkeypatch), slots=2)
    core.now[0] += 61
    await tick(core, 2)
    retry = await writer(core, a)
    core.restart.fail(retry, NormalizedError(Domain.NETWORK, Category.REMOTE_READ_FAILED, Stage.EXECUTION,
                                             retryability=Retryability.NEVER, integration_id="restart-copy"))
    await tick(core, 3)
    assert await artifact_state(core, a) != "recovery_wait" and await writer(core, d) is not None


async def test_a_new_engine_keeps_the_promoted_retry_slot_from_durable_state(tmp_path, monkeypatch):
    core, a, d, _others, _first = await promoted_in_retry((tmp_path, monkeypatch), slots=2)
    # The one occupancy count: the kept slot exists only for admission of
    # work ranked below A -- never for A itself, never in the reported count.
    mine, theirs = [(await core.repository.artifacts(tid))[0].id for tid in (a, d)]
    reported = await core.repository.occupied_execution_slots(core.now[0])
    assert await core.repository.occupied_execution_slots(core.now[0], exclude_artifact_id=theirs) == reported + 1
    assert await core.repository.occupied_execution_slots(core.now[0], exclude_artifact_id=mine) == reported
    # The candidate refresh a recovery decided is the same retry, still kept.
    async with database.get_db() as db:
        await db.execute("UPDATE download_files SET status='refresh_pending' WHERE id=?", (mine,))
        await db.commit()
    assert await core.repository.occupied_execution_slots(core.now[0], exclude_artifact_id=theirs) == reported + 1
    async with database.get_db() as db:
        await db.execute("UPDATE download_files SET status='recovery_wait' WHERE id=?", (mine,))
        await db.commit()
    from transfers.convergence_engine import TransferEngine
    engine = TransferEngine(TransferRepository(), core.registry, download_root=str(core.root),
                            policy=core.engine.policy, clock=lambda: core.now[0])
    await engine.initialize()
    await engine.tick()
    await engine.tick()
    assert await writer(core, d) is None
    core.now[0] += 61
    await engine.tick()
    await engine.tick()
    assert await writer(core, a) is not None and await writer(core, d) is None


# -- maintenance composition ----------------------------------------------------------------------

async def test_a_database_wipe_removes_every_queue_promotion(tmp_path, monkeypatch):
    from services import db_maintenance

    core = await queue_lab(tmp_path, monkeypatch)
    first = await submit(core, "first", kind="ledger")
    a = await running(core, first, completed=1)
    second = await submit(core, "second", kind="ledger")
    core.restart.cancel_mode = "unconfirmed"
    await move(core, second, before_id=first, confirm_restart=[a.attempt_id])
    await core.engine.tick()
    assert await promoted() == [second]
    await db_maintenance.wipe_database(verified_quiesced=True)
    async with database.get_db() as db:
        assert (await db.fetchone("SELECT COUNT(*) AS n FROM execution_attempts"))["n"] == 0
        assert (await db.fetchone("SELECT COUNT(*) AS n FROM torrents"))["n"] == 0
    assert await promoted() == []
