"""Defect B — a user-deleted transfer must never remain the active dedupe /
recovery identity, and a re-add is a genuinely fresh generation even when the
provider returns the identical native resource.

* Delete retires the active unique ``hash`` key to a deterministic
  transfer-specific tombstone and preserves the original logical fingerprint in
  ``torrents.source_fingerprint``.
* ``provider_resources.id`` is the durable (transfer, canonical-resource)
  binding-generation id; ``resource_key`` is the transfer-independent canonical
  ``ProviderResource.id``. Two transfers that bind the same canonical resource
  get distinct binding ids, so identical native identity + identical file trees
  can never alias one generation's manifest/selection rows into another's.
* A fresh re-add is admitted immediately even while predecessor provider cleanup
  is outstanding; a fence holds the fresh generation's first provider-resource
  creation until no predecessor cleanup operation can execute (pending /
  claimed-in-flight / scheduled-retry all block; completion or terminal
  abandonment releases; a crashed claim is re-driven by startup reclaim).

Regression matrix: specification section 22 (A-H) plus the operator-mandated
binding-isolation and fence-race regressions.
"""
from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Recovery, Retryability, Stage
from transfers.models import OutcomeKind, ResourceState, TransferOutcome, TransferRequest, TransferState
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


class Clock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


@pytest_asyncio.fixture
async def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "retire.db")
    await database.init_db()
    return TransferRepository()


@pytest_asyncio.fixture
async def core(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "retire-engine.db")
    await database.init_db()
    return await _build_core(tmp_path)


async def _build_core(tmp_path):
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider(file_manifest=True)
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    clock = Clock(1000.0)
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(max_attempts=50, retry_delay=0, resolution_retry_delay=0,
                              adoption_stability_seconds=0, resource_poll_interval=5,
                              max_active_executions=4),
        clock=clock,
    )
    await engine.initialize()
    return SimpleNamespace(engine=engine, repository=repository, registry=registry,
                           provider=provider, executor=executor, clock=clock, tmp_path=tmp_path)


def _magnet(fingerprint: str = "btih-abc", payload: str = "box") -> TransferRequest:
    return TransferRequest("parcel", payload, name="payload.bin", fingerprint=fingerprint)


async def _torrents(where: str = "", params=()):
    async with database.get_db() as db:
        return await db.fetchall(f"SELECT * FROM torrents {where}", params)


async def _provider_resource_rows(transfer_id: int | None = None):
    async with database.get_db() as db:
        if transfer_id is None:
            return await db.fetchall("SELECT * FROM provider_resources ORDER BY rowid")
        return await db.fetchall(
            "SELECT * FROM provider_resources WHERE transfer_id=? ORDER BY rowid", (transfer_id,))


def _transient_cleanup_failure() -> TransferOutcome:
    return TransferOutcome(OutcomeKind.FAILURE, NormalizedError(
        Domain.CLEANUP, Category.REMOTE_CLEANUP_FAILED, Stage.CLEANUP,
        retryability=Retryability.BACKOFF, recovery=Recovery.RETRY))


# --------------------------------------------------------------------------- #
# A. Deleted fingerprint retirement
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_delete_retires_the_active_fingerprint_and_readd_is_a_fresh_transfer(repo):
    a, created_a = await repo.admit((_magnet(),), name="A")
    assert created_a is True

    await repo.delete(a.id, remote=True, now=1000.0)

    rows = await _torrents("WHERE id=?", (a.id,))
    assert rows[0]["status"] == "deleted"
    assert rows[0]["source_fingerprint"] == "btih-abc"
    assert rows[0]["hash"] == f"deleted:{a.id}:btih-abc" != "btih-abc"

    b, created_b = await repo.admit((_magnet(),), name="B")
    assert created_b is True and b.id != a.id
    row_b = (await _torrents("WHERE id=?", (b.id,)))[0]
    assert row_b["hash"] == "btih-abc" and row_b["source_fingerprint"] == "btih-abc"
    assert row_b["status"] == "pending"
    assert len(await _torrents()) == 2                          # historical row retained


@pytest.mark.asyncio
async def test_repeated_delete_never_recursively_prefixes_the_tombstone(repo):
    a, _ = await repo.admit((_magnet(),), name="A")
    await repo.delete(a.id, remote=True)
    await repo.delete(a.id, remote=True)
    await repo.delete(a.id, remote=False)
    row = (await _torrents("WHERE id=?", (a.id,)))[0]
    assert row["hash"] == f"deleted:{a.id}:btih-abc"


# --------------------------------------------------------------------------- #
# B. Existing database migration (source_fingerprint + resource_key)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_existing_database_migration_is_idempotent_and_reversible(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    await database.init_db()

    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE torrents SET source_fingerprint=NULL")
        conn.execute("UPDATE provider_resources SET resource_key=NULL")
        conn.execute("INSERT INTO torrents(hash,name,status) VALUES('legacy-deleted','old','deleted')")
        conn.execute("INSERT INTO torrents(hash,name,status) VALUES('legacy-active','act','processing')")
        tid = conn.execute("SELECT id FROM torrents WHERE name='act'").fetchone()[0]
        conn.execute(
            "INSERT INTO provider_resources(id,transfer_id,provider_id,payload,state,resource_key) "
            "VALUES('legacy-res',?,'parcel-lab','{}','available',NULL)", (tid,))
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    pristine = tmp_path / "pristine.db"
    pristine.write_bytes(path.read_bytes())

    await database.init_db()                                    # run the additive backfills

    with sqlite3.connect(path) as conn:
        torrents = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT name,hash,source_fingerprint FROM torrents")}
        res = {r[0]: r[1] for r in conn.execute("SELECT id,resource_key FROM provider_resources")}
    assert torrents["old"][1] == "legacy-deleted"               # original fingerprint preserved
    assert torrents["old"][0].startswith("deleted:") and torrents["old"][0].endswith(":legacy-deleted")
    assert torrents["act"] == ("legacy-active", "legacy-active")
    assert res["legacy-res"] == "legacy-res"                    # resource_key backfilled from id, PK unchanged

    await database.init_db()                                    # idempotent
    with sqlite3.connect(path) as conn:
        again = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT name,hash,source_fingerprint FROM torrents")}
    assert again["old"] == torrents["old"]

    with sqlite3.connect(pristine) as conn:                     # untouched copy = pre-migration
        assert ("legacy-deleted", None) in [
            (r[0], r[1]) for r in conn.execute("SELECT hash,source_fingerprint FROM torrents")]
        assert conn.execute("SELECT resource_key FROM provider_resources").fetchone()[0] is None

    repository = TransferRepository()
    fresh, created = await repository.admit(
        (TransferRequest("parcel", "box", name="x", fingerprint="legacy-deleted"),), name="fresh")
    assert created is True


# --------------------------------------------------------------------------- #
# Binding-generation identity (operator-mandated)
# --------------------------------------------------------------------------- #

async def _resolve_transfer(core, *, fingerprint, name, payload="box"):
    core.provider.responses.append(core.provider.parcel(payload, state=ResourceState.AVAILABLE))
    transfer = await core.engine.submit((_magnet(fingerprint, payload),), name=name)
    await core.engine.resolve_pending()
    return transfer


@pytest.mark.asyncio
async def test_identical_native_resource_gets_distinct_binding_ids_per_transfer(core):
    a = await _resolve_transfer(core, fingerprint="fp1", name="A")
    canonical_a = (await core.repository.resources(a.id))[0][0]
    row_a = (await _provider_resource_rows(a.id))[0]
    assert row_a["resource_key"] == canonical_a.id              # canonical id, transfer-independent
    assert row_a["id"] != canonical_a.id                        # binding id != canonical id

    await core.engine.delete(a.id, remote=False)                # no remote cleanup owed

    b = await _resolve_transfer(core, fingerprint="fp1", name="B")
    assert b.id != a.id
    canonical_b = (await core.repository.resources(b.id))[0][0]
    row_b = (await _provider_resource_rows(b.id))[0]
    assert canonical_b.id == canonical_a.id                     # SAME native / canonical resource
    assert row_b["resource_key"] == canonical_b.id
    assert row_b["id"] != row_a["id"]                           # DISTINCT binding generation

    all_rows = await _provider_resource_rows()
    assert {r["transfer_id"] for r in all_rows} == {a.id, b.id}  # A's historical row coexists


# --------------------------------------------------------------------------- #
# Binding resolution with A (retired) and B (active) present simultaneously
# (operator-mandated final proof)
# --------------------------------------------------------------------------- #

async def _seed_both_bindings(repo, *, legacy_form: bool):
    """Historical deleted binding A and current active binding B, both with
    resource_key == the same canonical resource R."""
    canonical = "canon-R"
    native_ctx = {"id": "native-1"}
    async with database.get_db() as db:
        a_id = await db.execute_returning_id(
            "INSERT INTO torrents(hash,name,status,source_fingerprint) VALUES('deleted:0:fp-x','A','deleted','fp-x')")
        b_id = await db.execute_returning_id(
            "INSERT INTO torrents(hash,name,status,source_fingerprint) VALUES('fp-x','B','processing','fp-x')")
        for tid in (a_id, b_id):
            await db.execute("INSERT INTO transfer_requests(id,transfer_id,ordinal,payload,state) "
                             "VALUES(?,?,0,'{}','resolved')", (f"req-{tid}", tid))
        a_pk = canonical if legacy_form else TransferRepository._resource_binding_id(a_id, canonical)
        a_key = None if legacy_form else canonical
        b_pk = TransferRepository._resource_binding_id(b_id, canonical)
        payload = codec_dump_resource(canonical, native_ctx)
        await db.execute(
            "INSERT INTO provider_resources(id,transfer_id,provider_id,payload,state,resource_key,cleanup_authority) "
            "VALUES(?,?,?,?,?,?, 'user_request')", (a_pk, a_id, "parcel-lab", payload, "available", a_key))
        await db.execute(
            "INSERT INTO provider_resources(id,transfer_id,provider_id,payload,state,resource_key) "
            "VALUES(?,?,?,?,?,?)", (b_pk, b_id, "parcel-lab", payload, "available", canonical))
        await db.commit()
    return SimpleNamespace(canonical=canonical, native_ctx=native_ctx, a_id=a_id, b_id=b_id,
                           a_pk=a_pk, b_pk=b_pk)


def codec_dump_resource(canonical_id, context):
    from transfers import codec
    from transfers.models import Ownership, ProviderResource
    return codec.dump(ProviderResource("parcel-lab", context, Ownership.CREATED, id=canonical_id))


def _canonical_resource(seed):
    from transfers.models import Ownership, ProviderResource
    return ProviderResource("parcel-lab", seed.native_ctx, Ownership.CREATED, id=seed.canonical)


@pytest.mark.parametrize("legacy_form", [False, True])
@pytest.mark.asyncio
async def test_observation_of_shared_canonical_resource_targets_the_active_binding(repo, legacy_form):
    seed = await _seed_both_bindings(repo, legacy_form=legacy_form)

    before_a = (await _provider_resource_rows(seed.a_id))[0]

    # An observation/reconciliation that begins with the canonical resource id.
    from transfers.models import ResourceState as RS
    await repo.resource_observation(seed.b_id, _canonical_resource(seed), RS.AVAILABLE)
    await repo.resource_observation(seed.b_id, _canonical_resource(seed), RS.PREPARING)   # again

    rows = await _provider_resource_rows()
    assert len(rows) == 2                                       # no duplicate binding created
    by_transfer = {r["transfer_id"]: r for r in rows}
    assert by_transfer[seed.b_id]["state"] == "preparing"      # B was updated
    after_a = by_transfer[seed.a_id]
    assert after_a["state"] == before_a["state"] == "available"   # A untouched
    assert after_a["cleanup_authority"] == "user_request"      # A's cleanup authority not transferred
    assert after_a["id"] == seed.a_pk                          # A's binding id unchanged

    # resource_binding_id resolves the canonical id to each transfer's own binding.
    assert await repo.resource_binding_id(seed.b_id, seed.canonical) == seed.b_pk
    assert await repo.resource_binding_id(seed.a_id, seed.canonical) == seed.a_pk

    # A fresh repository (restart) resolves identically.
    restarted = TransferRepository()
    assert await restarted.resource_binding_id(seed.b_id, seed.canonical) == seed.b_pk
    await restarted.resource_observation(seed.b_id, _canonical_resource(seed), RS.AVAILABLE)
    assert len(await _provider_resource_rows()) == 2


@pytest.mark.asyncio
async def test_cleanup_on_one_binding_never_targets_the_other(repo):
    seed = await _seed_both_bindings(repo, legacy_form=False)

    # Clear A's cleanup responsibility via the canonical id + A's transfer.
    await repo.cleanup_intent(seed.a_id, seed.canonical, None)
    rows = {r["transfer_id"]: r for r in await _provider_resource_rows()}
    assert rows[seed.a_id]["cleanup_authority"] is None
    assert rows[seed.b_id]["cleanup_authority"] is None         # B never had one; unchanged

    # Give B a cleanup intent; A stays clear.
    await repo.cleanup_intent(seed.b_id, seed.canonical, "owned")
    rows = {r["transfer_id"]: r for r in await _provider_resource_rows()}
    assert rows[seed.b_id]["cleanup_authority"] == "owned"
    assert rows[seed.a_id]["cleanup_authority"] is None

    # pending_cleanup yields B's binding id + B's transfer id, never crossing.
    pending = await repo.pending_cleanup(now=0.0)
    assert [(t, b) for t, _r, _a, _n, b in pending] == [(seed.b_id, seed.b_pk)]


@pytest.mark.asyncio
async def test_resource_id_duplicate_lookup_resolves_to_the_active_binding(repo):
    from services.duplicates import find_resource_id_duplicate
    seed = await _seed_both_bindings(repo, legacy_form=True)   # A.id == canonical

    match = await find_resource_id_duplicate(seed.canonical)
    assert match is not None
    assert match.torrent_id == seed.b_id                        # the active binding, never deleted A
    assert match.status == "processing"


@pytest.mark.asyncio
async def test_inventory_reconciliation_targets_the_active_binding_not_the_retired_predecessor(core):
    from transfers.models import Ownership, ProviderObservation, ProviderResource

    # A resolves and is then deleted; B re-adds the same source (same native id).
    a = await _resolve_transfer(core, fingerprint="fp-inv2", name="A")
    canonical = (await core.repository.resources(a.id))[0][0]
    await core.engine.delete(a.id, remote=False)
    b = await _resolve_transfer(core, fingerprint="fp-inv2", name="B")
    b_binding = (await _provider_resource_rows(b.id))[0]["id"]
    a_binding = (await _provider_resource_rows(a.id))[0]["id"]

    # Inventory reports the shared canonical resource.
    core.provider.inventory_items = (
        ProviderObservation(
            ProviderResource(canonical.provider_id, dict(canonical.context), Ownership.OBSERVED, id=canonical.id),
            ResourceState.AVAILABLE),
    )
    await core.engine.reconcile_inventory()

    rows = {r["transfer_id"]: r for r in await _provider_resource_rows()}
    assert set(rows) == {a.id, b.id}                            # no third binding
    assert rows[b.id]["id"] == b_binding and rows[b.id]["state"] == "available"
    assert rows[a.id]["id"] == a_binding                        # A's binding untouched
    assert (await core.repository.get(a.id)).state == TransferState.DELETED   # not resurrected


@pytest.mark.asyncio
async def test_identical_file_tree_never_aliases_predecessor_manifest_or_selection(core):
    from file_selection_support import file_manifest

    files = [("e1", "s/e1", 10), ("e2", "s/e2", 20), ("e3", "s/e3", 30)]
    core.provider.responses.append(core.provider.parcel("box", state=ResourceState.AVAILABLE, files=files))
    a = await core.engine.submit((_magnet("fp-tree"),), name="A")
    await core.engine.resolve_pending()
    view_a = await core.repository.file_selection_presentation(a.id, now=core.clock())
    assert view_a is not None
    await core.repository.confirm_file_selection(
        a.id, view_a["manifest_id"], [view_a["entries"][0]["entry_id"]], now=core.clock())
    manifest_a, selection_a = view_a["manifest_id"], view_a["selection_id"]

    await core.engine.delete(a.id, remote=False)

    core.provider.responses.append(core.provider.parcel("box", state=ResourceState.AVAILABLE, files=files))
    b = await core.engine.submit((_magnet("fp-tree"),), name="B")
    await core.engine.resolve_pending()
    view_b = await core.repository.file_selection_presentation(b.id, now=core.clock())
    assert view_b is not None

    # Identical native resource, identical tree — but B's provenance is B-owned.
    assert view_b["manifest_id"] != manifest_a
    assert view_b["selection_id"] != selection_a
    assert view_b["provider_resource_id"] != view_a["provider_resource_id"]
    assert view_b["decision"] == "pending" and view_b["selected_entry_ids"] == []
    assert {e["entry_id"] for e in view_b["entries"]}.isdisjoint({e["entry_id"] for e in view_a["entries"]})

    async with database.get_db() as db:
        rows = await db.fetchall(
            "SELECT id, transfer_id, request_id FROM transfer_file_manifests ORDER BY rowid")
        gen_a = await db.fetchone("SELECT decision FROM transfer_file_selections WHERE id=?", (selection_a,))
    by_id = {r["id"]: r for r in rows}
    assert by_id[manifest_a]["transfer_id"] == a.id             # A's manifest row untouched
    assert by_id[view_b["manifest_id"]]["transfer_id"] == b.id  # B's manifest row is B's
    assert gen_a["decision"] == "explicit"                      # A's history intact


@pytest.mark.asyncio
async def test_pre_upgrade_provider_resource_row_keeps_working(repo):
    # A pre-split row whose primary key IS the canonical id (resource_key NULL,
    # as it is right after the additive column is added but before backfill)
    # still resolves to the right binding.
    async with database.get_db() as db:
        tid = await db.execute_returning_id(
            "INSERT INTO torrents(hash,name,status) VALUES('fp-legacy','L','processing')")
        await db.execute("INSERT INTO transfer_requests(id,transfer_id,ordinal,payload,state) "
                         "VALUES('req-L',?,0,'{}','resolved')", (tid,))
        await db.execute(
            "INSERT INTO provider_resources(id,transfer_id,provider_id,payload,state,resource_key) "
            "VALUES('parcel-lab:legacy',?,'parcel-lab','{}','available',NULL)", (tid,))
        await db.commit()

    resolved = await repo.resource_binding_id(tid, "parcel-lab:legacy")
    assert resolved == "parcel-lab:legacy"                      # legacy row matched by id, PK unchanged

    # After the backfill the same lookup still returns the historical primary key.
    await database.init_db()
    assert await repo.resource_binding_id(tid, "parcel-lab:legacy") == "parcel-lab:legacy"
    async with database.get_db() as db:
        row = await db.fetchone("SELECT resource_key FROM provider_resources WHERE id='parcel-lab:legacy'")
    assert row["resource_key"] == "parcel-lab:legacy"


# --------------------------------------------------------------------------- #
# C. Cleanup still pending — a fresh re-add waits safely, then proceeds
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_readd_while_predecessor_cleanup_pending_is_fresh_and_never_recovery_failed(core):
    a = await _resolve_transfer(core, fingerprint="btih-abc", name="A")
    assert await core.repository.resources(a.id)

    core.provider.cleanup_response = _transient_cleanup_failure()
    await core.engine.delete(a.id, remote=True)
    assert (await core.repository.get(a.id)).state == TransferState.DELETED
    assert [p for _r, _s, p in await core.repository.resources(a.id) if p]   # cleanup owed

    b = await core.engine.submit((_magnet(),), name="B")
    assert b.id != a.id and (await core.repository.get(b.id)).state == TransferState.ACCEPTED

    core.provider.responses.append(core.provider.parcel("box", state=ResourceState.AVAILABLE))
    for _ in range(3):
        await core.engine.resolve_pending()
        core.clock.advance(6)
    assert await core.repository.resources(b.id) == ()          # B fenced, no resource yet
    assert all(r.state in {"pending", "waiting"} for r in await core.repository.requests(b.id))
    assert (await core.repository.get(b.id)).error is None

    core.provider.cleanup_response = TransferOutcome(OutcomeKind.SUCCESS)
    for _ in range(8):
        await core.engine.cleanup_pending()
        await core.engine.resolve_pending()
        core.clock.advance(6)
    assert await core.repository.resources(b.id)                 # B proceeds automatically
    assert (await core.repository.get(b.id)).state not in {
        TransferState.FAILED, TransferState.DELETED, TransferState.CANCELLED}


# --------------------------------------------------------------------------- #
# D. Predecessor cleanup cannot mutate / delete the fresh generation
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_claimed_in_flight_predecessor_cleanup_cannot_delete_the_fresh_resource(core):
    a = await _resolve_transfer(core, fingerprint="btih-abc", name="A")
    canonical_a = (await core.repository.resources(a.id))[0][0]

    # Enter cleanup and CLAIM it (blocked=1), but the provider call never returns.
    entered, release = asyncio.Event(), asyncio.Event()
    original_cleanup = core.provider.cleanup

    async def blocking_cleanup(directive):
        entered.set()
        await release.wait()
        return await original_cleanup(directive)

    core.provider.cleanup = blocking_cleanup
    await core.repository.delete(a.id, remote=True, now=core.clock())
    task = asyncio.create_task(core.engine._cleanup_resources(a.id, explicit=True))
    await entered.wait()

    # While A's cleanup is claimed and in flight, B is admitted and driven.
    b = await core.engine.submit((_magnet(),), name="B")
    core.provider.responses.append(core.provider.parcel("box", state=ResourceState.AVAILABLE))
    for _ in range(3):
        await core.engine.resolve_pending()
        core.clock.advance(6)
    assert await core.repository.resources(b.id) == ()          # B cannot create its resource
    assert (await core.repository.get(b.id)).error is None

    # A's cleanup finally returns; B then proceeds and binds its OWN row.
    release.set()
    await task
    core.provider.cleanup = original_cleanup
    core.provider.responses.append(core.provider.parcel("box", state=ResourceState.AVAILABLE))
    for _ in range(8):
        await core.engine.cleanup_pending()
        await core.engine.resolve_pending()
        core.clock.advance(6)
    rows_b = await _provider_resource_rows(b.id)
    assert rows_b and all(r["transfer_id"] == b.id for r in rows_b)
    assert all(r["id"] != canonical_a.id for r in rows_b)       # never adopted A's binding


@pytest.mark.asyncio
async def test_restart_during_the_cleanup_fence_preserves_the_block(core):
    a = await _resolve_transfer(core, fingerprint="btih-abc", name="A")
    core.provider.cleanup_response = _transient_cleanup_failure()
    await core.engine.delete(a.id, remote=True)
    b = await core.engine.submit((_magnet(),), name="B")
    core.provider.responses.append(core.provider.parcel("box", state=ResourceState.AVAILABLE))
    await core.engine.resolve_pending()
    assert await core.repository.resources(b.id) == ()

    # A fresh engine/repository against the same durable database.
    restarted = await _build_core(core.tmp_path)
    restarted.provider.cleanup_response = _transient_cleanup_failure()
    assert await restarted.repository.predecessor_cleanup_barrier(b.id) is True
    restarted.provider.responses.append(restarted.provider.parcel("box", state=ResourceState.AVAILABLE))
    for _ in range(3):
        await restarted.engine.resolve_pending()
        restarted.clock.advance(6)
    assert await restarted.repository.resources(b.id) == ()     # still fenced after restart


@pytest.mark.asyncio
async def test_crashed_cleanup_claim_is_reclaimed_on_restart_and_fence_still_holds(core):
    a = await _resolve_transfer(core, fingerprint="btih-abc", name="A")
    core.provider.cleanup_response = _transient_cleanup_failure()   # cleanup stays owed
    await core.engine.delete(a.id, remote=True)
    b = await core.engine.submit((_magnet(),), name="B")

    # Simulate a claim that a crash interrupted: blocked=1, not abandoned.
    async with database.get_db() as db:
        await db.execute(
            "UPDATE provider_resources SET cleanup_blocked=1 WHERE transfer_id=?", (a.id,))
        await db.commit()
    assert await core.repository.predecessor_cleanup_barrier(b.id) is True   # blocks, conservatively

    restarted = await _build_core(core.tmp_path)                # initialize() runs reclaim
    async with database.get_db() as db:
        row = await db.fetchone(
            "SELECT cleanup_blocked, cleanup_abandoned, cleanup_authority FROM provider_resources WHERE transfer_id=?",
            (a.id,))
    assert row["cleanup_blocked"] == 0 and row["cleanup_abandoned"] == 0     # claim released for re-drive
    assert row["cleanup_authority"] is not None
    assert await restarted.repository.predecessor_cleanup_barrier(b.id) is True   # still owed → still fenced

    restarted.provider.cleanup_response = TransferOutcome(OutcomeKind.SUCCESS)
    for _ in range(6):
        await restarted.engine.cleanup_pending()
        restarted.clock.advance(6)
    assert await restarted.repository.predecessor_cleanup_barrier(b.id) is False  # released after real completion


@pytest.mark.asyncio
async def test_terminally_abandoned_predecessor_cleanup_releases_the_fence(core):
    a = await _resolve_transfer(core, fingerprint="btih-abc", name="A")
    binding_a = (await _provider_resource_rows(a.id))[0]["id"]
    core.provider.cleanup_response = _transient_cleanup_failure()    # cleanup stays owed
    await core.engine.delete(a.id, remote=True)

    # Terminal give-up is recorded only after a cleanup call returns.
    await core.repository.cleanup_retry(binding_a, _transient_cleanup_failure().error, None)
    async with database.get_db() as db:
        row = await db.fetchone(
            "SELECT cleanup_abandoned, cleanup_authority FROM provider_resources WHERE id=?", (binding_a,))
    assert row["cleanup_abandoned"] == 1 and row["cleanup_authority"] is not None

    b = await core.engine.submit((_magnet(),), name="B")
    assert await core.repository.predecessor_cleanup_barrier(b.id) is False
    core.provider.responses.append(core.provider.parcel("box", state=ResourceState.AVAILABLE))
    for _ in range(6):
        await core.engine.resolve_pending()
        core.clock.advance(6)
    rows_b = await _provider_resource_rows(b.id)
    assert rows_b and all(r["transfer_id"] == b.id for r in rows_b)   # B proceeds, no deadlock
    assert (await core.repository.get(b.id)).error is None
    assert len(await _provider_resource_rows()) == 2            # A's historical row still present


# --------------------------------------------------------------------------- #
# E. Executor cleanup stays bound to the predecessor
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_predecessor_executor_cleanup_stays_scoped_to_the_old_transfer(core):
    a = await core.engine.submit((_magnet(),), name="A")
    await core.engine.tick()
    artifact_a = (await core.repository.artifacts(a.id))[0]
    assert artifact_a.execution is not None

    await core.engine.delete(a.id, remote=False)
    b = await core.engine.submit((_magnet(),), name="B")
    for _ in range(3):
        await core.engine.tick()
        core.clock.advance(6)

    async with database.get_db() as db:
        attempts = await db.fetchall(
            "SELECT transfer_id, cleanup_state FROM execution_attempts WHERE cleanup_state IS NOT NULL")
    assert attempts and {r["transfer_id"] for r in attempts} == {a.id}
    b_ids = {art.id for art in await core.repository.artifacts(b.id)}
    assert b_ids and b_ids.isdisjoint({artifact_a.id})


# --------------------------------------------------------------------------- #
# F. Fresh file-selection generation across delete/re-add
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_readd_starts_a_fresh_file_selection_generation(core):
    files = [("e1", "s/e1", 10), ("e2", "s/e2", 20), ("e3", "s/e3", 30)]
    core.provider.responses.append(core.provider.parcel("box", state=ResourceState.AVAILABLE, files=files))
    a = await core.engine.submit((_magnet("fp-fs"),), name="A")
    await core.engine.resolve_pending()
    view_a = await core.repository.file_selection_presentation(a.id, now=core.clock())
    await core.repository.confirm_file_selection(
        a.id, view_a["manifest_id"], [view_a["entries"][0]["entry_id"]], now=core.clock())

    core.clock.advance(10)
    await core.engine.delete(a.id, remote=False)

    core.provider.responses.append(core.provider.parcel("box", state=ResourceState.AVAILABLE, files=files))
    b = await core.engine.submit((_magnet("fp-fs"),), name="B")
    assert b.id != a.id
    await core.engine.resolve_pending()
    view_b = await core.repository.file_selection_presentation(b.id, now=core.clock())
    assert view_b["selection_id"] != view_a["selection_id"]
    assert view_b["decision"] == "pending" and view_b["selected_entry_ids"] == []

    async with database.get_db() as db:
        gen_a = await db.fetchone(
            "SELECT decision FROM transfer_file_selections WHERE id=?", (view_a["selection_id"],))
    assert gen_a["decision"] == "explicit"                      # A's history intact


# --------------------------------------------------------------------------- #
# G. Concurrent re-add
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_concurrent_readd_creates_exactly_one_fresh_active_generation(repo):
    a, _ = await repo.admit((_magnet(),), name="A")
    await repo.delete(a.id, remote=True)

    results = await asyncio.gather(*(repo.admit((_magnet(),), name=f"B{i}") for i in range(6)))
    ids = {transfer.id for transfer, _created in results}
    assert len(ids) == 1 and a.id not in ids
    assert [created for _t, created in results].count(True) == 1

    active = [r for r in await _torrents() if r["status"] != "deleted"]
    assert len(active) == 1 and active[0]["hash"] == "btih-abc"


# --------------------------------------------------------------------------- #
# H. Non-deleted dedupe behavior is unchanged
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_active_and_completed_dedupe_behavior_is_unchanged(repo):
    a, created_a = await repo.admit((_magnet(),), name="A")
    assert created_a is True

    again, created_again = await repo.admit((_magnet(),), name="A-again")
    assert created_again is False and again.id == a.id

    async with database.get_db() as db:
        await db.execute("UPDATE torrents SET status='completed' WHERE id=?", (a.id,))
        await db.commit()
    after_complete, created_after = await repo.admit((_magnet(),), name="A-complete")
    assert created_after is False and after_complete.id == a.id
    row = (await _torrents("WHERE id=?", (a.id,)))[0]
    assert row["hash"] == "btih-abc"


# --------------------------------------------------------------------------- #
# Inventory / observation of the canonical resource still matches its binding
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_inventory_observation_matches_the_binding_without_changing_canonical_identity(core):
    a = await _resolve_transfer(core, fingerprint="fp-inv", name="A")
    canonical = (await core.repository.resources(a.id))[0][0]
    binding_before = (await _provider_resource_rows(a.id))[0]["id"]

    # An inventory/observation carrying the canonical resource id must update the
    # existing binding row, not create a second one.
    await core.repository.resource_observation(a.id, canonical, ResourceState.AVAILABLE)
    rows = await _provider_resource_rows(a.id)
    assert len(rows) == 1 and rows[0]["id"] == binding_before
    assert rows[0]["resource_key"] == canonical.id             # canonical identity unchanged
