import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.asyncio
async def test_builtin_automatic_purge_preserves_bounded_result_state(monkeypatch):
    import services.aria2 as aria2_module

    monkeypatch.setattr(aria2_module, "_is_builtin_mode", lambda: True)
    service = aria2_module.Aria2Service("http://127.0.0.1:6800/jsonrpc")
    service._call = AsyncMock()

    result = await service.purge_download_results()

    assert result == {
        "skipped": True,
        "reason": "bounded built-in aria2 result state is operator-visible",
    }
    service._call.assert_not_awaited()


@pytest.mark.asyncio
async def test_builtin_explicit_force_purge_remains_available(monkeypatch):
    import services.aria2 as aria2_module

    monkeypatch.setattr(aria2_module, "_is_builtin_mode", lambda: True)
    service = aria2_module.Aria2Service("http://127.0.0.1:6800/jsonrpc")
    service._call = AsyncMock(return_value="ok")

    result = await service.purge_download_results(force=True)

    assert result == "ok"
    service._call.assert_awaited_once_with("aria2.purgeDownloadResult")


@pytest.mark.asyncio
async def test_external_force_purge_remains_blocked(monkeypatch):
    import services.aria2 as aria2_module

    monkeypatch.setattr(aria2_module, "_is_builtin_mode", lambda: False)
    service = aria2_module.Aria2Service("http://external.example/jsonrpc")
    service._call = AsyncMock()

    result = await service.purge_download_results(force=True)

    assert result == {
        "skipped": True,
        "reason": "external aria2 result history is daemon-owned",
    }
    service._call.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconciliation_preserves_inert_stopped_results(monkeypatch):
    import services.reconciliation_service as reconciliation_module

    monkeypatch.setattr(
        reconciliation_module,
        "get_settings",
        lambda: SimpleNamespace(paused=False),
    )

    engine = SimpleNamespace(
        download_client_name=Mock(return_value="aria2"),
        _aria2_state_lock=asyncio.Lock(),
        _engine_aria2_get_all=AsyncMock(return_value=[]),
        sync_aria2_downloads=AsyncMock(),
        _aria2_slot_limit=Mock(return_value=2),
        _schedule_ready_aria2_parents=AsyncMock(return_value=0),
        resume_deferred_provider_submissions=AsyncMock(),
        _cleanup_aria2_orphans=AsyncMock(),
    )
    repository = SimpleNamespace(
        has_unintended_paused_children=AsyncMock(return_value=False),
    )
    control = SimpleNamespace(
        ensure_initialized=AsyncMock(),
        pause_intents=set(),
        enforce_global_pause=AsyncMock(),
        enforce_selective_pauses=AsyncMock(),
        resume_unintended_paused=AsyncMock(return_value=0),
        confirm_gid=AsyncMock(return_value=None),
    )
    dispatch = SimpleNamespace(dispatch_queue=AsyncMock())
    ownership = SimpleNamespace(filter_owned=AsyncMock(return_value=[]))

    service = reconciliation_module.ReconciliationService(
        engine, repository, control, dispatch, ownership
    )
    await service.reconcile()

    engine.sync_aria2_downloads.assert_awaited_once()
    engine._cleanup_aria2_orphans.assert_not_awaited()
    engine.resume_deferred_provider_submissions.assert_awaited_once()


@pytest.mark.asyncio
async def test_builtin_housekeeping_reapplies_tuning_without_result_cleanup(monkeypatch):
    import services.aria2_gateway as gateway_module

    monkeypatch.setattr(gateway_module, "is_builtin_mode", lambda: True)
    diagnostics = {"stopped_count": 4}
    engine = SimpleNamespace(
        apply_aria2_memory_tuning=AsyncMock(return_value={"ok": True}),
        _aria2_get_memory_diagnostics=AsyncMock(return_value=diagnostics),
        run_aria2_housekeeping=AsyncMock(),
    )
    ownership = SimpleNamespace()
    gateway = gateway_module.Aria2Gateway(engine, ownership)

    result = await gateway.housekeeping()

    assert result == {
        "ok": True,
        "reason": "bounded built-in aria2 result state retained",
        "diagnostics": diagnostics,
    }
    engine.apply_aria2_memory_tuning.assert_awaited_once()
    engine._aria2_get_memory_diagnostics.assert_awaited_once()
    engine.run_aria2_housekeeping.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_housekeeping_remains_observation_only(monkeypatch):
    import services.aria2_gateway as gateway_module

    monkeypatch.setattr(gateway_module, "is_builtin_mode", lambda: False)
    diagnostics = {"stopped_count": 7}
    engine = SimpleNamespace(
        apply_aria2_memory_tuning=AsyncMock(),
        _aria2_get_memory_diagnostics=AsyncMock(return_value=diagnostics),
        run_aria2_housekeeping=AsyncMock(),
    )
    ownership = SimpleNamespace()
    gateway = gateway_module.Aria2Gateway(engine, ownership)

    result = await gateway.housekeeping()

    assert result == {
        "ok": True,
        "reason": "external aria2 history is daemon-owned",
        "diagnostics": diagnostics,
    }
    engine.apply_aria2_memory_tuning.assert_not_awaited()
    engine._aria2_get_memory_diagnostics.assert_awaited_once()
    engine.run_aria2_housekeeping.assert_not_awaited()
