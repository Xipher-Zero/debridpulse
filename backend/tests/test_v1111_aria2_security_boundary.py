from __future__ import annotations

import asyncio
import shutil
import socket
import ssl
import subprocess
import time
from pathlib import Path

import pytest

from executors.aria2.client import Aria2Service
from services.downloader_egress_guard import DownloaderEgressGuard
import executors.aria2.executor as runtime_guard
from executors.aria2.executor import Aria2Executor, Aria2Configuration
from execution_requests import file_request
from transfers.models import Endpoint, ExecutionRequest, TransferCandidate, new_identity
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

pytestmark = pytest.mark.asyncio

# Hang guards for the real aria2 subprocess, in wall-clock seconds. Both exceed
# every timeout of the parts they observe, so a helper that gives up is reporting
# a genuine stall and never its own impatience under runner load.
RPC_READY_TIMEOUT_SECONDS = 60.0
TERMINAL_STATE_TIMEOUT_SECONDS = 180.0
# The in-test origins wait longer than aria2's own 60 s timeouts before
# abandoning a peer, so a stalled runner surfaces as aria2's verdict and never
# as this scaffolding dropping a request aria2 was still sending.
ORIGIN_REQUEST_TIMEOUT_SECONDS = 90.0


def _answer(address: str, port: int) -> tuple:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)


async def _start_http_server(body: bytes = b"ok", content_type: str = "application/octet-stream"):
    state = {"connections": 0}

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        state["connections"] += 1
        try:
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=ORIGIN_REQUEST_TIMEOUT_SECONDS)
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                + f"Content-Length: {len(body)}\r\n".encode()
                + f"Content-Type: {content_type}\r\n".encode()
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


def _test_tls_context(tmp_path: Path, state: dict) -> tuple[ssl.SSLContext, Path]:
    openssl = shutil.which("openssl")
    if openssl is None:
        raise AssertionError("openssl is required for HTTPS/SNI downloader-boundary regression")

    ca_key = tmp_path / "sni-test-ca.key"
    ca_cert = tmp_path / "sni-test-ca.crt"
    server_key = tmp_path / "sni-test.key"
    server_csr = tmp_path / "sni-test.csr"
    server_cert = tmp_path / "sni-test.crt"
    server_ext = tmp_path / "sni-test.ext"

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
            "/CN=DebridPulse Test CA",
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
            "/CN=sni.test",
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
                "subjectAltName=DNS:sni.test",
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


async def _start_https_server(tmp_path: Path, body: bytes = b"tls-ok"):
    state = {"connections": 0, "sni": [], "hosts": []}
    context, ca_cert = _test_tls_context(tmp_path, state)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        state["connections"] += 1
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=ORIGIN_REQUEST_TIMEOUT_SECONDS)
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


async def _start_aria2(tmp_path: Path, *, extra_args: tuple[str, ...] = ()):
    if shutil.which("aria2c") is None:
        pytest.skip("aria2c is required for downloader-boundary regression")

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    secret = "v1111-test-secret"
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
        *extra_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    service = Aria2Service(f"http://127.0.0.1:{port}/jsonrpc", secret, 3)
    last = None
    deadline = time.monotonic() + RPC_READY_TIMEOUT_SECONDS
    attempts = 0
    while True:
        if proc.returncode is not None:
            stdout, stderr = await proc.communicate()
            raise AssertionError(f"aria2c exited early: {stdout!r} {stderr!r}")
        try:
            await service.test()
            return proc, service
        except Exception as exc:  # pragma: no cover - transient startup only
            last = exc
        attempts += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            proc.terminate()
            await proc.wait()
            raise AssertionError(
                f"aria2 RPC did not become ready within {RPC_READY_TIMEOUT_SECONDS:.0f}s "
                f"({attempts} attempts): {last}")
        await asyncio.sleep(min(0.05, remaining))


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


async def _wait_status(service: Aria2Service, gid: str, terminal=("complete", "error", "removed")):
    """Return aria2's own verdict for ``gid``.

    The bound is a hang guard, never a correctness knob, so it is derived from
    wall clock and is the longest rung of the scaffolding's timeout ladder: it
    outlasts aria2's own 60 s connect/read timeouts and the in-test origins' own
    guards, so what it returns is aria2's verdict. A shorter bound reports the
    test's impatience as the system's verdict instead -- an 8 s ceiling (160
    polls, ignoring the cost of each RPC round trip) failed exactly that way on
    a loaded runner, with an 11-byte loopback transfer still ``active``.
    Reaching this deadline now means nothing terminated anything, so say so with
    the evidence.
    """
    last = None
    deadline = time.monotonic() + TERMINAL_STATE_TIMEOUT_SECONDS
    polls = 0
    while True:
        try:
            result = await service._call(
                "aria2.tellStatus",
                [gid, ["gid", "status", "followedBy", "errorCode", "errorMessage"]],
            )
            last = result
            polls += 1
            if str(result.get("status") or "") in terminal:
                return result
        except Exception as exc:  # pragma: no cover - transient RPC state
            last = exc
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                f"aria2 job {gid} did not reach a terminal state within "
                f"{TERMINAL_STATE_TIMEOUT_SECONDS:.0f}s ({polls} polls); last={last}")
        await asyncio.sleep(min(0.05, remaining))


async def test_canonical_job_options_disable_metadata_following(tmp_path, monkeypatch) -> None:
    async def validated(uri): return uri
    monkeypatch.setattr(runtime_guard, "validate_resolved_public_destination", validated)
    from types import SimpleNamespace
    guard = SimpleNamespace(ensure_started=AsyncMock(), job_options=lambda *args, **kwargs: {})
    executor = Aria2Executor(None, Aria2Configuration(str(tmp_path)), AsyncMock(return_value=True), egress=guard)
    request = file_request(TransferCandidate("payload.bin", (Endpoint("https", "https://example.test/file"),)), str(tmp_path / "payload.bin"), new_identity())
    _uri, options = await executor._options(request, executor.prepare(request))
    assert options["follow-torrent"] == "false"
    assert options["follow-metalink"] == "false"
    assert options["max-http-redirection"] == "0"
    assert options["max-tries"] == "1"


async def test_guarded_options_override_daemon_global_proxy_bypasses() -> None:
    guard = DownloaderEgressGuard(bind_port=0)
    await guard.ensure_started()
    try:
        loopback = f"http://127.0.0.1:{guard.bound_port}"
        options = guard.job_options("https://provider.example/file.bin")
        assert options["all-proxy"] == loopback
        assert options["http-proxy"] == loopback
        assert options["https-proxy"] == loopback
        assert options["no-proxy"] == ""
        assert options["proxy-method"] == "tunnel"
        assert options["all-proxy-user"] == "debridpulse"
        assert options["all-proxy-passwd"]
    finally:
        await guard.stop()


async def test_dns_rebinding_public_preflight_private_at_connect_is_blocked(
    tmp_path: Path, monkeypatch
) -> None:
    target, target_port, state = await _start_http_server(b"must-not-connect")

    async def connection_time_resolver(host: str, port: int):
        assert host == "rebind.test"
        assert port == target_port
        return [_answer("127.0.0.1", port)]

    guard = DownloaderEgressGuard(
        resolver=connection_time_resolver,
        bind_port=0,
    )
    await guard.ensure_started()
    proc, service = await _start_aria2(tmp_path)
    preflight = []

    async def validated_as_public(uri: str) -> str:
        preflight.append((uri, "93.184.216.34"))
        return uri

    monkeypatch.setattr(runtime_guard, "validate_resolved_public_destination", validated_as_public)
    monkeypatch.setattr(runtime_guard, "downloader_egress_guard", guard)
    guarded = Aria2Executor(service, Aria2Configuration(str(tmp_path)), AsyncMock(return_value=True), egress=guard)
    uri = f"http://rebind.test:{target_port}/payload.bin"
    try:
        gid = await _start_transfer(guarded, uri, tmp_path / 'blocked.bin')
        status = await _wait_status(service, gid)
        assert status["status"] == "error"
        assert preflight == [(uri, "93.184.216.34")]
        assert state["connections"] == 0
        assert not (tmp_path / "blocked.bin").exists()
    finally:
        await _stop_aria2(proc, service)
        await guard.stop()
        target.close()
        await target.wait_closed()


async def test_guarded_actual_http_connection_succeeds_and_keeps_hostname(
    tmp_path: Path, monkeypatch
) -> None:
    target, target_port, state = await _start_http_server(b"public-path-ok")
    seen_hosts = []

    async def resolver(host: str, port: int):
        seen_hosts.append(host)
        return [_answer("127.0.0.1", port)]

    guard = DownloaderEgressGuard(
        resolver=resolver,
        public_check=lambda address: address == "127.0.0.1",
        bind_port=0,
    )
    await guard.ensure_started()
    proc, service = await _start_aria2(tmp_path)

    async def validated_as_public(uri: str) -> str:
        return uri

    monkeypatch.setattr(runtime_guard, "validate_resolved_public_destination", validated_as_public)
    monkeypatch.setattr(runtime_guard, "downloader_egress_guard", guard)
    guarded = Aria2Executor(service, Aria2Configuration(str(tmp_path)), AsyncMock(return_value=True), egress=guard)
    uri = f"http://public.test:{target_port}/payload.bin"
    try:
        gid = await _start_transfer(guarded, uri, tmp_path / 'public.bin')
        status = await _wait_status(service, gid)
        assert status["status"] == "complete"
        assert (tmp_path / "public.bin").read_bytes() == b"public-path-ok"
        assert seen_hosts == ["public.test"]
        assert state["connections"] == 1
    finally:
        await _stop_aria2(proc, service)
        await guard.stop()
        target.close()
        await target.wait_closed()


async def test_guarded_https_preserves_original_hostname_and_tls_sni(
    tmp_path: Path, monkeypatch
) -> None:
    target, target_port, state, ca_cert = await _start_https_server(tmp_path, b"tls-path-ok")
    seen_hosts = []

    async def resolver(host: str, port: int):
        seen_hosts.append(host)
        assert port == target_port
        return [_answer("127.0.0.1", port)]

    guard = DownloaderEgressGuard(
        resolver=resolver,
        public_check=lambda address: address == "127.0.0.1",
        bind_port=0,
    )
    await guard.ensure_started()
    proc, service = await _start_aria2(
        tmp_path,
        extra_args=(f"--ca-certificate={ca_cert}",),
    )

    async def validated_as_public(uri: str) -> str:
        return uri

    monkeypatch.setattr(runtime_guard, "validate_resolved_public_destination", validated_as_public)
    monkeypatch.setattr(runtime_guard, "downloader_egress_guard", guard)
    guarded = Aria2Executor(service, Aria2Configuration(str(tmp_path)), AsyncMock(return_value=True), egress=guard)
    uri = f"https://sni.test:{target_port}/payload.bin"
    try:
        gid = await _start_transfer(guarded, uri, tmp_path / 'tls.bin')
        status = await _wait_status(service, gid)
        assert status["status"] == "complete", status
        assert (tmp_path / "tls.bin").read_bytes() == b"tls-path-ok"
        assert seen_hosts == ["sni.test"]
        assert state["connections"] == 1
        assert state["sni"] == ["sni.test"]
        assert len(state["hosts"]) == 1
        assert state["hosts"][0].casefold().startswith("sni.test")
    finally:
        await _stop_aria2(proc, service)
        await guard.stop()
        target.close()
        await target.wait_closed()


async def test_mixed_public_private_connection_time_answers_are_blocked() -> None:
    async def resolver(host: str, port: int):
        return [_answer("93.184.216.34", port), _answer("127.0.0.1", port)]

    guard = DownloaderEgressGuard(
        resolver=resolver,
        bind_port=0,
    )
    endpoints = None
    with pytest.raises(ValueError, match="non-public"):
        endpoints = await guard._approved_endpoints("mixed.test", 443)
    assert endpoints is None


async def test_literal_private_connection_target_is_blocked() -> None:
    guard = DownloaderEgressGuard(bind_port=0)
    with pytest.raises(ValueError, match="not public"):
        await guard._approved_endpoints("127.0.0.1", 80)


@pytest.mark.parametrize(
    ("content_type", "name", "body"),
    [
        (
            "application/x-bittorrent",
            "metadata.torrent",
            b"d4:infod6:lengthi1e4:name1:xee",
        ),
        (
            "application/metalink4+xml",
            "metadata.meta4",
            (
                b'<?xml version="1.0" encoding="UTF-8"?>'
                b'<metalink xmlns="urn:ietf:params:xml:ns:metalink">'
                b'<file name="child.bin"><size>1</size>'
                b'<url>http://127.0.0.1:9/child.bin</url></file></metalink>'
            ),
        ),
    ],
)
async def test_real_aria2_does_not_follow_http_metadata(tmp_path: Path, content_type: str, name: str, body: bytes, monkeypatch) -> None:
    server, port, _state = await _start_http_server(body, content_type)
    proc, service = await _start_aria2(tmp_path)
    async def resolver(host, port): return [_answer("127.0.0.1", port)]
    async def validated(uri): return uri
    monkeypatch.setattr(runtime_guard, "validate_resolved_public_destination", validated)
    guard = DownloaderEgressGuard(resolver=resolver, public_check=lambda address: address == "127.0.0.1", bind_port=0)
    executor = Aria2Executor(service, Aria2Configuration(str(tmp_path)), AsyncMock(return_value=True), egress=guard)
    try:
        gid = await _start_transfer(executor, f"http://metadata.test:{port}/{name}", tmp_path / name)
        status = await _wait_status(service, gid)
        assert status["status"] == "complete", status
        assert not status.get("followedBy")
        stopped = await service._call("aria2.tellStopped", [0, 20, ["gid", "followedBy"]])
        assert [item["gid"] for item in stopped] == [gid]
        assert all(not item.get("followedBy") for item in stopped)
    finally:
        await _stop_aria2(proc, service)
        await guard.stop()
        server.close()
        await server.wait_closed()


async def _start_transfer(executor, uri, target):
    request = file_request(TransferCandidate(target.name, (Endpoint(urlsplit(uri).scheme, uri),)), str(target), new_identity())
    handle = executor.prepare(request)
    observation = await executor.start(request, handle)
    assert observation.error is None, observation.error
    return handle.native["gid"]


# ── 1.0.13: the expanded transport claims run through this same boundary ─────

async def _guard_connect_authorities(
    tmp_path: Path, uri: str, monkeypatch, *, extra_args: tuple[str, ...] = ()
) -> list[tuple[str, int]]:
    """Drive a real aria2 job and report every authority it asked to tunnel to.

    The guard's injected resolver runs only after a CONNECT arrived AND its
    target-scoped credential verified, so a recorded authority simultaneously
    proves the job was proxied, that aria2 and the guard agree on the scheme's
    default port, and that the HMAC was scoped to that exact authority.
    """
    seen: list[tuple[str, int]] = []

    async def resolver(host: str, port: int):
        seen.append((host, port))
        return [_answer("127.0.0.1", port)]

    async def validated(address): return address

    monkeypatch.setattr(runtime_guard, "validate_resolved_public_destination", validated)
    proc, service = await _start_aria2(tmp_path, extra_args=extra_args)
    guard = DownloaderEgressGuard(
        resolver=resolver, public_check=lambda address: address == "127.0.0.1",
        bind_port=0,
    )
    executor = Aria2Executor(
        service, Aria2Configuration(str(tmp_path)),
        AsyncMock(return_value=True), egress=guard,
    )
    try:
        gid = await _start_transfer(executor, uri, tmp_path / "payload.bin")
        await _wait_status(service, gid)
    finally:
        await _stop_aria2(proc, service)
        await guard.stop()
    return seen


@pytest.mark.parametrize("scheme,port", [("ftp", 21), ("sftp", 22)])
async def test_real_aria2_tunnels_the_new_transports_through_the_guard(
    tmp_path: Path, monkeypatch, scheme: str, port: int
) -> None:
    seen = await _guard_connect_authorities(
        tmp_path, f"{scheme}://delivery.test/payload.bin", monkeypatch)
    assert seen == [("delivery.test", port)]


@pytest.mark.parametrize("scheme,port", [("http", 80), ("https", 443)])
async def test_real_aria2_still_tunnels_http_transports_through_the_guard(
    tmp_path: Path, monkeypatch, scheme: str, port: int
) -> None:
    seen = await _guard_connect_authorities(
        tmp_path, f"{scheme}://delivery.test/payload.bin", monkeypatch)
    assert seen == [("delivery.test", port)]


@pytest.mark.parametrize("preference", ["--ftp-proxy", "--http-proxy", "--https-proxy"])
async def test_a_shared_daemon_per_protocol_proxy_cannot_route_around_the_guard(
    tmp_path: Path, monkeypatch, preference: str
) -> None:
    """aria2 resolves a per-protocol proxy preference ahead of --all-proxy.

    A daemon started with one of these would otherwise carry that
    protocol around the guard entirely, so every one is pinned per job.
    """
    rogue = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
    rogue_port = int(rogue.sockets[0].getsockname()[1])
    scheme = {"--ftp-proxy": "ftp", "--http-proxy": "http", "--https-proxy": "https"}[preference]
    expected = {"ftp": 21, "http": 80, "https": 443}[scheme]
    try:
        seen = await _guard_connect_authorities(
            tmp_path, f"{scheme}://delivery.test/payload.bin", monkeypatch,
            extra_args=(f"{preference}=http://127.0.0.1:{rogue_port}",),
        )
    finally:
        rogue.close()
        await rogue.wait_closed()
    assert seen == [("delivery.test", expected)]
