"""DP-owned material state, writer fencing and the Continuation Planner
(1.0.13 protocol-agnostic continuation, phases 1-4)."""
from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import db.database as database
from continuation_fakes import MIB, SpoolExecutor, SpoolProvider, payload
from transfers import material as mat
from transfers.continuation import plan_continuation
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, TransferError
from transfers.filesystem import PayloadFacts, flush_payload
from transfers.models import (
    ContinuationCapability, ContinuationStrategy, ExecutorCapabilities, MaterializationKind, TransferCandidate,
    TransferRequest,
)
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

SIZE = 5 * MIB + 12345


def state(valid=(), *, expected=SIZE, generation=1):
    return mat.MaterialState(7, generation, mat.GEOMETRY_VERSION, mat.normalize(valid), "/d/x", 3, expected)


def candidate(size=SIZE, kind=MaterializationKind.FILE):
    return TransferCandidate("x", (), expected_bytes=size, materialization=kind)


def caps(*extra, alignment=1):
    return ExecutorCapabilities(continuation=frozenset({ContinuationCapability.FULL_RESTART, *extra}),
                                continuation_alignment=alignment)


CONTIGUOUS = (ContinuationCapability.CONTIGUOUS_FROM_OFFSET, ContinuationCapability.IMPORT_EXISTING_MATERIAL)


# -- pure representation --------------------------------------------------

def test_range_algebra_is_normalized_half_open_and_prefix_aware():
    assert mat.normalize([(5, 9), (0, 3), (3, 4), (8, 12), (20, 20)]) == ((0, 4), (5, 12))
    assert mat.subtract([(0, 10)], [(2, 4), (6, 20)]) == ((0, 2), (4, 6))
    assert mat.intersect([(0, 10), (20, 30)], [(5, 25)]) == ((5, 10), (20, 25))
    assert mat.contiguous_prefix([(0, 4), (5, 12)]) == 4
    assert mat.contiguous_prefix([(1, 4)]) == 0
    assert mat.align_inward([(10, 3 * MIB + 7)]) == ((MIB, 3 * MIB),)
    # a range may keep its true end only at the known end of file
    assert mat.align_inward([(0, SIZE)], end_of_file=SIZE) == ((0, SIZE),)
    assert mat.decode(mat.encode([(0, 2), (1, 5)])) == ((0, 5),)
    with pytest.raises(ValueError):
        mat.normalize([(5, 1)])


def test_unknown_expected_size_tracks_bytes_but_never_fabricates_a_percentage():
    unknown = state([(0, 2 * MIB)], expected=None)
    assert unknown.valid_bytes == 2 * MIB and unknown.safe_prefix == 2 * MIB
    assert unknown.percentage is None and not unknown.complete
    known = state([(0, 2 * MIB)])
    assert known.percentage == pytest.approx(2 * MIB / SIZE * 100)


def test_in_flight_is_derived_from_the_writer_authorization_and_never_stored():
    current = state([(0, MIB)])
    authorized = ((MIB, SIZE),)
    assert current.in_flight(authorized) == ((MIB, SIZE),)
    assert current.classify(0) == mat.MaterialClass.VALID
    assert current.classify(MIB + 1, authorized) == mat.MaterialClass.IN_FLIGHT
    assert current.classify(MIB + 1) == mat.MaterialClass.UNKNOWN


# -- the one planner ------------------------------------------------------

def test_planner_continues_the_safe_prefix_for_the_same_or_a_different_executor():
    current = state([(0, 3 * MIB)])
    same = plan_continuation(current, candidate=candidate(), executor_id="spool-a", capabilities=caps(*CONTIGUOUS),
                             reason="resume")
    other = plan_continuation(current, candidate=candidate(), executor_id="spool-b", capabilities=caps(*CONTIGUOUS),
                              reason="user_candidate_switch")
    for plan in (same, other):
        # a changed source/protocol/executor alone never forces a restart
        assert plan.strategy == ContinuationStrategy.CONTIGUOUS_FROM_OFFSET
        assert plan.boundary == 3 * MIB and plan.retained == ((0, 3 * MIB),) and plan.discarded == ()
        assert plan.authorized == ((3 * MIB, SIZE),) and plan.material_generation == 1
    assert other.executor_id == "spool-b" and other.reason == "user_candidate_switch"
    assert "contiguous_from_offset" in other.capabilities and other.alignment == 1
    assert type(other).from_dict(other.as_dict()) == other  # the durable provenance form round-trips


def test_planner_rolls_back_only_the_incompatible_alignment_tail():
    plan = plan_continuation(state([(0, 3 * MIB)]), candidate=candidate(), executor_id="coarse",
                             capabilities=caps(*CONTIGUOUS, alignment=2 * MIB), reason="auto_retry")
    assert plan.boundary == 2 * MIB and plan.retained == ((0, 2 * MIB),)
    assert plan.discarded == ((2 * MIB, 3 * MIB),)


def test_zero_retention_full_restart_is_a_legal_plan():
    plan = plan_continuation(state([(0, 3 * MIB)]), candidate=candidate(), executor_id="restart-only",
                             capabilities=caps(), reason="executor_recovery")
    assert plan.strategy == ContinuationStrategy.FULL_RESTART and plan.boundary == 0
    assert plan.retained == () and plan.discarded == ((0, 3 * MIB),) and plan.authorized == ((0, SIZE),)


def test_incompatible_equivalent_size_is_rejected_rather_than_guessed():
    with pytest.raises(TransferError) as raised:
        plan_continuation(state([(0, MIB)]), candidate=candidate(SIZE * 3), executor_id="spool-a",
                          capabilities=caps(*CONTIGUOUS), reason="auto_retry")
    assert raised.value.error.category == Category.SIZE_MISMATCH


def test_sparse_valid_ranges_exist_but_a_prefix_plan_never_counts_them():
    current = state([(0, MIB), (3 * MIB, 4 * MIB)])
    assert current.valid_bytes == 2 * MIB and current.safe_prefix == MIB
    plan = plan_continuation(current, candidate=candidate(), executor_id="spool-a", capabilities=caps(*CONTIGUOUS),
                             reason="resume")
    assert plan.retained == ((0, MIB),) and plan.discarded == ((3 * MIB, 4 * MIB),)


def test_unknown_size_leaves_the_authorization_open_and_collections_restart():
    open_plan = plan_continuation(state([(0, MIB)], expected=None), candidate=candidate(0), executor_id="spool-a",
                                  capabilities=caps(*CONTIGUOUS), reason="resume")
    assert open_plan.authorized == ((MIB, mat.OPEN_END),) and open_plan.expected_size is None
    collection = plan_continuation(state(), candidate=candidate(0, MaterializationKind.COLLECTION),
                                   executor_id="spool-a", capabilities=caps(*CONTIGUOUS), reason="admission")
    assert collection.strategy == ContinuationStrategy.FULL_RESTART


# -- the durable owner, driven through the real engine --------------------

async def build(tmp_path, monkeypatch, *, sources=None, executors=(("spool-a", "spoola"),), now=1000.0, db="state.db",
                alignments=None, continuations=None):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / db)
    await database.init_db()
    sources = sources if sources is not None else {"movie": payload(SIZE)}
    repository = TransferRepository()
    registry = IntegrationRegistry()
    spools = {}
    for identity, scheme in executors:
        registry.register_provider(SpoolProvider("src-" + identity, scheme, sources))
        extra = {"continuation": continuations[identity]} if continuations and identity in continuations else {}
        spools[identity] = SpoolExecutor(repository.authorize_execution, sources, identity=identity, scheme=scheme,
                                         alignment=(alignments or {}).get(identity, 1), **extra)
        registry.register_executor(spools[identity])
    clock = [now]
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"),
                            policy=TransferPolicy(retry_delay=0, adoption_stability_seconds=0,
                                                  max_active_executions=4),
                            clock=lambda: clock[0])
    await engine.initialize()
    return SimpleNamespace(engine=engine, repository=repository, registry=registry, spools=spools,
                           clock=clock, sources=sources, tmp_path=tmp_path)


async def admit(ctx, source="movie", provider="src-spool-a"):
    transfer = await ctx.engine.submit((TransferRequest("spool", source, name=source + ".bin",
                                                        preferred_provider=provider),), deduplicate=False)
    await ctx.engine.tick()
    artifact = (await ctx.repository.artifacts(transfer.id))[0]
    assert artifact.execution is not None
    return transfer, artifact


async def checkpoint(ctx, seconds=6):
    ctx.clock[0] += seconds
    await ctx.engine.reconcile_executions()


@pytest.mark.asyncio
async def test_checkpointed_material_is_valid_per_artifact_and_survives_restart(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
    transfer, artifact = await admit(ctx)
    spool = ctx.spools["spool-a"]
    spool.step(artifact.execution.attempt_id, 2 * MIB + 100)
    await checkpoint(ctx)
    current = await ctx.repository.material_state(artifact.id)
    # geometry v1: only whole 1 MiB chunks become VALID before end of file
    assert current.valid == ((0, 2 * MIB),) and current.material_generation == 1
    assert current.writer_generation == 1 and current.expected_size == SIZE

    # uncheckpointed in-flight work: written, but not yet committed
    spool.step(artifact.execution.attempt_id, 2 * MIB)
    ctx.clock[0] += 1
    await ctx.engine.reconcile_executions()
    assert (await ctx.repository.material_state(artifact.id)).valid == ((0, 2 * MIB),)

    # crash: a new process sees only committed VALID; the file length
    # (4 MiB + 100 physically written) promotes nothing
    assert os.path.getsize(artifact.target) >= 4 * MIB
    restarted = TransferRepository()
    after = await restarted.material_state(artifact.id)
    assert after.valid == ((0, 2 * MIB),)


@pytest.mark.asyncio
async def test_stale_writer_wrong_generation_and_unapproved_ranges_cannot_commit(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
    _transfer, artifact = await admit(ctx)
    handle = artifact.execution
    spool = ctx.spools["spool-a"]
    spool.step(handle.attempt_id, 3 * MIB)
    facts = flush_payload(artifact.target)

    # ranges past the authorization / physical length are clipped away
    added = await ctx.repository.commit_material(handle, ((0, 3 * MIB), (SIZE, SIZE + MIB)), facts, now=1.0)
    assert added == ((0, 3 * MIB),)
    # a wrong/unknown handle cannot commit
    forged = replace(handle, attempt_id="not-a-writer")
    assert await ctx.repository.commit_material(forged, ((0, SIZE),), facts, now=2.0) is None
    # a material-generation change makes the plan stale for its writer
    await ctx.repository.invalidate_material(artifact.id, "test_rewrite")
    assert await ctx.repository.commit_material(handle, ((0, 4 * MIB),), facts, now=3.0) is None
    current = await ctx.repository.material_state(artifact.id)
    assert current.valid == () and current.material_generation == 2


@pytest.mark.asyncio
async def test_reconciliation_invalidates_truncation_but_never_promotes_extra_bytes(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
    _transfer, artifact = await admit(ctx)
    ctx.spools["spool-a"].step(artifact.execution.attempt_id, 4 * MIB)
    await checkpoint(ctx)
    before = await ctx.repository.material_state(artifact.id)
    assert before.valid == ((0, 4 * MIB),)

    # extra physical bytes (larger file) add nothing
    with open(artifact.target, "r+b") as handle:
        handle.truncate(SIZE)
    grown = await ctx.repository.reconcile_material(artifact.id, artifact.target,
                                                    PayloadFacts(True, True, SIZE, before.destination_identity))
    assert grown.valid == ((0, 4 * MIB),) and grown.material_generation == 1

    # external truncation removes affected validity and advances generation
    os.truncate(artifact.target, 2 * MIB + 5)
    shrunk = await ctx.repository.reconcile_material(
        artifact.id, artifact.target, PayloadFacts(True, True, 2 * MIB + 5, before.destination_identity))
    assert shrunk.valid == ((0, 2 * MIB),) and shrunk.material_generation == 2

    # a missing payload invalidates everything
    missing = await ctx.repository.reconcile_material(artifact.id, artifact.target, PayloadFacts(True, False))
    assert missing.valid == () and missing.material_generation == 3


@pytest.mark.asyncio
async def test_open_material_state_migration_trusts_no_preexisting_partial(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
    ctx.engine.dispatch_permitted = False
    transfer = await ctx.engine.submit((TransferRequest("spool", "movie", name="movie.bin"),), deduplicate=False)
    await ctx.engine.tick()
    artifact = (await ctx.repository.artifacts(transfer.id))[0]
    os.makedirs(os.path.dirname(artifact.target), exist_ok=True)
    with open(artifact.target, "wb") as handle:
        handle.write(ctx.sources["movie"][:4 * MIB])
    first = await ctx.repository.open_material_state(artifact)
    again = await ctx.repository.open_material_state(artifact)
    assert first == again and first.valid == () and first.material_generation == 1


# -- one replacement path: pause / switch / resume / failover -------------

TWO = (("spool-a", "spoola"), ("spool-b", "spoolb"))


async def attach_alternate(ctx, canonical):
    await ctx.engine.submit((TransferRequest("spool", "movie", name="movie.bin",
                                             preferred_provider="src-spool-b"),), deduplicate=False)
    await ctx.engine.resolve_pending()
    artifact = (await ctx.repository.artifacts(canonical.id))[0]
    assert [item.provider_id for item in artifact.candidates] == ["src-spool-a", "src-spool-b"]
    return artifact


async def material_events(ctx, transfer_id):
    from db.database import get_db
    from transfers import codec
    async with get_db() as db:
        rows = await db.fetchall("SELECT detail FROM application_events WHERE kind='material_audit' AND transfer_id=?"
                                 " ORDER BY id", (transfer_id,))
    return [codec.load(row["detail"]) for row in rows]


@pytest.mark.asyncio
async def test_pause_switch_resume_continues_on_another_executor_from_dp_material(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch, executors=TWO)
    transfer, first = await admit(ctx)
    await attach_alternate(ctx, transfer)
    spool_a, spool_b = ctx.spools["spool-a"], ctx.spools["spool-b"]
    spool_a.step(first.execution.attempt_id, 3 * MIB + 123)

    # Pause: graceful native quiesce, forced checkpoint, then fence.
    assert await ctx.engine.pause(transfer.id) == ()
    paused = (await ctx.repository.artifacts(transfer.id))[0]
    assert paused.execution is None and ("pause", first.execution.attempt_id) in spool_a.calls
    assert (await ctx.repository.material_state(paused.id)).valid == ((0, 3 * MIB),)
    retired = [event for event in await material_events(ctx, transfer.id) if event["event"] == "writer_retired"]
    assert retired[-1]["quiesce"] == "graceful" and retired[-1]["boundary"] == "pause"

    # Operator switches source while paused; nothing starts.
    result = await ctx.engine.activate_candidate_command(transfer.id, paused.id, 1)
    assert result is not None and result.committed
    await ctx.engine.reconcile_executions()
    assert (await ctx.repository.artifacts(transfer.id))[0].execution is None
    assert spool_b.plans == []

    # Resume plans against the NEW candidate and executor, from DP material only.
    await ctx.engine.resume(transfer.id)
    resumed = (await ctx.repository.artifacts(transfer.id))[0]
    assert resumed.execution.executor_id == "spool-b"
    plan = spool_b.plans[-1]
    assert plan.strategy == ContinuationStrategy.CONTIGUOUS_FROM_OFFSET and plan.boundary == 3 * MIB
    assert plan.material_generation == 1 and plan.reason == "resume" and plan.discarded == ()
    state_now = await ctx.repository.material_state(resumed.id)
    assert state_now.writer_generation == 2 and state_now.material_generation == 1
    # spool-a's private journal was never translated -- it is simply gone
    assert not os.path.exists(spool_a.journal(resumed.target))

    spool_b.step(resumed.execution.attempt_id, SIZE)
    await checkpoint(ctx)
    await ctx.engine.reconcile_executions()
    done = (await ctx.repository.artifacts(transfer.id))[0]
    assert done.state == "completed"
    with open(done.target, "rb") as handle:
        assert handle.read() == ctx.sources["movie"]
    assert (await ctx.repository.material_state(done.id)).valid == ((0, SIZE),)


@pytest.mark.asyncio
async def test_quiesce_timeout_force_fences_and_leaves_uncheckpointed_work_unknown(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
    ctx.engine.configure_policy(replace(ctx.engine.policy, graceful_stop_timeout=1))
    transfer, artifact = await admit(ctx)
    spool = ctx.spools["spool-a"]
    spool.step(artifact.execution.attempt_id, 2 * MIB)
    spool.quiesce_hangs = True
    await ctx.engine.pause(transfer.id)
    assert (await ctx.repository.artifacts(transfer.id))[0].execution is None
    assert spool.jobs[artifact.execution.attempt_id].state.value == "cancelled"
    assert (await ctx.repository.material_state(artifact.id)).valid == ()
    retired = [event for event in await material_events(ctx, transfer.id) if event["event"] == "writer_retired"]
    assert retired[-1]["quiesce"] == "timeout"


@pytest.mark.asyncio
async def test_failover_retry_continues_same_executor_from_committed_prefix(tmp_path, monkeypatch):
    from transfers.errors import Domain, NormalizedError, Origin, Retryability, Stage
    from transfers.models import ExecutionState
    ctx = await build(tmp_path, monkeypatch)
    transfer, artifact = await admit(ctx)
    spool = ctx.spools["spool-a"]
    spool.step(artifact.execution.attempt_id, 4 * MIB + 7)
    await checkpoint(ctx)
    job = spool.jobs[artifact.execution.attempt_id]
    job.state = ExecutionState.FAILED
    original = spool._observation

    def failing(handle, current):
        observed = original(handle, current)
        if current.state == ExecutionState.FAILED:
            observed = replace(observed, error=NormalizedError(
                Domain.NETWORK, Category.REMOTE_READ_FAILED, Stage.EXECUTION, retryability=Retryability.BACKOFF,
                origin=Origin.REMOTE_SOURCE, integration_id="spool-a"))
        return observed
    spool._observation = failing
    for _ in range(4):
        await checkpoint(ctx, seconds=120)
        current = (await ctx.repository.artifacts(transfer.id))[0]
        if current.execution is not None and current.execution.attempt_id != artifact.execution.attempt_id:
            break
    assert current.execution.attempt_id != artifact.execution.attempt_id
    plan = spool.plans[-1]
    assert plan.strategy == ContinuationStrategy.CONTIGUOUS_FROM_OFFSET and plan.boundary == 4 * MIB
    assert plan.reason in {"auto_retry", "admission"}
    assert (await ctx.repository.material_state(artifact.id)).material_generation == 1


# -- progress and provenance project DP material ---------------------------

@pytest.mark.asyncio
async def test_displayed_completion_is_dp_valid_material_not_executor_percentage(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch, executors=TWO)
    transfer, artifact = await admit(ctx)
    await attach_alternate(ctx, transfer)
    ctx.spools["spool-a"].step(artifact.execution.attempt_id, 3 * MIB + 123)
    await checkpoint(ctx)
    ctx.spools["spool-a"].step(artifact.execution.attempt_id, MIB)  # in flight, uncommitted
    ctx.clock[0] += 1
    await ctx.engine.reconcile_executions()
    shown = await ctx.repository.presentation(transfer.id, details=True)
    executor_view = (4 * MIB + 123) / SIZE * 100
    assert shown["progress"] == pytest.approx(3 * MIB / SIZE * 100) and shown["progress"] < executor_view
    assert shown["retained_bytes"] == 3 * MIB and shown["files"][0]["retained_bytes"] == 3 * MIB
    assert (await ctx.repository.get(transfer.id)).progress == pytest.approx(3 * MIB / SIZE * 100)

    # A handoff that keeps all material does not move the figure.
    await ctx.engine.pause(transfer.id)
    await ctx.engine.activate_candidate_command(transfer.id, artifact.id, 1)
    await ctx.engine.resume(transfer.id)
    after = await ctx.repository.presentation(transfer.id, details=True)
    assert after["progress"] == pytest.approx(4 * MIB / SIZE * 100)  # the graceful pause committed 4 MiB


@pytest.mark.asyncio
async def test_real_rollback_lowers_progress_by_exactly_the_discarded_material_and_is_explained(tmp_path, monkeypatch):
    from services import transfer_trace
    ctx = await build(tmp_path, monkeypatch, executors=TWO, alignments={"spool-b": 2 * MIB})
    transfer, artifact = await admit(ctx)
    await attach_alternate(ctx, transfer)
    ctx.spools["spool-a"].step(artifact.execution.attempt_id, 3 * MIB + 123)
    await ctx.engine.pause(transfer.id)
    before = (await ctx.repository.presentation(transfer.id))["progress"]
    assert before == pytest.approx(3 * MIB / SIZE * 100)

    await ctx.engine.activate_candidate_command(transfer.id, artifact.id, 1)
    await ctx.engine.resume(transfer.id)
    plan = ctx.spools["spool-b"].plans[-1]
    assert plan.boundary == 2 * MIB and plan.discarded == ((2 * MIB, 3 * MIB),)
    after = (await ctx.repository.presentation(transfer.id))["progress"]
    assert before - after == pytest.approx(MIB / SIZE * 100)

    events = await material_events(ctx, transfer.id)
    rollback = [event for event in events if event["event"] == "rollback"][-1]
    assert rollback["discarded_bytes"] == MIB and rollback["reason"] == "resume"
    assert rollback["strategy"] == "contiguous_from_offset"

    trace = await transfer_trace.build(transfer.id, SimpleNamespace(engine=ctx.engine, repository=ctx.repository))
    assert trace["metadata"]["trace_format_version"] == 4
    rows = trace["data"]["artifact_material_state"]
    assert rows and rows[0]["row"]["writer_generation"] == 2
    target = next(item for item in trace["observations"]["filesystem"]["targets"] if item["artifact_id"] == artifact.id)
    assert target["material"]["safe_prefix"] == 2 * MIB and target["material"]["valid_beyond_observed_length"] is False
    plans = [row["row"]["continuation"] for row in trace["data"]["execution_attempts"] if row["row"].get("continuation")]
    assert any('"strategy":"contiguous_from_offset"' in plan and '"discarded":[[2097152,3145728]]' in plan
               for plan in plans)
    text = json.dumps(trace)
    assert "spool:" not in text or "<redacted" in text


@pytest.mark.asyncio
async def test_operator_switch_that_discards_valid_progress_requires_confirmation(tmp_path, monkeypatch):
    from transfers.manual_failover import DiscardConfirmationRequired, manual_candidate_failover
    restart_only = frozenset({ContinuationCapability.FULL_RESTART, ContinuationCapability.EXPORT_MATERIAL_RANGES})
    ctx = await build(tmp_path, monkeypatch, executors=TWO, continuations={"spool-b": restart_only})
    transfer, artifact = await admit(ctx)
    current = await attach_alternate(ctx, transfer)
    ctx.spools["spool-a"].step(artifact.execution.attempt_id, 3 * MIB)
    await checkpoint(ctx)
    target = str(current.candidates[1].id)

    with pytest.raises(DiscardConfirmationRequired) as refused:
        await manual_candidate_failover(ctx.engine, transfer.id, artifact.id, target)
    assert refused.value.discarded_bytes == 3 * MIB and refused.value.retained_bytes == 0
    unchanged = (await ctx.repository.artifacts(transfer.id))[0]
    assert unchanged.selected == 0 and unchanged.execution == artifact.execution
    assert (await ctx.repository.material_state(artifact.id)).valid == ((0, 3 * MIB),)

    result = await manual_candidate_failover(ctx.engine, transfer.id, artifact.id, target, discard_confirmed=True)
    assert result["ok"]
    await ctx.engine.reconcile_executions()
    plan = ctx.spools["spool-b"].plans[-1]
    assert plan.strategy == ContinuationStrategy.FULL_RESTART and plan.discarded == ((0, 3 * MIB),)
    # Logical invalidation first; the replacement writer owns the restart.
    assert (await ctx.repository.material_state(artifact.id)).valid == ()


@pytest.mark.asyncio
async def test_switch_preview_is_the_refusals_consequence_and_mutates_nothing(tmp_path, monkeypatch):
    from transfers.manual_failover import DiscardConfirmationRequired, manual_candidate_failover, preview_candidate_switch
    restart_only = frozenset({ContinuationCapability.FULL_RESTART, ContinuationCapability.EXPORT_MATERIAL_RANGES})
    ctx = await build(tmp_path, monkeypatch, executors=TWO, continuations={"spool-b": restart_only})
    transfer, artifact = await admit(ctx)
    current = await attach_alternate(ctx, transfer)
    ctx.spools["spool-a"].step(artifact.execution.attempt_id, 3 * MIB)
    await checkpoint(ctx)
    target = str(current.candidates[1].id)
    before = await ctx.repository.material_state(artifact.id)

    preview = await preview_candidate_switch(ctx.engine, transfer.id, artifact.id, target)
    assert preview["candidate_id"] == target and preview["artifact_id"] == artifact.id
    assert preview["discarded_bytes"] == 3 * MIB and preview["retained_bytes"] == 0
    assert preview["material_generation"] == before.material_generation
    unchanged = (await ctx.repository.artifacts(transfer.id))[0]
    assert unchanged.selected == 0 and unchanged.execution == artifact.execution
    assert await ctx.repository.material_state(artifact.id) == before

    with pytest.raises(DiscardConfirmationRequired) as refused:
        await manual_candidate_failover(ctx.engine, transfer.id, artifact.id, target)
    assert (refused.value.discarded_bytes, refused.value.retained_bytes, refused.value.material_generation) == (
        preview["discarded_bytes"], preview["retained_bytes"], preview["material_generation"])
    assert refused.value.changed is False


@pytest.mark.asyncio
async def test_a_confirmation_applies_only_to_the_consequence_it_confirmed(tmp_path, monkeypatch):
    from transfers.manual_failover import DiscardConfirmationRequired, manual_candidate_failover, preview_candidate_switch
    restart_only = frozenset({ContinuationCapability.FULL_RESTART, ContinuationCapability.EXPORT_MATERIAL_RANGES})
    ctx = await build(tmp_path, monkeypatch, executors=TWO, continuations={"spool-b": restart_only})
    transfer, artifact = await admit(ctx)
    current = await attach_alternate(ctx, transfer)
    ctx.spools["spool-a"].step(artifact.execution.attempt_id, 3 * MIB)
    await checkpoint(ctx)
    target = str(current.candidates[1].id)
    preview = await preview_candidate_switch(ctx.engine, transfer.id, artifact.id, target)

    # Another material generation, or a switch that would now keep less than
    # confirmed: refused as changed, and nothing moves.
    for stale in ({"material_generation": preview["material_generation"] + 1, "retained_bytes": 0},
                  {"material_generation": preview["material_generation"], "retained_bytes": MIB}):
        with pytest.raises(DiscardConfirmationRequired) as refused:
            await manual_candidate_failover(ctx.engine, transfer.id, artifact.id, target,
                                            discard_confirmed=True, discard_confirmation=stale)
        assert refused.value.changed is True and refused.value.discarded_bytes == 3 * MIB
        unchanged = (await ctx.repository.artifacts(transfer.id))[0]
        assert unchanged.selected == 0 and unchanged.execution == artifact.execution

    # Material written after the confirmation (same generation, the kept part
    # not smaller) is still the confirmed consequence.
    ctx.spools["spool-a"].step(artifact.execution.attempt_id, MIB)
    await checkpoint(ctx)
    result = await manual_candidate_failover(
        ctx.engine, transfer.id, artifact.id, target, discard_confirmed=True,
        discard_confirmation={"material_generation": preview["material_generation"],
                              "retained_bytes": preview["retained_bytes"]})
    assert result["ok"]
    assert (await ctx.repository.artifacts(transfer.id))[0].selected == 1


@pytest.mark.asyncio
async def test_candidate_switch_http_contract_refuses_previews_and_reports_changed(tmp_path, monkeypatch):
    from fastapi import HTTPException

    import api.operational_downloads as downloads
    from transfers.manual_failover import preview_candidate_switch
    restart_only = frozenset({ContinuationCapability.FULL_RESTART, ContinuationCapability.EXPORT_MATERIAL_RANGES})
    ctx = await build(tmp_path, monkeypatch, executors=TWO, continuations={"spool-b": restart_only})
    transfer, artifact = await admit(ctx)
    current = await attach_alternate(ctx, transfer)
    ctx.spools["spool-a"].step(artifact.execution.attempt_id, 3 * MIB)
    await checkpoint(ctx)
    target = str(current.candidates[1].id)

    class Application:
        engine = ctx.engine

        async def require(self, transfer_id):
            return None

    async def preview_switch(application, transfer_id, artifact_id, candidate_id):
        return await preview_candidate_switch(ctx.engine, transfer_id, artifact_id, candidate_id)

    monkeypatch.setattr(downloads, "preview_switch", preview_switch)
    preview = await downloads.preview_artifact_candidate(transfer.id, artifact.id, target, Application())
    assert preview["discarded_bytes"] == 3 * MIB

    stale = downloads.DiscardConfirmationBody(material_generation=preview["material_generation"] + 1, retained_bytes=0)

    async def switch_candidate(application, transfer_id, artifact_id, candidate_id, **kwargs):
        from transfers.manual_failover import manual_candidate_failover
        return await manual_candidate_failover(ctx.engine, transfer_id, artifact_id, candidate_id, **kwargs)

    monkeypatch.setattr(downloads, "switch_candidate", switch_candidate)
    with pytest.raises(HTTPException) as first:
        await downloads.activate_artifact_candidate(transfer.id, artifact.id, target, False, None, Application())
    assert first.value.status_code == 409
    assert first.value.detail["confirmation"] == "discard_material" and first.value.detail["changed"] is False
    assert first.value.detail["material_generation"] == preview["material_generation"]
    with pytest.raises(HTTPException) as changed:
        await downloads.activate_artifact_candidate(transfer.id, artifact.id, target, True, stale, Application())
    assert changed.value.status_code == 409 and changed.value.detail["changed"] is True
    assert (await ctx.repository.artifacts(transfer.id))[0].selected == 0


@pytest.mark.asyncio
async def test_external_truncation_is_reconciled_before_the_continuation_plan(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
    transfer, artifact = await admit(ctx)
    ctx.spools["spool-a"].step(artifact.execution.attempt_id, 4 * MIB + 9)
    await ctx.engine.pause(transfer.id)
    assert (await ctx.repository.material_state(artifact.id)).valid == ((0, 4 * MIB),)
    os.truncate(artifact.target, 2 * MIB + 77)  # shortened behind DP's back
    await ctx.engine.resume(transfer.id)
    plan = ctx.spools["spool-a"].plans[-1]
    assert plan.boundary == 2 * MIB and plan.material_generation == 2
    current = await ctx.repository.material_state(artifact.id)
    assert current.valid == ((0, 2 * MIB),) and current.material_generation == 2
    events = [event["event"] for event in await material_events(ctx, transfer.id)]
    assert "invalidated" in events


@pytest.mark.asyncio
async def test_delete_during_a_paused_handoff_leaves_no_writer_authority_or_owned_partial(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch, executors=TWO)
    transfer, artifact = await admit(ctx)
    await attach_alternate(ctx, transfer)
    ctx.spools["spool-a"].step(artifact.execution.attempt_id, 3 * MIB)
    await ctx.engine.pause(transfer.id)
    await ctx.engine.activate_candidate_command(transfer.id, artifact.id, 1)
    await ctx.engine.delete(transfer.id, remote=False)
    assert [item for item in await ctx.repository.live_executions() if item.transfer_id == transfer.id] == []
    assert not os.path.exists(artifact.target)  # DP owned it: reclaimed
    assert await ctx.repository.commit_material(artifact.execution, ((0, SIZE),), PayloadFacts(True, True, SIZE, "x"),
                                                now=1.0) is None
    assert (await ctx.repository.material_state(artifact.id)).valid == ()


@pytest.mark.asyncio
async def test_a_failed_writer_reported_work_is_checkpointed_at_the_recovery_handoff(tmp_path, monkeypatch):
    from transfers.errors import Domain, NormalizedError, Origin, Retryability, Stage
    from transfers.models import ExecutionState
    ctx = await build(tmp_path, monkeypatch)
    transfer, artifact = await admit(ctx)
    spool = ctx.spools["spool-a"]
    spool.step(artifact.execution.attempt_id, 3 * MIB + 5)  # never periodically checkpointed
    spool.jobs[artifact.execution.attempt_id].state = ExecutionState.FAILED
    original = spool._observation

    def failing(handle, current):
        observed = original(handle, current)
        if current.state == ExecutionState.FAILED:
            observed = replace(observed, error=NormalizedError(
                Domain.NETWORK, Category.REMOTE_READ_FAILED, Stage.EXECUTION, retryability=Retryability.BACKOFF,
                origin=Origin.REMOTE_SOURCE, integration_id="spool-a"))
        return observed
    spool._observation = failing
    ctx.clock[0] += 1  # not yet due for a periodic checkpoint
    await ctx.engine.reconcile_executions()
    assert (await ctx.repository.material_state(artifact.id)).valid == ((0, 3 * MIB),)
    events = [event for event in await material_events(ctx, transfer.id) if event["event"] == "checkpoint"]
    assert events[-1]["boundary"] == "writer_failed"


@pytest.mark.asyncio
async def test_clean_executor_shutdown_forces_a_checkpoint_of_live_writers(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
    transfer, artifact = await admit(ctx)
    ctx.spools["spool-a"].step(artifact.execution.attempt_id, 3 * MIB + 1)
    assert await ctx.engine.checkpoint_live_material("executor_shutdown") == 1
    assert (await ctx.repository.material_state(artifact.id)).valid == ((0, 3 * MIB),)
    events = [event for event in await material_events(ctx, transfer.id) if event["event"] == "checkpoint"]
    assert events[-1]["boundary"] == "executor_shutdown"


@pytest.mark.asyncio
async def test_unknown_total_size_shows_valid_bytes_but_no_percentage(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "unknown.db")
    await database.init_db()
    sources = {"movie": payload(SIZE)}
    repository, registry = TransferRepository(), IntegrationRegistry()
    registry.register_provider(SpoolProvider("src-spool-a", "spoola", sources, report_size=False))
    spool = SpoolExecutor(repository.authorize_execution, sources, report_total=False)
    registry.register_executor(spool)
    clock = [1000.0]
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"),
                            policy=TransferPolicy(retry_delay=0, adoption_stability_seconds=0),
                            clock=lambda: clock[0])
    await engine.initialize()
    ctx = SimpleNamespace(engine=engine, repository=repository, clock=clock)
    transfer = await engine.submit((TransferRequest("spool", "movie", name="movie.bin"),), deduplicate=False)
    await engine.tick()
    artifact = (await repository.artifacts(transfer.id))[0]
    spool.step(artifact.execution.attempt_id, 2 * MIB + 3)
    await checkpoint(ctx)
    assert (await repository.material_state(artifact.id)).valid == ((0, 2 * MIB),)
    shown = await repository.presentation(transfer.id, details=True)
    # Bytes stay available; a percentage does not exist -- never 0%.
    assert shown["retained_bytes"] == 2 * MIB and shown["progress"] is None
    assert shown["files"][0]["retained_bytes"] == 2 * MIB and shown["files"][0]["progress"] is None
    assert (await repository.get(transfer.id)).progress is None


# -- fork readiness: data-dependent boundaries and collection members --------

def test_a_discovered_boundary_only_ever_lowers_retention_to_at_most_the_prefix():
    current = state([(0, 4 * MIB)])
    caps_ = caps(*CONTIGUOUS, ContinuationCapability.BOUNDARY_DISCOVERY)
    lowered = plan_continuation(current, candidate=candidate(), executor_id="nzb", capabilities=caps_,
                                reason="user_candidate_switch", discovered={"": 3 * MIB - 777})
    assert lowered.boundary == 3 * MIB - 777 and lowered.discarded == ((3 * MIB - 777, 4 * MIB),)
    above = plan_continuation(current, candidate=candidate(), executor_id="nzb", capabilities=caps_,
                              reason="resume", discovered={"": 9 * MIB})
    assert above.boundary == 4 * MIB  # never beyond DP-valid material


@pytest.mark.asyncio
async def test_boundary_discovery_plans_the_exact_data_dependent_offset(tmp_path, monkeypatch):
    from continuation_fakes import BoundarySpoolExecutor
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "boundary.db")
    await database.init_db()
    sources = {"movie": payload(SIZE)}
    repository, registry = TransferRepository(), IntegrationRegistry()
    registry.register_provider(SpoolProvider("src-spool-a", "spoola", sources))
    starts = (0, 700_001, 1_900_003, 3 * MIB + 17, 4 * MIB + 5)
    spool = BoundarySpoolExecutor(repository.authorize_execution, sources, segment_starts=starts)
    registry.register_executor(spool)
    clock = [1000.0]
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"),
                            policy=TransferPolicy(retry_delay=0, adoption_stability_seconds=0),
                            clock=lambda: clock[0])
    await engine.initialize()
    transfer = await engine.submit((TransferRequest("spool", "movie", name="movie.bin"),), deduplicate=False)
    await engine.tick()
    artifact = (await repository.artifacts(transfer.id))[0]
    spool.step(artifact.execution.attempt_id, 4 * MIB + 1)
    await engine.pause(transfer.id)
    assert (await repository.material_state(artifact.id)).safe_prefix == 4 * MIB
    await engine.resume(transfer.id)
    plan = spool.plans[-1]
    # The executor's own segment start below the DP prefix, not the prefix itself.
    assert spool.asked[-1] == ("", 4 * MIB) and plan.boundary == 3 * MIB + 17
    assert plan.discarded == ((3 * MIB + 17, 4 * MIB),) and "boundary_discovery" in plan.capabilities

    # A misbehaving answer (above the prefix) retains nothing rather than more.
    spool.answer = 5 * MIB
    await engine.pause(transfer.id)
    await engine.resume(transfer.id)
    assert spool.plans[-1].boundary == 0


def test_boundary_discovery_must_be_implemented_to_be_declared():
    registry = IntegrationRegistry()
    liar = SpoolExecutor(None, {}, continuation={*CONTIGUOUS, ContinuationCapability.FULL_RESTART,
                                                 ContinuationCapability.BOUNDARY_DISCOVERY})
    with pytest.raises(TypeError):
        registry.register_executor(liar)


async def _collection(tmp_path, monkeypatch, members):
    from continuation_fakes import CollectionSpoolExecutor, CollectionSpoolProvider
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "collection.db")
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    registry.register_provider(CollectionSpoolProvider())
    executor = CollectionSpoolExecutor(repository.authorize_execution, members)
    registry.register_executor(executor)
    clock = [1000.0]
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"),
                            policy=TransferPolicy(retry_delay=0, adoption_stability_seconds=0),
                            clock=lambda: clock[0])
    await engine.initialize()
    transfer = await engine.submit((TransferRequest("bundle", "set", name="set"),), deduplicate=False)
    await engine.tick()
    artifact = (await repository.artifacts(transfer.id))[0]
    return SimpleNamespace(engine=engine, repository=repository, executor=executor, clock=clock,
                           transfer=transfer, artifact=artifact)


@pytest.mark.asyncio
async def test_collection_members_carry_their_own_material_across_pause_and_resume(tmp_path, monkeypatch):
    members = {"a.bin": payload(3 * MIB + 99, "a"), "sub/b.bin": payload(2 * MIB + 7, "b")}
    ctx = await _collection(tmp_path, monkeypatch, members)
    attempt = ctx.artifact.execution.attempt_id
    ctx.executor.step(attempt, "a.bin", 2 * MIB + 50)
    ctx.executor.step(attempt, "sub/b.bin", MIB + 1)
    await checkpoint(ctx)
    current = await ctx.repository.material_state(ctx.artifact.id)
    assert dict(current.members) == {"a.bin": ((0, 2 * MIB),), "sub/b.bin": ((0, MIB),)}
    assert current.valid == () and current.valid_bytes == 3 * MIB
    assert (await ctx.repository.presentation(ctx.transfer.id))["retained_bytes"] == 3 * MIB

    await ctx.engine.pause(ctx.transfer.id)
    await ctx.engine.resume(ctx.transfer.id)
    plan = ctx.executor.plans[-1]
    assert plan.strategy == ContinuationStrategy.CONTIGUOUS_FROM_OFFSET
    assert dict(plan.member_boundaries) == {"a.bin": 2 * MIB, "sub/b.bin": MIB}
    resumed = (await ctx.repository.artifacts(ctx.transfer.id))[0].execution.attempt_id
    for name, data in members.items():
        ctx.executor.step(resumed, name, len(data))
    await ctx.engine.reconcile_executions()
    done = (await ctx.repository.artifacts(ctx.transfer.id))[0]
    assert done.state == "completed"
    root = Path(done.target)
    for name, data in members.items():
        assert (root / name).read_bytes() == data
    final = await ctx.repository.material_state(done.id)
    assert dict(final.members) == {name: ((0, len(data)),) for name, data in members.items()}


@pytest.mark.asyncio
async def test_collection_member_truncation_stale_writers_and_escapes_commit_nothing(tmp_path, monkeypatch):
    members = {"a.bin": payload(3 * MIB, "a")}
    ctx = await _collection(tmp_path, monkeypatch, members)
    handle = ctx.artifact.execution
    ctx.executor.step(handle.attempt_id, "a.bin", 2 * MIB + 3)
    # A member path escaping the collection root is refused outright.
    ctx.executor.report = (("../escape.bin", ((0, 2 * MIB),)), ("a.bin", ((0, 2 * MIB + 3),)))
    await checkpoint(ctx)
    current = await ctx.repository.material_state(ctx.artifact.id)
    assert dict(current.members) == {"a.bin": ((0, 2 * MIB),)}
    ctx.executor.report = None

    # External truncation of a member is reconciled before the next plan.
    await ctx.engine.pause(ctx.transfer.id)
    os.truncate(Path(ctx.artifact.target) / "a.bin", MIB + 10)
    await ctx.engine.resume(ctx.transfer.id)
    plan = ctx.executor.plans[-1]
    assert dict(plan.member_boundaries) == {"a.bin": MIB} and plan.material_generation == 2

    # The retired writer can no longer commit member material.
    facts = flush_payload(str(Path(ctx.artifact.target) / "a.bin"))
    assert await ctx.repository.commit_material(handle, ((0, 3 * MIB),), facts, now=9.0, member="a.bin") is None
