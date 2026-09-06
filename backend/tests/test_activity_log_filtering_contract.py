from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import api.operational_downloads as activity_routes


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


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
        {"level": "warn", "message": "one", "created_at": "2026-09-06 01:00:00", "torrent_name": "A"},
    ])
    monkeypatch.setattr(activity_routes, "get_db", lambda: _fake_db(fake))

    result = await activity_routes.list_activity_events(
        search="100%_literal",
        level="warn",
        timeframe="12h",
        limit=500,
    )

    assert result["truncated"] is False
    assert result["limit"] == 500
    assert "datetime(e.created_at) >= datetime('now', ?)" in fake.sql
    assert "LOWER(COALESCE(e.level, 'info')) IN ('warn', 'warning')" in fake.sql
    assert "instr(LOWER(COALESCE(e.message, '')), ?) > 0" in fake.sql
    assert "instr(LOWER(COALESCE(t.name, '')), ?) > 0" in fake.sql
    assert "ORDER BY e.created_at DESC, e.id DESC" in fake.sql
    assert fake.params == ["-12 hours", "100%_literal", "100%_literal", 501]
    assert "%100%_literal%" not in fake.params


@pytest.mark.asyncio
async def test_activity_log_available_history_has_no_cutoff_and_all_severity_has_no_predicate(monkeypatch):
    fake = _FakeDb([])
    monkeypatch.setattr(activity_routes, "get_db", lambda: _fake_db(fake))

    result = await activity_routes.list_activity_events(
        search=None,
        level=None,
        timeframe="all",
        limit=500,
    )

    assert result == {"items": [], "truncated": False, "limit": 500}
    assert "datetime(e.created_at)" not in fake.sql
    assert "e.level" in fake.sql  # selected for presentation, not filtered
    assert "COALESCE(e.level" not in fake.sql
    assert fake.params == [501]


@pytest.mark.asyncio
async def test_activity_log_truncation_is_explicit_and_exact_limit_is_not_truncated(monkeypatch):
    rows_501 = [
        {"level": "info", "message": str(i), "created_at": "2026-09-06 01:00:00", "torrent_name": None}
        for i in range(501)
    ]
    fake = _FakeDb(rows_501)
    monkeypatch.setattr(activity_routes, "get_db", lambda: _fake_db(fake))

    result = await activity_routes.list_activity_events(timeframe="all", limit=500)
    assert len(result["items"]) == 500
    assert result["truncated"] is True
    assert fake.params[-1] == 501

    fake.rows = rows_501[:500]
    result = await activity_routes.list_activity_events(timeframe="all", limit=500)
    assert len(result["items"]) == 500
    assert result["truncated"] is False


def test_activity_log_timeframe_api_is_a_fixed_literal_enum():
    source = (ROOT / "backend" / "api" / "operational_downloads.py").read_text(encoding="utf-8")
    assert 'EventTimeframe = Literal["1h", "12h", "24h", "72h", "7d", "30d", "all"]' in source
    assert 'limit: int = Query(500, ge=1, le=500)' in source
    assert 'instr(LOWER(COALESCE(e.message, \'\')), ?) > 0' in source
    assert 'instr(LOWER(COALESCE(t.name, \'\')), ?) > 0' in source
