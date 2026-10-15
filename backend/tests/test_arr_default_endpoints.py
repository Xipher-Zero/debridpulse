"""DP 1.0.13 *arr adapters at the protocols' own default paths.

Sonarr and Radarr build a download client's URLs from Host, Port and URL Base;
with URL Base blank (their default) a SABnzbd client calls ``/api?mode=…`` and
a qBittorrent client ``/api/v2/{section}/{method}``. Live Radarr's SABnzbd Test
(``GET /api?mode=get_config&apikey=…&output=json``) was answered ``401``: the
request never reached the adapter. Each default path is now one more route of
the SAME adapter callable, so it is the same endpoint -- the same
authentication, ACL action, upload ceiling, admission and answers -- as the
prefixed path, which stays. These proofs run each case through both forms.
"""
from __future__ import annotations

import base64

import pytest
from fastapi.routing import APIRoute

import core.config as config
import main
from auth import accounts
from auth.permissions import ACTIONS, Capability, action_name
from external import arr
from external.intake import MAX_NZB_BYTES, MAX_TORRENT_BYTES
from test_arr_qbittorrent_adapter import HASH, MAGNET, OTHER_HASH, OTHER_MAGNET, info_hash, large_torrent, qb  # noqa: F401
from test_arr_qbittorrent_adapter import arr_preferences, arr_torrents, arr_version
from test_arr_sabnzbd_adapter import NZB, OTHER_NZB, arr_error, arr_history_accepts, arr_queue_accepts, sab  # noqa: F401
from test_external_client_foundation import demands, http, world  # noqa: F401 - fixtures

pytestmark = pytest.mark.asyncio

SAB_PATHS = ("/api", "/api/sabnzbd/api")
QB_BASES = ("/api/v2", "/api/qbittorrent/api/v2")


def open_mode():
    current = config.get_settings().model_copy(update={"auth_password_enabled": False})
    config.apply_settings(current)
    config.save_settings(current)


async def sab_call(sab, path, mode, *, key=None, method="GET", files=None, headers=None, **params):
    query = {"mode": mode, **params, "output": "json"}
    if key != "":
        query["apikey"] = sab.key if key is None else key
    response = await sab.client.request(method, path, params=query, files=files, headers=headers)
    assert not response.is_redirect, (path, response.headers.get("location"))
    return response


async def sab_add(sab, path, data=NZB, filename="Show.S01E01.nzb"):
    return await sab_call(sab, path, "addfile", method="POST", cat="tv", priority="-100",
                          files={"name": (filename, data, "application/x-nzb")})


async def jobs():
    import db.database as database

    async with database.get_db() as db:
        return await db.fetchall("SELECT * FROM external_jobs ORDER BY created_at")


async def qb_request(qb, base, method, path, headers=None, **kwargs):
    response = await qb.client.request(method, f"{base}/{path}", headers=qb.headers if headers is None else headers,
                                       **kwargs)
    assert not response.is_redirect, (base, path, response.headers.get("location"))
    return response


# ── the route table: one callable, two paths, one ACL action ────────────────

def served_routes():
    for entry in main.app.routes:
        nested = getattr(entry, "original_router", None)
        yield from (nested.routes if nested is not None else [entry])


async def test_each_default_path_is_one_more_route_of_the_same_adapter_callable():
    from api.arr_qbittorrent import qbittorrent_api
    from api.arr_sabnzbd import sabnzbd_api

    by_endpoint = {}
    for route in served_routes():
        endpoint = getattr(route, "endpoint", None)
        if endpoint in (sabnzbd_api, qbittorrent_api):
            by_endpoint.setdefault(endpoint, []).append((route.path, sorted(route.methods)))
    # Router-relative paths (the router is included under ``/api``).
    assert sorted(by_endpoint[sabnzbd_api]) == [("", ["GET", "POST"]), ("/sabnzbd/api", ["GET", "POST"])]
    assert sorted(by_endpoint[qbittorrent_api]) == [("/qbittorrent/api/v2/{section}/{method}", ["GET", "POST"]),
                                                    ("/v2/{section}/{method}", ["GET", "POST"])]
    # Both forms are the one action already classified, nothing new.
    assert ACTIONS[action_name(sabnzbd_api)] is Capability.EXTERNAL_CLIENT
    assert ACTIONS[action_name(qbittorrent_api)] is Capability.EXTERNAL_CLIENT
    # The adapter-owned declarations the shared boundaries consume.
    assert arr.QUERY_KEY_PATHS == frozenset(SAB_PATHS)
    assert arr.UPLOAD_CEILINGS["/api"] == arr.UPLOAD_CEILINGS["/api/sabnzbd/api"] == MAX_NZB_BYTES + 64 * 1024
    assert (arr.UPLOAD_CEILINGS["/api/v2/torrents/add"] == arr.UPLOAD_CEILINGS["/api/qbittorrent/api/v2/torrents/add"]
            == MAX_TORRENT_BYTES + 64 * 1024)
    # No native API route lives at, or beneath, either default path, whether
    # relative to the ``/api`` include prefix or (the two auth routers) absolute;
    # the static interface is a Mount.
    for route in served_routes():
        if isinstance(route, APIRoute) and route.endpoint not in (sabnzbd_api, qbittorrent_api):
            assert route.path not in ("", "/api") and not route.path.startswith(("/v2/", "/api/v2/")), route.path


# ── SABnzbd at /api ─────────────────────────────────────────────────────────

async def test_radarrs_default_connection_test_reaches_the_sabnzbd_adapter(sab):
    # The exact live request, URL Base blank.
    observed = await sab.client.get("/api", params={"mode": "get_config", "apikey": sab.key, "output": "json"})
    assert observed.status_code == 200 and not observed.is_redirect
    config_body = observed.json()["config"]
    assert [(item["name"], item["dir"]) for item in config_body["categories"]] == [("tv", "/imports/tv")]
    for mode in ("get_config", "version", "queue", "history", "nonsense", ""):
        default, prefixed = [(await sab_call(sab, path, mode)).json() for path in SAB_PATHS]
        assert default == prefixed, mode
    assert arr_error((await sab_call(sab, "/api", "")).json()) == "not implemented"


async def test_jobs_submitted_through_either_form_are_one_clients_items(sab):
    first = (await sab_add(sab, "/api")).json()
    second = (await sab_add(sab, "/api/sabnzbd/api", data=OTHER_NZB, filename="Show.S01E02.nzb")).json()
    assert first["status"] is True and second["status"] is True
    # One request, one job, one demand.
    assert len(await jobs()) == 2 and len(await demands()) == 2
    queues = [arr_queue_accepts((await sab_call(sab, path, "queue", start="0", limit="0")).json()) for path in SAB_PATHS]
    assert queues[0] == queues[1]
    assert {slot["nzo_id"] for slot in queues[0]} == {first["nzo_ids"][0], second["nzo_ids"][0]}
    histories = [arr_history_accepts((await sab_call(sab, path, "history", start="0", limit="60")).json())
                 for path in SAB_PATHS]
    assert histories[0] == histories[1]
    # Removing through the default form is the adapter's own removal.
    removed = await sab_call(sab, "/api", "queue", name="delete", value=first["nzo_ids"][0], del_files="1")
    assert removed.json()["status"] is True
    assert [row["withdrawn_at"] is not None for row in await demands()].count(True) == 1


@pytest.mark.parametrize("variant", ["missing", "wrong", "revoked"])
async def test_a_missing_or_invalid_key_at_api_is_the_adapters_refusal(sab, world, variant):
    key = {"missing": "", "wrong": "dp_wrong"}.get(variant)
    if variant == "revoked":
        await accounts.revoke_token(world.accounts.sonarr.id, sab.http.token["id"])
    for path in SAB_PATHS:
        response = await sab_call(sab, path, "get_config", key=key)
        assert response.status_code == 200
        assert arr_error(response.json()) == ("API Key Required" if variant == "missing" else "API Key Incorrect")
        assert (await sab_add(sab, path) if key is None else await sab_call(
            sab, path, "addfile", key=key, method="POST", cat="tv",
            files={"name": ("x.nzb", NZB, "application/x-nzb")})).json()["status"] is False
    assert await jobs() == []


@pytest.mark.parametrize("mode", ["password", "open"])
async def test_no_session_basic_or_open_mode_stands_in_for_the_key_at_api(sab, mode):
    if mode == "open":
        open_mode()
    browser = {**sab.http.admin_headers, "Accept": "text/html"}
    for headers in ({}, sab.http.admin_headers, browser, {"Authorization": "Basic b3BlcmF0b3I6ZXh0ZXJuYWwtc2VjcmV0"}):
        for path in SAB_PATHS:
            response = await sab_call(sab, path, "queue", key="", headers=headers)
            assert response.status_code == 200 and arr_error(response.json()) == "API Key Required", (path, headers)
    # A browser navigation carrying a key is answered, never redirected to
    # the sign-in page with the key in its URL.
    navigation = await sab.client.get("/api", params={"mode": "version", "apikey": "dp_wrong"},
                                      headers={"Accept": "text/html"})
    assert navigation.status_code == 200 and "dp_wrong" not in navigation.text
    # The token as a Bearer header is the same client, on both forms.
    for path in SAB_PATHS:
        bearer = await sab.client.get(path, params={"mode": "queue", "output": "json"}, headers=sab.http.client_headers)
        assert arr_queue_accepts(bearer.json()) == []
    assert await jobs() == []


async def test_the_query_key_authenticates_only_the_two_exact_protocol_paths(sab):
    for method, path in (("GET", "/api/"), ("GET", "/api/torrents"), ("GET", "/api/settings"),
                         ("GET", "/api/external/v1/jobs"), ("POST", "/api/processing/pause"),
                         ("GET", "/api/sabnzbd"), ("GET", "/api/sabnzbd/api/"), ("GET", "/api/v2"),
                         ("GET", "/api/v2/app/webapiVersion"), ("GET", "/"), ("GET", "/index.html")):
        response = await sab.client.request(method, path, params={"mode": "get_config", "apikey": sab.key})
        assert response.status_code == 401, (method, path, response.status_code)
        assert "categories" not in response.text
    # A method the endpoint does not serve is refused after the key, as on
    # the prefixed form.
    for path in SAB_PATHS:
        assert (await sab_call(sab, path, "version", method="PUT")).status_code == 405


async def test_the_nzb_upload_ceiling_is_the_same_at_api(sab):
    for path in SAB_PATHS:
        oversized = await sab_add(sab, path, data=b"x" * (MAX_NZB_BYTES + 128 * 1024))
        assert (oversized.status_code, oversized.text) == (413, "Request body too large"), path
        too_large = await sab_add(sab, path, data=b"x" * (MAX_NZB_BYTES + 1))
        assert "source_too_large" in arr_error(too_large.json()), path
    assert await jobs() == []


async def test_a_post_enters_application_admission_once_on_either_form(sab, world, monkeypatch):
    entered = []
    original = type(world.application).application_operation

    def counting(self, *args, **kwargs):
        entered.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(world.application), "application_operation", counting)
    counts = []
    for path, data in zip(SAB_PATHS, (NZB, OTHER_NZB)):
        entered.clear()
        assert (await sab_add(sab, path, data=data)).json()["status"] is True
        counts.append(len(entered))
    assert counts[0] == counts[1] and counts[0] >= 1


# ── qBittorrent at /api/v2 ──────────────────────────────────────────────────

async def test_the_qbittorrent_connection_test_answers_at_the_default_base(qb):
    for path in ("app/webapiVersion", "app/preferences", "torrents/categories", "torrents/info"):
        default, prefixed = [await qb_request(qb, base, "GET", path) for base in QB_BASES]
        assert (default.status_code, default.text) == (prefixed.status_code, prefixed.text) and default.status_code == 200
    assert arr_version((await qb_request(qb, "/api/v2", "GET", "app/webapiVersion")).text) >= (2, 11, 0)
    assert arr_preferences((await qb_request(qb, "/api/v2", "GET", "app/preferences")).json())["save_path"] == "/imports/tv"
    assert (await qb_request(qb, "/api/v2", "GET", "torrents/categories")).json() == {
        "tv": {"name": "tv", "savePath": "/imports/tv"}}
    for base in QB_BASES:
        assert (await qb_request(qb, base, "GET", "nonsense/call")).status_code == 404


async def test_torrents_added_through_either_base_are_one_clients_items(qb):
    magnet = await qb_request(qb, "/api/v2", "POST", "torrents/add", data={"urls": MAGNET, "category": "tv"})
    assert magnet.status_code == 200 and magnet.json()["added_torrent_ids"] == [HASH]
    # The same hash again, through the other base: the same item.
    again = await qb_request(qb, "/api/qbittorrent/api/v2", "POST", "torrents/add", data={"urls": MAGNET, "category": "tv"})
    assert again.json()["added_torrent_ids"] == [HASH]
    from test_arr_qbittorrent_adapter import torrent_file
    data = torrent_file()
    uploaded = await qb_request(qb, "/api/v2", "POST", "torrents/add", data={"category": "tv"},
                                files={"torrents": ("Show.S01.torrent", data, "application/x-bittorrent")})
    assert uploaded.json()["added_torrent_ids"] == [info_hash(data)]
    assert len(await jobs()) == 2 and len(await demands()) == 2
    listed = [arr_torrents((await qb_request(qb, base, "GET", "torrents/info")).json()) for base in QB_BASES]
    assert listed[0] == listed[1] and {item["hash"] for item in listed[0]} == {HASH, info_hash(data)}
    # Removal through the default base is the adapter's own.
    removed = await qb_request(qb, "/api/v2", "POST", "torrents/delete", data={"hashes": HASH, "deleteFiles": "true"})
    assert removed.status_code == 200
    assert [row["withdrawn_at"] is not None for row in await demands()].count(True) == 1


async def test_the_default_base_is_bearer_only(qb, world):
    for base in QB_BASES:
        for headers in ({}, {"Authorization": "Bearer dp_wrong"}, {"Authorization": "Basic b3BlcmF0b3I6eA=="}):
            assert (await qb_request(qb, base, "GET", "app/webapiVersion", headers=headers)).status_code == 401
        # The key in the URL is never a credential here.
        assert (await qb_request(qb, base, "GET", "app/webapiVersion", headers={},
                                 params={"apikey": qb.key})).status_code == 401
        # A browser session is no external client.
        assert (await qb_request(qb, base, "GET", "torrents/info", headers=qb.http.admin_headers)).status_code == 403
    await accounts.revoke_token(world.accounts.sonarr.id, qb.http.token["id"])
    for base in QB_BASES:
        assert (await qb_request(qb, base, "GET", "app/webapiVersion")).status_code == 401


async def test_open_mode_never_stands_in_for_the_bearer_key_at_the_default_base(qb):
    open_mode()
    for base in QB_BASES:
        assert (await qb_request(qb, base, "GET", "torrents/info", headers={})).status_code == 403
        assert (await qb_request(qb, base, "POST", "torrents/add", headers={},
                                 data={"urls": OTHER_MAGNET, "category": "tv"})).status_code == 403
    assert await jobs() == []


async def test_the_torrent_upload_ceiling_is_the_same_at_the_default_base(qb):
    beyond = large_torrent(MAX_TORRENT_BYTES + 128 * 1024)
    for base in QB_BASES:
        response = await qb_request(qb, base, "POST", "torrents/add", data={"category": "tv"},
                                    files={"torrents": ("x.torrent", beyond, "application/x-bittorrent")})
        assert (response.status_code, response.text) == (413, "Request body too large"), base
    within = large_torrent(int(15.5 * 1024 * 1024))
    assert MAX_TORRENT_BYTES - 1024 * 1024 < len(within) <= MAX_TORRENT_BYTES
    accepted = await qb_request(qb, "/api/v2", "POST", "torrents/add", data={"category": "tv"},
                                files={"torrents": ("Pack.torrent", within, "application/x-bittorrent")})
    assert accepted.status_code == 200 and accepted.json()["added_torrent_ids"] == [info_hash(within)]
    assert len(await jobs()) == 1
    # The native submission's own ceiling is untouched.
    assert arr.UPLOAD_CEILINGS[arr.NATIVE_JOBS_PATH] == (MAX_TORRENT_BYTES * 4) // 3 + 64 * 1024
    assert base64.b64encode(within)


# ── native routes keep their owners ─────────────────────────────────────────

async def test_native_routes_and_the_interface_keep_their_owners(sab):
    client, http = sab.client, sab.http
    assert (await client.get("/api/health")).status_code == 200
    assert (await client.get("/api/settings")).status_code == 401
    assert (await client.get("/api/settings", headers=http.admin_headers)).status_code == 200
    assert (await client.get("/api/settings", headers=http.client_headers)).status_code == 403
    assert (await client.get("/api/external/v1/capabilities", headers=http.client_headers)).status_code == 200
    # The interface is not an Integration token's.
    assert (await client.get("/", headers=http.client_headers)).status_code == 403
    # An unknown native path stays unknown, for every principal.
    assert (await client.get("/api/does-not-exist", headers=http.admin_headers)).status_code == 404
    assert (await client.get("/api/v2/app/webapiVersion", headers=http.admin_headers)).status_code == 403


async def test_the_default_paths_answer_the_same_behind_an_https_proxy(sab):
    # Routing is by path alone: a TLS-terminating proxy's scheme, host and
    # forwarding headers change nothing, and no proxy setting is required.
    forwarded = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "dp.example", "X-Forwarded-For": "10.0.0.9"}
    direct = (await sab.client.get("/api", params={"mode": "get_config", "apikey": sab.key})).json()
    proxied = await sab.client.get("https://dp.example/api", params={"mode": "get_config", "apikey": sab.key},
                                   headers=forwarded)
    assert proxied.status_code == 200 and proxied.json() == direct
    bearer = await sab.client.get("https://dp.example/api/v2/app/webapiVersion",
                                  headers={**forwarded, **sab.http.client_headers})
    assert (bearer.status_code, bearer.text) == (200, "2.15.1")
