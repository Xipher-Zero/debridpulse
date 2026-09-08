import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import api.operational_downloads as downloads


class _ExplodingRepository:
    async def presentation(self, *_args, **_kwargs):
        raise AssertionError(
            "Downloads collection must not invoke comprehensive presentation per row"
        )


class _FakeDb:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    async def fetchall(self, query, params=()):
        self.calls.append(("fetchall", query, tuple(params)))
        return list(self.rows)

    async def fetchone(self, query, params=()):
        self.calls.append(("fetchone", query, tuple(params)))
        return {"cnt": len(self.rows)}


def _row(transfer_id: int):
    return {
        "id": transfer_id,
        "hash": f"hash-{transfer_id}",
        "name": f"Transfer {transfer_id}",
        "magnet": "magnet:?xt=urn:btih:secret",
        "status": "completed",
        "size_bytes": 1024,
        "progress": 100.0,
        "download_url": "https://private.invalid/download",
        "local_path": "/download/private",
        "source": "https://example.invalid/file",
        "label": "fixture",
        "error_code": None,
        "error_message": None,
        "created_at": "2026-09-08T12:00:00Z",
        "extraction_status": "not_required",
        "extraction_message": None,
        "source_failure_count": 1,
        "current_provider_id": "alldebrid",
        "delivering_provider_id": "alldebrid",
        "provider_provenance_status": "recorded",
    }


def _run_list(monkeypatch, row_count: int):
    db = _FakeDb(_row(index) for index in range(1, row_count + 1))

    @asynccontextmanager
    async def fake_get_db():
        yield db

    monkeypatch.setattr(downloads, "get_db", fake_get_db)
    application = SimpleNamespace(
        repository=_ExplodingRepository(),
        definitions=[],
    )
    result = asyncio.run(
        downloads.list_operational_torrents(
            status=None,
            search=None,
            limit=25,
            offset=0,
            application=application,
        )
    )
    return db, result


def test_downloads_collection_uses_bounded_projection_not_comprehensive_presentations(monkeypatch):
    db, result = _run_list(monkeypatch, 25)

    # One bounded projection read plus one count read; no call to the exploding
    # comprehensive repository presenter can occur for any row.
    assert len(db.calls) == 2
    assert [kind for kind, _query, _params in db.calls] == ["fetchall", "fetchone"]

    projection_sql = db.calls[0][1]
    assert "WITH page AS" in projection_sql
    assert "route_attempt_provenance" in projection_sql
    assert "execution_attempt_provenance" in projection_sql
    assert "transfer_requests" in projection_sql
    assert "AND p.provider_id IS NOT NULL" in projection_sql
    assert "COALESCE(a.provider_id" not in projection_sql

    assert result["total"] == 25
    assert len(result["items"]) == 25
    first = result["items"][0]
    assert first["current_provider_id"] == "alldebrid"
    assert first["delivering_provider_id"] == "alldebrid"
    assert first["provider_provenance_status"] == "recorded"
    assert first["source_failure_count"] == 1
    assert "magnet" not in first
    assert "download_url" not in first
    assert "local_path" not in first


def test_downloads_collection_db_call_count_does_not_scale_with_page_size(monkeypatch):
    one_db, one_result = _run_list(monkeypatch, 1)
    many_db, many_result = _run_list(monkeypatch, 50)

    assert len(one_db.calls) == 2
    assert len(many_db.calls) == 2
    assert len(one_result["items"]) == 1
    assert len(many_result["items"]) == 50
