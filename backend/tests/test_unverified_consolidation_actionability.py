"""1.0.14 WS2: a consolidated contributor whose association is still owed
proof is presented truthfully, and its operator Retry is reachable from the
target's Details.

Transfer 1279's shape (investigations/dp-1.0.14-remaining-correctness-
closeout, item 3): a Debrid-Link file under a quota hold, and a late source on
another provider that states no name or size. Its proof needs a sample of the
held source, which C2 does not issue while the hold is current, so the bounded
reconsideration series runs out: the late transfer settles ``consolidated``
with an ``unverified`` association to the held file. Before this change it
read as an ordinary green "Consolidated", and the operator Retry that
reconsiders it (``TransferEngine._reconsider_unverified_associations``) had no
control anywhere: a consolidated transfer is in no Downloads filter.

Everything runs through the real HTTP routes with the one authorization
dependency (``auth.permissions.authorize_request``) applied, the real engine,
repository and policy, and the Debrid-Link client, provider and translation
over an in-memory transport on a scratch database.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from test_quota_scope_holds import (
    BODIES,
    HERE,
    THERE,
    DebridLinkHTTP,
    build,
    cycle,
    start,
)

import db.database as database
from api.operational_downloads import router as downloads_router
from api.routes import router
from application.service import ApplicationService
from auth.models import Principal
from auth.permissions import Role, authorize_request
from transfers.errors import TransferError
from transfers.models import TransferRequest

pytestmark = pytest.mark.asyncio

FILTERS = ("", "downloading", "paused", "processing", "ready", "completed", "error")   # index.html's tabs


async def exhausted(tmp_path, monkeypatch, *, same=True, attested=False, recovers=True):
    """The held file (``target``) and the late equivalent (``late``) after a
    four-hour hold -- longer than the reconsideration series -- and, unless
    ``recovers`` is false, an hour after the quota came back."""
    if not same:
        monkeypatch.setitem(BODIES, "other.example/movie.bin", b"diff")
    http = DebridLinkHTTP("maxData", warm={"https://hoster.example/f/movie.bin": 1})
    engine, repository, executor, clock = await start(tmp_path, monkeypatch, http, attested=attested)
    target = await engine.submit((TransferRequest("https", HERE, "movie.bin"),), name="movie")
    await engine.tick()
    began = clock()
    late = await engine.submit((TransferRequest("https", THERE, "movie.bin"),), name="movie-elsewhere")
    while clock() < began + 4 * 3600:
        await cycle(engine, clock, 300.0)
    http.recovered = recovers
    while clock() < began + 5 * 3600:
        await cycle(engine, clock, 300.0)
    return SimpleNamespace(engine=engine, repository=repository, executor=executor, clock=clock, http=http,
                           target=target, late=late, tmp_path=tmp_path)


@asynccontextmanager
async def api(engine, role=Role.ADMIN, account_id="acct-admin"):
    """The routed application as ``main`` assembles it, for one principal."""
    application = ApplicationService(engine)
    app = FastAPI(dependencies=[Depends(authorize_request)])
    caller = {"principal": Principal(authenticated=True, subject=account_id, account_id=account_id, role=role.value)}

    @app.middleware("http")
    async def principal(request, call_next):
        request.state.principal = caller["principal"]
        return await call_next(request)

    @app.exception_handler(TransferError)
    async def failure(_request, exc):
        return JSONResponse(status_code=409, content={"detail": exc.error.message, "error": exc.error.as_dict()})

    app.state.application = application
    app.include_router(downloads_router, prefix="/api")
    app.include_router(router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, caller


async def detail(client, transfer_id):
    response = await client.get(f"/api/torrents/{transfer_id}")
    assert response.status_code == 200, response.text
    return response.json()


def shown(item):
    return (item["status"], item["presentation_status"], item["presentation_label"], item["presentation_badge_status"])


def contributed_rows(target_detail, contributor_id):
    return [row for row in target_detail["route_attempts"] if row.get("contributing_transfer_id") == contributor_id]


async def canonical_consumers(client):
    """What filters, Statistics and the event stream classify: Downloads
    membership per tab, the summary and detailed Statistics, and every
    application event so far (notifications are dispatched from these)."""
    membership = {}
    for status in FILTERS:
        response = await client.get("/api/torrents", params={"status": status} if status else {})
        membership[status] = sorted(item["id"] for item in response.json()["items"])
    stats = (await client.get("/api/stats")).json()
    disposition = (await client.get("/api/stats/detail")).json()
    async with database.get_db() as db:
        events = [tuple(row) for row in await db.fetchall("SELECT id,transfer_id,kind FROM application_events")]
    return membership, stats, disposition, events


async def test_an_unverified_contribution_is_truthful_and_reconsidered_from_the_target_row(tmp_path, monkeypatch):
    lab = await exhausted(tmp_path, monkeypatch)
    # The hold's clearing woke nothing: no sample of the late source was issued
    # (resolution, the refusals, the clearing recheck -- all the held file's).
    assert all(link == HERE for _refused, link in lab.http.adds)
    async with api(lab.engine) as (client, _caller):
        before = await canonical_consumers(client)
        late = await detail(client, lab.late.id)
        assert shown(late) == ("consolidated", "consolidated", "Consolidated · Unverified", "partial")
        assert late["unverified_association_count"] == 1
        [diagnostic] = (await client.get("/api/torrents", params={"status": "consolidated"})).json()["items"]
        assert shown(diagnostic) == shown(late) and diagnostic["unverified_association_count"] == 1
        [row] = contributed_rows(await detail(client, lab.target.id), lab.late.id)
        assert (row["relation"], row["verification_state"], row["reconsider_available"]) == (
            "unverified", "unverified", True)
        # Presentation only: the same canonical classification everywhere, and
        # reading it wrote nothing (no event, so no notification).
        assert await canonical_consumers(client) == before
        membership, stats, disposition, _events = before
        assert all(lab.late.id not in members for members in membership.values())
        assert stats["by_status"].get("consolidated") == 1
        assert {row["status"]: row["count"] for row in disposition["torrent_status"]}["consolidated"] == 1

        # A restart keeps it (durable state only); the operator's Retry
        # reconsiders it: the same bytes join the held file's artifact.
        lab.engine, lab.repository, lab.executor = build(lab.tmp_path, lab.http, lab.clock)
        await lab.engine.initialize()
    async with api(lab.engine) as (client, _caller):
        assert (await client.post(f"/api/torrents/{lab.late.id}/retry")).status_code == 200
        for _ in range(6):
            await cycle(lab.engine, lab.clock, 60.0)
        late = await detail(client, lab.late.id)
        assert shown(late) == ("consolidated", "consolidated", "Consolidated", "consolidated")
        assert late["unverified_association_count"] == 0
        assert not any(row.get("reconsider_available") for row in await_rows(await detail(client, lab.target.id)))
        assert not await lab.repository.executions(lab.late.id)
        # One writer for the file (the restart's own in-memory executor lost
        # the job, so its attempt was replaced; never two authorized at once).
        assert await authorized(lab.target.id) == 1
        # Resolved: a stale view's Retry is refused, truthfully, and changes nothing.
        stale = await client.post(f"/api/torrents/{lab.late.id}/retry")
        assert stale.status_code == 409
        assert shown(await detail(client, lab.late.id)) == shown(late)


async def authorized(transfer_id):
    async with database.get_db() as db:
        row = await db.fetchone("SELECT COUNT(*) AS n FROM execution_attempts WHERE transfer_id=? AND authorized=1",
                                (transfer_id,))
    return int(row["n"])


def await_rows(target_detail):
    return target_detail["route_attempts"]


async def test_contradictory_content_gets_its_own_single_download(tmp_path, monkeypatch):
    lab = await exhausted(tmp_path, monkeypatch, same=False)
    async with api(lab.engine) as (client, _caller):
        assert (await client.post(f"/api/torrents/{lab.late.id}/retry")).status_code == 200
        for _ in range(6):
            await cycle(lab.engine, lab.clock, 60.0)
        late = await detail(client, lab.late.id)
        assert late["status"] == "downloading" and "unverified_association_count" not in late
        assert len(await lab.repository.executions(lab.late.id)) == 1 and await authorized(lab.late.id) == 1
        assert await authorized(lab.target.id) == 1
        assert not contributed_rows(await detail(client, lab.target.id), lab.late.id)
        # Now ordinary work, it is in Downloads like any other.
        listed = (await client.get("/api/torrents")).json()["items"]
        assert lab.late.id in [item["id"] for item in listed]


async def test_without_evidence_a_retry_stays_unverified_and_attaches_nothing(tmp_path, monkeypatch):
    lab = await exhausted(tmp_path, monkeypatch, recovers=False)
    async with api(lab.engine) as (client, _caller):
        assert (await client.post(f"/api/torrents/{lab.late.id}/retry")).status_code == 200
        for _ in range(4):
            await cycle(lab.engine, lab.clock, 30.0)
        # Reconsidering while the hold is still current: no sample is issued,
        # nothing is attached, no writer starts, and no success is shown.
        assert all(link == HERE for _refused, link in lab.http.adds)
        assert not await lab.repository.executions(lab.late.id)
        late = await detail(client, lab.late.id)
        assert late["presentation_label"] != "Consolidated"
        async with database.get_db() as db:
            [row] = [dict(item) for item in await db.fetchall(
                "SELECT equivalence_disposition FROM transfer_requests WHERE transfer_id=?", (lab.late.id,))]
        assert row["equivalence_disposition"] in {"pending", "unverified"}
        [association] = contributed_rows(await detail(client, lab.target.id), lab.late.id)
        assert association["verification_state"] == "unverified"
        # While its own reconsideration is under way it offers no second Retry.
        assert association["reconsider_available"] is (late["status"] == "consolidated")


async def test_only_an_operator_may_retry_and_the_server_decides(tmp_path, monkeypatch):
    lab = await exhausted(tmp_path, monkeypatch)
    for role in (Role.READONLY, Role.INTEGRATION):
        async with api(lab.engine, role=role, account_id="acct-other") as (client, _caller):
            assert (await client.post(f"/api/torrents/{lab.late.id}/retry")).status_code == 403
    async with api(lab.engine) as (client, _caller):
        assert shown(await detail(client, lab.late.id))[2] == "Consolidated · Unverified"
    # The existing operate rule is a capability, not an ownership: a User of
    # another account may retry, exactly as a Downloads Retry.
    async with api(lab.engine, role=Role.USER, account_id="acct-other") as (client, _caller):
        assert (await client.post(f"/api/torrents/{lab.late.id}/retry")).status_code == 200


async def test_concurrent_retries_reconsider_once_and_never_attach_twice(tmp_path, monkeypatch):
    lab = await exhausted(tmp_path, monkeypatch)
    async with api(lab.engine) as (client, _caller):
        answers = await asyncio.gather(*(client.post(f"/api/torrents/{lab.late.id}/retry") for _ in range(2)))
        assert min(answer.status_code for answer in answers) == 200
        assert all(answer.status_code in {200, 409} for answer in answers)
        for _ in range(6):
            await cycle(lab.engine, lab.clock, 60.0)
        async with database.get_db() as db:
            consolidations = await db.fetchall(
                "SELECT canonical_artifact_id FROM artifact_consolidations WHERE source_transfer_id=?", (lab.late.id,))
        assert len(consolidations) <= 1
        assert not await lab.repository.executions(lab.late.id)
        assert await authorized(lab.target.id) == 1
        assert shown(await detail(client, lab.late.id))[2] == "Consolidated"


async def test_a_verified_consolidation_keeps_its_presentation_and_offers_no_retry(tmp_path, monkeypatch):
    lab = await exhausted(tmp_path, monkeypatch, attested=True)
    async with api(lab.engine) as (client, _caller):
        late = await detail(client, lab.late.id)
        assert shown(late) == ("consolidated", "consolidated", "Consolidated", "consolidated")
        assert late["unverified_association_count"] == 0
        assert not any(row.get("reconsider_available") for row in (await detail(client, lab.target.id))["route_attempts"])


async def test_a_removed_target_leaves_the_contribution_truthful_and_its_retry_downloads_once(tmp_path, monkeypatch):
    lab = await exhausted(tmp_path, monkeypatch)
    async with api(lab.engine) as (client, _caller):
        assert (await client.delete(f"/api/torrents/{lab.target.id}")).status_code == 200
        for _ in range(4):
            await cycle(lab.engine, lab.clock, 60.0)
        # Still owed proof and still saying so; the removed target's Details
        # keeps its history row, and its Retry is still the contributor's own.
        assert shown(await detail(client, lab.late.id))[2] == "Consolidated · Unverified"
        [row] = contributed_rows(await detail(client, lab.target.id), lab.late.id)
        assert row["reconsider_available"] is True
        assert (await client.post(f"/api/torrents/{lab.late.id}/retry")).status_code == 200
        for _ in range(6):
            await cycle(lab.engine, lab.clock, 60.0)
        assert (await detail(client, lab.late.id))["status"] == "downloading"
        assert len(await lab.repository.executions(lab.late.id)) == 1 and await authorized(lab.late.id) == 1
