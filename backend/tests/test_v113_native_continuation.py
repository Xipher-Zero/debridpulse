"""Native continuation across Pause and same-executor source retarget (1.0.13).

Executor-private continuation state is disposable acceleration: DebridPulse
stays the sole owner of lifecycle intent, material truth, writer authority and
continuation policy. Driven through the real engine and repository with a
byte-moving executor that writes SPARSE pieces, parks across Pause (native
quiesce + native private resume) and retargets a paused job's source.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import db.database as database
from continuation_fakes import MIB, NativeSpoolExecutor, SpoolExecutor, SpoolProvider, payload
from transfers import codec
from transfers.continuation import plan_continuation
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, TransferError
from transfers.manual_failover import (
    DiscardConfirmationRequired, manual_candidate_failover, preview_candidate_switch,
)
from transfers.models import (
    ContinuationCapability, ContinuationStrategy, ExecutionState, ExecutorCapabilities, TransferRequest,
)
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry
from test_v113_material_continuation import candidate as bare_candidate, state as material


SIZE = 6 * MIB + 4321
# Two segments written by a multi-connection job: DP-valid material is sparse.
SPARSE = ((0, 2 * MIB), (3 * MIB, 4 * MIB))


async def build(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "native.db")
    await database.init_db()
    sources = {"movie": payload(SIZE)}
    repository = TransferRepository()
    registry = IntegrationRegistry()
    for identity, scheme in (("src-a", "parka"), ("src-b", "parkb"), ("src-c", "parkc"), ("src-z", "parkz"),
                             ("src-o", "spoolo")):
        registry.register_provider(SpoolProvider(identity, scheme, sources))
    native = NativeSpoolExecutor(repository.authorize_execution, sources, schemes=("parka", "parkb", "parkc", "parkz"),
                                 retargetable=("parka", "parkb", "parkc"))
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
    for action in ("resume", "start", "retarget"):
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


# -- Source retarget -------------------------------------------------------------

@pytest.mark.asyncio
async def test_running_switch_hands_the_native_job_to_a_new_writer_attempt(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
    transfer, first = await running_sparse(ctx)
    artifact, source_b = await attach(ctx, transfer, "src-b")
    old = first.execution

    preview = await preview_candidate_switch(ctx.engine, transfer.id, artifact.id, str(source_b.id))
    assert preview["discarded_bytes"] == 0 and preview["retained_bytes"] == 3 * MIB
    # Zero-loss handoff: no confirmation is asked for.
    result = await manual_candidate_failover(ctx.engine, transfer.id, artifact.id, str(source_b.id))
    assert result["ok"]

    current = await artifact_of(ctx, transfer.id)
    new = current.execution
    assert new.attempt_id != old.attempt_id and new.native == old.native
    assert current.candidates[current.selected].provider_id == "src-b"
    attempts = {item.handle.attempt_id: item for item in await ctx.repository.executions(transfer.id)}
    # The old attempt stays historically bound to source A; the new one to B.
    assert attempts[old.attempt_id].candidate.provider_id == "src-a"
    assert attempts[new.attempt_id].candidate.provider_id == "src-b"
    assert await provenance_outcome(old.attempt_id) == "handed_off"
    assert not await ctx.repository.authorize_execution(old, "observe")

    job = ctx.native.job_for(new)
    assert job.address == "parkb:movie" and job.owner == new.attempt_id
    assert job.state == ExecutionState.RUNNING  # DP intent is RUNNING: resumed by the lifecycle owner
    assert ("retarget", old.attempt_id, new.attempt_id) in ctx.native.calls and len(starts(ctx)) == 1
    state = await ctx.repository.material_state(artifact.id)
    assert state.valid == SPARSE and state.material_generation == 1 and state.writer_generation == 2
    plan = await ctx.repository.execution_continuation(new.attempt_id)
    assert plan.strategy == ContinuationStrategy.NATIVE_STATE_HANDOFF and plan.retained == SPARSE
    handoff = (await audit(ctx, transfer.id, "native_handoff"))[-1]
    assert (handoff["old_attempt_id"], handoff["new_attempt_id"]) == (old.attempt_id, new.attempt_id)
    assert (handoff["old_writer_generation"], handoff["new_writer_generation"]) == (1, 2)
    assert handoff["discarded_bytes"] == 0 and handoff["valid_bytes"] == 3 * MIB
    assert (await audit(ctx, transfer.id, "native_retarget"))[-1]["accepted"] is True
    assert not await audit(ctx, transfer.id, "rollback")

    ctx.native.finish(new)
    await ctx.engine.reconcile_executions()
    assert (await artifact_of(ctx, transfer.id)).state == "completed"
    assert Path(current.target).read_bytes() == ctx.source


@pytest.mark.asyncio
async def test_a_paused_switch_is_a_desired_source_that_resume_retargets_once(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
    transfer, first = await running_sparse(ctx)
    artifact, source_b = await attach(ctx, transfer, "src-b")
    artifact, source_c = await attach(ctx, transfer, "src-c")
    await ctx.engine.pause(transfer.id)
    old = first.execution
    job = ctx.native.job_for(old)

    for choice in (source_b, source_c):  # several paused switches collapse
        preview = await preview_candidate_switch(ctx.engine, transfer.id, artifact.id, str(choice.id))
        assert preview["discarded_bytes"] == 0
        result = await manual_candidate_failover(ctx.engine, transfer.id, artifact.id, str(choice.id))
        assert result["ok"]
    current = await artifact_of(ctx, transfer.id)
    # Only the desire changed: the parked writer, its candidate, its native
    # job and all material are untouched, and no new writer exists.
    assert current.candidates[current.selected].provider_id == "src-c" and current.execution == old
    assert len(await ctx.repository.executions(transfer.id)) == 1
    assert (await ctx.engine.writer_candidate(current)).provider_id == "src-a"
    assert job.address == "parka:movie" and job.state == ExecutionState.PAUSED
    assert not [call for call in ctx.native.calls if call[0] in {"retarget", "resume", "cancel"}]
    assert (await ctx.repository.material_state(artifact.id)).valid == SPARSE
    transitions = await audit(ctx, transfer.id, "source_transition")
    assert [item["transition"] for item in transitions] == ["pending", "pending"]
    for _ in range(2):
        await ctx.engine.reconcile_executions()
    assert job.state == ExecutionState.PAUSED and not await ctx.repository.authorize_execution(old, "resume")

    # Resume is the commit point: ONE retarget, straight to the final choice.
    await ctx.engine.resume(transfer.id)
    await ctx.engine.reconcile_executions()
    resumed = await artifact_of(ctx, transfer.id)
    new = resumed.execution
    assert new.attempt_id != old.attempt_id and new.native == old.native
    assert [call for call in ctx.native.calls if call[0] == "retarget"] == [("retarget", old.attempt_id, new.attempt_id)]
    assert job.address == "parkc:movie" and job.state == ExecutionState.RUNNING and len(starts(ctx)) == 1
    state = await ctx.repository.material_state(artifact.id)
    assert state.valid == SPARSE and state.material_generation == 1 and state.writer_generation == 2
    assert not await audit(ctx, transfer.id, "rollback")


@pytest.mark.asyncio
async def test_switching_back_while_paused_withdraws_the_desired_source(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
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
    # Plain Resume of the same job: no handoff, no retarget.
    assert (await artifact_of(ctx, transfer.id)).execution == first.execution
    assert ("resume", first.execution.attempt_id) in ctx.native.calls
    assert not [call for call in ctx.native.calls if call[0] == "retarget"]


@pytest.mark.asyncio
async def test_a_retarget_impossible_at_resume_keeps_the_parked_writer_and_reports_it(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
    transfer, first = await running_sparse(ctx)
    artifact, source_b = await attach(ctx, transfer, "src-b")
    await ctx.engine.pause(transfer.id)
    await manual_candidate_failover(ctx.engine, transfer.id, artifact.id, str(source_b.id))
    ctx.native.retargetable = frozenset()  # the pair is no longer retargetable

    await ctx.engine.resume(transfer.id)
    for _ in range(2):
        await ctx.engine.reconcile_executions()
    current = await artifact_of(ctx, transfer.id)
    # No silent destructive fallback: the desire is withdrawn, the operator is
    # told, and the intact parked writer continues on its own source.
    assert current.execution == first.execution and current.candidates[current.selected].provider_id == "src-a"
    assert ctx.native.job_for(first.execution).state == ExecutionState.RUNNING
    assert not [call for call in ctx.native.calls if call[0] in {"cancel", "retarget"}]
    assert (await ctx.repository.material_state(artifact.id)).valid == SPARSE
    assert (await audit(ctx, transfer.id, "source_transition"))[-1]["transition"] == "withdrawn"
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT detail FROM application_events WHERE kind='manual_candidate_failover'"
                                 " AND transfer_id=? ORDER BY id", (transfer.id,))
    assert [codec.load(row["detail"])["outcome"] for row in rows] == ["success", "failure"]


@pytest.mark.asyncio
async def test_an_incompatible_retarget_falls_back_portably_and_asks_for_confirmation(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
    transfer, first = await running_sparse(ctx)
    artifact, source_z = await attach(ctx, transfer, "src-z")  # same executor, not retargetable

    preview = await preview_candidate_switch(ctx.engine, transfer.id, artifact.id, str(source_z.id))
    assert preview["discarded_bytes"] == MIB and preview["retained_bytes"] == 2 * MIB
    with pytest.raises(DiscardConfirmationRequired) as refused:
        await manual_candidate_failover(ctx.engine, transfer.id, artifact.id, str(source_z.id))
    assert refused.value.discarded_bytes == MIB
    assert (await artifact_of(ctx, transfer.id)).execution == first.execution  # nothing changed

    await manual_candidate_failover(ctx.engine, transfer.id, artifact.id, str(source_z.id), discard_confirmed=True,
                                    discard_confirmation=preview)
    assert ("cancel", first.execution.attempt_id) in ctx.native.calls
    await ctx.engine.tick()
    plan = ctx.native.plans[-1]
    assert len(starts(ctx)) == 2 and plan.strategy == ContinuationStrategy.CONTIGUOUS_FROM_OFFSET
    assert plan.boundary == 2 * MIB and plan.discarded == ((3 * MIB, 4 * MIB),)
    unavailable = (await audit(ctx, transfer.id, "native_retarget"))[-1]
    assert unavailable["accepted"] is False and unavailable["fallback"] == "contiguous_from_offset"


@pytest.mark.asyncio
async def test_a_handoff_promised_by_the_preview_is_never_silently_replaced_by_a_discard(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
    transfer, first = await running_sparse(ctx)
    artifact, source_b = await attach(ctx, transfer, "src-b")
    asked = []

    async def change_after_preview():
        asked.append(1)
        if len(asked) > 1:  # the activation, after the zero-loss preview
            ctx.native.retargetable = frozenset()
    ctx.native.before_retarget_prepare = change_after_preview

    with pytest.raises(DiscardConfirmationRequired) as refused:
        await manual_candidate_failover(ctx.engine, transfer.id, artifact.id, str(source_b.id))
    assert refused.value.changed and refused.value.discarded_bytes == MIB
    # The old writer is intact and continues under the RUNNING intent.
    current = await artifact_of(ctx, transfer.id)
    assert current.execution == first.execution and current.candidates[current.selected].provider_id == "src-a"
    await ctx.engine.reconcile_executions()
    assert ctx.native.job_for(first.execution).state == ExecutionState.RUNNING
    assert not [call for call in ctx.native.calls if call[0] == "cancel"]


# -- Safety ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handoff_needs_the_same_executor_current_material_and_an_equivalent_candidate(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
    transfer, first = await running_sparse(ctx)
    artifact, source_o = await attach(ctx, transfer, "src-o")
    artifact, source_b = await attach(ctx, transfer, "src-b")
    # Another executor never inherits this executor's private state.
    assert not await ctx.engine.native_handoff_eligible(artifact, source_o, ctx.other)
    plan = await ctx.engine.preview_continuation(artifact, source_o)
    assert plan.strategy == ContinuationStrategy.CONTIGUOUS_FROM_OFFSET and plan.discarded_bytes == MIB
    assert await ctx.engine.native_handoff_eligible(artifact, source_b, ctx.native)
    # A source that is not an equivalent candidate of the artifact is never
    # reachable by a switch, so it never reaches a retarget.
    with pytest.raises(TransferError) as unknown:
        await preview_candidate_switch(ctx.engine, transfer.id, artifact.id, "not-an-equivalent-source")
    assert unknown.value.error.category == Category.SOURCE_NOT_FOUND
    # A changed material generation forbids the handoff.
    await ctx.repository.invalidate_material(artifact.id, "test_rewrite")
    assert not await ctx.engine.native_handoff_eligible(artifact, source_b, ctx.native)
    assert (await ctx.engine.preview_continuation(artifact, source_b)).strategy == ContinuationStrategy.FULL_RESTART
    assert not [call for call in ctx.native.calls if call[0] == "retarget"]


def test_the_planner_hands_off_only_for_a_declared_native_retarget():
    sparse = material(SPARSE, expected=SIZE)
    native = ExecutorCapabilities(per_execution_pause=True, continuation=frozenset({
        ContinuationCapability.FULL_RESTART, ContinuationCapability.NATIVE_QUIESCE,
        ContinuationCapability.NATIVE_PRIVATE_RESUME, ContinuationCapability.NATIVE_SOURCE_RETARGET}))
    plan = plan_continuation(sparse, candidate=bare_candidate(SIZE), executor_id="x", capabilities=native,
                             reason="user_candidate_switch", native_handoff=True)
    assert plan.strategy == ContinuationStrategy.NATIVE_STATE_HANDOFF
    assert plan.retained == SPARSE and plan.discarded == () and plan.authorized == ((0, SIZE),)
    assert type(plan).from_dict(plan.as_dict()) == plan
    # Without the declaration (or without core's handoff), the portable planner.
    private_only = ExecutorCapabilities(per_execution_pause=True, continuation=native.continuation - {
        ContinuationCapability.NATIVE_SOURCE_RETARGET})
    for capabilities, handoff in ((private_only, True), (native, False)):
        portable = plan_continuation(sparse, candidate=bare_candidate(SIZE), executor_id="x",
                                     capabilities=capabilities, reason="resume", native_handoff=handoff)
        assert portable.strategy == ContinuationStrategy.FULL_RESTART and portable.discarded == SPARSE


@pytest.mark.asyncio
async def test_a_fenced_attempt_can_neither_resume_nor_retarget_and_the_new_one_retargets_once(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
    transfer, first = await running_sparse(ctx)
    artifact, source_b = await attach(ctx, transfer, "src-b")
    await manual_candidate_failover(ctx.engine, transfer.id, artifact.id, str(source_b.id))
    new = (await artifact_of(ctx, transfer.id)).execution
    for action in ("resume", "pause", "retarget", "start", "cancel"):
        assert not await ctx.repository.authorize_execution(first.execution, action)
    assert not await ctx.repository.authorize_execution(new, "retarget")  # no longer 'prepared'
    assert await ctx.repository.authorize_execution(new, "pause")


async def executions_by_id(ctx, transfer_id):
    return {item.handle.attempt_id: item for item in await ctx.repository.executions(transfer_id)}


async def uncertain_handoff(ctx, *, cancel_uncertain=True):
    """A running switch whose retarget AND fencing cancellation are unproven."""
    transfer, first = await running_sparse(ctx)
    artifact, source_b = await attach(ctx, transfer, "src-b")
    ctx.native.retarget_result = "uncertain"
    ctx.native.cancel_uncertain = cancel_uncertain
    await manual_candidate_failover(ctx.engine, transfer.id, artifact.id, str(source_b.id))
    current = await artifact_of(ctx, transfer.id)
    return transfer, first, source_b, current


@pytest.mark.asyncio
async def test_a_lost_retarget_acknowledgement_with_source_b_proven_completes_the_handoff(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
    transfer, first = await running_sparse(ctx)
    artifact, source_b = await attach(ctx, transfer, "src-b")
    ctx.native.retarget_result = "ack_lost_b"
    result = await manual_candidate_failover(ctx.engine, transfer.id, artifact.id, str(source_b.id))
    assert result["ok"]
    new = (await artifact_of(ctx, transfer.id)).execution
    # Native truth, not the acknowledgement, proved source B: then, and only
    # then, the new writer gained authority and resumed.
    assert ("truth", new.attempt_id) in ctx.native.calls
    assert await ctx.repository.native_transition_from(new.attempt_id) is None
    job = ctx.native.job_for(new)
    assert job.address == "parkb:movie" and job.state == ExecutionState.RUNNING
    assert ("resume", new.attempt_id) in ctx.native.calls
    assert not [call for call in ctx.native.calls if call[0] == "cancel"]
    assert (await ctx.repository.material_state(artifact.id)).valid == SPARSE


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["ack_lost_a", "refused"])
async def test_a_retarget_that_provably_left_source_a_restores_it_through_a_new_attempt(tmp_path, monkeypatch,
                                                                                          outcome):
    ctx = await build(tmp_path, monkeypatch)
    transfer, first = await running_sparse(ctx)
    artifact, source_b = await attach(ctx, transfer, "src-b")
    ctx.native.retarget_result = outcome
    with pytest.raises(TransferError):  # the switch was not applied
        await manual_candidate_failover(ctx.engine, transfer.id, artifact.id, str(source_b.id))
    current = await artifact_of(ctx, transfer.id)
    attempts = await executions_by_id(ctx, transfer.id)
    unproven = next(item for item in attempts.values() if item.candidate.provider_id == "src-b")
    restored = current.execution
    # History is kept as it happened: A (source A, handed off), B (source B,
    # never proven, fenced), and a NEW attempt C for source A on the same job.
    assert [attempts[key].candidate.provider_id for key in attempts] == ["src-a", "src-b", "src-a"]
    assert restored.attempt_id not in {first.execution.attempt_id, unproven.handle.attempt_id}
    assert restored.native == first.execution.native and current.candidates[current.selected].provider_id == "src-a"
    assert await provenance_outcome(unproven.handle.attempt_id) == "handed_off"
    assert not await ctx.repository.authorize_execution(unproven.handle, "observe")
    assert await ctx.repository.native_transition_from(restored.attempt_id) is None
    # Zero loss, no cancel: the job continues on the source it never left.
    job = ctx.native.job_for(restored)
    assert job.address == "parka:movie" and job.state == ExecutionState.RUNNING
    assert not [call for call in ctx.native.calls if call[0] == "cancel"] and len(starts(ctx)) == 1
    state = await ctx.repository.material_state(artifact.id)
    assert state.valid == SPARSE and state.writer_generation == 3
    assert (await audit(ctx, transfer.id, "native_retarget"))[-1]["native_state"] == "restored"
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT detail FROM application_events WHERE kind='manual_candidate_failover'"
                                 " AND transfer_id=? ORDER BY id", (transfer.id,))
    assert [codec.load(row["detail"])["outcome"] for row in rows] == ["failure"]


@pytest.mark.asyncio
async def test_an_uncertain_retarget_with_an_uncertain_cancel_stays_fenced_and_unresolved(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
    transfer, first, _source_b, current = await uncertain_handoff(ctx)
    new = current.execution
    # The new attempt alone owns the job, holds no acquisition authority, and
    # nothing else is admitted beside it.
    assert new.attempt_id != first.execution.attempt_id
    assert await ctx.repository.native_transition_from(new.attempt_id) == first.execution.attempt_id
    assert not await ctx.repository.authorize_execution(new, "resume")
    assert not await ctx.repository.authorize_execution(new, "start")
    for _ in range(3):
        await ctx.engine.tick()
    assert len(await ctx.repository.executions(transfer.id)) == 2 and len(starts(ctx)) == 1
    assert ctx.native.job_for(new).state == ExecutionState.PAUSED
    assert not [call for call in ctx.native.calls if call == ("resume", new.attempt_id)]
    assert (await audit(ctx, transfer.id, "native_retarget"))[-1]["native_state"] == "uncertain"


@pytest.mark.asyncio
async def test_resume_while_the_transition_is_unresolved_cannot_cause_acquisition(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
    transfer, _first, _source_b, current = await uncertain_handoff(ctx)
    new = current.execution
    job = ctx.native.job_for(new)
    await ctx.engine.pause(transfer.id)
    await ctx.engine.resume(transfer.id)
    for _ in range(2):
        await ctx.engine.reconcile_executions()
    assert job.state == ExecutionState.PAUSED
    assert not [call for call in ctx.native.calls if call == ("resume", new.attempt_id)]
    # Even a job found acquiring on its own is quiesced, never left running.
    job.state = ExecutionState.RUNNING
    await ctx.engine.reconcile_executions()
    assert job.state == ExecutionState.PAUSED
    assert await ctx.repository.native_transition_from(new.attempt_id) is not None


@pytest.mark.asyncio
async def test_eventual_reconciliation_to_source_b_restores_progress(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
    transfer, _first, _source_b, current = await uncertain_handoff(ctx)
    new = current.execution
    job = ctx.native.job_for(new)
    job.address = "parkb:movie"  # native truth becomes provably source B
    for _ in range(2):
        await ctx.engine.reconcile_executions()
    assert await ctx.repository.native_transition_from(new.attempt_id) is None
    assert ("resume", new.attempt_id) in ctx.native.calls and job.state == ExecutionState.RUNNING
    assert (await artifact_of(ctx, transfer.id)).execution == new
    state = await ctx.repository.material_state(current.id)
    assert state.valid == SPARSE and state.writer_generation == 2
    ctx.native.finish(new)
    await ctx.engine.reconcile_executions()
    assert (await artifact_of(ctx, transfer.id)).state == "completed"


@pytest.mark.asyncio
async def test_a_conservative_portable_fallback_keeps_truthful_source_provenance(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
    transfer, first = await running_sparse(ctx)
    artifact, source_b = await attach(ctx, transfer, "src-b")
    ctx.native.retarget_result = "uncertain"
    await manual_candidate_failover(ctx.engine, transfer.id, artifact.id, str(source_b.id), discard_confirmed=True)
    await ctx.engine.tick()
    attempts = await executions_by_id(ctx, transfer.id)
    first_id, unproven_id, fresh_id = list(attempts)
    # Each attempt stays bound to the source it actually was: A, the unproven
    # B (cancelled through its own authority), then a fresh portable writer
    # for B that continues only from the DP prefix.
    assert [attempts[key].candidate.provider_id for key in (first_id, unproven_id, fresh_id)] == [
        "src-a", "src-b", "src-b"]
    assert ("cancel", unproven_id) in ctx.native.calls and attempts[unproven_id].state == "cancelled"
    assert await provenance_outcome(first_id) == "handed_off" and await provenance_outcome(unproven_id) == "cancelled"
    assert await ctx.repository.native_transition_from(unproven_id) is None
    plan = ctx.native.plans[-1]
    assert plan.strategy == ContinuationStrategy.CONTIGUOUS_FROM_OFFSET and plan.discarded == ((3 * MIB, 4 * MIB),)
    event = (await audit(ctx, transfer.id, "native_retarget"))[-1]
    assert event["native_state"] == "abandoned" and event["truth"] == "unknown"


@pytest.mark.asyncio
async def test_an_unconfirmed_conservative_fallback_waits_for_confirmation(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
    transfer, _first, _source_b, _current = await uncertain_handoff(ctx, cancel_uncertain=False)
    await ctx.engine.tick()
    held = await artifact_of(ctx, transfer.id)
    assert len(starts(ctx)) == 1 and held.execution is None
    assert held.state == "error" and held.error.operator_action_required
    assert (await ctx.repository.material_state(held.id)).valid == SPARSE


@pytest.mark.asyncio
async def test_a_pause_landing_during_a_handoff_stays_authoritative(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
    transfer, first = await running_sparse(ctx)
    artifact, source_b = await attach(ctx, transfer, "src-b")
    asked = []

    async def pause_lands():
        asked.append(1)
        if len(asked) > 1:
            await ctx.repository.set_pause_and_fence(transfer.id, True)
    ctx.native.before_retarget_prepare = pause_lands

    with pytest.raises(TransferError):
        await manual_candidate_failover(ctx.engine, transfer.id, artifact.id, str(source_b.id))
    current = await artifact_of(ctx, transfer.id)
    assert current.execution == first.execution and current.candidates[current.selected].provider_id == "src-a"
    assert len(await ctx.repository.executions(transfer.id)) == 1
    await ctx.engine.pause(transfer.id)
    await ctx.engine.reconcile_executions()
    assert ctx.native.job_for(first.execution).state == ExecutionState.PAUSED
    assert (await ctx.repository.get(transfer.id)).paused
    assert not [call for call in ctx.native.calls if call[0] in {"retarget", "resume", "cancel"}]


def test_native_retarget_and_private_resume_must_be_implemented_to_be_declared():
    from continuation_fakes import NATIVE

    def declaring(continuation, *, pause=True):
        executor = SpoolExecutor(None, {}, identity="declaring", scheme="declaring")
        executor.capabilities = ExecutorCapabilities(per_execution_pause=pause, continuation=frozenset(continuation))
        return executor

    # A retarget without the operation (SpoolExecutor has no prepare_retarget).
    with pytest.raises(TypeError):
        IntegrationRegistry().register_executor(declaring(NATIVE))
    # Private resume IS the per-execution resume operation.
    with pytest.raises(TypeError):
        IntegrationRegistry().register_executor(declaring(
            {ContinuationCapability.FULL_RESTART, ContinuationCapability.NATIVE_PRIVATE_RESUME}, pause=False))
    # A retarget is a handover of a quiesced, privately resumable job.
    implemented = NativeSpoolExecutor(None, {}, continuation=NATIVE - {ContinuationCapability.NATIVE_PRIVATE_RESUME})
    with pytest.raises(TypeError):
        IntegrationRegistry().register_executor(implemented)
    IntegrationRegistry().register_executor(NativeSpoolExecutor(None, {}))
