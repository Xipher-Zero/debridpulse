"""Section 13 regression coverage: Execution Admission Continuity Across
Failover (DP 1.0.12 recovery leveling, Phase 2 correction round).

An artifact that already owns one active execution slot must retain that
slot's entitlement across the short writer-retirement -> replacement-dispatch
handoff a candidate activation performs. Unrelated queued work must never
steal it in between, an artifact that was NOT actively occupying a slot must
never gain priority merely from switching, and the reservation must be
bounded/self-healing (never permanently consumed by a crash/restart).

All tests use the real production stack, matching
tests/production_stack_harness.py's rationale (Section 32), and run with
``max_active_executions=1`` for deterministic capacity contention (Section 9's
own low-capacity convention).
"""
from __future__ import annotations

import pytest

from test_candidate_activation_phase2 import activate_with_real_claim, attach_three, build_engine3
from transfers.models import TransferRequest


async def _submit_single(engine, provider, payload="single"):
    transfer = await engine.submit((TransferRequest(
        "parcel", payload, name="solo.bin", preferred_provider=provider.descriptor.id,
    ),), deduplicate=False)
    await engine.resolve_pending()
    return transfer


@pytest.mark.asyncio
async def test_capacity_only_blocked_reflects_a_held_continuation_reservation(tmp_path, monkeypatch):
    """Phase-1 carry-forward: Section 9's ``capacity_only_blocked_ids`` is
    positive evidence from the REAL dispatch path's own capacity gate, which
    now reads transfers.candidate_activation.activate_candidate's Section 13
    reservation, not just live executions. An unrelated artifact genuinely
    blocked only by a held continuation reservation must show up here too --
    proving the gate is the same single reservation-aware occupancy source,
    not a stale pre-reservation branch left in the classifier."""
    engine, repository, providers, _executor, now_box = await build_engine3(
        tmp_path, monkeypatch, max_active_executions=1,
    )
    canonical_a, artifact_a = await attach_three(engine, repository, providers)
    transfer_b = await _submit_single(engine, providers[0], payload="waits-on-reservation")

    await engine.reconcile_executions()
    a_live = (await repository.artifacts(canonical_a.id))[0]
    assert a_live.execution is not None

    result = await activate_with_real_claim(engine, a_live, 1, retry_at=now_box[0])
    assert result.committed
    assert await repository.continuation_reservation(artifact_a.id) is not None

    await engine.reconcile_executions()
    b_artifact = (await repository.artifacts(transfer_b.id))[0]
    assert b_artifact.execution is None
    assert engine.capacity_only_blocked_ids() == {b_artifact.id}


@pytest.mark.asyncio
async def test_active_failover_preserves_execution_admission(tmp_path, monkeypatch):
    engine, repository, providers, _executor, now_box = await build_engine3(
        tmp_path, monkeypatch, max_active_executions=1,
    )
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]
    assert live.execution is not None
    assert await repository.occupied_execution_slots(now_box[0]) == 1

    result = await activate_with_real_claim(engine, live, 1, retry_at=now_box[0])
    assert result.committed
    switched = (await repository.artifacts(canonical.id))[0]
    assert switched.execution is None
    # The old writer is gone but the slot entitlement is durably retained --
    # never zero here, and never more than the configured limit.
    assert await repository.occupied_execution_slots(now_box[0]) == 1
    assert await repository.continuation_reservation(artifact.id) is not None

    await engine.reconcile_executions()
    redispatched = (await repository.artifacts(canonical.id))[0]
    assert redispatched.execution is not None
    assert redispatched.selected == 1
    # Consumed by the real dispatch -- no longer a bare reservation, and
    # still never exceeding the configured limit.
    assert await repository.occupied_execution_slots(now_box[0]) == 1
    assert await repository.continuation_reservation(artifact.id) is None


@pytest.mark.asyncio
async def test_unrelated_queue_cannot_steal_failover_continuation_slot(tmp_path, monkeypatch):
    """The required deterministic Section 13 scenario: A is downloading and
    owns the only slot, B is queued, A activates another candidate and its
    old writer is retired -- B must not steal A's continuation reservation,
    and A's replacement must dispatch using the same entitlement."""
    engine, repository, providers, _executor, now_box = await build_engine3(
        tmp_path, monkeypatch, max_active_executions=1,
    )
    canonical_a, artifact_a = await attach_three(engine, repository, providers)
    transfer_b = await _submit_single(engine, providers[0], payload="unrelated")

    await engine.reconcile_executions()
    a_live = (await repository.artifacts(canonical_a.id))[0]
    b_queued = (await repository.artifacts(transfer_b.id))[0]
    assert a_live.execution is not None
    assert b_queued.execution is None and b_queued.state == "queued"

    result = await activate_with_real_claim(engine, a_live, 1, retry_at=now_box[0])
    assert result.committed

    # A's old writer is retired and B is still queued/capacity-blocked right
    # now -- this single reconcile pass must not let B grab the freed-looking
    # slot before A's replacement gets it.
    await engine.reconcile_executions()
    a_after = (await repository.artifacts(canonical_a.id))[0]
    b_after = (await repository.artifacts(transfer_b.id))[0]
    assert b_after.execution is None, "unrelated queued work must not steal A's continuation slot"
    assert a_after.execution is not None and a_after.selected == 1
    assert await repository.occupied_execution_slots(now_box[0]) == 1

    # B remains correctly capacity-blocked afterward too.
    await engine.reconcile_executions()
    assert (await repository.artifacts(transfer_b.id))[0].execution is None
    assert await repository.occupied_execution_slots(now_box[0]) == 1


@pytest.mark.asyncio
async def test_nonactive_manual_switch_does_not_gain_capacity_priority(tmp_path, monkeypatch):
    """An artifact that was NOT actively occupying a slot (still queued,
    never dispatched) must gain no reservation/priority merely because the
    operator changed its candidate."""
    engine, repository, providers, _executor, now_box = await build_engine3(
        tmp_path, monkeypatch, max_active_executions=1,
    )

    # B occupies the only slot first.
    canonical_b_transfer = await _submit_single(engine, providers[0], payload="holds-slot")
    await engine.reconcile_executions()
    holder = (await repository.artifacts(canonical_b_transfer.id))[0]
    assert holder.execution is not None

    # A never gets to dispatch (capacity already taken) -- still queued.
    canonical_a, artifact_a = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    a_queued = (await repository.artifacts(canonical_a.id))[0]
    assert a_queued.execution is None and a_queued.state == "queued"

    result = await activate_with_real_claim(engine, a_queued, 1, retry_at=now_box[0])
    assert result.committed
    assert await repository.continuation_reservation(a_queued.id) is None
    # Occupancy is unchanged -- B's real slot only -- A gained nothing.
    assert await repository.occupied_execution_slots(now_box[0]) == 1

    await engine.reconcile_executions()
    still_queued = (await repository.artifacts(canonical_a.id))[0]
    assert still_queued.execution is None, "A must not have jumped ahead of B for the single slot"


@pytest.mark.asyncio
async def test_failed_handoff_releases_capacity_reservation(tmp_path, monkeypatch):
    engine, repository, providers, _executor, now_box = await build_engine3(
        tmp_path, monkeypatch, max_active_executions=1,
    )
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]

    result = await activate_with_real_claim(engine, live, 1, retry_at=now_box[0])
    assert result.committed
    assert await repository.continuation_reservation(artifact.id) is not None
    assert await repository.occupied_execution_slots(now_box[0]) == 1

    # Simulate a handoff failure before the replacement ever dispatches
    # (e.g. the new candidate's own dispatch attempt fails permanently).
    from transfers.errors import Category, Domain, NormalizedError, Stage
    failure = NormalizedError(Domain.INTERNAL, Category.UNMAPPED_EXECUTOR_ERROR, Stage.QUEUE)
    await repository.artifact_state(artifact.id, "error", error=failure)

    assert await repository.continuation_reservation(artifact.id) is None
    assert await repository.occupied_execution_slots(now_box[0]) == 0


@pytest.mark.asyncio
async def test_restart_reconciles_stale_handoff_reservation(tmp_path, monkeypatch):
    """A reservation is an absolute expiry timestamp, not a duration -- a
    restarted process (simulated here by simply letting time pass and
    re-querying) self-heals with no dedicated reconciliation step: the
    comparison against current time is the reconciliation."""
    engine, repository, providers, _executor, now_box = await build_engine3(
        tmp_path, monkeypatch, max_active_executions=1,
    )
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]

    result = await activate_with_real_claim(engine, live, 1, retry_at=now_box[0])
    assert result.committed
    reserved_until = await repository.continuation_reservation(artifact.id)
    assert reserved_until is not None and reserved_until > now_box[0]
    assert await repository.occupied_execution_slots(now_box[0]) == 1

    # Time passes well beyond the reservation's bound -- as if the process
    # that would have consumed it crashed and only restarted much later.
    now_box[0] = reserved_until + 1.0
    assert await repository.occupied_execution_slots(now_box[0]) == 0, (
        "a stale reservation must not permanently consume capacity"
    )

    # Capacity is genuinely free again: some queued work -- the original
    # artifact reclaiming its own still-queued selection, or newly submitted
    # unrelated work -- can now dispatch and fully use the freed slot. Which
    # one wins the single slot is an ordinary capacity race, not the
    # invariant under test; what matters is that the stale reservation no
    # longer holds capacity hostage.
    await _submit_single(engine, providers[0], payload="after-restart")
    await engine.reconcile_executions()
    assert await repository.occupied_execution_slots(now_box[0]) == 1


# ---------------------------------------------------------------------------
# Reservation release/freeze semantics (Section 13 correction round)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_releases_or_freezes_continuation_reservation_by_documented_rule(tmp_path, monkeypatch):
    """Documented rule: pausing RELEASES any continuation reservation. Replacement
    dispatch is not legally permitted while paused, so holding the reservation
    across a pause would consume real capacity for up to its full bound for no
    reachable purpose; a later resume re-earns admission the ordinary way."""
    engine, repository, providers, _executor, now_box = await build_engine3(
        tmp_path, monkeypatch, max_active_executions=1,
    )
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]

    result = await activate_with_real_claim(engine, live, 1, retry_at=now_box[0])
    assert result.committed
    assert await repository.continuation_reservation(artifact.id) is not None

    await engine.pause(canonical.id)
    assert await repository.continuation_reservation(artifact.id) is None
    assert await repository.occupied_execution_slots(now_box[0]) == 0

    await engine.resume(canonical.id)
    await engine.reconcile_executions()
    resumed = (await repository.artifacts(canonical.id))[0]
    assert resumed.execution is not None and resumed.selected == 1


@pytest.mark.asyncio
async def test_global_pause_does_not_leave_capacity_consumed(tmp_path, monkeypatch):
    engine, repository, providers, _executor, now_box = await build_engine3(
        tmp_path, monkeypatch, max_active_executions=1,
    )
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]

    result = await activate_with_real_claim(engine, live, 1, retry_at=now_box[0])
    assert result.committed
    assert await repository.continuation_reservation(artifact.id) is not None

    await engine.pause_all()
    assert await repository.continuation_reservation(artifact.id) is None
    assert await repository.occupied_execution_slots(now_box[0]) == 0

    await engine.resume_all()
    await engine.reconcile_executions()
    resumed = (await repository.artifacts(canonical.id))[0]
    assert resumed.execution is not None and resumed.selected == 1


@pytest.mark.asyncio
async def test_storage_gate_does_not_leave_unusable_continuation_capacity_reserved(tmp_path, monkeypatch):
    """The storage/dispatch gate stays authoritative -- it is never bypassed
    to force a dispatch -- while the SAME bounded TTL that already protects
    against a crash/restart (test_restart_reconciles_stale_handoff_reservation)
    is the explicit, documented release mechanism here too: a reservation
    never outlives its bound regardless of how long storage stays down."""
    engine, repository, providers, _executor, now_box = await build_engine3(
        tmp_path, monkeypatch, max_active_executions=1,
    )
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]

    result = await activate_with_real_claim(engine, live, 1, retry_at=now_box[0])
    assert result.committed
    reserved_until = await repository.continuation_reservation(artifact.id)
    assert reserved_until is not None

    engine.dispatch_permitted = False
    await engine.reconcile_executions()
    still_queued = (await repository.artifacts(canonical.id))[0]
    assert still_queued.execution is None, "the storage gate must remain authoritative -- no dispatch while unhealthy"
    assert await repository.continuation_reservation(artifact.id) is not None

    # Time passes beyond the bound while storage is STILL down.
    now_box[0] = reserved_until + 1.0
    assert await repository.occupied_execution_slots(now_box[0]) == 0, (
        "the reservation must not outlive its bound merely because storage stayed down"
    )

    engine.dispatch_permitted = True
    await engine.reconcile_executions()
    recovered = (await repository.artifacts(canonical.id))[0]
    assert recovered.execution is not None


@pytest.mark.asyncio
async def test_permanent_quiescence_releases_continuation_reservation(tmp_path, monkeypatch):
    """Every transition_recovery() call implicitly releases a reservation
    unless it explicitly renews one (repository.py's own default), so parking
    an artifact into permanent/quiescent wait after a reservation was granted
    -- but before the replacement ever dispatched -- already releases it."""
    engine, repository, providers, _executor, now_box = await build_engine3(
        tmp_path, monkeypatch, max_active_executions=1,
    )
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]

    result = await activate_with_real_claim(engine, live, 1, retry_at=now_box[0])
    assert result.committed
    assert await repository.continuation_reservation(artifact.id) is not None

    quiesced = await repository.transition_recovery(
        artifact.id, "error", retry_at=0,
        quiescence_reason="recovery_exhausted", wake_condition="operator_retry",
    )
    assert quiesced
    assert await repository.continuation_reservation(artifact.id) is None
    assert await repository.occupied_execution_slots(now_box[0]) == 0


@pytest.mark.asyncio
async def test_cancel_delete_release_continuation_reservation(tmp_path, monkeypatch):
    engine, repository, providers, _executor, now_box = await build_engine3(
        tmp_path, monkeypatch, max_active_executions=1,
    )
    canonical_cancel, artifact_cancel = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live_cancel = (await repository.artifacts(canonical_cancel.id))[0]
    result = await activate_with_real_claim(engine, live_cancel, 1, retry_at=now_box[0])
    assert result.committed
    assert await repository.continuation_reservation(artifact_cancel.id) is not None

    await engine.cancel(canonical_cancel.id)
    assert await repository.continuation_reservation(artifact_cancel.id) is None
    assert await repository.occupied_execution_slots(now_box[0]) == 0

    canonical_delete, artifact_delete = await attach_three(
        engine, repository, providers, name="other.bin", payloads=("del-0", "del-1", "del-2"),
    )
    await engine.reconcile_executions()
    live_delete = (await repository.artifacts(canonical_delete.id))[0]
    result = await activate_with_real_claim(engine, live_delete, 1, retry_at=now_box[0])
    assert result.committed
    assert await repository.continuation_reservation(artifact_delete.id) is not None

    await engine.delete(canonical_delete.id, remote=False)
    # delete() durably clears the column itself -- not just excludes it via
    # occupied_execution_slots()'s torrents.status join filter -- because
    # DELETED is not a true dead end (see the reacquire-before-TTL test
    # below): relying on the join filter alone would let the reservation
    # silently reappear if the transfer is ever resurrected.
    assert await repository.continuation_reservation(artifact_delete.id) is None
    assert await repository.occupied_execution_slots(now_box[0]) == 0


@pytest.mark.asyncio
async def test_deleted_transfer_reservation_cannot_reacquire_before_ttl_on_resurrection(tmp_path, monkeypatch):
    """DELETED is operator-reversible (transfers.policy.transition_allowed
    permits DELETED -> ACCEPTED for an operator). A reservation that predates
    the delete must not silently reappear and consume capacity the moment a
    resurrected transfer becomes active again, well before its own TTL would
    ever have expired on its own."""
    from transfers.models import TransferState

    engine, repository, providers, _executor, now_box = await build_engine3(
        tmp_path, monkeypatch, max_active_executions=1,
    )
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]

    result = await activate_with_real_claim(engine, live, 1, retry_at=now_box[0])
    assert result.committed
    reserved_until = await repository.continuation_reservation(artifact.id)
    assert reserved_until is not None and reserved_until > now_box[0]

    transfer = await repository.get(canonical.id)
    await engine.delete(canonical.id, remote=False)
    assert await repository.continuation_reservation(artifact.id) is None

    # Resurrect the transfer well BEFORE the original reservation's TTL would
    # have expired on its own.
    assert now_box[0] < reserved_until
    resurrected = await repository.state(
        canonical.id, TransferState.ACCEPTED, operator=True, expected_epoch=transfer.epoch + 1,
    )
    assert resurrected

    assert await repository.continuation_reservation(artifact.id) is None
    assert await repository.occupied_execution_slots(now_box[0]) == 0, (
        "a pre-delete reservation must not reacquire capacity merely because "
        "the transfer was resurrected before its original TTL"
    )
