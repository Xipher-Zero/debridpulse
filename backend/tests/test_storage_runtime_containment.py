"""Runtime feedback contracts for WS1 S1 download-storage containment."""
import errno
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from application.service import ApplicationService
from transfers.errors import Category, Domain, NormalizedError, Recovery, Retryability, Stage
from transfers.policy import TransferPolicy
from transfers.storage import DiskCapacity, StorageDomain, StorageState


def _local_error(category):
    return NormalizedError(
        Domain.LOCAL_RESOURCE,
        category,
        Stage.EXECUTION,
        retryability=Retryability.AFTER_RESOURCE_CHANGE,
        recovery=Recovery.REQUIRE_OPERATOR,
    )


def test_universal_policy_does_not_reinterpret_local_storage_failure_without_application_handler():
    policy = TransferPolicy(max_attempts=1)
    decision = policy.retry(_local_error(Category.DISK_FULL), attempts=999, now=42.0)
    assert not decision.automatic


def test_application_storage_handler_can_defer_recognized_environmental_failure():
    seen = []

    def contain(error):
        seen.append(error.category)
        return error.category == Category.DISK_FULL

    policy = TransferPolicy(max_attempts=1, local_resource_failure_handler=contain)
    decision = policy.retry(_local_error(Category.DISK_FULL), attempts=999, now=42.0)
    assert decision.automatic
    assert decision.action == Recovery.RETRY
    assert decision.retry_at == 42.0
    assert seen == [Category.DISK_FULL]


def test_application_storage_handler_cannot_make_unrecognized_local_failure_retryable():
    policy = TransferPolicy(max_attempts=1, local_resource_failure_handler=lambda error: False)
    decision = policy.retry(_local_error(Category.LOCAL_PATH_CONFLICT), attempts=999, now=42.0)
    assert not decision.automatic


@pytest.mark.asyncio
async def test_executor_disk_full_feedback_closes_dispatch_and_preserves_nonterminal_artifact(tmp_path):
    app_dir = tmp_path / "app"
    download_dir = tmp_path / "download"
    app_dir.mkdir()
    download_dir.mkdir()
    capacity = DiskCapacity(download_dir, application_path=app_dir / "debridpulse.db")
    capacity.check()

    generic = _local_error(Category.DISK_FULL)
    artifact = SimpleNamespace(id=7, state="queued", error=generic, retry_at=42.0)
    repository = SimpleNamespace(
        artifacts=AsyncMock(return_value=(artifact,)),
        artifact_state=AsyncMock(),
    )
    engine = SimpleNamespace(repository=repository, dispatch_permitted=True)
    application = ApplicationService(engine, capacity=capacity)

    await application._contain_download_storage_faults((SimpleNamespace(id=3),))

    assert engine.dispatch_permitted is False
    assert capacity.snapshot(StorageDomain.DOWNLOAD).state == StorageState.FULL
    repository.artifact_state.assert_awaited_once()
    args = repository.artifact_state.await_args
    assert args.args[:2] == (7, "queued")
    assert args.kwargs["retry_at"] == 42.0
    assert args.kwargs["error"].category == Category.DOWNLOAD_STORAGE_FULL


@pytest.mark.asyncio
async def test_executor_local_io_feedback_is_unavailable_not_full(tmp_path):
    app_dir = tmp_path / "app"
    download_dir = tmp_path / "download"
    app_dir.mkdir()
    download_dir.mkdir()
    capacity = DiskCapacity(download_dir, application_path=app_dir / "debridpulse.db")
    capacity.check()

    artifact = SimpleNamespace(id=8, state="queued", error=_local_error(Category.LOCAL_IO_FAILURE), retry_at=0.0)
    repository = SimpleNamespace(
        artifacts=AsyncMock(return_value=(artifact,)),
        artifact_state=AsyncMock(),
    )
    engine = SimpleNamespace(repository=repository, dispatch_permitted=True)
    application = ApplicationService(engine, capacity=capacity)

    await application._contain_download_storage_faults((SimpleNamespace(id=4),))

    snapshot = capacity.snapshot(StorageDomain.DOWNLOAD)
    assert snapshot.state == StorageState.UNAVAILABLE
    assert snapshot.state != StorageState.FULL
    error = repository.artifact_state.await_args.kwargs["error"]
    assert error.category == Category.DOWNLOAD_STORAGE_UNAVAILABLE


def test_failed_probe_clears_stale_filesystem_topology(tmp_path, monkeypatch):
    import transfers.storage as storage

    app_dir = tmp_path / "app"
    download_dir = tmp_path / "download"
    app_dir.mkdir()
    download_dir.mkdir()
    capacity = DiskCapacity(download_dir, application_path=app_dir / "debridpulse.db")
    capacity.check()
    assert capacity.shared_filesystem is True
    assert capacity.snapshot(StorageDomain.APPLICATION_STATE).filesystem_id is not None

    original = storage.shutil.disk_usage

    def fail_application(path):
        if str(path) == str(app_dir):
            raise OSError(errno.EIO, "stat failed")
        return original(path)

    monkeypatch.setattr(storage.shutil, "disk_usage", fail_application)
    snapshot = capacity.probe(StorageDomain.APPLICATION_STATE)
    assert snapshot.state == StorageState.UNAVAILABLE
    assert snapshot.filesystem_id is None
    assert snapshot.total_bytes is None
    assert snapshot.free_bytes is None
    assert capacity.shared_filesystem is None
