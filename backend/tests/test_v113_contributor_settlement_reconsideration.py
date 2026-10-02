"""DP 1.0.13 adverse multi-source convergence, Pass B: canonical settlement and
retry wake.

A contributor transfer whose every source is a verified canonical
contribution, a failed contribution, or a TRANSIENTLY unverified contribution
associated with that same canonical artifact owes no writer work of its own:
it settles CONSOLIDATED. The unverified source stays exactly what it is --
non-executable, fail-closed, visible in the canonical source history -- and
its scheduled proof reconsideration keeps running after settlement through
the one resolution scheduler, which treats a persisted ``retry_at`` as a real
wake condition even inside a long-running cycle.
"""
from __future__ import annotations

import asyncio
import time

import pytest

import db.database as database
from test_input_required_lifecycle import base  # noqa: F401  (fixture)
from test_v113_adverse_consolidation_hygiene import _canonical, _cohort, _lab, _requests, _ticks
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage
from transfers.models import (
    Endpoint, ResolutionResult, ResourceState, TransferCandidate, TransferRequest, TransferState,
)
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

MIXED = ("good.example", "flaky.example", "dead-notfound.example", "dead-refused.example")


async def _settled_contributor(base):
    """A contributor transfer with verified, failed and one transiently
    unverified contribution to the same canonical artifact."""
    repository, engine, vault, now = _lab(base)
    canonical, artifact = await _canonical(repository, engine, now)
    cohort = await engine.submit(_cohort(*MIXED), deduplicate=False)
    await _ticks(engine, now, 10, step=2)
    return repository, engine, vault, now, canonical, artifact, cohort


async def _bindings_of(request_id):
    """The canonical artifacts ``request_id`` is a bound (verified) route of."""
    async with database.get_db() as db:
        rows = await db.fetchall("""SELECT b.canonical_artifact_id FROM canonical_candidate_origins o
            JOIN canonical_candidate_bindings b ON b.id=o.binding_id WHERE o.request_id=?""", (request_id,))
    return [int(row["canonical_artifact_id"]) for row in rows]


# ── B1: a mixed contributor settles CONSOLIDATED ────────────────────────────

async def test_b1_verified_failed_and_transiently_unverified_members_settle_the_contributor(base):
    repository, engine, vault, now, canonical, artifact, cohort = await _settled_contributor(base)
    rows = await _requests(cohort.id)
    flaky = rows["flaky.example"]
    # The unverified source: associated, reconsiderable, never executable.
    assert flaky["state"] == "materializing"
    assert flaky["equivalence_disposition"] == "unverified" and flaky["equivalence_reason"] == "timeout"
    assert int(flaky["equivalence_target_artifact_id"]) == artifact.id
    assert float(flaky["retry_at"]) > now[0]
    assert rows["good.example"]["equivalence_disposition"] == "recovered"
    assert {rows[host]["equivalence_disposition"] for host in MIXED[2:]} == {"failed_contribution"}
    # The contributor owes no writer work: it is settled into the canonical transfer.
    assert (await repository.get(cohort.id)).state == TransferState.CONSOLIDATED
    assert cohort.id not in {item.id for item in await repository.active()}
    # The canonical transfer remains the one owner; the unverified source has
    # no artifact, binding, origin or writer of its own.
    (owned,) = await repository.artifacts(canonical.id)
    assert owned.id == artifact.id and owned.transfer_id == canonical.id
    assert await _bindings_of(flaky["id"]) == []
    async with database.get_db() as db:
        own = await db.fetchall("SELECT id FROM download_files WHERE request_id=?", (flaky["id"],))
    assert own == []
    assert len([call for call in vault.calls if call[0] == "start"]) == 1
    # ...and it stays visible in the canonical source history as unverified.
    detail = await repository.presentation(canonical.id, details=True)
    unverified = [item for item in detail["route_attempts"] if item.get("relation") == "unverified"]
    assert [item["request_id"] for item in unverified] == [flaky["id"]]
    assert unverified[0]["verification_state"] == "unverified"


# ── B2: a contradiction prevents false consolidation ────────────────────────

async def test_b2_an_affirmatively_contradictory_source_is_never_hidden_under_the_canonical_artifact(base):
    repository, engine, vault, now = _lab(base)
    await _canonical(repository, engine, now)
    cohort = await engine.submit(_cohort(*MIXED, "contra.example"), deduplicate=False)
    await _ticks(engine, now, 10, step=2)
    contra = (await _requests(cohort.id))["contra.example"]
    assert contra["equivalence_disposition"] in {"contradictory", "independent"}
    assert contra["equivalence_target_artifact_id"] is None
    assert (await repository.get(cohort.id)).state != TransferState.CONSOLIDATED


# ── B3: reconsideration survives the settled parent ─────────────────────────

async def test_b3_scheduled_reconsideration_runs_after_settlement_without_resurrecting_the_contributor(base):
    repository, engine, vault, now, canonical, artifact, cohort = await _settled_contributor(base)
    flaky = (await _requests(cohort.id))["flaky.example"]
    assert (await repository.get(cohort.id)).state == TransferState.CONSOLIDATED
    starts = len([call for call in vault.calls if call[0] == "start"])
    sampled = len(vault.samples)
    # Nothing happens before the deadline.
    now[0] = float(flaky["retry_at"]) - 1
    await engine.tick()
    assert len(vault.samples) == sampled
    # The source recovers; the deadline passes; the ordinary cadence proves it.
    vault.flaky = False
    now[0] = float(flaky["retry_at"]) + 1
    states = []
    for _ in range(3):
        await engine.tick()
        states.append((await repository.get(cohort.id)).state)
    assert states == [TransferState.CONSOLIDATED] * 3  # never resurrected just to wait
    row = (await _requests(cohort.id))["flaky.example"]
    assert row["equivalence_disposition"] == "recovered"
    assert row["equivalence_target_artifact_id"] is None
    # Promoted to a verified, executable canonical route -- with no writer of its own.
    assert await _bindings_of(row["id"]) == [artifact.id]
    (owned,) = await repository.artifacts(canonical.id)
    assert any("flaky.example" in item.endpoints[0].address for item in owned.candidates)
    assert len([call for call in vault.calls if call[0] == "start"]) == starts


async def test_b3_a_later_contradiction_detaches_the_association_without_a_writer_from_weak_evidence(base):
    repository, engine, vault, now, canonical, artifact, cohort = await _settled_contributor(base)
    flaky = (await _requests(cohort.id))["flaky.example"]
    vault.flaky = False
    vault.objects["flaky.example/item.bin"] = b"diff"  # the source now proves to be another object
    now[0] = float(flaky["retry_at"]) + 1
    for _ in range(3):
        await engine.tick()
    row = (await _requests(cohort.id))["flaky.example"]
    assert row["equivalence_disposition"] in {"contradictory", "independent"}
    assert row["equivalence_target_artifact_id"] is None
    (owned,) = await repository.artifacts(canonical.id)
    assert not any("flaky.example" in item.endpoints[0].address for item in owned.candidates)


# ── B4: a retry deadline inside a live long-running cycle ───────────────────

class Scripted:
    """A provider whose ``block.example`` resolution holds the cycle open and
    whose ``retry.example`` resolution fails transiently once."""

    def __init__(self):
        from transfers.applicability import ProviderApplicability
        from transfers.models import Capability, IntegrationDescriptor
        self.descriptor = IntegrationDescriptor("scripted", "Scripted", frozenset({Capability.RESOLVE}),
                                                request_types=frozenset({"scripted"}))
        self.applicability = ProviderApplicability()
        self.release = asyncio.Event()
        self.blocking = asyncio.Event()
        self.retry_attempts = []

    async def resolve(self, request):
        if request.payload == "block.example":
            self.blocking.set()
            await self.release.wait()
        if request.payload == "retry.example":
            self.retry_attempts.append(time.monotonic())
            if len(self.retry_attempts) == 1:
                return ResolutionResult(ResourceState.UNKNOWN, error=NormalizedError(
                    Domain.NETWORK, Category.CONNECTION_TIMEOUT, Stage.RESOLUTION,
                    retryability=Retryability.BACKOFF, integration_id="scripted"))
        return ResolutionResult(ResourceState.AVAILABLE, (TransferCandidate(
            "item.bin", (Endpoint("memory", f"memory:{request.payload}"),), expected_bytes=4,
            provider_id=self.descriptor.id),))


async def test_b4_a_retry_deadline_wakes_its_request_inside_the_same_long_lived_cycle(tmp_path, monkeypatch):
    from fake_integrations import MemoryExecutor
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "wake.db")
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    provider = Scripted()
    registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"), policy=TransferPolicy(
        retry_delay=0.4, resolution_retry_delay=0.4, adoption_stability_seconds=0), clock=time.time)
    await engine.initialize()
    await engine.submit((TransferRequest("scripted", "block.example", name="block.bin"),), deduplicate=False)
    await engine.submit((TransferRequest("scripted", "retry.example", name="retry.bin"),), deduplicate=False)
    cycle = asyncio.ensure_future(engine.resolve_pending())
    try:
        await asyncio.wait_for(provider.blocking.wait(), timeout=5)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 3.0
        while len(provider.retry_attempts) < 2 and loop.time() < deadline:
            await asyncio.sleep(0.02)
        # The due request was served by the SAME cycle, still held open by
        # unrelated work, with no other durable mutation to wake it.
        assert len(provider.retry_attempts) == 2, "the persisted retry deadline never woke the running cycle"
        assert provider.retry_attempts[1] - provider.retry_attempts[0] >= 0.35
        assert not cycle.done()
    finally:
        provider.release.set()
    await asyncio.wait_for(cycle, timeout=10)


async def test_b4_a_settled_associations_reconsideration_wakes_inside_a_long_lived_cycle(base, tmp_path):
    repository, engine, vault, now, canonical, artifact, cohort = await _settled_contributor(base)
    flaky = (await _requests(cohort.id))["flaky.example"]
    assert (await repository.get(cohort.id)).state == TransferState.CONSOLIDATED
    provider = Scripted()
    engine.registry.register_provider(provider)
    await engine.submit((TransferRequest("scripted", "block.example", name="block.bin"),), deduplicate=False)
    vault.flaky = False
    # Real time for this proof: the reconsideration is due shortly after the
    # long cycle has started.
    engine.clock = time.time
    async with database.get_db() as db:
        await db.execute("UPDATE transfer_requests SET retry_at=? WHERE id=?", (time.time() + 0.4, flaky["id"]))
        await db.commit()
    cycle = asyncio.ensure_future(engine.resolve_pending())
    try:
        await asyncio.wait_for(provider.blocking.wait(), timeout=5)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 3.0
        row = None
        while loop.time() < deadline:
            row = (await _requests(cohort.id))["flaky.example"]
            if row["equivalence_disposition"] == "recovered":
                break
            await asyncio.sleep(0.02)
        assert row["equivalence_disposition"] == "recovered", "the settled association was never reconsidered"
        assert not cycle.done()
        assert (await repository.get(cohort.id)).state == TransferState.CONSOLIDATED
    finally:
        provider.release.set()
    await asyncio.wait_for(cycle, timeout=10)


async def test_b4_the_idle_resolution_cadence_wakes_for_the_nearest_persisted_deadline(monkeypatch):
    """With no cycle alive, the one resolution cadence waits for the earliest
    readiness deadline the last cycle held -- never the whole poll interval."""
    import core.scheduler as scheduler
    from types import SimpleNamespace

    cycles = []

    async def resolve_pending():
        cycles.append(time.monotonic())
        if len(cycles) >= 2:
            raise asyncio.CancelledError
    engine = SimpleNamespace(policy=SimpleNamespace(resource_poll_interval=3600), clock=time.time,
                             resolution_deadline=time.time() + 0.3)
    async def integrations_started():
        return None

    fake = SimpleNamespace(resolution_wakeup=asyncio.Event(), engine=engine,
                           application_storage_permitted=lambda: True, resolve_pending=resolve_pending,
                           integrations_started=integrations_started)
    monkeypatch.setattr(scheduler, "application", fake)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(scheduler.sync_status_loop(), timeout=5)
    assert len(cycles) == 2 and 0.2 <= cycles[1] - cycles[0] < 2.0
