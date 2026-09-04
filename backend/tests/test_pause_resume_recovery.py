"""Deterministic convergence and no-progress source-recovery contracts."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Origin, Recovery, Retryability, Stage
from transfers.models import ExecutionObservation, ExecutionState, ResolutionResult, ResourceState, TransferProgress, TransferRequest, TransferState
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


class ControlledMemoryExecutor(MemoryExecutor):
    def __init__(self, authorize, *, zero_progress=False):
        super().__init__(authorize)
        self.zero_progress = zero_progress
        self.native_pause_calls = 0
        self.native_resume_calls = 0
        self.resume_entered = asyncio.Event()
        self.resume_release = asyncio.Event()
        self.block_resume = False

    async def start(self, request, handle):
        assert await self.authorize(handle, "start")
        self.calls.append(("start", handle))
        error = self.start_errors.pop(0) if self.start_errors else None
        progress = TransferProgress(4, 0 if self.zero_progress else 1, 0 if self.zero_progress else 1)
        result = ExecutionObservation(handle, ExecutionState.FAILED if error else ExecutionState.TRANSFERRING,
                                      progress, (request.target,), error)
        self.jobs[handle.attempt_id] = result
        return result

    async def pause(self, handle):
        assert await self.authorize(handle, "pause")
        self.native_pause_calls += 1
        self.calls.append(("pause", handle))
        current = self.jobs.get(handle.attempt_id, ExecutionObservation(handle, ExecutionState.ABSENT))
        if current.resumable:
            current = replace(current, state=ExecutionState.PAUSED)
            self.jobs[handle.attempt_id] = current
        return current

    async def resume(self, handle):
        assert await self.authorize(handle, "resume")
        self.native_resume_calls += 1
        self.calls.append(("resume", handle))
        if self.block_resume:
            self.resume_entered.set()
            await self.resume_release.wait()
        current = self.jobs.get(handle.attempt_id, ExecutionObservation(handle, ExecutionState.ABSENT))
        if current.resumable:
            current = replace(current, state=ExecutionState.TRANSFERRING)
            self.jobs[handle.attempt_id] = current
        return current


@pytest_asyncio.fixture
async def convergence(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider()
    executor = ControlledMemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    now = [1000.0]
    policy = TransferPolicy(retry_delay=1, adoption_stability_seconds=0, max_active_executions=2)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"), policy=policy, clock=lambda: now[0])
    await engine.initialize()
    return SimpleNamespace(engine=engine, repository=repository, registry=registry, provider=provider, executor=executor, now=now)


@pytest_asyncio.fixture
async def recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "recovery.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider()
    executor = ControlledMemoryExecutor(repository.authorize_execution, zero_progress=True)
    registry.register_provider(provider)
    registry.register_executor(executor)
    now = [2000.0]
    policy = TransferPolicy(retry_delay=1, adoption_stability_seconds=0, max_active_executions=2)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"), policy=policy, clock=lambda: now[0])
    await engine.initialize()
    return SimpleNamespace(engine=engine, repository=repository, registry=registry, provider=provider, executor=executor, now=now)


async def submit(ctx, payload="box", name="payload.bin"):
    transfer = await ctx.engine.submit((TransferRequest("parcel", payload, name=name),))
    await ctx.engine.tick()
    return transfer


def source_failure(category=Category.REMOTE_READ_FAILED, *, retryability=Retryability.BACKOFF,
                   recovery=Recovery.TRY_ALTERNATE_CANDIDATE):
    return NormalizedError(
        Domain.NETWORK, category, Stage.EXECUTION,
        retryability=retryability, recovery=recovery, origin=Origin.REMOTE_SOURCE,
        operator_action_required=False, integration_id="memory-copy",
    )


def executor_uncertainty():
    return NormalizedError(
        Domain.EXECUTOR, Category.EXECUTOR_UNAVAILABLE, Stage.RECONCILIATION,
        retryability=Retryability.BACKOFF, recovery=Recovery.RECONCILE,
        origin=Origin.EXECUTOR, operator_action_required=False, integration_id="memory-copy",
    )


@pytest.mark.asyncio
async def test_scheduler_and_explicit_resume_share_one_native_convergence_owner(convergence):
    transfer = await submit(convergence)
    await convergence.engine.pause(transfer.id)
    artifact = (await convergence.repository.artifacts(transfer.id))[0]
    stale = convergence.executor.jobs[artifact.execution.attempt_id]
    convergence.executor.native_resume_calls = 0

    scheduler = asyncio.create_task(convergence.engine._process_executions(
        transfer.id, (artifact,), {artifact.execution.attempt_id: stale}, dispatch_allowed=True,
    ))
    explicit = asyncio.create_task(convergence.engine.resume(transfer.id))
    await asyncio.gather(scheduler, explicit)

    assert convergence.executor.native_resume_calls == 1
    assert convergence.executor.jobs[artifact.execution.attempt_id].state == ExecutionState.TRANSFERRING


@pytest.mark.asyncio
async def test_rapid_triple_resume_is_idempotent(convergence):
    transfer = await submit(convergence)
    await convergence.engine.pause(transfer.id)
    convergence.executor.native_resume_calls = 0
    await asyncio.gather(*(convergence.engine.resume(transfer.id) for _ in range(3)))
    assert convergence.executor.native_resume_calls == 1


@pytest.mark.asyncio
async def test_pause_arriving_during_resume_reconciliation_wins(convergence):
    transfer = await submit(convergence)
    await convergence.engine.pause(transfer.id)
    artifact = (await convergence.repository.artifacts(transfer.id))[0]
    convergence.executor.native_pause_calls = 0
    convergence.executor.native_resume_calls = 0
    convergence.executor.block_resume = True

    resuming = asyncio.create_task(convergence.engine.resume(transfer.id))
    await convergence.executor.resume_entered.wait()
    pausing = asyncio.create_task(convergence.engine.pause(transfer.id))
    await asyncio.sleep(0)
    convergence.executor.resume_release.set()
    await asyncio.gather(resuming, pausing)

    assert convergence.executor.native_resume_calls == 1
    assert convergence.executor.native_pause_calls == 1
    assert convergence.executor.jobs[artifact.execution.attempt_id].state == ExecutionState.PAUSED
    assert (await convergence.repository.get(transfer.id)).paused


@pytest.mark.asyncio
async def test_capacity_starvation_does_not_emit_unpause_or_consume_source_budget(convergence):
    target = await submit(convergence, "target", "target.bin")
    await convergence.engine.pause(target.id)
    convergence.engine.configure_policy(replace(convergence.engine.policy, max_active_executions=1))
    blocker = await submit(convergence, "blocker", "blocker.bin")
    assert (await convergence.repository.get(blocker.id)).state == TransferState.TRANSFERRING
    artifact = (await convergence.repository.artifacts(target.id))[0]
    convergence.executor.native_resume_calls = 0

    await convergence.engine.resume(target.id)
    assert convergence.executor.native_resume_calls == 0
    assert convergence.executor.jobs[artifact.execution.attempt_id].state == ExecutionState.PAUSED
    assert await convergence.repository.recovery_budget(artifact.id) == (0, 0)


@pytest.mark.asyncio
async def test_unknown_execution_never_aggregates_to_transferring_or_spawns_writer(convergence):
    transfer = await submit(convergence)
    artifact = (await convergence.repository.artifacts(transfer.id))[0]
    original = artifact.execution
    convergence.executor.jobs[original.attempt_id] = ExecutionObservation(
        original, ExecutionState.UNKNOWN, error=executor_uncertainty(), paths=(artifact.target,),
    )
    for _ in range(4):
        await convergence.engine.tick()
    assert (await convergence.repository.get(transfer.id)).state != TransferState.TRANSFERRING
    assert (await convergence.repository.artifacts(transfer.id))[0].execution == original
    assert len(await convergence.repository.executions(transfer.id)) == 1
    assert await convergence.repository.recovery_budget(artifact.id) == (0, 0)


@pytest.mark.asyncio
async def test_first_source_failure_retries_same_candidate_without_destructive_cancel(recovery):
    error = source_failure()
    recovery.executor.start_errors = [error]
    transfer = await submit(recovery)
    artifact = (await recovery.repository.artifacts(transfer.id))[0]
    old = (await recovery.repository.executions(transfer.id))[0].handle

    assert artifact.state == "queued"
    assert artifact.selected == 0
    assert await recovery.repository.recovery_budget(artifact.id) == (1, 0)
    assert not [call for call in recovery.executor.calls if call[0] == "cancel"]
    assert not await recovery.repository.authorize_execution(old, "resume")
    assert not await recovery.repository.authorize_execution(old, "pause")


@pytest.mark.asyncio
async def test_second_no_progress_source_failure_refreshes_once_with_identity_preserved(recovery):
    error = source_failure()
    recovery.executor.start_errors = [error, error]
    transfer = await submit(recovery)
    before = (await recovery.repository.artifacts(transfer.id))[0]
    recovery.now[0] += 1
    await recovery.engine.tick()
    pending = (await recovery.repository.artifacts(transfer.id))[0]
    assert pending.state == "refresh_pending"
    assert await recovery.repository.recovery_budget(before.id) == (2, 1)

    await recovery.engine.tick()
    refreshed = (await recovery.repository.artifacts(transfer.id))[0]
    assert refreshed.id == before.id
    assert refreshed.target == before.target
    assert " (2)" not in refreshed.target
    assert len([call for call in recovery.provider.calls if call[0] == "refresh"]) == 1
    details = await recovery.repository.presentation(transfer.id, details=True)
    assert any(item.get("transition_kind") == "candidate_refresh" for item in details["route_attempts"])


@pytest.mark.asyncio
async def test_definitive_expiry_refreshes_immediately(recovery):
    expired = source_failure(Category.CANDIDATE_EXPIRED, retryability=Retryability.AFTER_RERESOLUTION,
                             recovery=Recovery.TRY_ALTERNATE_CANDIDATE)
    recovery.executor.start_errors = [expired]
    transfer = await submit(recovery)
    artifact = (await recovery.repository.artifacts(transfer.id))[0]
    assert artifact.state == "refresh_pending"
    assert await recovery.repository.recovery_budget(artifact.id) == (1, 1)


@pytest.mark.asyncio
async def test_actual_progress_resets_no_progress_recovery_episode(recovery):
    error = source_failure()
    recovery.executor.start_errors = [error]
    transfer = await submit(recovery)
    recovery.now[0] += 1
    await recovery.engine.tick()
    artifact = (await recovery.repository.artifacts(transfer.id))[0]
    assert artifact.state == "downloading"
    handle = artifact.execution
    recovery.executor.jobs[handle.attempt_id] = replace(
        recovery.executor.jobs[handle.attempt_id], progress=TransferProgress(4, 1, 1),
    )
    await recovery.engine.tick()
    assert await recovery.repository.recovery_budget(artifact.id) == (0, 0)

    recovery.executor.jobs[handle.attempt_id] = replace(
        recovery.executor.jobs[handle.attempt_id], state=ExecutionState.FAILED, error=error,
    )
    await recovery.engine.tick()
    after = (await recovery.repository.artifacts(transfer.id))[0]
    assert after.state == "queued"
    assert await recovery.repository.recovery_budget(artifact.id) == (1, 0)
    assert not [call for call in recovery.provider.calls if call[0] == "refresh"]


@pytest.mark.asyncio
async def test_recovery_budget_survives_engine_restart(recovery):
    error = source_failure()
    recovery.executor.start_errors = [error]
    transfer = await submit(recovery)
    artifact = (await recovery.repository.artifacts(transfer.id))[0]
    assert await recovery.repository.recovery_budget(artifact.id) == (1, 0)

    restarted = TransferEngine(
        TransferRepository(), recovery.registry, download_root=recovery.engine.root,
        policy=recovery.engine.policy, clock=lambda: recovery.now[0],
    )
    await restarted.initialize()
    recovery.executor.start_errors = [error]
    recovery.now[0] += 1
    await restarted.tick()
    pending = (await recovery.repository.artifacts(transfer.id))[0]
    assert pending.state == "refresh_pending"
    assert await recovery.repository.recovery_budget(artifact.id) == (2, 1)


@pytest.mark.asyncio
async def test_refresh_generation_cannot_loop_without_progress(recovery):
    error = source_failure()
    recovery.executor.start_errors = [error, error]
    transfer = await submit(recovery)
    recovery.now[0] += 1
    await recovery.engine.tick()
    await recovery.engine.tick()
    recovery.executor.start_errors = [error]
    await recovery.engine.tick()
    exhausted = (await recovery.repository.artifacts(transfer.id))[0]
    assert exhausted.state == "error"
    assert await recovery.repository.recovery_budget(exhausted.id) == (3, 1)
    assert len([call for call in recovery.provider.calls if call[0] == "refresh"]) == 1


@pytest.mark.asyncio
async def test_executor_ambiguity_never_consumes_source_recovery_budget(recovery):
    transfer = await submit(recovery)
    artifact = (await recovery.repository.artifacts(transfer.id))[0]
    recovery.executor.jobs[artifact.execution.attempt_id] = ExecutionObservation(
        artifact.execution, ExecutionState.UNKNOWN, error=executor_uncertainty(), paths=(artifact.target,),
    )
    await recovery.engine.tick()
    assert await recovery.repository.recovery_budget(artifact.id) == (0, 0)
    assert len(await recovery.repository.executions(transfer.id)) == 1
