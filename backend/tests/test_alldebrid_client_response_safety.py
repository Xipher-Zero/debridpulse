"""1.0.13 SEC-006: the AllDebrid native client trusts only AllDebrid's own answer.

A redirect is never followed -- following one replays the request, its magnet
or torrent body included, to wherever ``Location`` points and takes that
origin's answer as AllDebrid's -- and a response is decoded only up to the
bound every debrid client keeps, compressed or not. Both are ordinary
provider protocol failures through the existing translation, and no failure
echoes the provider's body. Every exchange here is with disposable loopback
servers; nothing reaches the internet.
"""
from __future__ import annotations

import gzip

import pytest
import pytest_asyncio
from aiohttp import web

import providers.alldebrid.client as client
from providers.alldebrid.client import AllDebridAPIError, AllDebridService
from providers.alldebrid.translation import translate_error
from transfers.errors import Category

pytestmark = pytest.mark.asyncio

KEY = "SENTINEL-API-KEY"
MAGNET = "magnet:?xt=urn:btih:" + "ab" * 20 + "&dn=private-payload"
SUCCESS = {"status": "success", "data": {"magnets": [{"id": 7, "ready": True}],
                                         "files": [{"id": 8}], "user": {"username": "u"}}}


async def _serve(handler):
    runner = web.AppRunner(web.Application())
    runner.app.router.add_route("*", "/{tail:.*}", handler)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


@pytest_asyncio.fixture
async def origins(monkeypatch):
    """``api``: the stand-in for AllDebrid; ``third``: any other origin a
    redirect could name. Each records exactly what reached it."""
    seen = {"api": [], "third": []}
    behaviour = {}

    async def third(request):
        seen["third"].append((request.method, request.path, request.headers.get("Authorization"),
                              await request.read()))
        return web.json_response({"status": "success", "data": {"magnets": [{"id": 666}], "files": [{"id": 666}]}})

    third_runner, third_url = await _serve(third)

    async def api(request):
        seen["api"].append((request.method, request.path, request.headers.get("Authorization")))
        if request.path.startswith("/landing"):
            return web.json_response(SUCCESS)
        return await behaviour["respond"](request, third_url)

    api_runner, api_url = await _serve(api)
    monkeypatch.setattr(client, "API_V4", f"{api_url}/v4")
    monkeypatch.setattr(client, "API_V41", f"{api_url}/v4.1")
    try:
        yield seen, behaviour, api_url
    finally:
        await api_runner.cleanup()
        await third_runner.cleanup()


def _protocol_failure(exc: Exception) -> None:
    error = translate_error(exc, secrets=(KEY,))
    assert error.category == Category.PROVIDER_PROTOCOL_VIOLATION
    assert "private-payload" not in str(exc) and KEY not in str(exc)


async def test_an_ordinary_answer_is_still_accepted(origins):
    seen, behaviour, _api = origins

    async def ok(_request, _third):
        return web.json_response(SUCCESS)

    behaviour["respond"] = ok
    service = AllDebridService(KEY)
    assert (await service.upload_magnet(MAGNET))["id"] == 7
    assert (await service.upload_torrent_file(b"d4:infod4:name1:xee", "x.torrent"))["id"] == 8
    assert await service.get_user() == SUCCESS["data"]
    assert [(method, auth) for method, _path, auth in seen["api"]] == [("POST", f"Bearer {KEY}")] * 3


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("same_origin", [False, True])
async def test_a_redirect_is_never_followed_and_never_taken_as_alldebrids_answer(origins, status, same_origin):
    seen, behaviour, api = origins

    async def redirect(request, third):
        target = (api if same_origin else third) + "/landing" + request.path
        return web.Response(status=status, headers={"Location": target})

    behaviour["respond"] = redirect
    service = AllDebridService(KEY)
    for call in (lambda: service.upload_magnet(MAGNET),
                 lambda: service.upload_torrent_file(b"d4:infod4:name12:private-payloadee", "x.torrent"),
                 lambda: service.get_user()):
        with pytest.raises(Exception) as raised:
            await call()
        assert "redirect" in str(raised.value) and str(status) in str(raised.value)
        _protocol_failure(raised.value)
    assert seen["third"] == []  # no request, body or credential ever reached the other origin
    assert [path for _method, path, _auth in seen["api"] if path.startswith("/landing")] == []


async def test_a_response_past_the_bound_is_refused_without_being_held(origins, monkeypatch):
    monkeypatch.setattr(client, "MAX_RESPONSE_BYTES", 64 * 1024, raising=False)
    seen, behaviour, _api = origins
    written = {}

    async def huge(request, _third):
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        await response.prepare(request)
        written["bytes"] = 0
        try:
            await response.write(b'{"status":"success","data":{"pad":"')
            for _ in range(256):  # 16 MiB offered; the client stops reading long before
                await response.write(b"x" * 65536)
                written["bytes"] += 65536
            await response.write(b'"}}')
        except (ConnectionResetError, RuntimeError):
            pass
        return response

    behaviour["respond"] = huge
    with pytest.raises(Exception) as raised:
        await AllDebridService(KEY).upload_magnet(MAGNET)
    assert "oversized" in str(raised.value)
    _protocol_failure(raised.value)


async def test_a_compressed_response_is_bounded_by_its_decoded_size(origins, monkeypatch):
    monkeypatch.setattr(client, "MAX_RESPONSE_BYTES", 64 * 1024, raising=False)
    _seen, behaviour, _api = origins
    bomb = gzip.compress(b'{"status":"success","data":{"pad":"' + b"0" * (8 * 1024 * 1024) + b'"}}')
    assert len(bomb) < 64 * 1024  # small on the wire, eight MiB once decoded

    async def compressed(_request, _third):
        return web.Response(body=bomb, headers={"Content-Type": "application/json", "Content-Encoding": "gzip"})

    behaviour["respond"] = compressed
    with pytest.raises(Exception) as raised:
        await AllDebridService(KEY).upload_magnet(MAGNET)
    assert "oversized" in str(raised.value)
    _protocol_failure(raised.value)


async def test_an_answer_exactly_at_the_bound_is_still_data(origins, monkeypatch):
    monkeypatch.setattr(client, "MAX_RESPONSE_BYTES", 64 * 1024, raising=False)
    _seen, behaviour, _api = origins
    head, tail = b'{"status":"success","data":{"magnets":[{"id":9}],"pad":"', b'"}}'
    body = head + b"y" * (64 * 1024 - len(head) - len(tail)) + tail
    assert len(body) == 64 * 1024

    async def exact(_request, _third):
        return web.Response(body=body, headers={"Content-Type": "application/json"})

    behaviour["respond"] = exact
    assert (await AllDebridService(KEY).upload_magnet(MAGNET))["id"] == 9


@pytest.mark.parametrize("body", [b"<html>SENTINEL-BODY-FRAGMENT private-payload</html>", b"\xff\xfe not utf-8"])
async def test_a_malformed_answer_is_a_protocol_failure_that_echoes_no_body(origins, body):
    _seen, behaviour, _api = origins

    async def malformed(_request, _third):
        return web.Response(body=body, headers={"Content-Type": "text/html"})

    behaviour["respond"] = malformed
    with pytest.raises(Exception) as raised:
        await AllDebridService(KEY).upload_magnet(MAGNET)
    assert "invalid JSON" in str(raised.value)
    assert "SENTINEL-BODY-FRAGMENT" not in str(raised.value)
    _protocol_failure(raised.value)


async def test_server_errors_and_native_refusals_keep_their_classification(origins):
    _seen, behaviour, _api = origins

    async def unavailable(_request, _third):
        return web.Response(status=503, body=b"x" * (32 * 1024 * 1024))

    behaviour["respond"] = unavailable
    with pytest.raises(Exception) as raised:
        await AllDebridService(KEY).get_user()
    assert translate_error(raised.value).category == Category.PROVIDER_UNAVAILABLE

    async def refused(_request, _third):
        return web.json_response({"status": "error", "error": {"code": "AUTH_BAD_APIKEY", "message": "bad key"}})

    behaviour["respond"] = refused
    with pytest.raises(AllDebridAPIError) as native:
        await AllDebridService(KEY).get_user()
    assert native.value.code == "AUTH_BAD_APIKEY"
