"""Native continuation across Pause, and source switches as portable
continuation (1.0.13).

Executor-private continuation state is disposable acceleration for the SAME
source only: DebridPulse stays the sole owner of lifecycle intent, material
truth, writer authority and continuation policy, and a source switch --
operator, Resume or automatic -- is always a fresh writer that keeps what the
one planner retains. Driven through the real engine and repository with a
byte-moving executor that writes SPARSE pieces and parks across Pause (native
quiesce + native private resume).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import db.database as database
from dataclasses import replace

from continuation_fakes import (
    MIB, NATIVE, SPARSE_NATIVE, NativeSpoolExecutor, SpoolExecutor, SpoolProvider, payload,
)
from transfers import codec
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage
from transfers.manual_failover import (
    DiscardConfirmationRequired, manual_candidate_failover, preview_candidate_switch,
)
from transfers.models import (
    ContinuationCapability, ContinuationStrategy, ExecutionState, ExecutorCapabilities, TransferRequest,
)
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry


SIZE = 6 * MIB + 4321
# Two segments written by a multi-connection job: DP-valid material is sparse.
SPARSE = ((0, 2 * MIB), (3 * MIB, 4 * MIB))


async def build(tmp_path, monkeypatch, *, continuation=NATIVE):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "native.db")
    await database.init_db()
    sources = {"movie": payload(SIZE)}
    repository = TransferRepository()
    registry = IntegrationRegistry()
    for identity, scheme in (("src-a", "parka"), ("src-b", "parkb"), ("src-c", "parkc"), ("src-z", "parkz"),
                             ("src-o", "spoolo")):
        registry.register_provider(SpoolProvider(identity, scheme, sources))
    native = NativeSpoolExecutor(repository.authorize_execution, sources, schemes=("parka", "parkb", "parkc", "parkz"),
                                 continuation=continuation)
    other = SpoolExecutor(repository.authorize_execution, sources, identity="other", scheme="spoolo")
    registry.register_executor(native)
    registry.register_executor(other)
    clock = [1000.0]
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"),
                            policy=TransferPolicy(retry_delay=0, adoption_stability_seconds=0,
                                                  max_active_executions=4),
                            clock=lambda: clock[0])
    await engine.initialize()
    return SimpleNamespace(engine=engine, repository=repository, native=native, other=other, clock=clock,
                           source=sources["movie"])


async def artifact_of(ctx, transfer_id):
    return (await ctx.repository.artifacts(transfer_id))[0]


async def running_sparse(ctx):
    """A running writer on source A with sparse DP-valid material."""
    transfer = await ctx.engine.submit((TransferRequest("spool", "movie", name="movie.bin",
                                                        preferred_provider="src-a"),), deduplicate=False)
    await ctx.engine.tick()
    artifact = await artifact_of(ctx, transfer.id)
    ctx.native.write(artifact.execution, 0, 2 * MIB + 5)
    ctx.native.write(artifact.execution, 3 * MIB, 4 * MIB + 100)
    ctx.clock[0] += 6
    await ctx.engine.reconcile_executions()
    assert (await ctx.repository.material_state(artifact.id)).valid == SPARSE
    return transfer, artifact


async def attach(ctx, transfer, provider):
    await ctx.engine.submit((TransferRequest("spool", "movie", name="movie.bin", preferred_provider=provider),),
                            deduplicate=False)
    await ctx.engine.resolve_pending()
    artifact = await artifact_of(ctx, transfer.id)
    return artifact, next(item for item in artifact.candidates if item.provider_id == provider)


async def audit(ctx, transfer_id, event):
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT detail FROM application_events WHERE kind='material_audit' AND transfer_id=?"
                                 " ORDER BY id", (transfer_id,))
    return [detail for detail in (codec.load(row["detail"]) for row in rows) if detail["event"] == event]


async def provenance_outcome(attempt_id):
    async with database.get_db() as db:
        row = await db.fetchone("SELECT outcome FROM execution_attempt_provenance WHERE execution_attempt_id=?",
                                (attempt_id,))
    return row["outcome"]


def starts(ctx):
    return [call for call in ctx.native.calls if call[0] == "start"]


# -- Pause / Resume ----------------------------------------------------------

@pytest.mark.asyncio
async def test_pause_checkpoints_sparse_material_then_parks_instead_of_cancelling(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
    transfer, artifact = await running_sparse(ctx)
    ctx.native.write(artifact.execution, 5 * MIB, 6 * MIB + 50)  # written, not yet checkpointed

    assert await ctx.engine.pause(transfer.id) == ()
    parked = await artifact_of(ctx, transfer.id)
    job = ctx.native.job_for(artifact.execution)
    assert parked.execution == artifact.execution and job.state == ExecutionState.PAUSED
    assert not [call for call in ctx.native.calls if call[0] == "cancel"]
    # The forced checkpoint ran BEFORE parking and committed the newest piece.
    assert (await ctx.repository.material_state(artifact.id)).valid == (*SPARSE, (5 * MIB, 6 * MIB))
    parked_event = (await audit(ctx, transfer.id, "writer_parked"))[-1]
    assert parked_event["checkpointed"] is True and parked_event["quiesce"] == "graceful"

    # No progress authority while the pause intent stands...
    for action in ("resume", "start"):
        assert not await ctx.repository.authorize_execution(artifact.execution, action)
    for _ in range(3):
        await ctx.engine.reconcile_executions()
    assert job.state == ExecutionState.PAUSED
    assert not [call for call in ctx.native.calls if call[0] == "resume"]
    # ...and a parked job observed acquiring again is re-quiesced, not left running.
    job.state = ExecutionState.RUNNING
    await ctx.engine.reconcile_executions()
    assert job.state == ExecutionState.PAUSED and (await artifact_of(ctx, transfer.id)).execution == artifact.execution


@pytest.mark.asyncio
async def test_resume_continues_the_same_native_job_without_truncation_or_private_state_loss(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
    transfer, artifact = await running_sparse(ctx)
    await ctx.engine.pause(transfer.id)
    before = await ctx.repository.material_state(artifact.id)
    journal = Path(ctx.native.journal(artifact.target))
    assert journal.exists()

    await ctx.engine.resume(transfer.id)
    await ctx.engine.reconcile_executions()
    resumed = await artifact_of(ctx, transfer.id)
    job = ctx.native.job_for(artifact.execution)
    # The same DP writer continues the same native job: no fresh writer.
    assert resumed.execution == artifact.execution and job.state == ExecutionState.RUNNING
    assert ("resume", artifact.execution.attempt_id) in ctx.native.calls and len(starts(ctx)) == 1
    # Nothing was truncated, the private journal survived, sparse ranges stay
    # valid and neither generation moved.
    assert journal.exists()
    with open(artifact.target, "rb") as handle:
        handle.seek(3 * MIB)
        assert handle.read(MIB) == ctx.source[3 * MIB:4 * MIB]
    after = await ctx.repository.material_state(artifact.id)
    assert after.valid == SPARSE == before.valid
    assert (after.material_generation, after.writer_generation) == (before.material_generation, before.writer_generation)
    assert not await audit(ctx, transfer.id, "rollback")

    ctx.native.finish(resumed.execution)
    await ctx.engine.reconcile_executions()
    assert (await artifact_of(ctx, transfer.id)).state == "completed"
    assert Path(artifact.target).read_bytes() == ctx.source


@pytest.mark.asyncio
async def test_a_lost_parked_job_falls_back_to_the_portable_planner(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
    transfer, artifact = await running_sparse(ctx)
    await ctx.engine.pause(transfer.id)
    del ctx.native.jobs[artifact.execution.native["job"]]  # e.g. the executor restarted

    await ctx.engine.resume(transfer.id)
    for _ in range(4):
        ctx.clock[0] += 60
        await ctx.engine.reconcile_executions()
        await ctx.engine.tick()
        if len(starts(ctx)) > 1:
            break
    plan = ctx.native.plans[-1]
    assert len(starts(ctx)) == 2
    # No correctness dependency on the parked job: DP material alone plans.
    assert plan.strategy == ContinuationStrategy.CONTIGUOUS_FROM_OFFSET and plan.boundary == 2 * MIB
    assert plan.discarded == ((3 * MIB, 4 * MIB),)


@pytest.mark.asyncio
async def test_a_parked_job_of_a_stale_material_generation_is_retired_not_resumed(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
    transfer, artifact = await running_sparse(ctx)
    await ctx.engine.pause(transfer.id)
    await ctx.repository.invalidate_material(artifact.id, "test_rewrite")

    await ctx.engine.resume(transfer.id)
    for _ in range(3):
        await ctx.engine.reconcile_executions()
        await ctx.engine.tick()
    assert ("resume", artifact.execution.attempt_id) not in ctx.native.calls
    assert ("cancel", artifact.execution.attempt_id) in ctx.native.calls
    assert ctx.native.plans[-1].strategy == ContinuationStrategy.FULL_RESTART


# -- Source switch: a fresh writer, planned portably --------------------------------

async def switch_events(transfer_id):
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT detail FROM application_events WHERE kind='manual_candidate_failover'"
                                 " AND transfer_id=? ORDER BY id", (transfer_id,))
    return [codec.load(row["detail"]) for row in rows]


async def assert_fresh_sparse_writer(ctx, transfer, old, provider, *, fenced=True):
    """The replacement is a NEW attempt and a NEW native job, started through
    the executor's ordinary start with a plan that keeps every sparse range in
    place; the retired job was cancelled (``fenced``; a failed one is already
    terminal), never re-pointed."""
    current = await artifact_of(ctx, transfer.id)
    new = current.execution
    assert new is not None and new.attempt_id != old.attempt_id and new.native != old.native
    assert current.candidates[current.selected].provider_id == provider
    assert (("cancel", old.attempt_id) in ctx.native.calls) == fenced and ("start", new.attempt_id) in ctx.native.calls
    assert not await ctx.repository.authorize_execution(old, "observe")
    plan = await ctx.repository.execution_continuation(new.attempt_id)
    assert plan == ctx.native.plans[-1]
    assert plan.strategy == ContinuationStrategy.SPARSE_IMPORT and plan.retained == SPARSE and plan.discarded == ()
    job = ctx.native.job_for(new)
    assert job.address.startswith(provider.replace("src-", "park")) and job.pieces == SPARSE
    with open(current.target, "rb") as handle:  # the payload was reused in place
        handle.seek(3 * MIB)
        assert handle.read(MIB) == ctx.source[3 * MIB:4 * MIB]
    state = await ctx.repository.material_state(current.id)
    assert state.valid == SPARSE and state.material_generation == 1 and state.writer_generation == 2
    assert not await audit(ctx, transfer.id, "rollback") and not await audit(ctx, transfer.id, "native_handoff")
    return current, new


@pytest.mark.asyncio
async def test_a_running_switch_is_a_fresh_writer_keeping_every_sparse_range(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch, continuation=SPARSE_NATIVE)
    transfer, first = await running_sparse(ctx)
    artifact, source_b = await attach(ctx, transfer, "src-b")

    preview = await preview_candidate_switch(ctx.engine, transfer.id, artifact.id, str(source_b.id))
    assert preview["discarded_bytes"] == 0 and preview["retained_bytes"] == 3 * MIB
    # Nothing is discarded, so nothing is confirmed: permit_discard=False.
    assert (await manual_candidate_failover(ctx.engine, transfer.id, artifact.id, str(source_b.id)))["ok"]
    await ctx.engine.tick()
    current, new = await assert_fresh_sparse_writer(ctx, transfer, first.execution, "src-b")
    assert (await switch_events(transfer.id))[-1]["execution_transition"] == "retired_and_redispatch"

    ctx.native.finish(new)
    await ctx.engine.reconcile_executions()
    assert (await artifact_of(ctx, transfer.id)).state == "completed"
    assert Path(current.target).read_bytes() == ctx.source


@pytest.mark.asyncio
async def test_no_discard_is_decided_by_the_fresh_writers_plan(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)  # contiguous continuation only: sparse ranges are unusable
    transfer, first = await running_sparse(ctx)
    artifact, source_b = await attach(ctx, transfer, "src-b")
    index = next(i for i, item in enumerate(artifact.candidates) if item.id == source_b.id)

    # The plan would discard: a switch that may not is refused, the quiesced
    # writer stays and the lifecycle owner continues it on its own source.
    refused = await ctx.engine.activate_candidate_command(transfer.id, artifact.id, index, permit_discard=False)
    assert not refused.committed and refused.reason == "material_discard_refused"
    await ctx.engine.reconcile_executions()
    assert (await artifact_of(ctx, transfer.id)).execution == first.execution
    assert ctx.native.job_for(first.execution).state == ExecutionState.RUNNING
    assert not [call for call in ctx.native.calls if call[0] == "cancel"]
    assert (await ctx.repository.material_state(artifact.id)).valid == SPARSE

    # The operator is told exactly what is lost; a confirmed discard proceeds.
    preview = await preview_candidate_switch(ctx.engine, transfer.id, artifact.id, str(source_b.id))
    assert preview["discarded_bytes"] == MIB and preview["retained_bytes"] == 2 * MIB
    with pytest.raises(DiscardConfirmationRequired) as asked:
        await manual_candidate_failover(ctx.engine, transfer.id, artifact.id, str(source_b.id))
    assert asked.value.discarded_bytes == MIB
    await manual_candidate_failover(ctx.engine, transfer.id, artifact.id, str(source_b.id), discard_confirmed=True,
                                    discard_confirmation=preview)
    await ctx.engine.tick()
    plan = ctx.native.plans[-1]
    assert ("cancel", first.execution.attempt_id) in ctx.native.calls and len(starts(ctx)) == 2
    assert plan.strategy == ContinuationStrategy.CONTIGUOUS_FROM_OFFSET and plan.boundary == 2 * MIB
    assert plan.discarded == ((3 * MIB, 4 * MIB),)


@pytest.mark.asyncio
async def test_a_paused_switch_is_a_desired_source_that_resume_completes_with_a_fresh_writer(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch, continuation=SPARSE_NATIVE)
    transfer, first = await running_sparse(ctx)
    artifact, source_b = await attach(ctx, transfer, "src-b")
    artifact, source_c = await attach(ctx, transfer, "src-c")
    await ctx.engine.pause(transfer.id)
    old = first.execution
    job = ctx.native.job_for(old)

    for choice in (source_b, source_c):  # several paused switches collapse
        assert (await manual_candidate_failover(ctx.engine, transfer.id, artifact.id, str(choice.id)))["ok"]
    current = await artifact_of(ctx, transfer.id)
    # Only the desire changed: the parked writer, its candidate, its native
    # job and all material are untouched, and no new writer exists.
    assert current.candidates[current.selected].provider_id == "src-c" and current.execution == old
    assert len(await ctx.repository.executions(transfer.id)) == 1
    assert (await ctx.engine.writer_candidate(current)).provider_id == "src-a"
    assert job.address == "parka:movie" and job.state == ExecutionState.PAUSED
    assert not [call for call in ctx.native.calls if call[0] in {"resume", "cancel"}]
    assert [item["transition"] for item in await audit(ctx, transfer.id, "source_transition")] == [
        "pending", "pending"]
    assert [item["execution_transition"] for item in await switch_events(transfer.id)] == [
        "pending_source_transition", "pending_source_transition"]

    # Resume completes it through the one candidate activation: straight to
    # the final choice, as a fresh writer -- the parked job is never resumed.
    await ctx.engine.resume(transfer.id)
    await ctx.engine.reconcile_executions()
    await ctx.engine.tick()
    await assert_fresh_sparse_writer(ctx, transfer, old, "src-c")
    assert ("resume", old.attempt_id) not in ctx.native.calls and len(starts(ctx)) == 2


@pytest.mark.asyncio
async def test_switching_back_while_paused_withdraws_the_desired_source(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch, continuation=SPARSE_NATIVE)
    transfer, first = await running_sparse(ctx)
    artifact, source_b = await attach(ctx, transfer, "src-b")
    source_a = next(item for item in artifact.candidates if item.provider_id == "src-a")
    await ctx.engine.pause(transfer.id)
    await manual_candidate_failover(ctx.engine, transfer.id, artifact.id, str(source_b.id))
    await manual_candidate_failover(ctx.engine, transfer.id, artifact.id, str(source_a.id))
    current = await artifact_of(ctx, transfer.id)
    assert current.candidates[current.selected].provider_id == "src-a"
    assert await ctx.engine.pending_source(current) is None
    assert [item["transition"] for item in await audit(ctx, transfer.id, "source_transition")] == [
        "pending", "withdrawn"]

    await ctx.engine.resume(transfer.id)
    await ctx.engine.reconcile_executions()
    # Plain Resume of the same job: the same-source native lifecycle.
    assert (await artifact_of(ctx, transfer.id)).execution == first.execution
    assert ("resume", first.execution.attempt_id) in ctx.native.calls and len(starts(ctx)) == 1


@pytest.mark.asyncio
async def test_a_paused_switch_that_would_discard_at_resume_keeps_the_parked_writer_and_reports_it(
        tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch, continuation=SPARSE_NATIVE)
    transfer, first = await running_sparse(ctx)
    artifact, source_b = await attach(ctx, transfer, "src-b")
    await ctx.engine.pause(transfer.id)
    await manual_candidate_failover(ctx.engine, transfer.id, artifact.id, str(source_b.id))
    # The executor can no longer import sparse material: the fresh writer
    # would now discard what the operator was told survives.
    ctx.native.capabilities = replace(ctx.native.capabilities, continuation=NATIVE)

    await ctx.engine.resume(transfer.id)
    for _ in range(2):
        await ctx.engine.reconcile_executions()
    current = await artifact_of(ctx, transfer.id)
    # No silent destructive fallback: the desire is withdrawn, the operator is
    # told, and the intact parked writer continues on its own source.
    assert current.execution == first.execution and current.candidates[current.selected].provider_id == "src-a"
    assert ctx.native.job_for(first.execution).state == ExecutionState.RUNNING
    assert not [call for call in ctx.native.calls if call[0] == "cancel"] and len(starts(ctx)) == 1
    assert (await ctx.repository.material_state(artifact.id)).valid == SPARSE
    assert (await audit(ctx, transfer.id, "source_transition"))[-1]["transition"] == "withdrawn"
    assert [item["outcome"] for item in await switch_events(transfer.id)] == ["success", "failure"]


@pytest.mark.asyncio
async def test_automatic_failover_is_the_same_fresh_writer_activation(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch, continuation=SPARSE_NATIVE)
    transfer, first = await running_sparse(ctx)
    await attach(ctx, transfer, "src-b")
    job = ctx.native.job_for(first.execution)
    job.state = ExecutionState.FAILED
    observe = ctx.native._observation

    def gone(handle, current):
        observed = observe(handle, current)
        if current.state == ExecutionState.FAILED:
            observed = replace(observed, error=NormalizedError(
                Domain.NETWORK, Category.SOURCE_NOT_FOUND, Stage.EXECUTION, retryability=Retryability.NEVER,
                integration_id="native"))
        return observed
    ctx.native._observation = gone

    for _ in range(3):
        await ctx.engine.reconcile_executions()
        await ctx.engine.tick()
        if len(starts(ctx)) > 1:
            break
    await assert_fresh_sparse_writer(ctx, transfer, first.execution, "src-b", fenced=False)


def test_private_resume_must_be_implemented_and_source_retarget_is_not_a_capability():
    def declaring(continuation, *, pause=True):
        executor = SpoolExecutor(None, {}, identity="declaring", scheme="declaring")
        executor.capabilities = ExecutorCapabilities(per_execution_pause=pause, continuation=frozenset(continuation))
        return executor

    # Private resume IS the per-execution resume operation.
    with pytest.raises(TypeError):
        IntegrationRegistry().register_executor(declaring(
            {ContinuationCapability.FULL_RESTART, ContinuationCapability.NATIVE_PRIVATE_RESUME}, pause=False))
    IntegrationRegistry().register_executor(NativeSpoolExecutor(None, {}))
    assert not hasattr(ContinuationCapability, "NATIVE_SOURCE_RETARGET")
