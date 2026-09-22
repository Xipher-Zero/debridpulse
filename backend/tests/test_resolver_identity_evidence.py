"""DP 1.0.12 canonical architecture correction, Workstream B.

Resolver-attested identity evidence (``transfers.models
.ResolverArtifactIdentityEvidence``) is a neutral fact a resolver/provider may
attach to a ``TransferCandidate``: "the resolver reported this exact name and
size." It is provider-neutral in the transfer model and consumed only by the
canonical equivalence policy in ``transfers.mirrors``, which represents a
resulting consolidation as the durably distinguishable
``EvidenceKind.RESOLVER_ATTESTED`` member of the SAME taxonomy every other
equivalence basis already uses (STRONG_INTEGRITY / FULL_CONTENT_SAMPLE /
PREFIX_CONTENT_SAMPLE / UNAVAILABLE) -- never a parallel signal invisible to
presentation/audit/diagnostics code that already inspects ``EvidenceKind``.

Existing sibling coverage (not duplicated here): generic same-name/same-size
HTTP safety and near-size tolerance already have dedicated suites --
``test_reported_size_equivalence.py``, ``test_unknown_reported_size_equivalence
.py``, ``test_equivalence_evidence_reclassification.py``,
``test_real_world_equivalence.py``. This file adds only the resolver-attested
evidence path itself.
"""
from __future__ import annotations

import pytest

from transfers.mirrors import EvidenceKind, pairing_failure, shared_evidence, shared_size
from transfers.models import (
    Endpoint, ResolverArtifactIdentityEvidence, SourceIdentity, TransferCandidate,
)


GIB = 1024 ** 3


def candidate(
    identity,
    size,
    *,
    source=None,
    resolved_name=None,
    resolved_bytes=None,
    name="Example.Release.1080p.mkv",
):
    evidence = None
    if resolved_name is not None:
        evidence = ResolverArtifactIdentityEvidence(
            resolved_name=resolved_name,
            exact_bytes=size if resolved_bytes is None else resolved_bytes,
        )
    return TransferCandidate(
        id=identity,
        name=name,
        relative_path=name,
        expected_bytes=size,
        endpoints=(Endpoint("https", "https://example.invalid/" + identity),),
        provider_id="alldebrid",
        source_identity=SourceIdentity("host", source or identity),
        resolver_identity_evidence=evidence,
    )


class Registry:
    """No sampler should ever be reached once resolver-attested proof applies."""

    def executor_for_subject(self, _subject):
        raise AssertionError("sampling must not be reached when resolver evidence proves equivalence")


@pytest.mark.asyncio
async def test_exact_resolver_attested_mirrors_from_independent_sources_consolidate():
    left = candidate("left", 5 * GIB, source="rapidgator.net", resolved_name="Example.Release.1080p.mkv")
    right = candidate("right", 5 * GIB, source="1fichier.com", resolved_name="example.release.1080p.mkv")
    evidence = await shared_evidence(left, right, Registry())
    assert evidence.kind == EvidenceKind.RESOLVER_ATTESTED
    assert evidence.proves_individual
    assert evidence.proves_collection_member
    assert evidence.total_bytes == 5 * GIB
    assert await shared_size(left, right, Registry()) == 5 * GIB


@pytest.mark.asyncio
async def test_same_source_identity_never_consolidates_via_resolver_evidence():
    left = candidate("left", 5 * GIB, source="rapidgator.net", resolved_name="Example.Release.1080p.mkv")
    right = candidate("right", 5 * GIB, source="rapidgator.net", resolved_name="Example.Release.1080p.mkv")
    evidence = await shared_evidence(left, right, Registry())
    assert evidence.kind == EvidenceKind.UNAVAILABLE
    assert evidence.reason == "non_independent_source"


@pytest.mark.asyncio
async def test_different_resolved_names_do_not_consolidate():
    left = candidate("left", 5 * GIB, source="rapidgator.net", resolved_name="Example.Release.1080p.mkv")
    right = candidate("right", 5 * GIB, source="1fichier.com", resolved_name="Different.Release.1080p.mkv")
    evidence = await shared_evidence(left, right, Registry())
    assert evidence.kind == EvidenceKind.UNAVAILABLE
    assert not evidence.proves_individual


@pytest.mark.asyncio
async def test_zero_or_unknown_resolver_size_does_not_qualify():
    left = candidate("left", 0, source="rapidgator.net", resolved_name="Example.Release.1080p.mkv", resolved_bytes=0)
    right = candidate("right", 5 * GIB, source="1fichier.com", resolved_name="Example.Release.1080p.mkv")
    evidence = await shared_evidence(left, right, Registry())
    assert evidence.kind == EvidenceKind.UNAVAILABLE
    assert not evidence.proves_individual


@pytest.mark.asyncio
async def test_materially_different_resolver_size_does_not_qualify():
    left = candidate("left", 5 * GIB, source="rapidgator.net", resolved_name="Example.Release.1080p.mkv")
    right = candidate(
        "right", 5 * GIB, source="1fichier.com",
        resolved_name="Example.Release.1080p.mkv", resolved_bytes=4 * GIB,
    )
    evidence = await shared_evidence(left, right, Registry())
    assert evidence.kind == EvidenceKind.UNAVAILABLE


@pytest.mark.asyncio
async def test_near_but_not_exact_resolver_size_falls_through_to_content_evidence_requirement():
    """Near-size historical tolerance is a reported-size PAIRING rule only
    (``reported_sizes_compatible``, consumed by ``pairing_failure``) -- it
    must never let resolver-attested metadata alone prove identity. A pair
    close enough to pass the fuzzy pairing prefilter, but not EXACTLY equal,
    must fall through to the ordinary sampler path unproven here."""
    near_delta = 1024 * 1024  # within reported_sizes_compatible's tolerance for 5 GiB, but not exact
    left = candidate("left", 5 * GIB, source="rapidgator.net", resolved_name="Example.Release.1080p.mkv")
    right = candidate(
        "right", 5 * GIB - near_delta, source="1fichier.com",
        resolved_name="Example.Release.1080p.mkv", resolved_bytes=5 * GIB - near_delta,
    )
    assert pairing_failure(left, right) == ""  # confirms this pair reaches the evidence stage at all

    calls = []

    class SamplingRegistry:
        def executor_for_subject(self, subject):
            calls.append(subject.candidate.id)
            return None  # no sampling capability -> UNAVAILABLE, proves the resolver path did not consolidate

    evidence = await shared_evidence(left, right, SamplingRegistry())
    assert calls, "resolver-attested evidence must not short-circuit a near-but-not-exact size pair"
    assert evidence.kind == EvidenceKind.UNAVAILABLE
    assert not evidence.proves_individual


@pytest.mark.asyncio
async def test_generic_http_same_name_same_size_without_resolver_evidence_stays_independent():
    """Ordinary General HTTP candidates never populate
    ``resolver_identity_evidence`` -- same reported name/size alone must never
    be treated as resolver-attested proof (specification section 12.2.G)."""
    left = TransferCandidate(
        id="left", name="movie.mkv", relative_path="movie.mkv", expected_bytes=5 * GIB,
        endpoints=(Endpoint("https", "https://mirror-a.example/movie.mkv"),),
        provider_id="general_http", source_identity=SourceIdentity("host", "mirror-a.example"),
    )
    right = TransferCandidate(
        id="right", name="movie.mkv", relative_path="movie.mkv", expected_bytes=5 * GIB,
        endpoints=(Endpoint("https", "https://mirror-b.example/movie.mkv"),),
        provider_id="general_http", source_identity=SourceIdentity("host", "mirror-b.example"),
    )

    calls = []

    class SamplingRegistry:
        def executor_for_subject(self, subject):
            calls.append(subject.candidate.id)
            return None

    evidence = await shared_evidence(left, right, SamplingRegistry())
    assert calls, "generic HTTP same-name/same-size must reach the sampler path, never resolver-attested proof"
    assert evidence.kind == EvidenceKind.UNAVAILABLE
    assert not evidence.proves_individual


def test_evidence_kind_taxonomy_carries_resolver_attested_as_first_class_member():
    """The Workstream B integration requirement: resolver-attested
    consolidation must be a durably distinguishable value WITHIN the existing
    ``EvidenceKind`` taxonomy, never a parallel signal invisible to it."""
    assert EvidenceKind.RESOLVER_ATTESTED not in {
        EvidenceKind.STRONG_INTEGRITY, EvidenceKind.FULL_CONTENT_SAMPLE,
        EvidenceKind.PREFIX_CONTENT_SAMPLE, EvidenceKind.UNAVAILABLE,
    }
    assert isinstance(EvidenceKind.RESOLVER_ATTESTED, str)


def test_candidate_model_carries_resolver_evidence_without_equivalence_decision():
    evidence = ResolverArtifactIdentityEvidence(resolved_name="Example.mkv", exact_bytes=123)
    assert evidence.resolved_name == "Example.mkv"
    assert evidence.exact_bytes == 123
    assert not hasattr(evidence, "duplicate")
    assert not hasattr(evidence, "standby")


def test_resolver_identity_evidence_round_trips_through_durable_codec():
    """Specification section 8.4 / 12.2.H: durable evidence must survive a
    persistence/restart path without URL/hostname inference."""
    from transfers import codec

    original = candidate("left", 5 * GIB, source="rapidgator.net", resolved_name="Example.Release.1080p.mkv")
    reloaded = codec.candidate(codec.load(codec.dump(original)))
    assert reloaded.resolver_identity_evidence == original.resolver_identity_evidence
    assert reloaded.resolver_identity_evidence.resolved_name == "Example.Release.1080p.mkv"
    assert reloaded.resolver_identity_evidence.exact_bytes == 5 * GIB


def test_candidate_without_resolver_evidence_round_trips_as_none():
    from transfers import codec

    original = TransferCandidate(
        id="left", name="movie.mkv", relative_path="movie.mkv", expected_bytes=5 * GIB,
        endpoints=(Endpoint("https", "https://mirror-a.example/movie.mkv"),),
        provider_id="general_http", source_identity=SourceIdentity("host", "mirror-a.example"),
    )
    reloaded = codec.candidate(codec.load(codec.dump(original)))
    assert reloaded.resolver_identity_evidence is None
