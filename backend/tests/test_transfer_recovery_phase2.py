"""Phase 2 durable quiescence/readiness, protocol evidence, and migration contracts."""
from dataclasses import replace

import pytest
import pytest_asyncio

import db.database as database
from db.database import get_db
from executors.aria2.translation import native_failure
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.engine import TransferEngine
from transfers.errors import (
    Category, Domain, EvidenceBasis, NormalizedError, Origin, Permanence,
    Recovery, Retryability, Stage,
)
from transfers.models import Capability, ResolutionResult, ResourceState, TransferRequest
from transfers.policy import RecoveryAction, RecoveryContext, TransferPolicy
from transfers.recovery_execution import RecoveryTrigger
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


@pytest_asyncio.fixture
async def canonical_runtime(tmp_path, monkeypatch):
    """DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
    closure, Gate 9 revision 2): dispatch-time provider/executor/storage
    readiness gating and quiescent wake are now exclusively canonical-stack
    (transfers.convergence_engine.TransferEngine) behavior -- the lower,
    pre-Phase-3 ``runtime`` fixture's stack no longer performs this gating
    at all (it falls through to _engine_base.TransferEngine's plain
    dispatch, with no pre-flight readiness check)."""
    from transfers.convergence_engine import TransferEngine as CanonicalEngine
    from transfers.recovery_repository import TransferRepository as CanonicalRepository

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "phase2.db")
    await database.init_db()
    repository = CanonicalRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider()
    registry.register_provider(provider)
    now = [5000.0]
    engine = CanonicalEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0),
        clock=lambda: now[0],
    )
    await engine.initialize()
    return repository, registry, provider, engine, now


@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "phase2.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider()
    registry.register_provider(provider)
    now = [5000.0]
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0),
        clock=lambda: now[0],
    )
    await engine.initialize()
    return repository, registry, provider, engine, now


async def materialize(engine):
    transfer = await engine.submit((TransferRequest("parcel", "box", name="payload.bin"),))
    await engine.resolve_pending()
    artifact = (await engine.repository.artifacts(transfer.id))[0]
    assert artifact.state == "queued"
    assert artifact.execution is None
    return transfer, artifact


@pytest.mark.asyncio
async def test_provider_disablement_is_quiescent_and_reenable_wakes_same_work(canonical_runtime):
    repository, registry, provider, engine, _now = canonical_runtime
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_executor(executor)
    transfer, artifact = await materialize(engine)

    provider.descriptor = replace(provider.descriptor, enabled=False)
    await engine.reconcile_executions()
    parked = (await repository.artifacts(transfer.id))[0]
    assert parked.id == artifact.id
    assert parked.selected == artifact.selected
    assert parked.state == "recovery_wait"
    assert parked.execution is None
    state = await repository.recovery_context(artifact.id)
    assert state["quiescence_reason"] == "provider_disabled"
    assert state["wake_condition"] == f"provider_enabled:{provider.descriptor.id}"
    assert not [call for call in executor.calls if call[0] == "start"]

    provider.descriptor = replace(provider.descriptor, enabled=True)
    await engine.reconcile_executions()
    resumed = (await repository.artifacts(transfer.id))[0]
    assert resumed.id == artifact.id
    assert resumed.execution is not None
    assert [call for call in executor.calls if call[0] == "start"]


@pytest.mark.asyncio
async def test_executor_unavailable_parks_without_attempt_then_registration_wakes(canonical_runtime):
    repository, registry, _provider, engine, _now = canonical_runtime
    transfer, artifact = await materialize(engine)

    await engine.reconcile_executions()
    parked = (await repository.artifacts(transfer.id))[0]
    assert parked.state == "recovery_wait"
    assert parked.execution is None
    assert len(await repository.executions(transfer.id)) == 0
    state = await repository.recovery_context(artifact.id)
    assert state["quiescence_reason"] == "executor_unavailable"
    assert state["wake_condition"] == "executor_available"

    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_executor(executor)
    await engine.reconcile_executions()
    resumed = (await repository.artifacts(transfer.id))[0]
    assert resumed.execution is not None
    assert len(await repository.executions(transfer.id)) == 1


@pytest.mark.asyncio
async def test_storage_quiescence_survives_ticks_and_wakes_only_when_dispatch_recovers(canonical_runtime):
    repository, registry, _provider, engine, _now = canonical_runtime
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_executor(executor)
    transfer, artifact = await materialize(engine)
    error = NormalizedError(
        Domain.LOCAL_RESOURCE, Category.DISK_FULL, Stage.EXECUTION,
        retryability=Retryability.AFTER_RESOURCE_CHANGE, origin=Origin.LOCAL_SYSTEM,
    )
    assert await repository.transition_recovery(
        artifact.id, "recovery_wait", error=error,
        quiescence_reason="storage_unavailable",
        wake_condition="storage_healthy:local_resource",
    )
    engine.dispatch_permitted = False
    for _ in range(3):
        await engine.reconcile_executions()
    parked = (await repository.artifacts(transfer.id))[0]
    assert parked.state == "recovery_wait"
    assert parked.execution is None
    assert not [call for call in executor.calls if call[0] == "start"]

    engine.dispatch_permitted = True
    await engine.reconcile_executions()
    resumed = (await repository.artifacts(transfer.id))[0]
    assert resumed.execution is not None


class _RefreshlessProvider:
    """Delegates everything to a real provider except ``refresh``, which is
    absent entirely -- not merely disabled -- so ``isinstance(provider,
    CandidateRefresh)`` (a ``typing.Protocol`` structural check) genuinely
    fails for it. Deleting ``refresh`` from ``ParcelProvider`` itself at
    runtime does NOT work for this: CPython's Protocol ``isinstance`` cache
    is keyed by the concrete class and does not invalidate on attribute
    deletion, so a class already isinstance-checked once (as ``ParcelProvider``
    is, incidentally, by ordinary engine machinery) keeps reporting ``True``
    forever after. A genuinely fresh class that never had ``refresh`` is the
    only reliable way to construct this."""

    def __init__(self, inner):
        self._inner = inner
        self.descriptor = replace(inner.descriptor, capabilities=inner.descriptor.capabilities - {Capability.REFRESH})

    def __getattr__(self, name):
        if name == "refresh":
            raise AttributeError(name)
        return getattr(self._inner, name)


async def _refresh_pending_artifact(repository, engine):
    transfer, artifact = await materialize(engine)
    candidate = artifact.candidates[artifact.selected]
    await repository.artifact_state(
        artifact.id, "refresh_pending", selected=artifact.selected, expected_bytes=candidate.expected_bytes,
    )
    current = (await repository.artifacts(transfer.id))[0]
    return transfer, current


@pytest.mark.asyncio
async def test_refresh_returning_no_candidates_reaches_a_real_policy_decision(canonical_runtime):
    """DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
    closure, Gate 9 revision 7): a review correctly found that
    ``_refresh_claimed``'s ``refresh_result_empty`` reason carried no error,
    so a provider that successfully replies with zero replacement candidates
    left the artifact silently spinning in ``refresh_pending`` forever --
    ``policy.recover`` was never even consulted, so ``recovery_context``'s
    decision fields never changed no matter how many times this ran. After
    the fix, one call reaches a genuine, durably recorded policy decision."""
    repository, registry, provider, engine, _now = canonical_runtime
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    _transfer, current = await _refresh_pending_artifact(repository, engine)

    async def empty_refresh(_candidate):
        return ResolutionResult(ResourceState.AVAILABLE, ())
    provider.refresh = empty_refresh

    before = await repository.recovery_context(current.id)
    assert before["decision_reason"] is None

    await engine._refresh(current)

    after = await repository.recovery_context(current.id)
    assert after["decision_reason"] == "bounded_same_candidate_retry"
    assert after["decision_action"] == RecoveryAction.RETRY_SAME_CANDIDATE.value


@pytest.mark.asyncio
async def test_refresh_returning_an_expired_candidate_reaches_a_real_policy_decision(canonical_runtime):
    """Companion to the empty-result test above: ``refresh_candidate_expired``
    was equally misclassified as transient in rev. 6. After the fix, one
    call reaches ``policy.recover``'s existing, bounded expiry-category
    handling (``_EXPIRY_CATEGORIES``) instead of silently no-opping -- the
    refresh attempt that revealed the expiry already consumed this
    artifact's one-per-epoch refresh budget (``reserve_recovery_refresh``,
    reserved before the provider call), and there is no alternate candidate
    in this single-candidate fixture, so policy reaches the exhaustion
    disposition (``WAIT_FOR_OPERATOR``) in this one call rather than
    silently retrying forever."""
    repository, registry, provider, engine, now = canonical_runtime
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    _transfer, current = await _refresh_pending_artifact(repository, engine)

    async def expired_refresh(candidate):
        return ResolutionResult(ResourceState.AVAILABLE, (replace(candidate, expires_at=now[0] - 1),))
    provider.refresh = expired_refresh

    await engine._refresh(current)

    after = await repository.recovery_context(current.id)
    assert after["decision_reason"] == "candidate_expired_exhausted"
    assert after["decision_action"] == RecoveryAction.WAIT_FOR_OPERATOR.value
    assert after["quiescence_reason"] == "recovery_exhausted"


@pytest.mark.asyncio
async def test_refresh_returning_a_size_incompatible_candidate_fails_permanently(canonical_runtime):
    """``refresh_size_mismatch`` was also misclassified as transient in
    rev. 6, even though a provider returning a replacement of an
    incompatible size for the SAME resource is a genuine integrity failure
    that will never resolve itself by trying again. After the fix, one call
    reaches ``policy.recover``'s unconditional ``Domain.INTEGRITY`` ->
    FAIL_PERMANENTLY branch and the artifact settles into a real terminal
    ``error`` state instead of looping in ``refresh_pending`` forever."""
    repository, registry, provider, engine, _now = canonical_runtime
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    _transfer, current = await _refresh_pending_artifact(repository, engine)
    candidate = current.candidates[current.selected]
    assert candidate.expected_bytes > 0

    async def mismatched_refresh(candidate):
        return ResolutionResult(ResourceState.AVAILABLE, (replace(candidate, expected_bytes=candidate.expected_bytes * 100),))
    provider.refresh = mismatched_refresh

    await engine._refresh(current)

    after = await repository.recovery_context(current.id)
    assert after["decision_reason"] == "integrity_failure"
    assert after["decision_action"] == RecoveryAction.FAIL_PERMANENTLY.value
    updated = (await repository.artifacts(_transfer.id))[0]
    assert updated.state == "error"
    assert updated.error is not None and updated.error.category == Category.SIZE_MISMATCH


@pytest.mark.asyncio
async def test_refresh_unsupported_provider_reaches_a_real_bounded_policy_decision(canonical_runtime):
    """DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
    closure, Gate 9 revision 8): a review found ``refresh_unsupported`` (the
    bound provider does not implement ``CandidateRefresh`` at all -- a
    permanent fact about this candidate) was still classified as transient
    in rev. 7. It is now mapped to the same ``CANDIDATE_EXPIRED``-shaped
    error ``manual_failover._refresh_exact`` already uses for the identical
    structural case, so it reaches ``policy.recover``'s existing bounded
    expiry handling. ``policy.recover`` derives ``can_refresh`` from the
    SAME ``isinstance(provider, CandidateRefresh)`` fact this method just
    used to classify the failure, so a provider that structurally cannot
    refresh is correctly treated as having zero refresh budget available at
    all -- with no alternate candidate in this single-candidate fixture,
    genuine exhaustion (``WAIT_FOR_OPERATOR``) is reached in this one call,
    not left as an untouched, undecided no-op."""
    repository, registry, provider, engine, _now = canonical_runtime
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    _transfer, current = await _refresh_pending_artifact(repository, engine)
    registry.providers[provider.descriptor.id] = _RefreshlessProvider(provider)

    await engine._refresh(current)

    after = await repository.recovery_context(current.id)
    assert after["decision_reason"] == "candidate_expired_exhausted"
    assert after["decision_action"] == RecoveryAction.WAIT_FOR_OPERATOR.value
    assert after["quiescence_reason"] == "recovery_exhausted"


@pytest.mark.asyncio
async def test_refresh_with_already_consumed_budget_is_a_deliberate_operator_wait(canonical_runtime):
    """DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
    closure, Gate 9 revision 8): a review found ``refresh_budget_exhausted``
    still carried ``None`` -- documented as "transient" even though a
    consumed refresh budget does not become available merely because
    another scheduler tick occurs. This is exactly the restart/backward-
    state shape the review named: a persisted ``refresh_pending`` artifact
    whose budget was already spent in an earlier process, arriving here
    with no recorded decision id of its own. One call must reach the same
    deliberate, durably wakeable operator-wait used for the pre-existing
    ``refresh_outcome_unknown`` case, not another silent no-op tick."""
    repository, registry, provider, engine, _now = canonical_runtime
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    _transfer, current = await _refresh_pending_artifact(repository, engine)
    async with get_db() as db:
        await db.execute("UPDATE download_files SET recovery_refreshes=1 WHERE id=?", (current.id,))
        await db.commit()

    await engine._refresh(current)

    after = await repository.recovery_context(current.id)
    assert after["quiescence_reason"] == "recovery_exhausted"
    assert after["wake_condition"] == "operator_retry"
    updated = (await repository.artifacts(_transfer.id))[0]
    assert updated.state != "refresh_pending"


@pytest.mark.asyncio
async def test_refresh_with_no_selected_candidate_is_a_deliberate_operator_wait(canonical_runtime):
    """DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
    closure, Gate 9 revision 8): ``candidate_missing`` (the artifact's
    ``selected`` index no longer points at a real candidate -- a durable
    data-shape gap, not a retryable fact) shares the same deliberate
    operator-wait disposition as ``refresh_outcome_unknown`` and
    ``refresh_budget_exhausted``, proven directly rather than left to a
    comment claiming it is harmless."""
    repository, registry, provider, engine, _now = canonical_runtime
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    _transfer, current = await _refresh_pending_artifact(repository, engine)
    async with get_db() as db:
        await db.execute("UPDATE download_files SET selected_candidate=99 WHERE id=?", (current.id,))
        await db.commit()

    await engine._refresh(current)

    after = await repository.recovery_context(current.id)
    assert after["quiescence_reason"] == "recovery_exhausted"
    assert after["wake_condition"] == "operator_retry"
    updated = (await repository.artifacts(_transfer.id))[0]
    assert updated.state != "refresh_pending"


@pytest.mark.asyncio
async def test_refresh_pending_with_disabled_provider_applies_a_durable_wakeable_wait(canonical_runtime):
    """DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
    closure, Gate 9 revision 9): a review found the new ``provider_unavailable``
    branch in ``_plan_after_reconcile`` called ``_decision_step`` without the
    required keyword-only ``count_failure`` argument -- a guaranteed
    ``TypeError`` if ever reached.

    Disabling the provider before entering via ``engine._refresh(current)``
    (the ordinary claimed-recovery entry point) does NOT actually reach this
    branch: ``_reconcile_current`` performs the SAME provider-enabled check,
    unconditionally, BEFORE ``_plan_after_reconcile`` is ever called, and it
    already passes ``count_failure`` correctly -- so it always intercepts a
    disabled provider first and handles it via its own, already-correct
    call. ``_refresh_claimed``'s OWN ``provider_unavailable`` check inside
    ``_plan_after_reconcile`` can therefore only ever fire on a genuine
    TOCTOU race (the provider is disabled in the narrow window between the
    two back-to-back checks), which is not realistically constructible as
    an integration test. This test instead calls ``_plan_after_reconcile``
    directly with a real claim, bypassing ``_reconcile_current``'s
    redundant earlier gate, to exercise the exact branch that was actually
    broken -- proving it now applies a real, durably-wakeable
    ``provider_disabled``/``provider_enabled:<id>`` disposition (the SAME
    fields ``test_provider_disablement_is_quiescent_and_reenable_wakes_
    same_work`` already proves are wakeable for the ordinary dispatch-time
    case) instead of crashing."""
    repository, registry, provider, engine, _now = canonical_runtime
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    _transfer, current = await _refresh_pending_artifact(repository, engine)
    provider.descriptor = replace(provider.descriptor, enabled=False)

    claim = await repository.claim_recovery(current.id, RecoveryTrigger.AUTO_RETRY, engine.clock())
    assert claim is not None
    step = await engine._plan_after_reconcile(
        claim, current, RecoveryTrigger.AUTO_RETRY, None, None, None,
    )
    assert step.action == RecoveryAction.WAIT_FOR_PROVIDER.value

    parked = (await repository.artifacts(_transfer.id))[0]
    assert parked.id == current.id
    assert parked.state == "recovery_wait"
    assert parked.execution is None
    state = await repository.recovery_context(current.id)
    assert state["quiescence_reason"] == "provider_disabled"
    assert state["wake_condition"] == f"provider_enabled:{provider.descriptor.id}"


@pytest.mark.asyncio
async def test_phase1_database_counters_seed_context_without_fabricated_history(runtime):
    repository, _registry, _provider, engine, _now = runtime
    _transfer, artifact = await materialize(engine)
    async with get_db() as db:
        await db.execute(
            "UPDATE download_files SET recovery_failures=2,recovery_refreshes=1 WHERE id=?",
            (artifact.id,),
        )
        # DP 1.0.12 recovery leveling, Section 14: current recovery state is
        # now the single artifact_recovery_state row, not the latest
        # application_events snapshot of the legacy
        # repository._recovery_event_kind(artifact.id) kind. Deleting that
        # row (rather than the retired legacy event kind) is what now
        # simulates "no recovery-state row exists yet" for this artifact.
        await db.execute(
            "DELETE FROM artifact_recovery_state WHERE artifact_id=?",
            (artifact.id,),
        )
        await db.commit()

    restarted = TransferRepository()
    await restarted.initialize()
    context = await restarted.recovery_context(artifact.id)
    assert context["consecutive_no_progress_failures"] == 2
    assert context["candidate_refreshes"] == 1
    assert context["failure_signature"] is None
    assert context["same_signature_failures"] == 0
    assert context["recovery_epoch"] == 0
    assert context["progress_anchor"] is None
    assert context["decision_action"] is None
    assert context["decision_reason"] is None


@pytest.mark.asyncio
async def test_recovery_decision_and_reason_are_durable_across_restart(runtime):
    repository, _registry, _provider, engine, _now = runtime
    _transfer, artifact = await materialize(engine)
    await repository.record_recovery_decision(
        artifact.id, RecoveryAction.BACKOFF.value, "rate_limited_backoff",
    )
    before = await repository.recovery_context(artifact.id)
    assert before["decision_action"] == RecoveryAction.BACKOFF.value
    assert before["decision_reason"] == "rate_limited_backoff"

    restarted = TransferRepository()
    await restarted.initialize()
    after = await restarted.recovery_context(artifact.id)
    assert after["decision_action"] == before["decision_action"]
    assert after["decision_reason"] == before["decision_reason"]


@pytest.mark.asyncio
async def test_operator_retry_clears_exhaustion_without_claiming_progress(runtime):
    repository, _registry, _provider, engine, _now = runtime
    _transfer, artifact = await materialize(engine)
    error = NormalizedError(
        Domain.NETWORK, Category.REMOTE_READ_FAILED, Stage.EXECUTION,
        retryability=Retryability.BACKOFF, origin=Origin.REMOTE_SOURCE,
    )
    await repository.record_source_failure(artifact.id, error)
    await repository.record_source_failure(artifact.id, error)
    await repository.record_recovery_decision(
        artifact.id, RecoveryAction.WAIT_FOR_OPERATOR.value, "recovery_budget_exhausted",
    )
    assert await repository.transition_recovery(
        artifact.id, "error", error=error,
        quiescence_reason="recovery_exhausted", wake_condition="operator_retry",
    )
    before = await repository.recovery_context(artifact.id)
    assert before["recovery_epoch"] == 0
    assert before["same_signature_failures"] == 2
    assert before["quiescence_reason"] == "recovery_exhausted"

    await repository.reset_retry_budget(artifact.id)
    after = await repository.recovery_context(artifact.id)
    assert after["recovery_epoch"] == 0
    assert after["consecutive_no_progress_failures"] == 0
    assert after["same_signature_failures"] == 0
    assert after["decision_action"] is None
    assert after["decision_reason"] is None
    assert after["quiescence_reason"] is None


def test_reconcile_is_bounded_by_no_progress_accounting():
    policy = TransferPolicy(retry_delay=2, same_candidate_no_progress_limit=2)
    error = NormalizedError(
        Domain.RECONCILIATION, Category.RECONCILIATION_FAILED, Stage.RECONCILIATION,
        retryability=Retryability.BACKOFF, origin=Origin.CORE,
    )
    first = policy.recover(
        error,
        RecoveryContext(consecutive_no_progress_failures=1, same_signature_failures=1),
        100.0,
    )
    assert first.action == RecoveryAction.RECONCILE
    assert first.reason == "reconciliation_backoff"
    assert first.quiescence_reason == "retry_backoff"
    assert first.retry_at == 102.0
    assert first.wake_condition == "retry_at:102.0"

    exhausted = policy.recover(
        error,
        RecoveryContext(consecutive_no_progress_failures=2, same_signature_failures=2),
        100.0,
    )
    assert exhausted.action == RecoveryAction.WAIT_FOR_OPERATOR
    assert exhausted.reason == "reconciliation_exhausted"
    assert exhausted.quiescence_reason == "recovery_exhausted"
    assert exhausted.wake_condition == "operator_retry"


@pytest.mark.parametrize("status,category", [
    (429, Category.RATE_LIMITED),
    (500, Category.SOURCE_TEMPORARILY_UNAVAILABLE),
    (503, Category.SOURCE_TEMPORARILY_UNAVAILABLE),
    (599, Category.SOURCE_TEMPORARILY_UNAVAILABLE),
])
def test_aria2_http_status_is_factual_protocol_evidence_only(status, category):
    error = native_failure("22", f"The response status is not successful. status={status}")
    assert error.domain == Domain.NETWORK
    assert error.category == category
    assert error.retryability == Retryability.BACKOFF
    assert error.origin == Origin.REMOTE_SOURCE
    assert error.permanence == Permanence.TEMPORARY
    assert error.evidence_basis == EvidenceBasis.DIAGNOSTIC
    assert error.recovery == Recovery.NONE
    assert not error.operator_action_required


def test_aria2_code22_without_strict_status_remains_unknown_protocol_evidence():
    error = native_failure("22", "The response status is not successful. status=unknown")
    assert error.domain == Domain.NETWORK
    assert error.category == Category.PROTOCOL_ERROR
    assert error.retryability == Retryability.UNKNOWN
    assert error.permanence == Permanence.UNKNOWN
    assert error.recovery == Recovery.NONE
