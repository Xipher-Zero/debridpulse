"""DP 1.0.13 -- the compact provider badge names the ACTIVE provider.

Downloads and Dashboard Recent Activity name one provider per transfer: the
provider executing its work, or -- once no writer is live, including after
completion and restart -- the provider of its last execution activity
(execution authority). Only a transfer that never executed falls back to its
live routes, and only when they name one provider. Neither the latest route
ordinal, the last resolution attempt, the delivery majority nor the origin
decides it; an attempt a later one replaced never counts; different providers
at the same recorded instant are ambiguous, never tie-broken.

Both read models derive it -- the bounded list projection in its one SQL read
(``api.operational_downloads``) and the Details presentation
(``transfers._repository_base.active_provider``) -- and the browser's one badge
owner (``transferProviderPresentation``) only consumes it. Rows are seeded as
durable state directly, so every read is a recreation from durable records.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from db.database import get_db
from integrations.definition import IntegrationPresentation
from transfers.recovery_repository import TransferRepository

ROOT = Path(__file__).resolve().parents[2]
DEFINITIONS = [SimpleNamespace(id=identity, name=name, presentation=IntegrationPresentation())
               for identity, name in (("alldebrid", "AllDebrid"), ("debridlink", "Debrid-Link"),
                                      ("premiumize", "Premiumize"), ("torbox", "TorBox"))]


@pytest_asyncio.fixture
async def stage(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "active-provider.sqlite3")
    await database.init_db()
    return TransferRepository()


async def _seed(transfer_id, status, routes=(), executions=()):
    """``routes``: (request, provider, ordinal, resolution_state, at).
    ``executions``: (attempt, artifact, ordinal, provider, state, authorized,
    started, ended, delivered)."""
    async with get_db() as db:
        await db.execute("INSERT INTO torrents(id,hash,name,status,source,created_at,completed_at) VALUES(?,?,?,?,?,?,?)",
                         (transfer_id, f"request:{transfer_id}", f"transfer {transfer_id}", status, "direct_link",
                          "2026-10-09 08:16:03", "2026-10-09 09:13:48" if status == "completed" else None))
        for index, (request, provider, ordinal, state, at) in enumerate(routes):
            await db.execute("INSERT OR IGNORE INTO transfer_requests(id,transfer_id,ordinal,payload,state) VALUES(?,?,?,?,?)",
                             (request, transfer_id, index, '{"kind":"https","payload":"https://h.example/x"}', "resolved"))
            attempt = f"{request}:{ordinal}"
            await db.execute("INSERT INTO resolution_attempts(id,request_id,provider_id,state,created_at,updated_at) "
                             "VALUES(?,?,?,?,?,?)", (attempt, request, provider, state, at, at))
            await db.execute("INSERT INTO route_attempt_provenance(resolution_attempt_id,transfer_id,request_id,ordinal,"
                             "operation,outcome,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                             (attempt, transfer_id, request, ordinal, "resolve",
                              "declined" if state == "declined" else "resolved", at, at))
        for attempt, artifact, ordinal, provider, state, authorized, started, ended, delivered in executions:
            await db.execute("INSERT OR IGNORE INTO download_files(id,torrent_id,filename,size_bytes,status,mirror_group_id,"
                             "mirror_state) VALUES(?,?,?,?,?,?,?)",
                             (artifact, transfer_id, f"part{artifact}.rar", 4,
                              "completed" if status == "completed" else "downloading", artifact, "primary"))
            await db.execute("INSERT INTO execution_attempts(id,transfer_id,artifact_id,executor_id,handle,state,authorized,"
                             "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                             (attempt, transfer_id, artifact, "aria2", "{}", state, int(authorized), started, ended))
            await db.execute("INSERT INTO execution_attempt_provenance(execution_attempt_id,transfer_id,artifact_id,ordinal,"
                             "provider_id,outcome,delivered,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                             (attempt, transfer_id, artifact, ordinal, provider,
                              "completed" if delivered else state, int(delivered), started, ended))
        await db.commit()


async def _listed(transfer_id, *, activity=False):
    from api import operational_downloads as downloads
    page = await downloads.list_operational_torrents(
        status=None, search=None, limit=25, offset=0, order="activity" if activity else None,
        application=SimpleNamespace(repository=None, definitions=DEFINITIONS, engine=None))
    return next(item for item in page["items"] if int(item["id"]) == transfer_id)


async def _active(repository, transfer_id):
    """The one fact from every read model: Downloads, Recent Activity, Details."""
    from api.routes import _public_transfer_presentation
    details = _public_transfer_presentation(await repository.presentation(transfer_id, details=True), DEFINITIONS)
    reads = [await _listed(transfer_id), await _listed(transfer_id, activity=True), details]
    facts = {(item["active_provider_id"], item.get("active_provider_name"), item["active_provider_basis"])
             for item in reads}
    assert len(facts) == 1, facts
    return next(iter(facts)), details


# Transfer 580's shape: AllDebrid routes first (ordinals 1-2), Debrid-Link's
# later (3-4); Debrid-Link delivers most artifacts; artifact 3 is switched to
# Debrid-Link, then back to AllDebrid twice, and AllDebrid delivers it last.
SHAPE_580_ROUTES = (
    ("r-ad-1", "alldebrid", 1, "succeeded", "2026-10-09 08:16:03"),
    ("r-ad-2", "alldebrid", 2, "succeeded", "2026-10-09 08:16:04"),
    ("r-dl-1", "debridlink", 3, "succeeded", "2026-10-09 08:16:20"),
    ("r-dl-2", "debridlink", 4, "succeeded", "2026-10-09 08:16:25"),
)
SHAPE_580_EXECUTIONS = (
    ("e1-ad", 1, 1, "alldebrid", "cancelled", 0, "2026-10-09 08:16:09", "2026-10-09 08:17:04", False),
    ("e1-dl", 1, 2, "debridlink", "succeeded", 1, "2026-10-09 08:17:09", "2026-10-09 08:28:26", True),
    ("e2-dl", 2, 1, "debridlink", "succeeded", 1, "2026-10-09 08:49:40", "2026-10-09 08:54:20", True),
    ("e3-dl", 3, 1, "debridlink", "cancelled", 0, "2026-10-09 08:48:55", "2026-10-09 08:55:30", False),
    ("e3-ad", 3, 2, "alldebrid", "cancelled", 0, "2026-10-09 08:55:33", "2026-10-09 08:57:05", False),
    ("e3-ad2", 3, 3, "alldebrid", "succeeded", 1, "2026-10-09 08:57:08", "2026-10-09 08:58:17", True),
)


@pytest.mark.asyncio
async def test_a_580_shaped_completed_transfer_names_its_last_execution_provider(stage):
    await _seed(580, "completed", SHAPE_580_ROUTES, SHAPE_580_EXECUTIONS)
    fact, details = await _active(stage, 580)
    assert fact == ("alldebrid", "AllDebrid", "execution")
    # Not the latest route ordinal, the delivery majority or the origin, and
    # those facts themselves are unchanged beside it.
    listed = await _listed(580)
    assert listed["current_provider_id"] == "debridlink"
    assert listed["delivering_provider_id"] is None and listed["origin_provider_id"] is None
    assert listed["provider_provenance_status"] == "recorded"
    # Details keeps every route and execution, with its public row shape.
    assert {item["provider_id"] for item in details["route_attempts"]} == {"alldebrid", "debridlink"}
    assert len(details["execution_attempts"]) == len(SHAPE_580_EXECUTIONS)
    for item in details["execution_attempts"]:
        assert not {"authorized", "provenance_created_at", "provenance_updated_at"} & set(item)
    assert sorted(details["delivering_provider_ids"]) == ["alldebrid", "debridlink"]


@pytest.mark.asyncio
async def test_active_then_completed_keeps_the_same_identity(stage):
    live = SHAPE_580_EXECUTIONS[:-1] + (
        ("e3-ad2", 3, 3, "alldebrid", "running", 1, "2026-10-09 08:57:08", "2026-10-09 08:58:00", False),)
    await _seed(580, "downloading", SHAPE_580_ROUTES, live)
    assert (await _active(stage, 580))[0] == ("alldebrid", "AllDebrid", "execution")
    async with get_db() as db:
        await db.execute("UPDATE execution_attempts SET state='succeeded',updated_at='2026-10-09 08:58:17' WHERE id='e3-ad2'")
        await db.execute("UPDATE execution_attempt_provenance SET outcome='completed',delivered=1,"
                         "updated_at='2026-10-09 08:58:17' WHERE execution_attempt_id='e3-ad2'")
        # Retirement clears the live flag; the history still decides.
        await db.execute("UPDATE execution_attempts SET authorized=0 WHERE transfer_id=580")
        await db.execute("UPDATE torrents SET status='completed',completed_at='2026-10-09 09:13:48' WHERE id=580")
        await db.commit()
    assert (await _active(TransferRepository(), 580))[0] == ("alldebrid", "AllDebrid", "execution")


@pytest.mark.asyncio
async def test_live_writers_outrank_history_and_the_latest_started_one_is_active(stage):
    await _seed(41, "downloading", (("r", "alldebrid", 1, "succeeded", "2026-10-09 10:00:00"),), (
        ("done", 1, 1, "torbox", "succeeded", 1, "2026-10-09 09:00:00", "2026-10-09 10:30:00", True),
        ("p1", 2, 1, "premiumize", "running", 1, "2026-10-09 10:00:00", "2026-10-09 10:31:00", False),
        ("p2", 3, 1, "debridlink", "queued", 1, "2026-10-09 10:10:00", "2026-10-09 10:10:00", False),
    ))
    assert (await _active(stage, 41))[0] == ("debridlink", "Debrid-Link", "execution")


@pytest.mark.asyncio
async def test_the_four_competing_definitions_diverge_and_execution_activity_decides(stage):
    # Latest execution START: Premiumize (10:10). Latest activity END: TorBox
    # (10:30). Latest committed route ordinal: AllDebrid (3). Latest
    # resolution attempt: Debrid-Link, which declined (ordinal 4).
    await _seed(42, "completed", (
        ("r1", "torbox", 1, "succeeded", "2026-10-09 09:59:00"),
        ("r2", "premiumize", 2, "succeeded", "2026-10-09 09:59:30"),
        ("r3", "alldebrid", 3, "succeeded", "2026-10-09 10:40:00"),
        ("r3", "debridlink", 4, "declined", "2026-10-09 10:41:00"),
    ), (
        ("long", 1, 1, "torbox", "succeeded", 0, "2026-10-09 10:00:00", "2026-10-09 10:30:00", True),
        ("short", 2, 1, "premiumize", "succeeded", 0, "2026-10-09 10:10:00", "2026-10-09 10:20:00", True),
    ))
    assert (await _active(stage, 42))[0] == ("torbox", "TorBox", "execution")


@pytest.mark.asyncio
async def test_a_replaced_attempt_never_steals_the_badge(stage):
    # The newest-touched attempt of artifact 1 is a cancelled one that a later
    # attempt (ordinal 2) replaced; only the artifact's own final attempt counts.
    await _seed(43, "completed", (("r", "alldebrid", 1, "succeeded", "2026-10-09 10:00:00"),), (
        ("old", 1, 1, "debridlink", "cancelled", 0, "2026-10-09 10:00:00", "2026-10-09 10:50:00", False),
        ("new", 1, 2, "alldebrid", "succeeded", 0, "2026-10-09 10:01:00", "2026-10-09 10:40:00", True),
    ))
    assert (await _active(stage, 43))[0] == ("alldebrid", "AllDebrid", "execution")


@pytest.mark.asyncio
async def test_different_providers_at_the_same_instant_are_ambiguous_never_tie_broken(stage):
    await _seed(44, "completed", (("r", "alldebrid", 1, "succeeded", "2026-10-09 10:00:00"),), (
        ("a", 1, 1, "alldebrid", "succeeded", 0, "2026-10-09 10:00:00", "2026-10-09 10:30:00", True),
        ("b", 2, 1, "debridlink", "succeeded", 0, "2026-10-09 10:00:01", "2026-10-09 10:30:00", True),
    ))
    assert (await _active(stage, 44))[0] == (None, None, "ambiguous")
    # The same provider at the same instant is not a tie.
    await _seed(45, "completed", (("s", "alldebrid", 1, "succeeded", "2026-10-09 10:00:00"),), (
        ("c", 3, 1, "alldebrid", "succeeded", 0, "2026-10-09 10:00:00", "2026-10-09 10:30:00", True),
        ("d", 4, 1, "alldebrid", "succeeded", 0, "2026-10-09 10:00:01", "2026-10-09 10:30:00", True),
    ))
    assert (await _active(stage, 45))[0] == ("alldebrid", "AllDebrid", "execution")


@pytest.mark.asyncio
async def test_a_single_provider_transfer_is_unchanged(stage):
    await _seed(46, "completed", (("r", "alldebrid", 1, "succeeded", "2026-10-09 10:00:00"),), (
        ("a", 1, 1, "alldebrid", "succeeded", 0, "2026-10-09 10:00:01", "2026-10-09 10:30:00", True),
    ))
    assert (await _active(stage, 46))[0] == ("alldebrid", "AllDebrid", "execution")
    listed = await _listed(46)
    assert (listed["origin_provider_id"], listed["current_provider_id"], listed["delivering_provider_id"]) == (
        "alldebrid", "alldebrid", "alldebrid")


@pytest.mark.asyncio
async def test_without_execution_only_an_unambiguous_live_route_names_the_provider(stage):
    await _seed(47, "processing", (("r1", "alldebrid", 1, "succeeded", "2026-10-09 10:00:00"),
                                   ("r2", "alldebrid", 2, "started", "2026-10-09 10:00:01")))
    assert (await _active(stage, 47))[0] == ("alldebrid", "AllDebrid", "route")
    await _seed(48, "processing", (("s1", "alldebrid", 1, "succeeded", "2026-10-09 10:00:00"),
                                   ("s2", "debridlink", 2, "succeeded", "2026-10-09 10:00:01")))
    assert (await _active(stage, 48))[0] == (None, None, "none")
    # A provider that declined its request never routes it; an ended route is nobody's.
    await _seed(49, "processing", (("t1", "debridlink", 1, "declined", "2026-10-09 10:00:00"),
                                   ("t2", "alldebrid", 2, "exhausted", "2026-10-09 10:00:01")))
    assert (await _active(stage, 49))[0] == (None, None, "none")


@pytest.mark.asyncio
async def test_nothing_recorded_stays_honestly_absent(stage):
    await _seed(50, "completed")
    assert (await _active(stage, 50))[0] == (None, None, "none")
    await _seed(51, "pending")
    assert (await _active(stage, 51))[0] == (None, None, "none")


def test_one_browser_owner_renders_the_projection_for_both_lists():
    app = (ROOT / "frontend/static/app.js").read_text(encoding="utf-8")
    body = app.split("function transferProviderPresentation(t) {", 1)[1].split("\n}\n", 1)[0]
    assert "t?.active_provider_id" in body and "t?.active_provider_basis === 'ambiguous'" in body
    for retired in ("origin_provider", "delivering_provider", "current_provider", "provider_provenance_status"):
        assert retired not in body, retired
    # The single-root torrent route badge (and its switch) keeps its own owner.
    assert "route_provider_id" in body
    chip = app.split("function providerChip(t, surface) {", 1)[1].split("\n}\n", 1)[0]
    assert "window.DPRootProvider.badgeMarkup(t, surface)" in chip and "transferProviderPresentation(t)" in chip
    # Downloads and Recent Activity both render that one chip from the list row.
    downloads = (ROOT / "frontend/static/ui-downloads.js").read_text(encoding="utf-8")
    recent = (ROOT / "frontend/static/ui-dashboard-transfer-presentation.js").read_text(encoding="utf-8")
    assert "providerChip(t, 'downloads')" in downloads
    assert "providerChip(t,'dashboard_recent')" in recent
    for surface in (downloads, recent):
        assert "active_provider" not in surface
