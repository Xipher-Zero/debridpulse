"""DP 1.0.14: one global download cap that is used, measured on the real path.

The convergence engine and its bandwidth owner, real aria2 and rsync executors,
the egress guard, a real rsync daemon and a paced HTTP origin. Every byte aria2
and rsync download crosses the guard's per-executor budgets, read at the
moment of sampling: they are the independent delivery oracle, never the UI
speed or the allocator's own figures. aria2's ``completedLength`` is what aria2
itself accepted (it trails receipt in steps, so it is only read over long
windows); the origin counts what it sent, which includes what sits in socket
buffers.

Each case fails on the 1.0.13 equal-share allocator or on aria2's native
limiter set equal to its budget, for the reason the investigation measured
(``investigations/dp-1.0.14-speed-cap-details-width-20261031``):

* after a mid-download cap reduction, aria2's own trailing-average limiter
  held delivery near 90% of the cap while the budget refilled at the same rate;
* a low-demand second reservation stranded its equal share.

The utilization floors come from these cases' own measurements, twice on the
candidate and once on the unchanged 1.0.14 baseline (see the Gate 9 packet):
after the reduction aria2 accepted 10.30 and 10.34 MiB/s of 10 (its budget
delivering 10.00) against 7.45 on the baseline; with the slow rsync reserved
the two delivered 3.84 and 3.86 MiB/s of 4 against 2.50.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from rsync_origins import RsyncDaemon, write_tree
from test_v113_continuation_runtime import BODY
from test_v113_rsync_runtime import MIB, _runtime
from transfers.models import TransferRequest, TransferState

pytestmark = [pytest.mark.asyncio, pytest.mark.real_runtime]

UTILIZATION = 0.97
# With a low-demand executor reserved, the design keeps it 1.25x its measured
# rate, so up to a quarter of what it uses stays lent to it: 0.125 of 4 MiB/s
# here (3%). This floor keeps 3% for measurement noise and still fails both
# the stranded equal share (~62%) and the native==egress backlog (~90%).
SHARED_UTILIZATION = 0.93
# Upper bound on any 2 s window of budget-delivered bytes: one relayed chunk
# per connection and the sampling step (the existing aggregate suite's terms).
WINDOW = 2.0
TOLERANCE = 1.15
SLACK_BYTES = 256 * 1024


async def _origin(*, per_connection: int, size: int):
    """A Range-capable origin of ``size`` bytes, each connection paced to
    ``per_connection`` bytes/s by a token bucket with a 250 ms burst (a held
    back connection banks no credit). ``sent`` counts payload bytes written."""
    state = {"sent": 0}
    block = bytes(64 * 1024)

    async def handle(reader, writer):
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            start, end = 0, size
            for line in raw.decode("iso-8859-1").split("\r\n"):
                if line.casefold().startswith("range:"):
                    first, _, last = line.split("=", 1)[1].strip().partition("-")
                    start, end = int(first), (int(last) + 1 if last else size)
            partial = (start, end) != (0, size)
            head = b"HTTP/1.1 206 Partial Content\r\n" if partial else b"HTTP/1.1 200 OK\r\n"
            head += f"Content-Length: {end - start}\r\nAccept-Ranges: bytes\r\n".encode()
            if partial:
                head += f"Content-Range: bytes {start}-{end - 1}/{size}\r\n".encode()
            writer.write(head + b"Connection: close\r\n\r\n")
            began, done = time.monotonic(), 0
            for offset in range(start, end, len(block)):
                chunk = block[:min(len(block), end - offset)]
                writer.write(chunk)
                await writer.drain()
                done += len(chunk)
                state["sent"] += len(chunk)
                ahead = done / per_connection - (time.monotonic() - began)
                if ahead > 0:
                    await asyncio.sleep(ahead)
                elif ahead < -0.25:
                    began, done = time.monotonic() - 0.25, 0
        except (ConnectionError, asyncio.IncompleteReadError, TimeoutError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, int(server.sockets[0].getsockname()[1]), state


class Samples:
    """(time, aria2 budget delivered, rsync budget delivered, aria2 completedLength, origin sent)."""

    def __init__(self, runtime, origin):
        self.runtime, self.origin, self.rows = runtime, origin, []

    async def take(self):
        guard = self.runtime.guard
        active = await self.runtime.aria2_service._call("aria2.tellActive", [["completedLength"]])
        self.rows.append((time.monotonic(), guard.budget("aria2").delivered, guard.budget("rsync").delivered,
                          sum(int(item["completedLength"]) for item in active), self.origin["sent"]))

    def rate(self, column: int, since: float, until: float) -> float:
        rows = [row for row in self.rows if since <= row[0] <= until]
        return (rows[-1][column] - rows[0][column]) / (rows[-1][0] - rows[0][0])

    def peak(self, columns, since: float, until: float = float("inf")) -> float:
        rows = [(row[0], sum(row[column] for column in columns)) for row in self.rows if since <= row[0] <= until]
        best = 0.0
        for index, (start, first) in enumerate(rows):
            later = next(((at, value) for at, value in rows[index + 1:] if at - start >= WINDOW), None)
            if later is not None:
                best = max(best, (later[1] - first) / (later[0] - start))
        return best


async def _run(runtime, samples, seconds: float, until=None) -> float:
    """Tick the engine and sample for ``seconds`` (or until ``until()``); returns when it began."""
    began = time.monotonic()
    while time.monotonic() - began < seconds:
        await runtime.engine.tick()
        await samples.take()
        if until is not None and await until():
            return began
        await asyncio.sleep(0.1)
    if until is not None:
        raise AssertionError("bandwidth scenario did not settle")
    return began


async def _ceiling(runtime, value: int):
    runtime.engine.configure_runtime_limits(value)
    assert (await runtime.engine.converge_runtime_limits()).configured == value


async def _retire(runtime, *transfer_ids):
    for transfer_id in transfer_ids:
        if (await runtime.repository.get(transfer_id)).state not in {TransferState.COMPLETED, TransferState.CANCELLED}:
            await runtime.engine.cancel(transfer_id)


def _within(rate: float, ceiling: int) -> bool:
    return rate <= ceiling * TOLERANCE + SLACK_BYTES / WINDOW


async def test_aria2_recovers_the_whole_cap_after_a_mid_download_reduction(tmp_path, monkeypatch):
    # The investigation's conditions: 16 connections, each paced at 1.5 MiB/s
    # (a 24 MiB/s source), Unlimited long enough to fill the socket buffers
    # between the guard and aria2, then 10 MiB/s.
    cap = 10 * MIB
    server, port, sent = await _origin(per_connection=3 * MIB // 2, size=900 * MIB)
    runtime = await _runtime(tmp_path, monkeypatch, aria2=True, aria2_split=16)
    try:
        await _ceiling(runtime, 0)
        http = await runtime.engine.submit(
            (TransferRequest("http", f"http://http-origin.test:{port}/pub/large.bin"),), deduplicate=False)
        samples = Samples(runtime, sent)

        async def flowing():
            return samples.rows[-1][3] > 32 * MIB

        await _run(runtime, samples, 30, until=flowing)
        unlimited_from = await _run(runtime, samples, 12)
        unlimited = samples.rate(1, unlimited_from, time.monotonic())
        assert unlimited > cap * 1.5, unlimited               # the source can far exceed the cap
        await _ceiling(runtime, cap)
        changed = time.monotonic()
        await _run(runtime, samples, 30)
        steady_from, steady_to = changed + 10, time.monotonic()
        accepted = samples.rate(3, steady_from, steady_to)    # what aria2 itself took in
        delivered = samples.rate(1, steady_from, steady_to)   # what the budget let through
        print(f"aria2 after reduction: accepted {accepted / MIB:.2f} delivered {delivered / MIB:.2f} MiB/s "
              f"(cap {cap / MIB:.0f}, unlimited {unlimited / MIB:.1f})")
        assert accepted >= cap * UTILIZATION, accepted
        assert delivered <= cap * 1.02 and _within(samples.peak([1], changed + 1), cap)
        await _retire(runtime, http.id)
    finally:
        await runtime.close()
        server.close()


async def test_a_low_demand_reservation_no_longer_strands_its_share(tmp_path, monkeypatch):
    cap = 4 * MIB
    write_tree(tmp_path / "srv", {"slow.bin": BODY})
    daemon = RsyncDaemon(tmp_path / "daemon", {"pub": {"path": tmp_path / "srv"}}, bwlimit=512).start()
    server, port, sent = await _origin(per_connection=4 * MIB, size=256 * MIB)
    runtime = await _runtime(tmp_path, monkeypatch, aria2=True, aria2_split=4)
    try:
        await _ceiling(runtime, cap)
        http = await runtime.engine.submit(
            (TransferRequest("http", f"http://http-origin.test:{port}/pub/large.bin"),), deduplicate=False)
        samples = Samples(runtime, sent)

        async def aria2_flowing():
            return samples.rows[-1][1] > 2 * MIB

        await _run(runtime, samples, 30, until=aria2_flowing)
        rsync = await runtime.engine.submit((TransferRequest("rsync", daemon.url("/pub/slow.bin")),),
                                            deduplicate=False)

        async def both():
            return samples.rows[-1][2] > 128 * 1024 and runtime.engine.runtime.reserved == frozenset({"aria2", "rsync"})

        await _run(runtime, samples, 30, until=both)
        joined = time.monotonic()
        await _run(runtime, samples, 22)
        steady_from, steady_to = joined + 10, time.monotonic()
        aria2 = samples.rate(1, steady_from, steady_to)
        slow = samples.rate(2, steady_from, steady_to)
        print(f"with a 0.5 MiB/s rsync reserved: aria2 {aria2 / MIB:.2f} + rsync {slow / MIB:.2f} = "
              f"{(aria2 + slow) / MIB:.2f} MiB/s of {cap / MIB:.0f}")
        assert aria2 + slow >= cap * SHARED_UTILIZATION, (aria2, slow)
        assert slow >= 0.4 * MIB                               # the slow consumer is not starved
        assert aria2 + slow <= cap * 1.02 and _within(samples.peak([1, 2], joined), cap)
        # Its demand leaves: aria2 is given the whole cap again.
        await _retire(runtime, rsync.id)
        left = time.monotonic()
        await _run(runtime, samples, 12)
        alone = samples.rate(1, left + 6, time.monotonic())
        assert cap * UTILIZATION <= alone <= cap * 1.02, alone
        await _retire(runtime, http.id)
    finally:
        await runtime.close()
        server.close()
        daemon.stop()


async def test_two_saturated_executors_both_progress_within_the_cap(tmp_path, monkeypatch):
    cap = 2 * MIB
    write_tree(tmp_path / "srv", {"fast.bin": BODY})
    daemon = RsyncDaemon(tmp_path / "daemon", {"pub": {"path": tmp_path / "srv"}}).start()
    server, port, sent = await _origin(per_connection=4 * MIB, size=256 * MIB)
    runtime = await _runtime(tmp_path, monkeypatch, aria2=True, aria2_split=4)
    try:
        await _ceiling(runtime, cap)
        http = await runtime.engine.submit(
            (TransferRequest("http", f"http://http-origin.test:{port}/pub/large.bin"),), deduplicate=False)
        rsync = await runtime.engine.submit((TransferRequest("rsync", daemon.url("/pub/fast.bin")),),
                                            deduplicate=False)
        samples = Samples(runtime, sent)

        async def both():
            row = samples.rows[-1]
            return row[1] > 256 * 1024 and row[2] > 256 * 1024

        await _run(runtime, samples, 30, until=both)
        joined = time.monotonic()
        await _run(runtime, samples, 14)
        steady_from, steady_to = joined + 6, time.monotonic()
        aria2, fast = samples.rate(1, steady_from, steady_to), samples.rate(2, steady_from, steady_to)
        print(f"two saturated: aria2 {aria2 / MIB:.2f} rsync {fast / MIB:.2f} MiB/s of {cap / MIB:.0f}")
        assert aria2 >= 0.35 * cap and fast >= 0.35 * cap, (aria2, fast)
        assert cap * UTILIZATION <= aria2 + fast <= cap * 1.02
        assert _within(samples.peak([1, 2], joined), cap)
        await _retire(runtime, http.id, rsync.id)
    finally:
        await runtime.close()
        server.close()
        daemon.stop()
