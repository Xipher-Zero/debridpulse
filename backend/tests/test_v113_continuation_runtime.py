"""Real aria2 runtime proof of DebridPulse-owned continuation (1.0.13).

A real aria2 daemon (production-like 64 MiB write cache, falloc, several
connections per job) downloads from a throttled Range-capable HTTP origin.
Proves at the executor's real file boundary that DP-valid material is exactly
what is on disk, that Pause parks the native job and Resume continues it with
no sparse material re-fetched, that a fresh writer continues only at the DP
boundary, and that aria2's own control file can never expand DP-valid material
after a crash.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import signal
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

import db.database as database
import executors.aria2.executor as aria2_module
from executors.aria2.client import Aria2Service
from executors.aria2.executor import Aria2Configuration, Aria2Executor
from providers.general_http.provider import GeneralHttpProvider
from transfers import material as mat
from transfers.convergence_engine import TransferEngine
from transfers.models import ContinuationStrategy, TransferRequest, TransferState
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = [pytest.mark.asyncio, pytest.mark.real_runtime]
MIB = 1 << 20
BODY = b"".join(hashlib.sha256(str(index).encode()).digest() for index in range(24 * MIB // 32))


class Served(SimpleNamespace):
    """One origin request: where a ranged request started, and bytes sent."""


def ranged_starts(served):
    return [item.start for item in served if item.ranged]


def writer_starts(served):
    """Where a WRITER's ranged reads started: every ranged request except the
    one-byte location probe (``bytes=0-0``) that resolves an HTTP(S)
    download's answering address before its writer starts."""
    return [item.start for item in served if item.ranged and (item.start, item.end) != (0, 1)]


def bytes_sent(served):
    return sum(item.sent for item in served)


async def start_origin(*, rate=1 * MIB):
    requests = []

    async def handle(reader, writer):
        record = Served(start=0, end=len(BODY), ranged=False, sent=0)
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            start, end = 0, len(BODY)
            for line in raw.decode("iso-8859-1").split("\r\n"):
                if line.casefold().startswith("range:"):
                    spec = line.split("=", 1)[1].strip()
                    first, _, last = spec.partition("-")
                    start = int(first)
                    end = int(last) + 1 if last else len(BODY)
                    record.ranged = True
            record.start, record.end = start, end
            requests.append(record)
            partial = (start, end) != (0, len(BODY))
            head = (b"HTTP/1.1 206 Partial Content\r\n" if partial else b"HTTP/1.1 200 OK\r\n")
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


async def start_daemon(root: Path, port: int | None = None):
    if shutil.which("aria2c") is None:
        pytest.skip("aria2c is required for continuation runtime qualification")
    if port is None:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
        probe.close()
    secret = "continuation-runtime-secret"
    proc = await asyncio.create_subprocess_exec(
        "aria2c", "--enable-rpc=true", "--rpc-listen-all=false", f"--rpc-listen-port={port}",
        f"--rpc-secret={secret}", f"--dir={root}", "--summary-interval=0", "--console-log-level=warn",
        "--disk-cache=64M", "--file-allocation=falloc", "--auto-save-interval=1", "--max-download-result=50",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    service = Aria2Service(f"http://127.0.0.1:{port}/jsonrpc", secret, 3)
    for _ in range(100):
        try:
            await service.test()
            return proc, service
        except Exception:
            await asyncio.sleep(0.05)
    proc.kill()
    raise AssertionError("aria2 RPC did not become ready")


async def stop_daemon(proc, service):
    try:
        await service._call("aria2.shutdown")
        await asyncio.wait_for(proc.wait(), timeout=5)
    except Exception:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def _noop():
    return None


async def build(tmp_path, monkeypatch, *, checkpoint=1.0):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "runtime.sqlite3")
    await database.init_db()

    async def validated(uri, **_kwargs):
        return uri

    monkeypatch.setattr(aria2_module, "validate_resolved_public_destination", validated)
    root = tmp_path / "downloads"
    root.mkdir(exist_ok=True)
    return await engine_for(root, checkpoint=checkpoint)


async def engine_for(root, *, checkpoint=1.0, port=None):
    # A restarted daemon keeps the one fixed RPC endpoint, as in production:
    # execution handles are bound to it.
    proc, service = await start_daemon(root, port)
    repository = TransferRepository()
    registry = IntegrationRegistry()
    egress = SimpleNamespace(ensure_started=_noop, job_options=lambda address, scope=None, **_kw: {})
    executor = Aria2Executor(service, Aria2Configuration(str(root), split=4, minimum_split_size="1M",
                                                         connections_per_server=4, confirmation_delay=0.05),
                             repository.authorize_execution, egress=egress)
    registry.register_provider(GeneralHttpProvider())
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(root),
                            policy=TransferPolicy(retry_delay=0, adoption_stability_seconds=0,
                                                  max_active_executions=2,
                                                  material_checkpoint_interval=checkpoint))
    await engine.initialize()
    return SimpleNamespace(engine=engine, repository=repository, executor=executor, proc=proc, service=service,
                           root=root)


async def until(ctx, predicate, *, label, attempts=400):
    for _ in range(attempts):
        await ctx.engine.tick()
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(0.05)
    rows = [(item.state, item.error.category if item.error else None, item.execution)
             for transfer in await ctx.repository.active() for item in await ctx.repository.artifacts(transfer.id)]
    attempts = [(item.state, item.error.category if item.error else None) for item in await ctx.repository.executions()]
    raise AssertionError(f"runtime did not reach {label}: artifacts={rows} attempts={attempts}")


def on_disk_matches(path, ranges) -> bool:
    with open(path, "rb") as handle:
        for start, end in ranges:
            handle.seek(start)
            if handle.read(end - start) != BODY[start:end]:
                return False
    return True


async def test_real_aria2_pause_parks_sparse_material_and_resume_continues_the_same_job(tmp_path, monkeypatch):
    """Four connections fill four segments at once, so DP-valid material is
    sparse. Pause checkpoints it and parks the native job (aria2 keeps its own
    piece map); Resume unpauses that same job: no fresh writer, no truncation,
    no control-file loss, and nothing DP held valid is ever fetched again."""
    server, port, requests = await start_origin()
    ctx = await build(tmp_path, monkeypatch)
    try:
        transfer = await ctx.engine.submit((TransferRequest("http", f"http://127.0.0.1:{port}/movie.bin",
                                                            preferred_provider="general_http"),), deduplicate=False)

        async def sparse():
            artifacts = await ctx.repository.artifacts(transfer.id)
            if not artifacts:
                return None
            state = await ctx.repository.material_state(artifacts[0].id)
            return (artifacts[0], state) if state and mat.total(state.valid) - state.safe_prefix >= 3 * MIB else None

        artifact, _ = await until(ctx, sparse, label="sparse committed material")
        await ctx.engine.pause(transfer.id)
        paused = (await ctx.repository.artifacts(transfer.id))[0]
        state = await ctx.repository.material_state(paused.id)
        # Parked, not fenced: the same attempt keeps the paused native job.
        assert paused.execution == artifact.execution and (await ctx.repository.get(transfer.id)).paused
        assert (await ctx.service.tell_status(paused.execution.native["gid"])).status == "paused"
        # Everything DP calls VALID is byte-exact on disk -- sparse ranges past
        # the contiguous prefix included -- and the private control file stays.
        assert mat.total(state.valid) > state.safe_prefix and on_disk_matches(paused.target, state.valid)
        assert Path(paused.target + ".aria2").exists()
        assert os.path.getsize(paused.target) == len(BODY)  # falloc: length proves nothing
        before_resume = len(requests)

        await ctx.engine.resume(transfer.id)

        async def completed():
            current = await ctx.repository.get(transfer.id)
            return current if current.state == TransferState.COMPLETED else None

        await until(ctx, completed, label="completion after resume")
        resumed = requests[before_resume:]
        # The same job continued from its own piece map: no request began inside
        # material DP held valid, so the sparse ranges were never fetched again
        # (a fresh prefix-only writer would have had to re-fetch all of them).
        assert resumed and all(not (start <= item.start < end) for item in resumed if item.ranged
                                for start, end in state.valid)
        assert bytes_sent(resumed) <= len(BODY) - mat.total(state.valid) + 4 * MIB
        assert Path(paused.target).read_bytes() == BODY
        final = await ctx.repository.material_state(paused.id)
        assert final.valid == ((0, len(BODY)),) and final.writer_generation == 1
        assert final.material_generation == state.material_generation
        assert len(await ctx.repository.executions(transfer.id)) == 1
    finally:
        await stop_daemon(ctx.proc, ctx.service)
        server.close()


async def test_real_aria2_crash_stale_control_file_cannot_expand_dp_material(tmp_path, monkeypatch):
    server, port, requests = await start_origin()
    ctx = await build(tmp_path, monkeypatch)
    try:
        transfer = await ctx.engine.submit((TransferRequest("http", f"http://127.0.0.1:{port}/movie.bin",
                                                            preferred_provider="general_http"),), deduplicate=False)

        async def started():
            artifacts = await ctx.repository.artifacts(transfer.id)
            state = await ctx.repository.material_state(artifacts[0].id) if artifacts else None
            return artifacts[0] if state and state.safe_prefix >= 2 * MIB else None

        artifact = await until(ctx, started, label="a committed prefix")
        # From here on DP checkpoints nothing, while aria2's own control file
        # (saved every second) runs far ahead of what DP committed.
        from dataclasses import replace as _replace
        ctx.engine.configure_policy(_replace(ctx.engine.policy, material_checkpoint_interval=3600))
        await ctx.engine.tick()
        committed = (await ctx.repository.material_state(artifact.id)).valid
        await asyncio.sleep(3)
        status = await ctx.service.tell_status(artifact.execution.native["gid"])
        assert int(status.completed_length) > mat.total(committed) + MIB
        control = Path(artifact.target + ".aria2")
        assert control.exists()

        # Hard crash of the executor (and of this engine instance).
        ctx.proc.send_signal(signal.SIGKILL)
        await ctx.proc.wait()
        assert control.exists()
        restarted = await engine_for(ctx.root, port=int(ctx.service.url.rsplit(":", 1)[1].split("/")[0]))
        ctx.proc, ctx.service = restarted.proc, restarted.service
        after_crash = await restarted.repository.material_state(artifact.id)
        assert after_crash.valid == committed  # uncheckpointed work was never trusted
        before = len(requests)

        async def completed():
            current = await restarted.repository.get(transfer.id)
            return current if current.state == TransferState.COMPLETED else None

        await until(restarted, completed, label="completion after crash", attempts=800)
        plan_rows = [attempt for attempt in await restarted.repository.executions(transfer.id)]
        assert len(plan_rows) >= 2
        # The replacement continued at the DP boundary, not aria2's claim: no
        # ranged request below it and the committed prefix never re-fetched.
        boundary = mat.contiguous_prefix(committed)
        assert boundary >= 2 * MIB
        assert min(ranged_starts(requests[before:])) == boundary
        assert bytes_sent(requests[before:]) <= len(BODY) - boundary + MIB
        assert Path(artifact.target).read_bytes() == BODY
    finally:
        await stop_daemon(ctx.proc, ctx.service)
        server.close()


async def test_real_aria2_honors_a_dp_plan_from_another_source_and_ignores_foreign_state(tmp_path, monkeypatch):
    """Same executor, changed source: material written through one origin is
    continued from another, exactly at the plan boundary. A stale control file
    and extra bytes past the boundary carry no authority."""
    from transfers.models import (
        ContinuationPlan, Endpoint, ExecutionRequest, ExecutionState, ExecutionSubject, ExecutionWork,
        MaterializationKind, MaterializationPlan, TransferCandidate,
    )

    async def validated(uri, **_kwargs):
        return uri

    monkeypatch.setattr(aria2_module, "validate_resolved_public_destination", validated)
    root = tmp_path / "downloads"
    root.mkdir()
    server, port, requests = await start_origin(rate=8 * MIB)
    proc, service = await start_daemon(root)
    try:
        async def authorize(_handle, _action):
            return True

        egress = SimpleNamespace(ensure_started=_noop, job_options=lambda address, scope=None, **_kw: {})
        executor = Aria2Executor(service, Aria2Configuration(str(root), split=4, minimum_split_size="1M",
                                                             connections_per_server=4, confirmation_delay=0.05),
                                 authorize, egress=egress)
        target = root / "movie.bin"
        boundary = 5 * MIB
        # Retained prefix (written earlier through another source), plus bytes
        # past the boundary DP never validated, plus a foreign control file.
        target.write_bytes(BODY[:boundary] + b"\0" * (3 * MIB))
        (root / "movie.bin.aria2").write_bytes(os.urandom(4096))
        candidate = TransferCandidate("movie.bin", (Endpoint("http", f"http://127.0.0.1:{port}/b/movie.bin"),),
                                      expected_bytes=len(BODY), request_kind="http")
        work = ExecutionWork(ExecutionSubject.of(candidate),
                             MaterializationPlan(MaterializationKind.FILE, str(root), str(target)), "attempt-b")
        plan = ContinuationPlan(1, 1, mat.GEOMETRY_VERSION, str(candidate.id), "aria2",
                                ContinuationStrategy.CONTIGUOUS_FROM_OFFSET, boundary, ((0, boundary),), (),
                                ((boundary, len(BODY)),), len(BODY), "user_candidate_switch")
        request = ExecutionRequest(work, "attempt-b", continuation=plan)
        handle = executor.prepare(request)
        observed = await executor.start(request, handle)
        assert observed.error is None, observed.error
        for _ in range(400):
            observed = await executor.observe(handle)
            if observed.state in {ExecutionState.SUCCEEDED, ExecutionState.FAILED}:
                break
            await asyncio.sleep(0.05)
        assert observed.state == ExecutionState.SUCCEEDED, observed.error
        assert target.read_bytes() == BODY
        assert min(ranged_starts(requests)) == boundary
        assert bytes_sent(requests) <= len(BODY) - boundary + MIB
    finally:
        await stop_daemon(proc, service)
        server.close()
