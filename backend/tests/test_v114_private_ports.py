"""A taken port never stops an executor (core.ports).

DownloadPulse's private loopback listeners -- the downloader egress guard, the
aria2 daemon's RPC and the bundled Usenet service -- each prefer their usual
port (6811, 6800, 8090), but another program on the same computer may already
listen there: a desktop application shares the computer's network, and a
person's own aria2 on 6800 stopped DownloadPulse's (2026-11-17). Each now takes
its usual port when free and otherwise one the operating system chooses, in
every deployment; whoever connects asks its owner for the address in use.
aria2's handles are identified by the daemon, never its address, so a handle
persisted before the port moved still matches.

Scenarios that move a port run in their own process: the port in use is
process state, and every other test sees the usual one.
"""
from __future__ import annotations

import errno
import json
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from core import ports  # noqa: E402


def _listener(port: int = 0) -> socket.socket:
    """A real listener holding ``port`` on loopback, as another program would."""
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.bind(("127.0.0.1", port))
    held.listen(1)
    return held


def _run(script: str, tmp_path: Path) -> dict:
    done = subprocess.run([sys.executable, "-c", textwrap.dedent(script)], cwd=BACKEND, capture_output=True,
                          text=True, timeout=180, env={"PYTHONPATH": str(BACKEND), "TMP_ROOT": str(tmp_path),
                                                       "PATH": __import__("os").environ.get("PATH", "")})
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_taken_port_gives_way_to_a_free_one_and_a_free_one_is_kept():
    held = _listener()
    port = held.getsockname()[1]
    try:
        assert ports.available(port) is False
        chosen = ports.loopback_port(port, name="test listener")
        assert chosen != port and ports.available(chosen)
    finally:
        held.close()
    assert ports.loopback_port(port, name="test listener") == port          # free again: its usual port
    assert ports.address_in_use(OSError(errno.EADDRINUSE, "in use"))
    assert not ports.address_in_use(OSError(errno.EACCES, "denied"))
    assert not ports.address_in_use(ValueError("in use"))


@pytest.mark.asyncio
async def test_the_egress_guard_listens_elsewhere_and_jobs_are_given_where_it_is():
    from services.downloader_egress_guard import DownloaderEgressGuard
    held = _listener()
    port = held.getsockname()[1]
    guard = DownloaderEgressGuard(bind_port=port)
    try:
        await guard.ensure_started()
        assert guard.bound_port not in (0, port)
        options = guard.job_options("https://files.example/file.bin")
        assert options["all-proxy"] == f"http://127.0.0.1:{guard.bound_port}"
    finally:
        await guard.stop()
        held.close()


def test_the_usenet_service_takes_a_free_port_and_names_it_everywhere(tmp_path):
    """Without the bundled service here: the port start() chooses, then the
    configuration and the command it writes from it, and the client's address."""
    result = _run(f"""
        import json, os, socket
        os.environ["USENET_CONFIG_DIR"] = os.path.join(os.environ["TMP_ROOT"], "usenet")
        from core.ports import loopback_port
        from executors.sabnzbd import runtime
        from executors.sabnzbd.client import SabEndpoint, SabnzbdClient
        held = socket.socket(); held.bind(("127.0.0.1", 0)); held.listen(1)
        taken = held.getsockname()[1]
        runtime.SERVICE_PORT = taken                       # the usual port, held by another program
        runtime._service_port = loopback_port(runtime.SERVICE_PORT, name="Usenet service")   # start()'s choice
        runtime._write_bootstrap_ini(os.path.join(os.environ["TMP_ROOT"], "downloads"))
        ini = open(os.path.join(os.environ["USENET_CONFIG_DIR"], "sabnzbd.ini"), encoding="utf-8").read()
        command = runtime.UsenetRuntime()._command() if runtime.UsenetRuntime._binary() else []
        client = SabnzbdClient(SabEndpoint(base_url=runtime.service_url, api_key="k"))
        print(json.dumps({{"taken": taken, "chosen": runtime._service_port, "url": runtime.service_url(),
                          "ini": ini, "command": command, "api": client._url()}}))
    """, tmp_path)
    assert result["chosen"] != result["taken"]
    assert result["url"] == f"http://127.0.0.1:{result['chosen']}"
    assert f"port = {result['chosen']}" in result["ini"]
    assert result["api"] == f"http://127.0.0.1:{result['chosen']}/api"
    if result["command"]:
        assert f"127.0.0.1:{result['chosen']}" in result["command"]
    source = (BACKEND / "executors" / "sabnzbd" / "runtime.py").read_text(encoding="utf-8")
    start = source[source.index("    async def start(self)"):]
    assert start.index("_service_port = loopback_port(SERVICE_PORT") < start.index("_write_bootstrap_ini(")


@pytest.mark.real_runtime
def test_aria2_starts_elsewhere_when_6800_is_taken_and_its_handles_still_match(tmp_path):
    result = _run("""
        import asyncio, json, os, socket
        from executors.aria2 import runtime as module
        from executors.aria2.definition import Aria2Options
        from executors.aria2.executor import Aria2Configuration, Aria2Executor, execution_binding
        root = os.environ["TMP_ROOT"]
        held = socket.socket(); held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            held.bind(("127.0.0.1", module.RPC_PORT)); held.listen(1); holding = True
        except OSError:
            holding = False                              # something else already holds 6800: the same case
        async def main():
            options = Aria2Options(log_file=os.path.join(root, "aria2", "aria2.log"),
                                   session_file=os.path.join(root, "aria2", "aria2.session"))
            daemon = module.Aria2Runtime()
            daemon.configure(module.Aria2RuntimeConfiguration(options=options, download_root=os.path.join(root, "dl")))
            try:
                status = await daemon.start()
                client = module.rpc_service(options)
                version = await client._call("aria2.getVersion")
                configuration = Aria2Configuration(os.path.join(root, "dl"), options.split, options.min_split_size,
                                                   options.max_connection_per_server, options.continue_downloads,
                                                   secrets=(module.RPC_SECRET,))
                async def authorize(handle, action): return True
                executor = Aria2Executor(client, configuration, authorize, runtime=daemon)
                persisted = execution_binding(os.path.join(root, "dl"), "http://127.0.0.1:6800/jsonrpc")
                return {"rpc_ok": status.get("rpc_ok"), "url": client.url, "version": bool(version),
                        "binding_matches": executor.binding == persisted, "holding": holding}
            finally:
                await daemon.stop()
        print(json.dumps(asyncio.run(main())))
    """, tmp_path)
    assert result["rpc_ok"] is True and result["version"] is True
    assert not result["url"].startswith("http://127.0.0.1:6800/")               # it listens elsewhere
    assert result["binding_matches"] is True                                    # a handle saved under 6800 still matches
