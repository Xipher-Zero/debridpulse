"""DebridPulse-owned artifact material truth (pure model, no I/O).

One logical artifact owns one material state: the byte ranges of its final
file DebridPulse is willing to reuse, whatever source, transport or executor
wrote them. The canonical contract is half-open byte ranges ``[start, end)``;
the durable owner is ``artifact_material_state`` (written only by the
transfer repository), never an executor control file and never file length.

Semantics, all expressed over one normalized range set:

* ``VALID``    -- the stored ranges: checkpointed, durably committed material.
* ``IN_FLIGHT``-- a current writer's authorized ranges minus VALID. Derived,
                  never stored, so an unclean stop leaves nothing to trust.
* ``UNKNOWN``  -- everything else. Physical bytes may exist there; they carry
                  no DebridPulse meaning until a checkpoint commits them.

Geometry v1 is an internal persistence/planning grain (1 MiB): committed
material is aligned inward to it, except that a range may end exactly at the
artifact's known end of file. It is never an operator setting, and a future
geometry needs its own explicit migration rather than a reinterpretation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

GEOMETRY_VERSION = 1
CHUNK_BYTES = 1 << 20
# Upper bound of an authorization whose end is not yet known (unknown size).
OPEN_END = (1 << 63) - 1

Range = tuple[int, int]
Ranges = tuple[Range, ...]


class MaterialClass(StrEnum):
    VALID = "valid"
    IN_FLIGHT = "in_flight"
    UNKNOWN = "unknown"


def normalize(ranges) -> Ranges:
    """Sorted, merged, non-empty ``[start, end)`` ranges of non-negative ints."""
    items = []
    for item in ranges or ():
        start, end = item
        if isinstance(start, bool) or isinstance(end, bool):
            raise ValueError("Material ranges are integer byte offsets")
        start, end = int(start), int(end)
        if start < 0 or end < start:
            raise ValueError("Material ranges must be non-negative half-open intervals")
        if end > start:
            items.append((start, end))
    items.sort()
    merged: list[list[int]] = []
    for start, end in items:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return tuple((start, end) for start, end in merged)


def union(left, right) -> Ranges:
    return normalize((*normalize(left), *normalize(right)))


def intersect(left, right) -> Ranges:
    result = []
    right = normalize(right)
    for start, end in normalize(left):
        for other_start, other_end in right:
            low, high = max(start, other_start), min(end, other_end)
            if high > low:
                result.append((low, high))
    return normalize(result)


def subtract(left, right) -> Ranges:
    result = []
    right = normalize(right)
    for start, end in normalize(left):
        cursor = start
        for other_start, other_end in right:
            if other_end <= cursor or other_start >= end:
                continue
            if other_start > cursor:
                result.append((cursor, other_start))
            cursor = max(cursor, other_end)
            if cursor >= end:
                break
        if cursor < end:
            result.append((cursor, end))
    return normalize(result)


def total(ranges) -> int:
    return sum(end - start for start, end in normalize(ranges))


def contiguous_prefix(ranges) -> int:
    """End of the range that starts at byte 0 (0 when there is none)."""
    ranges = normalize(ranges)
    return ranges[0][1] if ranges and ranges[0][0] == 0 else 0


def align_inward(ranges, *, end_of_file: int | None = None, chunk: int = CHUNK_BYTES) -> Ranges:
    """Shrink every range to whole geometry chunks; a range may keep its true
    end only when that end is the artifact's known end of file."""
    aligned = []
    for start, end in normalize(ranges):
        low = -(-start // chunk) * chunk
        high = end if end_of_file is not None and end == end_of_file else (end // chunk) * chunk
        if high > low:
            aligned.append((low, high))
    return normalize(aligned)


def align_down(offset: int, alignment: int) -> int:
    alignment = max(1, int(alignment))
    return (max(0, int(offset)) // alignment) * alignment


def encode(ranges) -> str:
    return json.dumps([list(item) for item in normalize(ranges)], separators=(",", ":"))


def decode(value) -> Ranges:
    if not value:
        return ()
    loaded = json.loads(value) if isinstance(value, str) else value
    return normalize(tuple(tuple(item) for item in loaded))


def summary(ranges, *, limit: int = 8) -> list[list[int]]:
    """A bounded, human-auditable excerpt of a range set (trace/provenance)."""
    return [list(item) for item in normalize(ranges)[:limit]]


Members = tuple[tuple[str, Ranges], ...]


def encode_members(members, identities=()) -> str:
    """Per-member material of a collection artifact: ``{relative path:
    {"valid": [[start, end], ...], "identity": "<dev>:<ino>"}}``."""
    known = dict(identities)
    return json.dumps({member: {"valid": [list(item) for item in normalize(ranges)],
                                "identity": str(known.get(member, ""))}
                       for member, ranges in sorted(dict(members).items()) if normalize(ranges)},
                      separators=(",", ":"), sort_keys=True)


def decode_members(value) -> tuple[Members, tuple[tuple[str, str], ...]]:
    loaded = json.loads(value) if isinstance(value, str) and value else (value or {})
    members = tuple(sorted((str(member), decode(entry.get("valid"))) for member, entry in loaded.items()))
    identities = tuple(sorted((str(member), str(entry.get("identity") or "")) for member, entry in loaded.items()))
    return tuple(item for item in members if item[1]), identities


@dataclass(frozen=True)
class MaterialState:
    """The canonical material truth of one logical artifact.

    ``expected_size`` is ``None`` while no trustworthy total size exists; it is
    read from the artifact's own canonical size fact, never stored twice.
    ``material_generation`` changes only when previously valid physical
    content may no longer mean the same thing; ``writer_generation`` changes
    whenever writer authority changes. They are independent counters."""
    artifact_id: int
    material_generation: int
    geometry_version: int
    valid: Ranges
    destination: str
    writer_generation: int = 0
    expected_size: int | None = None
    destination_identity: str = ""
    checkpoint_at: float | None = None
    # A COLLECTION artifact's material, per member file (relative path); a
    # FILE artifact's material is ``valid`` itself. Generations stay per
    # artifact: one owner, however many files it spans.
    members: Members = ()
    member_identities: tuple[tuple[str, str], ...] = ()

    def member_valid(self, member: str) -> Ranges:
        return dict(self.members).get(member, ())

    @property
    def valid_bytes(self) -> int:
        return total(self.valid) + sum(total(ranges) for _member, ranges in self.members)

    @property
    def safe_prefix(self) -> int:
        return contiguous_prefix(self.valid)

    @property
    def complete(self) -> bool:
        return bool(self.expected_size) and self.valid == ((0, self.expected_size),)

    @property
    def percentage(self) -> float | None:
        """DP-valid completion; unavailable (``None``) without a trustworthy size."""
        if not self.expected_size:
            return None
        return min(100.0, total(intersect(self.valid, ((0, self.expected_size),))) / self.expected_size * 100)

    def in_flight(self, authorized) -> Ranges:
        return subtract(authorized, self.valid)

    def classify(self, offset: int, authorized=()) -> MaterialClass:
        probe = ((int(offset), int(offset) + 1),)
        if intersect(self.valid, probe):
            return MaterialClass.VALID
        if intersect(authorized, probe):
            return MaterialClass.IN_FLIGHT
        return MaterialClass.UNKNOWN
