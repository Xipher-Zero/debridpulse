from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import api.operational_downloads as activity_routes


ROOT = Path(__file__).resolve().parents[2]


class _FakeDb:
    def __init__(self, rows):
        self.rows = list(rows)
        self.sql = ""
        self.params = []

    async def fetchall(self, sql, params=()):
        self.sql = sql
        self.params = list(params)
        return list(self.rows)


@asynccontextmanager
async def _fake_db(fake):
    yield fake


@pytest.mark.asyncio
async def test_activity_log_combined_filters_are_parameterized_before_limit(monkeypatch):
    fake = _FakeDb([
        {"level": "warning", "message": "one", "created_at": "2026-09-06 01:00:00", "torrent_name": "A"},
    ])
    monkeypatch.setattr(activity_routes, "get_db", lambda: _fake_db(fake))

    result = await activity_routes.list_activity_events(
        search="100%_literal",
        level="warning",
        timeframe="24h",
        limit=500,
        include_meta=True,
    )

    assert result["truncated"] is False
    assert result["limit"] == 500
    assert "datetime(e.created_at) >= datetime('now', ?)" in fake.sql
    assert "LOWER(COALESCE(e.level, 'info')) IN ('warn', 'warning')" in fake.sql
    assert "instr(LOWER(COALESCE(e.message, '')), ?) > 0" in fake.sql
    assert "instr(LOWER(COALESCE(t.name, '')), ?) > 0" in fake.sql
    assert "ORDER BY e.created_at DESC, e.id DESC" in fake.sql
    assert fake.params == ["-24 hours", "100%_literal", "100%_literal", 501]
    assert "%100%_literal%" not in fake.params


@pytest.mark.asyncio
async def test_activity_log_default_contract_remains_a_list(monkeypatch):
    rows = [
        {"level": "info", "message": "ready", "created_at": "2026-09-06 01:00:00", "torrent_name": None},
    ]
    fake = _FakeDb(rows)
    monkeypatch.setattr(activity_routes, "get_db", lambda: _fake_db(fake))

    result = await activity_routes.list_activity_events()

    assert result == rows
    assert fake.params == [201]
    assert "datetime(e.created_at)" not in fake.sql
    assert "COALESCE(e.level" not in fake.sql


@pytest.mark.asyncio
async def test_activity_log_metadata_reports_only_actual_truncation(monkeypatch):
    rows_501 = [
        {"level": "info", "message": str(i), "created_at": "2026-09-06 01:00:00", "torrent_name": None}
        for i in range(501)
    ]
    fake = _FakeDb(rows_501)
    monkeypatch.setattr(activity_routes, "get_db", lambda: _fake_db(fake))

    result = await activity_routes.list_activity_events(timeframe="all", limit=500, include_meta=True)
    assert len(result["items"]) == 500
    assert result["truncated"] is True
    assert fake.params[-1] == 501

    fake.rows = rows_501[:500]
    result = await activity_routes.list_activity_events(timeframe="all", limit=500, include_meta=True)
    assert len(result["items"]) == 500
    assert result["truncated"] is False


def test_activity_log_timeframe_api_matches_reviewed_filter_set():
    source = (ROOT / "backend" / "api" / "operational_downloads.py").read_text(encoding="utf-8")
    assert 'EventTimeframe = Literal["all", "1h", "24h", "7d", "30d"]' in source
    assert 'limit: Annotated[int, Query(ge=1, le=500)] = 200' in source
    assert 'include_meta: bool = False' in source
    assert 'instr(LOWER(COALESCE(e.message, \'\')), ?) > 0' in source
    assert 'instr(LOWER(COALESCE(t.name, \'\')), ?) > 0' in source
    assert '"12h"' not in source
    assert '"72h"' not in source
