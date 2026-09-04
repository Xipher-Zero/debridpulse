"""Runtime feedback contracts for WS1 S1 download-storage containment."""
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


def test_download_storage_environmental_failure_does_not_terminalize_on_retry_budget():
    policy = TransferPolicy(max_attempts=1)
    decision = policy.retry(_local_error(Category.DISK_FULL), attempts=999, now=42.0)
    assert decision.automatic
    assert decision.action == Recovery.RETRY
    assert decision.retry_at == 42.0


def test_non_storage_local_failure_keeps_normal_retry_policy():
    policy = TransferPolicy(max_attempts=1)
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
