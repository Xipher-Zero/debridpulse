import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
import pytest_asyncio

import api.operational_downloads as downloads
import api.routes as legacy_routes
import db.database as database
import main as backend_main
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.engine import TransferEngine
from transfers.models import IntegrityMetadata, ResolutionResult, ResourceState, SourceIdentity, TransferRequest
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


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


def _row(transfer_id: int, common_candidate_count: int = 0):
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
        "error_message": None,
        "created_at": "2026-09-08T12:00:00Z",
        "updated_at": "2026-09-08T12:01:00Z",
        "completed_at": "2026-09-08T12:01:00Z",
        "extraction_status": "not_required",
        "extraction_error": None,
        "source_failure_count": 1,
        "common_candidate_count": common_candidate_count,
        "current_provider_id": "alldebrid",
        "delivering_provider_id": "alldebrid",
        "provider_provenance_status": "recorded",
    }


def _run_list(monkeypatch, row_count: int, rows=None):
    db = _FakeDb(rows if rows is not None else (_row(index) for index in range(1, row_count + 1)))

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
    assert "t.*" not in projection_sql
    assert "t.magnet" not in projection_sql
    assert "t.download_url" not in projection_sql
    assert "t.local_path" not in projection_sql

    # The transfer-level common-source group summary is derived from canonical
    # acquisition-candidate storage inside the same bounded read, never from
    # route/provider attempt counts.
    assert "group_common_sources AS" in projection_sql
    assert "canonical_candidate_bindings" in projection_sql
    assert "AS common_candidate_count" in projection_sql
    assert "candidate_source_max" not in projection_sql

    assert result["total"] == 25
    assert len(result["items"]) == 25
    first = result["items"][0]
    assert first["current_provider_id"] == "alldebrid"
    assert first["delivering_provider_id"] == "alldebrid"
    assert first["provider_provenance_status"] == "recorded"
    assert first["source_failure_count"] == 1
    assert first["common_candidate_count"] == 0
    assert "magnet" not in first
    assert "download_url" not in first


def test_downloads_collection_db_call_count_does_not_scale_with_page_size(monkeypatch):
    one_db, one_result = _run_list(monkeypatch, 1)
    many_db, many_result = _run_list(monkeypatch, 50)

    assert len(one_db.calls) == 2
    assert len(many_db.calls) == 2
    assert len(one_result["items"]) == 1
    assert len(many_result["items"]) == 50


def test_assembled_app_exposes_only_operational_downloads_collection_route():
    legacy_collection_routes = [
        route
        for route in legacy_routes.router.routes
        if getattr(route, "path", None) == "/torrents"
        and "GET" in (getattr(route, "methods", set()) or set())
    ]
    operational_collection_routes = [
        route
        for route in downloads.router.routes
        if getattr(route, "path", None) == "/torrents"
        and "GET" in (getattr(route, "methods", set()) or set())
    ]

    assert legacy_collection_routes == []
    assert len(operational_collection_routes) == 1
    assert operational_collection_routes[0].endpoint is downloads.list_operational_torrents

    paths = backend_main.app.openapi()["paths"]
    assert "/api/torrents" in paths
    assert paths["/api/torrents"]["get"]["operationId"].startswith(
        "list_operational_torrents_"
    )
    assert "/api/torrents/{torrent_id}" in paths
    assert paths["/api/torrents/{torrent_id}"]["get"]["operationId"].startswith(
        "get_torrent_"
    )


class _RecordingRepository:
    def __init__(self):
        self.calls = []

    async def presentation(self, transfer_id, **kwargs):
        self.calls.append((transfer_id, kwargs))
        return {
            "id": transfer_id,
            "hash": "detail-hash",
            "name": "Detail transfer",
            "status": "completed",
            "size_bytes": 1024,
            "progress": 100.0,
            "source": "https://example.invalid/file",
            "label": "detail",
            "created_at": "2026-09-08T12:00:00Z",
        }


def test_download_detail_explicitly_requests_comprehensive_presentation():
    repository = _RecordingRepository()
    application = SimpleNamespace(repository=repository, definitions=[])

    result = asyncio.run(legacy_routes.get_torrent(42, application=application))

    assert repository.calls == [(42, {"details": True})]
    assert result["id"] == 42


# ── Transfer-level common-source group summary on the bounded list ────────


def test_bounded_list_rows_carry_common_candidate_count(monkeypatch):
    rows = [
        _row(1, common_candidate_count=3),
        _row(2, common_candidate_count=0),
        _row(3, common_candidate_count=1),
    ]
    db, result = _run_list(monkeypatch, 3, rows=rows)

    # Still one bounded projection read plus one count read.
    assert [kind for kind, _query, _params in db.calls] == ["fetchall", "fetchone"]
    assert [item["common_candidate_count"] for item in result["items"]] == [3, 0, 1]
    # group_switch_available does not exist: it encoded the retired
    # actionability-gated launcher rule. Launcher visibility is derived purely
    # from common_candidate_count by the caller (0/1 -> hidden, 2+ -> shown).
    assert all("group_switch_available" not in item for item in result["items"])


def test_single_common_source_rows_do_not_advertise_a_group(monkeypatch):
    # 0 and 1 common hosts both suppress the group launcher: there is no
    # transfer-wide choice to make. This is a pure membership-count fact.
    for count in (0, 1):
        _db, result = _run_list(monkeypatch, 1, rows=[_row(9, common_candidate_count=count)])
        assert result["items"][0]["common_candidate_count"] == count


def test_common_candidate_count_is_normalized_to_non_negative_int(monkeypatch):
    _db, result = _run_list(monkeypatch, 1, rows=[_row(4, common_candidate_count=None)])
    assert result["items"][0]["common_candidate_count"] == 0


def test_group_summary_does_not_add_db_calls_or_scale_with_page_size(monkeypatch):
    one_db, _one = _run_list(
        monkeypatch, 1, rows=[_row(1, common_candidate_count=4)]
    )
    many_db, many = _run_list(
        monkeypatch, 40, rows=[_row(i, common_candidate_count=i % 5) for i in range(1, 41)]
    )

    assert len(one_db.calls) == 2
    assert len(many_db.calls) == 2
    assert [item["common_candidate_count"] for item in many["items"]] == [
        i % 5 for i in range(1, 41)
    ]


def test_projection_group_summary_is_derived_from_canonical_bindings(monkeypatch):
    """The group CTE reads canonical_candidate_bindings, not route history."""
    db, _result = _run_list(monkeypatch, 1, rows=[_row(1)])
    projection_sql = db.calls[0][1]

    cte = projection_sql.split("group_member_artifacts AS", 1)[1]
    assert "canonical_candidate_bindings" in cte
    assert "AS common_candidate_count" in cte
    # Current-artifact identity mirrors the per-artifact detail projection —
    # this is which download_files rows are the transfer's actual current
    # files, not a switchability gate.
    assert "f.request_id IS NOT NULL" in cte
    assert "COALESCE(f.mirror_state, '') != 'standby'" in cte
    assert "route_attempt_provenance" not in cte.split("group_common_sources", 1)[0]

    # Membership must NOT depend on switch-eligibility or artifact operational
    # state: no artifact-status literal, no selected-candidate comparison, and
    # no reference to download_files.status/selected_candidate/candidates
    # appears anywhere in the group CTE chain (isolated from the unrelated
    # 'pending' literal in the outer provider_provenance_status projection).
    group_block = projection_sql.split("group_member_artifacts AS", 1)[1].split(
        "\n        SELECT\n            t.id,", 1
    )[0]
    for switchable_state in (
        "'pending'", "'processing'", "'ready'", "'queued'", "'downloading'",
        "'paused'", "'refresh_pending'", "'error'",
    ):
        assert switchable_state not in group_block
    assert "f.status" not in group_block
    assert "f.selected_candidate" not in group_block
    assert "f.candidates" not in group_block
    assert "json_extract" not in group_block
    assert "group_switch_available" not in projection_sql


_SHA256 = "b1c3ed04a95a3da14a9d235c83d868bed7c0f45cf7f3faa751ee8f50598d2299"


class _HostParcelProvider(ParcelProvider):
    def candidate_for(self, request):
        candidate = super().candidate(request.name or "same.bin", payload="shared")
        host = "rapidgator.net" if request.payload == "rapidgator" else "1fichier.com"
        return replace(
            candidate,
            integrity=(IntegrityMetadata("sha256", _SHA256),),
            source_identity=SourceIdentity("host", host),
        )

    async def resolve(self, request):
        self.calls.append(("resolve", request.payload))
        return ResolutionResult(ResourceState.AVAILABLE, (self.candidate_for(request),))


@pytest_asyncio.fixture
async def projection_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = _HostParcelProvider("provider-a")
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    engine = TransferEngine(
        repository,
        registry,
        download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(retry_delay=0, adoption_stability_seconds=0, max_active_executions=8, resolution_concurrency=8),
    )
    await engine.initialize()
    return engine, repository


async def _submit(engine, name, payload):
    return await engine.submit(
        (TransferRequest("parcel", payload, name=name, preferred_provider="provider-a"),),
        name=name,
        deduplicate=False,
    )


async def _list_normal():
    async with _tracking_db() as tracker:
        result = await downloads.list_operational_torrents(
            status=None,
            search=None,
            limit=0,
            offset=0,
            application=SimpleNamespace(repository=_ExplodingRepository(), definitions=[]),
        )
    return result, tracker


class _DbCallTracker:
    def __init__(self):
        self.calls = []


@asynccontextmanager
async def _tracking_db():
    """Wrap the real get_db so the test can count round-trips."""
    tracker = _DbCallTracker()
    real_get_db = database.get_db

    @asynccontextmanager
    async def counting():
        async with real_get_db() as conn:
            real_fetchall = conn.fetchall
            real_fetchone = conn.fetchone

            async def fetchall(query, params=()):
                tracker.calls.append(("fetchall", query))
                return await real_fetchall(query, params)

            async def fetchone(query, params=()):
                tracker.calls.append(("fetchone", query))
                return await real_fetchone(query, params)

            conn.fetchall = fetchall
            conn.fetchone = fetchone
            yield conn

    token = downloads.get_db
    downloads.get_db = counting
    try:
        yield tracker
    finally:
        downloads.get_db = token


@pytest.mark.asyncio
async def test_list_common_candidate_count_reflects_canonical_bindings(projection_runtime):
    engine, _repository = projection_runtime
    multi = await _submit(engine, "multi.bin", "rapidgator")
    await engine.resolve_pending()
    await _submit(engine, "multi.bin", "1fichier")  # consolidates onto `multi`
    await engine.resolve_pending()
    single = await _submit(engine, "single.bin", "rapidgator")
    await engine.resolve_pending()

    result, tracker = await _list_normal()
    by_id = {item["id"]: item for item in result["items"]}

    # The sole artifact carries two canonical host candidates -> two common hosts.
    assert by_id[multi.id]["common_candidate_count"] == 2
    # A single unconsolidated source has no canonical alternate bindings — its
    # real host set is unknown here, so the (single-artifact) intersection is
    # empty and it never advertises a group.
    assert by_id[single.id]["common_candidate_count"] == 0

    # Bounded: exactly the projection read and the collection-count read.
    assert [kind for kind, _q in tracker.calls] == ["fetchall", "fetchone"]


@pytest.mark.asyncio
async def test_list_candidate_summary_never_scales_db_calls_with_rows(projection_runtime):
    engine, _repository = projection_runtime
    for index in range(6):
        await _submit(engine, f"file-{index}.bin", "rapidgator")
    await engine.resolve_pending()

    _result, tracker = await _list_normal()
    assert [kind for kind, _q in tracker.calls] == ["fetchall", "fetchone"]
