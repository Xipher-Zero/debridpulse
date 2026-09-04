"""Focused WS1 S1 storage-health and containment contracts."""
import asyncio
import errno
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from api.storage_health_routes import router as storage_health_router
from application.service import ApplicationService
from transfers.storage import (
    DiskCapacity,
    StorageDomain,
    StorageHealthError,
    StorageReason,
    StorageState,
    classify_sqlite_storage_fault,
    classify_storage_fault,
)

GIB = 1024 ** 3


def _usage(free_gb, total_gb=100):
    free = int(free_gb * GIB)
    total = int(total_gb * GIB)
    return SimpleNamespace(total=total, used=total - free, free=free)


def _capacity(tmp_path, *, minimum=0, hysteresis=0.5):
    app = tmp_path / "app"
    download = tmp_path / "download"
    app.mkdir()
    download.mkdir()
    return DiskCapacity(
        download,
        minimum,
        hysteresis,
        application_path=app / "debridpulse.db",
    ), app, download


def _patch_usage(monkeypatch, app, download, *, app_free=50, download_free=50):
    import transfers.storage as storage

    def fake(path):
        path = str(path)
        if path == str(download):
            return _usage(download_free)
        if path == str(app):
            return _usage(app_free)
        raise AssertionError(f"unexpected disk path: {path}")

    monkeypatch.setattr(storage.shutil, "disk_usage", fake)


def test_low_space_hysteresis_and_recovery_threshold(tmp_path, monkeypatch):
    capacity, app, download = _capacity(tmp_path, minimum=10, hysteresis=0.5)
    _patch_usage(monkeypatch, app, download, download_free=9)
    assert capacity.check()["download"]["state"] == "low_space"
    first_generation = capacity.snapshot(StorageDomain.DOWNLOAD).generation

    _patch_usage(monkeypatch, app, download, download_free=10.25)
    assert capacity.check()["download"]["state"] == "low_space"
    assert capacity.snapshot(StorageDomain.DOWNLOAD).generation == first_generation

    _patch_usage(monkeypatch, app, download, download_free=10.5)
    assert capacity.check()["download"]["state"] == "healthy"
    assert capacity.download_work_permitted


def test_zero_threshold_disables_only_low_space_guard(tmp_path, monkeypatch):
    capacity, app, download = _capacity(tmp_path, minimum=0)
    _patch_usage(monkeypatch, app, download, download_free=0.25)
    result = capacity.check()
    assert result["enabled"] is False
    assert result["download"]["state"] == "healthy"

    full = capacity.report_fault(StorageDomain.DOWNLOAD, OSError(errno.ENOSPC, "full"))
    assert full is not None and full.status_code == 507
    assert capacity.snapshot(StorageDomain.DOWNLOAD).state == StorageState.FULL

    read_only = capacity.report_fault(StorageDomain.DOWNLOAD, OSError(errno.EROFS, "read only"))
    assert read_only is not None and read_only.status_code == 503
    assert capacity.snapshot(StorageDomain.DOWNLOAD).state == StorageState.READ_ONLY


def test_errno_fault_classifier_and_eio_is_not_full():
    assert classify_storage_fault(OSError(errno.ENOSPC, "x")).state == StorageState.FULL
    if hasattr(errno, "EDQUOT"):
        quota = classify_storage_fault(OSError(errno.EDQUOT, "x"))
        assert quota.state == StorageState.FULL and quota.reason == StorageReason.QUOTA_EXHAUSTED
    assert classify_storage_fault(OSError(errno.EROFS, "x")).state == StorageState.READ_ONLY
    io = classify_storage_fault(OSError(errno.EIO, "x"))
    assert io.state == StorageState.UNAVAILABLE and io.state != StorageState.FULL


def test_generic_eio_with_free_capacity_returns_503_not_507(tmp_path, monkeypatch):
    capacity, app, download = _capacity(tmp_path)
    _patch_usage(monkeypatch, app, download, download_free=50)
    capacity.check()
    error = capacity.report_fault(StorageDomain.DOWNLOAD, OSError(errno.EIO, "disk i/o"))
    assert error.status_code == 503
    assert error.error.category.value == "download_storage_unavailable"
    assert capacity.snapshot(StorageDomain.DOWNLOAD).free_bytes > 0


def test_missing_and_stat_failure_are_unavailable(tmp_path, monkeypatch):
    capacity, app, download = _capacity(tmp_path)
    download.rmdir()
    assert capacity.probe(StorageDomain.DOWNLOAD).state == StorageState.UNAVAILABLE
    assert capacity.snapshot(StorageDomain.DOWNLOAD).reason == StorageReason.MISSING

    download.mkdir()
    import transfers.storage as storage
    original = storage.shutil.disk_usage

    def fail(path):
        if str(path) == str(app):
            raise OSError(errno.EIO, "stat failed")
        return original(path)

    monkeypatch.setattr(storage.shutil, "disk_usage", fail)
    snapshot = capacity.probe(StorageDomain.APPLICATION_STATE)
    assert snapshot.state == StorageState.UNAVAILABLE
    assert snapshot.reason == StorageReason.STAT_FAILED


def test_same_and_distinct_filesystem_topology(tmp_path, monkeypatch):
    capacity, app, download = _capacity(tmp_path)
    _patch_usage(monkeypatch, app, download)
    capacity.check()
    assert capacity.shared_filesystem is True
    assert set(capacity.health()) >= {"application_state", "download", "shared_filesystem"}

    monkeypatch.setattr(
        capacity,
        "_filesystem_identity",
        lambda path: "application-fs" if path.name == "app" else "download-fs",
    )
    capacity.check()
    assert capacity.shared_filesystem is False
    assert capacity.snapshot(StorageDomain.APPLICATION_STATE).domain != capacity.snapshot(StorageDomain.DOWNLOAD).domain


def test_sqlite_storage_faults_are_narrowly_normalized(tmp_path, monkeypatch):
    capacity, app, download = _capacity(tmp_path)
    _patch_usage(monkeypatch, app, download)
    capacity.check()

    full = capacity.report_application_exception(sqlite3.OperationalError("database or disk is full"))
    assert full.status_code == 507
    assert full.error.category.value == "application_storage_full"

    ro = capacity.report_application_exception(sqlite3.OperationalError("attempt to write a readonly database"))
    assert ro.status_code == 503
    assert ro.error.category.value == "application_storage_read_only"

    unavailable = capacity.report_application_exception(sqlite3.OperationalError("disk I/O error"))
    assert unavailable.status_code == 503
    assert unavailable.error.category.value == "application_storage_unavailable"

    unrelated = sqlite3.OperationalError("no such table: definitely_missing")
    assert classify_sqlite_storage_fault(unrelated) is None
    assert capacity.report_application_exception(unrelated) is None


def test_raw_non_sqlite_storage_errno_cannot_poison_application_state(tmp_path, monkeypatch):
    capacity, app, download = _capacity(tmp_path)
    _patch_usage(monkeypatch, app, download)
    capacity.check()
    before = capacity.snapshot(StorageDomain.APPLICATION_STATE)

    raw = OSError(errno.ENOSPC, "download target full")
    assert classify_sqlite_storage_fault(raw) is None
    assert capacity.report_application_exception(raw) is None
    after = capacity.snapshot(StorageDomain.APPLICATION_STATE)
    assert after.state == StorageState.HEALTHY
    assert after.generation == before.generation


def _dummy_application(capacity):
    repository = SimpleNamespace(active=AsyncMock(return_value=()))
    engine = SimpleNamespace(
        repository=repository,
        dispatch_permitted=True,
        resolve_pending=AsyncMock(),
        reconcile_executions=AsyncMock(),
        process_postprocessors=AsyncMock(),
    )
    pause_changed = Mock()
    return ApplicationService(engine, capacity=capacity, pause_changed=pause_changed), engine, pause_changed


@pytest.mark.asyncio
async def test_application_storage_blocks_mutation_before_side_effect(tmp_path, monkeypatch):
    capacity, app, download = _capacity(tmp_path)
    _patch_usage(monkeypatch, app, download)
    capacity.check()
    application, _engine, _pause_changed = _dummy_application(capacity)
    capacity.report_application_exception(sqlite3.OperationalError("database or disk is full"))

    touched = False
    with pytest.raises(StorageHealthError) as caught:
        async with application.application_operation():
            touched = True
    assert touched is False
    assert caught.value.status_code == 507


@pytest.mark.asyncio
async def test_application_storage_fault_closes_executor_dispatch(tmp_path, monkeypatch):
    capacity, app, download = _capacity(tmp_path)
    _patch_usage(monkeypatch, app, download, app_free=0, download_free=50)
    application, engine, _pause_changed = _dummy_application(capacity)

    health = await application.check_resources()
    assert health["application_state"]["state"] == "full"
    assert health["download"]["state"] == "healthy"
    assert engine.dispatch_permitted is False


@pytest.mark.asyncio
async def test_download_fault_allows_route_resolution_but_defers_dispatch_and_postprocessing_without_pause(tmp_path, monkeypatch):
    capacity, app, download = _capacity(tmp_path)
    _patch_usage(monkeypatch, app, download)
    capacity.check()
    application, engine, pause_changed = _dummy_application(capacity)

    capacity.report_fault(StorageDomain.DOWNLOAD, OSError(errno.EROFS, "read only"))
    await application.resolve_pending()
    await application.process_postprocessors()
    assert engine.resolve_pending.await_count == 1
    assert engine.process_postprocessors.await_count == 0

    _patch_usage(monkeypatch, app, download, download_free=0)
    health = await application.check_resources()
    assert health["download"]["state"] == "full"
    assert engine.dispatch_permitted is False
    pause_changed.assert_not_called()


@pytest.mark.asyncio
async def test_recovery_clears_containment_without_restart(tmp_path, monkeypatch):
    capacity, app, download = _capacity(tmp_path)
    _patch_usage(monkeypatch, app, download, download_free=0)
    capacity.check()
    application, engine, _pause_changed = _dummy_application(capacity)
    await application.check_resources()
    assert engine.dispatch_permitted is False

    _patch_usage(monkeypatch, app, download, download_free=50)
    health = await application.check_resources()
    assert health["download"]["state"] == "healthy"
    assert engine.dispatch_permitted is True


@pytest.mark.asyncio
async def test_health_endpoint_is_reachable_without_database_access(tmp_path):
    missing_app = tmp_path / "missing-app" / "debridpulse.db"
    download = tmp_path / "download"
    download.mkdir()
    capacity = DiskCapacity(download, application_path=missing_app)
    application, _engine, _pause_changed = _dummy_application(capacity)

    app = FastAPI()
    app.state.application = application
    app.include_router(storage_health_router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/storage/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["application_state"]["state"] == "unavailable"
    assert payload["download"]["state"] == "healthy"


def test_transition_generation_deduplicates_stable_fault(tmp_path, monkeypatch):
    capacity, app, download = _capacity(tmp_path)
    _patch_usage(monkeypatch, app, download)
    capacity.check()
    capacity.report_fault(StorageDomain.DOWNLOAD, OSError(errno.EIO, "first"))
    generation = capacity.snapshot(StorageDomain.DOWNLOAD).generation
    transition = capacity.snapshot(StorageDomain.DOWNLOAD).transitioned_at
    capacity.report_fault(StorageDomain.DOWNLOAD, OSError(errno.EIO, "same"))
    assert capacity.snapshot(StorageDomain.DOWNLOAD).generation == generation
    assert capacity.snapshot(StorageDomain.DOWNLOAD).transitioned_at == transition


@pytest.mark.asyncio
async def test_scheduler_skips_db_heavy_loop_while_application_storage_unhealthy(monkeypatch):
    import core.scheduler as scheduler

    fake = SimpleNamespace(
        resolution_wakeup=asyncio.Event(),
        engine=SimpleNamespace(policy=SimpleNamespace(resource_poll_interval=1)),
        application_storage_permitted=lambda: False,
        resolve_pending=AsyncMock(),
    )
    monkeypatch.setattr(scheduler, "application", fake)

    async def stop_after_skip(_event, _timeout):
        raise asyncio.CancelledError

    monkeypatch.setattr(scheduler, "_wait_for_work", stop_after_skip)
    with pytest.raises(asyncio.CancelledError):
        await scheduler.sync_status_loop()
    fake.resolve_pending.assert_not_called()


@pytest.mark.asyncio
async def test_disk_guard_runs_with_zero_low_space_threshold(monkeypatch):
    import core.scheduler as scheduler

    fake = SimpleNamespace(check_resources=AsyncMock())
    monkeypatch.setattr(scheduler, "application", fake)
    monkeypatch.setattr(
        scheduler,
        "get_settings",
        lambda: SimpleNamespace(min_free_disk_gb=0, disk_guard_interval_seconds=60),
    )
    calls = 0

    async def bounded_sleep(_seconds):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(scheduler.asyncio, "sleep", bounded_sleep)
    with pytest.raises(asyncio.CancelledError):
        await scheduler.disk_guard_loop()
    assert fake.check_resources.await_count == 1