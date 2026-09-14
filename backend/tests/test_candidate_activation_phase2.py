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

import uuid
from dataclasses import replace

import pytest

import db.database as database
from db.database import get_db
from fake_integrations import MemoryExecutor
from test_manual_candidate_failover import HostParcelProvider, attach_two
from test_manual_candidate_failover import build_engine as build_engine2
from test_ws2p1_failover_depth import remote_failure
from transfers.candidate_activation import activate_candidate
from transfers.convergence_engine import TransferEngine
from transfers.manual_failover import manual_candidate_failover
from transfers.models import ExecutionState, ResolutionResult, ResourceState, TransferRequest
from transfers.policy import TransferPolicy
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
    # .activate_candidate, via _convergence_phase3_base.py) must land on the
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

    # Fenced: an invalid target must leave the generation completely
    # untouched -- one canonical operation is either a full commit or a
    # true no-op, never a partial mutation.
    rejected = await activate_candidate(engine, artifact, artifact.selected, retry_at=0)
    assert rejected.committed is False and rejected.reason == "invalid_target"
    unchanged = await repository.recovery_context(artifact.id)
    assert unchanged == before

    # Advanced: a valid target commits and moves the generation forward.
    accepted = await activate_candidate(engine, artifact, 1, retry_at=0)
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
    stale_attempt = await activate_candidate(engine, stale_view, 2, retry_at=0)
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
    """DP 1.0.12 recovery leveling, Section 10 correction: the lower,
    pre-Phase-3 ``transfers.engine.TransferEngine`` stack (shared ancestry
    with the production ``convergence_engine.TransferEngine`` via
    ``transfers._engine_recovery``) must not retain its own independent
    candidate-mutation implementation alongside
    ``transfers.candidate_activation.activate_candidate``. A new module was
    not supposed to be created while the old implementation stayed intact --
    this asserts it did not."""
    import inspect

    from transfers import _engine_recovery, candidate_activation

    legacy_source = inspect.getsource(_engine_recovery.TransferEngine._activate_alternate)
    assert "activate_candidate(" in legacy_source
    for forbidden in ("retire_partial(", "transition_recovery(", "record_candidate_attempt("):
        assert forbidden not in legacy_source, (
            f"{forbidden!r} in _activate_alternate indicates a second, independent "
            "candidate-mutation implementation instead of a thin delegate"
        )

    canonical_source = inspect.getsource(candidate_activation.activate_candidate)
    assert "retire_partial(" in canonical_source
    assert "transition_recovery(" in canonical_source
    assert "record_candidate_attempt(" in canonical_source


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


@pytest.mark.asyncio
async def test_legacy_stack_automatic_switch_uses_canonical_operation_and_provenance(tmp_path, monkeypatch):
    """Behavioral half of the Section 10 proof: driving an automatic failover
    on the LOWER ``transfers.engine.TransferEngine`` stack still produces the
    one canonical durable activation-provenance record (authority falls back
    to ``"automatic"`` there, since that stack has no recovery-claim system),
    not a second, independently-shaped record."""
    from test_ws2p1_failover_depth import advance_a_to_b, attach_two as attach_two_legacy, remote_failure as legacy_failure
    from test_ws2p1_failover_progress import EquivalentParcelProvider, NoProgressMemoryExecutor, build_engine as build_engine_legacy
    from transfers.policy import TransferPolicy as LegacyPolicy

    first = EquivalentParcelProvider("provider-a")
    second = EquivalentParcelProvider("provider-b")
    executor = NoProgressMemoryExecutor(None)
    policy = LegacyPolicy(max_attempts=3, retry_delay=0, adoption_stability_seconds=0)
    engine, repository, _registry = await build_engine_legacy(
        tmp_path, monkeypatch, (first, second), executor, policy=policy,
    )
    canonical, _source = await attach_two_legacy(engine, repository, first, second)
    failure = legacy_failure()
    _a_attempts, artifact = await advance_a_to_b(engine, repository, executor, canonical.id, failure)
    assert artifact.selected == 1  # switched to provider-b automatically

    async with get_db() as db:
        rows = await db.fetchall(
            "SELECT detail FROM application_events WHERE transfer_id=? AND kind='candidate_activation' ORDER BY id",
            (canonical.id,),
        )
    from transfers import codec
    events = [codec.load(row["detail"], {}) for row in rows]
    activated = [event for event in events if event.get("outcome") == "activated"]
    assert activated, "the legacy stack's switch must record the SAME canonical provenance kind"
    assert activated[-1]["authority"] == "automatic"
    assert activated[-1]["new_candidate_id"] == str(artifact.candidates[1].id)


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

    result = await activate_candidate(engine, live, 1, retry_at=engine.clock())
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
    assert record["authority"] == "automatic"
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

    result = await activate_candidate(engine, live, 1, retry_at=now_box[0])
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
