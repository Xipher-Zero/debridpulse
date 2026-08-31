from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one match, found {count}: {old!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "backend/services/manager_v2.py",
    '        max_retries = max(0, int(getattr(cfg, "upload_fail_retry_count", 3) or 3))\n        delay_minutes = max(0, int(getattr(cfg, "upload_fail_retry_delay_minutes", 5) or 5))',
    '        retry_setting = getattr(cfg, "upload_fail_retry_count", 3)\n        delay_setting = getattr(cfg, "upload_fail_retry_delay_minutes", 5)\n        max_retries = max(0, int(3 if retry_setting is None else retry_setting))\n        delay_minutes = max(0, int(5 if delay_setting is None else delay_setting))',
)

replace_once(
    "frontend/static/ui-settings-page.js",
    "          ${input('aria2_max_active_downloads', 'Maximum Concurrent Downloads', s.max_concurrent_downloads ?? s.aria2_max_active_downloads ?? 3, {\n            type: 'number', min: 1, max: 100,",
    "          ${input('aria2_max_active_downloads', 'Maximum Concurrent Downloads', s.max_concurrent_downloads ?? s.aria2_max_active_downloads ?? 3, {\n            type: 'number', min: 1, max: 20,",
)
replace_once(
    "frontend/static/ui-settings-page.js",
    "            ${input('aria2_max_connection_per_server', 'Connections per Server', s.aria2_max_connection_per_server ?? 16, {\n              type: 'number', min: 1, max: 64,",
    "            ${input('aria2_max_connection_per_server', 'Connections per Server', s.aria2_max_connection_per_server ?? 16, {\n              type: 'number', min: 1, max: 32,",
)
replace_once(
    "frontend/static/ui-settings-page.js",
    "      ${input('stuck_download_timeout_hours', 'Stalled Download Timeout (hours)', s.stuck_download_timeout_hours ?? 6, {\n        type: 'number', min: 0, hint: '0 disables automatic stalled-download recovery.'",
    "      ${input('stuck_download_timeout_hours', 'Stalled Download Timeout (hours)', s.stuck_download_timeout_hours ?? 6, {\n        type: 'number', min: 0, max: 168, hint: '0 disables automatic stalled-download recovery.'",
)
replace_once(
    "frontend/static/ui-settings-page.js",
    "      ${input('stats_snapshot_interval_minutes', 'Stats Snapshot Interval (minutes)', s.stats_snapshot_interval_minutes ?? 60, {type: 'number', min: 1})}",
    "      ${input('stats_snapshot_interval_minutes', 'Stats Snapshot Interval (minutes)', s.stats_snapshot_interval_minutes ?? 60, {\n        type: 'number', min: 0, max: 1440, hint: '0 disables automatic statistics snapshots.'\n      })}",
)
replace_once(
    "frontend/static/ui-settings-page.js",
    "      ${input('stats_snapshot_keep_days', 'Stats Snapshot Retention (days)', s.stats_snapshot_keep_days ?? 30, {type: 'number', min: 1})}",
    "      ${input('stats_snapshot_keep_days', 'Stats Snapshot Retention (days)', s.stats_snapshot_keep_days ?? 30, {type: 'number', min: 1, max: 365})}",
)

Path("backend/tests/test_settings_runtime_contract_census.py").write_text(r'''from contextlib import asynccontextmanager
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
''', encoding="utf-8")
