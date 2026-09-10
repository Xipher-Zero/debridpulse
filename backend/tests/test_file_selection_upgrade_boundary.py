"""Phase B — upgrade-boundary invariant for the new ``selection_mode`` gate.

Torrent/Magnet File-Selection Lifecycle Correction §6 / §13, operator HOLD.

``selection_mode`` is new. Pre-implementation 1.0.12 ``TransferRequest``
payloads contain no ``selection_mode`` key and therefore deserialize with the
default ``selection_mode="all"``. A new submission policy may control whether a
NEW selection generation is created; it may NEVER invalidate or bypass a durable
selection generation that already exists.

Implementation rule under test: ``selection_mode`` gates generation *creation*
only (``engine._after_resolution_persisted``). Every engine step past that point
— manifest recording, selection gating, Confirm/Close/timeout, executable
manifest filtering — checks generation EXISTENCE
(``repository.selection_generation_exists``), never the request's
current/defaulted policy field.

Each test constructs a *genuinely pre-existing* generation: created, then the
owning request's stored payload is stripped of ``selection_mode`` and the
generation row is reshaped to its pre-correction form (no ``available_at``
anchor, the old submission-relative ``manifest_wait_until``), then the engine is
restarted fresh on the same database.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import ParcelProvider
from file_selection_support import Clock, file_manifest
from transfers import file_selection as fs
from transfers.engine import TransferEngine
from transfers.models import ResourceState, TransferRequest
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository

from test_file_selection_lifecycle import RecordingExecutor

FILES = [
    ("e1.mkv", "S1/e1.mkv", 10), ("e2.mkv", "S1/e2.mkv", 20), ("e3.mkv", "S1/e3.mkv", 30),
    ("e4.mkv", "S1/e4.mkv", 40), ("e5.mkv", "S1/e5.mkv", 50), ("e6.mkv", "S1/e6.mkv", 60),
]


def _build_engine(tmp_path, clock):
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider(identity="parcel-lab", file_manifest=True)
    executor = RecordingExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(adoption_stability_seconds=0, resource_poll_interval=5,
                              retry_delay=0, resolution_retry_delay=0,
                              max_active_executions=8, max_attempts=8),
        clock=clock,
    )
    return SimpleNamespace(engine=engine, repository=repository, registry=registry,
                           provider=provider, executor=executor, clock=clock)


@pytest_asyncio.fixture
async def core(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "fs-upgrade.db")
    await database.init_db()
    c = _build_engine(tmp_path, Clock(1000.0))
    await c.engine.initialize()
    c.tmp_path = tmp_path
    return c


async def _restart(core):
    """A fresh engine + repository on the same database and clock — an upgrade
    restart into this build."""
    fresh = _build_engine(core.tmp_path, core.clock)
    await fresh.engine.initialize()
    fresh.tmp_path = core.tmp_path
    return fresh


async def _submit(core, *, payload="box", selection_mode="interactive"):
    return await core.engine.submit(
        (TransferRequest("parcel", payload, name="show", fingerprint="fp-" + payload,
                         selection_mode=selection_mode),),
        deduplicate=False)


async def _root_request_id(transfer_id):
    async with database.get_db() as db:
        row = await db.fetchone(
            "SELECT id FROM transfer_requests WHERE transfer_id=? AND parent_id IS NULL LIMIT 1",
            (transfer_id,))
    return row["id"]


async def _selection_row(transfer_id):
    async with database.get_db() as db:
        return await db.fetchone(
            "SELECT * FROM transfer_file_selections WHERE transfer_id=? ORDER BY created_at DESC LIMIT 1",
            (transfer_id,))


async def _members(core, transfer_id):
    return [r for r in await core.repository.requests(transfer_id) if r.parent_id]


async def _make_legacy(transfer_id):
    """Reshape durable state to exactly how it would be on a database created
    before ``selection_mode`` existed: the owning request payload has no
    ``selection_mode`` key, and the generation row has no ``available_at``
    anchor plus the retired submission-relative ``manifest_wait_until`` value."""
    request_id = await _root_request_id(transfer_id)
    async with database.get_db() as db:
        row = await db.fetchone("SELECT payload FROM transfer_requests WHERE id=?", (request_id,))
        payload = json.loads(row["payload"])
        assert payload.pop("selection_mode", None) is not None      # was present
        await db.execute(
            "UPDATE transfer_requests SET payload=? WHERE id=?",
            (json.dumps(payload, separators=(",", ":"), sort_keys=True), request_id))
        gen = await db.fetchone(
            "SELECT id, created_at FROM transfer_file_selections WHERE transfer_id=?", (transfer_id,))
        await db.execute(
            "UPDATE transfer_file_selections SET available_at=NULL, manifest_wait_until=? WHERE id=?",
            (float(gen["created_at"]) + 60.0, gen["id"]))
        await db.commit()
    # The request now deserializes with the default policy — genuinely legacy.
    async with database.get_db() as db:
        check = await db.fetchone("SELECT payload FROM transfer_requests WHERE id=?", (request_id,))
    assert "selection_mode" not in json.loads(check["payload"])


# --------------------------------------------------------------------------- #
# Case 1 — legacy request + existing PENDING active hold survives restart
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_legacy_pending_hold_survives_restart_and_still_governs(core):
    core.provider.responses.append(
        core.provider.parcel("box", state=ResourceState.AVAILABLE, files=FILES))
    transfer = await _submit(core)
    await core.engine.resolve_pending()
    original = await _selection_row(transfer.id)
    assert original["decision"] == "pending" and original["hold_until"] is not None
    hold = original["hold_until"]

    await _make_legacy(transfer.id)
    fresh = await _restart(core)
    fresh.provider.members["parcel-lab:box"] = core.provider.members.get("parcel-lab:box")
    fresh.provider.resources["parcel-lab:box"] = core.provider.resources["parcel-lab:box"]

    # The restarted engine's request deserializes as selection_mode="all", but
    # the durable PENDING hold is still authoritative: nothing materialises.
    for _ in range(4):
        await fresh.engine.resolve_pending()
        core.clock.advance(1)
    row = await _selection_row(transfer.id)
    assert row["decision"] == "pending"
    assert row["hold_until"] == hold                               # never reset / extended
    assert row["manifest_committed_at"] is None
    assert await _members(fresh, transfer.id) == []
    assert ("manifest", "parcel-lab:box") not in fresh.provider.calls
    request_id = await _root_request_id(transfer.id)
    assert await fresh.repository.file_selection_gate(
        request_id, row["provider_resource_id"], now=core.clock(),
        resource_available=True) == fs.SelectionGate.WAIT_FOR_DECISION

    # And it still times out to ALL when the persisted deadline passes.
    core.clock.set(hold + 1)
    for _ in range(4):
        await fresh.engine.resolve_pending()
        core.clock.advance(1)
    row = await _selection_row(transfer.id)
    assert row["decision"] == "all" and row["decision_reason"] == fs.DecisionReason.DECISION_TIMEOUT
    assert sorted(r.entry.relative_path for r in await _members(fresh, transfer.id)) == sorted(f[1] for f in FILES)


# --------------------------------------------------------------------------- #
# Case 2 — legacy request + existing EXPLICIT subset + PREPARING → AVAILABLE
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_legacy_explicit_subset_materialises_only_the_subset_after_restart(core):
    from dataclasses import replace as _replace

    prepare = core.provider.parcel("box", state=ResourceState.PREPARING)
    core.provider.members["parcel-lab:box"] = None
    core.provider.responses.append(prepare)
    transfer = await _submit(core)
    await core.engine.resolve_pending()

    # A manifest arrives while still PREPARING; the user confirms {e2, e5}.
    core.clock.advance(10)
    core.provider.resources["parcel-lab:box"] = _replace(
        prepare.observation, file_manifest=file_manifest(*FILES))
    await core.engine.resolve_pending()
    view = await core.repository.file_selection_presentation(transfer.id, now=core.clock())
    keep = [view["entries"][1]["entry_id"], view["entries"][4]["entry_id"]]
    result = await core.repository.confirm_file_selection(transfer.id, view["manifest_id"], keep, now=core.clock())
    assert result.outcome == fs.SelectionOutcome.CONFIRMED
    assert (await _selection_row(transfer.id))["decision"] == "explicit"

    await _make_legacy(transfer.id)
    fresh = await _restart(core)
    # Provider is still PREPARING after the restart.
    fresh.provider.members["parcel-lab:box"] = None
    fresh.provider.resources["parcel-lab:box"] = _replace(
        prepare.observation, file_manifest=file_manifest(*FILES))
    core.clock.advance(20)
    for _ in range(4):
        await fresh.engine.resolve_pending()
        core.clock.advance(1)
    assert await _members(fresh, transfer.id) == []               # not AVAILABLE yet, nothing runs
    assert (await _selection_row(transfer.id))["decision"] == "explicit"   # decision durable

    # Provider becomes AVAILABLE — ONLY the confirmed subset may materialise.
    from file_selection_support import executable
    fresh.provider.members["parcel-lab:box"] = executable(*FILES)
    fresh.provider.resources["parcel-lab:box"] = _replace(
        prepare.observation, state=ResourceState.AVAILABLE, file_manifest=file_manifest(*FILES))
    core.clock.advance(5)
    for _ in range(8):
        await fresh.engine.resolve_pending()
        core.clock.advance(1)

    members = sorted(r.entry.relative_path for r in await _members(fresh, transfer.id))
    assert members == ["S1/e2.mkv", "S1/e5.mkv"]                   # subset only, never all six
    artifacts = sorted(a.name for a in await fresh.repository.artifacts(transfer.id))
    assert artifacts == ["e2.mkv", "e5.mkv"]
    assert (await _selection_row(transfer.id))["manifest_committed_at"] is not None


# --------------------------------------------------------------------------- #
# Case 3 — legacy request + existing PREPARING / no-manifest generation
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_legacy_preparing_no_manifest_generation_keeps_the_selection_opportunity(core):
    from dataclasses import replace as _replace

    prepare = core.provider.parcel("box", state=ResourceState.PREPARING)
    core.provider.members["parcel-lab:box"] = None
    core.provider.responses.append(prepare)
    transfer = await _submit(core)
    await core.engine.resolve_pending()
    gen = await _selection_row(transfer.id)
    assert gen is not None and gen["manifest_id"] is None and gen["hold_until"] is None

    await _make_legacy(transfer.id)
    row = await _selection_row(transfer.id)
    assert row["available_at"] is None and row["manifest_wait_until"] > 0   # retired legacy value

    fresh = await _restart(core)
    fresh.provider.members["parcel-lab:box"] = None
    fresh.provider.resources["parcel-lab:box"] = _replace(prepare.observation)

    # Long PREPARING after the restart: the stale submission-relative
    # manifest_wait_until must NOT convert this to ALL.
    core.clock.set(1000.0 + 400)
    for _ in range(4):
        await fresh.engine.resolve_pending()
        core.clock.advance(1)
    row = await _selection_row(transfer.id)
    assert row["decision"] == "pending"
    assert row["hold_until"] is None
    assert await _members(fresh, transfer.id) == []

    # A multi-file manifest finally arrives (still PREPARING) — the restarted
    # engine records it into the pre-existing generation and opens the 120s hold
    # anchored to the manifest's observed_at, not to submission or the restart.
    fresh.provider.resources["parcel-lab:box"] = _replace(
        prepare.observation, file_manifest=file_manifest(*FILES))
    for _ in range(3):
        await fresh.engine.resolve_pending()
        core.clock.advance(1)
    row = await _selection_row(transfer.id)
    assert row["manifest_id"] is not None
    async with database.get_db() as db:
        observed = await db.fetchone(
            "SELECT observed_at FROM transfer_file_manifests WHERE id=?", (row["manifest_id"],))
    assert row["hold_until"] == float(observed["observed_at"]) + fs.IMMEDIATE_DECISION_HOLD_SECONDS
    assert row["hold_until"] > 1000.0 + 400                       # anchored well after submission
    assert row["auto_offer_queued_at"] is not None
    view = await fresh.repository.file_selection_presentation(transfer.id, now=core.clock())
    assert view["auto_offer"] is True and view["mutable"] is True


# --------------------------------------------------------------------------- #
# Case 4 — a genuinely NEW submission that omits selection_mode is unchanged
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_new_submission_without_selection_mode_is_plain_all_no_generation(core):
    core.provider.responses.append(
        core.provider.parcel("box", state=ResourceState.AVAILABLE, files=FILES))
    # No legacy reshaping — this request is genuinely new and simply defaults.
    transfer = await core.engine.submit(
        (TransferRequest("parcel", "box", name="show", fingerprint="fp-new"),),
        deduplicate=False)
    assert transfer is not None
    for _ in range(6):
        await core.engine.resolve_pending()
        core.clock.advance(1)

    assert await _selection_row(transfer.id) is None              # no interactive generation
    async with database.get_db() as db:
        events = await db.fetchall(
            "SELECT 1 FROM application_events WHERE transfer_id=? AND kind='file_selection_available'",
            (transfer.id,))
    assert events == []
    members = sorted(r.entry.relative_path for r in await _members(core, transfer.id))
    assert members == sorted(f[1] for f in FILES)                 # plain ALL


# --------------------------------------------------------------------------- #
# Case 5 — a legacy transfer re-resolved onto a new provider resource stays
# interactive (transfer_has_selection_generation), never inheriting the subset
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_legacy_transfer_reresolution_opens_a_fresh_generation_for_the_new_binding(core):
    core.provider.responses.append(
        core.provider.parcel("box", state=ResourceState.AVAILABLE, files=FILES))
    transfer = await _submit(core)
    await core.engine.resolve_pending()
    first = await _selection_row(transfer.id)
    assert first["decision"] == "pending"

    await _make_legacy(transfer.id)
    request_id = await _root_request_id(transfer.id)

    # Simulate the provider resource being lost so the request re-resolves.
    async with database.get_db() as db:
        await db.execute(
            "UPDATE transfer_requests SET state='pending', resource=NULL, retry_at=0 WHERE id=?", (request_id,))
        await db.commit()

    fresh = await _restart(core)
    fresh.provider.responses.append(
        fresh.provider.parcel("box2", state=ResourceState.AVAILABLE, files=FILES))
    core.clock.advance(50)
    await fresh.engine.resolve_pending()

    # transfer_has_selection_generation() is true, so the re-resolution opens a
    # fresh generation for the new binding even though the request now
    # deserializes as selection_mode="all".
    async with database.get_db() as db:
        rows = await db.fetchall(
            "SELECT provider_resource_id, decision FROM transfer_file_selections WHERE transfer_id=? ORDER BY created_at",
            (transfer.id,))
    assert len(rows) == 2
    assert rows[1]["provider_resource_id"] != rows[0]["provider_resource_id"]
    assert rows[1]["decision"] == "pending"                       # fresh, inherits nothing
