"""Core-owned executor runtime telemetry: the ONE download-throughput fact.

Separate from ``runtime_coordination`` on purpose: that owner allocates the
global bandwidth CEILING across executors, this one reports what is currently
being acquired. Mixing a limit owner with a measurement owner would make either
one harder to reason about, and neither needs the other's state.

Measurement rule: a rate is never read from an executor. Every acquiring
execution's cumulative acquired-byte counter (``TransferProgress
.completed_bytes``) is sampled with the time it was observed, and a rate is
the counter's byte delta over the actual elapsed time of the trailing
``WINDOW_SECONDS`` -- never an average of rates. One calculation serves every
executor (a coarse counter, such as one reported in megabytes, is smoothed by
the same window rather than by any executor's own figure), every execution's
row and the aggregate, which is the sum of the executions' rates: each
execution is counted once, by its attempt identity, so nothing can be counted
twice.

History is per execution segment, bounded and in memory only. An execution
that is not acquiring (paused, post-processing, terminal, unknown), whose
counter moves backwards (a restart), or that a complete observation pass no
longer contains, starts over: no stale rate survives a discontinuity, and a new
attempt is a new identity. A sample older than the newest one already held for
its execution is late and ignored, so out-of-order passes cannot fabricate a
spike. ``max_age_seconds`` guards the case where observation itself stops:
past it the meter reports nothing rather than a frozen figure. For ordinary
operator presentation, "nothing currently measurable" is ``0``.

Sustained throughput is the same counting rule over a longer span: every
counted byte delta also accrues to one aggregate acquired-byte total, and the
sustained rate is that total's growth over the trailing
``SUSTAINED_WINDOW_SECONDS`` divided by the window -- time-weighted by
construction. It is reported only for a window observed throughout (no gap of
more than ``_SUSTAINED_GAP_SECONDS`` between passes: missing observation is not
evidence of zero) and not dominated by an isolated burst (no one of its
``_SUSTAINED_BUCKETS`` equal parts holding more than half of its bytes).
``sustained_peak`` is the highest such rate seen -- seeded from, and raised
into, its durable record by the engine -- and it never falls.

A logical transfer's estimated time remaining is that transfer's remaining
acquisition bytes over the summed acquisition rate of its acquiring executions
that have one (``acquisition_eta``), each read from this same history
(``acquisition_rate``): no second sampler. The rate is the counter's growth
over up to ``ETA_WINDOW_SECONDS`` ending at its last movement, measured only
over the current run of movement and only once that run spans
``ETA_MIN_SPAN_SECONDS``. Anchored at the last movement, a counter that stops
for less than ``ETA_HOLD_SECONDS`` keeps the estimate exactly as it was --
held, never counted down -- and one that stays still longer has none; after
such a stall the next run is measured afresh, so a stale rate never returns.
"""
from __future__ import annotations

import math
import time
from collections import deque
from typing import Callable, Mapping

# Generous against the ~1 s reconcile cadence: this bounds STOPPED observation,
# not an idle one (an idle execution's flat counter already decays to 0).
DEFAULT_MAX_AGE_SECONDS = 15.0
# Presentation cadence of the between-cycle sample of the same rule
# (``TransferEngine.sample_throughput``): the operator-facing speed follows the
# executors at this pace instead of the repository-backed reconcile cycle's.
THROUGHPUT_SAMPLE_SECONDS = 0.5
# The trailing window every presented rate is measured over.
WINDOW_SECONDS = 4.0
# How long the newest sample still ends the window: the gap to the next
# expected sample is not evidence that bytes stopped. Past it the window ends
# at the present and an unobserved execution decays like an idle one.
_SAMPLE_GRACE_SECONDS = 2 * THROUGHPUT_SAMPLE_SECONDS
# Bounds one execution's history whatever the observation frequency; dropping
# the oldest sample only shortens the measured span, never falsifies it.
_MAX_SAMPLES = 64
# The span a sustained rate is measured over.
SUSTAINED_WINDOW_SECONDS = 30.0
# A pass gap longer than this restarts sustained coverage.
_SUSTAINED_GAP_SECONDS = 5.0
# A sustained window is refused when one of these equal parts holds more than
# half its bytes: an isolated burst, not a sustained rate.
_SUSTAINED_BUCKETS = 6
# The time-remaining estimate (``acquisition_rate``), from the windows above:
# measured over at most the sustained window -- in practice the span the
# ``_MAX_SAMPLES`` most recent passes cover (about 20 s with the fast
# observation and the reconcile cycle both recording) -- so a bursty or coarse
# counter moves it gently; first shown once its run of movement spans the presentation
# window; held while the counter is still for no longer than the pass gap that
# already ends sustained coverage, and gone after it.
ETA_WINDOW_SECONDS = SUSTAINED_WINDOW_SECONDS
ETA_MIN_SPAN_SECONDS = WINDOW_SECONDS
ETA_HOLD_SECONDS = _SUSTAINED_GAP_SECONDS
# The largest estimate presented, 9999:59:59 -- a prediction past ~416 days
# says nothing an operator can use, and ten characters stay one fixed width.
MAX_ETA_SECONDS = 9999 * 3600 + 59 * 60 + 59


class ExecutionThroughputMeter:
    """The one canonical download-throughput owner: per execution and aggregate."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS, window_seconds: float = WINDOW_SECONDS):
        self.clock = clock
        self._max_age = max(0.0, float(max_age_seconds))
        self._window = float(window_seconds)
        # History spans the longest window read from it (the estimate's),
        # still bounded by ``_MAX_SAMPLES``.
        self._retention = max(self._window, ETA_WINDOW_SECONDS)
        self._series: dict[str, deque] = {}
        self._sampled_at: float | None = None
        self._acquired = 0                                 # aggregate counted bytes since construction
        self._totals: deque = deque()                      # (pass time, _acquired) over the sustained window
        self._covered_since: float | None = None
        self.sustained_peak: int | None = None

    def record(self, samples: Mapping[str, tuple[float, int | None]]) -> None:
        """One complete observation pass of the live executions.

        ``samples`` maps an execution's attempt identity to ``(observed_at,
        completed_bytes)``, ``completed_bytes`` being ``None`` while it is not
        acquiring. An execution this pass does not contain is retired unless a
        newer pass already sampled it."""
        passed = self.clock()
        if self._sampled_at is None or passed - self._sampled_at > _SUSTAINED_GAP_SECONDS:
            self._totals.clear()
            self._covered_since = passed
        oldest = min((at for at, _value in samples.values()), default=passed)
        for key in [key for key, series in self._series.items()
                    if key not in samples and series[-1][0] < oldest]:
            del self._series[key]
        for key, (at, value) in samples.items():
            series = self._series.get(key)
            if series and at <= series[-1][0]:
                continue                                   # late: a newer sample already holds
            if value is None:
                self._series.pop(key, None)
                continue
            value = max(0, int(value))
            if not series or value < series[-1][1]:
                self._series[key] = series = deque(maxlen=_MAX_SAMPLES)
            else:
                self._acquired += value - series[-1][1]
            series.append((at, value))
            while len(series) > 2 and series[1][0] <= at - self._retention:
                series.popleft()                           # keep one sample at or before the window
        self._sampled_at = passed
        self._totals.append((passed, self._acquired))
        while len(self._totals) > 2 and self._totals[1][0] <= passed - SUSTAINED_WINDOW_SECONDS:
            self._totals.popleft()                         # keep one total at or before the window

    def rate(self, key: str) -> int:
        """One execution's bytes/second over the trailing window, or 0."""
        if not self._fresh():
            return 0
        return int(self._rate(self._series.get(key), self.clock()))

    def current(self) -> int:
        """Aggregate bytes/second across every execution, or 0."""
        if not self._fresh():
            return 0
        now = self.clock()
        return int(sum(self._rate(series, now) for series in self._series.values()))

    def sustained(self) -> float | None:
        """Aggregate bytes/second over the trailing sustained window, or
        ``None`` when that window was not observed throughout or is one burst."""
        if not self._totals or self._covered_since is None:
            return None
        end = self._totals[-1][0]
        start = end - SUSTAINED_WINDOW_SECONDS
        if self._covered_since > start:
            return None
        edges = [self._bytes_at(self._totals, start + SUSTAINED_WINDOW_SECONDS * i / _SUSTAINED_BUCKETS)
                 for i in range(_SUSTAINED_BUCKETS + 1)]
        total = edges[-1] - edges[0]
        if total > 0 and max(b - a for a, b in zip(edges, edges[1:])) > total / 2:
            return None
        return max(0.0, total) / SUSTAINED_WINDOW_SECONDS

    def acquisition_rate(self, key: str) -> tuple[float, bool] | None:
        """One execution's rate for its time-remaining estimate, ``(bytes per
        second, held)``, or ``None`` when there is no credible one: observation
        stopped, the counter has not moved for ``ETA_HOLD_SECONDS``, or its
        current run of movement is shorter than ``ETA_MIN_SPAN_SECONDS``.

        Measured up to its last movement, so while the counter is still the
        rate -- and the estimate -- stay exactly what they were; ``held`` says
        so once the stillness outlasts the gap the presentation rate already
        tolerates (``_SAMPLE_GRACE_SECONDS``). The run starts where movement
        resumed after a stall longer than the hold, so the stall never dilutes
        it and a rate from before it never returns."""
        series = self._series.get(key)
        if not self._fresh() or not series or len(series) < 2:
            return None
        now = self.clock()
        run_start = moved_at = moved = None
        for before, sample in zip(series, list(series)[1:]):
            if sample[1] > before[1]:
                if moved_at is None or sample[0] - moved_at > ETA_HOLD_SECONDS:
                    run_start = before[0]
                moved_at, moved = sample[0], sample[1]
        if moved_at is None or now - moved_at > ETA_HOLD_SECONDS:
            return None
        start = max(run_start, moved_at - ETA_WINDOW_SECONDS)
        if moved_at - start < ETA_MIN_SPAN_SECONDS:
            return None
        rate = (moved - self._bytes_at(series, start)) / (moved_at - start)
        return (rate, now - moved_at > _SAMPLE_GRACE_SECONDS) if rate > 0 else None

    def acquisition_stalled(self, key: str) -> bool:
        """Whether an execution that moved has since been still for longer
        than ``ETA_HOLD_SECONDS``: sustained inactivity, as opposed to one that
        has not been measured yet (just started, never moved)."""
        series = self._series.get(key)
        if not series:
            return False
        moved_at = None
        for before, sample in zip(series, list(series)[1:]):
            if sample[1] > before[1]:
                moved_at = sample[0]
        return moved_at is not None and self.clock() - moved_at > ETA_HOLD_SECONDS

    def raise_sustained_peak(self) -> int | None:
        """Raise ``sustained_peak`` to the current sustained rate when that is
        higher; the new peak, or ``None`` when it did not rise."""
        rate = self.sustained()
        # To the nearest byte/second: the window's interpolated edges carry
        # floating-point error, and truncating made an exact rate one less.
        peak = round(rate) if rate else 0
        if not peak or peak <= (self.sustained_peak or 0):
            return None
        self.sustained_peak = int(peak)
        return self.sustained_peak

    def _fresh(self) -> bool:
        if self._sampled_at is None:
            return False
        return not (self._max_age and (self.clock() - self._sampled_at) > self._max_age)

    def _rate(self, series, now: float) -> float:
        if not series or len(series) < 2:
            return 0.0                                     # one sample is no elapsed time
        last_at, last = series[-1]
        end = max(last_at, now - _SAMPLE_GRACE_SECONDS)
        # A partial window (a new segment) is measured over what it has.
        start = max(end - self._window, series[0][0])
        if end <= start or start >= last_at:
            return 0.0
        return (last - self._bytes_at(series, start)) / (end - start)

    @staticmethod
    def _bytes_at(series, at: float) -> float:
        """The counter at ``at``, linear between the samples around it."""
        before = series[0]
        for sample in series:
            if sample[0] >= at:
                if sample[0] == before[0]:
                    return float(sample[1])
                share = (at - before[0]) / (sample[0] - before[0])
                return before[1] + (sample[1] - before[1]) * share
            before = sample
        return float(before[1])


def acquisition_eta(remaining_bytes: int | None, attempt_ids, meter: ExecutionThroughputMeter, *,
                    estimated: bool = False) -> tuple[int, bool, bool] | None:
    """A logical transfer's estimated time remaining, ``(whole seconds, held,
    estimated)``, or ``None``: its remaining acquisition bytes over the summed
    ``acquisition_rate`` of the executions acquiring them that have one, each
    counted once.

    Every writer's remaining work stays in the numerator. A writer not measured
    yet (just started: no run of movement as long as ``ETA_MIN_SPAN_SECONDS``)
    adds none to the denominator -- no rate is ever invented for it -- so the
    figure assumes the measured writers' pace for its work too, and is
    ``estimated``. A writer that moved and has been still past the hold is
    sustained inactivity (``acquisition_stalled``): there is no estimate, as
    for a single writer. ``None`` also when no writer has a credible rate, the
    remaining work is not positive, or the estimate does not fit
    ``MAX_ETA_SECONDS``. Rounded up, so unfinished work never reads as no
    time left. ``estimated`` is also set by the caller when the remaining work
    is itself extrapolated. Held only while every writer is measured and held:
    only then is the figure exactly the one last shown."""
    attempts = sorted(set(attempt_ids))
    if remaining_bytes is None or remaining_bytes <= 0 or not attempts:
        return None
    if any(meter.acquisition_stalled(attempt) for attempt in attempts):
        return None
    rates = [meter.acquisition_rate(attempt) for attempt in attempts]
    measured = [rate for rate in rates if rate is not None]
    if not measured:
        return None
    total = sum(rate for rate, _held in measured)
    seconds = remaining_bytes / total if total > 0 else math.inf
    if not math.isfinite(seconds) or seconds > MAX_ETA_SECONDS:
        return None
    seconds = max(1, math.ceil(seconds))
    if seconds > MAX_ETA_SECONDS:
        return None
    complete = len(measured) == len(rates)
    return seconds, complete and all(held for _rate, held in measured), estimated or not complete


def acquisition_eta_payload(eta: tuple[int, bool, bool] | None) -> dict | None:
    """The one public form of an estimate, for the list and its live
    progress updates alike: ``{"seconds", "held", "estimated"}``, or ``None``
    when the transfer has no credible estimate."""
    return None if eta is None else {"seconds": int(eta[0]), "held": bool(eta[1]), "estimated": bool(eta[2])}
