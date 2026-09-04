"""WS1 S2 authoritative Download Storage validation and recovery contracts."""
import errno
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import transfers.storage as storage
from api.storage_health_routes import storage_health
from application.service import ApplicationService
from core.config import AppSettings
from transfers.errors import Category
from transfers.storage import (
    DiskCapacity,
    StorageDomain,
    StorageHealthError,
    StorageReason,
    StorageState,
)


class _FaultingHandle:
    def __init__(self, wrapped, *, write_error=None, flush_error=None, close_error=None):
        self._wrapped = wrapped
        self._write_error = write_error
        self._flush_error = flush_error
        self._close_error = close_error

    def write(self, payload):
        if self._write_error is not None:
            raise self._write_error
        return self._wrapped.write(payload)

    def flush(self):
        if self._flush_error is not None:
            raise self._flush_error
        return self._wrapped.flush()

    def fileno(self):
        return self._wrapped.fileno()

    def close(self):
        self._wrapped.close()
        if self._close_error is not None:
            raise self._close_error


def _capacity(tmp_path, *, minimum_gb=0, hysteresis_gb=0.5):
    app_dir = tmp_path / "app"
    download_dir = tmp_path / "download"
    app_dir.mkdir()
    download_dir.mkdir()
    capacity = DiskCapacity(
        download_dir,
        minimum_gb=minimum_gb,
        hysteresis_gb=hysteresis_gb,
        application_path=app_dir / "debridpulse.db",
    )
    return capacity, app_dir, download_dir


def _fault_handle(monkeypatch, *, write_error=None, flush_error=None, close_error=None):
    original = storage.os.fdopen

    def factory(fd, *args, **kwargs):
        return _FaultingHandle(
            original(fd, *args, **kwargs),
            write_error=write_error,
            flush_error=flush_error,
            close_error=close_error,
        )

    monkeypatch.setattr(storage.os, "fdopen", factory)


def _service(capacity, *, references=False):
    repository = SimpleNamespace(has_integration_references=AsyncMock(return_value=references))
    engine = SimpleNamespace(repository=repository, dispatch_permitted=True)
    return ApplicationService(engine, capacity=capacity), repository, engine


def test_authoritative_probe_success_leaves_no_artifact_and_exposes_evidence(tmp_path):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    sentinel = download_dir / ".debridpulse-probe-owned"
    sentinel.write_text("user-data")

    snapshot = capacity.validate_download_path(download_dir)

    assert snapshot.state == StorageState.HEALTHY
    assert snapshot.exists is True
    assert snapshot.is_directory is True
    assert snapshot.accessible is True
    assert snapshot.writable is True
    assert snapshot.fsync_supported in {True, False}
    assert snapshot.total_bytes is not None
    assert snapshot.free_bytes is not None
    assert snapshot.filesystem_id is not None
    assert sentinel.read_text() == "user-data"
    assert sorted(path.name for path in download_dir.iterdir()) == [sentinel.name]


def test_candidate_probe_is_detached_from_active_health(tmp_path):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    capacity.check()
    active = capacity.snapshot(StorageDomain.DOWNLOAD)
    bad = tmp_path / "missing"

    candidate = capacity.validate_download_path(bad)

    assert candidate.state == StorageState.UNAVAILABLE
    assert candidate.reason == StorageReason.MISSING
    after = capacity.snapshot(StorageDomain.DOWNLOAD)
    assert after.state == StorageState.HEALTHY
    assert after.configured_path == active.configured_path == str(download_dir)
    assert after.generation == active.generation


def test_active_candidate_can_refresh_canonical_health(tmp_path, monkeypatch):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    capacity.check()
    original = storage.tempfile.mkstemp
    monkeypatch.setattr(storage.tempfile, "mkstemp", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EROFS, "ro")))

    snapshot = capacity.validate_download_path(download_dir, apply_if_active=True)
    assert snapshot.state == StorageState.READ_ONLY
    assert capacity.snapshot(StorageDomain.DOWNLOAD).state == StorageState.READ_ONLY

    monkeypatch.setattr(storage.tempfile, "mkstemp", original)
    recovered = capacity.validate_download_path(download_dir, apply_if_active=True)
    assert recovered.state == StorageState.HEALTHY


def test_missing_and_non_directory_candidates_are_rejected(tmp_path):
    capacity, _app_dir, _download_dir = _capacity(tmp_path)
    missing = capacity.validate_download_path(tmp_path / "missing")
    target_file = tmp_path / "regular"
    target_file.write_text("x")
    regular = capacity.validate_download_path(target_file)

    assert (missing.state, missing.reason, missing.exists) == (
        StorageState.UNAVAILABLE, StorageReason.MISSING, False
    )
    assert (regular.state, regular.reason, regular.is_directory) == (
        StorageState.UNAVAILABLE, StorageReason.INVALID_PATH, False
    )


def test_real_traversal_failure_is_unavailable(tmp_path, monkeypatch):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    original = storage.os.scandir

    def denied(path):
        if str(path) == str(download_dir.resolve()):
            raise OSError(errno.EACCES, "denied")
        return original(path)

    monkeypatch.setattr(storage.os, "scandir", denied)
    snapshot = capacity.validate_download_path(download_dir)
    assert snapshot.state == StorageState.UNAVAILABLE
    assert snapshot.reason == StorageReason.INACCESSIBLE
    assert snapshot.accessible is False


@pytest.mark.parametrize(
    ("code", "expected_state", "expected_reason"),
    [
        (errno.ENOSPC, StorageState.FULL, StorageReason.CAPACITY_EXHAUSTED),
        (errno.EROFS, StorageState.READ_ONLY, StorageReason.READ_ONLY),
        (errno.EIO, StorageState.UNAVAILABLE, StorageReason.IO_ERROR),
    ],
)
def test_creation_faults_use_canonical_classifier(tmp_path, monkeypatch, code, expected_state, expected_reason):
    capacity, _app_dir, download_dir = _capacity(tmp_path)

    def fail(*_args, **_kwargs):
        raise OSError(code, "injected")

    monkeypatch.setattr(storage.tempfile, "mkstemp", fail)
    snapshot = capacity.validate_download_path(download_dir)
    assert snapshot.state == expected_state
    assert snapshot.reason == expected_reason
    assert snapshot.writable is False


def test_creation_edquot_is_full_when_platform_exposes_edquot(tmp_path, monkeypatch):
    edquot = getattr(errno, "EDQUOT", None)
    if edquot is None:
        pytest.skip("EDQUOT is unavailable on this platform")
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    monkeypatch.setattr(
        storage.tempfile,
        "mkstemp",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError(edquot, "quota")),
    )
    snapshot = capacity.validate_download_path(download_dir)
    assert snapshot.state == StorageState.FULL
    assert snapshot.reason == StorageReason.QUOTA_EXHAUSTED


@pytest.mark.parametrize(
    ("code", "expected_state"),
    [
        (errno.ENOSPC, StorageState.FULL),
        (errno.EROFS, StorageState.READ_ONLY),
        (errno.EIO, StorageState.UNAVAILABLE),
    ],
)
def test_write_faults_are_authoritative_and_cleanup_runs(tmp_path, monkeypatch, code, expected_state):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    _fault_handle(monkeypatch, write_error=OSError(code, "write failed"))

    snapshot = capacity.validate_download_path(download_dir)

    assert snapshot.state == expected_state
    assert snapshot.writable is False
    assert not list(download_dir.glob(".debridpulse-probe-*"))


def test_write_edquot_is_full(tmp_path, monkeypatch):
    edquot = getattr(errno, "EDQUOT", None)
    if edquot is None:
        pytest.skip("EDQUOT is unavailable on this platform")
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    _fault_handle(monkeypatch, write_error=OSError(edquot, "quota"))
    snapshot = capacity.validate_download_path(download_dir)
    assert snapshot.state == StorageState.FULL
    assert snapshot.reason == StorageReason.QUOTA_EXHAUSTED


@pytest.mark.parametrize(
    ("code", "expected_state"),
    [
        (errno.ENOSPC, StorageState.FULL),
        (errno.EROFS, StorageState.READ_ONLY),
        (errno.EIO, StorageState.UNAVAILABLE),
    ],
)
def test_flush_faults_are_authoritative(tmp_path, monkeypatch, code, expected_state):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    _fault_handle(monkeypatch, flush_error=OSError(code, "flush failed"))
    snapshot = capacity.validate_download_path(download_dir)
    assert snapshot.state == expected_state
    assert not list(download_dir.glob(".debridpulse-probe-*"))


@pytest.mark.parametrize(
    ("code", "expected_state"),
    [
        (errno.ENOSPC, StorageState.FULL),
        (errno.EROFS, StorageState.READ_ONLY),
        (errno.EIO, StorageState.UNAVAILABLE),
    ],
)
def test_fsync_faults_are_authoritative(tmp_path, monkeypatch, code, expected_state):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    monkeypatch.setattr(storage.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError(code, "fsync failed")))
    snapshot = capacity.validate_download_path(download_dir)
    assert snapshot.state == expected_state
    assert snapshot.writable is False
    assert not list(download_dir.glob(".debridpulse-probe-*"))


def test_fsync_edquot_is_full(tmp_path, monkeypatch):
    edquot = getattr(errno, "EDQUOT", None)
    if edquot is None:
        pytest.skip("EDQUOT is unavailable on this platform")
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    monkeypatch.setattr(storage.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError(edquot, "quota")))
    snapshot = capacity.validate_download_path(download_dir)
    assert snapshot.state == StorageState.FULL
    assert snapshot.reason == StorageReason.QUOTA_EXHAUSTED


def test_explicitly_unsupported_fsync_is_narrowly_accepted(tmp_path, monkeypatch):
    unsupported = getattr(errno, "ENOTSUP", getattr(errno, "EOPNOTSUPP", None))
    if unsupported is None:
        pytest.skip("No supported-operation errno is exposed")
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    monkeypatch.setattr(storage.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError(unsupported, "unsupported")))
    snapshot = capacity.validate_download_path(download_dir)
    assert snapshot.state == StorageState.HEALTHY
    assert snapshot.writable is True
    assert snapshot.fsync_supported is False


def test_close_failure_after_success_is_validation_failure(tmp_path, monkeypatch):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    _fault_handle(monkeypatch, close_error=OSError(errno.EIO, "close failed"))
    snapshot = capacity.validate_download_path(download_dir)
    assert snapshot.state == StorageState.UNAVAILABLE
    assert snapshot.reason == StorageReason.IO_ERROR
    assert not list(download_dir.glob(".debridpulse-probe-*"))


def test_capacity_stat_failure_is_unavailable_and_cleanup_runs(tmp_path, monkeypatch):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    original = storage.shutil.disk_usage

    def fail(path):
        if str(path) == str(download_dir.resolve()):
            raise OSError(errno.EIO, "capacity failed")
        return original(path)

    monkeypatch.setattr(storage.shutil, "disk_usage", fail)
    snapshot = capacity.validate_download_path(download_dir)
    assert snapshot.state == StorageState.UNAVAILABLE
    assert snapshot.reason == StorageReason.IO_ERROR
    assert snapshot.total_bytes is None
    assert snapshot.free_bytes is None
    assert not list(download_dir.glob(".debridpulse-probe-*"))


def test_unclassified_capacity_stat_failure_uses_stat_failed(tmp_path, monkeypatch):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    monkeypatch.setattr(
        storage.shutil,
        "disk_usage",
        lambda _path: (_ for _ in ()).throw(OSError(getattr(errno, "EBUSY", 16), "busy")),
    )
    snapshot = capacity.validate_download_path(download_dir)
    assert snapshot.state == StorageState.UNAVAILABLE
    assert snapshot.reason == StorageReason.STAT_FAILED


def test_zero_reported_capacity_is_full(tmp_path, monkeypatch):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    usage = storage.shutil._ntuple_diskusage(total=100, used=100, free=0)
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda _path: usage)
    snapshot = capacity.validate_download_path(download_dir)
    assert snapshot.state == StorageState.FULL
    assert snapshot.reason == StorageReason.CAPACITY_EXHAUSTED


def test_cleanup_failure_after_success_is_not_reported_healthy(tmp_path, monkeypatch):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    original = storage.os.unlink

    def fail_after_remove(path):
        original(path)
        raise OSError(errno.EROFS, "cleanup failed")

    monkeypatch.setattr(storage.os, "unlink", fail_after_remove)
    snapshot = capacity.validate_download_path(download_dir)
    assert snapshot.state == StorageState.READ_ONLY
    assert snapshot.writable is False
    assert not list(download_dir.glob(".debridpulse-probe-*"))


def test_cleanup_failure_does_not_mask_primary_fault(tmp_path, monkeypatch):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    _fault_handle(monkeypatch, write_error=OSError(errno.ENOSPC, "primary"))
    original_unlink = storage.os.unlink

    def secondary(path):
        original_unlink(path)
        raise OSError(errno.EROFS, "cleanup")

    monkeypatch.setattr(storage.os, "unlink", secondary)
    snapshot = capacity.validate_download_path(download_dir)
    assert snapshot.state == StorageState.FULL
    assert snapshot.reason == StorageReason.CAPACITY_EXHAUSTED
    assert not list(download_dir.glob(".debridpulse-probe-*"))


def test_generic_eio_with_capacity_evidence_remains_unavailable(tmp_path, monkeypatch):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    _fault_handle(monkeypatch, write_error=OSError(errno.EIO, "io"))
    snapshot = capacity.validate_download_path(download_dir)
    assert snapshot.free_bytes is not None and snapshot.free_bytes > 0
    assert snapshot.state == StorageState.UNAVAILABLE
    assert snapshot.state != StorageState.FULL


def test_zero_low_space_threshold_does_not_disable_hard_probe_faults(tmp_path, monkeypatch):
    capacity, _app_dir, download_dir = _capacity(tmp_path, minimum_gb=0)
    monkeypatch.setattr(
        storage.tempfile,
        "mkstemp",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError(errno.ENOSPC, "full")),
    )
    snapshot = capacity.validate_download_path(download_dir)
    assert snapshot.state == StorageState.FULL


def test_authoritative_probe_preserves_low_space_hysteresis(tmp_path, monkeypatch):
    capacity, _app_dir, download_dir = _capacity(tmp_path, minimum_gb=1, hysteresis_gb=0.5)
    gib = 1024 ** 3
    free = {"value": int(0.9 * gib)}
    monkeypatch.setattr(
        storage.shutil,
        "disk_usage",
        lambda _path: storage.shutil._ntuple_diskusage(total=10 * gib, used=9 * gib, free=free["value"]),
    )

    first = capacity.probe(StorageDomain.DOWNLOAD)
    assert first.state == StorageState.LOW_SPACE
    free["value"] = int(1.2 * gib)
    middle = capacity.probe(StorageDomain.DOWNLOAD)
    assert middle.state == StorageState.LOW_SPACE
    free["value"] = int(1.6 * gib)
    recovered = capacity.probe(StorageDomain.DOWNLOAD)
    assert recovered.state == StorageState.HEALTHY


def test_runtime_path_disappearance_and_recovery_need_no_restart(tmp_path):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    assert capacity.check()["download"]["state"] == "healthy"
    download_dir.rmdir()
    failed = capacity.probe(StorageDomain.DOWNLOAD)
    assert failed.state == StorageState.UNAVAILABLE
    download_dir.mkdir()
    recovered = capacity.probe(StorageDomain.DOWNLOAD)
    assert recovered.state == StorageState.HEALTHY


@pytest.mark.parametrize(
    ("code", "state"),
    [
        (errno.EROFS, StorageState.READ_ONLY),
        (errno.ENOSPC, StorageState.FULL),
        (errno.EIO, StorageState.UNAVAILABLE),
    ],
)
def test_runtime_authoritative_fault_and_recovery(tmp_path, monkeypatch, code, state):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    capacity.check()
    original = storage.tempfile.mkstemp
    monkeypatch.setattr(
        storage.tempfile,
        "mkstemp",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError(code, "injected")),
    )
    failed = capacity.probe(StorageDomain.DOWNLOAD)
    assert failed.state == state
    generation = failed.generation
    repeated = capacity.probe(StorageDomain.DOWNLOAD)
    assert repeated.generation == generation

    monkeypatch.setattr(storage.tempfile, "mkstemp", original)
    recovered = capacity.probe(StorageDomain.DOWNLOAD)
    assert recovered.state == StorageState.HEALTHY
    assert recovered.generation == generation + 1


@pytest.mark.asyncio
async def test_runtime_check_resources_contains_and_releases_dispatch(tmp_path, monkeypatch):
    capacity, _app_dir, _download_dir = _capacity(tmp_path)
    service, _repository, engine = _service(capacity)
    await service.check_resources()
    assert engine.dispatch_permitted is True

    original = storage.tempfile.mkstemp
    monkeypatch.setattr(
        storage.tempfile,
        "mkstemp",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError(errno.EROFS, "ro")),
    )
    degraded = await service.check_resources()
    assert degraded["download"]["state"] == "read_only"
    assert engine.dispatch_permitted is False

    monkeypatch.setattr(storage.tempfile, "mkstemp", original)
    healthy = await service.check_resources()
    assert healthy["download"]["state"] == "healthy"
    assert engine.dispatch_permitted is True


@pytest.mark.asyncio
async def test_bad_download_storage_does_not_prevent_health_service_startup_path(tmp_path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    missing = tmp_path / "missing-download"
    capacity = DiskCapacity(missing, application_path=app_dir / "debridpulse.db")
    service, _repository, engine = _service(capacity)

    result = await storage_health(service)

    assert result["application_state"]["state"] == "healthy"
    assert result["download"]["state"] == "unavailable"
    assert engine.dispatch_permitted is False


@pytest.mark.asyncio
async def test_settings_valid_candidate_passes_authoritative_validation(tmp_path):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    capacity.check()
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    service, repository, _engine = _service(capacity, references=False)
    previous = AppSettings(download_folder=str(download_dir))
    current = previous.model_copy(update={"download_folder": str(candidate)})

    await service.validate_configuration(previous, current)

    repository.has_integration_references.assert_awaited_once_with()
    assert capacity.snapshot(StorageDomain.DOWNLOAD).configured_path == str(download_dir)
    assert capacity.snapshot(StorageDomain.DOWNLOAD).state == StorageState.HEALTHY
    assert not list(candidate.glob(".debridpulse-probe-*"))


@pytest.mark.asyncio
async def test_settings_missing_candidate_rejected_without_poisoning_active_state(tmp_path):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    capacity.check()
    active = capacity.snapshot(StorageDomain.DOWNLOAD)
    service, _repository, _engine = _service(capacity, references=False)
    previous = AppSettings(download_folder=str(download_dir))
    current = previous.model_copy(update={"download_folder": str(tmp_path / "missing")})

    with pytest.raises(StorageHealthError) as caught:
        await service.validate_configuration(previous, current)

    assert caught.value.status_code == 503
    assert caught.value.error.category == Category.DOWNLOAD_STORAGE_UNAVAILABLE
    after = capacity.snapshot(StorageDomain.DOWNLOAD)
    assert after.configured_path == active.configured_path
    assert after.state == StorageState.HEALTHY
    assert after.generation == active.generation


@pytest.mark.asyncio
async def test_settings_full_candidate_returns_507_semantics_without_active_mutation(tmp_path, monkeypatch):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    capacity.check()
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    original = storage.tempfile.mkstemp

    def fail(path_prefix=None, *args, **kwargs):
        target = kwargs.get("dir")
        if target == str(candidate.resolve()):
            raise OSError(errno.ENOSPC, "full")
        return original(path_prefix, *args, **kwargs)

    monkeypatch.setattr(storage.tempfile, "mkstemp", fail)
    service, _repository, _engine = _service(capacity, references=False)
    previous = AppSettings(download_folder=str(download_dir))
    current = previous.model_copy(update={"download_folder": str(candidate)})

    with pytest.raises(StorageHealthError) as caught:
        await service.validate_configuration(previous, current)

    assert caught.value.status_code == 507
    assert caught.value.error.category == Category.DOWNLOAD_STORAGE_FULL
    assert capacity.snapshot(StorageDomain.DOWNLOAD).state == StorageState.HEALTHY


@pytest.mark.asyncio
async def test_settings_read_only_candidate_returns_503_semantics(tmp_path, monkeypatch):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    original = storage.tempfile.mkstemp

    def fail(*args, **kwargs):
        if kwargs.get("dir") == str(candidate.resolve()):
            raise OSError(errno.EROFS, "read only")
        return original(*args, **kwargs)

    monkeypatch.setattr(storage.tempfile, "mkstemp", fail)
    service, _repository, _engine = _service(capacity, references=False)
    previous = AppSettings(download_folder=str(download_dir))
    current = previous.model_copy(update={"download_folder": str(candidate)})

    with pytest.raises(StorageHealthError) as caught:
        await service.validate_configuration(previous, current)

    assert caught.value.status_code == 503
    assert caught.value.error.category == Category.DOWNLOAD_STORAGE_READ_ONLY


@pytest.mark.asyncio
async def test_settings_active_resource_guard_precedes_candidate_probe(tmp_path):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    service, repository, _engine = _service(capacity, references=True)
    capacity.require_download_path = Mock(wraps=capacity.require_download_path)
    previous = AppSettings(download_folder=str(download_dir))
    current = previous.model_copy(update={"download_folder": str(tmp_path / "missing")})

    with pytest.raises(ValueError, match="Finish or remove existing resources"):
        await service.validate_configuration(previous, current)

    repository.has_integration_references.assert_awaited_once_with()
    capacity.require_download_path.assert_not_called()


@pytest.mark.asyncio
async def test_settings_same_active_path_fault_updates_canonical_health(tmp_path, monkeypatch):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    capacity.check()
    monkeypatch.setattr(
        storage.tempfile,
        "mkstemp",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError(errno.EIO, "io")),
    )
    service, repository, _engine = _service(capacity, references=True)
    current = AppSettings(download_folder=str(download_dir))

    with pytest.raises(StorageHealthError):
        await service.validate_configuration(current, current)

    # Unchanged path does not invoke the unsafe-change guard, but authoritative
    # revalidation still records a real fault in the currently active domain.
    repository.has_integration_references.assert_not_awaited()
    assert capacity.snapshot(StorageDomain.DOWNLOAD).state == StorageState.UNAVAILABLE


def test_download_path_local_failure_does_not_mirror_into_application_state(tmp_path):
    capacity, _app_dir, download_dir = _capacity(tmp_path)
    capacity.check()
    assert capacity.snapshot(StorageDomain.APPLICATION_STATE).state == StorageState.HEALTHY
    download_dir.rmdir()

    capacity.probe(StorageDomain.DOWNLOAD)

    assert capacity.snapshot(StorageDomain.DOWNLOAD).state == StorageState.UNAVAILABLE
    assert capacity.snapshot(StorageDomain.APPLICATION_STATE).state == StorageState.HEALTHY


def test_successful_new_candidate_can_be_activated_without_restarting_capacity_owner(tmp_path):
    capacity, app_dir, download_dir = _capacity(tmp_path)
    capacity.check()
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    detached = capacity.require_download_path(replacement)
    assert detached.state == StorageState.HEALTHY
    assert capacity.snapshot(StorageDomain.DOWNLOAD).configured_path == str(download_dir)

    capacity.configure(replacement, application_path=app_dir / "debridpulse.db")
    health = capacity.check()

    assert health["download"]["configured_path"] == str(replacement)
    assert health["download"]["state"] == "healthy"
    assert health["download"]["writable"] is True
