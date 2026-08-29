from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def test_settings_aria2_live_runtime_contract():
    runtime = (STATIC / "ui-settings-aria2-live.js").read_text()
    styles = (STATIC / "ui-settings-aria2-live.css").read_text()
    loader = (STATIC / "ui-presentation-loader.js").read_text()

    order = runtime.split("const TAB_ORDER = Object.freeze([", 1)[1].split("]);", 1)[0]
    expected = [
        "'sources'",
        "'downloads'",
        "'extraction'",
        "'authentication'",
        "'notifications'",
        "'maintenance'",
    ]
    positions = [order.index(item) for item in expected]
    assert positions == sorted(positions)

    assert "/ui-settings-aria2-live.css?v=3" in loader
    assert "/ui-settings-aria2-live.js?v=5" in loader
    assert "Built-In Download Engine State" in runtime
    assert "Inspect and control the built-in aria2 engine." in runtime
    assert "This reflects temporary aria2 runtime state, not transfer history. DebridPulse Downloads remains the historical record." in runtime
    assert "Direct Engine Controls" in runtime
    assert "Bypasses normal DebridPulse transfer controls. Use for troubleshooting or recovery." in runtime
    assert "const FILTERS = Object.freeze(['all', 'active', 'waiting', 'paused', 'stopped'])" in runtime
    assert "const STOPPED_STATES = new Set(['complete', 'error', 'removed'])" in runtime
    assert "data-dp-aria2-live-speed" in runtime
    assert "data-dp-aria2-live-remaining" in runtime
    assert "data-engine-filter" in runtime
    assert "filter-tabs dp-settings-aria2-live-filters" in runtime
    assert "Built-in ·" not in runtime
    assert "aria2 Live Downloads" not in runtime
    assert "const QUEUE_ID = 'dp-settings-aria2-downloads'" in runtime
    assert "data-dp-aria2-live-queue=\"1\"" in runtime
    assert "api('GET', '/aria2/downloads', null, QUEUE_TIMEOUT_MS)" in runtime
    assert "renderAria2Downloads(data);" in runtime
    assert "queue.querySelector('.aria2-summary')?.remove();" in runtime
    assert "No jobs currently retained by aria2." in runtime
    assert "showQueueError(error?.message || error)" in runtime
    assert "currentMode() === 'builtin'" in runtime
    assert "function syncCardForMode(panel = downloadsPanel())" in runtime
    assert "liveCard()?.remove();" in runtime
    assert "const card = ensureCard(panel);" in runtime
    assert "body.hidden = !builtin" not in runtime
    assert "is-external" not in runtime
    assert "Remove from aria2" in runtime
    assert "observer.observe(view, {childList: true, subtree: false})" in runtime

    # Header copy is true-centered across the card while Refresh alone owns the
    # right track. The direct-control note consumes available desktop space
    # instead of wrapping inside an artificially capped column.
    assert ".dp-settings-aria2-live-card > .card-header" in styles
    assert "grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);" in styles
    assert ".dp-settings-aria2-live-header-actions" in styles
    assert "grid-column: 3;" in styles
    assert ".dp-settings-aria2-live-note" in styles
    assert "flex: 1 1 auto;" in styles
    assert "max-width: none;" in styles
    assert "max-width: 430px" not in styles

    # Control composition: direct-engine warning left, stacked metrics and the
    # existing Downloads-style segmented filter language on the right.
    assert ".dp-settings-aria2-live-control-row" in styles
    assert ".dp-settings-aria2-live-tools" in styles
    assert ".dp-settings-aria2-live-metrics" in styles
    assert ".dp-settings-aria2-live-filters" in styles
    assert "justify-content: flex-end" in styles

    # The first built-in engine read is deterministic when the card is created.
    # Settings/Downloads visibility only controls the recurring poll loop.
    assert "void refreshQueue(false, true);" in runtime
    assert "async function refreshQueue(manual, force = false)" in runtime
    assert "if (!manual && !force && !shouldRunLiveQueue()) return null;" in runtime


@pytest.mark.asyncio
async def test_builtin_low_level_remove_delegates_without_dp_state_mutation(monkeypatch):
    import services.transfer_control as control_module

    monkeypatch.setattr(control_module, "is_builtin_mode", lambda: True)

    def forbidden_db_access(*_args, **_kwargs):
        raise AssertionError("low-level built-in aria2 removal must not mutate DebridPulse state")

    monkeypatch.setattr(control_module, "get_db", forbidden_db_access)

    manager = SimpleNamespace(
        _engine_dispatch_pending_aria2_queue=AsyncMock(),
        _engine_schedule_ready_parent_download=AsyncMock(),
        _engine_start_download=AsyncMock(),
        _engine_download=AsyncMock(),
        _engine_reset_torrent_for_redownload=AsyncMock(),
        _engine_update_aria2_parent_progress=AsyncMock(),
        _engine_sync_download_clients=AsyncMock(),
        _remove_owned_aria2_gid=AsyncMock(return_value=True),
        _aria2_owned_gids=AsyncMock(return_value=set()),
    )
    coordinator = control_module.TransferControlCoordinator(manager)

    result = await coordinator.control_aria2_gid("escape-hatch-gid", "remove")

    assert result == {"mutated": True, "result_preserved": False}
    manager._remove_owned_aria2_gid.assert_awaited_once_with("escape-hatch-gid")
    manager._aria2_owned_gids.assert_not_awaited()


@pytest.mark.asyncio
async def test_builtin_engine_remove_calls_aria2_only(monkeypatch):
    import services.manager_v2 as manager_module

    monkeypatch.setattr(manager_module, "is_builtin_mode", lambda: True)

    def forbidden_db_access(*_args, **_kwargs):
        raise AssertionError("engine escape-hatch removal must not access DebridPulse DB")

    monkeypatch.setattr(manager_module, "get_db", forbidden_db_access)

    aria2 = SimpleNamespace(remove=AsyncMock())
    manager = manager_module.TorrentManager()
    manager.aria2 = lambda: aria2

    result = await manager._remove_owned_aria2_gid("escape-hatch-gid")

    assert result is True
    aria2.remove.assert_awaited_once_with("escape-hatch-gid")