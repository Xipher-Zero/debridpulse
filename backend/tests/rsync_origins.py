"""Real rsync origins for DP 1.0.13 rsync qualification.

* ``RsyncDaemon`` -- the real ``rsync --daemon`` binary, unprivileged, on a
  free loopback port, with named roots (modules), optional ``auth users`` and an
  optional ``max connections`` limit.
* ``RsyncSshOrigin`` -- an in-process SSH server (AsyncSSH, offering ECDSA,
  Ed25519 and RSA host keys like ``SftpOrigin``) whose exec requests run the
  real ``rsync --server`` the client asks for, in a login directory beneath a
  root the test owns. Password and public-key logins are both supported.

Nothing here is a fake rsync: the protocol, the wildcard/escape handling, the
exit codes and the file I/O are the installed binary's own.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import asyncssh
import pytest

READY_SECONDS = 20.0


def require_rsync() -> str:
    binary = shutil.which("rsync")
    if binary is None:
        pytest.skip("the rsync binary is required for DP 1.0.13 rsync qualification")
    return binary


def free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class RsyncDaemon:
    """A real rsync daemon. ``modules`` maps a module name to its settings:
    ``path`` (required), ``auth`` ((user, password) or None), ``max_connections``
    (int or None), ``listed`` (bool). ``port`` fixes the listening port (the
    rsync default, 873, for sources written without one); 0 picks a free one."""

    def __init__(self, root: Path, modules: dict, *, motd: str = "", bwlimit: int = 0, port: int = 0):
        self.root = Path(root)
        self.modules = modules
        self.motd = motd
        # The daemon's own sending rate (KiB/s): a deterministic slow source.
        self.bwlimit = bwlimit
        self.fixed_port = int(port)
        self.port = 0
        self.process: subprocess.Popen | None = None

    def start(self) -> "RsyncDaemon":
        require_rsync()
        self.root.mkdir(parents=True, exist_ok=True)
        lines = ["use chroot = no", f"pid file = {self.root}/rsyncd.pid", f"lock file = {self.root}/rsyncd.lock"]
        if self.motd:
            (self.root / "motd").write_text(self.motd)
            lines.append(f"motd file = {self.root}/motd")
        secrets = []
        for name, module in self.modules.items():
            lines += [f"[{name}]", f"  path = {module['path']}", "  read only = yes"]
            if module.get("auth"):
                user, password = module["auth"]
                secrets.append(f"{user}:{password}")
                lines += [f"  auth users = {user}", f"  secrets file = {self.root}/rsyncd.secrets"]
            if module.get("max_connections"):
                lines.append(f"  max connections = {int(module['max_connections'])}")
            if module.get("listed") is False:
                lines.append("  list = no")
        if secrets:
            path = self.root / "rsyncd.secrets"
            path.write_text("\n".join(secrets) + "\n")
            path.chmod(0o600)
        (self.root / "rsyncd.conf").write_text("\n".join(lines) + "\n")
        for _attempt in range(3):
            if self._launch():
                return self
        raise AssertionError("rsync daemon did not become ready: " + self._log())

    def _log(self) -> str:
        try:
            return (self.root / "rsyncd.log").read_text()[-2000:]
        except OSError:
            return ""

    def _launch(self) -> bool:
        """One launch on a fresh port; ``False`` when the daemon exited (a
        port taken between probing and binding)."""
        self.port = self.fixed_port or free_port()
        self.process = subprocess.Popen(
            ["rsync", "--daemon", "--no-detach", f"--config={self.root}/rsyncd.conf", f"--port={self.port}",
             "--address=127.0.0.1", f"--log-file={self.root}/rsyncd.log",
             *([f"--bwlimit={int(self.bwlimit)}"] if self.bwlimit else [])],
            # A daemon whose standard input is a socket serves that one socket
            # (inetd mode) instead of listening; never inherit one.
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + READY_SECONDS
        while True:
            # Ready means the daemon answers its own protocol, not merely that a
            # socket accepted a connection.
            probe = subprocess.run(["rsync", "--no-motd", f"rsync://127.0.0.1:{self.port}/"],
                                   stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
            if probe.returncode == 0:
                return True
            if self.process.poll() is not None:
                return False
            if time.monotonic() > deadline:
                self.stop()
                raise AssertionError(f"rsync daemon did not become ready: {probe.stderr!r} {self._log()}")
            time.sleep(0.05)

    def url(self, path: str, host: str = "rsync-origin.test") -> str:
        return f"rsync://{host}:{self.port}{path}"

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


class _RsyncServer(asyncssh.SSHServer):
    def __init__(self, origin: "RsyncSshOrigin"):
        self.origin = origin

    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return self.origin.credentials is not None

    def validate_password(self, username, password):
        self.origin.auth_attempts.append(("password", username))
        return self.origin.credentials is not None and (username, password) == self.origin.credentials

    def public_key_auth_supported(self):
        return self.origin.authorized_key is not None

    def validate_public_key(self, username, key):
        self.origin.auth_attempts.append(("publickey", username))
        return (self.origin.authorized_key is not None and username == self.origin.key_user
                and key.public_data == self.origin.authorized_key.public_data)


class RsyncSshOrigin:
    """In-process SSH server that runs the real ``rsync --server`` per exec."""

    def __init__(self, root: Path, *, credentials=("rsync-user", "rsync-password"), authorized_key=None,
                 key_user="rsync-user", remote_rsync: bool = True):
        self.root = Path(root)
        self.home = self.root / "home"
        self.credentials = credentials
        self.authorized_key = authorized_key
        self.key_user = key_user
        self.remote_rsync = remote_rsync
        self.auth_attempts: list[tuple[str, str]] = []
        self.commands: list[str] = []
        self.keys = {alg: asyncssh.generate_private_key(alg)
                     for alg in ("ssh-rsa", "ssh-ed25519", "ecdsa-sha2-nistp256")}
        self.server = None
        self.port = 0

    def fingerprint(self, alg: str = "ecdsa-sha2-nistp256") -> str:
        return hashlib.sha1(self.keys[alg].public_data).hexdigest()

    async def start(self, algorithms=("ssh-rsa", "ssh-ed25519", "ecdsa-sha2-nistp256"), *,
                    port: int = 0) -> "RsyncSshOrigin":
        require_rsync()
        self.home.mkdir(parents=True, exist_ok=True)
        origin = self

        async def handle(process: asyncssh.SSHServerProcess):
            command = process.command or ""
            origin.commands.append(command)
            if not origin.remote_rsync:
                process.stderr.write(b"sh: 1: rsync: not found\n")
                process.exit(127)
                return
            # The fixture plays sshd: the client's command line is run by a
            # shell in the login directory, exactly as a real server would.
            child = await asyncio.create_subprocess_exec(
                "/bin/sh", "-c", command, cwd=str(origin.home), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await process.redirect(stdin=child.stdin, stdout=child.stdout, stderr=child.stderr)
            code = await child.wait()
            await process.stdout.drain()
            process.exit(code)

        self.server = await asyncssh.listen(
            "127.0.0.1", port, server_host_keys=[self.keys[alg] for alg in algorithms],
            server_factory=lambda: _RsyncServer(origin), process_factory=handle, encoding=None,
            allow_scp=False, sftp_factory=None)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    def url(self, path: str, host: str = "rsync-ssh-origin.test") -> str:
        return f"rsync+ssh://{host}:{self.port}{path}"

    async def close(self) -> None:
        self.server.close()
        await self.server.wait_closed()


def write_tree(root: Path, files: dict[str, bytes]) -> None:
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    os.sync()
