from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
repo = ROOT / "backend/transfers/repository.py"
maintenance = ROOT / "backend/services/db_maintenance.py"
tests = ROOT / "backend/tests/test_route_provider_provenance.py"


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, found {count}")
    path.write_text(text.replace(old, new, 1))


# Completed summaries may claim only a provider backed by verified delivery.
replace_once(
    repo,
    '        result["providers"] = delivering_providers if result["status"] == "completed" and delivering_providers else historical_providers\n',
    '        result["providers"] = delivering_providers if result["status"] == "completed" else historical_providers\n',
    "completed-provider projection",
)

# Route attempt order is a logical-transfer property. Do not depend on timestamps
# or reset ordering merely because a transfer contains multiple requests.
replace_once(
    repo,
    "        UNIQUE(request_id,ordinal))\"\"\",\n",
    "        UNIQUE(transfer_id,ordinal))\"\"\",\n",
    "route provenance unique ordering",
)
replace_once(
    repo,
    '            ORDER BY r.transfer_id,a.request_id,a.created_at,a.id""")\n',
    '            ORDER BY r.transfer_id,a.created_at,a.id""")\n',
    "legacy route ordering",
)
replace_once(
    repo,
    '            ordinal_row = await db.fetchone("SELECT COALESCE(MAX(ordinal),0) AS n FROM route_attempt_provenance WHERE request_id=?", (row["request_id"],))\n',
    '            ordinal_row = await db.fetchone("SELECT COALESCE(MAX(ordinal),0) AS n FROM route_attempt_provenance WHERE transfer_id=?", (row["transfer_id"],))\n',
    "legacy transfer-wide ordinal",
)
replace_once(
    repo,
    '''        previous = await db.fetchone("""SELECT a.id,a.provider_id,a.error,p.ordinal,p.outcome
            FROM resolution_attempts a JOIN route_attempt_provenance p ON p.resolution_attempt_id=a.id
            WHERE a.request_id=? AND a.id!=? ORDER BY p.ordinal DESC LIMIT 1""", (request_id, attempt_id))
        ordinal = int(previous["ordinal"] or 0) + 1 if previous else 1
''',
    '''        previous = await db.fetchone("""SELECT a.id,a.provider_id,a.error,p.ordinal,p.outcome
            FROM resolution_attempts a JOIN route_attempt_provenance p ON p.resolution_attempt_id=a.id
            WHERE a.request_id=? AND a.id!=? ORDER BY p.ordinal DESC LIMIT 1""", (request_id, attempt_id))
        ordinal_row = await db.fetchone(
            "SELECT COALESCE(MAX(ordinal),0) AS n FROM route_attempt_provenance WHERE transfer_id=?", (transfer_id,)
        )
        ordinal = int(ordinal_row["n"] or 0) + 1
''',
    "new route transfer-wide ordinal",
)
replace_once(
    repo,
    '                ORDER BY p.created_at,p.request_id,p.ordinal,p.resolution_attempt_id""", (transfer_id,))\n',
    '                ORDER BY p.ordinal,p.resolution_attempt_id""", (transfer_id,))\n',
    "presentation route ordering",
)

# Backups and explicit database wipe must own the new canonical provenance rows.
replace_once(
    maintenance,
    '''    "provider_resources",
    "resolution_attempts",
    "execution_attempts",
    "transfer_outcomes",
''',
    '''    "provider_resources",
    "resolution_attempts",
    "route_attempt_provenance",
    "execution_attempts",
    "execution_attempt_provenance",
    "transfer_outcomes",
''',
    "database backup table ownership",
)
replace_once(
    maintenance,
    '''    "provider_resources": "id",
    "resolution_attempts": "id",
    "execution_attempts": "id",
    "transfer_outcomes": "id",
''',
    '''    "provider_resources": "id",
    "resolution_attempts": "id",
    "route_attempt_provenance": "transfer_id,ordinal",
    "execution_attempts": "id",
    "execution_attempt_provenance": "transfer_id,artifact_id,ordinal",
    "transfer_outcomes": "id",
''',
    "database backup provenance ordering",
)
replace_once(
    maintenance,
    '''        for table in ("application_events", "postprocess_attempts", "transfer_outcomes", "execution_attempts", "resolution_attempts", "provider_resources"):
            await db.execute(f"DELETE FROM {table}")
''',
    '''        for table in (
            "application_events", "postprocess_attempts", "transfer_outcomes",
            "execution_attempt_provenance", "route_attempt_provenance",
            "execution_attempts", "resolution_attempts", "provider_resources",
        ):
            await db.execute(f"DELETE FROM {table}")
''',
    "database wipe provenance dependency order",
)

addition = r'''
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
    assert [item["ordinal"] for item in mid["route_attempts"]] == [1, 2]
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


async def test_route_attempt_ordinals_are_transfer_wide_across_multiple_requests(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "transfer-order.sqlite3")
    requests = (
        TransferRequest("https", "https://one.example/file", name="one.bin"),
        TransferRequest("https", "https://two.example/file", name="two.bin"),
    )
    transfer, created = await repository.admit(requests, name="multi", deduplicate=False)
    assert created
    first, second = await repository.requests(transfer.id)
    a = await repository.begin_resolution(first.id, "provider_a")
    b = await repository.begin_resolution(second.id, "provider_b")
    assert a is not None and b is not None
    await repository.resolution(a, ResolutionResult(ResourceState.AVAILABLE, (_candidate("provider_a", "one"),)))
    await repository.resolution(b, ResolutionResult(ResourceState.AVAILABLE, (_candidate("provider_b", "two"),)))

    presentation = await repository.presentation(transfer.id, details=True)
    assert [item["id"] for item in presentation["route_attempts"]] == [a.id, b.id]
    assert [item["ordinal"] for item in presentation["route_attempts"]] == [1, 2]
    assert len({item["request_id"] for item in presentation["route_attempts"]}) == 2


async def test_current_alldebrid_host_state_cannot_rewrite_completed_direct_history(tmp_path, monkeypatch):
    from integrations.runtime_state import ProviderRuntimeStateStore
    from providers.alldebrid.host_runtime import (
        HOST_SCHEMA_VERSION,
        HOST_STATE_KEY,
        encode_host_snapshot,
        parse_native_host_snapshot,
    )

    repository = await _repository(tmp_path, monkeypatch, "host-history.sqlite3")
    provider = GeneralHttpProvider()
    request = TransferRequest("https", "https://host-change.example/file.bin", name="file.bin")
    transfer, record = await _admit(repository, request)
    attempt = await repository.begin_resolution(record.id, provider.descriptor.id)
    result = await provider.resolve(request)
    await repository.resolution(attempt, result)
    record = (await repository.requests(transfer.id))[0]
    await _materialize_and_execute(repository, record, result.candidates[0], attempt_id="direct-before-host-change")
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
    from services.stats import collect_all_metrics

    repository = await _repository(tmp_path, monkeypatch, "stats.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://stats.example/file", name="stats.bin"))
    failed = NormalizedError(Domain.PROVIDER, Category.PROVIDER_UNAVAILABLE, Stage.RESOLUTION, retryability=Retryability.NEVER)
    await _resolve(repository, record, "provider_a", error=failed)
    await repository.retry_requests(transfer.id, request_id=record.id)
    record = (await repository.requests(transfer.id))[0]
    candidate_b = _candidate("provider_b", "stats-candidate")
    await _resolve(repository, record, "provider_b", (candidate_b,))
    record = (await repository.requests(transfer.id))[0]
    await _materialize_and_execute(repository, record, candidate_b, attempt_id="stats-execution")
    await _force_completed(transfer.id)

    metrics = await collect_all_metrics()
    assert metrics["torrents"]["total"] == 1
    assert metrics["torrents"]["completed"] == 1
    assert metrics["torrents"]["errors"] == 0
    presentation = await repository.presentation(transfer.id, details=True)
    assert [item["outcome"] for item in presentation["route_attempts"]] == ["failed", "completed"]


async def test_provenance_schema_is_provider_neutral_and_separate_from_logical_identity(tmp_path, monkeypatch):
    repository = await _repository(tmp_path, monkeypatch, "architecture.sqlite3")
    transfer, record = await _admit(repository, TransferRequest("https", "https://schema.example/file", name="schema.bin"))
    candidate = _candidate("arbitrary_provider_name", "schema-candidate")
    route = await _resolve(repository, record, "arbitrary_provider_name", (candidate,))
    record = (await repository.requests(transfer.id))[0]
    await _materialize_and_execute(repository, record, candidate, attempt_id="schema-execution")

    async with database.get_db() as db:
        table_rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        route_row = await db.fetchone(
            "SELECT transfer_id,resolution_attempt_id FROM route_attempt_provenance WHERE resolution_attempt_id=?", (route.id,)
        )
    names = {row["name"] for row in table_rows}
    assert "route_attempt_provenance" in names
    assert "execution_attempt_provenance" in names
    assert not any("alldebrid" in name or "general_http" in name for name in names if "provenance" in name)
    assert route_row["transfer_id"] == transfer.id
    assert str(route_row["resolution_attempt_id"]) != str(transfer.id)
'''
content = tests.read_text()
marker = "test_completed_without_proven_delivery_does_not_promote_historical_provider"
if marker not in content:
    tests.write_text(content.rstrip() + "\n\n" + addition.strip() + "\n")

print("Stage 9 audit corrections applied")
