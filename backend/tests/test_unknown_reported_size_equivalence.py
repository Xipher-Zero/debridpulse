"""DP 1.0.12 protocol-neutral correction: unknown reported size must not be a
false-negative identity signal.

Ordinary General HTTP candidates never populate ``expected_bytes`` (see
``providers/general_http/provider.py``: ``TransferCandidate`` is constructed
with no ``expected_bytes`` argument, defaulting to 0). Before this
correction, ``pairing_failure`` treated *any* unknown positive size as the
structural, non-retryable reason ``size_unknown`` -- which sits in
``_NONPAIRING_REASONS`` -- so ``shared_evidence`` returned before ever asking
a sampling-capable executor for bounded content evidence. Two independent
mirrors of the same payload could therefore never reach canonical
consolidation purely because neither provider supplied an advance size.

Per Section 9 of the correction task, the intended rule is:

    known positive + incompatible known positive  => cheap negative evidence
    unknown reported size                          => unknown, not proof of difference
    otherwise-plausible + sampling capability       => gather bounded content evidence
    known reported size incompatible with discovered actual size => reject
    unknown reported size imposes no reported-size consistency requirement
"""
from dataclasses import replace

import pytest

from transfers.mirrors import (
    EvidenceKind,
    pairing_failure,
    shared_evidence,
)
from transfers.models import ArtifactFingerprint, Endpoint, SourceIdentity, TransferCandidate


def candidate(identity, size, *, source=None, name="ubuntu-24.04.3-desktop-amd64.iso"):
    return TransferCandidate(
        id=identity,
        name=name,
        relative_path=name,
        expected_bytes=size,
        endpoints=(Endpoint("https", "https://example.invalid/" + identity),),
        provider_id="general_http",
        source_identity=SourceIdentity("host", source or identity),
    )


class SamplingExecutor:
    def __init__(self, fingerprints):
        self.fingerprints = fingerprints

    async def fingerprint(self, value):
        return self.fingerprints[value.id]


class Registry:
    def __init__(self, executor):
        self.executor = executor

    def executor_for(self, _candidate):
        return self.executor


def test_both_sides_unknown_size_is_pairable_not_structural_rejection():
    """Section 12.2: unknown+unknown must not be a permanent size_unknown veto."""
    left = candidate("left", 0, source="mirror1.example")
    right = candidate("right", 0, source="mirror2.example")
    assert pairing_failure(left, right) == ""


def test_one_side_unknown_size_is_still_pairable():
    left = candidate("left", 0, source="mirror1.example")
    right = candidate("right", 4_691_998_720, source="mirror2.example")
    assert pairing_failure(left, right) == ""


@pytest.mark.asyncio
async def test_both_sides_unknown_size_reaches_sampler_and_proves_identity():
    """Section 12.3 (unit slice): identical mirrors with no advance size must
    still be able to reach FULL_CONTENT_SAMPLE identity evidence."""
    left = candidate("left", 0, source="mirror1.example")
    right = candidate("right", 0, source="mirror2.example")
    actual = 4_691_998_720
    executor = SamplingExecutor({
        "left": ArtifactFingerprint(actual, "same-iso-content"),
        "right": ArtifactFingerprint(actual, "same-iso-content"),
    })

    evidence = await shared_evidence(left, right, Registry(executor))

    assert evidence.kind == EvidenceKind.FULL_CONTENT_SAMPLE
    assert evidence.total_bytes == actual
    assert evidence.proves_individual


@pytest.mark.asyncio
async def test_unknown_side_imposes_no_reported_size_consistency_requirement():
    """Section 12.6 (unknown + known): the known side's report must still be
    respected; the unknown side must not force a spurious size_disagreement."""
    known_size = 4_691_998_720
    left = candidate("left", 0, source="mirror1.example")
    right = candidate("right", known_size, source="mirror2.example")
    executor = SamplingExecutor({
        "left": ArtifactFingerprint(known_size, "same-iso-content"),
        "right": ArtifactFingerprint(known_size, "same-iso-content"),
    })

    evidence = await shared_evidence(left, right, Registry(executor))

    assert evidence.kind == EvidenceKind.FULL_CONTENT_SAMPLE
    assert evidence.total_bytes == known_size


@pytest.mark.asyncio
async def test_known_side_still_rejects_actual_size_it_contradicts():
    """Section 12.6 (unknown + known), second half: a known report that
    genuinely contradicts the discovered actual size must still reject."""
    known_size = 4_691_998_720
    contradicting_actual = 1_000_000  # far outside plausibility tolerance
    left = candidate("left", 0, source="mirror1.example")
    right = candidate("right", known_size, source="mirror2.example")
    executor = SamplingExecutor({
        "left": ArtifactFingerprint(contradicting_actual, "same"),
        "right": ArtifactFingerprint(contradicting_actual, "same"),
    })

    evidence = await shared_evidence(left, right, Registry(executor))

    assert evidence.kind == EvidenceKind.UNAVAILABLE
    assert evidence.reason == "size_disagreement"


@pytest.mark.asyncio
async def test_both_unknown_but_different_content_does_not_converge():
    """Section 12.4: same logical filename, unknown sizes, genuinely
    different payloads must remain distinct even once unknown-size pairing
    is allowed through."""
    left = candidate("left", 0, source="mirror1.example")
    right = candidate("right", 0, source="mirror2.example")
    executor = SamplingExecutor({
        "left": ArtifactFingerprint(4_691_998_720, "iso-a-content"),
        "right": ArtifactFingerprint(3_221_225_472, "iso-b-content"),
    })

    evidence = await shared_evidence(left, right, Registry(executor))

    assert evidence.kind == EvidenceKind.UNAVAILABLE
    assert evidence.reason == "size_disagreement"
    assert not evidence.proves_individual
    assert not evidence.proves_collection_member


def test_two_known_incompatible_sizes_still_reject_cheaply_without_sampling():
    """Section 12.5 regression guard: known+known incompatible sizes remain a
    cheap structural rejection; this correction must not weaken that path."""
    left = candidate("left", 100 * 1024 * 1024 * 1024, source="mirror1.example")
    right = candidate("right", 50 * 1024 * 1024 * 1024, source="mirror2.example")
    assert pairing_failure(left, right) == "size_disagreement"


def test_pairing_failure_never_returns_size_unknown_anymore():
    """size_unknown is retired as a pairing_failure outcome; unknown size is
    'unknown, not proof of difference' per Section 9, not a rejection reason."""
    left = candidate("left", 0, source="mirror1.example")
    right = replace(candidate("right", 0, source="mirror2.example"), name="different-name.iso",
                     relative_path="different-name.iso")
    # Unrelated filenames still fail on logical_pairing_mismatch, never size_unknown.
    assert pairing_failure(left, right) == "logical_pairing_mismatch"
