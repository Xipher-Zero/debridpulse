"""Phase-4 deterministic fault matrix, retained progress, and UX-truth contracts."""
from __future__ import annotations

from dataclasses import replace

import pytest
import pytest_asyncio

import db.database as database
from executors.aria2.translation import native_failure
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Origin, Retryability, Stage
from transfers.models import ExecutionObservation, ExecutionState, TransferProgress, TransferRequest
from transfers.policy import RecoveryAction, RecoveryContext, TransferPolicy, meaningful_progress_threshold
from transfers.presentation_repository import recovery_presentation
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry


MANDATORY_PHASE4_SCENARIOS = (
    "tls_receive_decode", "connection_reset", "premature_eof", "read_timeout",
    "connect_timeout", "dns_failure", "http_429", "http_500", "http_503",
    "expired_signed_url", "provider_candidate_refresh", "aria2_restart",
    "debridpulse_restart", "network_interruption", "pause_during_backoff",
    "resume_after_pause", "provider_disable_active", "provider_disable_backoff",
    "provider_reenable", "storage_unavailable", "one_child_complete_sibling_failures",
    "unknown_executor_failure", "repeated_zero_progress", "repeated_meaningful_progress",
)


@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "phase4.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider()
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    now = [12000.0]
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0),
        clock=lambda: now[0],
    )
    await engine.initialize()
    return repository, registry, provider, executor, engine, now


async def materialized(runtime, name="payload.bin"):
    repository, _registry, _provider, _executor, engine, _now = runtime
    transfer = await engine.submit((TransferRequest("parcel", "box", name=name),))
    await engine.resolve_pending()
    artifact = (await repository.artifacts(transfer.id))[0]
    assert artifact.execution is None
    return transfer, artifact


async def running(runtime):
    repository, _registry, _provider, _executor, engine, _now = runtime
    transfer, artifact = await materialized(runtime)
    await engine.reconcile_executions()
    artifact = (await repository.artifacts(transfer.id))[0]
    assert artifact.execution is not None
    return transfer, artifact


def test_phase4_fault_matrix_is_complete_and_unique():
    assert len(MANDATORY_PHASE4_SCENARIOS) == 24
    assert len(set(MANDATORY_PHASE4_SCENARIOS)) == 24


@pytest.mark.parametrize(("scenario", "code", "diagnostic", "category", "action"), (
    ("tls_receive_decode", "1", "Failed to receive data: Error decoding the received TLS packet", Category.TLS_FAILURE, RecoveryAction.BACKOFF),
    ("connection_reset", "1", "connection reset by peer", Category.REMOTE_RESET, RecoveryAction.BACKOFF),
    ("premature_eof", "1", "premature EOF", Category.REMOTE_READ_FAILED, RecoveryAction.BACKOFF),
    ("read_timeout", "2", "Timeout while receiving data", Category.READ_TIMEOUT, RecoveryAction.BACKOFF),
    ("connect_timeout", "1", "Connection timed out", Category.CONNECTION_TIMEOUT, RecoveryAction.BACKOFF),
    ("dns_failure", "19", "Name resolution failed", Category.DNS_FAILURE, RecoveryAction.BACKOFF),
    ("http_429", "22", "The response status is not successful. status=429", Category.RATE_LIMITED, RecoveryAction.BACKOFF),
    ("http_500", "22", "The response status is not successful. status=500", Category.SOURCE_TEMPORARILY_UNAVAILABLE, RecoveryAction.BACKOFF),
    ("http_503", "22", "The response status is not successful. status=503", Category.SOURCE_TEMPORARILY_UNAVAILABLE, RecoveryAction.BACKOFF),
    ("expired_signed_url", "24", "Authorization failed", Category.CANDIDATE_EXPIRED, RecoveryAction.REFRESH_CANDIDATE),
))
def test_native_faults_flow_through_real_normalization_and_core_policy(scenario, code, diagnostic, category, action):
    assert scenario in MANDATORY_PHASE4_SCENARIOS
    error = native_failure(code, diagnostic)
    assert error.category == category
    assert error.stage == Stage.EXECUTION
    assert error.origin in {Origin.REMOTE_SOURCE, Origin.EXECUTOR}
    assert error.evidence_basis.value != "unknown"
    decision = TransferPolicy(retry_delay=2).recover(
        error, RecoveryContext(can_refresh=category == Category.CANDIDATE_EXPIRED), 100.0,
    )
    assert decision.action == action
    assert decision.action != RecoveryAction.WAIT_FOR_OPERATOR


def test_original_tls_reproducer_is_transport_evidence_not_operator_attention():
    error = native_failure("1", "Failed to receive data\nError decoding the received TLS packet")
    assert error.domain == Domain.NETWORK
    assert error.category == Category.TLS_FAILURE
    assert error.retryability == Retryability.BACKOFF
    assert error.confidence.value in {"medium", "high"}
    decision = TransferPolicy(retry_delay=1).recover(error, RecoveryContext(), 100.0)
    assert decision.action == RecoveryAction.BACKOFF
    assert decision.reason == "transient_backoff"
    assert decision.quiescence_reason == "retry_backoff"
    assert decision.wake_condition == "retry_at:101.0"


def test_unknown_executor_failure_gets_bounded_recovery_before_attention():
    error = native_failure("1", "opaque executor failure")
    assert error.category == Category.UNMAPPED_EXECUTOR_ERROR
    first = TransferPolicy(retry_delay=1).recover(error, RecoveryContext(), 100.0)
    assert first.action == RecoveryAction.RETRY_SAME_CANDIDATE
    exhausted = TransferPolicy(retry_delay=1).recover(
        error, RecoveryContext(consecutive_no_progress_failures=2, same_signature_failures=2), 100.0,
    )
    assert exhausted.action == RecoveryAction.WAIT_FOR_OPERATOR
    assert exhausted.wake_condition == "operator_retry"


@pytest.mark.parametrize(("reason", "action", "wake", "expected"), (
    ("retry_backoff", "backoff", "retry_at:100.0", "waiting_for_retry"),
    ("provider_disabled", "wait_for_provider", "provider_enabled:parcel", "waiting_for_provider"),
    ("storage_unavailable", "wait_for_resource", "storage_healthy:local_resource", "waiting_for_storage"),
    ("executor_unavailable", "wait_for_resource", "executor_available", "waiting_for_executor"),
))
def test_quiescent_recovery_states_are_not_requires_attention(reason, action, wake, expected):
    view = recovery_presentation("recovery_wait", {
        "decision_action": action, "decision_reason": reason,
        "quiescence_reason": reason, "wake_condition": wake,
    })
    assert view["presentation_status"] == expected
    assert view["attention_required"] is False


def test_requires_attention_needs_persisted_operator_decision_reason_and_wake():
    base = {
        "decision_action": RecoveryAction.WAIT_FOR_OPERATOR.value,
        "decision_reason": "recovery_budget_exhausted",
        "quiescence_reason": "recovery_exhausted", "wake_condition": "operator_retry",
    }
    assert recovery_presentation("error", base)["presentation_status"] == "requires_attention"
    assert recovery_presentation("error", {**base, "decision_reason": None})["presentation_status"] != "requires_attention"
    assert recovery_presentation("error", {**base, "wake_condition": None})["presentation_status"] != "requires_attention"


def test_input_required_remains_distinct_from_requires_attention():
    view = recovery_presentation("recovery_wait", {
        "decision_action": RecoveryAction.WAIT_FOR_OPERATOR.value,
        "decision_reason": "input_required", "quiescence_reason": "input_required",
        "wake_condition": "operator_input",
    })
    assert view["presentation_status"] == "input_required"
    assert view["attention_required"] is False


def test_zero_progress_exhausts_same_candidate_then_refreshes_or_stops():
    error = NormalizedError(
        Domain.NETWORK, Category.REMOTE_READ_FAILED, Stage.EXECUTION,
        retryability=Retryability.BACKOFF, origin=Origin.REMOTE_SOURCE,
    )
    policy = TransferPolicy(retry_delay=1, same_candidate_no_progress_limit=2)
    assert policy.recover(error, RecoveryContext(consecutive_no_progress_failures=1, same_signature_failures=1), 100.0).action == RecoveryAction.BACKOFF
    assert policy.recover(error, RecoveryContext(consecutive_no_progress_failures=2, same_signature_failures=2, can_refresh=True), 100.0).action == RecoveryAction.REFRESH_CANDIDATE
    assert policy.recover(error, RecoveryContext(consecutive_no_progress_failures=2, same_signature_failures=2), 100.0).action == RecoveryAction.WAIT_FOR_OPERATOR


def test_meaningful_progress_threshold_preserves_phase2_rule():
    assert meaningful_progress_threshold(None) == 1024 * 1024
    assert meaningful_progress_threshold(32 * 1024) == 32 * 1024
    size = 10 * 1024 * 1024
    assert meaningful_progress_threshold(size) == (size + 99) // 100
    assert meaningful_progress_threshold(1024 * 1024 * 1024) == 1024 * 1024


@pytest.mark.asyncio
async def test_retained_progress_survives_failed_gid_and_recovery_quiescence(runtime):
    repository, _registry, _provider, executor, engine, _now = runtime
    transfer, artifact = await running(runtime)
    target, attempt = artifact.target, artifact.execution
    error = native_failure("1", "Failed to receive data: Error decoding the received TLS packet")
    executor.jobs[attempt.attempt_id] = ExecutionObservation(
        attempt, ExecutionState.FAILED, TransferProgress(4, 2, 0), error=error,
    )
    await engine.reconcile_executions()
    current = (await repository.artifacts(transfer.id))[0]
    assert current.target == target
    presentation = await repository.presentation(transfer.id, details=True)
    file_view = next(item for item in presentation["files"] if item["id"] == artifact.id)
    assert presentation["retained_bytes"] >= 2
    assert presentation["progress"] >= 50.0
    assert file_view["retained_bytes"] == 2
    assert file_view["progress"] == 50.0
    assert file_view["presentation_status"] in {"waiting_for_retry", "recovering"}
    assert file_view["attention_required"] is False
    context = await repository.recovery_context(artifact.id)
    assert context["durable_target"] == target
    assert context["failure_classification"]["category"] == Category.TLS_FAILURE.value
    assert context["classification_confidence"] in {"medium", "high"}
    assert context["wake_condition"].startswith("retry_at:")


@pytest.mark.asyncio
async def test_pause_during_backoff_and_resume_preserve_blocker_truth(runtime):
    repository, _registry, _provider, executor, engine, now = runtime
    transfer, artifact = await running(runtime)
    error = native_failure("2", "Timeout")
    executor.jobs[artifact.execution.attempt_id] = ExecutionObservation(
        artifact.execution, ExecutionState.FAILED, TransferProgress(4, 1, 0), error=error,
    )
    await engine.reconcile_executions()
    assert (await repository.artifacts(transfer.id))[0].retry_at > now[0]
    await engine.pause(transfer.id)
    assert (await repository.presentation(transfer.id))["presentation_status"] == "paused"
    await engine.resume(transfer.id)
    assert (await repository.presentation(transfer.id))["presentation_status"] in {"waiting_for_retry", "recovering", "queued"}


@pytest.mark.asyncio
async def test_provider_disable_reenable_never_fabricates_attention_or_budget(runtime):
    repository, _registry, provider, _executor, engine, _now = runtime
    transfer, artifact = await running(runtime)
    provider.descriptor = replace(provider.descriptor, enabled=False)
    await engine.reconcile_executions()
    waiting = await repository.presentation(transfer.id)
    assert waiting["presentation_status"] == "waiting_for_provider"
    assert waiting["attention_required"] is False
    assert await repository.recovery_budget(artifact.id) == (0, 0)
    provider.descriptor = replace(provider.descriptor, enabled=True)
    await engine.reconcile_executions()
    assert (await repository.presentation(transfer.id))["presentation_status"] != "requires_attention"
    assert await repository.recovery_budget(artifact.id) == (0, 0)


@pytest.mark.asyncio
async def test_storage_unavailable_is_quiescent_and_wakes_without_budget(runtime):
    repository, _registry, _provider, _executor, engine, _now = runtime
    transfer, artifact = await materialized(runtime, "storage.bin")
    error = NormalizedError(
        Domain.LOCAL_RESOURCE, Category.DISK_FULL, Stage.EXECUTION,
        retryability=Retryability.AFTER_RESOURCE_CHANGE, origin=Origin.LOCAL_SYSTEM,
    )
    assert await repository.transition_recovery(
        artifact.id, "recovery_wait", error=error,
        quiescence_reason="storage_unavailable", wake_condition="storage_healthy:local_resource",
    )
    engine.dispatch_permitted = False
    for _ in range(3):
        await engine.reconcile_executions()
    waiting = await repository.presentation(transfer.id)
    assert waiting["presentation_status"] == "waiting_for_storage"
    assert waiting["attention_required"] is False
    assert await repository.recovery_budget(artifact.id) == (0, 0)
    engine.dispatch_permitted = True
    await engine.reconcile_executions()
    assert (await repository.artifacts(transfer.id))[0].execution is not None


@pytest.mark.asyncio
async def test_recovery_truth_and_progress_survive_repository_restart(runtime):
    repository, _registry, _provider, executor, engine, _now = runtime
    transfer, artifact = await running(runtime)
    error = native_failure("1", "connection reset by peer")
    executor.jobs[artifact.execution.attempt_id] = ExecutionObservation(
        artifact.execution, ExecutionState.FAILED, TransferProgress(4, 3, 0), error=error,
    )
    await engine.reconcile_executions()
    before = await repository.presentation(transfer.id, details=True)
    restarted = TransferRepository()
    await restarted.initialize()
    after = await restarted.presentation(transfer.id, details=True)
    assert after["retained_bytes"] == before["retained_bytes"]
    assert after["presentation_status"] == before["presentation_status"]
    assert after["wake_condition"] == before["wake_condition"]


@pytest.mark.asyncio
async def test_completed_child_progress_is_not_erased_by_recovering_sibling(runtime):
    repository, _registry, _provider, executor, engine, _now = runtime
    transfer = await engine.submit((
        TransferRequest("parcel", "one", name="one.bin"),
        TransferRequest("parcel", "two", name="two.bin"),
    ))
    await engine.resolve_pending()
    await engine.reconcile_executions()
    artifacts = await repository.artifacts(transfer.id)
    assert len(artifacts) == 2
    executor.finish(artifacts[0].execution)
    await engine.reconcile_executions()
    sibling = next(item for item in await repository.artifacts(transfer.id) if item.state != "completed")
    error = native_failure("1", "connection reset by peer")
    executor.jobs[sibling.execution.attempt_id] = ExecutionObservation(
        sibling.execution, ExecutionState.FAILED, TransferProgress(4, 1, 0), error=error,
    )
    await engine.reconcile_executions()
    view = await repository.presentation(transfer.id, details=True)
    complete = next(item for item in view["files"] if item["status"] == "completed")
    assert complete["progress"] == 100.0
    assert view["presentation_status"] != "requires_attention"
