"""WS1 S2 explicit startup and Settings exit-gate contracts."""
import errno
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import transfers.storage as storage
from api.storage_health_routes import storage_health
from application.service import ApplicationService
from core.config import AppSettings
from transfers.errors import Category
from transfers.storage import DiskCapacity, StorageDomain, StorageHealthError, StorageState


def _service(capacity):
    repository = SimpleNamespace(has_integration_references=AsyncMock(return_value=False))
    engine = SimpleNamespace(repository=repository, dispatch_permitted=True)
    return ApplicationService(engine, capacity=capacity), repository, engine


@pytest.mark.asyncio
async def test_startup_missing_download_storage_degrades_without_blocking_health_contract(tmp_path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    missing = tmp_path / "missing-download"
    capacity = DiskCapacity(missing, application_path=app_dir / "debridpulse.db")

    # Composition performs this same canonical initial check. A bad Download
    # path must produce state, not an exception that aborts startup.
    initial = capacity.check()
    service, _repository, engine = _service(capacity)
    health = await storage_health(service)

    assert initial["application_state"]["state"] == "healthy"
    assert initial["download"]["state"] == "unavailable"
    assert health["application_state"]["state"] == "healthy"
    assert health["download"]["state"] == "unavailable"
    assert engine.dispatch_permitted is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected_state", "expected_category"),
    [
        (errno.ENOSPC, "full", Category.DOWNLOAD_STORAGE_FULL),
        (errno.EROFS, "read_only", Category.DOWNLOAD_STORAGE_READ_ONLY),
        (errno.EIO, "unavailable", Category.DOWNLOAD_STORAGE_UNAVAILABLE),
    ],
)
async def test_startup_authoritative_download_faults_degrade_without_abort(
    tmp_path, monkeypatch, code, expected_state, expected_category
):
    app_dir = tmp_path / "app"
    download_dir = tmp_path / "download"
    app_dir.mkdir()
    download_dir.mkdir()
    capacity = DiskCapacity(download_dir, application_path=app_dir / "debridpulse.db")

    def fail_probe(*_args, **_kwargs):
        raise OSError(code, "injected startup storage fault")

    monkeypatch.setattr(storage.tempfile, "mkstemp", fail_probe)

    # This is the startup boundary used by composition. The authoritative
    # Download fault is represented in canonical state while Application-State
    # remains independently usable.
    initial = capacity.check()
    service, _repository, engine = _service(capacity)
    health = await storage_health(service)

    assert initial["application_state"]["state"] == "healthy"
    assert initial["download"]["state"] == expected_state
    assert health["application_state"]["state"] == "healthy"
    assert health["download"]["state"] == expected_state
    assert engine.dispatch_permitted is False

    snapshot = capacity.snapshot(StorageDomain.DOWNLOAD)
    error = StorageHealthError(snapshot)
    assert error.error.category == expected_category
    assert error.status_code == (507 if code == errno.ENOSPC else 503)


@pytest.mark.asyncio
async def test_settings_non_directory_candidate_is_rejected_and_active_state_is_preserved(tmp_path):
    app_dir = tmp_path / "app"
    download_dir = tmp_path / "download"
    app_dir.mkdir()
    download_dir.mkdir()
    candidate = tmp_path / "not-a-directory"
    candidate.write_text("user data")

    capacity = DiskCapacity(download_dir, application_path=app_dir / "debridpulse.db")
    capacity.check()
    before = capacity.snapshot(StorageDomain.DOWNLOAD)
    service, repository, _engine = _service(capacity)
    previous = AppSettings(download_folder=str(download_dir))
    current = previous.model_copy(update={"download_folder": str(candidate)})

    with pytest.raises(StorageHealthError) as caught:
        await service.validate_configuration(previous, current)

    assert caught.value.status_code == 503
    assert caught.value.error.category == Category.DOWNLOAD_STORAGE_UNAVAILABLE
    repository.has_integration_references.assert_awaited_once_with()
    after = capacity.snapshot(StorageDomain.DOWNLOAD)
    assert after.state == StorageState.HEALTHY
    assert after.configured_path == before.configured_path
    assert after.generation == before.generation
    assert candidate.read_text() == "user data"
