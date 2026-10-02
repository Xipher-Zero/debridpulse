"""Real runtime proof: a source switch is a fresh, ordinarily prepared writer
that keeps DP-valid material (1.0.13).

Real owners end to end: the convergence engine, repository and canonical
owners, the real ``GeneralHttpProvider``/``GeneralFtpProvider``, the real
``Aria2Executor`` driving a real ``aria2c`` with several connections per job,
and the real ``DownloaderEgressGuard`` (only its resolver maps the fixture
hostnames to loopback). Several HTTP hostnames serve the same throttled bytes
-- one only through a redirect -- and an FTP origin serves them too.

Whatever the replacement -- a redirecting or a direct HTTP source, an FTP
source; while running or completed at Resume -- the old aria2 job is retired,
never re-pointed, and the replacement is a NEW job prepared by the executor's
ordinary start (guarded redirect resolution, destination and egress binding)
that imports every DP-valid whole piece: nothing to discard or confirm.
"""
from __future__ import annotations

import asyncio
import hashlib
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

import db.database as database
import executors.aria2.executor as executor_module
import services.network_safety as safety
from executors.aria2.executor import Aria2Configuration, Aria2Executor
from providers.general_ftp.provider import GeneralFtpProvider
from providers.general_http.provider import GeneralHttpProvider
from test_v113_egress_guard_route_scope import FtpOrigin
from test_v113_continuation_runtime import start_daemon
from test_v113_ftp_sftp_convergence_runtime import Runtime
from test_v113_transport_evidence_sampling import guard_for
from transfers import codec
from transfers import material as mat
from transfers.convergence_engine import TransferEngine
from transfers.manual_failover import manual_candidate_failover, preview_candidate_switch
from transfers.models import ContinuationStrategy, TransferRequest, TransferState
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = [pytest.mark.asyncio, pytest.mark.real_runtime]
MIB = 1 << 20
BODY = b"".join(hashlib.sha256(b"handoff" + str(index).encode()).digest() for index in range(24 * MIB // 32))


async def start_origin(*, rate=1 * MIB):
    """A throttled Range-capable HTTP origin recording, per request, the Host
    it was asked for, where a ranged request began, and the bytes it sent.
    ``/moved/<name>`` answers only with a redirect to ``mirror-c.test``."""
    requests = []
    bound = []

    async def handle(reader, writer):
        record = SimpleNamespace(host="", path="", start=0, end=0, ranged=False, sent=0)
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            start, end = 0, len(BODY)
            record.path = raw.decode("iso-8859-1").split(" ", 2)[1]
            for line in raw.decode("iso-8859-1").split("\r\n"):
                name, _, value = line.partition(":")
                if name.casefold() == "host":
                    record.host = value.strip().rsplit(":", 1)[0]
                if name.casefold() == "range":
                    first, _, last = value.split("=", 1)[1].strip().partition("-")
                    start, end = int(first), (int(last) + 1 if last else len(BODY))
                    record.ranged = True
            record.start, record.end = start, end
            requests.append(record)
            if record.path.startswith("/moved/"):
                moved = f"http://mirror-c.test:{bound[0]}/{record.path.removeprefix('/moved/')}"
                writer.write(f"HTTP/1.1 302 Found\r\nLocation: {moved}\r\nContent-Length: 0\r\n"
                             "Connection: close\r\n\r\n".encode())
                await writer.drain()
                return
            partial = (start, end) != (0, len(BODY))
            head = b"HTTP/1.1 206 Partial Content\r\n" if partial else b"HTTP/1.1 200 OK\r\n"
            head += f"Content-Length: {end - start}\r\nAccept-Ranges: bytes\r\n".encode()
            if partial:
                head += f"Content-Range: bytes {start}-{end - 1}/{len(BODY)}\r\n".encode()
            writer.write(head + b"Connection: close\r\n\r\n")
            chunk = 64 * 1024
            for offset in range(start, end, chunk):
                block = BODY[offset:min(end, offset + chunk)]
                writer.write(block)
                await writer.drain()
                record.sent += len(block)
                await asyncio.sleep(chunk / rate)
        except (ConnectionError, asyncio.IncompleteReadError, TimeoutError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    bound.append(int(server.sockets[0].getsockname()[1]))
    return server, bound[0], requests


async def runtime(tmp_path, monkeypatch, *origins) -> Runtime:
    async def validated(uri, **_kwargs):
        return uri

    async def local_resolve(self, host, port=0, family=0):
        return [{"hostname": host, "host": "127.0.0.1", "port": port, "family": socket.AF_INET,
                 "proto": 0, "flags": socket.AI_NUMERICHOST}]

    monkeypatch.setattr(safety, "validate_resolved_public_destination", validated)
    monkeypatch.setattr(safety.PublicDestinationResolver, "resolve", local_resolve)
    monkeypatch.setattr(executor_module, "validate_resolved_public_destination", validated)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "handoff-runtime.sqlite3")
    await database.init_db()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    engine = TransferEngine(repository, registry, download_root=str(downloads), policy=TransferPolicy(
        retry_delay=0, adoption_stability_seconds=0, max_active_executions=4, material_checkpoint_interval=1.0))
    await engine.initialize()
    guard = guard_for()
    # Production-like daemon: 64 MiB write cache and falloc, the configuration
    # in which a paused-job URI change aborts aria2 1.37.0.
    proc, service = await start_daemon(downloads)
    executor = Aria2Executor(service, Aria2Configuration(str(downloads), split=4, minimum_split_size="1M",
                                                         connections_per_server=4, confirmation_delay=0.05),
                             repository.authorize_execution, egress=guard)
    registry.register_provider(GeneralHttpProvider())
    registry.register_provider(GeneralFtpProvider())
    registry.register_executor(executor)
    return Runtime(repository=repository, registry=registry, engine=engine, executor=executor, guard=guard,
                   proc=proc, service=service, downloads=downloads, origins=list(origins))


async def sparse_writer(rt, url):
    """A running writer of ``url`` with sparse committed material."""
    transfer = await rt.engine.submit((TransferRequest("http", url),), deduplicate=False)

    async def sparse():
        artifacts = await rt.repository.artifacts(transfer.id)
        state = await rt.repository.material_state(artifacts[0].id) if artifacts else None
        return (artifacts[0], state) if state and mat.total(state.valid) - state.safe_prefix >= 2 * MIB else None

    artifact, _ = await rt.until(sparse, label="sparse committed material")
    return transfer, artifact


async def attach(rt, transfer, kind, url):
    """A later submission of an equivalent source consolidates, through the
    real bounded sampler, into the artifact as one more candidate."""
    await rt.engine.submit((TransferRequest(kind, url),), deduplicate=False)

    async def bound():
        artifact = (await rt.repository.artifacts(transfer.id))[0]
        found = [item for item in artifact.candidates if any(point.address == url for point in item.endpoints)]
        return (artifact, found[0]) if found else None

    return await rt.until(bound, label=f"{url} bound as an equivalent candidate")


async def audit(transfer_id, event):
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT detail FROM application_events WHERE kind='material_audit' AND transfer_id=?"
                                 " ORDER BY id", (transfer_id,))
    return [detail for detail in (codec.load(row["detail"]) for row in rows) if detail["event"] == event]


async def completed(rt, transfer):
    async def done():
        current = await rt.repository.get(transfer.id)
        return current if current.state == TransferState.COMPLETED else None
    return await rt.until(done, label="completion")


async def fresh_writer(rt, transfer, first, source):
    """The completed artifact's writers: the retired one and the one fresh
    replacement for ``source``, whose plan imported DP material and discarded
    none. The retired aria2 job is gone, never re-pointed."""
    attempts = await rt.repository.executions(transfer.id)
    old, new = attempts[0], attempts[-1]
    assert len(attempts) == 2 and old.handle == first.execution and new.candidate.id == source.id
    assert new.handle.native["gid"] != first.execution.native["gid"]
    with pytest.raises(Exception):
        await rt.service.tell_status(first.execution.native["gid"])
    plan = await rt.repository.execution_continuation(new.handle.attempt_id)
    assert plan.strategy == ContinuationStrategy.SPARSE_IMPORT and plan.discarded == ()
    assert not await audit(transfer.id, "rollback") and not await audit(transfer.id, "native_handoff")
    return plan


def refetched(requests, ranges) -> bool:
    """A job's ranged read starting inside DP-valid material. The one-byte
    ``bytes=0-0`` reads are the guarded redirect owner's own probes."""
    return any(start <= item.start < end for item in requests if item.ranged and item.end - item.start > 1
               for start, end in ranges)


async def test_real_running_switch_to_a_redirecting_source_is_a_fresh_guarded_start(tmp_path, monkeypatch):
    server, port, requests = await start_origin()
    rt = await runtime(tmp_path, monkeypatch)
    try:
        url_b = f"http://mirror-b.test:{port}/moved/movie.bin"  # answered only at mirror-c.test
        transfer, first = await sparse_writer(rt, f"http://mirror-a.test:{port}/movie.bin")
        artifact, source_b = await attach(rt, transfer, "http", url_b)
        preview = await preview_candidate_switch(rt.engine, transfer.id, artifact.id, str(source_b.id))
        assert preview["discarded_bytes"] == 0
        before = len(requests)
        await manual_candidate_failover(rt.engine, transfer.id, artifact.id, str(source_b.id))  # no 409
        await completed(rt, transfer)

        plan = await fresh_writer(rt, transfer, first, source_b)
        assert plan.retained_bytes >= preview["retained_bytes"]
        after = requests[before:]
        # aria2 never follows a redirect (max-http-redirection=0): every byte
        # from mirror-c.test reached a job the guarded owner pointed there.
        assert sum(item.sent for item in after if item.host == "mirror-c.test") > 0
        assert all(item.sent == 0 for item in after if item.host == "mirror-b.test")
        assert not refetched([item for item in after if item.host == "mirror-c.test"], plan.retained)
        assert Path(artifact.target).read_bytes() == BODY
    finally:
        await rt.close()
        server.close()


async def test_real_paused_switch_to_a_direct_source_completes_at_resume_as_a_fresh_job(tmp_path, monkeypatch):
    server, port, requests = await start_origin()
    rt = await runtime(tmp_path, monkeypatch)
    try:
        url_a, url_b = f"http://mirror-a.test:{port}/movie.bin", f"http://mirror-b.test:{port}/movie.bin"
        transfer, first = await sparse_writer(rt, url_a)
        artifact, source_b = await attach(rt, transfer, "http", url_b)
        await rt.engine.pause(transfer.id)
        gid = first.execution.native["gid"]
        at_pause = await rt.repository.material_state(artifact.id)
        assert mat.total(at_pause.valid) > at_pause.safe_prefix

        preview = await preview_candidate_switch(rt.engine, transfer.id, artifact.id, str(source_b.id))
        assert preview["discarded_bytes"] == 0 and preview["retained_bytes"] == mat.total(at_pause.valid)
        await manual_candidate_failover(rt.engine, transfer.id, artifact.id, str(source_b.id))  # no 409

        # Paused: only the desired source changed. The parked aria2 job is
        # untouched -- still paused, still on A -- and no new writer exists.
        switched = (await rt.repository.artifacts(transfer.id))[0]
        assert switched.execution == first.execution and switched.candidates[switched.selected].id == source_b.id
        native = await rt.service.tell_status(gid)
        assert native.status == "paused" and {uri["uri"] for uri in native.files[0]["uris"]} == {url_a}
        assert (await rt.repository.material_state(artifact.id)) == at_pause
        before_resume = len(requests)

        # Resume completes the switch: the parked job is retired and a fresh
        # job for B acquires only what DP does not already hold.
        await rt.engine.resume(transfer.id)
        await completed(rt, transfer)
        plan = await fresh_writer(rt, transfer, first, source_b)
        assert plan.retained == at_pause.valid and plan.material_generation == at_pause.material_generation
        resumed = requests[before_resume:]
        assert sum(item.sent for item in resumed if item.host == "mirror-b.test") > 0
        assert not [item for item in resumed if item.host == "mirror-a.test"]
        assert not refetched(resumed, at_pause.valid)
        assert sum(item.sent for item in resumed) <= len(BODY) - mat.total(at_pause.valid) + 4 * MIB
        assert Path(switched.target).read_bytes() == BODY
    finally:
        await rt.close()
        server.close()


async def test_real_http_to_ftp_switch_continues_portably(tmp_path, monkeypatch):
    server, port, requests = await start_origin()
    ftp = await FtpOrigin({"/pub/movie.bin": BODY}, pace=0.002).start()
    rt = await runtime(tmp_path, monkeypatch, ftp)
    try:
        url_ftp = f"ftp://ftp-mirror.test:{ftp.port}/pub/movie.bin"
        transfer, first = await sparse_writer(rt, f"http://mirror-a.test:{port}/movie.bin")
        artifact, source_ftp = await attach(rt, transfer, "ftp", url_ftp)

        preview = await preview_candidate_switch(rt.engine, transfer.id, artifact.id, str(source_ftp.id))
        state = await rt.repository.material_state(artifact.id)
        # A fresh job imports every whole DP-valid piece: nothing DP holds
        # valid is discarded, so nothing to confirm.
        assert preview["discarded_bytes"] == 0 and preview["retained_bytes"] == state.valid_bytes
        await manual_candidate_failover(rt.engine, transfer.id, artifact.id, str(source_ftp.id))
        await completed(rt, transfer)
        await fresh_writer(rt, transfer, first, source_ftp)
        # It fetched (REST) only past DP-valid ranges.
        assert ftp.rest_offsets and all(not any(start <= offset < end for start, end in state.valid)
                                        for offset in ftp.rest_offsets)
        assert Path(artifact.target).read_bytes() == BODY
    finally:
        await rt.close()
        server.close()
