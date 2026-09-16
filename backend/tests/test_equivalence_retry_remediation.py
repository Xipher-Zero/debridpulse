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
    first = await _submit_batch(retry_pair, retry_pair.a, "rapidgator")
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
