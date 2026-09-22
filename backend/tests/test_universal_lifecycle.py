"""Real persistence and lifecycle driven entirely by unrelated fake integrations."""
import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Origin, Recovery, Retryability, Stage
from transfers.models import (
    CleanupAuthority, ExecutionObservation, ExecutionRequest, ExecutionState,
    OutcomeKind, Ownership, ResolutionResult, ResourceState, TransferOutcome,
    TransferRequest, TransferState,
    SourceIdentity, ArtifactFingerprint,
)
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


@pytest_asyncio.fixture
async def core(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider()
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    now = [1000.0]
    policy = TransferPolicy(retry_delay=1, adoption_stability_seconds=0, max_active_executions=2)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"), policy=policy, clock=lambda: now[0])
    await engine.initialize()
    return SimpleNamespace(engine=engine, repository=repository, registry=registry, provider=provider, executor=executor, now=now)


@pytest_asyncio.fixture
async def canonical_core(tmp_path, monkeypatch):
    """DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
    closure): a small number of tests in this module (and in
    test_universal_hardening.py / test_universal_parity.py, which import
    fixtures from here) actually drive a failure/retry/refresh sequence and
    assert on its durable recovery-decision representation -- that authority
    now lives exclusively in the canonical stack
    (transfers.convergence_engine.TransferEngine +
    transfers.recovery_repository.TransferRepository), so those specific
    tests use this fixture instead of ``core``. Most tests in these three
    files exercise only neutral lifecycle mechanics common to both
    compositions and are deliberately left on ``core``."""
    from transfers.convergence_engine import TransferEngine as CanonicalEngine
    from transfers.recovery_repository import TransferRepository as CanonicalRepository

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = CanonicalRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider()
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    now = [1000.0]
    policy = TransferPolicy(retry_delay=1, adoption_stability_seconds=0, max_active_executions=2)
    engine = CanonicalEngine(repository, registry, download_root=str(tmp_path / "payloads"), policy=policy, clock=lambda: now[0])
    await engine.initialize()
    return SimpleNamespace(engine=engine, repository=repository, registry=registry, provider=provider, executor=executor, now=now)


async def submit(core, payload="box", name="payload.bin"):
    return await core.engine.submit((TransferRequest("parcel", payload, name=name),))


def failure(category=Category.UNMAPPED_PROVIDER_ERROR, *, retryability=Retryability.UNKNOWN, recovery=Recovery.REQUIRE_OPERATOR, domain=Domain.PROVIDER, origin=Origin.CORE):
    return NormalizedError(domain, category, Stage.RESOLUTION, retryability, recovery, origin=origin)


@pytest.mark.asyncio
async def test_identity_is_durable_before_resolution_and_survives_completion(core):
    transfer = await submit(core)
    assert transfer.state == TransferState.ACCEPTED
    assert not core.provider.calls
    records = await core.repository.requests(transfer.id)
    assert records[0].request.kind == "parcel"
    await core.engine.tick()
    artifacts = await core.repository.artifacts(transfer.id)
    assert len(artifacts) == 1
    handle = artifacts[0].execution
    assert handle and handle.attempt_id != str(transfer.id)
    core.executor.finish(handle)
    await core.engine.tick()
    completed = await core.repository.get(transfer.id)
    assert completed.state == TransferState.COMPLETED
    assert completed.progress == 100
    attempts = await core.repository.executions(transfer.id)
    assert len(attempts) == 1
    assert attempts[0].handle == handle


@pytest.mark.asyncio
async def test_pause_accepts_requests_without_contact_and_resume_one_preserves_siblings(canonical_core):
    # DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
    # closure, Gate 9 revision 5): pause/resume/pause_all/resume_all are now
    # exclusively a canonical-stack responsibility.
    core = canonical_core
    await core.engine.pause_all()
    first = await submit(core, "one", "one.bin")
    second = await submit(core, "two", "two.bin")
    await core.engine.tick()
    assert core.provider.calls == []
    assert (await core.repository.get(first.id)).state == TransferState.PAUSED
    await core.engine.resume(first.id)
    await core.engine.tick()
    assert [value for operation, value in core.provider.calls if operation == "resolve"] == ["one"]
    assert (await core.repository.get(second.id)).paused


@pytest.mark.asyncio
async def test_transient_retry_uses_durable_budget_and_elapsed_deadline(core):
    error = failure(Category.PROVIDER_UNAVAILABLE, retryability=Retryability.BACKOFF, recovery=Recovery.BACKOFF)
    core.provider.responses = [ResolutionResult(ResourceState.UNKNOWN, error=error)] * 5
    transfer = await submit(core)
    await core.engine.tick()
    await core.engine.tick()
    assert len(core.provider.calls) == 1
    for advance in (1, 2, 10, 10):
        core.now[0] += advance
        await core.engine.tick()
    assert len(core.provider.calls) == 3
    record = (await core.repository.requests(transfer.id))[0]
    assert record.attempts == 3
    assert record.state == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("error,expected_calls", [
    (failure(), 3),
    (failure(Category.DESTINATION_BLOCKED, retryability=Retryability.BACKOFF, recovery=Recovery.RETRY), 1),
])
async def test_unknown_retries_are_bounded_while_security_never_retries(core, error, expected_calls):
    core.provider.responses = [ResolutionResult(ResourceState.UNKNOWN, error=error)] * 5
    transfer = await submit(core)
    for _ in range(5):
        await core.engine.tick()
        core.now[0] += 1000
    assert len(core.provider.calls) == expected_calls
    assert (await core.repository.get(transfer.id)).state == TransferState.FAILED
    assert core.executor.calls == []


@pytest.mark.asyncio
async def test_provider_preparation_and_manifest_do_not_define_local_progress(core):
    result = core.provider.parcel()
    core.provider.responses = [result]
    transfer = await submit(core)
    await core.engine.tick()
    assert (await core.repository.get(transfer.id)).progress == 0
    core.provider.resources[result.observation.resource.id] = replace(result.observation, state=ResourceState.AVAILABLE)
    await core.engine.tick()
    await core.engine.tick()
    artifact = (await core.repository.artifacts(transfer.id))[0]
    assert artifact.target.endswith("Parcel/folder/payload.bin")
    assert artifact.execution is not None
    assert (await core.repository.get(transfer.id)).state == TransferState.TRANSFERRING


@pytest.mark.asyncio
async def test_delete_wins_over_late_provider_resource_creation(core):
    core.provider.entered, core.provider.release = asyncio.Event(), asyncio.Event()
    result = core.provider.parcel(state=ResourceState.AVAILABLE)
    core.provider.responses = [result]
    transfer = await submit(core)
    running = asyncio.create_task(core.engine.tick())
    await core.provider.entered.wait()
    await core.engine.delete(transfer.id, remote=True)
    core.provider.release.set()
    await running
    assert (await core.repository.get(transfer.id)).state == TransferState.DELETED
    cleanup = [value for operation, value in core.provider.calls if operation == "cleanup"]
    assert len(cleanup) == 1
    assert cleanup[0].authority == CleanupAuthority.USER_REQUEST
    assert not core.executor.calls


@pytest.mark.asyncio
async def test_delete_without_remote_authority_retains_even_created_resources(core):
    result = core.provider.parcel()
    core.provider.responses = [result]
    transfer = await submit(core)
    await core.engine.tick()
    await core.engine.delete(transfer.id, remote=False)
    assert not [entry for entry in core.provider.calls if entry[0] == "cleanup"]


@pytest.mark.asyncio
async def test_restart_recovers_same_execution_and_does_not_dispatch_duplicate(core):
    transfer = await submit(core)
    await core.engine.tick()
    original = (await core.repository.artifacts(transfer.id))[0].execution
    restarted = TransferEngine(TransferRepository(), core.registry, download_root=core.engine.root, policy=core.engine.policy, clock=lambda: core.now[0])
    await restarted.initialize()
    await restarted.tick()
    assert (await core.repository.artifacts(transfer.id))[0].execution == original
    assert len([entry for entry in core.executor.calls if entry[0] == "start"]) == 1


@pytest.mark.asyncio
async def test_completed_executor_observation_requires_actual_payload(core):
    transfer = await submit(core)
    await core.engine.tick()
    artifact = (await core.repository.artifacts(transfer.id))[0]
    core.executor.finish(artifact.execution, materialize=False)
    await core.engine.tick()
    assert (await core.repository.get(transfer.id)).state == TransferState.FAILED
    refreshed = (await core.repository.artifacts(transfer.id))[0]
    assert refreshed.error.category == Category.MATERIALIZATION_FAILED


@pytest.mark.asyncio
async def test_manual_retry_preserves_completed_sibling_and_canonical_paths(canonical_core):
    # DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
    # closure, Gate 9 revision 5): operator-initiated retry (reacquire=False,
    # the default) is now exclusively a canonical-stack responsibility.
    core = canonical_core
    transfer = await core.engine.submit((TransferRequest("parcel", "one", name="one.bin"), TransferRequest("parcel", "two", name="two.bin")))
    core.executor.start_errors = [None, failure(Category.UNMAPPED_EXECUTOR_ERROR, domain=Domain.EXECUTOR)]
    await core.engine.tick()
    before = await core.repository.artifacts(transfer.id)
    core.executor.finish(before[0].execution)
    await core.engine.tick()
    assert await core.engine.retry(transfer.id)
    await core.engine.tick()
    after = await core.repository.artifacts(transfer.id)
    assert after[0].execution == before[0].execution
    assert after[0].state == "completed"
    assert after[1].execution != before[1].execution
    assert [(item.id, item.target) for item in before] == [(item.id, item.target) for item in after]
    assert len(await core.repository.executions(transfer.id)) == 3


@pytest.mark.asyncio
async def test_explicit_reacquisition_revalidates_completed_history(canonical_core):
    """Terminal-transfer reacquisition (DP 1.0.12 canonical lifecycle/
    recovery/completion rework, CANON-001 closure): ``_reacquire_transfer``
    is defined only on ``convergence_engine.TransferEngine``, so this
    semantic recovery/control test must build the real canonical stack."""
    core = canonical_core
    transfer = await submit(core)
    await core.engine.tick()
    artifact = (await core.repository.artifacts(transfer.id))[0]
    core.executor.finish(artifact.execution)
    await core.engine.tick()
    Path(artifact.target).unlink()
    submitted = await submit(core)
    assert submitted.id == transfer.id
    await core.engine.tick()
    repaired = (await core.repository.artifacts(transfer.id))[0]
    assert repaired.execution != artifact.execution
    assert repaired.target == artifact.target
    assert repaired.state == "downloading"


async def _reacquisition_setup_blocked_at_cancel(core):
    """Shared setup for the adversarial concurrency tests below: a completed
    transfer whose payload has since disappeared, positioned so a
    re-submission's ``_reacquire_transfer`` will call ``executor.cancel()``
    on the stale execution handle -- the exact call these tests block on to
    force a deterministic interleaving window."""
    transfer = await submit(core)
    await core.engine.tick()
    artifact = (await core.repository.artifacts(transfer.id))[0]
    core.executor.finish(artifact.execution)
    await core.engine.tick()
    Path(artifact.target).unlink()

    entered = asyncio.Event()
    release = asyncio.Event()
    original_cancel = core.executor.cancel

    async def blocking_cancel(handle):
        entered.set()
        await release.wait()
        return await original_cancel(handle)

    core.executor.cancel = blocking_cancel
    return transfer, artifact, entered, release


async def _reacquisition_setup_blocked_at_observe(core):
    """DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
    closure, Gate 9 revision 8): a review found that rev. 7 still performed
    ``_reacquire_transfer``'s FIRST ``executor.observe()`` in the
    plan-building loop, before either ``_execution_cycle_lock`` or the
    per-attempt ``_convergence_lock`` was acquired -- a concurrent
    ``pause()``/``resume()`` could enter ``_converge_execution()`` and issue
    its OWN native ``observe()`` under that lock while this method's
    unfenced ``observe()`` ran at the same time on the same handle. The fix
    moves the observation entirely inside both locks. This setup blocks
    exactly that now-fenced observe call, so a test can prove a concurrent
    pause()/resume() cannot even begin its own native activity on this
    handle until it is released."""
    transfer = await submit(core)
    await core.engine.tick()
    artifact = (await core.repository.artifacts(transfer.id))[0]
    core.executor.finish(artifact.execution)
    await core.engine.tick()
    Path(artifact.target).unlink()

    entered = asyncio.Event()
    release = asyncio.Event()
    original_observe = core.executor.observe

    async def blocking_observe(handle):
        entered.set()
        await release.wait()
        return await original_observe(handle)

    core.executor.observe = blocking_observe
    return transfer, artifact, entered, release


async def _reacquisition_setup_blocked_at_finalize(core):
    """Variant of the setup above that blocks AFTER the artifact has already
    been rewritten to ``queued`` (the last repository call
    ``_reacquire_transfer`` makes before returning) rather than mid
    per-artifact mutation. This is the shape that actually exercises the
    scheduler race: an artifact still ``completed`` (as it is during the
    ``executor.cancel()`` window above) is never selected by
    ``reconcile_executions()`` in the first place, so only a block AFTER it
    becomes ``queued`` -- while still inside the method's own critical
    section -- can show whether a concurrent scheduler cycle could reach and
    dispatch it early."""
    transfer = await submit(core)
    await core.engine.tick()
    artifact = (await core.repository.artifacts(transfer.id))[0]
    core.executor.finish(artifact.execution)
    await core.engine.tick()
    Path(artifact.target).unlink()

    entered = asyncio.Event()
    release = asyncio.Event()
    original_retry_requests = core.repository.retry_requests

    async def blocking_retry_requests(transfer_id, **kwargs):
        entered.set()
        await release.wait()
        return await original_retry_requests(transfer_id, **kwargs)

    core.repository.retry_requests = blocking_retry_requests
    return transfer, artifact, entered, release


@pytest.mark.asyncio
async def test_reacquisition_serializes_against_scheduler_reconciliation(canonical_core):
    """DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
    closure, Gate 9 revision 7): a review correctly found that rev. 6's
    ``_reacquire_transfer`` published the transfer as ACCEPTED, then
    continued mutating its artifacts directly with ``executor.cancel()``/
    ``repository.execution()``/``artifact_state()``/``reset_retry_budget()``
    -- but ``reconcile_executions()`` only takes ``_execution_cycle_lock``,
    not the per-transfer lock ``_reacquire_transfer`` holds, so it could
    start a cycle, see the now-ACCEPTED transfer in ``repository.active()``,
    and observe/dispatch/mutate the very artifacts still being rewritten --
    up to and including an artifact this method has ALREADY rewritten to
    ``queued`` (eligible for the scheduler's own dispatch) while the method
    is still finishing its remaining bookkeeping. The fix wraps the whole
    publish-then-mutate phase in ``_execution_cycle_lock`` too -- the SAME
    lock ``reconcile_executions()`` holds for its entire cycle -- so the two
    provably cannot interleave: proven here by forcing ``_reacquire_transfer``
    to block at that exact late point and showing a concurrent
    ``reconcile_executions()`` call cannot even complete (let alone dispatch
    the artifact a second time) until it is released."""
    core = canonical_core
    transfer, artifact, entered, release = await _reacquisition_setup_blocked_at_finalize(core)

    reacquire_task = asyncio.create_task(submit(core))
    await asyncio.wait_for(entered.wait(), timeout=1)
    queued = (await core.repository.artifacts(transfer.id))[0]
    assert queued.state == "queued"

    reconcile_task = asyncio.create_task(core.engine.reconcile_executions())
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(reconcile_task), timeout=0.05)

    release.set()
    submitted = await reacquire_task
    await reconcile_task

    assert submitted.id == transfer.id
    repaired = (await core.repository.artifacts(transfer.id))[0]
    assert repaired.execution != artifact.execution
    # Reacquisition published ACCEPTED and reconcile_executions() -- now
    # unblocked -- was free to make ordinary forward progress from there
    # (e.g. dispatching the freshly-queued artifact); the transfer must
    # never have landed back in a terminal state.
    assert (await core.repository.get(transfer.id)).state not in {
        TransferState.COMPLETED, TransferState.DELETED, TransferState.CONSOLIDATED,
    }
    # Exactly one execution attempt exists for this artifact across the whole
    # sequence -- no double-dispatch from a reconcile cycle racing the
    # reacquisition's own replacement.
    assert len(await core.repository.executions(transfer.id)) <= 2


@pytest.mark.asyncio
async def test_reacquisition_serializes_against_concurrent_pause(canonical_core):
    """Companion to the reconciliation test above: a direct ``pause()`` call
    doesn't take ``_execution_cycle_lock``, but its per-artifact mutation
    goes through ``_converge_execution``, which acquires
    ``self._convergence_lock(handle.attempt_id)`` -- the SAME per-attempt
    lock ``_reacquire_transfer`` now holds while touching that exact
    artifact's stale execution handle. Whichever side wins proceeds
    cleanly; the loser's ``_converge_execution`` call safely detects the
    ownership/handle mismatch afterward (the handle it expected is gone)
    rather than double-mutating anything.

    DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
    closure, Gate 9 revision 9): a review found that proving the native
    execution calls don't overlap was not the same as proving the user's
    PAUSE INTENT survives the race. ``pause()`` sets the durable
    ``transfer_pause_intents`` row (``set_pause_and_fence``) immediately,
    unguarded by any lock, before it ever reaches the per-attempt lock this
    method also holds -- so by the time ``pause_task`` is blocked below,
    the intent is ALREADY durably recorded. Rev. 8's ``_reacquire_transfer``
    then unconditionally cleared it back to ``False`` right before
    returning, silently discarding the user's own pause request. That final
    clear is now removed entirely (terminal settlement already retires
    stale pause state for the ordinary case; the only case where it had any
    effect at all was this exact race). This test asserts the actual
    outcome that matters: the transfer is still durably paused afterward,
    not merely that ``pause()`` returned without raising or corrupting
    state."""
    core = canonical_core
    transfer, artifact, entered, release = await _reacquisition_setup_blocked_at_cancel(core)

    reacquire_task = asyncio.create_task(submit(core))
    await asyncio.wait_for(entered.wait(), timeout=1)

    pause_task = asyncio.create_task(core.engine.pause(transfer.id))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(pause_task), timeout=0.05)
    # The intent is durably recorded as soon as pause() starts, well before
    # it blocks trying to touch the (still reacquisition-owned) handle.
    assert (await core.repository.get(transfer.id)).paused

    release.set()
    submitted = await reacquire_task
    pause_errors = await pause_task

    assert submitted.id == transfer.id
    # pause() may report a benign ownership-conflict for the handle
    # reacquisition just retired underneath it -- never a crash, and never a
    # second, corrupting mutation of that handle.
    for error in pause_errors:
        assert error.category == Category.OWNERSHIP_CONFLICT
    repaired = (await core.repository.artifacts(transfer.id))[0]
    assert repaired.execution != artifact.execution
    assert len(await core.repository.artifacts(transfer.id)) == 1
    # The user's pause command must survive the race, not be silently
    # discarded by reacquisition's own bookkeeping.
    assert (await core.repository.get(transfer.id)).paused


@pytest.mark.asyncio
async def test_reacquisition_native_observe_cannot_overlap_concurrent_pause(canonical_core):
    """DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
    closure, Gate 9 revision 8): a review found that rev. 7's
    ``_reacquire_transfer`` still performed its FIRST ``executor.observe()``
    in the plan-building loop, before acquiring either
    ``_execution_cycle_lock`` or the per-attempt ``_convergence_lock`` --
    the exact case the rev. 7 pause regression above did not exercise,
    since it only blocked the LATER, already-fenced ``cancel()`` call. This
    test blocks the now-fenced ``observe()`` call directly and counts
    concurrent entries into it: a concurrent ``pause()`` racing the SAME
    handle must never be able to issue its OWN native ``observe()`` while
    this method's is still in flight -- proving native execution calls on
    one handle cannot overlap, not merely that the later mutation calls
    don't."""
    core = canonical_core
    transfer, artifact, entered, release = await _reacquisition_setup_blocked_at_observe(core)

    concurrent = 0
    max_concurrent = 0
    original_observe = core.executor.observe

    async def counting_observe(handle):
        nonlocal concurrent, max_concurrent
        concurrent += 1
        max_concurrent = max(max_concurrent, concurrent)
        try:
            return await original_observe(handle)
        finally:
            concurrent -= 1

    core.executor.observe = counting_observe

    reacquire_task = asyncio.create_task(submit(core))
    await asyncio.wait_for(entered.wait(), timeout=1)

    pause_task = asyncio.create_task(core.engine.pause(transfer.id))
    # Give pause() every real opportunity to reach its own native observe()
    # while reacquisition's is still blocked, before asserting it did not --
    # pause() crosses real (if fake) DB I/O first, so a bare cooperative
    # yield is not enough headroom to trust a negative result.
    await asyncio.sleep(0.05)
    assert max_concurrent == 1, "a concurrent pause() issued its own native observe() while reacquisition's was still in flight"

    release.set()
    submitted = await reacquire_task
    await pause_task

    assert submitted.id == transfer.id
    assert max_concurrent == 1


@pytest.mark.asyncio
async def test_reacquisition_serializes_against_concurrent_operator_retry(canonical_core):
    """Operator-initiated ``retry()`` (``reacquire=False``) and
    ``_reacquire_transfer`` (``reacquire=True``) are two branches of the
    SAME ``retry()`` method and share the SAME ``self._transfer_locks``
    entry -- proven here directly, not merely by code inspection: a
    concurrent operator retry on this exact transfer id cannot even begin
    its own body until the in-flight reacquisition fully releases the
    lock."""
    core = canonical_core
    transfer, artifact, entered, release = await _reacquisition_setup_blocked_at_cancel(core)

    reacquire_task = asyncio.create_task(submit(core))
    await asyncio.wait_for(entered.wait(), timeout=1)

    retry_task = asyncio.create_task(core.engine.retry(transfer.id))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(retry_task), timeout=0.05)

    release.set()
    submitted = await reacquire_task
    retry_ok = await retry_task

    assert submitted.id == transfer.id
    assert isinstance(retry_ok, bool)
    assert len(await core.repository.artifacts(transfer.id)) == 1


@pytest.mark.asyncio
async def test_reacquire_transfer_rejects_a_non_terminal_transfer(canonical_core):
    """DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
    closure, Gate 9 revision 7): a review correctly found rev. 6's
    precondition check rejected only ``CONSOLIDATED``, under-enforcing the
    documented contract ("a duplicate submission found already durably
    COMPLETED or DELETED"). The check must be authoritative inside the
    method itself, not merely assumed from the caller -- not merely from
    ``transition_allowed``'s own state-machine guard, which (unlike
    COMPLETED/DELETED) actually PERMITS an operator-authorized
    CANCELLED -> ACCEPTED transition, so a CANCELLED transfer is the one
    concrete case that would have slipped past rev. 6's CONSOLIDATED-only
    check and been silently, incorrectly reacquired."""
    core = canonical_core
    transfer = await submit(core)
    await core.engine.tick()
    before_transfer = await core.repository.get(transfer.id)
    assert await core.repository.state(
        transfer.id, TransferState.CANCELLED, operator=True, expected_epoch=before_transfer.epoch,
    )
    before_transfer = await core.repository.get(transfer.id)
    assert before_transfer.state == TransferState.CANCELLED
    before = await core.repository.artifacts(transfer.id)

    assert await core.engine._reacquire_transfer(transfer.id) is False

    after_transfer = await core.repository.get(transfer.id)
    after = await core.repository.artifacts(transfer.id)
    assert after_transfer.state == before_transfer.state == TransferState.CANCELLED
    assert [(item.id, item.state, item.execution) for item in before] == [
        (item.id, item.state, item.execution) for item in after
    ]


@pytest.mark.asyncio
async def test_unknown_cleanup_failure_is_retained_without_retry_storm(core):
    cleanup_error = NormalizedError(
        Domain.CLEANUP, Category.REMOTE_CLEANUP_FAILED, Stage.CLEANUP,
        retryability=Retryability.UNKNOWN,
    )
    core.provider.cleanup_response = TransferOutcome(OutcomeKind.FAILURE, cleanup_error)
    result = core.provider.parcel()
    core.provider.responses = [result]
    transfer = await submit(core)
    await core.engine.tick()
    await core.engine.delete(transfer.id)
    for _ in range(5):
        core.now[0] += 1000
        await core.engine.tick()
    assert len([item for item in core.provider.calls if item[0] == "cleanup"]) == 1
    resources = await core.repository.resources(transfer.id)
    assert resources[0][2] == CleanupAuthority.USER_REQUEST


@pytest.mark.asyncio
async def test_observed_inventory_resource_is_not_resubmitted_or_owned(core):
    # The observed root and its manifest member carry distinct provider
    # payloads, so a provider resolve of the ROOT (a resubmission of the
    # already-observed resource as a new source) is distinguishable from the
    # member's own legitimate resolution.
    result = core.provider.parcel(state=ResourceState.AVAILABLE, ownership=Ownership.OBSERVED,
                                  files=(("payload.bin", "folder/payload.bin", 4),))
    core.provider.inventory_items = (result.observation,)
    await core.engine.reconcile_inventory()
    await core.engine.tick()
    transfer = (await core.repository.active())[0]
    records = await core.repository.requests(transfer.id)
    root = next(item for item in records if item.parent_id is None)
    members = [item for item in records if item.parent_id == root.id]
    assert root.request.payload == "parcel" and [item.request.payload for item in members] == ["parcel:folder/payload.bin"]

    # The root is never resubmitted: no provider resolve, no durable
    # resolution attempt. Its member IS this cycle's work -- resolved exactly
    # once and materialized without waiting for a later cycle.
    resolved = [item[1] for item in core.provider.calls if item[0] == "resolve"]
    assert root.request.payload not in resolved
    async with database.get_db() as db:
        attempted = {row["request_id"] for row in await db.fetchall("SELECT request_id FROM resolution_attempts")}
    assert root.id not in attempted and root.state == "resolved"
    assert resolved == [item.request.payload for item in members]
    assert [item.state for item in members] == ["resolved"]
    assert [item.request_id for item in await core.repository.artifacts(transfer.id)] == [members[0].id]

    # Member resolution promoted nothing: the one resource is still OBSERVED,
    # so cleanup neither claims it nor contacts the provider.
    resources = await core.repository.resources(transfer.id)
    assert [(item.id, item.ownership) for item, _state, _pending in resources] == [
        (result.observation.resource.id, Ownership.OBSERVED)]
    await core.engine._cleanup_resources(transfer.id)
    assert not [item for item in core.provider.calls if item[0] == "cleanup"]
    assert not any(pending for _resource, _state, pending in await core.repository.resources(transfer.id))


@pytest.mark.asyncio
async def test_empty_incomplete_inventory_does_not_delete_known_resource(core):
    result = core.provider.parcel()
    core.provider.responses = [result]
    transfer = await submit(core)
    await core.engine.tick()
    await core.engine.reconcile_inventory()
    resource, state, _ = (await core.repository.resources(transfer.id))[0]
    assert state == ResourceState.PREPARING
    assert resource == result.observation.resource


@pytest.mark.asyncio
async def test_prepared_attempt_survives_crash_before_external_contact(canonical_core):
    # DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
    # closure): the orphaned-execution reconciliation this test drives calls
    # _recover_artifact with a candidate-bearing, CORE-origin error -- a case
    # _engine_base.TransferEngine now explicitly refuses to handle itself
    # (see its _recover_artifact docstring), by design, since that authority
    # lives exclusively in the canonical stack now.
    core = canonical_core
    transfer = await submit(core)
    record = (await core.repository.requests(transfer.id))[0]
    await core.engine._resolve(record)
    artifact = (await core.repository.artifacts(transfer.id))[0]
    request = ExecutionRequest(core.engine._work(artifact, artifact.candidates[0]), "crash-before-start")
    handle = core.executor.prepare(request)
    assert await core.repository.prepare_execution(artifact, handle)
    await core.engine.tick()
    core.now[0] += 2
    await core.engine.tick()
    attempts = await core.repository.executions(transfer.id)
    assert len(attempts) == 2
    assert attempts[0].state == ExecutionState.ABSENT
    assert len([item for item in core.executor.calls if item[0] == "start"]) == 1


@pytest.mark.asyncio
async def test_executor_uncertainty_reserves_slot_without_creating_replacement(canonical_core):
    # DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
    # closure, Gate 9 revision 5): operator-initiated retry (reacquire=False,
    # the default) is now exclusively a canonical-stack responsibility.
    core = canonical_core
    transfer = await submit(core)
    await core.engine.tick()
    artifact = (await core.repository.artifacts(transfer.id))[0]
    core.executor.jobs[artifact.execution.attempt_id] = ExecutionObservation(artifact.execution, ExecutionState.UNKNOWN,
        error=failure(Category.UNMAPPED_EXECUTOR_ERROR, domain=Domain.EXECUTOR))
    for _ in range(4):
        core.now[0] += 1000
        await core.engine.tick()
    assert len(await core.repository.executions(transfer.id)) == 1
    before = (await core.repository.artifacts(transfer.id))[0]
    assert before.state == "unknown"
    # The canonical stack's operator retry (transfers.convergence_engine
    # .TransferEngine.retry) treats an "unknown" artifact as not
    # actionable (transfers.recovery_execution/_retry_actionable does not
    # include "unknown") and trivially succeeds WITHOUT touching it --
    # correct and safe: unlike the retired base-only retry's blanket
    # `observation.state == UNKNOWN -> return False`, it never creates a
    # replacement execution nor mutates the artifact while its true state
    # remains genuinely uncertain.
    assert await core.engine.retry(transfer.id)
    after = (await core.repository.artifacts(transfer.id))[0]
    assert after.state == "unknown"
    assert after.execution == before.execution
    assert len(await core.repository.executions(transfer.id)) == 1


@pytest.mark.asyncio
async def test_postprocessing_failure_is_recorded_separately_from_delivery(core):
    class Processor:
        descriptor = SimpleNamespace(id="unpacker")
        async def process(self, transfer_id, paths):
            assert Path(paths[0]).read_bytes() == b"done"
            return TransferOutcome(OutcomeKind.FAILURE, NormalizedError(
                Domain.POST_PROCESSING, Category.EXTRACTION_FAILED, Stage.POST_PROCESSING,
                Retryability.NEVER, Recovery.REQUIRE_OPERATOR))
    core.engine.postprocessors = (Processor(),)
    transfer = await submit(core)
    await core.engine.tick()
    artifact = (await core.repository.artifacts(transfer.id))[0]
    core.executor.finish(artifact.execution)
    await core.engine.tick()
    assert (await core.repository.get(transfer.id)).state == TransferState.COMPLETED
    async with database.get_db() as db:
        outcomes = await db.fetchall("SELECT payload FROM transfer_outcomes WHERE transfer_id=?", (transfer.id,))
    assert any("extraction_failed" in row["payload"] for row in outcomes)
    assert len([item for item in core.provider.calls if item[0] == "resolve"]) == 1


@pytest.mark.asyncio
async def test_provider_retry_stays_bound_to_original_route(core):
    error = failure(Category.PROVIDER_UNAVAILABLE, retryability=Retryability.BACKOFF, recovery=Recovery.BACKOFF)
    core.provider.responses = [ResolutionResult(ResourceState.UNKNOWN, error=error)]
    transfer = await submit(core)
    await core.engine.tick()
    alternate = ParcelProvider("other-parcel")
    core.registry.register_provider(alternate)
    core.registry.mark_health(core.provider.descriptor.id, healthy=False)
    core.now[0] += 1
    await core.engine.tick()
    async with database.get_db() as db:
        attempts = await db.fetchall("SELECT provider_id FROM resolution_attempts ORDER BY rowid")
    assert [row["provider_id"] for row in attempts] == ["parcel-lab"]
    assert not [call for call in alternate.calls if call[0] == "resolve"]
    details = await core.repository.presentation(transfer.id, details=True)
    assert {item["provider_id"] for item in details["route_attempts"]} == {"parcel-lab"}
    assert len(await core.repository.active()) == 1


@pytest.mark.asyncio
async def test_executor_can_change_on_retry_without_recreating_artifact(canonical_core):
    # DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
    # closure, Gate 9 revision 5): operator-initiated retry (reacquire=False,
    # the default) is now exclusively a canonical-stack responsibility.
    core = canonical_core
    core.executor.start_errors = [failure(Category.UNMAPPED_EXECUTOR_ERROR, domain=Domain.EXECUTOR)]
    transfer = await submit(core)
    await core.engine.tick()
    before = (await core.repository.artifacts(transfer.id))[0]
    alternate = MemoryExecutor(core.repository.authorize_execution)
    alternate.descriptor = replace(alternate.descriptor, id="other-copy", priority=10)
    core.registry.register_executor(alternate)
    assert await core.engine.retry(transfer.id)
    await core.engine.tick()
    after = (await core.repository.artifacts(transfer.id))[0]
    assert (after.id, after.target) == (before.id, before.target)
    assert after.execution.executor_id == "other-copy"
    assert len(await core.repository.executions(transfer.id)) == 2


@pytest.mark.asyncio
async def test_routing_preference_and_display_name_do_not_change_source_identity(core):
    first = await core.engine.submit((TransferRequest("parcel", "same-input", name="one", preferred_provider="first"),))
    second = await core.engine.submit((TransferRequest("parcel", "same-input", name="two", preferred_provider="second"),))
    assert first.id == second.id
    independent = await core.engine.submit((TransferRequest("parcel", "same-input"),), deduplicate=False)
    assert independent.id != first.id


@pytest.mark.asyncio
async def test_resolved_candidate_survives_crash_before_file_planning(core, monkeypatch):
    transfer = await submit(core)
    record = (await core.repository.requests(transfer.id))[0]
    materialize = core.repository.materialize
    async def interrupted(*args, **kwargs):
        raise asyncio.CancelledError()
    monkeypatch.setattr(core.repository, "materialize", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await core.engine._resolve(record)
    assert (await core.repository.requests(transfer.id))[0].state == "materializing"
    monkeypatch.setattr(core.repository, "materialize", materialize)
    await core.engine.tick()
    assert (await core.repository.artifacts(transfer.id))[0].execution
    assert len([item for item in core.provider.calls if item[0] == "resolve"]) == 1


@pytest.mark.asyncio
async def test_slow_provider_does_not_block_existing_execution_updates(core):
    first = await submit(core, "first")
    await core.engine.tick()
    handle = (await core.repository.artifacts(first.id))[0].execution
    await submit(core, "slow", "slow.bin")
    core.provider.entered, core.provider.release = asyncio.Event(), asyncio.Event()
    resolving = asyncio.create_task(core.engine.resolve_pending())
    await core.provider.entered.wait()
    core.executor.finish(handle)
    try:
        await asyncio.wait_for(core.engine.reconcile_executions(), timeout=2)
        assert (await core.repository.get(first.id)).state == TransferState.COMPLETED
    finally:
        core.provider.release.set()
        await resolving


@pytest.mark.asyncio
async def test_mirrors_share_one_artifact_and_failover_retires_partial_bytes(canonical_core):
    # DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
    # closure): this drives a full failure/backoff/refresh/candidate-switch
    # sequence, which is now exclusively a canonical-stack behavior.
    core = canonical_core
    first = replace(core.provider.candidate("same.bin"), source_identity=SourceIdentity("host", "one"))
    second = replace(core.provider.candidate("same.bin"), source_identity=SourceIdentity("host", "two"))
    core.provider.responses = [ResolutionResult(ResourceState.AVAILABLE, (first,)), ResolutionResult(ResourceState.AVAILABLE, (second,))]
    transfer = await core.engine.submit((TransferRequest("parcel", "one"), TransferRequest("parcel", "two")))
    await core.engine.tick()
    artifacts = await core.repository.artifacts(transfer.id)
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert len(artifact.candidates) == 2
    assert len([item for item in core.executor.calls if item[0] == "start"]) == 1
    # Candidate identities and their bound order are not a product contract:
    # both mirrors are equivalent and their UUIDs are random. Derive the
    # actually-selected candidate and its one alternate by ID from the
    # converged artifact instead of assuming ``first`` is index 0.
    bound_ids = [candidate.id for candidate in artifact.candidates]
    assert set(bound_ids) == {first.id, second.id} and len(set(bound_ids)) == 2
    active_index = artifact.selected
    active_id = bound_ids[active_index]
    (alternate_id,) = set(bound_ids) - {active_id}
    assert alternate_id != active_id
    target = Path(artifact.target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"part")
    sidecar = Path(core.executor.sidecar(artifact.target))
    sidecar.write_bytes(b"resume")
    error = NormalizedError(Domain.NETWORK, Category.REMOTE_READ_FAILED, Stage.EXECUTION,
                            Retryability.BACKOFF, Recovery.TRY_ALTERNATE_CANDIDATE)

    core.executor.jobs[artifact.execution.attempt_id] = replace(
        core.executor.jobs[artifact.execution.attempt_id], state=ExecutionState.FAILED, error=error,
    )
    await core.engine.tick()
    first_retry = (await core.repository.artifacts(transfer.id))[0]
    assert first_retry.selected == active_index and first_retry.state == "recovery_wait"
    assert first_retry.candidates[first_retry.selected].id == active_id
    assert target.exists() and sidecar.exists()

    # DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
    # closure): the canonical stack's wake+decide+apply sequence converges
    # this second failure and its refresh within a single tick, so
    # "refresh_pending" is no longer an externally observable resting state
    # at this granularity (see the same note in
    # test_pause_resume_recovery.py).
    core.executor.start_errors = [error]
    core.now[0] += 1
    await core.engine.tick()
    refreshed = (await core.repository.artifacts(transfer.id))[0]
    assert refreshed.selected == active_index and refreshed.state == "queued"
    assert refreshed.candidates[refreshed.selected].id == active_id
    assert target.exists() and sidecar.exists()
    core.executor.start_errors = [error]
    await core.engine.tick()
    switched = (await core.repository.artifacts(transfer.id))[0]
    assert switched.id == artifact.id and switched.target == artifact.target
    assert switched.execution is None
    assert [candidate.id for candidate in switched.candidates] == bound_ids
    assert switched.selected != active_index
    assert switched.candidates[switched.selected].id == alternate_id
    # DP 1.0.12 recovery leveling, Section 28: automatic and operator
    # candidate activation now share ONE partial/resume policy
    # (transfers.candidate_activation.activate_candidate) -- partial bytes
    # are retired only when the old and new candidates do NOT share the same
    # executor and resumable-sidecar contract. Both mirrors here resolve
    # through the same core.executor against the same local target, so the
    # partial file is correctly REUSED, not discarded.
    assert target.exists() and sidecar.exists()

    await core.engine.tick()
    retried = (await core.repository.artifacts(transfer.id))[0]
    assert retried.selected == switched.selected and retried.execution is not None
    assert retried.id == artifact.id and retried.target == artifact.target
    attempts = await core.repository.executions(transfer.id)
    assert len(attempts) == 4
    assert {item.candidate.id for item in attempts} == {active_id, alternate_id}
    # Every attempt before the failover ran on the originally active
    # candidate; the final execution is the actual alternate candidate.
    assert [item.candidate.id for item in attempts] == [active_id] * 3 + [alternate_id]
    assert retried.execution.attempt_id == attempts[-1].handle.attempt_id


@pytest.mark.asyncio
async def test_same_source_scope_key_does_not_collapse_distinct_inputs(core):
    candidate = replace(core.provider.candidate("same.bin"), source_identity=SourceIdentity("host", "same-origin"))
    core.provider.responses = [ResolutionResult(ResourceState.AVAILABLE, (candidate,))] * 2
    transfer = await core.engine.submit((TransferRequest("parcel", "one"), TransferRequest("parcel", "two")))
    await core.engine.tick()
    artifacts = await core.repository.artifacts(transfer.id)
    assert len(artifacts) == 2
    assert artifacts[0].target != artifacts[1].target


@pytest.mark.asyncio
async def test_near_size_mirrors_require_sampling_before_consolidation(core):
    from unittest.mock import AsyncMock
    first = replace(core.provider.candidate("same.bin"), expected_bytes=1000, source_identity=SourceIdentity("host", "one"))
    second = replace(core.provider.candidate("same.bin"), expected_bytes=1001, source_identity=SourceIdentity("host", "two"))
    core.provider.responses = [ResolutionResult(ResourceState.AVAILABLE, (first,)), ResolutionResult(ResourceState.AVAILABLE, (second,))]
    core.executor.fingerprint = AsyncMock(return_value=ArtifactFingerprint(1000, "same-sampled-bytes"))
    transfer = await core.engine.submit((TransferRequest("parcel", "one"), TransferRequest("parcel", "two")))
    await core.engine.tick()
    artifacts = await core.repository.artifacts(transfer.id)
    assert len(artifacts) == 1
    assert len(artifacts[0].candidates) == 2
    assert len([item for item in core.executor.calls if item[0] == "start"]) == 1
    # DP 1.0.12 CANON-001 follow-up (cohorts.py bootstrap admission barrier):
    # the first-materializing mirror now takes one extra self-evidence
    # fingerprint call before seeding the empty-canonical cohort, on top of
    # the two calls the second mirror's ordinary pairwise mapping already
    # made against it -- sampling still happens for both sides either way.
    assert core.executor.fingerprint.await_count == 3


@pytest.mark.asyncio
async def test_file_selection_before_dispatch_excludes_blocked_artifact(core):
    transfer = await submit(core)
    await core.engine.resolve_pending()
    artifact = (await core.repository.artifacts(transfer.id))[0]
    await core.engine.select_artifact(transfer.id, artifact.id, selected=False)
    await core.engine.reconcile_executions()
    assert not core.executor.calls
    assert (await core.repository.get(transfer.id)).state == TransferState.COMPLETED
    await core.engine.select_artifact(transfer.id, artifact.id, selected=True)
    await core.engine.tick()
    assert (await core.repository.artifacts(transfer.id))[0].execution


@pytest.mark.asyncio
async def test_file_selection_cannot_change_after_execution_was_created(core):
    from transfers.errors import TransferError
    transfer = await submit(core)
    await core.engine.tick()
    artifact = (await core.repository.artifacts(transfer.id))[0]
    with pytest.raises(TransferError) as rejected:
        await core.engine.select_artifact(transfer.id, artifact.id, selected=False)
    assert rejected.value.error.category == Category.RESOURCE_STATE_CONFLICT


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [3, 5])
async def test_multiple_mirrors_keep_one_physical_artifact_and_do_not_cycle_on_local_failure(canonical_core, count):
    # DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
    # closure): a candidate-bearing, non-REMOTE_SOURCE-origin failure (this
    # DISK_FULL/LOCAL_RESOURCE case) now must go through the canonical
    # recovery-decision owner -- _engine_base.TransferEngine explicitly
    # refuses to decide that case itself.
    core = canonical_core
    candidates = [replace(core.provider.candidate("same.bin"), source_identity=SourceIdentity("host", str(i))) for i in range(count)]
    core.provider.responses = [ResolutionResult(ResourceState.AVAILABLE, (item,)) for item in candidates]
    core.executor.start_errors = [NormalizedError(Domain.LOCAL_RESOURCE, Category.DISK_FULL, Stage.EXECUTION,
        Retryability.AFTER_RESOURCE_CHANGE, Recovery.REQUIRE_OPERATOR)]
    transfer = await core.engine.submit(tuple(TransferRequest("parcel", str(i)) for i in range(count)))
    await core.engine.tick()
    await core.engine.tick()
    artifact, = await core.repository.artifacts(transfer.id)
    assert len(artifact.candidates) == count
    assert artifact.selected == 0
    assert len([call for call in core.executor.calls if call[0] == "start"]) == 1
    assert artifact.error.category == Category.DISK_FULL


@pytest.mark.asyncio
@pytest.mark.parametrize("retries, delay", [(0, 0), (2, 0), (2, 10)])
async def test_resolution_retry_budget_and_zero_delay_drive_actual_attempts(core, retries, delay):
    core.engine.policy = replace(core.engine.policy, resolution_max_attempts=retries + 1, resolution_retry_delay=delay)
    error = failure(Category.PROVIDER_UNAVAILABLE, retryability=Retryability.BACKOFF, recovery=Recovery.RETRY)
    core.provider.responses = [ResolutionResult(ResourceState.UNAVAILABLE, error=error)] * (retries + 1)
    await submit(core)
    await core.engine.resolve_pending()
    await core.engine.resolve_pending()
    assert len(core.provider.calls) == (2 if retries and not delay else 1)
    for _ in range(5):
        core.now[0] += 1000
        await core.engine.resolve_pending()
    assert len(core.provider.calls) == retries + 1


@pytest.mark.parametrize("left_size,right_size,expected", [
    # DP 1.0.12: both sizes unknown (0) is no longer a cheap pairing
    # rejection -- unknown is "unknown, not proof of difference"; the pair
    # must reach bounded content evidence instead (transfers/mirrors.py
    # pairing_failure). This case now exercises pairability, not a reported
    # -size boundary.
    (0, 0, True),
    (1000, 1000, True),
    (1000, 1001, True),
    (1000, 1002, False),
    (1024**4, 1024**4, True),
    (1024**4, 1024**4 + 512 * 1024**2, True),
    (1024**4, 1024**4 + 512 * 1024**2 + 1, False),
])
def test_mirror_size_boundaries_are_conservative(left_size, right_size, expected):
    from transfers.mirrors import comparable
    provider = ParcelProvider()
    left = replace(provider.candidate("Same.bin"), expected_bytes=left_size, source_identity=SourceIdentity("host", "one"))
    right = replace(provider.candidate("same.bin"), expected_bytes=right_size, source_identity=SourceIdentity("host", "two"))
    assert comparable(left, right) is expected
