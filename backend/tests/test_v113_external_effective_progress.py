"""DP 1.0.13: an *arr client sees the progress DebridPulse itself shows.

DebridPulse's primary bar (``app.js`` ``progress()``) shows the current
in-flight execution figure while a downloading transfer has nothing verified
yet; the SABnzbd dialect's queue reported verified material alone (0 for a
native Usenet job until import) and hardcoded ``mb``/``mbleft`` to 0.00.
Radarr and Sonarr draw their queue bar from ``mb``/``mbleft`` only
(``QueueRow.tsx``: ``100 - sizeleft / size * 100``; ``SabnzbdQueueItem``
``Percentage`` is parsed but never read; Radarr v6.4.4.10685, Sonarr
v4.0.20.3014), so the acceptance is on those fields: they carry the
executor's own exact byte counters whenever DebridPulse shows exactly those,
and stay SABnzbd's unknown form otherwise -- never a declared NZB size and
never a total derived from a percentage.

``transfers._repository_base.effective_progress`` is the one decision; the
external projection carries it as ``progress.effective`` beside the
unchanged verified ``progress.percent``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import db.database as database
from api import arr_sabnzbd
from external.service import ExternalClients
from test_v113_active_execution_progress_lane import _render
from test_v113_runtime_telemetry_matrix import mixed  # noqa: F401 -- the real SAB + Usenet fixture
from transfers._repository_base import active_execution_bytes, effective_progress
from transfers.models import TransferRequest

pytestmark = pytest.mark.asyncio

MIB = 1024 * 1024


async def render_slot(effective, *, verified=0.0, reason="downloading") -> dict:
    """The SABnzbd queue slot the dialect renders for one job whose external
    projection carries ``effective`` (none: projected before this change)."""
    job = {"id": "job_x", "display_name": "posting", "category": "tv", "reason": reason,
           "progress": {"basis": "members", "percent": verified, **({"effective": effective} if effective else {})}}

    async def visible(*_args, **_kwargs):
        return {"jobs": [job], "total": 1, "globally_paused": False}

    body = await arr_sabnzbd._queue(SimpleNamespace(visible=visible), None, {})
    [slot] = body["queue"]["slots"]
    return slot


def arr_progress(slot) -> float:
    """Radarr's ``QueueRow.tsx``: ``size ? 100 - (sizeleft / size) * 100 : 0``
    over ``TotalSize = mb * 1 MiB``, ``RemainingSize = mbleft * 1 MiB``."""
    size, sizeleft = float(slot["mb"]) * MIB, float(slot["mbleft"]) * MIB
    return 100 - (sizeleft / size) * 100 if size else 0


# ── the real Usenet job, mid-download: DP's bar == the *arr queue bar ──────

async def test_a_running_usenet_job_shows_radarr_the_bar_dp_shows(mixed):  # noqa: F811
    from api import operational_downloads as downloads
    from test_v113_runtime_telemetry_matrix import VALID_NZB, converge
    await mixed.engine.submit((TransferRequest("nzb", VALID_NZB, name="posting.nzb"),),
                              name="posting", deduplicate=False)
    await converge(mixed)
    [job] = mixed.sab.queue.values()
    external = ExternalClients(SimpleNamespace(repository=mixed.repository))

    async def observed(mbleft, status="Downloading"):
        job.mb, job.mbleft, job.status = 1.0, mbleft, status
        await mixed.engine.reconcile_executions()
        listed = await downloads.list_operational_torrents(
            status=None, search=None, limit=0, offset=0,
            application=SimpleNamespace(repository=mixed.repository, definitions=[]))
        [row] = listed["items"]
        async with database.get_db() as db:
            transfer = await db.fetchone("SELECT id, status, progress FROM torrents WHERE id=?", (row["id"],))
        return row, await external._effective_progress(transfer)

    for mbleft in (0.75, 0.25):
        row, effective = await observed(mbleft)
        # DP's own primary bar, rendered by the real app.js.
        [rendered] = _render([[row["progress"], row["status"], row["active_execution_progress"],
                               row["active_execution_basis"]]])
        assert row["progress"] == 0.0 and 'data-progress-basis="execution"' in rendered["bar"]
        width = float(rendered["bar"].split('style="width:', 1)[1].split("%", 1)[0])
        assert width == pytest.approx(row["active_execution_progress"])
        # The external figure is that bar, with the executor's exact counters.
        assert effective["percent"] == pytest.approx(width)
        assert effective == {"percent": round(row["active_execution_progress"], 1), "source": "execution",
                             "estimated": False, "completed_bytes": int((1.0 - mbleft) * MIB), "total_bytes": MIB}
        slot = await render_slot(effective)
        assert (slot["mb"], slot["mbleft"]) == ("1.00", "%.2f" % mbleft)
        assert arr_progress(slot) == pytest.approx(row["active_execution_progress"], abs=0.1)
        assert slot["percentage"] == str(int(row["active_execution_progress"])) and slot["status"] == "Downloading"
        # Nothing is invented: no time left, and verified material is unchanged.
        assert slot["timeleft"] == "0:00:00"

    # Repair / unpack: the acquisition figure is retired in DP and for *arr.
    row, effective = await observed(0.0, status="Extracting")
    assert row["active_execution_basis"] == "processing"
    assert effective["source"] == "verified" and effective["total_bytes"] is None
    slot = await render_slot(effective)
    assert (slot["mb"], slot["mbleft"], slot["percentage"]) == ("0.00", "0.00", "0")


# ── the decision, state by state ────────────────────────────────────────────

ROW = {"execution_writers": 1, "execution_acquiring": 1, "execution_bytes_known": 1,
       "execution_completed": 3 * MIB, "execution_total": 4 * MIB}


@pytest.mark.parametrize("status,verified,active,basis,source,percent", [
    ("downloading", 0.0, 65.2, "estimated", "execution", 65.2),     # 0 verified / 65.2 estimated
    ("downloading", None, 40.0, "bytes", "execution", 40.0),        # verified unknown
    ("downloading", 12.5, 65.2, "estimated", "verified", 12.5),     # nonzero verified wins
    ("downloading", 12.5, 80.0, "bytes", "verified", 12.5),
    ("downloading", 0.0, None, None, "verified", 0.0),              # no in-flight figure
    ("downloading", 0.0, None, "processing", "verified", 0.0),      # past acquisition
    ("downloading", 0.0, 50.0, "processing", "verified", 0.0),
    ("paused", 0.0, 65.2, "estimated", "verified", 0.0),            # only while downloading
    ("queued", 0.0, 10.0, "bytes", "verified", 0.0),
    ("extracting", 0.0, 10.0, "bytes", "verified", 0.0),
    ("completed", 100.0, None, None, "verified", 100.0),
    ("downloading", 0.0, 99.9, "estimated", "execution", 99.9),     # never 100 before materialization
])
async def test_the_effective_figure_follows_dps_own_primary_bar(status, verified, active, basis, source, percent):
    exact = (3 * MIB, 4 * MIB) if basis == "bytes" else None
    effective = effective_progress(status, verified, active, basis, exact)
    assert (effective["source"], effective["percent"]) == (source, percent)
    assert effective["estimated"] == (source == "execution" and basis == "estimated")
    # Exact bytes only with an exact in-flight byte figure; never with an
    # estimate, never beside verified material.
    assert (effective["total_bytes"] is not None) == (source == "execution" and basis == "bytes")
    # The same state through DP's renderer: the in-flight figure is its
    # primary bar exactly when it is the external one. (A processing
    # writer's fill is labelled "processing", not an acquisition figure.)
    [rendered] = _render([[verified, status, active, basis]])
    promoted = 'data-progress-basis="execution"' in rendered["bar"]
    assert promoted == (source == "execution") or basis == "processing"
    if basis == "processing" and promoted:
        assert ">processing<" in rendered["bar"] or "processing" in rendered["bar"]


async def test_an_estimate_has_no_size_so_the_arr_bar_shows_none_rather_than_a_fabricated_one():
    effective = effective_progress("downloading", 0.0, 65.2, "estimated", None)
    slot = await render_slot(effective)
    assert slot["percentage"] == "65"
    # No grounded total: *arr draws no bar rather than an invented one.
    assert (slot["mb"], slot["mbleft"], arr_progress(slot)) == ("0.00", "0.00", 0)


async def test_a_job_projected_before_this_change_still_renders_from_verified_progress():
    slot = await render_slot(None, verified=42.0)
    assert (slot["percentage"], slot["mb"], slot["mbleft"]) == ("42", "0.00", "0.00")


def test_exact_bytes_come_only_from_a_coherent_current_byte_scope():
    assert active_execution_bytes(ROW) == (3 * MIB, 4 * MIB)
    assert active_execution_bytes({**ROW, "execution_acquiring": 0}) is None          # processing
    assert active_execution_bytes({**ROW, "execution_bytes_known": 0}) is None        # no byte total
    assert active_execution_bytes({**ROW, "execution_writers": 2}) is None            # mixed scope
    assert active_execution_bytes({**ROW, "execution_estimates_known": 1, "execution_estimated": 90.0}) is None
    assert active_execution_bytes({}) is None and active_execution_bytes(None) is None
