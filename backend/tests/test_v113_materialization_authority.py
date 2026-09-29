"""DP 1.0.13 canonical materialization race (real transfer 438).

A request that already consolidated into a canonical artifact -- its
contribution a standby row, its request resolved, its binding and origin
recorded -- was later reached by a materialization decision holding an older
snapshot of it. ``TransferRepository.materialize()`` mutated the standby row
as if it were the request's own artifact, then could not find it among the
canonical artifacts and raised StopIteration ("coroutine raised
StopIteration"), which the ordinary request-failure path turned into a retry:
settled work resurrected.

The mutation boundary is the authority: the ``RequestRecord`` a caller holds
is a snapshot. Losing the opportunity to another legitimate owner is not a
failure -- the stale caller changes nothing and says so (``None``).
Orderings are forced with ``asyncio.Event``s, never timed.
"""
from __future__ import annotations

import asyncio
import json

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import VaultExecutor, VaultProvider
from transfers.convergence_engine import TransferEngine
from transfers.models import TransferRequest, TransferState
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio
PAYLOAD = b"same-bytes"


class GatedRepository(TransferRepository):
    """The real repository; ``materialize`` can be held at its entry -- after
    the decision chose to allocate, before anything durable happens."""

    def __init__(self):
        super().__init__()
        self.gate = None
        self.entered = asyncio.Event()
        self.results = []

    async def materialize(self, record, candidates, target):
        if self.gate is not None and record.id == self.gate[0]:
            self.entered.set()
            await self.gate[1].wait()
        result = await super().materialize(record, candidates, target)
        self.results.append((record.id, result))
        return result


@pytest_asyncio.fixture
async def lab(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "authority.sqlite3")
    await database.init_db()
    repository = GatedRepository()
    registry = IntegrationRegistry()
    now = [1000.0]
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"),
                            policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0,
                                                  max_active_executions=4), clock=lambda: now[0])
    await engine.initialize()
    registry.register_provider(VaultProvider())
    registry.register_executor(VaultExecutor(repository.authorize_execution, objects={
        "canon.example/item.bin": PAYLOAD, "late.example/item.bin": PAYLOAD, "keep.example/other.bin": b"other",
    }))
    return repository, engine


async def _ticks(engine, count=3):
    for _ in range(count):
        await engine.tick()


async def _durable(request_id):
    async with database.get_db() as db:
        request = dict(await db.fetchone("SELECT * FROM transfer_requests WHERE id=?", (request_id,)))
        rows = [dict(row) for row in await db.fetchall("SELECT * FROM download_files WHERE request_id=?", (request_id,))]
        consolidations = [dict(row) for row in await db.fetchall(
            "SELECT contributing_artifact_id,source_request_id,canonical_artifact_id FROM artifact_consolidations "
            "WHERE source_request_id=?", (request_id,))]
    return request, rows, consolidations


async def _canonical(repository, engine):
    """Transfer A: the established canonical artifact P."""
    first = await engine.submit((TransferRequest("vault", "canon.example/item.bin", name="item.bin"),),
                                deduplicate=False)
    await _ticks(engine)
    (primary,) = await repository.artifacts(first.id)
    return first, primary


async def _materializing(repository, engine, *, gate=None):
    """Transfer B: request R (an equivalent of P, not yet decided) and a
    sibling S that keeps B live after R consolidates. Returns R's snapshot."""
    second = await engine.submit((TransferRequest("vault", "late.example/item.bin", name="item.bin"),
                                  TransferRequest("vault", "keep.example/other.bin", name="other.bin")),
                                 deduplicate=False)
    requests = await repository.requests(second.id)
    late = next(item for item in requests if item.request.payload.startswith("late."))
    if gate is not None:
        repository.gate = (late.id, gate)
    return second, late


async def _stale_snapshot(repository, transfer_id, request_id):
    return next(item for item in await repository.requests(transfer_id) if item.id == request_id)


async def _attach(engine, repository, primary, record):
    candidates = await repository.resolved_candidates(record.id)
    assert await engine.canonical.attach(primary, record, candidates, len(PAYLOAD))
    return candidates


async def _resolve_to_materializing(repository, engine, transfer_id, request_id):
    """Record R's resolution (candidates, request ``materializing``) through
    the real repository without running its decision -- the decision is what
    the tests race."""
    record = await _stale_snapshot(repository, transfer_id, request_id)
    provider = engine.registry.providers["vault-lab"]
    attempt = await repository.begin_resolution(record.id, provider.descriptor.id)
    result = engine._authoritative_provider_result(provider.descriptor.id, await provider.resolve(record.request),
                                                   request_kind=record.request.kind)
    assert await repository.resolution(attempt, result)
    record = await _stale_snapshot(repository, transfer_id, request_id)
    assert record.state == "materializing" and await repository.resolved_candidates(record.id)
    return record


# -- repository boundary --------------------------------------------------------

async def test_a_stale_materialize_after_consolidation_changes_nothing_and_raises_nothing(lab):
    """A: attach wins before the stale materialize commits."""
    repository, engine = lab
    _first, primary = await _canonical(repository, engine)
    repository.gate = ("never", asyncio.Event())
    second, late = await _materializing(repository, engine)
    snapshot = await _resolve_to_materializing(repository, engine, second.id, late.id)
    candidates = await _attach(engine, repository, primary, snapshot)
    before = await _durable(late.id)
    assert before[0]["state"] == "resolved" and before[1][0]["mirror_state"] == "standby"

    result = await repository.materialize(snapshot, candidates, "/nowhere/else.bin")

    assert result is None  # lost authority: a neutral no-op, never an exception
    after = await _durable(late.id)
    assert after == before  # request, standby row and consolidation untouched
    assert after[1][0]["local_path"] != "/nowhere/else.bin"
    assert [item.request_id for item in await repository.artifacts(second.id)].count(late.id) == 0


async def test_a_snapshot_whose_request_is_no_longer_materializing_never_mutates(lab):
    """D: request state is exactly ``materializing`` at the mutation boundary or nothing happens."""
    repository, engine = lab
    repository.gate = ("never", asyncio.Event())
    second, late = await _materializing(repository, engine)
    snapshot = await _resolve_to_materializing(repository, engine, second.id, late.id)
    candidates = await repository.resolved_candidates(late.id)
    for state in ("pending", "failed", "resolved"):
        async with database.get_db() as db:
            await db.execute("UPDATE transfer_requests SET state=? WHERE id=?", (state, late.id))
            await db.commit()
        assert await repository.materialize(snapshot, candidates, "/any/target.bin") is None
        request, rows, _ = await _durable(late.id)
        assert request["state"] == state and rows == []


async def test_ordinary_materialization_still_allocates_and_rematerializes_its_own_artifact(lab):
    """B and C: the uncontended path, and a canonical row of this request that
    is re-materialized under the existing contract (candidates refreshed)."""
    repository, engine = lab
    repository.gate = ("never", asyncio.Event())
    second, late = await _materializing(repository, engine)
    snapshot = await _resolve_to_materializing(repository, engine, second.id, late.id)
    candidates = await repository.resolved_candidates(late.id)
    artifact = await repository.materialize(snapshot, candidates, "/downloads/item.bin")
    assert artifact is not None and artifact.request_id == late.id and artifact.target == "/downloads/item.bin"
    request, rows, _ = await _durable(late.id)
    assert request["state"] == "resolved" and len(rows) == 1
    # The request's own canonical row may be rebuilt (released, no writer).
    async with database.get_db() as db:
        await db.execute("UPDATE download_files SET status='unresolved',execution_attempt_id=NULL WHERE id=?",
                         (artifact.id,))
        await db.execute("UPDATE transfer_requests SET state='materializing' WHERE id=?", (late.id,))
        await db.commit()
    snapshot = await _stale_snapshot(repository, second.id, late.id)
    again = await repository.materialize(snapshot, candidates, "/downloads/item (2).bin")
    assert again is not None and again.id == artifact.id and again.target == "/downloads/item (2).bin"
    assert again.state == "queued"


async def test_concurrent_duplicate_materializers_leave_one_row_and_the_loser_exits_neutrally(lab):
    """F: two decisions allocate R at once: one durable row, one resolved request."""
    repository, engine = lab
    repository.gate = ("never", asyncio.Event())
    second, late = await _materializing(repository, engine)
    snapshot = await _resolve_to_materializing(repository, engine, second.id, late.id)
    candidates = await repository.resolved_candidates(late.id)
    first, other = await asyncio.gather(repository.materialize(snapshot, candidates, "/downloads/a.bin"),
                                        repository.materialize(snapshot, candidates, "/downloads/b.bin"))
    request, rows, _ = await _durable(late.id)
    assert request["state"] == "resolved" and len(rows) == 1
    winners = [item for item in (first, other) if item is not None]
    assert len(winners) == 1 and winners[0].target == rows[0]["local_path"]


# -- the real race through the engine -------------------------------------------

async def test_the_decision_that_lost_to_consolidation_never_resurrects_the_settled_request(lab):
    """The 438 shape through the real engine: R's own decision chose to
    allocate and is held at the mutation boundary; meanwhile R is consolidated
    into P (another owner won). Releasing the stale decision changes nothing:
    no StopIteration, no internal reconciliation failure, no retry."""
    repository, engine = lab
    _first, primary = await _canonical(repository, engine)
    gate = asyncio.Event()
    second, late = await _materializing(repository, engine, gate=gate)
    snapshot = await _resolve_to_materializing(repository, engine, second.id, late.id)
    # R's own decision, holding its snapshot, reaches the allocation step. The
    # canonical P is hidden from it (another transfer's owner not yet
    # established when it decided), exactly what a stale census snapshot sees.
    targets = engine.canonical.equivalence_targets

    async def nothing_yet(record):
        return () if record.id == late.id else await targets(record)

    engine.canonical.equivalence_targets = nothing_yet
    decision = asyncio.ensure_future(engine._process_request(snapshot))
    try:
        await asyncio.wait_for(repository.entered.wait(), timeout=10)
        engine.canonical.equivalence_targets = targets
        await _attach(engine, repository, primary, snapshot)  # another legitimate owner wins
        settled = await _durable(late.id)
    finally:
        gate.set()
    await asyncio.wait_for(decision, timeout=10)

    request, rows, consolidations = await _durable(late.id)
    assert (request, rows, consolidations) == settled
    assert request["state"] == "resolved" and request["error"] is None and not request["retry_at"]
    assert rows[0]["mirror_state"] == "standby" and consolidations
    assert repository.results[-1] == (late.id, None)
    async with database.get_db() as db:
        events = await db.fetchall("SELECT message FROM events WHERE torrent_id=?", (second.id,))
    assert not any("StopIteration" in json.dumps(dict(row)) for row in events)
    # The transfer goes on from settled truth.
    await _ticks(engine)
    assert (await repository.get(second.id)).state != TransferState.FAILED
    request, _rows, _ = await _durable(late.id)
    assert request["state"] == "resolved"


async def test_a_stale_materialize_after_a_same_transfer_attach_is_equally_neutral(lab):
    """G: the same authority rule whether the winner consolidated R across
    transfers or attached it inside its own transfer."""
    repository, engine = lab
    transfer = await engine.submit((TransferRequest("vault", "canon.example/item.bin", name="item.bin"),
                                    TransferRequest("vault", "late.example/item.bin", name="item.bin"),
                                    TransferRequest("vault", "keep.example/other.bin", name="other.bin")),
                                   deduplicate=False)
    records = await repository.requests(transfer.id)
    canon = next(item for item in records if item.request.payload.startswith("canon."))
    late = next(item for item in records if item.request.payload.startswith("late."))
    canon = await _resolve_to_materializing(repository, engine, transfer.id, canon.id)
    primary = await repository.materialize(canon, await repository.resolved_candidates(canon.id), "/downloads/item.bin")
    snapshot = await _resolve_to_materializing(repository, engine, transfer.id, late.id)
    candidates = await _attach(engine, repository, primary, snapshot)
    before = await _durable(late.id)
    assert before[0]["state"] == "resolved" and before[1][0]["mirror_state"] == "standby" and before[2] == []

    assert await repository.materialize(snapshot, candidates, "/downloads/elsewhere.bin") is None
    assert await _durable(late.id) == before
    assert [item.id for item in await repository.artifacts(transfer.id)
            if item.request_id in {canon.id, late.id}] == [primary.id]


async def test_a_decision_queued_behind_the_cohort_lock_does_no_work_for_a_request_settled_meanwhile(lab):
    """The scheduler side of 438: a cohort decision attaches its siblings, so
    R can be settled by a sibling's decision while R's own decision -- holding
    a census snapshot -- waits for the same per-transfer cohort lock. Once it
    holds the lock it decides from durable truth: no evidence acquisition (no
    remote connection, no lineage material used) for settled work."""
    repository, engine = lab
    _first, primary = await _canonical(repository, engine)
    second, late = await _materializing(repository, engine)
    snapshot = await _resolve_to_materializing(repository, engine, second.id, late.id)
    executor = engine.registry.executors["vault-copy"]
    lock = engine._cohort_locks.setdefault(second.id, asyncio.Lock())
    await lock.acquire()
    try:
        decision = asyncio.ensure_future(engine._process_request(snapshot))
        for _ in range(20):
            await asyncio.sleep(0)  # the decision is now waiting for the lock
        assert not decision.done()
        await _attach(engine, repository, primary, snapshot)  # the sibling's decision wins
        settled = await _durable(late.id)
        sampled = len(executor.samples)
    finally:
        lock.release()
    await asyncio.wait_for(decision, timeout=10)
    assert len(executor.samples) == sampled
    assert await _durable(late.id) == settled
    assert [result for request_id, result in repository.results if request_id == late.id] == []
