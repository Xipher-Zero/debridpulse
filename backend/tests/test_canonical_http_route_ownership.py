"""Canonical HTTP route ownership contract (ARCH-001).

GET /api/torrents and GET /api/events are each declared by exactly one owner:
``api.operational_downloads``. ``api.routes`` no longer declares superseded
variants, and ``main`` performs no startup-time surgery on a router's ``.routes``
list to make ownership come out right. These tests fail closed if either the
legacy declaration or the route-list mutation is reintroduced, and they cover
the externally visible behavior of both collections.
"""
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute

import api.operational_downloads as downloads
import api.routes as legacy_routes
import main as backend_main


# Relative path (as declared on the owning APIRouter) -> the sole endpoint that
# may serve it. operational_downloads is included with prefix="/api".
_CANONICAL_COLLECTIONS = {
    "/torrents": downloads.list_operational_torrents,
    "/events": downloads.list_activity_events,
}

# Same (method, path) registered by two handlers on purpose: the pending-aware
# OIDC callback is tried before the session-issuing one. Ordered registration,
# not route-list surgery, and explicitly out of scope for ARCH-001.
_KNOWN_DUAL_REGISTRATIONS = {("GET", "/auth/oidc/callback")}


def _all_api_routes():
    """Every APIRoute the assembled app serves.

    FastAPI 0.141 keeps an included router as a nested ``_IncludedRouter`` whose
    ``original_router.routes`` hold the real ``APIRoute`` objects (paths relative
    to the ``include_router`` prefix), rather than flattening them onto
    ``app.router.routes``.
    """
    routes = []
    for entry in backend_main.app.routes:
        if isinstance(entry, APIRoute):
            routes.append(entry)
        original = getattr(entry, "original_router", None)
        if original is not None:
            routes.extend(r for r in original.routes if isinstance(r, APIRoute))
    return routes


def test_no_unexpected_duplicate_handler_for_any_method_and_path():
    seen: dict[tuple[str, str], list[str]] = {}
    for route in _all_api_routes():
        for method in route.methods or ():
            if method in {"HEAD", "OPTIONS"}:
                continue
            seen.setdefault((method, route.path), []).append(
                f"{route.endpoint.__module__}.{route.endpoint.__qualname__}"
            )

    duplicates = {
        key: names
        for key, names in seen.items()
        if len(names) > 1 and key not in _KNOWN_DUAL_REGISTRATIONS
    }
    assert duplicates == {}, f"multiple handlers registered for {duplicates}"


def test_operational_downloads_is_sole_owner_of_migrated_collections():
    for path, expected_endpoint in _CANONICAL_COLLECTIONS.items():
        matches = [
            route
            for route in _all_api_routes()
            if route.path == path and "GET" in (route.methods or set())
        ]
        assert len(matches) == 1, f"{path} should have exactly one GET handler"
        assert matches[0].endpoint is expected_endpoint


def test_legacy_router_no_longer_declares_migrated_collections():
    offending = [
        route
        for route in legacy_routes.router.routes
        if getattr(route, "path", None) in {"/torrents", "/events"}
        and "GET" in (getattr(route, "methods", set()) or set())
    ]
    assert offending == []

    owned = [
        route
        for route in downloads.router.routes
        if getattr(route, "path", None) in {"/torrents", "/events"}
        and "GET" in (getattr(route, "methods", set()) or set())
    ]
    assert sorted(r.path for r in owned) == ["/events", "/torrents"]


def test_main_does_not_mutate_router_route_lists_at_startup():
    main_source = Path(backend_main.__file__).read_text(encoding="utf-8")
    assert "router.routes[:]" not in main_source
    downloads_source = Path(downloads.__file__).read_text(encoding="utf-8")
    assert "routes[:]" not in downloads_source
    assert "legacy_router" not in downloads_source


def test_openapi_exposes_one_operation_per_migrated_collection_path():
    paths = backend_main.app.openapi()["paths"]

    assert set(paths["/api/torrents"]) == {"get"}
    assert paths["/api/torrents"]["get"]["operationId"].startswith(
        "list_operational_torrents"
    )

    assert set(paths["/api/events"]) == {"get"}
    assert paths["/api/events"]["get"]["operationId"].startswith("list_activity_events")


# ── Behavioral: GET /api/torrents collection listing ──────────────────────────


class _FakeDb:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    async def fetchall(self, query, params=()):
        self.calls.append(("fetchall", query, list(params)))
        return list(self.rows)

    async def fetchone(self, query, params=()):
        self.calls.append(("fetchone", query, list(params)))
        return {"cnt": len(self.rows)}


@asynccontextmanager
async def _fake_db(db):
    yield db


def _torrent_row(transfer_id):
    return {
        "id": transfer_id,
        "hash": f"hash-{transfer_id}",
        "name": f"Transfer {transfer_id}",
        "status": "completed",
        "size_bytes": 1024,
        "progress": 100.0,
        "source": "https://example.invalid/file",
        "label": "",
        "error_message": None,
        "extraction_status": "not_required",
        "extraction_error": None,
        "created_at": "2026-09-08T12:00:00Z",
        "updated_at": "2026-09-08T12:00:00Z",
        "completed_at": "2026-09-08T12:00:00Z",
        "source_failure_count": 0,
        "current_provider_id": "alldebrid",
        "delivering_provider_id": "alldebrid",
        "provider_provenance_status": "recorded",
    }


async def _list_torrents(monkeypatch, **kwargs):
    db = _FakeDb([_torrent_row(1)])
    monkeypatch.setattr(downloads, "get_db", lambda: _fake_db(db))
    application = SimpleNamespace(repository=object(), definitions=[])
    result = await downloads.list_operational_torrents(application=application, **kwargs)
    return db, result


@pytest.mark.asyncio
async def test_torrents_default_view_excludes_deleted_and_consolidated(monkeypatch):
    db, _ = await _list_torrents(monkeypatch, status=None, search=None, limit=0, offset=0)
    projection_sql = db.calls[0][1]
    assert "t.status NOT IN ('deleted', 'consolidated')" in projection_sql


@pytest.mark.asyncio
async def test_torrents_explicit_status_filters_by_parameter(monkeypatch):
    db, _ = await _list_torrents(
        monkeypatch, status="downloading", search=None, limit=0, offset=0
    )
    projection_sql, params = db.calls[0][1], db.calls[0][2]
    assert "t.status = ?" in projection_sql
    assert "downloading" in params
    assert "NOT IN ('deleted', 'consolidated')" not in projection_sql


@pytest.mark.asyncio
async def test_torrents_search_is_parameterized_across_columns(monkeypatch):
    db, _ = await _list_torrents(
        monkeypatch, status=None, search="Example", limit=0, offset=0
    )
    projection_sql, params = db.calls[0][1], db.calls[0][2]
    assert "LOWER(COALESCE(t.name, '')) LIKE ?" in projection_sql
    assert params.count("%example%") == 5


@pytest.mark.asyncio
async def test_torrents_limit_and_offset_only_applied_when_limit_positive(monkeypatch):
    db, _ = await _list_torrents(monkeypatch, status=None, search=None, limit=0, offset=0)
    assert "LIMIT ? OFFSET ?" not in db.calls[0][1]

    db, _ = await _list_torrents(
        monkeypatch, status=None, search=None, limit=250, offset=25
    )
    assert "LIMIT ? OFFSET ?" in db.calls[0][1]
    assert db.calls[0][2][-2:] == [250, 25]


# ── Behavioral: GET /api/events collection listing ───────────────────────────


class _EventDb:
    def __init__(self, rows):
        self.rows = list(rows)
        self.sql = ""
        self.params = []

    async def fetchall(self, sql, params=()):
        self.sql = sql
        self.params = list(params)
        return list(self.rows)


@pytest.mark.asyncio
async def test_events_apply_filters_before_limit_and_normalize_timestamps(monkeypatch):
    db = _EventDb(
        [
            {
                "level": "warning",
                "message": "retry scheduled",
                "created_at": "2026-09-06 08:30:00",
                "torrent_name": "example.iso",
            }
        ]
    )
    monkeypatch.setattr(downloads, "get_db", lambda: _fake_db(db))

    result = await downloads.list_activity_events(
        search="retry", level="warning", timeframe="24h", limit=10
    )

    # Predicates land in WHERE (before the trailing LIMIT), user input stays
    # parameterized, and the row limit is fetched as limit + 1.
    assert "datetime(e.created_at) >= datetime('now', ?)" in db.sql
    assert db.sql.strip().endswith("LIMIT ?")
    assert db.params == ["-24 hours", "retry", "retry", 11]

    # Browser-facing timestamp normalization is preserved on the canonical path.
    assert result == [
        {
            "level": "warning",
            "message": "retry scheduled",
            "created_at": "2026-09-06T08:30:00Z",
            "torrent_name": "example.iso",
        }
    ]
