"""Second-pass STATE-002 durable external-execution cleanup contracts."""
import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Recovery, Retryability, Stage
from transfers.models import ExecutionState, OutcomeKind, TransferOutcome, TransferProgress, TransferRequest, TransferState
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
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state002.db")
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
        policy=TransferPolicy(max_attempts=3, retry_delay=0, resolution_retry_delay=0, adoption_stability_seconds=0),
        clock=clock,
    )
    await engine.initialize()
    return SimpleNamespace(engine=engine, repository=repository, registry=registry, provider=provider, executor=executor, clock=clock)


async def executing(core):
    transfer = await core.engine.submit((TransferRequest("parcel", "box", name="payload.bin"),), deduplicate=False)
    await core.engine.tick()
    artifact = (await core.repository.artifacts(transfer.id))[0]
    assert artifact.execution is not None
    return transfer, artifact


def transient_cleanup_error():
    return NormalizedError(
        Domain.CLEANUP, Category.REMOTE_CLEANUP_FAILED, Stage.CLEANUP,
        retryability=Retryability.BACKOFF, recovery=Recovery.RETRY,
    )


@pytest.mark.asyncio
async def test_state002_immediate_executor_cancel_satisfies_cleanup_without_resurrection(core):
    transfer, artifact = await executing(core)
    assert await core.engine.cancel(transfer.id) == ()
    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert (await core.repository.get(transfer.id)).state == TransferState.CANCELLED
    assert status["state"] == "complete"
    assert status["authorized"] is False
    assert status["attempts"] == 1
    before = len([call for call in core.executor.calls if call[0] == "cancel"])
    assert await core.engine.cancel(transfer.id) == ()
    assert len([call for call in core.executor.calls if call[0] == "cancel"]) == before


@pytest.mark.asyncio
async def test_state002_failed_cancel_is_durable_while_parent_is_already_cancelled(core):
    transfer, artifact = await executing(core)
    error = transient_cleanup_error()

    async def fail_cancel(_handle):
        return TransferOutcome(OutcomeKind.FAILURE, error)

    core.executor.cancel = fail_cancel
    assert await core.engine.cancel(transfer.id) == (error,)
    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert (await core.repository.get(transfer.id)).state == TransferState.CANCELLED
    assert (await core.repository.artifacts(transfer.id))[0].state == "cancelled"
    assert status["state"] == "pending"
    assert status["attempts"] == 1
    assert status["error"] == error
    assert status["authorized"] is True


@pytest.mark.asyncio
async def test_state002_failed_cancel_survives_restart_and_converges(core):
    transfer, artifact = await executing(core)
    error = transient_cleanup_error()
    original_cancel = core.executor.cancel
    calls = 0

    async def fail_once(handle):
        nonlocal calls
        calls += 1
        if calls == 1:
            return TransferOutcome(OutcomeKind.FAILURE, error)
        return await original_cancel(handle)

    core.executor.cancel = fail_once
    assert await core.engine.cancel(transfer.id) == (error,)
    restarted = TransferEngine(
        TransferRepository(), core.registry, download_root=core.engine.root, policy=core.engine.policy, clock=core.clock,
    )
    await restarted.initialize()
    await restarted.reconcile_executions()
    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert calls == 2
    assert status["state"] == "complete"
    assert status["authorized"] is False
    assert (await core.repository.get(transfer.id)).state == TransferState.CANCELLED


@pytest.mark.asyncio
async def test_state002_repeated_failures_bound_destructive_pressure_without_abandoning_reconciliation(core):
    transfer, artifact = await executing(core)
    error = transient_cleanup_error()
    calls = 0

    async def fail_cancel(_handle):
        nonlocal calls
        calls += 1
        return TransferOutcome(OutcomeKind.FAILURE, error)

    core.executor.cancel = fail_cancel
    for _ in range(4):
        await core.engine.cancel(transfer.id)
    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert calls == 3
    assert status["attempts"] == 3
    assert status["state"] == "pending"
    assert status["error"] == error
    assert status["authorized"] is True
    assert status["retry_at"] > core.clock()
    assert (await core.repository.get(transfer.id)).state == TransferState.CANCELLED


@pytest.mark.asyncio
async def test_state002_late_executor_success_only_reconciles_external_cleanup(core):
    transfer, artifact = await executing(core)
    error = transient_cleanup_error()

    async def fail_cancel(_handle):
        return TransferOutcome(OutcomeKind.FAILURE, error)

    core.executor.cancel = fail_cancel
    await core.engine.cancel(transfer.id)
    core.executor.finish(artifact.execution, materialize=False)
    restarted = TransferEngine(
        TransferRepository(), core.registry, download_root=core.engine.root, policy=core.engine.policy, clock=core.clock,
    )
    await restarted.initialize()
    await restarted.reconcile_executions()
    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert status["state"] == "complete"
    assert status["execution_state"] == ExecutionState.SUCCEEDED
    assert (await core.repository.get(transfer.id)).state == TransferState.CANCELLED
    assert (await core.repository.artifacts(transfer.id))[0].state == "cancelled"


@pytest.mark.asyncio
async def test_state002_repeated_user_cancel_does_not_duplicate_cleanup_obligation(core):
    transfer, artifact = await executing(core)
    error = transient_cleanup_error()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_failure(_handle):
        entered.set()
        await release.wait()
        return TransferOutcome(OutcomeKind.FAILURE, error)

    core.executor.cancel = blocked_failure
    first = asyncio.create_task(core.engine.cancel(transfer.id))
    await entered.wait()
    assert (await core.repository.get(transfer.id)).state == TransferState.CANCELLED
    release.set()
    assert await first == (error,)
    await core.engine.cancel(transfer.id)
    statuses = [item for item in await core.repository.executions(transfer.id) if item.handle.attempt_id == artifact.execution.attempt_id]
    assert len(statuses) == 1
    cleanup = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert cleanup["attempts"] == 2


@pytest.mark.asyncio
async def test_state002_cancel_commit_contains_cleanup_obligation_before_executor_io(core):
    transfer, artifact = await executing(core)
    observed = {}

    async def inspect_then_fail(_handle):
        observed["parent"] = (await core.repository.get(transfer.id)).state
        observed["artifact"] = (await core.repository.artifacts(transfer.id))[0].state
        observed["cleanup"] = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
        raise asyncio.TimeoutError("cancel timeout containing https://user:password@example.test/secret")

    core.executor.cancel = inspect_then_fail
    errors = await core.engine.cancel(transfer.id)
    assert observed["parent"] == TransferState.CANCELLED
    assert observed["artifact"] == "cancelled"
    assert observed["cleanup"]["state"] == "pending"
    assert observed["cleanup"]["attempts"] == 1
    assert errors and errors[0].category == Category.REMOTE_CLEANUP_FAILED
    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert "password" not in status["error"].diagnostic
    assert "example.test" not in status["error"].diagnostic
