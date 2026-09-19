"""Section 33 regression coverage: candidate traversal / shared activation, and
the truthful post-commit acknowledgement contract (DP 1.0.12 recovery
leveling, Phase 2, Sections 10-13 and 26).

All tests use the real production stack (transfers.convergence_engine
.TransferEngine + transfers.recovery_repository.TransferRepository), matching
tests/production_stack_harness.py's rationale (Section 32): a regression only
the Phase-3 claim/dispatch/truth/retry layers can produce is invisible to a
test built on the lower, isolated transfers.engine.TransferEngine stack.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import db.database as database
from db.database import get_db
from fake_integrations import MemoryExecutor
from test_manual_candidate_failover import HostParcelProvider, attach_two
from test_manual_candidate_failover import build_engine as build_engine2
from test_ws2p1_failover_depth import remote_failure
from transfers import candidate_activation
from transfers.candidate_activation import activate_candidate
from transfers.convergence_engine import TransferEngine
from transfers.manual_failover import manual_candidate_failover
from transfers.models import ExecutionState, ResolutionResult, ResourceState, TransferRequest
from transfers.policy import TransferPolicy
from transfers.recovery_execution import RecoveryTrigger
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry


async def build_engine3(tmp_path, monkeypatch, providers=None, *, max_active_executions=8, now=1000.0):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    providers = providers or tuple(
        HostParcelProvider(provider_id) for provider_id in ("provider-a", "provider-b", "provider-c")
    )
    executor = MemoryExecutor(repository.authorize_execution)
    for provider in providers:
        registry.register_provider(provider)
    registry.register_executor(executor)
    now_box = [now]
    engine = TransferEngine(
        repository,
        registry,
        download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(
            retry_delay=0,
            adoption_stability_seconds=0,
            max_active_executions=max_active_executions,
            resolution_concurrency=8,
        ),
        clock=lambda: now_box[0],
    )
    await engine.initialize()
    return engine, repository, providers, executor, now_box


async def attach_three(engine, repository, providers, *, name="same.bin", payloads=None):
    canonical = None
    payloads = payloads or tuple(f"original-{index}" for index in range(len(providers)))
    for provider, payload in zip(providers, payloads):
        transfer = await engine.submit((TransferRequest(
            "parcel", payload, name=name, preferred_provider=provider.descriptor.id,
        ),), deduplicate=False)
        await engine.resolve_pending()
        if canonical is None:
            canonical = transfer
    artifact = (await repository.artifacts(canonical.id))[0]
    assert [item.provider_id for item in artifact.candidates] == [p.descriptor.id for p in providers]
    return canonical, artifact


async def activate_with_real_claim(
    engine, artifact, target_index, *, retry_at, error=None, trigger=RecoveryTrigger.AUTO_RETRY,
):
    """Call the canonical primitive the way production does: inside a REAL recovery claim.

    ``activate_candidate`` requires the caller's already-held ``RecoveryClaim``; there is no claim-less
    mode and no test-only fabricated claim. This acquires the real exclusive claim through the production
    repository, runs the primitive, and releases the fence exactly like the two production callers
    (``TransferEngine._apply_recovery_decision`` and ``activate_candidate_command``) so later engine steps
    are not blocked by a leaked lease. ``trigger`` names the recovery authority being exercised.
    """
    claim = await engine.repository.claim_recovery(
        artifact.id, trigger, engine.clock(), lease_seconds=max(300.0, float(engine.policy.max_retry_delay)),
    )
    assert claim is not None, "the artifact must be claimable for a direct activation"
    result = None
    try:
        result = await activate_candidate(engine, artifact, target_index, retry_at=retry_at, error=error, claim=claim)
        return result
    finally:
        await engine.repository.finish_recovery_claim(
            claim,
            action="candidate_activation",
            reason=result.reason if result is not None else "application_error",
            outcome="activated" if (result is not None and result.committed) else "not_applied",
            candidate_changed=bool(result is not None and result.committed),
        )


async def _exhaust_current(engine, repository, executor, canonical_id, error, attempts=3):
    """Fail the currently-executing writer ``attempts`` times, tolerating the
    one candidate-owned refresh cycle each candidate goes through partway
    (mirrors test_ws2p1_failover_depth.py's advance_a_to_b/fail_current). A
    failure observation alone leaves the artifact "queued" with no execution
    for one reconcile cycle (and "refresh_pending" for one more, if this is
    the candidate's second failure) before redispatch actually reattaches an
    execution handle, so pump reconcile_executions() until one is attached or
    recovery has terminally parked."""
    handles = []
    for _ in range(attempts):
        artifact = (await repository.artifacts(canonical_id))[0]
        for _ in range(3):
            if artifact.execution is not None or artifact.state == "error":
                break
            await engine.reconcile_executions()
            artifact = (await repository.artifacts(canonical_id))[0]
        assert artifact.execution is not None
        handle = artifact.execution
        executor.jobs[handle.attempt_id] = replace(
            executor.jobs[handle.attempt_id], state=ExecutionState.FAILED, error=error,
        )
        await engine.reconcile_executions()
        handles.append(handle)
    return handles, (await repository.artifacts(canonical_id))[0]


async def _switch_switch_exhaust(tmp_path, monkeypatch):
    engine, repository, providers, executor, _now_box = await build_engine3(tmp_path, monkeypatch)
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()

    await manual_candidate_failover(engine, canonical.id, artifact.id, str(artifact.candidates[1].id))
    await engine.reconcile_executions()
    switched = (await repository.artifacts(canonical.id))[0]
    await manual_candidate_failover(engine, canonical.id, switched.id, str(switched.candidates[2].id))
    await engine.reconcile_executions()

    _handles, exhausted = await _exhaust_current(
        engine, repository, executor, canonical.id, remote_failure(), attempts=3,
    )
    return engine, repository, executor, canonical, exhausted


# ---------------------------------------------------------------------------
# Candidate traversal / shared activation (Section 12/10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_high_index_switch_does_not_hide_unattempted_lower_candidates(tmp_path, monkeypatch):
    engine, repository, providers, executor, _now_box = await build_engine3(tmp_path, monkeypatch)
    canonical, artifact = await attach_three(engine, repository, providers)
    assert artifact.selected == 0
    await engine.reconcile_executions()

    # Operator jumps directly from A (index 0) to C (index 2), skipping B
    # (index 1) entirely.
    result = await manual_candidate_failover(engine, canonical.id, artifact.id, str(artifact.candidates[2].id))
    assert result["candidate_id"] == str(artifact.candidates[2].id)
    switched = (await repository.artifacts(canonical.id))[0]
    assert switched.selected == 2
    await engine.reconcile_executions()

    _handles, exhausted = await _exhaust_current(
        engine, repository, executor, canonical.id, remote_failure(), attempts=3,
    )
    # B (index 1) was never attempted and must remain reachable to automatic
    # recovery even though the operator jumped straight past it to C.
    assert exhausted.selected == 1 and exhausted.state == "queued" and exhausted.execution is None


@pytest.mark.asyncio
async def test_candidate_attempt_history_prevents_cycle_within_generation(tmp_path, monkeypatch):
    _engine, repository, _executor, _canonical, exhausted = await _switch_switch_exhaust(tmp_path, monkeypatch)
    # A (retired by the first manual switch), B (retired by the second), and
    # C (just exhausted) have all been attempted this recovery generation --
    # automatic search must park, never cycle back to A or B.
    assert exhausted.execution is None
    assert exhausted.state == "error"
    context = await repository.recovery_context(exhausted.id)
    assert context.get("quiescence_reason") == "recovery_exhausted"
    assert set(context.get("candidate_attempt_history") or ()) == {
        str(candidate.id) for candidate in exhausted.candidates
    }


@pytest.mark.asyncio
async def test_new_recovery_generation_resets_candidate_attempt_scope(tmp_path, monkeypatch):
    engine, repository, _executor, canonical, exhausted = await _switch_switch_exhaust(tmp_path, monkeypatch)
    assert exhausted.state == "error"

    await engine.retry(canonical.id)
    reset_context = await repository.recovery_context(exhausted.id)
    # USER_RETRY is a full-reset boundary (Section 12): a fresh recovery
    # generation must not still treat every candidate as already tried.
    assert not (reset_context.get("candidate_attempt_history") or [])
    assert reset_context.get("quiescence_reason") is None


@pytest.mark.asyncio
async def test_refreshed_new_candidate_enters_current_traversal(tmp_path, monkeypatch):
    class ReissuingParcelProvider(HostParcelProvider):
        def __init__(self, identity, *, expire=False):
            super().__init__(identity)
            self._expire = expire

        def candidate(self, name="same.bin", *, payload="parcel"):
            base = super().candidate(name, payload=payload)
            return replace(base, expires_at=1.0) if self._expire else base

        async def refresh(self, candidate):
            reissued = replace(candidate, id=f"{candidate.id}-reissued-{uuid.uuid4().hex[:8]}", expires_at=None)
            return ResolutionResult(ResourceState.AVAILABLE, (reissued,))

    providers = (
        ReissuingParcelProvider("provider-a"),
        ReissuingParcelProvider("provider-b", expire=True),
        ReissuingParcelProvider("provider-c"),
    )
    engine, repository, providers, _executor, _now_box = await build_engine3(tmp_path, monkeypatch, providers)
    canonical, artifact = await attach_three(engine, repository, providers)
    original_b_id = str(artifact.candidates[1].id)
    original_a_id = str(artifact.candidates[0].id)
    assert artifact.candidates[1].expires_at is not None and artifact.candidates[1].expires_at <= engine.clock()

    # The operator names B by its (expired) id; manual_candidate_failover
    # must refresh it first, then activate the REISSUED candidate.
    await manual_candidate_failover(engine, canonical.id, artifact.id, original_b_id)
    switched = (await repository.artifacts(canonical.id))[0]
    reissued_b = switched.candidates[1]
    assert str(reissued_b.id) != original_b_id
    assert switched.selected == 1

    context = await repository.recovery_context(switched.id)
    attempted = set(context.get("candidate_attempt_history") or ())
    assert str(reissued_b.id) in attempted, "the reissued (live) id must be tracked, not the stale pre-refresh id"
    assert original_b_id not in attempted
    assert original_a_id in attempted


@pytest.mark.asyncio
async def test_manual_and_automatic_activation_use_same_core_transition(tmp_path, monkeypatch):
    engine, repository, providers, executor, _now_box = await build_engine3(tmp_path, monkeypatch)
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()

    before = await repository.recovery_context(artifact.id)
    await manual_candidate_failover(engine, canonical.id, artifact.id, str(artifact.candidates[1].id))
    after_manual = await repository.recovery_context(artifact.id)
    assert after_manual["candidate_switches"] == before["candidate_switches"] + 1
    assert after_manual["consecutive_no_progress_failures"] == 0
    assert after_manual["candidate_refreshes"] == 0
    manual_attempted = set(after_manual.get("candidate_attempt_history") or ())
    assert manual_attempted == {str(artifact.candidates[0].id), str(artifact.candidates[1].id)}

    await engine.reconcile_executions()
    _handles, exhausted_to_c = await _exhaust_current(
        engine, repository, executor, canonical.id, remote_failure(), attempts=3,
    )
    after_auto = await repository.recovery_context(exhausted_to_c.id)
    # The automatic TRY_ALTERNATE_CANDIDATE switch (transfers.candidate_activation
    # .activate_candidate, via convergence_engine.py) must land on the
    # exact same durable bookkeeping shape as the manual switch above.
    assert after_auto["candidate_switches"] == after_manual["candidate_switches"] + 1
    assert after_auto["consecutive_no_progress_failures"] == 0
    assert after_auto["candidate_refreshes"] == 0
    auto_attempted = set(after_auto.get("candidate_attempt_history") or ())
    assert auto_attempted == manual_attempted | {str(exhausted_to_c.candidates[2].id)}
    assert exhausted_to_c.selected == 2 and exhausted_to_c.state == "queued"


@pytest.mark.asyncio
async def test_candidate_activation_advances_or_fences_recovery_generation(tmp_path, monkeypatch):
    engine, repository, providers, _executor, _now_box = await build_engine3(tmp_path, monkeypatch)
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    artifact = (await repository.artifacts(canonical.id))[0]

    before = await repository.recovery_context(artifact.id)

    # Fenced: an invalid target must leave the recovery state completely
    # untouched -- one canonical operation is either a full commit or a true
    # no-op, never a partial mutation. Holding the real claim is the CALLER's
    # act (it legitimately advances the generation); the fence asserted here
    # is that the activation primitive itself changes nothing beyond it.
    claim = await repository.claim_recovery(artifact.id, RecoveryTrigger.USER_CANDIDATE_SWITCH, engine.clock())
    assert claim is not None and claim.generation >= 1
    held = await repository.recovery_context(artifact.id)
    rejected = await activate_candidate(engine, artifact, artifact.selected, retry_at=0, claim=claim)
    assert rejected.committed is False and rejected.reason == "invalid_target"
    assert await repository.recovery_context(artifact.id) == held
    await repository.finish_recovery_claim(claim, action="candidate_activation", reason=rejected.reason, outcome="not_applied")

    # Advanced: a valid target commits under a NEW claim generation and moves the switch bookkeeping forward.
    accepted = await activate_with_real_claim(engine, artifact, 1, retry_at=0, trigger=RecoveryTrigger.USER_CANDIDATE_SWITCH)
    assert accepted.committed is True
    advanced = await repository.recovery_context(artifact.id)
    assert advanced["candidate_switches"] == before["candidate_switches"] + 1
    assert advanced["consecutive_no_progress_failures"] == 0
    assert set(advanced.get("candidate_attempt_history") or ()) == {
        str(artifact.candidates[0].id), str(artifact.candidates[1].id),
    }


@pytest.mark.asyncio
async def test_stale_recovery_claim_cannot_overwrite_new_candidate(tmp_path, monkeypatch):
    engine, repository, providers, _executor, _now_box = await build_engine3(tmp_path, monkeypatch)
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    stale_view = (await repository.artifacts(canonical.id))[0]
    assert stale_view.selected == 0 and stale_view.execution is not None

    # A newer, up-to-date activation legitimately switches to B and starts a
    # fresh, genuinely active writer for it.
    fresh_result = await engine.activate_candidate_command(canonical.id, artifact.id, 1)
    assert fresh_result is not None and fresh_result.committed is True
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]
    assert live.selected == 1 and live.execution is not None

    # A stale caller still holding the ORIGINAL pre-switch snapshot (which
    # believes A's now-superseded execution is still the live writer) must
    # not be able to commit a second switch out from under the now-active
    # B writer (Section 11: a stale claim can never overwrite a newer one).
    stale_attempt = await activate_with_real_claim(engine, stale_view, 2, retry_at=0)
    assert stale_attempt.committed is False
    assert stale_attempt.reason == "execution_changed_concurrently"
    unchanged = (await repository.artifacts(canonical.id))[0]
    assert unchanged.selected == 1 and unchanged.execution == live.execution


# ---------------------------------------------------------------------------
# Truthful post-commit acknowledgement (Section 26)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_committed_candidate_activation_never_returns_plain_failure_due_to_postcommit_projection(
    tmp_path, monkeypatch,
):
    """A post-commit failure need not be limited to parent re-aggregation --
    the durable success-provenance write itself can also fail after the
    candidate switch is already committed. Section 26 applies identically:
    the operator must still receive a truthful success, not a fabricated
    failure, for a switch that genuinely happened."""
    engine, repository, first, second, _executor = await build_engine2(tmp_path, monkeypatch)
    canonical, _source, artifact = await attach_two(engine, repository, first, second)
    wanted = artifact.candidates[1]

    async def boom(*_a, **_k):
        raise RuntimeError("simulated durable provenance write failure")

    monkeypatch.setattr(repository, "record_manual_candidate_failover", boom)
    result = await manual_candidate_failover(engine, canonical.id, artifact.id, str(wanted.id))
    assert result["ok"] is True
    assert result["reconciliation_pending"] is True

    switched = (await repository.artifacts(canonical.id))[0]
    assert switched.selected == 1 and switched.execution is None and switched.state == "queued"


@pytest.mark.asyncio
async def test_activation_history_never_records_contradictory_success_and_failure(tmp_path, monkeypatch):
    engine, repository, first, second, _executor = await build_engine2(tmp_path, monkeypatch)
    canonical, _source, artifact = await attach_two(engine, repository, first, second)
    wanted = artifact.candidates[1]

    async def boom(*_a, **_k):
        raise RuntimeError("simulated durable provenance write failure")

    monkeypatch.setattr(repository, "record_manual_candidate_failover", boom)
    result = await manual_candidate_failover(engine, canonical.id, artifact.id, str(wanted.id))
    assert result.get("reconciliation_pending") is True

    events = (await repository.presentation(canonical.id, details=True))["manual_candidate_failovers"]
    failures = [item for item in events if item["outcome"] == "failure"]
    assert len(failures) == 0, (
        "a post-commit provenance-write failure must never be recorded as a "
        "contradictory candidate-switch failure event for a switch that "
        "genuinely succeeded"
    )


# ---------------------------------------------------------------------------
# One canonical activation owner (Section 10 correction round)
# ---------------------------------------------------------------------------


def test_one_canonical_candidate_activation_owner_for_both_engine_stacks():
    """DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
    closure): the lower, pre-Phase-3 ``transfers.engine.TransferEngine``
    stack (shared ancestry with the production
    ``convergence_engine.TransferEngine`` via ``transfers._engine_recovery``)
    no longer retains ANY candidate-mutation or recovery-decision
    implementation of its own -- not even a thin delegate. The prior
    "one shared mutation, two decision wrappers" compromise (DP 1.0.12
    recovery leveling, Section 10) still let a materially complete
    alternate recovery lifecycle exist below the canonical owner, whose
    correctness depended on ``convergence_engine.TransferEngine`` always
    being layered on top to shadow it (production transfer 265 proved this
    is not a safe invariant to depend on for parent-lifecycle authority, and
    the same MRO-shadowing risk applied here). There is now exactly ONE
    recovery-decision/candidate-activation owner in the whole engine MRO:
    ``transfers.convergence_engine.TransferEngine``, which funnels every
    candidate switch through ``transfers.candidate_activation
    .activate_candidate``."""
    import inspect

    from transfers import _engine_recovery, candidate_activation, convergence_engine

    for forbidden in ("_activate_alternate", "_decide_recovery", "_terminal_recovery", "_try_exhausted_alternate"):
        assert not hasattr(_engine_recovery.TransferEngine, forbidden), (
            f"{forbidden!r} must not exist on the lower engine stack -- it would be a "
            "second, independent recovery-decision implementation"
        )

    canonical_source = inspect.getsource(convergence_engine.TransferEngine._apply_recovery_decision)
    assert "activate_candidate(" in canonical_source

    activation_source = inspect.getsource(candidate_activation.activate_candidate)
    assert "retire_partial(" in activation_source
    assert "transition_recovery(" in activation_source
    assert "record_candidate_attempt(" in activation_source


def test_production_composition_uses_the_final_leveled_engine_and_repository():
    """Base document Section 35's architecture assertion, direct form:
    "production recovery tests instantiate final production engine/repository
    MRO." Every named regression in this file (and the harness in
    ``tests/production_stack_harness.py``) builds its own
    ``transfers.convergence_engine.TransferEngine`` +
    ``transfers.recovery_repository.TransferRepository`` instances and
    documents, in prose, that this "matches ``application.composition
    .compose()`` symbol-for-symbol" -- but nothing previously checked that
    ``compose()`` still actually binds those same two classes rather than a
    stale or substituted pair. Importing ``main`` (as
    ``test_canonical_http_route_ownership.py`` already safely does)
    transitively imports and executes ``application.composition.compose()``;
    this asserts the exact classes it bound by object identity, not by name
    or source text."""
    import main  # noqa: F401 -- triggers application.composition.compose()
    from application import composition
    from transfers.convergence_engine import TransferEngine as ProductionEngine
    from transfers.recovery_repository import TransferRepository as ProductionRepository

    assert composition.TransferEngine is ProductionEngine
    assert composition.TransferRepository is ProductionRepository
    assert type(composition.application.engine) is ProductionEngine
    assert type(composition.application.engine.repository) is ProductionRepository


# ---------------------------------------------------------------------------
# Shared partial/resume policy (Section 28) and full provenance (Section 29)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_and_automatic_paths_apply_identical_partial_resume_policy(tmp_path, monkeypatch):
    """Section 28: both callers reuse partial bytes under the identical
    condition (same executor, same resumable sidecar contract) -- proven here
    on the SAME production stack for both a manual and an automatic switch,
    both sharing one executor/target so reuse is the correct outcome."""
    engine, repository, providers, executor, _now_box = await build_engine3(tmp_path, monkeypatch)
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]
    from pathlib import Path
    target = Path(live.target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"partial-bytes")

    manual_result = await manual_candidate_failover(engine, canonical.id, artifact.id, str(artifact.candidates[1].id))
    assert manual_result["ok"] is True
    # All three candidates route through the same fake MemoryExecutor against
    # the same local target: the manual switch must REUSE, not retire.
    assert target.exists()

    await engine.reconcile_executions()
    _handles, exhausted = await _exhaust_current(
        engine, repository, executor, canonical.id, remote_failure(), attempts=3,
    )
    assert exhausted.selected == 2
    assert target.exists(), "the automatic switch must apply the identical reuse policy, not a divergent one"


@pytest.mark.asyncio
async def test_candidate_activation_provenance_records_all_required_fields(tmp_path, monkeypatch):
    """Section 29: for every activation, retain transfer, artifact, old/new
    candidate + provider identity, authority/reason, recovery generation,
    old execution identity, partial-state decision, admission-continuation
    decision, and outcome."""
    engine, repository, providers, _executor, _now_box = await build_engine3(
        tmp_path, monkeypatch, max_active_executions=1,
    )
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]
    old_handle = live.execution
    assert old_handle is not None

    # The recorded authority and generation are exactly the real claim's -- nothing is defaulted or fabricated.
    claim = await repository.claim_recovery(live.id, RecoveryTrigger.EXECUTOR_RECOVERY, engine.clock())
    assert claim is not None
    result = await activate_candidate(engine, live, 1, retry_at=engine.clock(), claim=claim)
    await repository.finish_recovery_claim(claim, action="candidate_activation", reason=result.reason, outcome="activated")
    assert result.committed

    async with get_db() as db:
        rows = await db.fetchall(
            "SELECT detail FROM application_events WHERE transfer_id=? AND kind='candidate_activation' ORDER BY id",
            (canonical.id,),
        )
    from transfers import codec
    events = [codec.load(row["detail"], {}) for row in rows]
    record = events[-1]
    assert record["transfer_id"] == canonical.id
    assert record["artifact_id"] == artifact.id
    assert record["old_candidate_id"] == str(artifact.candidates[0].id)
    assert record["old_provider_id"] == providers[0].descriptor.id
    assert record["new_candidate_id"] == str(artifact.candidates[1].id)
    assert record["new_provider_id"] == providers[1].descriptor.id
    assert record["authority"] == RecoveryTrigger.EXECUTOR_RECOVERY.value == "executor_recovery"
    assert record["recovery_generation"] == claim.generation and claim.generation >= 1
    assert record["old_execution_id"] == old_handle.attempt_id
    assert record["partial_decision"] in {"reused", "retired"}
    assert record["admission_decision"] == "reserved"  # the old writer held the only slot
    assert record["outcome"] == "activated"
    assert record["new_execution_id"] is None  # not knowable before the replacement ever dispatches


# ---------------------------------------------------------------------------
# Section 26 applied to the new canonical activation provenance write
# ---------------------------------------------------------------------------


def _break_activation_payload_encoding(monkeypatch, module):
    """Fail only the encoding of the candidate-activation provenance payload
    (identified by its unique ``admission_decision`` key) -- everything else
    ``codec.dump`` is asked to encode inside the same call (the normalized
    error, etc.) still works normally."""
    original_dump = module.codec.dump

    def failing_dump(value):
        if isinstance(value, dict) and "admission_decision" in value:
            raise RuntimeError("simulated candidate_activation provenance encoding failure")
        return original_dump(value)

    monkeypatch.setattr(module.codec, "dump", failing_dump)


@pytest.mark.asyncio
async def test_candidate_activation_provenance_write_is_atomic_with_the_commit(tmp_path, monkeypatch):
    """Section 26/29: the canonical activation provenance write is now part
    of the SAME transaction as the candidate-selection commit
    (transition_recovery's ``activation_provenance`` parameter) -- "committed
    but provenance lost" is structurally impossible. If that write fails, the
    WHOLE transaction rolls back: nothing durably changed, and the command
    correctly reports a real rejection rather than a fabricated success OR an
    uncaught exception."""
    import transfers.repository as repository_module

    engine, repository, providers, _executor, now_box = await build_engine3(tmp_path, monkeypatch)
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]

    _break_activation_payload_encoding(monkeypatch, repository_module)

    result = await activate_with_real_claim(engine, live, 1, retry_at=now_box[0])
    assert result.committed is False
    assert result.reason == "commit_conflict"

    unchanged = (await repository.artifacts(canonical.id))[0]
    assert unchanged.selected == 0, "the candidate switch must roll back with its provenance write"
    assert unchanged.execution is not None  # the original writer is untouched
    assert await repository.continuation_reservation(artifact.id) is None

    # No half-written "activated" record exists -- the INSERT rolled back
    # together with the candidate-selection UPDATE in the same transaction.
    async with get_db() as db:
        rows = await db.fetchall(
            "SELECT detail FROM application_events WHERE transfer_id=? AND kind='candidate_activation'",
            (canonical.id,),
        )
    from transfers import codec as _codec
    activated = [_codec.load(row["detail"], {}) for row in rows if _codec.load(row["detail"], {}).get("outcome") == "activated"]
    assert activated == []


@pytest.mark.asyncio
async def test_manual_switch_reports_real_rejection_when_activation_provenance_cannot_commit(tmp_path, monkeypatch):
    """End-to-end through the operator-facing command: since the provenance
    write is now atomic with the commit, a failure there means the switch
    itself genuinely did not happen -- the command must raise a real,
    retryable TransferError (nothing was durably mutated), never a fabricated
    success and never an uncaught, uncategorized exception."""
    import transfers.repository as repository_module
    from transfers.errors import Category, TransferError as _TransferError

    engine, repository, first, second, _executor = await build_engine2(tmp_path, monkeypatch)
    canonical, _source, artifact = await attach_two(engine, repository, first, second)
    wanted = artifact.candidates[1]

    _break_activation_payload_encoding(monkeypatch, repository_module)

    with pytest.raises(_TransferError) as excinfo:
        await manual_candidate_failover(engine, canonical.id, artifact.id, str(wanted.id))
    assert excinfo.value.error.category == Category.RESOURCE_STATE_CONFLICT

    unchanged = (await repository.artifacts(canonical.id))[0]
    assert unchanged.selected == 0, "nothing durably changed when the atomic commit+provenance write failed"


# ---------------------------------------------------------------------------
# Canonical-authority guardrails (Canonical Release Remediation, Workstream B)
#
# Every production candidate activation is attributable to exactly one REAL
# recovery claim (authority + generation). These prove the ABSENCE of the
# retired claim-less semantic -- not merely the presence of the claim path --
# structurally (signature + AST), so a comment-only regression cannot hide it.
# ---------------------------------------------------------------------------

_BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _production_modules():
    """(relative path, parsed AST) for every non-test backend module."""
    for path in sorted(_BACKEND_ROOT.rglob("*.py")):
        relative = path.relative_to(_BACKEND_ROOT)
        if "tests" in relative.parts or "__pycache__" in relative.parts:
            continue
        yield relative.as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def test_activate_candidate_signature_requires_a_keyword_only_claim():
    parameter = inspect.signature(activate_candidate).parameters["claim"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty, "claim must have no default (no claim=None mode)"


def test_activate_candidate_source_has_no_claimless_authority_path():
    source = inspect.getsource(candidate_activation)
    tree = ast.parse(source)
    for forbidden in ("claim=None", "claim is None", "claim is not None", "claim: None"):
        assert forbidden not in source, forbidden

    function = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "activate_candidate"
    )
    claim_names = {"claim"}

    def mentions_claim(node) -> bool:
        return any(isinstance(item, ast.Name) and item.id in claim_names for item in ast.walk(node))

    for node in ast.walk(function):
        # No comparison of the claim against None (or anything else that makes it optional).
        if isinstance(node, ast.Compare) and mentions_claim(node):
            assert not any(isinstance(item, ast.Constant) and item.value is None for item in ast.walk(node))
        # No conditional expression / branch that picks a different authority when the claim is absent.
        # Exactly two guard shapes may test the claim, and both only ever REFUSE: the fail-closed type guard
        # (raises) and the currency fence (returns a not-committed result). Neither selects another authority.
        if isinstance(node, ast.IfExp) and mentions_claim(node.test):
            raise AssertionError("activate_candidate must not select an authority based on its claim")
        if isinstance(node, ast.If) and mentions_claim(node.test):
            guard = ast.unparse(node.test)
            assert not node.orelse and len(node.body) == 1, guard
            if guard == "not isinstance(claim, RecoveryClaim)":
                assert isinstance(node.body[0], ast.Raise)
            else:
                assert guard == "not await engine.repository.recovery_claim_current(claim, now=engine.clock())", guard
                assert isinstance(node.body[0], ast.Return) and "claim_not_current" in ast.unparse(node.body[0])
        # No fabricated generic authority literal.
        if isinstance(node, ast.Constant) and node.value == "automatic":
            raise AssertionError('activate_candidate must not fabricate a generic "automatic" authority')

    # Authority and generation derive from the claim and from nothing else.
    assigned = {
        target.id: ast.unparse(node.value)
        for node in function.body if isinstance(node, ast.Assign)
        for target in node.targets if isinstance(target, ast.Name)
    }
    assert assigned["authority"] == "claim.trigger.value"
    assert assigned["recovery_generation"] == "claim.generation"

    # The claim is authority, not only provenance: the one durable commit is atomically fenced by it.
    commits = [
        node for node in ast.walk(function)
        if isinstance(node, ast.Call) and _call_name(node) == "transition_recovery"
    ]
    assert len(commits) == 1
    fence = next((keyword.value for keyword in commits[0].keywords if keyword.arg == "claim"), None)
    assert isinstance(fence, ast.Name) and fence.id == "claim"


@pytest.mark.asyncio
async def test_activate_candidate_rejects_a_missing_or_fabricated_claim_before_touching_state():
    with pytest.raises(TypeError):
        await activate_candidate(None, None, 0, retry_at=0)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await activate_candidate(None, None, 0, retry_at=0, claim=None)  # type: ignore[arg-type]
    fabricated = SimpleNamespace(trigger=SimpleNamespace(value="automatic"), generation=0)
    with pytest.raises(TypeError):
        await activate_candidate(None, None, 0, retry_at=0, claim=fabricated)  # type: ignore[arg-type]


def test_every_production_activation_call_passes_a_claim_from_the_one_engine_owner():
    calls = []
    importers = []
    for relative, tree in _production_modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) == "activate_candidate":
                calls.append((relative, node))
            # Importing the read-only helpers (e.g. resolve_candidate_index) is fine; only the MUTATION is fenced.
            if isinstance(node, ast.ImportFrom) and node.module == "transfers.candidate_activation" and any(
                alias.name == "activate_candidate" for alias in node.names
            ):
                importers.append(relative)
    assert {relative for relative, _ in calls} == {"transfers/convergence_engine.py"}
    assert importers == ["transfers/convergence_engine.py"]
    # Automatic (TRY_ALTERNATE_CANDIDATE) and manual (activate_candidate_command): the two, and only two, callers.
    assert len(calls) == 2
    for _relative, node in calls:
        claim = next((keyword.value for keyword in node.keywords if keyword.arg == "claim"), None)
        assert isinstance(claim, ast.Name) and claim.id == "claim", ast.unparse(node)


# ---------------------------------------------------------------------------
# The required claim is real AUTHORITY, not provenance metadata (Workstream B).
# A claim that was superseded (newer generation) or expired (lease) cannot
# retire a writer, retire partial bytes, or switch a candidate.
# ---------------------------------------------------------------------------


async def _stale_and_fresh_claims(repository, engine, artifact_id):
    stale = await repository.claim_recovery(artifact_id, RecoveryTrigger.AUTO_RETRY, engine.clock())
    assert stale is not None
    await repository.finish_recovery_claim(stale, action="candidate_activation", reason="released", outcome="not_applied")
    fresh = await repository.claim_recovery(artifact_id, RecoveryTrigger.USER_RETRY, engine.clock())
    assert fresh is not None and fresh.generation > stale.generation
    return stale, fresh


@pytest.mark.asyncio
async def test_superseded_claim_is_rejected_before_any_side_effect_and_the_current_claim_still_works(tmp_path, monkeypatch):
    engine, repository, providers, executor, _now_box = await build_engine3(tmp_path, monkeypatch)
    canonical, _artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]
    assert live.execution is not None and live.selected == 0
    stale, fresh = await _stale_and_fresh_claims(repository, engine, live.id)

    result = await activate_candidate(engine, live, 1, retry_at=engine.clock(), claim=stale)
    assert result.committed is False and result.reason == "claim_not_current"
    unchanged = (await repository.artifacts(canonical.id))[0]
    assert unchanged.selected == 0 and unchanged.execution == live.execution
    # Nothing was cancelled or retired on behalf of a claim that no longer holds authority.
    assert executor.jobs[live.execution.attempt_id].state not in {
        ExecutionState.CANCELLED, ExecutionState.FAILED, ExecutionState.ABSENT,
    }

    current = await activate_candidate(engine, live, 1, retry_at=engine.clock(), claim=fresh)
    assert current.committed is True and current.new_candidate is not None
    await repository.finish_recovery_claim(fresh, action="candidate_activation", reason=current.reason, outcome="activated")


@pytest.mark.asyncio
async def test_expired_claim_lease_cannot_switch_a_candidate(tmp_path, monkeypatch):
    engine, repository, providers, _executor, now_box = await build_engine3(tmp_path, monkeypatch)
    canonical, _artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]
    claim = await repository.claim_recovery(live.id, RecoveryTrigger.AUTO_RETRY, engine.clock(), lease_seconds=5)
    assert claim is not None

    now_box[0] += 3600
    result = await activate_candidate(engine, live, 1, retry_at=engine.clock(), claim=claim)
    assert result.committed is False and result.reason == "claim_not_current"
    assert (await repository.artifacts(canonical.id))[0].selected == 0


@pytest.mark.asyncio
async def test_candidate_commit_is_atomically_claim_fenced_even_if_the_entry_check_is_bypassed(tmp_path, monkeypatch):
    """Defense in depth: a claim superseded AFTER the entry check (mid-flight) is refused by the one atomic commit."""
    engine, repository, providers, _executor, _now_box = await build_engine3(tmp_path, monkeypatch)
    canonical, _artifact = await attach_three(engine, repository, providers)
    live = (await repository.artifacts(canonical.id))[0]
    assert live.execution is None  # no writer to retire: the commit itself is the only fence exercised
    stale, _fresh = await _stale_and_fresh_claims(repository, engine, live.id)

    async def entry_check_passes(*_args, **_kwargs):
        return True

    monkeypatch.setattr(repository, "recovery_claim_current", entry_check_passes)
    result = await activate_candidate(engine, live, 1, retry_at=engine.clock(), claim=stale)
    assert result.committed is False and result.reason == "commit_conflict"
    assert (await repository.artifacts(canonical.id))[0].selected == 0


@pytest.mark.asyncio
async def test_stale_claim_racing_a_new_candidate_switch_cannot_overwrite_it(tmp_path, monkeypatch):
    engine, repository, providers, _executor, _now_box = await build_engine3(tmp_path, monkeypatch)
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]
    stale = await repository.claim_recovery(live.id, RecoveryTrigger.AUTO_RETRY, engine.clock())
    await repository.finish_recovery_claim(stale, action="candidate_activation", reason="released", outcome="not_applied")

    stale_attempt, fresh_switch = await asyncio.gather(
        activate_candidate(engine, live, 2, retry_at=engine.clock(), claim=stale),
        engine.activate_candidate_command(canonical.id, artifact.id, 1),
    )
    assert stale_attempt.committed is False and stale_attempt.reason == "claim_not_current"
    assert fresh_switch is not None and fresh_switch.committed is True
    final = (await repository.artifacts(canonical.id))[0]
    assert final.selected == 1, "the newer authority's candidate stands; the stale claim wrote nothing"
