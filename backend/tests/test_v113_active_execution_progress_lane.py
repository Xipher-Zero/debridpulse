"""Generalized active execution progress (canonical visibility corrective, C).

``Transfer.progress`` stays DP-valid material. ``active_execution_progress``
is the measurable progress of the current authorized running writer that is
not already DP material -- decided from the writer's durable continuation plan
alone: a destination-aware plan, or a writer whose declared continuation
capabilities cannot export material ranges (the only way in-flight work ever
becomes DP material before completion). No executor identity decides it.
"""
from __future__ import annotations

import inspect
import re
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

import db.database as database
from application.service import ApplicationService
from executors.sabnzbd.executor import SabnzbdExecutor
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers import _repository_base
from transfers.models import (
    ContinuationCapability, ExecutionActivity, ExecutionState, TransferProgress, TransferRequest,
)
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
# The production composition (``application.composition``).
from transfers.convergence_engine import TransferEngine
from transfers.recovery_repository import TransferRepository
from test_v113_runtime_telemetry_matrix import mixed  # noqa: F401 -- the real SAB + Usenet fixture

pytestmark = pytest.mark.asyncio


class NativeJobExecutor(MemoryExecutor):
    """A writer with SABnzbd's own continuation declaration: native private
    resume and quiesce, no material-range export."""
    capabilities = replace(MemoryExecutor.capabilities, continuation=SabnzbdExecutor.capabilities.continuation)


class MaterialWriterExecutor(MemoryExecutor):
    """An ordinary material writer: its reported ranges become DP material
    through checkpoints, so its progress is already canonical progress."""
    capabilities = replace(MemoryExecutor.capabilities, continuation=frozenset({
        ContinuationCapability.FULL_RESTART, ContinuationCapability.EXPORT_MATERIAL_RANGES}))


async def _core(tmp_path, monkeypatch, executor_class):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    executor = executor_class(repository.authorize_execution)
    registry.register_provider(ParcelProvider())
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"),
                            policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0, max_active_executions=2),
                            clock=lambda: 1000.0)
    await engine.initialize()
    transfer = await engine.submit((TransferRequest("parcel", "parcel", name="payload.bin"),), deduplicate=False)
    for _ in range(4):
        await engine.tick()
        if executor.jobs:
            break
    assert executor.jobs, "the writer never started"
    return SimpleNamespace(engine=engine, repository=repository, executor=executor, transfer=transfer)


async def _set_progress(core, completed, total):
    [attempt] = core.executor.jobs
    core.executor.jobs[attempt] = replace(core.executor.jobs[attempt], progress=TransferProgress(total, completed, 7))
    await core.engine.reconcile_executions()


async def _active(core):
    return next(item for item in await core.repository.active() if item.id == core.transfer.id)


async def _listed(core):
    from api import operational_downloads as downloads
    result = await downloads.list_operational_torrents(
        status=None, search=None, limit=0, offset=0,
        application=SimpleNamespace(repository=core.repository, definitions=[]))
    return next(item for item in result["items"] if item["id"] == core.transfer.id)


async def test_a_running_native_job_writer_exposes_its_progress_beside_canonical_material(tmp_path, monkeypatch):
    core = await _core(tmp_path, monkeypatch, NativeJobExecutor)
    await _set_progress(core, 3, 4)
    active = await _active(core)
    # Canonical progress is DP-valid material only; the native job wrote none.
    assert not active.progress
    assert active.active_execution_progress == pytest.approx(75.0)
    listed = await _listed(core)
    assert listed["active_execution_progress"] == pytest.approx(75.0)
    assert not listed["progress"]
    details = await core.repository.presentation(core.transfer.id, details=True)
    assert details["active_execution_progress"] == pytest.approx(75.0)


async def test_progress_change_reaches_the_active_overlay_without_a_reload(tmp_path, monkeypatch):
    core = await _core(tmp_path, monkeypatch, NativeJobExecutor)
    await _set_progress(core, 1, 4)
    before = await _active(core)
    await _set_progress(core, 2, 4)
    after = await _active(core)
    # The overlay publishes exactly when this pair changes (ApplicationService).
    assert (before.progress, before.active_execution_progress) != (after.progress, after.active_execution_progress)
    assert ApplicationService._active_overlay_item(after)["active_execution_progress"] == pytest.approx(50.0)


async def test_an_unknown_native_total_shows_no_fabricated_percentage(tmp_path, monkeypatch):
    core = await _core(tmp_path, monkeypatch, NativeJobExecutor)
    await _set_progress(core, 9, 0)
    assert (await _active(core)).active_execution_progress is None
    assert (await _listed(core))["active_execution_progress"] is None


async def test_a_full_native_lane_is_never_completion(tmp_path, monkeypatch):
    core = await _core(tmp_path, monkeypatch, NativeJobExecutor)
    await _set_progress(core, 4, 4)            # acquisition done; native processing continues
    active = await _active(core)
    assert active.active_execution_progress == pytest.approx(100.0)
    assert str(getattr(active.state, "value", active.state)) not in {"completed", "consolidated"}
    assert not active.progress
    listed = await _listed(core)
    assert listed["status"] != "completed" and not listed["progress"]


async def test_completion_leaves_canonical_progress_authoritative_and_no_stale_lane(tmp_path, monkeypatch):
    core = await _core(tmp_path, monkeypatch, NativeJobExecutor)
    await _set_progress(core, 3, 4)
    [attempt] = core.executor.jobs
    core.executor.finish(core.executor.jobs[attempt].handle)
    for _ in range(4):
        await core.engine.tick()
    details = await core.repository.presentation(core.transfer.id, details=True)
    assert details["status"] == "completed" and details["progress"] == 100.0
    assert details.get("active_execution_progress") is None


async def test_an_ordinary_material_writer_has_no_second_lane(tmp_path, monkeypatch):
    core = await _core(tmp_path, monkeypatch, MaterialWriterExecutor)
    await _set_progress(core, 3, 4)
    assert (await _active(core)).active_execution_progress is None
    assert (await _listed(core))["active_execution_progress"] is None


async def test_a_destination_aware_writer_keeps_its_reconstruction_lane(tmp_path, monkeypatch):
    core = await _core(tmp_path, monkeypatch, MaterialWriterExecutor)
    [attempt] = core.executor.jobs
    async with database.get_db() as db:
        row = await db.fetchone("SELECT continuation FROM execution_attempts WHERE id=?", (attempt,))
        plan = json.loads(row["continuation"])
        plan["strategy"] = "destination_aware"
        await db.execute("UPDATE execution_attempts SET continuation=? WHERE id=?", (json.dumps(plan), attempt))
        await db.commit()
    await _set_progress(core, 1, 4)
    assert (await _active(core)).active_execution_progress == pytest.approx(25.0)
    # The reconstruction-only read (what a source switch abandons) is unchanged.
    assert await core.repository.private_reconstruction_bytes(
        (await core.repository.artifacts(core.transfer.id))[0].id) == 1


async def test_unauthorized_or_stopped_writers_contribute_nothing(tmp_path, monkeypatch):
    core = await _core(tmp_path, monkeypatch, NativeJobExecutor)
    await _set_progress(core, 3, 4)
    [attempt] = core.executor.jobs
    async with database.get_db() as db:
        await db.execute("UPDATE execution_attempts SET authorized=0 WHERE id=?", (attempt,))
        await db.commit()
    assert (await _active(core)).active_execution_progress is None
    async with database.get_db() as db:
        await db.execute("UPDATE execution_attempts SET authorized=1,state=? WHERE id=?",
                         (ExecutionState.PAUSED.value, attempt))
        await db.commit()
    assert (await _active(core)).active_execution_progress is None


async def test_a_native_job_does_not_count_as_reconstruction_a_switch_abandons(tmp_path, monkeypatch):
    core = await _core(tmp_path, monkeypatch, NativeJobExecutor)
    await _set_progress(core, 3, 4)
    artifact = (await core.repository.artifacts(core.transfer.id))[0]
    assert await core.repository.private_reconstruction_bytes(artifact.id) == 0


async def test_the_neutral_read_names_no_executor():
    source = inspect.getsource(_repository_base.active_execution_progress_sql).casefold()
    for name in ("sabnzbd", "rsync", "aria2", "nzb", "usenet"):
        assert name not in source


def test_the_shared_progress_presentation_words_the_lane_for_any_executor():
    from pathlib import Path
    app = (Path(__file__).resolve().parents[2] / "frontend" / "static" / "app.js").read_text()
    # The renderer's logic (progress() assembles progressParts(), 1.0.14).
    renderer = app[app.index("function progressParts(pct, status, activePct, basis, eta)"):]
    renderer = renderer[:renderer.index("\n}\n")]
    details = app[app.index("function dpDetailProgress(t)"):]
    details = details[:details.index("\n}\n")]
    predicate = app[app.index("function inFlightExecutionPercent(status, activePct)"):]
    predicate = predicate[:predicate.index("\n}\n")]
    words = app[app.index("function executionProgressWords(value, basis)"):]
    words = words[:words.index("\n}\n")]
    # One active-state predicate gates both presentations.
    assert "inFlightExecutionPercent(state, activePct)" in renderer
    assert "inFlightExecutionPercent(status, t.active_execution_progress)" in details
    assert "!== 'downloading'" in predicate
    for source in (renderer, details, predicate, words):
        lowered = source.casefold()
        assert "reconstruct" not in lowered
        for name in ("sabnzbd", "rsync", "destination_aware", "strategy"):
            assert name not in lowered
    # One wording of what the figure counts, from the neutral basis alone.
    assert "'in progress ' +" in words and "basis === 'units' ? ' of parts'" in words
    assert "executionProgressWords(activeValue, basis)" in renderer
    assert "executionProgressWords(active, t.active_execution_basis)" in details
    # The lane stays secondary and explicit about what it is not.
    assert "not yet verified as DebridPulse material" in renderer
    assert "' verified'" in renderer                     # the canonical number keeps its meaning
    # Promotion: the execution value leads only while nothing is verified (no
    # verified percentage, or exactly 0%), and the two are never compared.
    assert "const activeOnly = inFlight && (unknown || actual === 0);" in renderer
    assert "'in progress'" in renderer and ">not yet verified</span>" in renderer
    assert not re.search(r"activeValue\s*[<>]=?\s*actual|actual\s*[<>]=?\s*activeValue", renderer)
    for smoothing in ("highest", "high_water", "highWater", "floor", "max_seen", "maxSeen"):
        assert smoothing not in renderer


async def test_a_usenet_job_keeps_a_known_verified_zero_while_its_acquisition_lane_advances(mixed):  # noqa: F811
    """The observed Usenet shape, on the real SAB executor and Usenet provider:
    the NZB declares the payload size, so verified progress is a KNOWN 0% (not
    unknown) until import, while SAB's acquisition counter -- over its own,
    different denominator (posted article megabytes) -- is the lane."""
    from api import operational_downloads as downloads
    from test_v113_runtime_telemetry_matrix import VALID_NZB, converge
    await mixed.engine.submit((TransferRequest("nzb", VALID_NZB, name="posting.nzb"),),
                              name="posting", deduplicate=False)
    await converge(mixed)
    [job] = mixed.sab.queue.values()

    async def listed(mbleft):
        job.mb, job.mbleft = 1.0, mbleft
        await mixed.engine.reconcile_executions()
        result = await downloads.list_operational_torrents(
            status=None, search=None, limit=0, offset=0,
            application=SimpleNamespace(repository=mixed.repository, definitions=[]))
        [row] = result["items"]
        return row

    early, later = await listed(0.75), await listed(0.25)
    assert early["progress"] == 0.0 and later["progress"] == 0.0                 # known, verified, zero
    assert early["active_execution_progress"] == pytest.approx(25.0)
    assert later["active_execution_progress"] == pytest.approx(75.0)
    assert early["active_execution_basis"] == later["active_execution_basis"] == "bytes"
    # Not comparable: the transfer's denominator is the declared payload, the
    # lane's is SAB's article total -- so the lane is never promoted.
    assert later["size_bytes"] == 1024
    [attempt] = await mixed.repository.live_execution_handles()
    assert mixed.sab.queue and attempt[1] == "downloading"

    # Repair / unpack: the acquisition figure is retired, verified stays 0.
    job.status = "Extracting"
    unpacking = await listed(0.0)
    assert unpacking["active_execution_progress"] is None and unpacking["active_execution_basis"] == "processing"
    assert unpacking["progress"] == 0.0


async def _set(core, progress, *, network_active=True):
    [attempt] = core.executor.jobs
    core.executor.jobs[attempt] = replace(core.executor.jobs[attempt], progress=progress, activity=ExecutionActivity(
        network_active=network_active, bandwidth_reservation_required=network_active, progress_expected=True))
    await core.engine.reconcile_executions()


async def test_completed_units_are_a_percentage_of_their_own_basis(tmp_path, monkeypatch):
    core = await _core(tmp_path, monkeypatch, NativeJobExecutor)
    await _set(core, TransferProgress(0, 700, 7, 3, 12))
    active, listed = await _active(core), await _listed(core)
    assert (active.active_execution_progress, active.active_execution_basis) == (pytest.approx(25.0), "units")
    assert (listed["active_execution_progress"], listed["active_execution_basis"]) == (pytest.approx(25.0), "units")
    details = await core.repository.presentation(core.transfer.id, details=True)
    assert details["active_execution_basis"] == "units"
    assert ApplicationService._active_overlay_item(active)["active_execution_basis"] == "units"


@pytest.mark.parametrize("units", [(13, 12), (3, 0), (None, 12)])
async def test_inconsistent_or_unknown_units_project_no_percentage(tmp_path, monkeypatch, units):
    core = await _core(tmp_path, monkeypatch, NativeJobExecutor)
    await _set(core, TransferProgress(0, 700, 7, *units))
    listed = await _listed(core)
    assert listed["active_execution_progress"] is None and listed["active_execution_basis"] is None


async def test_a_writer_past_acquisition_retires_its_percentage(tmp_path, monkeypatch):
    core = await _core(tmp_path, monkeypatch, NativeJobExecutor)
    await _set(core, TransferProgress(4, 3))
    assert (await _listed(core))["active_execution_basis"] == "bytes"
    await _set(core, TransferProgress(4, 4), network_active=False)            # remux / finalization
    active = await _active(core)
    assert (active.active_execution_progress, active.active_execution_basis) == (None, "processing")
    assert not active.progress                                               # verified material untouched


async def test_a_mix_of_byte_and_unit_writers_is_no_scope():
    row = {"execution_writers": 2, "execution_acquiring": 2, "execution_bytes_known": 1, "execution_total": 100,
           "execution_completed": 50, "execution_units_known": 1, "execution_units_total": 8,
           "execution_units_completed": 4}
    assert _repository_base.active_execution_projection(row) == (None, None)
    assert _repository_base.active_execution_projection({**row, "execution_bytes_known": 2})[1] == "bytes"
    assert _repository_base.active_execution_projection({**row, "execution_units_known": 2}) == (50.0, "units")
    assert _repository_base.active_execution_projection({**row, "execution_acquiring": 0}) == (None, "processing")
    assert _repository_base.active_execution_projection({}) == (None, None)



# -- the one primary bar: verified, exact in-flight, estimated, or processing -----------

def _render(cases):
    """The real ``progress()`` / ``dpDetailProgress()`` of app.js, run under node."""
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is required to run the shared progress renderer")
    app = (Path(__file__).resolve().parents[2] / "frontend" / "static" / "app.js").read_text()
    # progress() assembles its output from progressParts() (1.0.14), so both are the real owner.
    names = ("progress(pct, status, activePct, basis, eta)", "progressParts(pct, status, activePct, basis, eta)",
             "dpDetailProgress(t)", "executionProgressWords(value, basis)",
             "executionProcessing(status, basis)", "transferDisplayStatus(t)",
             "inFlightExecutionPercent(status, activePct)")
    functions = []
    for name in names:
        body = app[app.index("function " + name):]
        functions.append(body[:body.index("\n}\n") + 3])
    script = ("const fmtSize = value => value + ' B';\n" + "\n".join(functions)
              + f"\nconst cases = {json.dumps(cases)};\n"
              + "console.log(JSON.stringify(cases.map(([pct, status, active, basis]) => ({"
              + "bar: progress(pct, status, active, basis), "
              + "details: dpDetailProgress({progress: pct, presentation_status: status, "
              + "active_execution_progress: active, active_execution_basis: basis})}))));")
    completed = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(completed.stdout)


def test_an_estimated_whole_plan_figure_is_the_one_labelled_primary_bar():
    """P8: the estimate fills the primary bar, says ``estimated`` in visible
    text, has no secondary lane beside verified material, and agrees with
    Details; an unknown native scope stays the indeterminate stripe."""
    nothing, zero, nonzero, unknown, done = _render([
        [None, "downloading", 25.7, "estimated"], [0, "downloading", 25.7, "estimated"],
        [40, "downloading", 25.7, "estimated"], [None, "downloading", None, None], [100, "completed", None, None]])
    for shown in (nothing, zero):
        assert "is-indeterminate" not in shown["bar"] and "width:25.7%" in shown["bar"]
        assert "~25.7% estimated" in shown["bar"] and "prog-lane" not in shown["bar"]
        assert shown["details"] == "~25.7% estimated (not yet verified)"
    # Verified material keeps the bar; the estimate is not a second figure beside it.
    assert "width:40%" in nonzero["bar"] and "prog-lane" not in nonzero["bar"] and "estimated" not in nonzero["bar"]
    assert nonzero["details"] == "40.0%"
    assert "is-indeterminate" in unknown["bar"]
    assert ">100%<" in done["bar"] and "estimated" not in done["bar"]


def test_processing_keeps_the_estimated_fill_and_says_processing_never_a_percentage():
    [processing, retired] = _render([[0, "downloading", 82.1, "processing"], [0, "downloading", None, "processing"]])
    assert "width:82.1%" in processing["bar"] and "is-indeterminate" not in processing["bar"]
    assert ">processing<" in processing["bar"] and "82.1%<" not in processing["bar"]
    assert processing["details"] == "processing (not yet verified)"
    assert "is-indeterminate" in retired["bar"] and ">processing<" in retired["bar"]   # no fill known: unchanged



def test_an_estimate_is_one_writers_own_never_combined_and_never_behind_or_beyond_its_exact_scope():
    """P3/P7 on the neutral projection: one writer's estimate mark is shown,
    labelled, while it is ahead of that writer's exact figure, and the exact
    figure once it catches up -- below 100% for such a writer; two writers'
    estimates have no common denominator, an estimate past acquisition is only
    a processing fill, and an older row (or any writer without an estimate)
    is unchanged."""
    one = {"execution_writers": 1, "execution_acquiring": 1, "execution_estimates_known": 1,
           "execution_estimated": 42.5}
    exact_bytes = {"execution_bytes_known": 1, "execution_total": 100}
    exact_units = {"execution_units_known": 1, "execution_units_total": 8}
    assert _repository_base.active_execution_projection(one) == (42.5, "estimated")
    assert _repository_base.active_execution_projection(
        {**one, **exact_bytes, "execution_completed": 30}) == (42.5, "estimated")      # the mark is ahead
    assert _repository_base.active_execution_projection(
        {**one, **exact_bytes, "execution_completed": 60}) == (60.0, "bytes")          # exact caught up
    assert _repository_base.active_execution_projection(
        {**one, **exact_units, "execution_units_completed": 2}) == (42.5, "estimated")
    assert _repository_base.active_execution_projection(
        {**one, **exact_units, "execution_units_completed": 6}) == (75.0, "units")
    assert _repository_base.active_execution_projection(
        {**one, **exact_bytes, "execution_completed": 100}) == (99.9, "bytes")         # never 100% while running
    without = {"execution_writers": 1, "execution_acquiring": 1, **exact_bytes, "execution_completed": 100}
    assert _repository_base.active_execution_projection(without) == (100.0, "bytes")   # other executors unchanged
    assert _repository_base.active_execution_projection(
        {**one, "execution_writers": 2, "execution_acquiring": 2, "execution_estimates_known": 2}) == (None, None)
    assert _repository_base.active_execution_projection({**one, "execution_acquiring": 0}) == (42.5, "processing")
    for unusable in (None, "nan", float("inf"), "x"):
        assert _repository_base.active_execution_projection({**one, "execution_estimated": unusable}) == (None, None)
    assert _repository_base.active_execution_projection({**one, "execution_estimated": 250})[0] == 99.9
    older = {key: value for key, value in one.items() if "estimate" not in key}
    assert _repository_base.active_execution_projection(older) == (None, None)


async def test_an_estimate_on_a_stopped_or_unauthorized_writer_contributes_nothing(tmp_path, monkeypatch):
    """P6/P7 through the real SQL: the estimate rides the writer's own row, so
    it is retired exactly with that writer."""
    core = await _core(tmp_path, monkeypatch, NativeJobExecutor)
    await _set(core, TransferProgress(0, 700, 7, estimated_percentage=61.2))
    assert ((await _listed(core))["active_execution_progress"], (await _listed(core))["active_execution_basis"]) \
        == (pytest.approx(61.2), "estimated")
    details = await core.repository.presentation(core.transfer.id, details=True)
    assert (details["active_execution_progress"], details["active_execution_basis"]) == (pytest.approx(61.2),
                                                                                         "estimated")
    assert ApplicationService._active_overlay_item(await _active(core))["active_execution_basis"] == "estimated"
    assert not (await _active(core)).progress                          # verified material untouched
    [attempt] = core.executor.jobs
    async with database.get_db() as db:
        row = await db.fetchone("SELECT progress FROM execution_attempts WHERE id=?", (attempt,))
        assert json.loads(row["progress"])["estimated_percentage"] == 61.2   # the writer's own row, no new table
        await db.execute("UPDATE execution_attempts SET authorized=0 WHERE id=?", (attempt,))
        await db.commit()
    assert (await _active(core)).active_execution_progress is None
    async with database.get_db() as db:
        await db.execute("UPDATE execution_attempts SET authorized=1,state=? WHERE id=?",
                         (ExecutionState.PAUSED.value, attempt))
        await db.commit()
    assert (await _active(core)).active_execution_basis is None


def test_a_progress_row_written_before_estimates_loads_unchanged():
    from transfers import codec
    older = TransferProgress(**codec.load('{"total_bytes":0,"completed_bytes":5,"bytes_per_second":1,'
                                          '"completed_units":null,"total_units":null,"acquiring":true}', {}))
    assert older.estimated_percentage is None and older.completed_bytes == 5
