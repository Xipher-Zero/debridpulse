"""Finalization S1 cross-workstream adversarial regression contracts."""
from __future__ import annotations

import errno
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import db.database as database
import transfers.storage as storage
from api.settings_validation_routes import _browse_directory
from application.service import ApplicationService
from fake_applicability_provider import SpecializedFixtureProvider
from providers.general_http.provider import GeneralHttpProvider
from transfers.applicability import ApplicabilityReadiness, HostClaimScope, ProviderApplicability
from transfers.engine import TransferEngine
from transfers.models import TransferRequest
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository
from transfers.storage import DiskCapacity, StorageDomain

GIB = 1024 ** 3
URL = "https://shared.example/file.bin"


class CountingGeneralHttpProvider(GeneralHttpProvider):
    def __init__(self):
        self.calls = 0

    async def resolve(self, request):
        self.calls += 1
        return await super().resolve(request)


def _usage(*, total_gb, free_gb):
    total = int(total_gb * GIB)
    free = int(free_gb * GIB)
    return SimpleNamespace(total=total, used=total - free, free=free)


def test_wrong_but_writable_small_filesystem_has_truthful_browser_and_active_capacity(tmp_path, monkeypatch):
    app = tmp_path / "app"
    download = tmp_path / "download"
    app.mkdir()
    download.mkdir()
    capacity = DiskCapacity(
        download,
        1,
        application_path=app / "debridpulse.db",
    )

    def fake_usage(path):
        path = Path(path)
        if path == download:
            return _usage(total_gb=5, free_gb=0.5)
        if path == app:
            return _usage(total_gb=100, free_gb=50)
        raise AssertionError(f"unexpected disk path: {path}")

    monkeypatch.setattr(storage.shutil, "disk_usage", fake_usage)
    monkeypatch.setattr(
        capacity,
        "_filesystem_identity",
        lambda path: "download-fs" if Path(path) == download else "application-fs",
    )

    browsed = _browse_directory(download, capacity)
    assert browsed.current.path == str(download)
    assert browsed.current.selectable is True
    assert browsed.current.capacity.total_bytes == 5 * GIB
    assert browsed.current.capacity.free_bytes == int(0.5 * GIB)

    health = capacity.check()
    assert health["download"]["state"] == "low_space"
    assert health["download"]["total_bytes"] == browsed.current.capacity.total_bytes
    assert health["download"]["free_bytes"] == browsed.current.capacity.free_bytes
    assert health["application_state"]["state"] == "healthy"
    assert health["shared_filesystem"] is False


async def _routing_runtime(tmp_path, monkeypatch, name):
    db_path = tmp_path / name
    monkeypatch.setattr(database, "DB_PATH", db_path)
    await database.init_db()

    download_root = tmp_path / f"{name}-downloads"
    download_root.mkdir()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    specialized = SpecializedFixtureProvider(
        "specialized",
        host="shared.example",
        scope=HostClaimScope.EXACT,
    )
    ready = replace(
        specialized.applicability,
        specialized=True,
        readiness=ApplicabilityReadiness.READY,
    )
    specialized.applicability = ProviderApplicability(
        specialized=True,
        readiness=ApplicabilityReadiness.UNRESOLVED,
    )
    generic = CountingGeneralHttpProvider()
    registry.register_provider(specialized)
    registry.register_provider(generic)
    engine = TransferEngine(
        repository,
        registry,
        download_root=str(download_root),
        policy=TransferPolicy(adoption_stability_seconds=0),
    )
    await engine.initialize()
    capacity = DiskCapacity(download_root, application_path=db_path)
    capacity.check()
    application = ApplicationService(engine, capacity=capacity)
    return application, engine, repository, specialized, generic, ready, download_root


def _make_download_unavailable(monkeypatch, download_root):
    original = storage.shutil.disk_usage

    def failing_usage(path):
        if Path(path) == download_root:
            raise OSError(errno.EROFS, "download storage read only")
        return original(path)

    monkeypatch.setattr(storage.shutil, "disk_usage", failing_usage)
    return original


def _request():
    return TransferRequest("https", URL, name="fixture.bin")


@pytest.mark.asyncio
async def test_readiness_first_binds_route_while_download_storage_still_blocks_execution(tmp_path, monkeypatch):
    application, engine, repository, specialized, generic, ready, download_root = await _routing_runtime(
        tmp_path, monkeypatch, "readiness-first.sqlite3"
    )
    original_disk_usage = _make_download_unavailable(monkeypatch, download_root)
    health = await application.check_resources()
    assert health["download"]["state"] == "read_only"
    assert engine.dispatch_permitted is False

    transfer = await engine.submit((_request(),), deduplicate=False)
    request_id = (await repository.requests(transfer.id))[0].id
    await application.resolve_pending()
    pending = (await repository.requests(transfer.id))[0]
    assert pending.id == request_id
    assert pending.state == "pending"
    assert await repository.bound_route_provider(request_id) is None
    assert generic.calls == 0

    specialized.applicability = ready
    await application.resolve_pending()
    presentation = await repository.presentation(transfer.id, details=True)
    assert await repository.bound_route_provider(request_id) == "specialized"
    assert presentation["route_attempts"][0]["provider_id"] == "specialized"
    assert presentation["execution_attempts"] == []
    assert generic.calls == 0
    assert engine.dispatch_permitted is False
    assert await repository.globally_paused() is False

    monkeypatch.setattr(storage.shutil, "disk_usage", original_disk_usage)
    recovered = await application.check_resources()
    assert recovered["download"]["state"] == "healthy"
    assert engine.dispatch_permitted is True
    assert await repository.bound_route_provider(request_id) == "specialized"


@pytest.mark.asyncio
async def test_storage_first_then_readiness_selects_same_provider_without_phantom_history(tmp_path, monkeypatch):
    application, engine, repository, specialized, generic, ready, download_root = await _routing_runtime(
        tmp_path, monkeypatch, "storage-first.sqlite3"
    )
    original_disk_usage = _make_download_unavailable(monkeypatch, download_root)
    await application.check_resources()

    transfer = await engine.submit((_request(),), deduplicate=False)
    request_id = (await repository.requests(transfer.id))[0].id
    await application.resolve_pending()
    before = await repository.presentation(transfer.id, details=True)
    assert before["route_attempts"] == []
    assert before["execution_attempts"] == []
    assert generic.calls == 0

    monkeypatch.setattr(storage.shutil, "disk_usage", original_disk_usage)
    recovered = await application.check_resources()
    assert recovered["download"]["state"] == "healthy"
    assert engine.dispatch_permitted is True
    await application.resolve_pending()
    assert await repository.bound_route_provider(request_id) is None
    assert generic.calls == 0

    specialized.applicability = ready
    await application.resolve_pending()
    after = await repository.presentation(transfer.id, details=True)
    assert await repository.bound_route_provider(request_id) == "specialized"
    assert after["route_attempts"][0]["provider_id"] == "specialized"
    assert generic.calls == 0
    assert await repository.globally_paused() is False


@pytest.mark.asyncio
async def test_storage_degradation_preserves_existing_route_and_provenance(tmp_path, monkeypatch):
    application, engine, repository, specialized, generic, ready, download_root = await _routing_runtime(
        tmp_path, monkeypatch, "provenance.sqlite3"
    )
    specialized.applicability = ready
    transfer = await engine.submit((_request(),), deduplicate=False)
    request_id = (await repository.requests(transfer.id))[0].id
    await application.resolve_pending()
    before = await repository.presentation(transfer.id, details=True)
    assert await repository.bound_route_provider(request_id) == "specialized"
    assert before["route_attempts"][0]["provider_id"] == "specialized"

    _make_download_unavailable(monkeypatch, download_root)
    health = await application.check_resources()
    after = await repository.presentation(transfer.id, details=True)
    assert health["download"]["state"] == "read_only"
    assert engine.dispatch_permitted is False
    assert await repository.bound_route_provider(request_id) == "specialized"
    assert after["route_attempts"] == before["route_attempts"]
    assert generic.calls == 0
    assert await repository.globally_paused() is False
