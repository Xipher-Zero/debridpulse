"""1.0.14: an estimated time remaining per actively downloading transfer.

The estimate is the transfer's provable remaining acquisition bytes
(``TransferRepository.acquisition_work``) over the summed rate the one core
throughput meter measures for exactly the attempts acquiring them
(``ExecutionThroughputMeter.acquisition_rate``, composed by
``runtime_telemetry.acquisition_eta`` and read through
``TransferEngine.acquisition_etas``). No second sampler, no global rate, no
estimate where the remaining work cannot be proven.

Expected figures are computed independently from the counters the tests feed,
never read back from the estimator. Meter cases use a stepped clock; engine
cases run the real repository and engine over a memory executor.
"""
from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import db.database as database
from application.service import ApplicationService
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.convergence_engine import TransferEngine
from transfers.models import ExecutionActivity, ExecutionState, TransferProgress, TransferRequest
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry
from transfers.runtime_telemetry import (
    ETA_HOLD_SECONDS, ETA_MIN_SPAN_SECONDS, ETA_PAUSED_WINDOW_SECONDS, ETA_SMOOTHING_SECONDS,
    ETA_DISPLAY_SECONDS, ETA_WINDOW_SECONDS, MAX_ETA_SECONDS, EtaPresentation, ExecutionThroughputMeter, acquisition_eta,
    acquisition_eta_payload,
)

INDETERMINATE = (None, False, False, True)

MIB = 1024 * 1024
ROOT = Path(__file__).resolve().parents[2]


class Clock:
    def __init__(self, now=1000.0):
        self.now = float(now)

    def __call__(self):
        return self.now


def _meter():
    clock = Clock()
    return ExecutionThroughputMeter(clock=clock), clock


def _run(meter, clock, counters, seconds, rates, step=0.5):
    """``rates`` bytes/second per attempt for ``seconds``, one pass per ``step``."""
    for _ in range(int(round(seconds / step))):
        clock.now += step
        for key, rate in rates.items():
            counters[key] = counters.get(key, 0) + int(rate * step)
        meter.record({key: (clock.now, value) for key, value in counters.items()})


# ── the estimator's measurement rules (stepped clock) ─────────────────────────

def test_the_estimator_derives_its_bounds_from_the_meters_own_windows():
    from transfers import runtime_telemetry as telemetry
    assert ETA_WINDOW_SECONDS == 5.0                                  # the presentation horizon, not 30 s
    assert ETA_SMOOTHING_SECONDS == 12.0 and ETA_DISPLAY_SECONDS == 2.5   # presentation only, never the rate
    assert ETA_MIN_SPAN_SECONDS == telemetry.WINDOW_SECONDS == 4.0
    assert ETA_HOLD_SECONDS == telemetry._SUSTAINED_GAP_SECONDS == 5.0
    assert MAX_ETA_SECONDS == 9999 * 3600 + 59 * 60 + 59


def test_a_new_run_has_no_rate_until_it_spans_the_presentation_window():
    meter, clock = _meter()
    counters = {}
    _run(meter, clock, counters, 3.5, {"a": 2 * MIB})
    assert meter.acquisition_rate("a") is None                      # 3.0 s of movement measured: not yet
    _run(meter, clock, counters, 1.0, {"a": 2 * MIB})
    rate, held = meter.acquisition_rate("a")
    assert rate == pytest.approx(2 * MIB) and held is False


def test_a_steady_download_counts_down_by_the_time_that_passed():
    meter, clock = _meter()
    counters, total = {}, 400 * MIB
    _run(meter, clock, counters, 10, {"a": 4 * MIB})
    seen = []
    for _ in range(10):
        _run(meter, clock, counters, 1, {"a": 4 * MIB})
        remaining = total - counters["a"]
        seen.append(acquisition_eta(remaining, ["a"], meter))
    expected = [(math.ceil((total - (10 + second) * 4 * MIB) / (4 * MIB)), False, False, False) for second in range(1, 11)]
    assert seen == expected                                         # 89, 88, ... 80: one second per second
    assert all(later[0] < earlier[0] for earlier, later in zip(seen, seen[1:]))


def test_a_bursty_coarse_counter_moves_the_estimate_gently():
    """A counter that only moves in 8 MiB steps every 2 s (4 MiB/s on average)
    swings a 5 s measurement between bursts; the presented estimate stays near
    the true average and never jumps upward of note."""
    meter, clock = _meter()
    presentation = EtaPresentation()
    counter, total, estimates = 0, 2048 * MIB, []
    for step in range(120):                                         # 60 s
        clock.now += 0.5
        if step % 4 == 3:
            counter += 8 * MIB
        meter.record({"a": (clock.now, counter)})
        if step >= 60:
            estimates.append(presentation.present(1, ("a",), acquisition_eta(total - counter, ["a"], meter),
                                                  clock.now)[0])
    true_seconds = [(total - 4 * MIB * (0.5 * (step + 1))) / (4 * MIB) for step in range(60, 120)]
    for got, true in zip(estimates, true_seconds):
        assert abs(got - true) <= 0.15 * true                       # never an absurd swing
    assert max(b - a for a, b in zip(estimates, estimates[1:])) <= 12  # no jump upward of note


def test_irregular_observation_intervals_are_measured_by_their_timestamps():
    meter, clock = _meter()
    counter = 0
    for step in (0.3, 0.9, 0.4, 1.4, 0.5, 0.7, 0.8, 0.6):          # 5.6 s at exactly 3 MiB/s
        clock.now += step
        counter += int(3 * MIB * step)
        meter.record({"a": (clock.now, counter)})
    rate, _held = meter.acquisition_rate("a")
    assert rate == pytest.approx(3 * MIB, rel=1e-3)


def test_a_brief_stall_holds_the_estimate_exactly_and_says_so():
    meter, clock = _meter()
    counters, total = {}, 100 * MIB
    _run(meter, clock, counters, 8, {"a": 2 * MIB})
    shown = acquisition_eta(total - counters["a"], ["a"], meter)
    assert shown[1] is False
    for missing in range(1, 7):                                     # up to 3 s of flat samples
        clock.now += 0.5
        meter.record({"a": (clock.now, counters["a"])})
        held = acquisition_eta(total - counters["a"], ["a"], meter)
        assert held[0] == shown[0], "a held estimate never counts down"
        assert held[1] is (missing * 0.5 > 1.0)                     # held past the presentation grace


def test_a_sustained_stall_has_no_estimate():
    meter, clock = _meter()
    counters = {}
    _run(meter, clock, counters, 8, {"a": 2 * MIB})
    _run(meter, clock, counters, ETA_HOLD_SECONDS, {"a": 0})
    assert acquisition_eta(10 * MIB, ["a"], meter) is not None       # exactly at the hold: still held
    _run(meter, clock, counters, 0.5, {"a": 0})
    assert acquisition_eta(10 * MIB, ["a"], meter) is None


def test_observation_that_stops_altogether_ends_the_estimate_too():
    meter, clock = _meter()
    counters = {}
    _run(meter, clock, counters, 8, {"a": 2 * MIB})
    clock.now += ETA_HOLD_SECONDS + 0.5                             # no pass at all
    assert meter.acquisition_rate("a") is None


def test_a_resumed_download_is_measured_afresh_never_from_its_old_rate():
    meter, clock = _meter()
    counters, total = {}, 1000 * MIB
    _run(meter, clock, counters, 10, {"a": 1 * MIB})
    _run(meter, clock, counters, 10, {"a": 0})                      # a sustained stall
    assert acquisition_eta(total - counters["a"], ["a"], meter) is None   # still past the hold: none at all
    _run(meter, clock, counters, 3.5, {"a": 5 * MIB})
    # Moving again but not measured yet: acquisition without a number, never the old rate's figure.
    assert acquisition_eta(total - counters["a"], ["a"], meter) == INDETERMINATE, "no resurrected estimate"
    _run(meter, clock, counters, 1.5, {"a": 5 * MIB})
    rate, _held = meter.acquisition_rate("a")
    assert rate == pytest.approx(5 * MIB), "neither the stall nor the old 1 MiB/s dilutes the new run"


@pytest.mark.parametrize("discontinuity", ["rollback", "not_acquiring", "new_attempt"])
def test_a_discontinuity_starts_a_new_measurement(discontinuity):
    meter, clock = _meter()
    counters = {}
    _run(meter, clock, counters, 8, {"a": 2 * MIB})
    assert meter.acquisition_rate("a") is not None
    clock.now += 0.5
    if discontinuity == "rollback":
        counters["a"] = 1 * MIB
        meter.record({"a": (clock.now, counters["a"])})
        assert meter.acquisition_rate("a") is None
    elif discontinuity == "not_acquiring":
        meter.record({"a": (clock.now, None)})
        assert meter.acquisition_rate("a") is None
    else:
        meter.record({"b": (clock.now, 0)})                         # the replacement attempt
        assert meter.acquisition_rate("a") is None and meter.acquisition_rate("b") is None


def test_concurrent_attempts_are_summed_and_only_the_named_ones():
    meter, clock = _meter()
    counters = {}
    _run(meter, clock, counters, 8, {"a": 1 * MIB, "b": 3 * MIB, "other": 50 * MIB})
    assert acquisition_eta(40 * MIB, ["a", "b"], meter) == (10, False, False, False)   # 40 MiB at 4 MiB/s
    assert acquisition_eta(40 * MIB, ["a", "a", "b"], meter) == (10, False, False, False)   # each attempt once
    # A writer without a rate yet keeps its bytes in the work but adds no rate:
    # 40 MiB over the 1 MiB/s actually measured, never an invented speed.
    assert acquisition_eta(40 * MIB, ["a", "missing"], meter) == (40, False, True, False)
    assert acquisition_eta(40 * MIB, ["missing"], meter) is None          # nothing measured: none


def test_one_writer_still_past_the_hold_makes_the_whole_estimate_indeterminate():
    """A writer not measured yet adds bytes but no rate; one that moved and has
    been still past the hold is sustained inactivity -- no countdown at the
    others' pace: while another still moves the transfer is acquiring without
    a number (indeterminate), and once none moves it has no estimate at all."""
    meter, clock = _meter()
    counters = {}
    _run(meter, clock, counters, 8, {"a": 2 * MIB, "b": 2 * MIB})
    assert acquisition_eta(80 * MIB, ["a", "b"], meter) == (20, False, False, False)
    _run(meter, clock, counters, ETA_HOLD_SECONDS, {"a": 2 * MIB, "b": 0})
    assert acquisition_eta(80 * MIB, ["a", "b"], meter) is not None    # b briefly still: within the hold
    _run(meter, clock, counters, 0.5, {"a": 2 * MIB, "b": 0})
    assert acquisition_eta(80 * MIB, ["a", "b"], meter) == INDETERMINATE   # b stalled: no countdown at a's pace
    # A writer that has not moved at all yet is new, not stalled: it does not clear it.
    meter.record({**{key: (clock.now, value) for key, value in counters.items()}, "c": (clock.now, 0)})
    assert not meter.acquisition_stalled("c")
    assert acquisition_eta(80 * MIB, ["a", "c"], meter) == (40, False, True, False)
    _run(meter, clock, counters, ETA_HOLD_SECONDS + 0.5, {"a": 0, "b": 0})
    assert acquisition_eta(80 * MIB, ["a", "b"], meter) is None        # nothing moves: no estimate, not infinity


def test_rounding_and_the_representable_ceiling():
    meter, clock = _meter()
    counters = {}
    _run(meter, clock, counters, 8, {"a": 1000})                    # exactly 1000 B/s
    assert acquisition_eta(1, ["a"], meter) == (1, False, False, False)           # a fraction of a second is 1 s, never 0
    assert acquisition_eta(1500, ["a"], meter) == (2, False, False, False)        # rounded up
    assert acquisition_eta(MAX_ETA_SECONDS * 1000, ["a"], meter) == (MAX_ETA_SECONDS, False, False, False)
    assert acquisition_eta(MAX_ETA_SECONDS * 1000 + 1, ["a"], meter) == INDETERMINATE   # past the ceiling, moving
    assert acquisition_eta(100 * 3600 * 1000, ["a"], meter) == (360000, False, False, False)   # 100 h stays 100 h
    assert acquisition_eta(0, ["a"], meter) is None                  # no remaining work is no estimate
    assert acquisition_eta(None, ["a"], meter) == INDETERMINATE      # unaccountable work, moving
    assert acquisition_eta(None, ["a"], meter, estimated=True) == (None, False, True, True)
    assert acquisition_eta(10, [], meter) is None
    assert acquisition_eta_payload(INDETERMINATE) == {"seconds": None, "held": False, "estimated": False,
                                                      "indeterminate": True}
    assert acquisition_eta_payload(None) is None
    assert acquisition_eta_payload((61, True, False, False)) == {"seconds": 61, "held": True, "estimated": False, "indeterminate": False}
    assert acquisition_eta(1500, ["a"], meter, estimated=True) == (2, False, True, False)   # extrapolated work


# ── what is shown: the one presentation of the estimate (stepped clock) ──────

def _replay(noise, *, change_at=None, seconds=90, total=4096 * MIB, seed=1105):
    """4 MiB/s (2 MiB/s from ``change_at``) with each 0.5 s pass's bytes off by
    up to ``noise`` of the rate, seeded: ``[(t, raw seconds, shown seconds,
    true seconds)]`` for every pass with an estimate. The truth is the remaining
    bytes over the true rate, never the estimator's own figure."""
    import random

    rnd, (meter, clock), presentation = random.Random(seed), _meter(), EtaPresentation()
    counter, timeline = 0, []
    for index in range(int(seconds / 0.5)):
        clock.now += 0.5
        elapsed = (index + 1) * 0.5
        rate = 2 * MIB if change_at is not None and elapsed >= change_at else 4 * MIB
        counter += int(rate * 0.5 * (1 + rnd.uniform(-noise, noise)))
        meter.record({"a": (clock.now, counter)})
        raw = acquisition_eta(total - counter, ["a"], meter)
        shown = presentation.present(1, ("a",), raw, clock.now)
        if raw is not None and raw[0] is not None:
            timeline.append((elapsed, raw[0], shown[0], (total - counter) / rate))
    return timeline


def _changes(timeline, at=0):
    """The instants at which the number shown changed: what the operator sees
    move, ordinary countdown steps included -- not only large revisions."""
    return [b[0] for a, b in zip(timeline, timeline[1:]) if b[at] != a[at]]


def _rested(changes, seconds):
    return all(later - earlier >= seconds for earlier, later in zip(changes, changes[1:]))


@pytest.mark.parametrize("noise", [0.2, 0.5])
def test_a_jittery_rate_is_shown_as_a_number_that_rests(noise):
    """The measurement's scatter is no longer put up every 0.5 s, and neither
    is the countdown: the number shown changes at most once in 2.5 s, rests
    between, and is no further from the truth than the raw measurement."""
    timeline = _replay(noise)
    raw = [(t, raw) for t, raw, _shown, _true in timeline]
    shown = [(t, shown) for t, _raw, shown, _true in timeline]
    error = lambda figures: sum(abs(got - item[3]) / item[3] for (_t, got), item in zip(figures, timeline)) / len(timeline)
    assert len(_changes(raw, 1)) > 0.9 * len(raw)                       # before: a new number nearly every pass
    changes = _changes(shown, 1)
    assert _rested(changes, 2.5) and len(changes) <= len(shown) * 0.5 / 2.5 + 1
    assert error(shown) <= error(raw)
    assert error(shown) <= (0.04 if noise == 0.2 else 0.08)


def test_a_steady_rate_counts_down_in_steps_of_the_time_that_passed():
    """At a steady rate the number steps every 2.5 s by the time that passed
    (2 or 3 s, as whole seconds), never one second at a time every pass, and
    is never more than one step behind the raw figure."""
    timeline = _replay(0.0)
    shown = [(t, shown) for t, _raw, shown, _true in timeline]
    changes = _changes(shown, 1)
    assert [b - a for a, b in zip(changes, changes[1:])] == [2.5] * (len(changes) - 1)
    figures = dict(shown)
    assert all(figures[earlier] - figures[later] in (2, 3) for earlier, later in zip(changes, changes[1:]))
    assert all(0 <= shown - raw <= 3 for _t, raw, shown, _true in timeline)


def test_a_sustained_change_of_pace_arrives_over_seconds_and_settles():
    """The rate halves at 40 s (the truth doubles). The number starts to move
    within one display step of the change being measured, is within 20 % of
    the truth 15 s after it and within 10 % 25 s after -- a revision spread
    over the smoothing horizon, neither at once nor the half-minute slide of a
    30 s window -- and keeps resting between steps throughout."""
    timeline = _replay(0.0, change_at=40, seconds=90)
    by_t = {round(t * 2): (shown, true) for t, _raw, shown, true in timeline}
    before = by_t[2 * 40][0]
    assert by_t[2 * 47.5][0] > before + 25                               # moving within 7.5 s
    assert all(abs(shown - true) <= 0.20 * true for key, (shown, true) in by_t.items() if key >= 2 * 55)
    assert all(abs(shown - true) <= 0.10 * true for key, (shown, true) in by_t.items() if key >= 2 * 65)
    assert _rested(_changes([(t, shown) for t, _raw, shown, _true in timeline], 1), 2.5)


def test_a_held_estimate_is_shown_frozen_then_counts_on_from_it():
    meter, clock = _meter()
    presentation, counters, total = EtaPresentation(), {}, 400 * MIB

    def shown():
        return presentation.present(1, ("a",), acquisition_eta(total - counters["a"], ["a"], meter), clock.now)

    for _ in range(20):
        _run(meter, clock, counters, 0.5, {"a": 4 * MIB})
        last = shown()
    frozen = None
    for _ in range(8):                                                   # 4 s still: within the hold
        _run(meter, clock, counters, 0.5, {"a": 0})
        figure = shown()
        if figure[1]:
            frozen = frozen or figure
            assert figure == frozen                                      # held: exactly the same, however long
        else:
            assert last[0] - 3 <= figure[0] <= last[0]                   # the presentation grace: still counting down
    assert frozen is not None and last[0] - 3 <= frozen[0] <= last[0]
    assert frozen == (frozen[0], True, False, False)
    # Resumed: a 4 s pause is part of the pace measured (``ETA_PAUSED_WINDOW_SECONDS``,
    # stillness included), so the figure may rise by at most that share,
    # smoothed, and its number still rests between steps.
    timeline = [(clock.now, frozen[0])]
    for _ in range(24):
        _run(meter, clock, counters, 0.5, {"a": 4 * MIB})
        figure = shown()
        true = (total - counters["a"]) / (4 * MIB)
        assert figure[1] is False and true - 1.5 <= figure[0] <= true * (1 + 4 / 10)
        timeline.append((clock.now, figure[0]))
    assert _rested(_changes(timeline, 1), 2.5)


@pytest.mark.parametrize("moving, still", [(1, 3), (3, 4.5), (4, 3), (6, 4.5), (8, 3), (12, 4.5)])
def test_a_download_that_stops_and_starts_is_shown_at_its_true_average(moving, still):
    """Moving at 1000 B/s for ``moving`` seconds, then still for ``still``
    (within the hold), repeatedly: the pace is the average, stillness
    included -- never the moving rate's promise, never a saw-tooth -- and the
    number rests between steps whether held or not."""
    meter, clock = _meter()
    presentation, counter, total, average = EtaPresentation(), 0, 10 ** 7, 1000 * moving / (moving + still)
    figures = []
    for index in range(400):                                             # 200 s
        clock.now += 0.5
        if 0 < ((index + 1) * 0.5) % (moving + still) <= moving:
            counter += 500
        meter.record({"a": (clock.now, counter)})
        shown = presentation.present(1, ("a",), acquisition_eta(total - counter, ["a"], meter), clock.now)
        if index >= 120 and shown is not None:
            figures.append((clock.now, shown[0], (total - counter) / average))
    assert len(figures) == 280                                           # a figure on every pass
    for _t, shown, true in figures:
        assert 0.85 * true <= shown <= 1.10 * true, (shown, true)
    assert _rested(_changes(figures, 1), 2.5)


@pytest.mark.parametrize("counter", ["steps_0_7s", "random_writes"])
def test_an_uneven_counter_is_no_pause_and_a_change_of_pace_shows_within_seconds(counter):
    """A counter that steps every 0.7 s (seen by 0.5 s passes) is uneven,
    never paused: the rate stays on the 5 s window, so the pace halving at 60 s
    is measured within 10 s -- not a 30 s slide. One written at random
    instants is still at times for seconds, which is a pause: it is measured
    within the paused window, never later."""
    import random

    rnd, (meter, clock) = random.Random(7), _meter()
    total, writes, at = 0, [], 0.0
    while at < 90:                                                       # the writes, 1 MB/s then 0.5 MB/s
        at += 0.7 if counter == "steps_0_7s" else rnd.expovariate(1 / 0.6)
        writes.append(at)
    for index in range(180):
        clock.now += 0.5
        elapsed = (index + 1) * 0.5
        while writes and writes[0] <= elapsed:
            instant = writes.pop(0)
            total = int(min(instant, 60) * 1_000_000 + max(0.0, instant - 60) * 500_000)
        meter.record({"a": (clock.now, total)})
        if elapsed == (70 if counter == "steps_0_7s" else 60 + ETA_PAUSED_WINDOW_SECONDS + 1):
            rate, _held = meter.acquisition_rate("a")
            assert rate == pytest.approx(500_000, rel=0.15)


def test_an_invalidated_estimate_starts_afresh():
    """No estimate, an indeterminate one, or a new basis (a writer started or
    became measured): nothing smoothed survives; the next figure is the raw
    one exactly."""
    presentation = EtaPresentation()
    assert presentation.present(1, ("a",), (100, False, False, False), 0.0) == (100, False, False, False)
    assert presentation.present(1, ("a",), (300, False, False, False), 0.5)[0] < 300   # smoothed toward it
    now = 1.0                                                            # each figure newer than the last
    for invalidation, basis in ((None, ("a",)), (INDETERMINATE, ("a",)), ((50, False, False, False), ("a", "b"))):
        assert presentation.present(1, basis, invalidation, now) == invalidation
        assert presentation.present(1, ("a",), (300, False, False, False), now + 0.5) == (300, False, False, False)
        now += 1.0
    # The engine's basis names which writers are measured, too.
    assert presentation.present(1, (("a", "b"), ("a",)), (300, False, True, False), now) == (300, False, True, False)
    assert presentation.present(1, (("a", "b"), ("a", "b")), (100, False, False, False), now + 0.5) == (
        100, False, False, False)


FINITE = (100, False, False, False)


@pytest.mark.parametrize("older", [
    pytest.param((("a",), (950, False, False, False)), id="changed-basis"),
    pytest.param((("b",), None), id="none"),
    pytest.param((("b",), INDETERMINATE), id="indeterminate"),
    pytest.param((("b",), (950, True, True, False)), id="same-basis-held-estimated"),
    pytest.param((("b",), (950, False, False, False)), id="same-basis"),
])
def test_older_figures_finishing_after_newer_finite_ones_change_nothing(older):
    """Writer B's figures, observed at 100, are presented first; figures read
    at 99 (writer A's basis, none, indeterminate, or B's own with any flags)
    finish after them: they are answered with what B's showed and change
    nothing -- B's next figures continue smoothing, never start afresh."""
    basis, eta = older
    presentation, control = EtaPresentation(), EtaPresentation()
    for subject in (presentation, control):
        subject.present(1, ("b",), (110, False, False, False), 97.5)
        newer = subject.present(1, ("b",), FINITE, 100.0)
    assert newer[1:] == (False, False, False) and 100 < newer[0] < 110   # smoothed toward B's figure
    assert presentation.present(1, basis, eta, 99.0) == newer
    assert presentation._state == control._state                      # nothing moved
    for at, raw in ((100.5, (60, False, False, False)), (101.0, (60, True, False, False)), (101.5, FINITE)):
        assert presentation.present(1, ("b",), raw, at) == control.present(1, ("b",), raw, at)
    assert presentation.present(1, ("b",), (60, False, False, False), 102.0)[0] != 60     # still smoothing


@pytest.mark.parametrize("newer", [pytest.param(None, id="none"), pytest.param(INDETERMINATE, id="indeterminate")])
def test_older_finite_figures_never_revive_a_newer_invalidation(newer):
    """A newer invalidation stands against older finite figures: they are
    answered with it, and the next current figure starts afresh."""
    presentation = EtaPresentation()
    presentation.present(1, ("b",), FINITE, 98.0)
    assert presentation.present(1, ("b",), newer, 100.0) == newer
    for older in (FINITE, (950, True, True, False), (950, False, False, False)):
        for basis in (("b",), ("a",)):
            assert presentation.present(1, basis, older, 99.0) == newer
    assert presentation._state[1] == (100.0, newer, None)
    assert presentation.present(1, ("b",), (300, False, False, False), 100.5) == (300, False, False, False)


def test_a_newer_invalidation_takes_effect_at_once():
    """Ordering never delays a genuinely newer transition: none or an
    indeterminate figure replaces a finite one the moment it is newer, and
    figures at the same instant are current, not older."""
    presentation = EtaPresentation()
    presentation.present(1, ("b",), FINITE, 100.0)
    assert presentation.present(1, ("b",), INDETERMINATE, 100.0) == INDETERMINATE
    assert presentation.present(1, ("b",), None, 100.5) is None
    assert presentation.present(1, ("b",), FINITE, 101.0) == FINITE


def test_the_ordering_fence_is_retired_with_its_entry():
    """An invalidation keeps a fence, bounded like every entry: dropped once
    not presented for the retention window, after which nothing is kept."""
    presentation = EtaPresentation()
    presentation.present(1, ("a",), FINITE, 0.0)
    presentation.present(1, ("a",), None, 1.0)
    presentation.present(2, ("b",), INDETERMINATE, 1.0)
    presentation.retire(30.0)
    assert set(presentation._state) == {1, 2}
    presentation.retire(31.5)
    assert presentation._state == {}


def test_no_estimate_indeterminate_and_a_new_basis_are_shown_at_once_whatever_the_pace():
    """The 2.5 s pace is the number's alone: half a second after the number
    changed, no estimate, an indeterminate one or a new basis is shown at
    once, and the next figure after any of them is the raw one at once."""
    for invalidation, basis in ((None, ("a",)), (INDETERMINATE, ("a",)), ((50, False, False, False), ("a", "b"))):
        presentation = EtaPresentation()
        presentation.present(1, ("a",), (300, False, False, False), 0.0)
        assert presentation.present(1, ("a",), (200, False, False, False), 2.5)[0] < 300     # the number changed
        assert presentation.present(1, basis, invalidation, 3.0) == invalidation              # 0.5 s later: at once
        assert presentation.present(1, ("a",), (120, False, False, False), 3.5) == (120, False, False, False)


def test_a_held_number_stays_exactly_while_held_toggles():
    """Held on and off every other pass for 20 s: while held the number is
    exactly the one shown, never counted down, and it still changes at most
    once in 2.5 s; held and best-effort are carried, never the number."""
    presentation, figures = EtaPresentation(), []
    presentation.present(1, ("a",), (600, False, False, False), 0.0)
    for index in range(1, 41):
        now, held, rough = index * 0.5, index % 2 == 0, index % 3 == 0
        shown = presentation.present(1, ("a",), (600 - index * 0.5, held, rough, False), now)
        assert shown[1:] == (held, rough, False)
        if held:
            assert shown[0] == figures[-1][1]                           # exactly the number last shown
        figures.append((now, shown[0]))
    assert _rested(_changes(figures, 1), 2.5) and figures[-1][1] < 600


def test_an_older_call_is_answered_with_the_rested_number_and_moves_nothing():
    """While the number rests, a newer pass still advances the observation
    instant (the ordering fence) without changing the number; a pass older
    than it is answered with that number and changes nothing -- the pace never
    lets an older figure through."""
    presentation, control = EtaPresentation(), EtaPresentation()
    for subject in (presentation, control):
        subject.present(1, ("b",), (300, False, False, False), 100.0)
        rested = subject.present(1, ("b",), (240, False, False, False), 101.0)
    assert rested == (300, False, False, False)                         # resting: the number stands
    for basis, older in ((("b",), (10, False, False, False)), (("a",), (10, False, False, False)),
                         (("b",), None), (("b",), INDETERMINATE)):
        assert presentation.present(1, basis, older, 100.5) == rested
    assert presentation._state == control._state
    assert presentation.present(1, ("b",), (240, False, False, False), 102.5) == control.present(
        1, ("b",), (240, False, False, False), 102.5)


def test_a_finishing_download_counts_down_to_its_end_and_leaves_at_once():
    """The last minute at a steady pace: the number steps down with the time
    that passed, is never more than one step above the truth, and the moment
    there is no remaining work it is gone."""
    meter, clock = _meter()
    presentation, counters, total, figures = EtaPresentation(), {}, 60 * 4 * MIB, []
    _run(meter, clock, counters, 5, {"a": 4 * MIB})
    while counters["a"] < total:
        _run(meter, clock, counters, 0.5, {"a": 4 * MIB})
        counters["a"] = min(counters["a"], total)
        shown = presentation.present(1, ("a",), acquisition_eta(total - counters["a"], ["a"], meter), clock.now)
        if shown is None:
            break
        true = (total - counters["a"]) / (4 * MIB)
        assert true <= shown[0] <= true + 3.5
        figures.append((clock.now, shown[0]))
    assert shown is None and counters["a"] == total                    # done: no estimate at once
    assert _rested(_changes(figures, 1), 2.5) and figures[-1][1] <= 4


def test_presentation_state_is_bounded_to_transfers_presented_recently():
    presentation = EtaPresentation()
    presentation.present(1, ("a",), (100, False, False, False), 0.0)
    presentation.present(2, ("b",), (100, False, False, False), 0.0)
    presentation.retire(25.0, keep={2})
    assert set(presentation._state) == {1, 2}
    presentation.retire(31.0, keep={2})
    assert set(presentation._state) == {2}                               # kept only while it is presented


# ── the logical transfer's remaining work (real repository and engine) ──────────

async def _core(tmp_path, monkeypatch, *, files=1, transfers=1, width=4):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(ParcelProvider())
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"), clock=lambda: 1000.0,
                            policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0,
                                                  max_active_executions=width))
    await engine.initialize()
    submitted = []
    for transfer in range(transfers):
        submitted.append(await engine.submit(tuple(
            TransferRequest("parcel", f"payload-{transfer}-{index}", name=f"file-{transfer}-{index}.bin")
            for index in range(files)), name=f"transfer-{transfer}", deduplicate=False))
    for _ in range(6):
        await engine.tick()
    clock = Clock()
    engine.throughput = ExecutionThroughputMeter(clock=clock)
    return SimpleNamespace(engine=engine, repository=repository, executor=executor, clock=clock,
                           transfers=[item.id for item in submitted])


async def _writers(core, transfer_id):
    return [item.execution.attempt_id for item in await core.repository.artifacts(transfer_id) if item.execution]


async def _acquire(core, seconds, totals, rates, *, step=0.5):
    """Each writer acquires at its rate toward its exact total, observed by
    the engine's own fast observation pass every ``step``."""
    for _ in range(int(round(seconds / step))):
        core.clock.now += step
        for attempt, rate in rates.items():
            job = core.executor.jobs[attempt]
            completed = min(totals[attempt], job.progress.completed_bytes + int(rate * step))
            core.executor.jobs[attempt] = replace(job, state=ExecutionState.RUNNING,
                                                  progress=TransferProgress(totals[attempt], completed, 0))
        await core.engine.sample_throughput()


def _started(core, attempt, total):
    core.executor.jobs[attempt] = replace(core.executor.jobs[attempt], progress=TransferProgress(total, 0, 0))


@pytest.mark.asyncio
async def test_one_transfer_estimate_is_its_writers_remaining_bytes_over_their_rate(tmp_path, monkeypatch):
    core = await _core(tmp_path, monkeypatch)
    [transfer] = core.transfers
    [attempt] = await _writers(core, transfer)
    _started(core, attempt, 400 * MIB)
    assert await core.engine.acquisition_etas([transfer]) == {}       # nothing moved yet: no estimate
    await _acquire(core, 3, {attempt: 400 * MIB}, {attempt: 4 * MIB})
    assert await core.engine.acquisition_etas([transfer]) == {transfer: INDETERMINATE}   # moving, still measuring
    await _acquire(core, 7, {attempt: 400 * MIB}, {attempt: 4 * MIB})
    assert await core.repository.acquisition_work([transfer]) == {transfer: (360 * MIB, (attempt,), False)}
    assert await core.engine.acquisition_etas([transfer]) == {transfer: (90, False, False, False)}
    await _acquire(core, 5, {attempt: 400 * MIB}, {attempt: 4 * MIB})
    assert await core.engine.acquisition_etas([transfer]) == {transfer: (85, False, False, False)}


@pytest.mark.asyncio
async def test_an_older_overlapping_pass_finishing_last_changes_nothing(tmp_path, monkeypatch):
    """``acquisition_etas`` reads its clock before awaiting the remaining
    work. A pass that read the work while the transfer was acquiring, but
    finishes after a newer pass that found it acquiring no more, is answered
    with the newer pass's answer (no estimate) and revives nothing."""
    import asyncio

    core = await _core(tmp_path, monkeypatch)
    [transfer] = core.transfers
    [attempt] = await _writers(core, transfer)
    _started(core, attempt, 400 * MIB)
    await _acquire(core, 10, {attempt: 400 * MIB}, {attempt: 4 * MIB})
    assert await core.engine.acquisition_etas([transfer]) == {transfer: (90, False, False, False)}
    work, gate, read = core.repository.acquisition_work, asyncio.Event(), []

    async def overlapping(ids):
        if not read:
            read.append(await work(ids))                                  # the older pass reads first ...
            await gate.wait()                                             # ... and finishes last
            return read[0]
        return {}                                                         # the newer: acquiring no more

    monkeypatch.setattr(core.repository, "acquisition_work", overlapping)
    older = asyncio.create_task(core.engine.acquisition_etas([transfer]))
    while not read:
        await asyncio.sleep(0)
    await _acquire(core, 0.5, {attempt: 400 * MIB}, {attempt: 4 * MIB})
    assert await core.engine.acquisition_etas([transfer]) == {}           # newer: no estimate, at once
    standing = dict(core.engine.eta_presentation._state)
    gate.set()
    assert await older == {}                                              # never the obsolete figure
    assert core.engine.eta_presentation._state == standing


@pytest.mark.asyncio
async def test_concurrent_transfers_never_share_a_rate(tmp_path, monkeypatch):
    core = await _core(tmp_path, monkeypatch, transfers=2)
    slow, fast = core.transfers
    [a] = await _writers(core, slow)
    [b] = await _writers(core, fast)
    totals = {a: 100 * MIB, b: 100 * MIB}
    for attempt, total in totals.items():
        _started(core, attempt, total)
    await _acquire(core, 10, totals, {a: 1 * MIB, b: 4 * MIB})
    etas = await core.engine.acquisition_etas([slow, fast])
    assert etas == {slow: (90, False, False, False), fast: (15, False, False, False)}             # 90 MiB at 1, 60 MiB at 4
    # The global speed (5 MiB/s) would have claimed 18 s and 12 s.
    assert core.engine.throughput.current() == 5 * MIB


@pytest.mark.asyncio
async def test_concurrent_artifacts_of_one_transfer_are_one_estimate(tmp_path, monkeypatch):
    core = await _core(tmp_path, monkeypatch, files=2)
    [transfer] = core.transfers
    a, b = await _writers(core, transfer)
    totals = {a: 100 * MIB, b: 300 * MIB}
    for attempt, total in totals.items():
        _started(core, attempt, total)
    await _acquire(core, 10, totals, {a: 1 * MIB, b: 3 * MIB})
    work = await core.repository.acquisition_work([transfer])
    assert work == {transfer: (90 * MIB + 270 * MIB, tuple(sorted((a, b))), False)}   # each writer once
    assert await core.engine.acquisition_etas([transfer]) == {transfer: (90, False, False, False)}   # 360 MiB at 4 MiB/s


@pytest.mark.asyncio
async def test_a_waiting_member_of_known_size_is_remaining_work(tmp_path, monkeypatch):
    """Counterexample to an executor-level estimate: one writer of a
    two-file transfer is nearly done, but the transfer is not."""
    core = await _core(tmp_path, monkeypatch, files=2, width=1)
    [transfer] = core.transfers
    [attempt] = await _writers(core, transfer)
    artifacts = await core.repository.artifacts(transfer)
    [waiting] = [item for item in artifacts if not item.execution]
    async with database.get_db() as db:
        await db.execute("UPDATE download_files SET size_bytes=? WHERE id=?", (600 * MIB, waiting.id))
        await db.commit()
    _started(core, attempt, 100 * MIB)
    await _acquire(core, 10, {attempt: 100 * MIB}, {attempt: 4 * MIB})
    work = await core.repository.acquisition_work([transfer])
    assert work == {transfer: (60 * MIB + 600 * MIB, (attempt,), False)}
    assert await core.engine.acquisition_etas([transfer]) == {transfer: (165, False, False, False)}   # not the writer's 15 s


@pytest.mark.asyncio
async def test_writers_that_start_apart_never_suppress_the_estimate(tmp_path, monkeypatch):
    """Transfer 1105's shape: three known-size downloads of one transfer,
    started a few seconds apart. A writer without a measured rate yet keeps its
    bytes in the work and adds no speed, so the estimate is there from the
    first measured writer -- longer than the truth and marked estimated -- and
    is exact once every writer is measured."""
    core = await _core(tmp_path, monkeypatch, files=3)
    [transfer] = core.transfers
    a, b, c = await _writers(core, transfer)
    totals = {a: 5000 * MIB, b: 10000 * MIB, c: 10000 * MIB}
    for attempt, total in totals.items():
        _started(core, attempt, total)
    await core.engine.reconcile_executions()                             # every writer's total is recorded
    rates = {a: 6 * MIB, b: 3 * MIB, c: 2 * MIB}
    await _acquire(core, 4.5, totals, {a: rates[a]})                      # only a has moved for 4 s
    remaining = sum(totals.values()) - int(4.5 * rates[a])
    assert await core.engine.acquisition_etas([transfer]) == {
        transfer: (math.ceil(remaining / rates[a]), False, True, False)}        # a's rate alone: never 0, never None
    await _acquire(core, 2, totals, {a: rates[a], b: rates[b]})
    eta = (await core.engine.acquisition_etas([transfer]))[transfer]
    assert eta[2] is True                                                # c not measured yet
    await _acquire(core, 6, totals, rates)
    done = {attempt: core.executor.jobs[attempt].progress.completed_bytes for attempt in totals}
    remaining = sum(totals[attempt] - done[attempt] for attempt in totals)
    seconds, held, estimated, indeterminate = (await core.engine.acquisition_etas([transfer]))[transfer]
    assert (held, estimated, indeterminate) == (False, False, False)                           # every writer measured: exact
    assert seconds == pytest.approx(remaining / sum(rates.values()), rel=0.02)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["parts", "own_estimate"])
async def test_measured_bytes_without_an_exact_total_give_a_best_effort_estimate(tmp_path, monkeypatch, scope):
    """No exact byte total, but measured bytes and the share of the work they
    are: the remaining bytes are those bytes scaled, marked estimated."""
    core = await _core(tmp_path, monkeypatch)
    [transfer] = core.transfers
    [attempt] = await _writers(core, transfer)
    completed = 0
    for _ in range(12):                                                  # 6 s at 2 MiB/s
        core.clock.now += 0.5
        completed += MIB
        share = completed / (80 * MIB)                                   # the work is 80 MiB in truth
        progress = (TransferProgress(0, completed, 0, completed_units=round(share * 40), total_units=40)
                    if scope == "parts" else TransferProgress(0, completed, 0, estimated_percentage=share * 100))
        core.executor.jobs[attempt] = replace(core.executor.jobs[attempt], state=ExecutionState.RUNNING,
                                              progress=progress)
        await core.engine.sample_throughput()
    [(remaining, attempts, estimated)] = (await core.repository.acquisition_work([transfer])).values()
    assert attempts == (attempt,) and estimated is True
    assert remaining == pytest.approx(80 * MIB - completed, rel=0.01)    # 68 MiB, from 12 MiB measured
    seconds, held, rough, indeterminate = (await core.engine.acquisition_etas([transfer]))[transfer]
    assert rough is True and held is False and indeterminate is False
    assert seconds == math.ceil(remaining / (2 * MIB))


@pytest.mark.asyncio
async def test_a_usenet_download_has_an_estimate_from_the_services_own_counters(tmp_path, monkeypatch):
    """The real SABnzbd executor over a fake SAB: the queue's ``mb``/``mbleft``
    are the job's own byte counters, so a downloading Usenet transfer has an
    estimate like any other."""
    from test_v113_runtime_telemetry_matrix import SteppedClock, VALID_NZB, converge, mixed as mixed_fixture

    mixed = await mixed_fixture.__wrapped__(tmp_path, monkeypatch)
    transfer = await mixed.engine.submit((TransferRequest("nzb", VALID_NZB, name="posting.nzb"),), name="posting",
                                         deduplicate=False)
    await converge(mixed)
    [job] = mixed.sab.queue.values()
    job.mb = job.mbleft = 1000.0
    clock = mixed.engine.throughput.clock = SteppedClock(mixed.engine.throughput.clock() + 1)
    for _ in range(16):                                                  # 4 MB/s in SAB's megabytes
        await mixed.engine.sample_throughput()
        clock.now += 0.5
        job.mbleft = round(job.mbleft - 2.0, 2)
    await mixed.engine.sample_throughput()
    assert await mixed.engine.acquisition_etas([transfer.id]) == {transfer.id: (math.ceil(968 / 4), False, False, False)}

@pytest.mark.asyncio
@pytest.mark.parametrize("unprovable", ["unknown_size", "open_request", "error_member"])
async def test_work_that_cannot_be_proven_complete_is_indeterminate_while_acquiring(tmp_path, monkeypatch,
                                                                                    unprovable):
    """No partial number: the transfer acquires, but its whole remaining work
    cannot be put into bytes -- an indeterminate estimate, and none at all
    once nothing moves."""
    core = await _core(tmp_path, monkeypatch, files=2, width=1)
    [transfer] = core.transfers
    [attempt] = await _writers(core, transfer)
    [waiting] = [item for item in await core.repository.artifacts(transfer) if not item.execution]
    async with database.get_db() as db:
        if unprovable == "unknown_size":
            await db.execute("UPDATE download_files SET size_bytes=0,size_knowledge='unknown' WHERE id=?",
                             (waiting.id,))
        elif unprovable == "open_request":
            await db.execute("UPDATE transfer_requests SET state='resolving' WHERE id=?", (waiting.request_id,))
        else:
            await db.execute("UPDATE download_files SET status='error' WHERE id=?", (waiting.id,))
        await db.commit()
    _started(core, attempt, 100 * MIB)
    await _acquire(core, 10, {attempt: 100 * MIB}, {attempt: 4 * MIB})
    assert await core.repository.acquisition_work([transfer]) == {transfer: (None, (attempt,), False)}
    assert await core.engine.acquisition_etas([transfer]) == {transfer: INDETERMINATE}
    await _acquire(core, ETA_HOLD_SECONDS + 0.5, {attempt: 100 * MIB}, {attempt: 0})
    assert await core.engine.acquisition_etas([transfer]) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["units", "processing", "estimated_only"])
async def test_a_writer_with_nothing_measured_or_past_acquisition_has_no_estimate(tmp_path, monkeypatch, scope):
    """A writer counting only parts or a percentage moves no byte counter: no
    accountable work and no movement, so no estimate -- not infinity. One past
    acquisition (processing) is no acquiring writer at all."""
    core = await _core(tmp_path, monkeypatch)
    [transfer] = core.transfers
    [attempt] = await _writers(core, transfer)
    job = core.executor.jobs[attempt]
    if scope == "units":
        progress, activity = TransferProgress(0, 0, 0, completed_units=3, total_units=10), job.activity
    elif scope == "processing":
        progress = TransferProgress(100 * MIB, 100 * MIB, 0)
        activity = ExecutionActivity(network_active=False, bandwidth_reservation_required=False,
                                     progress_expected=True)
    else:
        progress, activity = TransferProgress(0, 0, 0, estimated_percentage=40.0), job.activity   # a percentage alone
    core.executor.jobs[attempt] = replace(job, state=ExecutionState.RUNNING, progress=progress, activity=activity)
    await core.engine.reconcile_executions()
    expected = {} if scope == "processing" else {transfer: (None, (attempt,), False)}
    assert await core.repository.acquisition_work([transfer]) == expected
    assert await core.engine.acquisition_etas([transfer]) == {}


@pytest.mark.asyncio
async def test_list_and_live_updates_carry_the_same_estimate_and_clear_it_together(tmp_path, monkeypatch):
    from api import operational_downloads as downloads
    import application.service as service_module

    core = await _core(tmp_path, monkeypatch)
    [transfer] = core.transfers
    [attempt] = await _writers(core, transfer)
    application = ApplicationService(core.engine)
    published = []

    async def publish(kind, payload):
        published.append((kind, payload))
    monkeypatch.setattr(service_module, "publish", publish)

    async def listed():
        page = await downloads.list_operational_torrents(status=None, search=None, limit=0, offset=0,
                                                         application=application)
        return next(item["acquisition_eta"] for item in page["items"] if item["id"] == transfer)

    async def live(seconds, rate):
        before = await core.repository.active()
        await _acquire(core, seconds, {attempt: 200 * MIB}, {attempt: rate})
        await application._publish_active_changes(before, stats=False)
        updates = [item for kind, payload in published if kind == "torrent_updated" for item in payload["items"]
                   if item["id"] == transfer]
        return updates[-1]["acquisition_eta"] if updates else "unpublished"

    _started(core, attempt, 200 * MIB)
    await live(8, 2 * MIB)
    measured = await live(0.5, 2 * MIB)
    assert measured == {"seconds": math.ceil((200 - 17) * MIB / (2 * MIB)), "held": False, "estimated": False, "indeterminate": False}
    assert await listed() == measured                                # one owner, one truth
    published.clear()
    held = await live(1.5, 0)
    assert held == {"seconds": measured["seconds"], "held": True, "estimated": False, "indeterminate": False}   # same figure, marked held
    assert await listed() == held
    published.clear()
    assert await live(3.0, 0) == "unpublished"                       # 4.5 s still: held, nothing changed
    assert await listed() == held
    published.clear()
    assert await live(1.0, 0) is None                                # 5.5 s: past the hold, published as none
    assert await listed() is None


@pytest.mark.asyncio
async def test_list_and_live_updates_agree_on_an_indeterminate_estimate(tmp_path, monkeypatch):
    """A transfer acquiring work of unknown size: both surfaces read the same
    indeterminate estimate while it moves, and both clear it once it stops."""
    from api import operational_downloads as downloads
    import application.service as service_module

    core = await _core(tmp_path, monkeypatch, files=2, width=1)
    [transfer] = core.transfers
    [attempt] = await _writers(core, transfer)
    [waiting] = [item for item in await core.repository.artifacts(transfer) if not item.execution]
    async with database.get_db() as db:
        await db.execute("UPDATE download_files SET size_bytes=0,size_knowledge='unknown' WHERE id=?", (waiting.id,))
        await db.commit()
    application = ApplicationService(core.engine)
    published = []

    async def publish(kind, payload):
        published.append((kind, payload))
    monkeypatch.setattr(service_module, "publish", publish)

    async def listed():
        page = await downloads.list_operational_torrents(status=None, search=None, limit=0, offset=0,
                                                         application=application)
        return next(item["acquisition_eta"] for item in page["items"] if item["id"] == transfer)

    async def live(seconds, rate):
        before = await core.repository.active()
        await _acquire(core, seconds, {attempt: 200 * MIB}, {attempt: rate})
        await application._publish_active_changes(before, stats=False)
        updates = [item for kind, payload in published if kind == "torrent_updated" for item in payload["items"]
                   if item["id"] == transfer]
        return updates[-1]["acquisition_eta"] if updates else "unpublished"

    _started(core, attempt, 200 * MIB)
    infinity = {"seconds": None, "held": False, "estimated": False, "indeterminate": True}
    assert await live(1.0, 2 * MIB) == infinity                     # moving at once, never a figure
    assert await listed() == infinity
    assert await live(8, 2 * MIB) == infinity                       # measured, but the work is not: still infinity
    assert await listed() == infinity
    assert await live(ETA_HOLD_SECONDS + 0.5, 0) is None            # stopped past the hold: none on both
    assert await listed() is None


@pytest.mark.asyncio
async def test_a_paused_transfer_has_no_estimate_anywhere(tmp_path, monkeypatch):
    from api import operational_downloads as downloads

    core = await _core(tmp_path, monkeypatch)
    [transfer] = core.transfers
    [attempt] = await _writers(core, transfer)
    application = ApplicationService(core.engine)
    _started(core, attempt, 200 * MIB)
    await _acquire(core, 8, {attempt: 200 * MIB}, {attempt: 2 * MIB})
    assert await core.engine.acquisition_etas([transfer])
    import application.service as service_module
    published = []

    async def publish(kind, payload):
        published.append((kind, payload))
    monkeypatch.setattr(service_module, "publish", publish)
    await application._publish_active_changes(await core.repository.active(), stats=False)   # an estimate is live
    published.clear()
    before = await core.repository.active()
    await application.pause(transfer)
    await application._publish_active_changes(before, stats=False)
    [update] = [item for kind, payload in published if kind == "torrent_updated" and payload.get("progress_only")
                for item in payload["items"] if item["id"] == transfer][-1:]
    assert update["acquisition_eta"] is None                          # the live path drops it too
    page = await downloads.list_operational_torrents(status=None, search=None, limit=0, offset=0,
                                                     application=application)
    row = next(item for item in page["items"] if item["id"] == transfer)
    assert row["presentation_status"] != "downloading" and row["acquisition_eta"] is None


@pytest.mark.asyncio
async def test_concurrent_publishers_leave_the_memo_equal_to_what_was_last_sent(monkeypatch):
    """The reconcile cycle and the fast observation publish concurrently. An
    older computation that finishes last publishes against the newer memo, so
    the memo always equals what the browser last received."""
    import asyncio
    import application.service as service_module

    transfer = SimpleNamespace(id=7, state="downloading", progress=10.0, active_execution_progress=None,
                               active_execution_basis=None, paused=False)
    release = asyncio.Event()
    answers = iter([({7: (50, False, False, False)}, release), ({7: (40, False, False, False)}, None)])

    class Engine:
        async def acquisition_etas(self, ids):
            etas, gate = next(answers)
            if gate is not None:
                await gate.wait()
            return etas

    class Repository:
        async def active(self):
            return (transfer,)

    application = ApplicationService.__new__(ApplicationService)
    # The browser shows 50 s, the figure last published.
    application.engine, application.repository, application._published_etas = Engine(), Repository(), {7: (50, False, False, False)}
    sent = []

    async def publish(kind, payload):
        sent.extend(item["acquisition_eta"] for item in payload.get("items", ()))
    monkeypatch.setattr(service_module, "publish", publish)
    older = asyncio.create_task(application._publish_active_changes((transfer,), stats=False))
    await asyncio.sleep(0)                                                # computes 50 s: no change yet
    await application._publish_active_changes((transfer,), stats=False)   # newer: 40 s, sent
    release.set()
    await older                                                           # older finishes last
    # Against the memo it found then (50 s) it would send nothing and leave the
    # memo at 50 s while the browser shows 40 s -- a later unchanged 50 s would
    # never correct it. Against what was last sent (40 s) it sends its 50 s.
    assert sent == [{"seconds": 40, "held": False, "estimated": False, "indeterminate": False}, {"seconds": 50, "held": False, "estimated": False, "indeterminate": False}]
    assert application._published_etas == {7: (50, False, False, False)}              # the memo matches the last sent


# ── what the estimate must not touch ──────────────────────────────────────────

def test_external_adapters_carry_no_estimate():
    """Deferred, not done: SABnzbd's ``timeleft`` and qBittorrent's ``eta``
    keep their unknown forms; no adapter reads the new estimate."""
    sab = (ROOT / "backend/api/arr_sabnzbd.py").read_text()
    qbit = (ROOT / "backend/api/arr_qbittorrent.py").read_text()
    external = (ROOT / "backend/external/service.py").read_text()
    for source in (sab, qbit, external):
        assert "acquisition_eta" not in source
    assert '"timeleft": "0:00:00"' in sab
    assert '"eta": UNKNOWN_ETA' in qbit


def test_every_surface_reads_the_one_estimate_owner():
    downloads = (ROOT / "backend/api/operational_downloads.py").read_text()
    service = (ROOT / "backend/application/service.py").read_text()
    app = (ROOT / "frontend/static/app.js").read_text()
    for source in (downloads, service):
        assert "acquisition_etas(" in source and "acquisition_eta_payload(" in source
        assert "acquisition_rate(" not in source and "acquisition_eta(" not in source.replace(
            "acquisition_eta_payload(", "")
    # The browser renders the backend's estimate; it keeps no clock or rate of its own.
    eta_code = app[app.index("const ACQUISITION_ETA_MAX_SECONDS"):app.index("function progress(")]
    for forbidden in ("setInterval", "setTimeout", "Date.now", "performance.now", "/execution/throughput"):
        assert forbidden not in eta_code
    assert f"const ACQUISITION_ETA_MAX_SECONDS = 9999 * 3600 + 59 * 60 + 59;" in app


@pytest.mark.asyncio
@pytest.mark.real_runtime
async def test_three_real_aria2_downloads_of_one_transfer_have_one_estimate(tmp_path, monkeypatch):
    """Transfer 1105's shape on a real aria2 daemon: one transfer of three
    known-size downloads acquiring together, driven the way production drives
    it -- the fast observation every half second, the reconcile cycle each
    second -- has an estimate on the list and the live path, and it falls."""
    import asyncio
    import time
    from dataclasses import replace as _replace

    import application.service as service_module
    from api import operational_downloads as downloads
    from test_v113_continuation_runtime import build, start_origin, stop_daemon

    async def publish(_kind, _payload):
        return None
    monkeypatch.setattr(service_module, "publish", publish)
    server, port, _requests = await start_origin(rate=MIB // 5)          # 24 MiB each, ~30 s
    ctx = await build(tmp_path, monkeypatch, checkpoint=5.0)
    ctx.engine.policy = _replace(ctx.engine.policy, max_active_executions=3)
    application = ApplicationService(ctx.engine)
    try:
        transfer = await ctx.engine.submit(tuple(
            TransferRequest("http", f"http://127.0.0.1:{port}/part{index}.bin", name=f"part{index}.bin")
            for index in range(3)), name="three", deduplicate=False)
        seen, started, cycled = [], time.monotonic(), 0.0
        while time.monotonic() - started < 15 and len(seen) < 6:
            if time.monotonic() - cycled >= 1.0:
                await ctx.engine.tick()
                cycled = time.monotonic()
            await application.observe_live_executions()
            await asyncio.sleep(0.5)
            states = [str(getattr(item.state, "value", item.state)) for item in await ctx.repository.artifacts(transfer.id)]
            page = await downloads.list_operational_torrents(status=None, search=None, limit=0, offset=0,
                                                             application=application)
            eta = next(item["acquisition_eta"] for item in page["items"] if item["id"] == transfer.id)
            # "ETA ∞" while a download is moving but not yet measured is
            # correct; the steady figures are the ones that follow it.
            if states == ["downloading"] * 3 and eta is not None and not eta["indeterminate"]:
                seen.append(eta)
        assert len(seen) == 6, f"no steady estimate while all three downloaded: {seen}"
        assert all(0 < item["seconds"] <= 60 for item in seen)               # ~30 s of work at the origin's pace
        assert seen[-1]["seconds"] < seen[0]["seconds"]                      # it falls as bytes arrive
        assert seen[-1]["estimated"] is False                                # every writer measured: exact
    finally:
        await stop_daemon(ctx.proc, ctx.service)
        server.close()
