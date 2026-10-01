"""DP 1.0.13 Gate-10 corrections in two canonical owners, proven neutrally.

* Equivalence (``transfers.mirrors``): two routes of one server are never
  independent corroboration -- unless both carry the same strong whole-file
  digest, which names one artifact whichever server reaches it. Without that
  evidence the same-source refusal is exactly what it was.
* Recovery policy (``transfers.policy``): material the canonical verifier
  rejected activates an existing alternate; with none left it is terminal.
  No other integrity failure changes.
"""
from __future__ import annotations

import pytest

from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage
from transfers.mirrors import EvidenceKind, pairing_failure, shared_evidence
from transfers.models import Endpoint, IntegrityMetadata, SourceIdentity, TransferCandidate
from transfers.policy import RecoveryAction, RecoveryContext, TransferPolicy

SHA = "ab" * 32
NOW = 1000.0


def route(scheme, *, integrity=(), path="pub/release.iso"):
    return TransferCandidate("release.iso", (Endpoint(scheme, f"{scheme}://mirror.example/{path}"),),
                             expected_bytes=4, relative_path="release.iso", provider_id=f"provider-{scheme}",
                             source_identity=SourceIdentity("host", "mirror.example"), integrity=integrity)


class NoSampler:
    """Proof must be decided without sampling anything."""

    def claimants(self, _subject):
        raise AssertionError("strong identity must not need a sample")


@pytest.mark.asyncio
async def test_a_shared_strong_digest_proves_two_routes_of_one_server_one_artifact():
    digest = (IntegrityMetadata("sha256", SHA),)
    ftp, https = route("ftp", integrity=digest), route("https", integrity=digest)
    assert pairing_failure(ftp, https) == ""
    evidence = await shared_evidence(ftp, https, NoSampler())
    assert evidence.kind == EvidenceKind.STRONG_INTEGRITY


@pytest.mark.parametrize("left,right", [
    ((), ()),                                                                         # no evidence
    ((IntegrityMetadata("md5", "c" * 32),), (IntegrityMetadata("md5", "c" * 32),)),  # weak only
    ((IntegrityMetadata("sha256", SHA),), ()),                                        # one side only
    ((IntegrityMetadata("sha256", SHA),), (IntegrityMetadata("sha256", "cd" * 32),)),  # different digests
], ids=["none", "weak", "one-sided", "different"])
def test_without_shared_strong_evidence_one_server_stays_non_independent(left, right):
    assert pairing_failure(route("ftp", integrity=left), route("https", integrity=right)) == "non_independent_source"


def _rejected(stage=Stage.VERIFICATION, category=Category.MATERIALIZATION_FAILED):
    return NormalizedError(Domain.INTEGRITY, category, stage, retryability=Retryability.AFTER_RESOURCE_CHANGE)


def _context(has_alternate):
    return RecoveryContext(has_alternate=has_alternate, observed_completed_bytes=0)


def test_rejected_material_activates_an_existing_alternate():
    decision = TransferPolicy().recover(_rejected(), _context(True), NOW)
    assert (decision.action, decision.reason, decision.retry_at) == (
        RecoveryAction.TRY_ALTERNATE_CANDIDATE, "integrity_alternate", NOW)


def test_rejected_material_with_no_alternate_left_is_terminal():
    decision = TransferPolicy().recover(_rejected(), _context(False), NOW)
    assert (decision.action, decision.reason) == (RecoveryAction.FAIL_PERMANENTLY, "integrity_failure")


@pytest.mark.parametrize("stage,category", [
    (Stage.EXECUTION, Category.CHECKSUM_MISMATCH),
    (Stage.CANDIDATE_PREPARATION, Category.SIZE_MISMATCH),
])
def test_every_other_integrity_failure_is_unchanged(stage, category):
    decision = TransferPolicy().recover(_rejected(stage, category), _context(True), NOW)
    assert decision.action == RecoveryAction.FAIL_PERMANENTLY
