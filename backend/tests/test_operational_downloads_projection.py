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


def _row(
    transfer_id: int,
    common_candidate_count: int = 0,
    group_remaining_count: int = 0,
    filenames=None,
):
    import json as _json

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
        "group_remaining_count": group_remaining_count,
        "_group_member_filenames": _json.dumps(filenames) if filenames is not None else None,
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

    group_block = projection_sql.split("group_member_artifacts AS", 1)[1].split(
        "\n        SELECT\n            t.id,", 1
    )[0]

    # The base current-artifact CTE now also carries the artifact's own
    # status/filename — feeding the SIBLING remaining-work count and
    # display-name filename aggregate (DP 1.0.12 §10/§19), never the
    # membership computation itself. The remaining-work predicate reuses the
    # exact _SWITCHABLE_ARTIFACT_STATES classification the comprehensive
    # Details presentation already uses for switch_eligible (imported, not a
    # fourth hand-copied literal list) — 'error' (a recoverable-looking
    # failure) counts as remaining work, but a non-completed status outside
    # that set (e.g. 'cancelled', 'input_required') does not.
    from api.operational_downloads import _SWITCHABLE_STATES_SQL
    assert "f.status AS status" in group_block
    assert "group_remaining_counts AS" in group_block
    assert _SWITCHABLE_STATES_SQL in group_block
    assert "'error'" in group_block  # recoverable failure still counts as remaining work
    assert "'completed'" not in group_block.split("group_remaining_counts AS", 1)[1].split(")", 1)[0]

    # MEMBERSHIP itself (group_member_hosts -> group_true_common_hosts ->
    # group_common_sources) must still NOT depend on switch-eligibility,
    # artifact operational state, or which candidate is currently selected:
    # no artifact-status literal, no selected-candidate comparison, and no
    # reference to download_files.status/selected_candidate/candidates
    # anywhere in that specific sub-chain (isolated from the unrelated
    # 'pending' literal in the outer provider_provenance_status projection,
    # from the sibling remaining-work count above, and from the SEPARATE
    # group_remaining_host_movement/group_actionable_common_sources
    # actionability facts below -- those legitimately DO read status and
    # selected-candidate, but feed only group_remaining_count, never
    # common_candidate_count/membership; proven by the dedicated
    # test_group_actionable_common_sources_is_derived_from_the_same_true_common_host_set).
    membership_block = group_block.split("group_member_hosts AS", 1)[1].split(
        "group_remaining_host_movement AS", 1
    )[0]
    for switchable_state in (
        "'pending'", "'processing'", "'ready'", "'queued'", "'downloading'",
        "'paused'", "'refresh_pending'", "'error'", "'completed'",
    ):
        assert switchable_state not in membership_block
    assert "f.status" not in membership_block
    assert "f.selected_candidate" not in membership_block
    assert "f.candidates" not in membership_block
    assert "json_extract" not in membership_block
    assert "group_switch_available" not in projection_sql


# ── Canonical display name / remaining-work count on the bounded list ─────
# (DP 1.0.12 UI presentation task, §27 U/V: Recent/Downloads parity and
# boundedness are proven at this shared projection, never per-surface.)


def test_bounded_list_rows_carry_group_remaining_count(monkeypatch):
    rows = [
        _row(1, common_candidate_count=2, group_remaining_count=2),
        _row(2, common_candidate_count=2, group_remaining_count=0),
    ]
    db, result = _run_list(monkeypatch, 2, rows=rows)
    assert [kind for kind, _query, _params in db.calls] == ["fetchall", "fetchone"]
    assert [item["group_remaining_count"] for item in result["items"]] == [2, 0]


def test_group_remaining_count_is_normalized_to_non_negative_int(monkeypatch):
    _db, result = _run_list(monkeypatch, 1, rows=[_row(4, group_remaining_count=None)])
    assert result["items"][0]["group_remaining_count"] == 0


def test_display_name_prefers_artifact_filenames_over_root_name(monkeypatch):
    rows = [_row(1, filenames=["Example.Release.part01.rar", "Example.Release.part02.rar"])]
    _db, result = _run_list(monkeypatch, 1, rows=rows)
    item = result["items"][0]
    assert item["display_name"] == "Example.Release + 2 files"
    assert item["name"] == "Transfer 1"  # root/request name is preserved, untouched


def test_display_name_falls_back_to_root_name_when_no_artifact_filenames(monkeypatch):
    rows = [_row(1, filenames=None)]
    _db, result = _run_list(monkeypatch, 1, rows=rows)
    assert result["items"][0]["display_name"] == "Transfer 1"


def test_display_name_does_not_expose_raw_root_source_bookkeeping(monkeypatch):
    def _root_row(transfer_id):
        row = _row(transfer_id, filenames=["Movie.Title.2026.1080p.mkv"])
        row["name"] = "1fichier.com - abcdef123456 + 23 more"
        return row

    _db, result = _run_list(monkeypatch, 1, rows=[_root_row(7)])
    item = result["items"][0]
    assert item["display_name"] == "Movie.Title.2026.1080p.mkv"
    assert "1fichier.com" not in item["display_name"]
    # The root/request name is not destroyed -- still present as its own field.
    assert item["name"] == "1fichier.com - abcdef123456 + 23 more"


def test_display_name_and_remaining_count_do_not_add_db_calls_or_scale_with_page_size(monkeypatch):
    one_db, _one = _run_list(
        monkeypatch, 1, rows=[_row(1, filenames=["a.mkv", "b.mkv"], group_remaining_count=1)]
    )
    many_db, many = _run_list(
        monkeypatch, 40,
        rows=[_row(i, filenames=[f"file-{i}.mkv"], group_remaining_count=i % 2) for i in range(1, 41)],
    )
    assert len(one_db.calls) == 2
    assert len(many_db.calls) == 2
    assert [item["display_name"] for item in many["items"]] == [
        f"file-{i}.mkv" for i in range(1, 41)
    ]


def test_projection_filename_aggregate_reuses_current_artifact_identity(monkeypatch):
    """The filename aggregate must reuse group_member_artifacts, never a divergent definition."""
    db, _result = _run_list(monkeypatch, 1, rows=[_row(1)])
    projection_sql = db.calls[0][1]

    assert "group_member_filenames AS" in projection_sql
    assert "json_group_array(filename)" in projection_sql
    # It selects from group_member_artifacts -- the same current-artifact CTE
    # common-source membership and the remaining-work count both use -- never
    # a divergent re-derivation of the current-artifact WHERE clause.
    filenames_block = projection_sql.split("group_member_filenames AS", 1)[1].split(
        "group_member_hosts AS", 1
    )[0]
    assert "FROM group_member_artifacts" in filenames_block
    assert "download_files" not in filenames_block


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


@pytest.mark.asyncio
async def test_list_display_name_reflects_the_submitted_artifact_filename(projection_runtime):
    """Real-DB proof (not a fake row): display_name is artifact-derived, bounded."""
    engine, _repository = projection_runtime
    solo = await _submit(engine, "solo.bin", "rapidgator")
    await engine.resolve_pending()

    result, tracker = await _list_normal()
    by_id = {item["id"]: item for item in result["items"]}

    # Single current artifact -> its own filename is the display name, and the
    # root/request name is preserved untouched alongside it.
    assert by_id[solo.id]["display_name"] == "solo.bin"
    assert by_id[solo.id]["name"] == "solo.bin"
    assert isinstance(by_id[solo.id]["group_remaining_count"], int)

    # Bounded: exactly the projection read and the collection-count read --
    # the filename aggregate added no extra round-trip.
    assert [kind for kind, _q in tracker.calls] == ["fetchall", "fetchone"]


@pytest.mark.asyncio
async def test_list_remaining_count_excludes_non_switchable_states_but_includes_recoverable_error(projection_runtime):
    """Real-DB proof of the exact list-interactivity predicate (DP 1.0.12 §10).

    ``group_remaining_count`` must distinguish "unfinished work exists" from
    "meaningful remaining SWITCH work exists": a non-completed artifact whose
    own lifecycle state has no candidate-switch path (e.g. 'cancelled') must
    NOT count, so a transfer stuck there shows the static list indicator, not
    a dead chooser full of "not switchable" rows. A recoverable-looking
    failure ('error') DOES count -- this codebase's own established
    switch_eligible classification (transfers.repository
    ``_SWITCHABLE_ARTIFACT_STATES``, reused verbatim by the projection, not
    duplicated) already treats it that way for the comprehensive Details
    presentation, and the bounded list must agree.
    """
    engine, _repository = projection_runtime
    multi = await _submit(engine, "multi.bin", "rapidgator")
    await engine.resolve_pending()
    await _submit(engine, "multi.bin", "1fichier")  # consolidates onto `multi`: 2 common hosts
    await engine.resolve_pending()

    async with database.get_db() as db:
        row = await db.fetchone(
            "SELECT id FROM download_files WHERE torrent_id = ?", (multi.id,)
        )
        artifact_id = row["id"]

    async def _set_status(status):
        async with database.get_db() as db:
            await db.execute(
                "UPDATE download_files SET status = ? WHERE id = ?", (status, artifact_id)
            )
            await db.commit()

    # Case: unfinished (not completed) but genuinely non-switchable -> zero
    # meaningful remaining SWITCH work, even though common_candidate_count
    # still correctly reports the historical membership.
    await _set_status("cancelled")
    result, _tracker = await _list_normal()
    by_id = {item["id"]: item for item in result["items"]}
    assert by_id[multi.id]["common_candidate_count"] == 2
    assert by_id[multi.id]["group_remaining_count"] == 0

    # Case: recoverable failure -- still switchable, still counts.
    await _set_status("error")
    result, _tracker = await _list_normal()
    by_id = {item["id"]: item for item in result["items"]}
    assert by_id[multi.id]["common_candidate_count"] == 2
    assert by_id[multi.id]["group_remaining_count"] == 1

    # Case: a normal in-progress state also counts, as a control.
    await _set_status("downloading")
    result, _tracker = await _list_normal()
    by_id = {item["id"]: item for item in result["items"]}
    assert by_id[multi.id]["group_remaining_count"] == 1


# ── Target actionability, not just lifecycle switchability (DP 1.0.12 §10
#    reviewer follow-up) — a non-completed artifact in a "switchable" state
#    is NOT enough on its own; the list must also know at least one common
#    host actually has a currently-enabled provider behind it. ────────────


@pytest.mark.asyncio
async def test_list_remaining_count_is_zero_when_every_common_host_routes_through_a_disabled_provider(projection_runtime):
    """Real-DB, real-registry proof of the exact target-actionability gap.

    Lifecycle switchability (_SWITCHABLE_ARTIFACT_STATES) alone proves an
    artifact is in a state a switch would normally be attempted from -- it
    does NOT prove any target currently exists. A transfer with unfinished,
    switchable-state work and 2+ common hosts must still render the static
    list indicator (group_remaining_count == 0) when every one of those
    hosts routes through a provider that is not currently enabled: there is
    no possible target, so an interactive launcher would open a chooser full
    of dead rows. common_candidate_count (membership/history) is completely
    unaffected -- only the interactivity signal changes.
    """
    engine, _repository = projection_runtime
    multi = await _submit(engine, "multi.bin", "rapidgator")
    await engine.resolve_pending()
    await _submit(engine, "multi.bin", "1fichier")  # consolidates: 2 common hosts, one provider
    await engine.resolve_pending()

    provider = engine.registry.providers["provider-a"]
    application = SimpleNamespace(repository=_ExplodingRepository(), definitions=[], engine=engine)

    async def _list():
        return await downloads.list_operational_torrents(
            status=None, search=None, limit=0, offset=0, application=application,
        )

    # Baseline: provider enabled (fixture default) -> genuinely remaining work.
    result = await _list()
    by_id = {item["id"]: item for item in result["items"]}
    assert by_id[multi.id]["common_candidate_count"] == 2
    assert by_id[multi.id]["group_remaining_count"] == 1

    # Disable the provider backing every common host of this transfer --
    # durable, page-global (AppSettings-derived), not a live per-row check.
    provider.descriptor = replace(provider.descriptor, enabled=False)
    result = await _list()
    by_id = {item["id"]: item for item in result["items"]}
    assert by_id[multi.id]["common_candidate_count"] == 2  # history/membership unchanged
    assert by_id[multi.id]["group_remaining_count"] == 0   # no possible target -> static


@pytest.mark.asyncio
async def test_list_remaining_count_stays_positive_when_a_recoverable_failure_still_has_an_enabled_common_target(projection_runtime):
    """Companion GREEN case: an 'error' (recoverable-looking) artifact status
    with an enabled provider behind a common host still counts as remaining
    work -- interactive, not static."""
    engine, _repository = projection_runtime
    multi = await _submit(engine, "multi.bin", "rapidgator")
    await engine.resolve_pending()
    await _submit(engine, "multi.bin", "1fichier")
    await engine.resolve_pending()

    async with database.get_db() as db:
        row = await db.fetchone(
            "SELECT id FROM download_files WHERE torrent_id = ?", (multi.id,)
        )
        await db.execute(
            "UPDATE download_files SET status = 'error' WHERE id = ?", (row["id"],)
        )
        await db.commit()

    application = SimpleNamespace(repository=_ExplodingRepository(), definitions=[], engine=engine)
    result = await downloads.list_operational_torrents(
        status=None, search=None, limit=0, offset=0, application=application,
    )
    by_id = {item["id"]: item for item in result["items"]}
    assert by_id[multi.id]["common_candidate_count"] == 2
    assert by_id[multi.id]["group_remaining_count"] == 1


@pytest.mark.asyncio
async def test_provider_enablement_check_does_not_add_db_calls_or_scale_with_page_size(projection_runtime):
    """Bounded-list/N+1 invariant: the provider-enablement check reads the
    already-injected in-memory registry, never an extra DB round-trip, and
    never scales with row count."""
    engine, _repository = projection_runtime
    for index in range(6):
        await _submit(engine, f"file-{index}.bin", "rapidgator")
    await engine.resolve_pending()

    application = SimpleNamespace(repository=_ExplodingRepository(), definitions=[], engine=engine)

    async with _tracking_db() as tracker:
        await downloads.list_operational_torrents(
            status=None, search=None, limit=0, offset=0, application=application,
        )
    assert [kind for kind, _q in tracker.calls] == ["fetchall", "fetchone"]


def test_group_actionable_common_sources_is_derived_from_the_same_true_common_host_set(monkeypatch):
    """SQL-structure proof: the actionability gate reuses group_true_common_hosts
    (the exact same host set group_common_sources counts) rather than a
    divergent re-derivation, and group_common_sources itself is untouched."""
    db, _result = _run_list(monkeypatch, 1, rows=[_row(1)])
    projection_sql = db.calls[0][1]

    assert "group_true_common_hosts AS" in projection_sql
    assert "group_actionable_common_sources AS" in projection_sql
    # group_common_sources now reads straight from the shared host CTE --
    # still just a COUNT, no actionability/provider filter mixed in.
    common_sources_block = projection_sql.split("group_common_sources AS", 1)[1].split("GROUP BY common.transfer_id", 1)[0]
    assert "FROM group_true_common_hosts common" in common_sources_block
    assert "provider" not in common_sources_block
    assert "enabled" not in common_sources_block
    # The actionability CTE requires a movement-requiring, common, enabled
    # target -- not just "common host has an enabled provider" (which would
    # wrongly include an already-uniform ACTIVE source that needs zero
    # artifact movement). group_remaining_host_movement carries provider_id
    # through from group_member_hosts; the NOT IN (...) disabled-provider
    # clause itself is only interpolated when the request actually has a
    # disabled provider (empty here, since this fixture's application has no
    # .engine/.registry) -- what must always be present is the join path
    # that makes filtering on it possible, and the movement predicate.
    assert "group_remaining_host_movement AS" in projection_sql
    movement_block = projection_sql.split("group_remaining_host_movement AS", 1)[1].split(
        "group_actionable_common_sources AS", 1
    )[0]
    assert "gmh.candidate_id IS NOT gmsc.selected_candidate_id" in movement_block
    assert "gmh.provider_id" in movement_block
    actionable_block = projection_sql.split("group_actionable_common_sources AS", 1)[1]
    assert "JOIN group_remaining_host_movement movement" in actionable_block
    assert "movement.host = common.host" in actionable_block
    assert "b.provider_id AS provider_id" in projection_sql.split(
        "group_member_hosts AS", 1
    )[1].split("group_true_common_hosts AS", 1)[0]
    assert "b.candidate_id AS candidate_id" in projection_sql.split(
        "group_member_hosts AS", 1
    )[1].split("group_true_common_hosts AS", 1)[0]


class _FixedHostProvider(ParcelProvider):
    """A provider that always resolves to ONE fixed host, independent of its
    own request payload -- used to build two INDEPENDENTLY enable/disable-
    able providers that still consolidate onto the same artifact (shared
    integrity hash), so a "the ACTIVE host alone is not actionable" scenario
    can be constructed with genuinely different provider_ids per host."""

    def __init__(self, identity, host):
        super().__init__(identity)
        self._host = host

    def candidate_for(self, request):
        candidate = super().candidate(request.name or "same.bin", payload=self._host)
        return replace(
            candidate,
            integrity=(IntegrityMetadata("sha256", _SHA256),),
            source_identity=SourceIdentity("host", self._host),
        )

    async def resolve(self, request):
        self.calls.append(("resolve", request.payload))
        return ResolutionResult(ResourceState.AVAILABLE, (self.candidate_for(request),))


async def _submit_via(engine, provider_id, name, payload):
    return await engine.submit(
        (TransferRequest("parcel", payload, name=name, preferred_provider=provider_id),),
        name=name, deduplicate=False,
    )


@pytest.mark.asyncio
async def test_list_static_when_the_only_alternate_target_is_disabled_and_the_active_host_requires_no_movement(projection_runtime):
    """Counterexample proof (reviewer follow-up #2): the already-uniform
    ACTIVE source alone cannot satisfy list-level actionability, even though
    it is common and its own provider is enabled -- it requires ZERO
    artifact movement. Two common hosts A (enabled, currently selected by
    every remaining-work artifact) and B (disabled) -> no movement-requiring
    AND enabled target exists -> static, not a dead interactive chooser.
    """
    engine, _repository = projection_runtime
    provider_a = _FixedHostProvider("provider-a-fixed", "host-a.example")
    provider_b = _FixedHostProvider("provider-b-fixed", "host-b.example")
    engine.registry.register_provider(provider_a)
    engine.registry.register_provider(provider_b)

    # First submission (provider A) becomes the durably SELECTED candidate
    # (index 0); the second (provider B) consolidates in as an alternate.
    multi = await _submit_via(engine, "provider-a-fixed", "multi.bin", "seed-a")
    await engine.resolve_pending()
    await _submit_via(engine, "provider-b-fixed", "multi.bin", "seed-b")
    await engine.resolve_pending()

    application = SimpleNamespace(repository=_ExplodingRepository(), definitions=[], engine=engine)

    async def _list():
        return await downloads.list_operational_torrents(
            status=None, search=None, limit=0, offset=0, application=application,
        )

    # Baseline: both providers enabled. Remaining work exists and B is a
    # genuine (movement-requiring, enabled) alternate to A -> interactive.
    result = await _list()
    by_id = {item["id"]: item for item in result["items"]}
    assert by_id[multi.id]["common_candidate_count"] == 2
    assert by_id[multi.id]["group_remaining_count"] == 1

    # Disable B -- the ONLY host that would require movement. A (already
    # uniformly selected, zero movement) is common and enabled but that is
    # NOT enough: there is no possible switch target left at all.
    provider_b.descriptor = replace(provider_b.descriptor, enabled=False)
    result = await _list()
    by_id = {item["id"]: item for item in result["items"]}
    assert by_id[multi.id]["common_candidate_count"] == 2  # history/membership unchanged
    assert by_id[multi.id]["group_remaining_count"] == 0   # no possible movement -> static


@pytest.mark.asyncio
async def test_list_interactive_when_the_alternate_target_requires_movement_and_is_enabled(projection_runtime):
    """Positive inverse of the above: same already-uniform-on-A setup, but B
    stays enabled -> switching every remaining participant to B would issue
    >=1 real POST, so the launcher is correctly interactive."""
    engine, _repository = projection_runtime
    provider_a = _FixedHostProvider("provider-a-fixed2", "host-a2.example")
    provider_b = _FixedHostProvider("provider-b-fixed2", "host-b2.example")
    engine.registry.register_provider(provider_a)
    engine.registry.register_provider(provider_b)

    multi = await _submit_via(engine, "provider-a-fixed2", "multi.bin", "seed-a")
    await engine.resolve_pending()
    await _submit_via(engine, "provider-b-fixed2", "multi.bin", "seed-b")
    await engine.resolve_pending()

    application = SimpleNamespace(repository=_ExplodingRepository(), definitions=[], engine=engine)
    result = await downloads.list_operational_torrents(
        status=None, search=None, limit=0, offset=0, application=application,
    )
    by_id = {item["id"]: item for item in result["items"]}
    assert by_id[multi.id]["common_candidate_count"] == 2
    assert by_id[multi.id]["group_remaining_count"] == 1  # B is a genuine, actionable alternate


# ── Universal convergence: EVERY remaining-work participant (the shipped
#    chooser's own scope -- only completed is excluded, ui-group-candidates.js
#    computeGroup's actionParticipants) must be able to converge on a target,
#    not just "some switchable participant would move there" (DP 1.0.12 §10,
#    reviewer follow-up #3). ─────────────────────────────────────────────


class _TwoHostProvider(ParcelProvider):
    """Resolves to whichever host/integrity-hash the request payload encodes
    (``"<label>:<host>:<sha256hex>"``) -- used to build a real two-artifact
    transfer (via one multi-request submit) where each artifact independently
    consolidates a second host candidate through a second provider."""

    def candidate_for(self, request, host, sha256hex):
        candidate = super().candidate(request.name or "file.bin", payload=host)
        return replace(
            candidate,
            integrity=(IntegrityMetadata("sha256", sha256hex),),
            source_identity=SourceIdentity("host", host),
        )

    async def resolve(self, request):
        self.calls.append(("resolve", request.payload))
        _label, host, sha256hex = str(request.payload).split(":", 2)
        return ResolutionResult(ResourceState.AVAILABLE, (self.candidate_for(request, host, sha256hex),))


async def _two_artifact_two_host_transfer(engine, *, provider_a_id, provider_b_id):
    """Real two-artifact transfer: both artifacts start selected on host A
    (via provider_a_id), each independently consolidates an alternate
    candidate on host B (via provider_b_id). Returns (transfer, artifact_ids)
    with artifact_ids in filename order [file1, file2]."""
    h1, h2 = "1" * 64, "2" * 64
    req1 = TransferRequest("parcel", f"file1:host-a.example:{h1}", name="file1.bin", preferred_provider=provider_a_id)
    req2 = TransferRequest("parcel", f"file2:host-a.example:{h2}", name="file2.bin", preferred_provider=provider_a_id)
    transfer = await engine.submit((req1, req2), name="batch", deduplicate=False)
    await engine.resolve_pending()

    req1b = TransferRequest("parcel", f"file1:host-b.example:{h1}", name="file1.bin", preferred_provider=provider_b_id)
    req2b = TransferRequest("parcel", f"file2:host-b.example:{h2}", name="file2.bin", preferred_provider=provider_b_id)
    await engine.submit((req1b,), name="file1.bin", deduplicate=False)
    await engine.submit((req2b,), name="file2.bin", deduplicate=False)
    await engine.resolve_pending()

    async with database.get_db() as db:
        rows = await db.fetchall(
            "SELECT id, filename FROM download_files WHERE torrent_id = ? ORDER BY filename",
            (transfer.id,),
        )
    by_filename = {row["filename"]: row["id"] for row in rows}
    return transfer, [by_filename["file1.bin"], by_filename["file2.bin"]]


@pytest.mark.asyncio
async def test_list_static_when_a_non_switchable_participant_can_never_converge_on_the_only_movement_target(projection_runtime):
    """RED->GREEN, real two-artifact transfer, real registry.

    Two current, unfinished participants; common hosts A and B; both
    currently selected on A. Artifact 1 is switchable (queued -- the default
    post-submit state). Artifact 2 is 'cancelled': a genuine remaining-work
    participant under the SHIPPED CHOOSER's own scope (ui-group-candidates.js
    computeGroup's actionParticipants excludes ONLY completed -- confirmed by
    direct inspection before writing this fix), but not switch-eligible for
    B (not in _SWITCHABLE_ARTIFACT_STATES) and not already selected there.

    Required: common_candidate_count stays 2 (membership/history intact);
    B is enabled but is NOT offered, because artifact 2 can never converge
    there; A requires no movement (both already there); so there is no
    actionable target at all -- static, not a dead chooser.
    """
    engine, _repository = projection_runtime
    provider_a = _TwoHostProvider("provider-a-univ")
    provider_b = _TwoHostProvider("provider-b-univ")
    engine.registry.register_provider(provider_a)
    engine.registry.register_provider(provider_b)

    transfer, (artifact1, artifact2) = await _two_artifact_two_host_transfer(
        engine, provider_a_id="provider-a-univ", provider_b_id="provider-b-univ",
    )

    # Artifact 2 becomes a non-switchable (but non-completed) participant --
    # still counted by membership, still a chooser action participant, but
    # it can never converge on B by a candidate switch.
    async with database.get_db() as db:
        await db.execute("UPDATE download_files SET status = 'cancelled' WHERE id = ?", (artifact2,))
        await db.commit()

    application = SimpleNamespace(repository=_ExplodingRepository(), definitions=[], engine=engine)
    result = await downloads.list_operational_torrents(
        status=None, search=None, limit=0, offset=0, application=application,
    )
    by_id = {item["id"]: item for item in result["items"]}
    assert by_id[transfer.id]["common_candidate_count"] == 2
    assert by_id[transfer.id]["group_remaining_count"] == 0


@pytest.mark.asyncio
async def test_list_interactive_when_every_remaining_participant_can_converge_on_the_movement_target(projection_runtime):
    """Positive inverse, real two-artifact transfer, real registry.

    Same setup: both artifacts currently on A, B common and enabled. This
    time BOTH artifacts stay in a switchable lifecycle state (the default
    post-submit 'queued'), so every remaining-work participant can genuinely
    converge on B, and switching would issue >=1 real POST -> interactive.
    """
    engine, _repository = projection_runtime
    provider_a = _TwoHostProvider("provider-a-univ2")
    provider_b = _TwoHostProvider("provider-b-univ2")
    engine.registry.register_provider(provider_a)
    engine.registry.register_provider(provider_b)

    transfer, _artifact_ids = await _two_artifact_two_host_transfer(
        engine, provider_a_id="provider-a-univ2", provider_b_id="provider-b-univ2",
    )

    application = SimpleNamespace(repository=_ExplodingRepository(), definitions=[], engine=engine)
    result = await downloads.list_operational_torrents(
        status=None, search=None, limit=0, offset=0, application=application,
    )
    by_id = {item["id"]: item for item in result["items"]}
    assert by_id[transfer.id]["common_candidate_count"] == 2
    assert by_id[transfer.id]["group_remaining_count"] == 2
