"""Readiness-to-promotion handoff, timed through the real scheduler loop
(transfer 659, 1.0.13).

Transfer 659 (build 009e7949): an external select-all, one-file torrent
routed to a still-preparing primary, with a Real-Debrid backup handed that
selection. Its RD binding was journaled ``resource.available`` at
21:52:04.13 -- written by provider inventory reconciliation, outside a
coordinator pass (the backup's own observation stamp stayed at 21:51:48.43)
-- and nothing woke the coordinator for it: the yield would have waited for
the next scheduled pass (~21:52:18.4); the operator switched at 21:52:12.17.

Corrected: inventory reconciliation that commits a live, bound, unpromoted
backup's transition to AVAILABLE answers it (``inventory_observation``), and
``ApplicationService.reconcile_inventory`` sets the one existing resolution
wakeup after that commit. The wake is only a scheduling hint: the
coordinator's readiness promotion re-reads both resources and decides every
guard in its own commit, exactly as before.

These tests drive ``core.scheduler.sync_status_loop`` itself -- the
production loop body, its wait arithmetic and its wakeup event -- over a
virtual clock: only ``_wait_for_work`` is replaced, by a stand-in that
advances the engine clock by exactly the wait the loop asked for and runs
scheduled out-of-pass events at their virtual instant, ending the wait as
soon as the wakeup is set (as ``asyncio.wait_for`` would). Inventory runs
through the real ``ApplicationService.reconcile_inventory``.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_v113_readiness_promotion import history
from test_v113_realdebrid_upstream_selection import (
    MOVIE, MOVIE_BYTES, FakeRealDebrid, lab as rd_lab, preparing_parcel, root as rd_root,
)

import core.scheduler as scheduler
from application.service import ApplicationService
from db.database import get_db
from fake_integrations import ParcelProvider
from providers.realdebrid.provider import RealDebridProvider
from transfers.models import ResourceState, SubmissionDemand, TransferRequest

pytestmark = pytest.mark.asyncio

POLL = 30.0                                   # transfer 659's recorded resource_poll_interval
HASH = "3db68b30a953af479c17c396b69c95223e41b741"
SINGLE = [(1, f"Akira/{MOVIE}", MOVIE_BYTES)]
T0 = 1_792_101_070.0                          # 659's acceptance instant


class _Stop(Exception):
    pass


class CountingEvent(asyncio.Event):
    def __init__(self):
        super().__init__()
        self.sets = 0

    def set(self):
        self.sets += 1
        super().set()


class VirtualScheduler:
    """Runs the production ``sync_status_loop`` against one engine."""

    def __init__(self, engine, repository, monkeypatch, *, passes):
        self.engine, self.repository = engine, repository
        self.events, self.passes, self.log = [], passes, []
        self.wakeup = CountingEvent()
        self.after_pass = None

        @asynccontextmanager
        async def admitted():
            yield

        # The real application inventory entry point, on the pieces it uses.
        self.app = SimpleNamespace(application_operation=admitted, repository=repository, engine=engine,
                                   resolution_wakeup=self.wakeup)

        async def resolve_pending():
            self.log.append(("pass", engine.clock()))
            await engine.resolve_pending()
            if self.after_pass:
                await self.after_pass(engine.clock())

        async def started():
            return None

        monkeypatch.setattr(scheduler, "application", SimpleNamespace(
            integrations_started=started, resolve_pending=resolve_pending, engine=engine,
            resolution_wakeup=self.wakeup))
        monkeypatch.setattr(scheduler, "_wait_for_work", self.wait)

    async def inventory(self):
        return await ApplicationService.reconcile_inventory(self.app)

    def at(self, instant, action):
        self.events.append((instant, action))
        self.events.sort(key=lambda item: item[0])

    async def wait(self, event, timeout):
        self.log.append(("wait", self.engine.clock(), round(timeout, 6), event.is_set()))
        if len([item for item in self.log if item[0] == "pass"]) >= self.passes:
            raise _Stop
        if event.is_set():
            return                                                    # woken before the wait began
        end = self.engine.clock() + timeout
        while self.events and self.events[0][0] <= end:
            instant, action = self.events.pop(0)
            self.engine.clock.now = max(self.engine.clock(), instant)
            await action()
            if event.is_set():
                return                                                # woken during the wait
        self.engine.clock.now = end

    async def run(self):
        with pytest.raises(_Stop):
            await scheduler.sync_status_loop()

    def passes_at(self):
        return [instant for kind, instant, *_rest in self.log if kind == "pass"]


class HeldInventory(ParcelProvider):
    """An unrelated provider registered after Real-Debrid whose inventory
    answer is held until the test releases it -- a slow or stalled request
    later in the same inventory scan. It claims no torrent."""

    def __init__(self):
        super().__init__("held-inventory")
        self.release = asyncio.Event()
        self.entered = asyncio.Event()

    async def inventory(self):
        self.entered.set()
        await self.release.wait()
        return await super().inventory()


async def world(tmp_path, monkeypatch, *, external=False, held=False, ready=True):
    fake = FakeRealDebrid(SINGLE, name="Akira", fingerprint=HASH, ready=ready)
    first = preparing_parcel("parcel-a", [(MOVIE, f"Akira/{MOVIE}", MOVIE_BYTES)])
    realdebrid = RealDebridProvider(fake, prepare_backup_torrents=True)
    extra = (HeldInventory(),) if held else ()
    repository, engine = await rd_lab(tmp_path, monkeypatch, first, realdebrid, *extra)
    engine.clock.now = T0
    request = TransferRequest("magnet", f"magnet:?xt=urn:btih:{HASH}", name="Akira", fingerprint=HASH,
                              selection_mode="all")
    options = dict(source="external", demand=SubmissionDemand("radarr", "token")) if external else {}
    transfer = await engine.submit((request,), name="Akira", deduplicate=False, **options)
    return SimpleNamespace(fake=fake, realdebrid=realdebrid, repository=repository, engine=engine,
                           transfer=transfer, held=extra[0] if extra else None)


async def standby(w):
    return next(item for item in await w.repository.standbys(w.transfer.id) if item["provider_id"] == "realdebrid")


async def operations(w):
    return [row["operation"] for row in await history((await rd_root(w.repository, w.transfer)).id)]


def become_ready(w):
    """Real-Debrid finishes acquiring the selected content remotely."""
    for torrent in w.fake.torrents.values():
        torrent.ready = True


async def timeline(tmp_path, monkeypatch, *, inventory_offsets=(), during_pass=False, before_inventory=None,
                   external=False, passes=8, held=False, ready=None):
    """Drive the loop. ``inventory_offsets``: seconds after the pass that
    handed the selection over at which provider inventory runs (through the
    application). ``during_pass``: run it inside that very next pass's tail
    instead -- after the loop cleared its wakeup, before it waits.
    ``before_inventory``: an action applied just before the first inventory
    run. ``ready``: when Real-Debrid can deliver once selected -- ``"now"``
    (at once: the selection pass's own read-back finds it), ``"remote"``
    (only later, between passes: it becomes ready just before the first
    inventory run, which is how that readiness reaches DebridPulse), or a
    number of seconds after the selection pass (an asynchronous transition
    nothing but the ordinary backup poll observes). Default: ``"remote"``
    with inventory, ``"now"`` without. Returns (world, scheduler, marks)."""
    ready = ready if ready is not None else ("remote" if inventory_offsets or during_pass else "now")
    w = await world(tmp_path, monkeypatch, external=external, held=held, ready=ready == "now")
    vs = VirtualScheduler(w.engine, w.repository, monkeypatch, passes=passes)
    marks = {}

    async def inventory():
        if "inventory" not in marks:
            if ready == "remote":
                become_ready(w)
            if before_inventory:
                await before_inventory(w)
        wakes, reads = vs.wakeup.sets, w.fake.count("torrent_info")
        if w.held is None:
            try:
                await vs.inventory()
            except Exception as exc:                                  # e.g. a write that rolled back
                marks.setdefault("inventory_error", []).append(type(exc).__name__)
        else:
            # The scan runs on its own, as the full-sync loop's does, while a
            # later provider's answer is held; the loop's wait ends as soon
            # as the wake is set.
            scan = asyncio.create_task(vs.inventory())
            await w.held.entered.wait()                               # Real-Debrid done; the next provider held
            marks["woken_while_scan_pending"] = vs.wakeup.is_set() and not scan.done()
            marks["scan"] = scan
        marks.setdefault("inventory", []).append((w.engine.clock(), vs.wakeup.sets - wakes))
        marks.setdefault("inventory_reads", []).append(w.fake.count("torrent_info") - reads)
        item = await standby(w)
        if item["resource_state"] == ResourceState.AVAILABLE.value and "available" not in marks:
            marks["available"], marks["available_by"] = w.engine.clock(), "inventory"

    async def after_pass(now):
        item = await standby(w)
        if w.fake.selects() and "selected" not in marks:
            marks["selected"] = now
            if during_pass:
                marks["during_pass"] = True
                await inventory()                                     # the loop has cleared, not yet waited
            for offset in inventory_offsets:
                vs.at(now + offset, inventory)
            if isinstance(ready, (int, float)):
                async def remote():
                    become_ready(w)
                vs.at(now + ready, remote)
        if item["resource_state"] == ResourceState.AVAILABLE.value and "available" not in marks:
            marks["available"], marks["available_by"] = now, "pass"
        if "promoted" not in marks and "readiness_promotion" in await operations(w):
            marks["promoted"] = now

    vs.after_pass = after_pass
    await vs.run()
    return w, vs, marks


# -- the coordinator's own observation: unchanged, same pass ----------------------------------------------------

async def test_readiness_the_coordinator_observes_is_acted_on_in_the_same_pass(tmp_path, monkeypatch):
    # Ledger (transfer 660): a provider ready as soon as it is told what to
    # deliver is read back in the very pass that handed it the selection --
    # recorded AVAILABLE and yielded to there -- where it used to wait one
    # full backup poll (``available - selected == POLL``).
    w, vs, marks = await timeline(tmp_path, monkeypatch)
    assert w.fake.count("add_magnet") == 1 and w.fake.selects() == ["1"]  # select-all of the one file
    assert (await operations(w))[-1] == "readiness_promotion"
    assert "operator_switch" not in await operations(w)
    assert marks["available_by"] == "pass" and marks["available"] == marks["selected"]
    assert marks["promoted"] == marks["available"]                       # the very pass that observed READY
    assert vs.wakeup.sets == 0                                            # no inventory, no wake
    assert all(item[2] <= POLL for item in vs.log if item[0] == "wait")


# -- readiness recorded by inventory between passes: woken, not waiting a poll ---------------------------------

@pytest.mark.parametrize("offset", [16.0, 15.0, 0.5], ids=["659-shape", "halfway", "just-after-a-pass"])
async def test_readiness_inventory_records_between_passes_wakes_the_coordinator_at_once(
        tmp_path, monkeypatch, offset):
    """The 659 reproduction. Before the correction this yield waited for the
    next scheduled pass (``POLL - offset`` later: 14 s in 659's shape)."""
    w, vs, marks = await timeline(tmp_path, monkeypatch, inventory_offsets=(offset,))
    assert marks["available_by"] == "inventory"
    assert marks["available"] == marks["selected"] + offset
    assert marks["inventory"][0][1] == 1                                  # one wake, after the commit
    assert marks["available"] in vs.passes_at()                          # a pass runs at that very instant
    assert marks["promoted"] == marks["available"]                       # ready -> yield: 0 s, not POLL - offset
    assert (await operations(w))[-1] == "readiness_promotion"
    assert "operator_switch" not in await operations(w)
    item = await standby(w)
    assert item["promoted_at"] is not None


async def test_a_wake_racing_the_loop_s_wait_is_not_lost(tmp_path, monkeypatch):
    """Inventory commits and wakes while the coordinator is still finishing
    a pass -- after the loop cleared its wakeup, before it waits: the wait
    returns at once and the next pass yields, with no time passing."""
    w, vs, marks = await timeline(tmp_path, monkeypatch, during_pass=True)
    assert marks["during_pass"] and marks["available_by"] == "inventory"
    woken = [item for item in vs.log if item[0] == "wait" and item[3]]
    assert woken and woken[0][1] == marks["available"]                    # entered the wait already woken
    assert marks["promoted"] == marks["available"]
    assert (await operations(w))[-1] == "readiness_promotion"


async def test_a_duplicate_available_observation_wakes_nothing_and_reads_nothing(tmp_path, monkeypatch):
    """Paused, the transfer is never promoted, so its backup stays AVAILABLE
    across inventory runs: only the transition wakes the coordinator; an
    unchanged AVAILABLE neither wakes it nor causes a provider read."""
    async def pause(w):
        await w.engine.pause(w.transfer.id)

    w, vs, marks = await timeline(tmp_path, monkeypatch, inventory_offsets=(10.0, 20.0), before_inventory=pause)
    (first_at, first_wakes), (second_at, second_wakes) = marks["inventory"]
    assert (first_wakes, second_wakes) == (1, 0)
    assert "promoted" not in marks and "readiness_promotion" not in await operations(w)
    # Only the transition brought a pass: after the woken pass the next one
    # is the ordinary scheduled one, so the duplicate caused no pass -- and
    # with it no backup or primary read -- and its inventory run read no
    # resource itself.
    passes = vs.passes_at()
    assert first_at in passes
    assert [instant for instant in passes if first_at < instant < first_at + POLL] == []
    assert marks["inventory_reads"] == [0, 0]


# -- the wake follows each commit, not the end of the scan ---------------------------------------------------------

async def test_the_wake_follows_the_readiness_commit_while_a_later_provider_is_still_pending(tmp_path, monkeypatch):
    """Real-Debrid's inventory commits the backup's AVAILABLE; the next
    provider's inventory answer is then held. The scheduler is woken before
    that provider answers, the coordinator yields while the scan is still
    pending, and the scan then finishes normally."""
    w, vs, marks = await timeline(tmp_path, monkeypatch, inventory_offsets=(16.0,), held=True, passes=4)
    scan = marks["scan"]
    assert marks["woken_while_scan_pending"] is True                     # observable before provider B answers
    assert marks["inventory"][0][1] == 1                                  # one wake for the one transition
    assert marks["available_by"] == "inventory"
    assert marks["promoted"] == marks["available"]                       # yielded at that instant
    assert not scan.done()                                               # ...while the scan was still held
    assert "readiness_promotion" in await operations(w)
    w.held.release.set()
    result = await scan                                                  # the rest of the scan finishes normally
    assert result["errors"] == []
    assert vs.wakeup.sets == 1                                           # no further wake from the same scan


async def test_a_rolled_back_readiness_write_does_not_wake_the_scheduler(tmp_path, monkeypatch):
    """The write that would record the backup AVAILABLE fails inside its own
    transaction: nothing commits, nothing wakes, and the coordinator keeps
    its ordinary cadence."""
    async def failing_write(w):
        original, failed = w.repository._resource, []

        async def write(db, transfer_id, resource, state, **kwargs):
            binding = await original(db, transfer_id, resource, state, **kwargs)
            if resource.provider_id == "realdebrid" and state == ResourceState.AVAILABLE and not failed:
                failed.append(True)                                   # this one write only
                raise RuntimeError("write failed before commit")
            return binding

        w.repository._resource = write

    w, vs, marks = await timeline(tmp_path, monkeypatch, inventory_offsets=(16.0,), before_inventory=failing_write,
                                  passes=4)
    assert marks["inventory_error"] == ["RuntimeError"]
    assert marks["inventory"][0][1] == 0 and vs.wakeup.sets == 0         # rolled back: no wake
    inventory_at = marks["inventory"][0][0]
    assert inventory_at not in vs.passes_at()                            # no pass was brought forward
    async with get_db() as db:
        journaled = await db.fetchall("SELECT 1 FROM event_journal WHERE event_type='resource.available' "
                                      "AND integration_id='realdebrid' AND occurred_at<=?", (10**10,))
    # Nothing committed at the inventory instant; the coordinator's own next
    # observation is what records the readiness, on its ordinary cadence.
    assert marks["available_by"] == "pass" and marks["available"] > inventory_at
    assert len(journaled) == 1


# -- a wake is a hint, never authority -----------------------------------------------------------------------------

async def _pause(w):
    await w.engine.pause(w.transfer.id)


async def _cancel(w):
    await w.engine.cancel(w.transfer.id)


async def _delete(w):
    await w.engine.delete(w.transfer.id, remote=False)


async def _executing(w):
    async with get_db() as db:                                            # the root crossed its execution boundary
        await db.execute("UPDATE transfer_file_selections SET manifest_committed_at=? WHERE transfer_id=? "
                         "AND provider_id='parcel-a'", (w.engine.clock(), w.transfer.id))
        await db.commit()


@pytest.mark.parametrize("make_ineligible, wakes", [
    (_pause, 1), (_cancel, 0), (_delete, 0), (_executing, 1)],
    ids=["paused", "cancelled", "deleted", "already-executing"])
async def test_ineligible_work_is_never_promoted_because_a_wake_occurred(tmp_path, monkeypatch, make_ineligible,
                                                                         wakes):
    w, vs, marks = await timeline(tmp_path, monkeypatch, inventory_offsets=(15.0,), before_inventory=make_ineligible)
    assert marks["inventory"][0][1] == wakes                             # retired work does not even wake it
    assert "promoted" not in marks
    assert "readiness_promotion" not in await operations(w)


async def test_a_target_no_longer_ready_at_the_authoritative_re_read_is_not_promoted(tmp_path, monkeypatch):
    """Inventory said AVAILABLE and woke the coordinator; the coordinator's
    own re-read of the backup finds it preparing again: nothing moves, and
    the backup's state follows that re-read."""
    async def regressed(w):
        real = w.realdebrid.observe

        async def observe(resource):
            observed = await real(resource)
            return replace(observed, state=ResourceState.PREPARING)

        w.realdebrid.observe = observe

    w, vs, marks = await timeline(tmp_path, monkeypatch, inventory_offsets=(15.0,), before_inventory=regressed,
                                  passes=4)
    assert marks["inventory"][0][1] == 1
    assert "promoted" not in marks and "readiness_promotion" not in await operations(w)
    assert (await standby(w))["resource_state"] == ResourceState.PREPARING.value


# -- native and external are one lifecycle ------------------------------------------------------------------------

@pytest.mark.parametrize("offset", [None, 16.0], ids=["coordinator-observed", "inventory-recorded"])
async def test_an_external_submission_takes_the_identical_path(tmp_path, monkeypatch, offset):
    offsets = () if offset is None else (offset,)
    native, _vs, native_marks = await timeline(tmp_path / "native", monkeypatch, inventory_offsets=offsets)
    external, _vs, external_marks = await timeline(tmp_path / "external", monkeypatch, inventory_offsets=offsets,
                                                   external=True)
    assert (await external.repository.get(external.transfer.id)).source == "external"
    assert await operations(external) == await operations(native)
    assert {key: external_marks[key] - external_marks["selected"] for key in ("available", "promoted")} == {
        key: native_marks[key] - native_marks["selected"] for key in ("available", "promoted")}
