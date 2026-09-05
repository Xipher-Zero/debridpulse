"""WS2 P1 completion, contradiction, and artifact-local failover qualification."""
from dataclasses import replace

import pytest

from providers.general_http.provider import GeneralHttpProvider
from test_ws2p1_failover_depth import fail_current, remote_failure
from test_ws2p1_failover_progress import (
    MultiUnknownProvider,
    NoProgressMemoryExecutor,
    RuntimeHttpExecutor,
    build_engine,
)
from transfers.models import ExecutionState, TransferProgress, TransferRequest, TransferState


@pytest.mark.asyncio
async def test_unknown_size_http_progress_advances_live_then_completes_normally(tmp_path, monkeypatch):
    provider = GeneralHttpProvider()
    executor = RuntimeHttpExecutor(total=10, completed=2)
    engine, repository, _registry = await build_engine(tmp_path, monkeypatch, (provider,), executor)
    transfer = await engine.submit(
        (TransferRequest("https", "https://example.test/live.bin", name="live.bin"),),
        deduplicate=False,
    )

    await engine.tick()
    artifact = (await repository.artifacts(transfer.id))[0]
    assert artifact.candidates[0].expected_bytes == 0
    assert artifact.expected_bytes == 10
    assert (await repository.get(transfer.id)).progress == 20.0

    executor.jobs[artifact.execution.attempt_id] = replace(
        executor.jobs[artifact.execution.attempt_id],
        progress=TransferProgress(10, 6, 3),
    )
    await engine.reconcile_executions()
    assert (await repository.get(transfer.id)).progress == 60.0
    active = await repository.presentation(transfer.id, details=True)
    assert active["files"][0]["progress"] == 60.0

    artifact = (await repository.artifacts(transfer.id))[0]
    executor.finish(artifact.execution)
    await engine.reconcile_executions()
    completed = await repository.get(transfer.id)
    assert completed.state == TransferState.COMPLETED
    assert completed.progress == 100.0
    final_view = await repository.presentation(transfer.id, details=True)
    assert final_view["files"][0]["progress"] == 100
    assert final_view["files"][0]["size_bytes"] == 10


@pytest.mark.asyncio
async def test_contradictory_active_runtime_total_cannot_rewrite_accepted_denominator(tmp_path, monkeypatch):
    provider = GeneralHttpProvider()
    executor = RuntimeHttpExecutor(total=10, completed=2)
    engine, repository, _registry = await build_engine(tmp_path, monkeypatch, (provider,), executor)
    transfer = await engine.submit(
        (TransferRequest("https", "https://example.test/conflict.bin", name="conflict.bin"),),
        deduplicate=False,
    )
    await engine.tick()
    artifact = (await repository.artifacts(transfer.id))[0]
    assert artifact.expected_bytes == 10

    executor.jobs[artifact.execution.attempt_id] = replace(
        executor.jobs[artifact.execution.attempt_id],
        state=ExecutionState.TRANSFERRING,
        progress=TransferProgress(12, 6, 2),
    )
    await engine.reconcile_executions()

    current = (await repository.artifacts(transfer.id))[0]
    assert current.expected_bytes == 10
    assert (await repository.get(transfer.id)).progress == 60.0
    details = await repository.presentation(transfer.id, details=True)
    assert details["files"][0]["size_bytes"] == 10
    assert details["files"][0]["progress"] == 60.0


@pytest.mark.asyncio
async def test_multiartifact_failover_is_artifact_local(tmp_path, monkeypatch):
    provider = MultiUnknownProvider()
    executor = NoProgressMemoryExecutor(None)
    engine, repository, _registry = await build_engine(tmp_path, monkeypatch, (provider,), executor)
    transfer = await engine.submit(
        (
            TransferRequest("multi", "one", name="one.bin"),
            TransferRequest("multi", "two", name="two.bin"),
        ),
        name="two-artifacts",
        deduplicate=False,
    )
    await engine.tick()
    artifacts = await repository.artifacts(transfer.id)
    assert len(artifacts) == 2
    first, second = artifacts
    second_handle = second.execution
    assert first.execution is not None and second_handle is not None

    failure = remote_failure()
    _first_attempt, first_after = await fail_current(engine, repository, executor, transfer.id, failure)
    # fail_current addresses the first artifact returned by repository order.
    assert first_after.id == first.id
    await engine.reconcile_executions()
    first_retry = (await repository.artifacts(transfer.id))[0]
    executor.jobs[first_retry.execution.attempt_id] = replace(
        executor.jobs[first_retry.execution.attempt_id], state=ExecutionState.FAILED, error=failure,
    )
    await engine.reconcile_executions()

    current = await repository.artifacts(transfer.id)
    first_current = next(item for item in current if item.id == first.id)
    second_current = next(item for item in current if item.id == second.id)
    assert first_current.selected == 1
    assert first_current.execution is None
    assert second_current.selected == 0
    assert second_current.execution == second_handle
    assert second_current.state == "downloading"
