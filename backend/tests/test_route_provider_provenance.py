"""Roadmap Item 9 durable route/provider provenance acceptance tests."""
from __future__ import annotations

from dataclasses import replace

import pytest

import db.database as database
from providers.alldebrid.provider import AllDebridProvider
from providers.general_http.provider import GeneralHttpProvider
from transfers import codec
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage
from transfers.models import (
    Endpoint,
    ExecutionHandle,
    ExecutionObservation,
    ExecutionState,
    ResolutionResult,
    ResourceState,
    TransferCandidate,
    TransferProgress,
    TransferRequest,
)
from transfers.repository import TransferRepository

pytestmark = pytest.mark.asyncio


async def _repository(tmp_path, monkeypatch, name="provenance.sqlite3"):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / name)
    await database.init_db()
    repository = TransferRepository()
    await repository.initialize()
    return repository


async def _admit(repository, request):
    transfer, created = await repository.admit((request,), name=request.name or "fixture", deduplicate=False)
    assert created
    return transfer, (await repository.requests(transfer.id))[0]


def _candidate(provider_id, identity, *, secret=""):
    address = f"https://download.example/{identity}"
    if secret:
        address += f"?token={secret}"
    return TransferCandidate(
        name=f"{identity}.bin",
        endpoints=(Endpoint("https", address),),
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


async def _materialize_and_execute(repository, record, candidate, *, attempt_id="exec-1", succeed=True):
    artifact = await repository.materialize(record, (candidate,), f"/tmp/{candidate.name}")
    assert artifact is not None
    handle = ExecutionHandle("fixture_executor", {}, attempt_id=attempt_id)
    assert await repository.prepare_execution(artifact, handle)
    observation = ExecutionObservation(
        handle,
        ExecutionState.SUCCEEDED if succeed else ExecutionState.FAILED,
        TransferProgress(total_bytes=8, completed_bytes=8 if succeed else 2),
        error=None if succeed else NormalizedError(Domain.EXECUTOR, Category.TRANSFER_FAILED, Stage.EXECUTION),
    )
    await repository.execution(observation)
    if succeed:
        await repository.artifact_state(artifact.id, "completed", expected_bytes=8)
    else:
        await repository.artifact_state(artifact.id, "error", error=observation.error)
    return artifact, handle


async def _force_completed(transfer_id):
    async with database.get_db() as db:
        await db.execute("UPDATE torrents SET status='completed',progress=100,completed_at=CURRENT_TIMESTAMP WHERE id=?", (transfer_id,))
        await db.commit()


async def test_provider_a_failure_provider_b_delivery_is_append_only_and_restart_durable(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch)
    transfer, record = await _admit(repository, TransferRequest("https", "https://shared.example/file", name="route.bin"))
    failed = NormalizedError(Domain.PROVIDER, Category.PROVIDER_UNAVAILABLE, Stage.RESOLUTION, retryability=Retryability.NEVER)
    attempt_a = await _resolve(repository, record, "provider_a", error=failed)
    await repository.retry_requests(transfer.id, request_id=record.id)
    record = (await repository.requests(transfer.id))[0]
    candidate_b = _candidate("provider_b", "candidate-b", secret="signed-secret-sentinel")
    attempt_b = await _resolve(repository, record, "provider_b", (candidate_b,))
    record = (await repository.requests(transfer.id))[0]
    _, execution_b = await _materialize_and_execute(repository, record, candidate_b, attempt_id="execution-b")
    await _force_completed(transfer.id)

    presentation = await repository.presentation(transfer.id, details=True)
    assert presentation["delivering_provider_id"] == "provider_b"
    assert presentation["current_provider_id"] == "provider_b"
    assert presentation["providers"] == ["provider_b"]
    assert presentation["historical_providers"] == ["provider_a", "provider_b"]
    assert [item["id"] for item in presentation["route_attempts"]] == [attempt_a.id, attempt_b.id]
    assert presentation["route_attempts"][0]["outcome"] == "failed"
    assert presentation["route_attempts"][1]["outcome"] == "completed"
    assert presentation["route_attempts"][1]["previous_attempt_id"] == attempt_a.id
    assert presentation["route_attempts"][1]["transition_kind"] == "provider_change"
    assert presentation["execution_attempts"][0]["id"] == execution_b.attempt_id
    assert presentation["execution_attempts"][0]["route_attempt_id"] == attempt_b.id
    assert presentation["execution_attempts"][0]["provider_id"] == "provider_b"
    assert presentation["execution_attempts"][0]["candidate_id"] == candidate_b.id
    assert presentation["execution_attempts"][0]["delivered"] is True

    serialized = codec.dump({"routes": presentation["route_attempts"], "executions": presentation["execution_attempts"]})
    assert "signed-secret-sentinel" not in serialized
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT candidate_summary FROM route_attempt_provenance WHERE transfer_id=?", (transfer.id,))
        executions = await db.fetchall("SELECT candidate_source FROM execution_attempt_provenance WHERE transfer_id=?", (transfer.id,))
    assert "signed-secret-sentinel" not in codec.dump({"routes": rows, "executions": executions})

    restarted = TransferRepository()
    await restarted.initialize()
    after_restart = await restarted.presentation(transfer.id, details=True)
    assert after_restart["delivering_provider_id"] == "provider_b"
    assert [item["id"] for item in after_restart["route_attempts"]] == [attempt_a.id, attempt_b.id]
    assert after_restart["route_attempts"][0]["outcome"] == "failed"


async def test_candidate_change_within_provider_is_not_provider_failover(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "candidate.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://example.test/file", name="candidate.bin"))
    first = _candidate("provider_a", "candidate-1")
    second = _candidate("provider_a", "candidate-2")
    route = await _resolve(repository, record, "provider_a", (first, second))
    record = (await repository.requests(transfer.id))[0]
    artifact = await repository.materialize(record, (first, second), "/tmp/candidate.bin")

    handle1 = ExecutionHandle("fixture_executor", {}, attempt_id="candidate-exec-1")
    assert await repository.prepare_execution(artifact, handle1)
    error = NormalizedError(Domain.EXECUTOR, Category.TRANSFER_FAILED, Stage.EXECUTION)
    await repository.execution(ExecutionObservation(handle1, ExecutionState.FAILED, error=error))
    await repository.artifact_state(artifact.id, "queued", release=True, selected=1, expected_bytes=8)
    artifact = (await repository.artifacts(transfer.id))[0]
    handle2 = ExecutionHandle("fixture_executor", {}, attempt_id="candidate-exec-2")
    assert await repository.prepare_execution(artifact, handle2)
    await repository.execution(ExecutionObservation(handle2, ExecutionState.SUCCEEDED, TransferProgress(8, 8)))
    await repository.artifact_state(artifact.id, "completed", expected_bytes=8)

    presentation = await repository.presentation(transfer.id, details=True)
    assert len(presentation["route_attempts"]) == 1
    assert presentation["route_attempts"][0]["id"] == route.id
    history = presentation["execution_attempts"]
    assert [item["candidate_id"] for item in history] == [first.id, second.id]
    assert {item["route_attempt_id"] for item in history} == {route.id}
    assert {item["provider_id"] for item in history} == {"provider_a"}
    assert history[0]["outcome"] == "failed"
    assert history[1]["delivered"] is True


async def test_executor_retry_keeps_same_provider_candidate_route(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "executor-retry.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://example.test/retry", name="retry.bin"))
    candidate = _candidate("provider_a", "same-candidate")
    route = await _resolve(repository, record, "provider_a", (candidate,))
    record = (await repository.requests(transfer.id))[0]
    artifact = await repository.materialize(record, (candidate,), "/tmp/retry.bin")

    first = ExecutionHandle("fixture_executor", {}, attempt_id="retry-exec-1")
    assert await repository.prepare_execution(artifact, first)
    error = NormalizedError(Domain.EXECUTOR, Category.TRANSFER_FAILED, Stage.EXECUTION)
    await repository.execution(ExecutionObservation(first, ExecutionState.FAILED, error=error))
    await repository.artifact_state(artifact.id, "queued", release=True)
    artifact = (await repository.artifacts(transfer.id))[0]
    second = ExecutionHandle("fixture_executor", {}, attempt_id="retry-exec-2")
    assert await repository.prepare_execution(artifact, second)
    await repository.execution(ExecutionObservation(second, ExecutionState.SUCCEEDED, TransferProgress(8, 8)))
    await repository.artifact_state(artifact.id, "completed", expected_bytes=8)

    presentation = await repository.presentation(transfer.id, details=True)
    assert len(presentation["route_attempts"]) == 1
    history = presentation["execution_attempts"]
    assert len(history) == 2
    assert {item["route_attempt_id"] for item in history} == {route.id}
    assert {item["candidate_id"] for item in history} == {candidate.id}
    assert presentation["delivering_provider_id"] == "provider_a"


async def test_item8_style_rows_backfill_known_facts_idempotently_without_url_inference(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "migration.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://rapidgator.net/looks-specialized", name="legacy.bin"))
    candidate = _candidate("durably_known_provider", "legacy-candidate")
    result = ResolutionResult(ResourceState.AVAILABLE, (candidate,))
    async with database.get_db() as db:
        await db.execute("DROP TABLE execution_attempt_provenance")
        await db.execute("DROP TABLE route_attempt_provenance")
        await db.execute("INSERT INTO resolution_attempts(id,request_id,provider_id,state,result) VALUES('legacy-route',?,?, 'succeeded',?)", (record.id, "durably_known_provider", codec.dump(result)))
        file_id = await db.execute_returning_id("""INSERT INTO download_files(torrent_id,request_id,filename,size_bytes,local_path,status,candidates,selected_candidate,execution_attempt_id,download_client)\n            VALUES(?,?,?,8,'/tmp/legacy.bin','completed',?,0,'legacy-execution','fixture_executor')""", (transfer.id, record.id, "legacy.bin", codec.dump((candidate,))))
        handle = ExecutionHandle("fixture_executor", {}, attempt_id="legacy-execution")
        await db.execute("""INSERT INTO execution_attempts(id,transfer_id,artifact_id,executor_id,handle,state,candidate)\n            VALUES('legacy-execution',?,?, 'fixture_executor',?,'succeeded',?)""", (transfer.id, file_id, codec.dump(handle), codec.dump(candidate)))
        await db.execute("UPDATE torrents SET status='completed' WHERE id=?", (transfer.id,))
        await db.commit()

    migrated = TransferRepository()
    await migrated.initialize()
    first = await migrated.presentation(transfer.id, details=True)
    assert first["delivering_provider_id"] == "durably_known_provider"
    assert first["route_attempts"][0]["history_quality"] == "legacy_known"
    assert first["execution_attempts"][0]["history_quality"] == "legacy_known"
    assert first["execution_attempts"][0]["route_attempt_id"] == "legacy-route"

    await migrated.initialize()
    async with database.get_db() as db:
        route_count = (await db.fetchone("SELECT COUNT(*) AS n FROM route_attempt_provenance"))["n"]
        execution_count = (await db.fetchone("SELECT COUNT(*) AS n FROM execution_attempt_provenance"))["n"]
    assert route_count == 1
    assert execution_count == 1

    unknown, _ = await _admit(migrated, TransferRequest("https", "https://rapidgator.net/no-proof", name="unknown.bin"))
    async with database.get_db() as db:
        await db.execute("UPDATE torrents SET status='completed' WHERE id=?", (unknown.id,))
        await db.commit()
    unknown_presentation = await migrated.presentation(unknown.id, details=True)
    assert unknown_presentation["delivering_provider_id"] is None
    assert unknown_presentation["provider_provenance_status"] == "unknown_legacy"
    assert "alldebrid" not in unknown_presentation["providers"]


async def test_general_http_provider_identity_is_persisted_at_route_time(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "general-http.sqlite3")
    provider = GeneralHttpProvider()
    request = TransferRequest("https", "https://downloads.example/file.bin?capability=secret", name="file.bin")
    transfer, record = await _admit(repository, request)
    attempt = await repository.begin_resolution(record.id, provider.descriptor.id)
    result = await provider.resolve(request)
    await repository.resolution(attempt, result)
    record = (await repository.requests(transfer.id))[0]
    await _materialize_and_execute(repository, record, result.candidates[0], attempt_id="general-http-execution")
    presentation = await repository.presentation(transfer.id, details=True)
    assert presentation["delivering_provider_id"] == "general_http"
    assert presentation["current_provider_id"] == "general_http"
    assert presentation["route_attempts"][0]["provider_id"] == "general_http"
    assert presentation["execution_attempts"][0]["provider_id"] == "general_http"
    assert "capability=secret" not in codec.dump(presentation["route_attempts"])


class _AllDebridUnlockClient:
    async def unlock_link(self, _url):
        return {"link": "https://cdn.example/unlocked.bin?signature=provider-secret", "filename": "unlocked.bin", "filesize": 8}


async def test_alldebrid_fixture_route_persists_provider_candidate_and_delivery(tmp_path, monkeypatch):
    import providers.alldebrid.provider as provider_module

    repository = await _repository(tmp_path, monkeypatch, "alldebrid.sqlite3")
    monkeypatch.setattr(provider_module, "validate_provider_download_url", lambda value: value)
    provider = AllDebridProvider(client=_AllDebridUnlockClient())
    request = TransferRequest("https", "https://rapidgator.net/example", name="unlocked.bin")
    transfer, record = await _admit(repository, request)
    attempt = await repository.begin_resolution(record.id, provider.descriptor.id)
    result = await provider.resolve(request)
    await repository.resolution(attempt, result)
    record = (await repository.requests(transfer.id))[0]
    await _materialize_and_execute(repository, record, result.candidates[0], attempt_id="alldebrid-execution")
    presentation = await repository.presentation(transfer.id, details=True)
    assert presentation["delivering_provider_id"] == "alldebrid"
    assert presentation["route_attempts"][0]["provider_id"] == "alldebrid"
    assert presentation["route_attempts"][0]["candidates"][0]["source"] == {"scope": "host", "key": "rapidgator.net"}
    assert presentation["execution_attempts"][0]["provider_id"] == "alldebrid"
    assert "provider-secret" not in codec.dump(presentation["route_attempts"])

async def test_completed_without_proven_delivery_does_not_promote_historical_provider(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "unknown-completed.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://example.test/unproven", name="unproven.bin"))
    candidate = _candidate("historical_provider", "historical-candidate")
    await _resolve(repository, record, "historical_provider", (candidate,))
    await _force_completed(transfer.id)

    presentation = await repository.presentation(transfer.id, details=True)
    assert presentation["historical_providers"] == ["historical_provider"]
    assert presentation["delivering_provider_id"] is None
    assert presentation["delivering_provider_ids"] == []
    assert presentation["provider_provenance_status"] == "unknown_legacy"
    assert presentation["providers"] == []
    assert presentation["route_attempts"][0]["provider_id"] == "historical_provider"


async def test_restart_mid_provider_transition_preserves_order_and_can_complete_new_route(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "mid-transition.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://shared.example/restart", name="restart.bin"))
    failed = NormalizedError(Domain.PROVIDER, Category.PROVIDER_UNAVAILABLE, Stage.RESOLUTION, retryability=Retryability.NEVER)
    attempt_a = await _resolve(repository, record, "provider_a", error=failed)
    await repository.retry_requests(transfer.id, request_id=record.id)
    record = (await repository.requests(transfer.id))[0]
    attempt_b = await repository.begin_resolution(record.id, "provider_b")
    assert attempt_b is not None

    restarted = TransferRepository()
    await restarted.initialize()
    mid = await restarted.presentation(transfer.id, details=True)
    assert [item["id"] for item in mid["route_attempts"]] == [attempt_a.id, attempt_b.id]
    assert mid["route_attempts"][0]["outcome"] == "failed"
    assert mid["route_attempts"][1]["outcome"] == "started"
    assert mid["route_attempts"][1]["previous_attempt_id"] == attempt_a.id
    assert mid["route_attempts"][1]["transition_kind"] == "provider_change"

    candidate_b = _candidate("provider_b", "restart-candidate")
    await restarted.resolution(attempt_b, ResolutionResult(ResourceState.AVAILABLE, (candidate_b,)))
    record = (await restarted.requests(transfer.id))[0]
    await _materialize_and_execute(restarted, record, candidate_b, attempt_id="restart-execution")
    await _force_completed(transfer.id)
    completed = await restarted.presentation(transfer.id, details=True)
    assert completed["delivering_provider_id"] == "provider_b"
    assert [item["outcome"] for item in completed["route_attempts"]] == ["failed", "completed"]
    assert len(completed["route_attempts"]) == 2
