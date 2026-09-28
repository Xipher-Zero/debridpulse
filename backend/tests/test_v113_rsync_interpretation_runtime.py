"""DP 1.0.13 forgiving rsync input, proven end to end on the default ports.

The operator types ``rsync://host/path`` and never chooses between an rsync
daemon and rsync over SSH. A source written without a port reaches the
daemon on 873 and SSH on 22, so this proof binds exactly those ports on
loopback (a container with ``net.ipv4.ip_unprivileged_port_start=0``, or
root) and is skipped where they cannot be bound. Everything else is real: the
convergence engine, the rsync provider and executor, the egress guard, a real
rsync daemon and a real ``rsync --server`` behind an SSH origin, and the one
authentication-input lifecycle answering with an encrypted OpenSSH key.
"""
from __future__ import annotations

import asyncio
import errno
import json
import socket
import time
from pathlib import Path

import asyncssh
import pytest

import db.database as database
from rsync_origins import RsyncDaemon, RsyncSshOrigin, write_tree
from test_v113_continuation_runtime import BODY
from test_v113_rsync_runtime import MIB, USER, _runtime
from transfers.errors import Category
from transfers.models import TransferRequest, TransferState

pytestmark = pytest.mark.asyncio
HOST = "rsync-both.test"
DAEMON_PORT, SSH_PORT = 873, 22


def _default_ports_bindable() -> str:
    for port in (DAEMON_PORT, SSH_PORT):
        probe = socket.socket()
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            return f"loopback port {port} cannot be bound here ({errno.errorcode.get(exc.errno, exc.errno)})"
        finally:
            probe.close()
    return ""


_UNAVAILABLE = _default_ports_bindable()
requires_default_ports = pytest.mark.skipif(bool(_UNAVAILABLE), reason=_UNAVAILABLE or "default ports bindable")


def _daemon(tmp_path: Path, modules: dict) -> RsyncDaemon:
    return RsyncDaemon(tmp_path / "daemon", modules, port=DAEMON_PORT).start()


def _connects(daemon: RsyncDaemon) -> int:
    return daemon._log().count("connect from")


async def _key_origin(tmp_path: Path):
    key = asyncssh.generate_private_key("ssh-ed25519")
    origin = await RsyncSshOrigin(tmp_path / "ssh", credentials=None, authorized_key=key,
                                  key_user=USER).start(port=SSH_PORT)
    return origin, key


async def _answer_with_key(runtime, transfer_id, key, passphrase="interpretation-passphrase-sentinel"):
    challenge = await runtime.until(lambda: runtime.engine.challenges.current(transfer_id), label="challenge")
    assert [item.method.value for item in challenge.methods] == ["username_password", "username_private_key"]
    facts = {fact.name.value: fact.value for fact in challenge.facts}
    assert facts["server_host"] == HOST
    await runtime.engine.submit_input(transfer_id, challenge.id, "username_private_key", {
        "username": USER, "private_key": key.export_private_key("openssh", passphrase).decode(),
        "passphrase": passphrase})
    return passphrase


@requires_default_ports
@pytest.mark.parametrize("daemon_present", [True, False], ids=["unknown-module", "no-daemon-listening"])
async def test_a_plain_rsync_path_that_the_daemon_does_not_provide_resolves_over_ssh(
        tmp_path, monkeypatch, daemon_present):
    daemon = _daemon(tmp_path, {"pub": {"path": tmp_path / "pub"}}) if daemon_present else None
    origin, key = await _key_origin(tmp_path)
    write_tree(origin.root / "files", {"movie.bin": BODY[:2 * MIB]})
    runtime = await _runtime(tmp_path, monkeypatch)
    try:
        typed = f"rsync://{HOST}{origin.root}/files/movie.bin"
        before = _connects(daemon) if daemon is not None else 0  # the fixture's own readiness probe
        transfer = await runtime.engine.submit((TransferRequest("rsync", typed),), deduplicate=False)
        passphrase = await _answer_with_key(runtime, transfer.id, key)
        await runtime.until(lambda: runtime.completed(transfer.id), label="SSH reading of a plain rsync source")
        (artifact,) = await runtime.repository.artifacts(transfer.id)
        assert Path(artifact.target).read_bytes() == BODY[:2 * MIB]
        (request,) = await runtime.repository.requests(transfer.id)
        # What the operator typed is kept exactly; the established reading is
        # recorded beside it, and the candidate executes over SSH.
        assert request.request == TransferRequest("rsync", typed)
        assert request.interpretation == TransferRequest("rsync+ssh", f"rsync+ssh://{HOST}{origin.root}/files/movie.bin")
        assert [endpoint.scheme for endpoint in artifact.candidates[0].endpoints] == ["rsync+ssh"]
        assert ("publickey", USER) in origin.auth_attempts
        if daemon is not None:
            # The daemon was asked once -- never again after the SSH reading
            # was established, and never with the SSH answer.
            assert _connects(daemon) - before == 1
        async with database.get_db() as db:
            tables = [row["name"] for row in await db.fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            durable = json.dumps([dict(row) for table in tables
                                  for row in await db.fetchall(f'SELECT * FROM "{table}"')])  # nosec B608
        assert passphrase not in durable and "PRIVATE KEY" not in durable
    finally:
        await runtime.close()
        await origin.close()
        if daemon is not None:
            daemon.stop()


@requires_default_ports
async def test_a_daemon_that_provides_the_path_is_used_and_ssh_is_never_contacted(tmp_path, monkeypatch):
    write_tree(tmp_path / "pub", {"movie.bin": BODY[:MIB]})
    daemon = _daemon(tmp_path, {"pub": {"path": tmp_path / "pub"}})
    origin, _key = await _key_origin(tmp_path)
    runtime = await _runtime(tmp_path, monkeypatch)
    try:
        transfer = await runtime.engine.submit((TransferRequest("rsync", f"rsync://{HOST}/pub/movie.bin"),),
                                               deduplicate=False)
        await runtime.until(lambda: runtime.completed(transfer.id), label="daemon reading")
        (artifact,) = await runtime.repository.artifacts(transfer.id)
        assert Path(artifact.target).read_bytes() == BODY[:MIB]
        (request,) = await runtime.repository.requests(transfer.id)
        assert request.interpretation is None
        assert [endpoint.scheme for endpoint in artifact.candidates[0].endpoints] == ["rsync"]
        assert origin.auth_attempts == [] and origin.commands == []
    finally:
        await runtime.close()
        await origin.close()
        daemon.stop()


@requires_default_ports
@pytest.mark.parametrize("module", [
    {"auth": (USER, "daemon-password-sentinel")},
    {"max_connections": 1, "hold": True},
], ids=["daemon-login", "daemon-full"])
async def test_a_daemon_that_asks_for_a_login_or_is_full_keeps_the_source(tmp_path, monkeypatch, module):
    import subprocess
    write_tree(tmp_path / "pub", {"movie.bin": BODY[:MIB]})
    settings = {"path": tmp_path / "pub", **{key: value for key, value in module.items() if key != "hold"}}
    daemon = _daemon(tmp_path, {"pub": settings})
    origin, _key = await _key_origin(tmp_path)
    holder = None
    if module.get("hold"):
        # Another client holds the module's only slot (its own process group:
        # an rsync client forks).
        holder = subprocess.Popen(["rsync", "--bwlimit=16", f"rsync://127.0.0.1:{DAEMON_PORT}/pub/movie.bin",
                                   str(tmp_path / "holder.bin")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  start_new_session=True)
        await asyncio.sleep(1.0)
        assert holder.poll() is None, "the holder never obtained the module's slot"
    runtime = await _runtime(tmp_path, monkeypatch)
    try:
        transfer = await runtime.engine.submit((TransferRequest("rsync", f"rsync://{HOST}/pub/movie.bin"),),
                                               deduplicate=False)
        if module.get("auth"):
            challenge = await runtime.until(lambda: runtime.engine.challenges.current(transfer.id), label="daemon login")
            # The daemon's own account: a password, never an SSH key or identity.
            assert [item.method.value for item in challenge.methods] == ["username_password"]
            assert challenge.facts == ()
        else:
            async def refused():
                (request,) = await runtime.repository.requests(transfer.id)
                return request.error is not None and request.error.category == Category.CONCURRENCY_LIMITED

            await runtime.until(refused, label="daemon capacity")
        (request,) = await runtime.repository.requests(transfer.id)
        assert request.interpretation is None
        assert origin.auth_attempts == [] and origin.commands == []
        assert (await runtime.repository.get(transfer.id)).state != TransferState.COMPLETED
    finally:
        if holder is not None:
            import os
            import signal
            os.killpg(holder.pid, signal.SIGKILL)
            holder.wait()
        await runtime.close()
        await origin.close()
        daemon.stop()


@requires_default_ports
@pytest.mark.parametrize("slash", ["", "/"], ids=["dir", "dir-slash"])
async def test_a_directory_through_the_ssh_reading_is_one_tree_with_or_without_a_slash(
        tmp_path, monkeypatch, slash):
    daemon = _daemon(tmp_path, {"pub": {"path": tmp_path / "pub"}})
    origin, key = await _key_origin(tmp_path)
    write_tree(origin.root / "files" / "Album", {"cover.jpg": b"cover", "Disc 1/01.flac": BODY[:MIB]})
    runtime = await _runtime(tmp_path, monkeypatch)
    try:
        transfer = await runtime.engine.submit((TransferRequest("rsync", f"rsync://{HOST}{origin.root}/files/Album{slash}"),),
                                               deduplicate=False)
        await _answer_with_key(runtime, transfer.id, key)
        await runtime.until(lambda: runtime.completed(transfer.id), label="SSH tree")
        files = sorted(Path(item.target).relative_to(runtime.downloads).as_posix()
                       for item in await runtime.repository.artifacts(transfer.id))
        assert files == ["Album/Disc 1/01.flac", "Album/cover.jpg"]
        rows = await runtime.repository.requests(transfer.id)
        (root,) = [row for row in rows if row.parent_id is None]
        assert root.request.payload.startswith("rsync://")
        assert root.interpretation is not None and root.interpretation.kind == "rsync+ssh"
    finally:
        await runtime.close()
        await origin.close()
        daemon.stop()


@requires_default_ports
async def test_a_daemon_port_that_never_answers_is_a_bounded_timeout_and_never_a_fallback(tmp_path, monkeypatch):
    """The real-world case of a host whose firewall silently drops 873: the
    daemon reading times out within rsync's Connection Timeout (not the
    kernel's), and a timeout is uncertainty -- SSH is never tried."""
    from test_v113_egress_guard_route_scope import BlackHole
    origin, _key = await _key_origin(tmp_path)
    write_tree(origin.root / "files", {"movie.bin": BODY[:MIB]})
    runtime = await _runtime(tmp_path, monkeypatch)
    try:
        with BlackHole(DAEMON_PORT):
            started = time.monotonic()
            transfer = await runtime.engine.submit((TransferRequest("rsync", f"rsync://{HOST}{origin.root}/files/movie.bin"),),
                                                   deduplicate=False)

            async def timed_out():
                (request,) = await runtime.repository.requests(transfer.id)
                return request.error is not None

            await runtime.until(timed_out, label="daemon timeout", timeout=60)
            waited = time.monotonic() - started
        (request,) = await runtime.repository.requests(transfer.id)
        assert (request.error.domain.value, request.error.category) == ("network", Category.CONNECTION_TIMEOUT)
        # Bounded by the configured Connection Timeout (15 s here), not ~127 s.
        assert waited < 40
        assert request.interpretation is None and await runtime.engine.challenges.current(transfer.id) is None
        assert origin.auth_attempts == [] and origin.commands == []
    finally:
        await runtime.close()
        await origin.close()
