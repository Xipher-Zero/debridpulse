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
    SourceIdentity,
    TransferCandidate,
    TransferProgress,
    TransferRequest,
)
from transfers.presentation_repository import TransferRepository as PresentationTransferRepository
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
