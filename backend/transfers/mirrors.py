"""Conservative logical-artifact equivalence across independent source scopes."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import socket

from transfers.contracts import CandidateSampling
from transfers.models import FingerprintKind


logger = logging.getLogger(__name__)
_STRONG_INTEGRITY_ALGORITHMS = {"sha256", "sha512", "blake2", "blake2b", "blake2s"}
_TRANSIENT_REASONS = frozenset({"timeout", "dns_failure", "sampler_unavailable"})
_CONTRADICTORY_REASONS = frozenset({"size_disagreement", "sample_mismatch", "integrity_mismatch"})
_NONPAIRING_REASONS = frozenset({
    "same_candidate", "non_independent_source", "logical_pairing_mismatch", "size_unknown",
    "size_disagreement", "sample_mismatch", "integrity_mismatch",
})
_REPORTED_SIZE_RELATIVE_SCALE = 1000
_REPORTED_SIZE_MAX_DELTA_BYTES = 512 * 1024 * 1024


class EvidenceKind(str):
    STRONG_INTEGRITY = "strong_integrity"
    FULL_CONTENT_SAMPLE = "full_content_sample"
    PREFIX_CONTENT_SAMPLE = "prefix_content_sample"
    UNAVAILABLE = "unavailable"


class EvidenceFailureClass(str):
    NONE = "none"
    TRANSIENT = "transient"
    CONTRADICTORY = "contradictory"
    STRUCTURAL = "structural"


@dataclass(frozen=True)
class EquivalenceEvidence:
    kind: str
    total_bytes: int = 0
    reason: str = ""

    @property
    def proves_individual(self) -> bool:
        return self.kind in {EvidenceKind.STRONG_INTEGRITY, EvidenceKind.FULL_CONTENT_SAMPLE}

    @property
    def proves_collection_member(self) -> bool:
        return self.kind in {
            EvidenceKind.STRONG_INTEGRITY,
            EvidenceKind.FULL_CONTENT_SAMPLE,
            EvidenceKind.PREFIX_CONTENT_SAMPLE,
        }

    @property
    def failure_class(self) -> str:
        if self.kind != EvidenceKind.UNAVAILABLE:
            return EvidenceFailureClass.NONE
        if self.reason in _TRANSIENT_REASONS:
            return EvidenceFailureClass.TRANSIENT
        if self.reason in _CONTRADICTORY_REASONS:
            return EvidenceFailureClass.CONTRADICTORY
        return EvidenceFailureClass.STRUCTURAL

    @property
    def retryable(self) -> bool:
        return self.reason in _TRANSIENT_REASONS and self.kind in {
            EvidenceKind.UNAVAILABLE,
            EvidenceKind.PREFIX_CONTENT_SAMPLE,
        }

    @property
    def unresolved_pairing(self) -> bool:
        """True when this failure cannot rule out an otherwise plausible pair."""
        return (
            self.kind == EvidenceKind.UNAVAILABLE
            and self.reason not in _NONPAIRING_REASONS
            and self.failure_class != EvidenceFailureClass.CONTRADICTORY
        )


def _normalized_algorithm(value: str) -> str:
    return "".join(ch for ch in str(value).strip().lower() if ch.isalnum())


def _strong_integrity(candidate):
    result = set()
    for item in candidate.integrity:
        algorithm = _normalized_algorithm(item.algorithm)
        digest = str(item.digest).strip().lower()
        if algorithm in _STRONG_INTEGRITY_ALGORITHMS and digest:
            result.add((algorithm, digest))
    return result


def logical_key(candidate) -> str:
    """Normalized logical path used only as a pairing key, never identity proof."""
    value = str(candidate.relative_path or candidate.name or "").replace("\\", "/").strip()
    while value.startswith("./"):
        value = value[2:]
    return "/".join(part for part in value.split("/") if part not in {"", "."}).casefold()


def _source_key(candidate):
    source = candidate.source_identity
    if source is not None and str(source.scope).strip() and str(source.key).strip():
        return "source", str(source.scope), str(source.key)
    # Legacy/test providers that predate explicit source_identity still carry a
    # unique durable candidate identity. Production resolvers should provide the
    # stronger explicit source scope wherever one exists.
    return "candidate", str(candidate.id)


def _known_positive_size(value) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def reported_sizes_compatible(left_size, right_size) -> bool:
    """Return whether two known positive reported sizes are plausibly equivalent.

    This is deliberately only a pairing/plausibility rule. Identity still
    requires canonical strong integrity or bounded content evidence.
    """
    left = _known_positive_size(left_size)
    right = _known_positive_size(right_size)
    if left is None or right is None:
        return False
    delta = abs(left - right)
    larger = max(left, right)
    return (
        delta * _REPORTED_SIZE_RELATIVE_SCALE <= larger
        and delta <= _REPORTED_SIZE_MAX_DELTA_BYTES
    )


def _sample_size_compatible_with_reports(actual_size, left, right) -> bool:
    actual = _known_positive_size(actual_size)
    if actual is None:
        return False
    return (
        reported_sizes_compatible(actual, left.expected_bytes)
        and reported_sizes_compatible(actual, right.expected_bytes)
    )


def pairing_failure(left, right) -> str:
    """Return the exact cheap pairing rejection reason; empty means pairable."""
    if str(left.id) == str(right.id):
        return "same_candidate"
    if _source_key(left) == _source_key(right):
        return "non_independent_source"
    if not logical_key(left) or logical_key(left) != logical_key(right):
        return "logical_pairing_mismatch"
    if _known_positive_size(left.expected_bytes) is None or _known_positive_size(right.expected_bytes) is None:
        return "size_unknown"
    if not reported_sizes_compatible(left.expected_bytes, right.expected_bytes):
        return "size_disagreement"
    return ""


def comparable(left, right):
    """Cheap bounded pairing prefilter only; this never proves equivalence by itself."""
    return not pairing_failure(left, right)


def _unavailable(reason: str) -> EquivalenceEvidence:
    return EquivalenceEvidence(EvidenceKind.UNAVAILABLE, reason=reason)


def _fingerprint_kind(value) -> str:
    try:
        return str(value.kind.value)
    except AttributeError:
        return str(getattr(value, "kind", FingerprintKind.FULL_CONTENT_SAMPLE))


def _diagnose(left, right, evidence: EquivalenceEvidence, pair_reason: str = "") -> EquivalenceEvidence:
    """Emit only sanitized equivalence facts: never endpoints, headers or secrets."""
    logger.debug(
        "cross-transfer equivalence artifact=%r candidate_size=%d canonical_size=%d pairable=%s "
        "source_independent=%s evidence=%s reason=%s failure_class=%s",
        logical_key(right) or logical_key(left),
        max(0, int(right.expected_bytes or 0)),
        max(0, int(left.expected_bytes or 0)),
        not bool(pair_reason),
        _source_key(left) != _source_key(right),
        evidence.kind,
        evidence.reason or "none",
        evidence.failure_class,
    )
    return evidence


async def shared_evidence(left, right, registry) -> EquivalenceEvidence:
    """Return structured provider-neutral evidence without speculative merging."""
    pair_reason = pairing_failure(left, right)
    if pair_reason:
        return _diagnose(left, right, _unavailable(pair_reason), pair_reason)

    # Preserve the established canonical side's report when integrity itself is
    # the proof. A content fingerprint, when available, replaces this with the
    # discovered payload size below.
    canonical_reported_size = int(left.expected_bytes)
    left_integrity = _strong_integrity(left)
    right_integrity = _strong_integrity(right)
    if left_integrity & right_integrity:
        return _diagnose(
            left,
            right,
            EquivalenceEvidence(EvidenceKind.STRONG_INTEGRITY, canonical_reported_size),
        )
    left_by_algorithm = {algorithm for algorithm, _ in left_integrity}
    right_by_algorithm = {algorithm for algorithm, _ in right_integrity}
    if left_by_algorithm & right_by_algorithm:
        return _diagnose(left, right, _unavailable("integrity_mismatch"))

    try:
        first, second = registry.executor_for(left), registry.executor_for(right)
        if not isinstance(first, CandidateSampling) or not isinstance(second, CandidateSampling):
            return _diagnose(left, right, _unavailable("sampler_unsupported"))
        a, b = await asyncio.gather(first.fingerprint(left), second.fingerprint(right))
        # A sampler returning None has no proof capability for this candidate.
        # Temporary acquisition failures must cross the contract explicitly as
        # UNAVAILABLE with a retryable reason such as timeout/dns_failure.
        if a is None or b is None:
            return _diagnose(left, right, _unavailable("sampler_unsupported"))

        a_kind = _fingerprint_kind(a)
        b_kind = _fingerprint_kind(b)
        if a_kind == FingerprintKind.UNAVAILABLE.value:
            return _diagnose(left, right, _unavailable(str(getattr(a, "reason", "") or "sampler_unavailable")))
        if b_kind == FingerprintKind.UNAVAILABLE.value:
            return _diagnose(left, right, _unavailable(str(getattr(b, "reason", "") or "sampler_unavailable")))

        # Fingerprinting supplies a discovered payload size. It must remain
        # plausible against both pre-download reports, and two independently
        # sampled representations must agree on the actual payload length.
        if (
            not _sample_size_compatible_with_reports(a.total_bytes, left, right)
            or not _sample_size_compatible_with_reports(b.total_bytes, left, right)
            or int(a.total_bytes) != int(b.total_bytes)
        ):
            return _diagnose(left, right, _unavailable("size_disagreement"))
        actual_size = int(a.total_bytes)

        if a_kind == FingerprintKind.FULL_CONTENT_SAMPLE.value and b_kind == FingerprintKind.FULL_CONTENT_SAMPLE.value:
            if str(a.signature).strip() and a.signature == b.signature:
                return _diagnose(
                    left,
                    right,
                    EquivalenceEvidence(EvidenceKind.FULL_CONTENT_SAMPLE, actual_size),
                )
            return _diagnose(left, right, _unavailable("sample_mismatch"))

        a_prefix = str(getattr(a, "prefix_signature", "") or (
            a.signature if a_kind == FingerprintKind.PREFIX_CONTENT_SAMPLE.value else ""))
        b_prefix = str(getattr(b, "prefix_signature", "") or (
            b.signature if b_kind == FingerprintKind.PREFIX_CONTENT_SAMPLE.value else ""))
        if a_prefix and a_prefix == b_prefix:
            reason = str(getattr(a, "reason", "") or getattr(b, "reason", "") or "range_ignored")
            return _diagnose(
                left,
                right,
                EquivalenceEvidence(EvidenceKind.PREFIX_CONTENT_SAMPLE, actual_size, reason),
            )
        return _diagnose(left, right, _unavailable("sample_mismatch"))
    except TimeoutError:
        return _diagnose(left, right, _unavailable("timeout"))
    except socket.gaierror:
        return _diagnose(left, right, _unavailable("dns_failure"))
    except Exception:
        # An exception from a sampling-capable executor may be transient. The
        # bounded cohort policy decides whether another proof opportunity exists.
        return _diagnose(left, right, _unavailable("sampler_unavailable"))


async def shared_size(left, right, registry) -> int | None:
    """Compatibility seam: only strong/full evidence may merge one artifact."""
    evidence = await shared_evidence(left, right, registry)
    return evidence.total_bytes if evidence.proves_individual else None
