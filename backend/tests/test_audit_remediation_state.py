"""Deterministic STATE-001 cancellation/completion race contracts."""
import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Recovery, Retryability, Stage, TransferError
from transfers.models import (
    ExecutionObservation, ExecutionState, OutcomeKind, TransferOutcome,
    TransferProgress, TransferRequest, TransferState,
)
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


@pytest_asyncio.fixture
async def core(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state-audit.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider()
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(retry_delay=0, resolution_retry_delay=0, adoption_stability_seconds=0),
        clock=lambda: 1000.0,
    )
    await engine.initialize()
    return SimpleNamespace(engine=engine, repository=repository, registry=registry, provider=provider, executor=executor)


async def executing(core):
    transfer = await core.engine.submit((TransferRequest("parcel", "box", name="payload.bin"),), deduplicate=False)
    await core.engine.tick()
    artifact = (await core.repository.artifacts(transfer.id))[0]
    assert artifact.execution is not None
    return transfer, artifact


@pytest.mark.asyncio
async def test_cancellation_is_durable_before_blocked_executor_cancel_and_late_completion_cannot_win(core):
    transfer, artifact = await executing(core)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_cancel(handle):
        assert await core.repository.authorize_execution(handle, "cancel")
        entered.set()
        await release.wait()
        return TransferOutcome(OutcomeKind.CANCELLED)

    core.executor.cancel = blocked_cancel
    task = asyncio.create_task(core.engine.cancel(transfer.id))
    await entered.wait()
    assert (await core.repository.get(transfer.id)).state == TransferState.CANCELLED

    # A reconciliation observation can still be retained as remote history, but
    # it cannot rewrite the cancelled artifact or parent into completion.
    await core.repository.execution(ExecutionObservation(
        artifact.execution, ExecutionState.SUCCEEDED, TransferProgress(4, 4),
    ))
    await core.repository.artifact_state(artifact.id, "completed", expected_bytes=4)
    assert (await core.repository.get(transfer.id)).state == TransferState.CANCELLED
    assert (await core.repository.artifacts(transfer.id))[0].state != "completed"

    release.set()
    assert await task == ()
    assert (await core.repository.artifacts(transfer.id))[0].state == "cancelled"


@pytest.mark.asyncio
async def test_completion_that_wins_before_cancel_authority_is_truthful_conflict(core):
    transfer, artifact = await executing(core)
    core.executor.finish(artifact.execution)
    await core.engine.reconcile_executions()
    assert (await core.repository.get(transfer.id)).state == TransferState.COMPLETED
    before = len([call for call in core.executor.calls if call[0] == "cancel"])
    with pytest.raises(TransferError) as captured:
        await core.engine.cancel(transfer.id)
    assert captured.value.error.category == Category.RESOURCE_STATE_CONFLICT
    assert (await core.repository.get(transfer.id)).state == TransferState.COMPLETED
    assert len([call for call in core.executor.calls if call[0] == "cancel"]) == before


@pytest.mark.asyncio
async def test_executor_cancel_failure_preserves_logical_cancel_and_reports_cleanup_error(core):
    transfer, artifact = await executing(core)
    error = NormalizedError(
        Domain.EXECUTOR, Category.REMOTE_CLEANUP_FAILED, Stage.EXECUTION,
        retryability=Retryability.NEVER, recovery=Recovery.REQUIRE_OPERATOR,
    )

    async def failed_cancel(_handle):
        return TransferOutcome(OutcomeKind.FAILURE, error)

    core.executor.cancel = failed_cancel
    errors = await core.engine.cancel(transfer.id)
    assert errors == (error,)
    assert (await core.repository.get(transfer.id)).state == TransferState.CANCELLED
    assert (await core.repository.artifacts(transfer.id))[0].state == "cancelled"
    attempt = (await core.repository.executions(transfer.id))[0]
    assert attempt.state != ExecutionState.CANCELLED


@pytest.mark.asyncio
async def test_executor_cancel_timeout_preserves_logical_cancel_and_reports_error(core):
    transfer, _artifact = await executing(core)

    async def timed_out(_handle):
        raise asyncio.TimeoutError("executor cancel timed out")

    core.executor.cancel = timed_out
    errors = await core.engine.cancel(transfer.id)
    assert len(errors) == 1
    assert (await core.repository.get(transfer.id)).state == TransferState.CANCELLED


@pytest.mark.asyncio
async def test_repeated_cancel_is_idempotent_and_does_not_repeat_remote_cancel(core):
    transfer, _artifact = await executing(core)
    assert await core.engine.cancel(transfer.id) == ()
    first = len([call for call in core.executor.calls if call[0] == "cancel"])
    assert await core.engine.cancel(transfer.id) == ()
    assert len([call for call in core.executor.calls if call[0] == "cancel"]) == first


@pytest.mark.asyncio
async def test_restart_after_durable_cancel_does_not_reconcile_cancelled_execution(core):
    transfer, _artifact = await executing(core)
    await core.engine.cancel(transfer.id)
    before = len([call for call in core.executor.calls if call[0] == "observe"])
    restarted = TransferEngine(
        TransferRepository(), core.registry, download_root=core.engine.root,
        policy=core.engine.policy, clock=lambda: 1000.0,
    )
    await restarted.initialize()
    await restarted.reconcile_executions()
    assert (await core.repository.get(transfer.id)).state == TransferState.CANCELLED
    assert len([call for call in core.executor.calls if call[0] == "observe"]) == before


@pytest.mark.asyncio
async def test_cancelled_parent_rejects_new_resolution_materialization_and_start(core):
    transfer = await core.engine.submit((TransferRequest("parcel", "box", name="payload.bin"),), deduplicate=False)
    record = (await core.repository.requests(transfer.id))[0]
    assert await core.repository.state(transfer.id, TransferState.CANCELLED)
    assert await core.repository.begin_resolution(record.id, core.provider.descriptor.id) is None
    assert await core.repository.materialize(record, (core.provider.candidate("payload.bin"),), "payload.bin") is None
    assert not await core.repository.live_executions()
