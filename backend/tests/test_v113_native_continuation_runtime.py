"""Real runtime proof: native-state handoff between equivalent sources (1.0.13).

Real owners end to end: the convergence engine, repository and canonical
owners, the real ``GeneralHttpProvider``/``GeneralFtpProvider``, the real
``Aria2Executor`` driving a real ``aria2c`` with several connections per job,
and the real ``DownloaderEgressGuard`` (only its resolver maps the fixture
hostnames to loopback). Two HTTP hostnames serve the same throttled bytes, and
an FTP origin serves them too.

HTTP A -> HTTP B while paused only records the desired source; Resume retargets
the parked native job through the guard (no discard to confirm). HTTP -> FTP
cannot be retargeted and falls back to the portable planner, which reports and
needs confirmation for its discard.
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
from transfers.manual_failover import (
    DiscardConfirmationRequired, manual_candidate_failover, preview_candidate_switch,
)
from transfers.models import ContinuationStrategy, TransferRequest, TransferState
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio
MIB = 1 << 20
BODY = b"".join(hashlib.sha256(b"handoff" + str(index).encode()).digest() for index in range(24 * MIB // 32))


async def start_origin(*, rate=1 * MIB):
    """A throttled Range-capable HTTP origin recording, per request, the Host
    it was asked for, where a ranged request began, and the bytes it sent."""
    requests = []

    async def handle(reader, writer):
        record = SimpleNamespace(host="", path="", start=0, ranged=False, sent=0)
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
            record.start = start
            requests.append(record)
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
    return server, int(server.sockets[0].getsockname()[1]), requests


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


async def test_real_paused_switch_to_an_equivalent_http_source_keeps_the_native_job(tmp_path, monkeypatch):
    server, port, requests = await start_origin()
    rt = await runtime(tmp_path, monkeypatch)
    try:
        url_a, url_b = f"http://mirror-a.test:{port}/movie.bin", f"http://mirror-b.test:{port}/movie.bin"
        transfer, first = await sparse_writer(rt, url_a)
        artifact, source_b = await attach(rt, transfer, "http", url_b)
        await rt.engine.pause(transfer.id)
        paused = (await rt.repository.artifacts(transfer.id))[0]
        gid = paused.execution.native["gid"]
        assert paused.execution == first.execution
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
        assert Path(switched.target + ".aria2").exists()
        assert (await rt.repository.material_state(artifact.id)) == at_pause
        before_resume = len(requests)

        # Resume is the commit point: the one retarget, then acquisition under B.
        await rt.engine.resume(transfer.id)
        await completed(rt, transfer)
        attempts = await rt.repository.executions(transfer.id)
        new = attempts[-1].handle
        assert len(attempts) == 2 and new.native == {"gid": gid} and attempts[-1].candidate.id == source_b.id
        plan = await rt.repository.execution_continuation(new.attempt_id)
        assert plan.strategy == ContinuationStrategy.NATIVE_STATE_HANDOFF and plan.discarded == ()
        assert plan.material_generation == at_pause.material_generation and plan.retained == at_pause.valid
        resumed = requests[before_resume:]
        from_a = sum(item.sent for item in resumed if item.host == "mirror-a.test")
        from_b = sum(item.sent for item in resumed if item.host == "mirror-b.test")
        # aria2 can replace a job's source only while it runs: a short window
        # may still fetch from A; everything else came from B, and nothing DP
        # held valid was fetched again from either.
        assert from_b > 0 and from_a <= 2 * MIB and resumed[-1].host == "mirror-b.test"
        assert all(not (start <= item.start < end) for item in resumed if item.ranged
                   for start, end in at_pause.valid)
        assert from_a + from_b <= len(BODY) - mat.total(at_pause.valid) + 4 * MIB
        assert Path(switched.target).read_bytes() == BODY
        assert not await audit(transfer.id, "rollback")
        assert (await audit(transfer.id, "native_retarget"))[-1]["accepted"] is True
    finally:
        await rt.close()
        server.close()


async def test_real_http_to_ftp_switch_cannot_retarget_and_falls_back_truthfully(tmp_path, monkeypatch):
    server, port, requests = await start_origin()
    ftp = await FtpOrigin({"/pub/movie.bin": BODY}, pace=0.002).start()
    rt = await runtime(tmp_path, monkeypatch, ftp)
    try:
        url_ftp = f"ftp://ftp-mirror.test:{ftp.port}/pub/movie.bin"
        transfer, first = await sparse_writer(rt, f"http://mirror-a.test:{port}/movie.bin")
        artifact, source_ftp = await attach(rt, transfer, "ftp", url_ftp)
        gid = first.execution.native["gid"]

        preview = await preview_candidate_switch(rt.engine, transfer.id, artifact.id, str(source_ftp.id))
        state = await rt.repository.material_state(artifact.id)
        assert preview["discarded_bytes"] > 0 and preview["retained_bytes"] <= state.safe_prefix
        with pytest.raises(DiscardConfirmationRequired):
            await manual_candidate_failover(rt.engine, transfer.id, artifact.id, str(source_ftp.id))
        assert (await rt.repository.artifacts(transfer.id))[0].execution == first.execution

        await manual_candidate_failover(rt.engine, transfer.id, artifact.id, str(source_ftp.id),
                                        discard_confirmed=True, discard_confirmation=preview)
        await completed(rt, transfer)
        # The old native job was retired and a fresh FTP writer continued at
        # the DP prefix boundary (REST) -- never an import of sparse ranges.
        with pytest.raises(Exception):
            await rt.service.tell_status(gid)
        rollback = (await audit(transfer.id, "rollback"))[-1]
        plan_retained = rollback["retained_bytes"]
        assert plan_retained in ftp.rest_offsets and plan_retained <= state.safe_prefix + MIB
        assert rollback["discarded_bytes"] >= preview["discarded_bytes"]
        unavailable = (await audit(transfer.id, "native_retarget"))[-1]
        assert unavailable["accepted"] is False and unavailable["fallback"] == "contiguous_from_offset"
        assert Path(artifact.target).read_bytes() == BODY
    finally:
        await rt.close()
        server.close()
