"""Phase 2 durable quiescence/readiness and migration regression contracts."""
from dataclasses import replace

import pytest
import pytest_asyncio

import db.database as database
from db.database import get_db
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Origin, Retryability, Stage
from transfers.models import TransferRequest
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "phase2.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider()
    registry.register_provider(provider)
    now = [5000.0]
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0),
        clock=lambda: now[0],
    )
    await engine.initialize()
    return repository, registry, provider, engine, now


async def materialize(engine):
    transfer = await engine.submit((TransferRequest("parcel", "box", name="payload.bin"),))
    await engine.resolve_pending()
    artifact = (await engine.repository.artifacts(transfer.id))[0]
    assert artifact.state == "queued"
    assert artifact.execution is None
    return transfer, artifact


@pytest.mark.asyncio
async def test_provider_disablement_is_quiescent_and_reenable_wakes_same_work(runtime):
    repository, registry, provider, engine, _now = runtime
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_executor(executor)
    transfer, artifact = await materialize(engine)

    provider.descriptor = replace(provider.descriptor, enabled=False)
    await engine.reconcile_executions()
    parked = (await repository.artifacts(transfer.id))[0]
    assert parked.id == artifact.id
    assert parked.selected == artifact.selected
    assert parked.state == "recovery_wait"
    assert parked.execution is None
    state = await repository.recovery_context(artifact.id)
    assert state["quiescence_reason"] == "provider_disabled"
    assert state["wake_condition"] == f"provider_enabled:{provider.descriptor.id}"
    assert not [call for call in executor.calls if call[0] == "start"]

    provider.descriptor = replace(provider.descriptor, enabled=True)
    await engine.reconcile_executions()
    resumed = (await repository.artifacts(transfer.id))[0]
    assert resumed.id == artifact.id
    assert resumed.execution is not None
    assert [call for call in executor.calls if call[0] == "start"]


@pytest.mark.asyncio
async def test_executor_unavailable_parks_without_attempt_then_registration_wakes(runtime):
    repository, registry, _provider, engine, _now = runtime
    transfer, artifact = await materialize(engine)

    await engine.reconcile_executions()
    parked = (await repository.artifacts(transfer.id))[0]
    assert parked.state == "recovery_wait"
    assert parked.execution is None
    assert len(await repository.executions(transfer.id)) == 0
    state = await repository.recovery_context(artifact.id)
    assert state["quiescence_reason"] == "executor_unavailable"
    assert state["wake_condition"] == "executor_available"

    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_executor(executor)
    await engine.reconcile_executions()
    resumed = (await repository.artifacts(transfer.id))[0]
    assert resumed.execution is not None
    assert len(await repository.executions(transfer.id)) == 1


@pytest.mark.asyncio
async def test_storage_quiescence_survives_ticks_and_wakes_only_when_dispatch_recovers(runtime):
    repository, registry, _provider, engine, _now = runtime
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_executor(executor)
    transfer, artifact = await materialize(engine)
    error = NormalizedError(
        Domain.LOCAL_RESOURCE, Category.DISK_FULL, Stage.EXECUTION,
        retryability=Retryability.AFTER_RESOURCE_CHANGE, origin=Origin.LOCAL_SYSTEM,
    )
    assert await repository.transition_recovery(
        artifact.id, "recovery_wait", error=error,
        quiescence_reason="storage_unavailable",
        wake_condition="storage_healthy:local_resource",
    )
    engine.dispatch_permitted = False
    for _ in range(3):
        await engine.reconcile_executions()
    parked = (await repository.artifacts(transfer.id))[0]
    assert parked.state == "recovery_wait"
    assert parked.execution is None
    assert not [call for call in executor.calls if call[0] == "start"]

    engine.dispatch_permitted = True
    await engine.reconcile_executions()
    resumed = (await repository.artifacts(transfer.id))[0]
    assert resumed.execution is not None


@pytest.mark.asyncio
async def test_phase1_database_counters_seed_context_without_fabricated_history(runtime):
    repository, _registry, _provider, engine, _now = runtime
    _transfer, artifact = await materialize(engine)
    async with get_db() as db:
        await db.execute(
            "UPDATE download_files SET recovery_failures=2,recovery_refreshes=1 WHERE id=?",
            (artifact.id,),
        )
        await db.execute(
            "DELETE FROM application_events WHERE kind=?",
            (repository._recovery_event_kind(artifact.id),),
        )
        await db.commit()

    restarted = TransferRepository()
    await restarted.initialize()
    context = await restarted.recovery_context(artifact.id)
    assert context["consecutive_no_progress_failures"] == 2
    assert context["candidate_refreshes"] == 1
    assert context["failure_signature"] is None
    assert context["same_signature_failures"] == 0
    assert context["recovery_epoch"] == 0
    assert context["progress_anchor"] is None


@pytest.mark.asyncio
async def test_operator_retry_clears_exhaustion_without_claiming_progress(runtime):
    repository, _registry, _provider, engine, _now = runtime
    _transfer, artifact = await materialize(engine)
    error = NormalizedError(
        Domain.NETWORK, Category.REMOTE_READ_FAILED, Stage.EXECUTION,
        retryability=Retryability.BACKOFF, origin=Origin.REMOTE_SOURCE,
    )
    await repository.record_source_failure(artifact.id, error)
    await repository.record_source_failure(artifact.id, error)
    assert await repository.transition_recovery(
        artifact.id, "recovery_wait", error=error,
        quiescence_reason="recovery_exhausted", wake_condition="operator_retry",
    )
    before = await repository.recovery_context(artifact.id)
    assert before["recovery_epoch"] == 0
    assert before["same_signature_failures"] == 2

    await repository.reset_retry_budget(artifact.id)
    after = await repository.recovery_context(artifact.id)
    assert after["recovery_epoch"] == 0
    assert after["consecutive_no_progress_failures"] == 0
    assert after["same_signature_failures"] == 0
    assert after["quiescence_reason"] is None
