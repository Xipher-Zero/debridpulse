"""1.0.13 WS7 SEC-002 / PA-PROV-4: aria2's RPC authority is this process's own, and its saved session is private and short-lived.

SEC-002: the daemon's RPC secret was the constant ``debridpulse-internal-aria2-rpc``,
published with the source, so any local program could drive the daemon --
add, change or read jobs and their credentials. It is now unpredictable and
belongs to one DebridPulse process; restart and recovery in that process keep
using it.

PA-PROV-4: aria2 saves its session (download and proxy passwords, signed
addresses) every 30 s. DebridPulse never loads that session -- the database
is the source of truth and every job is re-added from it -- yet the file was
readable to other accounts wherever its folder was, and kept its credentials
after the daemon stopped. A folder DebridPulse creates for it is now private
to this account (aria2 replaces the file with its own mode on every save; the
image's data folder is already private, ``entrypoint.sh``), and the session is
emptied when the daemon stops, as it already was when one starts; saving
itself is unchanged.
"""
from __future__ import annotations

import socket
import stat
import subprocess
import sys
from pathlib import Path

import aiohttp
import pytest

from executors.aria2 import runtime as runtime_module
from executors.aria2.definition import Aria2Options
from executors.aria2.runtime import Aria2Runtime, Aria2RuntimeConfiguration

BACKEND = Path(__file__).resolve().parents[1]
PUBLISHED = "debridpulse-internal-aria2-rpc"
JOB = {"pause": "true", "http-passwd": "HTTP-PASSWORD", "all-proxy": "http://127.0.0.1:9",
       "all-proxy-user": "debridpulse", "all-proxy-passwd": "PROXY-CAPABILITY"}
SECRETS = ("HTTP-PASSWORD", "PROXY-CAPABILITY", "SIGNED-ADDRESS")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def _rpc(port: int, token: str, method: str, *params):
    async with aiohttp.ClientSession() as session:
        async with session.post(f"http://127.0.0.1:{port}/jsonrpc", json={
                "jsonrpc": "2.0", "id": "t", "method": method, "params": [f"token:{token}", *params]}) as response:
            return await response.json()


def test_every_debridpulse_process_draws_its_own_unpredictable_secret():
    drawn = {subprocess.run([sys.executable, "-c", "from executors.aria2.runtime import RPC_SECRET; print(RPC_SECRET)"],
                            cwd=BACKEND, check=True, capture_output=True, text=True).stdout.strip() for _ in range(2)}
    assert len(drawn) == 2 and PUBLISHED not in drawn
    assert all(len(secret) >= 32 for secret in drawn)
    client = runtime_module.rpc_service(Aria2Options())
    assert client.secret == runtime_module.RPC_SECRET


@pytest.mark.real_runtime
@pytest.mark.asyncio
async def test_only_this_process_drives_the_daemon_and_its_session_never_outlives_it(tmp_path, monkeypatch):
    port = _free_port()
    monkeypatch.setattr(runtime_module, "RPC_PORT", port)
    session = tmp_path / "aria2" / "aria2.session"
    options = Aria2Options(log_file=str(tmp_path / "aria2" / "aria2.log"), session_file=str(session))
    runtime = Aria2Runtime()
    runtime.configure(Aria2RuntimeConfiguration(options=options, download_root=str(tmp_path / "downloads")))
    try:
        assert (await runtime.start())["rpc_ok"] is True
        # The published constant authorizes nothing: no read, no job.
        assert "error" in await _rpc(port, PUBLISHED, "aria2.getVersion")
        assert "error" in await _rpc(port, PUBLISHED, "aria2.addUri", ["https://files.example.invalid/x"], JOB)
        assert (await _rpc(port, runtime_module.RPC_SECRET, "aria2.tellWaiting", 0, 10))["result"] == []
        # The argv aria2 keeps for itself does not show the secret (aria2 masks it).
        pid = runtime.process_identity()[0]
        assert runtime_module.RPC_SECRET not in Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")

        added = await _rpc(port, runtime_module.RPC_SECRET, "aria2.addUri",
                           ["https://files.example.invalid/x?token=SIGNED-ADDRESS"], JOB)
        assert "result" in added
        await _rpc(port, runtime_module.RPC_SECRET, "aria2.saveSession")
        # While it runs, what aria2 saved sits in a folder private to this
        # account (aria2 replaces the file itself with its own mode).
        assert stat.S_IMODE(session.parent.stat().st_mode) == 0o700
        assert all(secret in session.read_text() for secret in SECRETS)

        # A restart in this process keeps the one authority.
        assert (await runtime.restart())["rpc_ok"] is True
        assert "result" in await _rpc(port, runtime_module.RPC_SECRET, "aria2.getVersion")
        assert "error" in await _rpc(port, PUBLISHED, "aria2.getVersion")
        assert session.read_text() == ""                    # emptied with the daemon that saved it
    finally:
        await runtime.stop()
    assert not any(secret in session.read_text() for secret in SECRETS)
