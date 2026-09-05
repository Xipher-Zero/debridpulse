"""Conservative logical-artifact equivalence across independent source scopes."""
from __future__ import annotations

import asyncio

from transfers.contracts import CandidateSampling


_STRONG_INTEGRITY_ALGORITHMS = {"sha256", "sha512", "blake2b", "blake2s"}


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


def _compatible_size(left: int, right: int) -> bool:
    if left <= 0 or right <= 0:
        return False
    delta = abs(left - right)
    return delta <= 512 * 1024 * 1024 and delta * 1000 <= max(left, right)


def comparable(left, right):
    """Cheap pairing prefilter only; this never proves equivalence by itself."""
    if str(left.id) == str(right.id):
        return False
    if (left.source_identity is not None and right.source_identity is not None
            and left.source_identity == right.source_identity):
        return False
    if left.name.strip().casefold() != right.name.strip().casefold():
        return False
    return _compatible_size(left.expected_bytes, right.expected_bytes)


async def shared_size(left, right, registry) -> int | None:
    """Return proven payload size or None when equivalence cannot be established.

    Strong provider-neutral integrity identity is sufficient only when expected
    size also agrees.  Otherwise both executor samplers must independently
    return the same non-empty fingerprint and that sampled size must remain
    compatible with both provider expectations.  Equal expected size alone is
    deliberately not identity evidence.
    """
    if not comparable(left, right):
        return None
    if left.expected_bytes == right.expected_bytes and _strong_integrity(left) & _strong_integrity(right):
        return left.expected_bytes
    try:
        first, second = registry.executor_for(left), registry.executor_for(right)
        if not isinstance(first, CandidateSampling) or not isinstance(second, CandidateSampling):
            return None
        a, b = await asyncio.gather(first.fingerprint(left), second.fingerprint(right))
        if a is None or b is None or a != b or a.total_bytes <= 0 or not str(a.signature).strip():
            return None
        if not _compatible_size(a.total_bytes, left.expected_bytes):
            return None
        if not _compatible_size(a.total_bytes, right.expected_bytes):
            return None
        return a.total_bytes
    except Exception:
        # Inability to prove equivalence is a normal independent-download path.
        return None
