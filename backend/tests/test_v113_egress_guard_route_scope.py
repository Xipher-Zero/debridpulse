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
import socket
import time
from pathlib import Path

import pytest

from services.downloader_egress_guard import DownloaderEgressGuard, RouteScope, TunnelTargetTimeout
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
    """RFC 959 subset aria2 uses: USER PASS TYPE PWD CWD SIZE MDTM EPSV PASV REST RETR QUIT,
    plus the listing commands NLST and (RFC 3659) MLSD.

    Like a real server it converts LF to CRLF on an ASCII-type (TYPE A)
    retrieval, refuses active mode (PORT/EPRT) -- both are recorded -- and
    changes only into a directory that exists (a file is not a directory).
    ``mlsd=False`` models a server without MLSD (vsftpd).
    """

    def __init__(self, files: dict[str, bytes], *, users: dict[str, str] | None = None, anonymous: bool = True,
                 host: str = "127.0.0.1", rest: bool = True, pace: float = 0.0, mlsd: bool = True):
        self.files = files
        self.mlsd = mlsd
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

    def _directories(self) -> set[str]:
        found = {"/"}
        for path in self.files:
            parts = path.strip("/").split("/")[:-1]
            for index in range(1, len(parts) + 1):
                found.add("/" + "/".join(parts[:index]))
        return found

    def _children(self, directory: str) -> list[tuple[str, str, int]]:
        """``(kind, name, size)`` of the immediate entries of ``directory``."""
        prefix = directory.rstrip("/") + "/"
        seen = {}
        for path, body in self.files.items():
            if not path.startswith(prefix):
                continue
            head, _, rest = path[len(prefix):].partition("/")
            seen[head] = ("dir", head, 0) if rest else ("file", head, len(body))
        return sorted(seen.values(), key=lambda item: item[1])

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
                    target = argument if argument.startswith("/") else cwd.rstrip("/") + "/" + argument
                    target = "/" + target.strip("/") if target.strip("/") else "/"
                    if target in self._directories():
                        cwd = target
                        send("250 ok")
                    else:
                        send("550 not a directory")
                elif command in {"NLST", "MLSD"}:
                    listed = argument if argument.startswith("/") else (cwd.rstrip("/") + "/" + argument if argument else cwd)
                    if command == "MLSD" and not self.mlsd:
                        send("500 MLSD not understood")
                    elif listed not in self._directories():
                        send("550 not a directory")
                    else:
                        send("150 here comes the listing")
                        await writer.drain()
                        data_reader, data_writer = await asyncio.wait_for(data_ready, timeout=DATA_CHANNEL_TIMEOUT_SECONDS)
                        lines = [f"type={kind};size={size}; {name}" if command == "MLSD" else name
                                 for kind, name, size in self._children(listed)]
                        data_writer.write("".join(line + "\r\n" for line in lines).encode())
                        await data_writer.drain()
                        data_writer.close()
                        send("226 listing sent")
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

@pytest.mark.real_runtime
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

@pytest.mark.real_runtime
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


@pytest.mark.real_runtime
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
        # Its own authority passes authentication (then reaches no origin: one
        # that is not listening refuses -> 502, any other failure -> 403;
        # either proves it got past 407).
        assert _status(await _connect(guard, f"files.test:{port}", *_credential(options))) in {200, 403, 502}
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


# ── the guard's own connection is bounded by the route's Connection Timeout ───

class BlackHole:
    """A real loopback port that never answers: its one-slot accept queue is
    full, so the kernel silently drops every further connection attempt --
    exactly what a firewall that drops a port looks like to a client."""

    def __init__(self, port: int = 0):
        self.requested = port

    def __enter__(self) -> "BlackHole":
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", self.requested))
        self.listener.listen(0)
        self.port = self.listener.getsockname()[1]
        self.filler = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        return self

    def __exit__(self, *_exc) -> None:
        self.filler.close()
        self.listener.close()


def _closed_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


async def test_a_bounded_route_answers_504_when_its_connection_timeout_elapses() -> None:
    guard = _guard()
    await guard.ensure_started()
    try:
        with BlackHole() as hole:
            _host, _port, user, token = guard.proxy_credential(f"rsync://files.test:{hole.port}/m/f",
                                                               connect_timeout_seconds=1.0)
            started = time.monotonic()
            status = _status(await _connect(guard, f"files.test:{hole.port}", user, token))
            elapsed = time.monotonic() - started
        # A destination that never answers is uncertainty (504), never a
        # refusal (502) or a policy fact (403) -- and the guard decides within
        # the route's own bound, not the kernel's.
        assert status == 504 and 0.9 <= elapsed < 5.0
    finally:
        await guard.stop()


async def test_an_in_process_tunnel_is_bounded_by_its_own_timeout() -> None:
    guard = _guard()
    await guard.ensure_started()
    try:
        with BlackHole() as hole:
            started = time.monotonic()
            # The guard's 504 (TunnelTargetTimeout) and the tunnel's own timer
            # share one bound; whichever reports first, it is the TimeoutError
            # every consumer already handles, within that bound.
            with pytest.raises(TimeoutError):
                await guard.open_tunnel(f"sftp://files.test:{hole.port}/f", timeout_seconds=1.0)
            assert time.monotonic() - started < 5.0
        assert issubclass(TunnelTargetTimeout, TimeoutError)
    finally:
        await guard.stop()


async def test_the_bound_is_signed_optional_and_never_changes_refusal_or_policy() -> None:
    guard = _guard()
    await guard.ensure_started()
    try:
        with BlackHole() as hole:
            uri = f"rsync://files.test:{hole.port}/m/f"
            _h, _p, user, bounded = guard.proxy_credential(uri, connect_timeout_seconds=1.0)
            signed, _dot, bound = bounded.partition(".")
            assert bound == "1000"
            # A bound cannot be altered: the credential is refused outright.
            for forged in (f"{signed}.600000", f"{signed}.", f"{signed}.x"):
                assert _status(await _connect(guard, f"files.test:{hole.port}", user, forged)) == 407
        # A route issued without a bound keeps today's exact credential, and
        # an aria2 job's credential names no bound.
        _h, _p, _user, unbounded = guard.proxy_credential("rsync://files.test:873/m/f")
        options = guard.job_options("https://files.test/f.bin")
        assert "." not in unbounded and "." not in options["all-proxy-passwd"]
        # Refusal and policy keep their own answers on a bounded route.
        refused = _closed_port()
        _h, _p, user, token = guard.proxy_credential(f"rsync://files.test:{refused}/m/f", connect_timeout_seconds=5.0)
        assert _status(await _connect(guard, f"files.test:{refused}", user, token)) == 502
    finally:
        await guard.stop()
    blocked = _guard(public=())
    await blocked.ensure_started()
    try:
        _h, _p, user, token = blocked.proxy_credential("rsync://files.test:873/m/f", connect_timeout_seconds=5.0)
        assert _status(await _connect(blocked, "files.test:873", user, token)) == 403
    finally:
        await blocked.stop()


async def test_existing_in_process_consumers_still_report_a_silent_destination_as_their_timeout() -> None:
    """The FTP and SFTP evidence readers (aria2's sampling and discovery path)
    open tunnels with their own timeout; a destination that never answers is
    the same ``timeout`` they reported before the guard was bounded."""
    from services.artifact_sampling import ftp_fingerprint, sftp_fingerprint
    from transfers.models import FingerprintKind

    guard = _guard()
    await guard.ensure_started()
    try:
        with BlackHole() as hole:
            ftp = f"ftp://files.test:{hole.port}/pub/f.bin"
            sftp = f"sftp://files.test:{hole.port}/pub/f.bin"
            outcomes = [
                await ftp_fingerprint(ftp, username="", password="", timeout_seconds=5,
                                      connect=lambda port=None: guard.open_tunnel(ftp, timeout_seconds=1.0,
                                                                                  scope=RouteScope.SAME_HOST,
                                                                                  port=port)),
                await sftp_fingerprint(sftp, host_key_algorithms=("ssh-ed25519",), timeout_seconds=5,
                                       connect=lambda port=None: guard.open_tunnel(sftp, timeout_seconds=1.0)),
            ]
        assert [(kind, reason) for _total, _sig, kind, reason, _prefix in outcomes] == [
            (FingerprintKind.UNAVAILABLE, "timeout")] * 2
    finally:
        await guard.stop()


@pytest.mark.real_runtime
@pytest.mark.parametrize("scheme", ["http", "https", "ftp", "sftp"])
async def test_aria2_reports_a_guard_timeout_or_refusal_exactly_like_a_policy_refusal(tmp_path, scheme) -> None:
    """The guard's distinct answers (502 refused, 504 timed out) never change
    what a real aria2 job reports or how DebridPulse classifies it: aria2
    sees every non-200 CONNECT answer as the same proxy failure."""
    from executors.aria2.translation import native_failure

    verdicts = {}
    for status in ("403 Forbidden", "502 Bad Gateway", "504 Gateway Timeout"):
        async def answer(reader, writer, status=status):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(f"HTTP/1.1 {status}\r\nConnection: close\r\n\r\n".encode())
            await writer.drain()
            writer.close()

        stub = await asyncio.start_server(answer, "127.0.0.1", 0)
        proxy = f"http://127.0.0.1:{stub.sockets[0].getsockname()[1]}"
        try:
            result = await _aria2_ftp(tmp_path, f"{scheme}://files.test/f.bin",
                                      {"all-proxy": proxy, "proxy-method": "tunnel", "no-proxy": ""}, "f.bin")
        finally:
            stub.close()
            await stub.wait_closed()
        error = native_failure(result.get("errorCode"), result.get("errorMessage"))
        verdicts[status] = (result.get("status"), result.get("errorCode"), result.get("errorMessage"),
                            error.domain, error.category, error.retryability)
    assert len(set(verdicts.values())) == 1, verdicts
    assert next(iter(verdicts.values()))[0] == "error"


# ── one download budget: an aggregate rate, enforced by DebridPulse ──────────

async def test_a_download_budget_paces_every_consumer_together_and_follows_its_rate_live() -> None:
    from services.downloader_egress_guard import EgressBudget

    budget = EgressBudget()
    chunk = 64 * 1024
    delivered: list[tuple[float, int]] = []

    async def consumer(until: float) -> None:
        while time.monotonic() < until:
            await budget.consume(chunk)
            delivered.append((time.monotonic(), chunk))

    def rate(since: float, until: float) -> float:
        return sum(size for at, size in delivered if since <= at < until) / (until - since)

    # Unlimited (0) never waits.
    started = time.monotonic()
    for _ in range(64):
        await budget.consume(chunk)
    assert time.monotonic() - started < 0.5
    # Three consumers share one 1 MiB/s budget -- together, never each.
    budget.set_rate(1024 * 1024)
    started = time.monotonic()
    await asyncio.gather(*(consumer(started + 2.0) for _ in range(3)))
    assert rate(started, started + 2.0) <= 1024 * 1024 * 1.05 + chunk / 2.0
    # A new rate applies to consumers already running.
    delivered.clear()
    budget.set_rate(2 * 1024 * 1024)
    started = time.monotonic()
    await asyncio.gather(*(consumer(started + 2.0) for _ in range(3)))
    measured = rate(started, started + 2.0)
    assert 1.5 * 1024 * 1024 <= measured <= 2 * 1024 * 1024 * 1.05 + chunk / 2.0


async def test_only_a_route_that_names_a_budget_draws_on_it_and_the_name_is_signed() -> None:
    guard = _guard()
    await guard.ensure_started()
    try:
        _h, _p, user, budgeted = guard.proxy_credential("rsync://files.test:873/m/f", budget="rsync")
        assert budgeted.endswith(".0.rsync")
        signed = budgeted.split(".")[0]
        with BlackHole() as hole:
            _h, _p, user, other = guard.proxy_credential(f"rsync://files.test:{hole.port}/m/f", budget="rsync")
            forged = other.rsplit(".", 1)[0] + ".aria2"
            assert _status(await _connect(guard, f"files.test:{hole.port}", user, forged)) == 407
        assert "." not in guard.job_options("https://files.test/f.bin")["all-proxy-passwd"]
        assert guard.job_options("https://files.test/f.bin", budget="aria2")["all-proxy-passwd"].endswith(".0.aria2")
        with pytest.raises(ValueError):
            guard.budget("Not A Name")
        assert signed and guard.budget("rsync") is guard.budget("rsync")
    finally:
        await guard.stop()
