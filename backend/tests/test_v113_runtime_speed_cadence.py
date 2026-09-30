"""1.0.13: the operator-facing speed follows the one core throughput fact at
presentation cadence, never at the reconcile cycle's cadence.

Transfer 462 characterization (real container, unified multi-source load):
``/execution/runtime-status`` answered in 3.6 ms p50 (23 ms max) and the
occupancy query took 0.06 ms, yet the speed changed only every 2.1 s -- exactly
once per execution reconcile cycle (the 2 s policy interval plus ~150 ms of
cycle work). The meter was rebuilt only at the end of each whole
repository-backed cycle, so the volatile fact was hostage to it.

The one core owner now also samples that same counting rule between cycles,
from the executions the last cycle found live, serialized with the cycle and
without persisting anything; the presentation read of it is pure memory and
never waits for the slower repository-backed occupancy fact.
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
from transfers.models import TransferRequest
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


def _rate(executor, bytes_per_second):
    for job in executor.jobs.values():
        job.progress = job.progress.__class__(job.progress.total_bytes, job.progress.completed_bytes,
                                              bytes_per_second)


@pytest.mark.asyncio
async def test_the_throughput_fact_follows_the_executor_between_reconcile_cycles(acquiring):
    _rate(acquiring.executor, 2 * MIB)
    await acquiring.engine.reconcile_executions()
    assert acquiring.engine.throughput.current() == 2 * MIB
    # The executor's rate changes; no reconcile cycle runs.
    _rate(acquiring.executor, 7 * MIB)
    await acquiring.engine.sample_throughput()
    assert acquiring.engine.throughput.current() == 7 * MIB, "speed stayed hostage to the reconcile cycle"


@pytest.mark.asyncio
async def test_sampling_reads_nothing_from_the_repository_and_persists_nothing(acquiring, monkeypatch):
    _rate(acquiring.executor, 1 * MIB)
    await acquiring.engine.reconcile_executions()

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("throughput sampling touched the repository")
    for name in ("active", "artifacts", "occupied_execution_slots", "execution", "executions"):
        monkeypatch.setattr(acquiring.repository, name, forbidden)
    _rate(acquiring.executor, 3 * MIB)
    await acquiring.engine.sample_throughput()
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
async def test_sampling_is_serialized_with_the_reconcile_cycle(acquiring):
    _rate(acquiring.executor, 1 * MIB)
    await acquiring.engine.reconcile_executions()
    async with acquiring.engine._execution_cycle_lock:
        sample = asyncio.create_task(acquiring.engine.sample_throughput())
        await asyncio.sleep(0.05)
        assert not sample.done(), "a throughput sample observed executors during a reconcile cycle"
    await asyncio.wait_for(sample, 1)


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
    application = SimpleNamespace(engine=SimpleNamespace(sample_throughput=sample))
    monkeypatch.setattr(scheduler, "application", application)
    monkeypatch.setattr(scheduler, "_application_storage_ready", lambda: True)
    task = asyncio.create_task(scheduler.throughput_sampling_loop())
    await asyncio.sleep(1.3)
    task.cancel()
    gaps = [b - a for a, b in zip(calls, calls[1:])]
    assert len(calls) >= 3 and max(gaps) <= 0.6
