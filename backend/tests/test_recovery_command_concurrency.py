"""Section 11 (recovery-claim integration), Section 27 (writer retirement
fencing), and Section 33's "Command races" regression set (DP 1.0.12
recovery leveling, Phase 2 correction round and Phase 4 Section 25).

Automatic recovery (AUTO_RETRY/EXECUTOR_RECOVERY/PROVIDER_RECOVERY/
STARTUP_RECONCILE/USER_RETRY/RESUME) already runs inside the exclusive claim
``recover_artifact`` acquired for its OWN real trigger before it ever decides
to try an alternate candidate. The canonical candidate-activation operation
(``transfers.candidate_activation.activate_candidate``) must be called
*inside* that claim, attributed to its real authority -- never a second,
nested claim, and never relabeled as the operator's USER_CANDIDATE_SWITCH
merely because both paths share the same mutation function.

The Phase-4 additions below (``test_manual_activation_vs_resume_is_generation_safe``,
``test_manual_activation_vs_retry_is_generation_safe``,
``test_manual_activation_vs_pause_is_deterministic``) exercise the SAME
production ``convergence_engine.TransferEngine``/``recovery_repository
.TransferRepository`` stack under real concurrent ``asyncio.gather`` execution
against ``resume()``/``retry()``/``pause()`` -- the production overrides in
``transfers.convergence_engine.TransferEngine`` route ``resume``/``retry``
through the SAME exclusive ``claim_recovery`` system as candidate activation
(``recover_artifact(trigger=RecoveryTrigger.RESUME/USER_RETRY)``), and ``pause``
through the SAME per-execution-attempt ``_convergence_lock`` candidate
activation's own writer-retirement dance uses. These tests prove that
composition holds under real concurrency, not merely that each mechanism
exists in isolation.
"""
from __future__ import annotations

import asyncio

import pytest

from db.database import get_db
from test_candidate_activation_phase2 import activate_with_real_claim, attach_three, build_engine3
from test_ws2p1_failover_depth import remote_failure
from transfers import codec
from transfers.manual_failover import manual_candidate_failover
from transfers.models import ExecutionState
from transfers.recovery_execution import RecoveryTrigger


async def _candidate_activation_events(transfer_id: int) -> list[dict]:
    async with get_db() as db:
        rows = await db.fetchall(
            "SELECT detail FROM application_events WHERE transfer_id=? AND kind='candidate_activation' ORDER BY id",
            (transfer_id,),
        )
    return [codec.load(row["detail"], {}) for row in rows]


async def _exhaust_current(engine, repository, executor, canonical_id, error, attempts=3):
    from dataclasses import replace
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


@pytest.mark.asyncio
async def test_automatic_alternate_activation_reuses_existing_recovery_claim(tmp_path, monkeypatch):
    engine, repository, providers, executor, _now_box = await build_engine3(tmp_path, monkeypatch)
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()

    claim_trigger_calls = []
    original = repository.claim_recovery

    async def spy(artifact_id, trigger, *args, **kwargs):
        claim_trigger_calls.append((artifact_id, RecoveryTrigger(trigger)))
        return await original(artifact_id, trigger, *args, **kwargs)

    monkeypatch.setattr(repository, "claim_recovery", spy)

    _handles, exhausted = await _exhaust_current(
        engine, repository, executor, canonical.id, remote_failure(), attempts=3,
    )
    assert exhausted.selected == 1  # the automatic alternate switch happened

    this_artifact_triggers = [trigger for aid, trigger in claim_trigger_calls if aid == artifact.id]
    assert this_artifact_triggers, "expected at least one automatic recovery claim for this artifact"
    # Every claim acquired for this artifact during automatic exhaustion used
    # its REAL automatic trigger -- never the operator's USER_CANDIDATE_SWITCH
    # identity, even though the candidate switch happened inside one of them.
    assert all(trigger == RecoveryTrigger.AUTO_RETRY for trigger in this_artifact_triggers)
    assert RecoveryTrigger.USER_CANDIDATE_SWITCH not in this_artifact_triggers
    # Section 29: the durable activation provenance records the REAL
    # automatic authority, not a fabricated operator one.
    events = await _candidate_activation_events(canonical.id)
    activated = [event for event in events if event.get("outcome") == "activated"]
    assert activated and activated[-1]["authority"] == "auto_retry"
    # ...and a REAL claim generation: the retired claim-less mode recorded ``None`` here.
    assert isinstance(activated[-1]["recovery_generation"], int) and activated[-1]["recovery_generation"] >= 1


@pytest.mark.asyncio
async def test_manual_switch_uses_user_candidate_switch_trigger(tmp_path, monkeypatch):
    engine, repository, providers, _executor, _now_box = await build_engine3(tmp_path, monkeypatch)
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()

    claim_trigger_calls = []
    original = repository.claim_recovery

    async def spy(artifact_id, trigger, *args, **kwargs):
        claim_trigger_calls.append((artifact_id, RecoveryTrigger(trigger)))
        return await original(artifact_id, trigger, *args, **kwargs)

    monkeypatch.setattr(repository, "claim_recovery", spy)

    result = await manual_candidate_failover(engine, canonical.id, artifact.id, str(artifact.candidates[1].id))
    assert result["ok"] is True

    this_artifact_triggers = [trigger for aid, trigger in claim_trigger_calls if aid == artifact.id]
    assert this_artifact_triggers == [RecoveryTrigger.USER_CANDIDATE_SWITCH]

    events = await _candidate_activation_events(canonical.id)
    activated = [event for event in events if event.get("outcome") == "activated"]
    # Both paths arrive at the SAME canonical activation mutation function
    # (transfers.candidate_activation.activate_candidate) and its one
    # provenance record -- only the recorded authority differs.
    assert activated and activated[-1]["authority"] == "user_candidate_switch"
    assert isinstance(activated[-1]["recovery_generation"], int) and activated[-1]["recovery_generation"] >= 1


@pytest.mark.asyncio
async def test_late_retired_writer_observation_cannot_mutate_new_generation(tmp_path, monkeypatch):
    """Section 27: once a writer has been retired by a committed candidate
    activation, a LATE observation of that same (now-stale) execution handle
    must not be able to mutate the artifact's new generation."""
    engine, repository, providers, _executor, _now_box = await build_engine3(tmp_path, monkeypatch)
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]
    old_handle = live.execution
    assert old_handle is not None

    result = await activate_with_real_claim(engine, live, 1, retry_at=engine.clock())
    assert result.committed
    await engine.reconcile_executions()
    new_state = (await repository.artifacts(canonical.id))[0]
    assert new_state.selected == 1 and new_state.execution is not None
    assert new_state.execution.attempt_id != old_handle.attempt_id

    # The retired attempt row still exists (observation history is never
    # deleted), so a late observation of it is accepted as a write to THAT
    # row -- but it is fenced from ever mutating the artifact's new
    # generation: download_files.execution_attempt_id already points at the
    # new writer, so this late write is inert exactly where it matters.
    from transfers.models import ExecutionObservation
    stale_observation = ExecutionObservation(old_handle, ExecutionState.TRANSFERRING)
    await repository.execution(stale_observation)

    unchanged = (await repository.artifacts(canonical.id))[0]
    assert unchanged.selected == 1
    assert unchanged.execution is not None and unchanged.execution.attempt_id == new_state.execution.attempt_id
    assert not await repository.authorize_execution(old_handle, "resume")


@pytest.mark.asyncio
async def test_manual_activation_vs_cancel_never_reauthorizes_writer(tmp_path, monkeypatch):
    """Section 27: never authorize two current writers for one artifact --
    a manual switch's retired writer must not become authorized again by a
    concurrent/late cancel-path observation."""
    engine, repository, providers, _executor, _now_box = await build_engine3(tmp_path, monkeypatch)
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]
    old_handle = live.execution

    result = await manual_candidate_failover(engine, canonical.id, artifact.id, str(artifact.candidates[1].id))
    assert result["ok"] is True
    assert not await repository.authorize_execution(old_handle, "start")
    assert not await repository.authorize_execution(old_handle, "resume")

    await engine.reconcile_executions()
    current = (await repository.artifacts(canonical.id))[0]
    live_attempts = [
        item for item in await repository.executions(canonical.id)
        if item.state in {"prepared", "queued", "transferring", "unknown"}
    ]
    # Exactly one authorized live writer for this artifact, ever -- the new
    # one -- never the retired one restored alongside it.
    assert len(live_attempts) == 1
    assert current.execution is not None and live_attempts[0].handle == current.execution


@pytest.mark.asyncio
async def test_uncertain_writer_retirement_never_commits_candidate_activation(tmp_path, monkeypatch):
    """Section 27: retirement REQUESTED but the external writer's terminal
    state cannot be CONFIRMED must never commit the candidate activation --
    the old writer remains the only authorized generation, the selected
    candidate is unchanged, no continuation reservation is created, the new
    writer is never authorized, and the command is a real, retryable
    rejection (ACTIVATION_NOT_COMMITTED), never a plain exception."""
    from dataclasses import replace as dataclass_replace

    from transfers.models import ExecutionState, OutcomeKind, TransferOutcome

    engine, repository, providers, executor, now_box = await build_engine3(tmp_path, monkeypatch)
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]
    old_handle = live.execution
    assert old_handle is not None

    async def unconfirmable_cancel(handle):
        # The cancel call itself reports success, but the external writer's
        # actual state -- observed immediately after -- cannot be confirmed
        # terminal (still "transferring"), exactly the "requested but not
        # confirmed" case Section 27 requires activation to reject.
        executor.jobs[handle.attempt_id] = dataclass_replace(
            executor.jobs[handle.attempt_id], state=ExecutionState.TRANSFERRING,
        )
        return TransferOutcome(OutcomeKind.CANCELLED)

    monkeypatch.setattr(executor, "cancel", unconfirmable_cancel)

    result = await activate_with_real_claim(engine, live, 1, retry_at=now_box[0])
    assert result.committed is False
    assert result.reason == "writer_retirement_uncertain"
    assert result.retirement == "uncertain"

    unchanged = (await repository.artifacts(canonical.id))[0]
    assert unchanged.selected == 0, "selected candidate must be unchanged"
    assert unchanged.execution is not None and unchanged.execution.attempt_id == old_handle.attempt_id
    assert await repository.continuation_reservation(artifact.id) is None, "no reservation for a rejected activation"

    live_attempts = [
        item for item in await repository.executions(canonical.id)
        if item.state in {"prepared", "queued", "transferring", "unknown"}
    ]
    # The old writer remains the ONLY authorized generation -- no new writer
    # was ever authorized alongside it.
    assert len(live_attempts) == 1 and live_attempts[0].handle == old_handle


@pytest.mark.asyncio
async def test_candidate_activation_provenance_links_to_replacement_execution(tmp_path, monkeypatch):
    """Section 29: a committed activation cannot know the replacement
    execution's identity at commit time (it doesn't exist yet), but the
    durable audit link must still be completable without guessing from
    timestamps, URLs, or current candidate state -- transfers._repository_base
    .TransferRepository.prepare_execution links it in-place the first time
    this artifact actually dispatches afterward."""
    engine, repository, providers, _executor, now_box = await build_engine3(tmp_path, monkeypatch)
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]

    result = await activate_with_real_claim(engine, live, 1, retry_at=now_box[0])
    assert result.committed

    before_dispatch = await _candidate_activation_events(canonical.id)
    activated = [event for event in before_dispatch if event.get("outcome") == "activated"][-1]
    assert activated["new_execution_id"] is None, "not knowable before the replacement ever dispatches"

    await engine.reconcile_executions()
    dispatched = (await repository.artifacts(canonical.id))[0]
    assert dispatched.execution is not None and dispatched.selected == 1

    after_dispatch = await _candidate_activation_events(canonical.id)
    linked = [event for event in after_dispatch if event.get("outcome") == "activated"][-1]
    assert linked["new_execution_id"] == dispatched.execution.attempt_id, (
        "the exact replacement execution must be traceable back to this exact "
        "candidate activation without inference"
    )


@pytest.mark.asyncio
async def test_manual_activation_vs_resume_is_generation_safe(tmp_path, monkeypatch):
    """Section 25/33: a concurrent operator RESUME must never restore the old
    candidate/writer, clear a newer manual switch, or authorize two writers.
    Production ``resume()`` (transfers.convergence_engine.TransferEngine)
    routes through ``recover_artifact(trigger=RecoveryTrigger.RESUME)`` -- the
    SAME exclusive claim system candidate activation uses -- so whichever
    acquires the claim first fully owns the mutation; the loser is a clean
    no-op, never a partial or corrupted one."""
    engine, repository, providers, _executor, now_box = await build_engine3(tmp_path, monkeypatch)
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]
    assert live.execution is not None

    await engine.pause(canonical.id)

    results = await asyncio.gather(
        engine.activate_candidate_command(canonical.id, artifact.id, 1),
        engine.resume(canonical.id),
    )
    activation_result = results[0]

    live_attempts = [
        item for item in await repository.executions(canonical.id)
        if item.state in {"prepared", "queued", "transferring", "unknown"}
    ]
    assert len(live_attempts) <= 1, "never two live writers for one artifact"
    current = (await repository.artifacts(canonical.id))[0]
    if live_attempts:
        assert current.execution is not None and live_attempts[0].handle == current.execution
    if activation_result is not None and activation_result.committed:
        assert current.selected == 1, "a committed switch's candidate selection must survive a concurrent resume"


@pytest.mark.asyncio
async def test_manual_activation_vs_retry_is_generation_safe(tmp_path, monkeypatch):
    """Section 25/33: a concurrent operator RETRY (production ``retry()``
    routes each artifact through ``recover_artifact(trigger=RecoveryTrigger
    .USER_RETRY)``, the same exclusive claim as candidate activation) must
    never restore the old candidate/writer or authorize two writers."""
    engine, repository, providers, _executor, now_box = await build_engine3(tmp_path, monkeypatch)
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]
    old_handle = live.execution
    assert old_handle is not None

    await asyncio.gather(
        engine.activate_candidate_command(canonical.id, artifact.id, 1),
        engine.retry(canonical.id),
    )

    live_attempts = [
        item for item in await repository.executions(canonical.id)
        if item.state in {"prepared", "queued", "transferring", "unknown"}
    ]
    assert len(live_attempts) <= 1, "never two live writers for one artifact"
    assert not await repository.authorize_execution(old_handle, "resume"), (
        "the pre-race writer must never become re-authorized by a racing retry"
    )
    current = (await repository.artifacts(canonical.id))[0]
    if live_attempts:
        assert current.execution is not None and live_attempts[0].handle == current.execution


@pytest.mark.asyncio
async def test_manual_activation_vs_pause_is_deterministic(tmp_path, monkeypatch):
    """Section 25/33: a concurrent operator PAUSE (production ``pause()``
    converges every active execution through ``_converge_execution``, the SAME
    per-execution-attempt lock candidate activation's own writer-retirement
    dance uses) must never leave two authorized writers or a torn
    candidate-selection/execution pairing, regardless of which side's
    mutation happens to land first."""
    engine, repository, providers, _executor, now_box = await build_engine3(tmp_path, monkeypatch)
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]
    assert live.execution is not None

    activation_result, _pause_errors = await asyncio.gather(
        engine.activate_candidate_command(canonical.id, artifact.id, 1),
        engine.pause(canonical.id),
    )

    live_attempts = [
        item for item in await repository.executions(canonical.id)
        if item.state in {"prepared", "queued", "transferring", "unknown", "paused"}
    ]
    assert len(live_attempts) <= 1, "never two live writers for one artifact"
    current = (await repository.artifacts(canonical.id))[0]
    if live_attempts:
        assert current.execution is not None and live_attempts[0].handle == current.execution
    if activation_result is not None and activation_result.committed:
        assert current.selected == 1, "a committed switch's candidate selection is never torn by a concurrent pause"


@pytest.mark.asyncio
async def test_manual_activation_vs_auto_retry_is_generation_safe(tmp_path, monkeypatch):
    """Section 25/33 (missing pairing named on review): a manual candidate
    switch racing a REAL scheduler-driven AUTO_RETRY recovery for the SAME
    artifact -- both production ``convergence_engine.TransferEngine.recover_artifact``
    callers, competing for the SAME ``claim_recovery`` (exclusive across every
    trigger, including USER_CANDIDATE_SWITCH) -- must never authorize two
    writers or leave a torn candidate/execution pairing, regardless of which
    side wins the claim."""
    from dataclasses import replace as dataclass_replace

    engine, repository, providers, executor, now_box = await build_engine3(tmp_path, monkeypatch)
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]
    old_handle = live.execution
    assert old_handle is not None

    # A genuine execution failure the NEXT reconcile_executions() cycle will
    # observe and route into recover_artifact(trigger=AUTO_RETRY) for THIS
    # artifact -- the real scheduler path, not a direct internal call.
    executor.jobs[old_handle.attempt_id] = dataclass_replace(
        executor.jobs[old_handle.attempt_id], state=ExecutionState.FAILED, error=remote_failure(),
    )

    await asyncio.gather(
        engine.reconcile_executions(),
        engine.activate_candidate_command(canonical.id, artifact.id, 2),
    )

    live_attempts = [
        item for item in await repository.executions(canonical.id)
        if item.state in {"prepared", "queued", "transferring", "unknown"}
    ]
    assert len(live_attempts) <= 1, "never two live writers for one artifact"
    assert not await repository.authorize_execution(old_handle, "resume"), (
        "the pre-race failed writer must never become re-authorized"
    )
    current = (await repository.artifacts(canonical.id))[0]
    if live_attempts:
        assert current.execution is not None and live_attempts[0].handle == current.execution
    # Whichever side won the exclusive claim, the durable activation
    # provenance trail is never self-contradictory (no two "activated"
    # records both claiming the SAME old->new pair for different winners).
    events = await _candidate_activation_events(canonical.id)
    activated = [event for event in events if event.get("outcome") == "activated"]
    assert len(activated) <= 1


@pytest.mark.asyncio
async def test_manual_activation_vs_scheduler_execution_observation_is_deterministic(tmp_path, monkeypatch):
    """Section 25/33 (missing pairing named on review): a manual candidate
    switch racing an ORDINARY (non-failure) scheduler execution observation
    for the SAME execution attempt. Both the scheduler's
    ``_process_executions`` -> ``_converge_execution`` path and candidate
    activation's own writer-retirement dance
    (``transfers.candidate_activation.activate_candidate``) acquire the SAME
    ``engine._convergence_lock(attempt_id)`` -- this proves that composition
    holds under real concurrent execution, not merely that each caller uses
    the lock in isolation."""
    from dataclasses import replace as dataclass_replace

    engine, repository, providers, executor, now_box = await build_engine3(tmp_path, monkeypatch)
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]
    old_handle = live.execution
    assert old_handle is not None

    # Ordinary, healthy progress -- NOT a failure -- for the very next
    # observation the scheduler will make of this same attempt.
    executor.jobs[old_handle.attempt_id] = dataclass_replace(
        executor.jobs[old_handle.attempt_id],
        state=ExecutionState.TRANSFERRING,
        progress=executor.jobs[old_handle.attempt_id].progress,
    )

    await asyncio.gather(
        engine.reconcile_executions(),
        engine.activate_candidate_command(canonical.id, artifact.id, 1),
    )

    live_attempts = [
        item for item in await repository.executions(canonical.id)
        if item.state in {"prepared", "queued", "transferring", "unknown"}
    ]
    assert len(live_attempts) <= 1, "never two live writers for one artifact"
    current = (await repository.artifacts(canonical.id))[0]
    if live_attempts:
        assert current.execution is not None and live_attempts[0].handle == current.execution
    if current.execution is not None and current.execution.attempt_id != old_handle.attempt_id:
        # The switch committed -- the old handle must never be usable again,
        # regardless of whatever progress observation the scheduler recorded
        # for it concurrently.
        assert not await repository.authorize_execution(old_handle, "resume")


@pytest.mark.asyncio
async def test_manual_activation_vs_delete_never_reauthorizes_or_corrupts(tmp_path, monkeypatch):
    """Section 25/33 (missing pairing named on review): DELETE participates in
    NEITHER the exclusive recovery claim NOR the per-attempt convergence lock
    NOR ``_transfer_locks`` -- its safety against a concurrent candidate
    activation rests entirely on DB-transaction atomicity: both
    ``TransferRepository.transition_recovery`` (activation's commit) and
    ``TransferRepository.delete`` re-read the CURRENT authorized writer/
    transfer status from inside their own ``BEGIN IMMEDIATE`` transaction
    immediately before writing, so whichever commits first is respected and
    the other either cleanly no-ops (activation sees a deleted transfer) or
    correctly still catches the fresh writer (delete's cleanup UPDATE reads
    ``authorized=1`` at commit time, not a stale id). This proves that
    property under real concurrency."""
    engine, repository, providers, _executor, now_box = await build_engine3(tmp_path, monkeypatch)
    canonical, artifact = await attach_three(engine, repository, providers)
    await engine.reconcile_executions()
    live = (await repository.artifacts(canonical.id))[0]
    old_handle = live.execution
    assert old_handle is not None

    activation_result, _delete_result = await asyncio.gather(
        engine.activate_candidate_command(canonical.id, artifact.id, 1),
        engine.delete(canonical.id, remote=False),
    )

    transfer = await repository.get(canonical.id)
    assert transfer.state.value == "deleted"
    # No matter which order the two transactions actually applied in, no
    # writer is ever left authorized once the transfer is deleted.
    live_attempts = [
        item for item in await repository.executions(canonical.id)
        if item.state in {"prepared", "queued", "transferring", "unknown"}
    ]
    assert not await repository.live_executions(), (
        "a deleted transfer must never leave a currently-authorized writer "
        "counted as live, regardless of whether activation committed first"
    )
    if activation_result is not None and activation_result.committed:
        # Even a just-committed switch's NEW writer must be caught by
        # delete's own fresh (not stale-id) authorized-writer read.
        new_handle = activation_result.new_candidate
        assert new_handle is not None
        for attempt in live_attempts:
            assert not await repository.authorize_execution(attempt.handle, "resume")
    assert not await repository.authorize_execution(old_handle, "resume")
