"""Additional Roadmap Item 9 provenance invariants found during final scope audit."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import db.database as database
from providers.general_http.provider import GeneralHttpProvider
from services.stats import collect_all_metrics
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage
from transfers.models import (
    Endpoint,
    ExecutionHandle,
    ExecutionObservation,
    ExecutionState,
    OutcomeKind,
    ResolutionResult,
    ResourceState,
    TransferCandidate,
    TransferOutcome,
    TransferProgress,
    TransferRequest,
)
from transfers.repository import TransferRepository

pytestmark = pytest.mark.asyncio


async def _repository(tmp_path, monkeypatch, name):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / name)
    await database.init_db()
    repository = TransferRepository()
    await repository.initialize()
    return repository


async def _admit(repository, request):
    transfer, created = await repository.admit((request,), name=request.name or "fixture", deduplicate=False)
    assert created
    return transfer, (await repository.requests(transfer.id))[0]


def _candidate(provider_id, identity):
    return TransferCandidate(
        name=f"{identity}.bin",
        endpoints=(Endpoint("https", f"https://download.example/{identity}"),),
        expected_bytes=8,
        provider_id=provider_id,
        id=identity,
    )


async def _resolve(repository, record, provider_id, candidates=(), *, error=None):
    attempt = await repository.begin_resolution(record.id, provider_id)
    assert attempt is not None
    result = ResolutionResult(ResourceState.UNKNOWN if error else ResourceState.AVAILABLE, tuple(candidates), error=error)
    await repository.resolution(attempt, result)
    return attempt


async def _complete_execution(repository, record, candidate, attempt_id):
    artifact = await repository.materialize(record, (candidate,), f"/tmp/{candidate.name}")
    assert artifact is not None
    handle = ExecutionHandle("fixture_executor", {}, attempt_id=attempt_id)
    assert await repository.prepare_execution(artifact, handle)
    await repository.execution(ExecutionObservation(handle, ExecutionState.SUCCEEDED, TransferProgress(8, 8)))
    await repository.artifact_state(artifact.id, "completed", expected_bytes=8)
    return artifact, handle


async def _force_completed(transfer_id):
    async with database.get_db() as db:
        await db.execute("UPDATE torrents SET status='completed',progress=100,completed_at=CURRENT_TIMESTAMP WHERE id=?", (transfer_id,))
        await db.commit()


async def test_route_attempt_ordinals_are_transfer_wide_across_multiple_requests(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "transfer-order.sqlite3")
    requests = (
        TransferRequest("https", "https://one.example/file", name="one.bin"),
        TransferRequest("https", "https://two.example/file", name="two.bin"),
    )
    transfer, created = await repository.admit(requests, name="multi", deduplicate=False)
    assert created
    first, second = await repository.requests(transfer.id)
    route_a = await repository.begin_resolution(first.id, "provider_a")
    route_b = await repository.begin_resolution(second.id, "provider_b")
    assert route_a is not None and route_b is not None
    await repository.resolution(route_a, ResolutionResult(ResourceState.AVAILABLE, (_candidate("provider_a", "one"),)))
    await repository.resolution(route_b, ResolutionResult(ResourceState.AVAILABLE, (_candidate("provider_b", "two"),)))

    presentation = await repository.presentation(transfer.id, details=True)
    assert [item["id"] for item in presentation["route_attempts"]] == [route_a.id, route_b.id]
    assert [item["ordinal"] for item in presentation["route_attempts"]] == [1, 2]
    assert len({item["request_id"] for item in presentation["route_attempts"]}) == 2


async def test_current_alldebrid_host_state_cannot_rewrite_completed_direct_history(tmp_path, monkeypatch):
    from integrations.runtime_state import ProviderRuntimeStateStore
    from providers.alldebrid.host_runtime import HOST_SCHEMA_VERSION, HOST_STATE_KEY, encode_host_snapshot, parse_native_host_snapshot

    repository = await _repository(tmp_path, monkeypatch, "host-history.sqlite3")
    provider = GeneralHttpProvider()
    request = TransferRequest("https", "https://host-change.example/file.bin", name="file.bin")
    transfer, record = await _admit(repository, request)
    route = await repository.begin_resolution(record.id, provider.descriptor.id)
    assert route is not None
    result = await provider.resolve(request)
    await repository.resolution(route, result)
    record = (await repository.requests(transfer.id))[0]
    await _complete_execution(repository, record, result.candidates[0], "direct-before-host-change")
    await _force_completed(transfer.id)
    before = await repository.presentation(transfer.id, details=True)
    assert before["delivering_provider_id"] == "general_http"

    snapshot = parse_native_host_snapshot({
        "hosts": {
            "changed": {
                "name": "changed",
                "type": "premium",
                "domains": ["host-change.example"],
                "regexps": [r"https?://host-change\.example/.+"],
                "status": True,
            }
        }
    })
    store = ProviderRuntimeStateStore()
    await store.replace(
        "alldebrid",
        encode_host_snapshot(snapshot),
        schema_version=HOST_SCHEMA_VERSION,
        state_key=HOST_STATE_KEY,
        observed_at=1000.0,
        successful_at=1000.0,
        stale_after=2000.0,
    )
    after = await repository.presentation(transfer.id, details=True)
    assert after["delivering_provider_id"] == "general_http"
    assert after["providers"] == ["general_http"]
    assert after["route_attempts"][0]["provider_id"] == "general_http"


async def test_failed_provider_attempt_does_not_increment_failed_logical_transfer_statistics(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "stats.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://stats.example/file", name="stats.bin"))
    failure = NormalizedError(Domain.PROVIDER, Category.PROVIDER_UNAVAILABLE, Stage.RESOLUTION, retryability=Retryability.NEVER)
    await _resolve(repository, record, "provider_a", error=failure)
    await repository.retry_requests(transfer.id, request_id=record.id)
    record = (await repository.requests(transfer.id))[0]
    candidate_b = _candidate("provider_b", "stats-candidate")
    await _resolve(repository, record, "provider_b", (candidate_b,))
    record = (await repository.requests(transfer.id))[0]
    await _complete_execution(repository, record, candidate_b, "stats-execution")
    await _force_completed(transfer.id)

    metrics = await collect_all_metrics()
    assert metrics["torrents"]["total"] == 1
    assert metrics["torrents"]["completed"] == 1
    assert metrics["torrents"]["errors"] == 0
    presentation = await repository.presentation(transfer.id, details=True)
    assert [item["outcome"] for item in presentation["route_attempts"]] == ["failed", "completed"]


async def test_provenance_schema_is_provider_neutral_and_identity_is_separate(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "architecture.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://schema.example/file", name="schema.bin"))
    candidate = _candidate("arbitrary_provider_name", "schema-candidate")
    route = await _resolve(repository, record, "arbitrary_provider_name", (candidate,))
    record = (await repository.requests(transfer.id))[0]
    _, execution = await _complete_execution(repository, record, candidate, "schema-execution")

    async with database.get_db() as db:
        table_rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        route_row = await db.fetchone(
            "SELECT transfer_id,resolution_attempt_id FROM route_attempt_provenance WHERE resolution_attempt_id=?", (route.id,)
        )
        execution_row = await db.fetchone(
            "SELECT transfer_id,execution_attempt_id,route_attempt_id FROM execution_attempt_provenance WHERE execution_attempt_id=?",
            (execution.attempt_id,),
        )
    names = {row["name"] for row in table_rows}
    assert "route_attempt_provenance" in names
    assert "execution_attempt_provenance" in names
    assert not any("alldebrid" in name or "general_http" in name for name in names if "provenance" in name)
    assert route_row["transfer_id"] == transfer.id
    assert str(route_row["resolution_attempt_id"]) != str(transfer.id)
    assert execution_row["transfer_id"] == transfer.id
    assert execution_row["execution_attempt_id"] != route.id
    assert execution_row["route_attempt_id"] == route.id


async def test_executor_success_is_not_delivery_until_artifact_verification_completes(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "delivery-boundary.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://delivery.example/file", name="delivery.bin"))
    candidate = _candidate("provider_a", "delivery-candidate")
    route = await _resolve(repository, record, "provider_a", (candidate,))
    record = (await repository.requests(transfer.id))[0]
    artifact = await repository.materialize(record, (candidate,), "/tmp/delivery.bin")
    handle = ExecutionHandle("fixture_executor", {}, attempt_id="delivery-execution")
    assert await repository.prepare_execution(artifact, handle)
    await repository.execution(ExecutionObservation(handle, ExecutionState.SUCCEEDED, TransferProgress(8, 8)))

    before = await repository.presentation(transfer.id, details=True)
    assert before["delivering_provider_id"] is None
    assert before["execution_attempts"][0]["outcome"] == "succeeded"
    assert before["execution_attempts"][0]["delivered"] is False
    assert before["route_attempts"][0]["id"] == route.id
    assert before["route_attempts"][0]["outcome"] == "resolved"

    await repository.artifact_state(artifact.id, "completed", expected_bytes=8)
    after = await repository.presentation(transfer.id, details=True)
    assert after["delivering_provider_id"] == "provider_a"
    assert after["execution_attempts"][0]["outcome"] == "completed"
    assert after["execution_attempts"][0]["delivered"] is True
    assert after["route_attempts"][0]["outcome"] == "completed"


async def test_postprocessing_failure_does_not_rewrite_successful_acquisition_provider(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "postprocess.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://post.example/file", name="post.bin"))
    candidate = _candidate("provider_a", "post-candidate")
    route = await _resolve(repository, record, "provider_a", (candidate,))
    record = (await repository.requests(transfer.id))[0]
    await _complete_execution(repository, record, candidate, "post-execution")

    before = await repository.presentation(transfer.id, details=True)
    assert before["delivering_provider_id"] == "provider_a"
    assert before["route_attempts"][0]["outcome"] == "completed"

    processor = SimpleNamespace(descriptor=SimpleNamespace(id="fixture_postprocessor"))
    await repository.queue_postprocessing(transfer.id, (processor,), ("/tmp/post.bin",))
    error = NormalizedError(Domain.POST_PROCESSING, Category.EXTRACTION_FAILED, Stage.POST_PROCESSING)
    await repository.finish_postprocessing(transfer.id, "fixture_postprocessor", TransferOutcome(OutcomeKind.FAILURE, error=error))

    after = await repository.presentation(transfer.id, details=True)
    assert after["delivering_provider_id"] == "provider_a"
    assert after["route_attempts"][0]["id"] == route.id
    assert after["route_attempts"][0]["outcome"] == "completed"
    assert after["execution_attempts"][0]["delivered"] is True


async def test_cancelled_executor_attempt_is_historical_not_provider_failover(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "cancel.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://cancel.example/file", name="cancel.bin"))
    candidate = _candidate("provider_a", "cancel-candidate")
    route = await _resolve(repository, record, "provider_a", (candidate,))
    record = (await repository.requests(transfer.id))[0]
    artifact = await repository.materialize(record, (candidate,), "/tmp/cancel.bin")

    first = ExecutionHandle("fixture_executor", {}, attempt_id="cancel-exec-1")
    assert await repository.prepare_execution(artifact, first)
    await repository.execution(ExecutionObservation(first, ExecutionState.CANCELLED, TransferProgress(8, 2)))
    cancelled = await repository.presentation(transfer.id, details=True)
    assert len(cancelled["route_attempts"]) == 1
    assert cancelled["route_attempts"][0]["id"] == route.id
    assert cancelled["execution_attempts"][0]["outcome"] == "cancelled"
    assert cancelled["execution_attempts"][0]["delivered"] is False
    assert cancelled["delivering_provider_id"] is None

    await repository.artifact_state(artifact.id, "queued", release=True)
    artifact = (await repository.artifacts(transfer.id))[0]
    second = ExecutionHandle("fixture_executor", {}, attempt_id="cancel-exec-2")
    assert await repository.prepare_execution(artifact, second)
    await repository.execution(ExecutionObservation(second, ExecutionState.SUCCEEDED, TransferProgress(8, 8)))
    await repository.artifact_state(artifact.id, "completed", expected_bytes=8)
    completed = await repository.presentation(transfer.id, details=True)
    assert len(completed["route_attempts"]) == 1
    assert [item["outcome"] for item in completed["execution_attempts"]] == ["cancelled", "completed"]
    assert completed["delivering_provider_id"] == "provider_a"


async def test_restart_reconciliation_observation_preserves_existing_route_linkage(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "reconcile.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://reconcile.example/file", name="reconcile.bin"))
    candidate = _candidate("provider_a", "reconcile-candidate")
    route = await _resolve(repository, record, "provider_a", (candidate,))
    record = (await repository.requests(transfer.id))[0]
    artifact = await repository.materialize(record, (candidate,), "/tmp/reconcile.bin")
    handle = ExecutionHandle("fixture_executor", {"gid": "durable-handle"}, attempt_id="reconcile-exec")
    assert await repository.prepare_execution(artifact, handle)

    restarted = TransferRepository()
    await restarted.initialize()
    live = await restarted.live_executions()
    assert len(live) == 1
    assert live[0].handle.attempt_id == handle.attempt_id
    await restarted.execution(ExecutionObservation(live[0].handle, ExecutionState.TRANSFERRING, TransferProgress(8, 4)))
    mid = await restarted.presentation(transfer.id, details=True)
    assert len(mid["route_attempts"]) == 1
    assert mid["execution_attempts"][0]["route_attempt_id"] == route.id
    assert mid["execution_attempts"][0]["provider_id"] == "provider_a"

    await restarted.execution(ExecutionObservation(live[0].handle, ExecutionState.SUCCEEDED, TransferProgress(8, 8)))
    await restarted.artifact_state(artifact.id, "completed", expected_bytes=8)
    completed = await restarted.presentation(transfer.id, details=True)
    assert completed["delivering_provider_id"] == "provider_a"
    assert completed["execution_attempts"][0]["route_attempt_id"] == route.id
    assert completed["execution_attempts"][0]["delivered"] is True
