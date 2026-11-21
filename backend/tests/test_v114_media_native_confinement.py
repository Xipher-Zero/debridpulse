"""Media Downloads helper confinement on the platform it runs on (WS4).

Every native helper the sandboxed worker may start -- ffmpeg, ffprobe, deno --
is started through its confined wrapper (``MediaSandbox.confined_tools``),
which runs ``executors.media.netless`` first: seccomp on Linux, Seatbelt on
macOS, a capability-less AppContainer on Windows. These tests attack that
boundary with the real wrapper and the real deno (``--allow-all``: deno's own
permission system is out of the way, so only the operating system decides),
against listeners this test owns:

* no connection reaches a loopback listener (where the egress guard lives) or
  one on this machine's own non-loopback address (a LAN destination), and no
  name is resolved;
* nothing a confined helper starts gets further (on Windows it may start
  nothing at all);
* on macOS and Windows, handing a URL to the desktop (``open`` /
  ``ShellExecuteW``) reaches nothing either, and nothing can be written
  outside the workspace;
* the very same probe run unconfined does connect: every refusal is the
  confinement's.

Public-internet destinations are refused by the same rules; they are proved
natively in the WS4 packet, not here, so the module needs no internet.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from executors.media import netless, worker
from executors.media.sandbox import MediaSandbox

WINDOWS = os.name == "nt"
MACOS = sys.platform == "darwin"

_PROBE = r"""
const [ports, mode, marker, outside] = [JSON.parse(Deno.args[0]), Deno.args[1], Deno.args[2], Deno.args[3]];
const report = {};
for (const [label, host, port] of ports) {
  try { (await Deno.connect({hostname: host, port})).close(); report[label] = "connected"; }
  catch (e) { report[label] = "refused"; }
}
try { await Deno.resolveDns("localhost.example", "A"); report.dns = "resolved"; }
catch (e) { report.dns = e.name === "NotFound" ? "answered" : "refused"; }
if (mode === "child") {
  try {
    const out = await new Deno.Command(Deno.execPath(), {args: ["run", "--allow-all", "--no-remote", marker,
                                                                Deno.args[0], "leaf", marker, outside]}).output();
    report.child = new TextDecoder().decode(out.stdout).trim() || "silent";
  } catch (e) { report.child = "spawn refused"; }
}
if (mode !== "leaf") {
  try { Deno.writeTextFileSync(outside, "planted"); report.write_outside = "written"; }
  catch (e) { report.write_outside = "refused"; }
}
console.log(JSON.stringify(report));
"""

_DESKTOP = r"""
const url = Deno.args[0];
if (Deno.build.os === "windows") {
  const shell = Deno.dlopen("shell32.dll", {ShellExecuteW: {parameters: ["pointer", "buffer", "buffer", "pointer",
                                                                         "pointer", "i32"], result: "pointer"}});
  const wide = (s) => { const b = new Uint16Array(s.length + 1); for (let i = 0; i < s.length; i++) b[i] = s.charCodeAt(i); return new Uint8Array(b.buffer); };
  const result = shell.symbols.ShellExecuteW(null, wide("open"), wide(url), null, null, 0);
  console.log("ShellExecuteW", Deno.UnsafePointer.value(result));
} else {
  try { const out = await new Deno.Command("/usr/bin/open", {args: ["-g", "-j", url]}).output(); console.log("open", out.code); }
  catch (e) { console.log("open refused"); }
}
await new Promise((resolve) => setTimeout(resolve, 4000));
"""


class Listener:
    """A TCP listener on ``address``; ``hits`` counts accepted connections
    (and the first request line of each, for the desktop case)."""

    def __init__(self, address: str):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind((address, 0))
        self.socket.listen(16)
        self.socket.settimeout(0.2)
        self.address, self.port = address, self.socket.getsockname()[1]
        self.hits: list[bytes] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                peer, _ = self.socket.accept()
            except OSError:
                continue
            with peer:
                peer.settimeout(2)
                try:
                    self.hits.append(peer.recv(256).split(b"\r\n", 1)[0])
                    peer.sendall(b"HTTP/1.0 204 No Content\r\n\r\n")
                except OSError:
                    self.hits.append(b"")

    def close(self):
        self._stop.set()
        self._thread.join(timeout=5)
        self.socket.close()


def _own_address() -> str | None:
    """This machine's non-loopback IPv4 address (a LAN destination), if any."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))   # TEST-NET-1: selects a route, sends nothing
        address = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    return None if address.startswith("127.") else address


@pytest.fixture
def sandbox(tmp_path):
    sandbox = MediaSandbox(str(tmp_path / "runtime"))
    if sandbox.tools.missing():
        pytest.fail(f"missing media runtime: {sandbox.tools.missing()}")
    return sandbox


def _deno(sandbox, workspace: Path, executable: str, *argv: str, timeout: int = 90):
    """Run deno (``executable``: the confined wrapper or the real tool) as the
    worker would: the sandbox's environment, this workspace."""
    workspace.mkdir(parents=True, exist_ok=True)
    if WINDOWS:
        netless.permit(str(workspace), write=True)
    env = sandbox.environment(workspace)
    return subprocess.run([executable, "run", "--allow-all", "--no-remote", *argv], capture_output=True,
                          text=True, timeout=timeout, env=env, cwd=str(workspace), stdin=subprocess.DEVNULL)


def _probe(sandbox, tmp_path, executable: str, mode: str, ports, name: str):
    workspace = tmp_path / name
    script = workspace / "probe.js"
    workspace.mkdir(parents=True, exist_ok=True)
    script.write_text(_PROBE)
    outside = tmp_path / f"{name}-planted.txt"
    result = _deno(sandbox, workspace, executable, str(script), json.dumps(ports), mode, str(script), str(outside))
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    assert lines, (result.returncode, result.stdout[-500:], result.stderr[-1500:])
    return json.loads(lines[-1]), outside


@pytest.mark.real_runtime
def test_the_worker_is_handed_only_confined_wrappers_of_the_real_tools(sandbox):
    confined = sandbox.confined_tools()
    spec = json.loads(sandbox._spec("extract", "https://example.org/x", "http://p", ("127.0.0.1", 1)))
    assert spec["tools"] == confined and set(confined) == {"ffmpeg", "ffprobe", "deno"}
    for name, path in confined.items():
        script = Path(path[:-4] if WINDOWS else path).read_text(encoding="utf-8")
        interpreter = f'"{sys.executable}"' if WINDOWS else sys.executable
        assert script.startswith(f"#!{interpreter} -I\n")
        assert f"netless.run({getattr(sandbox.tools, name)!r})" in script
        if WINDOWS:
            assert path.endswith(f"{name}.exe") and Path(path).is_file()


@pytest.mark.real_runtime
def test_a_confined_helper_and_its_descendants_reach_no_network(sandbox, tmp_path):
    listeners = [Listener("127.0.0.1")]
    lan = _own_address()
    if lan:
        listeners.append(Listener(lan))
    ports = [[f"{'loopback' if item.address.startswith('127.') else 'lan'}", item.address, item.port]
             for item in listeners]
    try:
        control, planted_control = _probe(sandbox, tmp_path, sandbox.tools.deno, "child", ports, "control")
        hits_control = sum(len(item.hits) for item in listeners)
        for item in listeners:
            item.hits.clear()
        confined, planted = _probe(sandbox, tmp_path, sandbox.confined_tools()["deno"], "child", ports, "confined")
        hits = sum(len(item.hits) for item in listeners)
    finally:
        for item in listeners:
            item.close()
    # The probe is able to connect, resolve, start a child and write: only the
    # confinement stops it.
    assert all(control[label] == "connected" for label, _host, _port in ports), control
    assert hits_control >= len(ports) and planted_control.exists()
    assert json.loads(control["child"])["loopback"] == "connected"
    assert hits == 0, confined
    assert all(confined[label] == "refused" for label, _host, _port in ports), confined
    assert confined["dns"] == "refused", confined
    if WINDOWS:
        assert confined["child"] == "spawn refused", confined
    else:
        child = json.loads(confined["child"])
        assert all(child[label] == "refused" for label, _host, _port in ports) and child["dns"] == "refused", child
    if WINDOWS or MACOS:
        assert confined["write_outside"] == "refused" and not planted.exists(), confined


@pytest.mark.real_runtime
@pytest.mark.only_on(WINDOWS or MACOS, reason="a desktop shell hand-off exists on Windows and macOS")
def test_handing_a_link_to_the_desktop_reaches_nothing(sandbox, tmp_path):
    """The macOS analogue of ``os.startfile``: LaunchServices (``open``) would
    have an unconfined browser fetch the link; on Windows, ShellExecuteW."""
    listener = Listener("127.0.0.1")
    workspace = tmp_path / "desktop"
    script = workspace / "desktop.js"
    workspace.mkdir()
    script.write_text(_DESKTOP)
    try:
        result = _deno(sandbox, workspace, sandbox.confined_tools()["deno"], str(script),
                       f"http://127.0.0.1:{listener.port}/escape")
    finally:
        listener.close()
    assert listener.hits == [], (result.stdout, result.stderr[-800:])


def test_the_worker_refuses_to_hand_anything_to_the_desktop_shell():
    """W13: ``os.startfile`` (both audit forms) is refused by the worker's hook."""
    hook = worker.Sandbox(("127.0.0.1", 1), {}, (), (), None)
    for event, args in (("os.startfile", ("https://example.org/", "open")),
                        ("os.startfile/2", ("https://example.org/", "open", "", "", 1))):
        with pytest.raises(worker.SandboxRefusal) as refused:
            hook(event, args)
        assert refused.value.event == event


@pytest.mark.real_runtime
@pytest.mark.only_on(MACOS, reason="the macOS confinement names the workspace")
def test_without_a_nameable_workspace_the_helper_is_not_run(sandbox, tmp_path):
    marker = tmp_path / "ran"
    env = {**sandbox.environment(tmp_path), "TMPDIR": "relative/workspace"}
    result = subprocess.run([sandbox.confined_tools()["deno"], "eval", f"Deno.writeTextFileSync({str(marker)!r}, '1')"],
                            capture_output=True, text=True, timeout=60, env=env)
    assert result.returncode == 126 and "refusing to run" in result.stderr and not marker.exists()


# -- a real acquisition: cancelled across a restart, then retried to completion ------------

class _HeldOrigin:
    """The Dropbox fixture origin on loopback port 80 whose media body is sent
    in two halves: the second only once ``release`` is set."""

    def __init__(self, page: bytes, body: bytes):
        import asyncio
        self.page, self.body, self.release = page, body, asyncio.Event()
        self.server, self.requests = None, []

    async def start(self):
        import asyncio
        # macOS admits an unprivileged port-80 listener only on the wildcard
        # address; the guard still reaches it at 127.0.0.1.
        self.server = await asyncio.start_server(self._serve, "0.0.0.0" if MACOS else "127.0.0.1", 80)
        return self

    async def stop(self):
        self.release.set()
        self.server.close()
        await self.server.wait_closed()

    async def _serve(self, reader, writer):
        try:
            line = (await reader.readuntil(b"\r\n\r\n")).split(b"\r\n", 1)[0].decode("latin-1")
            self.requests.append(line)
            media = line.split(" ")[1].endswith("?dl=1")
            body = self.body if media else self.page
            kind = "video/mp4" if media else "text/html"
            writer.write(f"HTTP/1.1 200 X\r\nContent-Type: {kind}\r\nContent-Length: {len(body)}\r\n"
                         f"Connection: close\r\n\r\n".encode("latin-1"))
            half = len(body) // 2 if media else len(body)
            writer.write(body[:half])
            await writer.drain()
            if media:
                await self.release.wait()
                writer.write(body[half:])
                await writer.drain()
        except Exception:  # noqa: BLE001 -- a client that went away
            pass
        finally:
            writer.close()


@pytest.mark.real_runtime
@pytest.mark.asyncio
async def test_a_real_acquisition_is_cancelled_after_a_restart_and_its_retry_completes(tmp_path):
    import asyncio
    import time
    from executors.media.executor import MediaExecutor
    from providers.media.provider import MediaProvider
    from test_v113_media_downloads_runtime import _PAGE, FIXTURES, _allowed, _guard, _packets, _settled
    from transfers.models import (ExecutionRequest, ExecutionState, ExecutionSubject, ExecutionWork,
                                  MaterializationKind, MaterializationPlan, TransferRequest)

    clip = FIXTURES / "clip.mp4"
    origin = await _HeldOrigin(_PAGE, clip.read_bytes() * 40).start()   # big enough to be caught mid-way
    guard = _guard({"www.dropbox.com": ["127.0.0.1"]})
    runtime, root = tmp_path / "runtime", tmp_path / "downloads"
    root.mkdir()
    sandbox = MediaSandbox(str(runtime), egress=guard)
    try:
        provider = MediaProvider(sandbox.extract, target_resolution="1080", subtitle_language="en")
        (candidate,) = (await provider.resolve(TransferRequest("http", "http://www.dropbox.com/s/abc123/clip.mp4"))
                        ).candidates
        target = root / candidate.name
        plan = MaterializationPlan(MaterializationKind.FILE, str(root), str(target))

        def request(attempt):
            return ExecutionRequest(ExecutionWork(ExecutionSubject.of(candidate), plan, attempt), attempt)

        first = MediaExecutor(str(root), str(runtime), _allowed, sandbox=sandbox)
        handle = first.prepare(request("attempt-held"))
        assert (await first.start(request("attempt-held"), handle)).state == ExecutionState.RUNNING
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            running = await first.observe(handle)
            if running.progress and running.progress.completed_bytes:
                break
            await asyncio.sleep(0.2)
        assert running.state == ExecutionState.RUNNING and running.progress.completed_bytes > 0

        # DownloadPulse restarts: a new executor finds the group by its durable
        # ownership and stops all of it -- worker, launcher, any helper.
        restarted = MediaExecutor(str(root), str(runtime), _allowed, sandbox=sandbox)
        assert (await restarted.observe(handle)).state == ExecutionState.RUNNING
        assert (await restarted.cancel(handle)).state == ExecutionState.CANCELLED
        assert sandbox.processes.alive("attempt-held") is not True
        assert not target.exists()
        assert guard._public_scopes == set()

        origin.release.set()
        retry = MediaExecutor(str(root), str(runtime), _allowed, sandbox=sandbox)
        again = retry.prepare(request("attempt-retry"))
        assert (await retry.start(request("attempt-retry"), again)).state == ExecutionState.RUNNING
        done = await _settled(retry, again, timeout=180)
    finally:
        await guard.stop()
        await origin.stop()
    assert done.state == ExecutionState.SUCCEEDED, done.error
    assert sorted(path.name for path in root.iterdir()) == [candidate.name]
    assert _packets(target, "v:0") == _packets(clip, "v:0")
    assert guard._public_scopes == set()


@pytest.mark.real_runtime
@pytest.mark.only_on(WINDOWS, reason="Windows: the launcher and the shim wait for the tool")
def test_ending_the_launcher_ends_the_confined_helper(sandbox, tmp_path):
    """What the worker does to a stalled helper (and a job termination does
    to the whole group): ending the launcher leaves no confined helper alive."""
    import time
    workspace = tmp_path / "held"
    workspace.mkdir()
    netless.permit(str(workspace), write=True)
    marker = workspace / "alive.txt"
    script = workspace / "hold.js"
    script.write_text(f"Deno.writeTextFileSync({str(marker)!r}, String(Deno.pid)); "
                      "await new Promise((resolve) => setTimeout(resolve, 120000));")
    helper = subprocess.Popen([sandbox.confined_tools()["deno"], "run", "--allow-all", "--no-remote", str(script)],
                              env=sandbox.environment(workspace), stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 60
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    pid = int(marker.read_text())
    assert _running(pid)
    helper.kill()
    helper.wait(timeout=30)
    deadline = time.monotonic() + 30
    while _running(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _running(pid)


def _running(pid: int) -> bool:
    """Windows: whether ``pid`` names a live process."""
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x1000 | 0x00100000, False, pid)   # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == 0x102   # WAIT_TIMEOUT: still running
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
