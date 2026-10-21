"""DP 1.0.13 *arr compatibility adapter, SABnzbd dialect.

Discriminating proofs of the SABnzbd-shaped facade (``api.arr_sabnzbd``) over
the real application, middleware, native external contract
(``external.service``), canonical engine, demand ledger, staged NZB ingress and
filesystem: what Sonarr and Radarr send (upstream wire evidence in
``tests/fixtures/arr_sabnzbd``), answered in shapes their parsers accept,
with every decision made by an existing owner.

No live Sonarr or Radarr runs here: their parsing rules are restated below
from their source (``arr_*_accepts``) and every response is checked against
them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import pytest
import pytest_asyncio

import core.config as config
import db.database as database
from auth import accounts
from auth.permissions import ACTIONS, Capability, ROLE_CAPABILITIES
from auth.accounts import Role
from core.version import read_version
from external import arr
from external.intake import MAX_NZB_BYTES
from external.profiles import ExternalDeliverySettings
from external.service import ExternalRefused
from integrations.usenet.nzb import read as read_nzb
from test_external_client_foundation import complete, demands, http, transfer_of, world  # noqa: F401 - fixtures
from test_universal_lifecycle import failure
from test_v113_nzb_link_ingress import nzb as posting
from transfers.errors import Category, Recovery, Retryability
from transfers.models import ResolutionResult, ResourceState, TransferState
from transfers.staged_input import StagedInputStore

pytestmark = pytest.mark.asyncio

API = "/api/sabnzbd/api"
FIXTURES = Path(__file__).parent / "fixtures" / "arr_sabnzbd"
NZB = posting()
OTHER_NZB = posting(2)

# ── what Sonarr/Radarr's SABnzbd client parses (Sonarr 57816c14, Radarr
#    df2faa64; src/NzbDrone.Core/Download/Clients/Sabnzbd/) ─────────────────
# SabnzbdDownloadStatus.cs: any other string fails the WHOLE poll.
ARR_STATUSES = {"Grabbing", "Queued", "Paused", "Checking", "Downloading", "QuickCheck", "Verifying", "Repairing",
                "Fetching", "Extracting", "Moving", "Running", "Completed", "Failed", "Deleted", "Propagating"}
# SabnzbdQueueTimeConverter.cs:15-28: H:M:S or D:H:M:S integers, never null.
ARR_TIMELEFT = re.compile(r"\d+:\d+:\d+(:\d+)?")
# SabnzbdPriority.cs, read by name (SabnzbdPriorityTypeConverter.cs).
ARR_PRIORITIES = {"Default", "Paused", "Low", "Normal", "High", "Force"}
# Sabnzbd.cs:38, 334-404: unanchored, minimum 0.7.0.
ARR_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+|x)")


def arr_queue_accepts(body: dict) -> list[dict]:
    """``SabnzbdQueue``/``SabnzbdQueueItem`` deserialization (``queue`` token)."""
    queue = body["queue"]
    assert isinstance(queue["paused"], bool)
    for slot in queue["slots"]:
        assert slot["status"] in ARR_STATUSES, slot
        assert isinstance(slot["index"], int)
        assert ARR_TIMELEFT.fullmatch(slot["timeleft"]), slot
        Decimal(slot["mb"]), Decimal(slot["mbleft"])
        int(slot["percentage"])
        assert slot["priority"] in ARR_PRIORITIES
        assert all(isinstance(slot[key], str) and slot[key] for key in ("nzo_id", "filename", "cat"))
    return queue["slots"]


def arr_history_accepts(body: dict) -> list[dict]:
    """``SabnzbdHistory``/``SabnzbdHistoryItem`` deserialization (``history`` token)."""
    for slot in body["history"]["slots"]:
        assert slot["status"] in ARR_STATUSES, slot
        assert isinstance(slot["bytes"], int)
        assert all(isinstance(slot[key], str) for key in ("nzo_id", "name", "category", "storage", "fail_message"))
        # "Unpacking failed, write error or disk is full?" turns Failed into a
        # Warning (Sabnzbd.cs:140-148): never produced by the facade.
        assert slot["fail_message"].lower() != "unpacking failed, write error or disk is full?"
    return body["history"]["slots"]


def arr_error(body: dict) -> str:
    """``SabnzbdProxy.CheckForError``: ``status`` false carries ``error``."""
    assert body["status"] is False and isinstance(body["error"], str) and body["error"]
    return body["error"]


def fixture_request(name: str, key="request") -> tuple[str, dict]:
    """The upstream fixture's recorded request: method and query parameters."""
    request = json.loads((FIXTURES / name).read_text())[key]
    return request["method"], dict(parse_qsl(urlsplit(request["url"]).query))


# ── harness ─────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def sab(http, world, tmp_path):
    """The foundation's HTTP harness (password mode, real middleware and
    application) plus the staged NZB ingress the composition root wires and a
    provider that takes the canonical ``nzb`` request. ``key``/``other`` are
    the raw tokens of two clients of the Sonarr Integration account."""
    world.application.staged_input = StagedInputStore(str(tmp_path / "staged"))
    world.application.nzb_reader = read_nzb
    world.provider.descriptor = replace(world.provider.descriptor,
                                        request_types=world.provider.descriptor.request_types | {"nzb"})
    # The harness's second token is another client of the same account; both
    # may use the tv profile.
    other_client = next(token["client_id"] for token in await accounts.list_tokens(world.accounts.sonarr.id)
                        if token["label"] == "http-other")
    profiles = [profile.model_copy(update={"clients": [*profile.clients, other_client]}) if profile.id == "tv"
                else profile for profile in config.get_settings().external_delivery.profiles]
    config.apply_settings(config.get_settings().model_copy(update={
        "external_delivery": ExternalDeliverySettings(profiles=profiles)}))
    yield SimpleNamespace(client=http.client, http=http, store=world.application.staged_input,
                          key=http.client_headers["Authorization"].split(" ", 1)[1],
                          other=http.other_headers["Authorization"].split(" ", 1)[1])


async def call(sab, mode, *, key=None, method="GET", files=None, **params):
    query = {"mode": mode, **params, "apikey": sab.key if key is None else key, "output": "json"}
    if key == "":
        query.pop("apikey")
    return await sab.client.request(method, API, params=query, files=files)


async def addfile(sab, data=NZB, *, filename="Show.S01E01.nzb", cat="tv", key=None, field="name", **params):
    params.setdefault("priority", "-100")
    return await call(sab, "addfile", key=key, method="POST", cat=cat,
                      files={field: (filename, data, "application/x-nzb")}, **params)


async def submitted(sab, **kwargs) -> str:
    response = await addfile(sab, **kwargs)
    body = response.json()
    assert response.status_code == 200 and body["status"] is True, body
    assert len(body["nzo_ids"]) == 1
    return body["nzo_ids"][0]


async def queue(sab, key=None, **params) -> list[dict]:
    return arr_queue_accepts((await call(sab, "queue", key=key, **{"start": "0", "limit": "0", **params})).json())


async def history(sab, key=None, **params) -> list[dict]:
    return arr_history_accepts((await call(sab, "history", key=key, **{"start": "0", "limit": "60", **params})).json())


async def jobs():
    async with database.get_db() as db:
        return await db.fetchall("SELECT * FROM external_jobs ORDER BY created_at")


async def fail(world, transfer_id, *, terminal=False):
    """Fail one transfer through the core: a resolution refusal it does not
    retry by itself -- an obstruction an operator can clear (the provider's
    credentials were refused), or, ``terminal``, a source that does not
    exist, which invalidates the submission."""
    world.provider.responses = [ResolutionResult(ResourceState.UNKNOWN, error=failure(
        Category.SOURCE_NOT_FOUND, retryability=Retryability.NEVER) if terminal else failure(
        Category.AUTHENTICATION_FAILED, retryability=Retryability.AFTER_REAUTH, recovery=Recovery.REAUTHENTICATE))]
    for _ in range(3):
        await world.engine.tick()
        world.now[0] += 1000
    assert (await world.repository.get(transfer_id)).state == TransferState.FAILED


# ── 1. connection and authentication ────────────────────────────────────────

async def test_connection_test_answers_what_arr_reads_with_the_integration_token(sab):
    method, params = fixture_request("connection_version.json")
    version = (await sab.client.request(method, API, params={**params, "apikey": sab.key})).json()
    assert version == {"version": read_version()}
    major, minor, patch = ARR_VERSION.search(version["version"]).groups()
    assert (int(major), int(minor), int(patch)) >= (0, 7, 0)
    method, params = fixture_request("connection_get_config.json")
    body = (await sab.client.request(method, API, params={**params, "apikey": sab.key})).json()
    misc = body["config"]["misc"]
    assert misc["complete_dir"].startswith("/") and misc["pre_check"] is False
    # The shortest profile retention (168 h) is SABnzbd "days-7": *arr knows
    # completed items leave the history (RemovesCompletedDownloads, <14 days).
    assert (misc["history_retention_option"], misc["history_retention_number"]) == ("days-delete", 7)
    assert body["config"]["sorters"] == []
    # Exactly this client's NZB-permitted categories, each at its import root:
    # a rooted dir is the category's full path to *arr (OsPath +).
    assert [(item["name"], item["dir"]) for item in body["config"]["categories"]] == [("tv", "/imports/tv")]
    assert not any(item["dir"].endswith("*") for item in body["config"]["categories"])


@pytest.mark.parametrize("variant", ["missing", "wrong", "whitespace", "revoked", "disabled"])
async def test_an_absent_or_invalid_key_is_refused_in_the_form_arr_reports(sab, world, variant):
    key = {"missing": "", "wrong": "dp_wrong", "whitespace": "dp_a b"}.get(variant)
    if variant == "revoked":
        await accounts.revoke_token(world.accounts.sonarr.id, sab.http.token["id"])
    if variant == "disabled":
        await accounts.set_enabled(accounts.DEFAULT_ADMIN_ID, world.accounts.sonarr.id, False)
    for mode in ("version", "get_config", "queue", "history"):
        response = await call(sab, mode, key=key)
        assert response.status_code == 200
        assert arr_error(response.json()) == ("API Key Required" if variant == "missing" else "API Key Incorrect")
    refused = await addfile(sab, key=key)
    assert refused.json()["status"] is False
    assert await jobs() == []


@pytest.mark.parametrize("mode", ["open", "password"])
async def test_no_session_cookie_or_open_mode_ever_stands_in_for_the_key(sab, mode):
    if mode == "open":
        current = config.get_settings().model_copy(update={"auth_password_enabled": False})
        config.apply_settings(current)
        config.save_settings(current)
    for headers in ({}, sab.http.admin_headers):
        response = await sab.client.get(API, params={"mode": "queue", "output": "json"}, headers=headers)
        assert arr_error(response.json()) == "API Key Required"
    # The same token as a Bearer header is the same client.
    bearer = await sab.client.get(API, params={"mode": "queue", "output": "json"}, headers=sab.http.client_headers)
    assert arr_queue_accepts(bearer.json()) == []


async def test_the_query_key_authenticates_nothing_but_the_protocol_endpoint(sab, world):
    nzo_id = await submitted(sab)
    transfer_id = await transfer_of(nzo_id)
    for method, path in (("GET", f"/api/torrents/{transfer_id}"), ("DELETE", f"/api/torrents/{transfer_id}"),
                         ("POST", f"/api/torrents/{transfer_id}/pause"), ("GET", "/api/torrents"),
                         ("POST", "/api/processing/pause"), ("GET", "/api/external/v1/jobs"),
                         ("GET", "/api/external/profiles")):
        response = await sab.client.request(method, path, params={"apikey": sab.key})
        assert response.status_code == 401, (method, path, response.text)
    # The facade is the external-client capability and nothing more.
    assert ACTIONS["api.arr_sabnzbd.sabnzbd_api"] is Capability.EXTERNAL_CLIENT
    assert Capability.EXTERNAL_CLIENT not in ROLE_CAPABILITIES[Role.ADMIN]
    assert not {Capability.TRANSFERS_READ, Capability.TRANSFERS_OPERATE, Capability.TRANSFERS_DESTROY,
                Capability.ADMINISTER} & ROLE_CAPABILITIES[Role.INTEGRATION]
    assert (await world.repository.get(transfer_id)).state not in {TransferState.CANCELLED, TransferState.DELETED}


async def test_the_key_never_reaches_a_log_or_a_response(sab, caplog):
    caplog.set_level(logging.DEBUG)
    responses = [await call(sab, "get_config"), await addfile(sab), await addfile(sab, data=b"not a posting"),
                 await call(sab, "queue", name="delete", value="job_x", del_files="1"),
                 await call(sab, "history"), await call(sab, "nonsense"),
                 await addfile(sab, filename="bad.nzb", cat="unknown"),
                 await call(sab, "queue", key=sab.key + "x")]
    for response in responses:
        assert sab.key not in response.text and sab.key[8:] not in response.text
    assert sab.key not in caplog.text and sab.key[8:] not in caplog.text
    # The application's own log sanitizer redacts a logged protocol URL.
    from core.logging_utils import sanitize_log_value
    assert sab.key not in sanitize_log_value(f"GET {API}?mode=queue&apikey={sab.key}&output=json")


# ── 2. submission and retry identity ────────────────────────────────────────

async def test_addfile_admits_one_job_and_one_demand_through_the_staged_nzb_ingress(sab, world):
    method, params = fixture_request("submit_addfile.json")
    assert (method, params["cat"], params["priority"]) == ("POST", "tv", "-100")
    response = await sab.client.post(API, params={**params, "apikey": sab.key},
                                     files={"name": ("Show.S01E01.nzb", NZB, "application/x-nzb")})
    body = response.json()
    assert body["status"] is True and body["nzo_ids"][0].startswith("job_")
    [job] = await jobs()
    assert (job["id"], job["category"], job["source_kind"], job["display_name"]) == (
        body["nzo_ids"][0], "tv", "nzb", "Show.S01E01")
    assert json.loads(job["source_summary"]) == {"kind": "nzb", "name": "Show.S01E01.nzb"}
    transfer_id = await transfer_of(job["id"])
    [demand] = await demands(transfer_id)
    assert demand["id"] == job["demand_id"] and demand["withdrawn_at"] is None
    # The canonical nzb request carries a staged reference, never the bytes.
    records = await world.repository.requests(transfer_id)
    assert [record.request.kind for record in records] == ["nzb"]
    assert not isinstance(records[0].request.payload, (bytes, str))
    assert len(list(Path(sab.store.root).iterdir())) == 1
    # The answer is the logical job alone: no transfer, demand or path.
    assert body == {"status": True, "nzo_ids": [job["id"]]}


@pytest.mark.parametrize("data,expected", [
    (b"", "invalid_source"), (b"<nzb>not a posting", "invalid_source"),
    (b"<html><body>an indexer error page</body></html>", "invalid_source"),
    (NZB[: len(NZB) // 2], "invalid_source"), (b"x" * (MAX_NZB_BYTES + 1), "source_too_large")],
    # Named: an id built from the oversized body would be a single multi-megabyte line in every log.
    ids=["empty", "not_xml", "indexer_error_page", "truncated", "oversized"])
async def test_an_invalid_truncated_or_oversized_nzb_is_refused_with_nothing_admitted(sab, data, expected):
    error = arr_error((await addfile(sab, data=data)).json())
    assert expected in error
    assert await jobs() == [] and await demands() == []
    # A refused posting's staged bytes are discarded at once.
    assert (list(Path(sab.store.root).iterdir()) if Path(sab.store.root).exists() else []) == []


async def test_a_body_beyond_the_upload_ceiling_is_refused_before_it_is_read(sab):
    response = await addfile(sab, data=b"x" * (MAX_NZB_BYTES + 128 * 1024))
    assert response.status_code == 413
    assert await jobs() == []


@pytest.mark.parametrize("variant", ["field", "missing", "get", "paused", "pp", "nzbname", "no_category",
                                     "unknown_category", "foreign_category", "malformed"])
async def test_an_unsupported_or_malformed_submission_is_declined_explicitly(sab, variant):
    if variant == "field":
        response = await addfile(sab, field="nzbfile")
    elif variant == "missing":
        response = await call(sab, "addfile", method="POST", cat="tv", priority="0")
    elif variant == "get":
        response = await call(sab, "addfile", cat="tv")
    elif variant == "paused":
        response = await addfile(sab, priority="-2")
    elif variant in {"pp", "nzbname"}:
        response = await addfile(sab, **{variant: "3"})
    elif variant == "no_category":
        response = await addfile(sab, cat="")
    elif variant == "malformed":
        response = await sab.client.post(API, params={"mode": "addfile", "cat": "tv", "apikey": sab.key},
                                         content=b"--x\r\nbroken", headers={"Content-Type": "multipart/form-data; boundary=x"})
    else:
        # "movies" exists but names a profile this client may not use: the
        # same answer as a category nobody configured.
        response = await addfile(sab, cat="nonexistent" if variant == "unknown_category" else "movies")
    error = arr_error(response.json())
    if variant in {"unknown_category", "foreign_category"}:
        assert error == "This category selects no delivery profile for this client (unknown_category)"
    assert await jobs() == [] and await demands() == []


async def test_addurl_and_every_unimplemented_mode_are_declined(sab):
    method, params = fixture_request("submit_addurl.json")
    assert "addurl" in arr_error((await sab.client.request(method, API, params={**params, "apikey": sab.key})).json())
    for mode, extra in (("pause", {}), ("resume", {}), ("retry", {"value": "job_x"}), ("change_cat", {}),
                        ("queue", {"name": "pause", "value": "job_x"}), ("queue", {"name": "priority"}),
                        ("fullstatus", {}), ("", {})):
        arr_error((await call(sab, mode, **extra)).json())
    assert await jobs() == []


async def test_an_identical_resubmission_answers_the_same_job_while_it_is_live(sab):
    first = await submitted(sab)
    # A lost reply: *arr parks the release and sends it again later.
    assert await submitted(sab) == first
    concurrent = await asyncio.gather(*(addfile(sab) for _ in range(4)))
    assert {response.json()["nzo_ids"][0] for response in concurrent} == {first}
    assert len(await jobs()) == 1 and len(await demands()) == 1


async def test_concurrent_identical_first_submissions_resolve_to_one_job_and_one_demand(sab):
    responses = await asyncio.gather(*(addfile(sab) for _ in range(5)))
    assert len({response.json()["nzo_ids"][0] for response in responses}) == 1
    assert len(await jobs()) == 1 and len(await demands()) == 1


@pytest.mark.parametrize("change", ["bytes", "filename", "category", "client"])
async def test_a_different_identity_is_a_different_job(sab, world, change):
    profiles = [profile.model_copy(update={"clients": [*profile.clients, sab.http.token["client_id"]]})
                for profile in config.get_settings().external_delivery.profiles]
    config.apply_settings(config.get_settings().model_copy(update={
        "external_delivery": ExternalDeliverySettings(profiles=profiles)}))
    first = await submitted(sab)
    second = await submitted(sab, **{"bytes": {"data": OTHER_NZB}, "filename": {"filename": "Other.nzb"},
                                     "category": {"cat": "movies"}, "client": {"key": sab.other}}[change])
    assert first != second
    assert len(await jobs()) == 2 and len(await demands()) == 2


@pytest.mark.parametrize("ended", ["withdrawn", "acknowledged", "failed"])
async def test_a_job_that_left_the_view_is_never_reused(sab, world, ended):
    first = await submitted(sab)
    transfer_id = await transfer_of(first)
    if ended == "failed":
        await fail(world, transfer_id, terminal=True)
        assert arr_history_accepts((await call(sab, "history")).json())[0]["status"] == "Failed"
    else:
        if ended == "acknowledged":
            await complete(world, transfer_id)
        assert (await call(sab, "queue" if ended == "withdrawn" else "history", name="delete", value=first,
                           del_files="1")).json()["status"] is True
    second = await submitted(sab)
    assert second != first
    assert len(await jobs()) == 2


async def test_an_admission_that_fails_is_never_answered_as_success_and_a_retry_admits_once(sab, world, monkeypatch):
    original = world.application.submit

    async def broken(*args, **kwargs):
        raise RuntimeError("storage went away")

    monkeypatch.setattr(world.application, "submit", broken)
    assert "submission_outcome_unknown" in arr_error((await addfile(sab)).json())
    assert await demands() == []
    monkeypatch.setattr(world.application, "submit", original)
    nzo_id = await submitted(sab)
    assert len(await jobs()) == 1 and len(await demands()) == 1
    assert nzo_id == (await jobs())[0]["id"]


async def test_the_keyed_native_contract_is_unchanged_and_takes_the_nzb_kind(sab, world):
    import base64

    source = {"kind": "nzb", "nzb_base64": base64.b64encode(NZB).decode(), "filename": "Show.S01E01.nzb"}
    created = await sab.client.post("/api/external/v1/jobs", headers=sab.http.client_headers,
                                    json={"idempotency_key": "k-1", "category": "tv", "source": source})
    assert created.status_code == 201, created.text
    replay = await sab.client.post("/api/external/v1/jobs", headers=sab.http.client_headers,
                                   json={"idempotency_key": "k-1", "category": "tv", "source": source})
    assert (replay.status_code, replay.json()["id"]) == (200, created.json()["id"])
    keyless = await sab.client.post("/api/external/v1/jobs", headers=sab.http.client_headers,
                                    json={"category": "tv", "source": source})
    assert keyless.json()["detail"]["code"] == "idempotency_key_required"
    # The live derivation is not reachable over the native API, and never
    # takes a caller key or another source kind.
    external = world.external
    with pytest.raises(ExternalRefused) as caught:
        await external.submit_live(world.clients.a, {"idempotency_key": "x", "category": "tv", "source": source})
    assert caught.value.code == "invalid_request"
    with pytest.raises(ExternalRefused) as caught:
        await external.submit_live(world.clients.a, {"category": "tv", "source": {"kind": "links", "links":
                                                                                   ["https://example.com/a.mkv"]}})
    assert caught.value.code == "unsupported_source"
    capabilities = (await sab.client.get("/api/external/v1/capabilities", headers=sab.http.client_headers)).json()
    assert capabilities["limits"]["nzb_bytes"] == MAX_NZB_BYTES and "nzb" in capabilities["source_kinds"]


# ── 3. queue and history projection ─────────────────────────────────────────

async def test_the_queue_is_truthful_and_parseable_through_every_phase(sab, world):
    nzo_id = await submitted(sab)
    transfer_id = await transfer_of(nzo_id)
    [slot] = await queue(sab, category="tv")
    assert (slot["nzo_id"], slot["filename"], slot["cat"], slot["status"]) == (nzo_id, "Show.S01E01", "tv", "Grabbing")
    # No invented size, speed or time left.
    assert (slot["mb"], slot["mbleft"], slot["timeleft"]) == ("0.00", "0.00", "0:00:00")
    await world.engine.tick()
    [slot] = await queue(sab)
    assert slot["status"] in {"Downloading", "Queued", "Grabbing"} and slot["nzo_id"] == nzo_id
    assert await history(sab) == []
    await complete(world, transfer_id)
    assert await queue(sab) == []
    [slot] = await history(sab, category="tv")
    assert (slot["nzo_id"], slot["status"], slot["category"]) == (nzo_id, "Completed", "tv")
    assert slot["storage"] == "/imports/tv/Show.S01E01.nzb" and slot["bytes"] == 4


async def test_a_torrent_the_same_client_submitted_in_the_same_category_is_never_a_sabnzbd_item(sab, world):
    """The dialect lists only what it accepts (FUNC-A02): a magnet this same
    client submitted in the same category -- the qBittorrent dialect's item --
    is in neither the queue nor the history, while the NZB is."""
    from auth.models import Principal

    nzo_id = await submitted(sab)
    principal = Principal.integration_token(world.accounts.sonarr, sab.http.token["id"],
                                            sab.http.token["client_id"])
    torrent, _ = await world.external.submit(principal, {
        "idempotency_key": "magnet-1", "category": "tv",
        "source": {"kind": "magnet", "magnet": "magnet:?xt=urn:btih:" + "e" * 40 + "&dn=episode.mkv"}})
    assert [slot["nzo_id"] for slot in await queue(sab)] == [nzo_id]
    await complete(world, await transfer_of(nzo_id))
    await complete(world, await transfer_of(torrent["id"]))
    assert [(slot["nzo_id"], slot["status"]) for slot in await history(sab)] == [(nzo_id, "Completed")]
    assert await queue(sab) == []


async def test_a_retried_completed_item_returns_to_the_queue_and_is_completed_again_once_re_verified(sab, world):
    """A reopened delivery is never offered (FUNC-A01, D-X1): an operator
    Retry of the completed, not yet removed item moves it from history back
    to the queue under the same ``nzo_id`` -- never ``Completed`` with a
    storage path while its material is acquired again -- and it is Completed
    again once the work completes and is re-verified."""
    nzo_id = await submitted(sab)
    transfer_id = await transfer_of(nzo_id)
    await complete(world, transfer_id)
    [done] = await history(sab)
    assert (done["nzo_id"], done["status"]) == (nzo_id, "Completed") and done["storage"]
    await world.application.retry(transfer_id)
    # The queue is polled first, right after the Retry: the reopen is seen at
    # once (never missed for up to the re-verification interval), and the item
    # is never in both listings or in neither.
    [slot] = await queue(sab)
    assert slot["nzo_id"] == nzo_id and slot["status"] in {"Grabbing", "Queued", "Downloading"}
    assert await history(sab) == []
    await complete(world, transfer_id)
    assert await queue(sab) == []
    [again] = await history(sab)
    assert (again["nzo_id"], again["status"], again["storage"]) == (nzo_id, "Completed", done["storage"])


async def test_an_operator_pause_reads_paused_and_a_global_pause_flags_the_queue(sab, world):
    nzo_id = await submitted(sab)
    transfer_id = await transfer_of(nzo_id)
    await world.engine.tick()
    await world.application.pause(transfer_id)
    [slot] = await queue(sab)
    assert slot["status"] == "Paused"
    await world.application.resume(transfer_id)
    await world.application.pause_all()
    body = (await call(sab, "queue", start="0", limit="0")).json()
    arr_queue_accepts(body)
    assert body["queue"]["paused"] is True


@pytest.mark.parametrize("terminal", [True, False], ids=["release-invalidating", "operator-correctable"])
async def test_a_failure_reaches_history_only_when_the_core_says_failed(sab, world, terminal):
    """A core failure reaches history, but it is Failed -- on which *arr
    blocklists the release -- unless it is positively an obstruction an
    operator clears (here: the provider refused its credentials), which is
    Paused: *arr keeps waiting, nothing is blocklisted, and the same job
    continues once the operator fixes it (D-X2 / FUNC-A10). A source that
    does not exist stays Failed. The native job is unchanged by either."""
    nzo_id = await submitted(sab)
    transfer_id = await transfer_of(nzo_id)
    await fail(world, transfer_id, terminal=terminal)
    assert await queue(sab) == []
    [slot] = await history(sab)
    job = await world.external.job(sab_principal(world, sab), nzo_id)
    assert (job["phase"], job["reason"], job["actionability"]) == ("failed", "acquisition_failed", "operator")
    if terminal:
        assert (slot["nzo_id"], slot["status"], slot["storage"]) == (nzo_id, "Failed", "")
        assert slot["fail_message"] == "DebridPulse: acquisition_failed"
    else:
        assert (slot["nzo_id"], slot["status"], slot["storage"], slot["fail_message"]) == (nzo_id, "Paused", "", "")


async def test_a_download_clients_own_failure_with_its_retries_spent_stays_failed(sab, world):
    """The Usenet service's own failure (missing articles, a repair that
    failed: ``TRANSFER_FAILED``, which the core retries with backoff) ends
    the job once its retries are spent, and stays Failed -- *arr must
    blocklist that release and search again; it never waits forever."""
    from transfers.errors import Domain, NormalizedError, Stage

    nzo_id = await submitted(sab)
    transfer_id = await transfer_of(nzo_id)
    native = NormalizedError(Domain.EXECUTOR, Category.TRANSFER_FAILED, Stage.EXECUTION, Retryability.BACKOFF)
    assert await world.repository.state(transfer_id, TransferState.FAILED, error=native)
    [slot] = await history(sab)
    assert (slot["nzo_id"], slot["status"]) == (nzo_id, "Failed")


async def test_only_a_positive_operator_obstruction_is_kept_from_failed():
    """The one rule both dialects share (``arr.release_failed``): every
    failed job is final -- what *arr may blocklist -- except a positive
    obstruction an operator clears with the release never at fault: an
    acquisition failure of an obstruction category, or a delivery-side
    reason. Anything unknown, missing or ambiguous stays final."""
    def job(reason, category=None, phase="failed"):
        return {"phase": phase, "reason": reason, "actionability": "operator",
                **({"error": {"category": category}} if category else {})}

    final = [job("acquisition_failed", category) for category in (
        "source_not_found", "source_unavailable", "resource_not_found", "transfer_failed", "content_invalid",
        "checksum_mismatch", "size_mismatch", "destination_blocked", "no_transfer_candidate", "")]
    final += [job("acquisition_failed"), job("finalization_failed"), job("shared_transfer_failed"),
              job("demand_missing"), {"phase": "failed", "reason": "cancelled", "actionability": "resubmit"},
              {"phase": "failed", "reason": "deleted", "actionability": "resubmit"}]
    clearable = [job("acquisition_failed", category) for category in (
        "authentication_failed", "credential_missing", "invalid_configuration", "provider_unavailable",
        "provider_maintenance", "executor_unavailable", "quota_exceeded", "disk_full",
        "download_storage_unavailable")]
    clearable += [job(reason) for reason in (
        "profile_unavailable", "profile_root_outside_downloads", "outside_export_root", "material_missing",
        "extraction_outputs_unrecorded", "extraction_outputs_unattributable", "nothing_delivered")]
    other = [job("delivered", phase="completed"), job("downloading", phase="processing"),
             job("withdrawn_by_client", phase="withdrawn")]
    assert [arr.release_failed(item) for item in final] == [True] * len(final)
    assert [arr.release_failed(item) for item in clearable + other] == [False] * len(clearable + other)


async def test_each_client_and_category_sees_only_its_own_items_and_ids_survive_restart(sab, world):
    mine = await submitted(sab)
    other = await submitted(sab, key=sab.other, data=OTHER_NZB)
    assert [slot["nzo_id"] for slot in await queue(sab)] == [mine]
    assert [slot["nzo_id"] for slot in await queue(sab, key=sab.other)] == [other]
    assert await queue(sab, category="movies") == []
    # Another client's ID cannot be removed (nor learned to exist).
    removed = (await call(sab, "queue", name="delete", value=mine, del_files="1", key=sab.other)).json()
    assert removed == {"status": True, "nzo_ids": []}
    assert [row["withdrawn_at"] for row in await demands()] == [None, None]
    # A restart rebuilds the same view from durable state.
    from application.service import ApplicationService
    from external.service import ExternalClients

    restarted = ExternalClients(ApplicationService(world.engine))
    listing = await restarted.visible(sab_principal(world, sab), settled=False)
    assert [job["id"] for job in listing["jobs"]] == [mine]


def sab_principal(world, sab):
    from auth.models import Principal

    token = sab.http.token
    return Principal.integration_token(world.accounts.sonarr, token["id"], token["client_id"])


async def test_completion_is_advertised_only_as_one_complete_location(sab, world):
    assert arr.output_location({"state": "partial", "files": []}) is None
    files = [{"import_path": "/imports/tv/Show.S01/e1.mkv"}, {"import_path": "/imports/tv/Show.S01/sub/e1.srt"}]
    available = {"state": "available", "import_root": "/imports/tv", "files": files, "total_bytes": 12}
    assert arr.output_location(available) == ("/imports/tv/Show.S01", 12)
    # Two files directly in the shared root: no single folder is this job's.
    loose = {**available, "files": [{"import_path": "/imports/tv/a.mkv"}, {"import_path": "/imports/tv/b.mkv"}]}
    assert arr.output_location(loose) is None
    assert arr.output_location({**available, "state": "unavailable"}) is None
    nzo_id = await submitted(sab)
    transfer_id = await transfer_of(nzo_id)
    await complete(world, transfer_id)
    assert (await history(sab))[0]["status"] == "Completed"
    # The committed material disappears before *arr removed the item: no
    # longer importable, never Completed, and not a failure either.
    (world.payloads / "Show.S01E01.nzb").unlink()
    [slot] = await history(sab)
    assert (slot["nzo_id"], slot["status"], slot["storage"]) == (nzo_id, "Verifying", "")
    # Material lost before any delivery was ever committed is the native
    # verdict -- failed, operator action -- which never invalidates the
    # release: Paused, never Failed (*arr would blocklist it; D-X2).
    lost = await submitted(sab, data=OTHER_NZB, filename="Lost.nzb")
    await complete(world, await transfer_of(lost))
    (world.payloads / "Lost.nzb").unlink()
    assert {slot["nzo_id"]: slot["status"] for slot in await history(sab)}[lost] == "Paused"


# ── 4. removal, post-import and del_files ───────────────────────────────────

@pytest.mark.parametrize("del_files", ["0", "1"])
async def test_removing_a_completed_item_acknowledges_it_and_keeps_dp_history(sab, world, del_files):
    nzo_id = await submitted(sab)
    transfer_id = await transfer_of(nzo_id)
    await complete(world, transfer_id)
    assert (await history(sab))[0]["status"] == "Completed"
    material = world.payloads / "Show.S01E01.nzb"
    # *arr imports by moving, then removes the item from its client.
    material.rename(world.payloads / "imported.nzb")
    method, params = fixture_request("remove_history_delete.json", "request_completed")
    removed = await sab.client.request(method, API, params={**params, "value": nzo_id, "del_files": del_files,
                                                            "apikey": sab.key})
    assert removed.json() == {"status": True, "nzo_ids": [nzo_id]}
    assert await history(sab) == [] and await queue(sab) == []
    async with database.get_db() as db:
        delivery = await db.fetchone("SELECT acknowledged_at FROM external_deliveries WHERE job_id=?", (nzo_id,))
    assert delivery["acknowledged_at"] is not None
    # DebridPulse's transfer, demand and the moved file are untouched; nothing
    # is reacquired or reclassified as failed.
    for _ in range(3):
        await world.engine.tick()
    assert (await world.repository.get(transfer_id)).state == TransferState.COMPLETED
    assert [row["withdrawn_at"] for row in await demands(transfer_id)] == [None]
    assert (world.payloads / "imported.nzb").exists()
    assert len(await world.repository.requests(transfer_id)) == 1
    # A retried remove is idempotent.
    again = await sab.client.request(method, API, params={**params, "value": nzo_id, "apikey": sab.key})
    assert again.json()["status"] is True


@pytest.mark.parametrize("del_files", ["0", "1"])
async def test_removing_exclusive_active_work_cancels_it_through_the_core(sab, world, del_files):
    nzo_id = await submitted(sab)
    transfer_id = await transfer_of(nzo_id)
    await world.engine.tick()
    method, params = fixture_request("remove_queue_delete.json")
    removed = await sab.client.request(method, API, params={**params, "value": nzo_id, "del_files": del_files,
                                                            "apikey": sab.key})
    assert removed.json() == {"status": True, "nzo_ids": [nzo_id]}
    # Stopped by the core, and -- needed by nobody -- retired from the
    # ordinary transfer lists by the canonical (soft) delete.
    assert (await world.repository.get(transfer_id)).state == TransferState.DELETED
    assert await queue(sab) == [] and await history(sab) == []


async def shared_magnet(world, sab):
    """This SABnzbd client's job and another client's job on ONE canonical
    transfer. Uploaded NZBs carry no canonical fingerprint, so shared work is
    built from a magnet submitted through the native contract; removal acts on
    any of the client's jobs by its ID alike."""
    from test_external_client_foundation import body

    mine, _ = await world.external.submit(sab_principal(world, sab), body("shared-mine"))
    theirs, _ = await world.external.submit(world.clients.r, body("shared-theirs", category="movies"))
    transfer_id = await transfer_of(mine["id"])
    assert await transfer_of(theirs["id"]) == transfer_id
    return mine["id"], theirs["id"], transfer_id


async def test_removing_shared_active_work_withdraws_only_the_callers_demand(sab, world):
    mine, theirs, transfer_id = await shared_magnet(world, sab)
    assert (await call(sab, "queue", name="delete", value=mine, del_files="1")).json()["nzo_ids"] == [mine]
    state = (await world.repository.get(transfer_id)).state
    assert state not in {TransferState.CANCELLED, TransferState.DELETED}
    assert sorted(row["withdrawn_at"] is None for row in await demands(transfer_id)) == [False, True]
    assert (await world.external.job(world.clients.r, theirs))["phase"] != "withdrawn"
    assert await queue(sab) == []


async def test_removing_a_failed_item_follows_the_core_failure_policy(sab, world):
    nzo_id = await submitted(sab)
    transfer_id = await transfer_of(nzo_id)
    await fail(world, transfer_id)
    method, params = fixture_request("remove_history_delete.json", "request_failed")
    assert (await sab.client.request(method, API, params={**params, "value": nzo_id, "apikey": sab.key})).json() \
        == {"status": True, "nzo_ids": [nzo_id]}
    assert await history(sab) == []
    # The core decides what an unneeded failed transfer becomes.
    assert (await world.repository.get(transfer_id)).state in {TransferState.CANCELLED, TransferState.FAILED}


async def test_a_requester_arriving_during_removal_keeps_the_work(sab, world, monkeypatch):
    from test_external_client_foundation import body

    job, _ = await world.external.submit(sab_principal(world, sab), body("racing-mine"))
    transfer_id = await transfer_of(job["id"])
    original = world.application.withdraw_demand

    async def racing(demand_ids, *, by, **options):
        # Another client's demand on the same work lands after the facade
        # read the job and before the core settles the withdrawal.
        await world.external.submit(world.clients.r, body("racing-theirs", category="movies"))
        return await original(demand_ids, by=by, **options)

    monkeypatch.setattr(world.application, "withdraw_demand", racing)
    assert (await call(sab, "queue", name="delete", value=job["id"], del_files="1")).json()["status"] is True
    assert (await world.repository.get(transfer_id)).state not in {TransferState.CANCELLED, TransferState.DELETED}
    assert [row["withdrawn_at"] is None for row in await demands(transfer_id)].count(True) == 1


@pytest.mark.parametrize("value,del_files", [("", "1"), ("all", "1"), ("job_x", "yes")])
async def test_a_malformed_removal_is_refused_and_changes_nothing(sab, value, del_files):
    nzo_id = await submitted(sab)
    arr_error((await call(sab, "queue", name="delete", value=value, del_files=del_files)).json())
    assert [slot["nzo_id"] for slot in await queue(sab)] == [nzo_id]


# ── 5. identity continuity, sharing and robustness ─────────────────────────

async def test_rotation_keeps_the_items_and_a_shared_key_is_one_client(sab, world):
    nzo_id = await submitted(sab)
    successor, raw = await accounts.create_token(world.accounts.sonarr.id, "rotated", succeeds=sab.http.token["id"])
    assert arr_error((await call(sab, "queue")).json()) == "API Key Incorrect"
    assert [slot["nzo_id"] for slot in await queue(sab, key=raw)] == [nzo_id]
    # Two *arr instances deliberately given one key are one client: they see
    # one view, and an identical NZB from either is the same live job.
    assert await submitted(sab, key=raw) == nzo_id
    # The other token of the same account is a different client throughout.
    assert await queue(sab, key=sab.other) == []
    assert (await call(sab, "queue", name="delete", value=nzo_id, key=sab.other)).json()["nzo_ids"] == []


async def test_no_parameter_selects_another_client_and_paging_is_bounded(sab):
    mine = await submitted(sab)
    # The other client names this client's ID: it still sees only its own.
    mine_client = sab.http.token["client_id"]
    for spoof in ({"client_id": mine_client}, {"client": mine_client}, {"cat": "tv"}):
        assert [slot["nzo_id"] for slot in await queue(sab, key=sab.other, **spoof)] == []
    assert [slot["nzo_id"] for slot in await queue(sab, start="1")] == []
    assert [slot["nzo_id"] for slot in await queue(sab, limit="1")] == [mine]
    for bad in ({"start": "-1"}, {"limit": "x"}, {"start": "1e9"}):
        assert "invalid_request" in arr_error((await call(sab, "queue", **bad)).json())
    body = (await call(sab, "history", limit="100000")).json()
    assert arr_history_accepts(body) == []


async def test_every_native_condition_reads_as_a_status_arr_parses(sab):
    from api.arr_sabnzbd import _QUEUE_STATUS

    assert set(_QUEUE_STATUS.values()) <= ARR_STATUSES
    # Every native unfinished reason, including the two a completed-phase
    # refresh can leave while material is not yet verifiable or shared work
    # is still running, maps to a parseable non-terminal status.
    for reason in ("awaiting_resolution", "queued", "resolving", "input_required", "downloading", "verifying",
                   "finalizing", "paused", "processing", "served_by_shared_transfer", "verifying_material"):
        assert _QUEUE_STATUS.get(reason, "Downloading") in ARR_STATUSES - {"Completed", "Failed", "Deleted"}
