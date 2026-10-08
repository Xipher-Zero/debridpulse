"""DP 1.0.13: ONE download-throughput fact, owned by core, consumed
identically by the topbar and the browser tab.

Unified speed presentation: a rate is never read from an executor. Every
acquiring execution's cumulative acquired-byte counter is sampled with the time
it was observed, and the meter presents byte deltas over the actual elapsed time
of a trailing four-second window -- per execution and, summed once per
execution, in aggregate. A coarse counter (SAB's queue reports megabytes with
limited precision) is smoothed by the same window, never by any executor's own
figure, so no executor-level rate seam remains.
"""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from transfers.models import (
    ExecutionActivity, ExecutionHandle, ExecutionObservation, ExecutionState, ExecutorCapabilities, TransferProgress,
)
from transfers.runtime_telemetry import WINDOW_SECONDS, ExecutionThroughputMeter

BACKEND = Path(__file__).resolve().parents[1]
STATIC = BACKEND.parent / "frontend" / "static"
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")

CORE = (BACKEND / "transfers",)
MIB = 1024 * 1024


# --- the neutral contract ---------------------------------------------------

def test_no_executor_level_rate_seam_remains():
    from transfers import contracts, models
    from transfers.registry import _EXECUTOR_CAPABILITIES
    assert not hasattr(models, "ExecutorThroughput")
    assert not hasattr(contracts, "ExecutorAggregateThroughput")
    assert "aggregate_throughput" not in _EXECUTOR_CAPABILITIES
    assert not hasattr(ExecutorCapabilities(), "aggregate_throughput")


def test_the_neutral_seam_carries_no_integration_vocabulary():
    banned = re.compile(r"\bsabnzbd\b|\bsab\b|\busenet\b|\bnzb\b|\bnntp\b", re.I)
    for name in ("models.py", "contracts.py", "registry.py", "runtime_telemetry.py"):
        source = (BACKEND / "transfers" / name).read_text(encoding="utf-8")
        assert not banned.search(source), name


# --- the core-owned meter: byte deltas over the trailing window -------------

class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


def _meter(clock, **kwargs):
    return ExecutionThroughputMeter(clock=clock, **kwargs)


def _feed(meter, clock, counters, *, step=0.5):
    """One complete pass per entry of ``counters`` (``{attempt: bytes|None}``),
    ``step`` seconds apart; returns the aggregate after every pass."""
    seen = []
    for counter in counters:
        clock.now += step
        meter.record({key: (clock.now, value) for key, value in counter.items()})
        seen.append(meter.current())
    return seen


def test_an_idle_meter_reports_zero():
    assert _meter(Clock()).current() == 0


def test_one_sample_is_no_elapsed_time_and_shows_no_rate():
    clock = Clock()
    meter = _meter(clock)
    _feed(meter, clock, [{"a": 10 * MIB}])
    assert meter.current() == 0 and meter.rate("a") == 0


def test_a_partial_window_shows_speed_at_the_next_sample():
    clock = Clock()
    meter = _meter(clock)
    seen = _feed(meter, clock, [{"a": 0}, {"a": MIB // 2}])
    assert seen == [0, MIB]                                # 0.5 MiB in 0.5 s, before any full window


def test_a_steady_counter_is_its_true_rate_at_every_update():
    clock = Clock()
    meter = _meter(clock)
    seen = _feed(meter, clock, [{"a": index * MIB} for index in range(20)])
    assert all(value == 2 * MIB for value in seen[1:])     # 1 MiB per 0.5 s


def test_a_brief_chunk_gap_keeps_the_trailing_rate():
    clock = Clock()
    meter = _meter(clock)
    burst = [{"a": index * MIB} for index in range(9)]     # 4 s at 2 MiB/s
    gap = [{"a": 8 * MIB}, {"a": 8 * MIB}]                 # one second without bytes
    seen = _feed(meter, clock, burst + gap + [{"a": 9 * MIB}])
    assert seen[8] == 2 * MIB
    # The window's start keeps sliding over the burst: 7 then 6 MiB in 4 s.
    assert seen[9] == int(7 * MIB / WINDOW_SECONDS) and seen[10] == int(6 * MIB / WINDOW_SECONDS)
    assert min(seen[1:]) > 0                               # never collapses to zero between chunks


def test_sustained_idle_decays_to_zero_at_the_window():
    clock = Clock()
    meter = _meter(clock)
    seen = _feed(meter, clock, [{"a": index * MIB} for index in range(9)] + [{"a": 8 * MIB}] * 9)
    idle = seen[9:]
    assert all(later < earlier for earlier, later in zip(idle, idle[1:-1]))   # decays with elapsed time
    assert idle[6] > 0 and idle[7] == 0                    # gone 4 s after the last byte, not held


def test_unobserved_elapsed_time_decays_the_rate_too():
    clock = Clock()
    meter = _meter(clock)
    _feed(meter, clock, [{"a": index * MIB} for index in range(9)])
    assert meter.current() == 2 * MIB
    clock.now += 3.0
    assert 0 < meter.current() < 2 * MIB                   # no new sample is not a held rate
    clock.now += 2.5
    assert meter.current() == 0


def test_a_counter_reset_starts_a_new_segment_without_a_spike():
    clock = Clock()
    meter = _meter(clock)
    seen = _feed(meter, clock, [{"a": index * MIB} for index in range(9)] + [{"a": 0}, {"a": MIB // 2}])
    assert seen[9] == 0                                    # a restart is a discontinuity, never negative
    assert seen[10] == MIB                                 # and measured afresh


@pytest.mark.parametrize("stop", ["paused", "finished", "gone"])
def test_a_discontinuity_shows_no_stale_rate(stop):
    clock = Clock()
    meter = _meter(clock)
    _feed(meter, clock, [{"a": index * MIB} for index in range(9)])
    final = {"paused": {"a": None}, "finished": {"a": None}, "gone": {}}[stop]
    seen = _feed(meter, clock, [final])
    assert seen == [0] and meter.rate("a") == 0


def test_resume_is_measured_from_its_own_samples():
    clock = Clock()
    meter = _meter(clock)
    seen = _feed(meter, clock, [{"a": 0}, {"a": 4 * MIB}, {"a": None}, {"a": 4 * MIB}, {"a": 4 * MIB + MIB // 4}])
    assert seen == [0, 8 * MIB, 0, 0, MIB // 2]           # no rate spans the pause


def test_a_failover_to_a_new_attempt_is_a_new_identity():
    clock = Clock()
    meter = _meter(clock)
    _feed(meter, clock, [{"old": index * MIB} for index in range(9)])
    seen = _feed(meter, clock, [{"new": 0}, {"new": MIB}])
    assert seen == [0, 2 * MIB] and meter.rate("old") == 0


def test_the_aggregate_counts_each_execution_once():
    clock = Clock()
    meter = _meter(clock)
    seen = _feed(meter, clock, [{"a": index * MIB, "b": index * MIB // 2} for index in range(9)])
    assert seen[-1] == 3 * MIB == meter.rate("a") + meter.rate("b")


def test_a_late_pass_is_ignored_rather_than_read_as_a_rollback():
    clock = Clock()
    meter = _meter(clock)
    _feed(meter, clock, [{"a": index * MIB} for index in range(9)])
    before = meter.current()
    meter.record({"a": (clock.now - 1.0, 6 * MIB)})        # an older observation, recorded late
    assert meter.current() == before
    meter.record({"a": (clock.now - 1.0, None)})           # a late "stopped" cannot retire newer truth
    assert meter.current() == before
    meter.record({})                                       # a pass that started before it, also late
    assert meter.current() == before


def test_coarse_quantized_counters_are_stabilized_by_the_window():
    """SAB's queue counter moves in whole articles at irregular intervals."""
    clock = Clock()
    meter = _meter(clock)
    article = 768 * 1024
    counters, total, seen = [], 0, []
    for index in range(40):                                # ~1.5 MiB/s in article-sized steps
        total += article * (index % 2 + (1 if index % 5 == 0 else 0))
        counters.append({"a": total})
    seen = _feed(meter, clock, counters)
    steady = seen[9:]
    assert max(steady) - min(steady) < 0.25 * max(steady)  # the 0.5 s deltas swing 0..2 articles
    assert all(value > 0 for value in steady)


def test_history_is_bounded():
    clock = Clock()
    meter = _meter(clock)
    _feed(meter, clock, [{"a": index} for index in range(500)], step=0.01)
    assert len(meter._series["a"]) <= 64


def test_the_meter_reports_zero_once_observation_itself_stops():
    clock = Clock()
    meter = _meter(clock, max_age_seconds=5.0)
    _feed(meter, clock, [{"a": index * MIB} for index in range(9)])
    clock.now += 6.0
    assert meter.current() == 0


# --- the engine feeds counters, never an executor's rate --------------------

def _observation(state=ExecutionState.RUNNING, completed=10, rate=999_999, network_active=True, error=None):
    return ExecutionObservation(ExecutionHandle("x", "a", "c"), state, TransferProgress(100, completed, rate),
                                error=error, activity=ExecutionActivity(network_active=network_active))


def test_only_an_acquiring_execution_contributes_its_counter():
    from transfers._engine_base import TransferEngine
    assert TransferEngine._acquired_bytes(_observation()) == 10
    for observation in (_observation(ExecutionState.PAUSED), _observation(ExecutionState.QUEUED),
                        _observation(ExecutionState.SUCCEEDED), _observation(ExecutionState.UNKNOWN),
                        _observation(network_active=False)):                 # e.g. post-processing
        assert TransferEngine._acquired_bytes(observation) is None


def test_the_engine_never_reads_an_executor_reported_rate():
    source = (BACKEND / "transfers" / "_engine_base.py").read_text(encoding="utf-8")
    body = source[source.index("def _acquired_bytes"):source.index("async def _release_runtime_reservations")]
    assert "bytes_per_second" not in body and "completed_bytes" in body


# --- the neutral presentation route -----------------------------------------

@pytest.mark.asyncio
async def test_a_neutral_application_owned_runtime_status_route_exists():
    from api import routes

    application = SimpleNamespace(execution_runtime_status=lambda: _status())
    result = await routes.get_execution_runtime_status(application=application)
    assert result["ok"] is True
    assert result["download_bytes_per_second"] == 4096
    assert result["active_execution_slots"] == 2
    assert result["max_download_bytes_per_second"] == 1048576


async def _status():
    return {"download_bytes_per_second": 4096, "active_execution_slots": 2,
            "max_download_bytes_per_second": 1048576}


@pytest.mark.asyncio
async def test_the_application_projects_each_value_from_its_one_existing_owner():
    from application.service import ApplicationService

    engine = SimpleNamespace(
        throughput=SimpleNamespace(current=lambda: 8192),
        policy=SimpleNamespace(max_active_executions=4, resolution_concurrency=2),
        runtime=SimpleNamespace(configured=2097152),
        clock=lambda: 1000.0,
        repository=SimpleNamespace(occupied_execution_slots=_slots),
    )
    service = ApplicationService(engine)
    status = await service.execution_runtime_status()
    assert status == {"download_bytes_per_second": 8192, "active_execution_slots": 3,
                      "max_download_bytes_per_second": 2097152}


async def _slots(now, **kwargs):
    assert now == 1000.0
    return 3


# --- the browser consumes ONE neutral fact ----------------------------------

def test_generic_presentation_no_longer_polls_an_executor_specific_route():
    assert "/aria2/global-stat" not in APP_JS
    assert "/aria2/global-options" not in APP_JS
    assert "/execution/runtime-status" in APP_JS


def test_no_generic_presentation_state_owner_is_named_after_an_executor():
    assert "_aria2BadgeState" not in APP_JS
    assert "updateAria2TopbarBadge" not in APP_JS
    assert "loadAria2TopbarStat" not in APP_JS
    for identifier in ("aria2-speed-badge", "aria2-badge-active", "aria2-badge-speed",
                       "aria2-badge-max", "aria2-badge-limit", "aria2-cap-menu", "aria2-cap-toggle"):
        assert identifier not in INDEX, identifier
        assert identifier not in APP_JS, identifier


def test_the_topbar_and_the_browser_tab_read_the_same_value_from_the_same_owner():
    title = APP_JS[APP_JS.index("function renderOperatorTitle("):]
    title = title[:title.index("\n}") + 2]
    assert "_runtimeStatusState" in title
    badge = APP_JS[APP_JS.index("function updateRuntimeStatusBadge("):]
    badge = badge[:badge.index("\n}") + 2]
    assert "_runtimeStatusState" in badge
    # Exactly one writer of the shared state.
    assert APP_JS.count("Object.assign(_runtimeStatusState") == 1


def test_the_speed_cap_display_reads_the_neutral_runtime_limit_owner():
    # The cap is shown with the speed, from the one volatile neutral read.
    loader = APP_JS[APP_JS.index("async function loadRuntimeSpeed("):]
    loader = loader[:loader.index("\n}") + 2]
    assert "'/execution/throughput'" in loader and "max_download_bytes_per_second" in loader
    # Writes already went to the neutral surface and still do.
    assert "'/execution/runtime-limits'" in APP_JS


def test_executor_diagnostics_are_not_deleted_merely_because_presentation_moved():
    from api import routes
    paths = {route.path for route in routes.router.routes}
    assert "/aria2/global-stat" in paths
    assert "/aria2/global-options" in paths
    assert "/aria2/runtime" in paths
