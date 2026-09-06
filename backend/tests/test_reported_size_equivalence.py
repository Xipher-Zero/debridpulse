from dataclasses import replace

import pytest

from transfers.mirrors import (
    EvidenceKind,
    pairing_failure,
    reported_sizes_compatible,
    shared_evidence,
)
from transfers.models import (
    ArtifactFingerprint,
    Endpoint,
    FingerprintKind,
    IntegrityMetadata,
    SourceIdentity,
    TransferCandidate,
)


GIB = 1024 ** 3
MIB = 1024 ** 2
TIB = 1024 ** 4


def candidate(
    identity,
    size,
    *,
    source=None,
    integrity=(),
    name="GF200826-TMNTSFS-RN.rar",
):
    return TransferCandidate(
        id=identity,
        name=name,
        relative_path=name,
        expected_bytes=size,
        endpoints=(Endpoint("https", "https://example.invalid/" + identity),),
        provider_id="fake",
        source_identity=SourceIdentity("mirror", source or identity),
        integrity=integrity,
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


@pytest.mark.parametrize(
    ("left", "right", "compatible"),
    [
        (100 * GIB, 100 * GIB, True),
        (100 * GIB, 100 * GIB - 80 * MIB, True),
        (100 * GIB, 100 * GIB - 200 * MIB, False),
        (TIB, TIB - 512 * MIB, True),
        (TIB, TIB - 600 * MIB, False),
        (1_000_000_000, 999_000_000, True),
        (1_000_000_000, 998_999_999, False),
        (512_000 * GIB, 512_000 * GIB - 512 * MIB, True),
        (512_000 * GIB, 512_000 * GIB - 512 * MIB - 1, False),
        (0, 100, False),
        (100, 0, False),
    ],
)
def test_reported_size_compatibility_boundaries(left, right, compatible):
    assert reported_sizes_compatible(left, right) is compatible


def test_historical_megaup_variance_reaches_identity_evidence():
    left = candidate("left", 3_597_035_110, source="rapidgator")
    right = candidate("right", 3_595_501_360, source="megaup")
    assert reported_sizes_compatible(left.expected_bytes, right.expected_bytes)
    assert pairing_failure(left, right) == ""


@pytest.mark.asyncio
async def test_compatible_reported_sizes_require_matching_content_evidence():
    reported_a = 3_597_035_110
    reported_b = 3_595_501_360
    actual = 3_595_501_360
    left = candidate("left", reported_a, source="one")
    right = candidate("right", reported_b, source="two")
    executor = SamplingExecutor({
        "left": ArtifactFingerprint(actual, "same-content"),
        "right": ArtifactFingerprint(actual, "same-content"),
    })

    evidence = await shared_evidence(left, right, Registry(executor))

    assert evidence.kind == EvidenceKind.FULL_CONTENT_SAMPLE
    assert evidence.total_bytes == actual


@pytest.mark.asyncio
async def test_compatible_reported_sizes_with_mismatching_fingerprint_stay_separate():
    left = candidate("left", 3_597_035_110, source="one")
    right = candidate("right", 3_595_501_360, source="two")
    actual = 3_595_501_360
    executor = SamplingExecutor({
        "left": ArtifactFingerprint(actual, "left-content"),
        "right": ArtifactFingerprint(actual, "right-content"),
    })

    evidence = await shared_evidence(left, right, Registry(executor))

    assert evidence.kind == EvidenceKind.UNAVAILABLE
    assert evidence.reason == "sample_mismatch"


@pytest.mark.asyncio
async def test_sampled_actual_size_must_be_compatible_with_both_reports():
    left = candidate("left", 3_597_035_110, source="one")
    right = candidate("right", 3_595_501_360, source="two")
    incompatible_actual = 3_500_000_000
    executor = SamplingExecutor({
        "left": ArtifactFingerprint(incompatible_actual, "same"),
        "right": ArtifactFingerprint(incompatible_actual, "same"),
    })

    evidence = await shared_evidence(left, right, Registry(executor))

    assert evidence.kind == EvidenceKind.UNAVAILABLE
    assert evidence.reason == "size_disagreement"


@pytest.mark.asyncio
async def test_strong_digest_match_proves_identity_but_mismatch_is_contradiction():
    digest_a = (IntegrityMetadata("sha256", "a" * 64),)
    left = candidate("left", 3_597_035_110, source="one", integrity=digest_a)
    matching = candidate("matching", 3_595_501_360, source="two", integrity=digest_a)
    mismatch = candidate(
        "mismatch",
        3_595_501_360,
        source="three",
        integrity=(IntegrityMetadata("sha256", "b" * 64),),
    )
    registry = Registry(SamplingExecutor({}))

    matched = await shared_evidence(left, matching, registry)
    contradicted = await shared_evidence(left, mismatch, registry)

    assert matched.kind == EvidenceKind.STRONG_INTEGRITY
    assert contradicted.kind == EvidenceKind.UNAVAILABLE
    assert contradicted.reason == "integrity_mismatch"


def test_candidate_cannot_pair_with_itself_or_same_source():
    left = candidate("left", 100 * GIB, source="same")
    assert pairing_failure(left, left) == "same_candidate"

    other = replace(left, id="other")
    assert pairing_failure(left, other) == "non_independent_source"
