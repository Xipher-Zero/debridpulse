"""Stage 5 end-to-end HTTPS qualification through General HTTP and aria2."""
from __future__ import annotations

import asyncio
import shutil
import socket
import ssl
import subprocess
from pathlib import Path

import pytest

import db.database as database
import executors.aria2.executor as aria2_runtime
from executors.aria2.client import Aria2Service
from executors.aria2.executor import Aria2Configuration, Aria2Executor
from providers.general_http.provider import GeneralHttpProvider
from services.downloader_egress_guard import DownloaderEgressGuard
from transfers.engine import TransferEngine
from transfers.models import TransferRequest, TransferState
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository

pytestmark = pytest.mark.asyncio


def _answer(address: str, port: int) -> tuple:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)


def _test_tls_context(tmp_path: Path, state: dict) -> tuple[ssl.SSLContext, Path]:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is required for Stage 5 HTTPS qualification")

    ca_key = tmp_path / "stage5-ca.key"
    ca_cert = tmp_path / "stage5-ca.crt"
    server_key = tmp_path / "stage5-server.key"
    server_csr = tmp_path / "stage5-server.csr"
    server_cert = tmp_path / "stage5-server.crt"
    server_ext = tmp_path / "stage5-server.ext"

    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=DebridPulse Stage 5 Test CA",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca_cert),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            openssl,
            "req",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-nodes",
            "-subj",
            "/CN=stage5-https.test",
            "-keyout",
            str(server_key),
            "-out",
            str(server_csr),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    server_ext.write_text(
        "\n".join(
            (
                "basicConstraints=critical,CA:FALSE",
                "keyUsage=critical,digitalSignature,keyEncipherment",
                "extendedKeyUsage=serverAuth",
                "subjectAltName=DNS:stage5-https.test",
                "",
            )
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [
            openssl,
            "x509",
            "-req",
            "-in",
            str(server_csr),
            "-CA",
            str(ca_cert),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-days",
            "1",
            "-sha256",
            "-extfile",
            str(server_ext),
            "-out",
            str(server_cert),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=server_cert, keyfile=server_key)

    def record_sni(_socket, server_name, _context):
        state["sni"].append(server_name)

    context.set_servername_callback(record_sni)
    return context, ca_cert


async def _start_https(tmp_path: Path, body: bytes):
    state = {"connections": 0, "sni": [], "hosts": []}
    context, ca_cert = _test_tls_context(tmp_path, state)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        state["connections"] += 1
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=3)
            for line in raw.decode("iso-8859-1", errors="replace").split("\r\n"):
                if line.casefold().startswith("host:"):
                    state["hosts"].append(line.split(":", 1)[1].strip())
                    break
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                + f"Content-Length: {len(body)}\r\n".encode()
                + b"Content-Type: application/octet-stream\r\n"
                + b"Connection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    port = int(server.sockets[0].getsockname()[1])
    return server, port, state, ca_cert


async def _start_aria2(tmp_path: Path, ca_cert: Path):
    if shutil.which("aria2c") is None:
        pytest.skip("aria2c is required for Stage 5 HTTPS qualification")
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    secret = "stage5-https-rpc-secret"
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
        f"--ca-certificate={ca_cert}",
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


async def _stop_aria2(proc, service: Aria2Service) -> None:
    try:
        await service._call("aria2.shutdown")
    except Exception:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=3)
    except TimeoutError:
        proc.kill()
        await proc.wait()


async def _until(engine: TransferEngine, repository: TransferRepository, transfer_id: str):
    for _ in range(180):
        await engine.tick()
        current = await repository.get(transfer_id)
        if current.state == TransferState.FAILED:
            artifact = (await repository.artifacts(transfer_id))[0]
            executions = await repository.executions(transfer_id)
            raise AssertionError(
                f"Stage 5 HTTPS transfer failed: artifact={artifact.state}; executions={executions!r}"
            )
        if current.state == TransferState.COMPLETED:
            return current
        await asyncio.sleep(0.05)
    raise AssertionError("Stage 5 HTTPS transfer did not complete")


async def test_real_general_https_transfer_uses_general_provider_core_aria2_and_preserves_tls_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payload = b"ordinary-direct-https"
    server, port, state, ca_cert = await _start_https(tmp_path, payload)
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    proc, service = await _start_aria2(downloads, ca_cert)
    seen_hosts: list[str] = []

    async def resolver(host: str, resolved_port: int):
        seen_hosts.append(host)
        assert host == "stage5-https.test"
        assert resolved_port == port
        return [_answer("127.0.0.1", resolved_port)]

    guard = DownloaderEgressGuard(
        resolver=resolver,
        public_check=lambda address: address == "127.0.0.1",
        bind_host="127.0.0.1",
        bind_port=0,
    )

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "stage5-https.sqlite3")
    await database.init_db()

    async def validated(uri: str) -> str:
        return uri

    monkeypatch.setattr(aria2_runtime, "validate_resolved_public_destination", validated)

    repository = TransferRepository()
    registry = IntegrationRegistry()
    policy = TransferPolicy(retry_delay=0, adoption_stability_seconds=0, max_active_executions=2)
    engine = TransferEngine(repository, registry, download_root=str(downloads), policy=policy)
    await engine.initialize()
    executor = Aria2Executor(
        service,
        Aria2Configuration(str(downloads), external=False, confirmation_delay=0),
        repository.authorize_execution,
        egress=guard,
    )
    registry.register_provider(GeneralHttpProvider())
    registry.register_executor(executor)
    uri = f"https://stage5-https.test:{port}/secure.bin"

    try:
        transfer = await engine.submit(
            (TransferRequest("https", uri, preferred_provider="general_http"),),
            deduplicate=False,
        )
        current = await _until(engine, repository, transfer.id)

        assert current.id == transfer.id
        assert (downloads / "secure.bin").read_bytes() == payload
        presentation = await repository.presentation(transfer.id, details=True)
        assert presentation["providers"] == ["general_http"]
        assert presentation["executors"] == ["aria2"]
        assert seen_hosts == ["stage5-https.test"]
        assert state["connections"] == 1
        assert state["sni"] == ["stage5-https.test"]
        assert len(state["hosts"]) == 1
        assert state["hosts"][0].casefold().startswith("stage5-https.test")
    finally:
        await _stop_aria2(proc, service)
        await guard.stop()
        server.close()
        await server.wait_closed()
