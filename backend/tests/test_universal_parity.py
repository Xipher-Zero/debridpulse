"""Behavioral scenarios migrated from the retired manager and control layers."""
import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from test_universal_lifecycle import core, submit, failure
from transfers.errors import Category, Domain, Retryability, Recovery
from transfers.models import (
    SourceEntry, TransferRequest, ResolutionResult, ResourceState, ExecutionState,
    IntegrationDescriptor, TransferOutcome, OutcomeKind,
)


@pytest.mark.asyncio
async def test_resume_all_obeys_capacity_and_releases_parked_successors(core):
    core.engine.policy = replace(core.engine.policy, max_active_executions=3)
    parents = [await submit(core, str(index), f"{index}.bin") for index in range(3)]
    await core.engine.tick()
    await core.engine.pause_all()
    core.engine.policy = replace(core.engine.policy, max_active_executions=1)
    await core.engine.resume_all()
    jobs = list(core.executor.jobs.values())
    assert sum(job.occupies_slot for job in jobs) == 1
    active = next(job for job in jobs if job.occupies_slot)
    core.executor.finish(active.handle)
    await core.engine.tick()
    assert sum(job.occupies_slot for job in core.executor.jobs.values()) == 1
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
async def test_re_resolution_waits_for_configured_deadline(core):
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
@pytest.mark.parametrize("missing_member", [False, True])
async def test_expired_resource_re_resolution_preserves_completed_sibling_and_paths(core, missing_member):
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
    error = failure(Category.CANDIDATE_EXPIRED, retryability=Retryability.AFTER_RERESOLUTION, recovery=Recovery.RERESOLVE, domain=Domain.EXECUTOR)
    core.executor.jobs[second.execution.attempt_id] = replace(core.executor.jobs[second.execution.attempt_id], state=ExecutionState.FAILED, error=error)
    core.provider.resources[initial.observation.resource.id] = replace(initial.observation, state=ResourceState.EXPIRED)
    async def expired(_candidate):
        return ResolutionResult(ResourceState.EXPIRED, error=failure(Category.RESOURCE_EXPIRED, retryability=Retryability.AFTER_RERESOLUTION, recovery=Recovery.RERESOLVE))
    core.provider.refresh = expired
    await core.engine.tick()
    core.now[0] += 1
    await core.engine.tick()
    renewed = core.provider.parcel("renewed", state=ResourceState.AVAILABLE)
    core.provider.members[renewed.observation.resource.id] = tuple(replace(entry, request=replace(entry.request, payload="new-" + str(entry.request.payload))) for entry in (entries[:1] if missing_member else entries))
    core.provider.responses = [renewed]
    core.now[0] += 1
    await core.engine.tick()
    await core.engine.tick()
    latest = sorted(await core.repository.artifacts(transfer.id), key=lambda item: item.name)
    assert latest[0].id == first.id and latest[0].state == "completed"
    assert latest[0].execution == first.execution
    assert latest[1].id == second.id and latest[1].target == second.target
    if missing_member:
        assert latest[1].state == "error"
        assert latest[1].error.category == Category.SOURCE_NOT_FOUND
        assert not any(item.state == "waiting_parent" for item in await core.repository.requests(transfer.id))
        return
    assert latest[1].execution != second.execution
    assert any(operation == "resolve" and value == "new-second" for operation, value in core.provider.calls)


@pytest.mark.asyncio
async def test_manual_retry_opens_new_budget_without_erasing_attempt_history(core):
    core.engine.policy = replace(core.engine.policy, max_attempts=1)
    core.executor.start_errors = [failure(Category.REMOTE_RESET, retryability=Retryability.BACKOFF, recovery=Recovery.RETRY, domain=Domain.NETWORK)]
    transfer = await submit(core)
    await core.engine.tick()
    assert (await core.repository.get(transfer.id)).state == "error"
    assert await core.engine.retry(transfer.id)
    await core.engine.tick()
    assert len(await core.repository.executions(transfer.id)) == 2
    assert (await core.repository.artifacts(transfer.id))[0].retries == 1


@pytest.mark.asyncio
async def test_reacquisition_schedules_postprocessor_again(core):
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
