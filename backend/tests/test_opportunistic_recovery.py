"""DP 1.0.12 opportunistic zero-progress recovery.

A refresh-capable candidate whose execution fails at an AUTHORITATIVE zero
completed bytes is refreshed immediately -- through the existing canonical
refresh machinery -- instead of sleeping through the generic timed
same-candidate retry first. Every stronger stop/wait fact keeps outranking it,
unknown progress is never read as zero, and any positive progress keeps the
existing conservative recovery.

Facts come from explicit fake provider/executor contracts only; nothing here
names a real provider, host, executor or diagnostic string.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from test_opportunistic_resolution_fairness import GUARD_SECONDS, GatedProvider, _until
from transfers import codec
from transfers.applicability import ProviderApplicability
from transfers.contracts import CandidateRefresh
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Origin, Retryability, Stage
from transfers.models import (
    Capability, Endpoint, ExecutionObservation, ExecutionState, IntegrationDescriptor, ResolutionResult,
    ResourceState, TransferCandidate, TransferProgress, TransferRequest, TransferState,
)
from transfers.policy import RecoveryAction, RecoveryContext, TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

RETRY_DELAY = 60.0
NOW = 7000.0
ZERO = TransferProgress(0, 0)


def execution_failure(**facts) -> NormalizedError:
    """A generic, provider-neutral execution failure with no wait semantics.

    ``TRANSFER_FAILED`` keeps whatever retryability a case assigns;
    ``UNMAPPED_EXECUTOR_ERROR`` (an executor failure nothing could classify) is
    always canonicalized to ``Retryability.UNKNOWN`` by ``NormalizedError``.
    """
    base = NormalizedError(
        Domain.EXECUTOR, Category.TRANSFER_FAILED, Stage.EXECUTION,
        retryability=Retryability.UNKNOWN, origin=Origin.EXECUTOR, integration_id="memory-copy",
    )
    return replace(base, **facts)


class ScriptedExecutor(MemoryExecutor):
    """Memory executor whose next start(s) of a named payload fail with an
    exact, executor-reported progress observation."""

    def __init__(self, authorize):
        super().__init__(authorize)
        self.failures: dict[str, list[tuple[NormalizedError, TransferProgress]]] = {}
        self.observe_as: dict[str, object] = {}
        self.started: list[str] = []

    def fail_next(self, name: str, error: NormalizedError, progress: TransferProgress = ZERO) -> None:
        self.failures.setdefault(name, []).append((error, progress))

    def starts(self, name: str) -> int:
        return self.started.count(name)

    async def start(self, request, handle):
        observed = await super().start(request, handle)
        self.started.append(request.work.subject.candidate.name)
        scripted = self.failures.get(request.work.subject.candidate.name)
        if scripted:
            error, progress = scripted.pop(0)
            observed = replace(observed, state=ExecutionState.FAILED, progress=progress, error=error)
            self.jobs[handle.attempt_id] = observed
        return observed

    async def observe(self, handle):
        override = self.observe_as.get(handle.attempt_id) or self.observe_as.get("*")
        if isinstance(override, Exception):
            raise override
        if override is not None:
            return ExecutionObservation(handle, override)
        return await super().observe(handle)


class PlainProvider:
    """Resolves candidates but does not implement the CandidateRefresh contract."""

    def __init__(self):
        self.descriptor = IntegrationDescriptor(
            "plain-lab", "Plain lab", frozenset({Capability.RESOLVE}), request_types=frozenset({"parcel"}))
        self.calls = []

    @property
    def applicability(self):
        return ProviderApplicability()

    async def resolve(self, request):
        self.calls.append(("resolve", request.payload))
        return ResolutionResult(ResourceState.AVAILABLE, (TransferCandidate(
            request.name or "payload.bin", (Endpoint("memory", f"memory:{request.payload}"),),
            expected_bytes=4, provider_id=self.descriptor.id,
        ),))


class Runtime:
    def __init__(self, repository, provider, executor, engine, now):
        self.repository = repository
        self.provider = provider
        self.executor = executor
        self.engine = engine
        self.now = now

    async def artifact(self, transfer_id: int):
        return (await self.repository.artifacts(transfer_id))[0]

    async def fail_first_execution(self, error, progress=ZERO, *, name="payload.bin"):
        """Submit, materialize and dispatch one artifact whose first execution
        fails with exactly ``error`` / ``progress``; returns after the ONE
        reconcile cycle that observed the failure and decided recovery."""
        self.executor.fail_next(name, error, progress)
        transfer = await self.engine.submit((TransferRequest("parcel", "box", name=name),))
        await self.engine.resolve_pending()
        await self.engine.reconcile_executions()
        artifact = await self.artifact(transfer.id)
        return transfer, artifact, await self.repository.recovery_context(artifact.id)

    async def refresh_and_dispatch(self) -> None:
        """The existing machinery's own cadence: one reconcile pass applies a
        pending refresh, the next one dispatches the refreshed candidate."""
        await self.engine.reconcile_executions()
        await self.engine.reconcile_executions()

    async def audit(self, transfer_id: int) -> list[dict]:
        async with database.get_db() as db:
            rows = await db.fetchall(
                "SELECT detail FROM application_events WHERE transfer_id=? AND kind='recovery_audit' ORDER BY id",
                (transfer_id,),
            )
        return [codec.load(row["detail"], {}) for row in rows]


async def _runtime(tmp_path, monkeypatch, provider, *, concurrency=3) -> Runtime:
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "recovery.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    executor = ScriptedExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    now = [NOW]
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(
            retry_delay=RETRY_DELAY, adoption_stability_seconds=0, max_active_executions=4,
            resolution_concurrency=concurrency,
        ),
        clock=lambda: now[0],
    )
    await engine.initialize()
    return Runtime(repository, provider, executor, engine, now)


@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch):
    return await _runtime(tmp_path, monkeypatch, ParcelProvider())


def _refreshes(provider) -> int:
    return sum(1 for call in provider.calls if call[0] == "refresh")


# ---------------------------------------------------------------------------
# 15.1 / 15.2 -- the defect and its correction, through the real engine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("category", [Category.UNMAPPED_EXECUTOR_ERROR, Category.TRANSFER_FAILED])
async def test_zero_progress_refreshable_failure_refreshes_immediately(runtime, category):
    """BASE chooses ``retry_same_candidate`` / ``bounded_same_candidate_retry``
    with ``retry_at = now + 60`` for exactly these facts."""
    assert isinstance(runtime.provider, CandidateRefresh)
    transfer, artifact, context = await runtime.fail_first_execution(execution_failure(category=category))

    assert (context["decision_action"], context["decision_reason"]) == (
        RecoveryAction.REFRESH_CANDIDATE.value, "zero_progress_refresh")
    assert context["bytes_at_failure"] == 0
    assert artifact.state == "refresh_pending"
    assert artifact.retry_at <= runtime.now[0]
    assert context["quiescence_reason"] is None

    # The clock never moves: the existing canonical refresh path applies the
    # refresh on the next reconcile pass and the ordinary dispatch path starts
    # the refreshed candidate on the one after -- no timed wait anywhere.
    await runtime.refresh_and_dispatch()
    refreshed = await runtime.artifact(transfer.id)
    assert _refreshes(runtime.provider) == 1
    assert runtime.executor.starts("payload.bin") == 2
    assert refreshed.execution is not None and refreshed.state == "downloading"
    assert await runtime.repository.recovery_budget(refreshed.id) == (1, 1)

    runtime.executor.finish(refreshed.execution)
    await runtime.engine.reconcile_executions()
    assert (await runtime.artifact(transfer.id)).state == "completed"
    assert (await runtime.repository.get(transfer.id)).state == TransferState.COMPLETED
    assert runtime.now[0] == NOW


# ---------------------------------------------------------------------------
# 15.3 - 15.8 -- everything that must keep its existing recovery
# ---------------------------------------------------------------------------


async def _assert_timed_same_candidate_retry(runtime, artifact, context, *, delay=RETRY_DELAY):
    assert (context["decision_action"], context["decision_reason"]) == (
        RecoveryAction.RETRY_SAME_CANDIDATE.value, "bounded_same_candidate_retry")
    assert artifact.retry_at == runtime.now[0] + delay
    assert context["quiescence_reason"] == "retry_backoff"
    await runtime.engine.reconcile_executions()
    assert _refreshes(runtime.provider) == 0
    assert runtime.executor.starts("payload.bin") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("lost", [ExecutionState.ABSENT, RuntimeError("executor unreachable")],
                         ids=["absent", "unobservable"])
async def test_unknown_progress_is_never_read_as_zero(runtime, lost):
    """15.3. The executor cannot report this attempt's bytes: the synthesized
    observation's default ``completed_bytes=0`` is NOT an observed zero."""
    runtime.executor.observe_as["*"] = lost
    _transfer, artifact, context = await runtime.fail_first_execution(execution_failure())

    assert context["decision_action"] != RecoveryAction.REFRESH_CANDIDATE.value
    assert context["decision_reason"] != "zero_progress_refresh"
    assert _refreshes(runtime.provider) == 0
    assert artifact.state != "refresh_pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [1, 4096])
async def test_positive_progress_keeps_existing_recovery(runtime, completed):
    """15.4. The boundary is exactly zero; one observed byte is not zero."""
    _transfer, artifact, context = await runtime.fail_first_execution(
        execution_failure(), TransferProgress(8192, completed))
    assert context["bytes_at_failure"] == completed
    await _assert_timed_same_candidate_retry(runtime, artifact, context)


@pytest.mark.asyncio
async def test_non_refreshable_candidate_keeps_existing_recovery(tmp_path, monkeypatch):
    """15.5."""
    runtime = await _runtime(tmp_path, monkeypatch, PlainProvider())
    assert not isinstance(runtime.provider, CandidateRefresh)
    _transfer, artifact, context = await runtime.fail_first_execution(execution_failure())
    assert (context["decision_action"], context["decision_reason"]) == (
        RecoveryAction.RETRY_SAME_CANDIDATE.value, "bounded_same_candidate_retry")
    assert artifact.retry_at == runtime.now[0] + RETRY_DELAY


@pytest.mark.asyncio
@pytest.mark.parametrize("facts, action, reason, delay", [
    ({"retryability": Retryability.BACKOFF}, RecoveryAction.BACKOFF, "transient_backoff", RETRY_DELAY),
    ({"domain": Domain.NETWORK, "category": Category.RATE_LIMITED, "retryability": Retryability.BACKOFF},
     RecoveryAction.BACKOFF, "rate_limited_backoff", RETRY_DELAY),
    ({"domain": Domain.NETWORK, "category": Category.RATE_LIMITED},
     RecoveryAction.BACKOFF, "rate_limited_backoff", RETRY_DELAY),
    ({"retry_after_seconds": 240}, RecoveryAction.RETRY_SAME_CANDIDATE, "bounded_same_candidate_retry", 240.0),
], ids=["backoff", "rate-limited", "rate-limited-unknown-retryability", "retry-after"])
async def test_mandatory_wait_outranks_zero_progress_refresh(runtime, facts, action, reason, delay):
    """15.6."""
    _transfer, artifact, context = await runtime.fail_first_execution(execution_failure(**facts))
    assert (context["decision_action"], context["decision_reason"]) == (action.value, reason)
    assert artifact.retry_at == runtime.now[0] + delay
    await runtime.engine.reconcile_executions()
    assert _refreshes(runtime.provider) == 0
    assert runtime.executor.starts("payload.bin") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("facts, reason", [
    ({"domain": Domain.SECURITY, "category": Category.UNSAFE_REDIRECT, "retryability": Retryability.NEVER},
     "security_failure"),
    ({"domain": Domain.SECURITY}, "security_failure"),
    ({"domain": Domain.INTEGRITY, "category": Category.CHECKSUM_MISMATCH,
      "retryability": Retryability.AFTER_RERESOLUTION}, "integrity_failure"),
    ({"domain": Domain.INTEGRITY}, "integrity_failure"),
], ids=["security", "security-unknown-retryability", "integrity", "integrity-unknown-retryability"])
async def test_security_and_integrity_failures_are_never_refreshed_around(runtime, facts, reason):
    """15.7."""
    _transfer, artifact, context = await runtime.fail_first_execution(execution_failure(**facts))
    assert (context["decision_action"], context["decision_reason"]) == (
        RecoveryAction.FAIL_PERMANENTLY.value, reason)
    assert artifact.state == "error"
    await runtime.engine.reconcile_executions()
    assert _refreshes(runtime.provider) == 0
    assert runtime.executor.starts("payload.bin") == 1


@pytest.mark.asyncio
async def test_refresh_budget_bounds_the_opportunistic_refresh(runtime):
    """15.8. The refreshed candidate fails at zero bytes again: the epoch's
    refresh budget is spent, so existing bounded recovery continues -- no
    second refresh, no hot loop."""
    runtime.executor.fail_next("payload.bin", execution_failure())
    transfer, _artifact, first = await runtime.fail_first_execution(execution_failure())
    assert first["decision_reason"] == "zero_progress_refresh"

    await runtime.refresh_and_dispatch()
    artifact = await runtime.artifact(transfer.id)
    context = await runtime.repository.recovery_context(artifact.id)
    assert _refreshes(runtime.provider) == 1
    assert runtime.executor.starts("payload.bin") == 2
    assert context["decision_action"] != RecoveryAction.REFRESH_CANDIDATE.value
    assert context["decision_reason"] != "zero_progress_refresh"
    assert artifact.state != "refresh_pending"

    for _ in range(3):
        await runtime.engine.reconcile_executions()
    assert _refreshes(runtime.provider) == 1
    assert runtime.executor.starts("payload.bin") == 2


# ---------------------------------------------------------------------------
# 15.9 -- provenance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recovery_audit_records_the_zero_progress_refresh_truthfully(runtime):
    failure = execution_failure()
    transfer, artifact, context = await runtime.fail_first_execution(failure)
    await runtime.refresh_and_dispatch()
    refreshed = await runtime.artifact(transfer.id)

    assert context["failure_classification"]["category"] == failure.category.value
    assert context["failure_classification"]["stage"] == Stage.EXECUTION.value
    assert context["bytes_at_failure"] == 0

    audit = await runtime.audit(transfer.id)
    assert {item["artifact_id"] for item in audit} == {artifact.id}
    transitions = [item["transition"] for item in audit]
    ordered = ["source_failure", "decision", "refresh_reserved", "refresh_begin"]
    assert [transitions.index(name) for name in ordered] == sorted(transitions.index(name) for name in ordered)
    decision = audit[transitions.index("decision")]
    assert (decision["action"], decision["reason"], decision["bytes_at_failure"]) == (
        RecoveryAction.REFRESH_CANDIDATE.value, "zero_progress_refresh", 0)
    assert [item["transition"] for item in audit if item["transition"] == "decision"] == ["decision"]
    applied = [item for item in audit if item["transition"] == "application"]
    assert [(item["action"], item["reason"]) for item in applied] == [
        (RecoveryAction.REFRESH_CANDIDATE.value, "zero_progress_refresh"),
        (RecoveryAction.REFRESH_CANDIDATE.value, "refresh_applied"),
    ]

    executions = [item for item in await runtime.repository.executions(transfer.id)
                  if item.artifact_id == refreshed.id]
    assert len(executions) == 2
    assert refreshed.execution is not None and refreshed.state == "downloading"
    assert sorted(item.progress.completed_bytes for item in executions) == [0, 1]
    # The reason is one stable neutral token; no integration is named by it.
    assert all(item.get("reason") in {None, "zero_progress_refresh", "refresh_applied"} for item in audit)


# ---------------------------------------------------------------------------
# Policy matrix -- TransferPolicy.recover() is the one decision owner
# ---------------------------------------------------------------------------


def _eligible_context(**facts) -> RecoveryContext:
    return replace(RecoveryContext(can_refresh=True, observed_completed_bytes=0), **facts)


@pytest.mark.parametrize("retryability", [
    Retryability.UNKNOWN, Retryability.IMMEDIATE, Retryability.AFTER_RERESOLUTION])
def test_policy_selects_zero_progress_refresh_now(retryability):
    decision = TransferPolicy(retry_delay=RETRY_DELAY).recover(
        execution_failure(retryability=retryability), _eligible_context(), NOW)
    assert (decision.action, decision.reason, decision.retry_at) == (
        RecoveryAction.REFRESH_CANDIDATE, "zero_progress_refresh", NOW)
    assert decision.quiescence_reason is None and decision.wake_condition is None


def test_recovery_context_defaults_to_unknown_progress():
    assert RecoveryContext().observed_completed_bytes is None


@pytest.mark.parametrize("context_facts", [
    {"observed_completed_bytes": None},
    {"observed_completed_bytes": 1},
    {"can_refresh": False},
    {"candidate_refreshes": 1},
    {"provider_ready": False},
    {"executor_ready": False},
    {"storage_ready": False},
    {"input_required": True},
], ids=["unknown-progress", "positive-progress", "not-refreshable", "refresh-budget-spent",
        "provider-not-ready", "executor-not-ready", "storage-not-ready", "input-required"])
def test_policy_context_facts_that_forbid_zero_progress_refresh(context_facts):
    decision = TransferPolicy(retry_delay=RETRY_DELAY).recover(
        execution_failure(), _eligible_context(**context_facts), NOW)
    assert decision.reason != "zero_progress_refresh"
    assert decision.action != RecoveryAction.REFRESH_CANDIDATE


@pytest.mark.parametrize("facts", [
    {"stage": Stage.RECONCILIATION},
    {"stage": Stage.QUEUE},
    {"domain": Domain.SECURITY},
    {"domain": Domain.INTEGRITY},
    {"domain": Domain.LOCAL_RESOURCE},
    {"retryability": Retryability.NEVER},
    {"retryability": Retryability.BACKOFF},
    {"retryability": Retryability.AFTER_REAUTH},
    {"retryability": Retryability.AFTER_RESOURCE_CHANGE},
    {"category": Category.RATE_LIMITED},
    {"category": Category.CONCURRENCY_LIMITED},
    {"category": Category.CONTENT_INVALID},
    {"category": Category.TRANSFER_INTERRUPTED},
    {"category": Category.INVALID_CONFIGURATION, "retryability": Retryability.IMMEDIATE},
    {"retry_after_seconds": 0},
    {"retry_after_seconds": 30},
], ids=lambda facts: "-".join(f"{key}={getattr(value, 'value', value)}" for key, value in facts.items()))
def test_policy_error_facts_that_forbid_zero_progress_refresh(facts):
    decision = TransferPolicy(retry_delay=RETRY_DELAY).recover(
        execution_failure(**facts), _eligible_context(), NOW)
    assert decision.reason != "zero_progress_refresh"


def test_policy_permanent_failure_is_not_refreshed():
    from transfers.errors import Permanence
    decision = TransferPolicy(retry_delay=RETRY_DELAY).recover(
        execution_failure(permanence=Permanence.PERMANENT), _eligible_context(), NOW)
    assert (decision.action, decision.reason) == (RecoveryAction.FAIL_PERMANENTLY, "permanent_failure")


def test_configured_retry_timing_and_refresh_budget_are_untouched():
    policy = TransferPolicy()
    assert (policy.retry_delay, policy.max_retry_delay) == (5.0, 300.0)
    assert (policy.same_candidate_no_progress_limit, policy.refreshes_per_recovery_epoch) == (2, 1)
    timed = TransferPolicy(retry_delay=RETRY_DELAY).recover(
        execution_failure(), _eligible_context(observed_completed_bytes=1), NOW)
    assert (timed.action, timed.retry_at) == (RecoveryAction.RETRY_SAME_CANDIDATE, NOW + RETRY_DELAY)


# ---------------------------------------------------------------------------
# Gate 6 -- fairness and recovery in one deterministic scenario
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fair_resolution_then_immediate_zero_progress_refresh(tmp_path, monkeypatch):
    runtime = await _runtime(tmp_path, monkeypatch, GatedProvider(), concurrency=2)
    provider, executor, engine, repository = runtime.provider, runtime.executor, runtime.engine, runtime.repository

    async def payloads(transfer_id):
        names = {item.id: item.request.payload for item in await repository.requests(transfer_id)}
        return {names[item.request_id] for item in await repository.artifacts(transfer_id)}

    # A: many sources, a viable path at once, enrichment held inside the provider.
    a = await engine.submit(tuple(TransferRequest("parcel", f"a-{index}", name=f"a-{index}.bin")
                                  for index in range(5)), name="a")
    provider.hold("a-1", "a-2", "a-3", "a-4")
    cycle = asyncio.create_task(engine.resolve_pending())
    try:
        await provider.wait_entered("a-1", "a-2")

        async def a_has_path():
            return "a-0" in await payloads(a.id)
        await _until(a_has_path)
        await engine.reconcile_executions()
        a_writer = next(item for item in await repository.artifacts(a.id) if item.execution)
        assert a_writer.state == "downloading"

        # B arrives afterwards and gets the next released opportunity.
        executor.fail_next("b-0.bin", execution_failure())
        b = await engine.submit((TransferRequest("parcel", "b-0", name="b-0.bin"),), name="b")
        provider.open("a-1")
        await provider.wait_entered("b-0")
        assert sorted(provider.entered[:3]) == ["a-0", "a-1", "a-2"] and provider.entered[3:] == ["b-0"]

        async def b_has_path():
            return "b-0" in await payloads(b.id)
        await _until(b_has_path)

        # B executes, fails at an authoritative zero bytes, is refreshed and
        # re-dispatched without the clock ever advancing -- all while A's
        # enrichment is still held and its in-flight call is untouched.
        await engine.reconcile_executions()
        b_artifact = (await repository.artifacts(b.id))[0]
        context = await repository.recovery_context(b_artifact.id)
        assert (context["decision_action"], context["decision_reason"]) == (
            RecoveryAction.REFRESH_CANDIDATE.value, "zero_progress_refresh")

        await runtime.refresh_and_dispatch()
        b_artifact = (await repository.artifacts(b.id))[0]
        assert _refreshes(provider) == 1
        assert executor.starts("b-0.bin") == 2
        executor.finish(b_artifact.execution)
        await engine.reconcile_executions()
        assert (await repository.get(b.id)).state == TransferState.COMPLETED
        assert len(await repository.artifacts(b.id)) == 1
        assert runtime.now[0] == NOW

        assert not cycle.done()
        assert "a-2" not in provider.finished and provider.cancelled == []
        current_a_writer = next(item for item in await repository.artifacts(a.id) if item.id == a_writer.id)
        assert current_a_writer.execution == a_writer.execution

        # A's enrichment resumes and completes; nothing was abandoned.
        provider.open()
        await asyncio.wait_for(cycle, GUARD_SECONDS)
        assert await payloads(a.id) == {f"a-{index}" for index in range(5)}
        assert provider.max_active <= 2
        assert (await repository.get(a.id)).state not in {TransferState.FAILED, TransferState.COMPLETED}
    finally:
        provider.open()
        if not cycle.done():
            cycle.cancel()
        await asyncio.gather(cycle, return_exceptions=True)
