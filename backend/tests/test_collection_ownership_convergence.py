"""DP 1.0.13 collection ownership convergence (production transfers 394/395).

Once cross-transfer evidence establishes that two submissions are the same
multi-member collection, canonical ownership of every equivalent member
converges on the earliest admitted transfer, whatever order the individual
members happened to resolve in. Every scenario runs the real resolution,
cohort-coordination, materialization, canonical-attach, dispatch and recovery
path of the production composition; nothing here stamps ownership or writes
canonical rows directly.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Origin, Retryability, Stage, TransferError
from transfers.models import ResolutionResult, ResourceState, TransferRequest
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.recovery_repository import TransferRepository

PARTS = tuple(f"part{index:02d}.rar" for index in range(1, 6))
_OWNER_BACKOFF = 600


class SlowMemberProvider(ParcelProvider):
    """Resolves every member to the shared payload of its name. A member named
    in ``slow`` is not resolvable yet: its source is temporarily unavailable,
    so the request durably returns to PENDING with a backoff on its route --
    the ordinary unresolved state an overlapping earlier transfer's member
    sits in."""

    def __init__(self, identity, *, retry_after=None):
        super().__init__(identity)
        self.slow = set()
        self.retry_after = retry_after

    async def resolve(self, request):
        self.calls.append(("resolve", request.payload))
        if request.name in self.slow:
            raise TransferError(NormalizedError(
                Domain.RESOLUTION, Category.SOURCE_TEMPORARILY_UNAVAILABLE, Stage.RESOLUTION,
                integration_id=self.descriptor.id, retryability=Retryability.BACKOFF,
                origin=Origin.REMOTE_SOURCE, retry_after_seconds=self.retry_after,
            ))
        return ResolutionResult(
            ResourceState.AVAILABLE, (self.candidate(request.name, payload=f"shared:{request.name}"),),
        )


@pytest_asyncio.fixture
async def collection(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    # The earliest transfer's slow members back off past every pass the later
    # transfer resolves in, so they sit durably PENDING -- never mid-retry --
    # while the later members decide.
    first, second = SlowMemberProvider("provider-a", retry_after=_OWNER_BACKOFF), SlowMemberProvider("provider-b")
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(first)
    registry.register_provider(second)
    registry.register_executor(executor)
    now = [1000.0]
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0, resolution_max_attempts=20,
                              max_active_executions=32, resolution_concurrency=32),
        clock=lambda: now[0],
    )
    await engine.initialize()
    return SimpleNamespace(engine=engine, repository=repository, a=first, b=second, executor=executor, now=now)


async def _submit(env, provider, label):
    requests = tuple(
        TransferRequest("parcel", f"{label}-{name}", name=name, preferred_provider=provider.descriptor.id)
        for name in PARTS
    )
    return await env.engine.submit(requests, name=label, deduplicate=False)


async def _durable_ownership(first_id, second_id):
    """Every canonical (non-standby) artifact of either transfer, plus the
    decision each of the second transfer's requests recorded."""
    async with database.get_db() as db:
        canonicals = await db.fetchall(
            """SELECT f.id,f.torrent_id,f.filename,f.candidates FROM download_files f
                WHERE f.torrent_id IN (?,?) AND f.request_id IS NOT NULL
                AND COALESCE(f.mirror_state,'')!='standby' ORDER BY f.filename,f.id""",
            (first_id, second_id),
        )
        decisions = await db.fetchall(
            """SELECT json_extract(payload,'$.name') AS name,state,equivalence_disposition,equivalence_reason
                FROM transfer_requests WHERE transfer_id=? ORDER BY ordinal""",
            (second_id,),
        )
    return canonicals, decisions


async def _assert_converged_on(env, owner, later, *, retired_writers=0):
    canonicals, decisions = await _durable_ownership(owner.id, later.id)
    split = [(row["filename"], row["torrent_id"]) for row in canonicals if row["torrent_id"] != owner.id]
    assert not split, f"collection split across transfer owners: {split}; later-transfer decisions: {decisions}"
    # Exactly one executable canonical artifact per equivalent member, each
    # carrying both transfers' routes as candidates/failover.
    assert sorted(row["filename"] for row in canonicals) == list(PARTS)
    for artifact in await env.repository.artifacts(owner.id):
        assert sorted(candidate.provider_id for candidate in artifact.candidates) == ["provider-a", "provider-b"]
    assert await env.repository.artifacts(later.id) == ()
    async with database.get_db() as db:
        consolidations = await db.fetchall(
            """SELECT a.source_request_id,c.torrent_id AS canonical_transfer_id FROM artifact_consolidations a
                JOIN download_files c ON c.id=a.canonical_artifact_id WHERE a.source_transfer_id=?""",
            (later.id,),
        )
        inverted = await db.fetchone(
            "SELECT COUNT(*) AS n FROM artifact_consolidations WHERE source_transfer_id=?", (owner.id,),
        )
        origins = await db.fetchall(
            """SELECT DISTINCT o.contributing_transfer_id FROM canonical_candidate_origins o
                JOIN canonical_candidate_bindings b ON b.id=o.binding_id
                JOIN download_files f ON f.id=b.canonical_artifact_id WHERE f.torrent_id=?""",
            (owner.id,),
        )
    # Durable consolidation provenance: every later member maps into the owner.
    assert len(consolidations) == len(PARTS)
    assert {row["canonical_transfer_id"] for row in consolidations} == {owner.id}
    assert int(inverted["n"]) == 0
    assert {row["contributing_transfer_id"] for row in origins} == {owner.id, later.id}
    # No duplicate writer: once executions reconcile, exactly one live writer
    # per member exists and every one belongs to the collection owner; any
    # writer the later transfer ever started was retired, and its attempt is
    # kept as history.
    await env.engine.reconcile_executions()
    async with database.get_db() as db:
        writers = await db.fetchall("SELECT transfer_id,state,authorized FROM execution_attempts ORDER BY rowid")
    live = [row for row in writers if row["authorized"]]
    retired = [row for row in writers if not row["authorized"]]
    assert [row["transfer_id"] for row in live] == [owner.id] * len(PARTS)
    assert [(row["transfer_id"], row["state"]) for row in retired] == [(later.id, "cancelled")] * retired_writers
    assert len([call for call in env.executor.calls if call[0] == "start"]) == len(PARTS) + retired_writers


async def _later_members_decided(env, transfer_id, inverted):
    """Every non-inverted member consolidated; every inverted member resolved
    and its materialization decision made (materialized, or durably held)."""
    async with database.get_db() as db:
        rows = await db.fetchall(
            "SELECT json_extract(payload,'$.name') AS name,state FROM transfer_requests WHERE transfer_id=?",
            (transfer_id,),
        )
    return all(row["state"] in ({"resolved", "materializing"} if row["name"] in inverted else {"resolved"})
               for row in rows)


async def _run_inversion(env, *, slow, first_late=(), last_late=(), dispatch_first_late=False):
    """The first transfer is admitted first and establishes every member except
    ``slow``; the second transfer then resolves all of its members -- the
    ``first_late`` ones before ANY second-transfer member has consolidated
    (their writers started when ``dispatch_first_late``), the ``last_late``
    ones only after every other second-transfer member has -- and only
    afterwards do the first transfer's slow members resolve."""
    env.a.slow.update(slow)
    first = await _submit(env, env.a, "first")
    await env.engine.resolve_pending()
    assert len(await env.repository.artifacts(first.id)) == len(PARTS) - len(slow)

    env.b.slow.update(set(PARTS) - set(first_late))
    second = await _submit(env, env.b, "second")
    assert first.id < second.id
    await env.engine.resolve_pending()
    if dispatch_first_late:
        await env.engine.reconcile_executions()
        async with database.get_db() as db:
            running = await db.fetchall("SELECT id FROM execution_attempts WHERE transfer_id=?", (second.id,))
        assert len(running) == len(first_late)
    env.b.slow.clear()
    env.b.slow.update(last_late)
    env.now[0] += 60
    await env.engine.resolve_pending()
    env.b.slow.clear()
    env.now[0] += 60
    await env.engine.resolve_pending()
    assert await _later_members_decided(env, second.id, set(slow))

    env.a.slow.clear()
    env.now[0] += _OWNER_BACKOFF + 60
    await env.engine.resolve_pending()
    await env.engine.resolve_pending()
    return first, second


@pytest.mark.asyncio
async def test_member_resolution_inversion_converges_collection_on_earliest_transfer(collection):
    """Section 8.A (production class): most first-transfer members establish,
    the second transfer's members consolidate beneath them, and one second-
    transfer member resolves before its first-transfer counterpart."""
    first, second = await _run_inversion(collection, slow={"part04.rar"}, last_late=("part04.rar",))
    await _assert_converged_on(collection, first, second)
    # Collection evidence already existed when the inverted member resolved:
    # it was held, so no later-owned artifact ever had to be converged.
    assert await _convergence_events(second.id) == []


async def _convergence_events(transfer_id):
    async with database.get_db() as db:
        rows = await db.fetchall(
            "SELECT detail FROM event_journal WHERE event_type='consolidation.ownership_converged' "
            "AND (transfer_id=? OR related_transfer_id=?)",
            (transfer_id, transfer_id),
        )
    return [row["detail"] for row in rows]


@pytest.mark.asyncio
async def test_multiple_member_inversions_converge_including_one_before_collection_evidence(collection):
    """Section 8.B: several inverted members, one of which materializes before
    any cross-transfer collection evidence exists and must still converge."""
    first, second = await _run_inversion(
        collection, slow={"part01.rar", "part03.rar", "part05.rar"}, first_late=("part01.rar",),
    )
    await _assert_converged_on(collection, first, second)
    # part01 really did become canonical under the later transfer first; the
    # history of that decision is kept, and the convergence is recorded.
    _, decisions = await _durable_ownership(first.id, second.id)
    early = next(row for row in decisions if row["name"] == "part01.rar")
    assert (early["equivalence_disposition"], early["equivalence_reason"]) == ("independent", "logical_pairing_mismatch")
    assert await _convergence_events(second.id) == [f"Collection member ownership converged into transfer {first.id}"]
    # One occurrence, one journal record, correlated to both transfers -- and
    # both transfers' Details show it.
    assert await _convergence_events(first.id) == [f"Collection member ownership converged into transfer {first.id}"]
    for transfer in (first, second):
        details = await collection.repository.presentation(transfer.id, details=True)
        assert "Collection member ownership converged" in [event["message"] for event in details["events"]]


@pytest.mark.asyncio
async def test_running_later_writer_is_retired_and_collection_converges_on_earliest_transfer(collection):
    """A later-transfer member became canonical before any collection evidence
    existed and its writer is already running when the earliest transfer's
    matching member resolves: the writer is retired under a recovery claim and
    ownership still converges on the earliest admitted transfer."""
    first, second = await _run_inversion(
        collection, slow={"part01.rar"}, first_late=("part01.rar",), dispatch_first_late=True,
    )
    await _assert_converged_on(collection, first, second, retired_writers=1)
    assert await _convergence_events(second.id) == [f"Collection member ownership converged into transfer {first.id}"]


@pytest.mark.asyncio
async def test_completed_later_writer_freezes_ownership_and_satisfies_the_earlier_member(collection):
    """Terminal boundary: a later-transfer member completed before the earliest
    transfer's matching member resolved. Completed material is ownership-
    frozen -- never moved, re-parented or un-completed -- and the earlier
    member is satisfied by it: one completed file, no second writer, and the
    earlier transfer still settles."""
    env = collection
    env.a.slow.add("part01.rar")
    first = await _submit(env, env.a, "first")
    await env.engine.resolve_pending()
    env.b.slow.update(set(PARTS) - {"part01.rar"})
    second = await _submit(env, env.b, "second")
    await env.engine.resolve_pending()
    await env.engine.reconcile_executions()
    later = (await env.repository.artifacts(second.id))[0]
    env.executor.finish(later.execution)
    await env.engine.reconcile_executions()
    env.b.slow.clear()
    env.now[0] += 60
    await env.engine.resolve_pending()

    async with database.get_db() as db:
        frozen = await db.fetchone("SELECT * FROM download_files WHERE id=?", (later.id,))
        postprocess = await db.fetchall("SELECT * FROM postprocess_attempts ORDER BY rowid")
    assert frozen["status"] == "completed" and frozen["torrent_id"] == second.id

    env.a.slow.clear()
    env.now[0] += _OWNER_BACKOFF + 60
    await env.engine.resolve_pending()
    await env.engine.resolve_pending()
    await env.engine.reconcile_executions()
    for artifact in await env.repository.artifacts(first.id):
        env.executor.finish(artifact.execution)
    await env.engine.reconcile_executions()

    async with database.get_db() as db:
        member = await db.fetchall(
            """SELECT f.* FROM download_files f JOIN transfer_requests r ON r.id=f.request_id
                WHERE json_extract(r.payload,'$.name')='part01.rar' ORDER BY f.id""")
        writers = await db.fetchall(
            """SELECT e.state FROM execution_attempts e JOIN download_files f ON f.id=e.artifact_id
                JOIN transfer_requests r ON r.id=f.request_id WHERE json_extract(r.payload,'$.name')='part01.rar'""")
        consolidation = await db.fetchone(
            "SELECT * FROM artifact_consolidations WHERE source_transfer_id=? AND canonical_artifact_id=?",
            (first.id, later.id))
        contributed = await db.fetchone(
            """SELECT COUNT(*) AS n FROM canonical_candidate_origins o JOIN canonical_candidate_bindings b
                ON b.id=o.binding_id WHERE b.canonical_artifact_id=? AND o.contributing_transfer_id=?""",
            (later.id, first.id))
        after = await db.fetchone("SELECT * FROM download_files WHERE id=?", (later.id,))
        postprocess_after = await db.fetchall("SELECT * FROM postprocess_attempts ORDER BY rowid")
    # One completed file, never moved, re-parented or un-completed.
    assert dict(after) == dict(frozen)
    assert sorted(path.name for path in Path(later.target).parent.iterdir()
                  if path.name.startswith("part01")) == ["part01.rar"]
    # No second writer: the only part01 artifact the earlier transfer has is a
    # standby of the completed one, and only the completed writer ever ran.
    assert [(row["id"], row["mirror_state"]) for row in member] == [
        (later.id, ""), (member[1]["id"], "standby")]
    assert member[1]["torrent_id"] == first.id and member[1]["mirror_group_id"] == later.id
    assert [row["state"] for row in writers] == ["succeeded"]
    # Provenance: the earlier transfer later contributed an equivalent member.
    assert consolidation is not None and consolidation["contributing_artifact_id"] == member[1]["id"]
    assert int(contributed["n"]) == 1
    # No post-processing replayed, and the earlier transfer settles.
    assert [dict(row) for row in postprocess_after] == [dict(row) for row in postprocess]
    assert (await env.repository.get(first.id)).state.value == "completed"
