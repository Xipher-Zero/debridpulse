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
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

import db.database as database
from application.service import ApplicationService
from executors.sabnzbd.executor import SabnzbdExecutor
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers import _repository_base
from transfers.models import ContinuationCapability, ExecutionState, TransferProgress, TransferRequest
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
# The production composition (``application.composition``).
from transfers.convergence_engine import TransferEngine
from transfers.recovery_repository import TransferRepository

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
    renderer = app[app.index("function progress(pct, status, activePct)"):]
    renderer = renderer[:renderer.index("\n}\n")]
    details = app[app.index("t.active_execution_progress == null"):][:200]
    for source in (renderer, details):
        lowered = source.casefold()
        assert "reconstruct" not in lowered
        for name in ("sabnzbd", "rsync", "destination_aware", "strategy"):
            assert name not in lowered
    assert "in progress ' +" in renderer and "in progress ' +" in details
    # The lane stays secondary and explicit about what it is not.
    assert "not yet verified as DebridPulse material" in renderer
    assert "' verified'" in renderer                     # the canonical number keeps its meaning
