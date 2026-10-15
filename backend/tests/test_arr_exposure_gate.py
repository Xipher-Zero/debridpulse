"""DP 1.0.13 *arr compatibility dialects are exposed only while usable.

A dialect (SABnzbd, qBittorrent) is reachable at all only while the current
delivery-profile catalog and the current tokens make it usable: some profile
with a category, permitting a source kind the dialect accepts (SABnzbd: an
uploaded ``nzb``; qBittorrent: ``magnet`` or ``torrent``), lists a client that
holds a token ``resolve_token`` would accept. Otherwise every path of the
dialect -- default and prefixed, every method, every caller -- is the ordinary
``404``, decided at the authentication boundary before any credential is read.
Exposure is derived per request, never stored or toggled; once exposed, every
request still proves its own token and sees only its own client.

Everything here runs through the real application, middleware and Admin
routes (``test_external_client_foundation``'s harness).
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from starlette.routing import compile_path

import auth.accounts as accounts_module
import db.database as database
from external import arr
from test_arr_default_endpoints import QB_BASES, SAB_PATHS, served_routes
from test_arr_sabnzbd_adapter import NZB, sab  # noqa: F401 - fixture
from test_external_client_foundation import demands, http, world  # noqa: F401 - fixtures

pytestmark = pytest.mark.asyncio

NOT_FOUND = {"detail": "Not Found"}
BASIC = {"Authorization": "Basic b3BlcmF0b3I6ZXh0ZXJuYWwtc2VjcmV0"}


@pytest_asyncio.fixture
async def gate(sab, world):
    """The full harness with an EMPTY profile catalog (a fresh installation's)
    and a fresh Integration account whose tokens the cases issue through the
    Admin routes. ``world``'s own tokens stay active and unlisted."""
    client, admin = sab.client, sab.http.admin_headers
    created = await client.post("/api/accounts", headers=admin, json={"kind": "integration", "display_name": "Lidarr"})
    assert created.status_code == 200, created.text
    await put_catalog(sab)
    yield type("Gate", (), {"client": client, "admin": admin, "sab": sab, "world": world,
                            "account": created.json()["id"]})()


async def put_catalog(sab, *profiles, expect=200):
    client, admin = sab.client, sab.http.admin_headers
    revision = (await client.get("/api/external/profiles", headers=admin)).json()["revision"]
    response = await client.put("/api/external/profiles", headers=admin,
                                json={"expected_revision": revision, "profiles": list(profiles)})
    assert response.status_code == expect, response.text
    return response


def profile(world, pid, kinds, clients, categories=None):
    return {"id": pid, "categories": [pid] if categories is None else categories, "clients": clients,
            "source_kinds": kinds, "local_root": str(world.payloads), "import_root": f"/imports/{pid}"}


async def issue(gate, label="arr", succeeds=None, account=None):
    body = {"label": label, **({"succeeds": succeeds} if succeeds else {})}
    response = await gate.client.post(f"/api/accounts/{account or gate.account}/tokens", headers=gate.admin, json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def revoke(gate, token, account=None):
    response = await gate.client.delete(f"/api/accounts/{account or gate.account}/tokens/{token['id']}",
                                        headers=gate.admin)
    assert response.status_code == 200, response.text


async def set_enabled(gate, enabled, account=None):
    response = await gate.client.patch(f"/api/accounts/{account or gate.account}", headers=gate.admin,
                                       json={"enabled": enabled})
    assert response.status_code == 200, response.text


async def sab_get(gate, path, key, headers=None, mode="version"):
    params = {"mode": mode, "output": "json", **({"apikey": key} if key is not None else {})}
    return await gate.client.get(path, params=params, headers=headers)


async def qb_get(gate, base, key, headers=None, call="app/webapiVersion"):
    headers = dict(headers or {})
    if key is not None:
        headers["Authorization"] = f"Bearer {key}"
    return await gate.client.get(f"{base}/{call}", headers=headers)


async def exposure(gate, key) -> tuple[bool, bool]:
    """(SABnzbd, qBittorrent) as a client holding ``key`` sees them; the two
    path forms of each always agree."""
    sab_status = {(await sab_get(gate, path, key)).status_code for path in SAB_PATHS}
    qb_status = {(await qb_get(gate, base, key)).status_code for base in QB_BASES}
    assert len(sab_status) == 1 and len(qb_status) == 1, (sab_status, qb_status)
    return sab_status.pop() == 200, qb_status.pop() == 200


async def assert_hidden(gate, adapter, keys=()):
    """Every path of a hidden dialect is the ordinary Not Found for every
    caller and method: no protocol body, challenge, redirect or method list."""
    unknown = await gate.client.get("/api/does-not-exist", headers=gate.admin)
    assert (unknown.status_code, unknown.json()) == (404, NOT_FOUND)
    callers = [{}, gate.admin, {**gate.admin, "Accept": "text/html"}, BASIC, {"Accept": "text/html"}]
    if adapter == arr.SABNZBD:
        paths = list(SAB_PATHS)
        params = [{"mode": "get_config", "output": "json", **({"apikey": key} if key else {})}
                  for key in (None, "dp_wrong", *keys)]
    else:
        paths = [f"{base}/{call}" for base in QB_BASES for call in ("app/webapiVersion", "torrents/add")]
        params = [{}]
        callers += [{"Authorization": "Bearer dp_wrong"}, *({"Authorization": f"Bearer {key}"} for key in keys)]
    for path in paths:
        for query in params:
            for headers in callers:
                for method in ("GET", "POST", "PUT", "DELETE"):
                    response = await gate.client.request(method, path, params=query, headers=headers)
                    assert (response.status_code, response.json()) == (404, NOT_FOUND), (method, path, headers)
                    assert "www-authenticate" not in response.headers and "location" not in response.headers
                    assert "allow" not in response.headers
                    for key in keys:
                        assert key not in response.text
        # A CORS preflight is no way around it.
        preflight = await gate.client.options(path, headers={"Origin": "http://127.0.0.1",
                                                             "Access-Control-Request-Method": "POST"})
        assert preflight.status_code == 404 and "allow" not in preflight.headers


# ── 1. a fresh installation exposes neither dialect ─────────────────────────

async def test_a_fresh_installation_exposes_neither_dialect_on_any_path(gate):
    token = await issue(gate)
    await assert_hidden(gate, arr.SABNZBD, keys=(token["token"], gate.sab.key))
    await assert_hidden(gate, arr.QBITTORRENT, keys=(token["token"], gate.sab.key))
    # An upload beyond the dialect's own ceiling is not told apart either:
    # the body is never read, and the answer is the same Not Found.
    for path in (*SAB_PATHS, *(f"{base}/torrents/add" for base in QB_BASES)):
        response = await gate.client.post(path, content=b"x" * (arr.UPLOAD_CEILINGS.get(path, 0) + 1),
                                          params={"mode": "addfile", "apikey": token["token"]})
        assert (response.status_code, response.json()) == (404, NOT_FOUND), path


# ── 2. what does and does not make a dialect usable ─────────────────────────

@pytest.mark.parametrize("variant", ["no_clients", "never_issued", "revoked_only", "unlisted_token", "no_category",
                                     "disabled_account", "deleted_account", "links_only"])
async def test_a_profile_that_is_not_usable_exposes_nothing(gate, variant):
    world, token = gate.world, await issue(gate)
    clients, kinds, categories = [token["client_id"]], ["nzb", "magnet", "torrent"], None
    if variant == "no_clients":
        clients = []
    elif variant == "never_issued":
        clients = ["client_never_issued"]
    elif variant == "revoked_only":
        await revoke(gate, token)
    elif variant == "unlisted_token":
        # An active token of another client than the one listed.
        listed = await issue(gate, "listed")
        await revoke(gate, listed)
        clients = [listed["client_id"]]
    elif variant == "no_category":
        categories = []
    elif variant == "disabled_account":
        await set_enabled(gate, False)
    elif variant == "deleted_account":
        assert (await gate.client.delete(f"/api/accounts/{gate.account}", headers=gate.admin)).status_code == 200
    else:
        kinds = ["links", "nzb_link"]
    await put_catalog(gate.sab, profile(world, "music", kinds, clients, categories))
    await assert_hidden(gate, arr.SABNZBD, keys=(token["token"],))
    await assert_hidden(gate, arr.QBITTORRENT, keys=(token["token"],))


@pytest.mark.parametrize("kinds,expected", [
    (["nzb"], (True, False)), (["nzb_link"], (False, False)), (["links"], (False, False)),
    (["magnet"], (False, True)), (["torrent"], (False, True)), (["nzb", "torrent"], (True, True)),
    (["links", "magnet", "nzb_link", "nzb", "torrent"], (True, True))])
async def test_each_dialect_is_exposed_by_its_own_source_kinds_alone(gate, kinds, expected):
    token = await issue(gate)
    await put_catalog(gate.sab, profile(gate.world, "music", kinds, [token["client_id"]]))
    assert await exposure(gate, token["token"]) == expected
    for adapter, exposed in zip((arr.SABNZBD, arr.QBITTORRENT), expected):
        if not exposed:
            await assert_hidden(gate, adapter, keys=(token["token"],))


# ── 3. exposed, every request still proves its own token and client ───────

async def test_an_exposed_dialect_keeps_its_authentication_and_client_isolation(gate):
    world, sab = gate.world, gate.sab
    owner = await issue(gate, "owner")
    # Another active Integration client the profile does not list.
    stranger = await issue(gate, "stranger")
    await put_catalog(sab, profile(world, "music", ["nzb", "magnet"], [owner["client_id"]]))
    assert await exposure(gate, owner["token"]) == (True, True)
    for path in SAB_PATHS:
        assert (await sab_get(gate, path, None)).json() == {"status": False, "error": "API Key Required"}
        assert (await sab_get(gate, path, "dp_wrong")).json() == {"status": False, "error": "API Key Incorrect"}
        # No session, Basic or browser stands in for the key.
        for headers in (gate.admin, BASIC):
            assert (await sab_get(gate, path, None, headers=headers)).json()["error"] == "API Key Required"
        categories = (await sab_get(gate, path, owner["token"], mode="get_config")).json()["config"]["categories"]
        assert [item["name"] for item in categories] == ["music"]
        assert (await sab_get(gate, path, stranger["token"], mode="get_config")).json()["config"]["categories"] == []
    for base in QB_BASES:
        assert (await qb_get(gate, base, None)).status_code == 401
        assert (await qb_get(gate, base, "dp_wrong")).status_code == 401
        assert (await gate.client.get(f"{base}/app/webapiVersion", params={"apikey": owner["token"]})).status_code == 401
        assert (await qb_get(gate, base, None, headers=gate.admin)).status_code == 403
        assert (await qb_get(gate, base, stranger["token"], call="torrents/categories")).json() == {}
    # The owner's job is invisible to and untouchable by the stranger.
    added = await gate.client.post("/api", params={"mode": "addfile", "cat": "music", "priority": "-100",
                                                   "apikey": owner["token"], "output": "json"},
                                   files={"name": ("Album.nzb", NZB, "application/x-nzb")})
    nzo_id = added.json()["nzo_ids"][0]
    queue = (await sab_get(gate, "/api", stranger["token"], mode="queue")).json()["queue"]["slots"]
    assert queue == []
    await gate.client.get("/api", params={"mode": "queue", "name": "delete", "value": nzo_id, "apikey": stranger["token"],
                                          "output": "json"})
    assert [row["withdrawn_at"] for row in await demands()] == [None]
    # Revoking the owner's token hides both dialects at once; the job stays.
    await revoke(gate, owner)
    await assert_hidden(gate, arr.SABNZBD, keys=(owner["token"], stranger["token"]))
    await assert_hidden(gate, arr.QBITTORRENT, keys=(owner["token"], stranger["token"]))
    assert [row["withdrawn_at"] for row in await demands()] == [None]


# ── 4. Admin changes take effect on the next request ────────────────────────

async def test_exposure_follows_every_admin_change_on_the_next_request(gate):
    world, sab = gate.world, gate.sab
    # Token first, profile second.
    first = await issue(gate, "first")
    assert await exposure(gate, first["token"]) == (False, False)
    await put_catalog(sab, profile(world, "music", ["nzb"], [first["client_id"]]))
    assert await exposure(gate, first["token"]) == (True, False)
    # Editing the source kinds moves the exposure between dialects.
    await put_catalog(sab, profile(world, "music", ["magnet"], [first["client_id"]]))
    assert await exposure(gate, first["token"]) == (False, True)
    await put_catalog(sab, profile(world, "music", ["magnet"], [first["client_id"]], categories=[]))
    assert await exposure(gate, first["token"]) == (False, False)
    await put_catalog(sab, profile(world, "music", ["nzb", "torrent"], [first["client_id"]]))
    assert await exposure(gate, first["token"]) == (True, True)
    # Revoking the last token hides both; a rotation onto that client, or a
    # new token added to the profile, exposes them again.
    await revoke(gate, first)
    assert await exposure(gate, first["token"]) == (False, False)
    rotated = await issue(gate, "rotated", succeeds=first["id"])
    assert rotated["client_id"] == first["client_id"]
    assert await exposure(gate, rotated["token"]) == (True, True)
    # Disabling the account hides them; enabling it exposes them.
    await set_enabled(gate, False)
    assert await exposure(gate, rotated["token"]) == (False, False)
    await set_enabled(gate, True)
    assert await exposure(gate, rotated["token"]) == (True, True)
    # Two profiles: removing one leaves the other's exposure.
    second = await issue(gate, "second")
    await put_catalog(sab, profile(world, "music", ["nzb", "torrent"], [rotated["client_id"]]),
                      profile(world, "books", ["nzb"], [second["client_id"]]))
    await put_catalog(sab, profile(world, "books", ["nzb"], [second["client_id"]]))
    assert await exposure(gate, rotated["token"]) == (True, False)
    # Removing the last listed client hides it.
    await put_catalog(sab, profile(world, "books", ["nzb"], []))
    assert await exposure(gate, second["token"]) == (False, False)


async def test_a_refused_catalog_change_leaves_the_exposure_as_it_was(gate):
    world, sab = gate.world, gate.sab
    token = await issue(gate)
    await put_catalog(sab, profile(world, "music", ["nzb"], [token["client_id"]]))
    added = await gate.client.post("/api", params={"mode": "addfile", "cat": "music", "priority": "-100",
                                                   "apikey": token["token"], "output": "json"},
                                   files={"name": ("Album.nzb", NZB, "application/x-nzb")})
    assert added.json()["status"] is True
    # The profile serves an unsettled job: deleting it, or removing its
    # client, is refused (profile_in_use) and changes nothing.
    for refused in ((), (profile(world, "music", ["nzb"], []),)):
        response = await put_catalog(sab, *refused, expect=409)
        assert response.json()["detail"]["code"] == "profile_in_use"
        assert await exposure(gate, token["token"]) == (True, False)
    # Narrowing its source kinds is not guarded and hides SABnzbd at once.
    await put_catalog(sab, profile(world, "music", ["magnet"], [token["client_id"]]))
    assert await exposure(gate, token["token"]) == (False, True)


# ── 5. exact paths, and no other route changes ──────────────────────────────

async def test_the_gate_recognizes_exactly_the_paths_the_dialects_serve():
    from api.arr_qbittorrent import qbittorrent_api
    from api.arr_sabnzbd import sabnzbd_api

    patterns = [compile_path("/api" + route.path)[0] for route in served_routes()
                if getattr(route, "endpoint", None) in (sabnzbd_api, qbittorrent_api)]
    assert len(patterns) == 4
    samples = ["/api", "/api/", "/api/sabnzbd/api", "/api/sabnzbd/api/", "/api/sabnzbd", "/api/v2",
               "/api/v2/app", "/api/v2/app/webapiVersion", "/api/v2/app/webapiVersion/", "/api/v2/a/b/c",
               "/api/qbittorrent/api/v2/torrents/add", "/api/qbittorrent/api/v2/x", "/api/qbittorrent/api/v2/a/b/c",
               "/api/v2//x", "/api/settings", "/api/health", "/api/external/v1/jobs", "/", "/login", "/docs",
               "/apiv2/a/b", "/api/v22/a/b", "/xapi"]
    for path in samples:
        served = any(pattern.match(path) for pattern in patterns)
        assert (arr.adapter_of(path) is not None) == served, path


async def test_native_routes_and_the_interface_are_unchanged_while_both_dialects_are_hidden(gate):
    client, admin, sab = gate.client, gate.admin, gate.sab
    assert (await client.get("/api/health")).status_code == 200
    assert (await client.get("/api/settings")).status_code == 401
    assert (await client.get("/api/settings", headers=admin)).status_code == 200
    assert (await client.get("/api/settings", headers=sab.http.client_headers)).status_code == 403
    assert (await client.get("/api/external/v1/capabilities", headers=sab.http.client_headers)).status_code == 200
    assert (await client.get("/api/external/profiles", headers=admin)).status_code == 200
    assert (await client.get("/", headers=sab.http.client_headers)).status_code == 403
    for path in ("/api/", "/api/v2/a/b/c", "/api/sabnzbd/api/"):
        assert (await client.get(path, params={"apikey": sab.key})).status_code == 401, path
    # The native submission path keeps its own ceiling and authentication.
    oversized = await client.post(arr.NATIVE_JOBS_PATH, content=b"x" * (arr.UPLOAD_CEILINGS[arr.NATIVE_JOBS_PATH] + 1),
                                  headers=sab.http.client_headers)
    assert oversized.status_code == 413


# ── 6. bounded cost ─────────────────────────────────────────────────────────

async def test_each_request_asks_one_bounded_question_and_an_empty_catalog_asks_none(gate, monkeypatch):
    asked, opened = [], []
    original_ask, original_db = accounts_module.any_active_client, database.get_db

    async def ask(clients):
        asked.append(sorted(clients))
        return await original_ask(clients)

    def counting_db(*args, **kwargs):
        opened.append(1)
        return original_db(*args, **kwargs)

    monkeypatch.setattr(accounts_module, "any_active_client", ask)
    monkeypatch.setattr(database, "get_db", counting_db)
    for _ in range(10):
        assert (await sab_get(gate, "/api", "dp_wrong")).status_code == 404
        assert (await qb_get(gate, "/api/v2", "dp_wrong")).status_code == 404
    # No usable profile: decided from the in-memory catalog alone.
    assert asked == [] and opened == []
    token = await issue(gate)
    await put_catalog(gate.sab, profile(gate.world, "music", ["links"], [token["client_id"]]),
                      profile(gate.world, "books", ["nzb"], ["client_never_issued"]))
    asked.clear(), opened.clear()
    for _ in range(10):
        assert (await sab_get(gate, "/api", "dp_wrong")).status_code == 404
    # One existence read per request, of exactly the eligible clients.
    assert asked == [["client_never_issued"]] * 10 and len(opened) == 10
