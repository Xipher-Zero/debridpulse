"""Adversarial ROUTE-001 + CORE-001 contract tests."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

import db.database as database
from fake_integrations import ParcelProvider
from transfers.engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Recovery, Retryability, Stage, TransferError
from transfers.models import ResolutionResult, ResourceState, TransferRequest
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository

pytestmark = pytest.mark.asyncio


async def core(tmp_path, monkeypatch, name="group-a.sqlite3"):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / name)
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    first = ParcelProvider("provider_a")
    second = ParcelProvider("provider_b")
    registry.register_provider(first)
    registry.register_provider(second)
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "downloads"),
        policy=TransferPolicy(resolution_max_attempts=4, resolution_retry_delay=0, adoption_stability_seconds=0),
        clock=lambda: 1000.0,
    )
    await engine.initialize()
    return SimpleNamespace(repository=repository, registry=registry, first=first, second=second, engine=engine)


def retryable_failure():
    return NormalizedError(
        Domain.PROVIDER, Category.PROVIDER_UNAVAILABLE, Stage.RESOLUTION,
        retryability=Retryability.IMMEDIATE, recovery=Recovery.RETRY,
    )


async def test_retry_stays_bound_when_fresh_selection_would_choose_other_provider(tmp_path, monkeypatch):
    c = await core(tmp_path, monkeypatch)
    c.first.responses = [ResolutionResult(ResourceState.UNKNOWN, error=retryable_failure())]
    transfer = await c.engine.submit((TransferRequest("parcel", "box", name="payload.bin"),), deduplicate=False)
    await c.engine.resolve_pending()
    assert [call for call in c.first.calls if call[0] == "resolve"]
    assert not [call for call in c.second.calls if call[0] == "resolve"]

    c.first.descriptor = replace(c.first.descriptor, enabled=False)
    await c.engine.resolve_pending()
    assert not [call for call in c.second.calls if call[0] == "resolve"]
    details = await c.repository.presentation(transfer.id, details=True)
    assert {item["provider_id"] for item in details["route_attempts"]} == {"provider_a"}


async def test_health_drift_cannot_reopen_global_selection(tmp_path, monkeypatch):
    c = await core(tmp_path, monkeypatch, "health.sqlite3")
    c.first.responses = [ResolutionResult(ResourceState.UNKNOWN, error=retryable_failure())]
    transfer = await c.engine.submit((TransferRequest("parcel", "box", name="payload.bin"),), deduplicate=False)
    await c.engine.resolve_pending()
    c.registry.mark_health("provider_a", healthy=False)
    await c.engine.resolve_pending()
    assert not [call for call in c.second.calls if call[0] == "resolve"]
    assert (await c.repository.presentation(transfer.id, details=True))["current_provider_id"] == "provider_a"


async def test_restart_recovers_bound_provider_without_reclassification(tmp_path, monkeypatch):
    c = await core(tmp_path, monkeypatch, "restart.sqlite3")
    c.first.responses = [ResolutionResult(ResourceState.UNKNOWN, error=retryable_failure())]
    transfer = await c.engine.submit((TransferRequest("parcel", "box", name="payload.bin"),), deduplicate=False)
    await c.engine.resolve_pending()

    c.first.descriptor = replace(c.first.descriptor, priority=0)
    c.second.descriptor = replace(c.second.descriptor, priority=100)
    restarted = TransferEngine(
        TransferRepository(), c.registry, download_root=c.engine.root, policy=c.engine.policy, clock=lambda: 1000.0,
    )
    await restarted.initialize()
    before_b = len([call for call in c.second.calls if call[0] == "resolve"])
    await restarted.resolve_pending()
    assert len([call for call in c.second.calls if call[0] == "resolve"]) == before_b
    details = await c.repository.presentation(transfer.id, details=True)
    assert {item["provider_id"] for item in details["route_attempts"]} == {"provider_a"}


@pytest.mark.parametrize("claimed_identity", ["provider_b", "general_http", "alldebrid"])
async def test_adapter_cannot_forge_selected_provider_identity(tmp_path, monkeypatch, claimed_identity):
    c = await core(tmp_path, monkeypatch, f"forge-{claimed_identity}.sqlite3")
    forged = replace(c.first.candidate("payload.bin"), provider_id=claimed_identity)
    c.first.responses = [ResolutionResult(ResourceState.AVAILABLE, (forged,))]
    transfer = await c.engine.submit((TransferRequest("parcel", "box", name="payload.bin"),), deduplicate=False)
    await c.engine.resolve_pending()
    assert not await c.repository.artifacts(transfer.id)
    details = await c.repository.presentation(transfer.id, details=True)
    assert details["route_attempts"][0]["provider_id"] == "provider_a"
    assert details["route_attempts"][0]["outcome"] == "failed"
    assert details["route_attempts"][0]["candidates"] == []


async def test_core_stamps_provider_neutral_candidate_with_selected_route(tmp_path, monkeypatch):
    c = await core(tmp_path, monkeypatch, "stamp.sqlite3")
    neutral = replace(c.first.candidate("payload.bin"), provider_id="")
    c.first.responses = [ResolutionResult(ResourceState.AVAILABLE, (neutral,))]
    transfer = await c.engine.submit((TransferRequest("parcel", "box", name="payload.bin"),), deduplicate=False)
    await c.engine.resolve_pending()
    artifacts = await c.repository.artifacts(transfer.id)
    assert artifacts and artifacts[0].candidates[0].provider_id == "provider_a"
    details = await c.repository.presentation(transfer.id, details=True)
    assert details["route_attempts"][0]["candidates"][0]["provider_id"] == "provider_a"


async def test_repository_rejects_contradictory_provider_before_persistence(tmp_path, monkeypatch):
    c = await core(tmp_path, monkeypatch, "repository-guard.sqlite3")
    transfer = await c.engine.submit((TransferRequest("parcel", "box", name="payload.bin"),), deduplicate=False)
    record = (await c.repository.requests(transfer.id))[0]
    attempt = await c.repository.begin_resolution(record.id, "provider_a")
    forged = replace(c.first.candidate("payload.bin"), provider_id="provider_b")
    with pytest.raises(TransferError):
        await c.repository.resolution(attempt, ResolutionResult(ResourceState.AVAILABLE, (forged,)))
    details = await c.repository.presentation(transfer.id, details=True)
    assert details["route_attempts"][0]["candidates"] == []
