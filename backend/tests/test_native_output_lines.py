"""1.0.13 WS5c PA-PROC-3/4: native output is always drained, never trusted past its bound.

Every native-output reader (aria2, SABnzbd, rsync, Media Downloads) used
``StreamReader.readline()``, which raises on a line longer than 64 KiB: the
reader died (aria2, rsync, media) or quietly stopped (SABnzbd), nothing read
the pipe any more, and the child blocked on its next write -- with
``Process.wait()`` then unable to return, which let aria2's failed-start
cleanup wait forever under its runtime lock. One bounded line reader
(``executors.process_ownership.output_lines``) now keeps reading, drops an
over-long line whole (so no fragment of it is ever taken for a record), and
the cleanup wait is bounded.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

from executors.process_ownership import OUTPUT_LINE_LIMIT, ProcessOwnership, output_lines

pytestmark = pytest.mark.asyncio

CHILD = """
import sys
stream = (sys.stdout if sys.argv[1] == "stdout" else sys.stderr).buffer
stream.write(sys.argv[3].encode() * 70000 + b"\\n"); stream.flush()
for _ in range(1024):
    stream.write(b"B" * 1023 + b"\\n")
stream.flush()
open(sys.argv[2], "w").write("done")
"""


async def _lines(chunks: list[bytes], **kwargs) -> list[bytes]:
    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk)
    reader.feed_eof()
    return [line async for line in output_lines(reader, **kwargs)]


async def test_lines_are_read_whole_and_an_over_long_one_is_dropped_whole():
    dropped = []
    lines = await _lines([b"one\n", b"x" * 70000 + b"\n", b"two\n", b"thr", b"ee\n", b"tail"],
                         on_overlong=lambda: dropped.append(1))
    assert lines == [b"one\n", b"two\n", b"three\n", b"tail"]
    assert dropped == [1]


async def test_no_fragment_of_an_over_long_line_is_ever_yielded():
    """The bound is passed before the newline arrives; what follows -- shaped
    like a protocol record -- is still the same over-long line."""
    dropped = []
    lines = await _lines([b"x" * (OUTPUT_LINE_LIMIT + 10), b"x" * 5000, b"dp-transfer-begun 5\n", b"after\n"],
                         on_overlong=lambda: dropped.append(1))
    assert lines == [b"after\n"] and dropped == [1]


async def test_a_line_at_the_limit_is_kept_and_an_unterminated_over_long_end_is_not():
    exact = b"y" * (OUTPUT_LINE_LIMIT - 1) + b"\n"
    assert await _lines([exact, b"z" * (OUTPUT_LINE_LIMIT + 1)]) == [exact]


async def _aria2(stream_name, marker, mark):
    from executors.aria2.runtime import Aria2Runtime
    runtime = Aria2Runtime()
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", CHILD, stream_name, marker, mark,
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    other = process.stderr if stream_name == "stdout" else process.stdout
    tasks = [asyncio.ensure_future(runtime._drain_stream(getattr(process, stream_name), stream_name)),
             asyncio.ensure_future(runtime._drain_stream(other, "other"))]
    return process, tasks, lambda: list(runtime._last_output)


async def _sabnzbd(stream_name, marker, mark):
    from executors.sabnzbd.runtime import UsenetRuntime
    runtime = UsenetRuntime()
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", CHILD, stream_name, marker, mark,
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    other = process.stderr if stream_name == "stdout" else process.stdout
    tasks = [asyncio.ensure_future(runtime._drain(getattr(process, stream_name), stream_name)),
             asyncio.ensure_future(runtime._drain(other, "other"))]
    return process, tasks, lambda: dict(runtime._suppressed_output)


async def _owned(tmp_path, owner_class, marker, mark, **run_fields):
    owner = ProcessOwnership(tmp_path / "runtime")
    owned = await owner.spawn("attempt", [sys.executable, "-c", CHILD, "stdout", marker, mark],
                              env=owner.environment())
    run = types.SimpleNamespace(owned=owned, stderr=bytearray(), **run_fields)
    tasks = [asyncio.ensure_future(owner_class._follow(None, run)),
             asyncio.ensure_future(owner_class._pump_stderr(None, run))]
    return owned.process, tasks, lambda: run


@pytest.mark.parametrize("reader", ["aria2-stdout", "aria2-stderr", "sabnzbd-stdout", "sabnzbd-stderr",
                                    "rsync", "media"])
async def test_every_native_reader_keeps_draining_past_an_over_long_line(tmp_path, reader):
    marker = str(tmp_path / "finished")
    if reader.startswith("aria2"):
        process, tasks, state = await _aria2(reader.split("-")[1], marker, "A")
    elif reader.startswith("sabnzbd"):
        process, tasks, state = await _sabnzbd(reader.split("-")[1], marker, "A")
    elif reader == "rsync":
        from executors.rsync.executor import RsyncExecutor
        process, tasks, state = await _owned(tmp_path, RsyncExecutor, marker, "A", begun=False, reported_length=None)
    else:
        from executors.media.executor import MediaExecutor
        process, tasks, state = await _owned(tmp_path, MediaExecutor, marker, "A", progress={}, finalizing=False)
    try:
        assert await asyncio.wait_for(process.wait(), timeout=20) == 0   # the child was never blocked
        done, _pending = await asyncio.wait(tasks, timeout=10)
        assert len(done) == len(tasks) and all(task.exception() is None for task in done)
        assert Path(marker).read_text() == "done"
    finally:
        if process.returncode is None:
            process.kill()
    if reader.startswith("aria2-"):
        assert len(state()) <= 30                                    # its diagnostic retention stays bounded
    if reader.startswith("sabnzbd-"):
        assert state()[reader.split("-")[1]] == 1025                 # the over-long line counted once, never logged


async def test_aria2_notes_an_omitted_over_long_line_in_its_diagnostics():
    from executors.aria2.runtime import Aria2Runtime
    runtime = Aria2Runtime()
    reader = asyncio.StreamReader()
    reader.feed_data(b"q" * 70000 + b"\nshort\n")
    reader.feed_eof()
    await runtime._drain_stream(reader, "stdout")
    assert list(runtime._last_output) == ["stdout: (an over-long output line was omitted)", "stdout: short"]


async def test_rsync_never_takes_an_over_long_line_for_its_begun_record(tmp_path):
    from executors.rsync.executor import RsyncExecutor
    process, tasks, state = await _owned(tmp_path, RsyncExecutor, str(tmp_path / "finished"),
                                         "dp-transfer-begun 999 ", begun=False, reported_length=None)
    await asyncio.wait_for(process.wait(), timeout=20)
    await asyncio.wait(tasks, timeout=10)
    assert state().begun is False and state().reported_length is None


async def test_a_failed_aria2_start_is_cleaned_up_within_bounds_even_if_wait_never_returns(monkeypatch):
    from executors.aria2 import runtime as module

    class Stuck:
        returncode = None
        pid = 0

        def terminate(self):
            pass

        def kill(self):
            pass

        async def wait(self):
            await asyncio.Event().wait()                               # never returns, like an undrained child

    runtime = module.Aria2Runtime()
    runtime._process = Stuck()
    real_wait_for = asyncio.wait_for
    monkeypatch.setattr(module.asyncio, "wait_for", lambda awaitable, timeout: real_wait_for(awaitable, 0.2))
    await real_wait_for(runtime._cleanup_failed_start(), timeout=5)
    assert runtime._process is None
