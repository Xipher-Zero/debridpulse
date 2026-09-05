"""Conservative logical-artifact equivalence across independent source scopes."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from transfers.contracts import CandidateSampling
from transfers.models import FingerprintKind


_STRONG_INTEGRITY_ALGORITHMS = {"sha256", "sha512", "blake2b", "blake2s"}


class EvidenceKind(str):
    STRONG_INTEGRITY = "strong_integrity"
    FULL_CONTENT_SAMPLE = "full_content_sample"
    PREFIX_CONTENT_SAMPLE = "prefix_content_sample"
    UNAVAILABLE = "unavailable"


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
    return "candidate", str(candidate.id)


def comparable(left, right):
    """Cheap exact pairing prefilter only; this never proves equivalence by itself."""
    if str(left.id) == str(right.id):
        return False
    if _source_key(left) == _source_key(right):
        return False
    if not logical_key(left) or logical_key(left) != logical_key(right):
        return False
    if left.expected_bytes <= 0 or right.expected_bytes <= 0:
        return False
    return left.expected_bytes == right.expected_bytes


def _unavailable(reason: str) -> EquivalenceEvidence:
    return EquivalenceEvidence(EvidenceKind.UNAVAILABLE, reason=reason)


def _fingerprint_kind(value) -> str:
    try:
        return str(value.kind.value)
    except AttributeError:
        return str(getattr(value, "kind", FingerprintKind.FULL_CONTENT_SAMPLE))


async def shared_evidence(left, right, registry) -> EquivalenceEvidence:
    """Return structured provider-neutral evidence without speculative merging."""
    if not comparable(left, right):
        return _unavailable("pairing_mismatch")
    size = left.expected_bytes
    if _strong_integrity(left) & _strong_integrity(right):
        return EquivalenceEvidence(EvidenceKind.STRONG_INTEGRITY, size)
    try:
        first, second = registry.executor_for(left), registry.executor_for(right)
        if not isinstance(first, CandidateSampling) or not isinstance(second, CandidateSampling):
            return _unavailable("sampler_unavailable")
        a, b = await asyncio.gather(first.fingerprint(left), second.fingerprint(right))
        if a is None or b is None:
            return _unavailable("sampler_unavailable")
        if a.total_bytes != size or b.total_bytes != size:
            reason = str(getattr(a, "reason", "") or getattr(b, "reason", "") or "size_disagreement")
            return _unavailable(reason)
        if _fingerprint_kind(a) == FingerprintKind.UNAVAILABLE.value:
            return _unavailable(str(getattr(a, "reason", "") or "sampler_unavailable"))
        if _fingerprint_kind(b) == FingerprintKind.UNAVAILABLE.value:
            return _unavailable(str(getattr(b, "reason", "") or "sampler_unavailable"))

        a_kind = _fingerprint_kind(a)
        b_kind = _fingerprint_kind(b)
        if a_kind == FingerprintKind.FULL_CONTENT_SAMPLE.value and b_kind == FingerprintKind.FULL_CONTENT_SAMPLE.value:
            if str(a.signature).strip() and a.signature == b.signature:
                return EquivalenceEvidence(EvidenceKind.FULL_CONTENT_SAMPLE, size)
            return _unavailable("sample_mismatch")

        a_prefix = str(getattr(a, "prefix_signature", "") or (
            a.signature if a_kind == FingerprintKind.PREFIX_CONTENT_SAMPLE.value else ""))
        b_prefix = str(getattr(b, "prefix_signature", "") or (
            b.signature if b_kind == FingerprintKind.PREFIX_CONTENT_SAMPLE.value else ""))
        if a_prefix and a_prefix == b_prefix:
            reason = str(getattr(a, "reason", "") or getattr(b, "reason", "") or "range_ignored")
            return EquivalenceEvidence(EvidenceKind.PREFIX_CONTENT_SAMPLE, size, reason)
        return _unavailable("sample_mismatch")
    except Exception:
        # Inability to prove equivalence is a normal independent-download path.
        return _unavailable("sampler_unavailable")


async def shared_size(left, right, registry) -> int | None:
    """Compatibility seam: only strong/full evidence may merge one artifact."""
    evidence = await shared_evidence(left, right, registry)
    return evidence.total_bytes if evidence.proves_individual else None
