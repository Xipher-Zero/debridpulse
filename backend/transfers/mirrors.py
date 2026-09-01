"""Conservative logical-artifact equivalence across independent source scopes."""
from transfers.contracts import CandidateSampling


def comparable(left, right):
    if not left.source_identity or not right.source_identity:
        return False
    if not left.source_identity.key or not right.source_identity.key:
        return False
    if left.source_identity.scope != right.source_identity.scope or left.source_identity.key == right.source_identity.key:
        return False
    if left.name.strip().casefold() != right.name.strip().casefold():
        return False
    if left.expected_bytes <= 0 or right.expected_bytes <= 0:
        return False
    delta = abs(left.expected_bytes - right.expected_bytes)
    return delta <= 512 * 1024 * 1024 and delta * 1000 <= max(left.expected_bytes, right.expected_bytes)


async def shared_size(left, right, registry) -> int | None:
    if not comparable(left, right):
        return None
    if left.expected_bytes == right.expected_bytes:
        return left.expected_bytes
    try:
        first, second = registry.executor_for(left), registry.executor_for(right)
        if not isinstance(first, CandidateSampling) or not isinstance(second, CandidateSampling):
            return None
        a = await first.fingerprint(left)
        b = await second.fingerprint(right)
        if a is None or a != b or a.total_bytes <= 0:
            return None
        for candidate in (left, right):
            delta = abs(a.total_bytes - candidate.expected_bytes)
            if delta > 512 * 1024 * 1024 or delta * 1000 > max(a.total_bytes, candidate.expected_bytes):
                return None
        return a.total_bytes
    except Exception:
        # An unprovable relationship is not a failed transfer. Keep the sources
        # separate; each still passes normal dispatch security and validation.
        return None
