"""Stage 5 negative authentication-boundary proofs for ordinary HTTP resources."""
from __future__ import annotations

import asyncio
import shutil
import socket
from types import SimpleNamespace

import pytest

import db.database as database
import executors.aria2.executor as aria2_runtime
from executors.aria2.client import Aria2Service
from executors.aria2.executor import Aria2Configuration, Aria2Executor
from providers.general_http.provider import GeneralHttpProvider
from transfers.engine import TransferEngine
from transfers.models import TransferRequest, TransferState
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository

pytestmark = pytest.mark.asyncio


async def _start_aria2(tmp_path):
    if shutil.which("aria2c") is None:
        pytest.skip("aria2c is required for Stage 5 auth-boundary qualification")
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    secret = "stage5-boundary-rpc-secret"
    proc = await asyncio.create_subprocess_exec(
        "aria2c",
        "--enable-rpc=true",
        "--rpc-listen-all=false",
        f"--rpc-listen-port={port}",
        f"--rpc-secret={secret}",
        "--rpc-allow-origin-all=false",
        f"--dir={tmp_path}",
        "--max-download-result=20",
        "--summary-interval=0",
        "--console-log-level=warn",
        "--auto-file-renaming=false",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    service = Aria2Service(f"http://127.0.0.1:{port}/jsonrpc", secret, 3)
    last = None
    for _ in range(80):
        if proc.returncode is not None:
            stdout, stderr = await proc.communicate()
            raise AssertionError(f"aria2c exited early: {stdout!r} {stderr!r}")
        try:
            await service.test()
            return proc, service
        except Exception as exc:
            last = exc
            await asyncio.sleep(0.05)
    proc.terminate()
    await proc.wait()
    raise AssertionError(f"aria2 RPC did not become ready: {last}")


async def _stop_aria2(proc, service):
    try:
        await service._call("aria2.shutdown")
    except Exception:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=3)
    except TimeoutError:
        proc.kill()
        await proc.wait()


async def _start_origin(status: int, body: bytes = b"", *, content_type: str = "text/plain"):
    reason = {
        200: "OK",
        403: "Forbidden",
        404: "Not Found",
        503: "Service Unavailable",
    }[status]
    state = {"requests": 0}

    async def handle(reader, writer):
        state["requests"] += 1
        try:
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=3)
            writer.write(
                f"HTTP/1.1 {status} {reason}\r\n".encode()
                + f"Content-Length: {len(body)}\r\n".encode()
                + f"Content-Type: {content_type}\r\n".encode()
                + b"Connection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, int(server.sockets[0].getsockname()[1]), state


async def _runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "stage5-boundary.sqlite3")
    await database.init_db()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    policy = TransferPolicy(
        max_attempts=1,
        retry_delay=0,
        adoption_stability_seconds=0,
        max_active_executions=2,
    )
    engine = TransferEngine(repository, registry, download_root=str(downloads), policy=policy)
    await engine.initialize()
    proc, service = await _start_aria2(downloads)

    async def validated(uri):
        return uri

    monkeypatch.setattr(aria2_runtime, "validate_resolved_public_destination", validated)
    egress = SimpleNamespace(ensure_started=_noop, job_options=lambda address, external: {})
    executor = Aria2Executor(
        service,
        Aria2Configuration(str(downloads), external=False, confirmation_delay=0),
        repository.authorize_execution,
        egress=egress,
    )
    registry.register_provider(GeneralHttpProvider())
    registry.register_executor(executor)
    return repository, engine, proc, service, downloads


async def _noop():
    return None


async def _terminal(engine: TransferEngine, repository: TransferRepository, transfer_id: str):
    for _ in range(180):
        await engine.tick()
        challenge = await engine.challenges.current(transfer_id)
        if challenge is not None:
            raise AssertionError(
                f"non-auth HTTP response unexpectedly became input-required: {challenge!r}"
            )
        current = await repository.get(transfer_id)
        if current.state in {TransferState.COMPLETED, TransferState.FAILED}:
            return current
        await asyncio.sleep(0.05)
    raise AssertionError("Stage 5 auth-boundary transfer did not settle")


@pytest.mark.parametrize("status", [403, 404, 503])
async def test_http_failures_do_not_become_authentication_challenges(tmp_path, monkeypatch, status):
    server, port, state = await _start_origin(status)
    repository, engine, proc, service, _downloads = await _runtime(tmp_path, monkeypatch)
    uri = f"http://127.0.0.1:{port}/status-{status}.bin"
    try:
        transfer = await engine.submit(
            (TransferRequest("http", uri, preferred_provider="general_http"),),
            deduplicate=False,
        )
        current = await _terminal(engine, repository, transfer.id)
        assert current.state == TransferState.FAILED
        assert await engine.challenges.current(transfer.id) is None
        assert state["requests"] >= 1
    finally:
        await _stop_aria2(proc, service)
        server.close()
        await server.wait_closed()


async def test_html_login_form_is_ordinary_content_not_browser_auth_discovery(tmp_path, monkeypatch):
    body = (
        b"<!doctype html><title>Sign in</title>"
        b"<form method='post' action='/login'><input name='username'>"
        b"<input name='password' type='password'><button>Sign in</button></form>"
    )
    server, port, state = await _start_origin(200, body, content_type="text/html")
    repository, engine, proc, service, downloads = await _runtime(tmp_path, monkeypatch)
    uri = f"http://127.0.0.1:{port}/login.html"
    try:
        transfer = await engine.submit(
            (TransferRequest("http", uri, preferred_provider="general_http"),),
            deduplicate=False,
        )
        current = await _terminal(engine, repository, transfer.id)
        assert current.state == TransferState.COMPLETED
        assert await engine.challenges.current(transfer.id) is None
        assert (downloads / "login.html").read_bytes() == body
        assert state["requests"] == 1
    finally:
        await _stop_aria2(proc, service)
        server.close()
        await server.wait_closed()
