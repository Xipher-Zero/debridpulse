"""The delivery-profile catalog write never takes a profile away from jobs
that still depend on it (``api.external_routes.put_external_profiles``).

Deleting a profile, moving its roots or removing a client is refused with
``409 profile_in_use`` and the backend's own count while any of the profile's
jobs is unsettled; what governs only future admission (categories, source
kinds, retention, added clients) stays editable. The count and the catalog
replacement share one database write transaction with external reservation,
so an admission racing a catalog change is either counted or refused --
never admitted under a profile that has just been narrowed.
"""
from __future__ import annotations

import asyncio

import pytest

import core.config as config
import db.database as database
from external.service import ExternalClients, ExternalRefused
from test_external_client_foundation import body, complete, demands, http, transfer_of, world  # noqa: F401

pytestmark = pytest.mark.asyncio


async def catalog(http):
    response = await http.client.get("/api/external/profiles", headers=http.admin_headers)
    assert response.status_code == 200
    return response.json()


async def put(http, document, profiles):
    return await http.client.put("/api/external/profiles", headers=http.admin_headers,
                                 json={"expected_revision": document["revision"], "profiles": profiles})


def edited(document, profile_id, **changes):
    return [({**profile, **changes} if profile["id"] == profile_id else profile) for profile in document["profiles"]]


def without(document, profile_id):
    return [profile for profile in document["profiles"] if profile["id"] != profile_id]


async def journaled() -> int:
    async with database.get_db() as db:
        row = await db.fetchone("SELECT COUNT(*) AS n FROM event_journal WHERE event_type LIKE 'configuration.%'")
    return int(row["n"])


@pytest.mark.parametrize("change", ["delete", "local_root", "import_root", "remove_client"])
async def test_a_change_that_takes_the_profile_from_an_unsettled_job_is_refused_with_its_count(http, world, change):
    await world.external.submit(world.clients.a, body("k-1"))
    await world.external.submit(world.clients.a, body("k-2", source={"kind": "magnet", "magnet":
                                                                      "magnet:?xt=urn:btih:" + "a" * 40}))
    document = await catalog(http)
    sub = world.payloads / "sub"
    sub.mkdir()
    profiles = {"delete": lambda: without(document, "tv"),
                "local_root": lambda: edited(document, "tv", local_root=str(sub)),
                "import_root": lambda: edited(document, "tv", import_root="/elsewhere"),
                "remove_client": lambda: edited(document, "tv", clients=[world.clients.b.client_id])}[change]()
    before = await journaled()
    refused = await put(http, document, profiles)
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["code"] == "profile_in_use" and detail["affected_jobs"] == 2
    assert detail["profiles"] == [{"id": "tv", "change": {"delete": "deleted", "local_root": "roots_changed",
                                                          "import_root": "roots_changed",
                                                          "remove_client": "clients_removed"}[change],
                                   "affected_jobs": 2}]
    # Nothing changed: the catalog, its revision, the journal.
    assert await catalog(http) == document
    assert config.get_settings().external_delivery.revision == document["revision"]
    assert await journaled() == before


async def test_a_completed_but_unacknowledged_delivery_is_still_in_use(http, world):
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    await complete(world, await transfer_of(job["id"]))
    assert (await world.external.job(world.clients.a, job["id"]))["delivery"]["state"] == "available"
    document = await catalog(http)
    refused = await put(http, document, without(document, "tv"))
    assert (refused.status_code, refused.json()["detail"]["affected_jobs"]) == (409, 1)


@pytest.mark.parametrize("settle", ["withdrawn", "acknowledged"])
async def test_settled_jobs_do_not_hold_a_profile(http, world, settle):
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    if settle == "withdrawn":
        await world.external.withdraw(world.clients.a, job["id"])
    else:
        await complete(world, await transfer_of(job["id"]))
        delivery = (await world.external.job(world.clients.a, job["id"]))["delivery"]
        await world.external.acknowledge(world.clients.a, job["id"], {"delivery_id": delivery["delivery_id"],
                                                                     "generation": delivery["generation"]})
    document = await catalog(http)
    saved = await put(http, document, without(document, "tv"))
    assert saved.status_code == 200, saved.text
    assert [profile["id"] for profile in saved.json()["profiles"]] == ["movies"]
    # The job, its demand and its history are untouched by the catalog.
    assert len(await demands()) == 1
    assert (await world.external.job(world.clients.a, job["id"]))["id"] == job["id"]


async def test_what_governs_only_future_admission_stays_editable_while_jobs_depend(http, world):
    await world.external.submit(world.clients.a, body("k-1"))
    document = await catalog(http)
    for changes in ({"categories": ["tv", "tv-sonarr"]}, {"source_kinds": ["magnet", "torrent"]},
                    {"retention_hours": 24},
                    {"clients": [*document["profiles"][0]["clients"], "client-added"]},
                    # The same effective import root, spelled as "same as local".
                    {"import_root": ""} if document["profiles"][0]["import_root"] == document["profiles"][0][
                        "local_root"] else {"retention_hours": 12}):
        saved = await put(http, document, edited(document, "tv", **changes))
        assert saved.status_code == 200, (changes, saved.text)
        document = saved.json()
    # Removing a client that no unsettled job belongs to is allowed too.
    clients = [client for client in document["profiles"][0]["clients"] if client != "client-added"]
    assert (await put(http, document, edited(document, "tv", clients=clients))).status_code == 200


async def test_a_stale_revision_is_refused_before_any_impact_is_counted(http, world):
    await world.external.submit(world.clients.a, body("k-1"))
    document = await catalog(http)
    assert (await put(http, document, edited(document, "tv", retention_hours=12))).status_code == 200
    stale = await put(http, document, without(document, "tv"))
    assert (stale.status_code, stale.json()["detail"]["code"]) == (409, "stale_revision")


async def test_an_admission_racing_a_profile_delete_is_refused_never_admitted(http, world, monkeypatch):
    """The submission selected the profile, then the catalog removed it
    before the reservation: the reservation re-selects inside its write
    transaction and is refused; no job, no demand."""
    original = ExternalClients._reserve
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(self, *args, **kwargs):
        entered.set()
        await release.wait()
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(ExternalClients, "_reserve", held)
    submission = asyncio.create_task(world.external.submit(world.clients.a, body("k-1")))
    await entered.wait()
    document = await catalog(http)
    assert (await put(http, document, without(document, "tv"))).status_code == 200
    release.set()
    with pytest.raises(ExternalRefused) as caught:
        await submission
    assert caught.value.code == "unknown_category"
    async with database.get_db() as db:
        assert await db.fetchall("SELECT id FROM external_jobs") == []
    assert await demands() == []


async def test_a_reservation_in_flight_holds_the_profile_until_it_settles(http, world, monkeypatch):
    """The reservation exists and its admission is still running: the delete
    counts it and is refused; once accepted the job is an ordinary unsettled
    one, still counted."""
    original = ExternalClients._admit
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(self, *args, **kwargs):
        entered.set()
        await release.wait()
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(ExternalClients, "_admit", held)
    submission = asyncio.create_task(world.external.submit(world.clients.a, body("k-1")))
    await entered.wait()
    document = await catalog(http)
    refused = await put(http, document, without(document, "tv"))
    assert (refused.status_code, refused.json()["detail"]["affected_jobs"]) == (409, 1)
    release.set()
    job, _ = await submission
    assert job["phase"] in {"accepted", "queued", "processing"}
    refused = await put(http, document, without(document, "tv"))
    assert (refused.status_code, refused.json()["detail"]["affected_jobs"]) == (409, 1)


async def test_the_catalog_write_is_admin_only(http):
    for headers in (http.client_headers, {}):
        assert (await http.client.get("/api/external/profiles", headers=headers)).status_code in {401, 403}
        assert (await http.client.put("/api/external/profiles", headers=headers,
                                      json={"expected_revision": 0, "profiles": []})).status_code in {401, 403}
