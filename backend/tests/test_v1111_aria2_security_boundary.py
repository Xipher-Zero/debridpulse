from __future__ import annotations

import asyncio
import shutil
import socket
import ssl
import subprocess
from pathlib import Path

import pytest

from services.aria2 import Aria2Service
from services.downloader_egress_guard import DownloaderEgressGuard
from services.manager_v2 import TorrentManager
import services.transfer_runtime_guard as runtime_guard

pytestmark = pytest.mark.asyncio


def _answer(address: str, port: int) -> tuple:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)


async def _start_http_server(body: bytes = b"ok", content_type: str = "application/octet-stream"):
    state = {"connections": 0}

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        state["connections"] += 1
        try:
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=3)
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
    for _ in range(80):
        if proc.returncode is not None:
            stdout, stderr = await proc.communicate()
            raise AssertionError(f"aria2c exited early: {stdout!r} {stderr!r}")
        try:
            await service.test()
            return proc, service
        except Exception as exc:  # pragma: no cover - transient startup only
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


async def _wait_status(service: Aria2Service, gid: str, terminal=("complete", "error", "removed")):
    last = None
    for _ in range(160):
        try:
            result = await service._call(
                "aria2.tellStatus",
                [gid, ["gid", "status", "followedBy", "errorCode", "errorMessage"]],
            )
            last = result
            if str(result.get("status") or "") in terminal:
                return result
        except Exception as exc:  # pragma: no cover - transient RPC state
            last = exc
        await asyncio.sleep(0.05)
    raise AssertionError(f"aria2 job did not reach terminal state: {last}")


async def test_canonical_job_options_disable_metadata_following() -> None:
    manager = TorrentManager()
    options = manager._aria2_job_options({"dir": "/download", "out": "payload.bin"})
    assert options["follow-torrent"] == "false"
    assert options["follow-metalink"] == "false"
    assert options["max-http-redirection"] == "0"


async def test_external_mode_fails_closed_without_guard_route(monkeypatch) -> None:
    guard = DownloaderEgressGuard(bind_host="127.0.0.1", bind_port=0)
    await guard.ensure_started()
    try:
        monkeypatch.delenv("DEBRIDPULSE_EXTERNAL_ARIA2_EGRESS_PROXY", raising=False)
        with pytest.raises(RuntimeError, match="fail-closed"):
            guard.job_options("https://provider.example/file.bin", external=True)
    finally:
        await guard.stop()


async def test_guarded_options_override_shared_daemon_proxy_bypasses(monkeypatch) -> None:
    guard = DownloaderEgressGuard(bind_host="127.0.0.1", bind_port=0)
    await guard.ensure_started()
    try:
        advertised = f"http://127.0.0.1:{guard.bound_port}"
        monkeypatch.setenv("DEBRIDPULSE_EXTERNAL_ARIA2_EGRESS_PROXY", advertised)
        options = guard.job_options("https://provider.example/file.bin", external=True)
        assert options["all-proxy"] == advertised
        assert options["http-proxy"] == advertised
        assert options["https-proxy"] == advertised
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
        bind_host="127.0.0.1",
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
    monkeypatch.setattr(runtime_guard, "is_builtin_mode", lambda: True)
    guarded = runtime_guard.GuardedTransferIntegrityAria2Service(
        service.url, service.secret, 3
    )
    uri = f"http://rebind.test:{target_port}/payload.bin"
    try:
        gid = await guarded.ensure_download(
            uri,
            {
                "dir": str(tmp_path),
                "out": "blocked.bin",
                "max-tries": "1",
                "connect-timeout": "2",
                "timeout": "2",
            },
            max_retries=1,
            cached_downloads=[],
        )
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
        bind_host="127.0.0.1",
        bind_port=0,
    )
    await guard.ensure_started()
    proc, service = await _start_aria2(tmp_path)

    async def validated_as_public(uri: str) -> str:
        return uri

    monkeypatch.setattr(runtime_guard, "validate_resolved_public_destination", validated_as_public)
    monkeypatch.setattr(runtime_guard, "downloader_egress_guard", guard)
    monkeypatch.setattr(runtime_guard, "is_builtin_mode", lambda: True)
    guarded = runtime_guard.GuardedTransferIntegrityAria2Service(
        service.url, service.secret, 3
    )
    uri = f"http://public.test:{target_port}/payload.bin"
    try:
        gid = await guarded.ensure_download(
            uri,
            {"dir": str(tmp_path), "out": "public.bin", "max-tries": "1"},
            max_retries=1,
            cached_downloads=[],
        )
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
        bind_host="127.0.0.1",
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
    monkeypatch.setattr(runtime_guard, "is_builtin_mode", lambda: True)
    guarded = runtime_guard.GuardedTransferIntegrityAria2Service(
        service.url, service.secret, 3
    )
    uri = f"https://sni.test:{target_port}/payload.bin"
    try:
        gid = await guarded.ensure_download(
            uri,
            {
                "dir": str(tmp_path),
                "out": "tls.bin",
                "max-tries": "1",
            },
            max_retries=1,
            cached_downloads=[],
        )
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
        bind_host="127.0.0.1",
        bind_port=0,
    )
    endpoints = None
    with pytest.raises(ValueError, match="non-public"):
        endpoints = await guard._approved_endpoints("mixed.test", 443)
    assert endpoints is None


async def test_literal_private_connection_target_is_blocked() -> None:
    guard = DownloaderEgressGuard(bind_host="127.0.0.1", bind_port=0)
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
async def test_real_aria2_does_not_follow_http_metadata(
    tmp_path: Path, content_type: str, name: str, body: bytes
) -> None:
    server, port, _state = await _start_http_server(body, content_type)
    proc, service = await _start_aria2(tmp_path)
    manager = TorrentManager()
    options = manager._aria2_job_options(
        {
            "dir": str(tmp_path),
            "out": name,
            "max-tries": "1",
        }
    )
    try:
        gid = await service.ensure_download(
            f"http://127.0.0.1:{port}/{name}",
            options,
            max_retries=1,
            cached_downloads=[],
        )
        status = await _wait_status(service, gid)
        assert status["status"] == "complete"
        assert not status.get("followedBy")
        stopped = await service._call("aria2.tellStopped", [0, 20, ["gid", "followedBy"]])
        assert [item["gid"] for item in stopped] == [gid]
        assert all(not item.get("followedBy") for item in stopped)
    finally:
        await _stop_aria2(proc, service)
        server.close()
        await server.wait_closed()
