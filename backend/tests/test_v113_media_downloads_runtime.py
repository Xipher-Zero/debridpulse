"""Media Downloads (yt-dlp): the egress boundary, the sandboxed worker, the
executor's process lifecycle and the real lossless finalizers.

Everything runs against loopback only. The egress guard is the production
guard with an injected resolver: the test origin on 127.0.0.1 stands in for a
public destination (``public_check``), and named hosts resolve to it, to a
private address, or to both -- so the guard's own connection-time policy, not
the test, decides every hop. The address classifier itself is the canonical
one and is proven by ``test_v113_private_lan_policy`` and
``test_security_hardening_v106``; this module proves that the public route
delegates to it and can never grant private-LAN access.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from executors.media import worker
from executors.media.executor import MediaExecutor
from executors.media.sandbox import WORKER, MediaSandbox, MediaTools
from executors.process_ownership import ProcessOwnership
from integrations.media.outcomes import MediaFailure
from providers.media import plan as planning
from providers.media.provider import PLAN_KEY, MediaProvider
from services.downloader_egress_guard import DownloaderEgressGuard
from transfers.errors import Category
from transfers.models import (
    ExecutionRequest, ExecutionState, ExecutionSubject, ExecutionWork, MaterializationKind, MaterializationPlan,
    TransferCandidate, TransferRequest,
)

PUBLIC = "127.0.0.1"   # the loopback origin, standing in for a public address
PRIVATE = "10.0.0.7"
HANG_GUARD_SECONDS = 60


class Origin:
    """A minimal HTTP/1.1 origin: ``routes`` maps a path (query included) to
    ``(status, headers, body)``; every request line is recorded."""

    def __init__(self, routes, *, port=0):
        self.routes = routes
        self.port = port
        self.requests: list[str] = []
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self._serve, PUBLIC, self.port)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def _serve(self, reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HANG_GUARD_SECONDS)
            line = head.split(b"\r\n", 1)[0].decode("latin-1")
            self.requests.append(line)
            path = line.split(" ")[1]
            status, headers, body = self.routes.get(path, (404, {}, b"missing"))
            body = body() if callable(body) else body
            lines = [f"HTTP/1.1 {status} X", f"Content-Length: {len(body)}", "Connection: close",
                     *(f"{key}: {value}" for key, value in headers.items()), "", ""]
            writer.write("\r\n".join(lines).encode("latin-1") + body)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError):
            pass
        finally:
            writer.close()


def _guard(answers):
    """The production guard; ``answers[host]`` is an address list, or a list
    of them consumed one per resolution (a rebinding)."""
    async def resolver(host, port):
        value = answers[host]
        current = value.pop(0) if value and isinstance(value[0], list) else value
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port)) for address in current]
    return DownloaderEgressGuard(resolver=resolver, public_check=lambda address: address == PUBLIC, bind_port=0)


async def _ask(guard, request: bytes) -> bytes:
    reader, writer = await asyncio.open_connection(PUBLIC, guard.bound_port)
    try:
        writer.write(request)
        await writer.drain()
        return await asyncio.wait_for(reader.read(65536), HANG_GUARD_SECONDS)
    finally:
        writer.close()


def _auth(user, token):
    return base64.b64encode(f"{user}:{token}".encode()).decode()


# -- the public-destination route ---------------------------------------------------------

@pytest.mark.asyncio
async def test_the_public_route_reaches_only_public_destinations_judged_at_each_connection():
    origin = await Origin({"/file": (200, {}, b"public bytes")}).start()
    guard = _guard({"media.test": [PUBLIC], "private.test": [PRIVATE], "mixed.test": [PUBLIC, PRIVATE],
                    "rebind.test": [[PUBLIC], [PRIVATE]]})
    await guard.ensure_started()
    try:
        # The operator allowing LAN elsewhere changes nothing for this route.
        guard.configure_private_lan(True)
        _host, _port, user, token = guard.public_route("a" * 32, budget="yt_dlp")
        auth = _auth(user, token)
        get = lambda host, path="/file": (  # noqa: E731
            f"GET http://{host}:{origin.port}{path} HTTP/1.1\r\nHost: {host}\r\n"
            f"Proxy-Authorization: Basic {auth}\r\nConnection: keep-alive\r\n\r\n").encode()

        # Any public host the acquisition discovers, plain HTTP relayed as one
        # origin-form request on its own connection.
        answer = await _ask(guard, get("media.test"))
        assert answer.startswith(b"HTTP/1.1 200") and answer.endswith(b"public bytes")
        assert origin.requests[-1] == "GET /file HTTP/1.1"
        tunnel = await _ask(guard, f"CONNECT media.test:{origin.port} HTTP/1.1\r\n"
                                   f"Proxy-Authorization: Basic {auth}\r\n\r\n".encode())
        assert tunnel.startswith(b"HTTP/1.1 200 Connection Established")

        # Private, mixed, literal-private and rebinding answers: refused by the
        # guard itself, at the connection, saying so (RFC 9209).
        for host in ("private.test", "mixed.test", PRIVATE):
            refused = await _ask(guard, get(host))
            assert refused.startswith(b"HTTP/1.1 403") and b"Proxy-Status: debridpulse" in refused, host
        assert (await _ask(guard, get("rebind.test"))).startswith(b"HTTP/1.1 200")
        assert (await _ask(guard, get("rebind.test"))).startswith(b"HTTP/1.1 403")
        # A redirect is a new request, judged on its own.
        origin.routes["/hop"] = (302, {"Location": f"http://private.test:{origin.port}/file"}, b"")
        assert b"302" in (await _ask(guard, get("media.test", "/hop"))).split(b"\r\n", 1)[0]

        # Web ports only; another scope's name cannot borrow this token; a
        # revoked route admits nothing.
        assert guard._admits(user, token, "media.test", 22) is None
        assert guard._admits(user, token, "media.test", 443) == (False, None, "yt_dlp")
        assert guard._admits(f"debridpulse.public.{'b' * 32}", token, "media.test", 443) is None
        guard.revoke_public_route("a" * 32)
        assert (await _ask(guard, get("media.test"))).startswith(b"HTTP/1.1 407")
        assert origin.requests.count("GET /file HTTP/1.1") == 2
    finally:
        await guard.stop()
        await origin.stop()


# -- the worker's own boundary --------------------------------------------------------------

_PROBE = r'''
import json, os, socket, subprocess, sys
sys.path.insert(0, os.path.dirname(sys.argv[1]))
import worker
spec = json.loads(sys.stdin.read())
sandbox = worker.Sandbox(tuple(spec["guard"]), {"ffmpeg": spec["ffmpeg"]}, (spec["workspace"],), (), None)
sys.addaudithook(sandbox)
from yt_dlp import YoutubeDL
ydl = YoutubeDL(worker._params(spec, worker._Log()))
found = {}

def attempt(name, action):
    sandbox.refused.clear()
    try:
        found[name] = action()
    except Exception as exc:
        found[name] = "refused:" + worker.classify(exc, sandbox, acquire=True).code

def download(url, name):
    path = os.path.join(spec["workspace"], name)
    ydl.dl(path, {"id": "x", "title": "x", "url": url, "protocol": "http", "ext": "bin"})
    return open(path, "rb").read().decode()

attempt("guarded", lambda: download(spec["file"], "a.bin"))
attempt("redirect", lambda: download(spec["redirect"], "b.bin"))
attempt("rebinding_first", lambda: download(spec["rebind"], "c.bin"))
attempt("rebinding_second", lambda: download(spec["rebind"], "d.bin"))
attempt("direct", lambda: socket.create_connection(("127.0.0.1", spec["origin_port"]), timeout=5) and "connected")
attempt("dns", lambda: socket.getaddrinfo("media.test", 80) and "resolved")
attempt("udp", lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("127.0.0.1", 53)) and "sent")
attempt("helper_url", lambda: subprocess.run([spec["ffmpeg"], "-i", spec["file"]], capture_output=True) and "ran")
attempt("helper_other", lambda: subprocess.run(["/bin/sh", "-c", "true"]) and "ran")
attempt("write_outside", lambda: open(spec["outside"], "w").write("x") and "written")
attempt("file_scheme", lambda: ydl.urlopen("file:///etc/hostname").read() and "read")
attempt("ftp_scheme", lambda: ydl.urlopen(spec["ftp"]).read() and "read")
print(json.dumps(found))
'''


@pytest.mark.asyncio
async def test_the_worker_reaches_the_network_only_through_the_guard(tmp_path):
    origin = await Origin({"/file": (200, {}, b"guarded bytes")}).start()
    origin.routes["/hop"] = (302, {"Location": f"http://private.test:{origin.port}/file"}, b"")
    guard = _guard({"media.test": [PUBLIC], "private.test": [PRIVATE], "rebind.test": [[PUBLIC], [PRIVATE]]})
    await guard.ensure_started()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ffmpeg = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
    try:
        host, port, user, token = guard.public_route("c" * 32)
        spec = {"proxy": f"http://{user}:{token}@{host}:{port}", "guard": [host, port], "ffmpeg": ffmpeg,
                "workspace": str(workspace), "outside": str(tmp_path / "outside.txt"),
                "origin_port": origin.port, "socket_timeout": 10,
                "file": f"http://media.test:{origin.port}/file", "redirect": f"http://media.test:{origin.port}/hop",
                "rebind": f"http://rebind.test:{origin.port}/file", "ftp": f"ftp://media.test:{origin.port}/file"}
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-B", "-c", _PROBE, str(WORKER), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "YTDLP_NO_PLUGINS": "1"})
        stdout, stderr = await asyncio.wait_for(process.communicate(json.dumps(spec).encode()), 120)
    finally:
        await guard.stop()
        await origin.stop()
    assert process.returncode == 0, stderr.decode()[-2000:]
    found = json.loads(stdout.decode().strip().splitlines()[-1])
    assert found["guarded"] == "guarded bytes"
    assert found["redirect"] == "refused:egress_refused"
    assert found["rebinding_first"] == "guarded bytes" and found["rebinding_second"] == "refused:egress_refused"
    assert found["direct"] == found["dns"] == found["udp"] == "refused:egress_refused"
    assert found["helper_url"] == found["helper_other"] == "refused:transport_unsupported"
    assert found["write_outside"] == "refused:path_refused" and not (tmp_path / "outside.txt").exists()
    assert found["file_scheme"].startswith("refused:") and found["ftp_scheme"].startswith("refused:")
    # Every byte that reached the origin came through the guard's relays.
    assert origin.requests == ["GET /file HTTP/1.1", "GET /hop HTTP/1.1", "GET /file HTTP/1.1"]


@pytest.mark.asyncio
async def test_an_address_only_generic_handles_is_refused_by_the_real_worker_without_a_connection(tmp_path):
    guard = _guard({})
    sandbox = MediaSandbox(str(tmp_path / "runtime"), egress=guard,
                           tools=MediaTools("/usr/bin/true", "/usr/bin/true", "/usr/bin/true", "/usr/bin/true"))
    try:
        with pytest.raises(MediaFailure) as raised:
            await sandbox.extract("https://files.example.org/archive.zip", selection=planning.selection("best"),
                                  collection_bound=planning.COLLECTION_BOUND)
    finally:
        await guard.stop()
    assert raised.value.code == "unsupported"
    assert not any((tmp_path / "runtime" / "extract").iterdir())
    assert guard._public_scopes == set()


# -- the executor's process lifecycle -------------------------------------------------------

_FAKE_WORKER = r'''
import json, os, subprocess, sys, time
spec = json.loads(sys.stdin.read())
behaviour = spec["plan"]["fake"]
def record(value):
    with open(spec["result"] + ".tmp", "w") as handle:
        json.dump({"attempt": spec["attempt"], **value}, handle)
    os.replace(spec["result"] + ".tmp", spec["result"])
if behaviour == "sleep":
    child = subprocess.Popen(["sleep", "300"])
    with open(os.path.join(spec["workspace"], "child.pid"), "w") as handle:
        handle.write(str(child.pid))
    print(json.dumps({"event": "progress", "component": 0, "downloaded": 1000, "total": 4000}), flush=True)
    time.sleep(300)
elif behaviour == "complete":
    print(json.dumps({"event": "progress", "component": 0, "downloaded": 4, "total": 4}), flush=True)
    print(json.dumps({"event": "phase", "phase": "finalize"}), flush=True)
    with open(spec["target"], "wb") as handle:
        handle.write(b"media bytes")
    record({"state": "completed", "bytes": 11, "container": "mp4"})
elif behaviour == "fail":
    record({"state": "failed", "outcome": "unavailable", "detail": "gone"})
    sys.exit(3)
'''


class FakeSandbox:
    """The executor's sandbox seam with the REAL process ownership: only the
    child program is a stand-in for the yt-dlp worker."""

    def __init__(self, runtime):
        self.processes = ProcessOwnership(runtime)
        self.egress = DownloaderEgressGuard()
        self.tools = MediaTools("/bin/true", "/bin/true", "/bin/true", "/bin/true")
        self.revoked = []

    async def start(self, attempt_id, *, url, plan, workspace, target, result):
        spec = {"plan": plan, "workspace": str(workspace), "target": str(target), "result": str(result),
                "attempt": attempt_id}
        return await self.processes.spawn(attempt_id, [sys.executable, "-c", _FAKE_WORKER],
                                          env=self.processes.environment(), stdin=json.dumps(spec).encode())

    def revoke(self, scope):
        self.revoked.append(scope)


async def _allowed(*_args):
    return True


def _request(root: Path, behaviour: str, attempt: str):
    plan = {"v": 1, "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "extractor": "Youtube",
            "id": "dQw4w9WgXcQ", "formats": ["18"], "container": "mp4", "subtitle": None, "fake": behaviour}
    candidate = TransferCandidate("Clip.mp4", (), provider_id="media", context={PLAN_KEY: plan}, request_kind="https")
    target = root / "Clip.mp4"
    work = ExecutionWork(ExecutionSubject.of(candidate), MaterializationPlan(MaterializationKind.FILE, str(root),
                                                                             str(target)), attempt)
    return ExecutionRequest(work, attempt), target


async def _settled(executor, handle, *, timeout=30.0):
    deadline = time.monotonic() + timeout
    while True:
        observed = await executor.observe(handle)
        if observed.state != ExecutionState.RUNNING or time.monotonic() >= deadline:
            return observed
        await asyncio.sleep(0.1)


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as handle:
            return handle.read().split()[2] == "Z"
    except OSError:
        return True


@pytest.mark.asyncio
async def test_one_owned_group_per_attempt_cancelled_whole_and_reconciled_after_a_restart(tmp_path):
    root, runtime = tmp_path / "downloads", tmp_path / "runtime"
    root.mkdir()
    executor = MediaExecutor(str(root), str(runtime), _allowed, sandbox=FakeSandbox(str(runtime)))
    request, target = _request(root, "sleep", "attempt-sleep")
    handle = executor.prepare(request)
    started = await executor.start(request, handle)
    assert started.state == ExecutionState.RUNNING
    # A duplicate start never makes a second group.
    assert (await executor.start(request, handle)).state == ExecutionState.RUNNING
    workspace = executor.workspace(target.resolve())
    deadline = time.monotonic() + 10
    while not (workspace / "child.pid").exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    child = int((workspace / "child.pid").read_text())
    running = await executor.observe(handle)
    assert running.progress.completed_bytes == 1000 and running.activity.network_active

    # DebridPulse restarts: a new executor finds the still-running group by
    # its durable ownership, and stops all of it.
    restarted = MediaExecutor(str(root), str(runtime), _allowed, sandbox=FakeSandbox(str(runtime)))
    assert (await restarted.observe(handle)).state == ExecutionState.RUNNING
    cancelled = await restarted.cancel(handle)
    assert cancelled.state == ExecutionState.CANCELLED
    deadline = time.monotonic() + 10
    while not _gone(child) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert _gone(child)
    assert (await MediaExecutor(str(root), str(runtime), _allowed,
                                sandbox=FakeSandbox(str(runtime))).observe(handle)).state == ExecutionState.CANCELLED


@pytest.mark.asyncio
async def test_completion_truth_is_the_durable_record_and_the_target_never_a_vanished_process(tmp_path):
    root, runtime = tmp_path / "downloads", tmp_path / "runtime"
    root.mkdir()
    executor = MediaExecutor(str(root), str(runtime), _allowed, sandbox=FakeSandbox(str(runtime)))

    request, target = _request(root, "complete", "attempt-complete")
    handle = executor.prepare(request)
    await executor.start(request, handle)
    done = await _settled(executor, handle)
    assert done.state == ExecutionState.SUCCEEDED
    assert done.materialization.entries[0].relative_path == "Clip.mp4" and done.progress.total_bytes == 11
    assert not executor.workspace(target.resolve()).exists()
    fresh = MediaExecutor(str(root), str(runtime), _allowed, sandbox=FakeSandbox(str(runtime)))
    assert (await fresh.observe(handle)).state == ExecutionState.SUCCEEDED
    target.unlink()
    lost = await fresh.observe(handle)
    assert lost.state == ExecutionState.FAILED and lost.error.native_code == "output_missing"

    request, _target = _request(root, "vanish", "attempt-vanish")
    handle = executor.prepare(request)
    await executor.start(request, handle)
    vanished = await _settled(executor, handle)
    assert vanished.state == ExecutionState.FAILED and vanished.error.category == Category.TRANSFER_INTERRUPTED
    assert (await MediaExecutor(str(root), str(runtime), _allowed, sandbox=FakeSandbox(
        str(runtime))).observe(handle)).state == ExecutionState.ABSENT

    request, _target = _request(root, "fail", "attempt-fail")
    handle = executor.prepare(request)
    await executor.start(request, handle)
    failed = await _settled(executor, handle)
    assert failed.state == ExecutionState.FAILED and failed.error.category == Category.SOURCE_NOT_FOUND


# -- native helpers have no network ----------------------------------------------------------

_ESCAPE = r'''
import socket, sys
probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # local IPC stays available
try:
    socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=5)
    print("connected")
except OSError as exc:
    print("refused", exc.errno)
'''


@pytest.mark.real_runtime
@pytest.mark.asyncio
async def test_every_native_helper_runs_with_no_usable_network(tmp_path):
    """The kernel, not the worker's Python hook, takes the helpers' network
    away: a helper told to fetch a reachable loopback origin -- even deno with
    every network permission -- opens no connection, and neither does any
    native program a confined helper starts."""
    connections = []

    async def accept(reader, writer):
        connections.append(1)
        writer.close()

    origin = await asyncio.start_server(accept, PUBLIC, 0)
    port = origin.sockets[0].getsockname()[1]
    sandbox = MediaSandbox(str(tmp_path / "runtime"))
    confined = sandbox.confined_tools()
    try:
        # The worker is handed only the confined wrappers, each the real tool
        # run through the netless shim.
        spec = json.loads(sandbox._spec("extract", "https://example.org/x", "http://p", (PUBLIC, 1)))
        assert spec["tools"] == confined
        for name, path in confined.items():
            script = Path(path).read_text()
            assert script.startswith(f"#!{sys.executable} -I\n") and f"netless.run({getattr(sandbox.tools, name)!r})" \
                in script

        def run(*argv):
            return subprocess.run(argv, capture_output=True, text=True, timeout=60)

        assert run(confined["ffmpeg"], "-version").returncode == 0
        assert run(confined["mkvmerge"], "--version").returncode == 0
        fetched = run(confined["ffmpeg"], "-hide_banner", "-i", f"http://{PUBLIC}:{port}/x", "-f", "null", "-")
        assert fetched.returncode != 0 and "Permission denied" in fetched.stderr
        script = tmp_path / "fetch.js"
        script.write_text(f"await fetch('http://{PUBLIC}:{port}/x');")
        denied = run(confined["deno"], "run", "--allow-all", "--no-remote", str(script))
        assert denied.returncode != 0
        # Inherited by everything a confined program starts.
        from executors.media import netless
        child = run(sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv.pop(1)); import netless; "
                    "netless.run(sys.argv.pop(1))", str(Path(netless.__file__).parent), sys.executable, "-c", _ESCAPE,
                    str(port))
        assert child.stdout.strip() == "refused 13"
        await asyncio.sleep(0.2)
    finally:
        origin.close()
        await origin.wait_closed()
    assert connections == []


# -- the real finalizers and one whole acquisition ------------------------------------------

def _media(path: Path, *arguments: str) -> None:
    subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *arguments, str(path)],
                   check=True)


def _stream_digest(path: Path, selector: str) -> str:
    return subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-i", str(path),
                           "-map", selector, "-c", "copy", "-f", "md5", "-"], check=True, capture_output=True,
                          text=True).stdout.strip()


def _codecs(path: Path) -> list[str]:
    probe = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-show_entries", "stream=codec_name",
                            "-of", "json", str(path)], check=True, capture_output=True, text=True)
    return [item["codec_name"] for item in json.loads(probe.stdout)["streams"]]


@pytest.mark.real_runtime
def test_native_container_kept_and_mkv_fallback_both_copy_every_stream_unchanged(tmp_path):
    video, audio, subtitle = tmp_path / "component-0.mp4", tmp_path / "component-1.m4a", tmp_path / "subtitle.srt"
    _media(video, "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10", "-c:v", "libx264", "-an")
    _media(audio, "-f", "lavfi", "-i", "sine=duration=1", "-c:a", "aac", "-vn")
    subtitle.write_text("1\n00:00:00,000 --> 00:00:00,900\nHello\n", encoding="utf-8")
    tools = {"ffmpeg": shutil.which("ffmpeg"), "mkvmerge": shutil.which("mkvmerge")}
    components = [(str(video), {"vcodec": "avc1", "acodec": "none"}),
                  (str(audio), {"vcodec": "none", "acodec": "mp4a.40.2"})]
    metadata = {"title": "Clip", "description": "About the clip"}

    native = tmp_path / "output.mp4"
    subprocess.run(worker.finalization_argv(tools, "mp4", components, None, metadata, str(native), str(tmp_path)),
                   check=True)
    merged = tmp_path / "output.mkv"
    result = subprocess.run(worker.finalization_argv(tools, "mkv", components, (str(subtitle), "en"), metadata,
                                                     str(merged), str(tmp_path)))
    assert result.returncode in {0, 1}

    assert _codecs(native) == ["h264", "aac"]
    assert sorted(_codecs(merged)) == ["aac", "h264", "subrip"]
    for output in (native, merged):
        assert _stream_digest(output, "0:v:0") == _stream_digest(video, "0:v:0")
        assert _stream_digest(output, "0:a:0") == _stream_digest(audio, "0:a:0")
    tags = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-show_entries", "format_tags", "-of", "json",
                           str(native)], check=True, capture_output=True, text=True).stdout
    assert "About the clip" in tags


_PAGE = ('<html><script>registerStreamedPrefetch("x", "'
         + base64.b64encode(b"anonymous:\tanonymous").decode() + '")</script></html>').encode()


@pytest.mark.real_runtime
@pytest.mark.asyncio
async def test_one_medium_resolved_planned_acquired_and_finalized_through_the_guard(tmp_path):
    clip = tmp_path / "clip.mp4"
    _media(clip, "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10", "-f", "lavfi", "-i",
           "sine=duration=1", "-c:v", "libx264", "-c:a", "aac", "-shortest")
    # DropboxIE's own address grammar admits no port: the origin is on 80.
    origin = await Origin({"/s/abc123/clip.mp4": (200, {"Content-Type": "text/html"}, _PAGE),
                           "/s/abc123/clip.mp4?dl=1": (200, {"Content-Type": "video/mp4"}, clip.read_bytes())},
                          port=80).start()
    guard = _guard({"www.dropbox.com": [PUBLIC]})
    runtime, root = tmp_path / "runtime", tmp_path / "downloads"
    root.mkdir()
    sandbox = MediaSandbox(str(runtime), egress=guard)
    assert sandbox.tools.missing() == ()
    try:
        provider = MediaProvider(sandbox.extract, target_resolution="1080", subtitle_language="en")
        (candidate,) = (await provider.resolve(TransferRequest("http", "http://www.dropbox.com/s/abc123/clip.mp4"))
                        ).candidates
        plan = candidate.context[PLAN_KEY]
        assert (plan["extractor"], plan["id"], plan["formats"], plan["container"]) == (
            "Dropbox", "abc123", ["original"], "mp4")
        assert candidate.name == "clip [abc123].mp4"

        executor = MediaExecutor(str(root), str(runtime), _allowed, sandbox=sandbox)
        target = root / candidate.name
        work = ExecutionWork(ExecutionSubject.of(candidate), MaterializationPlan(
            MaterializationKind.FILE, str(root), str(target)), "attempt-e2e")
        request = ExecutionRequest(work, "attempt-e2e")
        handle = executor.prepare(request)
        assert (await executor.start(request, handle)).state == ExecutionState.RUNNING
        done = await _settled(executor, handle, timeout=120)
    finally:
        await guard.stop()
        await origin.stop()
    assert done.state == ExecutionState.SUCCEEDED, done.error
    assert done.materialization.entries[0].relative_path == "clip [abc123].mp4"
    assert sorted(path.name for path in root.iterdir()) == ["clip [abc123].mp4"]
    assert _stream_digest(target, "0:v:0") == _stream_digest(clip, "0:v:0")
    assert guard._public_scopes == set()
    assert origin.requests.count("GET /s/abc123/clip.mp4?dl=1 HTTP/1.1") == 1
