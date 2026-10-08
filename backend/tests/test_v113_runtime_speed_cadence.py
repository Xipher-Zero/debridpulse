"""1.0.13: the operator-facing speed follows the one core throughput fact at
presentation cadence, never at the reconcile cycle's cadence.

Transfer 462 characterization (real container, unified multi-source load):
``/execution/runtime-status`` answered in 3.6 ms p50 (23 ms max) and the
occupancy query took 0.06 ms, yet the speed changed only every 2.1 s -- exactly
once per execution reconcile cycle (the 2 s policy interval plus ~150 ms of
cycle work). The meter was rebuilt only at the end of each whole
repository-backed cycle, so the volatile fact was hostage to it.

The one core owner now also samples that same rule between cycles,
from the live writers alone (bounded by the execution width), without waiting
for the cycle; the same observation persists only factual nonterminal
activity, guarded. The presentation read of it is pure memory and never waits
for the slower repository-backed occupancy fact.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from executor_fakes import LedgerExecutor, LedgerProvider
from transfers.convergence_engine import TransferEngine
from transfers.models import ExecutionState, TransferRequest
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

MIB = 1024 * 1024


@pytest_asyncio.fixture
async def acquiring(tmp_path, monkeypatch):
    """One engine with one per-execution-rate executor whose job is acquiring."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    executor = LedgerExecutor(repository.authorize_execution)
    registry.register_provider(LedgerProvider())
    registry.register_executor(executor)
    now = [1000.0]
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"), clock=lambda: now[0],
                            policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0,
                                                  max_active_executions=4))
    await engine.initialize()
    await engine.submit((TransferRequest("ledger", "movie", name="movie"),), name="movie", deduplicate=False)
    for _ in range(6):
        await engine.tick()
    assert executor.jobs, "the fixture job never started"
    return SimpleNamespace(engine=engine, repository=repository, executor=executor)


class SteppedClock:
    def __init__(self, start):
        self.now = float(start)

    def __call__(self):
        return self.now


async def _moving(acquiring, bytes_per_second, observe, seconds=4.0):
    """The job acquires at ``bytes_per_second`` -- counter evidence, one
    observation pass every half second -- for a whole trailing window."""
    meter = acquiring.engine.throughput
    if not isinstance(meter.clock, SteppedClock):
        meter.clock = SteppedClock(meter.clock() + 1)
    for _ in range(int(seconds / 0.5)):
        for job in acquiring.executor.jobs.values():
            job.state = ExecutionState.RUNNING
            job.progress = job.progress.__class__(64 * MIB, job.progress.completed_bytes + bytes_per_second // 2, 1)
        meter.clock.now += 0.5
        await observe()


@pytest.mark.asyncio
async def test_the_throughput_fact_follows_the_executor_between_reconcile_cycles(acquiring):
    await _moving(acquiring, 2 * MIB, acquiring.engine.reconcile_executions)
    assert acquiring.engine.throughput.current() == 2 * MIB
    # The executor's pace changes; no reconcile cycle runs.
    await _moving(acquiring, 7 * MIB, acquiring.engine.sample_throughput)
    assert acquiring.engine.throughput.current() == 7 * MIB, "speed stayed hostage to the reconcile cycle"


@pytest.mark.asyncio
async def test_sampling_reads_only_the_live_writers_never_the_decomposition(acquiring, monkeypatch):
    await _moving(acquiring, 1 * MIB, acquiring.engine.reconcile_executions)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("throughput sampling walked transfers or artifacts")
    for name in ("active", "artifacts", "occupied_execution_slots", "executions"):
        monkeypatch.setattr(acquiring.repository, name, forbidden)
    await _moving(acquiring, 3 * MIB, acquiring.engine.sample_throughput)
    assert acquiring.engine.throughput.current() == 3 * MIB


@pytest.mark.asyncio
async def test_sampling_never_revives_an_executor_the_cycle_found_idle(acquiring):
    for job in acquiring.executor.jobs.values():
        job.activity = job.activity.__class__(network_active=False, bandwidth_reservation_required=True,
                                              progress_expected=False)
    await acquiring.engine.reconcile_executions()
    assert acquiring.engine.throughput.current() == 0
    await acquiring.engine.sample_throughput()
    assert acquiring.engine.throughput.current() == 0


@pytest.mark.asyncio
async def test_sampling_never_waits_for_the_reconcile_cycle(acquiring):
    await _moving(acquiring, 1 * MIB, acquiring.engine.reconcile_executions)
    async with acquiring.engine._execution_cycle_lock:               # a long cycle is running
        await asyncio.wait_for(_moving(acquiring, 5 * MIB, acquiring.engine.sample_throughput), 1)
        assert acquiring.engine.throughput.current() == 5 * MIB


# ── RED 1A: the speed read never waits for the slower occupancy fact ─────────

@pytest.mark.asyncio
async def test_the_speed_projection_is_delivered_while_occupancy_is_blocked():
    from application.service import ApplicationService

    blocked = asyncio.Event()

    async def occupancy(now, **_kwargs):
        await blocked.wait()  # a slow repository-backed runtime fact
        return 1

    engine = SimpleNamespace(throughput=SimpleNamespace(current=lambda: 6 * MIB),
                             runtime=SimpleNamespace(configured=10 * MIB), clock=lambda: 1000.0,
                             repository=SimpleNamespace(occupied_execution_slots=occupancy))
    service = ApplicationService(engine)
    started = time.monotonic()
    speed = await asyncio.wait_for(service.execution_throughput(), 0.2)
    assert time.monotonic() - started < 0.2
    assert speed == {"download_bytes_per_second": 6 * MIB, "max_download_bytes_per_second": 10 * MIB}
    # The slower facts keep their own, unchanged projection.
    blocked.set()
    status = await service.execution_runtime_status()
    assert status["active_execution_slots"] == 1 and status["download_bytes_per_second"] == 6 * MIB


@pytest.mark.asyncio
async def test_a_neutral_speed_route_exists_beside_the_runtime_status_route():
    from api import routes

    async def throughput():
        return {"download_bytes_per_second": 4096, "max_download_bytes_per_second": 0}
    result = await routes.get_execution_throughput(application=SimpleNamespace(execution_throughput=throughput))
    assert result == {"ok": True, "download_bytes_per_second": 4096, "max_download_bytes_per_second": 0}


# ── the sampling cadence is core's, and bounded ─────────────────────────────

@pytest.mark.asyncio
async def test_the_scheduler_samples_throughput_at_presentation_cadence(monkeypatch):
    from core import scheduler

    calls = []

    async def sample():
        calls.append(time.monotonic())
    application = SimpleNamespace(observe_live_executions=sample)
    monkeypatch.setattr(scheduler, "application", application)
    monkeypatch.setattr(scheduler, "_application_storage_ready", lambda: True)
    task = asyncio.create_task(scheduler.throughput_sampling_loop())
    await asyncio.sleep(1.3)
    task.cancel()
    gaps = [b - a for a, b in zip(calls, calls[1:])]
    assert len(calls) >= 3 and max(gaps) <= 0.6


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["sample_throughput", "reconcile_executions"])
async def test_a_slow_observation_is_timed_when_its_counters_were_read(acquiring, monkeypatch, path):
    """1 MiB/s, observed through an answer that takes 1.5 s to arrive: the
    counter is as of the answer, so the elapsed time must be too -- timing it
    from the request would report 4 MiB/s."""
    meter = acquiring.engine.throughput
    clock = meter.clock = SteppedClock(meter.clock() + 1)
    began = clock.now
    observe_many = acquiring.executor.observe_many
    latency = [0.0]

    async def slow(handles):
        clock.now += latency[0]                                       # the answer arrives later...
        for job in acquiring.executor.jobs.values():                  # ...with the counter as of then
            job.state = ExecutionState.RUNNING
            job.progress = job.progress.__class__(64 * MIB, int((clock.now - began) * MIB), 1)
        return await observe_many(handles)

    monkeypatch.setattr(acquiring.executor, "observe_many", slow)
    await getattr(acquiring.engine, path)()
    clock.now += 0.5
    latency[0] = 1.5
    await getattr(acquiring.engine, path)()
    assert meter.current() == MIB
