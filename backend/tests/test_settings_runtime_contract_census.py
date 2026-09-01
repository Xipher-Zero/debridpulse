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
