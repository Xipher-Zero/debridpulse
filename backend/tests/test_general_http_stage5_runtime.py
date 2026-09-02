"""Stage 5 real-runtime proof for direct HTTP and transient authentication."""
from __future__ import annotations

import asyncio
import base64
import json
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
from transfers.models import InputMethod, TransferRequest, TransferState
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository

pytestmark = pytest.mark.asyncio


async def _start_aria2(tmp_path):
    if shutil.which("aria2c") is None:
        pytest.skip("aria2c is required for Stage 5 runtime qualification")
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    secret = "stage5-rpc-secret"
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


async def _start_http(body=b"stage5-payload", *, username=None, password=None):
    state = {"requests": [], "authorized": 0}
    expected = None
    if username is not None:
        expected = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()

    async def handle(reader, writer):
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=3)
            text = raw.decode("iso-8859-1", errors="replace")
            authorization = None
            for line in text.split("\r\n"):
                if line.casefold().startswith("authorization:"):
                    authorization = line.split(":", 1)[1].strip()
                    break
            state["requests"].append(authorization)
            if expected is not None and authorization != expected:
                writer.write(
                    b"HTTP/1.1 401 Unauthorized\r\n"
                    b"WWW-Authenticate: Basic realm=\"DebridPulse Stage 5\"\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
            else:
                state["authorized"] += 1
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    + f"Content-Length: {len(body)}\r\n".encode()
                    + b"Content-Type: application/octet-stream\r\n"
                    + b"Connection: close\r\n\r\n"
                    + body
                )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    return server, port, state


async def _runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "stage5.sqlite3")
    await database.init_db()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    policy = TransferPolicy(retry_delay=0, adoption_stability_seconds=0, max_active_executions=2)
    engine = TransferEngine(repository, registry, download_root=str(downloads), policy=policy)
    await engine.initialize()
    proc, service = await _start_aria2(downloads)

    async def validated(uri):
        return uri

    monkeypatch.setattr(aria2_runtime, "validate_resolved_public_destination", validated)
    egress = SimpleNamespace(ensure_started=_noop, job_options=lambda address, external: {})
    executor = Aria2Executor(service, Aria2Configuration(str(downloads), external=False, confirmation_delay=0),
                             repository.authorize_execution, egress=egress)
    registry.register_provider(GeneralHttpProvider())
    registry.register_executor(executor)
    return repository, engine, proc, service, downloads


async def _noop():
    return None


async def _until(engine, predicate, *, label):
    for _ in range(180):
        await engine.tick()
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError(f"Stage 5 runtime did not reach {label}")


async def _db_text():
    async with database.get_db() as db:
        tables = [row["name"] for row in await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
                  if not row["name"].startswith("sqlite_")]
        content = {name: await db.fetchall(f"SELECT * FROM {name}") for name in tables}
    return json.dumps(content, default=str, sort_keys=True)


async def test_real_general_http_transfer_uses_general_provider_and_aria2(tmp_path, monkeypatch):
    server, port, state = await _start_http(b"ordinary-direct-http")
    repository, engine, proc, service, downloads = await _runtime(tmp_path, monkeypatch)
    uri = f"http://127.0.0.1:{port}/ordinary.bin"
    try:
        transfer = await engine.submit((TransferRequest("http", uri, preferred_provider="general_http"),), deduplicate=False)

        async def completed():
            current = await repository.get(transfer.id)
            return current if current.state == TransferState.COMPLETED else None

        current = await _until(engine, completed, label="ordinary HTTP completion")
        assert current.id == transfer.id
        assert (downloads / "ordinary.bin").read_bytes() == b"ordinary-direct-http"
        presentation = await repository.presentation(transfer.id, details=True)
        assert presentation["providers"] == ["general_http"]
        assert presentation["executors"] == ["aria2"]
        assert state["authorized"] == 1
    finally:
        await _stop_aria2(proc, service)
        server.close()
        await server.wait_closed()


async def test_definitive_401_wrong_then_correct_credentials_complete_same_transfer_and_attempt(tmp_path, monkeypatch):
    username = "stage5-user-sentinel"
    wrong = "stage5-wrong-password-sentinel"
    password = "stage5-correct-password-sentinel"
    wrong_header = "Basic " + base64.b64encode(f"{username}:{wrong}".encode()).decode()
    correct_header = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
    server, port, state = await _start_http(b"authenticated-direct-http", username=username, password=password)
    repository, engine, proc, service, downloads = await _runtime(tmp_path, monkeypatch)
    uri = f"http://127.0.0.1:{port}/protected.bin"
    try:
        transfer = await engine.submit((TransferRequest("http", uri, preferred_provider="general_http"),), deduplicate=False)

        async def challenged():
            return await engine.challenges.current(transfer.id)

        first = await _until(engine, challenged, label="first HTTP authentication challenge")
        assert first.origin.value == "executor"
        assert first.methods[0].method == InputMethod.USERNAME_PASSWORD
        artifact = (await repository.artifacts(transfer.id))[0]
        first_attempt = artifact.execution.attempt_id
        first_gid = artifact.execution.context["gid"]
        assert artifact.retries == 1

        await engine.submit_input(transfer.id, first.id, "username_password", {"username": username, "password": wrong})

        async def rechallenged():
            challenge = await engine.challenges.current(transfer.id)
            return challenge if challenge and challenge.id != first.id else None

        second = await _until(engine, rechallenged, label="correctable HTTP authentication challenge")
        artifact = (await repository.artifacts(transfer.id))[0]
        assert artifact.execution.attempt_id == first_attempt
        assert artifact.execution.context["gid"] == first_gid
        assert artifact.retries == 1
        with pytest.raises(ValueError):
            await engine.submit_input(transfer.id, first.id, "username_password", {"username": username, "password": password})

        await engine.submit_input(transfer.id, second.id, "username_password", {"username": username, "password": password})

        async def completed_without_rechallenge():
            current = await repository.get(transfer.id)
            challenge = await engine.challenges.current(transfer.id)
            if challenge is not None and challenge.id != second.id:
                pattern = ["none" if item is None else "wrong" if item == wrong_header else "correct" if item == correct_header else "other"
                           for item in state["requests"]]
                raise AssertionError(f"corrected credentials were rechallenged; request pattern={pattern}")
            if current.state == TransferState.FAILED:
                artifact = (await repository.artifacts(transfer.id))[0]
                attempt = (await repository.executions(transfer.id))[0]
                pattern = ["none" if item is None else "wrong" if item == wrong_header else "correct" if item == correct_header else "other"
                           for item in state["requests"]]
                raise AssertionError(f"corrected credentials failed; artifact={artifact.state} attempt={attempt.state} request pattern={pattern}")
            return current if current.state == TransferState.COMPLETED else None

        current = await _until(engine, completed_without_rechallenge, label="authenticated HTTP completion")
        artifact = (await repository.artifacts(transfer.id))[0]
        assert current.id == transfer.id
        assert artifact.execution.attempt_id == first_attempt
        assert artifact.execution.context["gid"] == first_gid
        assert artifact.retries == 1
        assert (downloads / "protected.bin").read_bytes() == b"authenticated-direct-http"
        assert await engine.challenges.current(transfer.id) is None
        assert state["authorized"] == 1
        assert state["requests"][0] is None
        assert wrong_header in state["requests"]
        assert correct_header in state["requests"]

        persisted = await _db_text()
        for secret in (username, wrong, password):
            assert secret not in persisted
            assert secret not in repr(artifact.execution)
    finally:
        await _stop_aria2(proc, service)
        server.close()
        await server.wait_closed()
