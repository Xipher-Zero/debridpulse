"""Focused qualification for operator-requested canonical candidate switching."""
from __future__ import annotations

from dataclasses import replace

import pytest

import db.database as database
from fake_integrations import MemoryExecutor
from test_ws2p1_failover_progress import EquivalentParcelProvider
from transfers.engine import TransferEngine
from transfers.errors import Category, TransferError
from transfers.manual_failover import manual_candidate_failover
from transfers.manual_repository import TransferRepository
from transfers.models import (
    ExecutionObservation,
    ExecutionState,
    SourceIdentity,
    TransferProgress,
    TransferRequest,
    TransferState,
)
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry


class HostParcelProvider(EquivalentParcelProvider):
    def candidate(self, name="same.bin", *, payload="parcel"):
        return replace(
            super().candidate(name, payload=payload),
            source_identity=SourceIdentity("host", f"{self.descriptor.id}.example"),
        )


async def build_engine(tmp_path, monkeypatch, *, provider_ids=("provider-a", "provider-b")):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    first = HostParcelProvider(provider_ids[0])
    second = HostParcelProvider(provider_ids[1])
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(first)
    registry.register_provider(second)
    registry.register_executor(executor)
    engine = TransferEngine(
        repository,
        registry,
        download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(
            retry_delay=0,
            adoption_stability_seconds=0,
            max_active_executions=8,
            resolution_concurrency=8,
        ),
        clock=lambda: 1000.0,
    )
    await engine.initialize()
    return engine, repository, first, second, executor


async def attach_two(engine, repository, first, second):
    canonical = await engine.submit((TransferRequest(
        "parcel", "original-a", name="same.bin", preferred_provider=first.descriptor.id,
    ),), deduplicate=False)
    await engine.resolve_pending()
    source = await engine.submit((TransferRequest(
        "parcel", "original-b", name="same.bin", preferred_provider=second.descriptor.id,
    ),), deduplicate=False)
    await engine.resolve_pending()
    artifact = (await repository.artifacts(canonical.id))[0]
    assert [item.provider_id for item in artifact.candidates] == [first.descriptor.id, second.descriptor.id]
    return canonical, source, artifact


async def _simulate_stale_operator_attention(repository, artifact_id, *, reason="recovery_budget_exhausted"):
    """Durably persist the exact combination
    transfers._engine_recovery.py's ``_apply_recovery_decision`` WAIT_FOR_OPERATOR
    branch produces: ``record_recovery_decision`` followed by ``_quiesce``'s
    transition (raw state ``"error"`` -- ``_quiesce`` uses
    ``"error" if reason == "recovery_exhausted" else "recovery_wait"``, and the
    WAIT_FOR_OPERATOR branch defaults its own ``reason`` to
    ``"recovery_exhausted"`` -- with ``quiescence_reason``/``wake_condition``
    set). The artifact's raw status therefore lands on the same real
    switchable state (``"error"``, a member of manual_failover.py's
    ``_OPERATIONAL_STATES``) a genuinely exhausted recovery leaves it in, so
    the fixture matches real production persistence rather than an invented
    shape. Field combination mirrors
    test_transfer_recovery_phase4.py::test_requires_attention_needs_persisted_operator_decision_reason_and_wake.
    """
    await repository.record_recovery_decision(artifact_id, "wait_for_operator", reason)
    assert await repository.transition_recovery(
        artifact_id, "error", retry_at=0,
        quiescence_reason="recovery_exhausted", wake_condition="operator_retry",
    )


@pytest.mark.asyncio
async def test_switch_retires_old_writer_and_redispatches_exact_candidate(tmp_path, monkeypatch):
    engine, repository, first, second, executor = await build_engine(tmp_path, monkeypatch)
    canonical, _source, artifact = await attach_two(engine, repository, first, second)
    await engine.reconcile_executions()
    artifact = (await repository.artifacts(canonical.id))[0]
    old = artifact.execution
    wanted = artifact.candidates[1]

    result = await manual_candidate_failover(engine, canonical.id, artifact.id, str(wanted.id))
    switched = (await repository.artifacts(canonical.id))[0]
    assert result["candidate_id"] == str(wanted.id)
    assert result["source_host"] == "provider-b.example"
    assert switched.selected == 1 and switched.execution is None and switched.state == "queued"
    attempts = {item.handle.attempt_id: item for item in await repository.executions(canonical.id)}
    assert attempts[old.attempt_id].state == ExecutionState.CANCELLED
    assert not await repository.authorize_execution(old, "start")

    await engine.reconcile_executions()
    active = (await repository.artifacts(canonical.id))[0]
    assert active.execution is not None and active.execution != old
    live = [item for item in await repository.executions(canonical.id)
            if item.state in {"prepared", "queued", "transferring", "paused", "unknown"}]
    assert len(live) == 1 and live[0].candidate.provider_id == "provider-b"
    assert len([call for call in executor.calls if call[0] == "start"]) == 2

    view = await repository.presentation(canonical.id, details=True)
    assert view["current_provider_id"] == "provider-b"
    event = view["manual_candidate_failovers"][-1]
    assert event["reason"] == "USER_REQUESTED" and event["outcome"] == "success"
    assert event["selected_candidate_id"] == str(wanted.id)
    assert "original-a" not in str(event) and "original-b" not in str(event)


@pytest.mark.asyncio
async def test_switch_while_manually_paused_stays_paused_until_resume(tmp_path, monkeypatch):
    engine, repository, first, second, _executor = await build_engine(tmp_path, monkeypatch)
    canonical, _source, artifact = await attach_two(engine, repository, first, second)
    await engine.reconcile_executions()
    await engine.pause(canonical.id)
    artifact = (await repository.artifacts(canonical.id))[0]
    await manual_candidate_failover(engine, canonical.id, artifact.id, str(artifact.candidates[1].id))
    switched = (await repository.artifacts(canonical.id))[0]
    assert switched.selected == 1 and switched.execution is None
    assert (await repository.get(canonical.id)).paused is True
    await engine.reconcile_executions()
    assert (await repository.artifacts(canonical.id))[0].execution is None
    await engine.resume(canonical.id)
    await engine.reconcile_executions()
    resumed = (await repository.artifacts(canonical.id))[0]
    assert resumed.selected == 1 and resumed.execution is not None


@pytest.mark.asyncio
async def test_switch_respects_global_pause(tmp_path, monkeypatch):
    engine, repository, first, second, _executor = await build_engine(tmp_path, monkeypatch)
    canonical, _source, artifact = await attach_two(engine, repository, first, second)
    await engine.reconcile_executions()
    await engine.pause_all()
    artifact = (await repository.artifacts(canonical.id))[0]
    await manual_candidate_failover(engine, canonical.id, artifact.id, str(artifact.candidates[1].id))
    switched = (await repository.artifacts(canonical.id))[0]
    assert switched.selected == 1 and switched.execution is None
    await engine.reconcile_executions()
    assert (await repository.artifacts(canonical.id))[0].execution is None
    await engine.resume_all()
    await engine.reconcile_executions()
    assert (await repository.artifacts(canonical.id))[0].execution is not None


@pytest.mark.asyncio
async def test_unknown_wrong_and_already_active_candidates_fail_truthfully(tmp_path, monkeypatch):
    engine, repository, first, second, _executor = await build_engine(tmp_path, monkeypatch)
    canonical, _source, artifact = await attach_two(engine, repository, first, second)
    with pytest.raises(TransferError) as missing:
        await manual_candidate_failover(engine, canonical.id, artifact.id, "not-a-candidate")
    assert missing.value.error.category == Category.SOURCE_NOT_FOUND
    with pytest.raises(TransferError) as active:
        await manual_candidate_failover(engine, canonical.id, artifact.id, str(artifact.candidates[0].id))
    assert active.value.error.category == Category.RESOURCE_STATE_CONFLICT

    other = await engine.submit((TransferRequest(
        "parcel", "different", name="other.bin", preferred_provider=first.descriptor.id,
    ),), deduplicate=False)
    await engine.resolve_pending()
    other_artifact = (await repository.artifacts(other.id))[0]
    with pytest.raises(TransferError) as wrong:
        await manual_candidate_failover(engine, other.id, other_artifact.id, str(artifact.candidates[1].id))
    assert wrong.value.error.category == Category.SOURCE_NOT_FOUND

    failures = [item for item in (await repository.presentation(canonical.id, details=True))["manual_candidate_failovers"]
                if item["outcome"] == "failure"]
    assert len(failures) == 2
    assert all(item["execution_transition"] == "unchanged" for item in failures)


@pytest.mark.asyncio
async def test_disabled_selected_provider_is_rejected_without_substitution(tmp_path, monkeypatch):
    engine, repository, first, second, _executor = await build_engine(tmp_path, monkeypatch)
    canonical, _source, artifact = await attach_two(engine, repository, first, second)
    second.descriptor = replace(second.descriptor, enabled=False)
    wanted = artifact.candidates[1]
    with pytest.raises(TransferError) as rejected:
        await manual_candidate_failover(engine, canonical.id, artifact.id, str(wanted.id))
    assert rejected.value.error.category == Category.PROVIDER_UNAVAILABLE
    current = (await repository.artifacts(canonical.id))[0]
    assert current.selected == 0 and current.execution is None
    event = (await repository.presentation(canonical.id, details=True))["manual_candidate_failovers"][-1]
    assert event["outcome"] == "failure"
    assert event["requested_candidate_id"] == str(wanted.id)
    assert event["error"]["category"] == Category.PROVIDER_UNAVAILABLE.value


@pytest.mark.asyncio
async def test_duplicate_activation_and_stale_callback_cannot_restore_old_owner(tmp_path, monkeypatch):
    engine, repository, first, second, executor = await build_engine(tmp_path, monkeypatch)
    canonical, _source, artifact = await attach_two(engine, repository, first, second)
    await engine.reconcile_executions()
    artifact = (await repository.artifacts(canonical.id))[0]
    old = artifact.execution
    wanted = artifact.candidates[1]
    await manual_candidate_failover(engine, canonical.id, artifact.id, str(wanted.id))
    with pytest.raises(TransferError) as duplicate:
        await manual_candidate_failover(engine, canonical.id, artifact.id, str(wanted.id))
    assert duplicate.value.error.category == Category.RESOURCE_STATE_CONFLICT

    await repository.execution(ExecutionObservation(
        old,
        ExecutionState.TRANSFERRING,
        TransferProgress(4, 3, 1),
        ((await repository.artifacts(canonical.id))[0].target,),
    ))
    current = (await repository.artifacts(canonical.id))[0]
    assert current.selected == 1 and current.execution is None
    attempts = {item.handle.attempt_id: item for item in await repository.executions(canonical.id)}
    assert attempts[old.attempt_id].state == ExecutionState.CANCELLED
    await engine.reconcile_executions()
    active = (await repository.artifacts(canonical.id))[0]
    assert active.selected == 1 and active.execution is not None
    assert len([call for call in executor.calls if call[0] == "start"]) == 2
    successes = [item for item in (await repository.presentation(canonical.id, details=True))["manual_candidate_failovers"]
                 if item["outcome"] == "success"]
    assert len(successes) == 1


# ── DP 1.0.12 Manual Source Switch Queued Presentation corrective task ────
#
# The accepted manual candidate-switch path already calls
# ``engine.repository.transition_recovery(current.id, "queued", ...)`` at
# manual_failover.py's success boundary. Stale recovery/quiescence context
# from a pre-switch attempt (decision_action/decision_reason/quiescence_reason/
# wake_condition persisted in the durable recovery snapshot) can outlive that
# call, and transfers.presentation_repository.recovery_presentation's
# "requires_attention" branch does not gate on the artifact's raw status at
# all -- so a stale "wait_for_operator" + "operator_retry" combination can
# still render Requires Attention even though the artifact is now genuinely
# queued under the newly-accepted candidate. The fix reuses
# transition_recovery's own existing ``clear_quiescence`` flag (already the
# established pattern for every OTHER "back to queued/normal" recovery
# transition in transfers/_engine_recovery.py) at this one call site.


@pytest.mark.asyncio
async def test_case_a_http_successful_switch_clears_stale_operator_state_and_presents_queued(tmp_path, monkeypatch):
    """Matrix Case A: an HTTP(S)-like (host-scoped) artifact carrying stale
    wait_for_operator/operator_retry recovery context is manually switched to
    a valid alternate candidate. The switch must still select the correct
    candidate, the raw lifecycle must still be queued, the stale operator
    context must no longer be active, and a fresh presentation read must
    resolve to Queued -- not Requires Attention -- with no provider-specific
    override involved (HostParcelProvider is the same generic fake used by
    every other test in this file; nothing in the production diff branches on
    it)."""
    engine, repository, first, second, _executor = await build_engine(tmp_path, monkeypatch)
    canonical, _source, artifact = await attach_two(engine, repository, first, second)
    await _simulate_stale_operator_attention(repository, artifact.id)

    stale = await repository.presentation(canonical.id, details=True)
    stale_file = next(item for item in stale["files"] if item["id"] == artifact.id)
    assert stale_file["presentation_status"] == "requires_attention"
    assert stale_file["attention_required"] is True
    assert stale["presentation_status"] == "requires_attention"

    wanted = artifact.candidates[1]
    result = await manual_candidate_failover(engine, canonical.id, artifact.id, str(wanted.id))
    assert result["candidate_id"] == str(wanted.id)
    assert result["source_host"] == "provider-b.example"

    switched = (await repository.artifacts(canonical.id))[0]
    assert switched.selected == 1 and switched.execution is None and switched.state == "queued"

    fresh_context = await repository.recovery_context(artifact.id)
    assert fresh_context.get("wake_condition") is None
    assert fresh_context.get("quiescence_reason") is None

    fresh = await repository.presentation(canonical.id, details=True)
    fresh_file = next(item for item in fresh["files"] if item["id"] == artifact.id)
    assert fresh_file["presentation_status"] == "queued"
    assert fresh_file["attention_required"] is False
    # The transfer-level aggregate only elevates to a child's specific
    # status for actionable states (downloading/recovering/waiting_for_*/
    # requires_attention); a merely-queued lone child does not force the
    # transfer's own top-level status label, which is unrelated pre-existing
    # behavior this task does not touch. What matters here -- and what the
    # regression this task corrects was about -- is that Requires Attention
    # no longer survives at the aggregate level either.
    assert fresh["presentation_status"] != "requires_attention"


@pytest.mark.asyncio
async def test_case_b_debrid_successful_switch_also_remains_generic_queued(tmp_path, monkeypatch):
    """Matrix Case B: the identical semantic assertion as Case A, driven
    through a differently-identified ("alldebrid"/"alldebrid-mirror")
    provider pair using the same generic manual-candidate-switch machinery,
    proving the correction is provider-neutral rather than coincidentally
    tied to the "provider-a"/"provider-b" ids Case A and every pre-existing
    test in this file already use."""
    engine, repository, first, second, _executor = await build_engine(
        tmp_path, monkeypatch, provider_ids=("alldebrid", "alldebrid-mirror"),
    )
    canonical, _source, artifact = await attach_two(engine, repository, first, second)
    await _simulate_stale_operator_attention(repository, artifact.id)

    stale = await repository.presentation(canonical.id, details=True)
    assert stale["presentation_status"] == "requires_attention"

    wanted = artifact.candidates[1]
    result = await manual_candidate_failover(engine, canonical.id, artifact.id, str(wanted.id))
    assert result["candidate_id"] == str(wanted.id)
    assert result["provider_id"] == "alldebrid-mirror"

    switched = (await repository.artifacts(canonical.id))[0]
    assert switched.selected == 1 and switched.state == "queued"

    fresh = await repository.presentation(canonical.id, details=True)
    fresh_file = next(item for item in fresh["files"] if item["id"] == artifact.id)
    assert fresh_file["presentation_status"] == "queued"
    assert fresh_file["attention_required"] is False
    # The transfer-level aggregate only elevates to a child's specific
    # status for actionable states (downloading/recovering/waiting_for_*/
    # requires_attention); a merely-queued lone child does not force the
    # transfer's own top-level status label, which is unrelated pre-existing
    # behavior this task does not touch. What matters here -- and what the
    # regression this task corrects was about -- is that Requires Attention
    # no longer survives at the aggregate level either.
    assert fresh["presentation_status"] != "requires_attention"


@pytest.mark.asyncio
async def test_case_c_genuine_unresolved_operator_state_still_requires_attention(tmp_path, monkeypatch):
    """Matrix Case C: without any superseding successful switch, an artifact
    carrying the same genuine wait_for_operator/operator_retry recovery
    context must keep presenting Requires Attention exactly as before. This
    protects against the fix globally weakening recovery precedence -- the
    correction only fires at the accepted manual-switch transition, never as
    a general recovery-presentation change."""
    engine, repository, first, second, _executor = await build_engine(tmp_path, monkeypatch)
    canonical, _source, artifact = await attach_two(engine, repository, first, second)
    await _simulate_stale_operator_attention(repository, artifact.id)

    view = await repository.presentation(canonical.id, details=True)
    file_view = next(item for item in view["files"] if item["id"] == artifact.id)
    assert file_view["presentation_status"] == "requires_attention"
    assert file_view["attention_required"] is True
    assert view["presentation_status"] == "requires_attention"

    # No switch was ever attempted or accepted; re-reading again must be stable.
    still = await repository.presentation(canonical.id, details=True)
    assert still["presentation_status"] == "requires_attention"


@pytest.mark.asyncio
async def test_case_d_rejected_switch_preserves_recovery_context(tmp_path, monkeypatch):
    """Matrix Case D: a manual switch that is rejected by existing validation
    (here, the destination provider is disabled -- the same rejection
    ``test_disabled_selected_provider_is_rejected_without_substitution``
    already covers) must NOT clear the artifact's legitimate stale recovery
    context. The cleanup added by this task lives strictly inside the
    successful-transition branch, after every existing validation gate has
    already passed."""
    engine, repository, first, second, _executor = await build_engine(tmp_path, monkeypatch)
    canonical, _source, artifact = await attach_two(engine, repository, first, second)
    await _simulate_stale_operator_attention(repository, artifact.id)
    second.descriptor = replace(second.descriptor, enabled=False)

    wanted = artifact.candidates[1]
    with pytest.raises(TransferError) as rejected:
        await manual_candidate_failover(engine, canonical.id, artifact.id, str(wanted.id))
    assert rejected.value.error.category == Category.PROVIDER_UNAVAILABLE

    current = (await repository.artifacts(canonical.id))[0]
    assert current.selected == 0

    context = await repository.recovery_context(artifact.id)
    assert context.get("decision_action") == "wait_for_operator"
    assert context.get("wake_condition") == "operator_retry"
    assert context.get("quiescence_reason") == "recovery_exhausted"

    view = await repository.presentation(canonical.id, details=True)
    file_view = next(item for item in view["files"] if item["id"] == artifact.id)
    assert file_view["presentation_status"] == "requires_attention"
    assert file_view["attention_required"] is True


# ── DP 1.0.12 Manual Candidate-Switch Operation Boundary correction ───────
#
# Live production reproduction (transfer_id=228, artifact_id=16347,
# 2026-09-12 07:14 UTC): a successful manual candidate switch commits new
# child truth (candidate selected, old writer retired, artifact queued) and
# returns success BEFORE the parent transfer's own raw lifecycle state has
# been canonically re-aggregated from that just-committed child truth. The
# browser's immediate post-POST refresh could therefore transiently observe
# an artifact already queued under a stale parent still carrying an older
# failed/error-era raw state, and
# transfers.presentation_repository._aggregate_presentation()'s final
# fallback branch (a merely-queued child triggers none of the
# downloading/recovering/waiting_for_*/requires_attention special cases) then
# renders straight from that stale raw status. This is NOT the same
# regression the Case A-D matrix above proves; those fixtures never actually
# drove the PARENT's own raw transfer.state to a failed/error-era value
# before switching, so they could not have caught this. The fix makes
# manual_candidate_failover() call the existing canonical
# ``engine._aggregate(transfer_id)`` after committing success provenance and
# before returning.


@pytest.mark.asyncio
async def test_switch_reaggregates_stale_failed_parent_before_returning(tmp_path, monkeypatch):
    """Section 10.1: a genuinely FAILED parent (produced by the real
    canonical ``_aggregate()`` reacting to a real artifact error -- not a
    hand-authored raw row) must already read back as re-aggregated
    immediately after a successful switch, with NO scheduler tick,
    ``reconcile_executions()``, or manual ``_aggregate()`` call in between.
    Fails against the pre-correction implementation because
    ``manual_candidate_failover`` never re-aggregated parent truth before
    returning success, so the parent raw state remained ``error`` and
    presentation still fell back to Failed/Requires-Attention."""
    engine, repository, first, second, _executor = await build_engine(tmp_path, monkeypatch)
    canonical, _source, artifact = await attach_two(engine, repository, first, second)

    assert await repository.transition_recovery(artifact.id, "error", retry_at=0)
    await engine._aggregate(canonical.id)
    pre = await repository.get(canonical.id)
    assert pre.state == TransferState.FAILED, (
        "fixture setup must reproduce a genuinely FAILED parent raw state "
        "before the switch, matching the production pre-switch truth"
    )

    wanted = artifact.candidates[1]
    result = await manual_candidate_failover(engine, canonical.id, artifact.id, str(wanted.id))
    assert result["ok"] is True
    assert result["candidate_id"] == str(wanted.id)

    # No scheduler tick / reconcile_executions() / manual _aggregate() call
    # between the switch returning and these assertions -- this is the exact
    # externally-observable operation boundary the live reproduction caught.
    switched = (await repository.artifacts(canonical.id))[0]
    assert switched.selected == 1 and switched.execution is None and switched.state == "queued"

    post = await repository.get(canonical.id)
    assert post.state == TransferState.QUEUED, (
        "parent raw transfer state must already be canonically re-aggregated "
        f"immediately after a successful switch, not left at {post.state!r}"
    )

    fresh = await repository.presentation(canonical.id, details=True)
    assert fresh["presentation_status"] not in ("failed", "requires_attention"), (
        "immediate post-switch presentation must not fall back to the stale "
        f"pre-switch parent truth, got {fresh['presentation_status']!r}"
    )


@pytest.mark.asyncio
async def test_switch_on_one_artifact_leaves_multi_artifact_parent_transferring(tmp_path, monkeypatch):
    """Section 10.2/11.2: a transfer with two artifacts -- one still actively
    downloading, the other error/switchable with two canonical candidates --
    must have its parent remain TRANSFERRING immediately after switching the
    second artifact, never forced to QUEUED. This is a design-safety /
    existing-proof test rather than a RED/GREEN pair: because the fix calls
    the real canonical ``_aggregate()`` (whose own precedence already ranks
    any downloading/verifying child above a merely-queued one), it cannot
    regress this case by construction. What this test protects against is a
    *different*, incorrect fix shape explicitly warned against in the task
    (unconditionally writing ``TransferState.QUEUED`` for any accepted
    switch), which would wrongly stomp an actively-downloading sibling
    artifact's own transfer-level presentation."""
    engine, repository, first, second, _executor = await build_engine(tmp_path, monkeypatch)
    request_a = TransferRequest(
        "parcel", "keep-going", name="file-a.bin", preferred_provider=first.descriptor.id,
    )
    request_b = TransferRequest(
        "parcel", "needs-switch", name="file-b.bin", preferred_provider=first.descriptor.id,
    )
    canonical = await engine.submit((request_a, request_b), deduplicate=False)
    await engine.resolve_pending()
    # Add a second canonical candidate onto artifact B only, via the same
    # cross-transfer consolidation mechanism attach_two() already relies on.
    await engine.submit((TransferRequest(
        "parcel", "needs-switch-mirror", name="file-b.bin", preferred_provider=second.descriptor.id,
    ),), deduplicate=False)
    await engine.resolve_pending()
    await engine.reconcile_executions()

    by_name = {item.name: item for item in await repository.artifacts(canonical.id)}
    artifact_a, artifact_b = by_name["file-a.bin"], by_name["file-b.bin"]
    assert len(artifact_b.candidates) == 2
    assert artifact_a.execution is not None and artifact_a.state == "downloading"
    assert artifact_b.execution is not None

    # transition_recovery() only accepts an artifact whose current execution
    # attempt is already terminal -- terminate it first, exactly as a real
    # observed failure would, before forcing the recovery-error state.
    await repository.execution(ExecutionObservation(
        artifact_b.execution, ExecutionState.FAILED, TransferProgress(4, 0, 0),
        (artifact_b.target,),
    ))
    assert await repository.transition_recovery(artifact_b.id, "error", retry_at=0)
    b_before = next(item for item in await repository.artifacts(canonical.id) if item.id == artifact_b.id)
    wanted_index = 1 if b_before.selected == 0 else 0
    wanted = b_before.candidates[wanted_index]

    result = await manual_candidate_failover(engine, canonical.id, artifact_b.id, str(wanted.id))
    assert result["ok"] is True

    refreshed = {item.id: item for item in await repository.artifacts(canonical.id)}
    assert refreshed[artifact_a.id].state == "downloading"
    assert refreshed[artifact_a.id].execution is not None
    assert refreshed[artifact_b.id].state == "queued"
    assert refreshed[artifact_b.id].selected == wanted_index

    parent = await repository.get(canonical.id)
    assert parent.state == TransferState.TRANSFERRING, (
        "a still-downloading sibling artifact must keep the parent "
        f"TRANSFERRING, not {parent.state!r} -- canonical _aggregate() "
        "precedence, not a naive unconditional QUEUED write"
    )


@pytest.mark.asyncio
async def test_rejected_switch_never_triggers_parent_aggregate_or_changes_state(tmp_path, monkeypatch):
    """Section 10.4: a rejected switch must not fabricate any parent
    lifecycle change. Reuses the genuinely-FAILED-parent fixture from
    ``test_switch_reaggregates_stale_failed_parent_before_returning`` but
    targets a disabled provider so validation rejects the switch before the
    success path (and therefore the new ``_aggregate()`` call, which is
    structurally unreachable from any exception branch) is ever reached."""
    engine, repository, first, second, _executor = await build_engine(tmp_path, monkeypatch)
    canonical, _source, artifact = await attach_two(engine, repository, first, second)
    assert await repository.transition_recovery(artifact.id, "error", retry_at=0)
    await engine._aggregate(canonical.id)
    pre = await repository.get(canonical.id)
    assert pre.state == TransferState.FAILED

    second.descriptor = replace(second.descriptor, enabled=False)
    wanted = artifact.candidates[1]
    with pytest.raises(TransferError) as rejected:
        await manual_candidate_failover(engine, canonical.id, artifact.id, str(wanted.id))
    assert rejected.value.error.category == Category.PROVIDER_UNAVAILABLE

    current = (await repository.artifacts(canonical.id))[0]
    assert current.selected == 0

    post = await repository.get(canonical.id)
    assert post.state == TransferState.FAILED, (
        "a rejected switch must not touch parent lifecycle truth at all, "
        f"got {post.state!r}"
    )


@pytest.mark.asyncio
async def test_aggregate_failure_after_committed_switch_does_not_fabricate_success(tmp_path, monkeypatch):
    """Section 6.4: if the candidate mutation and its durable success
    provenance are already committed but the canonical re-aggregation step
    itself unexpectedly raises, the operator must not receive a success
    response while parent truth is knowingly stale, the old writer must not
    be fictitiously restored, and the already-durable success provenance
    must remain -- a contradictory 'failure' event must not be appended for
    a switch that genuinely succeeded."""
    engine, repository, first, second, _executor = await build_engine(tmp_path, monkeypatch)
    canonical, _source, artifact = await attach_two(engine, repository, first, second)
    wanted = artifact.candidates[1]

    async def boom(_transfer_id):
        raise RuntimeError("simulated aggregation failure")

    monkeypatch.setattr(engine, "_aggregate", boom)
    with pytest.raises(TransferError) as failed:
        await manual_candidate_failover(engine, canonical.id, artifact.id, str(wanted.id))
    assert failed.value.error.category not in (Category.PROVIDER_UNAVAILABLE, Category.SOURCE_NOT_FOUND)

    # The mutation itself is not rolled back -- the switch genuinely
    # succeeded before aggregation failed.
    switched = (await repository.artifacts(canonical.id))[0]
    assert switched.selected == 1 and switched.execution is None and switched.state == "queued"

    events = (await repository.presentation(canonical.id, details=True))["manual_candidate_failovers"]
    successes = [item for item in events if item["outcome"] == "success"]
    failures = [item for item in events if item["outcome"] == "failure"]
    assert len(successes) == 1 and successes[0]["selected_candidate_id"] == str(wanted.id)
    assert len(failures) == 0, (
        "an aggregation failure after a genuinely successful switch must "
        "not be recorded as a contradictory candidate-switch failure event"
    )
