import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import api.operational_downloads as downloads


class _ExplodingRepository:
    async def presentation(self, *_args, **_kwargs):
        raise AssertionError("bounded collection must not reconstruct comprehensive presentation")


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


def _request(kind, payload):
    return json.dumps({
        "kind": kind,
        "payload": payload,
        "name": "",
        "fingerprint": "",
        "preferred_provider": None,
    })


def _row(transfer_id, *, status="downloading", request_kind="http", source=None):
    return {
        "id": transfer_id,
        "hash": f"hash-{transfer_id}",
        "name": f"Transfer {transfer_id}",
        "status": status,
        "size_bytes": 1024,
        "progress": 42.0,
        "source": "direct_link",
        "label": "fixture",
        "error_message": None,
        "extraction_status": "not_required",
        "extraction_error": None,
        "created_at": "2026-09-08T12:00:00Z",
        "updated_at": "2026-09-08T12:01:00Z",
        "completed_at": None,
        "source_failure_count": 0,
        "current_provider_id": "alldebrid",
        "delivering_provider_id": None,
        "provider_provenance_status": "pending",
        "_source_request_payload": _request(request_kind, "https://rapidgator.net/file"),
        "_delivered_candidate_source": None,
        "_active_candidate_source": json.dumps(source) if source is not None else None,
        "_route_candidate_summary": json.dumps([]),
    }


def _run(monkeypatch, rows):
    db = _FakeDb(rows)

    @asynccontextmanager
    async def fake_get_db():
        yield db

    monkeypatch.setattr(downloads, "get_db", fake_get_db)
    application = SimpleNamespace(repository=_ExplodingRepository(), definitions=[])
    result = asyncio.run(downloads.list_operational_torrents(
        status=None,
        search=None,
        limit=25,
        offset=0,
        application=application,
    ))
    return db, result


def test_bounded_collection_restores_safe_host_identity_without_read_fanout(monkeypatch):
    host_source = {"scope": "host", "key": "rapidgator.net"}
    db, result = _run(monkeypatch, [_row(1, source=host_source)])

    assert len(db.calls) == 2
    assert [kind for kind, _query, _params in db.calls] == ["fetchall", "fetchone"]
    projection_sql = db.calls[0][1]
    assert "root_request AS" in projection_sql
    assert "delivered_source AS" in projection_sql
    assert "active_source AS" in projection_sql
    assert "candidate_summary" in projection_sql

    item = result["items"][0]
    assert item["current_source_identity"] == {"kind": "host", "host": "rapidgator.net"}
    for private_field in downloads._SOURCE_PROJECTION_FIELDS:
        assert private_field not in item


def test_root_request_identity_still_beats_provider_generated_host(monkeypatch):
    host_source = {"scope": "host", "key": "rapidgator.net"}
    _db, result = _run(monkeypatch, [_row(2, request_kind="magnet", source=host_source)])

    assert result["items"][0]["current_source_identity"] == {"kind": "magnet"}


def test_route_candidate_summary_supplies_host_when_no_execution_source(monkeypatch):
    row = _row(3)
    row["_route_candidate_summary"] = json.dumps([
        {"source": {"scope": "host", "key": "1fichier.com"}},
    ])
    _db, result = _run(monkeypatch, [row])

    assert result["items"][0]["current_source_identity"] == {"kind": "host", "host": "1fichier.com"}
