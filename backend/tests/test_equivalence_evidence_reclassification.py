"""DP 1.0.12 corrective task: equivalence-evidence classification regressions.

Covers Section 9 (equivalence-policy correction) and Section 11
(diagnostic-truth correction) of the false-negative repair. These tests
operate directly on ``transfers.mirrors`` evidence classification (Case 3/4
vocabulary) and, for the diagnostic-truth requirement, on the full engine so
the durably persisted ``equivalence_reason`` is exercised end-to-end.
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.engine import TransferEngine
from transfers.mirrors import EvidenceFailureClass, EvidenceKind, EquivalenceEvidence, shared_evidence
from transfers.models import ArtifactFingerprint, FingerprintKind, ResolutionResult, ResourceState, TransferRequest
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


# --- Case 4: real provider capabilities sometimes answer a Range probe with
# range_unsupported, or the new incomplete_representation transport fact, or
# a transient sampler failure. None of these may be persisted as proof that
# two otherwise-pairable artifacts differ; they must reuse the existing
# bounded proof-retry machinery.
@pytest.mark.parametrize("reason", ["range_unsupported", "incomplete_representation", "sampler_unavailable", "timeout", "dns_failure"])
def test_case4_ambiguous_transport_reasons_are_transient_and_retryable(reason):
    evidence = EquivalenceEvidence(EvidenceKind.UNAVAILABLE, reason=reason)
    assert evidence.failure_class == EvidenceFailureClass.TRANSIENT
    assert evidence.retryable
    assert not evidence.proves_collection_member


# --- Case 3: genuine contradictions must remain contradictions. Confirms the
# repair does not weaken conservative identity protection for a real digest
# mismatch or a real trustworthy full-sample mismatch (unchanged behavior;
# see also test_equivalence_retry_remediation.py::test_size_and_content_contradictions_are_not_retryable).
@pytest.mark.parametrize("reason", ["size_disagreement", "sample_mismatch", "integrity_mismatch"])
def test_case3_genuine_contradictions_remain_non_retryable(reason):
    evidence = EquivalenceEvidence(EvidenceKind.UNAVAILABLE, reason=reason)
    assert evidence.failure_class == EvidenceFailureClass.CONTRADICTORY
    assert not evidence.retryable
    assert not evidence.unresolved_pairing


# --- Transfer 286: a bounded sample that reports ``range_ignored`` (a real
# HTTP 200 / Content-Length: 0 answer to the Range probe) is UNRESOLVED
# pairing evidence -- it neither proves collection membership nor affirmatively
# rules the pair out. The cohort owner (transfers.cohorts) consumes exactly
# this fact to keep the physical-writer barrier up; the evidence owner must
# keep reporting it as unresolved, never as a contradiction, so that "cannot
# prove identity" is never mistaken for "proven independent".
def test_range_ignored_is_unresolved_pairing_not_a_contradiction():
    evidence = EquivalenceEvidence(EvidenceKind.UNAVAILABLE, reason="range_ignored")
    assert evidence.proves_collection_member is False
    assert evidence.unresolved_pairing is True
    assert evidence.failure_class != EvidenceFailureClass.CONTRADICTORY


# --- The evidence owner exposes ONE semantic fact separating a proof attempt
# that ran and yielded unusable material evidence (hold: never an independent
# writer) from a candidate for which no proof can be established by
# construction (the existing structurally-unprovable degraded fallback may
# apply). It is orthogonal to ``retryable``/``unresolved_pairing`` and is not a
# per-reason policy property.
@pytest.mark.parametrize("reason", ["sampler_unsupported", "ambiguous_mapping"])
def test_no_possible_proof_is_structurally_unavailable_but_still_unresolved(reason):
    evidence = EquivalenceEvidence(EvidenceKind.UNAVAILABLE, reason=reason)
    assert evidence.proof_structurally_unavailable is True
    assert evidence.unresolved_pairing is True
    assert not evidence.retryable


@pytest.mark.parametrize("reason", [
    "range_ignored", "invalid_content_range", "destination_rejected",  # sampler ran, unusable material evidence.
    "range_unsupported", "incomplete_representation", "sampler_unavailable", "timeout", "dns_failure",
    "size_disagreement", "sample_mismatch", "integrity_mismatch",  # affirmative contradictions.
])
def test_attempted_proof_is_never_structurally_unavailable(reason):
    assert EquivalenceEvidence(EvidenceKind.UNAVAILABLE, reason=reason).proof_structurally_unavailable is False


def test_usable_evidence_is_never_structurally_unavailable():
    assert EquivalenceEvidence(EvidenceKind.PREFIX_CONTENT_SAMPLE, 4, "sampler_unsupported").proof_structurally_unavailable is False


# --- Transfer 291: once bounded proof has exhausted with no canonical writer,
# the evidence owner -- never the cohort consumer, by reason string -- says
# whether the exhausted evidence belongs to the narrow "proof unavailable but
# ordinary execution may still succeed" class: the sampler reached a material
# endpoint and got an ordinary response it could not trust as a complete
# representation. It is a boundary, not a broadening of what proves identity.
def test_incomplete_representation_is_eligible_for_a_provisional_writer_after_exhaustion():
    evidence = EquivalenceEvidence(EvidenceKind.UNAVAILABLE, reason="incomplete_representation")
    assert evidence.eligible_for_provisional_writer_after_exhaustion is True
    # Eligibility never implies identity, independence or a contradiction.
    assert evidence.proves_collection_member is False
    assert evidence.unresolved_pairing is True
    assert evidence.failure_class == EvidenceFailureClass.TRANSIENT


@pytest.mark.parametrize("reason", [
    "range_ignored",           # Transfer 286: HTTP 200 / Content-Length 0 -- degenerate, never a writer.
    "range_unsupported",       # the sampler emits it for ANY non-200/206 status, HTTP errors included.
    "invalid_content_range", "destination_rejected",  # security/policy rejection is never eligible.
    "sampler_unavailable", "timeout", "dns_failure",
    "sampler_unsupported", "ambiguous_mapping",
    "size_disagreement", "sample_mismatch", "integrity_mismatch",
    "", "pairing_mismatch", "no_unique_mapping",
])
def test_no_other_unavailable_reason_is_eligible_for_a_provisional_writer(reason):
    assert EquivalenceEvidence(
        EvidenceKind.UNAVAILABLE, reason=reason,
    ).eligible_for_provisional_writer_after_exhaustion is False


@pytest.mark.parametrize("kind", [
    EvidenceKind.STRONG_INTEGRITY, EvidenceKind.FULL_CONTENT_SAMPLE, EvidenceKind.PREFIX_CONTENT_SAMPLE,
    EvidenceKind.RESOLVER_ATTESTED,
])
def test_usable_evidence_is_never_reported_as_eligible_for_a_provisional_writer(kind):
    assert EquivalenceEvidence(
        kind, 4, "incomplete_representation",
    ).eligible_for_provisional_writer_after_exhaustion is False


@pytest_asyncio.fixture
async def evidence_pair(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    first = ParcelProvider("provider-a")
    second = ParcelProvider("provider-b")
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(first)
    registry.register_provider(second)
    registry.register_executor(executor)
    return SimpleNamespace(registry=registry, executor=executor, a=first, b=second)


@pytest.mark.asyncio
async def test_a_genuinely_incompatible_pre_sampling_report_remains_a_pairing_rejection(evidence_pair):
    # Outside the established pairing envelope entirely (Case 3): this never
    # reaches the sampler at all, and must not be reclassified as retryable.
    left = evidence_pair.a.candidate("same.rar", payload="a")
    right = replace(evidence_pair.b.candidate("same.rar", payload="b"),
                     expected_bytes=left.expected_bytes * 1000)
    evidence = await shared_evidence(left, right, evidence_pair.registry)
    assert evidence.kind == EvidenceKind.UNAVAILABLE
    assert evidence.reason == "size_disagreement"
    assert not evidence.retryable


class BatchProvider(ParcelProvider):
    async def resolve(self, request):
        self.calls.append(("resolve", request.payload))
        name = request.name or "payload.bin"
        return ResolutionResult(ResourceState.AVAILABLE, (self.candidate(name, payload=f"payload:{name}"),))


@pytest_asyncio.fixture
async def diagnostic_pair(tmp_path, monkeypatch):
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
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0,
                              max_active_executions=32, resolution_concurrency=32),
        clock=lambda: 1000.0,
    )
    await engine.initialize()
    return SimpleNamespace(engine=engine, repository=repository, a=first, b=second, executor=executor)


async def _reason_row(transfer_id):
    async with database.get_db() as db:
        return await db.fetchone(
            """SELECT equivalence_reason,equivalence_disposition FROM transfer_requests
                WHERE transfer_id=? AND NOT EXISTS(
                    SELECT 1 FROM transfer_requests c WHERE c.parent_id=transfer_requests.id)""",
            (transfer_id,),
        )


@pytest.mark.asyncio
async def test_section11_decisive_specific_reason_is_not_masked_by_unrelated_canonical(diagnostic_pair, monkeypatch):
    """Section 11 diagnostic-truth: an unrelated canonical's generic
    logical_pairing_mismatch must never win over the decisive, specific
    evidence failure from the actual same-logical-file counterpart.
    """
    unrelated = await diagnostic_pair.engine.submit(
        (TransferRequest("parcel", "u1", name="unrelated.bin", preferred_provider=diagnostic_pair.a.descriptor.id),),
        name="unrelated", deduplicate=False,
    )
    target = await diagnostic_pair.engine.submit(
        (TransferRequest("parcel", "t1", name="target.bin", preferred_provider=diagnostic_pair.a.descriptor.id),),
        name="target", deduplicate=False,
    )
    await diagnostic_pair.engine.tick()
    assert len(await diagnostic_pair.repository.artifacts(unrelated.id)) == 1
    assert len(await diagnostic_pair.repository.artifacts(target.id)) == 1

    async def ambiguous(candidate):
        if candidate.provider_id == diagnostic_pair.b.descriptor.id and candidate.name == "target.bin":
            return ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "incomplete_representation", "")
        signature = f"prefix:{candidate.name.casefold()}"
        return ArtifactFingerprint(candidate.expected_bytes, signature, FingerprintKind.PREFIX_CONTENT_SAMPLE,
                                   "range_ignored", signature)

    monkeypatch.setattr(diagnostic_pair.executor, "fingerprint", ambiguous)
    incoming = await diagnostic_pair.engine.submit(
        (TransferRequest("parcel", "i1", name="target.bin", preferred_provider=diagnostic_pair.b.descriptor.id),),
        name="incoming", deduplicate=False,
    )
    await diagnostic_pair.engine.resolve_pending()

    row = await _reason_row(incoming.id)
    assert row["equivalence_reason"] == "incomplete_representation"
    assert row["equivalence_reason"] != "logical_pairing_mismatch"
