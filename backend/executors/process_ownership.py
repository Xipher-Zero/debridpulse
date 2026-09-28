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
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import signal
from dataclasses import dataclass
from pathlib import Path

_PIPE_CAPACITY = 64 * 1024


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

    def alive(self, attempt_id: str) -> bool | None:
        """``True`` while a group of this attempt lives, ``False`` once none
        does, ``None`` when the attempt never started one here."""
        path = self.marker(attempt_id)
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

    # ── lifecycle ───────────────────────────────────────────────────────────

    def environment(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """A minimal native environment: a fixed PATH and C locale (stable
        diagnostics), an empty private home, and only what the caller adds."""
        env = {"PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin",
               "LC_ALL": "C", "LANG": "C", "HOME": str(self.home)}
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
            os.ftruncate(lock, 0)
            os.write(lock, json.dumps({"attempt": str(attempt_id)}).encode("utf-8"))
            process = await asyncio.create_subprocess_exec(
                *argv, stdin=standard_input, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=env, cwd=str(self.home), start_new_session=True,
                pass_fds=(lock, *pipes, *pass_fds), close_fds=True)
            # The group id is recorded only once it exists; the lock proves it.
            os.ftruncate(lock, 0)
            os.pwrite(lock, json.dumps({"attempt": str(attempt_id), "group": process.pid}).encode("utf-8"), 0)
            return OwnedProcess(str(attempt_id), process, process.pid)
        finally:
            for descriptor in pipes:
                os.close(descriptor)
            # The group holds the lock from here on; releasing this descriptor
            # never releases theirs.
            os.close(lock)

    @staticmethod
    def _pipe(value: bytes, pipes: list[int]) -> int:
        """A pipe already holding ``value`` and closed for writing."""
        if len(value) > _PIPE_CAPACITY:
            raise ValueError("transient secret exceeds one pipe")
        read, write = os.pipe()
        pipes.append(read)
        try:
            os.write(write, value)
        finally:
            os.close(write)
        return read

    async def terminate(self, owned: OwnedProcess | None, attempt_id: str, *, grace: float = 5.0) -> bool:
        """Stop the attempt's group and return whether it is PROVEN gone."""
        group = owned.group if owned is not None else self.recorded_group(attempt_id)
        if self.alive(attempt_id) is not True and (owned is None or owned.process.returncode is not None):
            return True
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
