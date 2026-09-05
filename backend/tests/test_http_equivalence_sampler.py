"""Deterministic HTTP sampler qualification for WS4 P1."""
from __future__ import annotations

import socket
from urllib.parse import urlsplit

from aiohttp import web
import pytest
import pytest_asyncio

import services.network_safety as safety
from transfers.models import FingerprintKind


PAYLOAD = bytes((index % 251 for index in range(24 * 1024)))


@pytest_asyncio.fixture
async def sampler_server(monkeypatch):
    calls = []

    async def handler(request):
        calls.append((request.path, request.headers.get("Range")))
        path = request.path
        if path == "/redirect":
            raise web.HTTPFound("/range")
        if path == "/unsafe":
            raise web.HTTPFound("http://127.0.0.1:9/private")

        requested = request.headers.get("Range", "")
        if path == "/ignored":
            return web.Response(body=PAYLOAD)
        if path == "/mismatch-size":
            return web.Response(body=PAYLOAD + b"x")
        if path == "/first-only" and requested and not requested.startswith("bytes=0-"):
            return web.Response(body=PAYLOAD)
        if path == "/malformed":
            body = PAYLOAD[:4096]
            return web.Response(status=206, body=body, headers={"Content-Range": "bytes nonsense"})

        if not requested.startswith("bytes="):
            return web.Response(body=PAYLOAD)
        start_text, end_text = requested[6:].split("-", 1)
        start = int(start_text)
        end = min(int(end_text), len(PAYLOAD) - 1)
        body = PAYLOAD[start:end + 1]
        if path == "/variant":
            content_range = f" BYTES  {start} - {end} / {len(PAYLOAD)} "
        else:
            content_range = f"bytes {start}-{end}/{len(PAYLOAD)}"
        return web.Response(status=206, body=body, headers={"Content-Range": content_range})

    app = web.Application()
    app.router.add_route("GET", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    async def allow_fixture(uri):
        parsed = urlsplit(uri)
        if parsed.hostname != "fixture.example" or parsed.port != port:
            raise safety.UnsafeDestinationError("fixture escape")
        return uri

    async def local_resolve(self, host, port=0, family=socket.AF_UNSPEC):
        return [{"hostname": host, "host": "127.0.0.1", "port": port,
                 "family": socket.AF_INET, "proto": socket.IPPROTO_TCP,
                 "flags": socket.AI_NUMERICHOST}]

    monkeypatch.setattr(safety, "validate_resolved_public_destination", allow_fixture)
    monkeypatch.setattr(safety.PublicDestinationResolver, "resolve", local_resolve)
    try:
        yield f"http://fixture.example:{port}", calls
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_proper_ranges_produce_full_sample(sampler_server):
    base, calls = sampler_server
    total, signature, kind, reason, prefix = await safety.sampled_public_artifact_fingerprint(
        base + "/range", sample_bytes=4096, expected_bytes=len(PAYLOAD))
    assert total == len(PAYLOAD)
    assert signature and prefix
    assert kind == FingerprintKind.FULL_CONTENT_SAMPLE
    assert reason == ""
    assert calls == [("/range", "bytes=0-4095"), ("/range", f"bytes={len(PAYLOAD)-4096}-{len(PAYLOAD)-1}")]


@pytest.mark.asyncio
async def test_range_ignored_returns_bounded_prefix_without_full_consumption(sampler_server, monkeypatch):
    base, _ = sampler_server
    reads = []
    original = safety._read_exactly

    async def observed_read(response, count):
        reads.append(count)
        return await original(response, count)

    monkeypatch.setattr(safety, "_read_exactly", observed_read)
    total, signature, kind, reason, prefix = await safety.sampled_public_artifact_fingerprint(
        base + "/ignored", sample_bytes=4096, expected_bytes=len(PAYLOAD))
    assert total == len(PAYLOAD)
    assert signature == prefix and signature
    assert kind == FingerprintKind.PREFIX_CONTENT_SAMPLE
    assert reason == "range_ignored"
    assert reads == [4096]


@pytest.mark.asyncio
async def test_range_ignored_size_disagreement_yields_no_evidence(sampler_server):
    base, _ = sampler_server
    total, signature, kind, reason, prefix = await safety.sampled_public_artifact_fingerprint(
        base + "/mismatch-size", sample_bytes=4096, expected_bytes=len(PAYLOAD))
    assert (total, signature, prefix) == (0, "", "")
    assert kind == FingerprintKind.UNAVAILABLE
    assert reason == "size_disagreement"


@pytest.mark.asyncio
async def test_safe_redirect_is_followed_with_per_hop_validation(sampler_server):
    base, calls = sampler_server
    result = await safety.sampled_public_artifact_fingerprint(
        base + "/redirect", sample_bytes=4096, expected_bytes=len(PAYLOAD))
    assert result[2] == FingerprintKind.FULL_CONTENT_SAMPLE
    assert result[3] == "redirect"
    assert calls[0][0] == "/redirect"
    assert any(path == "/range" for path, _ in calls)


@pytest.mark.asyncio
async def test_unsafe_redirect_is_blocked_before_private_contact(sampler_server):
    base, calls = sampler_server
    result = await safety.sampled_public_artifact_fingerprint(base + "/unsafe", sample_bytes=4096)
    assert result[2] == FingerprintKind.UNAVAILABLE
    assert result[3] == "destination_rejected"
    assert calls == [("/unsafe", "bytes=0-4095")]


@pytest.mark.asyncio
async def test_first_range_success_last_range_failure_never_becomes_full(sampler_server):
    base, _ = sampler_server
    result = await safety.sampled_public_artifact_fingerprint(
        base + "/first-only", sample_bytes=4096, expected_bytes=len(PAYLOAD))
    assert result[2] == FingerprintKind.PREFIX_CONTENT_SAMPLE
    assert result[3] == "range_ignored"


@pytest.mark.asyncio
async def test_semantic_content_range_accepts_valid_format_variation(sampler_server):
    base, _ = sampler_server
    result = await safety.sampled_public_artifact_fingerprint(
        base + "/variant", sample_bytes=4096, expected_bytes=len(PAYLOAD))
    assert result[2] == FingerprintKind.FULL_CONTENT_SAMPLE


@pytest.mark.asyncio
async def test_malformed_content_range_is_rejected(sampler_server):
    base, _ = sampler_server
    result = await safety.sampled_public_artifact_fingerprint(
        base + "/malformed", sample_bytes=4096, expected_bytes=len(PAYLOAD))
    assert result[2] == FingerprintKind.UNAVAILABLE
    assert result[3] == "invalid_content_range"


@pytest.mark.parametrize("uri", [
    "http://127.0.0.1/file",
    "http://10.0.0.1/file",
    "http://169.254.1.1/file",
    "http://localhost/file",
])
def test_private_local_and_link_local_redirect_destinations_are_rejected(uri):
    with pytest.raises(safety.UnsafeDestinationError):
        safety.validate_provider_download_url(uri, context="redirect target")
