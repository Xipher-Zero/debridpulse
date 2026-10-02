"""Completed-transfer resubmission is a new lineage decided by current material.

Historical completion is not the identity of a later submission, and a
completed row is not proof that its payload still exists:

* the same logical object submitted again is a DISTINCT transfer;
* when the earlier canonical material is present and verified NOW, the new
  transfer consolidates into it through ordinary canonical equivalence -- no
  second acquisition, durable cross-transfer provenance, the earlier transfer
  untouched;
* when that material is gone, the new transfer inherits nothing (route,
  candidates, resource, execution, exhaustion history) and competes through
  current provider truth only -- a provider disabled since is never asked.

A parent whose every obligation terminally failed before anything
materialized converges to FAILED, and stays there across a restart.

Every integration here is a neutral fixture.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from test_v113_provider_exhaustion_failover import PROVIDER_FINAL, RouteLab
from transfers.convergence_engine import TransferEngine
from transfers.models import ResourceState, TransferRequest, TransferState
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio


async def build(tmp_path, *providers):
    repository = TransferRepository()
    registry = IntegrationRegistry()
    for provider in providers:
        registry.register_provider(provider)
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_executor(executor)
    now = [1000.0]
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"),
                            policy=TransferPolicy(retry_delay=0, adoption_stability_seconds=0, max_attempts=3),
                            clock=lambda: now[0])
    await engine.initialize()
    return SimpleNamespace(engine=engine, repository=repository, registry=registry, executor=executor, now=now)


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()


def request():
    return TransferRequest("parcel", "box", name="payload.bin", fingerprint="logical-object")


async def settle(core, transfer_id, cycles=8):
    for _ in range(cycles):
        await core.engine.tick()
        for attempt in await core.repository.executions(transfer_id):
            if attempt.state not in {"succeeded", "failed", "absent", "cancelled"}:
                core.executor.finish(attempt.handle)
        core.now[0] += 5
        if (await core.repository.get(transfer_id)).state in {TransferState.COMPLETED, TransferState.CONSOLIDATED}:
            return


async def completed(core):
    transfer = await core.engine.submit((request(),), name="logical")
    await settle(core, transfer.id)
    assert (await core.repository.get(transfer.id)).state == TransferState.COMPLETED
    artifact = (await core.repository.artifacts(transfer.id))[0]
    assert Path(artifact.target).read_bytes() == b"done"
    return transfer, artifact


async def consolidations(transfer_id):
    async with database.get_db() as connection:
        return await connection.fetchall(
            """SELECT a.source_transfer_id,c.torrent_id AS canonical_transfer_id,c.id AS canonical_artifact_id
               FROM artifact_consolidations a JOIN download_files c ON c.id=a.canonical_artifact_id
               WHERE a.source_transfer_id=?""", (transfer_id,))


async def route_providers(core, transfer_id):
    detail = await core.repository.presentation(transfer_id, details=True)
    return [item["provider_id"] for item in detail["route_attempts"]]


# -- 14.1: present material ---------------------------------------------------

async def test_a_resubmitted_completed_object_with_present_material_consolidates_as_a_new_lineage(tmp_path, db):
    core = await build(tmp_path, ParcelProvider())
    a, artifact = await completed(core)
    starts_before = [call for call in core.executor.calls if call[0] == "start"]

    b = await core.engine.submit((request(),), name="logical")
    await settle(core, b.id)

    assert b.id != a.id
    assert (await core.repository.get(b.id)).state == TransferState.CONSOLIDATED
    # No second acquisition: nothing was started for B.
    assert await core.repository.executions(b.id) == ()
    assert [call for call in core.executor.calls if call[0] == "start"] == starts_before
    # Durable cross-transfer provenance into A's canonical artifact.
    assert [(row["canonical_transfer_id"], row["canonical_artifact_id"]) for row in await consolidations(b.id)] == [
        (a.id, artifact.id)]
    # A remains the completed canonical material owner, untouched.
    assert (await core.repository.get(a.id)).state == TransferState.COMPLETED
    assert (await core.repository.artifacts(a.id))[0].state == "completed"
    assert Path(artifact.target).read_bytes() == b"done"


async def test_a_completed_row_alone_never_satisfies_a_resubmission(tmp_path, db):
    core = await build(tmp_path, ParcelProvider())
    a, artifact = await completed(core)
    Path(artifact.target).unlink()

    b = await core.engine.submit((request(),), name="logical")
    await settle(core, b.id)

    assert b.id != a.id
    assert await consolidations(b.id) == []
    assert (await core.repository.get(b.id)).state == TransferState.COMPLETED
    assert len(await core.repository.executions(b.id)) == 1
    # A's history is not rewritten into B.
    assert (await core.repository.get(a.id)).state == TransferState.COMPLETED
    assert (await core.repository.artifacts(a.id))[0].execution != (await core.repository.artifacts(b.id))[0].execution


# -- 14.2: missing material, current provider truth -------------------------------

async def test_missing_material_resubmission_inherits_nothing_and_skips_a_since_disabled_provider(tmp_path, db):
    historical, current = ParcelProvider("parcel-alpha"), ParcelProvider("parcel-beta")
    core = await build(tmp_path, historical, current)
    a, artifact = await completed(core)
    assert await route_providers(core, a.id) == ["parcel-alpha"]
    Path(artifact.target).unlink()
    historical.descriptor = replace(historical.descriptor, enabled=False)
    asked_before = list(historical.calls)

    b = await core.engine.submit((request(),), name="logical")
    await settle(core, b.id)

    assert b.id != a.id
    assert historical.calls == asked_before                     # never asked for B
    assert ("resolve", "box") in current.calls
    assert await route_providers(core, b.id) == ["parcel-beta"]
    assert await consolidations(b.id) == []
    assert (await core.repository.get(b.id)).state == TransferState.COMPLETED
    acquired = (await core.repository.artifacts(b.id))[0]
    assert acquired.candidates[acquired.selected].provider_id == "parcel-beta"
    # A's provider remains A's history only.
    assert await route_providers(core, a.id) == ["parcel-alpha"]
    assert (await core.repository.get(a.id)).state == TransferState.COMPLETED


# -- 14.8: terminal parent convergence --------------------------------------------

async def test_every_route_exhausted_before_materialization_fails_the_parent_and_survives_restart(tmp_path, db):
    only = RouteLab("alpha-route")
    only.always = PROVIDER_FINAL
    core = await build(tmp_path, only)
    transfer = await core.engine.submit((TransferRequest("parcel", "logical-object"),), name="logical",
                                        deduplicate=False)
    for _ in range(4):
        await core.engine.tick()

    parent = await core.repository.get(transfer.id)
    assert parent.state == TransferState.FAILED and parent.error.category == PROVIDER_FINAL.category
    detail = await core.repository.presentation(transfer.id, details=True)
    assert detail["status"] == "error"
    assert [(item["provider_id"], item["resolution_state"]) for item in detail["route_attempts"]] == [
        ("alpha-route", "exhausted")]

    restarted_provider = RouteLab("alpha-route")
    restarted = await build(tmp_path, restarted_provider)
    for _ in range(3):
        await restarted.engine.tick()
    assert (await restarted.repository.get(transfer.id)).state == TransferState.FAILED
    assert restarted_provider.resolved == []


async def test_a_remaining_provider_keeps_the_parent_out_of_failed(tmp_path, db):
    first, second = RouteLab("alpha-route"), RouteLab("beta-route")
    first.always = PROVIDER_FINAL
    core = await build(tmp_path, first, second)
    transfer = await core.engine.submit((TransferRequest("parcel", "logical-object"),), name="logical",
                                        deduplicate=False)

    await core.engine.resolve_pending()
    await core.engine.reconcile_executions()

    assert first.resolved == ["logical-object"]
    assert (await core.repository.get(transfer.id)).state != TransferState.FAILED
    for _ in range(3):
        await core.engine.tick()
    assert second.resolved == ["logical-object"]
    assert (await core.repository.get(transfer.id)).state != TransferState.FAILED


async def test_a_new_lineage_may_bind_the_native_resource_its_retired_generation_still_holds(tmp_path, db):
    """The provider answers B with the very resource A's completed lifecycle
    still binds (nothing cleaned it up): a retired generation coexists with
    its successor, never an ownership conflict."""
    provider = ParcelProvider()
    core = await build(tmp_path, provider)
    provider.responses.append(provider.parcel("box", state=ResourceState.AVAILABLE, files=[("payload.bin", "payload.bin", 4)]))
    a = await core.engine.submit((request(),), name="logical")
    await settle(core, a.id)
    assert (await core.repository.get(a.id)).state == TransferState.COMPLETED
    held = [resource.id for resource, _state, _pending in await core.repository.resources(a.id)]
    assert held == ["parcel-lab:box"]
    for row in await core.repository.artifacts(a.id):
        Path(row.target).unlink()

    provider.responses.append(provider.parcel("box", state=ResourceState.AVAILABLE, files=[("payload.bin", "payload.bin", 4)]))
    b = await core.engine.submit((request(),), name="logical")
    await settle(core, b.id)

    assert b.id != a.id
    assert [resource.id for resource, _state, _pending in await core.repository.resources(b.id)] == held
    assert (await core.repository.get(b.id)).state == TransferState.COMPLETED
    assert [resource.id for resource, _state, _pending in await core.repository.resources(a.id)] == held
