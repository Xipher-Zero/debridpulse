"""Phase A — provider preparation is eager and independent of executor capacity.

Torrent/Magnet File-Selection Lifecycle Correction §2 (Invariant 2/3), §10, §18.

Executor capacity is a *dispatch* constraint enforced only in
``reconcile_executions`` / ``_dispatch``. The provider
resolve/observe/manifest/offer lifecycle in ``resolve_pending`` never inspects
``max_active_executions``, ``live_executions`` or executor occupancy. These
tests prove that with every configured execution slot occupied a newly added
interactive torrent still fully prepares (resource resolved, manifest recorded,
120s hold opened, ``file_selection_available`` queued) and that only the
confirmed subset dispatches once a slot frees — no torrent-only executor bypass.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import ParcelProvider
from file_selection_support import Clock
from transfers import file_selection as fs
from transfers.engine import TransferEngine
from transfers.models import ResourceState, TransferRequest
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository

from test_file_selection_lifecycle import RecordingExecutor

FILES = [
    ("e1.mkv", "S1/e1.mkv", 10), ("e2.mkv", "S1/e2.mkv", 20), ("e3.mkv", "S1/e3.mkv", 30),
    ("e4.mkv", "S1/e4.mkv", 40),
]


@pytest_asyncio.fixture
async def core(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "fs-exec-independence.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider(identity="parcel-lab", file_manifest=True)
    executor = RecordingExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    clock = Clock(1000.0)
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(adoption_stability_seconds=0, resource_poll_interval=1,
                              retry_delay=0, resolution_retry_delay=0,
                              max_active_executions=2, max_attempts=8),
        clock=clock,
    )
    await engine.initialize()
    return SimpleNamespace(engine=engine, repository=repository, registry=registry,
                           provider=provider, executor=executor, clock=clock)


async def _submit(core, payload, *, selection_mode="interactive"):
    return await core.engine.submit(
        (TransferRequest("parcel", payload, name=payload, selection_mode=selection_mode),),
        deduplicate=False)


async def _members(core, transfer_id):
    return [r for r in await core.repository.requests(transfer_id) if r.parent_id]


async def _selection_row(transfer_id):
    async with database.get_db() as db:
        return await db.fetchone(
            "SELECT * FROM transfer_file_selections WHERE transfer_id=? ORDER BY created_at DESC LIMIT 1",
            (transfer_id,))


async def _drain(core, rounds=8):
    for _ in range(rounds):
        await core.engine.tick()
        core.clock.advance(1)


@pytest.mark.asyncio
async def test_cached_interactive_torrent_fully_prepares_while_every_executor_slot_is_full(core):
    # Occupy both execution slots with two ordinary single-file transfers.
    for tag in ("FILL1", "FILL2"):
        core.provider.responses.append(
            core.provider.parcel(tag, state=ResourceState.AVAILABLE,
                                 files=[(f"{tag}.bin", f"{tag}.bin", 5)]))
        await _submit(core, tag, selection_mode="all")
    await _drain(core)
    assert len(core.executor.started) == 2
    occupied = sum(a.state in {"prepared", "queued", "transferring", "unknown"}
                   for a in await core.repository.live_executions())
    assert occupied == 2                                    # slots are full

    # Add a cached multi-file interactive torrent.
    core.provider.responses.append(
        core.provider.parcel("NEW", state=ResourceState.AVAILABLE, files=FILES))
    transfer = await _submit(core, "NEW")
    submitted_at = core.clock()
    await core.engine.resolve_pending()

    # Provider preparation ran to completion despite executor saturation.
    assert ("resolve", "NEW") in core.provider.calls
    row = await _selection_row(transfer.id)
    assert row is not None
    assert row["manifest_id"] is not None
    assert row["hold_until"] == submitted_at + fs.IMMEDIATE_DECISION_HOLD_SECONDS
    assert row["decision"] == "pending"
    assert row["auto_offer_queued_at"] is not None
    async with database.get_db() as db:
        events = await db.fetchall(
            "SELECT 1 FROM application_events WHERE transfer_id=? AND kind='file_selection_available'",
            (transfer.id,))
    assert len(events) == 1

    # No executable manifest fetched, no child request, no executor attempt for
    # the new torrent while the slots are still full.
    assert ("manifest", "parcel-lab:NEW") not in core.provider.calls
    assert await _members(core, transfer.id) == []
    assert len(core.executor.started) == 2

    # Confirm a subset while the executor is still saturated.
    view = await core.repository.file_selection_presentation(transfer.id, now=core.clock())
    keep = [view["entries"][1]["entry_id"], view["entries"][3]["entry_id"]]   # e2, e4
    result = await core.repository.confirm_file_selection(transfer.id, view["manifest_id"], keep, now=core.clock())
    assert result.outcome == fs.SelectionOutcome.CONFIRMED

    await _drain(core)
    # The confirmed subset's children may be authorized/queued, but dispatch is
    # still capacity-blocked: no third executor attempt yet.
    assert len(core.executor.started) == 2
    members = sorted(r.entry.relative_path for r in await _members(core, transfer.id))
    assert members == ["S1/e2.mkv", "S1/e4.mkv"]

    # Free one slot; only the selected work may now dispatch.
    core.executor.finish(core.executor.started[0])
    await _drain(core, rounds=12)
    selected_started = {
        req.candidate.relative_path for req in core.executor.started
        if req.candidate.relative_path in {"S1/e2.mkv", "S1/e4.mkv"}}
    assert selected_started                                 # selected work dispatched once a slot freed
    for req in core.executor.started:
        assert req.candidate.relative_path not in {"S1/e1.mkv", "S1/e3.mkv"}   # unselected never dispatched


@pytest.mark.asyncio
async def test_uncached_interactive_torrent_observes_and_records_manifest_while_slots_are_full(core):
    from dataclasses import replace as _replace

    for tag in ("FILL1", "FILL2"):
        core.provider.responses.append(
            core.provider.parcel(tag, state=ResourceState.AVAILABLE,
                                 files=[(f"{tag}.bin", f"{tag}.bin", 5)]))
        await _submit(core, tag, selection_mode="all")
    await _drain(core)
    assert len(core.executor.started) == 2

    prepare = core.provider.parcel("NEW", state=ResourceState.PREPARING)
    core.provider.members["parcel-lab:NEW"] = ()
    core.provider.responses.append(prepare)
    transfer = await _submit(core, "NEW")
    await core.engine.resolve_pending()
    row = await _selection_row(transfer.id)
    assert row["initially_available"] == 0 and row["hold_until"] is None

    # Provider keeps preparing; observation still progresses with slots full.
    core.clock.set(1200.0)
    await core.engine.resolve_pending()
    assert ("observe", "parcel-lab:NEW") in core.provider.calls
    assert (await _selection_row(transfer.id))["hold_until"] is None   # no manifest yet, no hold

    # A manifest becomes available while still PREPARING — the hold opens now,
    # anchored to arrival, executor capacity irrelevant.
    from file_selection_support import file_manifest as _fm
    core.clock.set(1240.0)
    core.provider.resources["parcel-lab:NEW"] = _replace(
        prepare.observation, file_manifest=_fm(*FILES))
    await core.engine.resolve_pending()
    row = await _selection_row(transfer.id)
    assert row["manifest_id"] is not None
    assert row["hold_until"] == 1240.0 + fs.IMMEDIATE_DECISION_HOLD_SECONDS
    assert len(core.executor.started) == 2                  # still no dispatch for NEW
