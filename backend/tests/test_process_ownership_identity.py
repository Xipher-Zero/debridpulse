"""1.0.13 WS5a: an owned native group is identified before it runs, and its
one-shot inputs never block the event loop.

FUNC-003: the group id used to be recorded only after the program had already
started, so a failed record write or a DebridPulse crash in between left a
live group no one could name or stop. The child now waits at a one-byte gate
until its id is durable; a failed record or a crash leaves the gate at
end-of-file and the child exits having run nothing.

FUNC-002: one-shot inputs are written before the child exists. A pipe the
kernel granted smaller than the input (as it does under its per-user pipe
quota) used to block the whole event loop forever; it is now enlarged when
the kernel allows, and otherwise refused at once.

Every case that could hang or orphan on a regression runs in its own
process under a hard ceiling, with short-lived children.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from executors.process_ownership import ProcessGroupAlive, ProcessOwnership

BACKEND = Path(__file__).resolve().parents[1]


def _child(script: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", textwrap.dedent(script)], cwd=BACKEND, capture_output=True,
                          text=True, timeout=timeout)


def _until(predicate, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


_SMALL_PIPES = """
    import asyncio, errno, fcntl, os, sys, time
    sys.path.insert(0, os.getcwd())
    import executors.process_ownership as po
    real_pipe, real_fcntl = os.pipe, fcntl.fcntl
    def small_pipe():
        read, write = real_pipe()
        real_fcntl(write, fcntl.F_SETPIPE_SZ, 4096)
        return read, write
    po.os.pipe = small_pipe
    if {refuse_growth}:
        def no_growth(fd, cmd, *args):
            if cmd == fcntl.F_SETPIPE_SZ:
                raise PermissionError(errno.EPERM, "pipe quota")
            return real_fcntl(fd, cmd, *args)
        po.fcntl.fcntl = no_growth
    async def main():
        owner = po.ProcessOwnership({runtime!r})
        gaps, last = [], [time.monotonic()]
        async def beat():
            while True:
                await asyncio.sleep(0.01)
                now = time.monotonic(); gaps.append(now - last[0]); last[0] = now
        task = asyncio.create_task(beat()); await asyncio.sleep(0.05)
        before = len(os.listdir("/proc/self/fd"))
        data = b"s" * 12288
        try:
            owned = await owner.spawn("a", ["sh", "-c", "wc -c > {out}"], env=owner.environment(), stdin=data)
            await owned.process.wait()
            outcome = "spawned"
        except ValueError as exc:
            outcome = "refused: " + str(exc)
        await asyncio.sleep(0.05); task.cancel()
        print(outcome, "|", round(max(gaps), 3), "|", len(os.listdir("/proc/self/fd")) - before, "|",
              owner.alive("a"))
    asyncio.run(main())
"""


def test_a_pipe_too_small_that_cannot_grow_is_refused_at_once_without_blocking_the_loop(tmp_path):
    out = tmp_path / "received"
    result = _child(_SMALL_PIPES.format(refuse_growth=True, runtime=str(tmp_path / "rt"), out=str(out)))
    outcome, gap, leaked, alive = (part.strip() for part in result.stdout.split("|"))
    assert outcome.startswith("refused: transient secret exceeds the pipe the system granted"), result.stderr
    assert float(gap) < 0.5          # the event loop kept running
    assert leaked == "0"             # every descriptor closed again
    assert alive != "True"           # nothing holds the attempt's lock
    assert not out.exists()          # nothing ran


def test_a_pipe_too_small_that_can_grow_delivers_the_whole_input(tmp_path):
    out = tmp_path / "received"
    result = _child(_SMALL_PIPES.format(refuse_growth=False, runtime=str(tmp_path / "rt"), out=str(out)))
    outcome, _gap, leaked, _alive = (part.strip() for part in result.stdout.split("|"))
    assert outcome == "spawned", result.stderr
    assert leaked == "0"
    assert out.read_text().strip() == "12288"


@pytest.mark.asyncio
async def test_a_failed_identity_record_leaves_no_live_group_and_runs_nothing(tmp_path, monkeypatch):
    import executors.process_ownership as po

    owner = ProcessOwnership(tmp_path / "rt")
    ran = tmp_path / "ran"

    def disk_full(*_args):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(po.os, "pwrite", disk_full)
    with pytest.raises(OSError):
        await owner.spawn("a", ["sh", "-c", f"touch {ran}; sleep 3"], env=owner.environment())
    monkeypatch.undo()
    assert _until(lambda: owner.alive("a") is False)
    assert not ran.exists()
    owner.forget("a")
    again = await owner.spawn("a", ["sh", "-c", "exit 0"], env=owner.environment())  # never ProcessGroupAlive
    await again.process.wait()


def test_a_crash_before_the_identity_is_recorded_leaves_no_live_group_and_runs_nothing(tmp_path):
    runtime, ran = tmp_path / "rt", tmp_path / "ran"
    result = _child(f"""
        import asyncio, os, sys
        sys.path.insert(0, os.getcwd())
        import executors.process_ownership as po
        po.os.pwrite = lambda *args: os._exit(0)   # DebridPulse dies right after the child exists
        owner = po.ProcessOwnership({str(runtime)!r})
        asyncio.run(owner.spawn("a", ["sh", "-c", "touch {ran}; sleep 3"], env=owner.environment()))
    """)
    assert result.returncode == 0, result.stderr
    owner = ProcessOwnership(runtime)
    assert _until(lambda: owner.alive("a") is False)
    assert not ran.exists()


@pytest.mark.asyncio
async def test_the_program_runs_as_itself_under_a_recorded_group(tmp_path):
    owner = ProcessOwnership(tmp_path / "rt")
    out = tmp_path / "out"
    owned = await owner.spawn("a", ["sh", "-c", f'printf "%s %s" "$0" "$(ps -o pgid= -p $$ | tr -d " ")" > {out}'],
                              env=owner.environment())
    assert await owned.process.wait() == 0
    name, group = out.read_text().split()
    assert name == "sh"                                   # the original argv[0], unchanged
    assert int(group) == owned.group == owner.recorded_group("a")


@pytest.mark.asyncio
async def test_a_missing_program_is_still_refused_before_anything_starts(tmp_path):
    owner = ProcessOwnership(tmp_path / "rt")
    with pytest.raises(FileNotFoundError):
        await owner.spawn("a", ["dp-no-such-program-xyz"], env=owner.environment())
    assert owner.alive("a") is not True
    with pytest.raises(ProcessGroupAlive):
        held = await owner.spawn("b", ["sh", "-c", "sleep 2"], env=owner.environment())
        await owner.spawn("b", ["sh", "-c", "exit 0"], env=owner.environment())
    assert await owner.terminate(held, "b")
    await asyncio.sleep(0)
    assert os.path.exists(owner.marker("b"))
