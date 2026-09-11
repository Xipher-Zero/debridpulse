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
from transfers.applicability import ProviderApplicability
from transfers.engine import TransferEngine
from transfers.errors import TransferError, Category, Domain, NormalizedError, Retryability, Recovery, Stage
from transfers.models import (
    ArtifactFingerprint, IntegrationDescriptor, OutcomeKind, ResolutionResult, ResourceState,
    TransferOutcome, TransferRequest, TransferState,
)
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
    monkeypatch.setattr(
        ParcelProvider,
        "applicability",
        property(lambda _provider: ProviderApplicability(generic_schemes=frozenset({"http", "https"}))),
    )
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
    assert detail["original_resource"] == "https://fake.example/payload"
    assert "request" not in detail
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
async def test_single_link_submission_response_shape_is_unchanged(runtime):
    """DP 1.0.12 Section 12.1a: single-link submission (the pre-existing,
    already-correct N=1 case) must keep its exact legacy response shape --
    a top-level id/torrent_id naming the one admitted transfer -- alongside
    the new `items` list."""
    _application, _provider, _executor, client = runtime
    response = await client.post("/api/links/add", json={"links": ["https://fake.example/solo"]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] == 1
    assert body["items"] and len(body["items"]) == 1
    assert body["id"] == body["torrent_id"] == body["items"][0]["id"]


@pytest.mark.asyncio
async def test_batch_link_submission_admits_one_transfer_with_n_independent_requests(runtime):
    """DP 1.0.12 corrective Sections 4 + 7 + 21 (Example A/B): one Quick Add
    batch submitting N URLs is one user submission and must durably admit
    exactly ONE transfer owning N independent root requests -- not N
    top-level transfers later requiring cross-transfer collapse. Every
    submitted URL still keeps its own durable request lineage (ordinal/id),
    but submission scope (one batch -> one transfer) is not the same thing
    as equivalence scope (which may still converge those N sibling requests
    onto one canonical artifact -- see test_multi_mirror_general_http_convergence.py).

    The response restores the established parent single-transfer contract
    (id/torrent_id/items[0] all identify the one admitted transfer) rather
    than the N-item shape the reverted topology invented. The only in-repo
    consumer of this response, the frontend Quick Add flow
    (frontend/static/app.js addDashboardEntries), never reads id/torrent_id/
    items from this endpoint at all (it only checks `_deferred`), so the
    restored shape cannot be misinterpreted by it either way."""
    application, _provider, _executor, client = runtime
    links = [f"https://fake.example/mirror-{index}" for index in range(1, 4)]
    response = await client.post("/api/links/add", json={"links": links})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["accepted"] == 3
    # One transfer represents the whole batch: id/torrent_id/items[0] agree.
    assert body["id"] == body["torrent_id"]
    items = body["items"]
    assert len(items) == 1  # ONE transfer represents the whole batch, not N.
    assert items[0]["id"] == body["id"]
    transfer_id = body["id"]
    transfer = await application.repository.get(transfer_id)
    assert transfer is not None
    requests = await application.repository.requests(transfer_id)
    assert len(requests) == 3  # the one transfer owns all 3 independent root requests.
    request_ids = {record.id for record in requests}
    assert len(request_ids) == 3  # each request keeps its own durable identity.


@pytest.mark.asyncio
async def test_quick_add_ten_equivalent_urls_admit_one_transfer_and_converge_to_one_canonical(runtime, monkeypatch):
    """DP 1.0.12 corrective Sections 4.1/4.2/4.3/9/21 Example A -- through the
    REAL Quick Add seam (POST /api/links/add -> ApplicationService.submit_links()),
    not engine.submit() directly.

    Ten equivalent-content mirror URLs submitted as one Quick Add batch must:
      * admit as ONE top-level transfer owning 10 durable root requests with
        distinct ids -- no 10-transfer fan-out (4.1/4.2);
      * then, once driven through the existing, UNMODIFIED engine (via
        ApplicationService.resolve_pending(), the same seam the production
        scheduler uses), converge onto Section A3's exact durable state, not
        merely engine capability proven in isolation:
          - 1 canonical primary artifact;
          - 9 same-transfer standby/duplicate sibling contributions under it;
          - 10 canonical candidate bindings;
          - 10 candidate-origin/provenance records tied back to the 10
            original requests;
          - 0 same-transfer artifact_consolidations rows;
          - the parent transfer NOT marked consolidated (Invariant 4/9).
    """
    application, _provider, executor, client = runtime

    # All ten candidates must be provably the SAME logical artifact to a
    # sampling-capable executor, exactly like ten real mirrors of one file --
    # override the fake sampler to a fixed shared signature regardless of
    # which mirror URL it was given (transfers/mirrors.py identity is proven
    # by sampled content, never by URL/hostname).
    async def shared_fingerprint(candidate):
        return ArtifactFingerprint(candidate.expected_bytes, "shared-quick-add-iso-content")

    monkeypatch.setattr(executor, "fingerprint", shared_fingerprint)

    links = [f"https://mirror{index}.example/ubuntu.iso" for index in range(1, 11)]
    response = await client.post("/api/links/add", json={"links": links})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["accepted"] == 10
    assert body["id"] == body["torrent_id"]  # one transfer represents the whole batch.
    assert len(body["items"]) == 1

    transfer_id = body["id"]
    async with database.get_db() as db:
        total_transfers = await db.fetchone("SELECT COUNT(*) AS n FROM torrents")
    assert int(total_transfers["n"]) == 1  # Section 4.1: no 10-transfer fan-out -- exactly one transfer exists.

    requests_before = await application.repository.requests(transfer_id)
    assert len(requests_before) == 10  # Section 4.1/4.2: one transfer, 10 independent root requests.
    request_ids = {record.id for record in requests_before}
    assert len(request_ids) == 10  # each request keeps its own durable identity.

    # Drive the EXISTING, unmodified engine to convergence through the same
    # application-level seam production's scheduler uses.
    for _ in range(8):
        await application.resolve_pending()
        artifacts = await application.repository.artifacts(transfer_id)
        if len(artifacts) == 1 and len(artifacts[0].candidates) == 10:
            break

    artifacts = await application.repository.artifacts(transfer_id)
    assert len(artifacts) == 1  # 1 canonical primary artifact (repository.artifacts() excludes standby rows).
    canonical_artifact = artifacts[0]
    assert len(canonical_artifact.candidates) == 10  # all 10 candidates bound onto the primary.

    async with database.get_db() as db:
        standby_rows = await db.fetchall(
            "SELECT id,request_id FROM download_files WHERE torrent_id=? AND mirror_state='standby' AND mirror_group_id=?",
            (transfer_id, canonical_artifact.id),
        )
    assert len(standby_rows) == 9  # 9 same-transfer standby/duplicate sibling contributions under the primary.

    bindings = await application.engine.canonical.bindings(canonical_artifact.id)
    assert len(bindings) == 10  # 10 canonical candidate bindings.
    origin_request_ids = set()
    total_origins = 0
    for binding in bindings:
        for origin in binding["origins"]:
            total_origins += 1
            origin_request_ids.add(str(origin["request_id"]))
    assert total_origins == 10  # 10 candidate-origin/provenance records.
    assert origin_request_ids == request_ids  # every origin ties back to one of the 10 original requests.

    async with database.get_db() as db:
        consolidations = await db.fetchone(
            "SELECT COUNT(*) AS n FROM artifact_consolidations WHERE source_transfer_id=?",
            (transfer_id,),
        )
    assert int(consolidations["n"]) == 0  # Invariant 9: zero same-transfer artifact_consolidations rows.

    final_transfer = await application.repository.get(transfer_id)
    assert final_transfer.state != TransferState.CONSOLIDATED  # Invariant 4: sibling convergence != transfer consolidation.


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
async def test_unknown_source_error_is_canonical_recoverable_and_excluded_from_physical_progress(runtime):
    application, provider, executor, client = runtime
    error = NormalizedError(Domain.PROVIDER, Category.UNMAPPED_PROVIDER_ERROR, Stage.RESOLUTION,
        native_code="NATIVE_SECRET_CODE", diagnostic="private provider detail")
    original_resolve = provider.resolve
    async def resolve(request):
        if request.payload == "bad":
            provider.calls.append(("resolve", request.payload))
            return ResolutionResult(ResourceState.UNKNOWN, error=error)
        return await original_resolve(request)
    provider.resolve = resolve
    item = await application.submit((TransferRequest("parcel", "bad", name="bad.bin"), TransferRequest("parcel", "good", name="good.bin")))
    await application.resolve_pending()
    await application.reconcile_executions()
    artifact = (await application.repository.artifacts(item["id"]))[0]
    executor.finish(artifact.execution)
    await application.reconcile_executions()
    detail = (await client.get(f"/api/torrents/{item['id']}")).json()
    requests = await application.repository.requests(item["id"])
    bad = next(record for record in requests if record.request.name == "bad.bin")
    assert detail["status"] == "processing" and detail["progress"] == 100
    assert bad.state == "pending" and bad.error is not None
    assert bad.error.category == Category.UNMAPPED_PROVIDER_ERROR
    assert detail["source_failure_count"] == 0 and detail["source_outcomes"] == []
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
