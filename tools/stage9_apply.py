from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
repo = ROOT / "backend/transfers/repository.py"
tests = ROOT / "backend/tests/test_route_provider_provenance.py"

text = repo.read_text()
old = '        result["providers"] = delivering_providers if result["status"] == "completed" and delivering_providers else historical_providers\n'
new = '        result["providers"] = delivering_providers if result["status"] == "completed" else historical_providers\n'
if old not in text:
    raise SystemExit("completed-provider projection pattern not found")
repo.write_text(text.replace(old, new, 1))

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
'''
content = tests.read_text()
marker = "test_completed_without_proven_delivery_does_not_promote_historical_provider"
if marker not in content:
    tests.write_text(content.rstrip() + addition + "\n")

print("Stage 9 audit corrections applied")
