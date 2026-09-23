"""1.0.13: DebridPulse core is the SOLE admission authority for Usenet work.

SAB's native queue is proven (Gate 1) to accept effectively unlimited jobs.
That is never DP capacity. These tests drive the REAL engine and prove that
exactly ``max_active_executions`` NZB executions ever reach the SAB
submission boundary.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from transfers.convergence_engine import TransferEngine
from transfers.models import MaterializationKind, TransferRequest
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

from sab_fakes import FakeSab, staged_store


VALID_NZB = b"""<?xml version="1.0" encoding="iso-8859-1" ?>
<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">
 <file poster="p@e.net" date="1700000000" subject="job [1/1] - &quot;job.bin&quot; yEnc (1/1)">
  <groups><group>alt.binaries.test</group></groups>
  <segments><segment bytes="1024" number="1">a@e.net</segment></segments>
 </file>
</nzb>
"""

MAX_ACTIVE = 2


@pytest_asyncio.fixture
async def usenet_core(tmp_path, monkeypatch):
    from executors.sabnzbd.executor import SabnzbdConfiguration, SabnzbdExecutor
    from providers.usenet.provider import UsenetProvider

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    root = tmp_path / "payloads"
    root.mkdir(parents=True, exist_ok=True)
    sab = FakeSab(complete_dir=str(root / ".dpwork" / "complete"),
                  download_dir=str(root / ".dpwork" / "incomplete"))
    executor = SabnzbdExecutor(
        sab,
        SabnzbdConfiguration(local_root=str(root), working_directory=str(root / ".dpwork"),
                             complete_directory=str(root / ".dpwork" / "complete")),
        repository.authorize_execution,
        staged_input=staged_store(),
    )
    registry.register_provider(UsenetProvider(staged_input=staged_store()))
    registry.register_executor(executor)
    now = [1000.0]
    policy = TransferPolicy(retry_delay=1, adoption_stability_seconds=0,
                            max_active_executions=MAX_ACTIVE)
    engine = TransferEngine(repository, registry, download_root=str(root),
                            policy=policy, clock=lambda: now[0])
    await engine.initialize()
    return SimpleNamespace(engine=engine, repository=repository, registry=registry,
                           executor=executor, sab=sab, now=now, root=root)


async def submit_nzbs(core, count):
    ids = []
    for index in range(count):
        result = await core.engine.submit(
            (TransferRequest("nzb", VALID_NZB, name=f"job-{index}.nzb"),),
            name=f"job-{index}", deduplicate=False)
        ids.append(result)
    return ids


async def converge(core, rounds=8):
    import asyncio
    for _ in range(rounds):
        await core.engine.tick()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_only_admitted_executions_ever_reach_sab(usenet_core):
    """N+K eligible NZB requests -> exactly N native SAB jobs."""
    await submit_nzbs(usenet_core, MAX_ACTIVE + 3)
    await converge(usenet_core)
    assert len(usenet_core.sab.submissions) == MAX_ACTIVE
    assert len(usenet_core.sab.queue) == MAX_ACTIVE


@pytest.mark.asyncio
async def test_remaining_requests_hold_no_native_identity(usenet_core):
    await submit_nzbs(usenet_core, MAX_ACTIVE + 3)
    await converge(usenet_core)
    bound = 0
    for transfer_id in range(1, MAX_ACTIVE + 5):
        for artifact in await usenet_core.repository.artifacts(transfer_id):
            if artifact.execution is not None and artifact.execution.native is not None:
                bound += 1
    assert bound == MAX_ACTIVE


@pytest.mark.asyncio
async def test_releasing_one_slot_admits_exactly_one_more(usenet_core):
    await submit_nzbs(usenet_core, MAX_ACTIVE + 3)
    await converge(usenet_core)
    assert len(usenet_core.sab.submissions) == MAX_ACTIVE

    finished = next(iter(usenet_core.sab.queue))
    usenet_core.sab.finish(finished)
    await converge(usenet_core)
    assert len(usenet_core.sab.submissions) == MAX_ACTIVE + 1


@pytest.mark.asyncio
async def test_enlarging_sabs_own_queue_capacity_admits_nothing_extra(usenet_core):
    """SAB accepting more work is never DP permission to submit more."""
    await submit_nzbs(usenet_core, MAX_ACTIVE + 4)
    await converge(usenet_core)
    before = len(usenet_core.sab.submissions)
    # Whatever SAB would tolerate natively, DP admission is unchanged.
    usenet_core.sab.queue_limit = 10_000
    await converge(usenet_core)
    assert len(usenet_core.sab.submissions) == before == MAX_ACTIVE


@pytest.mark.asyncio
async def test_prepare_never_creates_native_queue_entries(usenet_core):
    """prepare() runs before the core capacity gate and must be inert."""
    from transfers.models import (
        ExecutionRequest, ExecutionSubject, ExecutionWork, MaterializationPlan, TransferCandidate,
    )
    candidate = TransferCandidate(
        name="inert", endpoints=(), provider_id="usenet",
        materialization=MaterializationKind.COLLECTION, request_kind="nzb",
        context={"nzb": VALID_NZB})
    plan = MaterializationPlan(MaterializationKind.COLLECTION, str(usenet_core.root / "inert"))
    request = ExecutionRequest(
        ExecutionWork(ExecutionSubject.of(candidate), plan, "inert-attempt"), "inert-attempt")
    for _ in range(5):
        usenet_core.executor.prepare(request)
    assert usenet_core.sab.submissions == []
    assert usenet_core.sab.queue == {}


@pytest.mark.asyncio
async def test_per_server_connections_is_not_dp_admission_capacity(usenet_core):
    """NNTP Connections tunes acquisition of already-admitted work only."""
    usenet_core.sab.servers["primary"] = {"connections": 50, "priority": 0}
    await submit_nzbs(usenet_core, MAX_ACTIVE + 3)
    await converge(usenet_core)
    assert len(usenet_core.sab.submissions) == MAX_ACTIVE
