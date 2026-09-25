"""1.0.13: one neutral signed route scope on the one downloader egress guard.

Characterization of the packaged aria2 1.37.0 proved that a passive FTP job
tunnels its data connection through the guard as ``CONNECT <same host>:<server
selected port>`` with the job's proxy credential. An exact ``host:port``
credential therefore rejects the data channel with 407 and no FTP file can
transfer. The guard gains one generalization, not a second guard:

* ``RouteScope.ENDPOINT`` -- the unchanged default: exactly the authorized
  hostname and port (HTTP, HTTPS, SFTP and every existing caller);
* ``RouteScope.SAME_HOST`` -- the authorized hostname on the authorized port
  plus server-selected unprivileged ports, for native multi-connection
  transports chosen at the executor boundary.

Every CONNECT, whatever its scope, still passes guard-owned DNS resolution,
global-address validation and mixed/private/rebinding rejection.
"""
from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest

from services.downloader_egress_guard import DownloaderEgressGuard, RouteScope
from test_v1111_aria2_security_boundary import _answer, _start_aria2, _stop_aria2, _wait_status

pytestmark = pytest.mark.asyncio

# Hang guards for this scaffolding, in wall-clock seconds, ordered as a ladder so
# that the component under test always decides first and this file only ever
# reports that verdict: aria2's own connect/read timeouts are 60 s, so the origin
# waits longer than that before abandoning a peer, and ``_wait_status`` (in
# ``test_v1111_aria2_security_boundary``) waits longer still. A 10 s origin
# timeout inverted that ladder under runner load -- the origin dropped a control
# connection aria2 was still driving and the failure read "Got EOF from the
# server" for a healthy 11-byte loopback transfer.
ORIGIN_IDLE_TIMEOUT_SECONDS = 90.0
DATA_CHANNEL_TIMEOUT_SECONDS = 90.0
PROBE_RESPONSE_TIMEOUT_SECONDS = 60.0


# ── A minimal in-process passive FTP origin (no third-party server needed) ────

class FtpOrigin:
    """RFC 959 subset aria2 uses: USER PASS TYPE PWD CWD SIZE MDTM EPSV PASV REST RETR QUIT.

    Like a real server it converts LF to CRLF on an ASCII-type (TYPE A)
    retrieval, and it refuses active mode (PORT/EPRT); both are recorded.
    """

    def __init__(self, files: dict[str, bytes], *, users: dict[str, str] | None = None, anonymous: bool = True,
                 host: str = "127.0.0.1", rest: bool = True, pace: float = 0.0):
        self.files = files
        self.users = dict(users or {})
        self.anonymous = anonymous
        self.host = host
        self.rest = rest
        # Seconds between 16 KiB data chunks; 0 writes the payload at once.
        self.pace = pace
        self.rest_offsets: list[int] = []
        self._live: list = []
        self.logins: list[tuple[str, bool]] = []
        self.data_connections = 0
        self.retrieved: list[str] = []
        self.control_connections = 0
        self.transfer_types: list[str] = []
        self.data_modes: list[str] = []
        self.server = None
        self.port = 0

    async def start(self):
        self.server = await asyncio.start_server(self._control, self.host, 0)
        self.port = int(self.server.sockets[0].getsockname()[1])
        return self

    async def close(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def abort(self):
        """Stop listening AND sever every live control/data connection."""
        if self.server is not None:
            self.server.close()
        for writer in self._live:
            writer.transport.abort()
        self._live.clear()

    async def _control(self, reader, writer):
        self.control_connections += 1
        self._live.append(writer)
        user, authed, cwd, data_server, data_ready, ascii_type = "", False, "/", None, None, False
        offset = 0
        # A passive data listener lives on the address the client reached, as
        # on a real multi-homed server.
        local_address = writer.get_extra_info("sockname")[0]

        def send(line: str):
            writer.write((line + "\r\n").encode())

        send("220 dp-test ftp")
        try:
            while True:
                raw = await asyncio.wait_for(reader.readline(), timeout=ORIGIN_IDLE_TIMEOUT_SECONDS)
                if not raw:
                    return
                command, _, argument = raw.decode("latin-1").strip().partition(" ")
                command = command.upper()
                if command == "USER":
                    user = argument
                    send("331 password required")
                elif command == "PASS":
                    valid = (self.anonymous and user == "anonymous") or self.users.get(user) == argument
                    self.logins.append((user, valid))
                    authed = valid
                    send("230 logged in" if valid else "530 Login incorrect.")
                elif not authed:
                    send("530 Please login with USER and PASS.")
                elif command == "TYPE":
                    ascii_type = argument.upper().startswith("A")
                    self.transfer_types.append(argument.upper())
                    send("200 type set")
                elif command == "PWD":
                    send('257 "/" is current directory')
                elif command == "CWD":
                    cwd = argument if argument.startswith("/") else cwd.rstrip("/") + "/" + argument
                    send("250 ok")
                elif command in {"SIZE", "MDTM", "RETR"}:
                    path = argument if argument.startswith("/") else cwd.rstrip("/") + "/" + argument
                    if path not in self.files:
                        send("550 not found")
                    elif command == "SIZE":
                        send(f"213 {len(self.files[path])}")
                    elif command == "MDTM":
                        send("213 20260101000000")
                    else:
                        send("150 opening data connection")
                        await writer.drain()
                        data_reader, data_writer = await asyncio.wait_for(data_ready, timeout=DATA_CHANNEL_TIMEOUT_SECONDS)
                        self.retrieved.append(path)
                        payload = self.files[path]
                        if ascii_type:
                            payload = payload.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                        # REST applies to exactly the next RETR, like RFC 3659.
                        payload, offset = payload[offset:], 0
                        try:
                            step = 16 * 1024 if self.pace else max(1, len(payload))
                            for index in range(0, len(payload), step):
                                data_writer.write(payload[index:index + step])
                                await data_writer.drain()
                                if self.pace:
                                    await asyncio.sleep(self.pace)
                        except ConnectionError:
                            data_writer.close()
                            send("426 transfer aborted")
                        else:
                            data_writer.close()
                            send("226 transfer complete")
                elif command in {"PORT", "EPRT"}:
                    self.data_modes.append("active")
                    send("502 active mode not offered")
                elif command in {"PASV", "EPSV"}:
                    self.data_modes.append("passive")
                    loop = asyncio.get_running_loop()
                    data_ready = loop.create_future()

                    async def accept(r, w, future=data_ready):
                        self.data_connections += 1
                        self._live.append(w)
                        if not future.done():
                            future.set_result((r, w))

                    data_server = await asyncio.start_server(accept, local_address, 0)
                    port = int(data_server.sockets[0].getsockname()[1])
                    if command == "EPSV":
                        send(f"229 Entering Extended Passive Mode (|||{port}|)")
                    else:
                        send(f"227 Entering Passive Mode (127,0,0,1,{port // 256},{port % 256})")
                elif command == "REST":
                    if not self.rest:
                        send("502 REST not implemented")
                    else:
                        offset = int(argument)
                        self.rest_offsets.append(offset)
                        send("350 restarting")
                elif command == "QUIT":
                    send("221 bye")
                    await writer.drain()
                    return
                else:
                    send("502 not implemented")
                await writer.drain()
        except (asyncio.TimeoutError, ConnectionError):
            return
        finally:
            if data_server is not None:
                data_server.close()
            writer.close()


def _guard(seen: list | None = None, *, public=("127.0.0.1",), answers=None) -> DownloaderEgressGuard:
    async def resolver(host: str, port: int):
        if seen is not None:
            seen.append((host, port))
        chosen = answers(host, port) if answers else ["127.0.0.1"]
        return [_answer(address, port) for address in chosen]

    return DownloaderEgressGuard(
        resolver=resolver, public_check=lambda address: address in public,
        bind_port=0,
    )


_COMMON = {
    "allow-overwrite": "true", "auto-file-renaming": "false", "follow-torrent": "false",
    "follow-metalink": "false", "max-tries": "1", "no-netrc": "true", "ftp-reuse-connection": "false",
}


async def _aria2_ftp(tmp_path: Path, uri: str, options: dict, out: str):
    proc, service = await _start_aria2(tmp_path)
    try:
        gid = await service._call("aria2.addUri", [[uri], {**_COMMON, **options, "dir": str(tmp_path), "out": out}])
        return await _wait_status(service, gid)
    finally:
        await _stop_aria2(proc, service)


async def _connect(guard: DownloaderEgressGuard, authority: str, username: str, password: str) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", guard.bound_port)
    credential = base64.b64encode(f"{username}:{password}".encode()).decode()
    writer.write(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\nProxy-Authorization: Basic {credential}\r\n\r\n".encode())
    await writer.drain()
    try:
        return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=PROBE_RESPONSE_TIMEOUT_SECONDS)
    except asyncio.IncompleteReadError as exc:
        return exc.partial
    finally:
        writer.close()


def _status(response: bytes) -> int:
    return int(response.split(b" ", 2)[1]) if response.startswith(b"HTTP/1.1 ") else 0


def _credential(options: dict) -> tuple[str, str]:
    return options["all-proxy-user"], options["all-proxy-passwd"]


# ── 1. RED regression: the exact-endpoint credential cannot carry FTP data ────

async def test_real_aria2_passive_ftp_fails_under_an_exact_endpoint_credential(tmp_path) -> None:
    origin = await FtpOrigin({"/pub/file.bin": b"ftp-payload"}).start()
    seen: list = []
    guard = _guard(seen)
    await guard.ensure_started()
    try:
        uri = f"ftp://files.test:{origin.port}/pub/file.bin"
        status = await _aria2_ftp(tmp_path, uri, guard.job_options(uri), "exact.bin")
        assert status["status"] == "error"
        # The control channel was authorized and reached the origin; the
        # server-selected data CONNECT was refused before any DNS or socket.
        assert seen == [("files.test", origin.port)]
        assert origin.data_connections == 0
        assert not (tmp_path / "exact.bin").exists() or (tmp_path / "exact.bin").read_bytes() == b""
    finally:
        await guard.stop()
        await origin.close()


async def test_exact_endpoint_credential_gets_407_on_a_second_port() -> None:
    guard = _guard()
    await guard.ensure_started()
    try:
        options = guard.job_options("ftp://files.test:2121/f.bin")
        assert _status(await _connect(guard, "files.test:30007", *_credential(options))) == 407
    finally:
        await guard.stop()


# ── 2. GREEN: same-host scope carries control + passive data ─────────────────

@pytest.mark.parametrize("anonymous", [True, False])
async def test_real_aria2_passive_ftp_transfers_through_the_same_host_scope(tmp_path, anonymous) -> None:
    origin = await FtpOrigin({"/pub/file.bin": b"ftp-payload"}, users={"dp": "pw"}, anonymous=anonymous).start()
    seen: list = []
    guard = _guard(seen)
    await guard.ensure_started()
    try:
        uri = f"ftp://files.test:{origin.port}/pub/file.bin"
        options = guard.job_options(uri, scope=RouteScope.SAME_HOST)
        if not anonymous:
            options = {**options, "ftp-user": "dp", "ftp-passwd": "pw"}
        status = await _aria2_ftp(tmp_path, uri, options, "same-host.bin")
        assert status["status"] == "complete", status
        assert (tmp_path / "same-host.bin").read_bytes() == b"ftp-payload"
        assert origin.data_connections == 1
        # Every connection -- control and data -- was resolved by the guard
        # itself, for the one authorized hostname only.
        assert {host for host, _port in seen} == {"files.test"}
        assert len(seen) == 2 and seen[0] == ("files.test", origin.port) and seen[1][1] != origin.port
    finally:
        await guard.stop()
        await origin.close()


# ── 3. The same-host scope never widens beyond the one host ──────────────────

async def test_same_host_credential_rejects_another_hostname() -> None:
    seen: list = []
    guard = _guard(seen)
    await guard.ensure_started()
    try:
        options = guard.job_options("ftp://files.test:2121/f.bin", scope=RouteScope.SAME_HOST)
        for authority in ("other.test:2121", "other.test:30007", "files.test.evil:30007", "127.0.0.1:30007"):
            assert _status(await _connect(guard, authority, *_credential(options))) == 407
        assert seen == []
    finally:
        await guard.stop()


async def test_same_host_credential_rejects_other_privileged_ports() -> None:
    guard = _guard()
    await guard.ensure_started()
    try:
        options = guard.job_options("ftp://files.test/f.bin", scope=RouteScope.SAME_HOST)
        for port in (22, 25, 80, 443, 1023):
            assert _status(await _connect(guard, f"files.test:{port}", *_credential(options))) == 407
    finally:
        await guard.stop()


async def test_same_host_credential_is_not_forgeable_by_editing_its_scope() -> None:
    guard = _guard()
    await guard.ensure_started()
    try:
        options = guard.job_options("ftp://files.test:2121/f.bin", scope=RouteScope.SAME_HOST)
        username, password = _credential(options)
        forged = username.rsplit(".", 1)[0] + ".25"
        assert _status(await _connect(guard, "files.test:25", forged, password)) == 407
        exact = guard.job_options("ftp://files.test:2121/f.bin")
        assert _status(await _connect(guard, "files.test:30007", username, exact["all-proxy-passwd"])) == 407
        assert _status(await _connect(guard, "files.test:30007", "debridpulse", password)) == 407
        assert _status(await _connect(guard, "files.test:30007", username, "")) == 407
    finally:
        await guard.stop()


# ── 4. Guard-owned DNS policy still applies to every scoped CONNECT ──────────

@pytest.mark.parametrize("answers", [
    ["10.0.0.5"],                      # private
    ["127.0.0.2"],                     # local (not the one test-public address)
    ["127.0.0.1", "10.0.0.5"],         # mixed answer set
])
async def test_same_host_data_connect_still_rejects_non_public_resolution(answers) -> None:
    guard = _guard(answers=lambda host, port: answers)
    await guard.ensure_started()
    try:
        options = guard.job_options("ftp://files.test:2121/f.bin", scope=RouteScope.SAME_HOST)
        assert _status(await _connect(guard, "files.test:30007", *_credential(options))) == 403
    finally:
        await guard.stop()


async def test_same_host_rebinding_between_control_and_data_is_rejected(tmp_path) -> None:
    """The control CONNECT resolves public; the data CONNECT rebinds private."""
    origin = await FtpOrigin({"/pub/file.bin": b"ftp-payload"}).start()
    calls: list = []

    def answers(host, port):
        calls.append(port)
        return ["127.0.0.1"] if len(calls) == 1 else ["10.0.0.5"]

    guard = _guard(answers=answers)
    await guard.ensure_started()
    try:
        uri = f"ftp://files.test:{origin.port}/pub/file.bin"
        status = await _aria2_ftp(
            tmp_path, uri, guard.job_options(uri, scope=RouteScope.SAME_HOST), "rebind.bin")
        assert status["status"] == "error"
        assert len(calls) == 2 and origin.data_connections == 0
    finally:
        await guard.stop()
        await origin.close()


async def test_same_host_literal_private_target_is_rejected_before_any_credential() -> None:
    from services.network_safety import UnsafeDestinationError

    guard = _guard()
    await guard.ensure_started()
    try:
        with pytest.raises(UnsafeDestinationError):
            guard.job_options("ftp://127.0.0.1/f.bin", scope=RouteScope.SAME_HOST)
    finally:
        await guard.stop()


# ── 5. Exact-endpoint behavior is unchanged for HTTP/HTTPS/SFTP ──────────────

@pytest.mark.parametrize("scheme,port", [("http", 80), ("https", 443), ("sftp", 22), ("ftp", 21)])
async def test_default_scope_is_the_unchanged_exact_endpoint_credential(scheme, port) -> None:
    guard = _guard()
    await guard.ensure_started()
    try:
        uri = f"{scheme}://files.test/f.bin"
        default = guard.job_options(uri)
        assert default == guard.job_options(uri, scope=RouteScope.ENDPOINT)
        assert default["all-proxy-user"] == "debridpulse"
        assert default["all-proxy-passwd"] == guard._token("files.test", port)
        for family in ("http", "https", "ftp"):
            assert default[f"{family}-proxy-user"] == "debridpulse"
            assert default[f"{family}-proxy-passwd"] == default["all-proxy-passwd"]
    finally:
        await guard.stop()


@pytest.mark.parametrize("scheme,port", [("http", 80), ("https", 443), ("sftp", 22)])
async def test_exact_endpoint_credential_still_admits_only_its_own_authority(scheme, port) -> None:
    seen: list = []
    guard = _guard(seen)
    await guard.ensure_started()
    try:
        options = guard.job_options(f"{scheme}://files.test/f.bin")
        # Its own authority passes authentication (then fails to reach an
        # origin that is not listening -> 403, proving it got past 407).
        assert _status(await _connect(guard, f"files.test:{port}", *_credential(options))) in {200, 403}
        assert seen == [("files.test", port)]
        for other in (f"files.test:{port + 1}", "files.test:30007", f"other.test:{port}"):
            assert _status(await _connect(guard, other, *_credential(options))) == 407
        assert seen == [("files.test", port)]
    finally:
        await guard.stop()


async def test_same_host_scope_pins_every_per_protocol_proxy_to_the_guard() -> None:
    guard = _guard()
    await guard.ensure_started()
    try:
        options = guard.job_options("ftp://files.test/f.bin", scope=RouteScope.SAME_HOST)
        proxy = f"http://127.0.0.1:{guard.bound_port}"
        assert options["all-proxy"] == proxy and options["no-proxy"] == "" and options["proxy-method"] == "tunnel"
        for family in ("http", "https", "ftp"):
            assert options[f"{family}-proxy"] == proxy
            assert options[f"{family}-proxy-user"] == options["all-proxy-user"]
            assert options[f"{family}-proxy-passwd"] == options["all-proxy-passwd"]
        assert options["all-proxy-user"] != "debridpulse"
    finally:
        await guard.stop()
