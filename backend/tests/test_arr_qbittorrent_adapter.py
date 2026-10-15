"""DP 1.0.13 *arr compatibility adapter, qBittorrent dialect (Bearer only).

Discriminating proofs of the qBittorrent-shaped facade (``api.arr_qbittorrent``)
over the real application, middleware, native external contract, canonical
engine, demand ledger and filesystem: what released Sonarr (>= v4.0.18.2971)
and Radarr (>= v6.2.1.10461) send with an API key (upstream evidence in
``tests/fixtures/arr_qbittorrent``), answered in shapes their parsers accept,
with every decision made by an existing owner.

No live Sonarr or Radarr runs here: their parsing rules are restated below
from their source (``arr_*``) and every response is checked against them.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import bencode2
import pytest
import pytest_asyncio

import core.config as config
import db.database as database
from auth import accounts
from external.intake import MAX_TORRENT_BYTES
from external.profiles import ExternalDeliverySettings
from test_arr_sabnzbd_adapter import fail, sab_principal
from test_external_client_foundation import complete, demands, http, transfer_of, world  # noqa: F401 - fixtures
from test_v113_uploaded_torrent_metadata import V2_ONLY, metainfo, multi
from transfers.models import TransferState

pytestmark = pytest.mark.asyncio

BASE = "/api/qbittorrent/api/v2"
FIXTURES = Path(__file__).parent / "fixtures" / "arr_qbittorrent"
HASH = "e" * 40
MAGNET = f"magnet:?xt=urn:btih:{HASH}&dn=Show.S01E01.mkv"
OTHER_HASH = "f" * 40
OTHER_MAGNET = f"magnet:?xt=urn:btih:{OTHER_HASH}&dn=Other.mkv"

# ── what Sonarr v4.0.18.2971 / Radarr v6.2.1.10461 parse
#    (src/NzbDrone.Core/Download/Clients/QBittorrent/QBittorrent.cs:218-330) ──
ARR_COMPLETED = {"pausedUP", "stoppedUP", "uploading", "stalledUP", "queuedUP", "forcedUP"}
ARR_KNOWN = ARR_COMPLETED | {"error", "stoppedDL", "pausedDL", "queuedDL", "checkingDL", "checkingUP",
                             "checkingResumeData", "stalledDL", "missingFiles", "metaDL", "forcedMetaDL", "forcedDL",
                             "moving", "downloading"}


def arr_version(text: str) -> tuple[int, ...]:
    """``Version.Parse`` of /app/webapiVersion (QBittorrentProxyV2.cs:72-78)."""
    return tuple(int(part) for part in text.strip().split("."))


def arr_torrents(body) -> list[dict]:
    """``List<QBittorrentTorrent>`` (QBittorrentTorrent.cs) and the import
    rule: a completed state on API >= 2.6.1 needs content_path != save_path."""
    assert isinstance(body, list)
    for torrent in body:
        assert torrent["state"] in ARR_KNOWN, torrent
        assert isinstance(torrent["hash"], str) and len(torrent["hash"]) == 40 and torrent["hash"] == torrent[
            "hash"].lower()
        assert isinstance(torrent["name"], str) and torrent["name"]
        assert isinstance(torrent["size"], int) and isinstance(torrent["eta"], int)
        assert 0 <= float(torrent["progress"]) <= 1
        for key in ("ratio", "ratio_limit"):
            float(torrent[key])
        for key in ("seeding_time_limit", "inactive_seeding_time_limit"):
            assert isinstance(torrent[key], int)
        assert all(isinstance(torrent[key], str) for key in ("category", "save_path", "content_path"))
        if torrent["state"] in ARR_COMPLETED:
            assert torrent["content_path"] and torrent["content_path"] != torrent["save_path"]
            # CanBeRemoved: stopped, and a seed limit met (QBittorrent.cs:242-245, 630-652).
            assert torrent["ratio_limit"] >= 0 and torrent["ratio_limit"] - torrent["ratio"] <= 0.001
        # No speed, swarm or tracker figures are invented.
        assert not {"dlspeed", "upspeed", "num_seeds", "num_leechs", "trackers_count"} & set(torrent)
    return body


def arr_preferences(body: dict) -> dict:
    """``QBittorrentPreferences`` and the Test's RemovesCompletedDownloads."""
    for key in ("max_ratio_enabled", "max_seeding_time_enabled", "max_inactive_seeding_time_enabled",
                "queueing_enabled", "dht"):
        assert isinstance(body[key], bool)
    assert isinstance(body["save_path"], str) and body["max_ratio_act"] in {0, 1, 2, 3}
    removes = (body["max_ratio_enabled"] or (body["max_seeding_time_enabled"] and body["max_seeding_time"] < 20160)) \
        and body["max_ratio_act"] in {1, 3}
    assert not removes
    return body


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def torrent_file(name=b"Show.S01", files=None, extra=None) -> bytes:
    return metainfo(multi(name=name, files=files, extra=extra))


def info_hash(data: bytes) -> str:
    """The v1 info-hash *arr computes (MonoTorrent): SHA-1 of the info dict."""
    return hashlib.sha1(bencode2.bencode(bencode2.bdecode(data)[b"info"])).hexdigest()


@pytest_asyncio.fixture
async def qb(http, world):
    """The foundation's HTTP harness (password mode, real middleware) with a
    provider that also takes the canonical ``torrent`` request; the second
    token of the account may use the tv profile too."""
    world.provider.descriptor = replace(world.provider.descriptor,
                                        request_types=world.provider.descriptor.request_types | {"torrent"})
    other_client = next(token["client_id"] for token in await accounts.list_tokens(world.accounts.sonarr.id)
                        if token["label"] == "http-other")
    profiles = [profile.model_copy(update={"clients": [*profile.clients, other_client]}) if profile.id == "tv"
                else profile for profile in config.get_settings().external_delivery.profiles]
    config.apply_settings(config.get_settings().model_copy(update={
        "external_delivery": ExternalDeliverySettings(profiles=profiles)}))
    yield SimpleNamespace(client=http.client, http=http, headers=http.client_headers, other=http.other_headers,
                          key=http.client_headers["Authorization"].split(" ", 1)[1])


async def get(qb, path, headers=None, **params):
    return await qb.client.get(f"{BASE}/{path}", params=params, headers=qb.headers if headers is None else headers)


async def post(qb, path, headers=None, files=None, **form):
    return await qb.client.post(f"{BASE}/{path}", data=form, files=files,
                                headers=qb.headers if headers is None else headers)


async def add_magnet(qb, magnet=MAGNET, category="tv", headers=None, **form):
    return await post(qb, "torrents/add", headers=headers, urls=magnet, category=category, **form)


async def add_file(qb, data, category="tv", filename="Show.S01.torrent", headers=None, **form):
    return await post(qb, "torrents/add", headers=headers, files={"torrents": (filename, data, "application/x-bittorrent")},
                      category=category, **form)


async def added(response) -> str:
    assert response.status_code == 200, response.text
    assert response.text != "Fails."
    body = response.json()
    assert (body["success_count"], body["failure_count"], body["pending_count"]) == (1, 0, 0)
    return body["added_torrent_ids"][0]


async def torrents(qb, headers=None, **params) -> list[dict]:
    response = await get(qb, "torrents/info", headers=headers, **params)
    assert response.status_code == 200, response.text
    return arr_torrents(response.json())


async def jobs():
    async with database.get_db() as db:
        return await db.fetchall("SELECT * FROM external_jobs ORDER BY created_at")


async def delete(qb, hashes, delete_files="true", headers=None):
    return await post(qb, "torrents/delete", headers=headers, hashes=hashes, deleteFiles=delete_files)


# ── 1. authentication: the Integration token as Bearer, nothing else ────────

async def test_the_api_key_is_a_bearer_integration_token_and_nothing_else_authenticates(qb, world):
    assert fixture("auth.json")["request_headers"] == {"Authorization": "Bearer REDACTED"}
    version = await get(qb, "app/webapiVersion")
    assert (version.status_code, version.text) == (200, "2.15.1")
    assert arr_version(version.text) >= (2, 11, 0)
    for headers in ({}, {"Authorization": "Bearer dp_wrong"}, {"Authorization": "Basic b3BlcmF0b3I6eA=="}):
        assert (await get(qb, "app/webapiVersion", headers=headers)).status_code == 401
    # The key in the URL is never a credential here.
    assert (await qb.client.get(f"{BASE}/app/webapiVersion", params={"apikey": qb.key})).status_code == 401
    # A browser session is no external client.
    assert (await get(qb, "torrents/info", headers=qb.http.admin_headers)).status_code == 403
    await accounts.revoke_token(world.accounts.sonarr.id, qb.http.token["id"])
    assert (await get(qb, "app/webapiVersion")).status_code == 401


async def test_open_mode_never_stands_in_for_the_key(qb):
    current = config.get_settings().model_copy(update={"auth_password_enabled": False})
    config.apply_settings(current)
    config.save_settings(current)
    assert (await get(qb, "torrents/info", headers={})).status_code == 403
    assert (await add_magnet(qb, headers={})).status_code == 403
    assert await jobs() == []


async def test_the_dialect_grants_no_transfer_route(qb):
    await added(await add_magnet(qb))
    transfer_id = await transfer_of((await jobs())[0]["id"])
    for method, path in (("GET", f"/api/torrents/{transfer_id}"), ("DELETE", f"/api/torrents/{transfer_id}"),
                         ("POST", f"/api/torrents/{transfer_id}/pause"), ("POST", "/api/processing/pause"),
                         ("GET", "/api/torrents"), ("GET", "/api/external/profiles")):
        assert (await qb.client.request(method, path, headers=qb.headers)).status_code == 403, path


async def test_rotation_keeps_the_items_a_shared_key_is_one_client_and_tokens_are_isolated(qb, world):
    await added(await add_magnet(qb))
    _successor, raw = await accounts.create_token(world.accounts.sonarr.id, "rotated", succeeds=qb.http.token["id"])
    rotated = {"Authorization": f"Bearer {raw}"}
    assert [item["hash"] for item in await torrents(qb, headers=rotated)] == [HASH]
    assert await torrents(qb, headers=qb.other) == []
    assert (await delete(qb, HASH, headers=qb.other)).status_code == 200
    assert [row["withdrawn_at"] for row in await demands()] == [None]


async def test_the_key_never_reaches_a_log_or_a_response(qb, caplog):
    caplog.set_level(logging.DEBUG)
    responses = [await add_magnet(qb), await add_magnet(qb, magnet="magnet:?xt=urn:btih:zz"),
                 await add_file(qb, b"garbage"), await get(qb, "torrents/info"), await delete(qb, "x" * 40),
                 await get(qb, "app/webapiVersion", headers={"Authorization": f"Bearer {qb.key}x"})]
    for response in responses:
        assert qb.key not in response.text and qb.key[8:] not in response.text
    assert qb.key not in caplog.text and qb.key[8:] not in caplog.text


# ── 2. the connection Test ──────────────────────────────────────────────────

async def test_every_call_of_the_connection_test_answers_what_arr_parses(qb):
    calls = fixture("connection.json")["requests"]
    assert [call[1].split("?")[0] for call in calls] == [
        "/api/v2/app/webapiVersion", "/api/v2/app/preferences", "/api/v2/torrents/categories",
        "/api/v2/torrents/createCategory", "/api/v2/torrents/info"]
    assert arr_version((await get(qb, "app/webapiVersion")).text) >= (2, 6, 1)
    preferences = arr_preferences((await get(qb, "app/preferences")).json())
    assert preferences["dht"] is True and preferences["queueing_enabled"] is False
    assert preferences["save_path"] == "/imports/tv"
    categories = (await get(qb, "torrents/categories")).json()
    assert categories == {"tv": {"name": "tv", "savePath": "/imports/tv"}}
    # The configured category exists: "creating" it is a no-op; another
    # cannot be created by a client.
    assert (await post(qb, "torrents/createCategory", category="tv")).status_code == 200
    assert (await post(qb, "torrents/createCategory", category="tv-sonarr")).status_code == 409
    assert await torrents(qb, category="tv") == []
    assert (await get(qb, "torrents/categories")).json() == categories


# ── 3. admission and identity ───────────────────────────────────────────────

async def test_a_magnet_admits_one_job_and_one_demand_under_its_v1_hash(qb):
    assert fixture("add_magnet.json")["form"]["urls"].startswith("magnet:")
    assert await added(await add_magnet(qb, stopped="false")) == HASH
    [job] = await jobs()
    assert (job["source_kind"], job["category"], json.loads(job["source_summary"])["info_hash"]) == ("magnet", "tv",
                                                                                                    HASH)
    [demand] = await demands()
    assert demand["id"] == job["demand_id"]
    [item] = await torrents(qb, category="tv")
    assert (item["hash"], item["category"], item["state"]) == (HASH, "tv", "queuedDL")
    # No DebridPulse ID is on the wire.
    assert job["id"] not in json.dumps(item) and str(await transfer_of(job["id"])) not in item["hash"]


async def test_a_base32_magnet_is_the_same_hex_hash_arr_computes(qb):
    raw = bytes.fromhex(OTHER_HASH)
    base32 = base64.b32encode(raw).decode().lower()
    assert await added(await add_magnet(qb, magnet=f"magnet:?xt=urn:btih:{base32}&dn=Other")) == OTHER_HASH
    assert [item["hash"] for item in await torrents(qb)] == [OTHER_HASH]


@pytest.mark.parametrize("variant", ["v1", "hybrid"])
async def test_a_torrent_file_is_identified_by_the_v1_hash_arr_computes(qb, variant):
    extra = {b"meta version": 2, b"file tree": {b"Season 1": {b"e01.mkv": {b"": {b"length": 1000}}}}} \
        if variant == "hybrid" else None
    data = torrent_file(extra=extra)
    expected = info_hash(data)
    assert fixture("add_torrent.json")["multipart_parts"][0]["name"] == "torrents"
    assert await added(await add_file(qb, data)) == expected
    [job] = await jobs()
    assert job["source_kind"] == "torrent"
    assert [item["hash"] for item in await torrents(qb)] == [expected]


@pytest.mark.parametrize("variant,status", [
    ("v2_only", 415), ("garbage", 415), ("bad_magnet", 415), ("http_url", 415), ("two_urls", 400),
    ("url_and_file", 400), ("no_source", 400), ("oversize", 413), ("no_category", 409),
    ("unknown_category", 409), ("foreign_category", 409), ("savepath", 409), ("stopped", 409), ("paused", 409),
    ("get", 405)])
async def test_an_unsafe_or_unsupported_submission_admits_nothing(qb, variant, status):
    if variant == "v2_only":
        response = await add_file(qb, V2_ONLY)
    elif variant == "garbage":
        response = await add_file(qb, b"d8:announce1:xe")
    elif variant == "bad_magnet":
        response = await add_magnet(qb, magnet="magnet:?xt=urn:btih:nothex")
    elif variant == "http_url":
        response = await add_magnet(qb, magnet="http://127.0.0.1/file.torrent")
    elif variant == "two_urls":
        response = await add_magnet(qb, magnet=f"{MAGNET}\n{OTHER_MAGNET}")
    elif variant == "url_and_file":
        response = await post(qb, "torrents/add", urls=MAGNET, category="tv",
                              files={"torrents": ("a.torrent", torrent_file(), "application/x-bittorrent")})
    elif variant == "no_source":
        response = await post(qb, "torrents/add", category="tv")
    elif variant == "oversize":
        response = await add_file(qb, b"d" + b"0" * (MAX_TORRENT_BYTES + 16))
    elif variant == "no_category":
        response = await add_magnet(qb, category="")
    elif variant in {"unknown_category", "foreign_category"}:
        response = await add_magnet(qb, category="nonexistent" if variant == "unknown_category" else "movies")
    elif variant == "savepath":
        response = await add_magnet(qb, savepath="/etc")
    elif variant in {"stopped", "paused"}:
        response = await add_magnet(qb, **{variant: "true"})
    else:
        response = await get(qb, "torrents/add", urls=MAGNET, category="tv")
    assert response.status_code == status, response.text
    assert response.text != "" and await jobs() == [] and await demands() == []


async def test_settings_dp_does_not_honor_are_not_granted_and_change_nothing(qb, world):
    # Sequential order, first/last piece, content layout and share limits
    # are accepted and have no effect: DebridPulse owns layout and never seeds.
    assert await added(await add_magnet(qb, sequentialDownload="true", firstLastPiecePrio="true",
                                        contentLayout="Subfolder", ratioLimit="2", seedingTimeLimit="60")) == HASH
    [item] = await torrents(qb)
    assert item["ratio_limit"] == -2 and item["save_path"] == "/imports/tv"


async def test_the_same_hash_is_one_item_per_client(qb, world):
    first = await added(await add_magnet(qb))
    # A retried add (lost reply) and concurrent duplicates: the same item.
    assert await added(await add_magnet(qb)) == first
    responses = await asyncio.gather(*(add_magnet(qb) for _ in range(5)))
    assert {await added(response) for response in responses} == {first}
    # The same torrent as a .torrent upload is the same item.
    assert len(await jobs()) == 1 and len(await demands()) == 1
    # The same hash under another category is refused, never moved or merged.
    profiles = [profile.model_copy(update={"clients": [*profile.clients, qb.http.token["client_id"]]})
                for profile in config.get_settings().external_delivery.profiles]
    config.apply_settings(config.get_settings().model_copy(update={
        "external_delivery": ExternalDeliverySettings(profiles=profiles)}))
    refused = await add_magnet(qb, category="movies")
    assert refused.status_code == 409 and "duplicate_torrent" in refused.text
    assert len(await jobs()) == 1
    # Another client is its own item, on the one shared transfer.
    assert await added(await add_magnet(qb, headers=qb.other)) == first
    rows = await jobs()
    assert len(rows) == 2 and await transfer_of(rows[0]["id"]) == await transfer_of(rows[1]["id"])


async def test_concurrent_first_submissions_resolve_to_one_job(qb):
    responses = await asyncio.gather(*(add_magnet(qb) for _ in range(5)))
    assert {await added(response) for response in responses} == {HASH}
    assert len(await jobs()) == 1 and len(await demands()) == 1


@pytest.mark.parametrize("ended", ["withdrawn", "acknowledged"])
async def test_a_hash_that_left_the_view_is_a_new_job(qb, world, ended):
    await added(await add_magnet(qb))
    [job] = await jobs()
    if ended == "acknowledged":
        await complete(world, await transfer_of(job["id"]))
        assert (await torrents(qb))[0]["state"] == "stoppedUP"
    assert (await delete(qb, HASH)).status_code == 200
    assert await torrents(qb) == []
    assert await added(await add_magnet(qb)) == HASH
    rows = await jobs()
    assert len(rows) == 2 and rows[1]["id"] != job["id"]
    assert [item["hash"] for item in await torrents(qb)] == [HASH]


async def test_a_failed_item_holds_its_hash_until_the_client_removes_it(qb, world):
    await added(await add_magnet(qb))
    transfer_id = await transfer_of((await jobs())[0]["id"])
    await fail(world, transfer_id)
    [item] = await torrents(qb)
    assert item["state"] == "error"
    assert (await add_magnet(qb)).status_code == 409
    assert (await delete(qb, HASH)).status_code == 200
    assert await added(await add_magnet(qb)) == HASH
    rows = await jobs()
    assert len(rows) == 2
    # The new job is exactly what the core made of the new demand (here it
    # joined the existing failed transfer): the wire state mirrors the native
    # phase and is never invented by the dialect.
    phase = (await world.external.job(sab_principal(world, qb), rows[1]["id"]))["phase"]
    [item] = await torrents(qb)
    assert item["state"] == ("error" if phase == "failed" else item["state"])
    assert phase == "failed" or item["state"] != "error"


# ── 4. projection ───────────────────────────────────────────────────────────

async def test_the_list_is_truthful_and_parseable_through_completion(qb, world):
    fields = set(fixture("info.json")["fields_read"])
    await added(await add_magnet(qb))
    transfer_id = await transfer_of((await jobs())[0]["id"])
    [item] = await torrents(qb, category="tv")
    assert fields <= set(item) and item["eta"] == 8640000 and item["size"] == 0
    await world.engine.tick()
    [item] = await torrents(qb)
    assert item["state"] in {"queuedDL", "downloading"}
    await complete(world, transfer_id)
    [item] = await torrents(qb, category="tv", hashes=HASH)
    assert (item["state"], item["progress"], item["save_path"]) == ("stoppedUP", 1.0, "/imports/tv")
    assert item["content_path"].startswith("/imports/tv/") and item["size"] > 0 and item["eta"] == 0
    assert (item["ratio"], item["ratio_limit"]) == (0, 0)
    assert await torrents(qb, category="movies") == [] and await torrents(qb, hashes=OTHER_HASH) == []
    assert await torrents(qb, category="") == []
    # Properties (IsTorrentLoaded) answer the item; an unknown hash is 404.
    properties = (await get(qb, "torrents/properties", hash=HASH)).json()
    assert properties == {"hash": HASH, "save_path": "/imports/tv", "seeding_time": 0}
    assert (await get(qb, "torrents/properties", hash=OTHER_HASH)).status_code == 404


async def test_an_operator_pause_reads_stopped_and_lost_material_never_reads_completed(qb, world):
    await added(await add_magnet(qb))
    transfer_id = await transfer_of((await jobs())[0]["id"])
    await world.engine.tick()
    await world.application.pause(transfer_id)
    assert (await torrents(qb))[0]["state"] == "stoppedDL"
    await world.application.resume(transfer_id)
    await complete(world, transfer_id)
    [item] = await torrents(qb)
    assert item["state"] == "stoppedUP"
    Path(item["content_path"].replace("/imports/tv", str(world.payloads), 1)).unlink()
    [item] = await torrents(qb)
    assert (item["state"], item["content_path"]) == ("checkingUP", "")


async def test_the_hash_survives_restart_and_a_shared_transfer(qb, world):
    await added(await add_magnet(qb))
    await added(await add_magnet(qb, headers=qb.other))
    from application.service import ApplicationService
    from external.service import ExternalClients

    restarted = ExternalClients(ApplicationService(world.engine))
    listing = await restarted.visible(sab_principal(world, qb), settled=False, kinds=("magnet", "torrent"))
    assert [job["source"]["info_hash"] for job in listing["jobs"]] == [HASH]
    assert [item["hash"] for item in await torrents(qb, headers=qb.other)] == [HASH]


# ── 5. removal ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("delete_files", ["true", "false"])
async def test_removing_a_completed_torrent_acknowledges_it_and_keeps_dp_history(qb, world, delete_files):
    assert fixture("delete.json")["form"]["deleteFiles"] == "true"
    await added(await add_magnet(qb))
    [job] = await jobs()
    transfer_id = await transfer_of(job["id"])
    await complete(world, transfer_id)
    [item] = await torrents(qb)
    material = Path(item["content_path"].replace("/imports/tv", str(world.payloads), 1))
    moved = material.with_name("imported.mkv")
    material.rename(moved)
    assert (await delete(qb, HASH, delete_files)).text == ""
    assert await torrents(qb) == []
    for _ in range(3):
        await world.engine.tick()
    assert (await world.repository.get(transfer_id)).state == TransferState.COMPLETED
    assert [row["withdrawn_at"] for row in await demands(transfer_id)] == [None] and moved.exists()
    async with database.get_db() as db:
        row = await db.fetchone("SELECT acknowledged_at FROM external_deliveries WHERE job_id=?", (job["id"],))
    assert row["acknowledged_at"] is not None
    assert (await delete(qb, HASH, delete_files)).status_code == 200


@pytest.mark.parametrize("delete_files", ["true", "false", None])
async def test_removing_exclusive_active_work_cancels_it_through_the_core(qb, world, delete_files):
    await added(await add_magnet(qb))
    transfer_id = await transfer_of((await jobs())[0]["id"])
    await world.engine.tick()
    form = {"hashes": HASH.upper(), **({"deleteFiles": delete_files} if delete_files else {})}
    assert (await post(qb, "torrents/delete", **form)).status_code == 200
    # Stopped by the core, and -- needed by nobody -- retired from the
    # ordinary transfer lists by the canonical (soft) delete.
    assert (await world.repository.get(transfer_id)).state == TransferState.DELETED
    assert await torrents(qb) == []


async def test_removing_shared_active_work_withdraws_only_the_callers_demand(qb, world):
    await added(await add_magnet(qb))
    await added(await add_magnet(qb, headers=qb.other))
    transfer_id = await transfer_of((await jobs())[0]["id"])
    assert (await delete(qb, HASH)).status_code == 200
    assert (await world.repository.get(transfer_id)).state not in {TransferState.CANCELLED, TransferState.DELETED}
    assert sorted(row["withdrawn_at"] is None for row in await demands(transfer_id)) == [False, True]
    assert [item["hash"] for item in await torrents(qb, headers=qb.other)] == [HASH]


async def test_a_requester_arriving_during_removal_keeps_the_work(qb, world, monkeypatch):
    from test_external_client_foundation import body

    await added(await add_magnet(qb))
    transfer_id = await transfer_of((await jobs())[0]["id"])
    original = world.application.withdraw_demand

    async def racing(demand_ids, *, by, **options):
        await world.external.submit(world.clients.r, body("racing-theirs", category="movies"))
        return await original(demand_ids, by=by, **options)

    monkeypatch.setattr(world.application, "withdraw_demand", racing)
    assert (await delete(qb, HASH)).status_code == 200
    assert (await world.repository.get(transfer_id)).state not in {TransferState.CANCELLED, TransferState.DELETED}


@pytest.mark.parametrize("form", [{}, {"hashes": "", "deleteFiles": "true"}, {"hashes": "all", "deleteFiles": "true"},
                                  {"hashes": HASH, "deleteFiles": "maybe"}])
async def test_a_malformed_removal_is_refused_and_changes_nothing(qb, form):
    await added(await add_magnet(qb))
    assert (await post(qb, "torrents/delete", **form)).status_code == 400
    assert [item["hash"] for item in await torrents(qb)] == [HASH]


# ── 6. calls DebridPulse declines ───────────────────────────────────────────

async def test_calls_dp_does_not_honor_are_declined_and_change_nothing(qb, world):
    await added(await add_magnet(qb))
    transfer_id = await transfer_of((await jobs())[0]["id"])
    for path in fixture("declined.json")["calls"]:
        method = path.rsplit("/", 1)[1]
        response = await post(qb, f"torrents/{method}", hashes=HASH, category="tv-imported", value="true")
        assert response.status_code == 409, (path, response.text)
    assert (await get(qb, "torrents/files", hash=HASH)).status_code == 404
    assert (await get(qb, "transfer/info")).status_code == 404
    [item] = await torrents(qb)
    assert item["category"] == "tv"
    assert (await world.repository.get(transfer_id)).state not in {TransferState.PAUSED, TransferState.CANCELLED}
    assert [row["withdrawn_at"] for row in await demands()] == [None]


# ── 7. the torrent upload ceiling is DebridPulse's own (16 MiB) ─────────────

def large_torrent(size: int) -> bytes:
    """A valid v1 single-file .torrent of about ``size`` bytes: its piece
    hashes are what makes a metainfo file large."""
    pieces = max(1, (size - 512) // 20)
    return bencode2.bencode({b"announce": b"https://tracker.invalid/announce", b"info": {
        b"length": pieces * 16384, b"name": b"Complete.Series.Pack.mkv", b"piece length": 16384,
        b"pieces": b"\x01" * (20 * pieces)}})


async def test_a_torrent_beyond_8_mib_and_up_to_dp_s_own_16_mib_ceiling_is_accepted(qb):
    assert MAX_TORRENT_BYTES == 16 * 1024 * 1024
    data = large_torrent(int(15.5 * 1024 * 1024))
    assert 8 * 1024 * 1024 < len(data) <= MAX_TORRENT_BYTES
    assert await added(await add_file(qb, data, filename="Complete.Series.Pack.torrent")) == info_hash(data)
    # The native submission carries it base64-encoded: beyond the general
    # 20 MiB request ceiling, within the native upload's own.
    encoded = base64.b64encode(data).decode()
    assert len(encoded) > 20 * 1024 * 1024
    created = await qb.client.post("/api/external/v1/jobs", headers=qb.other, json={
        "idempotency_key": "large-1", "category": "tv",
        "source": {"kind": "torrent", "torrent_base64": encoded, "filename": "Complete.Series.Pack.torrent"}})
    assert created.status_code == 201, created.text[:300]
    assert created.json()["source"]["info_hash"] == info_hash(data)
    capabilities = (await qb.client.get("/api/external/v1/capabilities", headers=qb.headers)).json()
    assert capabilities["limits"]["torrent_bytes"] == MAX_TORRENT_BYTES


async def test_a_torrent_beyond_16_mib_is_refused_on_both_routes(qb):
    data = large_torrent(MAX_TORRENT_BYTES + 4096)
    assert (await add_file(qb, data)).status_code == 413
    refused = await qb.client.post("/api/external/v1/jobs", headers=qb.headers, json={
        "idempotency_key": "large-2", "category": "tv",
        "source": {"kind": "torrent", "torrent_base64": base64.b64encode(data).decode(), "filename": "x.torrent"}})
    assert refused.status_code == 413
    assert await jobs() == [] and await demands() == []
