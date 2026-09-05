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
from transfers.errors import Category, Domain, NormalizedError, Recovery, Retryability, Stage
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


async def submit(core, payload="box", name="payload.bin"):
    return await core.engine.submit((TransferRequest("parcel", payload, name=name),))


def failure(category=Category.UNMAPPED_PROVIDER_ERROR, *, retryability=Retryability.UNKNOWN, recovery=Recovery.REQUIRE_OPERATOR, domain=Domain.PROVIDER):
    return NormalizedError(domain, category, Stage.RESOLUTION, retryability, recovery)


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
async def test_pause_accepts_requests_without_contact_and_resume_one_preserves_siblings(core):
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
@pytest.mark.parametrize("error", [failure(), failure(Category.DESTINATION_BLOCKED, retryability=Retryability.BACKOFF, recovery=Recovery.RETRY)])
async def test_unknown_and_security_failures_never_automatically_retry(core, error):
    core.provider.responses = [ResolutionResult(ResourceState.UNKNOWN, error=error)]
    transfer = await submit(core)
    for _ in range(5):
        await core.engine.tick()
        core.now[0] += 1000
    assert len(core.provider.calls) == 1
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
async def test_manual_retry_preserves_completed_sibling_and_canonical_paths(core):
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
async def test_explicit_reacquisition_revalidates_completed_history(core):
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


@pytest.mark.asyncio
async def test_unknown_cleanup_failure_is_retained_without_retry_storm(core):
    core.provider.cleanup_response = TransferOutcome(OutcomeKind.FAILURE, failure())
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
    result = core.provider.parcel(state=ResourceState.AVAILABLE, ownership=Ownership.OBSERVED)
    core.provider.inventory_items = (result.observation,)
    await core.engine.reconcile_inventory()
    await core.engine.tick()
    assert not [item for item in core.provider.calls if item[0] == "resolve"]
    transfer = (await core.repository.active())[0]
    resource = (await core.repository.resources(transfer.id))[0][0]
    assert resource.ownership == Ownership.OBSERVED
    await core.engine._cleanup_resources(transfer.id)
    assert not [item for item in core.provider.calls if item[0] == "cleanup"]


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
async def test_prepared_attempt_survives_crash_before_external_contact(core):
    transfer = await submit(core)
    record = (await core.repository.requests(transfer.id))[0]
    await core.engine._resolve(record)
    artifact = (await core.repository.artifacts(transfer.id))[0]
    request = ExecutionRequest(artifact.candidates[0], artifact.target, "crash-before-start")
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
async def test_executor_uncertainty_reserves_slot_without_creating_replacement(core):
    transfer = await submit(core)
    await core.engine.tick()
    artifact = (await core.repository.artifacts(transfer.id))[0]
    core.executor.jobs[artifact.execution.attempt_id] = ExecutionObservation(artifact.execution, ExecutionState.UNKNOWN,
        error=failure(Category.UNMAPPED_EXECUTOR_ERROR, domain=Domain.EXECUTOR))
    for _ in range(4):
        core.now[0] += 1000
        await core.engine.tick()
    assert len(await core.repository.executions(transfer.id)) == 1
    assert not await core.engine.retry(transfer.id)


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
async def test_executor_can_change_on_retry_without_recreating_artifact(core):
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
async def test_mirrors_share_one_artifact_and_failover_retires_partial_bytes(core):
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
    target = Path(artifact.target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"part")
    sidecar = Path(core.executor.resumable_paths(artifact.target)[0])
    sidecar.write_bytes(b"resume")
    error = NormalizedError(Domain.NETWORK, Category.REMOTE_READ_FAILED, Stage.EXECUTION,
                            Retryability.BACKOFF, Recovery.TRY_ALTERNATE_CANDIDATE)
    core.executor.jobs[artifact.execution.attempt_id] = replace(core.executor.jobs[artifact.execution.attempt_id], state=ExecutionState.FAILED, error=error)
    await core.engine.tick()
    assert not target.exists() and not sidecar.exists()
    await core.engine.tick()
    retried = (await core.repository.artifacts(transfer.id))[0]
    assert retried.id == artifact.id and retried.target == artifact.target
    assert retried.selected == 1
    attempts = await core.repository.executions(transfer.id)
    assert len(attempts) == 2
    assert {item.candidate.id for item in attempts} == {first.id, second.id}


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
async def test_near_size_mirrors_are_independent_before_sampling(core):
    from unittest.mock import AsyncMock
    first = replace(core.provider.candidate("same.bin"), expected_bytes=1000, source_identity=SourceIdentity("host", "one"))
    second = replace(core.provider.candidate("same.bin"), expected_bytes=1001, source_identity=SourceIdentity("host", "two"))
    core.provider.responses = [ResolutionResult(ResourceState.AVAILABLE, (first,)), ResolutionResult(ResourceState.AVAILABLE, (second,))]
    core.executor.fingerprint = AsyncMock(return_value=ArtifactFingerprint(1000, "same-sampled-bytes"))
    transfer = await core.engine.submit((TransferRequest("parcel", "one"), TransferRequest("parcel", "two")))
    await core.engine.tick()
    artifacts = await core.repository.artifacts(transfer.id)
    assert len(artifacts) == 2
    assert artifacts[0].target != artifacts[1].target
    assert core.executor.fingerprint.await_count == 0


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
async def test_multiple_mirrors_keep_one_physical_artifact_and_do_not_cycle_on_local_failure(core, count):
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
    (0, 0, False),
    (1000, 1000, True),
    (1000, 1001, False),
    (1000, 1002, False),
    (1024**4, 1024**4, True),
    (1024**4, 1024**4 + 512 * 1024**2, False),
    (1024**4, 1024**4 + 512 * 1024**2 + 1, False),
])
def test_mirror_size_boundaries_are_conservative(left_size, right_size, expected):
    from transfers.mirrors import comparable
    provider = ParcelProvider()
    left = replace(provider.candidate("Same.bin"), expected_bytes=left_size, source_identity=SourceIdentity("host", "one"))
    right = replace(provider.candidate("same.bin"), expected_bytes=right_size, source_identity=SourceIdentity("host", "two"))
    assert comparable(left, right) is expected
