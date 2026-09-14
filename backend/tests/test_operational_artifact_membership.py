"""Canonical artifact membership and presentation-truth qualification
(DP 1.0.12 recovery leveling, Phase 1: Sections 7-9).

Built on the real production engine/repository ownership chain
(``tests.production_stack_harness.build_production_runtime``) rather than the
lower ``transfers.engine``/``transfers.manual_repository`` stack, per Section 32.
Every scenario test below instantiates that harness; see the module docstring
of ``production_stack_harness`` for exactly which production classes that is.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

import db.database as database
from fake_integrations import MemoryExecutor
from transfers.models import (
    Capability, InputField, InputFieldDescriptor, InputMethod, InputMethodDescriptor,
    InputReason, InputRequirement, IntegrationDescriptor, TransferRequest,
)
from transfers.presentation_repository import is_autonomous_presentation

from production_stack_harness import build_production_runtime


async def _simulate_requires_attention(repository, artifact_id, *, reason="recovery_budget_exhausted"):
    """Durably persist the exact combination a genuinely exhausted recovery
    leaves an artifact in: a recorded wait_for_operator decision plus the
    matching quiescence/wake transition (mirrors
    test_manual_candidate_failover.py's ``_simulate_stale_operator_attention``
    and test_transfer_recovery_phase4.py's own attention fixture)."""
    await repository.record_recovery_decision(artifact_id, "wait_for_operator", reason)
    assert await repository.transition_recovery(
        artifact_id, "error", retry_at=0,
        quiescence_reason="recovery_exhausted", wake_condition="operator_retry",
    )


async def _two_artifact_transfer(engine, repository, provider, *, names=("a.bin", "b.bin")):
    """One transfer, two independently-requested artifacts sharing no candidate."""
    transfer = await engine.submit(tuple(
        TransferRequest("parcel", f"box-{name}", name=name) for name in names
    ), deduplicate=False)
    await engine.resolve_pending()
    artifacts = {item.name: item for item in await repository.artifacts(transfer.id)}
    assert set(artifacts) == set(names)
    return transfer, artifacts


# ---------------------------------------------------------------------------
# Section 7 -- Canonical Artifact Membership
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_blocked_attention_child_does_not_vote_in_parent_presentation(tmp_path, monkeypatch):
    repository, registry, (provider,), executor, engine, now_box = await build_production_runtime(
        tmp_path, monkeypatch, db_name="membership_blocked.db",
    )
    transfer, artifacts = await _two_artifact_transfer(engine, repository, provider)
    healthy, attention = artifacts["a.bin"], artifacts["b.bin"]

    # Deselect (block) the second artifact through the real universal
    # file-selection path, then let it durably reach requires_attention --
    # exactly "blocked failed child + healthy actionable child" (Section 7).
    await repository.select_artifact(transfer.id, attention.id, False)
    await _simulate_requires_attention(repository, attention.id)

    presentation = await repository.presentation(transfer.id)
    assert presentation["presentation_status"] != "requires_attention", (
        "a blocked child's stale attention must not vote in aggregate presentation"
    )
    assert presentation["attention_required"] is False


@pytest.mark.asyncio
async def test_standby_attention_child_does_not_vote_in_parent_presentation(tmp_path, monkeypatch):
    repository, registry, (provider,), executor, engine, now_box = await build_production_runtime(
        tmp_path, monkeypatch, db_name="membership_standby.db",
    )
    transfer, artifacts = await _two_artifact_transfer(engine, repository, provider)
    healthy, attention = artifacts["a.bin"], artifacts["b.bin"]

    # mirror_state='standby' is durable mirror-cohort state (transfers/canonical.py);
    # this is the same shape a real duplicate/mirror consolidation persists.
    # No repository primitive constructs a standby row directly without full
    # duplicate-cohort detection, so the fixture sets the durable column the
    # same way test fixtures elsewhere set up recovery-context edge cases that
    # have no dedicated repository setter (see e.g. _row helpers reading raw
    # rows in test_transfer_preparing_presentation.py).
    async with database.get_db() as db:
        await db.execute("UPDATE download_files SET mirror_state='standby' WHERE id=?", (attention.id,))
        await db.commit()
    await _simulate_requires_attention(repository, attention.id)

    presentation = await repository.presentation(transfer.id)
    assert presentation["presentation_status"] != "requires_attention", (
        "a standby child's stale attention must not vote in aggregate presentation"
    )
    assert presentation["attention_required"] is False


@pytest.mark.asyncio
async def test_consolidated_contributor_with_stale_attention_does_not_vote(tmp_path, monkeypatch):
    """A cross-transfer consolidation contributor: standby (the same durable
    state real duplicate/mirror consolidation leaves a contributing artifact
    in, transfers/canonical.py) PLUS a real ``artifact_consolidations`` row
    recording it as absorbed into a canonical artifact elsewhere. Its stale
    recovery attention must not vote on ITS OWN (source) transfer either."""
    repository, registry, (provider,), executor, engine, now_box = await build_production_runtime(
        tmp_path, monkeypatch, db_name="membership_consolidated.db",
    )
    transfer, artifacts = await _two_artifact_transfer(engine, repository, provider)
    healthy, contributor = artifacts["a.bin"], artifacts["b.bin"]
    canonical_target, _ = await _two_artifact_transfer(
        engine, repository, provider, names=("c.bin", "d.bin"),
    )
    canonical_artifact = (await repository.artifacts(canonical_target.id))[0]

    async with database.get_db() as db:
        await db.execute("UPDATE download_files SET mirror_state='standby' WHERE id=?", (contributor.id,))
        await db.execute(
            """INSERT INTO artifact_consolidations(
                contributing_artifact_id,source_transfer_id,source_request_id,canonical_artifact_id)
                VALUES(?,?,?,?)""",
            (contributor.id, transfer.id, contributor.request_id, canonical_artifact.id),
        )
        await db.commit()
    await _simulate_requires_attention(repository, contributor.id)

    presentation = await repository.presentation(transfer.id)
    assert presentation["presentation_status"] != "requires_attention"
    assert presentation["attention_required"] is False


@pytest.mark.asyncio
async def test_completed_transfer_with_historical_row_still_presents_completed(tmp_path, monkeypatch):
    """One canonical actionable child completes; a blocked historical
    sibling still carries stale exhausted-recovery state. The transfer must
    present completed, undisturbed by the historical row (Section 7)."""
    repository, registry, (provider,), executor, engine, now_box = await build_production_runtime(
        tmp_path, monkeypatch, db_name="membership_completed_plus_historical.db",
    )
    transfer, artifacts = await _two_artifact_transfer(engine, repository, provider)
    completing, historical = artifacts["a.bin"], artifacts["b.bin"]

    await repository.select_artifact(transfer.id, historical.id, False)
    await _simulate_requires_attention(repository, historical.id)

    await engine.reconcile_executions()
    active = next(item for item in await repository.artifacts(transfer.id) if item.id == completing.id)
    assert active.execution is not None
    executor.finish(active.execution)
    await engine.reconcile_executions()

    presentation = await repository.presentation(transfer.id)
    assert presentation["status"] == "completed", (
        "the sole canonical actionable child completing must complete the "
        "transfer regardless of an exhausted historical sibling"
    )
    assert presentation["presentation_status"] == "completed"


@pytest.mark.asyncio
async def test_details_can_show_historical_child_without_operational_vote(tmp_path, monkeypatch):
    repository, registry, (provider,), executor, engine, now_box = await build_production_runtime(
        tmp_path, monkeypatch, db_name="membership_details.db",
    )
    transfer, artifacts = await _two_artifact_transfer(engine, repository, provider)
    healthy, historical = artifacts["a.bin"], artifacts["b.bin"]
    await repository.select_artifact(transfer.id, historical.id, False)
    await _simulate_requires_attention(repository, historical.id)

    details = await repository.presentation(transfer.id, details=True)
    files_by_id = {item["id"]: item for item in details["files"]}
    # Still visible for provenance...
    assert historical.id in files_by_id
    assert files_by_id[historical.id]["is_canonical"] is False
    assert files_by_id[healthy.id]["is_canonical"] is True
    # ...but does not influence the transfer-level aggregate.
    assert details["presentation_status"] != "requires_attention"


# ---------------------------------------------------------------------------
# Section 8 -- Presentation Truth and Queued Autonomous Work
# ---------------------------------------------------------------------------

def test_autonomous_presentation_is_a_semantic_predicate_not_a_hand_maintained_list():
    """is_autonomous_presentation must classify a status it has never seen
    enumerated anywhere as autonomous BY DEFAULT (an exclusion rule over the
    small closed operator-gated/terminal family), proving it is not a
    hand-maintained inclusion list (DP 1.0.12 recovery leveling, Section 8)."""
    hypothetical_future_status = "waiting_for_a_reason_nobody_has_invented_yet"
    assert is_autonomous_presentation(
        {"presentation_status": hypothetical_future_status, "attention_required": False}
    )
    for status in ("queued", "downloading", "recovering", "waiting_for_retry",
                   "waiting_for_provider", "waiting_for_storage", "waiting_for_executor",
                   "waiting_for_slot", "pending", "processing", "ready", "verifying",
                   "unresolved", "unknown"):
        assert is_autonomous_presentation({"presentation_status": status, "attention_required": False}), status


def test_operator_gated_and_terminal_states_are_not_autonomous():
    assert not is_autonomous_presentation({"presentation_status": "requires_attention", "attention_required": True})
    assert not is_autonomous_presentation({"presentation_status": "input_required", "attention_required": False})
    assert not is_autonomous_presentation({"presentation_status": "paused", "attention_required": False})
    assert not is_autonomous_presentation({"presentation_status": "failed", "attention_required": False})
    for status in ("completed", "cancelled", "deleted", "consolidated"):
        assert not is_autonomous_presentation({"presentation_status": status, "attention_required": False}), status


@pytest.mark.asyncio
async def test_queued_child_suppresses_sibling_requires_attention_aggregate(tmp_path, monkeypatch):
    repository, registry, (provider,), executor, engine, now_box = await build_production_runtime(
        tmp_path, monkeypatch, db_name="autonomy_queued.db",
    )
    transfer, artifacts = await _two_artifact_transfer(engine, repository, provider)
    queued, attention = artifacts["a.bin"], artifacts["b.bin"]
    await _simulate_requires_attention(repository, attention.id)

    presentation = await repository.presentation(transfer.id)
    assert presentation["presentation_status"] != "requires_attention", (
        "a canonical queued sibling capable of autonomous progress must not "
        "lose the aggregate vote to an exhausted sibling's requires_attention"
    )
    assert presentation["attention_required"] is False


@pytest.mark.asyncio
async def test_downloading_sibling_suppresses_requires_attention_aggregate(tmp_path, monkeypatch):
    repository, registry, (provider,), executor, engine, now_box = await build_production_runtime(
        tmp_path, monkeypatch, db_name="autonomy_downloading.db", max_active_executions=1,
    )
    transfer, artifacts = await _two_artifact_transfer(engine, repository, provider)
    await engine.reconcile_executions()
    refreshed = await repository.artifacts(transfer.id)
    downloading = next(item for item in refreshed if item.execution is not None)
    attention = next(item for item in refreshed if item.execution is None)
    await _simulate_requires_attention(repository, attention.id)

    presentation = await repository.presentation(transfer.id)
    assert presentation["presentation_status"] == "downloading"
    assert presentation["attention_required"] is False


@pytest.mark.asyncio
async def test_waiting_for_retry_sibling_suppresses_requires_attention_aggregate(tmp_path, monkeypatch):
    repository, registry, (provider,), executor, engine, now_box = await build_production_runtime(
        tmp_path, monkeypatch, db_name="autonomy_waiting_for_retry.db",
    )
    transfer, artifacts = await _two_artifact_transfer(engine, repository, provider)
    backing_off, attention = artifacts["a.bin"], artifacts["b.bin"]
    assert await repository.transition_recovery(
        backing_off.id, "recovery_wait", retry_at=now_box[0] + 30,
        quiescence_reason="retry_backoff", wake_condition=f"retry_at:{now_box[0] + 30}",
    )
    await _simulate_requires_attention(repository, attention.id)

    presentation = await repository.presentation(transfer.id)
    assert presentation["presentation_status"] == "waiting_for_retry"
    assert presentation["attention_required"] is False


@pytest.mark.asyncio
async def test_all_exhausted_children_require_attention(tmp_path, monkeypatch):
    repository, registry, (provider,), executor, engine, now_box = await build_production_runtime(
        tmp_path, monkeypatch, db_name="autonomy_all_exhausted.db",
    )
    transfer, artifacts = await _two_artifact_transfer(engine, repository, provider)
    for artifact in artifacts.values():
        await _simulate_requires_attention(repository, artifact.id)

    presentation = await repository.presentation(transfer.id)
    assert presentation["presentation_status"] == "requires_attention", (
        "once every canonical child is genuinely exhausted, the aggregate "
        "must surface requires_attention"
    )


# ---------------------------------------------------------------------------
# Section 9 -- Distinguish Queued From Capacity Waiting
#
# waiting_for_slot is never reconstructed by presentation from mirrored
# preconditions. It is asserted only by transfers.convergence_engine
# .TransferEngine.capacity_only_blocked_ids -- the REAL _dispatch()'s own
# positive record of which artifact ids it most recently reached the
# capacity admission gate for (having already passed target validation,
# candidate expiry, existing-payload, executor.prepare() with no
# InputRequirement, and storage/pause admission via actual code execution)
# and was rejected there. Every test below drives the REAL engine through
# reconcile_executions() and reads that one set; none of them re-derive or
# mirror any of the earlier gates.
# ---------------------------------------------------------------------------

class _InputRequiringExecutor(MemoryExecutor):
    """A higher-priority same-scheme executor whose prepare() always demands
    input, used to prove the real dispatch path routes into the
    input-required branch and returns before ever reaching the capacity
    gate -- registered alongside (not instead of) the harness's default
    MemoryExecutor and picked first by IntegrationRegistry.eligible_executors'
    own priority ordering."""
    descriptor = IntegrationDescriptor(
        "memory-copy-input-required", "Memory copy (input required)",
        frozenset({Capability.PAUSE, Capability.RESUME, Capability.RECONCILE}),
        schemes=frozenset({"memory"}), priority=10,
    )

    def prepare(self, request):
        return InputRequirement(
            InputReason.AUTH_REQUIRED,
            (InputMethodDescriptor(InputMethod.USERNAME_PASSWORD, (
                InputFieldDescriptor(InputField.USERNAME, True),
                InputFieldDescriptor(InputField.PASSWORD, True),
            )),),
        )

    def prepare_with_input(self, request, submitted):
        return super().prepare(request)


async def _saturate_the_only_slot(engine, repository, provider):
    """Submit and really dispatch one unrelated artifact so it owns the
    single execution slot -- the shared precondition for every
    capacity-versus-something-else regression below."""
    saturating_transfer = await engine.submit((TransferRequest(
        "parcel", "saturator", name="saturator.bin", preferred_provider=provider.descriptor.id,
    ),), deduplicate=False)
    await engine.resolve_pending()
    await engine.reconcile_executions()
    saturating_artifact = (await repository.artifacts(saturating_transfer.id))[0]
    assert saturating_artifact.execution is not None, "the saturating artifact should own the only slot"


@pytest.mark.asyncio
async def test_capacity_wait_is_not_operator_attention(tmp_path, monkeypatch):
    repository, registry, (provider,), executor, engine, now_box = await build_production_runtime(
        tmp_path, monkeypatch, db_name="capacity_wait.db",
        max_active_executions=1,
    )
    # One artifact claims the single execution slot; the other is plain
    # queued behind it with nothing else blocking it -- the deterministic
    # capacity scenario Section 13 also specifies (one artifact downloading
    # and owning a slot, the other queued). Dispatch order between two
    # simultaneously-materialized artifacts is not itself under test here, so
    # this identifies "running" vs "waiting" from the actual outcome rather
    # than assuming which of the two wins the single slot.
    transfer, artifacts = await _two_artifact_transfer(engine, repository, provider)
    await engine.reconcile_executions()
    refreshed = await repository.artifacts(transfer.id)
    running = [item for item in refreshed if item.execution is not None]
    waiting = [item for item in refreshed if item.execution is None]
    assert len(running) == 1, "exactly one artifact should have claimed the only slot"
    assert len(waiting) == 1, "the other artifact must remain unadmitted (capacity saturated)"
    waiting = waiting[0]

    # The label must originate from the real admission path's own record,
    # not a presentation-side reconstruction.
    assert waiting.id in engine.capacity_only_blocked_ids()

    presentation = await repository.presentation(
        transfer.id, details=True, capacity_only_blocked_ids=engine.capacity_only_blocked_ids(),
    )
    files_by_id = {item["id"]: item for item in presentation["files"]}
    waiting_presentation = files_by_id[waiting.id]["presentation_status"]
    assert waiting_presentation == "waiting_for_slot", (
        f"expected the capacity-blocked sibling to present waiting_for_slot, got {waiting_presentation!r}"
    )
    assert waiting_presentation != "requires_attention"
    # Capacity waiting must count as autonomous: the aggregate must not
    # present requires_attention merely because one sibling is capacity-bound.
    assert presentation["presentation_status"] != "requires_attention"


@pytest.mark.asyncio
async def test_capacity_wait_never_claimed_without_live_admission_facts(tmp_path, monkeypatch):
    """A caller that does not supply capacity_only_blocked_ids (the
    conservative default every non-upgraded ``presentation()`` caller gets)
    must never see waiting_for_slot -- it must fall back to plain queued,
    exactly prior behavior, rather than fabricate an assessment it cannot
    actually support."""
    repository, registry, (provider,), executor, engine, now_box = await build_production_runtime(
        tmp_path, monkeypatch, db_name="capacity_wait_no_facts.db",
        max_active_executions=1,
    )
    transfer, artifacts = await _two_artifact_transfer(engine, repository, provider)
    await engine.reconcile_executions()
    refreshed = await repository.artifacts(transfer.id)
    waiting = next(item for item in refreshed if item.execution is None)
    assert waiting.id in engine.capacity_only_blocked_ids()

    presentation = await repository.presentation(transfer.id, details=True)
    files_by_id = {item["id"]: item for item in presentation["files"]}
    assert files_by_id[waiting.id]["presentation_status"] == "queued"


@pytest.mark.asyncio
async def test_capacity_wait_excludes_executor_input_requirement(tmp_path, monkeypatch):
    """queued + capacity full + executor.prepare() requires input => NOT
    waiting_for_slot. The real dispatch path routes into the input-required
    branch and returns before ever reaching the capacity gate."""
    repository, registry, (provider,), executor, engine, now_box = await build_production_runtime(
        tmp_path, monkeypatch, db_name="capacity_wait_input_requirement.db",
        max_active_executions=1,
    )
    await _saturate_the_only_slot(engine, repository, provider)
    registry.register_executor(_InputRequiringExecutor(repository.authorize_execution))

    transfer = await engine.submit((TransferRequest(
        "parcel", "needs-input", name="needs_input.bin",
    ),), deduplicate=False)
    await engine.resolve_pending()
    await engine.reconcile_executions()

    artifact = (await repository.artifacts(transfer.id))[0]
    assert artifact.execution is None
    assert artifact.id not in engine.capacity_only_blocked_ids(), (
        "an artifact whose dispatch attempt hit an executor input requirement "
        "must not be recorded as capacity-only-blocked"
    )
    presentation = await repository.presentation(
        transfer.id, details=True, capacity_only_blocked_ids=engine.capacity_only_blocked_ids(),
    )
    status = presentation["files"][0]["presentation_status"]
    assert status != "waiting_for_slot", f"expected NOT waiting_for_slot, got {status!r}"
    assert status == "input_required"


@pytest.mark.asyncio
async def test_capacity_wait_excludes_current_input_challenge(tmp_path, monkeypatch):
    """queued + capacity full + a current input challenge => NOT
    waiting_for_slot. _process_executions() is invoked with
    dispatch_allowed=False for the whole transfer whenever a challenge is
    current, so _dispatch() (and the capacity gate) is never reached this
    cycle at all -- not merely re-checked and found irrelevant."""
    repository, registry, (provider,), executor, engine, now_box = await build_production_runtime(
        tmp_path, monkeypatch, db_name="capacity_wait_current_challenge.db",
        max_active_executions=1,
    )
    await _saturate_the_only_slot(engine, repository, provider)

    transfer = await engine.submit((TransferRequest(
        "parcel", "challenged", name="challenged.bin",
    ),), deduplicate=False)
    await engine.resolve_pending()
    artifact = (await repository.artifacts(transfer.id))[0]
    requirement = InputRequirement(
        InputReason.AUTH_REQUIRED,
        (InputMethodDescriptor(InputMethod.USERNAME_PASSWORD, (
            InputFieldDescriptor(InputField.USERNAME, True),
            InputFieldDescriptor(InputField.PASSWORD, True),
        )),),
    )
    await engine.challenges.wait_executor(artifact, "memory-copy", "op-1", requirement)

    await engine.reconcile_executions()
    assert artifact.id not in engine.capacity_only_blocked_ids(), (
        "an artifact behind a current input challenge must never be "
        "dispatch-attempted, so it can never be recorded as capacity-only-blocked"
    )
    presentation = await repository.presentation(
        transfer.id, details=True, capacity_only_blocked_ids=engine.capacity_only_blocked_ids(),
    )
    status = presentation["files"][0]["presentation_status"]
    assert status != "waiting_for_slot", f"expected NOT waiting_for_slot, got {status!r}"
    assert status == "input_required"


@pytest.mark.asyncio
async def test_capacity_wait_excludes_expired_candidate(tmp_path, monkeypatch):
    """queued + capacity full + candidate expired => NOT waiting_for_slot.
    The production Phase-3 dispatch layer
    (transfers._convergence_phase3_base.TransferEngine._dispatch) routes an
    expired candidate through recovery (AUTO_RETRY) before ever reaching the
    base engine's capacity gate."""
    repository, registry, (provider,), executor, engine, now_box = await build_production_runtime(
        tmp_path, monkeypatch, db_name="capacity_wait_expired_candidate.db",
        max_active_executions=1,
    )
    await _saturate_the_only_slot(engine, repository, provider)

    transfer = await engine.submit((TransferRequest(
        "parcel", "expiring", name="expiring.bin",
    ),), deduplicate=False)
    await engine.resolve_pending()
    artifact = (await repository.artifacts(transfer.id))[0]

    async with database.get_db() as db:
        row = await db.fetchone("SELECT candidates FROM download_files WHERE id=?", (artifact.id,))
        candidates = json.loads(row["candidates"])
        candidates[0]["expires_at"] = now_box[0] - 1
        await db.execute("UPDATE download_files SET candidates=? WHERE id=?",
                          (json.dumps(candidates), artifact.id))
        await db.commit()

    await engine.reconcile_executions()
    assert artifact.id not in engine.capacity_only_blocked_ids(), (
        "an artifact whose candidate is expired must not be recorded as capacity-only-blocked"
    )
    presentation = await repository.presentation(
        transfer.id, details=True, capacity_only_blocked_ids=engine.capacity_only_blocked_ids(),
    )
    status = presentation["files"][0]["presentation_status"]
    assert status != "waiting_for_slot", f"expected NOT waiting_for_slot, got {status!r}"


@pytest.mark.asyncio
async def test_capacity_wait_excludes_stable_completed_payload(tmp_path, monkeypatch):
    """queued + capacity full + an already-stable, correctly-sized payload on
    disk => NOT waiting_for_slot. _dispatch() marks the artifact completed
    before ever reaching the capacity gate."""
    repository, registry, (provider,), executor, engine, now_box = await build_production_runtime(
        tmp_path, monkeypatch, db_name="capacity_wait_stable_payload.db",
        max_active_executions=1,
    )
    await _saturate_the_only_slot(engine, repository, provider)

    transfer = await engine.submit((TransferRequest(
        "parcel", "already-done", name="already_done.bin",
    ),), deduplicate=False)
    await engine.resolve_pending()
    artifact = (await repository.artifacts(transfer.id))[0]
    Path(artifact.target).parent.mkdir(parents=True, exist_ok=True)
    Path(artifact.target).write_bytes(b"x" * artifact.expected_bytes)

    await engine.reconcile_executions()
    assert artifact.id not in engine.capacity_only_blocked_ids(), (
        "an artifact whose payload is already a stable, correctly-sized "
        "completed file must not be recorded as capacity-only-blocked"
    )
    refreshed = (await repository.artifacts(transfer.id))[0]
    assert refreshed.state == "completed"
    presentation = await repository.presentation(
        transfer.id, capacity_only_blocked_ids=engine.capacity_only_blocked_ids(),
    )
    assert presentation["presentation_status"] == "completed"


@pytest.mark.asyncio
async def test_capacity_wait_excludes_disabled_provider(tmp_path, monkeypatch):
    """queued + capacity full + provider disabled after candidate binding =>
    NOT waiting_for_slot. convergence_engine's own provider-enabled gate
    routes the artifact into recovery before the capacity check."""
    repository, registry, (provider, other_provider), executor, engine, now_box = await build_production_runtime(
        tmp_path, monkeypatch, db_name="capacity_wait_disabled_provider.db",
        max_active_executions=1, provider_ids=("provider-a", "provider-b"),
    )
    await _saturate_the_only_slot(engine, repository, provider)

    # Resolve onto provider-b WHILE IT IS STILL ENABLED (a disabled provider
    # is excluded from applicable candidates at resolution time,
    # transfers.registry.IntegrationRegistry, so this could never durably
    # bind to a provider that was already disabled) -- then disable it
    # afterward, exactly an operator disabling a provider a transfer had
    # already selected.
    blocked_transfer = await engine.submit((TransferRequest(
        "parcel", "blocked", name="blocked.bin", preferred_provider=other_provider.descriptor.id,
    ),), deduplicate=False)
    await engine.resolve_pending()
    blocked_artifact = (await repository.artifacts(blocked_transfer.id))[0]
    assert blocked_artifact.candidates[blocked_artifact.selected].provider_id == other_provider.descriptor.id

    other_provider.descriptor = replace(other_provider.descriptor, enabled=False)
    await engine.reconcile_executions()

    assert blocked_artifact.id not in engine.capacity_only_blocked_ids(), (
        "an artifact whose provider is disabled must not be recorded as "
        "capacity-only-blocked even though global capacity is saturated"
    )
    presentation = await repository.presentation(
        blocked_transfer.id, details=True, capacity_only_blocked_ids=engine.capacity_only_blocked_ids(),
    )
    status = presentation["files"][0]["presentation_status"]
    assert status != "waiting_for_slot", f"expected NOT waiting_for_slot, got {status!r}"


@pytest.mark.asyncio
async def test_capacity_wait_excludes_globally_paused(tmp_path, monkeypatch):
    """A globally-paused system must never present capacity waiting: nothing
    would dispatch even with free capacity, so capacity is not "the" blocker.
    _live(admission=True) gates dispatch before _dispatch() is ever called."""
    repository, registry, (provider,), executor, engine, now_box = await build_production_runtime(
        tmp_path, monkeypatch, db_name="capacity_wait_global_pause.db",
        max_active_executions=1,
    )
    await _saturate_the_only_slot(engine, repository, provider)

    second_transfer = await engine.submit((TransferRequest(
        "parcel", "second", name="second.bin",
    ),), deduplicate=False)
    await engine.resolve_pending()
    await repository.global_pause(True)

    await engine.reconcile_executions()
    second_artifact = (await repository.artifacts(second_transfer.id))[0]
    assert second_artifact.id not in engine.capacity_only_blocked_ids(), (
        "a globally paused system must never record an artifact as capacity-only-blocked"
    )
    presentation = await repository.presentation(
        second_transfer.id, details=True, capacity_only_blocked_ids=engine.capacity_only_blocked_ids(),
    )
    status = presentation["files"][0]["presentation_status"]
    assert status != "waiting_for_slot", f"expected NOT waiting_for_slot, got {status!r}"


@pytest.mark.asyncio
async def test_capacity_only_blocked_self_corrects_when_a_gate_closes_on_the_next_cycle(tmp_path, monkeypatch):
    """A genuinely capacity-only-blocked record must not survive stale once a
    DIFFERENT gate closes on a later cycle: it is reset every
    reconcile_executions() call and only re-populated by that cycle's own
    real dispatch attempts, so an artifact correctly recorded on one cycle is
    correctly ABSENT on the very next cycle once its provider is disabled --
    never optimistically carried forward."""
    repository, registry, (provider, other_provider), executor, engine, now_box = await build_production_runtime(
        tmp_path, monkeypatch, db_name="capacity_wait_self_corrects.db",
        max_active_executions=1, provider_ids=("provider-a", "provider-b"),
    )
    await _saturate_the_only_slot(engine, repository, provider)

    transfer = await engine.submit((TransferRequest(
        "parcel", "later-disabled", name="later_disabled.bin", preferred_provider=other_provider.descriptor.id,
    ),), deduplicate=False)
    await engine.resolve_pending()
    artifact = (await repository.artifacts(transfer.id))[0]

    await engine.reconcile_executions()
    assert artifact.id in engine.capacity_only_blocked_ids(), (
        "with its provider still enabled, this cycle's real dispatch attempt "
        "should reach and fail the capacity gate"
    )

    other_provider.descriptor = replace(other_provider.descriptor, enabled=False)
    await engine.reconcile_executions()
    assert artifact.id not in engine.capacity_only_blocked_ids(), (
        "once the provider is disabled, the NEXT cycle's real dispatch attempt "
        "never reaches the capacity gate, and the stale record must not survive"
    )
