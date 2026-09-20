"""Bounded real-world equivalence proof retry and diagnostic remediation tests."""
from dataclasses import replace
import inspect
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers import codec, cohorts
from transfers.engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Origin, Recovery, Retryability, Stage
from transfers.mirrors import EquivalenceEvidence, EvidenceFailureClass, EvidenceKind, shared_evidence
from transfers.models import (
    ArtifactFingerprint, ExecutionState, FingerprintKind, ResolutionResult, ResourceState, TransferRequest, TransferState,
)
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

    # DP 1.0.12 Section 11.6: cross-transfer mapping is preserved -- every
    # one of second's 7 durable requests is discoverable via
    # artifact_consolidations (this IS a genuine cross-transfer contribution)
    # and durable_owner_for_request agrees.
    async with database.get_db() as db:
        consolidation_count = await db.fetchone(
            "SELECT COUNT(*) AS n FROM artifact_consolidations WHERE source_transfer_id=?", (second.id,),
        )
    assert int(consolidation_count["n"]) == 7
    for record in rows:
        owner = await retry_pair.engine.canonical.durable_owner_for_request(record["id"])
        assert owner is not None
        assert owner in {item.id for item in canonicals}


@pytest.mark.asyncio
async def test_persistent_transient_failure_exhausts_bound_and_holds_unresolved(retry_pair, monkeypatch):
    """DP 1.0.12 canonical equivalence/lifecycle correction, Section 10.1:
    this test previously encoded the bug -- it asserted that ONE sibling
    persistently failing to acquire proof (a transient, retryable reason)
    exhausting the bounded retry budget caused the OTHER six, already
    prefix-matched siblings to be released to independent materialization
    alongside it, doubling the writer count (14 starts for 7 logical parts).

    Retry-budget exhaustion limits automatic proof ATTEMPTS only; it never
    creates identity truth (Section 4.1). The corrected contract: automatic
    proof attempts stop, identity remains unresolved, and NO member of this
    submission -- not the exhausted one, not its prefix-matched siblings --
    gets a competing writer. Repeated scheduler ticks after exhaustion must
    not hot-loop or consume unbounded proof attempts either."""
    first = await _submit_batch(retry_pair, retry_pair.a, "rapidgator")
    await retry_pair.engine.tick()

    fingerprint_calls = {"part1.rar": 0}

    async def persistent(candidate):
        if candidate.provider_id == retry_pair.b.descriptor.id and candidate.name == "part1.rar":
            fingerprint_calls["part1.rar"] += 1
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
    # Zero competing writers for the whole unresolved submission -- neither
    # the permanently-unresolved member nor its otherwise-provable siblings.
    assert len(await retry_pair.repository.artifacts(second.id)) == 0
    assert len([call for call in retry_pair.executor.calls if call[0] == "start"]) == 7

    rows = await _proof_rows(second.id)
    failed = next(row for row in rows if row["equivalence_reason"] == "timeout")
    assert int(failed["equivalence_retry_count"]) == 2
    assert failed["equivalence_disposition"] == "exhausted"
    assert float(failed["retry_at"] or 0) == 0
    assert sum(row["equivalence_disposition"] == "exhausted" for row in rows) == 1
    # No sibling is released to independence merely because one member
    # exhausted proof acquisition (Section 8.1.6) -- every one of the 7
    # durable requests for this submission stays held, unresolved.
    assert all(row["state"] == "materializing" for row in rows)

    exhausted_calls = fingerprint_calls["part1.rar"]
    retry_pair.now[0] += 1.1
    await retry_pair.engine.resolve_pending()
    await retry_pair.engine.resolve_pending()
    assert len(await retry_pair.repository.artifacts(second.id)) == 0
    assert len([call for call in retry_pair.executor.calls if call[0] == "start"]) == 7
    rows_after = await _proof_rows(second.id)
    failed_after = next(row for row in rows_after if row["equivalence_reason"] == "timeout")
    # Repeated scheduler ticks after exhaustion never hot-loop proof
    # acquisition or grow the retry counter past the budget.
    assert int(failed_after["equivalence_retry_count"]) == 2
    assert failed_after["equivalence_disposition"] == "exhausted"
    assert fingerprint_calls["part1.rar"] == exhausted_calls


@pytest.mark.asyncio
async def test_restart_after_exhaustion_stays_quiescent_and_can_still_recover(retry_pair, monkeypatch):
    """DP 1.0.12 Section 11.8: persist unresolved proof with the retry budget
    already exhausted (no physical artifact), restart a fresh production
    engine/repository instance against the same durable state, and assert:
    the proof counter is preserved, there is no automatic re-materialization,
    no duplicate writer is allocated, restart does not itself consume a proof
    attempt (no hot loop), and the request remains unresolved/quiescent until
    a later valid wake (here, an explicit operator retry) recovers it."""
    await _submit_batch(retry_pair, retry_pair.a, "rapidgator")
    await retry_pair.engine.tick()

    unavailable_forever = {"active": True}

    async def persistent(candidate):
        if candidate.provider_id == retry_pair.b.descriptor.id and candidate.name == "part1.rar" and unavailable_forever["active"]:
            return _unavailable("timeout")
        return _prefix(candidate)

    monkeypatch.setattr(retry_pair.executor, "fingerprint", persistent)
    second = await _submit_batch(retry_pair, retry_pair.b, "1fichier")

    for _ in range(3):  # 2 bounded proof opportunities, then exhaustion.
        await retry_pair.engine.resolve_pending()
        retry_pair.now[0] += 1.1

    before = await _proof_rows(second.id)
    failed_before = next(row for row in before if row["equivalence_reason"] == "timeout")
    assert failed_before["equivalence_disposition"] == "exhausted"
    assert int(failed_before["equivalence_retry_count"]) == 2
    assert len(await retry_pair.repository.artifacts(second.id)) == 0
    assert len([call for call in retry_pair.executor.calls if call[0] == "start"]) == 7

    # Simulate a process restart: a fresh production engine/repository
    # instance re-attached to the same durable SQLite state.
    restarted = TransferEngine(
        retry_pair.repository,
        retry_pair.registry,
        download_root=retry_pair.engine.root,
        policy=retry_pair.engine.policy,
        clock=lambda: retry_pair.now[0],
    )
    await restarted.initialize()

    for _ in range(3):  # repeated post-restart ticks: still no hot loop.
        await restarted.resolve_pending()
        retry_pair.now[0] += 1.1
    await restarted.reconcile_executions()

    after = await _proof_rows(second.id)
    failed_after = next(row for row in after if row["equivalence_reason"] == "timeout")
    assert int(failed_after["equivalence_retry_count"]) == 2  # proof counter preserved, not reset.
    assert failed_after["equivalence_disposition"] == "exhausted"  # no automatic re-materialization.
    assert len(await retry_pair.repository.artifacts(second.id)) == 0  # no duplicate writer.
    assert len([call for call in retry_pair.executor.calls if call[0] == "start"]) == 7
    assert (await retry_pair.repository.get(second.id)).state.value != "consolidated"

    # A later valid wake (explicit operator retry) can still recover it.
    async with database.get_db() as db:
        await db.execute(
            "UPDATE transfer_requests SET equivalence_disposition='',equivalence_retry_count=0,retry_at=0 WHERE id=?",
            (failed_after["id"],),
        )
        await db.commit()
    unavailable_forever["active"] = False
    retry_pair.now[0] += 1.1
    await restarted.resolve_pending()
    await restarted.reconcile_executions()

    assert (await retry_pair.repository.get(second.id)).state.value == "consolidated"
    assert len(await retry_pair.repository.artifacts(second.id)) == 0
    assert len([call for call in retry_pair.executor.calls if call[0] == "start"]) == 7
    recovered = next(row for row in await _proof_rows(second.id) if row["id"] == failed_after["id"])
    assert recovered["equivalence_disposition"] == "recovered"


@pytest.mark.asyncio
async def test_restart_preserves_pending_retry_budget_and_writer_barrier(retry_pair, monkeypatch):
    await _submit_batch(retry_pair, retry_pair.a, "rapidgator")
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
@pytest.mark.parametrize("reason", [
    "timeout", "dns_failure", "sampler_unavailable", "range_unsupported", "incomplete_representation",
])
async def test_each_transient_reason_exhausts_without_materializing_or_hot_looping(retry_pair, monkeypatch, reason):
    """DP 1.0.12 canonical equivalence/lifecycle correction, Section 11.2: for
    EVERY transient equivalence-proof failure reason, exhausting the bounded
    retry budget must never be read as permission to materialize an
    independent physical artifact or dispatch a competing executor writer,
    and repeated scheduler ticks after exhaustion must never hot-loop
    automatic proof sampling (Section 4.1/4.3). Identity remains unresolved."""
    first = await retry_pair.engine.submit(
        (TransferRequest("parcel", "a1", name="same.bin", preferred_provider=retry_pair.a.descriptor.id),),
        name="a", deduplicate=False,
    )
    await retry_pair.engine.tick()
    assert len(await retry_pair.repository.artifacts(first.id)) == 1

    calls = {"count": 0}

    async def persistent(candidate):
        if candidate.provider_id == retry_pair.b.descriptor.id:
            calls["count"] += 1
            return _unavailable(reason)
        return _prefix(candidate)

    monkeypatch.setattr(retry_pair.executor, "fingerprint", persistent)
    second = await retry_pair.engine.submit(
        (TransferRequest("parcel", "b1", name="same.bin", preferred_provider=retry_pair.b.descriptor.id),),
        name="b", deduplicate=False,
    )

    for _ in range(3):  # 2 bounded proof opportunities, then exhaustion.
        await retry_pair.engine.resolve_pending()
        retry_pair.now[0] += 1.1
    await retry_pair.engine.reconcile_executions()

    assert len(await retry_pair.repository.artifacts(second.id)) == 0  # no physical materialization.
    assert len([call for call in retry_pair.executor.calls if call[0] == "start"]) == 1  # no competing writer.

    row = (await _proof_rows(second.id))[0]
    assert row["equivalence_reason"] == reason
    assert row["equivalence_disposition"] == "exhausted"
    assert int(row["equivalence_retry_count"]) == 2
    assert row["state"] == "materializing"  # identity remains unresolved, held.
    calls_after_exhaustion = calls["count"]

    for _ in range(3):
        retry_pair.now[0] += 1.1
        await retry_pair.engine.resolve_pending()
    row_after = (await _proof_rows(second.id))[0]
    assert int(row_after["equivalence_retry_count"]) == 2  # no unbounded proof attempts.
    assert row_after["equivalence_disposition"] == "exhausted"
    assert calls["count"] == calls_after_exhaustion  # no hot loop of automatic proof sampling.
    assert len([call for call in retry_pair.executor.calls if call[0] == "start"]) == 1


@pytest.mark.asyncio
async def test_affirmative_size_contradiction_still_permits_independent_materialization(retry_pair, monkeypatch):
    """DP 1.0.12 Section 11.3, negative-space proof: the fix must not
    overcorrect into blocking all same-name siblings. Genuinely contradictory
    evidence (an authoritative size disagreement) is AFFIRMATIVE proof of
    distinction, not mere absence of proof -- it must still allow independent
    materialization, a unique second target, and a second executor writer."""
    first = await retry_pair.engine.submit(
        (TransferRequest("parcel", "a1", name="same.bin", preferred_provider=retry_pair.a.descriptor.id),),
        name="a", deduplicate=False,
    )
    await retry_pair.engine.tick()
    canonical_before = (await retry_pair.repository.artifacts(first.id))[0]
    assert canonical_before.expected_bytes > 0

    # size_disagreement is decided cheaply in pairing_failure() before any
    # sampler call, purely from the two RESOLVED candidates' reported sizes
    # -- an authoritative size contradiction, not a live-sampling outcome.
    contradicting_size = canonical_before.expected_bytes + 10 * 1024 * 1024
    original_candidate = retry_pair.b.candidate

    def bigger_candidate(name="payload.bin", *, payload="parcel"):
        return replace(original_candidate(name, payload=payload), expected_bytes=contradicting_size)

    monkeypatch.setattr(retry_pair.b, "candidate", bigger_candidate)
    second = await retry_pair.engine.submit(
        (TransferRequest("parcel", "b1", name="same.bin", preferred_provider=retry_pair.b.descriptor.id),),
        name="b", deduplicate=False,
    )
    await retry_pair.engine.resolve_pending()
    await retry_pair.engine.reconcile_executions()

    independent = await retry_pair.repository.artifacts(second.id)
    assert len(independent) == 1  # independent materialization allowed.
    assert independent[0].id != canonical_before.id  # unique second target.
    assert len([call for call in retry_pair.executor.calls if call[0] == "start"]) == 2  # second executor writer allowed.

    row = (await _proof_rows(second.id))[0]
    assert row["equivalence_disposition"] == "contradictory"
    assert row["equivalence_reason"] == "size_disagreement"


async def _artifact_rows_for_request(request_id):
    async with database.get_db() as db:
        return await db.fetchall("SELECT id FROM download_files WHERE request_id=?", (request_id,))


@pytest.mark.asyncio
async def test_non_retryable_unresolved_pairing_holds_and_never_becomes_independent(retry_pair, monkeypatch):
    """Transfer 286: ``range_ignored`` (a real HTTP 200 / Content-Length: 0
    answer to the bounded Range probe) is unresolved pairing evidence that is
    NOT retryable. Retryability decides only whether another automatic proof
    attempt is scheduled; it must never decide whether unresolved identity
    becomes independence. Against an existing canonical artifact the incoming
    same-slot request must therefore be durably held (the existing
    ``exhausted`` disposition), never released as ``independent`` with its own
    physical writer -- and repeated scheduler ticks must stay quiescent."""
    precondition = EquivalenceEvidence(EvidenceKind.UNAVAILABLE, reason="range_ignored")
    assert precondition.unresolved_pairing and not precondition.retryable  # the shape under test.

    first = await retry_pair.engine.submit(
        (TransferRequest("parcel", "a1", name="same.bin", preferred_provider=retry_pair.a.descriptor.id),),
        name="a", deduplicate=False,
    )
    await retry_pair.engine.tick()
    canonical = (await retry_pair.repository.artifacts(first.id))[0]

    calls = {"count": 0}

    async def range_ignored(candidate):
        if candidate.provider_id == retry_pair.b.descriptor.id:
            calls["count"] += 1
            return _unavailable("range_ignored")
        return _prefix(candidate)

    monkeypatch.setattr(retry_pair.executor, "fingerprint", range_ignored)
    second = await retry_pair.engine.submit(
        (TransferRequest("parcel", "b1", name="same.bin", preferred_provider=retry_pair.b.descriptor.id),),
        name="b", deduplicate=False,
    )
    await retry_pair.engine.resolve_pending()
    await retry_pair.engine.reconcile_executions()

    row = (await _proof_rows(second.id))[0]
    assert row["equivalence_disposition"] == "exhausted"  # held, never "independent".
    assert row["equivalence_reason"] == "range_ignored"
    assert float(row["retry_at"] or 0) == 0
    assert row["state"] == "materializing"
    assert await _artifact_rows_for_request(row["id"]) == []  # no independent download_files artifact.
    assert len(await retry_pair.repository.artifacts(second.id)) == 0
    assert len([call for call in retry_pair.executor.calls if call[0] == "start"]) == 1  # no executor writer.

    held_calls = calls["count"]
    for _ in range(3):
        retry_pair.now[0] += 1.1
        await retry_pair.engine.resolve_pending()
        await retry_pair.engine.reconcile_executions()
    row_after = (await _proof_rows(second.id))[0]
    assert row_after["equivalence_disposition"] == "exhausted"
    assert row_after["equivalence_reason"] == "range_ignored"
    assert float(row_after["retry_at"] or 0) == 0
    assert calls["count"] == held_calls  # no proof-acquisition hot loop while held.
    assert await _artifact_rows_for_request(row["id"]) == []
    assert len([call for call in retry_pair.executor.calls if call[0] == "start"]) == 1

    canonicals = await retry_pair.repository.artifacts(first.id)
    assert [item.id for item in canonicals] == [canonical.id]  # canonical stays the only physical writer.
    assert len(canonicals[0].candidates) == 1


@pytest.mark.asyncio
async def test_sampled_content_contradiction_still_releases_independent_writer(retry_pair, monkeypatch):
    """Adjacent control for the transfer-286 hold: a genuine sampled-content
    contradiction (two full samples that disagree) is AFFIRMATIVE non-pairing
    evidence, not unresolved -- it must keep releasing an independent writer."""
    contradiction = EquivalenceEvidence(EvidenceKind.UNAVAILABLE, reason="sample_mismatch")
    assert contradiction.failure_class == EvidenceFailureClass.CONTRADICTORY
    assert not contradiction.unresolved_pairing

    first = await retry_pair.engine.submit(
        (TransferRequest("parcel", "a1", name="same.bin", preferred_provider=retry_pair.a.descriptor.id),),
        name="a", deduplicate=False,
    )
    await retry_pair.engine.tick()

    async def different(candidate):
        signature = f"full:{candidate.provider_id}"
        return ArtifactFingerprint(
            candidate.expected_bytes, signature, FingerprintKind.FULL_CONTENT_SAMPLE, "", signature,
        )

    monkeypatch.setattr(retry_pair.executor, "fingerprint", different)
    second = await retry_pair.engine.submit(
        (TransferRequest("parcel", "b1", name="same.bin", preferred_provider=retry_pair.b.descriptor.id),),
        name="b", deduplicate=False,
    )
    await retry_pair.engine.resolve_pending()
    await retry_pair.engine.reconcile_executions()

    row = (await _proof_rows(second.id))[0]
    assert row["equivalence_disposition"] == "contradictory"
    assert row["equivalence_reason"] == "sample_mismatch"
    assert len(await retry_pair.repository.artifacts(second.id)) == 1  # independent artifact allowed.
    assert len([call for call in retry_pair.executor.calls if call[0] == "start"]) == 2  # second writer allowed.


@pytest.mark.asyncio
async def test_candidate_with_no_possible_proof_still_uses_degraded_fallback(retry_pair, monkeypatch):
    """Adjacent control for the transfer-286 hold: a candidate whose sampler
    reports no fingerprint at all (proof structurally unavailable -- nothing
    was sampled) is NOT the same as a sampler that ran and returned unusable
    evidence. It keeps the existing structurally-unprovable degraded fallback
    and still receives an independent writer."""
    first = await retry_pair.engine.submit(
        (TransferRequest("parcel", "a1", name="same.bin", preferred_provider=retry_pair.a.descriptor.id),),
        name="a", deduplicate=False,
    )
    await retry_pair.engine.tick()

    async def no_capability(_candidate):
        return None

    monkeypatch.setattr(retry_pair.executor, "fingerprint", no_capability)
    second = await retry_pair.engine.submit(
        (TransferRequest("parcel", "b1", name="same.bin", preferred_provider=retry_pair.b.descriptor.id),),
        name="b", deduplicate=False,
    )
    await retry_pair.engine.resolve_pending()
    await retry_pair.engine.reconcile_executions()

    row = (await _proof_rows(second.id))[0]
    assert row["equivalence_disposition"] == "independent"
    assert row["equivalence_reason"] == "sampler_unsupported"
    assert len(await retry_pair.repository.artifacts(second.id)) == 1
    assert len([call for call in retry_pair.executor.calls if call[0] == "start"]) == 2


@pytest.mark.asyncio
async def test_sibling_walk_non_retryable_unresolved_member_holds_whole_cohort(retry_pair, monkeypatch):
    """Transfer 286, materializing-cohort walk: a weak-evidence sibling whose
    own proof first failed transiently (a bounded retry is scheduled) and
    whose retried proof then comes back ``range_ignored`` (unresolved, NOT
    retryable) is evaluated during ANOTHER member's collection walk. That
    unresolved sibling must be durably held (``exhausted``) and keep the
    cohort's writer barrier up -- it must never be promoted to
    ``independent`` and release the whole cohort to independent
    materialization (a second physical writer for every member)."""
    third = BatchProvider("provider-c")
    retry_pair.registry.register_provider(third)
    first = await retry_pair.engine.submit(
        (TransferRequest("parcel", "a1", name="same.bin", preferred_provider=retry_pair.a.descriptor.id),),
        name="a", deduplicate=False,
    )
    await retry_pair.engine.tick()
    assert len(await retry_pair.repository.artifacts(first.id)) == 1

    mode = {"third": "timeout"}

    async def fingerprint(candidate):
        if candidate.provider_id == third.descriptor.id:
            return _unavailable(mode["third"])
        return _prefix(candidate)

    monkeypatch.setattr(retry_pair.executor, "fingerprint", fingerprint)
    second = await retry_pair.engine.submit(
        (
            TransferRequest("parcel", "b1", name="same.bin", preferred_provider=retry_pair.b.descriptor.id),
            TransferRequest("parcel", "c1", name="same.bin", preferred_provider=third.descriptor.id),
        ),
        name="mixed", deduplicate=False,
    )
    await retry_pair.engine.resolve_pending()
    rows = {row["id"]: row for row in await _proof_rows(second.id)}
    unresolved = next(row for row in rows.values() if row["equivalence_reason"] == "timeout")
    assert unresolved["equivalence_disposition"] == "pending"  # bounded retry scheduled for the third-provider member.
    assert len(await retry_pair.repository.artifacts(second.id)) == 0

    # The retried proof now reports the non-retryable unresolved shape.
    # Evaluate the OTHER member first so the unresolved one is met inside its
    # collection walk (not by its own turn).
    mode["third"] = "range_ignored"
    retry_pair.now[0] += 1.1
    other = next(
        item for item in await retry_pair.repository.requests(second.id)
        if item.id != unresolved["id"] and item.state == "materializing"
    )
    await retry_pair.engine._process_request(other)
    await retry_pair.engine.reconcile_executions()

    rows_after = {row["id"]: row for row in await _proof_rows(second.id)}
    assert rows_after[unresolved["id"]]["equivalence_disposition"] == "exhausted"
    assert rows_after[unresolved["id"]]["equivalence_reason"] == "range_ignored"
    assert float(rows_after[unresolved["id"]]["retry_at"] or 0) == 0
    assert not {row["equivalence_disposition"] for row in rows_after.values()} & {
        "independent", "contradictory", "released",
    }  # nobody in the cohort was released to independence.
    assert all(row["state"] == "materializing" for row in rows_after.values())
    assert len(await retry_pair.repository.artifacts(second.id)) == 0  # no second writer for any member.
    assert len([call for call in retry_pair.executor.calls if call[0] == "start"]) == 1


@pytest.mark.asyncio
async def test_later_evidence_recovers_after_exhaustion(retry_pair, monkeypatch):
    """DP 1.0.12 Section 11.4: initial sampler unavailable, retry budget
    exhausted, request held -- then later evidence becomes available (an
    explicit operator retry, one of Section 4.3's valid wake sources, resets
    the bounded proof-retry disposition so another proof opportunity exists)
    and proves equivalence. Required: the request attaches to the existing
    canonical, no competing writer was EVER started in between (not even
    while held), and the durable disposition transitions from
    unresolved/exhausted to recovered."""
    first = await retry_pair.engine.submit(
        (TransferRequest("parcel", "a1", name="same.bin", preferred_provider=retry_pair.a.descriptor.id),),
        name="a", deduplicate=False,
    )
    await retry_pair.engine.tick()

    unavailable_forever = {"active": True}

    def _full(candidate):
        signature = f"full:{candidate.name.casefold()}"
        return ArtifactFingerprint(candidate.expected_bytes, signature, FingerprintKind.FULL_CONTENT_SAMPLE, "", signature)

    async def flaky(candidate):
        if candidate.provider_id == retry_pair.b.descriptor.id and unavailable_forever["active"]:
            return _unavailable("dns_failure")
        # A lone request (no sibling cohort to corroborate weak prefix
        # evidence) needs individual-proving evidence to attach at all --
        # full-content proof, exactly like the strong-evidence fast path.
        return _full(candidate)

    monkeypatch.setattr(retry_pair.executor, "fingerprint", flaky)
    second = await retry_pair.engine.submit(
        (TransferRequest("parcel", "b1", name="same.bin", preferred_provider=retry_pair.b.descriptor.id),),
        name="b", deduplicate=False,
    )
    for _ in range(3):  # 2 bounded proof opportunities, then exhaustion.
        await retry_pair.engine.resolve_pending()
        retry_pair.now[0] += 1.1

    exhausted = (await _proof_rows(second.id))[0]
    assert exhausted["equivalence_disposition"] == "exhausted"
    assert len(await retry_pair.repository.artifacts(second.id)) == 0
    assert len([call for call in retry_pair.executor.calls if call[0] == "start"]) == 1  # never a competing writer.

    # Explicit operator retry (Section 4.3's valid wake source): reset the
    # bounded proof-retry disposition so another proof opportunity exists.
    # No executor-side retry mechanism is invented for this.
    async with database.get_db() as db:
        await db.execute(
            "UPDATE transfer_requests SET equivalence_disposition='',equivalence_retry_count=0,retry_at=0 WHERE id=?",
            (exhausted["id"],),
        )
        await db.commit()
    unavailable_forever["active"] = False  # later proof becomes available.

    await retry_pair.engine.resolve_pending()
    await retry_pair.engine.reconcile_executions()

    assert (await retry_pair.repository.get(second.id)).state.value == "consolidated"
    assert len(await retry_pair.repository.artifacts(second.id)) == 0  # still no competing writer.
    assert len([call for call in retry_pair.executor.calls if call[0] == "start"]) == 1
    canonicals = await retry_pair.repository.artifacts(first.id)
    assert len(canonicals) == 1
    assert len(canonicals[0].candidates) == 2  # attached to the existing canonical; origin preserved.

    recovered = (await _proof_rows(second.id))[0]
    assert recovered["equivalence_disposition"] == "recovered"


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


async def _insert_held_sibling_request(transfer_id: int, request_id: str, *, reason="dns_failure") -> None:
    """Directly durable-inject a request row already in the exact
    ``materializing`` + ``equivalence_disposition='exhausted'`` shape
    ``transfers.cohorts.coordinate_collection``/``_schedule_proof_retry``
    produce once the bounded automatic proof-retry budget is exhausted
    (proven by the tests above -- this helper skips reproducing that
    production machinery to isolate what these new tests actually check:
    ``transfers._repository_base.TransferRepository.aggregate_lifecycle``'s
    CONSUMPTION of the already-durable fact, transfer-265's NUS shape)."""
    payload = codec.dump(TransferRequest("parcel", "held", name="held.bin"))
    async with database.get_db() as db:
        await db.execute(
            """INSERT INTO transfer_requests(
                id,transfer_id,ordinal,payload,state,equivalence_disposition,equivalence_reason,
                equivalence_retry_count,retry_at)
                VALUES(?,?,1,?,'materializing','exhausted',?,2,0)""",
            (request_id, transfer_id, payload, reason),
        )
        await db.commit()


@pytest.mark.asyncio
async def test_quiescent_equivalence_hold_blocks_completion_until_resolved_or_released(retry_pair):
    """DP 1.0.12 canonical lifecycle/recovery/completion rework, Section 6.4
    (Gate 9 revision 2): transfer-265's exact shape -- one real artifact
    fully delivers while a SEPARATE, durably unresolved (materializing +
    equivalence_disposition=exhausted) request sits in the same transfer
    with no autonomous work left. A prior revision of this test wrongly
    treated the held request as a mere "artifact-less placeholder" and
    asserted the transfer reached COMPLETED regardless. That directly
    contradicts Section 6.4: "completed only when every logical delivery
    obligation is satisfied." An exhausted disposition means identity is
    UNRESOLVED, not proven equivalent -- completing around it silently
    infers non-equivalence, exactly the inference the equivalence
    correction exists to forbid. The parent must instead settle to the
    quiescent, nonterminal QUEUED wait (Section 6.4's "quiescent unresolved
    hold") for as long as the hold stands, and may reach COMPLETED only
    after the hold is actually resolved or released."""
    pair = retry_pair
    transfer = await pair.engine.submit(
        (TransferRequest("parcel", "solo", name="solo.bin", preferred_provider=pair.a.descriptor.id),),
        deduplicate=False,
    )
    await pair.engine.tick()
    artifact = (await pair.repository.artifacts(transfer.id))[0]
    pair.executor.finish(artifact.execution)

    # The held sibling must exist BEFORE the solo artifact's own completion
    # is durably aggregated, so this test actually exercises should_complete
    # deciding WHILE the held row is present -- inserting it only after an
    # earlier completion had already settled the (terminal, no-longer-
    # aggregated) transfer would silently pass regardless of this fix.
    await _insert_held_sibling_request(transfer.id, "held-nus")

    # A synthetic held sibling has no stored resolved candidates, so
    # ``resolve_pending()``'s ordinary re-resolution pass (irrelevant to
    # this test -- the point is aggregation's consumption of an ALREADY
    # durable disposition) must not touch it; only ``reconcile_executions()``
    # (execution observation + aggregation) is driven here.
    outcomes = []
    for _ in range(3):
        await pair.engine.reconcile_executions()
        outcomes.append((await pair.repository.get(transfer.id)).state)

    # Truthfully quiescent and nonterminal for every one of these cycles --
    # never COMPLETED, never oscillating, while the real artifact's own
    # bytes are fully delivered on disk the whole time.
    assert outcomes == [TransferState.QUEUED] * 3
    still_pending = await pair.repository.get(transfer.id)
    assert still_pending.state == TransferState.QUEUED
    assert still_pending.progress == 100  # the real artifact's own delivery is truthfully reflected...
    async with database.get_db() as db:
        held = await db.fetchone(
            "SELECT state,equivalence_disposition,equivalence_retry_count FROM transfer_requests WHERE id='held-nus'",
        )
    assert held["state"] == "materializing"
    assert held["equivalence_disposition"] == "exhausted"
    assert int(held["equivalence_retry_count"]) == 2  # no hot-looped proof re-attempts.

    # Now release the hold -- the ambiguous duplicate claim is administratively
    # retired (Section 6.3's "explicit operator action" wake source), leaving
    # no further unsatisfied obligation. Only THEN may completion follow.
    async with database.get_db() as db:
        await db.execute("DELETE FROM transfer_requests WHERE id='held-nus'")
        await db.commit()
    await pair.engine.reconcile_executions()

    final = await pair.repository.get(transfer.id)
    assert final.state == TransferState.COMPLETED
    assert final.progress == 100


@pytest.mark.asyncio
async def test_quiescent_hold_does_not_mask_an_independent_terminal_failure(retry_pair):
    """DP 1.0.12 canonical lifecycle/recovery/completion rework, Section 6.4
    (Gate 9 revision 3): a quiescent equivalence hold on ONE request must
    never launder a genuinely terminal, independent failure belonging to a
    DIFFERENT voting artifact into a truthless nonterminal wait. Topology:
    artifact A reaches a real, independent terminal ERROR (its own
    unsatisfied logical delivery obligation, nothing to do with B's identity
    ambiguity); request B sits in the same durable quiescent hold shape the
    sibling tests above use. No autonomous work remains for either. The
    parent must settle FAILED -- the hold is not license to erase an actual
    failure -- while the existing completed-artifact-plus-hold case (see
    ``test_quiescent_equivalence_hold_blocks_completion_until_resolved_or_
    released`` above) still correctly settles QUEUED, never COMPLETED,
    proving the fix distinguishes the two cases rather than just always
    picking one outcome."""
    pair = retry_pair
    terminal_error = NormalizedError(
        Domain.SECURITY, Category.PATH_POLICY_VIOLATION, Stage.EXECUTION,
        retryability=Retryability.NEVER, recovery=Recovery.FAIL, origin=Origin.REMOTE_SOURCE,
    )
    transfer = await pair.engine.submit(
        (TransferRequest("parcel", "solo", name="solo.bin", preferred_provider=pair.a.descriptor.id),),
        deduplicate=False,
    )
    await pair.engine.tick()
    artifact = (await pair.repository.artifacts(transfer.id))[0]

    # The held sibling must exist BEFORE the failing artifact's own
    # aggregation runs, so this test genuinely exercises the decision made
    # WHILE both facts (an independent terminal failure and an unresolved
    # hold) are simultaneously present.
    await _insert_held_sibling_request(transfer.id, "held-nus-failure")

    pair.executor.jobs[artifact.execution.attempt_id] = replace(
        pair.executor.jobs[artifact.execution.attempt_id], state=ExecutionState.FAILED, error=terminal_error,
    )
    await pair.engine.reconcile_executions()

    failed = (await pair.repository.artifacts(transfer.id))[0]
    assert failed.state == "error"
    final = await pair.repository.get(transfer.id)
    assert final.state == TransferState.FAILED, (
        "an independent artifact's genuine terminal failure must not be masked by an "
        "unrelated sibling's quiescent equivalence hold"
    )
    async with database.get_db() as db:
        held = await db.fetchone(
            "SELECT state,equivalence_disposition,equivalence_retry_count FROM transfer_requests WHERE id='held-nus-failure'",
        )
    assert held["state"] == "materializing"
    assert held["equivalence_disposition"] == "exhausted"
    assert int(held["equivalence_retry_count"]) == 2  # untouched by the unrelated failure.


@pytest.mark.asyncio
async def test_quiescent_equivalence_hold_with_no_artifacts_is_queued_not_perpetually_resolving(retry_pair):
    """DP 1.0.12 canonical lifecycle/recovery/completion rework, Section 6.4:
    a transfer whose ONLY request is durably held (no autonomous work
    scheduled, identity unresolved, no artifact yet) must truthfully show a
    quiescent non-resolving wait, not perpetual RESOLVING ("processing") and
    not silent staleness. Repeated scheduler cycles must not create a writer,
    hot-loop proof attempts, or oscillate the parent state."""
    pair = retry_pair
    transfer = await pair.repository.admit(
        (TransferRequest("parcel", "held-only", name="held.bin"),), name="held-only",
    )
    transfer_id = transfer[0].id
    async with database.get_db() as db:
        await db.execute("DELETE FROM transfer_requests WHERE transfer_id=?", (transfer_id,))
        await db.commit()
    await _insert_held_sibling_request(transfer_id, "held-solo")

    outcomes = []
    for _ in range(3):
        await pair.engine._aggregate(transfer_id)
        outcomes.append((await pair.repository.get(transfer_id)).state)

    assert outcomes == [TransferState.QUEUED] * 3  # truthful quiescent wait, no oscillation, no hot loop.
    async with database.get_db() as db:
        held = await db.fetchone(
            "SELECT equivalence_disposition,equivalence_retry_count FROM transfer_requests WHERE id='held-solo'",
        )
    assert held["equivalence_disposition"] == "exhausted"
    assert int(held["equivalence_retry_count"]) == 2


# --- Transfer 291: bootstrap (no canonical anywhere yet) proof exhaustion ---
#
# Everything above proves the STEADY-STATE rule: once a canonical writer
# exists, an unresolved sibling is held and never handed a competing writer.
# The tests below cover the different, bootstrap-only situation in which no
# canonical exists yet: bounded self-proof exhaustion means "identity cannot be
# proven automatically", which must not strand every usable candidate forever
# when one safe provisional writer can make progress.

async def _submit_bootstrap_cohort(pair):
    """One transfer, two same-named sibling requests: a multi-member cohort, so
    the bootstrap admission barrier applies."""
    transfer = await pair.engine.submit(
        (
            TransferRequest("parcel", "a1", name="same.bin", preferred_provider=pair.a.descriptor.id),
            TransferRequest("parcel", "b1", name="same.bin", preferred_provider=pair.b.descriptor.id),
        ),
        name="bootstrap-cohort", deduplicate=False,
    )
    by_payload = {record.request.payload: record for record in await pair.repository.requests(transfer.id)}
    return transfer, by_payload["a1"], by_payload["b1"]


async def _refreshed(pair, transfer_id, request_id):
    return next(item for item in await pair.repository.requests(transfer_id) if item.id == request_id)


async def _drive(pair, transfer_id, request_id, passes):
    """``passes`` further scheduler passes over one request, each after its
    proof-retry timer has come due."""
    for _ in range(passes):
        pair.now[0] += 1.1
        await pair.engine._process_request(await _refreshed(pair, transfer_id, request_id))


def _starts(pair):
    return len([call for call in pair.executor.calls if call[0] == "start"])


@pytest.mark.asyncio
async def test_bootstrap_incomplete_representation_exhaustion_admits_exactly_one_provisional_writer(
    retry_pair, monkeypatch,
):
    pair = retry_pair
    calls = {"count": 0}

    async def incomplete(_candidate):
        calls["count"] += 1
        return _unavailable("incomplete_representation")

    monkeypatch.setattr(pair.executor, "fingerprint", incomplete)
    transfer, record_a, record_b = await _submit_bootstrap_cohort(pair)

    # Candidate A resolves first and its own self-proof keeps coming back
    # UNAVAILABLE / incomplete_representation: two bounded retries, then the
    # budget exhausts with no canonical anywhere.
    await pair.engine._resolve(record_a)
    assert await pair.repository.artifacts(transfer.id) == ()  # still within the bounded proof budget.
    assert _starts(pair) == 0
    await _drive(pair, transfer.id, record_a.id, 2)
    await pair.engine.reconcile_executions()  # ordinary dispatch of the admitted writer.

    artifacts = await pair.repository.artifacts(transfer.id)
    assert [item.request_id for item in artifacts] == [record_a.id]  # exactly one artifact...
    assert _starts(pair) == 1  # ...and exactly one execution start.
    row = next(item for item in await _proof_rows(transfer.id) if item["id"] == record_a.id)
    assert row["equivalence_disposition"] == "provisional"  # honest: identity remains unknown,
    assert row["equivalence_disposition"] not in {"independent", "released", "contradictory", "recovered"}
    assert row["equivalence_reason"] == "incomplete_representation"
    assert int(row["equivalence_retry_count"]) == 2
    assert float(row["retry_at"] or 0) == 0
    assert calls["count"] == 3  # 1 + 2 bounded retries, never more.

    # No proof hot-loop once admitted.
    await _drive(pair, transfer.id, record_a.id, 3)
    assert calls["count"] == 3
    assert len(await pair.repository.artifacts(transfer.id)) == 1 and _starts(pair) == 1

    # The sibling is an ordinary later candidate: it maps against the existing
    # writer's artifact and its own unresolved proof is HELD -- never a second
    # provisional/independent writer.
    await pair.engine._resolve(record_b)
    await _drive(pair, transfer.id, record_b.id, 3)
    row_b = next(item for item in await _proof_rows(transfer.id) if item["id"] == record_b.id)
    assert row_b["equivalence_disposition"] == "exhausted"
    assert await _artifact_rows_for_request(record_b.id) == []
    assert len(await pair.repository.artifacts(transfer.id)) == 1 and _starts(pair) == 1
    held_calls = calls["count"]
    await _drive(pair, transfer.id, record_b.id, 3)
    assert calls["count"] == held_calls  # quiescent once held.


@pytest.mark.asyncio
async def test_bootstrap_provisional_writer_is_never_a_second_writer_after_the_first_completes(
    retry_pair, monkeypatch,
):
    """A completed provisional artifact leaves the canonical-artifact set, so a
    later sibling reaches the bootstrap barrier with "no canonical" again. It
    must still not become a SECOND provisional writer: the cohort's provisional
    role is a durable fact, not a function of what is currently in flight."""
    pair = retry_pair

    async def incomplete(_candidate):
        return _unavailable("incomplete_representation")

    monkeypatch.setattr(pair.executor, "fingerprint", incomplete)
    transfer, record_a, record_b = await _submit_bootstrap_cohort(pair)
    await pair.engine._resolve(record_a)
    await _drive(pair, transfer.id, record_a.id, 2)
    await pair.engine.reconcile_executions()
    artifact = (await pair.repository.artifacts(transfer.id))[0]
    pair.executor.finish(artifact.execution)
    await pair.engine.reconcile_executions()
    assert _starts(pair) == 1

    await pair.engine._resolve(record_b)
    await _drive(pair, transfer.id, record_b.id, 3)
    rows = {item["id"]: item for item in await _proof_rows(transfer.id)}
    assert rows[record_a.id]["equivalence_disposition"] == "provisional"
    assert rows[record_b.id]["equivalence_disposition"] != "provisional"
    assert rows[record_b.id]["equivalence_disposition"] not in {"independent", "released", "contradictory"}
    assert await _artifact_rows_for_request(record_b.id) == []
    assert _starts(pair) == 1


@pytest.mark.asyncio
async def test_bootstrap_zero_byte_range_ignored_still_gets_no_artifact_and_no_execution(retry_pair, monkeypatch):
    """Transfer 286 control: ``UNAVAILABLE / range_ignored`` (a real HTTP 200 /
    Content-Length: 0 empty body) is proof-unavailable evidence that is NOT in
    the provisional-writer class -- no matter how many scheduler passes go by,
    it gets zero artifacts and zero executions (hence no ``(2)`` duplicate path
    and no late ``materialization_failed``)."""
    pair = retry_pair
    calls = {"count": 0}

    async def zero_byte(_candidate):
        calls["count"] += 1
        return _unavailable("range_ignored")

    monkeypatch.setattr(pair.executor, "fingerprint", zero_byte)
    transfer, record_a, _record_b = await _submit_bootstrap_cohort(pair)
    await pair.engine._resolve(record_a)
    await _drive(pair, transfer.id, record_a.id, 6)

    assert await pair.repository.artifacts(transfer.id) == ()
    assert await _artifact_rows_for_request(record_a.id) == []
    assert _starts(pair) == 0
    row = next(item for item in await _proof_rows(transfer.id) if item["id"] == record_a.id)
    assert row["equivalence_disposition"] not in {"provisional", "independent", "released", "contradictory"}
    assert row["state"] == "materializing"
    assert calls["count"] == 1  # no proof hot-loop either.


def test_cohorts_admits_the_provisional_writer_from_the_semantic_fact_not_a_reason_string():
    source = inspect.getsource(cohorts)
    assert "eligible_for_provisional_writer_after_exhaustion" in source
    for reason in ("incomplete_representation", "range_unsupported", "range_ignored"):
        assert f'"{reason}"' not in source and f"'{reason}'" not in source
    assert "provisional" not in cohorts._INDEPENDENT_DISPOSITIONS  # identity is never read as independence.
    assert "provisional" not in cohorts._HELD_DISPOSITIONS
