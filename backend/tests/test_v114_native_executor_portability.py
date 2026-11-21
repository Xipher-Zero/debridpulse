"""Native executors on every host (1.0.14 desktop WS3).

The executors keep one contract wherever DownloadPulse runs; what differs is
the platform's own behaviour, handled at each existing owner:

* rsync's local arguments (``executors.rsync.executor.local_argument``):
  never a ``C:``-style string rsync could read as a remote host;
* the egress guard's tunnel (``open_tunnel``): a socket a CHILD process can
  drive with its own event loop (on Windows a socket the parent's event loop
  has used is bound to the parent's I/O completion port for good);
* rsync over SSH through DownloadPulse's one channel (``services.ssh_channel``):
  the secret, status and tunnel pass through the native rsync to the channel,
  and the transfer completes byte for byte;
* the bundled Usenet service's program and state (``executors.sabnzbd.runtime``):
  beside the backend and beside the database, as in the container.

Nothing is replaced: every case drives the real owner, a real process and,
for rsync, the host's real rsync. What only Windows has is generated only
there; nothing here is ever skipped.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shlex
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from executors.process_ownership import ProcessOwnership
from executors.rsync.executor import local_argument

WINDOWS = os.name == "nt"
BACKEND = Path(__file__).resolve().parents[1]


# ── rsync's local arguments ──────────────────────────────────────────────────

def test_a_local_rsync_argument_is_the_path_itself_on_posix_and_never_a_host_on_windows(tmp_path):
    if not WINDOWS:
        for path in (tmp_path / "file.bin", "/srv/a b/c.iso", "/x/C:/y"):
            assert local_argument(path) == str(path)
        return
    assert local_argument(r"C:\Users\me\Downloads\file.bin") == "/c/Users/me/Downloads/file.bin"
    assert local_argument(r"d:\Media\TV Shows\e01.mkv") == "/d/Media/TV Shows/e01.mkv"
    assert local_argument(r"\\nas\share\dl\a.iso") == "//nas/share/dl/a.iso"
    for refused in (r"Downloads\file.bin", r"C:Downloads\file.bin", r"\Downloads\file.bin", r"C:\a\b:stream", ""):
        with pytest.raises(ValueError):
            local_argument(refused)


# ── the egress guard's tunnel can be driven by a child process ───────────────

_TUNNEL_CHILD = textwrap.dedent("""
    import asyncio, socket, sys
    async def main():
        reader, writer = await asyncio.open_connection(sock=socket.socket(fileno=int(sys.argv[1])))
        writer.write(b"through the tunnel"); await writer.drain()
        sys.stdout.write((await reader.readexactly(4)).decode()); sys.stdout.flush()
    asyncio.run(main())
""")


@pytest.mark.asyncio
async def test_a_guard_tunnel_is_driven_by_the_child_it_is_handed_to(tmp_path):
    from services.downloader_egress_guard import DownloaderEgressGuard
    from test_v1111_aria2_security_boundary import _answer

    received = asyncio.get_running_loop().create_future()

    async def origin(reader, writer):
        received.set_result(await reader.readexactly(len(b"through the tunnel")))
        writer.write(b"pong")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(origin, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def resolver(_host, wanted):
        return [_answer("127.0.0.1", wanted)]

    guard = DownloaderEgressGuard(resolver=resolver, public_check=lambda address: address == "127.0.0.1", bind_port=0)
    owner = ProcessOwnership(tmp_path / "rt")
    try:
        tunnel = await guard.open_tunnel(f"rsync+ssh://origin.test:{port}/x", timeout_seconds=10, budget="rsync")
        owned = await owner.spawn("tunnel", [sys.executable, "-I", "-c", _TUNNEL_CHILD, str(tunnel.fileno())],
                                  env=owner.environment(), pass_fds=(tunnel.fileno(),))
        tunnel.close()
        out, err = await asyncio.wait_for(owned.process.communicate(), 60)
        assert owned.process.returncode == 0, err.decode(errors="replace")
        assert await asyncio.wait_for(received, 5) == b"through the tunnel"
        assert out == b"pong"
    finally:
        server.close()
        await guard.stop()


# ── rsync over SSH through the one channel, with the host's real rsync ──────

@pytest.mark.real_runtime
@pytest.mark.asyncio
async def test_rsync_over_ssh_passes_secret_status_and_tunnel_through_the_native_rsync(tmp_path):
    import asyncssh
    from executors.rsync.executor import _inheritable
    from services import ssh_channel
    from services.artifact_sampling import SSH_HOST_KEY_ALGORITHMS
    from services.downloader_egress_guard import DownloaderEgressGuard
    from test_v1111_aria2_security_boundary import _answer

    assert shutil.which("rsync"), "rsync is required for the rsync-over-SSH channel proof"
    payload = os.urandom(300_000)
    (tmp_path / "srv").mkdir()
    (tmp_path / "dst").mkdir()
    source = tmp_path / "srv" / "payload.bin"
    source.write_bytes(payload)
    commands: list[list[str]] = []

    class Server(asyncssh.SSHServer):
        def begin_auth(self, username):
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, username, password):
            return (username, password) == ("user", "s3cret-pass")

    async def remote(process):
        argv = shlex.split(process.command)
        commands.append(argv)
        argv[0] = shutil.which("rsync")
        native = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
                                                      stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await process.redirect(stdin=native.stdin, stdout=native.stdout, stderr=native.stderr)
        process.exit(await native.wait())

    key = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.listen("127.0.0.1", 0, server_host_keys=[key], server_factory=Server,
                                   process_factory=remote, encoding=None)
    port = server.sockets[0].getsockname()[1]

    async def resolver(_host, wanted):
        return [_answer("127.0.0.1", wanted)]

    guard = DownloaderEgressGuard(resolver=resolver, public_check=lambda address: address == "127.0.0.1", bind_port=0)
    owner = ProcessOwnership(tmp_path / "rt")
    status_read, status_write = os.pipe()
    try:
        tunnel = await guard.open_tunnel(f"rsync+ssh://origin.test:{port}/srv/payload.bin", timeout_seconds=10,
                                         budget="rsync")
        spec = ssh_channel.channel_spec(host="origin.test", username="user", password="s3cret-pass",
                                        identity=hashlib.sha1(key.public_data).hexdigest(),  # nosec B324
                                        host_key_algorithms=SSH_HOST_KEY_ALGORITHMS, timeout=20)
        target = tmp_path / "dst" / "payload.bin"

        def argv(descriptors):
            shell = ssh_channel.remote_shell(sys.executable, spec_fd=descriptors["spec"],
                                             status_fd=_inheritable(status_write), tunnel_fd=tunnel.fileno())
            return ["rsync", "-e", " ".join(shell), "--secluded-args",
                    f"{ssh_channel.CHANNEL_HOST}:{local_argument(source)}", local_argument(target)]

        owned = await owner.spawn("ssh", [], env=owner.environment({"PYTHONPATH": str(BACKEND)}),
                                  secrets={"spec": spec}, pass_fds=(tunnel.fileno(), _inheritable(status_write)),
                                  fd_argv=argv)
        os.close(status_write)
        status_write = -1
        tunnel.close()
        _out, err = await asyncio.wait_for(owned.process.communicate(), 120)
        assert owned.process.returncode == 0, err.decode(errors="replace")
        assert target.read_bytes() == payload
        with os.fdopen(status_read, "rb") as status:
            status_read = -1
            events = [record["event"] for record in ssh_channel.read_status(status.read())]
        assert events == ["connected", "exit"]
        # Secluded arguments: neither the path nor the secret is on any command line.
        assert commands and not any("s3cret-pass" in part or "payload" in part for part in commands[0])
        assert owner.alive("ssh") is False
    finally:
        for descriptor in (status_read, status_write):
            if descriptor >= 0:
                os.close(descriptor)
        server.close()
        await guard.stop()


# ── the packaged aria2 verifies the very SFTP host key DownloadPulse confirms ──

async def _aria2_sftp(tmp_path, origin, fingerprint, attempt):
    from test_v113_transport_evidence_sampling import PASSWORD, USER

    target = tmp_path / f"out-{attempt}"
    target.mkdir()
    # Credentials and the expected key travel in aria2's input file, never on argv.
    listing = tmp_path / f"input-{attempt}.txt"
    listing.write_text(f"sftp://127.0.0.1:{origin.port}/payload.bin\n  ftp-user={USER}\n  ftp-passwd={PASSWORD}\n"
                       f"  ssh-host-key-md=sha-1={fingerprint}\n", encoding="utf-8")
    process = await asyncio.create_subprocess_exec(
        shutil.which("aria2c"), "--no-conf", f"--dir={target}", f"--input-file={listing}", "--max-tries=1",
        "--connect-timeout=15", "--timeout=15", "--summary-interval=0", "--console-log-level=error",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(process.communicate(), 90)
    return process.returncode, target / "payload.bin", (out + err).decode(errors="replace")


@pytest.mark.real_runtime
@pytest.mark.asyncio
@pytest.mark.parametrize("offered", [("ssh-rsa", "ssh-ed25519", "ecdsa-sha2-nistp256"),
                                     ("ssh-rsa", "ssh-ed25519"), ("ssh-rsa",)],
                         ids=["ecdsa-ed25519-rsa", "ed25519-rsa", "rsa"])
async def test_the_packaged_aria2_verifies_the_host_key_dp_confirms_and_no_other(tmp_path, offered):
    # DP confirms the first key of its one order the server offers (the SSH client's
    # preference decides); the packaged aria2's libssh2 must negotiate that same key.
    # A libssh2 crypto backend without it (Windows CNG: no Ed25519/ECDSA; mbedTLS: no
    # Ed25519) negotiates another and every confirmed download fails closed.
    from services.artifact_sampling import SSH_HOST_KEY_ALGORITHMS
    from test_v113_transport_evidence_sampling import SftpOrigin

    assert shutil.which("aria2c"), "the packaged aria2c is required for the SFTP host-key proof"
    (tmp_path / "srv").mkdir()
    payload = os.urandom(70_000)
    (tmp_path / "srv" / "payload.bin").write_bytes(payload)
    origin = await SftpOrigin(tmp_path / "srv").start(algorithms=offered)
    try:
        confirmed = next(algorithm for algorithm in SSH_HOST_KEY_ALGORITHMS if algorithm in offered)
        code, delivered, said = await _aria2_sftp(tmp_path, origin, origin.fingerprint(confirmed), "confirmed")
        assert code == 0, said
        assert delivered.read_bytes() == payload
        for other in (algorithm for algorithm in offered if algorithm != confirmed):
            code, delivered, said = await _aria2_sftp(tmp_path, origin, origin.fingerprint(other), other)
            assert code != 0 and not delivered.exists(), f"aria2 accepted the unconfirmed {other} key"
    finally:
        await origin.close()


# ── the bundled Usenet service: beside the backend and beside the database ──

def test_the_usenet_service_lives_beside_the_backend_and_its_state_beside_the_database(tmp_path):
    script = ("import executors.sabnzbd.runtime as r; print(r.SERVICE_ROOT); print(r.CONFIG_ROOT)")
    database = tmp_path / "data" / "downloadpulse.db"
    environment = {key: value for key, value in os.environ.items() if key != "USENET_CONFIG_DIR"}
    environment["DB_PATH"] = str(database)
    result = subprocess.run([sys.executable, "-c", script], cwd=BACKEND, env=environment, capture_output=True,
                            text=True, timeout=120, check=True)
    service_root, config_root = result.stdout.splitlines()[-2:]
    assert Path(service_root) == BACKEND / "usenet"
    assert Path(config_root) == database.parent / "usenet"
