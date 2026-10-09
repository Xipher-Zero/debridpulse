"""The one download budget (``EgressBudget``) follows a live ceiling change.

The guard's relays pace every delivered chunk through the executor's one
named budget; the runtime coordinator changes its rate while connections are
open. A change applies to connections already waiting -- Unlimited releases
them at once and forgives obsolete debt, a different finite rate re-times
their wait -- without bursts, double accounting or reordering, and a
cancelled waiter never strands the others.

Low rates make every wait long (seconds) against generous bounds, so the
assertions separate "re-timed" from "slept out the old wait" decisively.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from services.downloader_egress_guard import EgressBudget

pytestmark = pytest.mark.asyncio

KIB = 1024
CHUNK = 64 * KIB                                   # the relay's read size


async def _timed(coroutine):
    began = time.monotonic()
    await coroutine
    return time.monotonic() - began


async def test_unlimited_releases_a_pending_wait_at_once():
    budget = EgressBudget()
    budget.set_rate(16 * KIB)                       # one chunk = 4 s
    await budget.consume(CHUNK)                     # delivered; its 4 s are owed
    waiter = asyncio.ensure_future(_timed(budget.consume(CHUNK)))
    await asyncio.sleep(0.2)
    budget.set_rate(0)
    assert await asyncio.wait_for(waiter, 2) < 0.6  # not the 4 s it was owed
    assert await _timed(budget.consume(CHUNK)) < 0.1   # and no debt survives
    assert budget.delivered == 3 * CHUNK


async def test_a_different_finite_ceiling_re_times_a_pending_wait():
    budget = EgressBudget()
    budget.set_rate(16 * KIB)                       # 4 s owed for the first chunk
    await budget.consume(CHUNK)
    waiter = asyncio.ensure_future(_timed(budget.consume(CHUNK)))
    await asyncio.sleep(0.5)                        # 8 KiB of it paid at the old rate
    budget.set_rate(256 * KIB)                      # the remaining 56 KiB: ~0.22 s
    assert 0.5 < await asyncio.wait_for(waiter, 5) < 1.2


async def test_a_lower_ceiling_lengthens_a_pending_wait_without_a_burst():
    budget = EgressBudget()
    budget.set_rate(128 * KIB)                      # 0.5 s owed
    await budget.consume(CHUNK)
    waiter = asyncio.ensure_future(_timed(budget.consume(CHUNK)))
    await asyncio.sleep(0.1)                        # 12.8 KiB paid
    budget.set_rate(32 * KIB)                       # remaining 51.2 KiB: 1.6 s
    assert 1.4 < await asyncio.wait_for(waiter, 5) < 2.4


async def test_limiting_a_running_unlimited_connection_paces_from_its_next_chunk():
    budget = EgressBudget()
    for _ in range(4):
        await budget.consume(CHUNK)                 # unlimited: no pacing, no debt
    budget.set_rate(128 * KIB)
    elapsed = await _timed(asyncio.gather(*(budget.consume(CHUNK) for _ in range(3))))
    assert 0.9 < elapsed < 1.5                      # the first is free, two owe 0.5 s each


async def test_idle_time_is_never_saved_up_into_a_burst():
    budget = EgressBudget()
    budget.set_rate(128 * KIB)
    await budget.consume(CHUNK)
    await asyncio.sleep(1.0)                        # idle: the debt is paid, nothing more
    assert await _timed(budget.consume(CHUNK)) < 0.1
    assert 0.4 < await _timed(budget.consume(CHUNK)) < 0.8


async def test_connections_share_one_budget_in_order_and_all_follow_a_change():
    budget = EgressBudget()
    budget.set_rate(128 * KIB)
    order = []

    async def connection(name, chunks):
        for index in range(chunks):
            await budget.consume(CHUNK)
            order.append((name, index))

    began = time.monotonic()
    both = asyncio.gather(connection("a", 3), connection("b", 3))
    await asyncio.sleep(1.1)                        # one aggregate rate: ~3 chunks so far
    assert 2 <= len(order) <= 4
    assert {name for name, _index in order} == {"a", "b"}   # neither starves
    budget.set_rate(0)
    await asyncio.wait_for(both, 2)
    assert time.monotonic() - began < 1.6           # the rest were released at once
    assert budget.delivered == 6 * CHUNK


async def test_a_cancelled_waiter_never_strands_the_connections_behind_it():
    budget = EgressBudget()
    budget.set_rate(64 * KIB)                       # 1 s per chunk
    await budget.consume(CHUNK)
    first = asyncio.ensure_future(budget.consume(CHUNK))
    second = asyncio.ensure_future(_timed(budget.consume(CHUNK)))
    await asyncio.sleep(0.1)
    first.cancel()                                  # a disconnect while paced
    with pytest.raises(asyncio.CancelledError):
        await first
    budget.set_rate(0)
    assert await asyncio.wait_for(second, 2) < 0.4
    assert budget.delivered == 2 * CHUNK            # the cancelled chunk was never delivered


async def test_an_unlimited_interval_leaves_no_obsolete_debt_for_a_later_ceiling():
    budget = EgressBudget()
    budget.set_rate(16 * KIB)
    await budget.consume(CHUNK)                     # 4 s owed...
    budget.set_rate(0)                              # ...forgiven
    budget.set_rate(64 * KIB)
    assert await _timed(budget.consume(CHUNK)) < 0.1


async def _arrival(budget, began):
    await budget.consume(CHUNK)
    return time.monotonic() - began


async def test_a_cancelled_middle_reservation_is_refunded_to_the_connections_behind_it():
    budget = EgressBudget()
    budget.set_rate(64 * KIB)                       # 1 s per chunk, the ceiling stays finite
    began = time.monotonic()
    await budget.consume(CHUNK)                     # delivered; 1 s owed
    first, middle, last = (asyncio.ensure_future(_arrival(budget, began)) for _ in range(3))
    await asyncio.sleep(0.1)                        # queued in order: 1 s, 2 s, 3 s
    middle.cancel()
    with pytest.raises(asyncio.CancelledError):
        await middle
    assert 0.9 < await asyncio.wait_for(first, 5) < 1.3
    assert 1.9 < await asyncio.wait_for(last, 5) < 2.4   # not 3 s: the cancelled chunk owes nothing
    assert budget.delivered == 3 * CHUNK
    # The rate still holds afterwards: no credit was handed out by the refund.
    assert 0.9 < await _timed(budget.consume(CHUNK)) < 1.3


async def test_repeated_cancellations_leave_no_phantom_debt_and_no_burst():
    budget = EgressBudget()
    budget.set_rate(64 * KIB)
    began = time.monotonic()
    await budget.consume(CHUNK)
    queued = [asyncio.ensure_future(_arrival(budget, began)) for _ in range(6)]
    await asyncio.sleep(0.1)
    for index in (1, 2, 4):                         # three cancelled, two of them adjacent
        queued[index].cancel()
    survivors = [queued[index] for index in (0, 3, 5)]
    arrivals = [await asyncio.wait_for(task, 6) for task in survivors]
    assert [round(value) for value in arrivals] == [1, 2, 3]   # in order, one chunk per second
    assert all(later > earlier + 0.8 for earlier, later in zip(arrivals, arrivals[1:]))   # no burst
    assert budget.delivered == 4 * CHUNK



@pytest.mark.parametrize("elapsed", [0.4, 0.9])
async def test_cancelling_the_first_waiter_moves_the_queue_up_without_excess_credit(elapsed):
    """The first pending reservation is cancelled partway through its wait
    (``elapsed`` of the 1 s the delivered chunk owes). The rest move up in
    order: the next is delivered when the delivered chunk's second is paid --
    never earlier -- and every delivery stays within the rate plus the one
    chunk the contract permits."""
    rate = 64 * KIB
    budget = EgressBudget()
    budget.set_rate(rate)
    began = time.monotonic()
    await budget.consume(CHUNK)                     # pacing history: 1 s owed
    first, second, third = (asyncio.ensure_future(_arrival(budget, began)) for _ in range(3))
    await asyncio.sleep(elapsed)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    arrivals = [await asyncio.wait_for(task, 5) for task in (second, third)]
    arrivals.append(await _arrival(budget, began))  # a later connection: no credit was left over
    assert [round(value, 1) for value in arrivals] == [1.0, 2.0, 3.0]
    for delivered, at in enumerate([0.0, *arrivals], start=1):
        assert delivered * CHUNK <= rate * at + CHUNK + rate * 0.05   # rate + one chunk (+ timer slack)
    assert budget.delivered == 4 * CHUNK
