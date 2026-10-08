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
"""
from __future__ import annotations

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


class ExecutionThroughputMeter:
    """The one canonical download-throughput owner: per execution and aggregate."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS, window_seconds: float = WINDOW_SECONDS):
        self.clock = clock
        self._max_age = max(0.0, float(max_age_seconds))
        self._window = float(window_seconds)
        self._series: dict[str, deque] = {}
        self._sampled_at: float | None = None

    def record(self, samples: Mapping[str, tuple[float, int | None]]) -> None:
        """One complete observation pass of the live executions.

        ``samples`` maps an execution's attempt identity to ``(observed_at,
        completed_bytes)``, ``completed_bytes`` being ``None`` while it is not
        acquiring. An execution this pass does not contain is retired unless a
        newer pass already sampled it."""
        oldest = min((at for at, _value in samples.values()), default=self.clock())
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
            series.append((at, value))
            while len(series) > 2 and series[1][0] <= at - self._window:
                series.popleft()                           # keep one sample at or before the window
        self._sampled_at = self.clock()

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
