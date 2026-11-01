"""DP 1.0.14: the global download cap is a usable maximum, not a quota.

``transfers.runtime_coordination`` lends a reserved executor's unused part of
the cap to the executors that can use it and takes it back when demand
returns -- decreasing and confirming before it raises anything, so the
ceilings that may be in effect never add up to more than the cap, through lost
acknowledgements, timeouts, native restarts and executors that wake on their
own. aria2's own limiter keeps 1.5x headroom over the egress budget, which
alone binds.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from transfers import runtime_coordination
from transfers.models import (ExecutorCapabilities, ExecutorHealth, ExecutorRuntimeCapability,
                              ExecutorRuntimeControlResult, IntegrationDescriptor)
from transfers.runtime_coordination import ExecutionRuntimeCoordinator, ExecutorDemand

from sab_fakes import FakeSab  # noqa: F401
from test_v113_sabnzbd_executor import build, lab  # noqa: F401

MIB = 1 << 20
CAP = 10 * MIB
FLOOR = CAP // 32


class _Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


class _Native:
    """An executor whose native ceiling is what it last applied. ``lose_ack``
    applies the value and then loses the answer; ``hang`` applies it and never
    answers."""

    def __init__(self, identity, history, *, minimum=1):
        self.descriptor = IntegrationDescriptor(identity, identity, frozenset())
        self.capabilities = ExecutorCapabilities(aggregate_bandwidth_ceiling=True, minimum_bandwidth_ceiling=minimum)
        self.history = history
        self.applied = None
        self.lose_ack = False
        self.hang = False

    async def health(self):
        return ExecutorHealth(True, True, frozenset({ExecutorRuntimeCapability.AGGREGATE_BANDWIDTH_CEILING}))

    async def set_bandwidth_ceiling(self, value):
        self.applied = value
        self.history.append((self.descriptor.id, value))
        if self.hang:
            await asyncio.sleep(3600)
        if self.lose_ack:
            raise ConnectionResetError("answer lost")
        return ExecutorRuntimeControlResult(value, value)


class _Repo:
    async def executors_with_live_work(self):
        return frozenset()

    async def globally_paused(self):
        return False


def _owner(executors, cap, clock):
    registry = SimpleNamespace(executors={executor.descriptor.id: executor for executor in executors})
    owner = ExecutionRuntimeCoordinator(lambda: registry, _Repo(), clock=clock)
    owner.configure(cap)
    return owner


def _never_over(history, cap):
    """Replaying every native apply in order, the ceilings in effect never add
    up to more than ``cap`` (0 = unlimited counts as unbounded)."""
    current = {}
    for identity, value in history:
        current[identity] = value or float("inf")
        assert sum(current.values()) <= cap, (identity, value, dict(current))


def _demand(rate=None):
    """A certain reservation, acquiring at ``rate`` (None = dormant)."""
    return ExecutorDemand(True, True, rate is not None, float(rate or 0))


async def _pass(owner, clock, facts, seconds=6.0):
    clock.now += seconds
    await owner.observe(facts)


async def _borrowing_pair(cap=CAP):
    """``a`` dormant at its floor and ``b`` saturated, holding the rest."""
    history, clock = [], _Clock()
    a, b = _Native("a", history), _Native("b", history)
    owner = _owner([a, b], cap, clock)
    assert await owner.admit(a) and await owner.admit(b)
    await _pass(owner, clock, {"a": _demand(), "b": _demand(b.applied)})
    assert (a.applied, b.applied) == (cap // 32, cap - cap // 32)
    return owner, clock, a, b, history


# --- demand: lend, reclaim, and never strand --------------------------------

@pytest.mark.asyncio
async def test_a_dormant_reservation_lends_all_but_its_floor_and_takes_back_a_fair_share():
    owner, clock, a, b, history = await _borrowing_pair()
    # a wakes on its own: bounded by the floor it already enforces.
    await _pass(owner, clock, {"a": _demand(a.applied), "b": _demand(b.applied)})
    assert a.applied == b.applied == CAP // 2
    assert history[-2:] == [("b", CAP // 2), ("a", CAP // 2)]  # reclaimed first, then raised
    _never_over(history, CAP)
    assert owner.status().ok


@pytest.mark.asyncio
async def test_a_low_demand_executor_keeps_its_need_and_a_noisy_sample_does_not_thrash():
    owner, clock, a, b, history = await _borrowing_pair()
    # a wakes and is given a fair share; its source then delivers only 1 MiB/s.
    await _pass(owner, clock, {"a": _demand(a.applied), "b": _demand(b.applied)})
    await _pass(owner, clock, {"a": _demand(MIB), "b": _demand(b.applied)})
    assert a.applied == int(1.25 * MIB) and b.applied == CAP - a.applied
    calls = len(history)
    # Small variations inside the hysteresis band change nothing.
    for rate in (0.9 * MIB, 1.05 * MIB, 0.95 * MIB):
        await _pass(owner, clock, {"a": _demand(rate), "b": _demand(b.applied)})
    assert len(history) == calls
    _never_over(history, CAP)


@pytest.mark.asyncio
async def test_a_changed_ceiling_is_not_judged_before_the_rate_window_has_seen_it():
    owner, clock, a, b, history = await _borrowing_pair()
    calls = len(history)
    # Just after the change, a rate read over a window still dominated by the
    # old ceiling is not judged: both executors are held.
    for _ in range(2):
        await _pass(owner, clock, {"a": _demand(a.applied), "b": _demand(b.applied)}, seconds=1.0)
    assert len(history) == calls
    # Once the window has seen the new ceilings, the same facts are acted on.
    await _pass(owner, clock, {"a": _demand(a.applied), "b": _demand(b.applied)},
                seconds=runtime_coordination.SETTLE_SECONDS)
    assert history[calls:] == [("b", CAP // 2), ("a", CAP // 2)]


# --- mutation order and failure ---------------------------------------------

@pytest.mark.asyncio
async def test_an_unconfirmed_donor_decrease_lends_nothing_until_it_is_confirmed():
    owner, clock, a, b, history = await _borrowing_pair()
    b.lose_ack = True                       # b applies the decrease but its answer is lost
    await _pass(owner, clock, {"a": _demand(a.applied), "b": _demand(b.applied)})
    assert a.applied == FLOOR               # nothing was raised against b's unconfirmed decrease
    assert not owner.status().ok and owner.status().effective is None
    b.lose_ack = False
    await _pass(owner, clock, {"a": _demand(a.applied), "b": _demand(b.applied)})
    assert a.applied == b.applied == CAP // 2 and owner.status().ok
    _never_over(history, CAP)


@pytest.mark.asyncio
async def test_a_lost_acknowledgement_of_a_raise_counts_the_raise():
    owner, clock, a, b, history = await _borrowing_pair()
    # a wakes; b is reduced; a's raise is applied but its answer lost.
    a.lose_ack = True
    await _pass(owner, clock, {"a": _demand(a.applied), "b": _demand(b.applied)})
    assert a.applied == CAP // 2 and not owner.status().ok
    # b wants everything again: it cannot be raised past what a may now hold.
    await _pass(owner, clock, {"a": _demand(), "b": _demand(b.applied)})
    assert b.applied <= CAP - CAP // 2
    _never_over(history, CAP)


@pytest.mark.asyncio
async def test_a_control_call_that_never_answers_is_ambiguous_not_released(monkeypatch):
    monkeypatch.setattr(runtime_coordination, "CONTROL_TIMEOUT_SECONDS", 0.05)
    owner, clock, a, b, history = await _borrowing_pair()
    b.hang = True
    await _pass(owner, clock, {"a": _demand(a.applied), "b": _demand(b.applied)})
    assert a.applied == FLOOR and not owner.status().ok
    _never_over(history, CAP)


@pytest.mark.asyncio
async def test_an_executor_observable_again_is_reconfirmed_before_anything_is_lent_against_it():
    owner, clock, a, b, history = await _borrowing_pair()
    await _pass(owner, clock, {"a": ExecutorDemand(False, True), "b": _demand(b.applied)})
    # a restarted natively while unobservable: whatever it now enforces is unknown.
    a.applied = 0
    history.append(("a", 0))
    calls = len(history)
    await _pass(owner, clock, {"a": _demand(), "b": _demand(b.applied)})
    assert history[calls][0] == "a" and a.applied == FLOOR    # re-confirmed first
    assert owner.status().ok
    _never_over(history[calls:], CAP)


@pytest.mark.parametrize("cap,expected", [(100, None), (101, 101), (150, 150)])
@pytest.mark.asyncio
async def test_the_cap_is_never_assigned_below_an_executors_smallest_enforceable_ceiling(lab, cap, expected):
    clock = _Clock()
    owner = _owner([lab.executor], cap, clock)
    admitted = await owner.admit(lab.executor)
    assert admitted is (expected is not None)
    if expected is None:
        assert lab.sab.speedlimit_abs == 0 and not owner.status().ok   # never told 100 (a percentage)
    else:
        assert lab.sab.speedlimit_abs == expected


@pytest.mark.asyncio
async def test_a_tight_cap_shared_with_sab_never_rounds_into_zero_or_below_its_minimum(lab):
    history, clock = [], _Clock()
    other = _Native("other", history)
    owner = _owner([lab.executor, other], 150, clock)
    assert await owner.admit(lab.executor) and await owner.admit(other)
    assert lab.sab.speedlimit_abs >= 101 and other.applied >= 1
    assert lab.sab.speedlimit_abs + other.applied <= 150
    tight = _owner([lab.executor, other], 101, clock)
    assert await tight.admit(lab.executor) is True
    assert await tight.admit(other) is False            # no positive share fits beside SAB's minimum
    assert 0 not in [value for _identity, value in history]


# --- SAB: a dormant native reservation that can wake on its own -------------

@pytest.mark.asyncio
async def test_sab_waking_natively_stays_within_its_floor_and_is_then_given_a_fair_share(lab):
    history, clock = [], _Clock()
    sab_id = lab.executor.descriptor.id
    original = lab.sab.set_speedlimit

    async def recorded(value):
        effective = await original(value)
        history.append((sab_id, effective))
        return effective

    lab.sab.set_speedlimit = recorded
    borrower = _Native("borrower", history)
    owner = _owner([lab.executor, borrower], CAP, clock)
    assert await owner.admit(lab.executor) and await owner.admit(borrower)
    # SAB's job waits in its queue (reserved, not acquiring): it lends all but its floor.
    await _pass(owner, clock, {sab_id: _demand(), "borrower": _demand(borrower.applied)})
    assert lab.sab.speedlimit_abs == FLOOR and borrower.applied == CAP - FLOOR
    # SAB starts the job without a core admission: its confirmed native floor
    # bounds it while the borrower holds the rest.
    _never_over(history, CAP)
    # The next pass sees it saturated at its floor and reclaims a fair share
    # for it -- the borrower is reduced before SAB is raised.
    calls = len(history)
    await _pass(owner, clock, {sab_id: _demand(lab.sab.speedlimit_abs), "borrower": _demand(borrower.applied)})
    assert history[calls:] == [("borrower", CAP // 2), (sab_id, CAP // 2)]
    assert lab.sab.speedlimit_abs == borrower.applied == CAP // 2
    _never_over(history, CAP)
    assert owner.status().ok
    # SAB has no per-executor acquisition gate: nothing here paused it.
    assert not lab.executor.capabilities.acquisition_gate


def test_sab_declares_its_smallest_expressible_absolute_ceiling():
    from executors.sabnzbd.executor import SabnzbdExecutor
    assert SabnzbdExecutor.capabilities.minimum_bandwidth_ceiling == 101


def test_a_minimum_bandwidth_ceiling_is_a_positive_rate():
    for value in (0, -1, True):
        with pytest.raises(ValueError):
            ExecutorCapabilities(minimum_bandwidth_ceiling=value)


# --- aria2: the egress budget binds, the native limiter keeps 1.5x headroom --

class _Aria2Client:
    url = "http://127.0.0.1:1/jsonrpc"

    def __init__(self, *, deaf=False):
        self.options = {}
        self.deaf = deaf
        self.fail = False

    async def change_global_options(self, values):
        if self.fail:
            raise ConnectionResetError("aria2 RPC lost")
        if not self.deaf:
            self.options.update(values)
        return "OK"

    async def get_global_options(self):
        return dict(self.options)

    async def test(self):
        return "1.37.0"


def _aria2(tmp_path, client):
    from executors.aria2.executor import Aria2Configuration, Aria2Executor
    from services.downloader_egress_guard import DownloaderEgressGuard

    async def authorize(_handle, _action):
        return True

    guard = DownloaderEgressGuard()
    return Aria2Executor(client, Aria2Configuration(str(tmp_path)), authorize, egress=guard), guard


@pytest.mark.parametrize("share,native", [(10 * MIB, 15 * MIB), (3, 5), (1, 2), (0, 0)])
@pytest.mark.asyncio
async def test_aria2_enforces_its_share_on_the_budget_with_native_headroom(tmp_path, share, native):
    client = _Aria2Client()
    executor, guard = _aria2(tmp_path, client)
    result = await executor.set_bandwidth_ceiling(share)
    assert result.error is None and result.effective_bytes_per_second == share
    assert guard.budget("aria2").rate == share                    # authoritative
    assert client.options["max-overall-download-limit"] == str(native)


@pytest.mark.asyncio
async def test_aria2_share_is_unconfirmed_when_the_native_headroom_does_not_read_back(tmp_path):
    executor, guard = _aria2(tmp_path, _Aria2Client(deaf=True))
    result = await executor.set_bandwidth_ceiling(10 * MIB)
    assert result.effective_bytes_per_second is None
    assert guard.budget("aria2").rate == 10 * MIB                 # the budget never exceeds the share


def test_aria2_daemon_start_uses_the_same_headroom_as_a_live_change(tmp_path):
    from executors.aria2.definition import Aria2Options
    from executors.aria2.runtime import Aria2Runtime, Aria2RuntimeConfiguration, native_download_limit

    options = Aria2Options(log_file=str(Path(tmp_path) / "aria2.log"), session_file=str(Path(tmp_path) / "aria2.session"))
    runtime = Aria2Runtime()
    runtime.configure(Aria2RuntimeConfiguration(options=options, download_root=str(tmp_path / "download")))
    runtime.assign_bandwidth_ceiling(10 * MIB)
    assert f"--max-overall-download-limit={native_download_limit(10 * MIB)}" in runtime._command()
    assert native_download_limit(10 * MIB) == 15 * MIB
    runtime.assign_bandwidth_ceiling(0)
    assert "--max-overall-download-limit=0" in runtime._command()


# --- restoring Unlimited lifts every ceiling, confirmed or not ---------------

async def _aria2_reserved_under_unlimited(tmp_path):
    client = _Aria2Client()
    executor, guard = _aria2(tmp_path, client)
    owner = _owner([executor], 0, _Clock())
    assert await owner.admit(executor)                    # reserved; nothing limited under Unlimited
    # A finite cap: the budget takes it, aria2's native change is lost.
    client.fail = True
    owner.configure(CAP)
    status = await owner.converge()
    assert guard.budget("aria2").rate == CAP and not status.ok and status.effective is None
    return owner, client, guard


@pytest.mark.asyncio
async def test_restoring_unlimited_lifts_a_ceiling_whose_finite_apply_was_never_confirmed(tmp_path):
    owner, client, guard = await _aria2_reserved_under_unlimited(tmp_path)
    client.fail = False
    owner.configure(0)
    status = await owner.converge()
    assert guard.budget("aria2").rate == 0                          # budget lifted
    assert client.options["max-overall-download-limit"] == "0"       # native lifted, read back
    assert status.ok and status.effective == 0 and owner.assigned == {"aria2": 0}


@pytest.mark.asyncio
async def test_a_failed_unlimited_apply_stays_unproven_and_is_retried(tmp_path):
    owner, client, guard = await _aria2_reserved_under_unlimited(tmp_path)
    owner.configure(0)
    status = await owner.converge()                                  # aria2 still unreachable
    assert not status.ok and status.effective is None                # never reported as Unlimited
    assert "aria2" not in owner.assigned
    client.fail = False
    await owner.observe({})                                          # the next reconcile pass retries
    status = owner.status()
    assert guard.budget("aria2").rate == 0 and client.options["max-overall-download-limit"] == "0"
    assert status.ok and status.effective == 0
    # Once confirmed, later Unlimited passes leave it alone.
    calls = dict(client.options)
    client.fail = True
    await owner.observe({})
    assert (await owner.converge()).ok and client.options == calls
