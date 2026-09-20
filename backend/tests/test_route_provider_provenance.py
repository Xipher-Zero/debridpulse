"""Roadmap Item 9 durable route/provider provenance acceptance tests."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from providers.alldebrid.provider import AllDebridProvider
from providers.general_http.provider import GeneralHttpProvider
from transfers import codec
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage
from transfers.models import (
    ArtifactFingerprint,
    CachePresence,
    Capability,
    DeliveryKind,
    Endpoint,
    ExecutionHandle,
    ExecutionObservation,
    ExecutionState,
    FingerprintKind,
    IntegrationDescriptor,
    Ownership,
    ProviderObservation,
    ProviderResource,
    ResolutionResult,
    ResourceState,
    SourceEntry,
    SourceIdentity,
    TransferCandidate,
    TransferProgress,
    TransferRequest,
)
from transfers.policy import TransferPolicy
from transfers.presentation_repository import TransferRepository as PresentationTransferRepository
from transfers.recovery_repository import TransferRepository as RecoveryTransferRepository
from transfers.registry import IntegrationRegistry
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

    # Route History identity correction, Case B9: this SAME resolution
    # attempt's durable result carries two candidates -- there is no durable
    # per-route candidate_id to disambiguate them, so the historical route
    # identity must report unknown (None) rather than guessing
    # result.candidates[0].
    assert presentation["route_attempts"][0]["route_origin"] is None
    assert presentation["route_attempts"][0]["route_location"] is None
    assert presentation["route_attempts"][0]["route_identity"] is None


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

    # Re-enter through the canonical database owner to recreate current-schema
    # tables before ordinary repository startup validates them.
    await database.init_db()
    migrated = TransferRepository()
    await migrated.initialize()
    # DB-001: ordinary repository startup validates current-schema readiness only.
    # Historical provenance reconstruction is invoked by the v1.0.12 migration owner.
    async with database.get_db() as db:
        assert (await db.fetchone("SELECT COUNT(*) AS n FROM route_attempt_provenance"))["n"] == 0
        assert (await db.fetchone("SELECT COUNT(*) AS n FROM execution_attempt_provenance"))["n"] == 0
        await migrated._backfill_provenance(db)
        await db.commit()

    first = await migrated.presentation(transfer.id, details=True)
    assert first["delivering_provider_id"] == "durably_known_provider"
    assert first["route_attempts"][0]["history_quality"] == "legacy_known"
    assert first["execution_attempts"][0]["history_quality"] == "legacy_known"
    assert first["execution_attempts"][0]["route_attempt_id"] == "legacy-route"

    async with database.get_db() as db:
        await migrated._backfill_provenance(db)
        await db.commit()
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


async def test_completed_transfer_provenance_follows_delivered_execution_not_submission_order(tmp_path, monkeypatch):
    """Canonical transfer-detail provenance correction, Case B: canonical
    source/request provenance must come from durable delivered-execution
    state -- never "first source submitted." The root request here names
    ``hosta.example`` (the original, first-submitted user request), but the
    artifact's actual DELIVERED execution attempt durably carries
    ``hostb.example`` -- e.g. the request was re-resolved onto a different
    mirror before delivery. ``current_source_identity`` (``transfers
    .presentation_repository.TransferRepository.presentation``) must report
    the durable delivered source, not the submitted request's own host."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "provenance_order.sqlite3")
    await database.init_db()
    repository = PresentationTransferRepository()
    await repository.initialize()
    transfer, record = await _admit(
        repository, TransferRequest("https", "https://hosta.example/file.bin", name="file.bin"),
    )
    candidate = _candidate("general_http", "delivered-candidate")
    delivered_source = SourceIdentity("host", "hostb.example")
    async with database.get_db() as db:
        file_id = await db.execute_returning_id(
            """INSERT INTO download_files(torrent_id,request_id,filename,size_bytes,local_path,status,
                   candidates,selected_candidate,execution_attempt_id,download_client)
               VALUES(?,?,?,8,'/tmp/file.bin','completed',?,0,'exec-1','fixture_executor')""",
            (transfer.id, record.id, "file.bin", codec.dump((candidate,))),
        )
        handle = ExecutionHandle("fixture_executor", {}, attempt_id="exec-1")
        await db.execute(
            """INSERT INTO execution_attempts(id,transfer_id,artifact_id,executor_id,handle,state,candidate)
               VALUES('exec-1',?,?, 'fixture_executor',?,'succeeded',?)""",
            (transfer.id, file_id, codec.dump(handle), codec.dump(candidate)),
        )
        await db.execute(
            """INSERT INTO execution_attempt_provenance(
                   execution_attempt_id,transfer_id,artifact_id,ordinal,provider_id,candidate_id,
                   candidate_source,outcome,delivered)
               VALUES('exec-1',?,?,1,'general_http',?,?, 'completed',1)""",
            (transfer.id, file_id, candidate.id, codec.dump(delivered_source)),
        )
        await db.execute("UPDATE torrents SET status='completed' WHERE id=?", (transfer.id,))
        await db.commit()

    presentation = await repository.presentation(transfer.id, details=False)
    assert presentation["current_source_identity"] == {"kind": "host", "host": "hostb.example"}, (
        "durable delivered-execution provenance must win over the root request's own "
        "submitted/named host -- provenance is never derived from submission order"
    )


# ---------------------------------------------------------------------------
# DP 1.0.12 Details Page canonical Route History identity projection
# (Workstream B). Every row's route identity comes from that SAME resolution
# attempt's own durable resolution_attempts.result -- never from current
# artifact/candidate state, execution, canonical binding, or request order.
# ---------------------------------------------------------------------------

async def test_safe_route_endpoint_preserves_scheme_and_port_strips_credentials_and_query():
    """B2/B4/B5/B6, unit level: the provider-neutral route-safety helper."""
    from core.presentation_safety import safe_route_endpoint

    assert safe_route_endpoint("http://mirror.example/file.iso") == (
        "http://mirror.example", "http://mirror.example/file.iso",
    )
    assert safe_route_endpoint("https://mirror.example/file.iso") == (
        "https://mirror.example", "https://mirror.example/file.iso",
    )
    assert safe_route_endpoint("ftp://mirror.example/file.bin") == (
        "ftp://mirror.example", "ftp://mirror.example/file.bin",
    )
    assert safe_route_endpoint("sftp://mirror.example/file.bin") == (
        "sftp://mirror.example", "sftp://mirror.example/file.bin",
    )
    assert safe_route_endpoint("scp://backup.example/file.bin") == (
        "scp://backup.example", "scp://backup.example/file.bin",
    )

    origin, location = safe_route_endpoint("https://example.org:8443/file.iso")
    assert origin == "https://example.org:8443"
    assert location == "https://example.org:8443/file.iso"

    # Gate 9 revision 2: an explicit well-known default port identifies the
    # SAME origin as no port at all -- both for display and for same-origin
    # collision detection -- while a genuinely non-default port stays visible.
    assert safe_route_endpoint("https://example.org:443/file.iso") == safe_route_endpoint("https://example.org/file.iso")
    assert safe_route_endpoint("http://example.org:80/file.iso") == safe_route_endpoint("http://example.org/file.iso")
    assert safe_route_endpoint("ftp://example.org:21/file.bin") == safe_route_endpoint("ftp://example.org/file.bin")
    assert safe_route_endpoint("sftp://example.org:22/file.bin") == safe_route_endpoint("sftp://example.org/file.bin")
    assert safe_route_endpoint("scp://example.org:22/file.bin") == safe_route_endpoint("scp://example.org/file.bin")
    default_origin, _ = safe_route_endpoint("https://example.org:443/file.iso")
    assert default_origin == "https://example.org"
    non_default_origin, _ = safe_route_endpoint("https://example.org:8443/file.iso")
    assert non_default_origin == "https://example.org:8443"
    # A scheme's default port is NOT another scheme's default -- https:21
    # must remain visible even though 21 is FTP's default.
    cross_scheme_origin, _ = safe_route_endpoint("https://example.org:21/file.iso")
    assert cross_scheme_origin == "https://example.org:21"

    origin, location = safe_route_endpoint(
        "https://user:secret@example.org/path/file.iso?token=abc#fragment",
    )
    assert origin == "https://example.org"
    assert location == "https://example.org/path/file.iso"
    for leaked in ("user", "secret", "token=abc", "fragment"):
        assert leaked not in origin
        assert leaked not in location

    # Unsupported/unparseable input fails closed -- never leaks raw input.
    assert safe_route_endpoint("magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567") == (None, None)
    assert safe_route_endpoint("") == (None, None)
    assert safe_route_endpoint(None) == (None, None)

    # Bounded output (Section 17): an attacker-controlled long path cannot
    # create an unbounded Details row.
    huge_path = "/" + ("a" * 5000)
    _, huge_location = safe_route_endpoint(f"https://example.org{huge_path}")
    assert len(huge_location) <= 180


async def test_distinct_origin_routes_are_individually_identifiable(tmp_path, monkeypatch):
    """B1: two HTTP(S) route attempts with different origins become
    distinguishable route_identity values."""
    repository = await _repository(tmp_path, monkeypatch, "route-b1.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://one.example/file.iso", name="file.iso"))
    first = TransferCandidate(
        name="file.iso", endpoints=(Endpoint("https", "https://one.example/file.iso"),),
        provider_id="general_http", id="route-one",
    )
    route_one = await _resolve(repository, record, "general_http", (first,))
    await repository.retry_requests(transfer.id, request_id=record.id)
    record = (await repository.requests(transfer.id))[0]
    second = TransferCandidate(
        name="file.iso", endpoints=(Endpoint("https", "https://two.example/file.iso"),),
        provider_id="general_http", id="route-two",
    )
    route_two = await _resolve(repository, record, "general_http", (second,))

    presentation = await repository.presentation(transfer.id, details=True)
    routes = {item["id"]: item for item in presentation["route_attempts"]}
    assert routes[route_one.id]["route_identity"] == "https://one.example"
    assert routes[route_two.id]["route_identity"] == "https://two.example"


async def test_same_origin_routes_disambiguate_by_path(tmp_path, monkeypatch):
    """B7: two historical routes sharing one origin disambiguate via the
    safe path-bearing route_location instead of colliding on the bare
    origin."""
    repository = await _repository(tmp_path, monkeypatch, "route-b7.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://foo.example/releases/a.iso", name="a.iso"))
    releases = TransferCandidate(
        name="a.iso", endpoints=(Endpoint("https", "https://foo.example/releases/a.iso"),),
        provider_id="general_http", id="releases-route",
    )
    route_releases = await _resolve(repository, record, "general_http", (releases,))
    await repository.retry_requests(transfer.id, request_id=record.id)
    record = (await repository.requests(transfer.id))[0]
    archive = TransferCandidate(
        name="a.iso", endpoints=(Endpoint("https", "https://foo.example/archive/a.iso"),),
        provider_id="general_http", id="archive-route",
    )
    route_archive = await _resolve(repository, record, "general_http", (archive,))

    presentation = await repository.presentation(transfer.id, details=True)
    routes = {item["id"]: item for item in presentation["route_attempts"]}
    assert routes[route_releases.id]["route_origin"] == "https://foo.example"
    assert routes[route_archive.id]["route_origin"] == "https://foo.example"
    assert routes[route_releases.id]["route_identity"] == "https://foo.example/releases/a.iso"
    assert routes[route_archive.id]["route_identity"] == "https://foo.example/archive/a.iso"
    assert routes[route_releases.id]["route_identity"] != routes[route_archive.id]["route_identity"]


async def test_explicit_default_port_does_not_falsely_disambiguate_same_origin(tmp_path, monkeypatch):
    """Gate 9 revision 2: an explicit well-known default port (``:443`` for
    HTTPS) must normalize to the SAME route_origin as no port at all -- not
    just for display, but for same-origin collision detection itself. Before
    this correction, these two historical routes would falsely appear to be
    two different origins and both would incorrectly get bare-origin
    identities instead of colliding and falling back to route_location."""
    repository = await _repository(tmp_path, monkeypatch, "route-default-port.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://foo.example/a.iso", name="a.iso"))
    bare = TransferCandidate(
        name="a.iso", endpoints=(Endpoint("https", "https://foo.example/releases/a.iso"),),
        provider_id="general_http", id="bare-port-route",
    )
    route_bare = await _resolve(repository, record, "general_http", (bare,))
    await repository.retry_requests(transfer.id, request_id=record.id)
    record = (await repository.requests(transfer.id))[0]
    explicit = TransferCandidate(
        name="a.iso", endpoints=(Endpoint("https", "https://foo.example:443/archive/a.iso"),),
        provider_id="general_http", id="explicit-default-port-route",
    )
    route_explicit = await _resolve(repository, record, "general_http", (explicit,))

    presentation = await repository.presentation(transfer.id, details=True)
    routes = {item["id"]: item for item in presentation["route_attempts"]}
    assert routes[route_bare.id]["route_origin"] == "https://foo.example"
    assert routes[route_explicit.id]["route_origin"] == "https://foo.example"
    assert routes[route_bare.id]["route_identity"] == "https://foo.example/releases/a.iso"
    assert routes[route_explicit.id]["route_identity"] == "https://foo.example/archive/a.iso"


async def test_multi_endpoint_candidate_reports_unknown_identity_not_first_endpoint(tmp_path, monkeypatch):
    """Gate 9 revision 2: a durable resolution attempt identifies the
    candidate, but nothing durable identifies which endpoint WITHIN a
    candidate that carries more than one represented the actual historical
    route. This must report unknown route identity rather than guessing
    ``endpoints[0]``."""
    repository = await _repository(tmp_path, monkeypatch, "route-multi-endpoint.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://mirror.example/file.bin", name="file.bin"))
    dual_endpoint = TransferCandidate(
        name="file.bin",
        endpoints=(
            Endpoint("https", "https://mirror-primary.example/file.bin"),
            Endpoint("https", "https://mirror-backup.example/file.bin"),
        ),
        provider_id="general_http", id="dual-endpoint-route",
    )
    route = await _resolve(repository, record, "general_http", (dual_endpoint,))

    presentation = await repository.presentation(transfer.id, details=True)
    item = next(entry for entry in presentation["route_attempts"] if entry["id"] == route.id)
    assert item["route_origin"] is None
    assert item["route_location"] is None
    assert item["route_identity"] is None


async def test_list_path_route_query_excludes_heavy_resolution_result_column(tmp_path, monkeypatch):
    """Gate 9 revision 3: ``presentation(details=False)`` is the list-path
    call made for every row on every poll -- it must never pull the durable,
    materially-larger-than-``candidate_summary`` ``resolution_attempts.result``
    blob it never reads. Captures the ACTUAL SQL text the route-attempts
    query executes for both ``details=False`` and ``details=True``, proving
    the query shape directly rather than only asserting on returned data."""
    repository = await _repository(tmp_path, monkeypatch, "route-list-query-shape.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://mirror.example/file.bin", name="file.bin"))
    candidate = TransferCandidate(
        name="file.bin", endpoints=(Endpoint("https", "https://mirror.example/file.bin"),),
        provider_id="general_http", id="query-shape-route",
    )
    await _resolve(repository, record, "general_http", (candidate,))

    captured = []
    original_fetchall = database._DbConnection.fetchall

    async def _recording_fetchall(self, sql, params=()):
        if "route_attempt_provenance" in sql:
            captured.append(sql)
        return await original_fetchall(self, sql, params)

    monkeypatch.setattr(database._DbConnection, "fetchall", _recording_fetchall)

    captured.clear()
    await repository.presentation(transfer.id, details=False)
    assert len(captured) == 1, "the route-attempts query must still run exactly once for the list path"
    assert "a.result" not in captured[0]
    assert "resolution_result" not in captured[0]

    captured.clear()
    await repository.presentation(transfer.id, details=True)
    assert len(captured) == 1, "the route-attempts query must still run exactly once for the detail path"
    assert "a.result AS resolution_result" in captured[0]


async def test_ftp_sftp_scp_are_distinguishable_within_generic_transport_projection(tmp_path, monkeypatch):
    """B3: FTP vs SFTP vs SCP remain distinct historical routes through the
    same provider-neutral safety helper, using direct candidate/endpoint
    construction -- no live FTP/SFTP/SCP provider runtime is required to
    prove the sanitizer/projection contract."""
    repository = await _repository(tmp_path, monkeypatch, "route-b3.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://mirror.example/file.bin", name="file.bin"))
    ftp = TransferCandidate(
        name="file.bin", endpoints=(Endpoint("ftp", "ftp://mirror.example/file.bin"),),
        provider_id="generic_transport", id="ftp-route",
    )
    route_ftp = await _resolve(repository, record, "generic_transport", (ftp,))
    await repository.retry_requests(transfer.id, request_id=record.id)
    record = (await repository.requests(transfer.id))[0]
    sftp = TransferCandidate(
        name="file.bin", endpoints=(Endpoint("sftp", "sftp://mirror.example/file.bin"),),
        provider_id="generic_transport", id="sftp-route",
    )
    route_sftp = await _resolve(repository, record, "generic_transport", (sftp,))
    await repository.retry_requests(transfer.id, request_id=record.id)
    record = (await repository.requests(transfer.id))[0]
    scp = TransferCandidate(
        name="file.bin", endpoints=(Endpoint("scp", "scp://backup.example/file.bin"),),
        provider_id="generic_transport", id="scp-route",
    )
    route_scp = await _resolve(repository, record, "generic_transport", (scp,))

    presentation = await repository.presentation(transfer.id, details=True)
    routes = {item["id"]: item for item in presentation["route_attempts"]}
    assert routes[route_ftp.id]["route_identity"] == "ftp://mirror.example"
    assert routes[route_sftp.id]["route_identity"] == "sftp://mirror.example"
    assert routes[route_scp.id]["route_identity"] == "scp://backup.example"


async def test_historical_route_identity_survives_later_candidate_switch(tmp_path, monkeypatch):
    """B8: Route History for an EARLIER resolution attempt keeps identifying
    its own original route even after the transfer's active candidate later
    switches to a different provider/host -- Route History is historical
    provenance, not a reconstruction from current state."""
    repository = await _repository(tmp_path, monkeypatch, "route-b8.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://shared.example/file", name="route.bin"))
    candidate_a = TransferCandidate(
        name="route.bin", endpoints=(Endpoint("https", "https://mirror-a.example/route.bin"),),
        provider_id="provider_a", id="route-a",
    )
    route_a = await _resolve(repository, record, "provider_a", (candidate_a,))
    await repository.retry_requests(transfer.id, request_id=record.id)
    record = (await repository.requests(transfer.id))[0]
    candidate_b = TransferCandidate(
        name="route.bin", endpoints=(Endpoint("https", "https://mirror-b.example/route.bin"),),
        provider_id="provider_b", id="route-b",
    )
    route_b = await _resolve(repository, record, "provider_b", (candidate_b,))
    record = (await repository.requests(transfer.id))[0]
    await _materialize_and_execute(repository, record, candidate_b, attempt_id="route-b8-exec")
    await _force_completed(transfer.id)

    presentation = await repository.presentation(transfer.id, details=True)
    routes = {item["id"]: item for item in presentation["route_attempts"]}
    assert routes[route_a.id]["route_identity"] == "https://mirror-a.example"
    assert routes[route_b.id]["route_identity"] == "https://mirror-b.example"
    assert presentation["delivering_provider_id"] == "provider_b"


async def test_route_identity_restart_determinism(tmp_path, monkeypatch):
    """B10: reinstantiating the repository against the same durable database
    reproduces identical ordinal/provider/route fields."""
    repository = await _repository(tmp_path, monkeypatch, "route-b10.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://mirror.example/file.bin", name="file.bin"))
    candidate = TransferCandidate(
        name="file.bin", endpoints=(Endpoint("https", "https://mirror.example:8443/file.bin"),),
        provider_id="general_http", id="restart-route",
    )
    await _resolve(repository, record, "general_http", (candidate,))

    before = await repository.presentation(transfer.id, details=True)
    restarted = TransferRepository()
    await restarted.initialize()
    after = await restarted.presentation(transfer.id, details=True)
    fields = ("id", "ordinal", "provider_id", "route_origin", "route_location", "route_identity", "outcome")
    assert [{f: item[f] for f in fields} for item in before["route_attempts"]] == \
        [{f: item[f] for f in fields} for item in after["route_attempts"]]


async def test_production_shaped_five_mirror_route_history_acceptance(tmp_path, monkeypatch):
    """Section 19 production-shaped acceptance: five independent historical
    HTTP(S) route attempts for the same logical download (deterministic
    fixtures standing in for real mirrors -- no live DNS/network involved)
    each remain individually identifiable by their own historical route,
    matching the Ubuntu-ISO multi-mirror shape motivating this correction."""
    repository = await _repository(tmp_path, monkeypatch, "route-acceptance.sqlite3")
    transfer, record = await _admit(
        repository,
        TransferRequest("https", "https://releases.ubuntu.com/ubuntu-26.04-desktop-amd64.iso", name="ubuntu-26.04-desktop-amd64.iso"),
    )
    mirrors = (
        "https://releases.ubuntu.com/ubuntu-26.04-desktop-amd64.iso",
        "https://mirror.pilotfiber.com/ubuntu-26.04-desktop-amd64.iso",
        "https://mirrors.tuna.tsinghua.edu.cn/ubuntu-26.04-desktop-amd64.iso",
        "https://mirror.arizona.edu/ubuntu-26.04-desktop-amd64.iso",
        "https://download.nus.edu.sg/ubuntu-26.04-desktop-amd64.iso",
    )
    route_ids = []
    for index, address in enumerate(mirrors):
        candidate = TransferCandidate(
            name="ubuntu-26.04-desktop-amd64.iso", endpoints=(Endpoint("https", address),),
            provider_id="general_http", id=f"mirror-{index}",
        )
        route = await _resolve(repository, record, "general_http", (candidate,))
        route_ids.append(route.id)
        if index < len(mirrors) - 1:
            await repository.retry_requests(transfer.id, request_id=record.id)
            record = (await repository.requests(transfer.id))[0]

    presentation = await repository.presentation(transfer.id, details=True)
    routes = {item["id"]: item for item in presentation["route_attempts"]}
    expected_identities = [
        "https://releases.ubuntu.com", "https://mirror.pilotfiber.com",
        "https://mirrors.tuna.tsinghua.edu.cn", "https://mirror.arizona.edu", "https://download.nus.edu.sg",
    ]
    assert [routes[rid]["route_identity"] for rid in route_ids] == expected_identities
    assert len({routes[rid]["route_identity"] for rid in route_ids}) == 5
    assert [routes[rid]["ordinal"] for rid in route_ids] == [1, 2, 3, 4, 5]


# --------------------------------------------------------------------------- #
# DP 1.0.12 canonical torrent cache fact + debrid Route History identity
# --------------------------------------------------------------------------- #

_DELIVERY = "https://f8g9h0.debrid.it/dl/abc123/file.bin?token=SECRETTOKEN"


def _hoster_candidate(*, provider="alldebrid", host="1fichier.com", endpoint=_DELIVERY,
                      delivery=DeliveryKind.PROVIDER_ISSUED, identity="hoster-route", source=True):
    return TransferCandidate(
        name="file.bin", endpoints=(Endpoint("https", endpoint),), expected_bytes=8,
        provider_id=provider, id=identity, delivery=delivery,
        source_identity=SourceIdentity("host", host) if source else None,
    )


def _upload(provider_id, cache, native_id="native"):
    """A provider upload result: resource readiness and cache presence are
    independent facts, so a MISS may still be AVAILABLE and vice versa."""
    state = ResourceState.AVAILABLE if cache == CachePresence.HIT else ResourceState.PREPARING
    resource = ProviderResource(provider_id, {"id": native_id}, Ownership.CREATED)
    return ResolutionResult(state, observation=ProviderObservation(resource, state, "payload", cache_presence=cache))


async def _root_attempt(repository, record, provider_id, result, *, first):
    attempt = (await repository.begin_resolution(record.id, provider_id) if first
               else await repository.begin_refresh(record, provider_id))
    assert attempt is not None
    await repository.resolution(attempt, result)
    return attempt


async def _torrent_root(repository, *, kind="magnet", suffix="a"):
    payload = f"magnet:?xt=urn:btih:{suffix * 40}" if kind == "magnet" else b"d4:infod4:name1:xee"
    return await _admit(repository, TransferRequest(kind, payload, name=f"payload-{suffix}"))


async def _children(repository, root, count=2):
    entries = tuple(
        SourceEntry(f"f{index}.bin", 8, f"f{index}.bin",
                    TransferRequest("https", f"https://alldebrid.example/f/{root.id[:6]}{index}",
                                    f"f{index}.bin", preferred_provider="alldebrid"))
        for index in range(count)
    )
    await repository.manifest(root, entries)
    return [item for item in await repository.requests(root.transfer_id) if item.parent_id == root.id]


async def _child_attempt(repository, child, provider_id="alldebrid", *, first=True):
    # The provider-generated child is HTTP(S) with a provider-issued endpoint; its
    # own source identity is the provider's unlock host, NOT the logical source.
    candidate = _hoster_candidate(provider=provider_id, host="alldebrid.com", identity=f"child-{child.id[:8]}")
    return await _root_attempt(repository, child, provider_id, ResolutionResult(ResourceState.AVAILABLE, (candidate,)), first=first)


def _routes(presentation):
    return {item["id"]: item for item in presentation["route_attempts"]}


def _identity_fields(item):
    """Everything Route History presents as identity/hover for a row (the
    ``candidates`` provenance summary is a separate, pre-existing field)."""
    return str((item["route_origin"], item["route_location"], item["route_identity"]))


async def _rewrite_result(attempt_id, rewrite):
    async with database.get_db() as db:
        row = await db.fetchone("SELECT result FROM resolution_attempts WHERE id=?", (attempt_id,))
        payload = codec.load(row["result"], {})
        rewrite(payload)
        await db.execute("UPDATE resolution_attempts SET result=? WHERE id=?", (codec.dump(payload), attempt_id))
        await db.commit()


# --- Section 9: one canonical host normalizer ------------------------------ #

async def test_safe_public_host_is_the_single_canonical_host_normalizer():
    from core.presentation_safety import safe_public_host
    import transfers.presentation_repository as presentation_repository

    assert safe_public_host("1FICHIER.com.") == "1fichier.com"          # case + trailing dot
    assert safe_public_host("www.Example.ORG") == "example.org"
    assert safe_public_host("a-b.c9.example") == "a-b.c9.example"
    for rejected in (
        "user:pass@example.org", "user@example.org", "example.org:8443", "example.org/path",
        "example.org?token=1", "example.org#frag", "https://example.org", "exa mple.org",
        "-bad.example", "bad-.example", "a..b.example", "münchen.example", "", None, "   ",
        "a" * 64 + ".example", ".".join(["a" * 60] * 5),
    ):
        assert safe_public_host(rejected) is None, rejected
    # Consolidation, not a third copy: the previous private implementation is gone
    # and its consumer uses the canonical helper.
    assert presentation_repository.safe_public_host is safe_public_host
    assert not hasattr(presentation_repository, "_public_host")
    assert not hasattr(presentation_repository, "_HOST_LABEL_RE")
    assert presentation_repository._candidate_source({"scope": "host", "key": "Www.Example.org."}) == {
        "kind": "host", "host": "example.org"}
    assert presentation_repository._candidate_source({"scope": "host", "key": "user@example.org"}) is None


# --- 10.3 durable round trip / restart / legacy ----------------------------- #

async def test_cache_fact_persists_in_the_existing_resolution_result_and_survives_restart(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "cache-durable.sqlite3")
    transfer, root = await _torrent_root(repository)
    attempt = await _root_attempt(repository, root, "alldebrid", _upload("alldebrid", CachePresence.HIT), first=True)

    async with database.get_db() as db:
        row = await db.fetchone("SELECT result FROM resolution_attempts WHERE id=?", (attempt.id,))
        tables = {r["name"] for r in await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {r["name"] for r in await db.fetchall("PRAGMA table_info(resolution_attempts)")}
    # The existing durable ResolutionResult owns the fact: no side store, no new column.
    assert codec.load(row["result"])["observation"]["cache_presence"] == "hit"
    assert not any("cache" in name for name in tables)
    assert columns == {"id", "request_id", "provider_id", "state", "error", "result", "created_at", "updated_at"}

    restarted = TransferRepository()
    await restarted.initialize()
    details = await restarted.presentation(transfer.id, details=True)
    assert [item["route_identity"] for item in details["route_attempts"]] == ["Torrent cache"]


async def test_rows_persisted_before_the_fact_existed_decode_and_present_as_unknown(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "cache-legacy.sqlite3")
    transfer, root = await _torrent_root(repository)
    attempt = await _root_attempt(repository, root, "alldebrid", _upload("alldebrid", CachePresence.HIT), first=True)
    children = await _children(repository, root, 1)
    child_attempt = await _child_attempt(repository, children[0])

    def strip_new_fields(payload):
        (payload.get("observation") or {}).pop("cache_presence", None)
        for candidate in payload.get("candidates", []):
            candidate.pop("delivery", None)
    await _rewrite_result(attempt.id, strip_new_fields)
    await _rewrite_result(child_attempt.id, strip_new_fields)

    async with database.get_db() as db:
        raw = await db.fetchone("SELECT result FROM resolution_attempts WHERE id=?", (attempt.id,))
    assert "cache_presence" not in raw["result"]
    assert codec.cache_presence(codec.load(raw["result"])["observation"].get("cache_presence")) == CachePresence.UNKNOWN
    for garbage in (None, "", "cached", 1, True, [], {}):
        assert codec.cache_presence(garbage) == CachePresence.UNKNOWN

    first = await repository.presentation(transfer.id, details=True)
    second = await TransferRepository().presentation(transfer.id, details=True)
    routes = _routes(first)
    # Legacy history is never migrated into a guessed hit: BitTorrent, deterministically.
    assert routes[attempt.id]["route_identity"] == "BitTorrent"
    assert routes[child_attempt.id]["route_identity"] == "BitTorrent"
    assert [i["route_identity"] for i in first["route_attempts"]] == [i["route_identity"] for i in second["route_attempts"]]


# --- 10.4 generic / direct routes unchanged --------------------------------- #

async def test_direct_route_keeps_endpoint_identity_even_when_it_carries_a_source_identity(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "route-direct-unchanged.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://foo.example/a/b.iso", name="b.iso"))
    direct = TransferCandidate(
        name="b.iso", endpoints=(Endpoint("https", "https://user:pw@Foo.Example:443/a/b.iso?token=SECRETTOKEN#frag"),),
        provider_id="general_http", id="direct-route", source_identity=SourceIdentity("host", "other.example"),
    )
    route = await _resolve(repository, record, "general_http", (direct,))
    item = _routes(await repository.presentation(transfer.id, details=True))[route.id]
    assert item["route_origin"] == "https://foo.example"
    assert item["route_location"] == "https://foo.example/a/b.iso"
    assert item["route_identity"] == "https://foo.example"
    blob = _identity_fields(item)
    for leaked in ("user", "pw", "SECRETTOKEN", "frag", "other.example"):
        assert leaked not in blob


async def test_logical_rows_never_perturb_same_origin_disambiguation_of_direct_rows(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "route-mixed.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://foo.example/x.iso", name="x.iso"))
    ids = []
    for index, address in enumerate(("https://foo.example/releases/x.iso", "https://foo.example/archive/x.iso")):
        candidate = TransferCandidate(name="x.iso", endpoints=(Endpoint("https", address),),
                                      provider_id="general_http", id=f"g{index}")
        ids.append((await _resolve(repository, record, "general_http", (candidate,))).id)
        await repository.retry_requests(transfer.id, request_id=record.id)
        record = (await repository.requests(transfer.id))[0]
    # A debrid row whose delivery endpoint shares that very origin must not take part.
    hoster = _hoster_candidate(endpoint="https://foo.example/dl/zzz/x.iso?sig=SECRET", identity="mediated")
    mediated = await _resolve(repository, record, "alldebrid", (hoster,))
    routes = _routes(await repository.presentation(transfer.id, details=True))
    assert routes[ids[0]]["route_identity"] == "https://foo.example/releases/x.iso"
    assert routes[ids[1]]["route_identity"] == "https://foo.example/archive/x.iso"
    assert routes[mediated.id]["route_identity"] == "1fichier.com"


# --- 10.5 debrid direct-hoster identity ------------------------------------- #

async def test_debrid_hoster_route_presents_upstream_host_never_the_delivery_url(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "route-hoster.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://www.1fichier.com/?abc", name="file.bin"))
    route = await _resolve(repository, record, "alldebrid", (_hoster_candidate(host="www.1FICHIER.com."),))
    item = _routes(await repository.presentation(transfer.id, details=True))[route.id]
    assert item["route_identity"] == "1fichier.com"
    # The provider capability is neither the identity nor the hover identity.
    assert item["route_origin"] is None and item["route_location"] is None
    blob = str(item)
    for leaked in ("debrid.it", "f8g9h0", "/dl/", "abc123", "SECRETTOKEN"):
        assert leaked not in blob


async def test_alldebrid_provider_result_flows_to_the_hoster_identity_end_to_end(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "route-hoster-e2e.sqlite3")
    request = TransferRequest("https", "https://1fichier.com/?xyz", name="file.bin")
    transfer, record = await _admit(repository, request)
    client = AsyncMock()
    client.unlock_link.return_value = {"link": _DELIVERY, "filename": "file.bin", "filesize": 8}
    attempt = await repository.begin_resolution(record.id, "alldebrid")
    await repository.resolution(attempt, await AllDebridProvider(client=client).resolve(request))
    item = _routes(await repository.presentation(transfer.id, details=True))[attempt.id]
    assert item["route_identity"] == "1fichier.com"
    assert "debrid.it" not in str(item)


@pytest.mark.parametrize("candidate", [
    _hoster_candidate(source=False, identity="no-source"),
    TransferCandidate("f", (Endpoint("https", _DELIVERY),), provider_id="alldebrid", id="wrong-scope",
                      delivery=DeliveryKind.PROVIDER_ISSUED, source_identity=SourceIdentity("account", "1fichier.com")),
    _hoster_candidate(host="user:pw@evil.example/x", identity="malformed-host"),
    _hoster_candidate(host="", identity="empty-host"),
])
async def test_provider_issued_route_without_a_provable_safe_host_is_unknown_not_the_delivery_url(tmp_path, monkeypatch, candidate):
    repository = await _repository(tmp_path, monkeypatch, "route-hoster-failclosed.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://x.example/a", name="a"))
    route = await _resolve(repository, record, "alldebrid", (candidate,))
    item = _routes(await repository.presentation(transfer.id, details=True))[route.id]
    assert item["route_identity"] is None
    assert item["route_origin"] is None and item["route_location"] is None
    assert "debrid.it" not in str(item) and "evil.example" not in _identity_fields(item)


async def test_provider_issued_route_with_several_candidates_is_unknown(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "route-hoster-multi.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://x.example/a", name="a"))
    route = await _resolve(repository, record, "alldebrid",
                           (_hoster_candidate(identity="one"), _hoster_candidate(identity="two", host="other.example")))
    item = _routes(await repository.presentation(transfer.id, details=True))[route.id]
    assert item["route_identity"] is None


# --- 10.6 torrent lineage presentation -------------------------------------- #

@pytest.mark.parametrize("kind", ["magnet", "torrent"])
@pytest.mark.parametrize("cache,label", [
    (CachePresence.HIT, "Torrent cache"),
    (CachePresence.MISS, "BitTorrent"),
    (CachePresence.UNKNOWN, "BitTorrent"),
])
async def test_torrent_root_and_provider_generated_descendants_present_the_root_class(tmp_path, monkeypatch, kind, cache, label):
    repository = await _repository(tmp_path, monkeypatch, f"route-torrent-{kind}-{cache.value}.sqlite3")
    transfer, root = await _torrent_root(repository, kind=kind)
    root_attempt = await _root_attempt(repository, root, "alldebrid", _upload("alldebrid", cache), first=True)
    children = await _children(repository, root, 2)
    child_attempts = [await _child_attempt(repository, child) for child in children]
    presentation = await repository.presentation(transfer.id, details=True)
    routes = _routes(presentation)
    for attempt in (root_attempt, *child_attempts):
        item = routes[attempt.id]
        assert item["route_identity"] == label
        # Never the provider-generated http(s) descendant nor its unlock host.
        assert item["route_origin"] is None and item["route_location"] is None
        assert "debrid.it" not in str(item) and "alldebrid.com" not in _identity_fields(item)
    # The child kind alone is http(s); lineage is what made it BitTorrent.
    assert {child.request.kind for child in children} == {"https"}


async def test_failed_root_attempt_still_belongs_to_its_bittorrent_lineage(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "route-torrent-failed.sqlite3")
    transfer, root = await _torrent_root(repository)
    failed = await _resolve(repository, root, "alldebrid", (), error=NormalizedError(Domain.PROVIDER, Category.PROVIDER_UNAVAILABLE, Stage.RESOLUTION))
    item = _routes(await repository.presentation(transfer.id, details=True))[failed.id]
    assert item["route_identity"] == "BitTorrent"


@pytest.mark.parametrize("sequence,label", [
    ((CachePresence.MISS, CachePresence.HIT), "BitTorrent"),                  # later readiness never upgrades a MISS
    ((CachePresence.UNKNOWN, CachePresence.MISS, CachePresence.HIT), "BitTorrent"),
    ((CachePresence.UNKNOWN, CachePresence.HIT), "Torrent cache"),            # UNKNOWN never establishes a fact
    ((CachePresence.HIT, CachePresence.MISS), "Torrent cache"),               # not "latest state"
    ((CachePresence.HIT, CachePresence.UNKNOWN), "Torrent cache"),
    ((CachePresence.UNKNOWN, CachePresence.UNKNOWN), "BitTorrent"),
])
async def test_cache_label_is_the_first_authoritative_observation_never_the_latest(tmp_path, monkeypatch, sequence, label):
    repository = await _repository(tmp_path, monkeypatch, "route-first-authoritative.sqlite3")
    transfer, root = await _torrent_root(repository)
    attempts = []
    for index, cache in enumerate(sequence):
        attempts.append(await _root_attempt(repository, root, "alldebrid", _upload("alldebrid", cache, f"n{index}"), first=index == 0))
    children = await _children(repository, root, 1)
    attempts.append(await _child_attempt(repository, children[0]))
    routes = _routes(await repository.presentation(transfer.id, details=True))
    # One label for the whole lineage on this provider, every row, every time.
    assert {routes[a.id]["route_identity"] for a in attempts} == {label}


async def test_cache_fact_is_scoped_to_the_provider_that_observed_it(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "route-provider-scoped.sqlite3")
    transfer, root = await _torrent_root(repository)
    a = await _root_attempt(repository, root, "provider_a", _upload("provider_a", CachePresence.MISS), first=True)
    b = await _root_attempt(repository, root, "provider_b", _upload("provider_b", CachePresence.HIT, "nb"), first=False)
    c = await _root_attempt(repository, root, "provider_c", ResolutionResult(ResourceState.UNKNOWN), first=False)
    routes = _routes(await repository.presentation(transfer.id, details=True))
    # B's HIT is B's statement: it must not relabel A's (or C's) route.
    assert routes[a.id]["route_identity"] == "BitTorrent"
    assert routes[b.id]["route_identity"] == "Torrent cache"
    assert routes[c.id]["route_identity"] == "BitTorrent"


async def test_multi_root_transfer_computes_source_class_and_cache_per_root_lineage(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "route-multi-root.sqlite3")
    transfer, created = await repository.admit((
        TransferRequest("magnet", "magnet:?xt=urn:btih:" + "1" * 40, name="cached"),
        TransferRequest("torrent", b"d4:infod4:name1:xee", name="uncached.torrent"),
        TransferRequest("https", "https://direct.example/plain.bin", name="plain.bin"),
    ), name="multi", deduplicate=False)
    roots = [item for item in await repository.requests(transfer.id) if item.parent_id is None]
    assert [r.request.kind for r in roots] == ["magnet", "torrent", "https"]
    hit_root, miss_root, direct_root = roots
    hit = await _root_attempt(repository, hit_root, "alldebrid", _upload("alldebrid", CachePresence.HIT, "h"), first=True)
    miss = await _root_attempt(repository, miss_root, "alldebrid", _upload("alldebrid", CachePresence.MISS, "m"), first=True)
    direct = await _resolve(repository, direct_root, "general_http", (TransferCandidate(
        name="plain.bin", endpoints=(Endpoint("https", "https://direct.example/plain.bin"),),
        provider_id="general_http", id="plain"),))
    hit_children = await _children(repository, hit_root, 1)
    miss_children = await _children(repository, miss_root, 1)
    hit_child = await _child_attempt(repository, hit_children[0])
    miss_child = await _child_attempt(repository, miss_children[0])
    routes = _routes(await repository.presentation(transfer.id, details=True))
    # Neither "the first request in the transfer" nor the last observation decides.
    assert routes[hit.id]["route_identity"] == routes[hit_child.id]["route_identity"] == "Torrent cache"
    assert routes[miss.id]["route_identity"] == routes[miss_child.id]["route_identity"] == "BitTorrent"
    assert routes[direct.id]["route_identity"] == "https://direct.example"


async def test_unproven_lineage_is_never_guessed_as_bittorrent(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "route-orphan.sqlite3")
    transfer, root = await _torrent_root(repository)
    await _root_attempt(repository, root, "alldebrid", _upload("alldebrid", CachePresence.HIT), first=True)
    children = await _children(repository, root, 1)
    attempt = await _child_attempt(repository, children[0])
    async with database.get_db() as db:      # sever the parent chain: lineage unprovable
        await db.execute("PRAGMA foreign_keys=OFF")
        await db.execute("UPDATE transfer_requests SET parent_id='missing-parent' WHERE id=?", (children[0].id,))
        await db.commit()
    item = _routes(await repository.presentation(transfer.id, details=True))[attempt.id]
    # No proven BitTorrent root: the provider-issued child falls back to its own
    # (fail-closed) rule -- its attested source host, never a guessed torrent label.
    assert item["route_identity"] == "alldebrid.com"
    assert item["route_identity"] not in {"BitTorrent", "Torrent cache"}
    assert item["route_origin"] is None and "debrid.it" not in str(item)


# --- 10.2 / 10.7 historical stability --------------------------------------- #

async def test_initial_miss_stays_bittorrent_after_the_provider_later_reports_ready(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "route-later-ready.sqlite3")
    transfer, root = await _torrent_root(repository)
    miss = await _root_attempt(repository, root, "alldebrid", _upload("alldebrid", CachePresence.MISS), first=True)
    children = await _children(repository, root, 1)
    child = await _child_attempt(repository, children[0])
    before = await repository.presentation(transfer.id, details=True)
    assert {_routes(before)[a.id]["route_identity"] for a in (miss, child)} == {"BitTorrent"}

    # Later the provider reports the very same torrent ready (status polling):
    # readiness changes; the historical acquisition fact does not.
    provider_resource = (await repository.requests(transfer.id))[0].resource
    ready = ProviderObservation(provider_resource, ResourceState.AVAILABLE, "payload")          # statusCode==4 style
    assert ready.cache_presence == CachePresence.UNKNOWN
    later = await _root_attempt(repository, root, "alldebrid", ResolutionResult(ResourceState.AVAILABLE, observation=ready), first=False)
    after = await repository.presentation(transfer.id, details=True)
    assert [r["state"] for r in after["resources"]] == ["available"]                            # readiness moved...
    routes = _routes(after)
    assert {routes[a.id]["route_identity"] for a in (miss, child, later)} == {"BitTorrent"}     # ...history did not
    for attempt in (miss, child):
        assert routes[attempt.id]["route_identity"] == _routes(before)[attempt.id]["route_identity"]


async def test_provider_configuration_changes_never_rewrite_historical_labels(tmp_path, monkeypatch):
    from transfers.registry import IntegrationRegistry

    repository = await _repository(tmp_path, monkeypatch, "route-config-stable.sqlite3")
    registry = IntegrationRegistry()
    provider = AllDebridProvider(client=AsyncMock())
    registry.register_provider(provider)
    transfer, root = await _torrent_root(repository)
    hit = await _root_attempt(repository, root, "alldebrid", _upload("alldebrid", CachePresence.HIT), first=True)
    hoster_transfer, hoster_record = await _admit(repository, TransferRequest("https", "https://1fichier.com/?q", name="file.bin"))
    hoster = await _resolve(repository, hoster_record, "alldebrid", (_hoster_candidate(),))

    def snapshot(a, b):
        return ([(i["id"], i["route_identity"], i["route_origin"], i["route_location"]) for i in a["route_attempts"]],
                [(i["id"], i["route_identity"], i["route_origin"], i["route_location"]) for i in b["route_attempts"]])
    before = snapshot(await repository.presentation(transfer.id, details=True),
                      await repository.presentation(hoster_transfer.id, details=True))
    assert [row[1] for row in before[0]] == ["Torrent cache"] and [row[1] for row in before[1]] == ["1fichier.com"]

    # Disable, mark unhealthy, and even replace the provider's descriptor: history reads durable facts only.
    provider.descriptor = replace(provider.descriptor, enabled=False)
    registry.mark_health("alldebrid", healthy=False)
    monkeypatch.setattr(IntegrationRegistry, "provider_for", lambda *a, **k: (_ for _ in ()).throw(AssertionError("registry consulted")))
    after = snapshot(await repository.presentation(transfer.id, details=True),
                     await repository.presentation(hoster_transfer.id, details=True))
    assert after == before
    assert hit.id == before[0][0][0]


# --------------------------------------------------------------------------- #
# DP 1.0.12 consolidation corrective, Remediation 5: canonical-object Route
# History -- original + verified consolidated + unverified associated sources,
# each with its real contributing transfer, projected from durable provenance.
# --------------------------------------------------------------------------- #

_ISO = "ubuntu-24.04.3-desktop-amd64.iso"
_SECRET = "SECRET-SIGNED-TOKEN"


class _HttpsExecutor(MemoryExecutor):
    descriptor = IntegrationDescriptor(
        "memory-copy", "Memory copy", frozenset({Capability.PAUSE, Capability.RESUME, Capability.RECONCILE}),
        schemes=frozenset({"https"}),
    )


class _MirrorProvider(ParcelProvider):
    """One independent mirror host per provider: a real HTTPS route whose URL
    carries a credential-bearing query the projection must never expose."""

    def __init__(self, host):
        super().__init__(f"mirror:{host}")
        self.host = host

    def candidate(self, name=_ISO, *, payload="parcel"):
        return replace(
            super().candidate(name, payload=payload), expected_bytes=0,
            endpoints=(Endpoint("https", f"https://{self.host}/releases/{name}?token={_SECRET}"),),
            source_identity=SourceIdentity("host", self.host),
        )


async def _canonical_history_runtime(tmp_path, monkeypatch, hosts, *, unresolved=()):
    """``unresolved`` is either hosts (answering ``range_ignored``) or a
    ``{host: reason}`` mapping. Every fingerprint acquisition is logged by
    host in ``runtime.probes``; ``runtime.now`` is the engine clock."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "canonical-route-history.sqlite3")
    await database.init_db()
    repository = RecoveryTransferRepository()
    registry = IntegrationRegistry()
    executor = _HttpsExecutor(repository.authorize_execution)
    providers = {host: _MirrorProvider(host) for host in hosts}
    reasons = dict(unresolved) if isinstance(unresolved, dict) else {host: "range_ignored" for host in unresolved}
    host_of = {provider.descriptor.id: host for host, provider in providers.items()}
    probes, now = [], [1000.0]

    async def fingerprint(candidate):
        host = host_of[candidate.provider_id]
        probes.append(host)
        if host in reasons:
            return ArtifactFingerprint(0, "", kind=FingerprintKind.UNAVAILABLE, reason=reasons[host])
        return ArtifactFingerprint(4, f"full:{candidate.name}", FingerprintKind.FULL_CONTENT_SAMPLE)

    monkeypatch.setattr(executor, "fingerprint", fingerprint)
    for provider in providers.values():
        registry.register_provider(provider)
    registry.register_executor(executor)
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0, max_active_executions=8,
                              resolution_concurrency=8),
        clock=lambda: now[0],
    )
    await engine.initialize()

    async def submit(*batch, name=_ISO):
        transfer = await engine.submit(
            tuple(TransferRequest("parcel", f"{host}/{name}", name=name,
                                  preferred_provider=providers[host].descriptor.id) for host in batch),
            name=name, deduplicate=False,
        )
        for record in await repository.requests(transfer.id):
            await engine._resolve(record)
        return transfer

    return SimpleNamespace(repository=repository, engine=engine, executor=executor, submit=submit, probes=probes, now=now)



async def test_canonical_object_route_history_projects_original_consolidated_and_unverified(tmp_path, monkeypatch):
    originals = ("releases.ubuntu.com", "mirrors.mit.edu", "mirror.pilotfiber.com")
    consolidated = ("mirrors.tuna.tsinghua.edu.cn", "mirror.sg.gs")
    runtime = await _canonical_history_runtime(
        tmp_path, monkeypatch, (*originals, *consolidated, "mirrors.163.com", "mirrors.aliyun.com", "unrelated.example"),
        unresolved=("mirrors.aliyun.com",),
    )
    unrelated = await runtime.submit("unrelated.example", name="unrelated-object.bin")  # an unrelated canonical object.
    owner = await runtime.submit(*originals)
    second = await runtime.submit(*consolidated)
    third = await runtime.submit("mirrors.163.com", "mirrors.aliyun.com")
    assert (await runtime.repository.get(second.id)).state.value == "consolidated"
    assert (await runtime.repository.get(third.id)).state.value == "consolidated"

    presentation = await runtime.repository.presentation(owner.id, details=True)
    rows = presentation["route_attempts"]
    assert [(row["route_identity"], row["relation"], row["verification_state"], row["contributing_transfer_id"])
            for row in rows] == [
        ("https://releases.ubuntu.com", "original", "verified", owner.id),
        ("https://mirrors.mit.edu", "original", "verified", owner.id),
        ("https://mirror.pilotfiber.com", "original", "verified", owner.id),
        ("https://mirrors.tuna.tsinghua.edu.cn", "consolidated", "verified", second.id),
        ("https://mirror.sg.gs", "consolidated", "verified", second.id),
        ("https://mirrors.163.com", "consolidated", "verified", third.id),
        ("https://mirrors.aliyun.com", "unverified", "unverified", third.id),
    ]
    assert len({row["id"] for row in rows}) == len(rows)  # every source exactly once: no duplicate rows.
    assert [row["unverified_reason"] for row in rows] == [None] * 6 + ["range_ignored"]  # the factual reason.
    # Durable ordinals are never renumbered: each row keeps its OWN transfer's ordinal; only the dedicated
    # presentation ordinal sequences the combined history.
    assert [row["ordinal"] for row in rows] == [1, 2, 3, 1, 2, 1, 2]
    assert [row["presentation_ordinal"] for row in rows] == [1, 2, 3, 4, 5, 6, 7]
    assert _SECRET not in str(rows) and "token=" not in str(rows)  # existing safety projection still applies.
    assert "unrelated.example" not in str(rows)  # an unrelated canonical object contributes nothing.

    # "N Candidates" still means VERIFIED candidates: the unverified source is not one of them.
    bindings = presentation["candidate_bindings"]
    assert len(bindings) == 6
    assert {binding["source_identity"]["key"] for binding in bindings} == {*originals, *consolidated, "mirrors.163.com"}

    # A contributing transfer's own Details still shows ITS routes as its own (never rewritten onto the owner),
    # with the unverified one marked as such.
    third_rows = (await runtime.repository.presentation(third.id, details=True))["route_attempts"]
    assert [(row["route_identity"], row["relation"], row["contributing_transfer_id"]) for row in third_rows] == [
        ("https://mirrors.163.com", "original", third.id),
        ("https://mirrors.aliyun.com", "unverified", third.id),
    ]
    unrelated_rows = (await runtime.repository.presentation(unrelated.id, details=True))["route_attempts"]
    assert [(row["relation"], row["contributing_transfer_id"]) for row in unrelated_rows] == [("original", unrelated.id)]

    # The list path never pays for any of this.
    assert "route_attempts" not in await runtime.repository.presentation(owner.id, details=False)


async def test_contributed_route_provenance_follows_durable_origin_not_current_candidates(tmp_path, monkeypatch):
    """The projection reads ``canonical_candidate_origins`` / the durable
    ``unverified`` association -- never the canonical artifact's CURRENT
    candidate URLs. Rewriting every current candidate to a misleading host
    changes nothing; removing the durable origin removes the row."""
    runtime = await _canonical_history_runtime(
        tmp_path, monkeypatch, ("releases.ubuntu.com", "mirrors.tuna.tsinghua.edu.cn"),
    )
    owner = await runtime.submit("releases.ubuntu.com")
    second = await runtime.submit("mirrors.tuna.tsinghua.edu.cn")
    expected = [("https://releases.ubuntu.com", "original", owner.id),
                ("https://mirrors.tuna.tsinghua.edu.cn", "consolidated", second.id)]

    def reading(presentation):
        return [(row["route_identity"], row["relation"], row["contributing_transfer_id"])
                for row in presentation["route_attempts"]]

    assert reading(await runtime.repository.presentation(owner.id, details=True)) == expected
    async with database.get_db() as db:
        row = await db.fetchone("SELECT id,candidates FROM download_files WHERE torrent_id=?", (owner.id,))
        await db.execute(
            "UPDATE download_files SET candidates=? WHERE id=?",
            (row["candidates"].replace("mirrors.tuna.tsinghua.edu.cn", "misleading.example")
                              .replace("releases.ubuntu.com", "misleading.example"), row["id"]),
        )
        await db.commit()
    assert reading(await runtime.repository.presentation(owner.id, details=True)) == expected

    async with database.get_db() as db:
        await db.execute("DELETE FROM canonical_candidate_origins WHERE contributing_transfer_id=?", (second.id,))
        await db.commit()
    assert reading(await runtime.repository.presentation(owner.id, details=True)) == expected[:1]


# --------------------------------------------------------------------------- #
# DP 1.0.12 Details Files canonical-object presentation leveling: the Details
# Files card presents the whole canonical-object source/file story -- this
# transfer's own physical artifacts, the verified artifacts other transfers
# contributed to the canonical object, and the terminal UNVERIFIED associations
# that have no artifact at all -- while ``files[]``/``file_count`` stay exactly
# what they have always been: physical transfer-local artifact truth.
# --------------------------------------------------------------------------- #


async def _settle_unverified(runtime, transfer_id, *, expected, budget=8):
    """Drive the REAL resolution cycle until every held proof retry of
    ``transfer_id`` has durably exhausted into its terminal disposition.

    Bounded and deterministic: the proof retry budget is finite, so a fixed
    number of cycles is an upper bound, never a retry-until-green loop. The
    terminal state is asserted, not hoped for.
    """
    for _ in range(budget):
        async with database.get_db() as db:
            held = await db.fetchall(
                """SELECT id FROM transfer_requests
                    WHERE transfer_id=? AND state='materializing' AND equivalence_disposition='pending'""",
                (transfer_id,),
            )
        if not held:
            break
        runtime.now[0] += 60
        await runtime.engine.resolve_pending()
    async with database.get_db() as db:
        rows = await db.fetchall(
            """SELECT id,equivalence_reason FROM transfer_requests
                WHERE transfer_id=? AND equivalence_disposition='unverified'
                ORDER BY ordinal,id""",
            (transfer_id,),
        )
    assert [row["equivalence_reason"] for row in rows] == list(expected)
    return [row["id"] for row in rows]


async def _canonical_object_runtime(tmp_path, monkeypatch):
    """The production 303/304/305 shape, built only by the real engine.

    owner: 3 native physical artifacts, owns the canonical artifact.
    second: 3 verified contributed artifacts.
    third: 2 verified contributed artifacts + 2 terminal UNVERIFIED associations.
    """
    originals = ("releases.ubuntu.com", "mirrors.mit.edu", "mirror.pilotfiber.com")
    second_hosts = ("mirrors.tuna.tsinghua.edu.cn", "mirror.sg.gs", "mirrors.kernel.org")
    third_hosts = ("mirrors.163.com", "mirror.rackspace.com", "held-a.example", "held-b.example")
    runtime = await _canonical_history_runtime(
        tmp_path, monkeypatch, (*originals, *second_hosts, *third_hosts),
        unresolved={"held-a.example": "range_ignored", "held-b.example": "range_unsupported"},
    )
    runtime.owner = await runtime.submit(*originals)
    runtime.second = await runtime.submit(*second_hosts)
    runtime.third = await runtime.submit(*third_hosts)
    runtime.held = await _settle_unverified(
        runtime, runtime.third.id, expected=("range_ignored", "range_unsupported"),
    )
    return runtime


async def test_details_files_present_the_canonical_object_not_only_physical_artifacts(tmp_path, monkeypatch):
    runtime = await _canonical_object_runtime(tmp_path, monkeypatch)
    owner, second, third = runtime.owner, runtime.second, runtime.third
    presentation = await runtime.repository.presentation(owner.id, details=True)

    # --- Physical artifact truth is untouched ------------------------------- #
    assert presentation["file_count"] == 3
    assert len(presentation["files"]) == 3
    physical = [int(item["id"]) for item in presentation["files"]]
    canonical_id = physical[0]
    assert [item["candidate_count"] for item in presentation["files"]] == [8, 0, 0]
    assert len(presentation["route_attempts"]) == 10

    # --- The Details-only canonical-object Files presentation --------------- #
    rows = presentation["file_presentations"]
    assert len(rows) == 10
    assert [row["relationship"] for row in rows] == ["original"] * 3 + ["consolidated"] * 5 + ["unverified"] * 2
    assert [row["verification_state"] for row in rows] == ["verified"] * 8 + ["unverified"] * 2
    assert [row["contributing_transfer_id"] for row in rows] == (
        [owner.id] * 3 + [second.id] * 3 + [third.id] * 2 + [third.id] * 2
    )
    # Deterministic, backend-owned ordering and identity: native artifacts in durable
    # artifact order, contributed artifacts in durable binding/origin order, terminal
    # associations in durable request order.
    assert [row["presentation_id"] for row in rows] == (
        [f"artifact:{artifact_id}" for artifact_id in physical]
        + [f"artifact:{row['artifact_id']}" for row in rows[3:8]]
        + [f"request:{request_id}" for request_id in runtime.held]
    )
    assert len({row["presentation_id"] for row in rows}) == 10

    # --- Only the canonical actionable row owns the candidate surface -------- #
    native, contributed, unverified = rows[:3], rows[3:8], rows[8:]
    assert native[0]["artifact_id"] == canonical_id
    assert native[0]["candidate_count"] == 8
    assert len(native[0]["acquisition_candidates"]) == 8
    for row in (*contributed, *unverified):
        assert not row.get("candidate_count")
        assert not row.get("acquisition_candidates")
        assert not row.get("source_candidates")

    # --- Verified contributed rows are REAL foreign artifacts ---------------- #
    async with database.get_db() as db:
        foreign = {
            int(item["id"]): item for item in await db.fetchall(
                "SELECT id,torrent_id,filename,size_bytes,status FROM download_files WHERE torrent_id IN (?,?)",
                (second.id, third.id),
            )
        }
    for row in contributed:
        artifact = foreign[int(row["artifact_id"])]
        assert (row["filename"], row["size_bytes"], row["status"]) == (
            artifact["filename"], artifact["size_bytes"], artifact["status"])
        assert int(artifact["torrent_id"]) == row["contributing_transfer_id"]
        # Rendered exactly like a native duplicate: the one presentation owner, not a literal.
        assert row["presentation_label"] == "Duplicate"

    # --- Terminal UNVERIFIED associations: no artifact, no borrowed size ----- #
    assert [row["artifact_id"] for row in unverified] == [None, None]
    assert [row["unverified_reason"] for row in unverified] == ["range_ignored", "range_unsupported"]
    assert [row["size_bytes"] for row in unverified] == [None, None]
    assert [row["presentation_label"] for row in unverified] == ["Unverified", "Unverified"]
    # The canonical artifact has a real size; an unverified association must never borrow it.
    assert int(presentation["files"][0]["size_bytes"]) > 0
    # Its name is the durable request's own name, never a URL, host or filename guess.
    assert {row["filename"] for row in unverified} == {_ISO}

    # --- The list path never pays for any of this ---------------------------- #
    assert "file_presentations" not in await runtime.repository.presentation(owner.id, details=False)


async def test_details_files_presentation_follows_durable_provenance_only(tmp_path, monkeypatch):
    """Contributed rows come from canonical_candidate_bindings/origins and the
    terminal association from the durable equivalence record -- never from Route
    History, a current candidate URL, a host or a filename."""
    runtime = await _canonical_object_runtime(tmp_path, monkeypatch)
    owner, second, third = runtime.owner, runtime.second, runtime.third

    def reading(presentation):
        return [(row["relationship"], row["contributing_transfer_id"]) for row in presentation["file_presentations"]]

    assert reading(await runtime.repository.presentation(owner.id, details=True)) == (
        [("original", owner.id)] * 3 + [("consolidated", second.id)] * 3
        + [("consolidated", third.id)] * 2 + [("unverified", third.id)] * 2
    )

    # Rewriting every current candidate to a misleading host changes nothing.
    async with database.get_db() as db:
        row = await db.fetchone("SELECT id,candidates FROM download_files WHERE torrent_id=?", (owner.id,))
        await db.execute(
            "UPDATE download_files SET candidates=? WHERE id=?",
            (str(row["candidates"]).replace("mirrors.tuna.tsinghua.edu.cn", "misleading.example"), row["id"]),
        )
        await db.commit()
    assert reading(await runtime.repository.presentation(owner.id, details=True)) == (
        [("original", owner.id)] * 3 + [("consolidated", second.id)] * 3
        + [("consolidated", third.id)] * 2 + [("unverified", third.id)] * 2
    )

    # Removing the durable origin removes exactly those contributed rows; the
    # terminal associations, which have no binding at all, are unaffected.
    async with database.get_db() as db:
        await db.execute("DELETE FROM canonical_candidate_origins WHERE contributing_transfer_id=?", (second.id,))
        await db.commit()
    assert reading(await runtime.repository.presentation(owner.id, details=True)) == (
        [("original", owner.id)] * 3 + [("consolidated", third.id)] * 2 + [("unverified", third.id)] * 2
    )

    # Removing the durable equivalence association removes exactly the terminal rows.
    async with database.get_db() as db:
        await db.execute(
            "UPDATE transfer_requests SET equivalence_target_artifact_id=NULL WHERE transfer_id=?", (third.id,))
        await db.commit()
    assert reading(await runtime.repository.presentation(owner.id, details=True)) == (
        [("original", owner.id)] * 3 + [("consolidated", third.id)] * 2
    )


async def test_ordinary_transfer_details_files_presentation_is_just_its_own_artifacts(tmp_path, monkeypatch):
    """No contribution, no association: the canonical presentation is exactly the
    physical collection, so an ordinary transfer's Details is unchanged."""
    runtime = await _canonical_history_runtime(tmp_path, monkeypatch, ("releases.ubuntu.com",))
    transfer = await runtime.submit("releases.ubuntu.com")
    presentation = await runtime.repository.presentation(transfer.id, details=True)
    rows = presentation["file_presentations"]
    assert len(rows) == len(presentation["files"]) == presentation["file_count"] == 1
    assert [row["relationship"] for row in rows] == ["original"]
    assert rows[0]["artifact_id"] == presentation["files"][0]["id"]
    assert rows[0]["presentation_id"] == f"artifact:{presentation['files'][0]['id']}"
