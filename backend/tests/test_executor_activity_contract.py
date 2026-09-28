"""Neutral lifecycle, activity facts, dynamic controls and cancellation truth.

Lifecycle state says only whether executor work is active; network activity,
bandwidth reservation and stall expectations are independent executor facts.
Controls available *now* come from the current observation. A cancel
acknowledgement is never proof the native writer stopped.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from executor_fakes import LedgerExecutor, artifact_of, ledger_capabilities, ledger_core, submit_ledger
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage
from transfers.models import (
    ContinuationCapability, ExecutionActivity, ExecutionControl, ExecutionObservation, ExecutionState,
)
from transfers.policy import TransferPolicy

pytestmark = pytest.mark.asyncio


def _stall_policy():
    return TransferPolicy(retry_delay=1, adoption_stability_seconds=0, max_active_executions=3,
                          stalled_after_seconds=10)


async def _running(core, **kwargs):
    transfer = await submit_ledger(core)
    artifact = await artifact_of(core, transfer.id)
    core.executor.run(artifact.execution, **kwargs)
    await core.engine.reconcile_executions()
    return transfer, await artifact_of(core, transfer.id)


async def test_running_execution_can_report_no_network_activity(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer, artifact = await _running(core, network_active=False)
    observation = (await core.engine._observe_execution(core.executor, artifact.execution))
    assert observation.state == ExecutionState.RUNNING and observation.activity.network_active is False
    assert artifact.state == "downloading"
    assert (await core.repository.executions(transfer.id))[0].state == "running"


async def test_running_execution_can_report_no_progress_expected(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    _transfer, artifact = await _running(core, progress_expected=False)
    observation = await core.engine._observe_execution(core.executor, artifact.execution)
    assert observation.state == ExecutionState.RUNNING
    assert observation.activity == ExecutionActivity(True, True, False)
    assert await core.repository.execution_idle_seconds(observation, core.now[0] + 1000) == 0


async def test_no_progress_expected_does_not_trigger_stall_timeout(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch, policy=_stall_policy())
    transfer, artifact = await _running(core, progress_expected=False, completed=1)
    for _ in range(4):
        core.now[0] += 60
        await core.engine.reconcile_executions()
    assert ("cancel", artifact.execution.attempt_id) not in core.executor.calls
    assert (await artifact_of(core, transfer.id)).execution.attempt_id == artifact.execution.attempt_id

    # The same unchanged byte count with progress expected IS a stall.
    core.executor.run(artifact.execution, progress_expected=True, completed=1)
    for _ in range(3):
        core.now[0] += 60
        await core.engine.reconcile_executions()
    assert ("cancel", artifact.execution.attempt_id) in core.executor.calls


async def test_network_active_does_not_itself_decide_core_lifecycle(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer, artifact = await _running(core, network_active=True)
    job = core.executor.job_for(artifact.execution)
    job.state = ExecutionState.PAUSED
    job.controls = frozenset({ExecutionControl.RESUME})
    job.activity = ExecutionActivity(network_active=True, bandwidth_reservation_required=True)
    await core.engine.pause(transfer.id)
    assert (await artifact_of(core, transfer.id)).state == "paused"
    assert not [call for call in core.executor.calls if call[0] == "pause"]  # lifecycle already says paused


async def test_batch_observation_failure_yields_unknown_not_absent(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer, artifact = await _running(core)
    core.executor.observe_failure = NormalizedError(Domain.EXECUTOR, Category.EXECUTOR_UNAVAILABLE,
                                                    Stage.RECONCILIATION, retryability=Retryability.BACKOFF,
                                                    integration_id="ledger-copy")
    await core.engine.reconcile_executions()
    current = await artifact_of(core, transfer.id)
    assert current.state == "unknown" and current.execution.attempt_id == artifact.execution.attempt_id
    assert (await core.repository.executions(transfer.id))[0].state == "unknown"
    assert await core.repository.recovery_budget(artifact.id) == (0, 0)


async def test_one_batch_observation_call_per_executor_per_reconcile_cycle(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    for index in range(3):
        await submit_ledger(core, f"item-{index}")
    core.executor.calls.clear()
    await core.engine.reconcile_executions()
    batches = [call for call in core.executor.calls if call[0] == "observe_many"]
    assert len(batches) == 1 and len(batches[0][1]) == 3


QUIESCE = frozenset({ContinuationCapability.FULL_RESTART, ContinuationCapability.NATIVE_QUIESCE})


def _cancels(core):
    return [call for call in core.executor.calls if call[0] == "cancel"]


def _starts(core):
    return [call for call in core.executor.calls if call[0] == "start"]


async def test_native_quiesce_is_used_only_when_the_current_observation_advertises_pause(tmp_path, monkeypatch):
    """DP 1.0.13: the writer is fenced by DebridPulse Pause either way; a
    native pause is only the optional graceful quiesce before it, invoked
    only while the current observation offers it."""
    core = await ledger_core(tmp_path, monkeypatch, executors=lambda authorize: (
        LedgerExecutor(authorize, capabilities=ledger_capabilities(continuation=QUIESCE)),))
    transfer, artifact = await _running(core)
    core.executor.job_for(artifact.execution).controls = frozenset()
    await core.engine.pause(transfer.id)
    assert not [call for call in core.executor.calls if call[0] == "pause"]
    assert _cancels(core) == [("cancel", artifact.execution.attempt_id)]
    assert (await artifact_of(core, transfer.id)).execution is None

    second = await submit_ledger(core, "second")
    running = await artifact_of(core, second.id)
    core.executor.run(running.execution)
    await core.engine.pause(second.id)
    assert [call for call in core.executor.calls if call[0] == "pause"] == [("pause", running.execution.attempt_id)]
    assert (await artifact_of(core, second.id)).execution is None


async def test_resume_is_a_new_writer_never_a_native_resume(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer, artifact = await _running(core)
    await core.engine.pause(transfer.id)
    job = core.executor.job_for(artifact.execution)
    job.controls = frozenset({ExecutionControl.RESUME})
    starts = len(_starts(core))
    await core.engine.resume(transfer.id)
    await core.engine.reconcile_executions()
    assert not [call for call in core.executor.calls if call[0] == "resume"]
    assert len(_starts(core)) == starts + 1
    assert (await artifact_of(core, transfer.id)).execution.attempt_id != artifact.execution.attempt_id


async def test_a_writer_whose_stop_cannot_yet_be_proven_stays_owned_until_it_can(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer, artifact = await _running(core)
    job = core.executor.job_for(artifact.execution)
    core.executor.cancel_mode = "unconfirmed"
    await core.engine.pause(transfer.id)
    await core.engine.reconcile_executions()
    current = await artifact_of(core, transfer.id)
    assert current.execution is not None  # still observable and owned, never guessed stopped
    core.executor.cancel_mode = "confirm"
    await core.engine.reconcile_executions()
    assert job.state == ExecutionState.CANCELLED
    assert (await artifact_of(core, transfer.id)).execution is None


async def test_executor_without_static_pause_capability_is_never_asked_to_pause(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch, executors=lambda authorize: (
        LedgerExecutor(authorize, capabilities=ledger_capabilities(per_execution_pause=False)),))
    transfer, artifact = await _running(core)
    await core.engine.pause(transfer.id)
    await core.engine.reconcile_executions()
    assert not [call for call in core.executor.calls if call[0] in {"pause", "resume"}]


async def test_scheduler_and_operator_control_still_share_one_convergence_owner(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer, artifact = await _running(core)
    await core.engine.pause(transfer.id)
    core.executor.calls.clear()
    scheduler = asyncio.create_task(core.engine._process_executions(
        transfer.id, (await artifact_of(core, transfer.id),), {}))
    explicit = asyncio.create_task(core.engine.resume(transfer.id))
    await asyncio.gather(scheduler, explicit)
    await core.engine.reconcile_executions()
    assert len(_starts(core)) == 1  # one owner admits exactly one new writer
    assert not [call for call in core.executor.calls if call[0] == "resume"]


async def test_pause_arriving_during_resume_still_wins(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    class Blocking(LedgerExecutor):
        async def start(self, request, handle):
            if self.block:
                entered.set()
                await release.wait()
            return await super().start(request, handle)

    core = await ledger_core(tmp_path, monkeypatch, executors=lambda authorize: (Blocking(authorize),))
    core.executor.block = False
    transfer, artifact = await _running(core)
    await core.engine.pause(transfer.id)
    core.executor.block = True
    resuming = asyncio.create_task(core.engine.resume(transfer.id))
    await entered.wait()
    pausing = asyncio.create_task(core.engine.pause(transfer.id))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(resuming, pausing)
    await core.engine.reconcile_executions()
    assert (await core.repository.get(transfer.id)).paused
    assert (await artifact_of(core, transfer.id)).execution is None
    assert not [item for item in await core.repository.live_executions() if item.transfer_id == transfer.id]


async def test_rapid_repeated_control_remains_idempotent(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer, artifact = await _running(core)
    starts = len(_starts(core))
    await asyncio.gather(*(core.engine.pause(transfer.id) for _ in range(3)))
    await asyncio.gather(*(core.engine.resume(transfer.id) for _ in range(3)))
    await core.engine.reconcile_executions()
    assert len(_cancels(core)) == 1
    assert len(_starts(core)) == starts + 1


async def test_global_acquisition_gate_blocks_network_start_before_new_admission(tmp_path, monkeypatch):
    log = []
    core = await ledger_core(tmp_path, monkeypatch, executors=lambda authorize: (
        LedgerExecutor(authorize, capabilities=ledger_capabilities(acquisition_gate=True), log=log),))
    await _running(core)
    await core.engine.pause_all()
    assert core.executor.gate == [True]
    assert await core.repository.globally_paused()  # durable intent recorded before the executor gate
    log.clear()
    waiting = await core.engine.submit((__import__("transfers.models", fromlist=["TransferRequest"]).TransferRequest(
        "ledger", "new-item", name="new-item"),))
    await core.engine.tick()
    assert (core.executor.descriptor.id, "start") not in log  # core blocks new admission itself
    await core.engine.resume_all()
    assert core.executor.gate == [True, False]
    gate_index = log.index((core.executor.descriptor.id, "gate", False))
    starts = [index for index, entry in enumerate(log) if entry == (core.executor.descriptor.id, "start")]
    assert all(index > gate_index for index in starts)
    del waiting


async def test_acquisition_gate_does_not_claim_full_database_wipe_quiescence(tmp_path, monkeypatch):
    from application.service import ApplicationService

    core = await ledger_core(tmp_path, monkeypatch, executors=lambda authorize: (
        LedgerExecutor(authorize, capabilities=ledger_capabilities(acquisition_gate=True)),))
    _transfer, artifact = await _running(core)
    core.executor.cancel_mode = "unconfirmed"  # the writer's stop cannot be proven now
    service = ApplicationService(core.engine)
    with pytest.raises(RuntimeError):
        await service.quiesce_for_database_wipe()
    assert core.executor.gate == [True]  # the gate engaged, yet it is not proof of quiescence


async def test_cancel_ack_without_terminal_truth_remains_unknown(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer, artifact = await _running(core)
    core.executor.cancel_mode = "unconfirmed"
    with pytest.raises(Exception):
        await core.engine.cancel_artifact(transfer.id, artifact.id)
    current = await artifact_of(core, transfer.id)
    assert current.state != "cancelled"
    assert current.execution.attempt_id == artifact.execution.attempt_id
    attempt = (await core.repository.executions(transfer.id))[0]
    assert attempt.state == "unknown"
    assert await core.repository.authorize_execution(current.execution, "observe")


async def test_lost_cancel_ack_reconciles_terminal_truth_without_second_cancel_storm(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer, artifact = await _running(core)
    core.executor.cancel_mode = "lost_ack"
    await core.engine.cancel(transfer.id)
    first = [call for call in core.executor.calls if call[0] == "cancel"]
    assert len(first) == 1
    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert status["state"] in {"pending", "blocked"}
    core.now[0] += 3600
    await core.engine.reconcile_executions()
    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert status["state"] == "complete"
    assert len([call for call in core.executor.calls if call[0] == "cancel"]) == 1  # observation proved the stop


async def test_uncertain_cancel_retains_bandwidth_reservation_and_cleanup_authority(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch, executors=lambda authorize: (
        LedgerExecutor(authorize, capabilities=ledger_capabilities(aggregate_bandwidth_ceiling=True)),))
    core.engine.configure_runtime_limits(30_000_000)
    transfer, artifact = await _running(core)
    assert core.engine.runtime.reserved == frozenset({"ledger-copy"})
    core.executor.cancel_mode = "unconfirmed"
    await core.engine.cancel(transfer.id)
    await core.engine.reconcile_executions()
    assert core.engine.runtime.reserved == frozenset({"ledger-copy"})
    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert status["state"] in {"pending", "blocked"}
    core.executor.cancel_mode = "confirm"
    core.now[0] += 3600
    await core.engine.reconcile_executions()
    assert (await core.repository.execution_cleanup_status(artifact.execution.attempt_id))["state"] == "complete"
    await core.engine.reconcile_executions()
    assert core.engine.runtime.reserved == frozenset()


async def test_cancel_contract_returns_an_observation_not_an_acknowledgement():
    from transfers.contracts import Executor
    annotation = inspect.signature(Executor.cancel).return_annotation
    assert "ExecutionObservation" in str(annotation)
    assert "TransferOutcome" not in str(annotation)


async def test_pause_that_cannot_be_confirmed_is_reported_not_assumed(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer, artifact = await _running(core)
    core.executor.cancel_mode = "unconfirmed"
    errors = await core.engine.pause(transfer.id)
    assert errors and errors[0].category.value == "reconciliation_failed"
    assert (await core.repository.get(transfer.id)).paused  # the durable intent stands
    core.executor.cancel_mode = "confirm"
    assert await core.engine.pause(transfer.id) == ()


async def test_engaged_acquisition_gate_covers_global_pause_of_an_unpausable_execution(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch, executors=lambda authorize: (
        LedgerExecutor(authorize, capabilities=ledger_capabilities(acquisition_gate=True)),))
    _transfer, artifact = await _running(core)
    core.executor.job_for(artifact.execution).controls = frozenset()
    results = await core.engine.pause_all()
    assert core.executor.gate == [True]
    assert all(errors == () for errors in results.values())


# -- Native parking across Pause (parks_on_pause: quiesce + private resume) --

PARKING = frozenset({ContinuationCapability.FULL_RESTART, ContinuationCapability.NATIVE_PRIVATE_RESUME,
                     ContinuationCapability.NATIVE_QUIESCE})


async def _parking_core(tmp_path, monkeypatch):
    return await ledger_core(tmp_path, monkeypatch, executors=lambda authorize: (
        LedgerExecutor(authorize, capabilities=ledger_capabilities(continuation=PARKING)),))


async def test_a_parked_collection_job_has_no_progress_authority_while_paused(tmp_path, monkeypatch):
    core = await _parking_core(tmp_path, monkeypatch)
    transfer = await submit_ledger(core, "bundle:collection")
    artifact = await artifact_of(core, transfer.id)
    core.executor.run(artifact.execution)
    await core.engine.reconcile_executions()
    assert await core.engine.pause(transfer.id) == ()
    job = core.executor.job_for(artifact.execution)
    parked = await artifact_of(core, transfer.id)
    # Parked, not fenced: the native job keeps its private progress...
    assert parked.execution == artifact.execution and job.state == ExecutionState.PAUSED
    assert not _cancels(core)
    # ...but no acquisition mutation is authorized while the intent stands.
    assert not await core.repository.authorize_execution(artifact.execution, "resume")
    assert not await core.repository.authorize_execution(artifact.execution, "start")
    for _ in range(3):
        await core.engine.reconcile_executions()
    assert job.state == ExecutionState.PAUSED
    assert not [call for call in core.executor.calls if call[0] == "resume"]
    # A parked job observed acquiring again (e.g. resumed natively outside DP)
    # is re-quiesced by the one writer retirement, never left making progress.
    job.state, job.controls = ExecutionState.RUNNING, frozenset({ExecutionControl.PAUSE})
    await core.engine.reconcile_executions()
    assert job.state in {ExecutionState.PAUSED, ExecutionState.CANCELLED}
    assert (await core.repository.get(transfer.id)).paused


async def test_only_resume_through_the_canonical_lifecycle_continues_a_parked_job(tmp_path, monkeypatch):
    core = await _parking_core(tmp_path, monkeypatch)
    transfer = await submit_ledger(core, "bundle:collection")
    artifact = await artifact_of(core, transfer.id)
    core.executor.run(artifact.execution)
    await core.engine.reconcile_executions()
    await core.engine.pause(transfer.id)
    starts = len(_starts(core))
    await core.engine.resume(transfer.id)
    await core.engine.reconcile_executions()
    # Resume cleared the durable intent first; the canonical convergence
    # owner then continued the parked job under ordinary admission.
    assert not (await core.repository.get(transfer.id)).paused
    assert [call for call in core.executor.calls if call[0] == "resume"] == [("resume", artifact.execution.attempt_id)]
    assert core.executor.job_for(artifact.execution).state == ExecutionState.RUNNING
    assert len(_starts(core)) == starts


async def test_a_file_artifact_parks_when_its_executor_resumes_its_own_quiesced_job(tmp_path, monkeypatch):
    core = await _parking_core(tmp_path, monkeypatch)
    transfer, artifact = await _running(core)  # an ordinary FILE artifact
    await core.engine.pause(transfer.id)
    # Parking is capability-driven, never materialization-driven.
    parked = await artifact_of(core, transfer.id)
    assert parked.execution == artifact.execution and not _cancels(core)
    assert core.executor.job_for(artifact.execution).state == ExecutionState.PAUSED
    assert not await core.repository.authorize_execution(artifact.execution, "resume")


async def test_a_file_executor_without_native_private_resume_still_retires_on_pause(tmp_path, monkeypatch):
    quiesce_only = frozenset({ContinuationCapability.FULL_RESTART, ContinuationCapability.NATIVE_QUIESCE})
    core = await ledger_core(tmp_path, monkeypatch, executors=lambda authorize: (
        LedgerExecutor(authorize, capabilities=ledger_capabilities(continuation=quiesce_only)),))
    transfer, artifact = await _running(core)
    await core.engine.pause(transfer.id)
    # Quiesce alone is only an optimization of the fence: no writer remains.
    assert (await artifact_of(core, transfer.id)).execution is None
    assert _cancels(core) == [("cancel", artifact.execution.attempt_id)]
    assert not [item for item in await core.repository.live_executions() if item.transfer_id == transfer.id]
