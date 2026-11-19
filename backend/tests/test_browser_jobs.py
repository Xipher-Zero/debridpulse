"""WS-S2: a connected browser's own downloads (``external.service`` browser jobs).

Live proofs against a real backend in its own process (the WS-S1 harness):
real sign-ins, real browser tokens from real pairings, real submissions
through the external-client owner. Nothing in the server is replaced.
"""
from __future__ import annotations

import asyncio
import base64
import json
import uuid

import pytest

from test_browser_pairing import PASSWORD, Person, Server, anonymous, extension, pair

PUBLIC_LINK = "https://203.0.113.10/releases/file.iso"      # TEST-NET-3: never resolved, never private
LAN_LINK = "http://192.168.77.10/share/file.iso"
META4 = ('<?xml version="1.0" encoding="UTF-8"?><metalink xmlns="urn:ietf:params:xml:ns:metalink">'
         '<file name="file.iso"><size>1024</size><url>https://203.0.113.10/file.iso</url></file></metalink>')


def _key() -> str:
    return "k-" + uuid.uuid4().hex


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """One backend; the owner (Admin) and Una (User), each with one connected browser."""
    server = Server(tmp_path_factory.mktemp("browser-jobs"))
    try:
        server.wait_ready()

        async def people():
            owner = await Person.sign_in(server, "owner")
            status, body = await owner.call("POST", "/api/accounts", json={
                "kind": "local", "role": "user", "username": "una", "password": PASSWORD, "display_name": "Una"})
            assert status == 200, body
            una = await Person.sign_in(server, "una")
            tokens = {"owner": (await pair(server, owner, "Owner's Firefox"))[0],
                      "una": (await pair(server, una, "Una's Chrome"))[0]}
            await owner.close()
            await una.close()
            return tokens

        server.tokens = asyncio.run(people())
        yield server
    finally:
        server.stop()


async def submit(server, token, source, key=None, **extra):
    return await extension(server, "POST", "/api/browser/v1/jobs", token,
                           json={"idempotency_key": key or _key(), "source": source, **extra})


@pytest.mark.asyncio
async def test_a_browser_submits_its_own_download_once_and_sees_only_its_own(world):
    una, owner = world.tokens["una"], world.tokens["owner"]
    key = _key()
    status, job = await submit(world, una, {"kind": "links", "links": [PUBLIC_LINK]}, key, display_name="ISO")
    assert status == 201, job
    # The person's own download: no category, profile or delivery; an estimate field.
    assert not {"category", "profile_id", "delivery"} & set(job)
    assert "estimate" in job and job["display_name"] == "ISO"
    # ``name``: the transfer's title as DownloadPulse shows it, the Downloads list's own.
    una_session = await Person.sign_in(world, "una")
    try:
        _, listed = await una_session.call("GET", "/api/torrents", csrf=False)
        shown = {str(item.get("display_name") or item.get("name")) for item in listed.get("items", listed.get("torrents", []))}
    finally:
        await una_session.close()
    assert job["name"] and job["name"] in shown, (job["name"], shown)
    assert job["source"] == {"kind": "links", "count": 1, "hosts": ["203.0.113.10"]}
    # A retry is the same job, never a second download; the same key for something else is refused.
    status, again = await submit(world, una, {"kind": "links", "links": [PUBLIC_LINK]}, key, display_name="ISO")
    assert status == 200 and again["id"] == job["id"]
    status, conflict = await submit(world, una, {"kind": "magnet", "magnet": "magnet:?xt=urn:btih:" + "c" * 40}, key)
    assert status == 409 and conflict["detail"]["code"] == "idempotency_conflict"

    _, mine = await extension(world, "GET", "/api/browser/v1/jobs", una)
    assert job["id"] in {item["id"] for item in mine["jobs"]}
    _, theirs = await extension(world, "GET", "/api/browser/v1/jobs", owner)
    assert job["id"] not in {item["id"] for item in theirs["jobs"]}
    status, _ = await extension(world, "GET", f"/api/browser/v1/jobs/{job['id']}", owner)
    assert status == 404
    # The same download, attributed to Una and to that browser, in her own requests.
    una_person = await Person.sign_in(world, "una")
    try:
        _, requests = await una_person.call("GET", "/api/requests", csrf=False)
        session = (await extension(world, "GET", "/api/browser/v1/session", una))[1]
        assert any(item["credential_id"] == session["token"]["id"] for item in requests["requests"])
    finally:
        await una_person.close()


@pytest.mark.asyncio
async def test_a_browser_submits_every_kind_quick_add_takes(world):
    owner = world.tokens["owner"]
    torrent = b"d4:infod6:lengthi1024e4:name8:file.iso12:piece lengthi16384e6:pieces20:" + b"\x00" * 20 + b"ee"
    sources = [
        {"kind": "magnet", "magnet": "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=one"},
        {"kind": "torrent", "filename": "file.torrent", "torrent_base64": base64.b64encode(torrent).decode()},
        {"kind": "metalink", "filename": "file.meta4", "metalink_base64": base64.b64encode(META4.encode()).decode()},
    ]
    for source in sources:
        status, job = await submit(world, owner, source)
        assert status == 201, (source["kind"], job)
        assert job["source"]["kind"] == source["kind"]
    status, refused = await submit(world, owner, {"kind": "metalink", "filename": "file.metalink",
                                                  "metalink_base64": base64.b64encode(META4.encode()).decode()})
    assert status == 422 and refused["detail"]["code"] == "invalid_source"
    # Nothing outside the browser contract is accepted.
    status, refused = await submit(world, owner, {"kind": "links", "links": [PUBLIC_LINK]}, category="tv")
    assert status == 422 and refused["detail"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_remove_withdraws_only_this_browsers_request(world):
    una = world.tokens["una"]
    _, job = await submit(world, una, {"kind": "links", "links": ["https://203.0.113.11/other.bin"]})
    status, withdrawn = await extension(world, "POST", f"/api/browser/v1/jobs/{job['id']}/withdraw", una)
    assert status == 200, withdrawn
    _, current = await extension(world, "GET", f"/api/browser/v1/jobs/{job['id']}", una)
    assert current["phase"] == "withdrawn"
    status, _ = await extension(world, "POST", f"/api/browser/v1/jobs/{job['id']}/withdraw", world.tokens["owner"])
    assert status == 404


@pytest.mark.asyncio
async def test_pause_and_resume_are_the_protocols_advisory_controls(world):
    una = world.tokens["una"]
    _, job = await submit(world, una, {"kind": "links", "links": ["https://203.0.113.12/paused.bin"]})
    status, paused = await extension(world, "POST", f"/api/browser/v1/jobs/{job['id']}/controls", una,
                                     json={"kind": "pause", "request_key": _key()})
    assert status == 200, paused
    # Its request is the only one this download serves: the pause is honoured, as the browser's own hold.
    assert (paused["disposition"], paused["reason"], paused["effective"]) == ("honored", "paused", "paused")
    _, current = await extension(world, "GET", f"/api/browser/v1/jobs/{job['id']}", una)
    assert (current["reason"], current["paused_by"]) == ("paused", "client")
    status, resumed = await extension(world, "POST", f"/api/browser/v1/jobs/{job['id']}/controls", una,
                                      json={"kind": "resume", "request_key": _key()})
    assert status == 200 and (resumed["disposition"], resumed["reason"]) == ("honored", "resumed")
    # Another browser cannot touch it.
    status, _ = await extension(world, "POST", f"/api/browser/v1/jobs/{job['id']}/controls", world.tokens["owner"],
                                json={"kind": "pause", "request_key": _key()})
    assert status == 404
    status, invalid = await extension(world, "POST", f"/api/browser/v1/jobs/{job['id']}/controls", una,
                                      json={"kind": "delete", "request_key": _key()})
    assert status == 422 and invalid["detail"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_a_browser_job_has_no_delivery_and_the_external_api_stays_closed_to_it(world):
    owner = world.tokens["owner"]
    _, job = await submit(world, owner, {"kind": "links", "links": ["https://203.0.113.13/x.bin"]})
    for method, path in (("GET", f"/api/external/v1/jobs/{job['id']}/delivery"),
                         ("POST", f"/api/external/v1/jobs/{job['id']}/delivery/ack"),
                         ("GET", "/api/external/v1/jobs")):
        status, _ = await extension(world, method, path, owner, json={} if method == "POST" else None)
        assert status == 403, (method, path)


def test_private_lan_links_follow_the_installations_local_network_settings(tmp_path):
    """D4 (operator): Local Network Connections and Skip Local Connection
    Confirmation govern a browser's private-LAN link, exactly as they govern
    any submitter who cannot answer the confirmation; nothing new consents."""
    server = Server(tmp_path)
    try:
        server.wait_ready()

        async def scenario():
            owner = await Person.sign_in(server, "owner")
            try:
                token, _ = await pair(server, owner, "LAN browser")
                assert (await owner.call("PATCH", "/api/transfer-policy", json={
                    "private_lan_connections": True, "skip_private_lan_confirmation": False}))[0] == 200
                status, refused = await submit(server, token, {"kind": "links", "links": [LAN_LINK]})
                assert status == 422 and refused["detail"]["code"] == "local_network_confirmation_required"
                assert (await owner.call("PATCH", "/api/transfer-policy", json={
                    "skip_private_lan_confirmation": True}))[0] == 200
                status, job = await submit(server, token, {"kind": "links", "links": [LAN_LINK]})
                assert status == 201, job
            finally:
                await owner.close()

        asyncio.run(scenario())
    finally:
        server.stop()


@pytest.mark.asyncio
async def test_the_session_route_names_what_this_server_offers_a_browser(world):
    _, session = await extension(world, "GET", "/api/browser/v1/session", world.tokens["una"])
    assert session["capabilities"] == {"sources": ["links", "magnet", "metalink", "nzb", "nzb_link", "torrent"],
                                       "controls": ["pause", "resume"], "withdraw": True, "estimate": True,
                                       "cookies": {"max_count": 300, "max_bytes": 32768, "sources": ["links"]}}
    status, _ = await anonymous(world, "GET", "/api/browser/v1/jobs")
    assert status == 401


# -- a video's sign-in (external.browser_cookies, operator 2026-11-19) -------------------

VIDEO_LINK = "https://www.example-video.test/watch?v=abc"
SECRET = "s3cr3t-cookie-value-9f1c"


def _cookie(**overrides):
    return {"name": "SID", "value": SECRET, "domain": ".example-video.test", "path": "/", "secure": True,
            "http_only": True, "host_only": False, "expires": None, **overrides}


@pytest.mark.asyncio
async def test_a_video_sign_in_goes_only_with_its_own_links_and_is_never_part_of_the_job(world):
    una = world.tokens["una"]
    key = _key()
    status, job = await submit(world, una, {"kind": "links", "links": [VIDEO_LINK]}, key, cookies=[_cookie()])
    assert status == 201, job
    assert SECRET not in json.dumps(job)
    # Not part of the request's identity: the same key without them is the same job, never a conflict.
    status, again = await submit(world, una, {"kind": "links", "links": [VIDEO_LINK]}, key)
    assert status == 200 and again["id"] == job["id"]
    _, listed = await extension(world, "GET", "/api/browser/v1/jobs", una)
    assert SECRET not in json.dumps(listed)
    # Refused before anything is reserved: another site's cookie, a non-links source, a malformed cookie.
    refusals = [
        ({"kind": "links", "links": [VIDEO_LINK]}, [_cookie(domain=".elsewhere.test")]),
        ({"kind": "links", "links": [VIDEO_LINK]}, [_cookie(domain="test")]),
        ({"kind": "magnet", "magnet": "magnet:?xt=urn:btih:" + "d" * 40}, [_cookie()]),
        ({"kind": "links", "links": [VIDEO_LINK]}, [_cookie(value="a;b")]),
        ({"kind": "links", "links": [VIDEO_LINK]}, [_cookie(extra=1)]),
        ({"kind": "links", "links": [VIDEO_LINK]}, []),
        ({"kind": "links", "links": [VIDEO_LINK]}, [_cookie()] * 301),
    ]
    for source, cookies in refusals:
        status, refused = await submit(world, una, source, cookies=cookies)
        assert status == 422 and refused["detail"]["code"] == "invalid_request", (source, cookies, refused)
        assert SECRET not in json.dumps(refused)


@pytest.mark.asyncio
async def test_the_sign_in_is_offered_only_while_media_downloads_takes_part(world):
    una = world.tokens["una"]
    owner = await Person.sign_in(world, "owner")
    try:
        assert "cookies" in (await extension(world, "GET", "/api/browser/v1/session", una))[1]["capabilities"]
        status, body = await owner.call("PATCH", "/api/integrations/media/configuration", json={"enabled": False})
        assert status == 200, body
        try:
            capabilities = (await extension(world, "GET", "/api/browser/v1/session", una))[1]["capabilities"]
            assert "cookies" not in capabilities
        finally:
            assert (await owner.call("PATCH", "/api/integrations/media/configuration", json={"enabled": True}))[0] == 200
    finally:
        await owner.close()
