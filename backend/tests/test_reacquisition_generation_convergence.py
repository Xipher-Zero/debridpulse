"""Reacquiring a terminal transfer across a semantic upgrade boundary.

A completed transfer whose delivered material was removed can be legitimately
reacquired by the operator's Retry onto the same logical transfer (a later
independent submission is a new lineage instead, never this). Everything
about that acquisition is NEW -- a fresh provider resource, a fresh manifest,
fresh candidates -- but the transfer still carries the child requests and
artifacts a PREVIOUS generation established. Two durable facts made the old
generation outrank the new one:

* A member's canonical identity is ``uuid5(parent, relative_path)``, while its
  slot is ``UNIQUE(transfer_id,parent_id,ordinal)``. A provider that changes a
  member's canonical coordinate therefore publishes a child with a NEW identity
  competing for an ordinal a superseded child still held, so the fan-out's
  ``INSERT OR IGNORE`` silently dropped it. The current generation could never
  establish its own members, and the transfer churned against coordinates no
  current manifest contains.

* An artifact's durable target was carried forward purely because the row
  existed, so a member whose coordinate moved for ANY reason kept writing to
  the coordinate an earlier generation chose.

Both are generation defects, not path defects: nothing here inspects a path.

TASK3d-3a0 keeps this convergence for exactly the decompositions it was built
for -- ones persisted BEFORE decomposition-generation authority existed -- and
nothing else. The 3a0 upgrade marks such a decomposition's generation as
COMPATIBILITY LINEAGE (``legacy_established``): a pre-3a0 boundary, deliberately
not a claim that it predates any particular coordinate correction, which is not
durably recoverable. When that work is reacquired as terminal work, it may cross
into the generation model ONCE by this reconstruction, under the frozen
collection root; the crossing is consumed and never carried forward. A
decomposition established under the current model whose replacement cannot be
proven one-to-one holds instead (the current-model controls below), and so does
a legacy decomposition rebound by anything but a terminal reacquisition.

History is never rewritten. A superseded child keeps its row, its artifact, its
execution attempts and all of its provenance; it stops being current work.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from test_resubmission_lifecycle_invariants import build_engine, rows
from transfers.models import ProviderObservation, ResourceState, TransferRequest, TransferState

FINGERPRINT = "reacquisition-fp"
SUBMITTED_NAME = "Collection"

#: The member as a PRE-upgrade manifest published it: the collection root is
#: present inside the member's own relative path as well as on the transfer.
LEGACY_MANIFEST = [("payload.bin", "Parcel/payload.bin", 4)]
#: The same logical member under current canonical manifest truth.
CURRENT_MANIFEST = [("payload.bin", "payload.bin", 4)]


@pytest_asyncio.fixture
async def core(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "reacquire.db")
    await database.init_db()
    built = build_engine(tmp_path, ParcelProvider(file_manifest=True))
    await built.engine.initialize()
    return built


def source(payload="box"):
    return TransferRequest("parcel", payload, name=SUBMITTED_NAME,
                           fingerprint=FINGERPRINT, selection_mode="all")


async def requests_of(transfer_id):
    return await rows(
        "SELECT id,parent_id,ordinal,state,attempts FROM transfer_requests WHERE transfer_id=? ORDER BY ordinal,id",
        (transfer_id,))


async def children_of(transfer_id):
    """A child's ordinal restarts at 0, so a level-blind index is not an identity."""
    return await rows(
        "SELECT id,parent_id,ordinal,state,attempts FROM transfer_requests "
        "WHERE transfer_id=? AND parent_id IS NOT NULL ORDER BY ordinal,id", (transfer_id,))


async def artifacts_of(transfer_id):
    return await rows(
        "SELECT id,request_id,local_path,status,blocked,block_reason,execution_attempt_id "
        "FROM download_files WHERE torrent_id=? ORDER BY id", (transfer_id,))


async def settle(core, transfer_id, cycles=6):
    """Drive ordinary lifecycle cycles, finishing whatever the fake starts."""
    for _ in range(cycles):
        await core.engine.tick()
        for attempt in await core.repository.executions(transfer_id):
            if attempt.state not in {"succeeded", "failed", "absent", "cancelled"}:
                core.executor.finish(attempt.handle)
        core.clock.advance(5)
        if (await core.repository.get(transfer_id)).state == TransferState.COMPLETED:
            break
    return await core.repository.get(transfer_id)


async def acquire(core, manifest, payload="box"):
    """One complete acquisition: resolve, fan out, execute, complete."""
    core.provider.responses.append(
        core.provider.parcel(payload, state=ResourceState.AVAILABLE, files=manifest))
    transfer = await core.engine.submit((source(payload),), name=SUBMITTED_NAME)
    await core.engine.resolve_pending()
    await settle(core, transfer.id)
    return transfer


async def material_removed(core, transfer_id):
    """The delivered payload is gone from disk, and the provider resource with it."""
    for row in await artifacts_of(transfer_id):
        path = Path(row["local_path"])
        if path.exists():
            path.unlink()
    for identity, observed in list(core.provider.resources.items()):
        core.provider.resources[identity] = ProviderObservation(observed.resource, ResourceState.ABSENT)


def _observation(core, payload, manifest, observed_name):
    """The provider's own statement about the renewed resource."""
    result = core.provider.parcel(payload, state=ResourceState.AVAILABLE, files=manifest)
    if observed_name is None:
        return result
    from dataclasses import replace as _replace
    renamed = _replace(result.observation, name=observed_name)
    core.provider.resources[renamed.resource.id] = renamed
    return _replace(result, observation=renamed)


async def upgraded_from_before_generations(core):
    """The database crosses the 3a0 upgrade boundary with this transfer's
    decomposition already persisted: the one-time upgrade marks the generation
    governing it as compatibility lineage (``legacy_established``)."""
    import aiosqlite
    from db.migrations.v113_decomposition_generations import (
        MARKER,
        backfill_decomposition_generations,
    )
    async with database.get_db() as db:
        await db.execute("DELETE FROM transfer_controls WHERE key=?", (MARKER,))
        await db.commit()
    async with aiosqlite.connect(database.DB_PATH) as raw:
        await backfill_decomposition_generations(raw)


async def reacquire(core, manifest, *, renewed="box2", cycles=6, observed_name=None):
    """The operator reacquiring the SAME logical transfer -- Retry of the
    completed transfer -- while the provider answers with a genuinely new
    resource carrying current manifest truth."""
    core.provider.responses.append(_observation(core, renewed, manifest, observed_name))
    async with database.get_db() as db:
        row = await db.fetchone("SELECT id FROM torrents WHERE hash=?", (FINGERPRINT,))
    assert await core.engine.retry(int(row["id"]), reacquire=True)
    transfer = await core.repository.get(int(row["id"]))
    for _ in range(cycles):
        core.provider.responses.append(_observation(core, renewed, manifest, observed_name))
        await core.engine.tick()
        for attempt in await core.repository.executions(transfer.id):
            if attempt.state not in {"succeeded", "failed", "absent", "cancelled"}:
                core.executor.finish(attempt.handle)
        core.clock.advance(5)
        if (await core.repository.get(transfer.id)).state == TransferState.COMPLETED:
            break
    return transfer


# --- 20.1  legacy pre-upgrade reacquisition ---------------------------------

@pytest.mark.asyncio
async def test_legacy_generation_reacquires_onto_current_manifest_truth(core):
    """The whole regression narrative, driven only through the lifecycle.

    Nothing here rewrites a path before the run: the two generations differ
    only in what the PROVIDER publishes as the member's canonical coordinate,
    which is exactly the upgrade boundary.
    """
    first = await acquire(core, LEGACY_MANIFEST)
    await upgraded_from_before_generations(core)
    assert (await core.repository.get(first.id)).state == TransferState.COMPLETED
    legacy = (await artifacts_of(first.id))[0]
    legacy_child = (await children_of(first.id))[0]
    legacy_execution = legacy["execution_attempt_id"]
    # The pre-upgrade coordinate carries the collection root twice.
    assert legacy["local_path"].endswith("Parcel/Parcel/payload.bin")

    await material_removed(core, first.id)
    again = await reacquire(core, CURRENT_MANIFEST)

    # Same logical transfer, converged.
    assert again.id == first.id
    settled = await core.repository.get(first.id)
    assert settled.state == TransferState.COMPLETED
    assert settled.progress == 100

    artifacts = await artifacts_of(first.id)
    current = [row for row in artifacts if not row["blocked"]]
    assert len(current) == 1
    # Derived by the canonical destination owner from current manifest truth --
    # one root, and never produced by editing the old string.
    assert current[0]["local_path"].endswith("Parcel/payload.bin")
    assert not current[0]["local_path"].endswith("Parcel/Parcel/payload.bin")
    assert current[0]["status"] == "completed"
    # A new writer was genuinely admitted for the new generation.
    assert current[0]["execution_attempt_id"] not in {None, legacy_execution}

    # History is intact and no longer votes.
    superseded = [row for row in artifacts if row["blocked"]]
    assert len(superseded) == 1
    assert superseded[0]["id"] == legacy["id"]
    assert superseded[0]["local_path"] == legacy["local_path"]
    assert superseded[0]["block_reason"] == "superseded_generation"
    retired = next(row for row in await requests_of(first.id) if row["id"] == legacy_child["id"])
    assert retired["state"] == "skipped"


@pytest.mark.asyncio
async def test_reacquisition_does_not_churn_resolution_attempts(core):
    """Section 11: convergence, not a capped loop.

    The transfer reaches completion, and the superseded child stops consuming
    resolution attempts entirely once it is no longer current work.
    """
    first = await acquire(core, LEGACY_MANIFEST)
    await upgraded_from_before_generations(core)
    legacy_child = (await children_of(first.id))[0]
    await material_removed(core, first.id)
    await reacquire(core, CURRENT_MANIFEST)
    assert (await core.repository.get(first.id)).state == TransferState.COMPLETED

    before = next(row for row in await requests_of(first.id) if row["id"] == legacy_child["id"])["attempts"]
    for _ in range(8):
        await core.engine.tick()
        core.clock.advance(30)
    after = next(row for row in await requests_of(first.id) if row["id"] == legacy_child["id"])["attempts"]
    assert after == before
    assert (await core.repository.get(first.id)).state == TransferState.COMPLETED
    # And no second artifact was manufactured for the current member.
    assert len([row for row in await artifacts_of(first.id) if not row["blocked"]]) == 1


# --- 20.2  modern control case ----------------------------------------------

@pytest.mark.asyncio
async def test_modern_reacquisition_keeps_its_canonical_target_untouched(core):
    """A transfer whose coordinate is already canonical must not move at all."""
    first = await acquire(core, CURRENT_MANIFEST)
    assert (await core.repository.get(first.id)).state == TransferState.COMPLETED
    before = (await artifacts_of(first.id))[0]
    child_before = (await children_of(first.id))[0]

    await material_removed(core, first.id)
    again = await reacquire(core, CURRENT_MANIFEST)

    assert again.id == first.id
    assert (await core.repository.get(first.id)).state == TransferState.COMPLETED
    artifacts = await artifacts_of(first.id)
    # Exactly one artifact, the same row, at the same coordinate: no duplicate
    # row for the same logical member, and no gratuitous retarget.
    assert len(artifacts) == 1
    assert artifacts[0]["id"] == before["id"]
    assert artifacts[0]["local_path"] == before["local_path"]
    assert not artifacts[0]["blocked"]
    # The same logical child, reused rather than superseded.
    children = await children_of(first.id)
    assert [row["id"] for row in children] == [child_before["id"]]
    assert children[0]["state"] != "skipped"
    # It did reacquire: a new writer ran.
    assert artifacts[0]["execution_attempt_id"] != before["execution_attempt_id"]


# --- 20.7  the coordinate moves for a reason that is not a doubled root -----

@pytest.mark.asyncio
async def test_a_renamed_provider_report_never_moves_a_member(core):
    """The member's coordinate is unchanged, so it is the same logical member
    and reacquires in place -- and although the provider now reports the
    collection under another name (which the display name follows), the member
    is rebuilt under the collection folder frozen at the first fan-out."""
    first = await acquire(core, CURRENT_MANIFEST)
    before = (await artifacts_of(first.id))[0]
    child_before = (await children_of(first.id))[0]
    assert before["local_path"].endswith("Parcel/payload.bin")

    await material_removed(core, first.id)
    await reacquire(core, CURRENT_MANIFEST, renewed="box2", observed_name="Parcel Renamed")

    artifacts = await artifacts_of(first.id)
    assert len(artifacts) == 1 and artifacts[0]["id"] == before["id"]
    assert [row["id"] for row in await children_of(first.id)] == [child_before["id"]]
    assert artifacts[0]["local_path"] == before["local_path"]                 # never relocated
    assert artifacts[0]["execution_attempt_id"] != before["execution_attempt_id"]   # it did reacquire
    transfer = await core.repository.get(first.id)
    assert transfer.state == TransferState.COMPLETED
    assert (transfer.name, transfer.collection_root) == ("Parcel Renamed", "Parcel")


# --- 20.3  live writers are never retargeted underneath themselves ----------

@pytest.mark.asyncio
async def test_a_live_writer_keeps_its_target_and_its_slot(core):
    """A running execution owns its coordinate. A newer manifest generation may
    not move it, may not retire its member, and may not take its ordinal."""
    core.provider.responses.append(
        core.provider.parcel("box", state=ResourceState.AVAILABLE, files=CURRENT_MANIFEST))
    transfer = await core.engine.submit((source(),), name=SUBMITTED_NAME)
    await core.engine.resolve_pending()
    await core.engine.tick()

    live = (await artifacts_of(transfer.id))[0]
    assert live["execution_attempt_id"] and live["status"] not in {"completed", "error"}
    child = (await children_of(transfer.id))[0]

    # A newer generation arrives naming the member under a different canonical
    # coordinate while that writer is still running.
    record = next(item for item in await core.repository.requests(transfer.id) if item.parent_id is None)
    from transfers.models import SourceEntry
    moved = (SourceEntry("payload.bin", 4, "elsewhere/payload.bin",
                         TransferRequest("parcel-member", "box:elsewhere/payload.bin", name="payload.bin")),)
    await core.repository.manifest(record, moved)

    after = await artifacts_of(transfer.id)
    still = next(row for row in after if row["id"] == live["id"])
    assert still["local_path"] == live["local_path"], "a live writer was retargeted"
    assert still["execution_attempt_id"] == live["execution_attempt_id"]
    assert not still["blocked"], "a live member was retired underneath its writer"
    held = next(row for row in await children_of(transfer.id) if row["id"] == child["id"])
    assert held["state"] != "skipped"
    assert held["ordinal"] == 0, "a live member's slot was taken"


@pytest.mark.asyncio
async def test_the_slot_is_released_once_the_writer_is_gone(core):
    """The handshake: retirement first, then the current generation proceeds."""
    core.provider.responses.append(
        core.provider.parcel("box", state=ResourceState.AVAILABLE, files=CURRENT_MANIFEST))
    transfer = await core.engine.submit((source(),), name=SUBMITTED_NAME)
    await core.engine.resolve_pending()
    await core.engine.tick()
    live = (await artifacts_of(transfer.id))[0]
    record = next(item for item in await core.repository.requests(transfer.id) if item.parent_id is None)

    from transfers.models import SourceEntry
    moved = (SourceEntry("payload.bin", 4, "elsewhere/payload.bin",
                         TransferRequest("parcel-member", "box:elsewhere/payload.bin", name="payload.bin")),)
    await core.repository.manifest(record, moved)
    assert len(await children_of(transfer.id)) == 1, "the new member must wait for the live one"

    # The writer genuinely stops, exactly as STALE retirement makes it.
    await core.repository.artifact_state(live["id"], "unresolved", release=True)
    async with database.get_db() as db:
        await db.execute("UPDATE execution_attempts SET state='cancelled',authorized=0 WHERE id=?",
                         (live["execution_attempt_id"],))
        await db.commit()

    await core.repository.manifest(record, moved)
    children = await children_of(transfer.id)
    assert len(children) == 2, "the current generation must establish its member once the slot is free"
    assert sorted(row["ordinal"] for row in children) == [0, 1]


# --- 20.4  historical execution and provenance retention --------------------

@pytest.mark.asyncio
async def test_historical_executions_and_provenance_survive_reacquisition(core):
    first = await acquire(core, LEGACY_MANIFEST)
    await upgraded_from_before_generations(core)
    historical = await core.repository.executions(first.id)
    assert len(historical) == 1
    historical_id = historical[0].handle.attempt_id
    provenance_before = await rows(
        "SELECT COUNT(*) AS n FROM route_attempt_provenance WHERE transfer_id=?", (first.id,))

    await material_removed(core, first.id)
    await reacquire(core, CURRENT_MANIFEST)

    attempts = await core.repository.executions(first.id)
    assert historical_id in {item.handle.attempt_id for item in attempts}
    assert len(attempts) >= 2, "the new generation ran its own writer"
    rows_ = await rows("SELECT id,authorized,state FROM execution_attempts WHERE id=?", (historical_id,))
    assert rows_[0]["authorized"] == 0, "a historical attempt must never be reauthorized"
    provenance_after = await rows(
        "SELECT COUNT(*) AS n FROM route_attempt_provenance WHERE transfer_id=?", (first.id,))
    assert provenance_after[0]["n"] >= provenance_before[0]["n"]
    # The historical request row itself is still there.
    assert len(await children_of(first.id)) == 2


# --- 20.6  partial material -------------------------------------------------

@pytest.mark.asyncio
async def test_surviving_material_is_adopted_while_only_the_missing_member_reacquires(core):
    """Section 13, the ordinary partial case: the coordinate model has NOT
    changed, so both members keep their identity. The delivered payload that
    is still on disk is adopted exactly where it stands -- not moved, not
    re-downloaded, not even re-resolved -- and only the missing one is rebuilt.
    """
    pair = [("one.bin", "one.bin", 4), ("two.bin", "two.bin", 4)]
    first = await acquire(core, pair)
    assert (await core.repository.get(first.id)).state == TransferState.COMPLETED

    before = await artifacts_of(first.id)
    assert len(before) == 2
    kept, removed = before[0], before[1]
    Path(removed["local_path"]).unlink()
    for identity, observed in list(core.provider.resources.items()):
        core.provider.resources[identity] = ProviderObservation(observed.resource, ResourceState.ABSENT)

    await reacquire(core, pair)

    after = await artifacts_of(first.id)
    assert len(after) == 2, "no member was duplicated"
    surviving = next(row for row in after if row["id"] == kept["id"])
    rebuilt = next(row for row in after if row["id"] == removed["id"])

    # Untouched: same row, same coordinate, same writer, file still there.
    assert surviving["local_path"] == kept["local_path"]
    assert surviving["status"] == "completed"
    assert surviving["execution_attempt_id"] == kept["execution_attempt_id"]
    assert not surviving["blocked"]
    assert Path(kept["local_path"]).exists()

    # Rebuilt: same logical member, same canonical coordinate, a NEW writer.
    assert rebuilt["local_path"] == removed["local_path"]
    assert rebuilt["execution_attempt_id"] != removed["execution_attempt_id"]
    assert (await core.repository.get(first.id)).state == TransferState.COMPLETED


@pytest.mark.asyncio
async def test_partial_material_under_a_changed_coordinate_model_converges(core):
    """Section 13 across the upgrade boundary. Here EVERY member's canonical
    coordinate moved, so the material still sitting at a legacy coordinate is
    not adoptable for the current generation -- by definition, it is not where
    the current manifest says the member lives.

    The honest consequence, asserted rather than papered over: the legacy file
    is left on disk untouched (nothing deletes an operator's data), the legacy
    rows become history, and the collection converges at current coordinates.
    """
    legacy_pair = [("one.bin", "Parcel/one.bin", 4), ("two.bin", "Parcel/two.bin", 4)]
    current_pair = [("one.bin", "one.bin", 4), ("two.bin", "two.bin", 4)]
    first = await acquire(core, legacy_pair)
    await upgraded_from_before_generations(core)
    assert (await core.repository.get(first.id)).state == TransferState.COMPLETED

    before = await artifacts_of(first.id)
    kept, removed = before[0], before[1]
    Path(removed["local_path"]).unlink()
    for identity, observed in list(core.provider.resources.items()):
        core.provider.resources[identity] = ProviderObservation(observed.resource, ResourceState.ABSENT)

    await reacquire(core, current_pair)

    assert (await core.repository.get(first.id)).state == TransferState.COMPLETED
    after = await artifacts_of(first.id)
    current = [row for row in after if not row["blocked"]]
    superseded = [row for row in after if row["blocked"]]
    assert len(current) == 2 and len(superseded) == 2
    for row in current:
        assert "Parcel/Parcel/" not in row["local_path"]
        assert row["status"] == "completed"
    # The operator's surviving file is still on disk; nothing deleted it.
    assert Path(kept["local_path"]).exists()
    assert {row["id"] for row in superseded} == {kept["id"], removed["id"]}


# --- generation coherence ---------------------------------------------------

@pytest.mark.asyncio
async def test_a_superseded_member_stops_being_recoverable_at_all(core):
    """Section 21: the reacquired legacy artifact cannot remain indefinitely on
    ``existing_candidate_reused``, because it stops being an artifact recovery
    can select. Nothing caps or suppresses a loop -- the row simply leaves the
    canonical actionable set the whole recovery/dispatch path reads."""
    first = await acquire(core, LEGACY_MANIFEST)
    await upgraded_from_before_generations(core)
    await material_removed(core, first.id)
    await reacquire(core, CURRENT_MANIFEST)

    superseded = next(row for row in await artifacts_of(first.id) if row["blocked"])
    audits_before = await rows(
        "SELECT COUNT(*) AS n FROM application_events WHERE transfer_id=? AND kind='recovery_audit'",
        (first.id,))

    for _ in range(10):
        await core.engine.tick()
        core.clock.advance(60)

    assert superseded["id"] not in {item.id for item in await core.repository.artifacts(first.id)}
    audits_after = await rows(
        "SELECT COUNT(*) AS n FROM application_events WHERE transfer_id=? AND kind='recovery_audit'",
        (first.id,))
    assert audits_after[0]["n"] == audits_before[0]["n"], "a settled transfer kept generating recovery work"
    assert (await core.repository.get(first.id)).state == TransferState.COMPLETED


@pytest.mark.asyncio
async def test_a_superseded_member_never_votes_in_transfer_truth(core):
    """The retired row is readable history, excluded from the canonical set."""
    first = await acquire(core, LEGACY_MANIFEST)
    await upgraded_from_before_generations(core)
    await material_removed(core, first.id)
    await reacquire(core, CURRENT_MANIFEST)

    canonical = await core.repository.artifacts(first.id)
    stored = await artifacts_of(first.id)
    assert len(stored) == 2 and len(canonical) == 1
    assert canonical[0].id == next(row["id"] for row in stored if not row["blocked"])
    assert (await core.repository.get(first.id)).state == TransferState.COMPLETED


@pytest.mark.asyncio
async def test_a_member_the_manifest_never_delivered_is_still_a_failure(core):
    """The pre-existing retirement is unchanged. A child that was PROMISED by
    one manifest, never established, and is absent from the next is a genuine
    ``SOURCE_NOT_FOUND`` failure -- not a superseded generation. Only a member a
    previous generation actually built becomes blocked history."""
    from transfers.models import SourceEntry

    core.provider.responses.append(
        core.provider.parcel("box", state=ResourceState.AVAILABLE, files=CURRENT_MANIFEST))
    transfer = await core.engine.submit((source(),), name=SUBMITTED_NAME)
    await core.engine.resolve_pending()
    record = next(item for item in await core.repository.requests(transfer.id) if item.parent_id is None)

    payload = SourceEntry("payload.bin", 4, "payload.bin",
                          TransferRequest("parcel-member", "box:payload.bin", name="payload.bin"))
    ghost = SourceEntry("ghost.bin", 4, "ghost.bin",
                        TransferRequest("parcel-member", "box:ghost.bin", name="ghost.bin"))
    # A manifest promising a second member, which never resolves into anything.
    await core.repository.manifest(record, (payload, ghost))
    established = {row["request_id"] for row in await artifacts_of(transfer.id)}
    promised = next(row for row in await children_of(transfer.id) if row["id"] not in established)

    # The next manifest no longer contains it.
    await core.repository.manifest(record, (payload,))

    retired = next(row for row in await children_of(transfer.id) if row["id"] == promised["id"])
    assert retired["state"] == "failed", "a never-established member must still fail closed"
    # Nothing was retired as history: no member a previous generation built was
    # superseded here.
    assert not [row for row in await artifacts_of(transfer.id) if row["block_reason"]]


# --- 20.5  selection / materialization authority across the boundary --------

@pytest.mark.asyncio
async def test_a_reacquired_member_joins_the_current_authorized_generation(tmp_path, monkeypatch):
    """Section 14: the reacquired member must END UP in the current authorized
    materialization generation, not be pinned behind a superseded one.

    Generation A commits a member under a pre-upgrade coordinate and builds it.
    The request then re-resolves onto a NEW provider resource, and generation B
    commits the same logical member under its current coordinate. The member
    the current generation owns must be admitted (PROCEED); the superseded one
    must simply stop being an artifact the dispatch path can see at all.
    """
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "authority.db")
    await database.init_db()
    from file_selection_support import executable, rebind_resource, seed_window
    from transfers.models import MaterializationAdmission, MaterializationAdmissionKind
    from transfers.repository import TransferRepository as PlainRepository

    repo = PlainRepository()
    seed = await seed_window(transfer_hash="g" * 40)
    await repo.begin_file_selection_window(
        seed.request_id, seed.transfer_id, seed.provider_resource_id, seed.provider_id,
        initially_available=True, now=1000.0)

    # Generation A: the member as the pre-upgrade manifest published it.
    legacy_entries = executable(("payload.bin", "Collection/payload.bin", 4))
    first = await repo.commit_selected_manifest(seed.record, legacy_entries, now=1000.0)
    await repo.manifest(seed.record, first, selection_id=first.selection_id)
    legacy_child = await rows(
        "SELECT id FROM transfer_requests WHERE transfer_id=? AND parent_id=? ORDER BY ordinal",
        (seed.transfer_id, seed.request_id))
    assert len(legacy_child) == 1
    legacy_id = legacy_child[0]["id"]
    async with database.get_db() as db:
        await db.execute(
            "INSERT INTO download_files(torrent_id,request_id,filename,size_bytes,local_path,status) "
            "VALUES(?,?,?,?,?,'completed')",
            (seed.transfer_id, legacy_id, "payload.bin", 4, "/download/Collection/Collection/payload.bin"))
        # Generation A governs a decomposition persisted before generation
        # authority existed (the upgrade's compatibility lineage) ...
        await db.execute("UPDATE transfer_file_selections SET legacy_established=1 WHERE id=?",
                         (first.selection_id,))
        await db.commit()
    # ... and the completed work is reopened by a terminal reacquisition.
    await repo.record_terminal_reacquisition(seed.transfer_id, 1500.0)

    # A new provider resource, and generation B committing current truth.
    renewed = await rebind_resource(seed, suffix="generation-b")
    await repo.begin_file_selection_window(
        seed.request_id, seed.transfer_id, renewed.provider_resource_id, seed.provider_id,
        initially_available=True, now=2000.0)
    current_entries = executable(("payload.bin", "payload.bin", 4))
    second = await repo.commit_selected_manifest(renewed.record, current_entries, now=2000.0)
    await repo.manifest(renewed.record, second, selection_id=second.selection_id)
    assert second.selection_id != first.selection_id

    children = await rows(
        "SELECT id,ordinal,state,materialized_selection_id FROM transfer_requests "
        "WHERE transfer_id=? AND parent_id=? ORDER BY ordinal", (seed.transfer_id, seed.request_id))
    assert len(children) == 2
    current_child = next(row for row in children if row["id"] != legacy_id)
    retired = next(row for row in children if row["id"] == legacy_id)

    # The current member belongs to the current generation and is admitted.
    assert current_child["materialized_selection_id"] == second.selection_id
    admission = await repo.materialization_authorization(
        SimpleNamespace(id=2, transfer_id=seed.transfer_id, request_id=current_child["id"],
                        execution=None, state="queued"))
    assert admission == MaterializationAdmission(
        MaterializationAdmissionKind.PROCEED, second.selection_id)

    # The superseded member is history: retired, and outside the canonical set
    # the dispatch and recovery paths read -- never cycling STALE forever.
    assert retired["state"] == "skipped"
    assert retired["ordinal"] > current_child["ordinal"]
    # The crossing is ordinary current-model state from here on, and consumed.
    (b_row,) = await rows("SELECT legacy_established,continuity,continuity_reason FROM transfer_file_selections "
                          "WHERE id=?", (second.selection_id,))
    assert tuple(b_row.values()) == (0, "proven", "legacy_compatibility_reconstruction")
    (a_row,) = await rows("SELECT reacquisition_consumed_at FROM transfer_file_selections WHERE id=?",
                          (first.selection_id,))
    assert a_row["reacquisition_consumed_at"] is not None
    stored = await rows("SELECT id,blocked,block_reason FROM download_files WHERE request_id=?", (legacy_id,))
    assert stored[0]["blocked"] == 1 and stored[0]["block_reason"] == "superseded_generation"
    assert legacy_id not in {item.request_id for item in await repo.artifacts(seed.transfer_id)}


@pytest.mark.asyncio
async def test_explicit_selection_still_fails_closed_for_a_new_binding(tmp_path, monkeypatch):
    """Section 20.5, last clause: reacquisition must not become a way to skip
    proving a selection. A request that REQUIRES selection and re-resolves onto
    a new provider resource has no generation for that binding, and the absence
    of one is still never ALL."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "failclosed.db")
    await database.init_db()
    from dataclasses import replace as _replace

    from file_selection_support import executable, rebind_resource, seed_window
    from transfers.errors import Category, TransferError
    from transfers.repository import TransferRepository as PlainRepository

    repo = PlainRepository()
    seed = await seed_window(transfer_hash="h" * 40)
    await repo.begin_file_selection_window(
        seed.request_id, seed.transfer_id, seed.provider_resource_id, seed.provider_id,
        initially_available=True, now=1000.0)
    renewed = await rebind_resource(seed, suffix="unproven")
    # A root that genuinely requires an operator decision.
    interactive = _replace(renewed.record,
                           request=_replace(renewed.record.request, selection_mode="interactive"))

    with pytest.raises(TransferError) as raised:
        await repo.commit_selected_manifest(
            interactive, executable(("payload.bin", "payload.bin", 4)), now=2000.0)
    assert raised.value.error.category == Category.RESOURCE_STATE_CONFLICT


@pytest.mark.asyncio
async def test_an_authorized_subset_is_not_mistaken_for_a_superseded_member(tmp_path, monkeypatch):
    """A selecting transfer hands ``manifest()`` only the AUTHORIZED subset, so
    "absent from this call" must not mean "superseded" for a member the
    operator simply never selected -- and re-running the same generation must
    retire nothing at all.

    A member that WAS selected and then deselected is a different matter: it
    genuinely leaves current authorized truth, and it lands in exactly the
    states deselection already uses (``skipped``/``blocked``).
    """
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "subset.db")
    await database.init_db()
    from file_selection_support import executable, seed_window
    from transfers.repository import TransferRepository as PlainRepository

    repo = PlainRepository()
    seed = await seed_window(transfer_hash="s" * 40)
    await repo.begin_file_selection_window(
        seed.request_id, seed.transfer_id, seed.provider_resource_id, seed.provider_id,
        initially_available=True, now=1000.0)

    chosen = executable(("a.bin", "a.bin", 1), ("b.bin", "b.bin", 2))
    committed = await repo.commit_selected_manifest(seed.record, chosen, now=1000.0)
    await repo.manifest(seed.record, committed, selection_id=committed.selection_id)
    first = await rows(
        "SELECT id,ordinal,state FROM transfer_requests WHERE transfer_id=? AND parent_id=? ORDER BY ordinal",
        (seed.transfer_id, seed.request_id))
    assert len(first) == 2 and all(row["state"] != "skipped" for row in first)

    # The same generation resolving again changes nothing.
    await repo.manifest(seed.record, committed, selection_id=committed.selection_id)
    repeated = await rows(
        "SELECT id,ordinal,state FROM transfer_requests WHERE transfer_id=? AND parent_id=? ORDER BY ordinal",
        (seed.transfer_id, seed.request_id))
    assert [(row["id"], row["ordinal"], row["state"]) for row in repeated] == \
           [(row["id"], row["ordinal"], row["state"]) for row in first]


# --- current-model controls: strict continuity (TASK3d-3a0 D1) ---------------

#: A decomposition established under the CURRENT model whose replacement
#: publishes the member at another coordinate: not the legacy fixture.
REPLACEMENT_MANIFEST = [("payload.bin", "moved/payload.bin", 4)]

@pytest.mark.asyncio
async def test_a_current_model_reacquisition_that_moves_a_member_coordinate_holds(core):
    """The legacy (doubled-root) generation reacquired under current manifest
    truth publishes the member at another coordinate: continuity cannot be
    proven, so the reacquisition holds. Nothing is superseded, retargeted,
    deleted or started -- driven only through the lifecycle."""
    first = await acquire(core, CURRENT_MANIFEST)
    assert (await core.repository.get(first.id)).state == TransferState.COMPLETED
    legacy = (await artifacts_of(first.id))[0]
    legacy_child = (await children_of(first.id))[0]

    await material_removed(core, first.id)
    again = await reacquire(core, REPLACEMENT_MANIFEST)

    assert again.id == first.id
    assert (await core.repository.get(first.id)).state != TransferState.COMPLETED
    (generation,) = await rows(
        "SELECT continuity,continuity_reason,manifest_committed_at FROM transfer_file_selections "
        "WHERE transfer_id=? ORDER BY created_at DESC, id DESC LIMIT 1", (first.id,))
    assert (generation["continuity"], generation["continuity_reason"]) == ("held", "established_member_missing")
    assert generation["manifest_committed_at"] is None
    # Every member, artifact and writer stays where it was: the one artifact
    # keeps its row and coordinate and is never superseded (its material is
    # gone, which the Retry already recorded before any generation opened).
    (artifact,) = await artifacts_of(first.id)
    assert (artifact["id"], artifact["local_path"], artifact["blocked"]) == (legacy["id"], legacy["local_path"], 0)
    assert [row["id"] for row in await children_of(first.id)] == [legacy_child["id"]]
    assert (await children_of(first.id))[0]["state"] != "skipped"
    assert len(await core.repository.executions(first.id)) == 1


@pytest.mark.asyncio
async def test_a_held_reacquisition_does_not_churn(core):
    """Holding is a stable state, not a loop: the held generation is not
    reopened, no member consumes resolution attempts, and nothing is
    manufactured while it holds."""
    first = await acquire(core, CURRENT_MANIFEST)
    legacy_child = (await children_of(first.id))[0]
    await material_removed(core, first.id)
    await reacquire(core, REPLACEMENT_MANIFEST)

    async def generations():
        return await rows("SELECT id FROM transfer_file_selections WHERE transfer_id=?", (first.id,))

    before = next(row for row in await requests_of(first.id) if row["id"] == legacy_child["id"])["attempts"]
    held = await generations()
    for _ in range(8):
        core.provider.responses.append(_observation(core, "box2", REPLACEMENT_MANIFEST, None))
        await core.engine.tick()
        core.clock.advance(30)
    after = next(row for row in await requests_of(first.id) if row["id"] == legacy_child["id"])["attempts"]
    assert after == before
    assert await generations() == held
    assert len(await artifacts_of(first.id)) == 1


@pytest.mark.asyncio
async def test_historical_executions_and_provenance_survive_a_held_reacquisition(core):
    first = await acquire(core, CURRENT_MANIFEST)
    historical = await core.repository.executions(first.id)
    assert len(historical) == 1
    historical_id = historical[0].handle.attempt_id
    provenance_before = await rows(
        "SELECT COUNT(*) AS n FROM route_attempt_provenance WHERE transfer_id=?", (first.id,))

    await material_removed(core, first.id)
    await reacquire(core, REPLACEMENT_MANIFEST)

    attempts = await core.repository.executions(first.id)
    assert [item.handle.attempt_id for item in attempts] == [historical_id], "a held generation starts no writer"
    rows_ = await rows("SELECT id,authorized,state FROM execution_attempts WHERE id=?", (historical_id,))
    assert rows_[0]["authorized"] == 0, "a historical attempt must never be reauthorized"
    provenance_after = await rows(
        "SELECT COUNT(*) AS n FROM route_attempt_provenance WHERE transfer_id=?", (first.id,))
    assert provenance_after[0]["n"] >= provenance_before[0]["n"]
    assert len(await children_of(first.id)) == 1


@pytest.mark.asyncio
async def test_material_under_a_changed_coordinate_model_is_held_in_place(core):
    """Every member's coordinate moved, so continuity cannot be proven: the
    surviving completed file stays exactly where it is, as current (not
    superseded) work, and nothing re-downloads it under another coordinate."""
    current_pair = [("one.bin", "one.bin", 4), ("two.bin", "two.bin", 4)]
    moved_pair = [("one.bin", "moved/one.bin", 4), ("two.bin", "moved/two.bin", 4)]
    first = await acquire(core, current_pair)
    assert (await core.repository.get(first.id)).state == TransferState.COMPLETED

    before = await artifacts_of(first.id)
    kept, removed = before[0], before[1]
    Path(removed["local_path"]).unlink()
    for identity, observed in list(core.provider.resources.items()):
        core.provider.resources[identity] = ProviderObservation(observed.resource, ResourceState.ABSENT)

    await reacquire(core, moved_pair)

    assert (await core.repository.get(first.id)).state != TransferState.COMPLETED
    after = await artifacts_of(first.id)
    assert [(row["id"], row["local_path"], row["blocked"]) for row in after] == [
        (row["id"], row["local_path"], 0) for row in before]          # no row moved, blocked or added
    assert next(row for row in after if row["id"] == kept["id"]) == kept   # the surviving file's row is untouched
    assert Path(kept["local_path"]).exists()
    assert len(await core.repository.executions(first.id)) == 2         # only the two original writers


@pytest.mark.asyncio
async def test_a_held_reacquisition_generates_no_recovery_work(core):
    """A held generation is not a loop: no member is superseded, nothing leaves
    or joins the canonical set, and recovery stays quiet while it holds."""
    first = await acquire(core, CURRENT_MANIFEST)
    await material_removed(core, first.id)
    await reacquire(core, REPLACEMENT_MANIFEST)
    canonical_before = {item.id for item in await core.repository.artifacts(first.id)}
    audits_before = await rows(
        "SELECT COUNT(*) AS n FROM application_events WHERE transfer_id=? AND kind='recovery_audit'",
        (first.id,))

    for _ in range(10):
        await core.engine.tick()
        core.clock.advance(60)

    assert {item.id for item in await core.repository.artifacts(first.id)} == canonical_before
    audits_after = await rows(
        "SELECT COUNT(*) AS n FROM application_events WHERE transfer_id=? AND kind='recovery_audit'",
        (first.id,))
    assert audits_after[0]["n"] == audits_before[0]["n"]


@pytest.mark.asyncio
async def test_a_held_reacquisition_supersedes_nothing(core):
    """Every stored row is still current canonical work: nothing was retired."""
    first = await acquire(core, CURRENT_MANIFEST)
    await material_removed(core, first.id)
    await reacquire(core, REPLACEMENT_MANIFEST)

    canonical = await core.repository.artifacts(first.id)
    stored = await artifacts_of(first.id)
    assert len(stored) == 1 and [item.id for item in canonical] == [stored[0]["id"]]
    assert not stored[0]["blocked"]


@pytest.mark.asyncio
async def test_a_current_model_generation_that_moves_a_member_is_held_before_anything_changes(tmp_path, monkeypatch):
    """Repository level: generation A commits and fans out a member; the root
    rebinds and generation B publishes that member under another coordinate.
    B cannot be committed: it is recorded ``held`` with the bounded reason,
    authorizes nothing, and A's member is neither superseded nor moved -- and,
    its binding no longer current, it is not runnable either."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "authority.db")
    await database.init_db()
    from file_selection_support import executable, rebind_resource, seed_window
    from transfers.models import MaterializationAdmissionKind
    from transfers.repository import TransferRepository as PlainRepository

    repo = PlainRepository()
    seed = await seed_window(transfer_hash="g" * 40)
    await repo.begin_file_selection_window(
        seed.request_id, seed.transfer_id, seed.provider_resource_id, seed.provider_id,
        initially_available=True, now=1000.0)
    first = await repo.commit_selected_manifest(seed.record, executable(("payload.bin", "payload.bin", 4)),
                                                now=1000.0)
    await repo.manifest(seed.record, first, selection_id=first.selection_id)
    (legacy,) = await rows("SELECT id,state,ordinal FROM transfer_requests WHERE transfer_id=? AND parent_id=?",
                           (seed.transfer_id, seed.request_id))

    renewed = await rebind_resource(seed, suffix="generation-b")
    await repo.begin_file_selection_window(
        seed.request_id, seed.transfer_id, renewed.provider_resource_id, seed.provider_id,
        initially_available=True, now=2000.0)
    second = await repo.commit_selected_manifest(renewed.record, executable(("payload.bin", "moved/payload.bin", 4)),
                                                 now=2000.0)

    assert tuple(second) == () and second.held == "established_member_missing"
    (generation,) = await rows("SELECT continuity,continuity_reason,manifest_committed_at FROM "
                               "transfer_file_selections WHERE id=?", (second.selection_id,))
    assert tuple(generation.values()) == ("held", "established_member_missing", None)
    from transfers.errors import TransferError
    with pytest.raises(TransferError):
        await repo.manifest(renewed.record, second, selection_id=second.selection_id)   # never fans out
    assert await rows("SELECT id,state,ordinal FROM transfer_requests WHERE transfer_id=? AND parent_id=?",
                      (seed.transfer_id, seed.request_id)) == [legacy]
    admission = await repo.materialization_authorization(
        SimpleNamespace(id=1, transfer_id=seed.transfer_id, request_id=legacy["id"], execution=None, state="queued"))
    assert admission.kind == MaterializationAdmissionKind.STALE


# --- canonical candidate bindings across an in-place rebuild (TASK3d-3a0) ------

async def bindings_of(transfer_id):
    return await rows(
        """SELECT b.id,b.canonical_artifact_id,b.candidate_id,b.candidate_order FROM canonical_candidate_bindings b
           JOIN download_files f ON f.id=b.canonical_artifact_id WHERE f.torrent_id=? ORDER BY b.id""", (transfer_id,))


async def origins_of(transfer_id):
    return await rows(
        """SELECT o.id,o.binding_id FROM canonical_candidate_origins o JOIN canonical_candidate_bindings b
           ON b.id=o.binding_id JOIN download_files f ON f.id=b.canonical_artifact_id WHERE f.torrent_id=?
           ORDER BY o.id""", (transfer_id,))


async def restart(core):
    """A process restart over the same database: a fresh engine initializes."""
    from transfers.convergence_engine import TransferEngine
    restarted = TransferEngine(core.repository, core.engine.registry, download_root=core.engine.root,
                               policy=core.engine.policy, clock=core.clock)
    await restarted.initialize()
    return restarted


async def assert_aligned(core, transfer_id):
    """Every current candidate's binding holds its position; every other
    binding sits in the non-current band -- what ordinary readers rely on."""
    for artifact in await core.repository.artifacts(transfer_id):
        current = {str(candidate.id): position for position, candidate in enumerate(artifact.candidates, start=1)}
        for binding in await rows("SELECT candidate_id,candidate_order FROM canonical_candidate_bindings "
                                  "WHERE canonical_artifact_id=?", (artifact.id,)):
            if binding["candidate_id"] in current:
                assert binding["candidate_order"] == current[binding["candidate_id"]]
            else:
                assert binding["candidate_order"] > 100000


@pytest.mark.asyncio
async def test_a_retry_reacquisition_rebuilt_in_place_survives_a_restart_with_its_history(core):
    """The pre-existing defect: the rebuilt member's earlier candidate binding
    kept order 1, and the next start's binding backfill collided with it."""
    first = await acquire(core, CURRENT_MANIFEST)
    await restart(core)                                        # the first start formalizes its binding
    history = await bindings_of(first.id)
    origins = await origins_of(first.id)
    assert history and origins
    await material_removed(core, first.id)
    await reacquire(core, CURRENT_MANIFEST)

    await restart(core)
    await restart(core)                                        # repeated restarts change nothing further
    after = await bindings_of(first.id)
    assert {row["id"] for row in history} <= {row["id"] for row in after}        # no binding deleted
    assert {row["id"] for row in origins} <= {row["id"] for row in await origins_of(first.id)}
    await assert_aligned(core, first.id)
    settled = await bindings_of(first.id)
    await restart(core)
    assert await bindings_of(first.id) == settled              # idempotent


@pytest.mark.asyncio
async def test_a_database_damaged_before_the_fix_is_repaired_at_startup(core, monkeypatch):
    """A rebuild that ran without the realignment left its stale binding at an
    active position; the next start corrects that durable state."""
    first = await acquire(core, CURRENT_MANIFEST)
    await restart(core)
    await material_removed(core, first.id)
    from transfers.canonical import CanonicalOwnership

    async def unrealigned(self, artifact_id):
        return None

    with monkeypatch.context() as patched:
        patched.setattr(CanonicalOwnership, "realign_rebuilt", unrealigned)
        await reacquire(core, CURRENT_MANIFEST)
    await restart(core)
    await assert_aligned(core, first.id)


# --- the compatibility crossing is one-way, one-generation, and durable ------

async def generation_rows(transfer_id):
    return await rows("SELECT id,legacy_established,reacquired_at,reacquisition_consumed_at,continuity,"
                      "continuity_reason,manifest_committed_at FROM transfer_file_selections WHERE transfer_id=? "
                      "ORDER BY created_at,id", (transfer_id,))


@pytest.mark.asyncio
async def test_the_compatibility_crossing_is_consumed_once_and_never_carried_forward(core):
    """Cases 1 and 2: a pre-3a0 decomposition crosses once on its terminal
    reacquisition; the generation it crosses into is ordinary current-model
    state, so the NEXT unprovable replacement holds under strict continuity."""
    first = await acquire(core, LEGACY_MANIFEST)
    await upgraded_from_before_generations(core)
    await material_removed(core, first.id)
    await reacquire(core, CURRENT_MANIFEST)
    assert (await core.repository.get(first.id)).state == TransferState.COMPLETED

    legacy, crossed = await generation_rows(first.id)
    assert legacy["legacy_established"] == 1 and legacy["reacquisition_consumed_at"] is not None
    assert (crossed["legacy_established"], crossed["continuity"], crossed["continuity_reason"]) == (
        0, "proven", "legacy_compatibility_reconstruction")
    assert (await core.repository.get(first.id)).collection_root == "Parcel"          # G4: never moved

    # The same transfer, later: its predecessor is current-model now.
    await material_removed(core, first.id)
    await reacquire(core, [("payload.bin", "moved/payload.bin", 4)], renewed="box3")
    assert (await core.repository.get(first.id)).state != TransferState.COMPLETED
    *_, held = await generation_rows(first.id)
    assert (held["continuity"], held["continuity_reason"]) == ("held", "established_member_missing")


@pytest.mark.asyncio
async def test_a_restart_between_the_reopen_and_the_new_generation_keeps_the_crossing_and_replays_nothing(core):
    """Case 6: the terminal-reacquisition fact survives a restart before the
    replacement generation exists, the crossing happens exactly once, and a
    restart (and a repeated upgrade) after it replays nothing."""
    first = await acquire(core, LEGACY_MANIFEST)
    await upgraded_from_before_generations(core)
    await material_removed(core, first.id)
    core.provider.responses.append(_observation(core, "box2", CURRENT_MANIFEST, None))
    assert await core.engine.retry(first.id, reacquire=True)
    (legacy,) = await generation_rows(first.id)
    assert legacy["reacquired_at"] is not None and legacy["reacquisition_consumed_at"] is None

    core.engine = await restart(core)                          # crash: no replacement generation yet
    for _ in range(6):
        core.provider.responses.append(_observation(core, "box2", CURRENT_MANIFEST, None))
        await core.engine.tick()
        for attempt in await core.repository.executions(first.id):
            if attempt.state not in {"succeeded", "failed", "absent", "cancelled"}:
                core.executor.finish(attempt.handle)
        core.clock.advance(5)
    assert (await core.repository.get(first.id)).state == TransferState.COMPLETED
    settled_generations = await generation_rows(first.id)
    settled_artifacts = await artifacts_of(first.id)
    assert [row["continuity_reason"] for row in settled_generations] == [None, "legacy_compatibility_reconstruction"]

    core.engine = await restart(core)                          # crash after the crossing committed
    import aiosqlite
    from db.migrations.v113_decomposition_generations import (
        backfill_decomposition_generations,
    )
    async with aiosqlite.connect(database.DB_PATH) as raw:
        await backfill_decomposition_generations(raw)          # a later start's upgrade is a no-op
    for _ in range(4):
        await core.engine.tick()
        core.clock.advance(30)
    assert await generation_rows(first.id) == settled_generations
    assert await artifacts_of(first.id) == settled_artifacts
