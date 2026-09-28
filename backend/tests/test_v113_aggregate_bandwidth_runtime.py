"""DP 1.0.13: the one global download ceiling governs every executor, measured.

``execution_runtime_limits.max_download_bytes_per_second`` has one owner --
``transfers.runtime_coordination`` -- which splits it across the executors
holding a bandwidth reservation and tells each only its own share. aria2
enforces its share natively (its daemon-wide limit); rsync enforces its share
on the one DebridPulse download budget every rsync connection draws on in the
egress guard, so its concurrent transfers together -- never each -- stay
within it, whatever the server does.

Everything is real: the convergence engine and coordinator, the rsync and
aria2 executors, the egress guard, a real rsync daemon and a Range-capable
HTTP origin. Delivery is measured from each executor's own byte counter at the moment
of sampling, over sliding windows, and must never exceed the configured
ceiling:

    sum(active DP download bandwidth) <= configured global DP ceiling
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest

from rsync_origins import RsyncDaemon, write_tree
from test_v113_continuation_runtime import BODY, start_origin
from test_v113_rsync_runtime import MIB, _runtime
from transfers.models import TransferRequest, TransferState

pytestmark = pytest.mark.asyncio
WINDOW = 2.0
# One relayed chunk per budget, sampling granularity and the executors' own
# write buffering: a measured window may exceed the ceiling by at most this.
TOLERANCE = 1.15
SLACK_BYTES = 256 * 1024


class Meter:
    """What DebridPulse has downloaded, over time.

    The aggregate is every byte the executors' download budgets delivered --
    all aria2 and rsync traffic crosses them, and they are read at the moment
    of sampling. Per-transfer rsync delivery is also read natively (its target
    grows in place) as an independent check. aria2's own completedLength is
    not used for windows: it trails receipt (piece and disk-cache accounting)
    and then catches up in steps."""

    BUDGETS = ("aria2", "rsync")

    def __init__(self, runtime, transfer_ids):
        self.runtime = runtime
        self.transfer_ids = set(transfer_ids)
        self.samples: list[tuple[float, int, dict[int, int]]] = []
        self._targets: dict[int, str] = {}

    async def _rsync_target(self, transfer_id: int) -> int:
        if transfer_id not in self._targets:
            artifacts = await self.runtime.repository.artifacts(transfer_id)
            if not artifacts:
                return 0
            self._targets[transfer_id] = artifacts[0].target
        try:
            return os.stat(self._targets[transfer_id]).st_size
        except FileNotFoundError:
            return 0

    async def sample(self) -> dict[int, int]:
        per = {}
        for transfer_id in self.transfer_ids:
            executions = [item for item in await self.runtime.repository.executions()
                          if item.transfer_id == transfer_id]
            rsync = bool(executions) and executions[-1].handle.executor_id == "rsync"
            per[transfer_id] = await self._rsync_target(transfer_id) if rsync else int(sum(
                item.progress.completed_bytes or 0 for item in executions))
        delivered = sum(self.runtime.guard.budget(name).delivered for name in self.BUDGETS)
        self.samples.append((time.monotonic(), delivered, per))
        return per

    def peak(self, transfer_id: int | None = None, *, since: float = 0.0, until: float = float("inf")) -> float:
        """The highest delivery rate over any WINDOW-long span in [since, until]
        (the aggregate, or one transfer's)."""
        points = [(at, total if transfer_id is None else per.get(transfer_id, 0))
                  for at, total, per in self.samples if since <= at <= until]
        best = 0.0
        for index, (start, first) in enumerate(points):
            later = next(((at, value) for at, value in points[index + 1:] if at - start >= WINDOW), None)
            if later is not None:
                best = max(best, (later[1] - first) / (later[0] - start))
        return best

    def peak_of_rsync_targets(self, ids, **bounds) -> float:
        """Native cross-check: the rsync targets' own combined growth."""
        points = [(at, sum(per.get(transfer_id, 0) for transfer_id in ids))
                  for at, _total, per in self.samples if bounds.get("since", 0.0) <= at <= bounds.get("until", float("inf"))]
        best = 0.0
        for index, (start, first) in enumerate(points):
            later = next(((at, value) for at, value in points[index + 1:] if at - start >= WINDOW), None)
            if later is not None:
                best = max(best, (later[1] - first) / (later[0] - start))
        return best


def _within(rate: float, ceiling: int) -> bool:
    return rate <= ceiling * TOLERANCE + SLACK_BYTES / WINDOW


async def _drive(runtime, meter, *, until, seconds=120.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        await runtime.engine.tick()
        await meter.sample()
        if await until():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("bandwidth scenario did not settle")


async def _hold(runtime, meter, seconds: float) -> float:
    """Keep the engine running and sampled for ``seconds``; returns when it began."""
    began = time.monotonic()
    while time.monotonic() - began < seconds:
        await runtime.engine.tick()
        await meter.sample()
        await asyncio.sleep(0.1)
    return began


async def _ceiling(runtime, bytes_per_second: int):
    runtime.engine.configure_runtime_limits(bytes_per_second)
    status = await runtime.engine.converge_runtime_limits()
    assert status.configured == bytes_per_second
    return status


def _daemon(tmp_path, files: dict[str, bytes]) -> RsyncDaemon:
    write_tree(tmp_path / "srv", files)
    return RsyncDaemon(tmp_path / "daemon", {"pub": {"path": tmp_path / "srv"}}).start()


async def _retire(runtime, *transfer_ids) -> None:
    """Cancel whatever of these transfers is still live (a finished one is left as it is)."""
    for transfer_id in transfer_ids:
        if (await runtime.repository.get(transfer_id)).state not in {TransferState.COMPLETED, TransferState.CANCELLED}:
            await runtime.engine.cancel(transfer_id)


async def _all_completed(runtime, transfer_ids) -> bool:
    for transfer_id in transfer_ids:
        if (await runtime.repository.get(transfer_id)).state != TransferState.COMPLETED:
            return False
    return True


async def test_one_rsync_transfer_is_held_to_the_global_ceiling(tmp_path, monkeypatch):
    ceiling = 1 * MIB
    daemon = _daemon(tmp_path, {"movie.bin": BODY[:5 * MIB]})
    runtime = await _runtime(tmp_path, monkeypatch)
    try:
        await _ceiling(runtime, ceiling)
        started = time.monotonic()
        transfer = await runtime.engine.submit((TransferRequest("rsync", daemon.url("/pub/movie.bin")),),
                                               deduplicate=False)
        meter = Meter(runtime, [transfer.id])
        await _drive(runtime, meter, until=lambda: _all_completed(runtime, [transfer.id]))
        # DP assigned rsync the whole ceiling (it is the only reserved executor)...
        assert runtime.guard.budget("rsync").rate == ceiling
        # ...and rsync held to it: never faster over any window, so 5 MiB
        # took at least about five seconds.
        assert _within(meter.peak(), ceiling), meter.peak()
        assert _within(meter.peak_of_rsync_targets([transfer.id]), ceiling)
        assert time.monotonic() - started >= 4.0
        (artifact,) = await runtime.repository.artifacts(transfer.id)
        assert open(artifact.target, "rb").read() == BODY[:5 * MIB]
    finally:
        await runtime.close()
        daemon.stop()


async def test_concurrent_rsync_transfers_share_one_ceiling_rather_than_each_taking_it(tmp_path, monkeypatch):
    ceiling = 1536 * 1024
    files = {f"part{index}.bin": BODY[index * 3 * MIB:(index + 1) * 3 * MIB] for index in range(3)}
    daemon = _daemon(tmp_path, files)
    runtime = await _runtime(tmp_path, monkeypatch)
    try:
        await _ceiling(runtime, ceiling)
        started = time.monotonic()
        transfers = [await runtime.engine.submit((TransferRequest("rsync", daemon.url(f"/pub/{name}")),),
                                                 deduplicate=False) for name in files]
        ids = [transfer.id for transfer in transfers]
        meter = Meter(runtime, ids)
        await _drive(runtime, meter, until=lambda: _all_completed(runtime, ids))
        # Three writers together, not three full ceilings: 9 MiB at 1.5 MiB/s.
        assert _within(meter.peak(), ceiling), meter.peak()
        assert _within(meter.peak_of_rsync_targets(ids), ceiling), meter.peak_of_rsync_targets(ids)
        assert time.monotonic() - started >= 5.0
        assert runtime.guard.budget("rsync").rate == ceiling
    finally:
        await runtime.close()
        daemon.stop()


async def test_mixed_aria2_and_rsync_work_stays_under_the_one_ceiling_and_a_finished_consumer_releases_its_share(
        tmp_path, monkeypatch):
    ceiling = 2 * MIB
    daemon = _daemon(tmp_path, {"movie.bin": BODY[:4 * MIB]})
    server, port, _served = await start_origin(rate=64 * MIB)
    runtime = await _runtime(tmp_path, monkeypatch, aria2=True)
    try:
        await _ceiling(runtime, ceiling)
        http = await runtime.engine.submit((TransferRequest("http", f"http://http-origin.test:{port}/pub/movie.bin"),),
                                           deduplicate=False)
        rsync = await runtime.engine.submit((TransferRequest("rsync", daemon.url("/pub/movie.bin")),),
                                            deduplicate=False)
        meter = Meter(runtime, [http.id, rsync.id])

        async def both_running():
            per = await meter.sample()
            return all(per.values()) and runtime.engine.runtime.reserved == frozenset({"aria2", "rsync"})

        await _drive(runtime, meter, until=both_running)
        overlap_started = time.monotonic()
        # DP split the one ceiling: each executor enforces only its own half.
        assert runtime.guard.budget("rsync").rate == ceiling // 2
        aria2_limit = int((await runtime.aria2_service.get_global_options())["max-overall-download-limit"])
        assert aria2_limit == ceiling // 2 and runtime.guard.budget("aria2").rate == ceiling // 2
        await _drive(runtime, meter, until=lambda: _all_completed(runtime, [rsync.id]))
        overlap_ended = time.monotonic()
        assert _within(meter.peak(since=overlap_started, until=overlap_ended), ceiling), meter.peak()

        # rsync finished: its reservation is released and aria2 receives the
        # whole ceiling -- never more.
        async def aria2_alone():
            limit = int((await runtime.aria2_service.get_global_options())["max-overall-download-limit"])
            return runtime.engine.runtime.reserved == frozenset({"aria2"}) and limit == ceiling

        await _drive(runtime, meter, until=aria2_alone, seconds=30)
        assert runtime.guard.budget("aria2").rate == ceiling
        # aria2's own limiter runs ahead of a raised limit until its trailing
        # average catches up; its egress budget holds the share at every moment.
        alone = await _hold(runtime, meter, 3.0)
        assert _within(meter.peak(since=alone), ceiling), meter.peak(since=alone)
        await _retire(runtime, http.id)
    finally:
        await runtime.close()
        server.close()
        daemon.stop()


async def test_pausing_one_rsync_transfer_releases_its_part_to_the_other(tmp_path, monkeypatch):
    ceiling = 1 * MIB
    daemon = _daemon(tmp_path, {"a.bin": BODY[:8 * MIB], "b.bin": BODY[8 * MIB:16 * MIB]})
    runtime = await _runtime(tmp_path, monkeypatch)
    try:
        await _ceiling(runtime, ceiling)
        first = await runtime.engine.submit((TransferRequest("rsync", daemon.url("/pub/a.bin")),), deduplicate=False)
        second = await runtime.engine.submit((TransferRequest("rsync", daemon.url("/pub/b.bin")),), deduplicate=False)
        meter = Meter(runtime, [first.id, second.id])

        async def both_flowing():
            per = await meter.sample()
            return all(value > 256 * 1024 for value in per.values())

        await _drive(runtime, meter, until=both_flowing)
        shared_from = await _hold(runtime, meter, 3.0)
        shared = meter.peak(second.id, since=shared_from)
        await runtime.engine.pause(first.id)

        async def paused():
            return (await runtime.repository.get(first.id)).state == TransferState.PAUSED

        await _drive(runtime, meter, until=paused)
        alone_from = await _hold(runtime, meter, 3.5)
        alone = meter.peak(second.id, since=alone_from)
        # Sharing, the second got about half; alone, about all -- never more.
        assert shared < ceiling * 0.8 and alone > shared * 1.4, (shared, alone)
        assert _within(meter.peak(since=shared_from), ceiling)
        await _retire(runtime, first.id, second.id)
    finally:
        await runtime.close()
        daemon.stop()


async def test_changing_or_lifting_the_ceiling_applies_to_running_rsync_transfers(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path, {"movie.bin": BODY[:16 * MIB]})
    runtime = await _runtime(tmp_path, monkeypatch)
    try:
        await _ceiling(runtime, 512 * 1024)
        transfer = await runtime.engine.submit((TransferRequest("rsync", daemon.url("/pub/movie.bin")),),
                                               deduplicate=False)
        meter = Meter(runtime, [transfer.id])

        async def flowing():
            return (await meter.sample())[transfer.id] > 256 * 1024

        await _drive(runtime, meter, until=flowing)
        slow_from = await _hold(runtime, meter, 3.0)
        slow = meter.peak(since=slow_from)
        await _ceiling(runtime, 2 * MIB)
        assert runtime.guard.budget("rsync").rate == 2 * MIB
        fast_from = await _hold(runtime, meter, 3.0)
        fast = meter.peak(since=fast_from)
        assert _within(slow, 512 * 1024) and _within(fast, 2 * MIB) and fast > slow * 2, (slow, fast)
        # Lifted (0 = unlimited): the running transfer is no longer held back.
        await _ceiling(runtime, 0)
        assert runtime.guard.budget("rsync").rate == 0
        lifted = time.monotonic()
        await _drive(runtime, meter, until=lambda: _all_completed(runtime, [transfer.id]), seconds=30)
        assert time.monotonic() - lifted < 4.0
    finally:
        await runtime.close()
        daemon.stop()
