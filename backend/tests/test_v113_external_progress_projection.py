"""External download-client progress: the qBittorrent dialect carries the
progress DebridPulse itself shows (1.0.13, Sonarr BitTorrent qualification).

Sonarr and Radarr read a qBittorrent torrent as ``TotalSize = size`` and
``RemainingSize = size * (1 - progress)``; ``RemainingTime`` from ``eta``
(``8640000`` or out of range = unknown); ``amount_left``/``completed`` are never
read (Sonarr ``develop`` and Radarr ``develop``,
``src/NzbDrone.Core/Download/Clients/QBittorrent/QBittorrent.cs``
``GetItems`` / ``GetRemainingTime``). Their queue bar is
``size ? 100 - sizeleft / size * 100 : 0``. So a torrent reported with
``size: 0`` -- what the dialect sent for every unfinished job -- never shows a
bar, whatever its ``progress``; and ``progress`` came from verified material
(``progress.percent``), 0 while the only executor of a single-file torrent
was mid-download. DP's dashboard showed 41%, Sonarr showed nothing.

Corrected at the wire projection only: ``progress.effective`` (the canonical
``effective_progress``, the same figure the SABnzbd dialect already uses) is
the source; ``size``/``progress``/``amount_left``/``completed`` carry its exact
byte counters when -- and only when -- the canonical projection attaches them
(one exact byte scope); an estimate, a unit count, verified material or
nothing keeps ``size`` 0 (no total derived from a percentage) and ``progress``
the figure itself. ``eta`` stays unknown.

The endpoint cases run the real application, middleware, external service,
engine and executor over HTTP (``test_arr_qbittorrent_adapter``'s harness);
the knowledge-state matrix runs the canonical ``active_execution_projection``
/ ``active_execution_bytes`` / ``effective_progress`` into the dialect.
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_arr_qbittorrent_adapter import HASH, add_magnet, added, jobs, qb, torrents  # noqa: F401 - fixture
from test_external_client_foundation import complete, http, transfer_of, world  # noqa: F401 - fixtures

from api import arr_qbittorrent, arr_sabnzbd
from transfers._repository_base import active_execution_bytes, active_execution_projection, effective_progress
from transfers.models import TransferProgress

pytestmark = pytest.mark.asyncio

MIB = 1024 * 1024
UNKNOWN_ETA = 8640000


def arr_view(torrent: dict) -> dict:
    """What Sonarr/Radarr make of one torrent (QBittorrent.cs GetItems,
    QueueRow progress): total, remaining, the bar, and the time left."""
    size = int(torrent["size"])
    remaining = int(size * (1.0 - float(torrent["progress"])))
    eta = int(torrent["eta"])
    return {"total": size, "remaining": remaining,
            "bar": (100 - remaining / size * 100) if size else 0,
            "time_left": None if eta < 0 or eta > 365 * 24 * 3600 or eta == UNKNOWN_ETA else eta}


async def downloading(qb, world):  # noqa: F811
    """One magnet submitted through the qBittorrent API, its single member
    being written by the executor."""
    await added(await add_magnet(qb))
    transfer_id = await transfer_of((await jobs())[0]["id"])
    for _ in range(4):
        await world.engine.tick()
        running = [item for item in await world.repository.artifacts(transfer_id) if item.execution]
        if running:
            return transfer_id, running[0]
    raise AssertionError("no executor started")


async def report(world, artifact, progress: TransferProgress):  # noqa: F811
    """The executor's next observation carries ``progress``; the engine
    records it through its ordinary reconciliation."""
    attempt = artifact.execution.attempt_id
    world.executor.jobs[attempt] = replace(world.executor.jobs[attempt], progress=progress)
    await world.engine.reconcile_executions()


# ── the live defect: exact in-flight bytes ──────────────────────────────────────────────────────────────────────

async def test_an_exactly_measured_download_shows_sonarr_and_radarr_the_bar_dp_shows(qb, world):  # noqa: F811
    transfer_id, artifact = await downloading(qb, world)
    await report(world, artifact, TransferProgress(1000 * MIB, 410 * MIB, 0))
    [item] = await torrents(qb, hashes=HASH)
    assert item["state"] == "downloading"
    assert (item["size"], item["progress"], item["amount_left"], item["completed"]) == (
        1000 * MIB, 0.41, 590 * MIB, 410 * MIB)
    view = arr_view(item)
    assert view["total"] == 1000 * MIB and view["remaining"] == pytest.approx(590 * MIB, abs=MIB)
    assert view["bar"] == pytest.approx(41.0, abs=0.01)
    assert view["time_left"] is None and item["eta"] == UNKNOWN_ETA   # never invented
    # The same figure DP shows: the external projection's effective figure.
    [job] = await jobs()
    from external.service import ExternalClients
    effective = await ExternalClients(SimpleNamespace(repository=world.repository))._effective_progress(
        {"id": transfer_id, "status": "downloading", "progress": 0.0})
    assert effective["percent"] == pytest.approx(41.0) and effective["total_bytes"] == 1000 * MIB


async def test_successive_polls_stay_coherent_and_never_move_backwards(qb, world):  # noqa: F811
    _transfer_id, artifact = await downloading(qb, world)
    seen = []
    for done in (100, 410, 700, 999):
        await report(world, artifact, TransferProgress(1000 * MIB, done * MIB, 0))
        [item] = await torrents(qb, hashes=HASH)
        assert item["size"] == 1000 * MIB
        assert item["amount_left"] + item["completed"] == item["size"]          # one scope, one total
        assert item["progress"] == pytest.approx(item["completed"] / item["size"], abs=1e-4)
        seen.append(arr_view(item)["bar"])
    assert seen == sorted(seen) and seen[-1] < 100


async def test_completion_retires_every_in_flight_counter(qb, world):  # noqa: F811
    transfer_id, artifact = await downloading(qb, world)
    await report(world, artifact, TransferProgress(1000 * MIB, 410 * MIB, 0))
    await complete(world, transfer_id)
    [item] = await torrents(qb, hashes=HASH)
    assert (item["state"], item["progress"], item["eta"]) == ("stoppedUP", 1.0, 0)
    # The committed delivery's size, never the executor's last counters.
    assert item["size"] > 0 and item["size"] != 1000 * MIB
    assert (item["amount_left"], item["completed"]) == (0, item["size"])
    assert arr_view(item)["remaining"] == 0


async def test_unknown_size_is_reported_as_unknown_never_as_a_total(qb, world):  # noqa: F811
    _transfer_id, artifact = await downloading(qb, world)
    await report(world, artifact, TransferProgress(0, 0, 0))             # the executor knows no total
    [item] = await torrents(qb, hashes=HASH)
    assert (item["size"], item["progress"]) == (0, 0.0)
    assert "amount_left" not in item and "completed" not in item
    assert arr_view(item)["bar"] == 0


async def test_unit_progress_keeps_its_figure_without_an_invented_byte_total(qb, world):  # noqa: F811
    _transfer_id, artifact = await downloading(qb, world)
    await report(world, artifact, TransferProgress(0, 0, 0, completed_units=3, total_units=10))
    [item] = await torrents(qb, hashes=HASH)
    assert (item["size"], item["progress"]) == (0, 0.3)
    assert "amount_left" not in item


async def test_paused_and_resumed_execution(qb, world):  # noqa: F811
    transfer_id, artifact = await downloading(qb, world)
    await report(world, artifact, TransferProgress(1000 * MIB, 410 * MIB, 0))
    await world.engine.pause(transfer_id)
    [item] = await torrents(qb, hashes=HASH)
    # Paused, the canonical figure is verified material alone (the in-flight
    # figure is shown only while downloading): no byte total is reported.
    assert item["state"] == "stoppedDL" and item["size"] == 0 and "amount_left" not in item
    await world.engine.resume(transfer_id)
    running = []
    for _ in range(4):
        await world.engine.tick()
        running = [entry for entry in await world.repository.artifacts(transfer_id) if entry.execution]
        if running:
            break
    assert running, "resumed execution did not start"
    await report(world, running[0], TransferProgress(1000 * MIB, 500 * MIB, 0))
    [item] = await torrents(qb, hashes=HASH)
    assert item["state"] == "downloading"
    assert (item["size"], item["progress"], item["amount_left"]) == (1000 * MIB, 0.5, 500 * MIB)


async def test_two_clients_sharing_one_transfer_see_the_same_figure(qb, world):  # noqa: F811
    from test_arr_qbittorrent_adapter import add_magnet as add

    transfer_id, artifact = await downloading(qb, world)
    await added(await add(qb, headers=qb.other))                         # the same torrent, another client
    assert len({await transfer_of(job["id"]) for job in await jobs()}) == 1
    await report(world, artifact, TransferProgress(1000 * MIB, 410 * MIB, 0))
    [mine] = await torrents(qb, hashes=HASH)
    [theirs] = await torrents(qb, headers=qb.other, hashes=HASH)
    assert {key: mine[key] for key in ("size", "progress", "amount_left")} == {
        key: theirs[key] for key in ("size", "progress", "amount_left")}


# ── the knowledge-state matrix, canonical owner into both dialects ───────────────────────────────────────────

def row(writers=1, acquiring=1, bytes_known=0, completed=0, total=0, units_known=0, units_completed=0,
        units_total=0, estimates_known=0, estimated=None):
    return {"execution_writers": writers, "execution_acquiring": acquiring, "execution_bytes_known": bytes_known,
            "execution_completed": completed, "execution_total": total, "execution_units_known": units_known,
            "execution_units_completed": units_completed, "execution_units_total": units_total,
            "execution_estimates_known": estimates_known, "execution_estimated": estimated}


CASES = {
    "one-exact": (row(bytes_known=1, completed=410 * MIB, total=1000 * MIB), "downloading", 0.0),
    "two-exact-compatible": (row(writers=2, bytes_known=2, completed=600 * MIB, total=2000 * MIB),
                             "downloading", 0.0),
    "two-writers-incomplete-scope": (row(writers=2, bytes_known=1, completed=600 * MIB, total=1000 * MIB),
                                     "downloading", 0.0),
    "mixed-bytes-and-units": (row(writers=2, bytes_known=1, completed=1, total=4, units_known=1,
                                  units_completed=1, units_total=2), "downloading", 0.0),
    "estimated": (row(estimates_known=1, estimated=65.2), "downloading", 0.0),
    "units": (row(units_known=1, units_completed=3, units_total=10), "downloading", 0.0),
    "processing": (row(acquiring=0), "downloading", 0.0),
    "verified-material": (row(bytes_known=1, completed=410 * MIB, total=1000 * MIB), "downloading", 25.0),
    "nothing": (None, "downloading", 0.0),
}


def wire(case):
    projection, status, verified = CASES[case]
    effective = effective_progress(status, verified, *active_execution_projection(projection),
                                   active_execution_bytes(projection))
    job = {"id": "job", "display_name": "Show", "category": "tv", "phase": "processing", "reason": "downloading",
           "profile_id": "tv", "source": {"info_hash": HASH, "name": "Show"}, "delivery": {},
           "progress": {"basis": "members", "percent": verified, "effective": effective}}
    return effective, job


@pytest.mark.parametrize("case", sorted(CASES))
async def test_every_knowledge_state_is_carried_without_invention(world, case):  # noqa: F811
    effective, job = wire(case)
    torrent = arr_qbittorrent._torrent(job)
    exact = effective["total_bytes"] is not None
    if exact:
        assert torrent["size"] == effective["total_bytes"]
        assert torrent["amount_left"] == effective["total_bytes"] - effective["completed_bytes"]
        assert arr_view(torrent)["bar"] == pytest.approx(effective["percent"], abs=0.1)
    else:
        assert torrent["size"] == 0 and "amount_left" not in torrent         # no total from a percentage
        expected = 0.0 if effective["percent"] is None else effective["percent"] / 100
        assert torrent["progress"] == pytest.approx(expected, abs=1e-4)
    assert torrent["eta"] == UNKNOWN_ETA
    # The SABnzbd dialect reports the same knowledge in its own fields.
    async def visible(*_args, **_kwargs):
        return {"jobs": [job], "total": 1, "globally_paused": False}

    [slot] = (await arr_sabnzbd._queue(SimpleNamespace(visible=visible), None, {}))["queue"]["slots"]
    if exact:
        assert float(slot["mb"]) * MIB == pytest.approx(torrent["size"], abs=0.01 * MIB)
        assert float(slot["mbleft"]) * MIB == pytest.approx(torrent["amount_left"], abs=0.01 * MIB)
    else:
        assert (slot["mb"], slot["mbleft"]) == ("0.00", "0.00")
    assert slot["timeleft"] == "0:00:00"                                 # SABnzbd's only unknown form


async def test_the_matrix_exercises_each_basis():
    bases = {case: wire(case)[0] for case in CASES}
    assert bases["one-exact"]["total_bytes"] == 1000 * MIB
    assert bases["two-exact-compatible"]["total_bytes"] == 2000 * MIB
    assert bases["two-writers-incomplete-scope"]["percent"] in {None, 0.0}
    assert bases["mixed-bytes-and-units"]["total_bytes"] is None
    assert bases["estimated"]["estimated"] is True and bases["estimated"]["total_bytes"] is None
    assert bases["units"]["percent"] == 30.0 and bases["units"]["total_bytes"] is None
    assert bases["processing"]["source"] == "verified"
    assert bases["verified-material"]["source"] == "verified" and bases["verified-material"]["percent"] == 25.0
