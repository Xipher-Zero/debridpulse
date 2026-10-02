"""NZB link ingress normalization.

An NZB link is how a posting reaches DebridPulse, never a request kind: the
explicit NZB-link submission fetches it through the one guarded HTTP(S) owner,
streams it straight into the one staged-input owner, validates it with the
existing NZB reader, and admits the SAME canonical ``nzb`` request an uploaded
NZB becomes. Every server here is a local fixture.
"""
from __future__ import annotations

import asyncio
import json
import socket
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from aiohttp import web
from fastapi import HTTPException

import db.database as database
import services.network_safety as safety
from application.service import ApplicationService
from providers.usenet.provider import UsenetProvider
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, Stage, TransferError
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.recovery_repository import TransferRepository
from transfers.staged_input import StagedInputStore, StagedPayload

pytestmark = pytest.mark.asyncio


def nzb(files: int = 1) -> bytes:
    entries = b"".join(
        b'<file poster="p@e.net" date="1700000000" subject="show [%d/%d] - &quot;show.part%d.rar&quot; yEnc (1/1)">'
        % (index, files, index)
        + b"<groups><group>alt.binaries.test</group></groups>"
        b'<segments><segment bytes="1024" number="1">seg%d@e.net</segment></segments></file>' % index
        for index in range(1, files + 1))
    return (b'<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">'
            b'<head><meta type="name">Show S01</meta></head>' + entries + b"</nzb>")


POSTING = nzb()
LARGE = nzb(12_000)  # several MiB, streamed in many chunks


class CountingStore(StagedInputStore):
    """The real store; it only records how many chunks reached its writer."""

    def __init__(self, root, **kwargs):
        super().__init__(root, **kwargs)
        self.chunks = 0

    async def stage(self, chunks):
        async def counted():
            async for chunk in chunks:
                self.chunks += 1
                yield chunk
        return await super().stage(counted())


def leftovers(store) -> list[str]:
    return sorted(entry.name for entry in store.root.iterdir()) if store.root.exists() else []


@pytest_asyncio.fixture
async def server(monkeypatch):
    seen = []
    streaming = asyncio.Event()

    async def handler(request):
        seen.append(request)
        path = request.path
        if path == "/posting.nzb":
            return web.Response(body=POSTING, content_type="application/octet-stream")
        if path == "/large.nzb":
            response = web.StreamResponse()
            await response.prepare(request)
            for start in range(0, len(LARGE), 64 * 1024):
                await response.write(LARGE[start:start + 64 * 1024])
            return response
        if path == "/page":
            return web.Response(text="<html><body>not a posting</body></html>", content_type="text/html")
        if path == "/missing":
            return web.Response(status=404)
        if path == "/moved":
            raise web.HTTPFound("/posting.nzb")
        if path == "/to-private":
            raise web.HTTPFound("http://10.0.0.5/posting.nzb")
        if path == "/declared-huge":
            response = web.StreamResponse(headers={"Content-Length": "999999"})
            await response.prepare(request)
            await response.write(b"<nzb>")
            return response
        if path == "/endless":
            response = web.StreamResponse()
            await response.prepare(request)
            for _ in range(64):
                await response.write(b"x" * 4096)
            return response
        if path == "/interrupted":
            response = web.StreamResponse(headers={"Content-Length": str(len(LARGE))})
            await response.prepare(request)
            await response.write(LARGE[:8192])
            request.transport.close()
            return response
        if path == "/slow":
            response = web.StreamResponse()
            await response.prepare(request)
            await response.write(POSTING[:64])
            streaming.set()
            await asyncio.sleep(6)
            return response
        if path == "/secret.nzb":
            if request.query.get("token") != "s3cr3t":
                return web.Response(status=403)
            return web.Response(body=POSTING)
        return web.Response(status=500)

    app = web.Application()
    app.router.add_route("GET", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    # Only the fixture host is authorized; every other destination -- each
    # redirect target included -- still meets the real policy.
    real = safety.validate_resolved_public_destination

    async def allow_fixture(uri, **kwargs):
        parsed = urlsplit(uri)
        if parsed.hostname == "fixture.example" and parsed.port == port:
            return uri
        return await real(uri, **kwargs)

    async def local_resolve(self, host, port=0, family=socket.AF_UNSPEC):
        return [{"hostname": host, "host": "127.0.0.1", "port": port, "family": socket.AF_INET,
                 "proto": socket.IPPROTO_TCP, "flags": socket.AI_NUMERICHOST}]

    monkeypatch.setattr(safety, "validate_resolved_public_destination", allow_fixture)
    monkeypatch.setattr(safety.PublicDestinationResolver, "resolve", local_resolve)
    try:
        yield f"http://fixture.example:{port}", seen, streaming
    finally:
        await runner.cleanup()


async def build(tmp_path, monkeypatch, *, max_bytes=None, policy=None):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    store = CountingStore(str(tmp_path / "staged"), **({"max_bytes": max_bytes} if max_bytes else {}))
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = UsenetProvider(staged_input=store)
    registry.register_provider(provider)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "downloads"),
                            policy=policy or TransferPolicy(), clock=lambda: 1000.0)
    await engine.initialize()
    return ApplicationService(engine, staged_input=store), store, repository, provider


async def roots(repository):
    transfers = await repository.active()
    return [record for transfer in transfers for record in await repository.requests(transfer.id)
            if record.parent_id is None]


async def refused(coroutine) -> TransferError:
    with pytest.raises(TransferError) as caught:
        await coroutine
    return caught.value


# -- happy path and equivalence -------------------------------------------------------

async def test_an_nzb_link_becomes_the_canonical_nzb_request(tmp_path, monkeypatch, server):
    base, seen, _ = server
    service, store, repository, _provider = await build(tmp_path, monkeypatch)

    result = await service.submit_nzb_link(f"{base}/posting.nzb")

    assert result["id"]
    [record] = await roots(repository)
    assert record.request.kind == "nzb"
    assert isinstance(record.request.payload, StagedPayload)
    assert record.request.name == "posting.nzb"
    # Named exactly as the existing NZB reader names the posting.
    from providers.usenet.nzb import parse
    assert (await repository.get(result["id"])).name == parse(POSTING, fallback_name="posting.nzb").name
    assert len(seen) == 1  # one fetch: never once to look and again to stage
    assert leftovers(store) == [f"{record.request.payload.id}.input"]


async def test_link_and_upload_converge_on_the_same_provider_facing_request(tmp_path, monkeypatch, server):
    base, _seen, _ = server
    service, _store, repository, provider = await build(tmp_path, monkeypatch)

    async def uploaded_chunks():  # exactly what the /usenet/add-file route hands over
        for start in range(0, len(POSTING), 256):
            yield POSTING[start:start + 256]

    await service.submit_nzb_link(f"{base}/posting.nzb")
    await service.submit_nzb(uploaded_chunks(), "posting.nzb")

    linked, uploaded = sorted(await roots(repository), key=lambda item: item.transfer_id)
    assert (linked.request.kind, linked.request.name) == (uploaded.request.kind, uploaded.request.name)
    assert linked.request.preferred_provider == uploaded.request.preferred_provider is None
    assert linked.request.fingerprint == uploaded.request.fingerprint
    assert isinstance(linked.request.payload, StagedPayload) and isinstance(uploaded.request.payload, StagedPayload)
    assert (linked.request.payload.sha256, linked.request.payload.byte_length) == (
        uploaded.request.payload.sha256, uploaded.request.payload.byte_length)
    first = (await provider.resolve(linked.request)).candidates[0]
    second = (await provider.resolve(uploaded.request)).candidates[0]
    for field in ("name", "expected_bytes", "materialization", "source_identity", "endpoints"):
        assert getattr(first, field) == getattr(second, field), field
    assert first.context["nzb_declared_bytes"] == second.context["nzb_declared_bytes"]


async def test_a_large_posting_streams_chunk_by_chunk_into_staged_input(tmp_path, monkeypatch, server):
    base, _seen, _ = server
    service, store, repository, _provider = await build(tmp_path, monkeypatch)

    await service.submit_nzb_link(f"{base}/large.nzb")

    [record] = await roots(repository)
    assert record.request.payload.byte_length == len(LARGE) > 2 * 1024 * 1024
    assert store.chunks > 2  # written as it arrived, never assembled whole


async def test_a_redirect_is_followed_through_the_guarded_owner(tmp_path, monkeypatch, server):
    base, seen, _ = server
    service, _store, repository, _provider = await build(tmp_path, monkeypatch)

    await service.submit_nzb_link(f"{base}/moved")

    [record] = await roots(repository)
    assert record.request.kind == "nzb"
    assert [request.path for request in seen] == ["/moved", "/posting.nzb"]


# -- refusals: nothing admitted, nothing staged ------------------------------------------

async def assert_refused_cleanly(service, store, repository, url, category, status, *, domain=None):
    failure = await refused(service.submit_nzb_link(url))
    assert failure.error.category == category
    assert failure.error.stage == Stage.SUBMISSION
    assert failure.error.integration_id == ""
    if domain is not None:
        assert failure.error.domain == domain
    assert failure.status_code == status
    assert await roots(repository) == []
    assert leftovers(store) == []
    return failure


async def test_content_that_is_not_a_posting_is_rejected_by_the_nzb_reader(tmp_path, monkeypatch, server):
    base, _seen, _ = server
    service, store, repository, _provider = await build(tmp_path, monkeypatch)
    await assert_refused_cleanly(service, store, repository, f"{base}/page", Category.INVALID_REQUEST, 400)


async def test_a_non_success_answer_is_a_normalized_source_failure(tmp_path, monkeypatch, server):
    base, _seen, _ = server
    service, store, repository, _provider = await build(tmp_path, monkeypatch)
    await assert_refused_cleanly(service, store, repository, f"{base}/missing", Category.SOURCE_NOT_FOUND, 502,
                                 domain=Domain.RESOLUTION)


async def test_a_loopback_link_is_refused_before_any_connection(tmp_path, monkeypatch):
    service, store, repository, _provider = await build(tmp_path, monkeypatch)
    await assert_refused_cleanly(service, store, repository, "http://127.0.0.1:9/posting.nzb",
                                 Category.DESTINATION_BLOCKED, 400, domain=Domain.SECURITY)


async def test_a_public_link_redirecting_to_a_private_address_is_refused(tmp_path, monkeypatch, server):
    base, seen, _ = server
    service, store, repository, _provider = await build(tmp_path, monkeypatch)
    await assert_refused_cleanly(service, store, repository, f"{base}/to-private",
                                 Category.DESTINATION_BLOCKED, 400, domain=Domain.SECURITY)
    assert [request.path for request in seen] == ["/to-private"]


async def test_only_http_links_are_fetched(tmp_path, monkeypatch):
    service, store, repository, _provider = await build(tmp_path, monkeypatch)
    await assert_refused_cleanly(service, store, repository, "ftp://indexer.example/posting.nzb",
                                 Category.INVALID_REQUEST, 400)
    await assert_refused_cleanly(service, store, repository, "not a link", Category.INVALID_REQUEST, 400)


async def test_a_declared_oversize_body_is_refused_before_it_is_read(tmp_path, monkeypatch, server):
    base, _seen, _ = server
    service, store, repository, _provider = await build(tmp_path, monkeypatch, max_bytes=4096)
    await assert_refused_cleanly(service, store, repository, f"{base}/declared-huge", Category.INVALID_REQUEST, 413)
    assert store.chunks == 0


async def test_an_undeclared_oversize_body_is_stopped_by_the_staged_input_ceiling(tmp_path, monkeypatch, server):
    base, _seen, _ = server
    service, store, repository, _provider = await build(tmp_path, monkeypatch, max_bytes=16 * 1024)
    await assert_refused_cleanly(service, store, repository, f"{base}/endless", Category.INVALID_REQUEST, 400)


async def test_an_interrupted_body_leaves_nothing_staged(tmp_path, monkeypatch, server):
    base, _seen, _ = server
    service, store, repository, _provider = await build(tmp_path, monkeypatch)
    await assert_refused_cleanly(service, store, repository, f"{base}/interrupted", Category.CONNECTION_FAILED, 502,
                                 domain=Domain.NETWORK)


async def test_a_stalled_body_times_out_through_the_guarded_reader(server):
    from services.artifact_sampling import HttpReadRefused, http_body
    base, _seen, _ = server
    with pytest.raises(HttpReadRefused) as caught:
        async for _chunk in http_body(f"{base}/slow", max_bytes=1 << 20, timeout_seconds=5):
            pass
    assert caught.value.reason == "timeout"


async def test_cancellation_stops_the_fetch_and_leaves_nothing_behind(tmp_path, monkeypatch, server):
    base, _seen, streaming = server
    service, store, repository, _provider = await build(tmp_path, monkeypatch)

    task = asyncio.create_task(service.submit_nzb_link(f"{base}/slow"))
    await asyncio.wait_for(streaming.wait(), 5)
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert leftovers(store) == []
    assert await roots(repository) == []
    assert not [other for other in asyncio.all_tasks()
                if other is not asyncio.current_task() and not other.done() and "submit_nzb_link" in repr(other)]


# -- secrets ----------------------------------------------------------------------------

async def test_a_capability_bearing_link_is_fetched_whole_and_never_retained(tmp_path, monkeypatch, server):
    base, seen, _ = server
    service, _store, repository, _provider = await build(tmp_path, monkeypatch)

    result = await service.submit_nzb_link(f"{base}/secret.nzb?token=s3cr3t")

    assert seen[-1].query["token"] == "s3cr3t"  # the fetch keeps what the server needs
    [record] = await roots(repository)
    durable = json.dumps(await repository.presentation(result["id"], details=True), default=str)
    assert "s3cr3t" not in durable and "s3cr3t" not in repr(record.request)
    failure = await refused(service.submit_nzb_link(f"{base}/missing?token=s3cr3t"))
    assert "s3cr3t" not in json.dumps(failure.error.as_dict(diagnostics=True), default=str)


async def test_a_bare_query_capability_never_becomes_a_name(tmp_path, monkeypatch, server):
    base, _seen, _ = server
    service, _store, repository, _provider = await build(tmp_path, monkeypatch)

    result = await service.submit_nzb_link(f"{base}/posting.nzb?K3YT0K3N")

    [record] = await roots(repository)
    assert record.request.name == "posting.nzb"
    durable = json.dumps(await repository.presentation(result["id"], details=True), default=str)
    assert "K3YT0K3N" not in durable and "K3YT0K3N" not in repr(record.request)


async def test_a_link_carrying_a_username_or_password_is_refused_before_any_fetch(tmp_path, monkeypatch, server):
    base, seen, _ = server
    service, store, repository, _provider = await build(tmp_path, monkeypatch)
    parsed = urlsplit(base)

    failure = await assert_refused_cleanly(service, store, repository,
                                           f"http://reader:hunter2@{parsed.netloc}/secret.nzb",
                                           Category.INVALID_REQUEST, 400)

    assert seen == []
    assert "hunter2" not in json.dumps(failure.error.as_dict(diagnostics=True), default=str)


# -- intent and the API action -------------------------------------------------------------

async def test_an_ordinary_link_stays_an_ordinary_link(tmp_path, monkeypatch, server):
    base, seen, _ = server
    service, _store, repository, _provider = await build(tmp_path, monkeypatch)

    await service.submit_links([f"{base}/posting.nzb"])

    [record] = await roots(repository)
    assert record.request.kind == "http"
    assert record.request.payload == f"{base}/posting.nzb"
    assert seen == []  # nothing was fetched or sniffed at submission


async def test_the_api_action_requires_a_link_and_delegates_to_the_ingress(tmp_path, monkeypatch, server):
    from api.routes import add_usenet_link
    base, _seen, _ = server
    service, _store, repository, _provider = await build(tmp_path, monkeypatch)

    with pytest.raises(HTTPException) as missing:
        await add_usenet_link({}, application=service)
    assert missing.value.status_code == 400

    result = await add_usenet_link({"url": f"{base}/posting.nzb"}, application=service)
    assert result["id"] and [record.request.kind for record in await roots(repository)] == ["nzb"]


async def test_a_private_network_link_asks_for_this_submission_s_confirmation(tmp_path, monkeypatch):
    from api.routes import add_usenet_link
    service, store, repository, _provider = await build(
        tmp_path, monkeypatch, policy=TransferPolicy(private_lan_connections=True))

    with pytest.raises(HTTPException) as asked:
        await add_usenet_link({"url": "http://10.0.0.5/posting.nzb"}, application=service)

    assert asked.value.status_code == 409
    assert asked.value.detail["confirmation"] == "local_network"
    assert await roots(repository) == [] and leftovers(store) == []


def test_no_request_kind_and_no_provider_or_executor_learned_about_links():
    from pathlib import Path
    backend = Path(__file__).resolve().parents[1]
    for path in [*(backend / "providers").rglob("*.py"), *(backend / "executors").rglob("*.py"),
                 backend / "transfers" / "registry.py", backend / "transfers" / "applicability.py"]:
        text = path.read_text().casefold()
        assert "nzb_url" not in text and "http_body" not in text and "submit_nzb_link" not in text, path
