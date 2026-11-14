"""Attempt-owned native process groups for subprocess executors.

An executor whose native job is an operating-system process (rather than a job
inside a daemon it can query) owns that process through exactly this module.
One DebridPulse execution attempt owns at most one native process group:

* Before the process exists, a durable marker is created for the attempt and
  an exclusive ``flock`` is taken on it. The descriptor is inherited by the
  whole process group, so the lock is held precisely as long as any process of
  that group is alive -- across a DebridPulse restart too -- and it is released
  by the kernel when the last one exits. No command string is matched and no
  PID is trusted on its own: the lock is the liveness proof, and the recorded
  process-group id is only acted on while the lock proves the group exists.
* A start whose attempt still has a live group is refused -- a lost or
  uncertain start acknowledgement can never produce a second group. A marker
  whose lock is free names a group that is gone.
* Transient secrets reach the process only through one-shot pipes -- its
  standard input, or inherited descriptors -- never argv, the environment or
  a file.
* The process runs in its own session (no controlling terminal, so nothing can
  prompt) with a minimal environment: no inherited DebridPulse configuration,
  no operator shell state, and an empty private home.
* The group's identity is durable before its program runs. The child first
  waits at a one-byte gate; DebridPulse records its group id and only then
  opens the gate. If recording fails, or DebridPulse dies before it opens the
  gate -- even without any cleanup running -- the gate reads end-of-file and
  the child exits having done nothing, so no live group is ever unidentified.

On Windows the same contract is kept with that platform's own primitives,
inside this module:

* The marker is held open with a share mode that refuses every other writer
  or deleter (instead of ``flock``), by a handle the gate and the program
  inherit -- so it is held exactly as long as one of them is alive -- and read
  by anyone; ``alive()`` is a write-open that fails with a sharing violation.
* The group is a per-attempt **Job Object**, named after the marker, which no
  member may leave. The gate is assigned to it while it still waits at the
  gate, and checks for itself that it is in the job before it starts the
  program. It holds a query-only handle to the job (inherited by the program
  too), so the job -- and its name -- outlive a DebridPulse crash for as long
  as any member lives, and a restarted DebridPulse re-opens it by name.
* Windows cannot replace a process image, so the gate starts the program,
  waits until every process of the job but itself has ended, and exits with
  the program's code; the recorded group id is the gate's process id.
* There is no signal a whole job can receive: ``terminate`` ends the job
  (``TerminateJobObject``), which is forced, never graceful.
* Secrets travel in pipes created at their size, inherited by handle; the
  handle values take the place of descriptor numbers in the argv.
"""
from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_WINDOWS = os.name == "nt"
if _WINDOWS:
    import ctypes
    import msvcrt
    from ctypes import wintypes
else:
    import fcntl

_PIPE_CAPACITY = 64 * 1024
# The longest native output line read as one line (``asyncio``'s own default).
OUTPUT_LINE_LIMIT = 64 * 1024
_OUTPUT_CHUNK = 64 * 1024
# The one-byte identity gate: the child waits here until its group id is
# durable, then becomes the real program; end-of-file means nobody recorded it.
# Before it becomes the program it hands back the signal dispositions Python
# changed at its own start (as ``subprocess`` does for every child), so the
# program starts exactly as it would have without the gate.
_GATE = (
    "import os, signal, sys\n"
    "for name in ('SIGPIPE', 'SIGXFSZ'):\n"
    "    if hasattr(signal, name):\n"
    "        signal.signal(getattr(signal, name), signal.SIG_DFL)\n"
    "gate = int(sys.argv[1])\n"
    "opened = os.read(gate, 1) == b'1'\n"
    "os.close(gate)\n"
    "if not opened:\n"
    "    os._exit(125)\n"
    "os.execv(sys.argv[2], sys.argv[3:])\n"
)
# The Windows gate (argv: gate handle, job handle, handles the program
# inherits, program, its argv). It runs nothing until it is opened AND it is
# itself a member of the attempt's job; then it starts the program -- which
# joins the job, and cannot leave it -- and ends only once every process of
# the job but itself has ended, with the program's exit code. The gate has no
# console (its console host would be a member of the job for as long as the
# gate lives); the program gets a hidden one, whose host ends with it, and
# the gate's own standard handles -- DebridPulse's pipes -- named explicitly
# (left unnamed, the program would read and write its new console instead).
_WINDOWS_GATE = (
    "import ctypes, msvcrt, os, subprocess, sys, time\n"
    "from ctypes import wintypes\n"
    "gate = msvcrt.open_osfhandle(int(sys.argv[1]), os.O_RDONLY)\n"
    "opened = os.read(gate, 1) == b'1'\n"
    "os.close(gate)\n"
    "kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)\n"
    "kernel32.GetCurrentProcess.restype = wintypes.HANDLE\n"
    "kernel32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]\n"
    "kernel32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,\n"
    "                                               wintypes.DWORD, ctypes.c_void_p]\n"
    "job = int(sys.argv[2])\n"
    "member = wintypes.BOOL()\n"
    "if not opened or not kernel32.IsProcessInJob(kernel32.GetCurrentProcess(), job, ctypes.byref(member)) \\\n"
    "        or not member.value:\n"
    "    os._exit(125)\n"
    "startup = subprocess.STARTUPINFO()\n"
    "startup.lpAttributeList = {'handle_list': [int(value) for value in sys.argv[3].split(',') if value]}\n"
    "program = subprocess.Popen(sys.argv[5:], executable=sys.argv[4], startupinfo=startup, close_fds=True,\n"
    "                           stdin=0, stdout=1, stderr=2, creationflags=subprocess.CREATE_NO_WINDOW)\n"
    "code = program.wait()\n"
    "accounting = (ctypes.c_ulong * 12)()   # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION\n"
    "while kernel32.QueryInformationJobObject(job, 1, accounting, ctypes.sizeof(accounting), None) \\\n"
    "        and accounting[10] > 1:          # ActiveProcesses: anyone but this gate\n"
    "    time.sleep(0.05)\n"
    "os._exit(code)\n"
)


async def output_lines(stream, *, limit: int = OUTPUT_LINE_LIMIT, on_overlong=None):
    """Every complete line a native process writes to ``stream`` (each ending
    in its newline; a final unterminated one too), read in bounded chunks.

    A line longer than ``limit`` is consumed and dropped whole -- never yielded,
    in part or in full, so no fragment of it can be taken for a protocol
    record -- and reported to ``on_overlong``. The stream keeps being read
    either way: a reader that gave up would leave the writer blocked on a
    full pipe."""
    buffer = bytearray()
    skipping = False
    while chunk := await stream.read(_OUTPUT_CHUNK):
        buffer += chunk
        while (end := buffer.find(b"\n")) >= 0:
            line = bytes(buffer[:end + 1])
            del buffer[:end + 1]
            if skipping:
                skipping = False
            elif len(line) > limit:
                if on_overlong is not None:
                    on_overlong()
            else:
                yield line
        if len(buffer) > limit:
            if not skipping and on_overlong is not None:
                on_overlong()
            skipping = True
            buffer.clear()
    if buffer and not skipping:
        yield bytes(buffer)


class ProcessGroupAlive(Exception):
    """The attempt already owns a live native process group."""


@dataclass
class OwnedProcess:
    """The in-memory handle of a group this DebridPulse process started."""
    attempt_id: str
    process: asyncio.subprocess.Process
    group: int


class ProcessOwnership:
    def __init__(self, runtime_dir: str | os.PathLike):
        # Nothing is created until a process is first owned: registering an
        # executor never touches storage.
        self.root = Path(runtime_dir)
        self.markers = self.root / "processes"
        self.home = self.root / "home"

    def _prepare(self) -> None:
        for path in (self.root, self.markers, self.home):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)

    # ── identity ────────────────────────────────────────────────────────────

    def marker(self, attempt_id: str) -> Path:
        digest = hashlib.sha256(str(attempt_id).encode("utf-8")).hexdigest()[:32]
        return self.markers / f"{digest}.lock"

    @staticmethod
    def _job_name(marker: Path) -> str:
        """The name of a marker's Windows Job Object: derived from the marker,
        so a restarted DebridPulse names the same job."""
        digest = hashlib.sha256(os.path.normcase(str(marker)).encode("utf-8")).hexdigest()[:32]
        return f"Local\\DebridPulse-attempt-{digest}"

    def alive(self, attempt_id: str) -> bool | None:
        """``True`` while a group of this attempt lives, ``False`` once none
        does, ``None`` when the attempt never started one here."""
        return self._alive(self.marker(attempt_id))

    def _alive(self, path: Path) -> bool | None:
        """``alive`` judged by the marker itself, which needs no readable
        record (``stop`` judges every marker it finds, readable or not)."""
        if _WINDOWS:
            # A write-open fails with a sharing violation while any member
            # holds the marker; a member still in the job counts all the same.
            handle, error = _open_marker(str(path), _GENERIC_WRITE, _SHARE_ALL, _OPEN_EXISTING)
            if handle is None:
                if error in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND):
                    return None
                if error == _ERROR_SHARING_VIOLATION:
                    return True
                raise _windows_error(error, str(path))
            _kernel32.CloseHandle(handle)
            job = _kernel32.OpenJobObjectW(_JOB_QUERY, False, self._job_name(path))
            if not job:
                return False
            try:
                return _job_active(job) > 0
            finally:
                _kernel32.CloseHandle(job)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    def recorded_group(self, attempt_id: str) -> int | None:
        try:
            value = json.loads(self.marker(attempt_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        group = value.get("group") if isinstance(value, dict) else None
        return group if isinstance(group, int) and group > 1 else None

    def forget(self, attempt_id: str) -> None:
        """Drop the marker of an attempt whose group is proven gone."""
        if self.alive(attempt_id) is False:
            try:
                self.marker(attempt_id).unlink()
            except FileNotFoundError:
                pass
            except PermissionError:
                # Windows: a group started again since the check holds the
                # marker against deletion, so it is (rightly) kept.
                if not _WINDOWS:
                    raise

    # ── lifecycle ───────────────────────────────────────────────────────────

    def environment(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """A minimal native environment: a fixed PATH and C locale (stable
        diagnostics), an empty private home, and only what the caller adds."""
        env = {"PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin",
               "LC_ALL": "C", "LANG": "C", "HOME": str(self.home)}
        if _WINDOWS:
            # Windows programs find their home, profile and temporary files
            # through these instead of HOME: all of them are the private home.
            # SYSTEMROOT is required by the C runtime and Winsock to start.
            env.update({key: str(self.home) for key in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP")})
            env.update({key: os.environ[key] for key in ("SYSTEMROOT", "PATHEXT") if os.environ.get(key)})
        env.update({str(key): str(value) for key, value in (extra or {}).items()})
        return env

    async def spawn(self, attempt_id: str, argv: list[str], *, env: dict[str, str],
                    secrets: dict[str, bytes] | None = None, stdin: bytes | None = None,
                    pass_fds: tuple[int, ...] = (), fd_argv=None) -> OwnedProcess:
        """Start the attempt's one native group. ``secrets`` become inherited
        one-shot pipes and ``fd_argv(descriptors)`` may place their descriptor
        numbers into the argv; ``stdin`` becomes a one-shot pipe on standard
        input (otherwise it is empty). Raises ``ProcessGroupAlive`` rather than
        start a second group."""
        self._prepare()
        path = self.marker(attempt_id)
        if _WINDOWS:
            # The marker held so no one else can write or delete it; the
            # group a per-attempt Job Object no member can leave.
            handle, error = _open_marker(str(path), _GENERIC_READ | _GENERIC_WRITE, _SHARE_READ, _OPEN_ALWAYS)
            if handle is None:
                if error == _ERROR_SHARING_VIOLATION:
                    raise ProcessGroupAlive(attempt_id)
                raise _windows_error(error, str(path))
            lock = msvcrt.open_osfhandle(handle, os.O_RDWR)
            pipes: list[int] = []
            job = query = None
            try:
                name = self._job_name(path)
                job = _kernel32.CreateJobObjectW(None, name)
                if not job:
                    raise _windows_error(ctypes.get_last_error(), name)
                if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
                    # A job of this name outlives its gate only while a member
                    # lives; one with none is never reused.
                    if _job_active(job) > 0:
                        raise ProcessGroupAlive(attempt_id)
                    raise OSError(errno.EEXIST, "The attempt's job is held open by another process", name)
                query = wintypes.HANDLE()
                current = _kernel32.GetCurrentProcess()
                if not _kernel32.DuplicateHandle(current, job, current, ctypes.byref(query), _JOB_QUERY, False, 0):
                    raise _windows_error(ctypes.get_last_error(), name)
                query = query.value
                descriptors = {}
                for key, value in (secrets or {}).items():
                    descriptors[key] = msvcrt.get_osfhandle(self._pipe(value, pipes))
                standard_input = self._pipe(stdin, pipes) if stdin is not None else asyncio.subprocess.DEVNULL
                handed = [msvcrt.get_osfhandle(descriptor) for descriptor in pass_fds]
                if fd_argv is not None:
                    argv = fd_argv(descriptors)
                program = shutil.which(str(argv[0]), path=env.get("PATH")) if argv else None
                if program is None:
                    raise FileNotFoundError(argv[0] if argv else "")
                if program.lower().endswith((".bat", ".cmd")):
                    # Windows runs a batch file only through cmd.exe, whose own
                    # parsing of the command line no argv quoting can make safe.
                    raise PermissionError(errno.EACCES, "a batch file is never started", program)
                gate, opener = os.pipe()
                pipes.append(gate)
                pipes.append(opener)
                inherited = [msvcrt.get_osfhandle(lock), query, *descriptors.values(), *handed]
                for item in (msvcrt.get_osfhandle(gate), *inherited):
                    os.set_handle_inheritable(item, True)
                os.ftruncate(lock, 0)
                os.write(lock, json.dumps({"attempt": str(attempt_id)}).encode("utf-8"))
                startup = subprocess.STARTUPINFO()
                startup.lpAttributeList = {"handle_list": [msvcrt.get_osfhandle(gate), *inherited]}
                process = await asyncio.create_subprocess_exec(
                    _GATE_INTERPRETER, "-I", "-S", "-c", _WINDOWS_GATE, str(msvcrt.get_osfhandle(gate)), str(query),
                    ",".join(str(item) for item in inherited), program, *argv,
                    stdin=standard_input, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
                    cwd=str(self.home), startupinfo=startup, close_fds=True,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)
                try:
                    # The gate joins the job while it still waits, then its
                    # identity is recorded; it checks its own membership itself.
                    member = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, process.pid)
                    if not member:
                        raise _windows_error(ctypes.get_last_error())
                    try:
                        if not _kernel32.AssignProcessToJobObject(job, member):
                            raise _windows_error(ctypes.get_last_error())
                    finally:
                        _kernel32.CloseHandle(member)
                    os.ftruncate(lock, 0)
                    os.lseek(lock, 0, os.SEEK_SET)
                    os.write(lock, json.dumps({"attempt": str(attempt_id), "group": process.pid}).encode("utf-8"))
                except BaseException:
                    # Unjoined or unrecorded: the gate stays shut (closing it
                    # below is its end-of-file) and is ended, having run nothing.
                    _kernel32.TerminateJobObject(job, 1)
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    raise
                os.write(opener, b"1")
                return OwnedProcess(str(attempt_id), process, process.pid)
            finally:
                for descriptor in pipes:
                    os.close(descriptor)
                os.close(lock)
                for item in (job, query):
                    if item:
                        _kernel32.CloseHandle(item)
        lock = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        pipes: list[int] = []
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ProcessGroupAlive(attempt_id) from None
            descriptors = {}
            for name, value in (secrets or {}).items():
                descriptors[name] = self._pipe(value, pipes)
            standard_input = self._pipe(stdin, pipes) if stdin is not None else asyncio.subprocess.DEVNULL
            if fd_argv is not None:
                argv = fd_argv(descriptors)
            program = shutil.which(str(argv[0]), path=env.get("PATH")) if argv else None
            if program is None:
                raise FileNotFoundError(argv[0] if argv else "")
            gate, opener = os.pipe()
            pipes.append(gate)
            pipes.append(opener)
            os.ftruncate(lock, 0)
            os.write(lock, json.dumps({"attempt": str(attempt_id)}).encode("utf-8"))
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-S", "-c", _GATE, str(gate), program, *argv,
                stdin=standard_input, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=env, cwd=str(self.home), start_new_session=True,
                pass_fds=(lock, gate, *pipes[:-2], *pass_fds), close_fds=True)
            try:
                # The group id is recorded while the child still waits at the
                # gate; the lock proves the group, the record names it.
                os.ftruncate(lock, 0)
                os.pwrite(lock, json.dumps({"attempt": str(attempt_id), "group": process.pid}).encode("utf-8"), 0)
            except BaseException:
                # Unrecorded: the gate stays shut (closing it below is its
                # end-of-file) and the group, which has run nothing, is ended.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                raise
            os.write(opener, b"1")
            return OwnedProcess(str(attempt_id), process, process.pid)
        finally:
            for descriptor in pipes:
                os.close(descriptor)
            # The group holds the lock from here on; releasing this descriptor
            # never releases theirs.
            os.close(lock)

    @staticmethod
    def _pipe(value: bytes, pipes: list[int]) -> int:
        """A pipe already holding ``value`` and closed for writing.

        Nothing reads it until the child runs, so it is filled without ever
        blocking: a pipe the kernel granted too small for ``value`` (as it
        does under its per-user pipe quota) is enlarged if the kernel allows,
        and otherwise refused -- never a wait the child cannot end."""
        if len(value) > _PIPE_CAPACITY:
            raise ValueError("transient secret exceeds one pipe")
        # Windows: a pipe asked for at the input's size (``CreatePipe``);
        # elsewhere the system's pipe, enlarged where the system can.
        read, write = _create_pipe(len(value)) if _WINDOWS else os.pipe()
        pipes.append(read)
        try:
            os.set_blocking(write, False)
            try:
                # Linux alone can size a pipe; macOS has neither constant.
                if (not _WINDOWS and hasattr(fcntl, "F_GETPIPE_SZ")
                        and fcntl.fcntl(write, fcntl.F_GETPIPE_SZ) < len(value)):
                    fcntl.fcntl(write, fcntl.F_SETPIPE_SZ, len(value))
            except OSError:
                pass
            written = 0
            while written < len(value):
                try:
                    written += os.write(write, value[written:])
                except BlockingIOError:
                    raise ValueError("transient secret exceeds the pipe the system granted") from None
        finally:
            os.close(write)
        return read

    async def terminate(self, owned: OwnedProcess | None, attempt_id: str, *, grace: float = 5.0) -> bool:
        """Stop the attempt's group and return whether it is PROVEN gone."""
        group = owned.group if owned is not None else self.recorded_group(attempt_id)
        if self.alive(attempt_id) is not True and (owned is None or owned.process.returncode is not None):
            return True
        if _WINDOWS:
            # No signal reaches a whole job: it is ended, forced, every member
            # at once. Only members hold the job, so none left means none alive.
            job = _kernel32.OpenJobObjectW(_JOB_QUERY | _JOB_TERMINATE, False,
                                           self._job_name(self.marker(attempt_id)))
            if not job:
                return await self._gone(owned, attempt_id, 0.1)
            try:
                for _attempt in range(2):
                    _kernel32.TerminateJobObject(job, 1)
                    if await self._gone(owned, attempt_id, grace):
                        return True
                return False
            finally:
                _kernel32.CloseHandle(job)
        if group is None:
            return False
        for signum, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, grace)):
            try:
                os.killpg(group, signum)
            except ProcessLookupError:
                pass
            except PermissionError:
                return False
            if await self._gone(owned, attempt_id, wait):
                return True
        return False

    async def _gone(self, owned: OwnedProcess | None, attempt_id: str, wait: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.1, float(wait))
        while True:
            if owned is not None and owned.process.returncode is None:
                try:
                    await asyncio.wait_for(asyncio.shield(owned.process.wait()), timeout=0.05)
                except TimeoutError:
                    pass
            if (owned is None or owned.process.returncode is not None) and self.alive(attempt_id) is not True:
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.05)

    # ── application lifecycle (``integrations.definition.IntegrationLifecycle``) ──

    async def start(self) -> None:
        """Nothing to start: a group exists only once an attempt spawns it."""

    async def maintain(self) -> None:
        """Nothing to maintain: liveness is read from the markers when asked."""

    async def stop(self) -> None:
        """The graceful application stop -- never a pause: every group this
        owner's markers name, started by this process or found after a
        restart, is asked to end through ``terminate``, each whatever another
        does. Nothing durable changes; after a restart the attempt has no
        group, and canonical recovery retries it.

        The stop succeeds only when every marker is proven to name no live
        group. It fails -- after every identifiable group was asked to end --
        when the markers cannot be listed, when a group is not proven gone, or
        when a marker whose record names no attempt is not proven free."""
        try:
            names = sorted(entry.name for entry in os.scandir(self.markers) if entry.name.endswith(".lock"))
        except FileNotFoundError:
            names = []                                    # no group was ever started here
        except OSError as exc:
            raise RuntimeError("the attempt-owned process markers could not be listed") from exc
        attempts, unresolved = [], []
        for name in names:
            path = self.markers / name
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                value = None
            attempt = value.get("attempt") if isinstance(value, dict) else None
            if isinstance(attempt, str) and self.marker(attempt) == path:
                attempts.append(attempt)
                continue
            # A record that names no attempt: proven free, it names no group;
            # held (or not judgeable) it names one no one can identify.
            try:
                held = self._alive(path)
            except OSError:
                held = True
            if held is True:
                unresolved.append(name)
        outcomes = await asyncio.gather(*(self.terminate(None, attempt) for attempt in attempts),
                                        return_exceptions=True)
        failed = sum(1 for outcome in outcomes if outcome is not True)
        if failed or unresolved:
            raise RuntimeError(f"attempt-owned process groups not proven stopped: {failed} of {len(attempts)}"
                               f" identified, {len(unresolved)} unidentifiable")


# ── Windows primitives: the documented kernel32 calls the owner uses ────────

if _WINDOWS:
    _GENERIC_READ, _GENERIC_WRITE = 0x80000000, 0x40000000
    _SHARE_READ, _SHARE_ALL = 0x1, 0x7
    _OPEN_EXISTING, _OPEN_ALWAYS = 3, 4
    _FLAG_OPEN_REPARSE_POINT, _ATTRIBUTE_REPARSE_POINT = 0x00200000, 0x400
    _JOB_QUERY, _JOB_TERMINATE = 0x0004, 0x0008
    _PROCESS_TERMINATE, _PROCESS_SET_QUOTA = 0x0001, 0x0100
    _ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND, _ERROR_SHARING_VIOLATION, _ERROR_ALREADY_EXISTS = 2, 3, 32, 183
    _INVALID_HANDLE = ctypes.c_void_p(-1).value
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    for _name, _restype, _argtypes in (
            ("CreateFileW", wintypes.HANDLE, [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]),
            ("GetFileInformationByHandle", wintypes.BOOL, [wintypes.HANDLE, ctypes.c_void_p]),
            ("CloseHandle", wintypes.BOOL, [wintypes.HANDLE]),
            ("CreatePipe", wintypes.BOOL, [ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE),
                                           ctypes.c_void_p, wintypes.DWORD]),
            ("CreateJobObjectW", wintypes.HANDLE, [ctypes.c_void_p, wintypes.LPCWSTR]),
            ("OpenJobObjectW", wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]),
            ("AssignProcessToJobObject", wintypes.BOOL, [wintypes.HANDLE, wintypes.HANDLE]),
            ("TerminateJobObject", wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT]),
            ("QueryInformationJobObject", wintypes.BOOL, [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                          wintypes.DWORD, ctypes.c_void_p]),
            ("OpenProcess", wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]),
            ("GetCurrentProcess", wintypes.HANDLE, []),
            ("DuplicateHandle", wintypes.BOOL, [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                                ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL,
                                                wintypes.DWORD])):
        getattr(_kernel32, _name).restype = _restype
        getattr(_kernel32, _name).argtypes = _argtypes
    # The gate must be the interpreter itself: a virtual environment's
    # ``python.exe`` is a launcher that starts the interpreter as its own child
    # at once, before the gate could join the job (the gate's own membership
    # check refuses that case either way).
    _GATE_INTERPRETER = (sys.executable if sys.prefix == sys.base_prefix
                         else os.path.join(sys.base_exec_prefix, "python.exe"))

    def _windows_error(code: int, path: str | None = None) -> OSError:
        return OSError(0, ctypes.FormatError(code).strip(), path, code)

    def _open_marker(path: str, access: int, share: int, disposition: int):
        """``CreateFileW`` on the marker itself, never a reparse point's target
        (``O_NOFOLLOW``): ``(handle, 0)``, or ``(None, error)``. A marker that is
        a link raises ``ELOOP``, as ``O_NOFOLLOW`` does."""
        handle = _kernel32.CreateFileW(path, access, share, None, disposition, _FLAG_OPEN_REPARSE_POINT, None)
        if handle is None or handle == _INVALID_HANDLE:
            return None, ctypes.get_last_error()
        information = (ctypes.c_ulong * 13)()             # BY_HANDLE_FILE_INFORMATION
        if not _kernel32.GetFileInformationByHandle(handle, information) \
                or information[0] & _ATTRIBUTE_REPARSE_POINT:
            _kernel32.CloseHandle(handle)
            raise OSError(errno.ELOOP, "A marker that is a link is never used", path)
        return handle, 0

    def _job_active(job) -> int:
        """``ActiveProcesses`` of ``JOBOBJECT_BASIC_ACCOUNTING_INFORMATION``."""
        accounting = (ctypes.c_ulong * 12)()
        if not _kernel32.QueryInformationJobObject(job, 1, accounting, ctypes.sizeof(accounting), None):
            raise _windows_error(ctypes.get_last_error())
        return int(accounting[10])

    def _create_pipe(size: int) -> tuple[int, int]:
        """An anonymous pipe asked for at ``size`` bytes (``os.pipe`` takes no
        size), as binary, non-inheritable descriptors ``(read, write)``."""
        read, write = wintypes.HANDLE(), wintypes.HANDLE()
        if not _kernel32.CreatePipe(ctypes.byref(read), ctypes.byref(write), None, max(int(size), 1)):
            raise _windows_error(ctypes.get_last_error())
        try:
            read_fd = msvcrt.open_osfhandle(read.value, os.O_RDONLY)
        except BaseException:
            _kernel32.CloseHandle(read)
            _kernel32.CloseHandle(write)
            raise
        try:
            return read_fd, msvcrt.open_osfhandle(write.value, os.O_WRONLY)
        except BaseException:
            os.close(read_fd)
            _kernel32.CloseHandle(write)
            raise
