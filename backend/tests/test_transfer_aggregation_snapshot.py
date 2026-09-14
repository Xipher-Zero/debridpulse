"""Section 33 regression coverage: parent snapshot / concurrency (DP 1.0.12
recovery leveling, Phase 4, Sections 21-22).

``transfers._repository_base.TransferRepository.aggregate_lifecycle`` replaced
four independently timed ``get_db()`` reads (transfer, requests, artifacts,
executions) followed by a separate, unfenced ``state()`` write with ONE atomic
``BEGIN IMMEDIATE`` transaction. These tests prove that change actually closes
the architectural race, not merely that the full suite stays green.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

import db.database as database
from db.database import get_db
from test_candidate_activation_phase2 import attach_three, build_engine3
from transfers.models import TransferState
from transfers.policy import transition_allowed


_BACKEND = Path(__file__).parents[1]

# Base document Section 44's code-review checklist names ``UPDATE torrents SET
# status`` as a pattern to audit for "direct parent-state writes outside
# approved owners." These are the only files/purposes ever permitted to
# contain that literal SQL fragment -- every one of them is a core,
# repository-layer transition, never an api/, application/, executor/, or
# provider/-level shortcut. Adding a new writer anywhere else is exactly the
# second-lifecycle-authority regression Section 24 forbids.
_APPROVED_PARENT_STATUS_WRITERS = frozenset({
    "transfers/_repository_base.py",   # _write_lifecycle_transition, cancel(), delete()
    "transfers/canonical.py",          # consolidation's terminal transition
    "transfers/input_required.py",     # the input-required challenge transition
    "db/migrations/v112.py",           # one-time startup data repair on pre-existing rows,
                                        # not a live ordinary-lifecycle-transition authority
})


async def _legacy_style_race_write(repository, transfer_id: int, hold: asyncio.Event, resume: asyncio.Event) -> None:
    """Standalone reproduction of the pre-Phase-4 ``_engine_base.TransferEngine
    ._aggregate`` body: artifacts read from their own independent ``get_db()``
    connection, then -- after yielding back to the event loop, exactly like
    any real ``await`` boundary between two separate connections -- a decision
    computed from that now-possibly-stale snapshot is written via the ordinary
    ``state()`` call. This is a comparison oracle only; production code no
    longer contains this shape."""
    artifacts = await repository.artifacts(transfer_id)
    hold.set()
    await resume.wait()
    if any(item.state in {"downloading", "verifying"} for item in artifacts):
        await repository.state(transfer_id, TransferState.TRANSFERRING, progress=1.0)


@pytest.mark.asyncio
async def test_parent_aggregate_uses_coherent_child_snapshot(tmp_path, monkeypatch):
    """Section 21: the whole decision comes from ONE connection/transaction."""
    engine, repository, providers, _executor, _now_box = await build_engine3(tmp_path, monkeypatch)
    canonical, _artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]
    assert live.execution is not None and live.state == "downloading"

    before = database.db_runtime_metrics()["sqlite_acquires"]
    outcome = await repository.aggregate_lifecycle(canonical.id, input_required=False)
    after = database.db_runtime_metrics()["sqlite_acquires"]

    assert after - before == 1, (
        "the aggregation decision must be read from exactly one SQLite "
        f"connection/transaction, not several independently timed ones "
        f"(observed {after - before} acquisitions for one aggregate_lifecycle call)"
    )
    assert outcome is not None and not outcome.should_complete
    assert {item.id for item in outcome.artifacts} == {live.id}
    assert outcome.artifacts[0].state == "downloading"
    transfer = await repository.get(canonical.id)
    assert transfer.state == TransferState.TRANSFERRING


@pytest.mark.asyncio
async def test_stale_aggregate_cannot_overwrite_newer_generation_red_legacy_oracle(tmp_path, monkeypatch):
    """RED: the pre-Phase-4 shape (separate reads, separate write) really does
    let a concurrent mutation's committed truth be silently overwritten by a
    decision computed before that mutation happened -- exactly the "downloading
    <-> queued" churn symptom (base document Section 23)."""
    engine, repository, providers, _executor, _now_box = await build_engine3(tmp_path, monkeypatch)
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]
    assert live.state == "downloading"

    hold = asyncio.Event()
    resume = asyncio.Event()

    async def run_legacy_oracle():
        await _legacy_style_race_write(repository, canonical.id, hold, resume)

    async def run_concurrent_mutation():
        await hold.wait()
        # A real, independent commit landing in the window between the
        # oracle's artifact read and its later write -- e.g. a candidate
        # activation retiring the old writer and re-queuing the artifact for
        # its replacement. artifact_state() has already fully committed by
        # the time this returns (it is its own separate get_db() call, not
        # blocked by anything -- the oracle holds no transaction open between
        # its own separate calls).
        await repository.artifact_state(artifact.id, "queued", release=True)
        resume.set()

    await asyncio.gather(run_legacy_oracle(), run_concurrent_mutation())

    final_transfer = await repository.get(canonical.id)
    final_artifact = (await repository.artifacts(canonical.id))[0]
    # RED: the legacy oracle's stale "downloading" read wins the write race
    # even though the artifact was already durably "queued" by the time that
    # write happened -- the transfer is left showing an incorrect status that
    # contradicts its own child's already-committed truth.
    assert final_transfer.state == TransferState.TRANSFERRING
    assert final_artifact.state == "queued"
    assert final_transfer.state != TransferState.QUEUED, (
        "demonstrates the architectural race: this incorrect combination is "
        "exactly what the legacy 4-read/1-write shape allows"
    )


@pytest.mark.asyncio
async def test_stale_aggregate_cannot_overwrite_newer_generation(tmp_path, monkeypatch):
    """GREEN: the SAME race, run against the real production
    ``aggregate_lifecycle``, cannot corrupt or lose either side -- the
    concurrent mutation is genuinely serialized behind aggregation's held
    write lock (proven by it not completing during the hold window), and
    whichever transaction commits first leaves a fully self-consistent
    result, never a hybrid of both."""
    engine, repository, providers, _executor, _now_box = await build_engine3(tmp_path, monkeypatch)
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]
    assert live.state == "downloading"

    hold = asyncio.Event()
    resume = asyncio.Event()

    from transfers import _repository_base

    original_globally_paused = _repository_base.TransferRepository._globally_paused

    async def patched_globally_paused(db):
        result = await original_globally_paused(db)
        hold.set()
        await resume.wait()
        return result

    monkeypatch.setattr(_repository_base.TransferRepository, "_globally_paused", staticmethod(patched_globally_paused))

    finished_early = {"value": None}

    async def run_aggregate():
        return await repository.aggregate_lifecycle(canonical.id, input_required=False)

    async def run_concurrent_mutation():
        await hold.wait()
        task = asyncio.ensure_future(repository.artifact_state(artifact.id, "queued", release=True))
        await asyncio.sleep(0.05)
        # If the concurrent mutation had genuinely raced (interleaved) with
        # aggregation's still-open transaction, its own independent
        # BEGIN IMMEDIATE would have been free to proceed immediately since
        # nothing held the lock in the legacy shape. Here it must still be
        # blocked, waiting on aggregation's write lock.
        finished_early["value"] = task.done()
        resume.set()
        await task

    agg_result, _ = await asyncio.gather(run_aggregate(), run_concurrent_mutation())

    assert finished_early["value"] is False, (
        "the concurrent mutation must be genuinely blocked by aggregation's "
        "held BEGIN IMMEDIATE lock, not free to interleave and commit while "
        "aggregation's transaction is still open"
    )
    # Aggregation's own decision, made atomically while it held the lock,
    # correctly reflects the artifact as it truly was at that atomic instant
    # -- still "downloading", since the mutation had not committed yet.
    assert agg_result is not None
    assert agg_result.artifacts[0].state == "downloading"
    final_transfer = await repository.get(canonical.id)
    final_artifact = (await repository.artifacts(canonical.id))[0]
    # Aggregation's write committed first (atomically) and the mutation then
    # applied cleanly afterward -- both facts durably true, neither corrupted
    # or silently lost, unlike the RED oracle above.
    assert final_transfer.state == TransferState.TRANSFERRING
    assert final_artifact.state == "queued"
    # A subsequent aggregation pass (the next real scheduler tick) converges
    # on the now-current truth -- nothing was permanently stuck.
    converged = await repository.aggregate_lifecycle(canonical.id, input_required=False)
    assert converged is not None
    refreshed_transfer = await repository.get(canonical.id)
    assert refreshed_transfer.state == TransferState.QUEUED


@pytest.mark.asyncio
async def test_multi_artifact_parent_does_not_flap_from_noncanonical_rows(tmp_path, monkeypatch):
    """Section 7's canonical-membership predicate applies to aggregation too:
    a blocked (non-canonical) sibling's own state must never influence the
    parent decision, regardless of how it churns."""
    engine, repository, providers, _executor, _now_box = await build_engine3(tmp_path, monkeypatch)
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]
    assert live.execution is not None

    async with get_db() as db:
        await db.execute(
            """INSERT INTO download_files(torrent_id,request_id,filename,size_bytes,local_path,status,blocked,candidates,download_client)
               VALUES(?,NULL,'blocked.bin',10,'',?,1,'[]','')""",
            (canonical.id, "error"),
        )
        await db.commit()

    outcome = await repository.aggregate_lifecycle(canonical.id, input_required=False)
    assert outcome is not None
    assert {item.id for item in outcome.artifacts} == {live.id}, (
        "the blocked, non-request-bound row must never be counted as a "
        "canonical artifact in the aggregation decision"
    )
    transfer = await repository.get(canonical.id)
    assert transfer.state == TransferState.TRANSFERRING, (
        "a blocked sibling sitting in 'error' must not flip the parent to FAILED"
    )

    # Churning the blocked row's own status must never move the parent.
    async with get_db() as db:
        await db.execute(
            "UPDATE download_files SET status='queued' WHERE torrent_id=? AND blocked=1", (canonical.id,),
        )
        await db.commit()
    outcome2 = await repository.aggregate_lifecycle(canonical.id, input_required=False)
    assert outcome2 is not None
    transfer2 = await repository.get(canonical.id)
    assert transfer2.state == TransferState.TRANSFERRING
    assert transition_allowed(TransferState.TRANSFERRING, TransferState.TRANSFERRING)


def test_only_approved_owners_write_parent_status():
    """Base document Section 35/44's architecture assertion: "only approved
    lifecycle owners write parent status." A source-text check is the right
    tool for this specific structural question (which FILES ever emit
    ``UPDATE torrents SET status``) -- there is no behavioral proxy for "no
    new writer exists anywhere in the tree." Any file outside
    ``_APPROVED_PARENT_STATUS_WRITERS`` gaining this fragment is exactly the
    second-parent-status-authority regression Section 24 forbids, whether it
    lands in api/, application/, an executor, or a provider."""
    pattern = re.compile(r"UPDATE\s+torrents\s+SET\s+status", re.IGNORECASE)
    offenders = []
    for path in _BACKEND.rglob("*.py"):
        relative = path.relative_to(_BACKEND).as_posix()
        if relative.startswith("tests/"):
            continue
        if pattern.search(path.read_text()) and relative not in _APPROVED_PARENT_STATUS_WRITERS:
            offenders.append(relative)
    assert offenders == [], (
        f"unapproved parent-status writer(s) found: {offenders}; either this is a "
        "genuine new lifecycle owner that needs review, or it should route through "
        "the existing canonical transition instead"
    )
    # The approved list itself must not silently rot into a lie: every listed
    # file must still actually contain the fragment, or it should be removed.
    missing = [
        relative for relative in _APPROVED_PARENT_STATUS_WRITERS
        if not pattern.search((_BACKEND / relative).read_text())
    ]
    assert missing == [], f"approved writer(s) no longer contain the fragment: {missing}"
