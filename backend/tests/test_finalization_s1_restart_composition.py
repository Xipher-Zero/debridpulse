"""Finalization S1 restart-composition exit gates."""
from __future__ import annotations

import errno
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import core.config as config
import db.database as database
import transfers.storage as storage
from api.settings_validation_routes import _browse_directory
from application.service import ApplicationService
from core.config import AppSettings
from fake_applicability_provider import RuntimeClaimProvider, SpecializedFixtureProvider
from integrations.runtime_state import ProviderRuntimeStateStore
from providers.general_http.provider import GeneralHttpProvider
from transfers.applicability import ApplicabilityReadiness, HostClaimScope, ProviderApplicability
from transfers.engine import TransferEngine
from transfers.models import TransferRequest
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository
from transfers.storage import DiskCapacity


URL = "https://shared.example/file.bin"


class CountingGeneralHttpProvider(GeneralHttpProvider):
    def __init__(self):
        self.calls = 0

    async def resolve(self, request):
        self.calls += 1
        return await super().resolve(request)


def _make_download_read_only(monkeypatch, download_root):
    original = storage.shutil.disk_usage
    root = Path(download_root)

    def failing_usage(path):
        if Path(path) == root:
            raise OSError(errno.EROFS, "download storage read only")
        return original(path)

    monkeypatch.setattr(storage.shutil, "disk_usage", failing_usage)
    return original


def _engine(repository, registry, download_root):
    return TransferEngine(
        repository,
        registry,
        download_root=str(download_root),
        policy=TransferPolicy(adoption_stability_seconds=0),
    )


@pytest.mark.asyncio
async def test_browse_confirm_authoritative_save_persists_and_restart_reconstructs_same_download_truth(
    tmp_path, monkeypatch
):
    config_dir = tmp_path / "config"
    app_dir = tmp_path / "app"
    old_download = tmp_path / "old-download"
    selected = tmp_path / "selected-download"
    for directory in (config_dir, app_dir, old_download, selected):
        directory.mkdir()
    monkeypatch.setattr(config, "CONFIG_PATH", config_dir / "config.json")

    capacity = DiskCapacity(old_download, application_path=app_dir / "debridpulse.db")
    capacity.check()

    browsed = _browse_directory(selected, capacity)
    assert browsed.current.path == str(selected.resolve())
    assert browsed.current.selectable is True
    assert browsed.current.writable is True

    repository = SimpleNamespace(
        has_integration_references=AsyncMock(return_value=False)
    )
    engine = SimpleNamespace(repository=repository, dispatch_permitted=True)
    service = ApplicationService(engine, capacity=capacity)
    previous = AppSettings(download_folder=str(old_download))
    current = previous.model_copy(update={"download_folder": str(selected)})
    prior_settings = config.get_settings()

    try:
        config.apply_settings(previous)
        await service.validate_configuration(previous, current)
        repository.has_integration_references.assert_awaited_once_with()

        config.save_settings(current)
        config.apply_settings(current)

        # Simulate a new process rather than carrying the in-memory selection.
        config.apply_settings(AppSettings())
        reloaded = config.load_settings()
        assert Path(reloaded.download_folder).resolve() == selected.resolve()

        restarted_capacity = DiskCapacity(
            reloaded.download_folder,
            application_path=app_dir / "debridpulse.db",
        )
        health = restarted_capacity.check()
        restarted_browse = _browse_directory(
            Path(reloaded.download_folder), restarted_capacity
        )

        assert health["download"]["state"] == "healthy"
        assert Path(health["download"]["configured_path"]).resolve() == selected.resolve()
        assert Path(health["download"]["resolved_path"]).resolve() == selected.resolve()
        assert restarted_browse.current.path == str(selected.resolve())
        assert restarted_browse.current.selectable is True
        assert restarted_browse.current.capacity.total_bytes == health["download"]["total_bytes"]
        assert restarted_browse.current.capacity.free_bytes == health["download"]["free_bytes"]
    finally:
        config.apply_settings(prior_settings)


@pytest.mark.asyncio
async def test_restart_keeps_unresolved_request_neutral_under_storage_fault_then_binds_once_ready(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "restart-unresolved.sqlite3"
    download_root = tmp_path / "downloads"
    download_root.mkdir()
    monkeypatch.setattr(database, "DB_PATH", db_path)
    await database.init_db()

    repository1 = TransferRepository()
    registry1 = IntegrationRegistry()
    specialized1 = SpecializedFixtureProvider(
        "specialized", host="shared.example", scope=HostClaimScope.EXACT
    )
    specialized1.applicability = ProviderApplicability(
        specialized=True,
        readiness=ApplicabilityReadiness.UNRESOLVED,
    )
    generic1 = CountingGeneralHttpProvider()
    registry1.register_provider(specialized1)
    registry1.register_provider(generic1)
    engine1 = _engine(repository1, registry1, download_root)
    await engine1.initialize()

    transfer = await engine1.submit(
        (TransferRequest("https", URL, name="fixture.bin"),), deduplicate=False
    )
    request_id = (await repository1.requests(transfer.id))[0].id
    await engine1.resolve_pending()
    before = await repository1.presentation(transfer.id, details=True)
    assert await repository1.bound_route_provider(request_id) is None
    assert before["route_attempts"] == []
    assert before["execution_attempts"] == []
    assert generic1.calls == 0

    # First restart: the durable request remains unresolved while Download
    # Storage is independently degraded.
    repository2 = TransferRepository()
    registry2 = IntegrationRegistry()
    specialized2 = SpecializedFixtureProvider(
        "specialized", host="shared.example", scope=HostClaimScope.EXACT
    )
    ready = replace(
        specialized2.applicability,
        specialized=True,
        readiness=ApplicabilityReadiness.READY,
    )
    specialized2.applicability = ProviderApplicability(
        specialized=True,
        readiness=ApplicabilityReadiness.UNRESOLVED,
    )
    generic2 = CountingGeneralHttpProvider()
    registry2.register_provider(specialized2)
    registry2.register_provider(generic2)
    engine2 = _engine(repository2, registry2, download_root)
    await engine2.initialize()

    _make_download_read_only(monkeypatch, download_root)
    capacity2 = DiskCapacity(download_root, application_path=db_path)
    application2 = ApplicationService(engine2, capacity=capacity2)
    health2 = await application2.check_resources()
    assert health2["download"]["state"] == "read_only"
    assert engine2.dispatch_permitted is False

    restarted_record = (await repository2.requests(transfer.id))[0]
    assert restarted_record.id == request_id
    await application2.resolve_pending()
    unresolved = await repository2.presentation(transfer.id, details=True)
    assert await repository2.bound_route_provider(request_id) is None
    assert unresolved["route_attempts"] == []
    assert unresolved["execution_attempts"] == []
    assert generic2.calls == 0

    specialized2.applicability = ready
    await application2.resolve_pending()
    bound = await repository2.presentation(transfer.id, details=True)
    assert await repository2.bound_route_provider(request_id) == "specialized"
    assert bound["route_attempts"][0]["provider_id"] == "specialized"
    assert bound["execution_attempts"] == []
    assert generic2.calls == 0
    assert engine2.dispatch_permitted is False
    assert await repository2.globally_paused() is False

    # Second restart: the selected route remains durable even if applicability
    # becomes unresolved again while the storage fault is still present.
    repository3 = TransferRepository()
    registry3 = IntegrationRegistry()
    specialized3 = SpecializedFixtureProvider(
        "specialized", host="shared.example", scope=HostClaimScope.EXACT
    )
    specialized3.applicability = ProviderApplicability(
        specialized=True,
        readiness=ApplicabilityReadiness.UNRESOLVED,
    )
    generic3 = CountingGeneralHttpProvider()
    registry3.register_provider(specialized3)
    registry3.register_provider(generic3)
    engine3 = _engine(repository3, registry3, download_root)
    await engine3.initialize()
    capacity3 = DiskCapacity(download_root, application_path=db_path)
    application3 = ApplicationService(engine3, capacity=capacity3)
    health3 = await application3.check_resources()
    assert health3["download"]["state"] == "read_only"
    assert engine3.dispatch_permitted is False
    assert await repository3.bound_route_provider(request_id) == "specialized"

    before_second_resolution = await repository3.presentation(transfer.id, details=True)
    await application3.resolve_pending()
    after_second_resolution = await repository3.presentation(transfer.id, details=True)
    assert await repository3.bound_route_provider(request_id) == "specialized"
    assert after_second_resolution["route_attempts"] == before_second_resolution["route_attempts"]
    assert after_second_resolution["execution_attempts"] == []
    assert generic3.calls == 0
    assert await repository3.globally_paused() is False


@pytest.mark.asyncio
async def test_restart_rehydrates_persisted_ready_applicability_while_download_execution_stays_blocked(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "restart-ready.sqlite3"
    download_root = tmp_path / "ready-downloads"
    download_root.mkdir()
    monkeypatch.setattr(database, "DB_PATH", db_path)
    await database.init_db()

    store1 = ProviderRuntimeStateStore()
    await store1.start()
    writer = RuntimeClaimProvider(store1, "persisted-specialized")
    await writer.retain(
        ["shared.example"],
        observed_at=1000.0,
        stale_after=2000.0,
    )

    # A fresh store/provider instance reconstructs routing truth only from the
    # persisted neutral runtime state.
    store2 = ProviderRuntimeStateStore()
    await store2.start()
    specialized = RuntimeClaimProvider(store2, "persisted-specialized")
    await specialized.refresh_applicability(now=1001.0)
    assert specialized.applicability.specialized_hosts

    repository = TransferRepository()
    registry = IntegrationRegistry()
    generic = CountingGeneralHttpProvider()
    registry.register_provider(specialized)
    registry.register_provider(generic)
    engine = _engine(repository, registry, download_root)
    await engine.initialize()

    _make_download_read_only(monkeypatch, download_root)
    capacity = DiskCapacity(download_root, application_path=db_path)
    application = ApplicationService(engine, capacity=capacity)
    health = await application.check_resources()
    assert health["download"]["state"] == "read_only"
    assert engine.dispatch_permitted is False

    transfer = await engine.submit(
        (TransferRequest("https", URL, name="fixture.bin"),), deduplicate=False
    )
    request_id = (await repository.requests(transfer.id))[0].id
    await application.resolve_pending()
    presentation = await repository.presentation(transfer.id, details=True)

    assert await repository.bound_route_provider(request_id) == "persisted-specialized"
    assert presentation["route_attempts"][0]["provider_id"] == "persisted-specialized"
    assert presentation["execution_attempts"] == []
    assert len(specialized.calls) == 1
    assert generic.calls == 0
    assert engine.dispatch_permitted is False
    assert await repository.globally_paused() is False
