"""TASK3d-3a0: provider/resource-independent decomposition generations.

Every binding of a manifest root owns a decomposition generation. A member is
runnable only while its generation is, conjunctively, its root's current
generation, bound to the root's CURRENT binding, and committed ``proven``; a
rebind therefore fences the old generation at the moment it commits. Before a
new generation of a root that already fanned out commits, it must prove it is
exactly the established decomposition (a bijection on normalized path with
compatible sizes); otherwise it holds and changes nothing. The collection
folder is frozen at the first fan-out. These facts hold for a same-provider
reacquisition -- no operator involved -- which is the defect they repair.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import db.database as database
import pytest
from db.database import get_db
from db.migrations.v113_decomposition_generations import (
    backfill_decomposition_generations,
)
from fake_integrations import MemoryExecutor, ParcelProvider
from test_v113_collection_route_generic_closure import Clock
from transfers import codec
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, TransferError
from transfers.models import (
    ExecutionHandle,
    ExecutionState,
    MaterializationAdmissionKind,
    ProviderResource,
    ResourceState,
    SourceEntry,
    TransferRequest,
)
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

FILES = [("one.bin", "Show/one.bin", 4), ("two.bin", "Show/two.bin", 4)]


async def lab(tmp_path, monkeypatch, *, fresh=True, provider=None, executor=None, clock=None):
    if fresh:
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "generations.sqlite3")
        await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    provider = provider or ParcelProvider("parcel-a", file_manifest=True)
    registry.register_provider(provider)
    executor = executor or MemoryExecutor(repository.authorize_execution)
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "dl"),
                            policy=TransferPolicy(retry_delay=0.0, max_attempts=3), clock=clock or Clock())
    await engine.initialize()
    return repository, engine, provider, executor


async def decomposed(tmp_path, monkeypatch, *, selection_mode="all", files=FILES):
    """A root on parcel-a fanned out into two members, both writing."""
    repository, engine, provider, executor = await lab(tmp_path, monkeypatch)
    provider.responses.append(provider.parcel("x", state=ResourceState.AVAILABLE, files=files))
    transfer = await engine.submit((TransferRequest("parcel", "x", name="Show", selection_mode=selection_mode),),
                                   name="Show", deduplicate=False)
    for _ in range(6):
        await engine.tick()
    return repository, engine, provider, executor, transfer


async def root_of(repository, transfer_id):
    return next(item for item in await repository.requests(transfer_id) if item.parent_id is None)


def rebound(provider, generation: int, *, files, name=None):
    """The same provider re-acquires the root as a NEW native resource."""
    result = provider.parcel("x", state=ResourceState.AVAILABLE, files=files)
    old = result.observation.resource
    resource = ProviderResource(old.provider_id, {"box_ticket": f"x#{generation}"}, old.ownership,
                                id=f"{old.id}#{generation}")
    observed = replace(result.observation, resource=resource, name=name or result.observation.name)
    provider.resources.pop(old.id, None)
    provider.resources[resource.id] = observed
    provider.members[resource.id] = provider.members.pop(old.id)
    provider.responses.append(replace(result, observation=observed))


async def reacquire(repository, engine, provider, transfer, *, files=FILES, name=None, ticks=8, between=None):
    """The ordinary reacquisition path: the root's resource expires and a
    member's renewal re-acquires it from the same provider."""
    root = await root_of(repository, transfer.id)
    provider.resources.pop(root.resource.id)
    rebound(provider, 2, files=files, name=name)
    member = next(item for item in await repository.requests(transfer.id) if item.parent_id == root.id)
    engine.clock.now += 60
    assert await engine._renew_source_parent(member)
    for _ in range(ticks):
        engine.clock.now += 60
        await engine.tick()
        if between:
            await between()


async def rows(sql, params=()):
    async with get_db() as db:
        return await db.fetchall(sql, params)


async def files_of(transfer_id):
    return await rows("SELECT id,request_id,status,blocked,local_path FROM download_files WHERE torrent_id=? "
                      "ORDER BY id", (transfer_id,))


async def generations(transfer_id):
    return await rows("SELECT * FROM transfer_file_selections WHERE transfer_id=? ORDER BY created_at, id",
                      (transfer_id,))


async def current_generation(repository, transfer_id):
    """The generation of the root's CURRENT binding -- never chosen by time."""
    root = await root_of(repository, transfer_id)
    binding = await repository.resource_binding_id(transfer_id, root.resource.id)
    (row,) = await rows("SELECT * FROM transfer_file_selections WHERE request_id=? AND provider_resource_id=?",
                        (root.id, binding))
    return row


def running(executor) -> set[str]:
    return {attempt for attempt, job in executor.jobs.items() if job.state == ExecutionState.RUNNING}


async def runnable_generations(repository, transfer_id) -> set[str]:
    """Which generations any member could start a writer under right now."""
    runnable = set()
    for artifact in await repository.artifacts(transfer_id):
        admission = await repository.materialization_authorization(artifact)
        if admission.kind == MaterializationAdmissionKind.PROCEED and admission.authority_generation:
            runnable.add(admission.authority_generation)
    return runnable


# -- 1. same-provider rebind, identical manifest: the old writer cannot survive -----------------------------

async def test_an_identical_reacquisition_retires_the_old_writer_and_continues_in_place(tmp_path, monkeypatch):
    repository, engine, provider, executor, transfer = await decomposed(tmp_path, monkeypatch)
    before = await files_of(transfer.id)
    old_writers = running(executor)
    assert len(old_writers) == 2
    executor.finish((await repository.artifacts(transfer.id))[0].execution)       # one member completes
    await engine.tick()

    await reacquire(repository, engine, provider, transfer)

    second = await current_generation(repository, transfer.id)
    (first,) = [row for row in await generations(transfer.id) if row["id"] != second["id"]]
    assert second["predecessor_id"] == first["id"]
    assert (second["continuity"], second["decision"], second["interactive"]) == ("proven", "all", 0)
    assert not (running(executor) & old_writers), "an old-generation writer survived the rebind"
    after = await files_of(transfer.id)
    assert [(row["id"], row["request_id"], row["local_path"]) for row in after] == [
        (row["id"], row["request_id"], row["local_path"]) for row in before]          # same members, same targets
    assert after[0]["status"] == "completed"                                          # completed stays completed
    current = {artifact.execution.attempt_id for artifact in await repository.artifacts(transfer.id)
               if artifact.execution and artifact.state != "completed"}
    assert running(executor) == current and len(current) == 1                          # one writer per target
    assert await runnable_generations(repository, transfer.id) <= {second["id"]}


# -- 2. same-provider rebind, renamed root: the target stays under the frozen folder ----------------------------

async def test_a_renamed_provider_report_never_relocates_a_member(tmp_path, monkeypatch):
    repository, engine, provider, executor, transfer = await decomposed(tmp_path, monkeypatch)
    before = await files_of(transfer.id)
    assert (await repository.get(transfer.id)).collection_root == "Parcel"

    await reacquire(repository, engine, provider, transfer, name="Parcel (another provider name)")

    renamed = await repository.get(transfer.id)
    assert (renamed.name, renamed.collection_root) == ("Parcel (another provider name)", "Parcel")
    assert [row["local_path"] for row in await files_of(transfer.id)] == [row["local_path"] for row in before]
    for artifact in await repository.artifacts(transfer.id):
        if artifact.execution:
            assert executor.jobs[artifact.execution.attempt_id].handle.correlation["destination"] == artifact.target


# -- 3. same-provider rebind, different paths: continuity holds atomically, nothing is superseded ---------------

@pytest.mark.parametrize("files, reason", [
    ([("one.bin", "Show.2024/one.bin", 4), ("two.bin", "Show.2024/two.bin", 4)], "established_member_missing"),
    ([*FILES, ("three.bin", "Show/three.bin", 4)], "unexplained_member"),
    ([("one.bin", "Show/one.bin", 4)], "established_member_missing"),
    ([("one.bin", "Show/one.bin", 4), ("two.bin", "Show/two.bin", 5)], "member_size_conflict"),
])
async def test_a_reacquisition_that_is_not_the_established_decomposition_holds(tmp_path, monkeypatch, files, reason):
    repository, engine, provider, executor, transfer = await decomposed(tmp_path, monkeypatch)
    before_members = await rows("SELECT id,ordinal,materialized_selection_id,payload FROM transfer_requests "
                                "WHERE transfer_id=? AND parent_id IS NOT NULL ORDER BY id", (transfer.id,))
    before = await files_of(transfer.id)
    started = set(executor.jobs)

    await reacquire(repository, engine, provider, transfer, files=files)

    held = await current_generation(repository, transfer.id)
    assert (held["continuity"], held["continuity_reason"], held["manifest_committed_at"]) == ("held", reason, None)
    # No member was superseded, re-stamped, re-ordered or given the new payload.
    assert await rows("SELECT id,ordinal,materialized_selection_id,payload FROM transfer_requests "
                      "WHERE transfer_id=? AND parent_id IS NOT NULL ORDER BY id", (transfer.id,)) == before_members
    assert {row["state"] for row in await rows("SELECT state FROM transfer_requests WHERE transfer_id=? AND "
                                               "parent_id IS NOT NULL", (transfer.id,))} <= {"resolved", "pending"}
    assert [(row["id"], row["local_path"], row["blocked"]) for row in await files_of(transfer.id)] == [
        (row["id"], row["local_path"], 0) for row in before]
    assert set(executor.jobs) == started, "nothing was newly dispatched"
    assert not running(executor), "no writer runs under a superseded or held generation"
    assert await runnable_generations(repository, transfer.id) == set()
    # Holding is quiet: further passes resolve no member and start nothing.
    resolutions = len([call for call in provider.calls if call[0] == "resolve"])
    for _ in range(5):
        engine.clock.now += 600
        await engine.tick()
    assert len([call for call in provider.calls if call[0] == "resolve"]) == resolutions
    assert set(executor.jobs) == started


async def test_the_continuity_proof_is_a_bijection_on_path_and_size_never_position():
    from transfers import file_selection as fs
    from transfers.models import SourceEntry

    def entries(*pairs):
        return tuple(SourceEntry(path, size, path, TransferRequest("parcel-member", path)) for path, size in pairs)

    established = [("Show/a", 4), ("Show/b", 5)]
    assert fs.decomposition_continuity(established, entries(("Show/b", 5), ("Show/a", 4))) is None   # permutation
    assert fs.decomposition_continuity(established, entries(("Show/a", 4), ("Show/b", 0))) is None   # unknown size
    assert fs.decomposition_continuity(established, entries(("Show/a", 4), ("Show/a", 4), ("Show/b", 5))) \
        == "replacement_path_duplicate"
    assert fs.decomposition_continuity(established, entries(("Show/a", 4))) == "established_member_missing"
    assert fs.decomposition_continuity(established, entries(("Show/a", 4), ("Show/b", 5), ("Show/c", 1))) \
        == "unexplained_member"
    assert fs.decomposition_continuity(established, entries(("Show/a", 4), ("Show/B", 5))) \
        == "established_member_missing"                                           # no case/basename guessing


# -- 4. completed and partial valid material stay in place after a successful proof -----------------------------

async def test_completed_and_partial_material_stay_in_place_across_a_proven_rebind(tmp_path, monkeypatch):
    repository, engine, provider, executor, transfer = await decomposed(tmp_path, monkeypatch)
    one, two = await repository.artifacts(transfer.id)
    executor.finish(one.execution)                                                 # one.bin delivered
    await engine.tick()
    Path(two.target).parent.mkdir(parents=True, exist_ok=True)
    Path(two.target).write_bytes(b"pa")                                            # two.bin partially written

    await reacquire(repository, engine, provider, transfer)

    assert Path(one.target).read_bytes() == b"done"
    assert Path(two.target).exists()
    after = {row["id"]: row for row in await files_of(transfer.id)}
    assert after[one.id]["status"] == "completed" and after[one.id]["local_path"] == one.target
    assert after[two.id]["local_path"] == two.target
    rebuilt = next(artifact for artifact in await repository.artifacts(transfer.id) if artifact.id == two.id)
    assert executor.jobs[rebuilt.execution.attempt_id].handle.correlation["destination"] == two.target


# -- 5. executor.start / executor.retry_from (one admission) and the parked resume reject stale/held ------------

async def rebind_root_without_resolution(repository, transfer_id):
    """The root's binding moves (as ``resolution()`` leaves it), nothing else."""
    root = await root_of(repository, transfer_id)
    other = ProviderResource(root.resource.provider_id, {"box_ticket": "elsewhere"}, root.resource.ownership,
                             id=root.resource.id + "#elsewhere")
    async with get_db() as db:
        await repository._resource(db, transfer_id, other, ResourceState.AVAILABLE)
        await db.execute("UPDATE transfer_requests SET resource=? WHERE id=?", (codec.dump(other), root.id))
        await db.commit()


async def test_writer_admission_refuses_a_stale_or_held_generation_inside_its_transaction(tmp_path, monkeypatch):
    repository, engine, _provider, executor, transfer = await decomposed(tmp_path, monkeypatch)
    artifact = next(item for item in await repository.artifacts(transfer.id) if item.execution)
    executor.jobs[artifact.execution.attempt_id] = replace(
        executor.jobs[artifact.execution.attempt_id], state=ExecutionState.CANCELLED)
    await engine.tick()
    queued = next(item for item in await repository.artifacts(transfer.id) if item.id == artifact.id)
    async with get_db() as db:                                   # a detached, queued member: admissible
        await db.execute("UPDATE download_files SET status='queued',execution_attempt_id=NULL WHERE id=?",
                         (queued.id,))
        await db.commit()
    queued = next(item for item in await repository.artifacts(transfer.id) if item.id == artifact.id)
    assert (await repository.materialization_authorization(queued)).kind == MaterializationAdmissionKind.PROCEED

    # Held: the member's current generation's continuity is not proven.
    async with get_db() as db:
        await db.execute("UPDATE transfer_file_selections SET continuity='held' WHERE transfer_id=?", (transfer.id,))
        await db.commit()
    handle = ExecutionHandle("memory-copy", "never-admitted", {})
    assert (await repository.materialization_authorization(queued)).kind == MaterializationAdmissionKind.HOLD
    assert not await repository.prepare_execution(queued, handle)
    async with get_db() as db:
        await db.execute("UPDATE transfer_file_selections SET continuity='proven' WHERE transfer_id=?",
                         (transfer.id,))
        await db.commit()
    # Stale: the root's binding moved.
    await rebind_root_without_resolution(repository, transfer.id)
    assert (await repository.materialization_authorization(queued)).kind == MaterializationAdmissionKind.STALE
    assert not await repository.prepare_execution(queued, handle)
    assert await rows("SELECT id FROM execution_attempts WHERE id='never-admitted'") == []


async def test_dispatch_and_native_retry_reach_a_writer_only_through_the_one_admission():
    """``executor.start`` and ``executor.retry_from`` are both reached only after
    ``prepare_execution`` admitted the writer: no start path bypasses it."""
    import inspect

    from transfers import _engine_base
    source = inspect.getsource(_engine_base.TransferEngine._dispatch)
    admitted = source.index("self.repository.prepare_execution(")
    assert admitted < source.index("executor.retry_from(") and admitted < source.index("executor.start(")


def parked(job):
    """A natively paused job the executor offers to resume."""
    from transfers.models import ExecutionActivity, ExecutionControl
    return replace(job, state=ExecutionState.PAUSED, activity=ExecutionActivity(),
                   controls=frozenset({ExecutionControl.RESUME}))


async def test_a_parked_writer_is_never_natively_resumed_under_a_stale_generation(tmp_path, monkeypatch):
    repository, engine, _provider, executor, transfer = await decomposed(tmp_path, monkeypatch)
    artifact = next(item for item in await repository.artifacts(transfer.id) if item.execution)
    attempt = artifact.execution.attempt_id
    executor.jobs[attempt] = parked(executor.jobs[attempt])
    await rebind_root_without_resolution(repository, transfer.id)

    await engine._converge_execution(artifact, executor)

    assert executor.jobs[attempt].state == ExecutionState.PAUSED, "a stale generation was natively resumed"


async def test_a_parked_writer_of_the_current_generation_still_resumes(tmp_path, monkeypatch):
    repository, engine, _provider, executor, transfer = await decomposed(tmp_path, monkeypatch)
    artifact, other = [item for item in await repository.artifacts(transfer.id) if item.execution]
    executor.finish(other.execution)                                  # its slot is free
    await engine.tick()
    attempt = artifact.execution.attempt_id
    executor.jobs[attempt] = parked(executor.jobs[attempt])

    await engine._converge_execution(artifact, executor)

    assert executor.jobs[attempt].state == ExecutionState.RUNNING


# -- 6. a restart at any transition boundary never makes both generations runnable -------------------------------

@pytest.mark.parametrize("files", [FILES, [("one.bin", "Show.2024/one.bin", 4), ("two.bin", "Show.2024/two.bin", 4)]])
async def test_restart_at_every_reacquisition_boundary_never_runs_two_generations(tmp_path, monkeypatch, files):
    repository, engine, provider, executor, transfer = await decomposed(tmp_path, monkeypatch)
    old_generation = (await generations(transfer.id))[0]["id"]
    old_writers = running(executor)
    root = await root_of(repository, transfer.id)
    provider.resources.pop(root.resource.id)
    rebound(provider, 2, files=files)
    member = next(item for item in await repository.requests(transfer.id) if item.parent_id == root.id)
    assert await engine._renew_source_parent(member)

    clock = engine.clock
    for step in range(10):
        # A process restart between every pass: a fresh engine over the same
        # database and the same native jobs.
        clock.now += 60
        repository, engine, provider, executor = await lab(tmp_path, monkeypatch, fresh=False,
                                                           provider=provider, executor=executor, clock=clock)
        await engine.tick()
        runnable = await runnable_generations(repository, transfer.id)
        assert len(runnable) <= 1, f"two generations runnable after pass {step}"
        if (await root_of(repository, transfer.id)).resource.id != root.resource.id:
            assert old_generation not in runnable, f"the old generation stayed runnable after pass {step}"
            assert not (running(executor) & old_writers), f"an old writer ran after pass {step}"
        assert len(running(executor)) <= len(await repository.artifacts(transfer.id))


async def test_a_rebind_before_its_generation_opens_already_fences_the_old_generation(tmp_path, monkeypatch):
    """The boundary between the rebind commit and the new generation: the old
    generation is not runnable, and nothing new exists to run."""
    repository, _engine, _provider, _executor, transfer = await decomposed(tmp_path, monkeypatch)
    await rebind_root_without_resolution(repository, transfer.id)
    assert len(await generations(transfer.id)) == 1
    assert await runnable_generations(repository, transfer.id) == set()


# -- 7./8. migration: an existing consistent folder is kept; conflicting evidence holds -----------------------

async def legacy_transfer(name, placements, *, request_id="legacy-root"):
    """A pre-3a0 decomposed transfer: no generation rows, members already placed."""
    async with get_db() as db:
        transfer_id = await db.execute_returning_id("INSERT INTO torrents(hash,name,status) VALUES(?,?,?)",
                                                    (request_id + "-hash", name, "downloading"))
        resource = ProviderResource("parcel-a", {"box_ticket": request_id}, "created", id=f"parcel-a:{request_id}")
        await TransferRepository._resource(db, transfer_id, resource, ResourceState.AVAILABLE)
        await db.execute("INSERT INTO transfer_requests(id,transfer_id,ordinal,payload,state,resource) "
                         "VALUES(?,?,0,?,'resolved',?)",
                         (request_id, transfer_id, codec.dump(TransferRequest("parcel", "x")), codec.dump(resource)))
        for ordinal, (relative, target) in enumerate(placements):
            child = f"{request_id}-member-{ordinal}"
            entry = SourceEntry(relative.rsplit("/", 1)[-1], 4, relative, TransferRequest("parcel-member", relative))
            await db.execute("INSERT INTO transfer_requests(id,transfer_id,parent_id,ordinal,payload,metadata,state) "
                             "VALUES(?,?,?,?,?,?,'resolved')",
                             (child, transfer_id, request_id, ordinal,
                              codec.dump(TransferRequest("parcel-member", relative)), codec.dump(entry)))
            await db.execute("INSERT INTO download_files(torrent_id,request_id,filename,size_bytes,local_path,status) "
                             "VALUES(?,?,?,4,?,'queued')", (transfer_id, child, entry.name, target))
        await db.commit()
    return transfer_id


async def upgrade(*, first=True):
    """The one-time 3a0 upgrade meeting this database (``first``: as a
    database that predates it; otherwise a later start, which is a no-op)."""
    import aiosqlite
    from db.migrations.v113_decomposition_generations import MARKER
    if first:
        async with get_db() as db:
            await db.execute("DELETE FROM transfer_controls WHERE key=?", (MARKER,))
            await db.commit()
    async with aiosqlite.connect(database.DB_PATH) as raw:
        await backfill_decomposition_generations(raw)


async def test_the_upgrade_freezes_the_folder_members_already_live_in(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "upgrade.sqlite3")
    await database.init_db()
    transfer_id = await legacy_transfer("A Later Provider Name", [
        ("Show/one.bin", "/dl/The Original Folder/Show/one.bin"),
        ("Show/two.bin", "/dl/The Original Folder/Show/two.bin")])
    await upgrade()
    await upgrade(first=False)                                                      # a later start: no-op

    transfer = await TransferRepository().get(transfer_id)
    assert (transfer.collection_root, transfer.collection_root_conflict) == ("The Original Folder", False)
    (generation,) = await generations(transfer_id)
    assert (generation["interactive"], generation["decision"], generation["continuity"]) == (0, "all", "proven")
    assert generation["legacy_established"] == 1                       # pre-3a0 compatibility lineage
    assert {row["materialized_selection_id"] for row in await rows(
        "SELECT materialized_selection_id FROM transfer_requests WHERE parent_id='legacy-root'")} == {generation["id"]}
    assert [row["local_path"] for row in await files_of(transfer_id)] == [
        "/dl/The Original Folder/Show/one.bin", "/dl/The Original Folder/Show/two.bin"]   # nothing moved
    engine = TransferEngine(TransferRepository(), IntegrationRegistry(), download_root="/dl", clock=Clock())
    child = next(item for item in await engine.repository.requests(transfer_id) if item.parent_id)
    candidate = replace(child, entry=None)
    assert engine._materialization_relative(candidate, type("C", (), {"relative_path": "Show/one.bin",
                                                                       "name": "one.bin"})(), transfer) \
        == "The Original Folder/Show/one.bin"


async def test_conflicting_placement_holds_the_transfer_and_guesses_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "upgrade.sqlite3")
    await database.init_db()
    transfer_id = await legacy_transfer("Whatever", [
        ("Show/one.bin", "/dl/Folder A/Show/one.bin"),
        ("Show/two.bin", "/dl/Folder B/Show/two.bin")])
    await upgrade()

    transfer = await TransferRepository().get(transfer_id)
    assert (transfer.collection_root, transfer.collection_root_conflict) == (None, True)
    (generation,) = await generations(transfer_id)
    assert (generation["continuity"], generation["continuity_reason"]) == ("held", "collection_root_conflict")
    for artifact in await TransferRepository().artifacts(transfer_id):
        assert (await TransferRepository().materialization_authorization(artifact)).kind == \
            MaterializationAdmissionKind.HOLD
    assert [row["local_path"] for row in await files_of(transfer_id)] == [
        "/dl/Folder A/Show/one.bin", "/dl/Folder B/Show/two.bin"]                  # nothing moved
    engine = TransferEngine(TransferRepository(), IntegrationRegistry(), download_root="/dl", clock=Clock())
    child = next(item for item in await engine.repository.requests(transfer_id) if item.parent_id)
    with pytest.raises(TransferError) as refused:
        engine._materialization_relative(child, type("C", (), {"relative_path": "Show/one.bin",
                                                               "name": "one.bin"})(), transfer)
    assert refused.value.error.category == Category.LOCAL_PATH_CONFLICT


async def test_an_upgrade_never_opens_a_generation_for_a_rebind_in_progress(tmp_path, monkeypatch):
    """A root that already owns any generation is not legacy: its newer binding
    waits for the engine to open (and prove) its generation."""
    repository, _engine, _provider, _executor, transfer = await decomposed(tmp_path, monkeypatch)
    await rebind_root_without_resolution(repository, transfer.id)
    await upgrade()
    assert len(await generations(transfer.id)) == 1
    assert await runnable_generations(repository, transfer.id) == set()


# -- 9. explicit selection follows the immediate predecessor across a real rebind --------------------------------

async def test_an_explicit_subset_is_carried_and_proven_across_a_same_provider_reacquisition(tmp_path, monkeypatch):
    repository, engine, provider, _executor = await lab(tmp_path, monkeypatch)
    three = [*FILES, ("three.bin", "Show/three.bin", 4)]
    provider.responses.append(provider.parcel("x", state=ResourceState.AVAILABLE, files=three,
                                              file_manifest=None))
    transfer = await engine.submit((TransferRequest("parcel", "x", name="Show", selection_mode="interactive"),),
                                   name="Show", deduplicate=False)
    for _ in range(3):
        await engine.tick()
    view = await repository.file_selection_presentation(transfer.id, now=engine.clock())
    chosen = [entry["entry_id"] for entry in view["entries"] if entry["relative_path"] != "Show/three.bin"]
    await repository.confirm_file_selection(transfer.id, view["manifest_id"], chosen, now=engine.clock())
    for _ in range(4):
        await engine.tick()
    members = sorted(item.entry.relative_path for item in await repository.requests(transfer.id) if item.parent_id)
    assert members == ["Show/one.bin", "Show/two.bin"]

    await reacquire(repository, engine, provider, transfer, files=three)

    first, second = await generations(transfer.id)
    assert (second["decision"], second["decision_reason"], second["continuity"]) == ("explicit", "inherited", "proven")
    assert second["predecessor_id"] == first["id"]
    assert sorted(item.entry.relative_path for item in await repository.requests(transfer.id)
                  if item.parent_id) == ["Show/one.bin", "Show/two.bin"]        # never broadened to three
    assert await repository.file_selection_presentation(transfer.id, now=engine.clock()) is not None


# -- 10. repeated observation of the same binding never churns generations ---------------------------------------

async def test_observing_the_same_binding_again_never_opens_another_generation(tmp_path, monkeypatch):
    repository, engine, _provider, _executor, transfer = await decomposed(tmp_path, monkeypatch)
    root = await root_of(repository, transfer.id)
    (only,) = await generations(transfer.id)
    for _ in range(5):
        await repository.ensure_selection_generation(root, "parcel-a", root.resource, available=True,
                                                     file_manifest=None, now=engine.clock())
        await engine.tick()
    assert await generations(transfer.id) == [only]


async def test_a_default_all_root_is_never_offered_or_presented_as_a_selection(tmp_path, monkeypatch):
    repository, engine, _provider, _executor, transfer = await decomposed(tmp_path, monkeypatch)
    assert await repository.file_selection_presentation(transfer.id, now=engine.clock()) is None
    assert await repository.active_file_selection_offers(now=engine.clock()) == []
    (generation,) = await generations(transfer.id)
    assert (generation["interactive"], generation["decision"]) == (0, "all")


async def test_every_production_manifest_provider_declares_the_generation_boundary():
    """Universal generations open for roots whose provider declares
    FILE_MANIFEST (``_file_manifest_root``); every production provider that
    decomposes a root through ``manifest()`` declares it."""
    root = Path(__file__).resolve().parents[1] / "providers"
    decomposing = [path for path in root.glob("*/provider.py") if "async def manifest(" in path.read_text()]
    assert len(decomposing) >= 9
    assert [path.parent.name for path in decomposing
            if "Capability.FILE_MANIFEST" not in path.read_text()] == []


# -- canonical bindings across the ordinary reacquisition rebuild ------------------------------------------------

async def test_an_ordinary_reacquisition_rebuild_keeps_canonical_bindings_truthful_across_restarts(tmp_path, monkeypatch):
    repository, engine, provider, executor, transfer = await decomposed(tmp_path, monkeypatch)
    history = await rows("SELECT b.id FROM canonical_candidate_bindings b JOIN download_files f "
                         "ON f.id=b.canonical_artifact_id WHERE f.torrent_id=?", (transfer.id,))
    await reacquire(repository, engine, provider, transfer)

    for _ in range(2):
        repository, engine, provider, executor = await lab(tmp_path, monkeypatch, fresh=False,
                                                           provider=provider, executor=executor, clock=engine.clock)
    after = await rows("SELECT b.id,b.candidate_id,b.candidate_order,b.canonical_artifact_id FROM "
                       "canonical_candidate_bindings b JOIN download_files f ON f.id=b.canonical_artifact_id "
                       "WHERE f.torrent_id=? ORDER BY b.id", (transfer.id,))
    assert {row["id"] for row in history} <= {row["id"] for row in after}
    for artifact in await repository.artifacts(transfer.id):
        current = {str(candidate.id): position for position, candidate in enumerate(artifact.candidates, start=1)}
        mine = [row for row in after if row["canonical_artifact_id"] == artifact.id]
        assert sorted(row["candidate_order"] for row in mine if row["candidate_id"] in current) == \
            sorted(current.values())
        assert all(row["candidate_order"] > 100000 for row in mine if row["candidate_id"] not in current)


# -- the compatibility crossing needs a terminal reacquisition; interactive ALL keeps its window ----------------

async def upgraded_from_before_generations():
    import aiosqlite
    from db.migrations.v113_decomposition_generations import MARKER
    async with get_db() as db:
        await db.execute("DELETE FROM transfer_controls WHERE key=?", (MARKER,))
        await db.commit()
    async with aiosqlite.connect(database.DB_PATH) as raw:
        await backfill_decomposition_generations(raw)


async def test_a_legacy_decomposition_rebound_without_a_terminal_reacquisition_holds(tmp_path, monkeypatch):
    """Case 4: compatibility lineage alone is no permission -- an ordinary
    same-provider rebind of a pre-3a0 decomposition stays under strict D1."""
    repository, engine, provider, _executor, transfer = await decomposed(tmp_path, monkeypatch)
    await upgraded_from_before_generations()
    assert (await current_generation(repository, transfer.id))["legacy_established"] == 1
    before = await files_of(transfer.id)

    await reacquire(repository, engine, provider, transfer,
                    files=[("one.bin", "Show.2024/one.bin", 4), ("two.bin", "Show.2024/two.bin", 4)])

    held = await current_generation(repository, transfer.id)
    assert (held["continuity"], held["continuity_reason"]) == ("held", "established_member_missing")
    assert [(row["id"], row["local_path"], row["blocked"]) for row in await files_of(transfer.id)] == [
        (row["id"], row["local_path"], 0) for row in before]


async def test_an_interactive_all_predecessor_gets_a_fresh_window_that_converges_on_timeout(tmp_path, monkeypatch):
    """Case 8: an ALL predecessor is not carried; the new generation opens its
    own selection window, and its timeout to ALL proves continuity normally."""
    repository, engine, provider, _executor = await lab(tmp_path, monkeypatch)
    provider.responses.append(provider.parcel("x", state=ResourceState.AVAILABLE, files=FILES, file_manifest=None))
    transfer = await engine.submit((TransferRequest("parcel", "x", name="Show", selection_mode="interactive"),),
                                   name="Show", deduplicate=False)
    for _ in range(3):
        await engine.tick()
    engine.clock.now += 300                                          # the first window times out: ALL
    for _ in range(4):
        await engine.tick()
    assert (await current_generation(repository, transfer.id))["decision"] == "all"
    before = await files_of(transfer.id)

    await reacquire(repository, engine, provider, transfer, ticks=2)
    fresh = await current_generation(repository, transfer.id)
    assert (fresh["decision"], fresh["decision_reason"], fresh["manifest_committed_at"]) == ("pending", None, None)

    engine.clock.now += 300                                          # its own window times out: ALL
    for _ in range(6):
        engine.clock.now += 60
        await engine.tick()
    committed = await current_generation(repository, transfer.id)
    assert (committed["decision"], committed["continuity"]) == ("all", "proven")
    assert [(row["id"], row["local_path"]) for row in await files_of(transfer.id)] == [
        (row["id"], row["local_path"]) for row in before]
