"""Shared pytest bootstrap for the DebridPulse backend suite.

Some inherited unittest modules conditionally install lightweight dependency
stubs during module import. Import the real SQLite driver first so those legacy
guards cannot replace the process-wide ``aiosqlite`` module and leak a fake
``connect`` implementation into later persistence tests.

It also owns the suite's two layers (docs/QUALIFICATION_DETERMINISM.md):
``real_runtime`` marks a test that drives a real external executable -- rsync
(>= 3.4.0, the product minimum), aria2c, the openssl CLI or ffmpeg/ffprobe --
and runs in the runtime layer, ``-m real_runtime``; everything
else is the deterministic contract layer, ``-m "not real_runtime"``. ``DP_TEST_SHARD=k/n`` keeps only
the tests of shard ``k`` of ``n``: a fixed partition by test node id, so the
shards of one layer together run each of its tests exactly once. (By test, not
by module: a module whose scale scenarios cost minutes no longer holds one
shard past the budget while the others idle.) The partition is weighted by the
measured durations committed in ``.github/qualification/pytest_seconds.json``:
each listed test is placed, longest first, on the least-loaded shard of its
layer, a test listed as isolated gets a shard of its own, and every other test
is spread by a hash of its node id over the shards that are not isolated. It is
a pure function of the node id, the layer, the shard count and that committed
table -- never of what else was collected -- so every shard, and any isolated
re-run of one node id, computes the same owner.

``--stall-dump=SECONDS`` writes every thread's stack, and every pending task of
the running event loop, to stderr once a test has run that long; the test goes
on and is judged as before. The snapshot is taken holding the GIL, unlike
faulthandler's ``faulthandler_timeout``, whose watchdog walks other threads'
frames without it and crashes the process (SIGSEGV) when one of them is
running Python at that moment -- an aiosqlite worker handing a result back
through ``call_soon_threadsafe``, say.
"""
import asyncio
import functools
import json
import os
import sys
import threading
import traceback
import zlib
from pathlib import Path

import aiosqlite  # noqa: F401
import pytest

RUNTIME_MARKER = "real_runtime"


def pytest_addoption(parser):
    parser.addoption("--stall-dump", type=float, default=0.0, metavar="SECONDS",
                     help="dump every thread's stack and pending task once a test runs this long (0: off)")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", f"{RUNTIME_MARKER}: drives a real external executable; runs in the runtime layer")
    if config.getoption("stall_dump") > 0:
        config.stash[_STALL_FD] = os.dup(2)        # the real stderr: capture is suspended here


_STALL_FD = pytest.StashKey[int]()


# The dump is bounded, so collecting it can neither grow without limit nor
# follow an abnormal await chain forever: innermost frames per stack, tasks
# per loop, links per await chain (a chain revisiting an awaitable stops), and
# bytes in all; whatever is cut is said so.
STALL_STACK_FRAMES = 60
STALL_TASKS = 200
STALL_AWAIT_LINKS = 64
STALL_BYTES = 512 * 1024


def _await_chain(awaitable) -> list[str]:
    """One task's await chain, outermost first, as frame lines -- bounded."""
    lines, seen, links = [], set(), 0
    while awaitable is not None:
        if id(awaitable) in seen:
            lines.append("  (await chain revisits an awaitable; stopped)\n")
            break
        if links == STALL_AWAIT_LINKS:                    # independent of the revisit check
            lines.append(f"  (await chain longer than {STALL_AWAIT_LINKS}; cut)\n")
            break
        seen.add(id(awaitable))
        links += 1
        frame = getattr(awaitable, "cr_frame", None) or getattr(awaitable, "gi_frame", None)
        if frame is not None:
            lines.extend(traceback.format_stack(frame, limit=1))
        awaitable = getattr(awaitable, "cr_await", None) or getattr(awaitable, "gi_yieldfrom", None)
    return lines


def _stall_report(nodeid: str, seconds: float) -> str:
    """Every pending task of a running loop, then every thread's stack.

    ``sys._current_frames()`` is taken in one step holding the GIL, and each
    frame is read holding it, so nothing is read mid-update (where
    faulthandler's watchdog reads without it); the threads keep running between
    reads, so this is not one frozen snapshot. Tasks come first so that the
    byte cap cuts thread stacks before await chains, and one unreadable task
    or thread costs only its own entry.
    """
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    frames = {ident: frame for ident, frame in sys._current_frames().items() if ident != threading.get_ident()}
    loops = []
    for frame in frames.values():
        while frame is not None:                          # a thread's stack is finite and acyclic
            owner = frame.f_locals.get("self") if frame.f_code.co_name == "_run_once" else None
            if isinstance(owner, asyncio.AbstractEventLoop) and owner not in loops:
                loops.append(owner)
            frame = frame.f_back
    lines = [f"\n{nodeid} still running after {seconds:g} s (stall dump; the test continues)\n"]
    for loop in loops:
        try:
            tasks = list(asyncio.all_tasks(loop))
        except RuntimeError as exc:                       # the task set changed while copied
            lines.append(f"(tasks of {loop!r} unreadable: {exc})\n")
            continue
        for task in tasks[:STALL_TASKS]:
            try:
                lines.append(f"Task {task.get_name()}, awaiting (outermost first):\n")
                lines.extend(_await_chain(task.get_coro()))
            except Exception as exc:  # noqa: BLE001 -- one task never costs the dump
                lines.append(f"  (task unreadable: {exc!r})\n")
        if len(tasks) > STALL_TASKS:
            lines.append(f"({len(tasks) - STALL_TASKS} more tasks not shown)\n")
    for ident, frame in frames.items():
        lines.append(f"Thread {ident:#x} ({names.get(ident, '?')}), most recent call last"
                     f" (innermost {STALL_STACK_FRAMES} frames at most):\n")
        try:
            lines.extend(traceback.format_stack(frame, limit=STALL_STACK_FRAMES))
        except Exception as exc:  # noqa: BLE001 -- one thread never costs the dump
            lines.append(f"  (stack unreadable: {exc!r})\n")
    return _bounded("".join(lines))


def _bounded(report: str) -> str:
    data = report.encode("utf-8", "backslashreplace")
    if len(data) <= STALL_BYTES:
        return report
    return data[:STALL_BYTES].decode("utf-8", "ignore") + f"\n(stall dump cut at {STALL_BYTES} bytes)\n"


def _stall_dump(fd: int, nodeid: str, seconds: float) -> None:
    try:
        report = _stall_report(nodeid, seconds)
    except Exception as exc:  # noqa: BLE001 -- diagnostics never fail the run
        report = f"\n{nodeid} still running after {seconds:g} s; stall dump failed: {exc!r}\n"
    data = memoryview(report.encode("utf-8", "backslashreplace"))
    while data:                                           # a pipe may take it in parts
        data = data[os.write(fd, data):]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    seconds = item.config.getoption("stall_dump")
    if seconds <= 0:
        yield
        return
    timer = threading.Timer(seconds, _stall_dump, (item.config.stash[_STALL_FD], item.nodeid, seconds))
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()


SHARD_TIMINGS = Path(__file__).resolve().parents[2] / ".github" / "qualification" / "pytest_seconds.json"


@functools.lru_cache(maxsize=None)
def _shard_plan(layer: str, shards: int) -> tuple[dict, tuple[int, ...]]:
    """(node id -> shard for the timed tests of ``layer``, the shards hashing spreads the rest over)."""
    try:
        table = json.loads(SHARD_TIMINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, tuple(range(1, shards + 1))            # no table: the plain node-id hash
    seconds = {str(node): float(value) for node, value in (table.get(layer) or {}).items()}
    isolated = sorted(node for node in table.get("isolated", ()) if node in seconds)
    if len(isolated) >= shards:
        isolated = []                                     # too few shards to give any one its own
    plan = {node: index for index, node in enumerate(isolated, start=1)}
    free = tuple(range(len(isolated) + 1, shards + 1))
    load = dict.fromkeys(free, 0.0)
    for node, cost in sorted(seconds.items(), key=lambda item: (-item[1], item[0])):
        if node in plan:
            continue
        target = min(free, key=lambda shard: (load[shard], shard))
        plan[node] = target
        load[target] += cost
    return plan, free


def shard_of(node_id: str, shards: int, layer: str = "contract") -> int:
    """The 1-based shard a test belongs to: a pure function of its node id, its
    layer, the shard count and the committed timing table."""
    node = node_id.replace(os.sep, "/")
    plan, free = _shard_plan(layer, shards)
    if node in plan:
        return plan[node]
    return free[zlib.crc32(node.encode("utf-8")) % len(free)]


def layer_of(item) -> str:
    return "runtime" if item.get_closest_marker(RUNTIME_MARKER) else "contract"


def parse_shard(value: str) -> tuple[int, int] | None:
    if not value:
        return None
    index, _, total = value.partition("/")
    shard, shards = int(index), int(total)
    if not 1 <= shard <= shards:
        raise pytest.UsageError(f"DP_TEST_SHARD must be k/n with 1 <= k <= n, not {value!r}")
    return shard, shards


def pytest_collection_modifyitems(config, items):
    selected = parse_shard(os.environ.get("DP_TEST_SHARD", ""))
    if selected is None:
        return
    shard, shards = selected
    kept, dropped = [], []
    for item in items:
        (kept if shard_of(item.nodeid, shards, layer_of(item)) == shard else dropped).append(item)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept
