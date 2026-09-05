"""Bounded real-world equivalence proof retry and diagnostic remediation tests."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.engine import TransferEngine
from transfers.mirrors import EvidenceFailureClass, EvidenceKind, shared_evidence
from transfers.models import ArtifactFingerprint, FingerprintKind, ResolutionResult, ResourceState, TransferRequest
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


class BatchProvider(ParcelProvider):
    async def resolve(self, request):
        self.calls.append(("resolve", request.payload))
        name = request.name or "payload.bin"
        return ResolutionResult(
            ResourceState.AVAILABLE,
            (self.candidate(name, payload=f"payload:{name}"),),
        )


@pytest_asyncio.fixture
async def retry_pair(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    first = BatchProvider("provider-a")
    second = BatchProvider("provider-b")
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(first)
    registry.register_provider(second)
    registry.register_executor(executor)
    now = [1000.0]
    engine = TransferEngine(
        repository,
        registry,
        download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(
            retry_delay=1,
            adoption_stability_seconds=0,
            max_active_executions=32,
            resolution_concurrency=32,
        ),
        clock=lambda: now[0],
    )
    await engine.initialize()
    return SimpleNamespace(
        engine=engine, repository=repository, registry=registry,
        a=first, b=second, executor=executor, now=now,
    )


async def _submit_batch(pair, provider, prefix):
    requests = tuple(
        TransferRequest(
            "parcel", f"{prefix}-{index}", name=f"part{index}.rar",
            preferred_provider=provider.descriptor.id,
        )
        for index in range(1, 8)
    )
    return await pair.engine.submit(requests, name=prefix, deduplicate=False)


def _prefix(candidate):
    signature = f"prefix:{candidate.name.casefold()}"
    return ArtifactFingerprint(
        candidate.expected_bytes, signature,
        FingerprintKind.PREFIX_CONTENT_SAMPLE, "range_ignored", signature,
    )


def _unavailable(reason):
    return ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, reason, "")


async def _proof_rows(transfer_id):
    async with database.get_db() as db:
        return await db.fetchall(
            """SELECT id,state,retry_at,equivalence_retry_count,equivalence_reason,equivalence_disposition
                FROM transfer_requests WHERE transfer_id=?
                AND NOT EXISTS(SELECT 1 FROM transfer_requests child WHERE child.parent_id=transfer_requests.id)
                ORDER BY ordinal,id""",
            (transfer_id,),
        )


@pytest.mark.asyncio
async def test_seven_plus_seven_two_transient_failures_recover_to_full_consolidation(retry_pair, monkeypatch):
    first = await _submit_batch(retry_pair, retry_pair.a, "rapidgator")
    await retry_pair.engine.tick()
    assert len(await retry_pair.repository.artifacts(first.id)) == 7
    assert len([call for call in retry_pair.executor.calls if call[0] == "start"]) == 7

    seen = {"part1.rar": 0, "part3.rar": 0}

    async def flaky(candidate):
        if candidate.provider_id == retry_pair.b.descriptor.id and candidate.name in seen:
            seen[candidate.name] += 1
            if seen[candidate.name] == 1:
                return _unavailable("timeout" if candidate.name == "part1.rar" else "sampler_unavailable")
        return _prefix(candidate)

    monkeypatch.setattr(retry_pair.executor, "fingerprint", flaky)
    second = await _submit_batch(retry_pair, retry_pair.b, "1fichier")
    await retry_pair.engine.resolve_pending()

    # The transient members hold the entire weak-evidence cohort before any
    # second writer can be allocated.
    assert len(await retry_pair.repository.artifacts(second.id)) == 0
    assert len([call for call in retry_pair.executor.calls if call[0] == "start"]) == 7
    rows = await _proof_rows(second.id)
    pending = {row["equivalence_reason"] for row in rows if row["equivalence_disposition"] == "pending"}
    assert {"timeout", "sampler_unavailable"}.issubset(pending)
    assert all(int(row["equivalence_retry_count"] or 0) <= 1 for row in rows)

    retry_pair.now[0] += 1.1
    await retry_pair.engine.resolve_pending()
    await retry_pair.engine.reconcile_executions()

    assert (await retry_pair.repository.get(second.id)).state.value == "consolidated"
    assert len(await retry_pair.repository.artifacts(second.id)) == 0
    canonicals = await retry_pair.repository.artifacts(first.id)
    assert len(canonicals) == 7
    assert all(len(item.candidates) == 2 for item in canonicals)
    assert len([call for call in retry_pair.executor.calls if call[0] == "start"]) == 7

    rows = await _proof_rows(second.id)
    by_reason = {row["equivalence_reason"]: row for row in rows if row["equivalence_reason"]}
    assert by_reason["timeout"]["equivalence_disposition"] == "recovered"
    assert by_reason["sampler_unavailable"]["equivalence_disposition"] == "recovered"
    assert all(int(row["equivalence_retry_count"] or 0) <= 2 for row in rows)


@pytest.mark.asyncio
async def test_persistent_transient_failure_exhausts_bound_and_releases_independently(retry_pair, monkeypatch):
    first = await _submit_batch(retry_pair, retry_pair.a, "rapidgator")
    await retry_pair.engine.tick()

    async def persistent(candidate):
        if candidate.provider_id == retry_pair.b.descriptor.id and candidate.name == "part1.rar":
            return _unavailable("timeout")
        return _prefix(candidate)

    monkeypatch.setattr(retry_pair.executor, "fingerprint", persistent)
    second = await _submit_batch(retry_pair, retry_pair.b, "1fichier")

    await retry_pair.engine.resolve_pending()
    assert len(await retry_pair.repository.artifacts(second.id)) == 0
    retry_pair.now[0] += 1.1
    await retry_pair.engine.resolve_pending()
    assert len(await retry_pair.repository.artifacts(second.id)) == 0
    retry_pair.now[0] += 1.1
    await retry_pair.engine.resolve_pending()
    await retry_pair.engine.reconcile_executions()

    assert (await retry_pair.repository.get(second.id)).state.value != "consolidated"
    assert len(await retry_pair.repository.artifacts(first.id)) == 7
    assert len(await retry_pair.repository.artifacts(second.id)) == 7
    assert len([call for call in retry_pair.executor.calls if call[0] == "start"]) == 14

    rows = await _proof_rows(second.id)
    failed = next(row for row in rows if row["equivalence_reason"] == "timeout")
    assert int(failed["equivalence_retry_count"]) == 2
    assert failed["equivalence_disposition"] == "exhausted"
    assert float(failed["retry_at"] or 0) == 0
    assert all(row["state"] != "materializing" for row in rows)


@pytest.mark.asyncio
async def test_restart_preserves_pending_retry_budget_and_writer_barrier(retry_pair, monkeypatch):
    first = await _submit_batch(retry_pair, retry_pair.a, "rapidgator")
    await retry_pair.engine.tick()

    seen = 0

    async def once(candidate):
        nonlocal seen
        if candidate.provider_id == retry_pair.b.descriptor.id and candidate.name == "part1.rar":
            seen += 1
            if seen == 1:
                return _unavailable("dns_failure")
        return _prefix(candidate)

    monkeypatch.setattr(retry_pair.executor, "fingerprint", once)
    second = await _submit_batch(retry_pair, retry_pair.b, "1fichier")
    await retry_pair.engine.resolve_pending()
    before = await _proof_rows(second.id)
    failed_before = next(row for row in before if row["equivalence_reason"] == "dns_failure")
    assert int(failed_before["equivalence_retry_count"]) == 1
    assert len([call for call in retry_pair.executor.calls if call[0] == "start"]) == 7

    restarted = TransferEngine(
        retry_pair.repository,
        retry_pair.registry,
        download_root=retry_pair.engine.root,
        policy=retry_pair.engine.policy,
        clock=lambda: retry_pair.now[0],
    )
    await restarted.initialize()

    # Before durable retry_at, restart does not create another writer or consume
    # another proof opportunity.
    await restarted.resolve_pending()
    unchanged = await _proof_rows(second.id)
    failed_unchanged = next(row for row in unchanged if row["equivalence_reason"] == "dns_failure")
    assert int(failed_unchanged["equivalence_retry_count"]) == 1
    assert len([call for call in retry_pair.executor.calls if call[0] == "start"]) == 7

    retry_pair.now[0] += 1.1
    await restarted.resolve_pending()
    await restarted.reconcile_executions()
    assert (await retry_pair.repository.get(second.id)).state.value == "consolidated"
    assert len(await retry_pair.repository.artifacts(second.id)) == 0
    assert len([call for call in retry_pair.executor.calls if call[0] == "start"]) == 7
    recovered = await _proof_rows(second.id)
    recovered_row = next(row for row in recovered if row["equivalence_reason"] == "dns_failure")
    assert recovered_row["equivalence_disposition"] == "recovered"
    assert int(recovered_row["equivalence_retry_count"]) == 1


@pytest.mark.asyncio
async def test_specific_unavailable_reason_beats_generic_pairing_placeholder(retry_pair, monkeypatch, caplog):
    left = retry_pair.a.candidate("same.rar", payload="a")
    right = retry_pair.b.candidate("same.rar", payload="b")

    async def timeout(candidate):
        if candidate.provider_id == retry_pair.b.descriptor.id:
            return _unavailable("timeout")
        return _prefix(candidate)

    monkeypatch.setattr(retry_pair.executor, "fingerprint", timeout)
    caplog.set_level("DEBUG")
    evidence = await shared_evidence(left, right, retry_pair.registry)
    assert evidence.kind == EvidenceKind.UNAVAILABLE
    assert evidence.reason == "timeout"
    assert evidence.failure_class == EvidenceFailureClass.TRANSIENT
    assert evidence.retryable
    assert "memory:" not in caplog.text
    assert "endpoint" not in caplog.text.casefold()


@pytest.mark.asyncio
async def test_size_and_content_contradictions_are_not_retryable(retry_pair, monkeypatch):
    left = retry_pair.a.candidate("same.rar", payload="a")
    right = replace(retry_pair.b.candidate("same.rar", payload="b"), expected_bytes=left.expected_bytes + 1)
    size = await shared_evidence(left, right, retry_pair.registry)
    assert size.reason == "size_disagreement"
    assert size.failure_class == EvidenceFailureClass.CONTRADICTORY
    assert not size.retryable

    async def different(candidate):
        signature = f"full:{candidate.provider_id}"
        return ArtifactFingerprint(
            candidate.expected_bytes, signature,
            FingerprintKind.FULL_CONTENT_SAMPLE, "", signature,
        )

    monkeypatch.setattr(retry_pair.executor, "fingerprint", different)
    mismatch = await shared_evidence(
        retry_pair.a.candidate("same.rar", payload="a2"),
        retry_pair.b.candidate("same.rar", payload="b2"),
        retry_pair.registry,
    )
    assert mismatch.reason == "sample_mismatch"
    assert mismatch.failure_class == EvidenceFailureClass.CONTRADICTORY
    assert not mismatch.retryable
