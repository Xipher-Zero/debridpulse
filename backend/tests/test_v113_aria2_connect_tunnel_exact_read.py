"""aria2 keeps every byte a tunnelled server sends after the CONNECT response.

aria2 1.37.0 read a CONNECT response with one buffered recv and discarded
whatever followed its header, so a server that speaks first -- an FTP greeting,
an SSH banner -- lost those bytes whenever they arrived in the same read, and
the transfer timed out waiting for them. The image ships Debian's aria2 rebuilt
with ``packaging/aria2/connect-tunnel-exact-read.patch``.

The CONNECT proxy here is a test instrument, not DebridPulse's egress guard:
for the server-first case it waits until the destination has spoken and writes
the ``200`` and those first bytes in ONE write, so the same-read condition is
forced deterministically instead of depending on scheduler timing. The guard
itself never loses these bytes (it relays them after its own ``200``).
"""
from __future__ import annotations

import asyncio

import pytest

from test_v113_egress_guard_route_scope import _COMMON, FtpOrigin
from test_v1111_aria2_security_boundary import _start_aria2, _stop_aria2, _wait_status

pytestmark = [pytest.mark.real_runtime, pytest.mark.asyncio]

PAYLOAD = b"tunnelled payload\x00\xff" * 64
ESTABLISHED = b"HTTP/1.1 200 Connection Established\r\n\r\n"
FIRST_BYTES_TIMEOUT_SECONDS = 90.0


class TunnelProxy:
    """Plain CONNECT proxy. For a destination port in ``coalesce`` it answers
    only once the destination has spoken, in one write together with those
    bytes; ``refuse`` answers every CONNECT with a non-2xx response followed
    by bytes that must never be taken as tunnelled data."""

    def __init__(self, *, coalesce=(), refuse: bytes | None = None):
        self.coalesce = set(coalesce)
        self.refuse = refuse
        self.first_writes: list[bytes] = []
        self.server = None
        self.port = 0

    async def start(self):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = int(self.server.sockets[0].getsockname()[1])
        return self

    async def close(self):
        self.server.close()
        await self.server.wait_closed()

    @property
    def options(self) -> dict:
        return {"all-proxy": f"http://127.0.0.1:{self.port}", "proxy-method": "tunnel", "no-proxy": ""}

    async def _client(self, reader, writer):
        upstream_writer = None
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            target = request.split(b" ", 2)[1].decode()
            port = int(target.rsplit(":", 1)[1])
            if self.refuse is not None:
                writer.write(self.refuse)
                self.first_writes.append(self.refuse)
                await writer.drain()
                return
            upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", port)
            first = ESTABLISHED
            if port in self.coalesce:
                first += await asyncio.wait_for(upstream_reader.read(65536), FIRST_BYTES_TIMEOUT_SECONDS)
            self.first_writes.append(first)
            writer.write(first)
            await writer.drain()

            async def relay(source, destination):
                while chunk := await source.read(65536):
                    destination.write(chunk)
                    await destination.drain()
                destination.close()

            await asyncio.gather(relay(reader, upstream_writer), relay(upstream_reader, writer),
                                 return_exceptions=True)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
            pass
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
            writer.close()


SSH_MSG_DISCONNECT = 1
SSH_MSG_KEXINIT = 20


class BannerOrigin:
    """A server-first TCP destination that is not FTP: it sends an SSH
    identification line at once and records the first packet the client sends
    after its own identification line. A client that READ our identification
    begins key exchange (``SSH_MSG_KEXINIT``); one that never saw it waits and
    eventually gives up (``SSH_MSG_DISCONNECT``)."""

    def __init__(self):
        self.received = bytearray()
        self.first_message: int | None = None
        self.answered = asyncio.Event()
        self.server = None
        self.port = 0

    async def start(self):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = int(self.server.sockets[0].getsockname()[1])
        return self

    async def close(self):
        self.server.close()
        await self.server.wait_closed()

    async def _client(self, reader, writer):
        writer.write(b"SSH-2.0-dp-tunnel-test\r\n")
        await writer.drain()
        try:
            while chunk := await reader.read(65536):
                self.received.extend(chunk)
                line, sep, after = bytes(self.received).partition(b"\r\n")
                # uint32 packet length, byte padding length, byte message type.
                if sep and line.startswith(b"SSH-2.0-") and len(after) >= 6:
                    self.first_message = after[5]
                    self.answered.set()
                    return
        except ConnectionError:
            pass
        finally:
            writer.close()


async def _aria2(tmp_path, uri: str, options: dict, out: str):
    proc, service = await _start_aria2(tmp_path)
    try:
        gid = await service._call("aria2.addUri", [[uri], {**_COMMON, **options, "dir": str(tmp_path), "out": out}])
        return await _wait_status(service, gid)
    finally:
        await _stop_aria2(proc, service)


async def test_an_ftp_greeting_in_the_same_read_as_the_connect_response_is_kept(tmp_path) -> None:
    origin = await FtpOrigin({"/pub/file.bin": PAYLOAD}).start()
    proxy = await TunnelProxy(coalesce={origin.port}).start()
    try:
        status = await _aria2(tmp_path, f"ftp://127.0.0.1:{origin.port}/pub/file.bin",
                              {**proxy.options, "ftp-pasv": "true"}, "same-read.bin")
    finally:
        await proxy.close()
        await origin.close()

    # The greeting really did arrive in the same write as the 200.
    assert proxy.first_writes[0].startswith(ESTABLISHED + b"220 ")
    assert status["status"] == "complete", status
    assert (tmp_path / "same-read.bin").read_bytes() == PAYLOAD
    # Read exactly once: one greeting answered by exactly one login.
    assert origin.logins == [("anonymous", True)]
    assert origin.control_connections == 1


async def test_an_ordinarily_relayed_ftp_greeting_is_kept_whenever_it_arrives(tmp_path) -> None:
    """The ordinary path: the proxy answers at once and relays the greeting
    whenever the destination sends it -- after the 200 in a later read, or in
    the same read when the client is slow to read (the race this fixes)."""
    origin = await FtpOrigin({"/pub/file.bin": PAYLOAD}).start()
    proxy = await TunnelProxy().start()
    try:
        status = await _aria2(tmp_path, f"ftp://127.0.0.1:{origin.port}/pub/file.bin",
                              {**proxy.options, "ftp-pasv": "true"}, "later.bin")
    finally:
        await proxy.close()
        await origin.close()

    assert proxy.first_writes[0] == ESTABLISHED
    assert status["status"] == "complete", status
    assert (tmp_path / "later.bin").read_bytes() == PAYLOAD
    assert origin.logins == [("anonymous", True)]


async def test_non_ftp_server_first_bytes_in_the_same_read_reach_the_tunnelled_client(tmp_path) -> None:
    """Protocol-neutral: the SFTP tunnel takes the same CONNECT path, and its
    client answers our identification with a key exchange only if it read it."""
    origin = await BannerOrigin().start()
    proxy = await TunnelProxy(coalesce={origin.port}).start()
    try:
        proc, service = await _start_aria2(tmp_path)
        try:
            await service._call("aria2.addUri", [[f"sftp://127.0.0.1:{origin.port}/file.bin"],
                                                 {**_COMMON, **proxy.options, "dir": str(tmp_path),
                                                  "out": "banner.bin", "ftp-user": "u", "ftp-passwd": "p"}])
            await asyncio.wait_for(origin.answered.wait(), FIRST_BYTES_TIMEOUT_SECONDS)
        finally:
            await _stop_aria2(proc, service)
    finally:
        await proxy.close()
        await origin.close()

    assert proxy.first_writes[0] == ESTABLISHED + b"SSH-2.0-dp-tunnel-test\r\n"
    # Key exchange, not a give-up: the identification that arrived in the same
    # read as the 200 was read by the tunnelled client.
    assert origin.first_message == SSH_MSG_KEXINIT, origin.first_message


async def test_a_refused_connect_tunnels_nothing(tmp_path) -> None:
    origin = await FtpOrigin({"/pub/file.bin": PAYLOAD}).start()
    proxy = await TunnelProxy(refuse=b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n220 not a tunnel\r\n").start()
    try:
        status = await _aria2(tmp_path, f"ftp://127.0.0.1:{origin.port}/pub/file.bin",
                              {**proxy.options, "ftp-pasv": "true"}, "refused.bin")
    finally:
        await proxy.close()
        await origin.close()

    assert status["status"] == "error", status
    assert origin.control_connections == 0 and origin.logins == []
    assert not (tmp_path / "refused.bin").exists()
