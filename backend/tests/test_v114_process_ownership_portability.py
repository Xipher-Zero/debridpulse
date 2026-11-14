"""Attempt-owned process groups on every host (1.0.14 desktop WS2).

``executors.process_ownership`` keeps one contract -- one group per attempt,
identified before it runs, liveness that survives a DebridPulse restart,
termination proven for every descendant, secrets only in one-shot pipes -- on
POSIX (``flock``, sessions, ``killpg``) and on Windows (a share-locked marker,
a per-attempt Job Object, ``TerminateJobObject``). The cases here drive the
real owner with a Python child program, so the same case runs on Linux, macOS
and Windows; what only Windows has (junction-free share locks, job membership
and breakaway) is generated only there. Nothing here is ever skipped.

Also here: the macOS pipe guard (C3: no ``F_GETPIPE_SZ`` off Linux), and the
graceful application stop of rsync and Media Downloads (C2): every live group
ends through ``terminate``, nothing durable changes, and a restarted
DebridPulse finds the attempt without a group.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import executors.process_ownership as po
from executors.process_ownership import ProcessGroupAlive, ProcessOwnership

WINDOWS = os.name == "nt"
BACKEND = Path(__file__).resolve().parents[1]
# A real interpreter, never a virtual environment's launcher (on Windows that
# is a separate parent process, so "DebridPulse dies" would kill only it).
if WINDOWS and sys.prefix != sys.base_prefix and os.path.isfile(os.path.join(sys.base_exec_prefix, "python.exe")):
    INTERPRETER = os.path.join(sys.base_exec_prefix, "python.exe")
else:
    INTERPRETER = sys.executable

# The attempt's program: records its pid and its own child's pid, then waits.
FAMILY = textwrap.dedent("""
    import subprocess, sys, time
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(sys.argv[1] + ".tmp", "w") as handle:
        handle.write(f"{__import__('os').getpid()} {child.pid}")
    __import__("os").replace(sys.argv[1] + ".tmp", sys.argv[1])
    time.sleep(120)
""")


def _alive(pid: int) -> bool:
    if WINDOWS:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, pid)   # SYNCHRONIZE | QUERY_LIMITED
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == 0x102   # WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as handle:
            return handle.read().split()[2] != "Z"
    except OSError:
        return True


def _until(predicate, seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


async def _family(owner: ProcessOwnership, attempt: str, pids: Path):
    owned = await owner.spawn(attempt, [INTERPRETER, "-c", FAMILY, str(pids)], env=owner.environment())
    assert await asyncio.to_thread(_until, pids.exists)
    return owned, [int(value) for value in pids.read_text().split()]


def _open_descriptors() -> int:
    if WINDOWS:
        import ctypes
        count = ctypes.c_ulong()
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.GetProcessHandleCount(ctypes.c_void_p(kernel32.GetCurrentProcess()), ctypes.byref(count))
        return count.value
    return len(os.listdir("/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"))


# ── one group per attempt, its whole family terminated ───────────────────────

@pytest.mark.asyncio
async def test_one_live_group_per_attempt_and_terminate_ends_every_descendant(tmp_path):
    owner = ProcessOwnership(tmp_path / "rt")
    owned, (program, grandchild) = await _family(owner, "a", tmp_path / "pids")
    assert owner.alive("a") is True and _alive(program) and _alive(grandchild)
    assert owner.recorded_group("a") == owned.group
    with pytest.raises(ProcessGroupAlive):
        await owner.spawn("a", [INTERPRETER, "-c", "0"], env=owner.environment())
    owner.forget("a")                                     # never forgets a live group
    assert owner.marker("a").exists()
    assert await owner.terminate(owned, "a", grace=2.0)
    assert await asyncio.to_thread(_until, lambda: not _alive(program) and not _alive(grandchild))
    assert owner.alive("a") is False
    owner.forget("a")
    assert not owner.marker("a").exists() and owner.alive("a") is None


@pytest.mark.asyncio
async def test_a_program_that_finishes_leaves_no_live_group(tmp_path):
    owner = ProcessOwnership(tmp_path / "rt")
    owned = await owner.spawn("a", [INTERPRETER, "-c", "import sys; sys.exit(7)"], env=owner.environment())
    assert await owned.process.wait() == 7                # the program's own exit code
    assert owner.alive("a") is False
    assert await owner.terminate(owned, "a")


# ── a DebridPulse restart: liveness and termination survive it ────────────────

_CRASH = """
    import asyncio, os, sys
    sys.path.insert(0, {backend!r})
    import executors.process_ownership as po
    async def main():
        owner = po.ProcessOwnership({runtime!r})
        owned = await owner.spawn("a", [{interpreter!r}, "-c", {family!r}, {pids!r}], env=owner.environment())
        while not os.path.exists({pids!r}):
            await asyncio.sleep(0.05)
        print(owned.group, flush=True)
        os._exit(9)              # DebridPulse dies: no cleanup of any kind runs
    asyncio.run(main())
"""


@pytest.mark.asyncio
async def test_a_group_outlives_a_debridpulse_crash_and_is_found_and_ended_after_restart(tmp_path):
    runtime, pids = tmp_path / "rt", tmp_path / "pids"
    crashed = await asyncio.to_thread(subprocess.run, [INTERPRETER, "-c", textwrap.dedent(_CRASH.format(
        backend=str(BACKEND), runtime=str(runtime), interpreter=INTERPRETER, family=FAMILY, pids=str(pids)))],
        capture_output=True, text=True, timeout=60)
    assert crashed.returncode == 9, crashed.stderr
    program, grandchild = (int(value) for value in pids.read_text().split())
    restarted = ProcessOwnership(runtime)
    assert restarted.alive("a") is True and _alive(program) and _alive(grandchild)
    assert restarted.recorded_group("a") == int(crashed.stdout.split()[0])
    with pytest.raises(ProcessGroupAlive):                # still never a second group
        await restarted.spawn("a", [INTERPRETER, "-c", "0"], env=restarted.environment())
    assert await restarted.terminate(None, "a", grace=2.0)
    assert await asyncio.to_thread(_until, lambda: not _alive(program) and not _alive(grandchild))
    assert restarted.alive("a") is False
    restarted.forget("a")
    assert restarted.alive("a") is None


def _gate(gate_read: int, program_argv: list[str], *, job=None, handles=()) -> subprocess.Popen:
    """Start the owner's own gate program exactly as ``spawn`` starts it,
    with real inputs: its gate pipe and, on Windows, a job handle."""
    if WINDOWS:
        import msvcrt
        gate = msvcrt.get_osfhandle(gate_read)
        os.set_handle_inheritable(gate, True)
        startup = subprocess.STARTUPINFO()
        startup.lpAttributeList = {"handle_list": [gate, *handles]}
        return subprocess.Popen([po._GATE_INTERPRETER, "-I", "-S", "-c", po._WINDOWS_GATE, str(gate), str(job or 0),
                                 ",".join(str(item) for item in handles), program_argv[0], *program_argv],
                                startupinfo=startup, close_fds=True, creationflags=subprocess.DETACHED_PROCESS)
    return subprocess.Popen([INTERPRETER, "-I", "-S", "-c", po._GATE, str(gate_read), program_argv[0], *program_argv],
                            pass_fds=(gate_read,), close_fds=True)


def test_a_gate_debridpulse_never_opens_runs_nothing(tmp_path):
    """DebridPulse dying before it records the group closes its end of the
    gate: the gate reads end-of-file and the program never runs."""
    ran = tmp_path / "ran"
    read, write = os.pipe()
    gate = _gate(read, [INTERPRETER, "-c", f"open({str(ran)!r}, 'w').close()"])
    os.close(read)
    os.close(write)                                       # what DebridPulse's death does
    assert gate.wait(timeout=60) == 125
    time.sleep(0.3)
    assert not ran.exists()


# ── one-shot secrets ─────────────────────────────────────────────────────────

_READS_ITS_INPUTS = textwrap.dedent("""
    import json, os, sys
    def descriptor(value):
        if os.name == "nt":
            import msvcrt
            return msvcrt.open_osfhandle(int(value), os.O_RDONLY)
        return int(value)
    with os.fdopen(descriptor(sys.argv[2]), "rb") as handle:
        secret = handle.read()
    stdin = sys.stdin.buffer.read()
    seen = {"secret": secret.decode(), "stdin": len(stdin), "argv": sys.argv[1:], "env": dict(os.environ)}
    with open(sys.argv[1], "w") as out:
        json.dump(seen, out)
""")


@pytest.mark.asyncio
async def test_secrets_reach_the_program_only_through_one_shot_pipes_and_nothing_is_left_open(tmp_path):
    # C3 where it applies: on macOS ``fcntl`` really has no F_GETPIPE_SZ, and
    # this delivery must still succeed there; on Linux the pipe is enlarged.
    owner = ProcessOwnership(tmp_path / "rt")
    out, secret = tmp_path / "seen.json", "s3cret-" + "k" * 12000         # larger than a default pipe
    owned = await owner.spawn(
        "a", [], env=owner.environment({"DP_HARMLESS": "1"}), secrets={"spec": secret.encode()},
        stdin=b"i" * 20000,
        fd_argv=lambda descriptors: [INTERPRETER, "-c", _READS_ITS_INPUTS, str(out), str(descriptors["spec"])])
    assert await owned.process.wait() == 0, (await owned.process.stderr.read()).decode()
    seen = json.loads(out.read_text())
    assert seen["secret"] == secret and seen["stdin"] == 20000
    assert all(secret not in value for value in seen["argv"])
    assert all(secret not in value for value in seen["env"].values())
    assert seen["env"]["DP_HARMLESS"] == "1"
    assert not {"CONFIG_PATH", "DB_PATH", "DEBRIDPULSE_SETUP_PREVIEW"} & set(seen["env"])
    assert seen["env"].get("HOME") == str(owner.home)
    if WINDOWS:
        assert all(seen["env"].get(key) == str(owner.home) for key in ("USERPROFILE", "APPDATA", "LOCALAPPDATA",
                                                                        "TEMP", "TMP"))


@pytest.mark.asyncio
async def test_repeated_spawns_leave_no_descriptor_or_handle_behind(tmp_path):
    """Every pipe, marker and job copy DebridPulse makes for a spawn is closed
    again: twenty attempts with secrets and input grow nothing."""
    import gc
    owner = ProcessOwnership(tmp_path / "rt")

    async def once(index: int) -> None:
        owned = await owner.spawn(
            f"a{index}", [], env=owner.environment(), secrets={"spec": b"x" * 5000}, stdin=b"i" * 5000,
            fd_argv=lambda descriptors: [INTERPRETER, "-c", "import sys; sys.stdin.buffer.read()"])
        await owned.process.communicate()
        owner.forget(f"a{index}")

    await once(-1)                                        # warm-up: the loop's own machinery
    gc.collect()
    await asyncio.sleep(0.2)
    before = _open_descriptors()
    for index in range(20):
        await once(index)
    gc.collect()
    await asyncio.sleep(0.2)
    assert _open_descriptors() - before <= 2              # nothing per spawn


# ── graceful application stop (C2) ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_application_stop_ends_every_live_group_known_or_found_after_a_restart(tmp_path):
    runtime, pids = tmp_path / "rt", tmp_path / "pids"
    crashed = await asyncio.to_thread(subprocess.run, [INTERPRETER, "-c", textwrap.dedent(_CRASH.format(
        backend=str(BACKEND), runtime=str(runtime), interpreter=INTERPRETER, family=FAMILY, pids=str(pids)))],
        capture_output=True, text=True, timeout=60)
    assert crashed.returncode == 9, crashed.stderr
    found = [int(value) for value in pids.read_text().split()]          # from before the "restart"
    owner = ProcessOwnership(runtime)
    owned, started = await _family(owner, "b", tmp_path / "pids-b")
    finished = await owner.spawn("c", [INTERPRETER, "-c", "0"], env=owner.environment())
    await finished.process.wait()
    await owner.start()
    await owner.maintain()
    assert owner.alive("a") is True and owner.alive("b") is True
    await owner.stop()                                    # the graceful application stop
    assert await asyncio.to_thread(_until, lambda: not any(_alive(pid) for pid in (*found, *started)))
    assert owner.alive("a") is False and owner.alive("b") is False and owner.alive("c") is False
    # Nothing durable is decided by the stop: the markers stay for recovery to
    # observe (no group, no terminal record), and forgetting them is safe.
    assert owner.marker("a").exists() and owner.marker("b").exists()


def test_rsync_and_media_downloads_take_part_in_the_application_lifecycle(tmp_path):
    """Composition discovers lifecycle components generically: an executor
    that is a ``ManagedIntegration`` is driven by start/stop/maintain."""
    from executors.media.executor import MediaExecutor
    from executors.rsync.executor import RsyncConfiguration, RsyncExecutor
    from integrations.definition import ManagedIntegration, lifecycle_components

    async def allowed(*_args):
        return True

    media = MediaExecutor(str(tmp_path / "downloads"), str(tmp_path / "media"), allowed)
    rsync = RsyncExecutor(RsyncConfiguration(local_root=str(tmp_path / "downloads"),
                                             runtime_dir=str(tmp_path / "rsync")), allowed)
    for executor in (media, rsync):
        assert isinstance(executor, ManagedIntegration)
        # The executor's own process owner is its lifecycle part: no other object.
        assert lifecycle_components(executor) == (executor.processes,)


# The media worker's seam with the REAL process ownership: only the child
# program stands in for the yt-dlp worker (it waits, like a long download).
_WAITING_WORKER = "import sys, time; sys.stdin.buffer.read(); time.sleep(120)"


class _MediaSandbox:
    def __init__(self, runtime: str):
        from executors.media.sandbox import MediaTools
        from services.downloader_egress_guard import DownloaderEgressGuard
        self.processes = ProcessOwnership(runtime)
        self.egress = DownloaderEgressGuard()
        self.tools = MediaTools(INTERPRETER, INTERPRETER, INTERPRETER)
        self.revoked = []

    async def start(self, attempt_id, *, url, plan, workspace, target, result, concurrent_fragments=1):
        spec = {"plan": plan, "workspace": str(workspace), "target": str(target), "result": str(result)}
        return await self.processes.spawn(attempt_id, [INTERPRETER, "-c", _WAITING_WORKER],
                                          env=self.processes.environment(), stdin=json.dumps(spec).encode())

    def revoke(self, scope):
        self.revoked.append(scope)


def _media_request(root: Path, attempt: str):
    from providers.media.provider import PLAN_KEY
    from transfers.models import (ExecutionRequest, ExecutionSubject, ExecutionWork, MaterializationKind,
                                  MaterializationPlan, TransferCandidate)
    plan = {"v": 1, "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "extractor": "Youtube",
            "id": "dQw4w9WgXcQ", "formats": ["18"], "container": "mp4", "subtitle": None}
    candidate = TransferCandidate("Clip.mp4", (), provider_id="media", context={PLAN_KEY: plan}, request_kind="https")
    work = ExecutionWork(ExecutionSubject.of(candidate), MaterializationPlan(MaterializationKind.FILE, str(root),
                                                                             str(root / "Clip.mp4")), attempt)
    return ExecutionRequest(work, attempt)


_MEDIA_STOP = """
    import asyncio, json, sys
    sys.path[:0] = [{backend!r}, {tests!r}]
    from executors.media.executor import MediaExecutor
    from test_v114_process_ownership_portability import _MediaSandbox, _media_request
    async def allowed(*_args):
        return True
    async def main():
        executor = MediaExecutor({root!r}, {runtime!r}, allowed, sandbox=_MediaSandbox({runtime!r}))
        request = _media_request(__import__("pathlib").Path({root!r}), "attempt-quit")
        handle = executor.prepare(request)
        started = await executor.start(request, handle)
        await executor.lifecycle.stop()                   # the graceful application stop
        alive = executor.processes.alive(handle.attempt_id)
        interrupted = await executor.observe(handle)
        print(json.dumps({{"started": started.state.value, "alive": alive,
                           "interrupted": interrupted.state.value,
                           "retryability": interrupted.error.retryability.value if interrupted.error else None}}))
    asyncio.run(main())
"""


@pytest.mark.asyncio
async def test_a_media_attempt_stopped_with_the_application_is_found_absent_never_cancelled(tmp_path):
    """Quit is not Pause All and not a cancellation. The application (a real
    process) starts an attempt and stops; this process -- the restart, with
    nothing in memory -- then finds no group and no outcome (ABSENT), which
    recovery retries."""
    from executors.media.executor import MediaExecutor
    from transfers.models import ExecutionState

    root, runtime = tmp_path / "downloads", tmp_path / "runtime"
    root.mkdir()
    stopped = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", textwrap.dedent(_MEDIA_STOP.format(
        backend=str(BACKEND), tests=str(BACKEND / "tests"), root=str(root), runtime=str(runtime)))],
        capture_output=True, text=True, timeout=120, cwd=str(BACKEND))
    assert stopped.returncode == 0, stopped.stderr
    seen = json.loads(stopped.stdout.strip().splitlines()[-1])
    assert seen == {"started": "running", "alive": False, "interrupted": "failed", "retryability": "backoff"}

    async def allowed(*_args):
        return True

    restarted = MediaExecutor(str(root), str(runtime), allowed, sandbox=_MediaSandbox(str(runtime)))
    handle = restarted.prepare(_media_request(root, "attempt-quit"))
    assert (await restarted.observe(handle)).state == ExecutionState.ABSENT


# ── Windows: share-locked marker, job containment, the gate's own check ──────

if WINDOWS:
    @pytest.mark.asyncio
    async def test_a_live_marker_is_readable_but_never_writable_or_deletable(tmp_path):
        owner = ProcessOwnership(tmp_path / "rt")
        owned, _ = await _family(owner, "a", tmp_path / "pids")
        marker = owner.marker("a")
        assert json.loads(marker.read_text())["group"] == owned.group
        with pytest.raises(PermissionError):
            open(marker, "r+b").close()
        with pytest.raises(PermissionError):
            os.unlink(marker)
        assert await owner.terminate(owned, "a", grace=2.0)
        open(marker, "r+b").close()                       # released with the last member

    @pytest.mark.asyncio
    async def test_no_member_can_leave_the_job_and_one_without_inherited_handles_is_still_ended(tmp_path):
        owner = ProcessOwnership(tmp_path / "rt")
        out = tmp_path / "out.json"
        program = textwrap.dedent(f"""
            import json, subprocess, sys, time
            try:
                subprocess.Popen([sys.executable, "-c", "0"], creationflags=0x01000000).wait()
                breakaway = "allowed"
            except OSError as exc:
                breakaway = exc.winerror
            bare = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], close_fds=True)
            json.dump({{"breakaway": breakaway, "bare": bare.pid}}, open({str(out)!r} + ".tmp", "w"))
            __import__("os").replace({str(out)!r} + ".tmp", {str(out)!r})
            time.sleep(120)
        """)
        owned = await owner.spawn("a", [INTERPRETER, "-c", program], env=owner.environment())
        assert await asyncio.to_thread(_until, out.exists)
        seen = json.loads(out.read_text())
        assert seen["breakaway"] == 5                     # ERROR_ACCESS_DENIED: the job allows no breakaway
        assert _alive(seen["bare"])
        assert await owner.terminate(owned, "a", grace=2.0)
        assert await asyncio.to_thread(_until, lambda: not _alive(seen["bare"]))

    @pytest.mark.parametrize("joined", [False, True])
    def test_an_opened_gate_runs_the_program_only_as_a_member_of_its_job(tmp_path, joined):
        import ctypes
        from ctypes import wintypes
        kernel32 = po._kernel32
        ran = tmp_path / "ran"
        job = kernel32.CreateJobObjectW(None, None)
        query = wintypes.HANDLE()
        current = kernel32.GetCurrentProcess()
        assert job and kernel32.DuplicateHandle(current, job, current, ctypes.byref(query), po._JOB_QUERY, True, 0)
        try:
            read, write = os.pipe()
            gate = _gate(read, [INTERPRETER, "-c", f"open({str(ran)!r}, 'w').close()"], job=query.value,
                         handles=(query.value,))
            os.close(read)
            if joined:
                member = kernel32.OpenProcess(po._PROCESS_SET_QUOTA | po._PROCESS_TERMINATE, False, gate.pid)
                assert member and kernel32.AssignProcessToJobObject(job, member)
                kernel32.CloseHandle(member)
            os.write(write, b"1")
            os.close(write)
            assert gate.wait(timeout=60) == (0 if joined else 125)
            assert ran.exists() is joined
        finally:
            kernel32.TerminateJobObject(job, 1)
            kernel32.CloseHandle(job)
            kernel32.CloseHandle(query)

    @pytest.mark.asyncio
    async def test_a_batch_file_is_never_started(tmp_path):
        owner = ProcessOwnership(tmp_path / "rt")
        script = tmp_path / "tool.cmd"
        script.write_text("@echo off\r\n")
        with pytest.raises(PermissionError):
            await owner.spawn("a", [str(script)], env=owner.environment())
        assert owner.alive("a") is not True

    def test_a_marker_that_is_a_link_is_refused(tmp_path):
        owner = ProcessOwnership(tmp_path / "rt")
        owner._prepare()
        elsewhere = tmp_path / "elsewhere.lock"
        elsewhere.write_text("{}")
        os.symlink(elsewhere, owner.marker("a"))
        with pytest.raises(OSError):
            owner.alive("a")
        with pytest.raises(OSError):
            asyncio.run(owner.spawn("a", [INTERPRETER, "-c", "0"], env=owner.environment()))
        assert elsewhere.read_text() == "{}"


# ── shutdown correctness when a component or a record fails ──────────────────

class _Component:
    """A lifecycle component with a known stop outcome (ordinary test
    isolation: the shutdown owner under test is the real one)."""

    def __init__(self, name: str, log: list, fails: bool = False):
        self.name, self.log, self.fails = name, log, fails

    async def start(self):
        pass

    async def maintain(self):
        pass

    async def stop(self):
        self.log.append(self.name)
        if self.fails:
            raise RuntimeError(f"{self.name} could not stop")


class _Engine:
    repository = None


@pytest.mark.asyncio
async def test_every_component_is_asked_to_stop_even_after_one_fails():
    from application.service import ApplicationService, IntegrationStopFailed
    log = []
    first, broken, last = _Component("first", log), _Component("broken", log, fails=True), _Component("last", log)
    service = ApplicationService(_Engine(), lifecycle=(last, broken, first))        # stopped in reverse
    with pytest.raises(IntegrationStopFailed) as failed:
        await service.stop_integrations()
    assert log == ["first", "broken", "last"]
    assert failed.value.stopped == (first, last)
    assert [str(failure) for failure in failed.value.failures] == ["broken could not stop"]


@pytest.mark.asyncio
async def test_every_failure_stays_observable():
    from application.service import ApplicationService, IntegrationStopFailed
    log = []
    a, b, c = _Component("a", log, fails=True), _Component("b", log), _Component("c", log, fails=True)
    service = ApplicationService(_Engine(), lifecycle=(c, b, a))
    with pytest.raises(IntegrationStopFailed) as failed:
        await service.stop_integrations()
    assert log == ["a", "b", "c"]
    assert failed.value.stopped == (b,)
    assert [str(failure) for failure in failed.value.failures] == ["a could not stop", "c could not stop"]


@pytest.mark.asyncio
async def test_a_shutdown_where_everything_stops_is_unchanged():
    from application.service import ApplicationService
    log = []
    a, b = _Component("a", log), _Component("b", log)
    assert await ApplicationService(_Engine(), lifecycle=(b, a)).stop_integrations() == (a, b)
    assert log == ["a", "b"]


@pytest.mark.asyncio
async def test_markers_that_cannot_be_listed_never_pass_for_no_groups(tmp_path):
    owner = ProcessOwnership(tmp_path / "rt")
    owner.root.mkdir()
    owner.markers.write_text("not a directory")          # listing the markers fails for real
    with pytest.raises(RuntimeError, match="could not be listed"):
        await owner.stop()
    assert await ProcessOwnership(tmp_path / "never-used").stop() is None   # nothing ever started: no groups


# A separate process holding a marker the way a group member does (the real
# lock primitive of this host), outside any group the owner could end.
_HOLDER = """
    import os, sys, time
    sys.path.insert(0, {backend!r})
    import executors.process_ownership as po
    path = {path!r}
    if os.name == "nt":
        handle, error = po._open_marker(path, po._GENERIC_READ | po._GENERIC_WRITE, po._SHARE_READ, po._OPEN_ALWAYS)
        assert handle is not None, error
    else:
        import fcntl
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    print("held", flush=True)
    time.sleep(120)
"""


def _hold(path: Path) -> subprocess.Popen:
    holder = subprocess.Popen([INTERPRETER, "-c", textwrap.dedent(_HOLDER.format(backend=str(BACKEND), path=str(path)))],
                              stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "held"
    return holder


@pytest.mark.asyncio
async def test_an_unidentifiable_held_marker_is_reported_and_a_valid_group_beside_it_still_ends(tmp_path):
    owner = ProcessOwnership(tmp_path / "rt")
    owned, (program, grandchild) = await _family(owner, "valid", tmp_path / "pids")
    stale = owner.markers / ("0" * 32 + ".lock")
    stale.write_text("{ not json")                       # a free malformed marker: proven to name no group
    held = owner.markers / ("1" * 32 + ".lock")
    held.write_text("{ not json")
    holder = await asyncio.to_thread(_hold, held)       # a malformed marker something still holds
    try:
        with pytest.raises(RuntimeError, match="0 of 1 identified, 1 unidentifiable"):
            await owner.stop()
        assert await asyncio.to_thread(_until, lambda: not _alive(program) and not _alive(grandchild))
        assert owner.alive("valid") is False
    finally:
        holder.kill()
        holder.wait()
    await owner.stop()                                   # once nothing holds it: proven, success


@pytest.mark.asyncio
async def test_one_group_not_proven_gone_fails_the_stop_after_every_group_was_asked(tmp_path):
    """Simulated boundary (not native proof): the 'stuck' marker is held by a
    process outside any group or job, so no termination can free it."""
    owner = ProcessOwnership(tmp_path / "rt")
    owned, (program, grandchild) = await _family(owner, "ending", tmp_path / "pids")
    stuck = owner.marker("stuck")
    stuck.write_text(json.dumps({"attempt": "stuck", "group": 2 ** 22 + 12345}))
    holder = await asyncio.to_thread(_hold, stuck)
    try:
        with pytest.raises(RuntimeError, match="1 of 2 identified, 0 unidentifiable"):
            await owner.stop()
        assert await asyncio.to_thread(_until, lambda: not _alive(program) and not _alive(grandchild))
        assert owner.alive("ending") is False and owner.alive("stuck") is True
    finally:
        holder.kill()
        holder.wait()
