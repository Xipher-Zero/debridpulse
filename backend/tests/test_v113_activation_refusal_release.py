"""A candidate activation that stopped the file's writer and then did not
commit (Gate 9 rejection of 79a8d2b3; pre-existing on fa14e1bc).

``activate_candidate`` retires the old writer, then commits the selection in
``transition_recovery``. When that commit is refused -- a stale recovery claim
(a Pause fenced it meanwhile), a concurrent writer, a moved target -- the
stopped attempt used to stay the file's AUTHORIZED execution: reconciliation
read its cancellation as the transfer's, and the whole transfer ended
``cancelled``. The stopped writer is now released through its one owner
(``detach_retired_writer``): the file keeps its own selection and is queued
again (or paused under a pause in force); a newer writer or a settled
file/transfer is never overwritten.

Neutral fixtures: part01 downloads from ddownload via prov-rd; 1fichier (prov-ad)
is an equivalent source of it.
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from test_v113_multi_root_provider_switch import settle
from test_v113_pending_provider_choice import (
    choices, landed_lab, lab, part, rows, running, selected_provider,
)
from test_v113_transient_bound_candidate import DDL1, FICHIER01

pytestmark = pytest.mark.asyncio


def refuse_commit(monkeypatch, lab_, before=None):
    """The first selection commit of an activation is refused AFTER its writer
    was retired: ``before`` (if any) runs first -- a concurrent canonical action
    -- and the claim the commit checks is stale, exactly as a fence makes it."""
    real = lab_.repository.transition_recovery
    fired = []

    async def transition_recovery(artifact_id, state, **kwargs):
        if kwargs.get("candidate_switched") and kwargs.get("claim") is not None and not fired:
            fired.append(kwargs.get("fulfils_choice"))
            if before is not None:
                await before()
            kwargs["claim"] = replace(kwargs["claim"], generation=kwargs["claim"].generation + 1)
        return await real(artifact_id, state, **kwargs)
    monkeypatch.setattr(lab_.repository, "transition_recovery", transition_recovery)
    return fired


async def authorized_stopped(artifact_id) -> list:
    """Attempts that are stopped yet still authorized: never left behind."""
    return [dict(row) for row in await rows(
        "SELECT id,state FROM execution_attempts WHERE artifact_id=? AND authorized=1 "
        "AND state IN ('cancelled','failed','absent')", (artifact_id,))]


async def transfer_status(lab_) -> str:
    return (await rows("SELECT status FROM torrents WHERE id=?", (lab_.transfer.id,)))[0]["status"]


def other_source(artifact):
    return next(index for index, item in enumerate(artifact.candidates)
                if item.provider_id != selected_provider(artifact))


async def test_a_source_switch_refused_after_stopping_the_writer_keeps_the_transfer_going(tmp_path, monkeypatch):
    """The operator's source switch stops prov-rd's writer, then its commit is
    refused (stale claim). Nothing is selected; the stopped writer loses its
    authority; the file is queued on its own source and downloads again with
    one writer -- the transfer is never cancelled."""
    lab_ = await lab(tmp_path, monkeypatch, links=(DDL1, FICHIER01))
    first = lab_.part.execution.attempt_id
    fired = refuse_commit(monkeypatch, lab_)
    result = await lab_.engine.activate_candidate_command(lab_.transfer.id, lab_.part.id, other_source(lab_.part))
    assert fired and not result.committed and result.reason == "commit_conflict"
    now = await part(lab_)
    assert selected_provider(now) == "prov-rd" and now.state == "queued" and now.execution is None
    assert await authorized_stopped(now.id) == []
    await settle(lab_.engine, 6)
    now = await part(lab_)
    assert await transfer_status(lab_) == "downloading" and now.state == "downloading"
    assert selected_provider(now) == "prov-rd" and running(lab_) == [now.execution.attempt_id] != [first]
    assert await authorized_stopped(now.id) == []


async def test_a_refusal_before_the_writer_is_stopped_leaves_it_running(tmp_path, monkeypatch):
    """Refused before retirement (the target's provider is unavailable): the
    running writer is untouched -- same attempt, still running."""
    lab_ = await lab(tmp_path, monkeypatch, links=(DDL1, FICHIER01))
    first = lab_.part.execution.attempt_id
    lab_.registry.mark_health("prov-ad", healthy=False)
    result = await lab_.engine.activate_candidate_command(lab_.transfer.id, lab_.part.id, other_source(lab_.part))
    assert not result.committed and result.reason == "candidate_provider_unavailable"
    now = await part(lab_)
    assert now.execution.attempt_id == first and running(lab_) == [first] and now.state == "downloading"


async def test_a_pause_that_fences_the_switch_keeps_the_file_paused_not_cancelled(tmp_path, monkeypatch):
    """A Pause lands while the switch is between stopping the writer and
    committing: its fence makes the switch's claim stale. The Pause holds --
    the file is paused with no writer, the transfer is paused, nothing is
    cancelled -- and Resume restarts the file on its own source."""
    lab_ = await lab(tmp_path, monkeypatch, links=(DDL1, FICHIER01))

    async def pause():
        await lab_.repository.set_pause_and_fence(lab_.transfer.id, True)
    refuse_commit(monkeypatch, lab_, before=pause)
    result = await lab_.engine.activate_candidate_command(lab_.transfer.id, lab_.part.id, other_source(lab_.part))
    assert not result.committed
    now = await part(lab_)
    assert now.state == "paused" and now.execution is None and selected_provider(now) == "prov-rd"
    assert await authorized_stopped(now.id) == [] and await transfer_status(lab_) != "cancelled"
    await settle(lab_.engine, 3)
    assert not running(lab_)                                    # the Pause holds
    await lab_.engine.resume(lab_.transfer.id)
    await settle(lab_.engine, 6)
    now = await part(lab_)
    assert now.state == "downloading" and selected_provider(now) == "prov-rd"
    assert running(lab_) == [now.execution.attempt_id]


async def test_a_cancellation_committed_meanwhile_is_never_undone(tmp_path, monkeypatch):
    """The operator cancelled the transfer while the switch was between
    stopping the writer and committing: the cancellation stands -- the file
    and transfer stay cancelled -- and the stopped attempt only loses its
    authority."""
    lab_ = await lab(tmp_path, monkeypatch, links=(DDL1, FICHIER01))
    from db.database import get_db

    async def cancelled():
        async with get_db() as db:
            await db.execute("UPDATE torrents SET status='cancelled' WHERE id=?", (lab_.transfer.id,))
            await db.execute("UPDATE download_files SET status='cancelled' WHERE torrent_id=?", (lab_.transfer.id,))
            await db.commit()
    refuse_commit(monkeypatch, lab_, before=cancelled)
    result = await lab_.engine.activate_candidate_command(lab_.transfer.id, lab_.part.id, other_source(lab_.part))
    assert not result.committed
    assert await transfer_status(lab_) == "cancelled"
    assert (await rows("SELECT status FROM download_files WHERE id=?", (lab_.part.id,)))[0]["status"] == "cancelled"
    assert await authorized_stopped(lab_.part.id) == []


async def test_the_operators_withdrawal_of_the_file_meanwhile_is_never_undone(tmp_path, monkeypatch):
    """The operator cancelled just this file (a withdrawal, ``withdrawn_attempt``)
    while the switch was between stopping its writer and committing: the file
    stays withdrawn; only the stopped attempt's authority is revoked."""
    lab_ = await lab(tmp_path, monkeypatch, links=(DDL1, FICHIER01))

    async def withdrawn():
        await lab_.repository.artifact_state(lab_.part.id, "cancelled", withdrawn=True)
    refuse_commit(monkeypatch, lab_, before=withdrawn)
    result = await lab_.engine.activate_candidate_command(lab_.transfer.id, lab_.part.id, other_source(lab_.part))
    assert not result.committed
    row = (await rows("SELECT status,withdrawn_attempt FROM download_files WHERE id=?", (lab_.part.id,)))[0]
    assert row["status"] == "cancelled" and row["withdrawn_attempt"] is not None
    assert await authorized_stopped(lab_.part.id) == []
    await settle(lab_.engine, 4)
    assert (await rows("SELECT status FROM download_files WHERE id=?", (lab_.part.id,)))[0]["status"] == "cancelled"
    assert not running(lab_)


async def test_a_newer_writer_admitted_meanwhile_is_never_overwritten(tmp_path, monkeypatch):
    """Another canonical action installed a newer execution of the file while
    the switch was between stopping its writer and committing. The refused
    switch releases only its own stopped attempt: the newer execution, the
    file's state and selection are left exactly as they are."""
    lab_ = await lab(tmp_path, monkeypatch, links=(DDL1, FICHIER01))
    from db.database import get_db
    newer = "newer-attempt"

    async def newer_writer():
        async with get_db() as db:
            await db.execute("""INSERT INTO execution_attempts(id,transfer_id,artifact_id,executor_id,handle,state,
                authorized,created_at,updated_at) VALUES(?,?,?,'memory','{}','running',1,CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP)""", (newer, lab_.transfer.id, lab_.part.id))
            await db.execute("UPDATE download_files SET execution_attempt_id=?,status='downloading' WHERE id=?",
                             (newer, lab_.part.id))
            await db.commit()
    refuse_commit(monkeypatch, lab_, before=newer_writer)
    result = await lab_.engine.activate_candidate_command(lab_.transfer.id, lab_.part.id, other_source(lab_.part))
    assert not result.committed
    row = (await rows("SELECT status,execution_attempt_id FROM download_files WHERE id=?", (lab_.part.id,)))[0]
    assert (row["status"], row["execution_attempt_id"]) == ("downloading", newer)
    assert (await rows("SELECT authorized FROM execution_attempts WHERE id=?", (newer,)))[0]["authorized"] == 1
    assert await authorized_stopped(lab_.part.id) == []


async def test_a_pending_choice_landing_refused_after_stopping_the_writer_keeps_the_transfer_going(
        tmp_path, monkeypatch):
    """The same refusal on a pending provider choice's landing: the choice is
    not committed, the transfer goes on, and a later cycle still lands
    prov-y's copy with exactly one writer."""
    lab_ = await landed_lab(tmp_path, monkeypatch)
    fired = refuse_commit(monkeypatch, lab_)
    await settle(lab_.engine, 3)
    assert fired and fired[0] is not None                       # the choice's own landing was refused
    assert await transfer_status(lab_) != "cancelled" and await authorized_stopped(lab_.part.id) == []
    await settle(lab_.engine, 6)
    (choice,) = await choices(lab_)
    now = await part(lab_)
    assert choice["state"] == "committed" and selected_provider(now) == "prov-y"
    assert now.state == "downloading" and running(lab_) == [now.execution.attempt_id]
    assert await transfer_status(lab_) == "downloading"


async def mark_revived(lab_):
    """The file was withdrawn once, on an EARLIER attempt, and revived since:
    a stale ``withdrawn_attempt`` its current attempt voids
    (``cohorts.artifact_withdrawn``)."""
    from db.database import get_db
    async with get_db() as db:
        await db.execute("UPDATE download_files SET withdrawn_attempt='an-earlier-withdrawn-attempt' WHERE id=?",
                         (lab_.part.id,))
        await db.commit()


@pytest.mark.parametrize("paused", [False, True], ids=["refused", "pause_fenced"])
async def test_a_revived_file_is_released_like_any_other(tmp_path, monkeypatch, paused):
    """A file revived after an earlier withdrawal is not withdrawn now: the
    refused switch releases its stopped writer -- queued again (or paused
    under the Pause that fenced it) -- and the transfer is never cancelled."""
    lab_ = await lab(tmp_path, monkeypatch, links=(DDL1, FICHIER01))
    await mark_revived(lab_)

    async def pause():
        await lab_.repository.set_pause_and_fence(lab_.transfer.id, True)
    refuse_commit(monkeypatch, lab_, before=pause if paused else None)
    result = await lab_.engine.activate_candidate_command(lab_.transfer.id, lab_.part.id, other_source(lab_.part))
    assert not result.committed
    now = await part(lab_)
    assert now.state == ("paused" if paused else "queued") and now.execution is None
    assert await authorized_stopped(now.id) == []
    if paused:
        await lab_.engine.resume(lab_.transfer.id)
    await settle(lab_.engine, 6)
    now = await part(lab_)
    assert now.state == "downloading" and selected_provider(now) == "prov-rd"
    assert running(lab_) == [now.execution.attempt_id] and await transfer_status(lab_) == "downloading"


async def test_a_failure_raised_after_stopping_the_writer_still_releases_it(tmp_path, monkeypatch):
    """Something raises between the activation's stop of the writer and its
    commit (here: building the switch's provenance). The failure propagates,
    but the stopped writer is released first -- the transfer goes on."""
    lab_ = await lab(tmp_path, monkeypatch, links=(DDL1, FICHIER01))
    real = lab_.repository.build_candidate_activation_detail
    calls = []

    def failing(**kwargs):
        if not calls and kwargs.get("outcome") == "activated":
            calls.append(True)
            raise RuntimeError("provenance unavailable")
        return real(**kwargs)
    monkeypatch.setattr(lab_.repository, "build_candidate_activation_detail", failing)
    with pytest.raises(RuntimeError):
        await lab_.engine.activate_candidate_command(lab_.transfer.id, lab_.part.id, other_source(lab_.part))
    assert calls
    now = await part(lab_)
    assert selected_provider(now) == "prov-rd" and now.execution is None and now.state == "queued"
    assert await authorized_stopped(now.id) == []
    await settle(lab_.engine, 6)
    now = await part(lab_)
    assert now.state == "downloading" and running(lab_) == [now.execution.attempt_id]
    assert await transfer_status(lab_) == "downloading"
