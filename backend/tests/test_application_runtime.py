"""The actual HTTP commands and scheduler drive the unrelated fake integrations."""
import asyncio
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse

import db.database as database
from api.routes import router
from application.service import ApplicationService
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.engine import TransferEngine
from transfers.errors import TransferError, Category, Domain, NormalizedError, Retryability, Recovery, Stage
from transfers.models import TransferRequest, ResolutionResult, ResourceState, TransferOutcome, OutcomeKind, IntegrationDescriptor
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "application.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider()
    provider.descriptor = replace(provider.descriptor, request_types=frozenset({"parcel", "http", "https", "magnet", "torrent"}))
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "files"), policy=TransferPolicy(adoption_stability_seconds=0))
    await engine.initialize()
    application = ApplicationService(engine)
    app = FastAPI()
    app.state.application = application
    app.include_router(router, prefix="/api")
    @app.exception_handler(TransferError)
    async def failure(_request, exc):
        return JSONResponse(status_code=409, content={"detail": exc.error.message, "error": exc.error.as_dict()})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield application, provider, executor, client


@pytest.mark.asyncio
async def test_actual_api_drives_fake_provider_and_executor_without_native_id(runtime):
    application, provider, executor, client = runtime
    response = await client.post("/api/links/add", json={"links": ["https://fake.example/payload"]})
    assert response.status_code == 200, response.text
    transfer_id = response.json()["id"]
    assert provider.calls == []  # durable admission precedes remote work
    await application.resolve_pending()
    await application.reconcile_executions()
    artifact = (await application.repository.artifacts(transfer_id))[0]
    executor.finish(artifact.execution)
    await application.reconcile_executions()
    detail = (await client.get(f"/api/torrents/{transfer_id}")).json()
    assert detail["status"] == "completed"
    assert detail["files"][0]["status"] == "completed"
    assert detail["executors"] == [executor.descriptor.id]
    assert "handle" not in str(detail) and "context" not in str(detail)
    assert "https://fake.example" not in str(detail)
    assert (await client.post(f"/api/torrents/{transfer_id}/retry")).status_code == 200
    assert (await client.delete(f"/api/torrents/{transfer_id}?from_alldebrid=false")).status_code == 200
    assert (await application.repository.get(transfer_id)).state == "deleted"


@pytest.mark.asyncio
async def test_duplicate_preview_validates_empty_input_and_accepts_core_resource_identity(runtime):
    _application, provider, _executor, client = runtime
    assert (await client.post("/api/torrents/check-duplicate", json={})).status_code == 400
    response = await client.post("/api/torrents/check-duplicate", json={"resource_id": "unknown-core-resource"})
    assert response.status_code == 200, response.text
    assert response.json()["duplicate"]["is_duplicate"] is False
    assert provider.calls == []


@pytest.mark.asyncio
async def test_database_wipe_backs_up_canonical_state_and_preserves_pause(runtime, tmp_path, monkeypatch):
    import json
    from core.config import AppSettings
    application, _provider, _executor, client = runtime
    response = await client.post("/api/links/add", json={"links": ["https://fake.example/payload"]})
    await application.resolve_pending()
    await application.reconcile_executions()
    cfg = AppSettings(db_wipe_enabled=True, paused=True, db_backup_before_wipe=True, db_backup_folder=str(tmp_path / "backups"))
    monkeypatch.setattr("api.routes.get_settings", lambda: cfg)
    monkeypatch.setattr("services.db_maintenance.get_settings", lambda: cfg)
    monkeypatch.setattr("api.routes.scheduler_runtime.scheduler_running", lambda: False)
    wiped = await client.post("/api/admin/database/wipe", json={"confirm": True})
    assert wiped.status_code == 200, wiped.text
    report = wiped.json()
    backup = json.loads(Path(report["backup"]["file"]).read_text())
    assert len(backup["tables"]["execution_attempts"]) == 1
    assert backup["tables"]["torrents"][0]["id"] == response.json()["id"]
    assert await application.repository.globally_paused()
    assert await application.repository.active() == ()
    async with database.get_db() as db:
        assert await db.fetchall("PRAGMA foreign_key_check") == []


@pytest.mark.asyncio
async def test_database_wipe_refuses_unknown_execution_state(runtime, monkeypatch):
    from core.config import AppSettings
    from transfers.models import ExecutionObservation, ExecutionState
    from unittest.mock import AsyncMock
    application, _provider, executor, client = runtime
    response = await client.post("/api/links/add", json={"links": ["https://fake.example/payload"]})
    await application.resolve_pending()
    await application.reconcile_executions()
    artifact = (await application.repository.artifacts(response.json()["id"]))[0]
    executor.pause = AsyncMock(return_value=ExecutionObservation(artifact.execution, ExecutionState.UNKNOWN))
    executor.observe = AsyncMock(return_value=ExecutionObservation(artifact.execution, ExecutionState.UNKNOWN))
    cfg = AppSettings(db_wipe_enabled=True, paused=True, db_backup_before_wipe=False)
    monkeypatch.setattr("api.routes.get_settings", lambda: cfg)
    monkeypatch.setattr("api.routes.scheduler_runtime.scheduler_running", lambda: False)
    wiped = await client.post("/api/admin/database/wipe", json={"confirm": True})
    assert wiped.status_code == 409, wiped.text
    assert await application.repository.get(response.json()["id"]) is not None


@pytest.mark.asyncio
async def test_api_pause_defers_intake_and_resume_one_preserves_siblings(runtime):
    application, provider, _executor, client = runtime
    assert (await client.post("/api/processing/pause")).status_code == 200
    first = await client.post("/api/links/add", json={"links": ["https://fake.example/first"]})
    second = await client.post("/api/links/add", json={"links": ["https://fake.example/second"]})
    await application.resolve_pending()
    assert provider.calls == []
    assert (await client.post(f"/api/torrents/{first.json()['id']}/resume")).status_code == 200
    await application.resolve_pending()
    assert (await application.repository.get(second.json()["id"])).paused
    assert [value for operation, value in provider.calls if operation == "resolve"] == ["https://fake.example/first"]


@pytest.mark.asyncio
async def test_scheduler_uses_injected_application(runtime, monkeypatch):
    import core.scheduler as scheduler
    application, provider, _executor, _client = runtime
    await application.submit((TransferRequest("parcel", "scheduled", name="scheduled.bin"),))
    monkeypatch.setattr(scheduler, "application", application)
    async def once(*_args):
        raise asyncio.CancelledError
    async def no_delay(_delay):
        return None
    monkeypatch.setattr(scheduler, "_jitter_sleep", no_delay)
    monkeypatch.setattr(scheduler, "_wait_for_work", once)
    with pytest.raises(asyncio.CancelledError):
        await scheduler.sync_status_loop()
    assert [value for operation, value in provider.calls if operation == "resolve"] == ["scheduled"]


@pytest.mark.asyncio
async def test_unknown_source_error_is_canonical_and_excluded_from_physical_progress(runtime):
    application, provider, executor, client = runtime
    error = NormalizedError(Domain.PROVIDER, Category.UNMAPPED_PROVIDER_ERROR, Stage.RESOLUTION,
        native_code="NATIVE_SECRET_CODE", diagnostic="private provider detail")
    provider.responses = [ResolutionResult(ResourceState.UNKNOWN, error=error)]
    item = await application.submit((TransferRequest("parcel", "bad", name="bad.bin"), TransferRequest("parcel", "good", name="good.bin")))
    await application.resolve_pending()
    await application.reconcile_executions()
    artifact = (await application.repository.artifacts(item["id"]))[0]
    executor.finish(artifact.execution)
    await application.reconcile_executions()
    detail = (await client.get(f"/api/torrents/{item['id']}")).json()
    assert detail["status"] == "completed" and detail["progress"] == 100
    assert detail["source_failure_count"] == 1
    assert detail["source_outcomes"][0]["error"]["category"] == "unmapped_provider_error"
    assert "NATIVE_SECRET_CODE" not in str(detail) and "private provider detail" not in str(detail)


@pytest.mark.asyncio
async def test_slow_postprocessor_does_not_block_executor_observation(runtime):
    application, _provider, executor, _client = runtime
    entered, release = asyncio.Event(), asyncio.Event()
    class Processor:
        descriptor = IntegrationDescriptor("slow", "Slow processor", frozenset())
        async def process(self, _transfer_id, _paths):
            entered.set()
            await release.wait()
            return TransferOutcome(OutcomeKind.SUCCESS)
    application.engine.postprocessors = (Processor(),)
    first = await application.submit((TransferRequest("parcel", "first", name="first.bin"),))
    second = await application.submit((TransferRequest("parcel", "second", name="second.bin"),))
    await application.resolve_pending()
    await application.reconcile_executions()
    executor.finish((await application.repository.artifacts(first["id"]))[0].execution)
    await application.reconcile_executions()
    task = asyncio.create_task(application.process_postprocessors())
    await asyncio.wait_for(entered.wait(), 1)
    executor.finish((await application.repository.artifacts(second["id"]))[0].execution)
    await asyncio.wait_for(application.reconcile_executions(), 1)
    assert (await application.repository.get(second["id"])).state == "extracting"
    release.set()
    await task


@pytest.mark.asyncio
async def test_pause_wins_while_executor_start_acknowledgement_is_delayed(runtime):
    application, _provider, executor, _client = runtime
    started, release = asyncio.Event(), asyncio.Event()
    original = executor.start
    async def delayed(request, handle):
        result = await original(request, handle)
        started.set()
        await release.wait()
        return result
    executor.start = delayed
    item = await application.submit((TransferRequest("parcel", "late", name="late.bin"),))
    await application.resolve_pending()
    task = asyncio.create_task(application.reconcile_executions())
    await started.wait()
    await application.pause(item["id"])
    release.set()
    await task
    artifact = (await application.repository.artifacts(item["id"]))[0]
    assert artifact.state == "paused"
    assert (await executor.observe(artifact.execution)).state == "paused"
