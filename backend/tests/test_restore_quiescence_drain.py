"""Restore quiescence: the generic native-execution drain before a state swap.

A restore may replace the database only once no native execution owned by the
pre-restore state is alive. The drain is the canonical lifecycle, not a
restore-specific execution manager: durable global pause (new admission
stops), then every remaining writer -- a parked one included -- goes through
the ONE writer retirement (quiesce, forced checkpoint, fence) and is detached
as paused. Logical transfers are never rewritten as cancelled to get there.
"""
from __future__ import annotations

import pytest

from application.service import ApplicationService
from executor_fakes import LedgerExecutor, artifact_of, ledger_capabilities, ledger_core, submit_ledger
from transfers.models import ContinuationCapability, ExecutionState, TransferState

pytestmark = pytest.mark.asyncio

PARKING = frozenset({ContinuationCapability.FULL_RESTART, ContinuationCapability.NATIVE_PRIVATE_RESUME,
                     ContinuationCapability.NATIVE_QUIESCE})


async def _running(core, payload="ledger-item"):
    transfer = await submit_ledger(core, payload)
    artifact = await artifact_of(core, transfer.id)
    core.executor.run(artifact.execution)
    await core.engine.reconcile_executions()
    return transfer, await artifact_of(core, transfer.id)


async def _parking_core(tmp_path, monkeypatch):
    return await ledger_core(tmp_path, monkeypatch, executors=lambda authorize: (
        LedgerExecutor(authorize, capabilities=ledger_capabilities(continuation=PARKING)),))


async def test_drain_releases_parked_writers_that_pause_alone_leaves_alive(tmp_path, monkeypatch):
    core = await _parking_core(tmp_path, monkeypatch)
    first, first_artifact = await _running(core, "one")
    second, second_artifact = await _running(core, "two")
    service = ApplicationService(core.engine)

    report = await service.drain_executions()

    assert report["live_before"] == 2
    assert report["live_after"] == 0
    assert await core.repository.live_executions() == ()
    # Each native job was stopped by its own executor's cancel -- the parked
    # job that Pause alone would have kept alive is gone too.
    for artifact in (first_artifact, second_artifact):
        assert core.executor.job_for(artifact.execution).state == ExecutionState.CANCELLED
        assert ("cancel", artifact.execution.attempt_id) in core.executor.calls


async def test_drain_never_cancels_the_logical_transfer(tmp_path, monkeypatch):
    core = await _parking_core(tmp_path, monkeypatch)
    transfer, artifact = await _running(core)
    service = ApplicationService(core.engine)

    await service.drain_executions()

    current = await core.repository.get(transfer.id)
    assert current.state != TransferState.CANCELLED
    assert current.paused
    assert await core.repository.globally_paused()
    released = await artifact_of(core, transfer.id)
    assert released.state == "paused"
    assert released.execution is None
    # Recoverable: the ordinary global Resume admits a fresh writer.
    await core.engine.resume_all()
    await core.engine.tick()
    resumed = await artifact_of(core, transfer.id)
    assert resumed.execution is not None
    assert resumed.execution.attempt_id != artifact.execution.attempt_id


async def test_drain_stops_new_execution_admission(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    await _running(core)
    service = ApplicationService(core.engine)
    await service.drain_executions()
    starts = len([call for call in core.executor.calls if call[0] == "start"])

    await submit_ledger(core, "late-arrival")
    await core.engine.tick()

    assert len([call for call in core.executor.calls if call[0] == "start"]) == starts
    assert await core.repository.live_executions() == ()


async def test_drain_refuses_when_a_native_stop_cannot_be_proven(tmp_path, monkeypatch):
    core = await _parking_core(tmp_path, monkeypatch)
    transfer, artifact = await _running(core)
    core.executor.cancel_mode = "unconfirmed"
    service = ApplicationService(core.engine)

    with pytest.raises(RuntimeError):
        await service.drain_executions()

    # The writer is still owned: nothing may cross a state swap while it lives.
    assert [item.handle for item in await core.repository.live_executions()] == [artifact.execution]
    assert (await core.repository.get(transfer.id)).state != TransferState.CANCELLED
