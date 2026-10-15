"""DP 1.0.13: an acknowledged delivery's empty collection folder is retired.

Radarr imported transfer 639's MKV by moving it, leaving DebridPulse's empty
collection folder behind. Acknowledging a delivery never touched the folder.
Now, when an external client acknowledges its CURRENT delivery generation
and every outstanding claim on that transfer is such an acknowledged
delivery, the core removes each completed collection artifact's folder that
DebridPulse durably owns -- only if it is empty at that instant, through
``filesystem.remove_empty_directory`` (descriptor-relative, ``O_NOFOLLOW``,
``rmdir`` only) inside one transaction that re-checks the claims. A copy-style
import, any leftover, another claimant, a live writer or any doubt keeps it.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

import db.database as database
from test_arr_sabnzbd_adapter import call, sab, shared_magnet, submitted  # noqa: F401 - fixtures
from test_external_client_foundation import complete, demands, http, transfer_of, world  # noqa: F401 - fixtures
from test_v113_runtime_telemetry_matrix import VALID_NZB, converge, mixed  # noqa: F401 - real SAB + Usenet
from transfers import filesystem
from transfers.models import SubmissionDemand, TransferRequest, TransferState

pytestmark = pytest.mark.asyncio


# ── the primitive ───────────────────────────────────────────────────────────

def tree(tmp_path):
    root = tmp_path / "downloads"
    (root / "Release").mkdir(parents=True)
    return root, root / "Release"


async def test_an_empty_owned_directory_is_removed_and_nothing_else(tmp_path):
    root, folder = tree(tmp_path)
    assert filesystem.remove_empty_directory(str(root), str(folder)) == "removed"
    assert not folder.exists() and root.is_dir()
    assert filesystem.remove_empty_directory(str(root), str(folder)) == "absent"      # idempotent


async def test_a_directory_with_anything_in_it_is_left_exactly_as_it_is(tmp_path):
    root, folder = tree(tmp_path)
    (folder / "Release.mkv").write_bytes(b"copy-imported")
    (folder / "sub").mkdir()
    assert filesystem.remove_empty_directory(str(root), str(folder)) == "not_empty"
    assert (folder / "Release.mkv").read_bytes() == b"copy-imported" and (folder / "sub").is_dir()


@pytest.mark.parametrize("shape", ["symlink_to_empty_dir", "regular_file", "symlinked_parent", "outside_root",
                                   "the_root_itself", "dot_dot"])
async def test_anything_but_the_exact_directory_is_refused_and_untouched(tmp_path, shape):
    root, folder = tree(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = folder
    if shape == "symlink_to_empty_dir":
        folder.rmdir()
        folder.symlink_to(elsewhere, target_is_directory=True)
    elif shape == "regular_file":
        folder.rmdir()
        folder.write_bytes(b"x")
    elif shape == "symlinked_parent":
        (elsewhere / "Release").mkdir()
        (root / "linked").symlink_to(elsewhere, target_is_directory=True)
        target = root / "linked" / "Release"
    elif shape == "outside_root":
        target = elsewhere
    elif shape == "the_root_itself":
        target = root
    else:
        target = Path(f"{root}/x/../Release")
    assert filesystem.remove_empty_directory(str(root), str(target)) == "refused"
    assert elsewhere.is_dir() and root.is_dir()
    if shape == "symlink_to_empty_dir":
        assert folder.is_symlink()
    if shape == "regular_file":
        assert folder.read_bytes() == b"x"
    if shape == "symlinked_parent":
        assert (elsewhere / "Release").is_dir()


async def test_a_symlink_swapped_in_after_the_check_is_never_followed_or_removed(tmp_path, monkeypatch):
    root, folder = tree(tmp_path)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    real_stat = os.stat

    def swap_after_check(path, *args, **kwargs):
        facts = real_stat(path, *args, **kwargs)
        if path == "Release" and kwargs.get("dir_fd") is not None:
            folder.rmdir()                                             # the race: replaced by a link
            folder.symlink_to(decoy, target_is_directory=True)
        return facts

    monkeypatch.setattr(filesystem.os, "stat", swap_after_check)
    monkeypatch.setattr(filesystem.os, "supports_dir_fd", os.supports_dir_fd | {swap_after_check})
    assert filesystem.remove_empty_directory(str(root), str(folder)) == "refused"
    assert folder.is_symlink() and decoy.is_dir()                     # neither the link nor its target


async def test_a_file_swapped_in_after_the_check_is_never_removed(tmp_path, monkeypatch):
    root, folder = tree(tmp_path)
    real_stat = os.stat

    def swap_after_check(path, *args, **kwargs):
        facts = real_stat(path, *args, **kwargs)
        if path == "Release" and kwargs.get("dir_fd") is not None:
            folder.rmdir()
            folder.write_bytes(b"replacement")
        return facts

    monkeypatch.setattr(filesystem.os, "stat", swap_after_check)
    monkeypatch.setattr(filesystem.os, "supports_dir_fd", os.supports_dir_fd | {swap_after_check})
    assert filesystem.remove_empty_directory(str(root), str(folder)) == "refused"
    assert folder.read_bytes() == b"replacement"


# ── the core retirement on a real completed SAB collection ─────────────────

async def completed_collection(mixed, account="radarr"):  # noqa: F811
    demand = SubmissionDemand(account)
    transfer = await mixed.engine.submit((TransferRequest("nzb", VALID_NZB, name="Release.nzb", name_supplied=True),),
                                         name="Release", deduplicate=False, demand=demand)
    await converge(mixed)
    [job] = mixed.sab.queue.values()
    mixed.sab.finish(job.nzo_id, files=(("Release.mkv", 4096),))
    await mixed.engine.reconcile_executions()
    await converge(mixed)
    assert (await mixed.repository.get(transfer.id)).state == TransferState.COMPLETED
    [artifact] = await mixed.repository.artifacts(transfer.id)
    folder = Path(artifact.target)
    assert folder.is_dir() and (folder / "Release.mkv").is_file()
    return transfer.id, artifact, folder, demand.id


def moved_away(folder, tmp_path):
    """What a move-style import leaves: the folder, empty."""
    library = tmp_path / "library"
    library.mkdir(exist_ok=True)
    for item in folder.iterdir():
        shutil.move(str(item), str(library / item.name))


async def journal_events(transfer_id):
    async with database.get_db() as db:
        return [row["event_type"] for row in await db.fetchall(
            "SELECT event_type FROM event_journal WHERE transfer_id=? ORDER BY id", (transfer_id,))]


async def test_b1_a_settled_empty_owned_folder_is_retired_and_its_history_kept(mixed, tmp_path):  # noqa: F811
    transfer_id, artifact, folder, demand = await completed_collection(mixed)
    moved_away(folder, tmp_path)
    outcomes = await mixed.engine.retire_vacated_collection(transfer_id, expected_demand=frozenset({demand}))
    assert outcomes == {artifact.id: "removed"} and not folder.exists() and Path(mixed.engine.root).is_dir()
    assert "execution.folder_retired" in await journal_events(transfer_id)
    # The transfer, its artifact record (former destination) and demand stand.
    assert (await mixed.repository.get(transfer_id)).state == TransferState.COMPLETED
    [kept] = await mixed.repository.artifacts(transfer_id)
    assert kept.target == artifact.target and kept.state == "completed"
    # B5: replayed, it is a no-op.
    assert await mixed.engine.retire_vacated_collection(
        transfer_id, expected_demand=frozenset({demand})) == {artifact.id: "absent"}


async def test_b3_a_copy_style_import_or_leftover_keeps_the_folder_and_all_of_it(mixed):  # noqa: F811
    transfer_id, artifact, folder, demand = await completed_collection(mixed)
    outcomes = await mixed.engine.retire_vacated_collection(transfer_id, expected_demand=frozenset({demand}))
    assert outcomes == {artifact.id: "not_empty"}
    assert (folder / "Release.mkv").stat().st_size == 4096
    assert "execution.folder_retired" not in await journal_events(transfer_id)


async def test_b4_a_new_claim_at_commit_keeps_the_folder(mixed, tmp_path):  # noqa: F811
    transfer_id, artifact, folder, demand = await completed_collection(mixed)
    moved_away(folder, tmp_path)
    async with database.get_db() as db:
        await db.execute("INSERT INTO transfer_demands(id,transfer_id,account_id,origin,created_at) "
                         "VALUES('late-claim',?,'someone','submission',1)", (transfer_id,))
        await db.commit()
    outcomes = await mixed.engine.retire_vacated_collection(transfer_id, expected_demand=frozenset({demand}))
    assert outcomes == {artifact.id: "claimed"} and folder.is_dir()


async def test_b4_a_live_writer_or_an_unfinished_transfer_keeps_the_folder(mixed, tmp_path):  # noqa: F811
    transfer_id, artifact, folder, demand = await completed_collection(mixed)
    moved_away(folder, tmp_path)
    async with database.get_db() as db:
        await db.execute("UPDATE execution_attempts SET state='running' WHERE artifact_id=?", (artifact.id,))
        await db.commit()
    assert await mixed.engine.retire_vacated_collection(
        transfer_id, expected_demand=frozenset({demand})) == {artifact.id: "not_eligible"}
    async with database.get_db() as db:
        await db.execute("UPDATE execution_attempts SET state='succeeded' WHERE artifact_id=?", (artifact.id,))
        await db.execute("UPDATE torrents SET status='cancelled' WHERE id=?", (transfer_id,))
        await db.commit()
    # B7: a cancelled (or failed, withdrawn) transfer is never this path's.
    assert await mixed.engine.retire_vacated_collection(transfer_id, expected_demand=frozenset({demand})) == {}
    assert folder.is_dir()


async def test_b4_material_dp_never_owned_is_never_retired(mixed, tmp_path):  # noqa: F811
    transfer_id, artifact, folder, demand = await completed_collection(mixed)
    moved_away(folder, tmp_path)
    async with database.get_db() as db:
        await db.execute("UPDATE execution_attempts SET material_owner_attempt_id=NULL WHERE artifact_id=?",
                         (artifact.id,))
        await db.commit()
    assert await mixed.engine.retire_vacated_collection(transfer_id, expected_demand=frozenset({demand})) == {}
    assert folder.is_dir()


# ── eligibility: only an acknowledged delivery, only when every claim is ─────

@pytest.fixture
def retirements(sab, world, monkeypatch):  # noqa: F811
    calls = []

    async def spy(transfer_id, *, expected_demand, settled=None):
        calls.append((transfer_id, expected_demand))
        return {}

    monkeypatch.setattr(world.engine, "retire_vacated_collection", spy)
    return calls


async def remove(sab, nzo_id, mode="history"):
    response = await call(sab, mode, name="delete", value=nzo_id, del_files="1")
    assert response.json()["status"] is True, response.text


async def test_b1_the_acknowledging_remove_of_the_only_client_asks_once_per_acknowledgement(
        sab, world, retirements):  # noqa: F811
    nzo_id = await submitted(sab)
    transfer_id = await transfer_of(nzo_id)
    await complete(world, transfer_id)
    await remove(sab, nzo_id)                         # *arr's "remove completed" = acknowledge
    [demand] = await demands(transfer_id)
    assert retirements == [(transfer_id, frozenset({demand["id"]}))]
    # The acknowledgement is recorded regardless; nothing was withdrawn.
    async with database.get_db() as db:
        row = await db.fetchone("SELECT acknowledged_at FROM external_deliveries")
    assert row["acknowledged_at"] is not None and demand["withdrawn_at"] is None


async def test_b2_no_acknowledgement_no_retirement(sab, world, retirements):  # noqa: F811
    nzo_id = await submitted(sab)
    transfer_id = await transfer_of(nzo_id)
    await complete(world, transfer_id)
    # Imported, polled as Completed, never removed (or acknowledged): nothing.
    await call(sab, "history")
    await call(sab, "queue")
    assert retirements == []


async def test_b2_b6_another_claim_on_the_same_work_keeps_it_until_that_one_is_settled_too(
        sab, world, retirements):  # noqa: F811
    mine, theirs, transfer_id = await shared_magnet(world, sab)
    await complete(world, transfer_id)
    await remove(sab, mine)
    assert retirements == []                          # the other client's delivery is unacknowledged
    view = await world.external.job(world.clients.r, theirs)
    await world.external.acknowledge(world.clients.r, theirs, {
        "delivery_id": view["delivery"]["delivery_id"], "generation": view["delivery"]["generation"]})
    assert [(tid, len(claims)) for tid, claims in retirements] == [(transfer_id, 2)]


async def test_b2_an_operators_own_request_keeps_the_folder(sab, world, retirements):  # noqa: F811
    nzo_id = await submitted(sab)
    transfer_id = await transfer_of(nzo_id)
    async with database.get_db() as db:
        await db.execute("INSERT INTO transfer_demands(id,transfer_id,account_id,origin,created_at) "
                         "VALUES('operator-demand',?,?,'submission',1)", (transfer_id, "00000000-0000-4000-8000-000000000001"))
        await db.commit()
    await complete(world, transfer_id)
    await remove(sab, nzo_id)
    assert retirements == []


async def test_a_retirement_failure_never_costs_the_acknowledgement(sab, world, monkeypatch):  # noqa: F811
    nzo_id = await submitted(sab)
    transfer_id = await transfer_of(nzo_id)
    await complete(world, transfer_id)

    async def broken(*_args, **_kwargs):
        raise OSError("filesystem trouble")

    monkeypatch.setattr(world.engine, "retire_vacated_collection", broken)
    await remove(sab, nzo_id)
    async with database.get_db() as db:
        row = await db.fetchone("SELECT acknowledged_at FROM external_deliveries")
    assert row["acknowledged_at"] is not None
    assert (await world.repository.get(transfer_id)).state == TransferState.COMPLETED


@pytest.mark.parametrize("newer_generation", [True, False])
async def test_a_newer_unacknowledged_generation_after_eligibility_keeps_the_folder(
        sab, world, tmp_path, monkeypatch, newer_generation):  # noqa: F811
    """The preliminary check found every claim acknowledged; before the final
    transaction, a newer delivery generation of that job commits, not yet
    acknowledged. The final transaction re-proves the claims and refuses: the
    folder stays. (Control: with no newer generation, it is removed.)"""
    nzo_id = await submitted(sab)
    transfer_id = await transfer_of(nzo_id)
    await complete(world, transfer_id)
    [artifact] = await world.repository.artifacts(transfer_id)
    root = tmp_path / "downloads"
    folder = root / "Release"
    folder.mkdir(parents=True)
    outcomes = []

    async def racing(target_transfer, *, expected_demand, settled=None):
        if newer_generation:
            async with database.get_db() as db:
                await db.execute("BEGIN IMMEDIATE")
                current = await db.fetchone(
                    "SELECT * FROM external_deliveries WHERE job_id=? ORDER BY generation DESC LIMIT 1", (nzo_id,))
                await db.execute("UPDATE external_deliveries SET superseded_at=1 WHERE job_id=? AND generation=?",
                                 (nzo_id, current["generation"]))
                columns = [key for key in current.keys()]
                values = {**current, "generation": current["generation"] + 1, "acknowledged_at": None,
                          "acknowledged_by": None, "superseded_at": None}
                await db.execute(f"INSERT INTO external_deliveries({','.join(columns)}) "
                                 f"VALUES({','.join('?' * len(columns))})", tuple(values[key] for key in columns))
                await db.commit()
        outcomes.append(await world.repository.retire_vacated_directory(
            artifact.id, root=str(root), target=str(folder), expected_demand=expected_demand, settled=settled))
        return {}

    monkeypatch.setattr(world.engine, "retire_vacated_collection", racing)
    await remove(sab, nzo_id)
    if newer_generation:
        assert outcomes == ["unsettled"] and folder.is_dir()
    else:
        assert outcomes == ["removed"] and not folder.exists()
    # The acknowledgement itself stands either way.
    async with database.get_db() as db:
        acknowledged = await db.fetchone("SELECT acknowledged_at FROM external_deliveries WHERE job_id=? "
                                         "ORDER BY generation LIMIT 1", (nzo_id,))
    assert acknowledged["acknowledged_at"] is not None
