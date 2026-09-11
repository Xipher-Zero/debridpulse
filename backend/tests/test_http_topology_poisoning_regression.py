"""DP 1.0.12 corrective task, Case 6: three-source topology-poisoning regression.

Reproduces the catastrophic shape observed in production transfer 186 end to
end, through the real HTTP transport sampler (``services.network_safety``)
and the real ``Aria2Executor.fingerprint()`` wiring -- not a fingerprint
double. Source A establishes the canonical artifact. Source B is pairable
with A but its first Range probe is answered with a short, Range-ignoring
200 response (the exact production shape: a small body against a much
larger declared size). Source C is a third, independent source of the same
logical artifact, submitted before B's bounded proof retry has fired.

Required outcome (Section 20 success criterion): B's temporary evidence
ambiguity must never poison the collection into multiple canonical
artifacts. Once B's bounded retry samples the (by-then well-behaved)
capability, everything converges onto ONE canonical artifact carrying all
three candidates.
"""
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
from transfers.models import Endpoint, ResolutionResult, ResourceState, TransferRequest
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


PAYLOAD = bytes((index % 251 for index in range(12000)))
DECLARED_SIZE = len(PAYLOAD)


def _range_response(request):
    requested = request.headers.get("Range", "")
    if not requested.startswith("bytes="):
        return web.Response(body=PAYLOAD)
    start_text, end_text = requested[6:].split("-", 1)
    start = int(start_text)
    end = min(int(end_text), len(PAYLOAD) - 1)
    body = PAYLOAD[start:end + 1]
    return web.Response(status=206, body=body, headers={"Content-Range": f"bytes {start}-{end}/{len(PAYLOAD)}"})


@pytest_asyncio.fixture
async def poisoning_server(monkeypatch):
    calls: list[str] = []
    degraded_state = {"count": 0}

    async def handler(request):
        calls.append(request.path)
        if request.path == "/artifact-b":
            degraded_state["count"] += 1
            if degraded_state["count"] <= 2:
                # The exact observed production shape (Section 4B): a real
                # HTTP 200 with a short body, drastically incompatible with
                # the candidate's declared size, in response to a bounded
                # Range probe the server chose to ignore.
                return web.Response(body=PAYLOAD[:50])
        return _range_response(request)

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
        yield f"http://fixture.example:{port}", calls, degraded_state
    finally:
        await runner.cleanup()


class SingleHttpSourceProvider(ParcelProvider):
    def __init__(self, identity: str, endpoint: str):
        super().__init__(identity)
        self.endpoint = endpoint

    def candidate(self, name="artifact.bin", *, payload="parcel"):
        return replace(
            super().candidate(name, payload=payload),
            endpoints=(Endpoint("http", self.endpoint),),
            expected_bytes=DECLARED_SIZE,
        )

    async def resolve(self, request):
        self.calls.append(("resolve", request.payload))
        return ResolutionResult(
            ResourceState.AVAILABLE,
            (self.candidate(request.name or "artifact.bin", payload=str(request.payload)),),
        )


@pytest_asyncio.fixture
async def topology(tmp_path, poisoning_server, monkeypatch):
    base, calls, degraded_state = poisoning_server
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()

    repository = TransferRepository()
    registry = IntegrationRegistry()
    source_a = SingleHttpSourceProvider("source-a", base + "/artifact-a")
    source_b = SingleHttpSourceProvider("source-b", base + "/artifact-b")
    source_c = SingleHttpSourceProvider("source-c", base + "/artifact-c")
    for provider in (source_a, source_b, source_c):
        registry.register_provider(provider)

    async def authorize(_handle, _action):
        return True

    executor = Aria2Executor(
        SimpleNamespace(url="http://aria2.invalid/jsonrpc"),
        Aria2Configuration(local_root=str(tmp_path / "payloads")),
        authorize,
    )
    registry.register_executor(executor)

    now = [1000.0]
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0,
                              max_active_executions=32, resolution_concurrency=32),
        clock=lambda: now[0],
    )
    await engine.initialize()
    return SimpleNamespace(
        engine=engine, repository=repository, a=source_a, b=source_b, c=source_c,
        calls=calls, degraded_state=degraded_state, now=now,
    )


async def _submit(ctx, provider, payload):
    return await ctx.engine.submit(
        (TransferRequest("parcel", payload, name="artifact.bin", preferred_provider=provider.descriptor.id),),
        name=payload, deduplicate=False,
    )


async def _canonical_ids(transfer_ids):
    placeholders = ",".join("?" * len(transfer_ids))
    async with database.get_db() as db:
        rows = await db.fetchall(
            f"""SELECT torrent_id,id,mirror_group_id,mirror_state FROM download_files
                WHERE torrent_id IN ({placeholders}) AND request_id IS NOT NULL""",
            tuple(transfer_ids),
        )
    result = {}
    for row in rows:
        canonical_id = int(row["mirror_group_id"]) if row["mirror_state"] == "standby" and row["mirror_group_id"] else int(row["id"])
        result[int(row["torrent_id"])] = canonical_id
    return result


@pytest.mark.asyncio
async def test_three_source_arrival_order_converges_on_one_canonical_after_bounded_retry(topology):
    ctx = topology

    source_a = await _submit(ctx, ctx.a, "from-a")
    for _ in range(4):
        await ctx.engine.resolve_pending()
    assert len(await ctx.repository.artifacts(source_a.id)) == 1

    # B's first (and, once fixed, only ambiguous) proof attempt.
    source_b = await _submit(ctx, ctx.b, "from-b")
    for _ in range(4):
        await ctx.engine.resolve_pending()
    assert ctx.degraded_state["count"] >= 1

    # C arrives independently, before B's bounded retry has fired.
    source_c = await _submit(ctx, ctx.c, "from-c")
    for _ in range(4):
        await ctx.engine.resolve_pending()

    # Give B's bounded retry window a chance to fire and converge.
    ctx.now[0] += 1.1
    for _ in range(4):
        await ctx.engine.resolve_pending()

    ids = await _canonical_ids((source_a.id, source_b.id, source_c.id))
    assert len(ids) == 3, "every source must reach a resolved artifact identity"
    distinct_canonicals = set(ids.values())
    assert len(distinct_canonicals) == 1, (
        f"three-source topology poisoning: expected one canonical artifact, got {len(distinct_canonicals)} "
        f"({ids!r}) -- a temporary evidence failure on B must not create a second canonical that blocks C"
    )

    canonical_artifact_id = next(iter(distinct_canonicals))
    canonical_transfer_id = next(tid for tid, cid in ids.items() if cid == canonical_artifact_id)
    canonical = next(item for item in await ctx.repository.artifacts(canonical_transfer_id) if item.id == canonical_artifact_id)
    assert len(canonical.candidates) == 3
