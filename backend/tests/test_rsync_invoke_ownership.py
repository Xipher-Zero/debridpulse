"""1.0.13 WS5b PA-PROC-2: a bounded rsync invocation never outlives itself.

``RsyncExecutor._invoke`` (listings and evidence windows) runs one native group
under a throwaway ownership identity. It terminated that group only on its
ordinary exits: a cancelled invocation -- an input question torn down, a
resolution cycle ended, shutdown -- or one whose acceptance notice raised
cancelled the output pumps and returned, leaving the group running with
nothing draining it. Every exit now ends the group first (through the one
ownership owner, while its output is still drained), and only a group proven
gone loses its marker.

The real ``_invoke`` and the real ``ProcessOwnership`` run harmless shell
children; each case has a hard ceiling.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from executors.process_ownership import ProcessOwnership
from executors.rsync.executor import RsyncExecutor

pytestmark = pytest.mark.asyncio


def _executor(tmp_path):
    owner = ProcessOwnership(tmp_path / "runtime")
    executor = types.SimpleNamespace(
        configuration=types.SimpleNamespace(connection_timeout_seconds=600, transfer_timeout_seconds=600),
        processes=owner, egress=None, _accepted=RsyncExecutor._accepted, _status=RsyncExecutor._status)
    executor._end_route = lambda holder: RsyncExecutor._end_route(executor, holder)
    return executor, owner


def _transport(owner, script: str):
    return types.SimpleNamespace(
        env=owner.environment(), secrets={}, stdin=None, pass_fds=(), status=None,
        argv=lambda _head, _tail, _descriptors: ["sh", "-c", script],
        discard=lambda: None, close=lambda: None)


async def _started(owner) -> str:
    for _ in range(200):
        markers = list(owner.markers.glob("*.lock")) if owner.markers.exists() else []
        if markers:
            return markers[0].stem
        await asyncio.sleep(0.01)
    raise AssertionError("the invocation never started its group")


async def _group_of(owner) -> int:
    for _ in range(200):
        for marker in owner.markers.glob("*.lock"):
            text = marker.read_text()
            if '"group"' in text:
                return int(text.rsplit(":", 1)[1].strip(" }"))
        await asyncio.sleep(0.01)
    raise AssertionError("no group recorded")


def _gone(group: int) -> bool:
    import os
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return True
    return False


async def test_a_cancelled_invocation_ends_its_group_and_releases_its_marker(tmp_path):
    executor, owner = _executor(tmp_path)
    task = asyncio.ensure_future(RsyncExecutor._invoke(executor, [], [], _transport(owner, "sleep 30")))
    await _started(owner)
    group = await _group_of(owner)
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)
    assert _gone(group)
    assert list(owner.markers.glob("*.lock")) == []        # proven gone, so forgotten


async def test_a_group_that_ignores_termination_is_still_ended_on_cancellation(tmp_path):
    executor, owner = _executor(tmp_path)
    script = 'trap "" TERM; sleep 30 & wait'
    task = asyncio.ensure_future(RsyncExecutor._invoke(executor, [], [], _transport(owner, script)))
    await _started(owner)
    group = await _group_of(owner)
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=15)
    assert _gone(group)


async def test_cancelling_again_while_it_stops_does_not_end_the_invocation_before_the_group(tmp_path):
    """The termination is held at a controlled point; a second cancellation
    arrives while it is held. ``_invoke`` must still be waiting for it, and
    when it does exit the group is already gone and its marker disposed."""
    executor, owner = _executor(tmp_path)
    real_terminate = owner.terminate
    stopping, release = asyncio.Event(), asyncio.Event()

    async def held_terminate(*args, **kwargs):
        stopping.set()
        await release.wait()
        return await real_terminate(*args, **kwargs)

    owner.terminate = held_terminate
    task = asyncio.ensure_future(RsyncExecutor._invoke(executor, [], [], _transport(owner, "sleep 30")))
    await _started(owner)
    group = await _group_of(owner)
    task.cancel()
    await asyncio.wait_for(stopping.wait(), timeout=10)     # termination has begun and is held
    task.cancel()                                           # a second teardown while it is held
    for _ in range(20):
        await asyncio.sleep(0)
    assert not task.done()                                  # still waiting for the termination
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)
    assert _gone(group)                                     # at exit, not eventually
    assert list(owner.markers.glob("*.lock")) == []


async def test_a_failing_acceptance_notice_also_ends_the_group(tmp_path):
    executor, owner = _executor(tmp_path)

    def accepted():
        raise RuntimeError("the question owner refused")

    task = asyncio.ensure_future(RsyncExecutor._invoke(
        executor, [], [], _transport(owner, "echo listed; sleep 30"), on_accepted=accepted))
    await _started(owner)
    group = await _group_of(owner)
    with pytest.raises(RuntimeError, match="refused"):
        await asyncio.wait_for(task, timeout=10)
    assert _gone(group)


async def test_an_ordinary_invocation_still_returns_its_output_and_status(tmp_path):
    executor, owner = _executor(tmp_path)
    code, stdout, stderr, records, overflow = await asyncio.wait_for(
        RsyncExecutor._invoke(executor, [], [], _transport(owner, "echo listed; echo note >&2; exit 3")), timeout=10)
    assert (code, stdout, stderr, records, overflow) == (3, b"listed\n", b"note\n", [], False)
    assert list(owner.markers.glob("*.lock")) == []
