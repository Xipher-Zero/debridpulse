"""Deep WS2 P1 qualification for failover ordering, provenance, and mixed progress."""
from dataclasses import replace
from pathlib import Path

import pytest

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from providers.general_http.provider import GeneralHttpProvider
from test_ws2p1_failover_progress import (
    EquivalentParcelProvider,
    MultiUnknownProvider,
    NoProgressMemoryExecutor,
    RuntimeHttpExecutor,
    build_engine,
)
from transfers.engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Origin, Recovery, Retryability, Stage
from transfers.models import ExecutionObservation, ExecutionState, TransferProgress, TransferRequest, TransferState
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


def remote_failure():
    return NormalizedError(
        Domain.NETWORK,
        Category.REMOTE_READ_FAILED,
        Stage.EXECUTION,
        Retryability.BACKOFF,
        Recovery.TRY_ALTERNATE_CANDIDATE,
        origin=Origin.REMOTE_SOURCE,
        operator_action_required=False,
    )


async def attach_two(engine, repository, first, second, *, first_payload="original-a", second_payload="original-b"):
    canonical = await engine.submit(
        (TransferRequest("parcel", first_payload, name="same.bin", preferred_provider=first.descriptor.id),),
        deduplicate=False,
    )
    await engine.resolve_pending()
    source = await engine.submit(
        (TransferRequest("parcel", second_payload, name="same.bin", preferred_provider=second.descriptor.id),),
        deduplicate=False,
    )
    await engine.resolve_pending()
    artifact = (await repository.artifacts(canonical.id))[0]
    assert [candidate.provider_id for candidate in artifact.candidates] == [first.descriptor.id, second.descriptor.id]
    return canonical, source


async def fail_current(engine, repository, executor, transfer_id, error):
    artifact = (await repository.artifacts(transfer_id))[0]
    assert artifact.execution is not None
    handle = artifact.execution
    executor.jobs[handle.attempt_id] = replace(
        executor.jobs[handle.attempt_id], state=ExecutionState.FAILED, error=error,
    )
    await engine.reconcile_executions()
    return handle, (await repository.artifacts(transfer_id))[0]


async def advance_a_to_b(engine, repository, executor, transfer_id, error):
    await engine.reconcile_executions()
    a1, artifact = await fail_current(engine, repository, executor, transfer_id, error)
    assert artifact.selected == 0 and artifact.state == "queued" and artifact.execution is None

    await engine.reconcile_executions()
    a2, artifact = await fail_current(engine, repository, executor, transfer_id, error)
    assert artifact.selected == 0 and artifact.state == "refresh_pending"

    await engine.reconcile_executions()
    artifact = (await repository.artifacts(transfer_id))[0]
    assert artifact.selected == 0 and artifact.state == "queued"

    await engine.reconcile_executions()
    a3, artifact = await fail_current(engine, repository, executor, transfer_id, error)
    assert artifact.selected == 1 and artifact.state == "queued" and artifact.execution is None
    return (a1, a2, a3), artifact


@pytest.mark.asyncio
async def test_healthy_active_candidate_is_not_preempted_when_equivalent_alternate_arrives(tmp_path, monkeypatch):
    first = EquivalentParcelProvider("provider-a")
    second = EquivalentParcelProvider("provider-b")
    executor = NoProgressMemoryExecutor(None)
    engine, repository, _registry = await build_engine(tmp_path, monkeypatch, (first, second), executor)

    canonical = await engine.submit(
        (TransferRequest("parcel", "original-a", name="same.bin", preferred_provider="provider-a"),),
        deduplicate=False,
    )
    await engine.resolve_pending()
    await engine.reconcile_executions()
    before = (await repository.artifacts(canonical.id))[0]
    assert before.selected == 0 and before.execution is not None

    source = await engine.submit(
        (TransferRequest("parcel", "original-b", name="same.bin", preferred_provider="provider-b"),),
        deduplicate=False,
    )
    await engine.resolve_pending()
    after_attach = (await repository.artifacts(canonical.id))[0]
    assert after_attach.execution == before.execution
    assert after_attach.selected == 0
    assert (await repository.get(source.id)).state == TransferState.CONSOLIDATED

    await engine.reconcile_executions()
    healthy = (await repository.artifacts(canonical.id))[0]
    assert healthy.execution == before.execution
    assert healthy.selected == 0
    starts = [handle for operation, handle in executor.calls if operation == "start"]
    assert starts == [before.execution]


@pytest.mark.asyncio
async def test_foreign_b_refresh_uses_b_request_then_all_candidates_exhaust_without_bounce(tmp_path, monkeypatch):
    first = EquivalentParcelProvider("provider-a")
    second = EquivalentParcelProvider("provider-b")
    executor = NoProgressMemoryExecutor(None)
    policy = TransferPolicy(max_attempts=3, retry_delay=0, adoption_stability_seconds=0)
    engine, repository, _registry = await build_engine(
        tmp_path, monkeypatch, (first, second), executor, policy=policy,
    )
    canonical, source = await attach_two(engine, repository, first, second)
    failure = remote_failure()
    a_attempts, artifact = await advance_a_to_b(engine, repository, executor, canonical.id, failure)
    assert ("refresh_request", "original-a") in first.calls

    await engine.reconcile_executions()
    b1, artifact = await fail_current(engine, repository, executor, canonical.id, failure)
    assert artifact.selected == 1 and artifact.state == "queued"

    await engine.reconcile_executions()
    b2, artifact = await fail_current(engine, repository, executor, canonical.id, failure)
    assert artifact.selected == 1 and artifact.state == "refresh_pending"

    await engine.reconcile_executions()
    artifact = (await repository.artifacts(canonical.id))[0]
    assert artifact.selected == 1 and artifact.state == "queued"
    assert ("refresh_request", "original-b") in second.calls

    await engine.reconcile_executions()
    b3, artifact = await fail_current(engine, repository, executor, canonical.id, failure)
    assert artifact.selected == 1
    assert artifact.state == "error"
    assert artifact.execution is None
    assert (await repository.get(source.id)).state == TransferState.CONSOLIDATED

    attempts = await repository.executions(canonical.id)
    providers = [attempt.candidate.provider_id for attempt in attempts if attempt.candidate is not None]
    assert providers == ["provider-a", "provider-a", "provider-a", "provider-b", "provider-b", "provider-b"]
    by_id = {attempt.handle.attempt_id: attempt for attempt in attempts}
    for handle in (*a_attempts, b1, b2, b3):
        assert by_id[handle.attempt_id].state == "failed"


@pytest.mark.asyncio
async def test_delivering_alternate_owns_final_execution_provenance(tmp_path, monkeypatch):
    first = EquivalentParcelProvider("provider-a")
    second = EquivalentParcelProvider("provider-b")
    executor = NoProgressMemoryExecutor(None)
    engine, repository, _registry = await build_engine(tmp_path, monkeypatch, (first, second), executor)
    canonical, source = await attach_two(engine, repository, first, second)
    failure = remote_failure()
    _a_attempts, artifact = await advance_a_to_b(engine, repository, executor, canonical.id, failure)
    b_candidate = artifact.candidates[1]

    await engine.reconcile_executions()
    artifact = (await repository.artifacts(canonical.id))[0]
    assert artifact.execution is not None and artifact.selected == 1
    executor.finish(artifact.execution)
    await engine.reconcile_executions()

    view = await repository.presentation(canonical.id, details=True)
    assert view["status"] == TransferState.COMPLETED
    assert view["delivering_provider_id"] == "provider-b"
    delivered = [item for item in view["execution_attempts"] if item["delivered"]]
    assert len(delivered) == 1
    assert delivered[0]["provider_id"] == "provider-b"
    assert delivered[0]["candidate_id"] == str(b_candidate.id)
    assert (await repository.get(source.id)).state == TransferState.CONSOLIDATED
    assert all(not item["delivered"] for item in view["execution_attempts"] if item["provider_id"] == "provider-a")


@pytest.mark.asyncio
async def test_discovered_size_survives_unknown_size_failover_and_stale_a_observation(tmp_path, monkeypatch):
    provider = MultiUnknownProvider()
    executor = NoProgressMemoryExecutor(None)
    engine, repository, registry = await build_engine(tmp_path, monkeypatch, (provider,), executor)
    transfer = await engine.submit((TransferRequest("multi", "x", name="unknown.bin"),), deduplicate=False)
    await engine.tick()
    artifact = (await repository.artifacts(transfer.id))[0]
    assert artifact.expected_bytes == 4
    assert artifact.candidates[0].expected_bytes == 0
    assert artifact.candidates[1].expected_bytes == 0
    a1 = artifact.execution

    failure = remote_failure()
    _handle, artifact = await fail_current(engine, repository, executor, transfer.id, failure)
    assert artifact.selected == 0 and artifact.expected_bytes == 4
    await engine.reconcile_executions()
    a2, artifact = await fail_current(engine, repository, executor, transfer.id, failure)
    assert artifact.selected == 1
    assert artifact.expected_bytes == 4
    assert artifact.execution is None

    stale = ExecutionObservation(
        a2,
        ExecutionState.TRANSFERRING,
        TransferProgress(999, 999, 1),
        (artifact.target,),
    )
    await repository.execution(stale)
    artifact = (await repository.artifacts(transfer.id))[0]
    assert artifact.selected == 1 and artifact.expected_bytes == 4
    history = {attempt.handle.attempt_id: attempt for attempt in await repository.executions(transfer.id)}
    assert history[a2.attempt_id].state == "failed"
    assert history[a1.attempt_id].state == "failed"

    restarted_repository = TransferRepository()
    restarted = TransferEngine(
        restarted_repository,
        registry,
        download_root=engine.root,
        policy=engine.policy,
        clock=lambda: 1000.0,
    )
    await restarted.initialize()
    persisted = (await restarted_repository.artifacts(transfer.id))[0]
    assert persisted.selected == 1 and persisted.expected_bytes == 4

    await restarted.reconcile_executions()
    active = (await restarted_repository.artifacts(transfer.id))[0]
    executor.jobs[active.execution.attempt_id] = replace(
        executor.jobs[active.execution.attempt_id],
        progress=TransferProgress(4, 2, 1),
    )
    await restarted.reconcile_executions()
    assert (await restarted_repository.get(transfer.id)).progress == 50.0
    assert (await restarted_repository.artifacts(transfer.id))[0].expected_bytes == 4


@pytest.mark.asyncio
async def test_mixed_provider_known_and_executor_discovered_sizes_aggregate_correctly(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    parcel = ParcelProvider("parcel-known")
    http = GeneralHttpProvider()
    memory = MemoryExecutor(repository.authorize_execution)
    runtime_http = RuntimeHttpExecutor(repository.authorize_execution, total=10, completed=2)
    registry.register_provider(parcel)
    registry.register_provider(http)
    registry.register_executor(memory)
    registry.register_executor(runtime_http)
    engine = TransferEngine(
        repository,
        registry,
        download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(retry_delay=0, adoption_stability_seconds=0, max_active_executions=8),
        clock=lambda: 1000.0,
    )
    await engine.initialize()

    transfer = await engine.submit(
        (
            TransferRequest("parcel", "known", name="known.bin", preferred_provider="parcel-known"),
            TransferRequest("https", "https://example.test/runtime.bin", name="runtime.bin"),
        ),
        name="mixed",
        deduplicate=False,
    )
    await engine.tick()
    artifacts = await repository.artifacts(transfer.id)
    assert sorted(item.expected_bytes for item in artifacts) == [4, 10]
    assert (await repository.get(transfer.id)).progress == pytest.approx(3 / 14 * 100)

    known = next(item for item in artifacts if item.candidates[item.selected].provider_id == "parcel-known")
    memory.finish(known.execution)
    await engine.reconcile_executions()
    assert (await repository.get(transfer.id)).progress == pytest.approx(6 / 14 * 100)

    details = await repository.presentation(transfer.id, details=True)
    by_name = {item["filename"]: item for item in details["files"]}
    assert by_name["known.bin"]["progress"] == 100
    assert by_name["runtime.bin"]["progress"] == 20.0
    assert details["size_bytes"] == 14
