"""Exercise the real HTTP sampling runtime with mismatched provider-reported sizes."""
from __future__ import annotations

from dataclasses import replace
import socket
from types import SimpleNamespace
from urllib.parse import urlsplit

from aiohttp import web
import pytest
import pytest_asyncio

import db.database as database
import services.network_safety as safety
from executors.aria2.executor import Aria2Configuration, Aria2Executor
from fake_integrations import ParcelProvider
from transfers.engine import TransferEngine
from transfers.models import Endpoint, ResolutionResult, ResourceState, SourceIdentity, TransferRequest
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


PAYLOAD = bytes((index % 251 for index in range(1024 * 1024)))
ACTUAL_SIZE = len(PAYLOAD)
REPORT_A = ACTUAL_SIZE
REPORT_B = ACTUAL_SIZE + 1024


class HttpNearSizeProvider(ParcelProvider):
    def __init__(self, identity: str, reported_size: int, endpoint: str):
        super().__init__(identity)
        self.reported_size = reported_size
        self.endpoint = endpoint

    def candidate(self, name="payload.bin", *, payload="parcel"):
        return replace(
            super().candidate(name, payload=payload),
            endpoints=(Endpoint("http", self.endpoint),),
            expected_bytes=self.reported_size,
            source_identity=SourceIdentity("runtime-http-fixture", self.descriptor.id),
        )

    async def resolve(self, request):
        self.calls.append(("resolve", request.payload))
        return ResolutionResult(
            ResourceState.AVAILABLE,
            (self.candidate(request.name or "payload.bin", payload=str(request.payload)),),
        )


@pytest_asyncio.fixture
async def http_payload_server(monkeypatch):
    calls: list[tuple[str, str | None]] = []

    async def handler(request):
        requested = request.headers.get("Range")
        calls.append((request.path, requested))
        if not requested or not requested.startswith("bytes="):
            return web.Response(body=PAYLOAD)
        start_text, end_text = requested[6:].split("-", 1)
        start = int(start_text)
        end = min(int(end_text), ACTUAL_SIZE - 1)
        body = PAYLOAD[start:end + 1]
        return web.Response(
            status=206,
            body=body,
            headers={"Content-Range": f"bytes {start}-{end}/{ACTUAL_SIZE}"},
        )

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
        return [{
            "hostname": host,
            "host": "127.0.0.1",
            "port": port,
            "family": socket.AF_INET,
            "proto": socket.IPPROTO_TCP,
            "flags": socket.AI_NUMERICHOST,
        }]

    monkeypatch.setattr(safety, "validate_resolved_public_destination", allow_fixture)
    monkeypatch.setattr(safety.PublicDestinationResolver, "resolve", local_resolve)
    try:
        yield f"http://fixture.example:{port}", calls
    finally:
        await runner.cleanup()


@pytest_asyncio.fixture
async def runtime_engine(tmp_path, http_payload_server, monkeypatch):
    base, calls = http_payload_server
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()

    repository = TransferRepository()
    registry = IntegrationRegistry()
    providers = (
        HttpNearSizeProvider("provider-a", REPORT_A, base + "/provider-a"),
        HttpNearSizeProvider("provider-b", REPORT_B, base + "/provider-b"),
    )

    async def authorize(_handle, _action):
        return True

    executor = Aria2Executor(
        SimpleNamespace(url="http://aria2.invalid/jsonrpc"),
        Aria2Configuration(str(tmp_path / "payloads")),
        authorize,
    )
    for provider in providers:
        registry.register_provider(provider)
    registry.register_executor(executor)

    engine = TransferEngine(
        repository,
        registry,
        download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(
            retry_delay=1,
            adoption_stability_seconds=0,
            max_active_executions=32,
            resolution_concurrency=32,
        ),
        clock=lambda: 1000.0,
    )
    await engine.initialize()
    return SimpleNamespace(
        engine=engine,
        repository=repository,
        providers=providers,
        calls=calls,
    )


async def submit_one(ctx, provider, payload):
    return await ctx.engine.submit(
        (TransferRequest(
            "parcel",
            payload,
            name="runtime-near-size.bin",
            preferred_provider=provider.descriptor.id,
        ),),
        name="runtime-near-size.bin",
        deduplicate=False,
    )


def assert_real_sampler_reached_both_candidates(calls):
    paths = {path for path, _ in calls}
    assert {"/provider-a", "/provider-b"} <= paths
    assert all(requested and requested.startswith("bytes=") for _, requested in calls)
    assert len(calls) >= 4


@pytest.mark.asyncio
async def test_intra_transfer_near_size_reports_reach_real_http_sampler_and_consolidate(runtime_engine):
    ctx = runtime_engine
    requests = tuple(
        TransferRequest(
            "parcel",
            f"submission-{index}",
            name="runtime-near-size.bin",
            preferred_provider=provider.descriptor.id,
        )
        for index, provider in enumerate(ctx.providers, start=1)
    )
    transfer = await ctx.engine.submit(requests, name="runtime near-size cohort", deduplicate=False)

    for _ in range(4):
        await ctx.engine.resolve_pending()

    artifacts = await ctx.repository.artifacts(transfer.id)
    assert len(artifacts) == 1
    assert [candidate.provider_id for candidate in artifacts[0].candidates] == ["provider-a", "provider-b"]
    assert_real_sampler_reached_both_candidates(ctx.calls)


@pytest.mark.asyncio
async def test_cross_transfer_near_size_reports_reach_same_real_http_sampler_and_canonicalize(runtime_engine):
    ctx = runtime_engine
    first = await submit_one(ctx, ctx.providers[0], "submission-a")
    for _ in range(2):
        await ctx.engine.resolve_pending()
    assert len(await ctx.repository.artifacts(first.id)) == 1

    ctx.calls.clear()
    second = await submit_one(ctx, ctx.providers[1], "submission-b")
    for _ in range(3):
        await ctx.engine.resolve_pending()

    canonical = (await ctx.repository.artifacts(first.id))[0]
    assert [candidate.provider_id for candidate in canonical.candidates] == ["provider-a", "provider-b"]
    assert await ctx.repository.artifacts(second.id) == ()
    assert_real_sampler_reached_both_candidates(ctx.calls)
