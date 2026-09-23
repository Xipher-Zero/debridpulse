"""Core-owned executor runtime telemetry: the ONE aggregate download-throughput fact.

Separate from ``runtime_coordination`` on purpose: that owner allocates the
global bandwidth CEILING across executors, this one reports what is currently
being acquired. Mixing a limit owner with a measurement owner would make either
one harder to reason about, and neither needs the other's state.

Counting rule (the reason double counting is structurally impossible):

    an executor that declares ``aggregate_throughput`` contributes exactly its
    own single measured value, counted once whatever its job count, and no
    per-execution rate from it is ever added;

    every other executor contributes the sum of the ``bytes_per_second`` its
    currently network-active executions report through ``TransferProgress``.

Each executor therefore reaches the aggregate through exactly one path.

Freshness: the meter is REBUILT from scratch at the end of every execution
reconcile cycle, so a paused, finished, unreachable or absent executor stops
contributing in the very next cycle rather than leaving a stale rate behind.
``max_age_seconds`` is the second guard, for the case where the cycle itself
stops running: past it the meter reports nothing rather than a frozen figure.
For ordinary operator presentation, "nothing currently measurable" is ``0``.
"""
from __future__ import annotations

import time
from typing import Callable, Mapping

# Generous against the ~1 s reconcile cadence: this bounds a STOPPED cycle, not
# an idle one (an idle cycle records zero contributions, which is already 0).
DEFAULT_MAX_AGE_SECONDS = 15.0


class ExecutionThroughputMeter:
    """The one canonical current aggregate download-throughput owner."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS):
        self._clock = clock
        self._max_age = max(0.0, float(max_age_seconds))
        self._contributions: dict[str, int] = {}
        self._sampled_at: float | None = None

    def record(self, contributions: Mapping[str, int]) -> None:
        """Replace the whole sample with one reconcile cycle's contributions."""
        self._contributions = {
            str(identity): max(0, int(value or 0))
            for identity, value in (contributions or {}).items()
        }
        self._sampled_at = self._clock()

    def current(self) -> int:
        """Current aggregate bytes/second, or 0 when nothing is measurable."""
        if self._sampled_at is None:
            return 0
        if self._max_age and (self._clock() - self._sampled_at) > self._max_age:
            return 0
        return sum(self._contributions.values())

    def per_executor(self) -> dict[str, int]:
        """The last sample's contributions, for diagnostics only."""
        return dict(self._contributions)
