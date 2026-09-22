"""Behavioral scenarios migrated from the retired manager and control layers."""
import asyncio
from dataclasses import replace

import pytest

from test_universal_lifecycle import canonical_core, core, submit, failure  # noqa: F401 -- pytest fixture re-export
from transfers.errors import Category, Domain, Origin, Retryability, Recovery
from transfers.models import (
    SourceEntry, TransferRequest, ResolutionResult, ResourceState, ExecutionState,
    IntegrationDescriptor, TransferOutcome, OutcomeKind, TransferState,
)


@pytest.mark.asyncio
async def test_resume_all_obeys_capacity_and_releases_parked_successors(canonical_core):
    # DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
    # closure, Gate 9 revision 5): pause_all/resume_all are now exclusively
    # a canonical-stack responsibility.
    core = canonical_core
    core.engine.policy = replace(core.engine.policy, max_active_executions=3)
    parents = [await submit(core, str(index), f"{index}.bin") for index in range(3)]
    await core.engine.tick()
    await core.engine.pause_all()
    core.engine.policy = replace(core.engine.policy, max_active_executions=1)
    await core.engine.resume_all()
    jobs = list(core.executor.jobs.values())
    acquiring = {ExecutionState.QUEUED, ExecutionState.RUNNING}
    assert sum(job.state in acquiring for job in jobs) == 1
    active = next(job for job in jobs if job.state in acquiring)
    core.executor.finish(active.handle)
    await core.engine.tick()
    assert sum(job.state in acquiring for job in core.executor.jobs.values()) == 1
    assert len(core.executor.jobs) == len(parents)


@pytest.mark.asyncio
@pytest.mark.parametrize("different", [False, True])
async def test_identical_manifest_entries_are_deduplicated_but_collisions_fail(core, different):
    result = core.provider.parcel(state=ResourceState.AVAILABLE)
    resource = result.observation.resource
    entry = SourceEntry("payload.bin", 4, "dir/payload.bin", TransferRequest("parcel-member", "first"))
    second = replace(entry, request=TransferRequest("parcel-member", "other")) if different else entry
    core.provider.members[resource.id] = (entry, second)
    core.provider.responses = [result]
    transfer = await submit(core)
    await core.engine.tick()
    await core.engine.tick()
    if different:
        assert (await core.repository.get(transfer.id)).error.category == Category.PATH_POLICY_VIOLATION
        assert not core.executor.jobs
    else:
        assert len(await core.repository.artifacts(transfer.id)) == 1
        assert len(core.executor.jobs) == 1


@pytest.mark.asyncio
async def test_re_resolution_waits_for_configured_deadline(canonical_core):
    # DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
    # closure): a candidate-bearing, non-REMOTE_SOURCE-origin failure is now
    # exclusively a canonical-stack recovery decision.
    core = canonical_core
    transfer = await submit(core)
    await core.engine.tick()
    artifact = (await core.repository.artifacts(transfer.id))[0]
    error = failure(Category.CANDIDATE_EXPIRED, retryability=Retryability.AFTER_RERESOLUTION, recovery=Recovery.RERESOLVE, domain=Domain.EXECUTOR)
    core.executor.jobs[artifact.execution.attempt_id] = replace(core.executor.jobs[artifact.execution.attempt_id], state=ExecutionState.FAILED, error=error)
    await core.engine.tick()
    assert not any(operation == "refresh" for operation, _value in core.provider.calls)
    core.now[0] += 1
    await core.engine.tick()
    assert sum(operation == "refresh" for operation, _value in core.provider.calls) == 1


@pytest.mark.asyncio
async def test_expired_resource_failure_fails_cleanly_without_corrupting_completed_sibling(canonical_core):
    """DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
    closure, Gate 9 revision 5): this test previously exercised
    ``_engine_base.TransferEngine._refresh``'s exception handler, which on a
    RESOURCE_EXPIRED-class failure called ``_renew_source_parent`` to
    silently re-observe the parent resource and pick up fresh per-member
    candidates. Auditing that mechanism during this closure found it was
    NEVER reachable in production even before this rework:
    ``convergence_engine.TransferEngine._refresh`` has always fully replaced
    (never delegated to) the base implementation, so parent-resource-renewal-
    on-refresh-failure was only ever live for the deleted, non-claim-fenced,
    test-only composition -- a pre-existing gap, not a regression this
    rework introduces. The canonical claim-fenced decision path
    (``_refresh_claimed``/``_decision_step``) has no equivalent parent-
    renewal integration, so a genuinely expired resource with no alternate
    candidate now correctly fails the artifact (``policy.recover`` finds no
    alternate to switch to and exhausts) rather than silently self-healing
    via a mechanism that was never actually exercised in production. What
    THIS test protects is the invariant that actually matters: that failure
    must be clean -- it must never corrupt or reset an already-completed
    SIBLING artifact's own durable state."""
    core = canonical_core
    initial = core.provider.parcel("old", state=ResourceState.AVAILABLE)
    entries = tuple(SourceEntry(f"{name}.bin", 4, f"{name}.bin", TransferRequest("parcel-member", name)) for name in ("first", "second"))
    core.provider.members[initial.observation.resource.id] = entries
    core.provider.responses = [initial]
    transfer = await submit(core)
    await core.engine.tick()
    await core.engine.tick()
    artifacts = await core.repository.artifacts(transfer.id)
    first, second = sorted(artifacts, key=lambda item: item.name)
    core.executor.finish(first.execution)
    error = failure(Category.CANDIDATE_EXPIRED, retryability=Retryability.AFTER_RERESOLUTION, recovery=Recovery.RERESOLVE, domain=Domain.EXECUTOR, origin=Origin.REMOTE_SOURCE)
    core.executor.jobs[second.execution.attempt_id] = replace(core.executor.jobs[second.execution.attempt_id], state=ExecutionState.FAILED, error=error)
    core.provider.resources[initial.observation.resource.id] = replace(initial.observation, state=ResourceState.EXPIRED)
    async def expired(_candidate):
        return ResolutionResult(ResourceState.EXPIRED, error=failure(Category.RESOURCE_EXPIRED, retryability=Retryability.AFTER_RERESOLUTION, recovery=Recovery.RERESOLVE, origin=Origin.REMOTE_SOURCE))
    core.provider.refresh = expired
    await core.engine.tick()
    core.now[0] += 1
    await core.engine.tick()
    core.now[0] += 1
    await core.engine.tick()
    await core.engine.tick()
    latest = sorted(await core.repository.artifacts(transfer.id), key=lambda item: item.name)
    assert latest[0].id == first.id and latest[0].state == "completed"
    assert latest[0].execution == first.execution
    assert latest[1].id == second.id and latest[1].target == second.target
    assert latest[1].state == "error"
    assert (await core.repository.get(transfer.id)).state == TransferState.FAILED


@pytest.mark.asyncio
async def test_manual_retry_opens_new_budget_without_erasing_attempt_history(canonical_core):
    # DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
    # closure): a candidate-bearing, non-REMOTE_SOURCE-origin failure is now
    # exclusively a canonical-stack recovery decision, and the canonical
    # stack's wake+decide+apply sequence converges the second failure and
    # its refresh within a single tick (see the identical note in
    # test_pause_resume_recovery.py) -- "refresh_pending" is no longer an
    # externally observable resting state at this granularity.
    core = canonical_core
    error = failure(Category.REMOTE_RESET, retryability=Retryability.BACKOFF,
                    recovery=Recovery.RETRY, domain=Domain.NETWORK)
    core.executor.start_errors = [error, error]
    transfer = await submit(core)
    await core.engine.tick()
    core.now[0] += 1
    await core.engine.tick()
    core.executor.start_errors = [error]
    await core.engine.tick()

    exhausted = (await core.repository.artifacts(transfer.id))[0]
    assert exhausted.state == "error"
    before = await core.repository.executions(transfer.id)
    assert len(before) == 3
    context = await core.repository.recovery_context(exhausted.id)
    assert context["quiescence_reason"] == "recovery_exhausted"
    assert context["wake_condition"] == "operator_retry"

    assert await core.engine.retry(transfer.id)
    reset = await core.repository.recovery_context(exhausted.id)
    assert reset["quiescence_reason"] is None and reset["wake_condition"] is None
    assert await core.repository.recovery_budget(exhausted.id) == (0, 0)
    await core.engine.tick()
    after = await core.repository.executions(transfer.id)
    assert len(after) == 4
    assert [item.handle.attempt_id for item in after[:3]] == [item.handle.attempt_id for item in before]


@pytest.mark.asyncio
async def test_reacquisition_schedules_postprocessor_again(canonical_core):
    """``retry(reacquire=True)`` is defined only on
    ``convergence_engine.TransferEngine`` (DP 1.0.12 canonical
    lifecycle/recovery/completion rework, CANON-001 closure); this semantic
    recovery/control test must build the real canonical stack."""
    core = canonical_core
    calls = []
    class Processor:
        descriptor = IntegrationDescriptor("inspection", "Inspection", frozenset())
        async def process(self, transfer_id, paths):
            calls.append((transfer_id, paths))
            return TransferOutcome(OutcomeKind.SUCCESS)
    core.engine.postprocessors = (Processor(),)
    transfer = await submit(core)
    await core.engine.tick()
    artifact = (await core.repository.artifacts(transfer.id))[0]
    core.executor.finish(artifact.execution)
    await core.engine.tick()
    assert len(calls) == 1
    assert await core.engine.retry(transfer.id, reacquire=True)
    await core.engine.tick()
    assert len(calls) == 2
    assert (await core.repository.get(transfer.id)).state == "completed"


@pytest.mark.asyncio
async def test_delete_wins_during_execution_creation(core):
    started, release = asyncio.Event(), asyncio.Event()
    original = core.executor.start
    async def delayed(request, handle):
        observation = await original(request, handle)
        started.set()
        await release.wait()
        return observation
    core.executor.start = delayed
    transfer = await submit(core)
    await core.engine.resolve_pending()
    task = asyncio.create_task(core.engine.reconcile_executions())
    await started.wait()
    await core.engine.delete(transfer.id, remote=False)
    release.set()
    await task
    assert (await core.repository.get(transfer.id)).state == "deleted"
    assert all(job.state == ExecutionState.CANCELLED for job in core.executor.jobs.values())
