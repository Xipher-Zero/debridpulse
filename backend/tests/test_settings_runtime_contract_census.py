from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class _Cursor:
    async def fetchone(self):
        return {"upload_retry_count": 0}


class _Db:
    async def execute(self, _sql, _params=()):
        return _Cursor()

    async def commit(self):
        return None


@asynccontextmanager
async def _db():
    yield _Db()


@pytest.mark.asyncio
async def test_upload_retry_count_zero_disables_automatic_requeue(monkeypatch):
    import services.manager_v2 as manager_module
    from services.manager_v2 import TorrentManager

    manager = TorrentManager()
    manager._log_event = AsyncMock()
    manager._fail_torrent = AsyncMock()
    provider = SimpleNamespace(delete_magnet=AsyncMock(), upload_magnet=AsyncMock())
    manager.ad = lambda: provider

    monkeypatch.setattr(
        manager_module,
        "get_settings",
        lambda: SimpleNamespace(
            upload_fail_retry_count=0,
            upload_fail_retry_delay_minutes=0,
            discord_notify_error=False,
        ),
    )
    monkeypatch.setattr(manager_module, "get_db", _db)
    sleep = AsyncMock()
    monkeypatch.setattr(manager_module.asyncio, "sleep", sleep)

    await manager._handle_upload_failed(
        {
            "id": 41,
            "name": "zero-retry",
            "alldebrid_id": "ad-41",
            "magnet": "magnet:?xt=urn:btih:0000000000000000000000000000000000000041",
            "source": "manual",
        },
        "provider upload failed",
    )

    manager._fail_torrent.assert_awaited_once()
    provider.delete_magnet.assert_not_awaited()
    provider.upload_magnet.assert_not_awaited()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_retry_delay_zero_requeues_without_sleep(monkeypatch):
    import services.manager_v2 as manager_module
    from services.manager_v2 import TorrentManager

    manager = TorrentManager()
    manager._log_event = AsyncMock()
    manager._fail_torrent = AsyncMock()
    provider = SimpleNamespace(
        delete_magnet=AsyncMock(),
        upload_magnet=AsyncMock(return_value={"id": "ad-new", "name": "retried"}),
    )
    manager.ad = lambda: provider

    monkeypatch.setattr(
        manager_module,
        "get_settings",
        lambda: SimpleNamespace(
            upload_fail_retry_count=1,
            upload_fail_retry_delay_minutes=0,
            discord_notify_error=False,
        ),
    )
    monkeypatch.setattr(manager_module, "get_db", _db)
    sleep = AsyncMock()
    monkeypatch.setattr(manager_module.asyncio, "sleep", sleep)

    magnet = "magnet:?xt=urn:btih:0000000000000000000000000000000000000042"
    await manager._handle_upload_failed(
        {
            "id": 42,
            "name": "zero-delay",
            "alldebrid_id": "",
            "magnet": magnet,
            "source": "manual",
        },
        "provider upload failed",
    )

    provider.upload_magnet.assert_awaited_once_with(magnet)
    sleep.assert_not_awaited()
    manager._fail_torrent.assert_not_awaited()


def test_effective_settings_numeric_limits_match_server_contract():
    root = Path(__file__).resolve().parents[2]
    source = (root / "frontend/static/ui-settings-page.js").read_text(encoding="utf-8")

    def field(key: str, span: int = 700) -> str:
        marker = f"input('{key}'"
        start = source.index(marker)
        return source[start:start + span]

    assert "max: 20" in field("aria2_max_active_downloads")
    assert "max: 32" in field("aria2_max_connection_per_server")
    assert "max: 168" in field("stuck_download_timeout_hours")

    snapshots = field("stats_snapshot_interval_minutes")
    assert "min: 0" in snapshots
    assert "max: 1440" in snapshots
    assert "0 disables automatic statistics snapshots." in snapshots

    retention = field("stats_snapshot_keep_days")
    assert "max: 365" in retention


def test_retired_file_filter_policy_is_physically_pruned_but_manual_blocking_and_labels_remain():
    from core.config import AppSettings, _build_effective_settings

    root = Path(__file__).resolve().parents[2]
    retired = {
        "filters_enabled",
        "blocked_extensions",
        "blocked_keywords",
        "min_file_size_mb",
        "block_samples",
        "block_extras",
        "torrent_labels",
    }

    assert retired.isdisjoint(AppSettings.model_fields)

    legacy = {
        "download_folder": "/download",
        "filters_enabled": True,
        "blocked_extensions": [".nfo"],
        "blocked_keywords": ["sample"],
        "min_file_size_mb": 100,
        "block_samples": True,
        "block_extras": True,
        "torrent_labels": ["legacy"],
    }
    upgraded = _build_effective_settings(legacy)
    assert upgraded.download_folder == "/download"
    assert retired.isdisjoint(upgraded.model_dump())

    manager = (root / "backend/services/manager_v2.py").read_text(encoding="utf-8")
    integrity = (root / "backend/services/transfer_integrity.py").read_text(encoding="utf-8")
    runtime_guard = (root / "backend/services/transfer_runtime_guard.py").read_text(encoding="utf-8")
    for owner in (manager, integrity, runtime_guard):
        assert "is_blocked" not in owner
    assert "blocked_items" not in manager
    assert "blocked_items" not in integrity
    assert "Filtered files were skipped" not in manager
    assert "filtered/blocked" not in manager
    assert "filtered/blocked" not in integrity
    assert "_send_partial_summary" not in integrity

    routes = (root / "backend/api/routes.py").read_text(encoding="utf-8")
    assert '@router.post("/torrents/{torrent_id}/files/{file_id}/block")' in routes
    assert '@router.put("/torrents/{torrent_id}/label")' in routes
    assert "SET status='blocked', blocked=1" in manager


def test_active_settings_runtime_contains_no_retired_file_filter_surface():
    root = Path(__file__).resolve().parents[2]
    page = (root / "frontend/static/ui-settings-page.js").read_text(encoding="utf-8")
    completion = (root / "frontend/static/ui-settings-downloads-completion.js").read_text(encoding="utf-8")
    completion_css = (root / "frontend/static/ui-settings-downloads-completion.css").read_text(encoding="utf-8")

    for token in (
        "File Filters",
        "filters_enabled",
        "blocked_extensions",
        "blocked_keywords",
        "min_file_size_mb",
        "block_samples",
        "block_extras",
        "torrent_labels_raw",
    ):
        assert token not in page

    assert "dp-settings-file-filters-retired" not in completion
    assert "dp-settings-file-filters-retired" not in completion_css
