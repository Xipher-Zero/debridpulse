"""Phase 3 unified recovery execution, fencing, target, and trigger contracts."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.convergence_engine import TransferEngine
from transfers.errors import (
    Category, Domain, NormalizedError, Origin, Retryability, Stage,
)
from transfers.models import (
    ExecutionObservation, ExecutionState, ResolutionResult, ResourceState,
    TransferRequest,
)
from transfers.policy import RecoveryAction, RecoveryDecision, TransferPolicy
from transfers.recovery_execution import RecoveryTrigger
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry


@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "phase3.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider()
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    now = [7000.0]
    engine = TransferEngine(
        repository,
        registry,
        download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(
            retry_delay=1,
            adoption_stability_seconds=0,
            max_active_executions=4,
        ),
        clock=lambda: now[0],
    )
    await engine.initialize()
    return repository, registry, provider, executor, engine, now


async def materialize_only(runtime, candidates=None):
    repository, _registry, provider, _executor, engine, _now = runtime
    if candidates is not None:
        provider.responses = [ResolutionResult(ResourceState.AVAILABLE, tuple(candidates))]
    transfer = await engine.submit((TransferRequest("parcel", "box", name="payload.bin"),))
    await engine.resolve_pending()
    artifact = (await repository.artifacts(transfer.id))[0]
    assert artifact.execution is None
    return transfer, artifact


async def start_transfer(runtime):
    repository, _registry, _provider, _executor, engine, _now = runtime
    transfer, artifact = await materialize_only(runtime)
    await engine.reconcile_executions()
    artifact = (await repository.artifacts(transfer.id))[0]
    assert artifact.execution is not None
    return transfer, artifact


def transient_error():
    return NormalizedError(
        Domain.NETWORK,
        Category.REMOTE_READ_FAILED,
        Stage.EXECUTION,
        retryability=Retryability.BACKOFF,
        origin=Origin.REMOTE_SOURCE,
        integration_id="memory-copy",
    )


def test_all_required_triggers_are_canonical():
    assert {item.value for item in RecoveryTrigger} == {
        "auto_retry",
        "user_retry",
        "resume",
        "startup_reconcile",
        "provider_recovery",
        "executor_recovery",
    }


def test_production_composition_uses_phase3_owners():
    composition = Path(__file__).parents[1] / "application" / "composition.py"
    text = composition.read_text(encoding="utf-8")
    assert "from transfers.convergence_engine import TransferEngine" in text
    assert "from transfers.recovery_repository import TransferRepository" in text
    assert "from transfers.engine import TransferEngine" not in text
    assert "from transfers.manual_repository import TransferRepository" not in text


def test_phase3_trigger_adapters_have_one_recovery_entry_and_no_partial_retirement():
    transfers = Path(__file__).parents[1] / "transfers"
    public = (transfers / "convergence_engine.py").read_text(encoding="utf-8")
    coordinator = (transfers / "_convergence_phase3_public_base.py").read_text(encoding="utf-8")
    adapters = (transfers / "_convergence_phase3_base.py").read_text(encoding="utf-8")
    combined = public + coordinator + adapters
    assert "retire_partial" not in combined
    assert "async def recover_artifact" in coordinator
    for trigger in RecoveryTrigger:
        assert f"RecoveryTrigger.{trigger.name}" in combined
    for owner in ("retry", "resume", "reconcile_executions", "_wake_quiescent_recoveries"):
        assert f"async def {owner}" in combined
    assert "recover_artifact(" in public
    assert "recover_artifact(" in adapters


@pytest.mark.asyncio
async def test_recovery_claim_is_durable_exclusive_and_operator_does_not_steal(runtime):
    repository, _registry, _provider, _executor, _engine, now = runtime
    _transfer, artifact = await materialize_only(runtime)
    auto = await repository.claim_recovery(
        artifact.id, RecoveryTrigger.AUTO_RETRY, now[0], lease_seconds=60,
    )
    assert auto is not None
    assert await repository.recovery_claim_current(auto, now=now[0])
    assert await repository.claim_recovery(
        artifact.id, RecoveryTrigger.STARTUP_RECONCILE, now[0], lease_seconds=60,
    ) is None
    assert await repository.claim_recovery(
        artifact.id, RecoveryTrigger.USER_RETRY, now[0], lease_seconds=60,
    ) is None

    assert await repository.finish_recovery_claim(
        auto, action="reconcile", reason="test", outcome="complete",
    )
    user = await repository.claim_recovery(
        artifact.id, RecoveryTrigger.USER_RETRY, now[0], lease_seconds=60,
    )
    assert user is not None
    assert user.generation > auto.generation
    assert await repository.finish_recovery_claim(
        user, action="reconcile", reason="test-user", outcome="complete",
    )


@pytest.mark.asyncio
async def test_expired_claim_can_be_recovered_but_stale_owner_cannot_commit(runtime):
    repository, _registry, _provider, _executor, _engine, now = runtime
    _transfer, artifact = await materialize_only(runtime)
    first = await repository.claim_recovery(
        artifact.id, RecoveryTrigger.AUTO_RETRY, now[0], lease_seconds=1,
    )
    assert first is not None
    now[0] += 2
    second = await repository.claim_recovery(
        artifact.id, RecoveryTrigger.STARTUP_RECONCILE, now[0], lease_seconds=60,
    )
    assert second is not None
    assert second.generation > first.generation
    assert not await repository.recovery_claim_current(first)
    assert not await repository.finish_recovery_claim(
        first, action="backoff", reason="stale", outcome="must-not-commit",
    )
    assert await repository.finish_recovery_claim(
        second, action="reconcile", reason="current", outcome="complete",
    )


@pytest.mark.asyncio
async def test_duplicate_failure_observation_consumes_budget_once(runtime):
    repository, _registry, _provider, _executor, _engine, _now = runtime
    _transfer, artifact = await materialize_only(runtime)
    error = transient_error()
    first = await repository.record_source_failure_once(
        artifact.id, error, "attempt:0:network",
    )
    second = await repository.record_source_failure_once(
        artifact.id, error, "attempt:0:network",
    )
    assert first[2] is True
    assert second[2] is False
    assert await repository.recovery_budget(artifact.id) == (1, 0)
    context = await repository.recovery_context(artifact.id)
    assert context["last_budget_before"] == {"failures": 0, "refreshes": 0}
    assert context["last_budget_after"] == {"failures": 1, "refreshes": 0}


@pytest.mark.asyncio
async def test_pause_fences_stale_recovery_claim_and_preserves_target(runtime):
    repository, _registry, _provider, _executor, engine, now = runtime
    transfer, artifact = await materialize_only(runtime)
    target = artifact.target
    claim = await repository.claim_recovery(
        artifact.id, RecoveryTrigger.AUTO_RETRY, now[0], lease_seconds=60,
    )
    assert claim is not None
    await repository.set_pause_and_fence(transfer.id, True)
    assert not await repository.recovery_claim_current(claim)
    current = (await repository.artifacts(transfer.id))[0]
    assert current.target == target
    await engine.pause(transfer.id)
    current = (await repository.artifacts(transfer.id))[0]
    assert current.target == target


@pytest.mark.asyncio
async def test_provider_disable_reenable_reuses_same_execution_and_lifecycle_wait(runtime):
    repository, _registry, provider, executor, engine, _now = runtime
    transfer, artifact = await start_transfer(runtime)
    original = artifact.execution
    start_calls = len([call for call in executor.calls if call[0] == "start"])

    provider.descriptor = replace(provider.descriptor, enabled=False)
    await engine.reconcile_executions()
    parked = (await repository.artifacts(transfer.id))[0]
    context = await repository.recovery_context(parked.id)
    assert parked.execution == original
    assert parked.state == "recovery_wait"
    assert context["quiescence_reason"] == "provider_disabled"
    assert await repository.recovery_budget(parked.id) == (0, 0)

    provider.descriptor = replace(provider.descriptor, enabled=True)
    await engine.reconcile_executions()
    resumed = (await repository.artifacts(transfer.id))[0]
    context = await repository.recovery_context(resumed.id)
    assert resumed.execution == original
    assert resumed.state == "downloading"
    assert len([call for call in executor.calls if call[0] == "start"]) == start_calls
    assert context["last_applied_trigger"] == RecoveryTrigger.PROVIDER_RECOVERY.value


@pytest.mark.asyncio
async def test_provider_disable_during_backoff_preserves_retry_eligibility(runtime):
    repository, _registry, provider, executor, engine, now = runtime
    executor.start_errors = [transient_error()]
    transfer, _artifact = await materialize_only(runtime)
    await engine.reconcile_executions()
    backed_off = (await repository.artifacts(transfer.id))[0]
    context = await repository.recovery_context(backed_off.id)
    assert context["quiescence_reason"] == "retry_backoff"
    retry_at = backed_off.retry_at
    assert retry_at > now[0]

    provider.descriptor = replace(provider.descriptor, enabled=False)
    await engine.reconcile_executions()
    parked = (await repository.artifacts(transfer.id))[0]
    context = await repository.recovery_context(parked.id)
    assert context["quiescence_reason"] == "provider_disabled"
    assert context["blocked_retry_at"] == retry_at

    provider.descriptor = replace(provider.descriptor, enabled=True)
    await engine.reconcile_executions()
    still_waiting = (await repository.artifacts(transfer.id))[0]
    context = await repository.recovery_context(still_waiting.id)
    assert context["quiescence_reason"] == "retry_backoff"
    assert still_waiting.execution is None

    now[0] = retry_at
    await engine.reconcile_executions()
    resumed = (await repository.artifacts(transfer.id))[0]
    assert resumed.execution is not None


@pytest.mark.asyncio
async def test_provider_reenable_while_user_paused_does_not_resume(runtime):
    repository, _registry, provider, executor, engine, _now = runtime
    transfer, artifact = await start_transfer(runtime)
    original = artifact.execution
    await engine.pause(transfer.id)
    provider.descriptor = replace(provider.descriptor, enabled=False)
    await engine.reconcile_executions()
    provider.descriptor = replace(provider.descriptor, enabled=True)
    resume_before = len([call for call in executor.calls if call[0] == "resume"])
    await engine.reconcile_executions()
    current = (await repository.artifacts(transfer.id))[0]
    assert current.execution == original
    assert current.state == "paused"
    assert len([call for call in executor.calls if call[0] == "resume"]) == resume_before


@pytest.mark.asyncio
async def test_executor_disappearance_and_return_reuses_surviving_gid(runtime):
    repository, registry, _provider, executor, engine, _now = runtime
    transfer, artifact = await start_transfer(runtime)
    original = artifact.execution
    start_calls = len([call for call in executor.calls if call[0] == "start"])

    registry.executors.pop(original.executor_id)
    await engine.reconcile_executions()
    parked = (await repository.artifacts(transfer.id))[0]
    context = await repository.recovery_context(parked.id)
    assert parked.execution == original
    assert parked.state == "recovery_wait"
    assert context["quiescence_reason"] == "executor_unavailable"
    assert await repository.recovery_budget(parked.id) == (0, 0)

    registry.register_executor(executor)
    await engine.reconcile_executions()
    recovered = (await repository.artifacts(transfer.id))[0]
    context = await repository.recovery_context(recovered.id)
    assert recovered.execution == original
    assert len([call for call in executor.calls if call[0] == "start"]) == start_calls
    assert context["last_applied_trigger"] == RecoveryTrigger.EXECUTOR_RECOVERY.value


@pytest.mark.asyncio
async def test_startup_reconciliation_reuses_surviving_execution(runtime):
    repository, registry, _provider, executor, engine, now = runtime
    transfer, artifact = await start_transfer(runtime)
    original = artifact.execution
    start_calls = len([call for call in executor.calls if call[0] == "start"])

    restarted = TransferEngine(
        TransferRepository(),
        registry,
        download_root=engine.root,
        policy=engine.policy,
        clock=lambda: now[0],
    )
    await restarted.initialize()
    await restarted.reconcile_executions()
    current = (await repository.artifacts(transfer.id))[0]
    context = await repository.recovery_context(current.id)
    assert current.execution == original
    assert len([call for call in executor.calls if call[0] == "start"]) == start_calls
    assert context["last_applied_trigger"] == RecoveryTrigger.STARTUP_RECONCILE.value


@pytest.mark.asyncio
async def test_startup_missing_gid_backs_off_then_reconstructs_once_on_same_target(runtime):
    repository, registry, _provider, executor, engine, now = runtime
    transfer, artifact = await start_transfer(runtime)
    original = artifact.execution
    target = artifact.target
    executor.jobs.pop(original.attempt_id)

    restarted = TransferEngine(
        TransferRepository(), registry, download_root=engine.root,
        policy=engine.policy, clock=lambda: now[0],
    )
    await restarted.initialize()
    await restarted.reconcile_executions()
    waiting = (await repository.artifacts(transfer.id))[0]
    assert waiting.execution is None
    assert waiting.target == target
    assert waiting.state == "recovery_wait"
    retry_at = waiting.retry_at
    assert retry_at > now[0]

    now[0] = retry_at
    await restarted.reconcile_executions()
    rebuilt = (await repository.artifacts(transfer.id))[0]
    assert rebuilt.execution is not None
    assert rebuilt.execution != original
    assert rebuilt.target == target
    assert len(await repository.executions(transfer.id)) == 2


@pytest.mark.asyncio
async def test_alternate_candidate_preserves_target_and_partial_bytes(runtime):
    repository, _registry, provider, _executor, engine, now = runtime
    first = provider.candidate("payload.bin", payload="first")
    second = provider.candidate("payload.bin", payload="second")
    transfer, artifact = await materialize_only(runtime, (first, second))
    target = Path(artifact.target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"partial")

    claim = await repository.claim_recovery(
        artifact.id, RecoveryTrigger.AUTO_RETRY, now[0], lease_seconds=60,
    )
    assert claim is not None
    decision = RecoveryDecision(
        RecoveryAction.TRY_ALTERNATE_CANDIDATE,
        "test_alternate",
        retry_at=now[0],
    )
    applied = await engine._apply_recovery_decision(
        claim,
        artifact,
        transient_error(),
        decision,
        decision_id="test-alternate",
        next_index=1,
    )
    assert applied
    assert await repository.record_phase3_application(
        claim,
        action=decision.action.value,
        reason=decision.reason,
        partial_preserved=True,
    )
    assert await repository.finish_recovery_claim(
        claim,
        action=decision.action.value,
        reason=decision.reason,
        outcome="applied",
        candidate_changed=True,
    )
    current = (await repository.artifacts(transfer.id))[0]
    context = await repository.recovery_context(current.id)
    assert current.target == artifact.target
    assert current.selected == 1
    assert target.read_bytes() == b"partial"
    assert " (2)" not in current.target
    assert context["candidate_generation"] == 1
    assert context["last_candidate_switch_reason"] == "test_alternate"


@pytest.mark.asyncio
async def test_existing_materialized_artifact_never_reallocates_target(runtime):
    repository, _registry, _provider, _executor, _engine, _now = runtime
    transfer, artifact = await materialize_only(runtime)
    record = next(item for item in await repository.requests(transfer.id) if item.id == artifact.request_id)
    alternate_target = str(Path(artifact.target).with_name("payload (2).bin"))
    rematerialized = await repository.materialize(record, artifact.candidates, alternate_target)
    assert rematerialized is not None
    current = (await repository.artifacts(transfer.id))[0]
    assert current.id == artifact.id
    assert current.target == artifact.target
    assert current.target != alternate_target


@pytest.mark.asyncio
async def test_refresh_reservation_and_attempt_are_single_flight(runtime):
    repository, _registry, provider, _executor, _engine, now = runtime
    transfer, artifact = await materialize_only(runtime)
    record = next(item for item in await repository.requests(transfer.id) if item.id == artifact.request_id)
    claim = await repository.claim_recovery(
        artifact.id, RecoveryTrigger.AUTO_RETRY, now[0], lease_seconds=60,
    )
    assert claim is not None
    decision_id = "refresh-decision"
    assert await repository.record_phase3_decision(
        claim,
        decision_id=decision_id,
        action=RecoveryAction.REFRESH_CANDIDATE.value,
        reason="test",
    )
    assert await repository.reserve_recovery_refresh(claim, decision_id)
    assert await repository.reserve_recovery_refresh(claim, decision_id)
    first = await repository.begin_recovery_refresh(
        claim, record, provider.descriptor.id, decision_id,
    )
    second = await repository.begin_recovery_refresh(
        claim, record, provider.descriptor.id, decision_id,
    )
    assert first is not None and second is not None
    assert first["created"] is True
    assert second["created"] is False
    assert first["attempt_id"] == second["attempt_id"]
    assert await repository.recovery_budget(artifact.id) == (0, 1)


@pytest.mark.asyncio
async def test_user_retry_clears_exhaustion_without_fabricating_progress(runtime):
    repository, _registry, _provider, _executor, engine, _now = runtime
    transfer, artifact = await materialize_only(runtime)
    error = transient_error()
    await repository.record_source_failure(artifact.id, error)
    await repository.record_source_failure(artifact.id, error)
    assert await repository.transition_recovery(
        artifact.id,
        "error",
        error=error,
        quiescence_reason="recovery_exhausted",
        wake_condition="operator_retry",
    )
    before = await repository.recovery_context(artifact.id)
    assert before["recovery_epoch"] == 0
    assert before["same_signature_failures"] == 2

    assert await engine.retry(transfer.id)
    after = await repository.recovery_context(artifact.id)
    assert after["recovery_epoch"] == 0
    assert after["consecutive_no_progress_failures"] == 0
    assert after["same_signature_failures"] == 0
    assert after["last_applied_trigger"] == RecoveryTrigger.USER_RETRY.value


@pytest.mark.asyncio
async def test_resume_does_not_reset_recovery_budget(runtime):
    repository, _registry, _provider, _executor, engine, _now = runtime
    transfer, artifact = await start_transfer(runtime)
    await repository.record_source_failure(artifact.id, transient_error())
    assert await repository.recovery_budget(artifact.id) == (1, 0)
    await engine.pause(transfer.id)
    await engine.resume(transfer.id)
    assert await repository.recovery_budget(artifact.id) == (1, 0)
    context = await repository.recovery_context(artifact.id)
    assert context["last_applied_trigger"] == RecoveryTrigger.RESUME.value


@pytest.mark.asyncio
async def test_recovery_provenance_separates_classification_decision_and_application(runtime):
    repository, _registry, _provider, _executor, engine, _now = runtime
    _transfer, artifact = await materialize_only(runtime)
    assert await engine.recover_artifact(
        artifact,
        trigger=RecoveryTrigger.AUTO_RETRY,
        error=transient_error(),
    )
    context = await repository.recovery_context(artifact.id)
    assert context["last_applied_trigger"] == RecoveryTrigger.AUTO_RETRY.value
    assert context["failure_classification"]["category"] == Category.REMOTE_READ_FAILED.value
    assert context["failure_classification"]["domain"] == Domain.NETWORK.value
    assert context["decision_action"] == RecoveryAction.BACKOFF.value
    assert context["decision_reason"]
    assert context["decision_recovery_epoch"] == context["recovery_epoch"]
    assert context["bytes_at_failure"] == 0
    assert context["durable_target"] == artifact.target
    assert context["last_budget_before"] == {"failures": 0, "refreshes": 0}
    assert context["last_budget_after"] == {"failures": 1, "refreshes": 0}


@pytest.mark.asyncio
async def test_two_simultaneous_user_retry_calls_coalesce(runtime):
    repository, _registry, _provider, executor, engine, _now = runtime
    transfer, artifact = await materialize_only(runtime)
    error = transient_error()
    await repository.record_source_failure(artifact.id, error)
    await repository.record_source_failure(artifact.id, error)
    assert await repository.transition_recovery(
        artifact.id,
        "error",
        error=error,
        quiescence_reason="recovery_exhausted",
        wake_condition="operator_retry",
    )
    results = await asyncio.gather(engine.retry(transfer.id), engine.retry(transfer.id))
    assert all(results)
    current = (await repository.artifacts(transfer.id))[0]
    assert current.execution is not None
    assert len(await repository.executions(transfer.id)) == 1
    assert len([call for call in executor.calls if call[0] == "start"]) == 1
    assert await repository.recovery_budget(artifact.id) == (0, 0)


@pytest.mark.asyncio
async def test_auto_retry_and_user_retry_race_create_at_most_one_execution(runtime):
    repository, _registry, _provider, executor, engine, _now = runtime
    transfer, artifact = await materialize_only(runtime)
    await asyncio.gather(
        engine.recover_artifact(artifact, trigger=RecoveryTrigger.AUTO_RETRY),
        engine.retry(transfer.id),
    )
    current = (await repository.artifacts(transfer.id))[0]
    attempts = await repository.executions(transfer.id)
    assert current.execution is not None
    assert len(attempts) == 1
    assert len([call for call in executor.calls if call[0] == "start"]) <= 1
    assert await repository.recovery_budget(artifact.id) == (0, 0)


@pytest.mark.asyncio
async def test_auto_retry_and_startup_reconcile_race_create_one_execution(runtime):
    repository, _registry, _provider, executor, engine, _now = runtime
    transfer, artifact = await materialize_only(runtime)
    await asyncio.gather(
        engine.recover_artifact(artifact, trigger=RecoveryTrigger.AUTO_RETRY),
        engine.recover_artifact(artifact, trigger=RecoveryTrigger.STARTUP_RECONCILE),
    )
    current = (await repository.artifacts(transfer.id))[0]
    assert current.execution is not None
    assert len(await repository.executions(transfer.id)) == 1
    assert len([call for call in executor.calls if call[0] == "start"]) == 1


@pytest.mark.asyncio
async def test_pause_and_auto_retry_race_pause_wins(runtime):
    repository, _registry, _provider, executor, engine, _now = runtime
    transfer, artifact = await materialize_only(runtime)
    await asyncio.gather(
        engine.recover_artifact(artifact, trigger=RecoveryTrigger.AUTO_RETRY),
        engine.pause(transfer.id),
    )
    current = (await repository.artifacts(transfer.id))[0]
    transfer_state = await repository.get(transfer.id)
    assert transfer_state.paused
    if current.execution is not None:
        observed = executor.jobs.get(current.execution.attempt_id)
        assert observed is None or observed.state == ExecutionState.PAUSED


@pytest.mark.asyncio
async def test_provider_recovery_and_auto_retry_race_honors_backoff(runtime):
    repository, _registry, provider, executor, engine, now = runtime
    executor.start_errors = [transient_error()]
    transfer, _artifact = await materialize_only(runtime)
    await engine.reconcile_executions()
    waiting = (await repository.artifacts(transfer.id))[0]
    retry_at = waiting.retry_at
    provider.descriptor = replace(provider.descriptor, enabled=False)
    await engine.reconcile_executions()
    provider.descriptor = replace(provider.descriptor, enabled=True)
    artifact = (await repository.artifacts(transfer.id))[0]
    await asyncio.gather(
        engine.recover_artifact(artifact, trigger=RecoveryTrigger.PROVIDER_RECOVERY),
        engine.recover_artifact(artifact, trigger=RecoveryTrigger.AUTO_RETRY),
    )
    current = (await repository.artifacts(transfer.id))[0]
    context = await repository.recovery_context(current.id)
    assert now[0] < retry_at
    assert current.execution is None
    assert context["quiescence_reason"] == "retry_backoff"
    assert current.retry_at == retry_at


@pytest.mark.asyncio
async def test_executor_recovery_and_startup_race_reuses_surviving_gid(runtime):
    repository, registry, _provider, executor, engine, _now = runtime
    transfer, artifact = await start_transfer(runtime)
    original = artifact.execution
    starts = len([call for call in executor.calls if call[0] == "start"])
    registry.executors.pop(original.executor_id)
    await engine.reconcile_executions()
    registry.register_executor(executor)
    artifact = (await repository.artifacts(transfer.id))[0]
    await asyncio.gather(
        engine.recover_artifact(artifact, trigger=RecoveryTrigger.EXECUTOR_RECOVERY),
        engine.recover_artifact(artifact, trigger=RecoveryTrigger.STARTUP_RECONCILE),
    )
    current = (await repository.artifacts(transfer.id))[0]
    assert current.execution == original
    assert len([call for call in executor.calls if call[0] == "start"]) == starts
