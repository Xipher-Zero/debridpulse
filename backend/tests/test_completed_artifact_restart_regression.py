"""1.0.14: a settled artifact is never reopened by a later observation of the
attempt that once wrote it, nor by startup recovery.

Transfer 1279 (2026-11-02): artifact 22408 was verified and committed
``completed`` at 01:35:54.588 UTC. After a VM reboot the restarted aria2 no
longer knew the finished job; startup reconciliation observed the retained
attempt ABSENT and the observation writer turned the completed file into
``lost``; Resume then ran orphan recovery and completed it a second time from
its durable material (investigations/dp-1.0.14-delayed-completion-1279).

Real ``TransferEngine`` and ``TransferRepository`` on a scratch database; the
executor is the in-process spool fake (``continuation_fakes``): no daemon,
port, network or shared storage. A "restart" is a new engine and a new
executor that holds no jobs over the same database.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import db.database as database
from continuation_fakes import MIB, SpoolExecutor, SpoolProvider, payload
from transfers.convergence_engine import TransferEngine
from transfers.models import ExecutionObservation, ExecutionState, TransferProgress, TransferRequest
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio
SOURCES = {"a": payload(2 * MIB, "a"), "b": payload(2 * MIB, "b")}


@pytest.fixture(autouse=True)
def scratch_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "restart.db")


async def boot(tmp_path, *, first=True):
    """One application start: engine, repository and an executor that knows
    only the jobs started in this process."""
    if first:
        await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    registry.register_provider(SpoolProvider("src", "spoola", SOURCES))
    executor = SpoolExecutor(repository.authorize_execution, SOURCES, identity="spool-a", scheme="spoola")
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"),
                            policy=TransferPolicy(retry_delay=0, adoption_stability_seconds=0,
                                                  max_active_executions=4))
    await engine.initialize()
    return SimpleNamespace(engine=engine, repository=repository, executor=executor)


async def run(ctx, passes=4):
    for _ in range(passes):
        await ctx.engine.reconcile_executions()
        await ctx.engine.tick()


async def row(artifact_id):
    async with database.get_db() as db:
        return await db.fetchone("SELECT status,execution_attempt_id FROM download_files WHERE id=?", (artifact_id,))


async def journaled(artifact_id, event):
    async with database.get_db() as db:
        found = await db.fetchall("SELECT 1 FROM event_journal WHERE subject_id=? AND event_type=?",
                                  (str(artifact_id), event))
    return len(found)


async def attempts(artifact_id):
    async with database.get_db() as db:
        return await db.fetchall("SELECT id,state FROM execution_attempts WHERE artifact_id=? ORDER BY rowid",
                                 (artifact_id,))


async def orphan_decisions(artifact_id):
    async with database.get_db() as db:
        found = await db.fetchall("""SELECT 1 FROM event_journal WHERE subject_id=? AND event_type='recovery.decided'
            AND error_category='orphaned_resource'""", (str(artifact_id),))
    return len(found)


async def with_completed_sibling(ctx):
    """One transfer, two files: 'a' verified and committed complete, 'b'
    still unfinished -- so the transfer stays active, as 1279 did."""
    transfer = await ctx.engine.submit((TransferRequest("spool", "a", name="a.bin"),
                                        TransferRequest("spool", "b", name="b.bin")), deduplicate=False)
    await run(ctx)
    a, b = sorted(await ctx.repository.artifacts(transfer.id), key=lambda item: item.id)
    assert a.execution is not None and b.execution is not None
    ctx.executor.step(a.execution.attempt_id, 2 * MIB)
    await run(ctx, 3)
    completed = await row(a.id)
    assert completed["status"] == "completed" and completed["execution_attempt_id"] == a.execution.attempt_id
    assert await journaled(a.id, "execution.file_completed") == 1
    return transfer, a, b


@pytest.mark.parametrize("paused", [True, False], ids=["D1_paused_across_restart", "D2_not_paused"])
async def test_a_restart_never_reopens_a_completed_artifact(tmp_path, paused):
    ctx = await boot(tmp_path)
    transfer, a, b = await with_completed_sibling(ctx)
    target = Path(a.target)
    content = target.read_bytes()
    before = await attempts(a.id)
    if paused:
        await ctx.engine.pause(transfer.id)

    ctx = await boot(tmp_path, first=False)  # restart: the new executor knows no job at all
    await ctx.engine.reconcile_executions()
    assert (await row(a.id))["status"] == "completed"
    if paused:
        await ctx.engine.resume(transfer.id)
    await run(ctx, 6)

    # The completed file stays completed and is completed once: no orphan
    # recovery, no new writer, no download, its bytes untouched.
    assert (await row(a.id))["status"] == "completed"
    assert await journaled(a.id, "execution.file_completed") == 1
    assert await orphan_decisions(a.id) == 0
    assert await attempts(a.id) == before
    assert not [call for call in ctx.executor.calls if call[0] == "start" and call[1] == a.execution.attempt_id]
    assert target.read_bytes() == content
    # Its unfinished sibling is recovered as before: a fresh writer, which completes.
    restarted = [call for call in ctx.executor.calls if call[0] == "start"]
    assert len(restarted) == 1 and ctx.executor.jobs[restarted[0][1]].target == b.target
    ctx.executor.step(restarted[0][1], 2 * MIB)
    await run(ctx, 3)
    assert (await row(b.id))["status"] == "completed"


async def test_D0_same_boot_pause_and_resume_leave_a_completed_artifact_alone(tmp_path):
    ctx = await boot(tmp_path)
    transfer, a, _b = await with_completed_sibling(ctx)
    await ctx.engine.pause(transfer.id)
    await ctx.engine.resume(transfer.id)
    await run(ctx, 5)
    assert (await row(a.id))["status"] == "completed"
    assert await journaled(a.id, "execution.file_completed") == 1


async def test_full_bytes_alone_never_complete_an_artifact(tmp_path):
    """Completion still needs the native SUCCEEDED and verification."""
    ctx = await boot(tmp_path)
    transfer = await ctx.engine.submit((TransferRequest("spool", "a", name="a.bin"),), deduplicate=False)
    await run(ctx)
    a = (await ctx.repository.artifacts(transfer.id))[0]
    ctx.executor.step(a.execution.attempt_id, 2 * MIB)
    ctx.executor.jobs[a.execution.attempt_id].state = ExecutionState.RUNNING
    await run(ctx, 3)
    assert (await row(a.id))["status"] == "downloading"
    assert await journaled(a.id, "execution.file_completed") == 0


def observed(handle, state):
    return ExecutionObservation(handle, state, TransferProgress(2 * MIB, 2 * MIB if state == ExecutionState.SUCCEEDED
                                                                else MIB, 0))


@pytest.mark.parametrize("state", [ExecutionState.SUCCEEDED, ExecutionState.ABSENT, ExecutionState.FAILED,
                                   ExecutionState.UNKNOWN, ExecutionState.CANCELLED, ExecutionState.RUNNING,
                                   ExecutionState.QUEUED, ExecutionState.PAUSED])
async def test_a_late_observation_records_the_attempt_but_never_moves_a_completed_file(tmp_path, state):
    ctx = await boot(tmp_path)
    _transfer, a, _b = await with_completed_sibling(ctx)
    persisted = await ctx.repository.execution(observed(a.execution, state))
    assert persisted is True
    # The attempt's own truth moves; the settled file does not.
    assert (await attempts(a.id))[-1]["state"] == state.value
    assert (await row(a.id))["status"] == "completed"


async def test_a_duplicate_standby_is_not_moved_by_an_observation_either(tmp_path):
    ctx = await boot(tmp_path)
    _transfer, _a, b = await with_completed_sibling(ctx)
    async with database.get_db() as db:
        # A standby that still carries an attempt reference (the database
        # predicate is what is under test, not how one came to be).
        await db.execute("UPDATE download_files SET status='duplicate' WHERE id=?", (b.id,))
        await db.commit()
    await ctx.repository.execution(observed(b.execution, ExecutionState.ABSENT))
    assert (await row(b.id))["status"] == "duplicate"


async def test_an_observation_taken_before_completion_cannot_reopen_it_after(tmp_path):
    """The race: an observation of the writer is obtained while the file is
    still being verified, completion commits, and only then is the stale
    observation projected. The database statement itself refuses it."""
    ctx = await boot(tmp_path)
    transfer = await ctx.engine.submit((TransferRequest("spool", "a", name="a.bin"),
                                        TransferRequest("spool", "b", name="b.bin")), deduplicate=False)
    await run(ctx)
    a = sorted(await ctx.repository.artifacts(transfer.id), key=lambda item: item.id)[0]
    stale = {state: observed(a.execution, state) for state in (ExecutionState.RUNNING, ExecutionState.SUCCEEDED,
                                                               ExecutionState.ABSENT)}
    await ctx.repository.execution(stale[ExecutionState.SUCCEEDED])
    assert (await row(a.id))["status"] == "verifying"
    await ctx.repository.artifact_state(a.id, "completed")  # the completion commit lands first
    for state, observation in stale.items():
        await ctx.repository.execution(observation)
        assert (await row(a.id))["status"] == "completed", state


@pytest.mark.parametrize("state,expected", [(ExecutionState.ABSENT, "lost"), (ExecutionState.FAILED, "error"),
                                            (ExecutionState.RUNNING, "downloading"), (ExecutionState.QUEUED, "queued"),
                                            (ExecutionState.PAUSED, "paused"), (ExecutionState.UNKNOWN, "unknown"),
                                            (ExecutionState.SUCCEEDED, "verifying")])
async def test_an_unfinished_file_still_takes_its_writers_observed_state(tmp_path, state, expected):
    ctx = await boot(tmp_path)
    transfer = await ctx.engine.submit((TransferRequest("spool", "b", name="b.bin"),), deduplicate=False)
    await run(ctx)
    b = (await ctx.repository.artifacts(transfer.id))[0]
    assert (await row(b.id))["status"] == "downloading"
    await ctx.repository.execution(observed(b.execution, state))
    assert (await row(b.id))["status"] == expected


async def test_an_unfinished_file_whose_job_vanished_is_still_recovered_after_a_restart(tmp_path):
    ctx = await boot(tmp_path)
    transfer = await ctx.engine.submit((TransferRequest("spool", "b", name="b.bin"),), deduplicate=False)
    await run(ctx)
    b = (await ctx.repository.artifacts(transfer.id))[0]
    ctx = await boot(tmp_path, first=False)
    await run(ctx, 4)
    started = [call for call in ctx.executor.calls if call[0] == "start"]
    assert started and ctx.executor.jobs[started[-1][1]].target == b.target
    ctx.executor.step(started[-1][1], 2 * MIB)
    await run(ctx, 3)
    assert (await row(b.id))["status"] == "completed"


async def test_an_explicit_state_change_of_a_completed_file_is_still_its_owners_to_make(tmp_path):
    """Only observations are refused: the lifecycle writer an authorized
    operation uses (reacquisition, cancellation, ...) is unchanged."""
    ctx = await boot(tmp_path)
    _transfer, a, _b = await with_completed_sibling(ctx)
    await ctx.repository.artifact_state(a.id, "unresolved", release=True)
    assert (await row(a.id))["status"] == "unresolved"
