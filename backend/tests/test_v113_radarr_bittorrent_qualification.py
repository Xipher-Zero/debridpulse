"""Radarr BitTorrent qualification: the three remaining lifecycle issues
(transfers 660/661, 1.0.13).

A -- Real-Debrid readiness latency (661). The backup was bound at +4.9 s,
     but its first observation -- the only place its selection was handed
     over -- was gated one backup poll later (+38.2 s); the immediate
     read-back then found it AVAILABLE and the yield followed in 1.1 s.
     Corrected: a backup whose CREATION answer already lists its files and
     waits for its selection is handed the selection at bind time by the one
     selection owner (``_select_on_standby``), read back at once, and a
     read-back that finds it AVAILABLE brings the next pass now through the
     persisted readiness deadline.
B -- Debrid-Link ``notAddTorrent``. A recoverable standby refusal is deferred
     with the existing backoff, retried by the production preparation pass
     when -- and only when -- its deadline has passed and the transfer is
     still live; worded "Retrying…". Nothing here changes Debrid-Link.
C -- Empty download-folder retirement after a Radarr import. Transfer 661's
     torrent fanned out into its collection folder as FILE material, and the
     acknowledged-delivery retirement only ever considered COLLECTION
     artifacts: the folder was never even attempted. Corrected through the
     same owner (``retire_vacated_collection``); and an acknowledgement that
     arrived before the folder emptied is asked again by the existing
     external maintenance pass, bounded by the delivery's own retention.

Providers are local fakes; Real-Debrid and Debrid-Link run their real adapters
over scripted clients; the timing cases run the production
``core.scheduler.sync_status_loop`` over a virtual clock.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from test_external_client_foundation import body, complete, transfer_of, world  # noqa: F401 - fixture
from test_v113_debridlink_provider import ok, refused, torrent
from test_v113_debridlink_provider import service as dl_service
from test_v113_readiness_handoff_timeline import DEADLINE_WAKE, POLL, T0, operations, timeline
from test_v113_readiness_promotion import rows
from test_v113_realdebrid_upstream_selection import MOVIE, MOVIE_BYTES, preparing_parcel

import external.service as external_service
from db import database
from fake_integrations import MemoryExecutor
from providers.debridlink.provider import DebridLinkProvider
from test_v113_collection_route_generic_closure import Clock
from transfers.convergence_engine import TransferEngine
from transfers.manual_route_switch import route_providers, switch_root_provider
from transfers.models import Capability, ResourceState, TransferRequest
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio


# ═══ A — Real-Debrid readiness latency ═════════════════════════════════════════════════════════════════════════

def rd_calls(w, name):
    return [call for call in w.fake.calls if call[0] == name]


async def test_a_backup_ready_at_once_is_selected_at_bind_and_yielded_within_a_deadline_wake(tmp_path, monkeypatch):
    """661's shape, corrected: bound and ready in the same pass; the yield is
    one deadline wake later -- not one backup poll (33 s) later."""
    w, vs, marks = await timeline(tmp_path, monkeypatch)
    assert marks["selected"] == marks["available"] == T0                 # the pass that bound it
    assert marks["promoted"] - T0 == pytest.approx(DEADLINE_WAKE)
    assert w.fake.selects() == ["1"] and w.fake.count("add_magnet") == 1
    assert (await operations(w)).count("readiness_promotion") == 1

async def test_provider_io_from_creation_to_the_yield_is_bounded(tmp_path, monkeypatch):
    """Exactly two passes (bind, deadline wake): the creation and its read,
    the selection owner's read-first and one selection, the read-back, and
    the yield's own re-read and takeover -- nothing repeated."""
    w, vs, marks = await timeline(tmp_path, monkeypatch, passes=2)
    assert "promoted" in marks
    names = [call[0] for call in w.fake.calls]
    assert names.count("add_magnet") == 1 and names.count("select_files") == 1
    assert names.count("torrent_info") <= 6


async def test_ready_shortly_after_an_initial_preparing_read_back_is_recognized_at_the_ordinary_poll(
        tmp_path, monkeypatch):
    """Selected at bind, still acquiring at the read-back, ready 10 s later:
    recognized by the next ordinary backup observation, one poll after the
    bind, and yielded in that same pass. No early follow-up is invented (it
    would need a new per-backup schedule; see the Gate 9 owner decision)."""
    w, vs, marks = await timeline(tmp_path, monkeypatch, ready=10.0)
    assert marks["selected"] == T0 and marks["available"] == T0 + POLL
    assert marks["promoted"] == marks["available"]
    assert w.fake.selects() == ["1"]


@pytest.mark.parametrize("ready", [10_000.0], ids=["permanently-unready"])
async def test_a_backup_that_stays_preparing_is_polled_at_the_ordinary_cadence_and_never_reselected(
        tmp_path, monkeypatch, ready):
    w, vs, marks = await timeline(tmp_path, monkeypatch, ready=ready, passes=7)
    assert "promoted" not in marks and w.fake.selects() == ["1"]
    # One observation per backup poll, plus the selection owner's read-first
    # (which sends nothing once the selection stands): bounded, never a loop.
    reads = len(rd_calls(w, "torrent_info"))
    passes = len(vs.passes_at())
    assert reads <= 2 + 2 * passes


async def test_an_operator_switch_before_readiness_leaves_one_route_and_one_selection(tmp_path, monkeypatch):
    import test_v113_readiness_handoff_timeline as harness

    original = harness.world

    async def world_(*args, **kwargs):
        w = await original(*args, **kwargs)
        return w

    monkeypatch.setattr(harness, "world", world_)
    w, vs, marks = await timeline(tmp_path, monkeypatch, ready=10_000.0, passes=2)
    await switch_root_provider(w.engine, w.transfer.id, "realdebrid", expected_provider_id="parcel-a")
    for _ in range(3):
        w.engine.clock.now += POLL
        await w.engine.resolve_pending()
    operations_ = await operations(w)
    assert operations_.count("operator_switch") == 1 and "readiness_promotion" not in operations_
    assert w.fake.selects() == ["1"] and w.fake.count("add_magnet") == 1


# ═══ B — Debrid-Link recoverable refusal and its retry ════════════════════════════════════════════════════════

MAGNET = "magnet:?xt=urn:btih:" + "d" * 40 + "&dn=Show"


async def dl_lab(tmp_path, monkeypatch, script):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "dl.sqlite3")
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    # The primary sorts first in the neutral order, as AllDebrid does live.
    first = preparing_parcel("aa-primary", [(MOVIE, f"Show/{MOVIE}", MOVIE_BYTES)])
    client, transport = dl_service(script)
    debridlink = DebridLinkProvider(client, prepare_backup_torrents=True)
    registry.register_provider(first)
    registry.register_provider(debridlink)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    # The live policy's backoff: 60 s, doubling, capped at 300 s.
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "dl"),
                            policy=TransferPolicy(retry_delay=60.0, max_retry_delay=300.0, max_attempts=4),
                            clock=Clock())
    await engine.initialize()
    transfer = await engine.submit((TransferRequest("magnet", MAGNET, name="Show", fingerprint="d" * 40),),
                                   name="Show", deduplicate=False)
    return repository, engine, transfer, transport


def adds(transport):
    return [call for call in transport.calls if call["url"].endswith("seedbox/add")]


async def dl_standby(repository, transfer):
    return next(item for item in await repository.standbys(transfer.id) if item["provider_id"] == "debridlink")


async def advance(engine, seconds):
    engine.clock.now += seconds
    await engine.resolve_pending()


async def test_a_due_retry_runs_while_the_primary_still_prepares_and_binds_one_backup(tmp_path, monkeypatch):
    stored = torrent(status=4, percent=40)
    repository, engine, transfer, transport = await dl_lab(tmp_path, monkeypatch, {
        ("POST", "seedbox/add"): [refused("notAddTorrent"), ok(torrent(files=[]))],
        ("GET", "seedbox/list"): [ok([stored])] * 20})
    await engine.resolve_pending()
    item = await dl_standby(repository, transfer)
    assert item["state"] == "deferred" and len(adds(transport)) == 1
    assert item["retry_at"] - engine.clock() == 60.0                     # the existing backoff, from attempt 0
    view = {entry["provider_id"]: entry for entry in (await route_providers(engine, transfer.id))["providers"]}
    assert view["debridlink"]["status"] == "retrying" and view["debridlink"]["retry_in_seconds"] == 60
    await advance(engine, 30.0)                                          # not yet due: nothing is sent
    assert len(adds(transport)) == 1
    await advance(engine, 30.0)                                          # due: the production pass retries
    assert len(adds(transport)) == 2
    item = await dl_standby(repository, transfer)
    assert item["state"] == "bound" and item["binding_id"]
    assert len(await rows("SELECT id FROM standby_resources WHERE transfer_id=? AND provider_id='debridlink'",
                          (transfer.id,))) == 1                          # one claim, one remote torrent
    for _ in range(3):
        await advance(engine, POLL)
    assert len(adds(transport)) == 2                                     # never created again


async def test_a_repeated_refusal_follows_the_backoff_ladder_never_a_tight_loop(tmp_path, monkeypatch):
    repository, engine, transfer, transport = await dl_lab(tmp_path, monkeypatch, {
        ("POST", "seedbox/add"): [refused("notAddTorrent")] * 10, ("GET", "seedbox/list"): []})
    await engine.resolve_pending()
    waits = []
    for _ in range(4):
        item = await dl_standby(repository, transfer)
        waits.append(item["retry_at"] - engine.clock())
        engine.clock.now = item["retry_at"] - 1
        await engine.resolve_pending()                                   # one second early: nothing
        sent = len(adds(transport))
        engine.clock.now = item["retry_at"]
        await engine.resolve_pending()
        assert len(adds(transport)) == sent + 1                          # exactly one attempt when due
    assert waits == [60.0, 120.0, 240.0, 300.0]
    view = {entry["provider_id"]: entry for entry in (await route_providers(engine, transfer.id))["providers"]}
    assert view["debridlink"]["status"] == "retrying"
    assert view["debridlink"]["detail"] == "Resolution temporarily failed"


@pytest.mark.parametrize("end", ["delete", "withdraw", "cancel"])
async def test_a_retry_never_resurrects_a_transfer_that_ended(tmp_path, monkeypatch, end):
    repository, engine, transfer, transport = await dl_lab(tmp_path, monkeypatch, {
        ("POST", "seedbox/add"): [refused("notAddTorrent"), ok(torrent(files=[]))], ("GET", "seedbox/list"): []})
    await engine.resolve_pending()
    if end == "delete":
        await engine.delete(transfer.id, remote=False)
    elif end == "cancel":
        await engine.cancel(transfer.id)
    else:
        demands = tuple(row["id"] for row in await rows("SELECT id FROM transfer_demands WHERE transfer_id=?",
                                                         (transfer.id,)))
        await engine.withdraw_demand(demands, by="external-client")
    for _ in range(4):
        await advance(engine, 120.0)
    assert len(adds(transport)) == 1                                     # nothing created after the end


# ═══ C — empty download-folder retirement after a Radarr import ═════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _fresh_retirement_walk(monkeypatch):
    """Each test starts with no throttle and no walk position, as a new
    process does (``raising=False``: the walk position may not exist)."""
    monkeypatch.setattr(external_service, "_FOLDER_RETIREMENT_ASKED_AT", 0.0)
    monkeypatch.setattr(external_service, "_FOLDER_RETIREMENT_CURSOR", None, raising=False)


M = "magnet:?xt=urn:btih:" + "e" * 40 + "&dn=Akira"


def offer_fan_out(world, *, files=(("movie.mkv", "Akira/movie.mkv", 4096),), payload="box",  # noqa: F811
                  name="Parcel"):
    """The parcel provider fans the magnet out into members placed beneath the
    transfer's collection folder (FILE material), as Real-Debrid's did for 661."""
    world.provider.declares_file_manifest = True
    world.provider.descriptor = replace(world.provider.descriptor,
                                        capabilities=world.provider.descriptor.capabilities | {Capability.FILE_MANIFEST})
    result = world.provider.parcel(payload, state=ResourceState.AVAILABLE, files=list(files))
    observed = replace(result.observation, request=TransferRequest("magnet", M), name=name)
    world.provider.resources[observed.resource.id] = observed
    world.provider.responses.append(replace(result, observation=observed))


async def delivered(world, key="k-1", client=None, **kwargs):  # noqa: F811
    offer_fan_out(world, **kwargs)
    job, _ = await world.external.submit(client or world.clients.r, body(key, category="movies",
                                                                         source={"kind": "magnet", "magnet": M}))
    transfer_id = await transfer_of(job["id"])
    await complete(world, transfer_id)
    # The client polls, as Radarr does before it imports: the completed
    # delivery's generation is committed.
    view = await world.external.job(client or world.clients.r, job["id"])
    assert view["phase"] == "completed" and view["delivery"]["state"] == "available"
    transfer = await world.repository.get(transfer_id)
    collection = Path(world.engine.root) / transfer.collection_root
    return job, transfer_id, collection


def imported(collection, library):
    """Radarr's move-style import: the files leave; DebridPulse's folders stay."""
    library.mkdir(exist_ok=True)
    for item in sorted(collection.rglob("*"), key=lambda path: -len(path.parts)):
        if item.is_file():
            shutil.move(str(item), str(library / item.name))


async def acknowledge(world, job, client=None):  # noqa: F811
    from external import arr

    return await arr.remove(world.external, client or world.clients.r, job["id"])


async def test_c_a_moved_fan_out_folder_is_retired_on_the_acknowledging_remove(world, tmp_path):  # noqa: F811
    """661: before the correction this folder was never even attempted."""
    job, transfer_id, collection = await delivered(world)
    assert collection.is_dir() and (collection / "Akira" / "movie.mkv").is_file()
    imported(collection, tmp_path / "library")
    assert await acknowledge(world, job) == "acknowledged"
    assert not collection.exists() and Path(world.engine.root).is_dir()  # nested folder and collection folder
    assert (tmp_path / "library" / "movie.mkv").is_file()                # Radarr's library untouched
    assert (await world.repository.get(transfer_id)).state.value == "completed"


async def test_c_an_acknowledgement_before_the_move_converges_on_the_maintenance_pass(
        world, tmp_path, monkeypatch):  # noqa: F811
    job, transfer_id, collection = await delivered(world)
    assert await acknowledge(world, job) == "acknowledged"               # the file is still there
    assert (collection / "Akira" / "movie.mkv").is_file()                # never touched
    monkeypatch.setattr(external_service, "_FOLDER_RETIREMENT_ASKED_AT", 0.0)
    await world.external.maintain()
    assert collection.is_dir()                                           # still holds the file: kept
    imported(collection, tmp_path / "library")                           # Radarr's import lands later
    monkeypatch.setattr(external_service, "_FOLDER_RETIREMENT_ASKED_AT", 0.0)
    result = await world.external.maintain()
    assert result["retired"] >= 1 and not collection.exists()


async def test_c_the_retry_survives_a_restart_and_is_bounded_by_the_delivery_retention(
        world, tmp_path, monkeypatch):  # noqa: F811
    job, transfer_id, collection = await delivered(world)
    await acknowledge(world, job)
    imported(collection, tmp_path / "library")
    # A restart: nothing in memory, only the durable acknowledgement.
    monkeypatch.setattr(external_service, "_FOLDER_RETIREMENT_ASKED_AT", 0.0)
    from external.service import ExternalClients
    await ExternalClients(world.application).maintain()
    assert not collection.exists()

    # Past its retention window a delivery is never asked again.
    job2, transfer2, collection2 = await delivered(world, key="k-2", payload="box2",
                                                   files=(("other.mkv", "Other/other.mkv", 4096),))
    await acknowledge(world, job2)
    imported(collection2, tmp_path / "library2")
    async with database.get_db() as db:
        await db.execute("UPDATE external_deliveries SET expires_at=0 WHERE job_id=?", (job2["id"],))
        await db.commit()
    monkeypatch.setattr(external_service, "_FOLDER_RETIREMENT_ASKED_AT", 0.0)
    assert (await world.external.maintain())["retired"] == 1            # only the in-window delivery is asked
    assert collection2.is_dir()                                          # the expired one: never again


async def test_c_maintenance_asks_at_most_once_per_recheck_interval(world, tmp_path, monkeypatch):  # noqa: F811
    job, transfer_id, collection = await delivered(world)
    await acknowledge(world, job)
    monkeypatch.setattr(external_service, "_FOLDER_RETIREMENT_ASKED_AT", 0.0)
    assert (await world.external.maintain())["retired"] == 1
    assert (await world.external.maintain())["retired"] == 0            # throttled: no crawl every 15 s


async def test_c_unrelated_contents_or_another_live_transfer_keep_the_folder(world, tmp_path):  # noqa: F811
    job, transfer_id, collection = await delivered(world)
    imported(collection, tmp_path / "library")
    (collection / "Akira" / "notes.txt").write_text("someone else's")
    await acknowledge(world, job)
    assert (collection / "Akira" / "notes.txt").is_file() and collection.is_dir()
    # Nothing outside the collection folder, nor the download root, is touched.
    assert Path(world.engine.root).is_dir()


async def test_c_another_live_transfer_planning_material_in_the_same_folder_keeps_it(world, tmp_path):  # noqa: F811
    job, transfer_id, collection = await delivered(world)
    imported(collection, tmp_path / "library")
    other = await world.engine.submit((TransferRequest("parcel", "reuse", name="reuse"),), name="reuse",
                                      deduplicate=False)
    async with database.get_db() as db:                                  # a later transfer reusing the folder
        await db.execute("INSERT INTO download_files(torrent_id,filename,local_path,status) VALUES(?,?,?,?)",
                         (other.id, "movie.mkv", str(collection / "Akira" / "movie.mkv"), "pending"))
        await db.commit()
    outcomes = await world.engine.retire_vacated_collection(
        transfer_id, expected_demand=frozenset(str(item["id"]) for item in
                                               await world.repository.outstanding_demand(transfer_id)))
    # The reused folder is the later transfer's: kept ("claimed"), and so its
    # parent, which still holds it.
    assert set(outcomes.values()) == {"not_empty"}
    assert (collection / "Akira").is_dir() and collection.is_dir()


async def test_c_two_claimants_keep_the_folder_until_both_have_acknowledged(world, tmp_path):  # noqa: F811
    offer_fan_out(world)
    source = {"kind": "magnet", "magnet": M}
    job_a, _ = await world.external.submit(world.clients.r, body("k-a", category="movies", source=source))
    job_b, _ = await world.external.submit(world.clients.a, body("k-b", category="movies", source=source))
    transfer_id = await transfer_of(job_a["id"])
    assert await transfer_of(job_b["id"]) == transfer_id                # the same work, two claims
    await complete(world, transfer_id)
    for client, job in ((world.clients.r, job_a), (world.clients.a, job_b)):
        assert (await world.external.job(client, job["id"]))["delivery"]["state"] == "available"
    collection = Path(world.engine.root) / (await world.repository.get(transfer_id)).collection_root
    imported(collection, tmp_path / "library")
    await acknowledge(world, job_a)
    assert collection.is_dir()                                           # B has not acknowledged
    await acknowledge(world, job_b, client=world.clients.a)
    assert not collection.exists()


async def test_c_a_symlink_inside_the_collection_is_never_followed(world, tmp_path):  # noqa: F811
    job, transfer_id, collection = await delivered(world)
    imported(collection, tmp_path / "library")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (collection / "Akira").rmdir()
    (collection / "Akira").symlink_to(elsewhere, target_is_directory=True)
    await acknowledge(world, job)
    assert elsewhere.is_dir() and (collection / "Akira").is_symlink()   # neither followed nor removed
    assert collection.is_dir()


# ═══ cross-workstream: one Radarr-style torrent end to end ═══════════════════════════════════════════════════

async def test_cross_external_torrent_promotes_promptly_and_its_folder_is_retired_after_import(
        tmp_path, monkeypatch):
    """External admission, a preparing primary, a Real-Debrid backup selected
    at bind and yielded on its deadline, one route, one selection; the
    remaining steps (execution, delivery, acknowledgement, retirement) are
    proven on the production external boundary by the C tests above."""
    w, vs, marks = await timeline(tmp_path, monkeypatch, external=True)
    assert (await w.repository.get(w.transfer.id)).source == "external"
    assert marks["promoted"] - T0 == pytest.approx(DEADLINE_WAKE)
    assert "operator_switch" not in await operations(w) and w.fake.selects() == ["1"]
    primary = await rows("SELECT state FROM resolution_attempts WHERE provider_id='parcel-a'")
    assert [row["state"] for row in primary] == ["released"]             # the preparing primary given back
    assert json.dumps(marks, default=str)                                # marks are serializable evidence


# ── fair traversal: an older acknowledgement never starves behind newer ones ─────────────────────────────────

async def walk(world, monkeypatch, budget):  # noqa: F811
    """One maintenance retirement pass at ``budget`` (the throttle elapsed)."""
    monkeypatch.setattr(external_service, "_FOLDER_RETIREMENT_ASKED_AT", 0.0)
    return await world.external._retire_acknowledged_folders(budget=budget)


async def acknowledged_kept(world, key, n):  # noqa: F811
    """An acknowledged delivery whose folder still holds its file (kept)."""
    job, transfer_id, collection = await delivered(world, key=key, payload=f"box-{n}", name=f"Release{n}",
                                                   files=((f"f{n}.mkv", f"D{n}/f{n}.mkv", 4096),))
    assert collection.name == f"Release{n}"                             # its own folder
    assert await acknowledge(world, job) == "acknowledged"
    assert collection.is_dir()
    return collection


async def test_c_the_oldest_acknowledgement_is_revisited_while_newer_ones_stay_eligible(
        world, tmp_path, monkeypatch):  # noqa: F811
    """Three acknowledged deliveries, a budget of two per pass. The OLDEST
    folder empties only after its first retirement opportunity; newer ones
    stay eligible (their folders keep their files). The walk revisits and
    retires it within ceil(3 / 2) further passes -- newest-first selection
    would offer the two newer ones forever."""
    oldest = await acknowledged_kept(world, "k-old", 0)
    world.now[0] += 1
    newer = [await acknowledged_kept(world, f"k-{n}", n) for n in (1, 2)]
    world.now[0] += 1
    assert await walk(world, monkeypatch, 2) == 2                        # its first opportunity: still full
    assert oldest.is_dir()
    imported(oldest, tmp_path / "library-old")                           # Radarr's late move empties it
    for _ in range(3):
        await walk(world, monkeypatch, 2)
        if not oldest.exists():
            break
    assert not oldest.exists()
    assert all(folder.is_dir() for folder in newer)                      # still holding their files


async def test_c_a_later_acknowledgement_joins_the_walk_and_a_restart_begins_it_again(
        world, tmp_path, monkeypatch):  # noqa: F811
    oldest = await acknowledged_kept(world, "k-old", 0)
    world.now[0] += 1
    await acknowledged_kept(world, "k-1", 1)
    world.now[0] += 1
    await acknowledged_kept(world, "k-2", 2)
    assert await walk(world, monkeypatch, 2) == 2                        # oldest + k-1
    imported(oldest, tmp_path / "library-old")
    world.now[0] += 1
    later = await acknowledged_kept(world, "k-3", 3)                     # acknowledged mid-walk
    assert await walk(world, monkeypatch, 2) == 2                        # k-2 + the later one, in order
    assert oldest.is_dir() and later.is_dir()
    # A restart: the walk position is gone; it begins again from the oldest.
    monkeypatch.setattr(external_service, "_FOLDER_RETIREMENT_CURSOR", None)
    assert await walk(world, monkeypatch, 2) == 2
    assert not oldest.exists()


async def test_c_each_pass_stays_within_its_budget_and_the_walk_covers_everyone(
        world, tmp_path, monkeypatch):  # noqa: F811
    folders = []
    for n in range(5):
        folders.append(await acknowledged_kept(world, f"k-{n}", n))
        world.now[0] += 1
    asked = [await walk(world, monkeypatch, 2) for _ in range(3)]
    assert all(count <= 2 for count in asked) and sum(asked) == 5        # all five within ceil(5/2) passes
    for folder in folders:
        imported(folder, tmp_path / f"library-{folder.name}")
    for _ in range(3):
        await walk(world, monkeypatch, 2)
    assert not any(folder.exists() for folder in folders)
