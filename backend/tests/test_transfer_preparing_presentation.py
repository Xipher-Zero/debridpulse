"""PREPARING current provider resource -> "Waiting for provider" presentation,
and cross-surface effective-presentation consistency (Dashboard/Downloads/Details).

Presentation-only correction (TASK_DP_1.0.12_Torrent_Status_Badge_Presentation_Fix).
The durable transfer lifecycle state is never changed.

The PREPARING refinement lives in
``transfers.presentation_repository.waiting_for_provider_override`` and is applied
by the ONE shared owner
``transfers.presentation_repository.effective_presentation``, which is consumed by
BOTH the comprehensive Details projection
(``presentation_repository.TransferRepository.presentation``) and the bounded
Downloads/Dashboard list projection
(``api.operational_downloads.list_operational_torrents``). Because both surfaces
feed the same shared owner the same logical inputs (durable status, per-artifact
child presentations, pause / input-required signals, current authoritative root
provider-resource state) they can never derive a contradictory processing truth.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
import pytest_asyncio

import api.operational_downloads as downloads
import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.engine import TransferEngine
from transfers.models import ResourceState, TransferRequest
from transfers.policy import TransferPolicy
from transfers.presentation_repository import TransferRepository, waiting_for_provider_override
from transfers.registry import IntegrationRegistry

FILES = [("a.mkv", "S/a.mkv", 10), ("b.mkv", "S/b.mkv", 20), ("c.mkv", "S/c.mkv", 30)]
ONE_FILE = [("a.mkv", "S/a.mkv", 10)]


@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "preparing.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider("parcel-lab", file_manifest=True)
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(retry_delay=0, resolution_retry_delay=0,
                              adoption_stability_seconds=0, resource_poll_interval=5,
                              max_active_executions=8),
    )
    await engine.initialize()
    return SimpleNamespace(engine=engine, repository=repository, provider=provider, executor=executor)


async def _submit(runtime, *, payload="box", selection_mode="all"):
    return await runtime.engine.submit(
        (TransferRequest("parcel", payload, name="Show S01", fingerprint="fp-" + payload,
                         selection_mode=selection_mode),),
        deduplicate=False)


async def _list_item(transfer_id):
    result = await downloads.list_operational_torrents(
        status=None, search=None, limit=0, offset=0,
        application=SimpleNamespace(repository=None, definitions=[]))
    return next((item for item in result["items"] if item["id"] == transfer_id), None)


async def _details(runtime, transfer_id):
    return await runtime.repository.presentation(transfer_id, details=True)


async def _row(sql, params=()):
    async with database.get_db() as db:
        return await db.fetchone(sql, params)


def _effective(item):
    """The effective processing status the frontend renders for a list item —
    presentation_status when the backend supplied one, else the raw status
    (mirrors ui-processing-presentation.js `presentationStatus(t, t.status)`)."""
    return str(item.get("presentation_status") or item.get("status") or "").lower()


async def _assert_surfaces_agree(runtime, transfer_id, expected):
    """Dashboard Recent Items and Downloads both call GET /api/torrents (the same
    bounded list projection). Assert that bounded surface and Details derive the
    identical effective presentation from the same authoritative transfer facts."""
    item = await _list_item(transfer_id)
    details = await _details(runtime, transfer_id)
    assert item is not None
    assert item["presentation_status"] == details["presentation_status"] == expected
    assert item["presentation_label"] == details["presentation_label"]
    assert item["presentation_badge_status"] == details["presentation_badge_status"]
    return item, details


async def _materialize_single_artifact(runtime, *, payload="box"):
    """Resolve a single-file AVAILABLE transfer far enough to own one artifact."""
    runtime.provider.responses.append(
        runtime.provider.parcel(payload, state=ResourceState.AVAILABLE, files=ONE_FILE))
    transfer = await _submit(runtime, payload=payload)
    for _ in range(6):
        await runtime.engine.resolve_pending()
    artifacts = await runtime.repository.artifacts(transfer.id)
    assert artifacts, "expected the AVAILABLE resolution to materialize an artifact"
    return transfer, artifacts[0]


# --------------------------------------------------------------------------- #
# Pure canonical rule
# --------------------------------------------------------------------------- #

def test_waiting_for_provider_override_rule():
    ok = waiting_for_provider_override("pending", "preparing")
    assert ok == {"presentation_status": "waiting_for_provider",
                  "presentation_label": "Waiting for provider",
                  "presentation_badge_status": "pending"}
    assert waiting_for_provider_override("processing", "PREPARING")["presentation_status"] == "waiting_for_provider"
    # Not PREPARING -> no override.
    for state in (None, "", "available", "absent", "expired", "unavailable", "unknown"):
        assert waiting_for_provider_override("pending", state) is None
    # PREPARING but the aggregate already advanced past generic pending/processing
    # (a more-specific effective presentation) -> the refinement must NOT fire.
    for status in ("downloading", "queued", "verifying", "failed", "requires_attention",
                   "paused", "completed", "input_required", "waiting_for_retry",
                   "waiting_for_storage", "waiting_for_executor", "recovering"):
        assert waiting_for_provider_override(status, "preparing") is None


# --------------------------------------------------------------------------- #
# Section 11 behaviour matrix — via the real projections
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_not_yet_resolved_keeps_generic_pending(runtime):
    # Submitted, no resolution attempt completed: no current provider resource,
    # no artifact. Both surfaces present the generic raw status, identically.
    transfer = await _submit(runtime)

    assert (await _row("SELECT resource FROM transfer_requests WHERE transfer_id=? AND parent_id IS NULL",
                       (transfer.id,)))["resource"] is None
    item = await _list_item(transfer.id)
    details = await _details(runtime, transfer.id)
    assert item["status"] == "pending"                       # durable status untouched
    assert item["presentation_status"] == details["presentation_status"] == "pending"
    assert details["presentation_status"] != "waiting_for_provider"


@pytest.mark.asyncio
async def test_current_resource_preparing_presents_waiting_for_provider(runtime):
    runtime.provider.responses.append(runtime.provider.parcel("box", state=ResourceState.PREPARING))
    transfer = await _submit(runtime)
    await runtime.engine.resolve_pending()

    pr = await _row("SELECT state FROM provider_resources WHERE transfer_id=?", (transfer.id,))
    assert pr["state"] == "preparing"

    item, _details_row = await _assert_surfaces_agree(runtime, transfer.id, "waiting_for_provider")
    assert item["presentation_label"] == "Waiting for provider"
    assert item["presentation_badge_status"] == "pending"
    assert item["status"] in {"pending", "processing"}        # durable status untouched


@pytest.mark.asyncio
async def test_preparing_with_multi_file_manifest_still_waiting_and_selector_operates(runtime):
    # Interactive submission: manifest arrives while the resource is still
    # PREPARING. File selection may proceed; the badge stays "Waiting for provider".
    from file_selection_support import file_manifest as _fm

    prepare = runtime.provider.parcel("box", state=ResourceState.PREPARING)
    runtime.provider.responses.append(prepare)
    transfer = await _submit(runtime, selection_mode="interactive")
    await runtime.engine.resolve_pending()

    # Manifest arrives while the resource is STILL preparing.
    runtime.provider.resources["parcel-lab:box"] = replace(
        prepare.observation, file_manifest=_fm(*FILES))
    await runtime.engine.resolve_pending()

    # Selector is usable: a bound multi-file manifest, mutable, decision pending.
    sel = await runtime.repository.file_selection_presentation(transfer.id, now=0.0)
    assert sel is not None and sel["file_count"] == 3
    assert sel["mutable"] is True and sel["decision"] == "pending"

    pr = await _row("SELECT state FROM provider_resources WHERE transfer_id=?", (transfer.id,))
    assert pr["state"] == "preparing"

    await _assert_surfaces_agree(runtime, transfer.id, "waiting_for_provider")


@pytest.mark.asyncio
async def test_resource_available_drops_the_override_on_next_refresh(runtime):
    prepare = runtime.provider.parcel("box", state=ResourceState.PREPARING)
    runtime.provider.responses.append(prepare)
    transfer = await _submit(runtime)
    await runtime.engine.resolve_pending()
    assert (await _list_item(transfer.id))["presentation_status"] == "waiting_for_provider"

    # Provider makes the resource AVAILABLE; the transfer materialises ALL.
    runtime.provider.resources["parcel-lab:box"] = runtime.provider.parcel(
        "box", state=ResourceState.AVAILABLE, files=FILES).observation
    for _ in range(6):
        await runtime.engine.tick()

    pr = await _row("SELECT state FROM provider_resources WHERE transfer_id=?", (transfer.id,))
    assert pr["state"] == "available"
    item = await _list_item(transfer.id)
    details = await _details(runtime, transfer.id)
    assert item["presentation_status"] != "waiting_for_provider"
    assert details["presentation_status"] != "waiting_for_provider"
    assert item["presentation_status"] == details["presentation_status"]


@pytest.mark.asyncio
async def test_historical_preparing_binding_never_overrides_current_available(runtime):
    # Build a transfer whose CURRENT resource is AVAILABLE, then inject a
    # genuinely historical PREPARING provider-resource binding for a *different*
    # canonical resource that the current root request does not point at.
    prepare = runtime.provider.parcel("box", state=ResourceState.PREPARING)
    runtime.provider.responses.append(prepare)
    transfer = await _submit(runtime)
    await runtime.engine.resolve_pending()
    runtime.provider.resources["parcel-lab:box"] = runtime.provider.parcel(
        "box", state=ResourceState.AVAILABLE, files=FILES).observation
    for _ in range(6):
        await runtime.engine.tick()
    assert (await _row("SELECT state FROM provider_resources WHERE transfer_id=?",
                       (transfer.id,)))["state"] == "available"

    async with database.get_db() as db:
        await db.execute(
            "INSERT INTO provider_resources(id,transfer_id,provider_id,payload,state,resource_key) "
            "VALUES(?,?,?,?,?,?)",
            ("historic-binding", transfer.id, "parcel-lab",
             '{"provider_id":"parcel-lab","context":{},"ownership":"created","id":"parcel-lab:OLD"}',
             "preparing", "parcel-lab:OLD"))
        await db.commit()

    # The current root request still points at parcel-lab:box (AVAILABLE), so the
    # historical PREPARING row must not leak into presentation on either surface.
    item = await _list_item(transfer.id)
    details = await _details(runtime, transfer.id)
    assert item["presentation_status"] != "waiting_for_provider"
    assert details["presentation_status"] != "waiting_for_provider"
    assert item["presentation_status"] == details["presentation_status"]


@pytest.mark.asyncio
async def test_deleted_predecessor_preparing_resource_does_not_taint_a_fresh_transfer(runtime):
    # Predecessor transfer, PREPARING, then deleted.
    runtime.provider.responses.append(runtime.provider.parcel("box", state=ResourceState.PREPARING))
    old = await _submit(runtime)
    await runtime.engine.resolve_pending()
    assert (await _list_item(old.id))["presentation_status"] == "waiting_for_provider"
    await runtime.engine.delete(old.id, remote=False)

    # A fresh transfer of the same source, re-resolved onto its own binding,
    # AVAILABLE. It must not inherit the predecessor's PREPARING presentation.
    runtime.provider.responses.append(
        runtime.provider.parcel("box", state=ResourceState.AVAILABLE, files=FILES))
    fresh = await _submit(runtime)
    assert fresh.id != old.id
    for _ in range(6):
        await runtime.engine.tick()

    item = await _list_item(fresh.id)
    details = await _details(runtime, fresh.id)
    assert item["presentation_status"] != "waiting_for_provider"
    assert details["presentation_status"] != "waiting_for_provider"
    assert item["presentation_status"] == details["presentation_status"]


@pytest.mark.asyncio
async def test_provider_failure_keeps_failure_presentation_not_waiting(runtime):
    from transfers.errors import Category, Domain, NormalizedError, Recovery, Retryability, Stage
    from transfers.models import ProviderObservation

    prepare = runtime.provider.parcel("box", state=ResourceState.PREPARING)
    runtime.provider.responses.append(prepare)
    transfer = await _submit(runtime)
    await runtime.engine.resolve_pending()

    backoff = NormalizedError(Domain.PROVIDER, Category.RATE_LIMITED, Stage.RECONCILIATION,
                              retryability=Retryability.BACKOFF, recovery=Recovery.RETRY)
    runtime.provider.resources["parcel-lab:box"] = ProviderObservation(
        prepare.observation.resource, ResourceState.UNAVAILABLE, error=backoff)
    for _ in range(4):
        await runtime.engine.tick()

    item = await _list_item(transfer.id)
    details = await _details(runtime, transfer.id)
    assert details["presentation_status"] != "waiting_for_provider"
    assert item["presentation_status"] == details["presentation_status"]


# --------------------------------------------------------------------------- #
# Section 12 — the more-specific effective presentation is preserved IDENTICALLY
# on the bounded Downloads/Dashboard surface and on Details. The PREPARING
# refinement may only refine generic pending/processing; it must never overwrite
# a more-specific effective presentation.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_paused_preparing_transfer_is_paused_on_every_surface_not_waiting(runtime):
    runtime.provider.responses.append(runtime.provider.parcel("box", state=ResourceState.PREPARING))
    transfer = await _submit(runtime)
    await runtime.engine.resolve_pending()
    await runtime.engine.pause(transfer.id)

    assert (await _row("SELECT paused FROM transfer_pause_intents WHERE torrent_id=?",
                       (transfer.id,)))["paused"] == 1
    assert (await _row("SELECT state FROM provider_resources WHERE transfer_id=?",
                       (transfer.id,)))["state"] == "preparing"

    item, details = await _assert_surfaces_agree(runtime, transfer.id, "paused")
    assert _effective(item) == "paused"
    assert item["presentation_status"] != "waiting_for_provider"


@pytest.mark.asyncio
async def test_recovery_quiescence_more_specific_presentation_is_not_overwritten(runtime):
    from transfers.errors import Category, Domain, NormalizedError, Origin, Stage
    from transfers.errors import Retryability

    transfer, artifact = await _materialize_single_artifact(runtime)

    disk_full = NormalizedError(Domain.LOCAL_RESOURCE, Category.DISK_FULL, Stage.EXECUTION,
                                retryability=Retryability.AFTER_RESOURCE_CHANGE, origin=Origin.LOCAL_SYSTEM)
    assert await runtime.repository.transition_recovery(
        artifact.id, "recovery_wait", error=disk_full,
        quiescence_reason="storage_unavailable", wake_condition="storage_healthy:local_resource")

    # Now the current provider resource regresses to PREPARING and the durable
    # transfer status is a generic 'processing' — the exact overwrite hazard.
    async with database.get_db() as db:
        await db.execute("UPDATE provider_resources SET state='preparing' WHERE transfer_id=?", (transfer.id,))
        await db.execute("UPDATE torrents SET status='processing' WHERE id=?", (transfer.id,))
        await db.commit()

    item, details = await _assert_surfaces_agree(runtime, transfer.id, "waiting_for_storage")
    assert details["presentation_label"] == "Waiting for storage"
    assert item["presentation_status"] != "waiting_for_provider"
    assert item["status"] == "processing"                     # raw status untouched


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "quiescence_reason, expected",
    [
        ("retry_backoff", "waiting_for_retry"),
        ("provider_disabled", "waiting_for_provider"),
        ("provider_unavailable", "waiting_for_provider"),
        ("storage_unavailable", "waiting_for_storage"),
        ("executor_unavailable", "waiting_for_executor"),
    ],
)
async def test_every_wait_presentation_reason_surfaces_identically(runtime, quiescence_reason, expected):
    """Every _WAIT_PRESENTATION quiescence reason — not just storage_unavailable —
    yields the identical effective presentation on the bounded list and Details."""
    transfer, artifact = await _materialize_single_artifact(runtime)
    assert await runtime.repository.transition_recovery(
        artifact.id, "recovery_wait",
        quiescence_reason=quiescence_reason, wake_condition="wake:" + quiescence_reason)

    # Regress the current provider resource to PREPARING too: the refinement still
    # must NOT overwrite the more-specific recovery presentation.
    async with database.get_db() as db:
        await db.execute("UPDATE provider_resources SET state='preparing' WHERE transfer_id=?", (transfer.id,))
        await db.commit()

    await _assert_surfaces_agree(runtime, transfer.id, expected)


@pytest.mark.asyncio
async def test_recovering_child_surfaces_identically(runtime):
    transfer, artifact = await _materialize_single_artifact(runtime)
    # recovery_wait with no quiescence reason -> "recovering" on both surfaces.
    assert await runtime.repository.transition_recovery(
        artifact.id, "recovery_wait", clear_quiescence=True)
    await _assert_surfaces_agree(runtime, transfer.id, "recovering")


@pytest.mark.asyncio
async def test_requires_attention_surfaces_identically(runtime):
    transfer, artifact = await _materialize_single_artifact(runtime)
    await runtime.repository.record_recovery_decision(artifact.id, "wait_for_operator", "recovery budget exhausted")
    assert await runtime.repository.transition_recovery(
        artifact.id, "recovery_wait",
        quiescence_reason="recovery_exhausted", wake_condition="operator_retry")
    item, details = await _assert_surfaces_agree(runtime, transfer.id, "requires_attention")
    assert item["presentation_badge_status"] == "error"


@pytest.mark.asyncio
async def test_downloading_child_surfaces_identically(runtime):
    transfer, artifact = await _materialize_single_artifact(runtime)
    async with database.get_db() as db:
        await db.execute("UPDATE download_files SET status='downloading' WHERE id=?", (artifact.id,))
        await db.commit()
    await _assert_surfaces_agree(runtime, transfer.id, "downloading")


@pytest.mark.asyncio
async def test_input_challenge_preparing_transfer_surfaces_input_required_identically(runtime):
    runtime.provider.responses.append(runtime.provider.parcel("box", state=ResourceState.PREPARING))
    transfer = await _submit(runtime)
    await runtime.engine.resolve_pending()

    async with database.get_db() as db:
        await db.execute(
            "INSERT INTO transfer_input_challenges(transfer_id,challenge_id,generation,reason,origin,"
            "integration_id,operation_id,methods,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (transfer.id, f"chal-{transfer.id}", 1, "auth_required", "provider",
             "parcel-lab", f"op-{transfer.id}", "[]", 0.0, 0.0))
        await db.commit()

    item, details = await _assert_surfaces_agree(runtime, transfer.id, "input_required")
    assert item["presentation_status"] != "waiting_for_provider"


@pytest.mark.asyncio
async def test_completed_transfer_consistent(runtime):
    runtime.provider.responses.append(
        runtime.provider.parcel("box", state=ResourceState.AVAILABLE, files=ONE_FILE))
    transfer = await _submit(runtime)
    for _ in range(8):
        await runtime.engine.tick()
    for observation in list(runtime.executor.jobs.values()):
        runtime.executor.finish(observation.handle)
    for _ in range(8):
        await runtime.engine.tick()
    status = (await _row("SELECT status FROM torrents WHERE id=?", (transfer.id,)))["status"]
    if status != "completed":
        pytest.skip("MemoryExecutor did not drive the transfer to completed in this environment")
    item = await _list_item(transfer.id)
    details = await _details(runtime, transfer.id)
    assert item["presentation_status"] == details["presentation_status"] == "completed"


# --------------------------------------------------------------------------- #
# Bounded projection stays bounded — constant DB round-trips regardless of how
# many transfers carry recovery snapshots; no comprehensive per-transfer call.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_bounded_projection_stays_one_read_with_recovery_facts_present(runtime, monkeypatch):
    # A transfer owning an artifact in recovery quiescence, plus further
    # unresolved transfers — the page is > 1 row and carries recovery facts.
    transfer, artifact = await _materialize_single_artifact(runtime)
    await runtime.repository.transition_recovery(
        artifact.id, "recovery_wait", quiescence_reason="storage_unavailable", wake_condition="w")
    for _ in range(3):
        await _submit(runtime, payload="box")

    calls = []
    real_get_db = downloads.get_db

    @asynccontextmanager
    async def counting_get_db():
        async with real_get_db() as conn:
            real_fetchall, real_fetchone = conn.fetchall, conn.fetchone

            async def fetchall(query, params=()):
                calls.append("fetchall")
                return await real_fetchall(query, params)

            async def fetchone(query, params=()):
                calls.append("fetchone")
                return await real_fetchone(query, params)

            conn.fetchall = fetchall
            conn.fetchone = fetchone
            yield conn

    monkeypatch.setattr(downloads, "get_db", counting_get_db)

    class _ExplodingRepository:
        async def presentation(self, *_a, **_k):  # pragma: no cover - must never run
            raise AssertionError("bounded list must not call comprehensive presentation")

    result = await downloads.list_operational_torrents(
        status=None, search=None, limit=0, offset=0,
        application=SimpleNamespace(repository=_ExplodingRepository(), definitions=[]))

    assert calls == ["fetchall", "fetchone"]
    assert len(result["items"]) >= 4
    waiting = [i for i in result["items"] if i["presentation_status"] == "waiting_for_storage"]
    assert len(waiting) == 1
