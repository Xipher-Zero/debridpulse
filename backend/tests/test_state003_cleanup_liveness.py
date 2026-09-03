"""STATE-003: durable executor cleanup remains live after destructive retry exhaustion."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Recovery, Retryability, Stage
from transfers.models import ExecutionState, OutcomeKind, TransferOutcome, TransferRequest, TransferState
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


class Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


@pytest_asyncio.fixture
async def core(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state003.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider()
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    clock = Clock()
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(
            max_attempts=3, retry_delay=0, max_retry_delay=60,
            resolution_retry_delay=0, adoption_stability_seconds=0,
            resource_poll_interval=30,
        ),
        clock=clock,
    )
    await engine.initialize()
    return SimpleNamespace(
        engine=engine, repository=repository, registry=registry,
        provider=provider, executor=executor, clock=clock,
    )


async def executing(core):
    transfer = await core.engine.submit(
        (TransferRequest("parcel", "box", name="payload.bin"),), deduplicate=False,
    )
    await core.engine.tick()
    artifact = (await core.repository.artifacts(transfer.id))[0]
    assert artifact.execution is not None
    return transfer, artifact


def transient_cleanup_error():
    return NormalizedError(
        Domain.CLEANUP, Category.REMOTE_CLEANUP_FAILED, Stage.CLEANUP,
        retryability=Retryability.BACKOFF, recovery=Recovery.RETRY,
    )


async def exhaust_destructive_cancel_budget(core, transfer_id):
    error = transient_cleanup_error()
    calls = 0

    async def fail_cancel(_handle):
        nonlocal calls
        calls += 1
        return TransferOutcome(OutcomeKind.FAILURE, error)

    core.executor.cancel = fail_cancel
    for _ in range(4):
        await core.engine.cancel(transfer_id)
    return error, calls


@pytest.mark.asyncio
async def test_state003_retry_exhaustion_switches_to_throttled_observation_without_abandonment(core):
    transfer, artifact = await executing(core)
    error, calls = await exhaust_destructive_cancel_budget(core, transfer.id)
    assert calls == 3

    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert status["state"] == "pending"
    assert status["attempts"] == 3
    assert status["error"] == error
    assert status["authorized"] is True
    assert status["retry_at"] == 1030.0

    await core.engine.reconcile_executions()
    assert calls == 3
    core.clock.value = 1030.0
    await core.engine.reconcile_executions()
    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert calls == 3
    assert status["state"] == "pending"
    assert status["attempts"] == 3
    assert status["authorized"] is True
    assert status["retry_at"] == 1060.0
    assert (await core.repository.get(transfer.id)).state == TransferState.CANCELLED


@pytest.mark.asyncio
async def test_state003_retry_exhaustion_survives_restart_and_eventual_absence_settles(core):
    transfer, artifact = await executing(core)
    _error, calls = await exhaust_destructive_cancel_budget(core, transfer.id)
    assert calls == 3
    core.executor.jobs.pop(artifact.execution.attempt_id, None)
    core.clock.value = 1030.0

    restarted = TransferEngine(
        TransferRepository(), core.registry, download_root=core.engine.root,
        policy=core.engine.policy, clock=core.clock,
    )
    await restarted.initialize()
    await restarted.reconcile_executions()

    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert status["state"] == "complete"
    assert status["execution_state"] == ExecutionState.ABSENT
    assert status["attempts"] == 3
    assert status["authorized"] is False
    assert (await core.repository.get(transfer.id)).state == TransferState.CANCELLED
    assert (await core.repository.artifacts(transfer.id))[0].state == "cancelled"


@pytest.mark.asyncio
async def test_state003_late_external_success_settles_uncertainty_without_logical_resurrection(core):
    transfer, artifact = await executing(core)
    _error, calls = await exhaust_destructive_cancel_budget(core, transfer.id)
    assert calls == 3
    core.executor.finish(artifact.execution, materialize=False)
    core.clock.value = 1030.0

    await core.engine.reconcile_executions()
    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert status["state"] == "complete"
    assert status["execution_state"] == ExecutionState.SUCCEEDED
    assert status["authorized"] is False
    assert (await core.repository.get(transfer.id)).state == TransferState.CANCELLED
    assert (await core.repository.artifacts(transfer.id))[0].state == "cancelled"


@pytest.mark.asyncio
async def test_state003_observation_outage_is_throttled_without_spending_destructive_budget(core):
    transfer, artifact = await executing(core)
    assert await core.repository.cancel_with_execution_cleanup(
        transfer.id, expected_epoch=transfer.epoch, now=core.clock(),
    )
    observes = 0

    async def fail_observe(_handle):
        nonlocal observes
        observes += 1
        raise OSError("executor temporarily unreachable")

    core.executor.observe = fail_observe
    await core.engine.reconcile_executions()
    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert observes == 1
    assert status["attempts"] == 0
    assert status["state"] == "pending"
    assert status["authorized"] is True
    assert status["retry_at"] == 1030.0

    await core.engine.reconcile_executions()
    assert observes == 1
    core.clock.value = 1030.0
    await core.engine.reconcile_executions()
    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert observes == 2
    assert status["attempts"] == 0
    assert status["retry_at"] == 1060.0


@pytest.mark.asyncio
async def test_state003_cleanup_claim_lease_serializes_concurrent_workers(core):
    transfer, artifact = await executing(core)
    assert await core.repository.cancel_with_execution_cleanup(
        transfer.id, expected_epoch=transfer.epoch, now=core.clock(),
    )
    original_observe = core.executor.observe
    entered = asyncio.Event()
    release = asyncio.Event()
    observations = 0

    async def blocked_observe(handle):
        nonlocal observations
        observations += 1
        entered.set()
        await release.wait()
        return await original_observe(handle)

    core.executor.observe = blocked_observe
    second = TransferEngine(
        TransferRepository(), core.registry, download_root=core.engine.root,
        policy=core.engine.policy, clock=core.clock,
    )
    await second.initialize()

    first_task = asyncio.create_task(core.engine._cleanup_executions_pending(transfer_id=transfer.id))
    await entered.wait()
    assert await second._cleanup_executions_pending(transfer_id=transfer.id) == ()
    assert observations == 1
    release.set()
    assert await first_task == ()

    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert observations == 1
    assert status["state"] == "complete"
    assert status["attempts"] == 1
    assert status["authorized"] is False


@pytest.mark.asyncio
async def test_state003_delete_preserves_cleanup_and_path_reservation_until_external_absence(core):
    transfer, artifact = await executing(core)
    target = artifact.target.casefold()
    assert target in await core.repository.occupied_paths()
    error = transient_cleanup_error()
    calls = 0

    async def fail_cancel(_handle):
        nonlocal calls
        calls += 1
        return TransferOutcome(OutcomeKind.FAILURE, error)

    core.executor.cancel = fail_cancel
    await core.engine.delete(transfer.id, remote=False)
    await core.engine._cleanup_executions_pending(transfer_id=transfer.id)
    await core.engine._cleanup_executions_pending(transfer_id=transfer.id)
    await core.engine._cleanup_executions_pending(transfer_id=transfer.id)

    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert calls == 3
    assert (await core.repository.get(transfer.id)).state == TransferState.DELETED
    assert status["state"] == "pending"
    assert status["attempts"] == 3
    assert status["authorized"] is True
    assert target in await core.repository.occupied_paths()

    core.executor.jobs.pop(artifact.execution.attempt_id, None)
    core.clock.value = status["retry_at"]
    await core.engine.reconcile_executions()
    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert status["state"] == "complete"
    assert status["authorized"] is False
    assert target not in await core.repository.occupied_paths()
    assert (await core.repository.get(transfer.id)).state == TransferState.DELETED


@pytest.mark.asyncio
async def test_state003_legacy_blocked_obligation_is_reclaimed_and_can_converge(core):
    transfer, artifact = await executing(core)
    handle = artifact.execution
    error = transient_cleanup_error()
    assert await core.repository.cancel_with_execution_cleanup(
        transfer.id, expected_epoch=transfer.epoch, now=core.clock(),
    )
    for _ in range(3):
        assert await core.repository.claim_execution_cleanup(
            handle.attempt_id, now=core.clock(), lease_until=core.clock(),
        )
        assert await core.repository.execution_cleanup_attempt(handle.attempt_id)
        await core.repository.execution_cleanup_retry(handle.attempt_id, error, core.clock())
    await core.repository.execution_cleanup_retry(handle.attempt_id, error, None)
    before = await core.repository.execution_cleanup_status(handle.attempt_id)
    assert before["state"] == "blocked"
    assert before["attempts"] == 3
    assert before["authorized"] is True

    core.executor.jobs.pop(handle.attempt_id, None)
    await core.engine.reconcile_executions()
    after = await core.repository.execution_cleanup_status(handle.attempt_id)
    assert after["state"] == "complete"
    assert after["execution_state"] == ExecutionState.ABSENT
    assert after["attempts"] == 3
    assert after["authorized"] is False
    assert (await core.repository.get(transfer.id)).state == TransferState.CANCELLED


def test_state003_architecture_document_records_liveness_contract():
    root = Path(__file__).resolve().parents[2]
    doc = (root / "docs/architecture/EXECUTION_CLEANUP_LIFECYCLE.md").read_text(encoding="utf-8")
    assert "Exhausting destructive retry pressure does **not** end reconciliation ownership" in doc
    assert "legacy `blocked` cleanup row is a degraded unresolved obligation, not completion" in doc
    assert "later executor observations cannot revive the logical transfer" in doc
    assert "target path remains conservatively reserved" in doc
