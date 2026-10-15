"""A backup handed its selection is read back at once (transfer 660, 1.0.13).

Transfer 660 (build 0df18b53): a select-all, one-file torrent routed to a
still-preparing AllDebrid route, with a Real-Debrid backup. The coordinator's
backup observation at +36.7 s found RD waiting for its file selection,
recorded it PREPARING and handed it DP's established select-all
(``_select_on_standby`` -> ``synchronize_selection``). RD then had the content
at once, but nothing read the backup again until its next poll (+66.7 s): the
operator switched at +57.4 s and the takeover observation found it AVAILABLE.

Corrected at the backup selection owner: a selection the provider says it
applied is read back once, at once -- the same policy a route's own selection
already follows -- and that reading is the backup's observation. A provider
ready as soon as it is told what to deliver is recorded AVAILABLE in that
pass, and the readiness promotion that follows in the same pass yields to it.
Still preparing, it waits for the ordinary backup poll; nothing is ever sent
twice.

Also: a backup whose preparation failed recoverably (Debrid-Link
``notAddTorrent``, ``resolution_temporarily_failed``, backoff) is worded
"Retrying…" -- with when and why -- instead of "Deferred"; its backend state
stays ``deferred``.

The timing cases run the production ``core.scheduler.sync_status_loop`` over a
virtual clock (``test_v113_readiness_handoff_timeline``); every provider is a
local fake.
"""
from __future__ import annotations

import asyncio

import pytest
from test_v113_readiness_handoff_timeline import POLL, operations, standby, timeline
from test_v113_readiness_promotion import history, rows
from test_v113_realdebrid_upstream_selection import (
    FILES_592, MOVIE, FakeRealDebrid, choose, lab as rd_lab, preparing_parcel, root as rd_root, submit as rd_submit,
    ticks as rd_ticks,
)

from db.database import get_db
from providers.realdebrid.provider import RealDebridProvider
from transfers import codec
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage
from transfers.manual_route_switch import route_providers, standby_choice
from transfers.models import ResourceState

pytestmark = pytest.mark.asyncio


def reads(fake):
    return fake.count("torrent_info")


# -- 660: ready as soon as selected -> recognized and yielded in that same pass ----------------------------------

@pytest.mark.parametrize("external", [False, True], ids=["native", "external"])
async def test_a_backup_ready_once_selected_is_read_back_and_yielded_in_the_selection_pass(
        tmp_path, monkeypatch, external):
    w, vs, marks = await timeline(tmp_path, monkeypatch, external=external)
    assert w.fake.selects() == ["1"]                                      # DP's select-all, sent once
    assert marks["available_by"] == "pass"
    assert marks["available"] == marks["selected"]                       # read back in the selection pass
    assert marks["promoted"] == marks["selected"]                        # and yielded there: no extra poll
    assert (await operations(w))[-1] == "readiness_promotion"
    assert "operator_switch" not in await operations(w)


async def test_native_and_external_selection_read_back_are_identical(tmp_path, monkeypatch):
    native, _vs, native_marks = await timeline(tmp_path / "native", monkeypatch)
    external, _vs, external_marks = await timeline(tmp_path / "external", monkeypatch, external=True)
    assert (await external.repository.get(external.transfer.id)).source == "external"
    assert await operations(external) == await operations(native)
    assert external.fake.selects() == native.fake.selects()
    assert ({key: external_marks[key] - external_marks["selected"] for key in ("available", "promoted")}
            == {key: native_marks[key] - native_marks["selected"] for key in ("available", "promoted")})


async def test_selection_acknowledged_but_not_ready_stays_preparing_until_the_ordinary_poll(tmp_path, monkeypatch):
    """Real-Debrid accepted the selection but is still acquiring: the
    read-back records the truth (PREPARING), the menu says Preparing, and
    nothing moves; ready 10 s later, the next ordinary backup poll -- one
    interval after the read-back -- recognizes it and yields in that pass."""
    w, vs, marks = await timeline(tmp_path, monkeypatch, ready=10.0)
    assert w.fake.selects() == ["1"]
    assert marks["available_by"] == "pass"
    assert marks["available"] == marks["selected"] + POLL                 # bounded by the ordinary poll
    assert marks["promoted"] == marks["available"]
    assert w.fake.selects() == ["1"]                                      # never sent again


async def test_a_preparing_read_back_is_what_the_menu_shows(tmp_path, monkeypatch):
    w, vs, marks = await timeline(tmp_path, monkeypatch, ready=10_000.0, passes=3)
    assert (await standby(w))["resource_state"] == ResourceState.PREPARING.value
    choice = {item["provider_id"]: item["status"] for item in
              (await route_providers(w.engine, w.transfer.id))["providers"]}
    assert choice["realdebrid"] == "preparing"
    assert "promoted" not in marks


# -- selection integrity ------------------------------------------------------------------------------------------

async def test_an_explicit_subset_is_handed_unaltered_and_read_back_at_once(tmp_path, monkeypatch):
    fake = FakeRealDebrid()                                               # #592: five files, movie is id 3
    first = preparing_parcel("parcel-a", [(path.rsplit("/", 1)[-1], path, size) for _i, path, size in FILES_592])
    repository, engine = await rd_lab(tmp_path, monkeypatch, first, RealDebridProvider(fake, prepare_backup_torrents=True))
    transfer = await rd_submit(engine)                                    # interactive
    await rd_ticks(engine, 4, step=POLL)
    assert fake.selects() == []                                           # undecided: nothing invented
    await choose(repository, engine, transfer, {MOVIE})
    before = reads(fake)
    await rd_ticks(engine, 1, step=POLL)                                  # the pass that hands the choice over
    assert fake.selects() == ["3"]                                        # exactly the movie
    root = await rd_root(repository, transfer)
    operations_ = [row["operation"] for row in await history(root.id)]
    assert operations_[-1] == "readiness_promotion"                       # yielded in that same pass
    assert reads(fake) > before


async def test_an_unknown_selection_is_never_invented_nor_read_back(tmp_path, monkeypatch):
    fake = FakeRealDebrid()
    first = preparing_parcel("parcel-a", [(path.rsplit("/", 1)[-1], path, size) for _i, path, size in FILES_592])
    repository, engine = await rd_lab(tmp_path, monkeypatch, first, RealDebridProvider(fake, prepare_backup_torrents=True))
    transfer = await rd_submit(engine)
    await rd_ticks(engine, 6, step=POLL)
    assert fake.selects() == []
    assert (await rd_root(repository, transfer)).resource.provider_id == "parcel-a"


# -- failures around the selection --------------------------------------------------------------------------------

async def test_an_uncertain_selection_is_never_sent_again_and_is_recognized_on_the_next_poll(tmp_path, monkeypatch):
    """The selection is applied but its answer is lost: nothing is read back
    in that pass and nothing is re-sent; the next poll's synchronization
    reads first, finds it applied, and the backup is yielded to then."""
    w, vs, marks = await timeline(tmp_path, monkeypatch, passes=6, ready="now", before_inventory=None)
    assert marks["promoted"] == marks["selected"]                        # control: the ordinary case

    w, vs, marks = await _with_select_failure(tmp_path / "lost", monkeypatch, "lost")
    assert w.fake.selects() == ["1"]                                      # sent once, never repeated
    assert marks["available"] == marks["selected"] + POLL
    assert marks["promoted"] == marks["available"]


async def test_a_failed_read_back_records_nothing_and_the_next_poll_decides(tmp_path, monkeypatch):
    w, vs, marks = await _with_read_failure(tmp_path, monkeypatch)
    assert w.fake.selects() == ["1"]
    assert marks["available"] == marks["selected"] + POLL                 # nothing was recorded from the failure
    assert marks["promoted"] == marks["available"]


async def _with_select_failure(tmp_path, monkeypatch, failure):
    import test_v113_readiness_handoff_timeline as harness

    original = harness.world

    async def world(*args, **kwargs):
        w = await original(*args, **kwargs)
        w.fake.select_errors.append(failure)
        return w

    monkeypatch.setattr(harness, "world", world)
    return await timeline(tmp_path, monkeypatch, passes=6)


async def _with_read_failure(tmp_path, monkeypatch):
    import test_v113_readiness_handoff_timeline as harness
    from providers.realdebrid.client import RealDebridAPIError

    original = harness.world

    async def world(*args, **kwargs):
        w = await original(*args, **kwargs)
        info = w.fake.torrent_info

        async def torrent_info(native_id):
            if w.fake.selects() and not getattr(w.fake, "_failed_once", False):
                w.fake._failed_once = True                                # the read right after the selection
                w.fake.calls.append(("torrent_info", native_id))
                raise RealDebridAPIError(-1, "service_unavailable", 503)
            return await info(native_id)

        w.fake.torrent_info = torrent_info
        return w

    monkeypatch.setattr(harness, "world", world)
    return await timeline(tmp_path, monkeypatch, passes=6)


# -- races with the follow-up observation ---------------------------------------------------------------------------

async def _racing(tmp_path, monkeypatch, act):
    """``act(w)`` lands while Real-Debrid is answering the selection -- after
    the coordinator decided to send it, before its read-back."""
    import test_v113_readiness_handoff_timeline as harness

    original = harness.world

    async def world(*args, **kwargs):
        w = await original(*args, **kwargs)

        async def during_select():
            w.fake.during_select = None
            await act(w)

        w.fake.during_select = during_select
        return w

    monkeypatch.setattr(harness, "world", world)
    return await timeline(tmp_path, monkeypatch, passes=5)


async def _pause(w):
    await w.engine.pause(w.transfer.id)


async def _delete(w):
    await w.engine.delete(w.transfer.id, remote=False)


async def _withdraw(w):
    demands = tuple(row["id"] for row in await rows("SELECT id FROM transfer_demands WHERE transfer_id=?",
                                                     (w.transfer.id,)))
    await w.engine.withdraw_demand(demands, by="external-client")


@pytest.mark.parametrize("act", [_pause, _delete, _withdraw], ids=["paused", "deleted", "demand-withdrawn"])
async def test_a_transfer_paused_deleted_or_withdrawn_during_the_selection_is_not_read_back_or_promoted(
        tmp_path, monkeypatch, act):
    w, vs, marks = await _racing(tmp_path, monkeypatch, act)
    assert w.fake.selects() == ["1"]
    assert "promoted" not in marks
    assert "readiness_promotion" not in await operations(w)


async def test_an_already_executing_route_is_never_switched_by_a_ready_backup(tmp_path, monkeypatch):
    async def executing(w):
        async with get_db() as db:                                        # the route crossed its boundary
            await db.execute("UPDATE transfer_file_selections SET manifest_committed_at=? WHERE transfer_id=? "
                             "AND provider_id='parcel-a'", (w.engine.clock(), w.transfer.id))
            await db.commit()

    w, vs, marks = await _racing(tmp_path, monkeypatch, executing)
    assert marks["available"] == marks["selected"]                        # read back and recorded ready...
    assert "promoted" not in marks                                        # ...yet never switched to


# -- ownership: one mutation, one promotion, one writer ---------------------------------------------------------

async def test_one_selection_one_readiness_promotion_one_fan_out(tmp_path, monkeypatch):
    """The read-back adds one provider read, never a mutation, a route or a
    writer: one torrent, one selection, one readiness yield, one route
    attempt per provider on the root, and the one member fanned out once."""
    w, vs, marks = await timeline(tmp_path, monkeypatch, passes=8)
    assert w.fake.selects() == ["1"] and w.fake.count("add_magnet") == 1
    assert (await operations(w)).count("readiness_promotion") == 1
    root = await rd_root(w.repository, w.transfer)
    attempts = await rows("SELECT provider_id, COUNT(*) AS n FROM resolution_attempts WHERE request_id=? "
                          "GROUP BY provider_id", (root.id,))
    assert {row["provider_id"]: row["n"] for row in attempts} == {"parcel-a": 1, "realdebrid": 1}
    members = await rows("SELECT id FROM transfer_requests WHERE parent_id=?", (root.id,))
    assert len(members) == 1
    executions = await rows("SELECT COUNT(*) AS n FROM execution_attempts WHERE transfer_id=?", (w.transfer.id,))
    assert executions[0]["n"] <= 1                                        # never a second writer


# -- Debrid-Link: a recoverable deferral is "Retrying…" -----------------------------------------------------------

def _error(category, retryability):
    return NormalizedError(Domain.PROVIDER, category, Stage.RESOLUTION, retryability=retryability,
                           integration_id="debridlink", native_code="notAddTorrent")


@pytest.mark.parametrize("error, expected", [
    (_error(Category.RESOLUTION_TEMPORARILY_FAILED, Retryability.BACKOFF), ("retrying", True)),   # transfer 660
    (_error(Category.RATE_LIMITED, Retryability.BACKOFF), ("deferred", True)),
    (_error(Category.CONCURRENCY_LIMITED, Retryability.BACKOFF), ("deferred", True)),
    (_error(Category.QUOTA_EXCEEDED, Retryability.BACKOFF), ("deferred", True)),
    (None, ("deferred", True)),                                          # a reclaimed backup: no failure
], ids=["recoverable", "rate-limited", "concurrency", "quota", "reclaimed"])
async def test_only_a_recoverable_failure_awaiting_retry_is_worded_retrying(error, expected):
    assert standby_choice({"state": "deferred", "resource_state": None, "error": error}) == expected


async def test_the_menu_says_retrying_with_when_and_why_for_debrid_link_s_660_deferral(tmp_path, monkeypatch):
    w, vs, marks = await timeline(tmp_path, monkeypatch, ready=10_000.0, passes=2)
    root = await rd_root(w.repository, w.transfer)
    error = _error(Category.RESOLUTION_TEMPORARILY_FAILED, Retryability.BACKOFF)
    async with get_db() as db:                                            # 660's recorded Debrid-Link backup
        await db.execute(
            "INSERT INTO standby_resources(id,transfer_id,request_id,provider_id,generation,state,error,attempts,"
            "retry_at,created_at,updated_at) VALUES('dl-660',?,?,'parcel-dl',1,'deferred',?,1,?,?,?)",
            (w.transfer.id, root.id, codec.dump(error), w.engine.clock() + 45, w.engine.clock(),
             w.engine.clock()))
        await db.commit()
    from test_v113_root_provider_switch import magnet_provider
    w.engine.registry.register_provider(magnet_provider("parcel-dl"))
    view = {item["provider_id"]: item for item in (await route_providers(w.engine, w.transfer.id))["providers"]}
    dl = view["parcel-dl"]
    assert (dl["status"], dl["selectable"]) == ("retrying", True)
    assert dl["retry_in_seconds"] == 45
    assert dl["detail"] == error.message
    async with get_db() as db:
        row = await db.fetchone("SELECT state FROM standby_resources WHERE id='dl-660'")
    assert row["state"] == "deferred"                                    # the backend state is unchanged
    await asyncio.sleep(0)
