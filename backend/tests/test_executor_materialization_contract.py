"""One generalized materialization verifier/cleanup owner for FILE and COLLECTION.

The executor reports what it produced relative to the core-authorized plan;
core verifies it on the local filesystem, persists the verified result, hands
verified paths to post-processing, and retires only material it can prove the
execution owns.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import db.database as database
from executor_fakes import RecordingPostProcessor, artifact_of, ledger_core, submit_ledger
from transfers.errors import Category
from transfers.models import MaterializationKind, MaterializedEntry, TransferState

pytestmark = pytest.mark.asyncio


async def _complete(core, transfer_id, rounds=3):
    for _ in range(rounds):
        await core.engine.tick()
    return await core.repository.get(transfer_id)


async def _stored_materialization(attempt_id):
    async with database.get_db() as db:
        row = await db.fetchone("SELECT materialization FROM execution_attempts WHERE id=?", (attempt_id,))
    return json.loads(row["materialization"]) if row and row["materialization"] else None


async def test_file_executor_reports_and_verifies_single_file_result(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer = await submit_ledger(core, "single")
    artifact = await artifact_of(core, transfer.id)
    assert artifact.candidates[0].materialization == MaterializationKind.FILE
    core.executor.finish_file(artifact.execution, artifact.target)
    assert (await _complete(core, transfer.id)).state == TransferState.COMPLETED
    stored = await _stored_materialization(artifact.execution.attempt_id)
    assert stored == {"kind": "file", "entries": [{"relative_path": Path(artifact.target).name, "bytes": 4}]}


async def test_collection_executor_reports_and_verifies_multiple_files(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer = await submit_ledger(core, "bundle:collection")
    artifact = await artifact_of(core, transfer.id)
    assert artifact.candidates[0].materialization == MaterializationKind.COLLECTION
    core.executor.finish_collection(artifact.execution, artifact.target)
    assert (await _complete(core, transfer.id)).state == TransferState.COMPLETED
    current = await artifact_of(core, transfer.id)
    assert current.state == "completed" and current.expected_bytes == 4
    stored = await _stored_materialization(artifact.execution.attempt_id)
    assert stored["kind"] == "collection"
    assert sorted(item["relative_path"] for item in stored["entries"]) == ["nested/part-2.bin", "part-1.bin"]


async def test_collection_output_paths_feed_postprocessor_as_plural_paths(tmp_path, monkeypatch):
    post = RecordingPostProcessor()
    core = await ledger_core(tmp_path, monkeypatch, postprocessors=(post,))
    transfer = await submit_ledger(core, "bundle:collection")
    artifact = await artifact_of(core, transfer.id)
    core.executor.finish_collection(artifact.execution, artifact.target)
    assert (await _complete(core, transfer.id, rounds=4)).state == TransferState.COMPLETED
    assert len(post.calls) == 1
    root = Path(artifact.target)
    assert sorted(post.calls[0][1]) == sorted([str(root / "nested/part-2.bin"), str(root / "part-1.bin")])


async def _rejected(core, transfer_id):
    for _ in range(2):
        await core.engine.reconcile_executions()
    artifact = await artifact_of(core, transfer_id)
    return artifact


async def _collection(core, name="bundle"):
    transfer = await submit_ledger(core, f"{name}:collection")
    return transfer, await artifact_of(core, transfer.id)


def _failed_verification(artifact):
    return artifact.state in {"error", "recovery_wait", "queued", "unresolved"} and artifact.state != "completed"


async def test_collection_path_escape_is_rejected(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer, artifact = await _collection(core)
    outside = Path(artifact.target).parent / "escape.bin"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"xx")
    core.executor.finish_collection(artifact.execution, artifact.target, entries=(
        MaterializedEntry("part-1.bin", 2), MaterializedEntry("../escape.bin", 2)))
    current = await _rejected(core, transfer.id)
    assert current.state != "completed" and outside.exists()
    for bad in (str(outside), "/etc/passwd"):
        other, other_artifact = await _collection(core, f"abs{len(bad)}")
        core.executor.finish_collection(other_artifact.execution, other_artifact.target,
                                        entries=(MaterializedEntry(bad, 2),))
        assert (await _rejected(core, other.id)).state != "completed"


async def test_collection_symlink_escape_is_rejected(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer, artifact = await _collection(core)
    secret = tmp_path / "outside-secret.bin"
    secret.write_bytes(b"zz")
    core.executor.finish_collection(artifact.execution, artifact.target, files={"part-1.bin": b"ab"},
                                    entries=(MaterializedEntry("part-1.bin", 2), MaterializedEntry("link.bin", 2)))
    os.symlink(secret, Path(artifact.target) / "link.bin")
    assert (await _rejected(core, transfer.id)).state != "completed"
    assert secret.exists()


async def test_duplicate_collection_relative_path_is_rejected(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer, artifact = await _collection(core)
    core.executor.finish_collection(artifact.execution, artifact.target, files={"part-1.bin": b"ab"},
                                    entries=(MaterializedEntry("part-1.bin", 2), MaterializedEntry("./part-1.bin", 2)))
    assert (await _rejected(core, transfer.id)).state != "completed"


async def test_executor_reported_size_is_verified_against_local_file(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer, artifact = await _collection(core)
    core.executor.finish_collection(artifact.execution, artifact.target, files={"part-1.bin": b"ab"},
                                    entries=(MaterializedEntry("part-1.bin", 999),))
    current = await _rejected(core, transfer.id)
    assert current.state != "completed"
    assert current.error is None or current.error.category in {Category.MATERIALIZATION_FAILED,
                                                               Category.TRANSFER_FAILED}


async def test_success_without_valid_materialization_fails_verification(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer = await submit_ledger(core, "single")
    artifact = await artifact_of(core, transfer.id)
    core.executor.finish_file(artifact.execution, artifact.target)
    core.executor.job_for(artifact.execution).materialization = None
    assert (await _rejected(core, transfer.id)).state != "completed"
    assert await _stored_materialization(artifact.execution.attempt_id) is None


async def test_execution_owned_invalid_file_material_can_be_retired(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer = await submit_ledger(core, "single")
    artifact = await artifact_of(core, transfer.id)
    core.executor.finish_file(artifact.execution, artifact.target, content=b"wrong-size-content")
    job = core.executor.job_for(artifact.execution)
    job.materialization = type(job.materialization)(MaterializationKind.FILE, (
        MaterializedEntry(Path(artifact.target).name, 4),))
    Path(artifact.target + ".ledger-journal").write_bytes(b"journal")
    await _rejected(core, transfer.id)
    assert not Path(artifact.target).exists()
    assert not Path(artifact.target + ".ledger-journal").exists()


async def test_execution_owned_invalid_collection_can_be_retired_safely(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer, artifact = await _collection(core)
    sibling = Path(artifact.target).parent / "unrelated.bin"
    sibling.parent.mkdir(parents=True, exist_ok=True)
    sibling.write_bytes(b"keep")
    core.executor.finish_collection(artifact.execution, artifact.target,
                                    entries=(MaterializedEntry("part-1.bin", 999),))
    await _rejected(core, transfer.id)
    assert not Path(artifact.target).exists()
    assert sibling.read_bytes() == b"keep"


async def test_preexisting_collection_root_is_never_recursively_deleted(tmp_path, monkeypatch):
    from transfers.models import TransferRequest

    core = await ledger_core(tmp_path, monkeypatch)
    transfer = await core.engine.submit((TransferRequest("ledger", "bundle:collection", name="bundle"),))
    await core.engine.resolve_pending()
    artifact = await artifact_of(core, transfer.id)
    root = Path(artifact.target)
    root.mkdir(parents=True)
    (root / "operator-owned.bin").write_bytes(b"precious")
    await core.engine.reconcile_executions()
    artifact = await artifact_of(core, transfer.id)
    assert artifact.execution is not None
    core.executor.finish_collection(artifact.execution, artifact.target,
                                    entries=(MaterializedEntry("part-1.bin", 999),))
    await _rejected(core, transfer.id)
    assert (root / "operator-owned.bin").read_bytes() == b"precious"


async def test_transient_executor_paths_are_not_postprocessor_outputs(tmp_path, monkeypatch):
    post = RecordingPostProcessor()
    core = await ledger_core(tmp_path, monkeypatch, postprocessors=(post,))
    transfer, artifact = await _collection(core)
    journal = Path(artifact.target) / ".ledger-journal"
    core.executor.finish_collection(artifact.execution, artifact.target)
    journal.write_bytes(b"native bookkeeping")
    assert (await _complete(core, transfer.id, rounds=4)).state == TransferState.COMPLETED
    assert post.calls and str(journal) not in post.calls[0][1]

    other, other_artifact = await _collection(core, "second")
    core.executor.finish_collection(other_artifact.execution, other_artifact.target,
                                    files={"part-1.bin": b"ab", ".ledger-journal": b"jj"})
    assert (await _rejected(core, other.id)).state != "completed"  # a transient path is never final material


async def test_collection_report_must_account_for_every_produced_file(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer, artifact = await _collection(core)
    core.executor.finish_collection(artifact.execution, artifact.target,
                                    files={"a.rar": b"aa", "b.rar": b"bb"},
                                    entries=(MaterializedEntry("a.rar", 2),))
    current = await _rejected(core, transfer.id)
    assert current.state != "completed"  # b.rar was produced but not reported
    assert await _stored_materialization(artifact.execution.attempt_id) is None


async def test_transient_tree_is_excluded_from_census_and_postprocessing(tmp_path, monkeypatch):
    post = RecordingPostProcessor()
    core = await ledger_core(tmp_path, monkeypatch, postprocessors=(post,))
    transfer, artifact = await _collection(core)
    core.executor.finish_collection(artifact.execution, artifact.target)
    partial = Path(artifact.target) / ".ledger-partial" / "deep"
    partial.mkdir(parents=True)
    (partial / "chunk.part").write_bytes(b"native partial")
    assert (await _complete(core, transfer.id, rounds=4)).state == TransferState.COMPLETED
    assert post.calls and not any(".ledger-partial" in path for path in post.calls[0][1])


async def test_owned_transient_tree_is_retired_with_invalid_collection(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer, artifact = await _collection(core)
    core.executor.finish_collection(artifact.execution, artifact.target,
                                    entries=(MaterializedEntry("part-1.bin", 999),))
    partial = Path(artifact.target) / ".ledger-partial"
    partial.mkdir()
    (partial / "chunk.part").write_bytes(b"native partial")
    await _rejected(core, transfer.id)
    assert not Path(artifact.target).exists()


async def test_unowned_material_is_never_retired_by_the_cleanup_owner(tmp_path):
    from transfers.filesystem import retire_materialization
    from transfers.models import ExecutionFootprint, MaterializationPlan

    root = tmp_path / "downloads"
    (root / "tree").mkdir(parents=True)
    target = root / "file.bin"
    target.write_bytes(b"operator")
    (root / "file.bin.journal").write_bytes(b"j")
    (root / "tree" / "x").write_bytes(b"t")
    (root / "bundle").mkdir()
    (root / "bundle" / "keep.bin").write_bytes(b"k")
    footprint = ExecutionFootprint((str(root / "file.bin.journal"),), (str(root / "tree"),))
    for plan in (MaterializationPlan(MaterializationKind.FILE, str(root), str(target)),
                 MaterializationPlan(MaterializationKind.COLLECTION, str(root / "bundle"))):
        retire_materialization(str(root), plan, footprint, owned=False)
    assert target.read_bytes() == b"operator" and (root / "file.bin.journal").exists()
    assert (root / "tree" / "x").exists() and (root / "bundle" / "keep.bin").exists()
    retire_materialization(str(root), MaterializationPlan(MaterializationKind.FILE, str(root), str(target)),
                           footprint, owned=True)
    assert not target.exists() and not (root / "file.bin.journal").exists() and not (root / "tree").exists()


class _TwoSourceProvider:
    def __init__(self):
        from transfers.applicability import ProviderApplicability
        from transfers.models import Capability, IntegrationDescriptor
        self.descriptor = IntegrationDescriptor("two-source", "Two source", frozenset({Capability.RESOLVE}),
                                                request_types=frozenset({"ledger"}))
        self._applicability = ProviderApplicability()

    @property
    def applicability(self):
        return self._applicability

    async def resolve(self, request):
        from transfers.models import ResolutionResult, ResourceState, SourceIdentity, TransferCandidate
        return ResolutionResult(ResourceState.AVAILABLE, tuple(
            TransferCandidate("switch.bin", (), expected_bytes=4, provider_id=self.descriptor.id, priority=10 - index,
                              source_identity=SourceIdentity("host", f"mirror-{label}.example"),
                              context={"route": label})
            for index, label in enumerate(("a", "b"))))


def _route_claimant(identity, label):
    from executor_fakes import LedgerExecutor
    from transfers.models import ExecutorClaim

    class RouteExecutor(LedgerExecutor):
        def claim(self, subject):
            return ExecutorClaim(subject.candidate.context.get("route") == label)

    def build(authorize):
        return RouteExecutor(authorize, identity=identity)
    return build


async def _switch_core(tmp_path, monkeypatch):
    first, second = _route_claimant("route-a", "a"), _route_claimant("route-b", "b")
    return await ledger_core(tmp_path, monkeypatch, providers=(_TwoSourceProvider(),),
                             executors=lambda authorize: (first(authorize), second(authorize)))


async def test_candidate_switch_never_retires_material_the_old_writer_does_not_own(tmp_path, monkeypatch):
    from transfers.models import TransferRequest
    core = await _switch_core(tmp_path, monkeypatch)
    transfer = await core.engine.submit((TransferRequest("ledger", "switch", name="switch.bin"),))
    await core.engine.resolve_pending()
    artifact = await artifact_of(core, transfer.id)
    assert len(artifact.candidates) == 2
    target = Path(artifact.target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"pre-existing")                    # present before any execution admission
    Path(str(target) + ".ledger-journal").write_bytes(b"pre-existing journal")
    await core.engine.reconcile_executions()
    artifact = await artifact_of(core, transfer.id)
    assert artifact.execution.executor_id == "route-a"
    assert not await core.repository.execution_owns_target(artifact.execution)
    result = await core.engine.activate_candidate_command(transfer.id, artifact.id, 1)
    assert result is not None and result.committed
    assert target.read_bytes() == b"pre-existing"
    assert Path(str(target) + ".ledger-journal").read_bytes() == b"pre-existing journal"


async def test_candidate_switch_retires_material_the_old_writer_owns(tmp_path, monkeypatch):
    from transfers.models import TransferRequest
    core = await _switch_core(tmp_path, monkeypatch)
    transfer = await core.engine.submit((TransferRequest("ledger", "switch", name="switch.bin"),))
    await core.engine.tick()
    artifact = await artifact_of(core, transfer.id)
    assert await core.repository.execution_owns_target(artifact.execution)
    target = Path(artifact.target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"partial written by the owning attempt")
    result = await core.engine.activate_candidate_command(transfer.id, artifact.id, 1)
    assert result is not None and result.committed
    assert not target.exists()


async def test_retry_attempt_inherits_its_artifacts_material_ownership(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer = await submit_ledger(core, "single")
    first = (await artifact_of(core, transfer.id)).execution
    assert await core.repository.execution_owns_target(first)
    target = Path((await artifact_of(core, transfer.id)).target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"partial")                          # the owning attempt's own partial
    core.executor.fail(first)
    for _ in range(3):
        core.now[0] += 5
        await core.engine.reconcile_executions()
    second = (await artifact_of(core, transfer.id)).execution
    assert second.attempt_id != first.attempt_id
    async with database.get_db() as db:
        row = await db.fetchone("SELECT target_initially_absent,material_owner_attempt_id FROM execution_attempts WHERE id=?",
                                (second.attempt_id,))
    assert row["target_initially_absent"] == 0                # material was present at its admission...
    assert row["material_owner_attempt_id"] == first.attempt_id  # ...and is DebridPulse's own lineage
    assert await core.repository.execution_owns_target(second)


# ---------------------------------------------------------------------------
# Deleted transfers: incomplete material the stopped execution positively owns
# ---------------------------------------------------------------------------


async def _restarted(core):
    """A fresh engine over the same durable state: nothing in-memory survives."""
    from transfers.convergence_engine import TransferEngine
    from transfers.recovery_repository import TransferRepository

    engine = TransferEngine(TransferRepository(), core.registry, download_root=core.engine.root,
                            policy=core.engine.policy, clock=lambda: core.now[0])
    await engine.initialize()
    return engine


async def _cleanup_state(attempt_id):
    async with database.get_db() as db:
        row = await db.fetchone("SELECT cleanup_state FROM execution_attempts WHERE id=?", (attempt_id,))
    return row["cleanup_state"] if row else None


async def _preallocated(core, payload="single", content=b"\x00" * 16):
    """An admitted execution that has since created its own partial target."""
    transfer = await submit_ledger(core, payload)
    artifact = await artifact_of(core, transfer.id)
    assert artifact.execution is not None
    assert await core.repository.execution_owns_target(artifact.execution)
    target = Path(artifact.target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    core.executor.run(artifact.execution)
    return transfer, artifact


async def test_deleting_a_transfer_retires_the_incomplete_material_its_execution_owns(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer, artifact = await _preallocated(core)
    assert Path(artifact.target).exists()

    await core.engine.delete(transfer.id)

    assert not Path(artifact.target).exists()
    assert await _cleanup_state(artifact.execution.attempt_id) == "complete"


async def test_deleting_a_transfer_never_removes_material_its_execution_does_not_own(tmp_path, monkeypatch):
    from transfers.models import TransferRequest

    core = await ledger_core(tmp_path, monkeypatch)
    transfer = await core.engine.submit((TransferRequest("ledger", "single", name="single"),))
    await core.engine.resolve_pending()
    artifact = await artifact_of(core, transfer.id)
    target = Path(artifact.target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"operator material")          # pre-dates the execution
    await core.engine.reconcile_executions()
    artifact = await artifact_of(core, transfer.id)
    assert artifact.execution is not None
    assert not await core.repository.execution_owns_target(artifact.execution)
    core.executor.run(artifact.execution)

    await core.engine.delete(transfer.id)

    assert target.read_bytes() == b"operator material"
    assert await _cleanup_state(artifact.execution.attempt_id) == "complete"


async def test_unconfirmed_native_stop_preserves_owned_material_and_keeps_cleanup_pending(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer, artifact = await _preallocated(core)
    core.executor.cancel_mode = "unconfirmed"

    await core.engine.delete(transfer.id)

    assert Path(artifact.target).exists()
    assert await _cleanup_state(artifact.execution.attempt_id) in {"pending", "blocked"}


async def test_pending_cleanup_retires_owned_material_after_restart(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer, artifact = await _preallocated(core)
    core.executor.cancel_mode = "unconfirmed"
    await core.engine.delete(transfer.id)
    assert Path(artifact.target).exists()

    restarted = await _restarted(core)
    core.executor.cancel_mode = "confirm"
    core.now[0] += 1000
    await restarted.reconcile_executions()

    assert not Path(artifact.target).exists()
    assert await _cleanup_state(artifact.execution.attempt_id) == "complete"


async def test_retaining_the_provider_resource_still_reclaims_owned_local_material(tmp_path, monkeypatch):
    """``remote=False`` withholds provider-resource deletion only. It says
    nothing about material a deleted local execution owns."""
    core = await ledger_core(tmp_path, monkeypatch)
    transfer, artifact = await _preallocated(core)

    await core.engine.delete(transfer.id, remote=False)

    assert not Path(artifact.target).exists()
    assert await _cleanup_state(artifact.execution.attempt_id) == "complete"


async def test_deleting_a_completed_transfer_never_sweeps_its_delivered_payload(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer = await submit_ledger(core, "single")
    artifact = await artifact_of(core, transfer.id)
    core.executor.finish_file(artifact.execution, artifact.target)
    assert (await _complete(core, transfer.id)).state == TransferState.COMPLETED

    await core.engine.delete(transfer.id)

    assert Path(artifact.target).read_bytes() == b"done"


async def test_retiring_an_owned_target_prunes_only_the_empty_scaffolding_it_left(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer, artifact = await _preallocated(core, "Transfer/A/B/file.bin")
    target = Path(artifact.target)
    assert target.parent.name == "B"

    await core.engine.delete(transfer.id)

    assert not target.exists()
    assert not (core.root / "Transfer").exists()
    assert core.root.is_dir()


async def test_scaffolding_pruning_never_follows_a_symlinked_ancestor(tmp_path, monkeypatch):
    """A symlinked ancestor is a boundary, not a directory to walk through.

    The owned target is still reclaimed -- it is a real file and the execution
    owns it -- but the directory the link points at belongs to whoever created
    it, so pruning stops at the link rather than reaching through it and
    leaving the link dangling."""
    core = await ledger_core(tmp_path, monkeypatch)
    root = Path(core.engine.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    real = root / "real"
    real.mkdir()
    (root / "link").symlink_to(real, target_is_directory=True)

    transfer, artifact = await _preallocated(core, "link/file.bin")
    assert Path(artifact.target) == root / "link" / "file.bin"
    assert (real / "file.bin").exists()

    await core.engine.delete(transfer.id)

    assert not (real / "file.bin").exists()   # the owned target is still reclaimed
    assert real.is_dir()                      # the linked-to directory is never pruned
    assert (root / "link").is_symlink()       # the link is neither followed nor removed
    assert root.is_dir()


async def test_scaffolding_pruning_stops_at_the_first_directory_holding_other_material(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer, artifact = await _preallocated(core, "Transfer/A/B/file.bin")
    keep = Path(artifact.target).parent / "keep.bin"
    keep.write_bytes(b"unrelated")

    await core.engine.delete(transfer.id)

    assert not Path(artifact.target).exists()
    assert keep.read_bytes() == b"unrelated"
    assert keep.parent.is_dir()
    assert (core.root / "Transfer" / "A").is_dir()
