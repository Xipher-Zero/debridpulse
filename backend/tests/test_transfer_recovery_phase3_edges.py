"""Phase-3 edge/race matrix for completion, refresh, restart, and readiness."""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Origin, Retryability, Stage
from transfers.models import ExecutionObservation, ExecutionState, ResolutionResult, ResourceState, TransferRequest
from transfers.policy import RecoveryAction, TransferPolicy
from transfers.recovery_execution import RecoveryTrigger
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry


@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "phase3-edges.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider()
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    now = [9000.0]
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0, max_active_executions=4),
        clock=lambda: now[0],
    )
    await engine.initialize()
    return repository, registry, provider, executor, engine, now


async def materialize(runtime):
    repository, _registry, _provider, _executor, engine, _now = runtime
    transfer = await engine.submit((TransferRequest("parcel", "box", name="payload.bin"),))
    await engine.resolve_pending()
    return transfer, (await repository.artifacts(transfer.id))[0]


async def running(runtime):
    repository, _registry, _provider, _executor, engine, _now = runtime
    transfer, _artifact = await materialize(runtime)
    await engine.reconcile_executions()
    artifact = (await repository.artifacts(transfer.id))[0]
    assert artifact.execution is not None
    return transfer, artifact


def transient_error():
    return NormalizedError(
        Domain.NETWORK, Category.REMOTE_READ_FAILED, Stage.EXECUTION,
        retryability=Retryability.BACKOFF, origin=Origin.REMOTE_SOURCE,
        integration_id="memory-copy",
    )


async def make_refresh_pending(runtime):
    repository, _registry, _provider, _executor, _engine, now = runtime
    transfer, artifact = await materialize(runtime)
    claim = await repository.claim_recovery(
        artifact.id, RecoveryTrigger.AUTO_RETRY, now[0], lease_seconds=60,
    )
    assert claim is not None
    decision_id = f"test-refresh:{artifact.id}"
    assert await repository.record_phase3_decision(
        claim, decision_id=decision_id,
        action=RecoveryAction.REFRESH_CANDIDATE.value, reason="test-refresh",
    )
    assert await repository.reserve_recovery_refresh(claim, decision_id)
    assert await repository.transition_recovery(
        artifact.id, "refresh_pending", error=transient_error(), retry_at=now[0], clear_quiescence=True,
    )
    assert await repository.finish_recovery_claim(
        claim, action=RecoveryAction.REFRESH_CANDIDATE.value,
        reason="test-refresh", outcome="scheduled",
    )
    return transfer, (await repository.artifacts(transfer.id))[0]


@pytest.mark.asyncio
async def test_executor_unavailable_before_execution_uses_unified_wait(runtime):
    repository, registry, _provider, executor, engine, _now = runtime
    transfer, artifact = await materialize(runtime)
    registry.executors.pop(executor.descriptor.id)
    await engine.reconcile_executions()
    current = (await repository.artifacts(transfer.id))[0]
    context = await repository.recovery_context(current.id)
    assert current.id == artifact.id
    assert current.execution is None
    assert current.state == "recovery_wait"
    assert context["quiescence_reason"] == "executor_unavailable"
    assert context["last_applied_trigger"] == RecoveryTrigger.AUTO_RETRY.value
    assert await repository.recovery_budget(current.id) == (0, 0)


@pytest.mark.asyncio
async def test_provider_disabled_before_execution_uses_unified_wait(runtime):
    repository, _registry, provider, _executor, engine, _now = runtime
    transfer, artifact = await materialize(runtime)
    provider.descriptor = replace(provider.descriptor, enabled=False)
    await engine.reconcile_executions()
    current = (await repository.artifacts(transfer.id))[0]
    context = await repository.recovery_context(current.id)
    assert current.id == artifact.id
    assert current.execution is None
    assert current.state == "recovery_wait"
    assert context["quiescence_reason"] == "provider_disabled"
    assert context["last_applied_trigger"] == RecoveryTrigger.AUTO_RETRY.value
    assert await repository.recovery_budget(current.id) == (0, 0)


@pytest.mark.asyncio
async def test_completed_execution_truth_wins_over_provider_disable(runtime):
    repository, _registry, provider, executor, engine, _now = runtime
    transfer, artifact = await running(runtime)
    executor.finish(artifact.execution)
    provider.descriptor = replace(provider.descriptor, enabled=False)
    await engine.reconcile_executions()
    current = (await repository.artifacts(transfer.id))[0]
    assert current.state == "completed"
    context = await repository.recovery_context(current.id)
    assert context.get("quiescence_reason") != "provider_disabled"


@pytest.mark.asyncio
async def test_completed_execution_truth_wins_over_storage_blocker(runtime):
    repository, _registry, _provider, executor, engine, _now = runtime
    transfer, artifact = await running(runtime)
    executor.finish(artifact.execution)
    engine.dispatch_permitted = False
    assert await engine.recover_artifact(
        artifact, trigger=RecoveryTrigger.AUTO_RETRY,
    )
    current = (await repository.artifacts(transfer.id))[0]
    assert current.state == "completed"


@pytest.mark.asyncio
async def test_provider_disabled_before_refresh_issues_no_refresh(runtime):
    repository, _registry, provider, _executor, engine, _now = runtime
    transfer, artifact = await make_refresh_pending(runtime)
    provider.descriptor = replace(provider.descriptor, enabled=False)
    refresh_before = len([call for call in provider.calls if call[0] == "refresh"])
    assert await engine.recover_artifact(
        artifact, trigger=RecoveryTrigger.AUTO_RETRY,
    )
    current = (await repository.artifacts(transfer.id))[0]
    context = await repository.recovery_context(current.id)
    assert len([call for call in provider.calls if call[0] == "refresh"]) == refresh_before
    assert current.state == "recovery_wait"
    assert context["quiescence_reason"] == "provider_disabled"


@pytest.mark.asyncio
async def test_provider_disable_during_refresh_does_not_duplicate_refresh_or_start(runtime, monkeypatch):
    repository, _registry, provider, executor, engine, _now = runtime
    transfer, artifact = await make_refresh_pending(runtime)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []
    original = provider.refresh

    async def blocked(candidate):
        calls.append(candidate.id)
        entered.set()
        await release.wait()
        return await original(candidate)

    monkeypatch.setattr(provider, "refresh", blocked)
    task = asyncio.create_task(engine.recover_artifact(
        artifact, trigger=RecoveryTrigger.AUTO_RETRY,
    ))
    await entered.wait()
    provider.descriptor = replace(provider.descriptor, enabled=False)
    second = await engine.recover_artifact(
        artifact, trigger=RecoveryTrigger.PROVIDER_RECOVERY,
    )
    assert second is False
    release.set()
    assert await task
    assert len(calls) == 1
    await engine.reconcile_executions()
    current = (await repository.artifacts(transfer.id))[0]
    context = await repository.recovery_context(current.id)
    assert current.execution is None
    assert context["quiescence_reason"] == "provider_disabled"
    assert not [call for call in executor.calls if call[0] == "start"]
    assert context["candidate_generation"] == 1


@pytest.mark.asyncio
async def test_pause_during_refresh_fences_result_then_resume_replays_without_second_refresh(runtime, monkeypatch):
    repository, _registry, provider, _executor, engine, _now = runtime
    transfer, artifact = await make_refresh_pending(runtime)
    target = artifact.target
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []
    original = provider.refresh

    async def blocked(candidate):
        calls.append(candidate.id)
        entered.set()
        await release.wait()
        return await original(candidate)

    monkeypatch.setattr(provider, "refresh", blocked)
    task = asyncio.create_task(engine.recover_artifact(
        artifact, trigger=RecoveryTrigger.AUTO_RETRY,
    ))
    await entered.wait()
    await engine.pause(transfer.id)
    release.set()
    result = await task
    assert result is False
    paused = (await repository.artifacts(transfer.id))[0]
    assert paused.target == target
    assert len(calls) == 1

    await engine.resume(transfer.id)
    await engine.reconcile_executions()
    current = (await repository.artifacts(transfer.id))[0]
    assert current.target == target
    assert len(calls) == 1
    assert current.execution is not None


@pytest.mark.asyncio
async def test_resume_and_startup_reconcile_race_reuses_paused_gid(runtime):
    repository, _registry, _provider, executor, engine, _now = runtime
    transfer, artifact = await running(runtime)
    original = artifact.execution
    await engine.pause(transfer.id)
    resume_before = len([call for call in executor.calls if call[0] == "resume"])
    paused = (await repository.artifacts(transfer.id))[0]
    await asyncio.gather(
        engine.resume(transfer.id),
        engine.recover_artifact(paused, trigger=RecoveryTrigger.STARTUP_RECONCILE),
    )
    current = (await repository.artifacts(transfer.id))[0]
    assert current.execution == original
    assert len([call for call in executor.calls if call[0] == "resume"]) <= resume_before + 1


@pytest.mark.asyncio
async def test_two_reconstruction_triggers_create_one_replacement(runtime):
    repository, _registry, _provider, executor, engine, now = runtime
    transfer, artifact = await running(runtime)
    original = artifact.execution
    target = artifact.target
    executor.jobs.pop(original.attempt_id)
    await engine.recover_artifact(artifact, trigger=RecoveryTrigger.STARTUP_RECONCILE)
    waiting = (await repository.artifacts(transfer.id))[0]
    assert waiting.execution is None
    now[0] = waiting.retry_at
    await asyncio.gather(
        engine.recover_artifact(waiting, trigger=RecoveryTrigger.AUTO_RETRY),
        engine.recover_artifact(waiting, trigger=RecoveryTrigger.STARTUP_RECONCILE),
    )
    current = (await repository.artifacts(transfer.id))[0]
    assert current.execution is not None
    assert current.execution != original
    assert current.target == target
    assert len(await repository.executions(transfer.id)) == 2


@pytest.mark.asyncio
async def test_executor_return_while_provider_disabled_keeps_provider_blocker(runtime):
    repository, registry, provider, executor, engine, _now = runtime
    transfer, artifact = await running(runtime)
    original = artifact.execution
    registry.executors.pop(original.executor_id)
    await engine.reconcile_executions()
    provider.descriptor = replace(provider.descriptor, enabled=False)
    registry.register_executor(executor)
    await engine.reconcile_executions()
    current = (await repository.artifacts(transfer.id))[0]
    context = await repository.recovery_context(current.id)
    assert current.execution == original
    assert current.state == "recovery_wait"
    assert context["quiescence_reason"] == "provider_disabled"


@pytest.mark.asyncio
async def test_unknown_executor_truth_is_quiescent_without_replacement_or_budget(runtime):
    repository, _registry, _provider, executor, engine, _now = runtime
    transfer, artifact = await running(runtime)
    original = artifact.execution
    executor.jobs[original.attempt_id] = ExecutionObservation(
        original,
        state=ExecutionState.UNKNOWN,
        error=NormalizedError(
            Domain.EXECUTOR, Category.EXECUTOR_UNAVAILABLE, Stage.RECONCILIATION,
            retryability=Retryability.BACKOFF, origin=Origin.EXECUTOR,
        ),
    )
    assert await engine.recover_artifact(
        artifact, trigger=RecoveryTrigger.STARTUP_RECONCILE,
    )
    current = (await repository.artifacts(transfer.id))[0]
    context = await repository.recovery_context(current.id)
    assert current.execution == original
    assert current.state == "recovery_wait"
    assert context["quiescence_reason"] == "retry_backoff"
    assert len(await repository.executions(transfer.id)) == 1
    assert await repository.recovery_budget(current.id) == (0, 0)
