"""External download-client progress once material is verified (1.0.13,
transfers 666 and 670).

Sonarr (666, Premiumize) and Radarr (670, Real-Debrid) showed no BitTorrent
progress bar. Their writer was aria2, which declares
``EXPORT_MATERIAL_RANGES``: its completely written ranges are checkpointed
into DP-valid material while it downloads, so it never has an in-flight lane
(``active_execution_progress_sql``) and DebridPulse's primary figure is
verified material -- 42.50% and 31.59%. ``effective_progress`` attached byte
counters only to an in-flight figure, so the qBittorrent dialect received a
bare percentage and reported ``size: 0``; *arr reads ``TotalSize = size``,
``RemainingSize = size * (1 - progress)`` and draws nothing without a size.

That percentage has an exact byte basis: ``aggregate_lifecycle`` writes
``torrents.progress = completed / total`` and ``torrents.size_bytes = total``
from the canonical artifacts' sizes and their DP-valid material
(``verified_material_measure``, now its one definition). The canonical
projection carries that measurement with the verified figure it measures,
and only when something is verified: nothing verified is no measured
figure, so a writer whose work is not DP material keeps its existing
representation. It is never combined with an executor's own counters.

The endpoint cases run the real HTTP endpoint, middleware, external service,
engine, executor checkpoint and material owner with a material-exporting
writer (as aria2 declares) at the two live transfers' sizes. Provider names
are scenario labels only; nothing here is provider- or client-specific.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import db.database as database
import fake_integrations
from api import arr_qbittorrent, arr_sabnzbd
from test_arr_qbittorrent_adapter import HASH, add_magnet, added, jobs, qb, torrents  # noqa: F401 - fixture
from test_external_client_foundation import http, transfer_of, world  # noqa: F401 - fixtures
from test_v113_external_progress_projection import UNKNOWN_ETA, arr_view
from test_v113_radarr_bittorrent_qualification import M, offer_fan_out
from transfers._repository_base import effective_progress
from transfers.models import (
    ContinuationCapability, ExecutionActivity, ExecutionState, SizeKnowledge, TransferProgress, TransferState)

pytestmark = pytest.mark.asyncio

MIB = 1024 * 1024
# Transfer 666 (Sonarr, Premiumize): 1,682,509,189 bytes, 42.50% verified.
SONARR_SIZE, SONARR_VERIFIED = 1_682_509_189, 682 * MIB
# Transfer 670 (Radarr, Real-Debrid): 531,023,364 bytes, 31.59% verified.
RADARR_SIZE, RADARR_VERIFIED = 531_023_364, 160 * MIB


@pytest.fixture(autouse=True)
def material_writer(monkeypatch):
    """The executor declares what aria2 declares: its written ranges are
    exported and checkpointed as DP material (no in-flight lane)."""
    monkeypatch.setattr(fake_integrations.MemoryExecutor, "capabilities", replace(
        fake_integrations.MemoryExecutor.capabilities, continuation=frozenset({
            ContinuationCapability.FULL_RESTART, ContinuationCapability.EXPORT_MATERIAL_RANGES})))


async def submitted(qb, world, files):  # noqa: F811
    """One magnet through the qBittorrent API, fanned out into ``files``
    (``(name, size)``; size 0 = undeclared) and every member being written."""
    offer_fan_out(world, files=tuple((name, f"Release/{name}", size) for name, size in files))
    sizes = dict(files)
    candidate = world.provider.candidate
    world.provider.candidate = lambda name="payload.bin", **kwargs: replace(
        candidate(name, **kwargs), expected_bytes=sizes.get(name, 4))
    await added(await add_magnet(qb, magnet=M))
    transfer_id = await transfer_of((await jobs())[0]["id"])
    for _ in range(6):
        await world.engine.tick()
        running = [item for item in await world.repository.artifacts(transfer_id) if item.execution]
        if len(running) == len(files):
            return transfer_id, {Path(item.target).name: item for item in running}
    raise AssertionError("not every member started")


async def write(world, artifact, verified, *, downloaded=None, size=None):  # noqa: F811
    """The writer's next observation: ``downloaded`` bytes on disk (its own
    counter), ``verified`` of them reported completely written; the engine's
    ordinary reconcile checkpoints them and the next pass aggregates."""
    size = artifact.expected_bytes if size is None else size
    downloaded = verified if downloaded is None else downloaded
    path = Path(artifact.target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as handle:
        handle.truncate(max(downloaded, path.stat().st_size))
    attempt = artifact.execution.attempt_id
    world.executor.jobs[attempt] = replace(world.executor.jobs[attempt], progress=TransferProgress(
        size, downloaded, 0), material=((0, verified),) if verified else None)
    world.now[0] += 10
    await world.engine.reconcile_executions()
    await world.engine.tick()


async def stored(transfer_id):
    async with database.get_db() as db:
        return await db.fetchone("SELECT status, progress, size_bytes FROM torrents WHERE id=?", (transfer_id,))


def coherent(item):
    """One accounting scope: what the dialect sends and what *arr makes of it."""
    assert 0 <= item["completed"] <= item["size"]
    assert item["amount_left"] == item["size"] - item["completed"]
    assert item["progress"] == round(item["completed"] / item["size"], 4)
    view = arr_view(item)
    assert view["total"] == item["size"]
    assert view["remaining"] == pytest.approx(item["amount_left"], abs=item["size"] * 1e-4)
    assert view["time_left"] is None and item["eta"] == UNKNOWN_ETA      # never invented
    return view


# ── the live cases ──────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("size,verified,downloaded,percent", [
    (SONARR_SIZE, SONARR_VERIFIED, 740_982_784, 42.5),                   # transfer 666
    (RADARR_SIZE, RADARR_VERIFIED, 190_644_224, 31.6),                   # transfer 670
], ids=["666-sonarr", "670-radarr"])
async def test_verified_material_reaches_sonarr_and_radarr_as_one_byte_scope(
        qb, world, size, verified, downloaded, percent):  # noqa: F811
    transfer_id, members = await submitted(qb, world, [("Release.mkv", size)])
    await write(world, members["Release.mkv"], verified, downloaded=downloaded)
    row = await stored(transfer_id)
    # The canonical state the live traces record: verified material, nonzero.
    assert row["status"] == "downloading" and row["size_bytes"] == size
    assert row["progress"] == pytest.approx(verified / size * 100)
    [item] = await torrents(qb, hashes=HASH)
    assert item["state"] == "downloading"
    assert (item["size"], item["completed"]) == (size, verified)        # verified, never the executor's counter
    view = coherent(item)
    assert view["bar"] == pytest.approx(percent, abs=0.05) == pytest.approx(row["progress"], abs=0.05)
    [job] = await jobs()                                                 # the projection the dialect served
    effective = json.loads(job["projection"])["progress"]["effective"]
    assert effective == {"source": "verified", "estimated": False, "percent": round(row["progress"], 1),
                         "completed_bytes": verified, "total_bytes": size}


async def test_successive_polls_advance_monotonically_in_one_scope(qb, world):  # noqa: F811
    _transfer_id, members = await submitted(qb, world, [("Release.mkv", SONARR_SIZE)])
    bars = []
    for verified in (64 * MIB, 300 * MIB, SONARR_VERIFIED, 1500 * MIB):
        await write(world, members["Release.mkv"], verified)
        [item] = await torrents(qb, hashes=HASH)
        assert item["size"] == SONARR_SIZE and item["completed"] == verified
        bars.append(coherent(item)["bar"])
    assert bars == sorted(bars) and bars[-1] < 100


async def test_nothing_verified_yet_is_unknown_never_zero_of_a_total(qb, world):  # noqa: F811
    """Before the first checkpoint nothing is verified: the canonical policy
    treats exactly 0 as unverified, so no total is attached."""
    transfer_id, members = await submitted(qb, world, [("Release.mkv", SONARR_SIZE)])
    await write(world, members["Release.mkv"], 0, downloaded=5 * MIB)
    assert (await stored(transfer_id))["progress"] == 0.0
    [item] = await torrents(qb, hashes=HASH)
    assert (item["size"], item["progress"]) == (0, 0.0) and "amount_left" not in item


async def test_pause_keeps_the_verified_scope_and_resume_continues_it(qb, world):  # noqa: F811
    transfer_id, members = await submitted(qb, world, [("Release.mkv", RADARR_SIZE)])
    await write(world, members["Release.mkv"], RADARR_VERIFIED)
    [before] = await torrents(qb, hashes=HASH)
    await world.application.pause(transfer_id)
    [paused] = await torrents(qb, hashes=HASH)
    # No active executor: the verified measurement stands alone.
    assert paused["state"] == "stoppedDL"
    assert (paused["size"], paused["completed"]) == (RADARR_SIZE, RADARR_VERIFIED)
    assert coherent(paused)["bar"] == pytest.approx(arr_view(before)["bar"])
    await world.application.resume(transfer_id)
    running = []
    for _ in range(4):
        await world.engine.tick()
        running = [item for item in await world.repository.artifacts(transfer_id) if item.execution]
        if running:
            break
    assert running, "resumed execution did not start"
    await write(world, running[0], 320 * MIB)
    [resumed] = await torrents(qb, hashes=HASH)
    assert resumed["state"] == "downloading" and resumed["completed"] == 320 * MIB
    assert coherent(resumed)["bar"] > arr_view(paused)["bar"]


async def test_all_material_verified_before_finalization_is_complete_but_not_importable(qb, world):  # noqa: F811
    transfer_id, members = await submitted(qb, world, [("Release.mkv", RADARR_SIZE)])
    await write(world, members["Release.mkv"], RADARR_SIZE)
    assert (await stored(transfer_id))["status"] not in {"completed", "consolidated"}
    [item] = await torrents(qb, hashes=HASH)
    assert (item["size"], item["completed"], item["amount_left"], item["progress"]) == (
        RADARR_SIZE, RADARR_SIZE, 0, 1.0)
    assert item["state"] not in {"stoppedUP", "pausedUP"} and item["content_path"] == ""


async def test_a_completed_member_and_an_active_member_are_one_scope(qb, world):  # noqa: F811
    first, second = 400 * MIB, 600 * MIB
    transfer_id, members = await submitted(qb, world, [("E01.mkv", first), ("E02.mkv", second)])
    await write(world, members["E01.mkv"], first)
    await write(world, members["E02.mkv"], 150 * MIB, downloaded=170 * MIB)
    [item] = await torrents(qb, hashes=HASH)
    assert (item["size"], item["completed"]) == (first + second, first + 150 * MIB)
    assert coherent(item)["bar"] == pytest.approx((await stored(transfer_id))["progress"], abs=0.05)


async def test_two_active_writers_sum_their_verified_material(qb, world):  # noqa: F811
    transfer_id, members = await submitted(qb, world, [("E01.mkv", 400 * MIB), ("E02.mkv", 600 * MIB)])
    await write(world, members["E01.mkv"], 100 * MIB)
    await write(world, members["E02.mkv"], 300 * MIB)
    [item] = await torrents(qb, hashes=HASH)
    assert (item["size"], item["completed"]) == (1000 * MIB, 400 * MIB)
    assert coherent(item)["bar"] == pytest.approx(40.0, abs=0.01)
    assert (await stored(transfer_id))["progress"] == pytest.approx(40.0)


async def test_a_member_of_unknown_size_leaves_the_total_unknown(qb, world):  # noqa: F811
    transfer_id, members = await submitted(qb, world, [("E01.mkv", 400 * MIB), ("E02.mkv", 600 * MIB)])
    attempt = members["E02.mkv"].execution.attempt_id                    # nor does its writer know one
    world.executor.jobs[attempt] = replace(world.executor.jobs[attempt], progress=TransferProgress(0, 0, 0))
    async with database.get_db() as db:                                  # the provider declared no size for it
        await db.execute("UPDATE download_files SET size_bytes=0, size_knowledge=? WHERE id=?",
                         (SizeKnowledge.UNKNOWN.value, members["E02.mkv"].id))
        await db.commit()
    await write(world, members["E01.mkv"], 100 * MIB)
    assert (await stored(transfer_id))["progress"] is None              # no canonical percentage either
    assert await world.repository.verified_material(transfer_id) is None
    [item] = await torrents(qb, hashes=HASH)
    assert (item["size"], item["progress"]) == (0, 0.0) and "amount_left" not in item


async def test_a_member_outside_canonical_membership_is_outside_the_scope(qb, world):  # noqa: F811
    """Only canonical artifacts measure the job's work: a standby (or
    blocked, or unselected) row is never counted, exactly as the canonical
    percentage never counts it."""
    transfer_id, members = await submitted(qb, world, [("E01.mkv", 400 * MIB), ("E02.mkv", 600 * MIB)])
    await write(world, members["E01.mkv"], 100 * MIB)
    async with database.get_db() as db:
        await db.execute("UPDATE download_files SET mirror_state='standby' WHERE id=?", (members["E02.mkv"].id,))
        await db.commit()
    assert await world.repository.verified_material(transfer_id) == (100 * MIB, 400 * MIB)
    [item] = await torrents(qb, hashes=HASH)
    assert (item["size"], item["completed"]) == (400 * MIB, 100 * MIB)


async def test_two_clients_sharing_one_transfer_see_the_same_scope(qb, world):  # noqa: F811
    transfer_id, members = await submitted(qb, world, [("Release.mkv", SONARR_SIZE)])
    await added(await add_magnet(qb, magnet=M, headers=qb.other))
    assert {await transfer_of(job["id"]) for job in await jobs()} == {transfer_id}
    await write(world, members["Release.mkv"], SONARR_VERIFIED)
    [mine] = await torrents(qb, hashes=HASH)
    [theirs] = await torrents(qb, headers=qb.other, hashes=HASH)
    fields = ("size", "progress", "amount_left", "completed")
    assert {key: mine[key] for key in fields} == {key: theirs[key] for key in fields}
    coherent(mine)


async def test_completion_reports_the_committed_delivery_never_the_last_measurement(qb, world):  # noqa: F811
    transfer_id, members = await submitted(qb, world, [("Release.mkv", RADARR_SIZE)])
    artifact = members["Release.mkv"]
    await write(world, artifact, RADARR_VERIFIED)
    await write(world, artifact, RADARR_SIZE)
    world.executor.jobs[artifact.execution.attempt_id] = replace(
        world.executor.jobs[artifact.execution.attempt_id], state=ExecutionState.SUCCEEDED,
        progress=TransferProgress(RADARR_SIZE, RADARR_SIZE), activity=ExecutionActivity(), controls=frozenset(),
        materialization=world.executor.file_result(artifact.execution, RADARR_SIZE))
    for _ in range(6):
        await world.engine.tick()
        if (await world.repository.get(transfer_id)).state == TransferState.COMPLETED:
            break
    assert (await world.repository.get(transfer_id)).state == TransferState.COMPLETED
    [item] = await torrents(qb, hashes=HASH)
    assert (item["state"], item["progress"], item["size"]) == ("stoppedUP", 1.0, RADARR_SIZE)
    assert (item["amount_left"], item["completed"]) == (0, RADARR_SIZE)
    # The delivery gone: downloaded, but never falsely importable.
    Path(item["content_path"].replace("/imports/tv", str(world.payloads), 1)).unlink()
    [item] = await torrents(qb, hashes=HASH)
    assert (item["state"], item["size"], item["content_path"]) == ("checkingUP", 0, "")
    assert "amount_left" not in item and "completed" not in item


async def test_a_cancelled_transfer_reads_error_and_never_completed(qb, world):  # noqa: F811
    transfer_id, members = await submitted(qb, world, [("Release.mkv", RADARR_SIZE)])
    await write(world, members["Release.mkv"], RADARR_VERIFIED)
    await world.application.cancel(transfer_id)
    [item] = await torrents(qb, hashes=HASH)
    # Failed, never importable; the material it verified is still what it shows.
    assert item["state"] == "error" and item["content_path"] == ""
    assert (item["size"], item["completed"]) == (RADARR_SIZE, RADARR_VERIFIED)
    coherent(item)


# ── the canonical decision into both dialects ───────────────────────────────────────────────────────────────────

class _Member(SimpleNamespace):
    pass


def member(id, state, size, knowledge=SizeKnowledge.KNOWN_POSITIVE):
    return _Member(id=id, state=state, expected_bytes=size, size_knowledge=knowledge)


async def test_the_measurement_is_the_one_aggregate_lifecycle_writes():
    from transfers._repository_base import verified_material_measure

    artifacts = (member(1, "completed", 400), member(2, "downloading", 600), member(3, "queued", 1000))
    # A completed artifact counts its whole size, any other its valid
    # material up to its size.
    assert verified_material_measure(artifacts, {2: 150, 3: 5000}) == (400 + 150 + 1000, 2000)
    assert verified_material_measure((member(1, "downloading", 0, SizeKnowledge.UNKNOWN),), {1: 10}) is None
    assert verified_material_measure((member(1, "completed", 0, SizeKnowledge.UNKNOWN),), {}) == (0, 0)


@pytest.mark.parametrize("status,verified,active,basis,active_bytes,measure,expected", [
    # nonzero verified carries its own measurement and its percentage
    ("downloading", 42.5, None, None, None, (715_128_832, SONARR_SIZE),
     (round(715_128_832 / SONARR_SIZE * 100, 1), 715_128_832, SONARR_SIZE, "verified")),
    ("paused", 31.6, None, None, None, (RADARR_VERIFIED, RADARR_SIZE),
     (round(RADARR_VERIFIED / RADARR_SIZE * 100, 1), RADARR_VERIFIED, RADARR_SIZE, "verified")),
    # an in-flight lane beside nonzero verified material: verified wins, the
    # executor's counters never mix in
    ("downloading", 25.0, 80.0, "bytes", (800, 1000), (250, 1000), (25.0, 250, 1000, "verified")),
    # nothing verified: the in-flight figure keeps its own counters ...
    ("downloading", 0.0, 41.0, "bytes", (410, 1000), (0, 1000), (41.0, 410, 1000, "execution")),
    # ... or, without one, no measured figure at all
    ("downloading", 0.0, None, None, None, (0, 1000), (0.0, None, None, "verified")),
    ("paused", 0.0, None, None, None, (0, 1000), (0.0, None, None, "verified")),
    ("extracting", 0.0, None, "processing", None, (0, 1000), (0.0, None, None, "verified")),
    # an estimate or a unit count is no byte scope
    ("downloading", 0.0, 65.2, "estimated", None, (0, 1000), (65.2, None, None, "execution")),
    ("downloading", 0.0, 30.0, "units", None, (0, 1000), (30.0, None, None, "execution")),
    # unknown: no measurement, the stored figure (or none)
    ("downloading", None, None, None, None, None, (None, None, None, "verified")),
    ("downloading", 12.5, None, None, None, None, (12.5, None, None, "verified")),
    # completion is the committed delivery's, never a measurement
    ("completed", 100.0, None, None, None, (1000, 1000), (100.0, None, None, "verified")),
])
async def test_each_knowledge_state_reaches_both_dialects_without_invention(
        status, verified, active, basis, active_bytes, measure, expected):
    effective = effective_progress(status, verified, active, basis, active_bytes, measure)
    assert (effective["percent"], effective["completed_bytes"], effective["total_bytes"], effective["source"]) == expected
    job = {"id": "job", "display_name": "Show", "category": "tv", "phase": "processing", "reason": "downloading",
           "profile_id": "tv", "source": {"info_hash": HASH, "name": "Show"}, "delivery": {},
           "progress": {"basis": "members", "percent": verified, "effective": effective}}
    torrent = arr_qbittorrent._torrent(job)

    async def visible(*_args, **_kwargs):
        return {"jobs": [job], "total": 1, "globally_paused": False}

    [slot] = (await arr_sabnzbd._queue(SimpleNamespace(visible=visible), None, {}))["queue"]["slots"]
    if effective["total_bytes"] is not None:
        coherent(torrent)
    else:
        assert torrent["size"] == 0 and "amount_left" not in torrent     # no total from a percentage
    # SABnzbd carries the executor's own counters only, exactly as before: a
    # NZB's declared size is never its total.
    if effective["source"] == "execution" and effective["total_bytes"] is not None:
        assert float(slot["mb"]) * MIB == pytest.approx(torrent["size"], abs=0.01 * MIB)
        assert float(slot["mbleft"]) * MIB == pytest.approx(torrent["amount_left"], abs=0.01 * MIB)
    else:
        assert (slot["mb"], slot["mbleft"]) == ("0.00", "0.00")
    assert slot["percentage"] == str(int(effective["percent"] or 0))
    assert torrent["eta"] == UNKNOWN_ETA and slot["timeleft"] == "0:00:00"
