"""Provider-neutral file-selection manifest policy and normalization.

This is the canonical owner of ALL-vs-explicit acquisition policy, the three
independent lifecycle timing dimensions (provider preparation, the 120-second
user-decision hold measured from the first actionable multi-file manifest, and
the bounded 60-second post-AVAILABLE manifest-acquisition grace), neutral
manifest/entry identity, early-manifest validation, and executable-manifest
reconciliation.

Timing model (specification sections 4-5, Torrent/Magnet File-Selection
Lifecycle Correction):

* Provider preparation is eager and open-ended. A resource may remain PREPARING
  for far longer than 60 seconds without losing the interactive selection
  opportunity. No timer runs while the provider is preparing and no manifest is
  available.
* The 120-second hold is the maximum unanswered USER-DECISION time. It is
  anchored exactly once, to the arrival of the first actionable multi-file
  manifest, whether the provider resource is PREPARING or AVAILABLE at that
  moment. It is never a provider-preparation timeout, a manifest-discovery
  timeout from submission, or a minimum delay before execution.
* The 60-second window is ONLY a bounded manifest-acquisition grace that starts
  when an interactive FILE_MANIFEST-capable resource is first observed
  executable/AVAILABLE while a usable manifest is still unobtainable. It never
  runs while the provider is PREPARING. If a usable manifest arrives inside it,
  the 120-second decision hold begins from that arrival; if it expires with no
  usable manifest, the selection settles ALL.

Invariants enforced here:

* No provider-native or executor-native vocabulary. This module must never
  contain concrete integration names or provider-native status/error strings.
* No wall clock. Every timing value is derived from an injected core ``now``
  and reasoned about only as an absolute deadline. This module imports neither
  ``time`` nor ``datetime``.
* The provider supplies facts; core decides. Nothing here consults a provider
  or an executor.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import PurePosixPath
from uuid import NAMESPACE_URL, uuid5

from transfers.filesystem import safe_name
from transfers.models import FileManifest, SourceEntry


# Three independent, non-overlapping timing dimensions (specification sections
# 4-5). ``POST_AVAILABLE_MANIFEST_GRACE_SECONDS`` is the bounded grace that only
# runs after an AVAILABLE resource still cannot supply a usable manifest — never
# a submission-relative or resource-creation-relative window.
POST_AVAILABLE_MANIFEST_GRACE_SECONDS = 60.0
IMMEDIATE_DECISION_HOLD_SECONDS = 120.0


# Neutral submission-intent policy (correction section 6). Interactive
# file-selection is entered ONLY when the submitter explicitly opts in; it is
# never inferred from an SSE connection, a browser session, a user agent, or a
# ``source`` string. Historical/headless callers that send the unchanged
# request shape therefore always default to ALL.
SELECTION_MODE_ALL = "all"
SELECTION_MODE_INTERACTIVE = "interactive"
SELECTION_MODES = frozenset({SELECTION_MODE_ALL, SELECTION_MODE_INTERACTIVE})
DEFAULT_SELECTION_MODE = SELECTION_MODE_ALL


def normalize_selection_mode(value: str | None) -> str:
    """Return a validated ``selection_mode``; ``None``/blank -> the ALL default.

    Raises :class:`ValueError` for any other value so the API boundary rejects
    it with ordinary request validation. ``selection_mode`` is a per-submission
    policy only; it never participates in source dedupe/fingerprint identity.
    """
    text = str(value).strip().lower() if value is not None else ""
    if not text:
        return DEFAULT_SELECTION_MODE
    if text not in SELECTION_MODES:
        raise ValueError(f"Unsupported selection_mode: {value!r}")
    return text


# Bounded early-manifest payload limits (specification section 23).
MAX_MANIFEST_ENTRIES = 20000
MAX_MANIFEST_PATH_LENGTH = 1024
MAX_SELECTION_ENTRIES = MAX_MANIFEST_ENTRIES

_MANIFEST_NAMESPACE = "file-manifest"
_ENTRY_NAMESPACE = "file-manifest-entry"
_SELECTION_NAMESPACE = "file-selection"


def selection_identity(request_id: str, provider_resource_id: str) -> str:
    """Core-generated identity for one selection generation.

    Selection provenance follows the provider resource that produced the file
    facts, not merely the durable request that led to it. A request that is
    re-resolved onto a new provider resource gets a new generation; the prior
    generation stays as historical truth and is never inherited.
    """
    return uuid5(
        NAMESPACE_URL, f"{_SELECTION_NAMESPACE}:{request_id}:{provider_resource_id}",
    ).hex


class SelectionDecision(StrEnum):
    PENDING = "pending"
    EXPLICIT = "explicit"
    ALL = "all"


class DecisionReason(StrEnum):
    CONFIRMED = "confirmed"
    CLOSED = "closed"
    DECISION_TIMEOUT = "decision_timeout"
    MANIFEST_TIMEOUT = "manifest_timeout"
    SINGLE_FILE = "single_file"
    DEFAULT_MATERIALIZATION = "default_materialization"
    # A promoted backup's generation carries the operator's earlier explicit
    # subset forward (proven against the new manifest, never broadened).
    INHERITED = "inherited"


class Continuity(StrEnum):
    """Whether a decomposition generation's members are its root's
    established decomposition. Only a ``PROVEN``, committed generation is ever
    runnable; a ``HELD`` one changed nothing and starts nothing."""
    PROVEN = "proven"
    HELD = "held"


class SelectionGate(StrEnum):
    """Whether executable child fan-out may proceed for a request."""
    WAIT_FOR_MANIFEST = "wait_for_manifest"
    WAIT_FOR_DECISION = "wait_for_decision"
    PROCEED = "proceed"


class SelectionOutcome(StrEnum):
    """Neutral result of a mutating file-selection command.

    The dedicated API owner maps these to transport status codes; core never
    speaks HTTP. ``NOT_FOUND`` -> 404, ``CONFLICT`` -> 409 (stale manifest /
    already committed / no longer mutable), ``INVALID`` -> 422.
    """
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    INVALID = "invalid"


@dataclass(frozen=True)
class SelectionCommandResult:
    outcome: str
    detail: str = ""
    decision: str | None = None
    manifest_id: str | None = None
    committed: bool = False


class ManifestInvalid(ValueError):
    """The neutral early manifest is not a usable selectable tree.

    Before explicit confirmation this is non-fatal: the selector is simply
    unavailable and default ALL continues to govern the full transfer.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SelectionUnprovable(RuntimeError):
    """A confirmed explicit subset can no longer be proven; fail closed.

    A confirmed explicit subset must never degrade back to ALL. Callers map
    this to a neutral core conflict category and contain the affected request.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def decomposition_continuity(established: list[tuple[str, int]],
                             authorized: tuple[SourceEntry, ...]) -> str | None:
    """Prove a new generation's authorized members are exactly the root's
    established logical decomposition: ``None`` when proven, else the bounded
    reason it is not.

    ``established`` is ``(relative_path, expected_bytes)`` per established
    member. A BIJECTION is required on the normalized relative path -- the one
    identity a member's ``uuid5(root, path)`` and its target derive from --
    with a compatible size on every pair (equal whenever both are known, the
    rule ``reconcile_executable_subset`` applies). A duplicate on either side,
    a missing established member, an unexplained new member or a size
    conflict is not the same decomposition: nothing is matched by position,
    basename or provider, and nothing is accepted in part."""
    def keyed(pairs, side):
        result = {}
        for path, size in pairs:
            try:
                key = normalize_relative_path(path)
            except ManifestInvalid:
                return None, f"{side}_path_invalid"
            if key in result:
                return None, f"{side}_path_duplicate"
            result[key] = int(size or 0)
        return result, None

    old, reason = keyed(established, "established")
    if reason:
        return reason
    new, reason = keyed([(entry.relative_path, entry.expected_bytes) for entry in authorized], "replacement")
    if reason:
        return reason
    if old.keys() - new.keys():
        return "established_member_missing"
    if new.keys() - old.keys():
        return "unexplained_member"
    if any(old[key] > 0 and new[key] > 0 and old[key] != new[key] for key in old):
        return "member_size_conflict"
    return None


def normalize_relative_path(value: str) -> str:
    """Deterministic sanitized POSIX relative path used for identity and collision.

    Mirrors the destination path-safety rules already applied to executable
    ``SourceEntry`` members: POSIX separators, no absolute paths, no ``..``,
    per-part name sanitation. Raises :class:`ManifestInvalid` for an unusable
    path so an optional malformed early manifest never reaches persistence.
    """
    text = str(value or "").replace("\\", "/").strip()
    if not text:
        raise ManifestInvalid("empty_path")
    raw = PurePosixPath(text)
    if raw.is_absolute() or not raw.parts or ".." in raw.parts:
        raise ManifestInvalid("unsafe_path")
    parts = [safe_name(part) for part in raw.parts]
    if any(not part for part in parts):
        raise ManifestInvalid("unsafe_path")
    normalized = "/".join(parts)
    if len(normalized) > MAX_MANIFEST_PATH_LENGTH:
        raise ManifestInvalid("path_too_long")
    return normalized


def collection_member_paths(root_name: str, members) -> tuple[str, ...]:
    """THE collection-member path rule: each member's path INSIDE the root.

    ``members`` is every member of one provider resource as the path segments
    the provider's own native shape gave it, in native order; the result is one
    POSIX relative path per member, in that same order -- an ordinal is never
    moved, so a provider that pairs members with links by position still can.
    Materialization applies the durable collection root exactly once, so a
    member path must never contain it.

    Exactly one top-level wrapper is removed, and only on authoritative
    evidence: ``root_name`` is the resource's authoritative collection name,
    EVERY member's first segment is exactly that name, and every member keeps
    at least one segment after it. A directory merely shared by every member,
    a lone top-level directory, a matching basename or a lookalike name proves
    nothing and stays. Nesting is preserved; an inner directory of the same
    name is member hierarchy and stays.

    Fails closed with :class:`ManifestInvalid` on a member with no segments or
    any empty, ``.``, ``..`` or separator-bearing segment: an unsafe native path
    is never repaired into an executable one.
    """
    paths = []
    for member in members:
        parts = tuple(member)
        if not parts or any(not isinstance(part, str) or part in {"", ".", ".."} or "/" in part or "\\" in part
                            for part in parts):
            raise ManifestInvalid("unsafe_path")
        paths.append(parts)
    wrapped = bool(root_name) and bool(paths) and all(len(parts) > 1 and parts[0] == root_name for parts in paths)
    return tuple("/".join(parts[1:] if wrapped else parts) for parts in paths)


@dataclass(frozen=True)
class CanonicalEntry:
    entry_id: str
    ordinal: int          # original provider order, kept only for presentation
    name: str
    relative_path: str    # normalized
    expected_bytes: int


@dataclass(frozen=True)
class CanonicalManifest:
    manifest_id: str
    manifest_digest: str
    provider_resource_id: str
    entries: tuple[CanonicalEntry, ...]   # sorted by normalized relative path

    @property
    def file_count(self) -> int:
        return len(self.entries)

    @property
    def total_bytes(self) -> int:
        return sum(max(0, entry.expected_bytes) for entry in self.entries)

    def entry_ids(self) -> frozenset[str]:
        return frozenset(entry.entry_id for entry in self.entries)


def entry_identity(provider_resource_id: str, normalized_relative_path: str) -> str:
    """Core-generated logical entry identity. Size is never part of it."""
    return uuid5(
        NAMESPACE_URL,
        f"{_ENTRY_NAMESPACE}:{provider_resource_id}:{normalized_relative_path}",
    ).hex


def _digest(pairs: list[tuple[str, int]]) -> str:
    hasher = hashlib.sha256()
    for path, size in pairs:
        hasher.update(path.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(str(int(size)).encode("ascii"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def canonicalize_manifest(provider_resource_id: str, manifest: FileManifest) -> CanonicalManifest:
    """Validate and canonicalize a neutral early manifest into stable identity.

    * Deterministic sort by normalized relative path before the digest, so a
      provider reordering the same files alone yields the same manifest id.
    * ``manifest_digest`` covers normalized path + expected byte count for
      every entry, so ``same path + changed size`` is a new manifest version
      while pathname identity of an entry is preserved.
    """
    provider_resource_id = str(provider_resource_id or "").strip()
    if not provider_resource_id:
        raise ManifestInvalid("missing_provider_resource")
    if manifest is None or not manifest.entries:
        raise ManifestInvalid("empty_manifest")
    if len(manifest.entries) > MAX_MANIFEST_ENTRIES:
        raise ManifestInvalid("too_many_entries")

    seen: set[str] = set()
    prepared: list[tuple[str, str, int, int]] = []  # normalized, name, size, original ordinal
    for original_ordinal, entry in enumerate(manifest.entries):
        normalized = normalize_relative_path(entry.relative_path)
        size = entry.expected_bytes
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ManifestInvalid("negative_size")
        if normalized in seen:
            raise ManifestInvalid("duplicate_path")
        seen.add(normalized)
        name = str(entry.name or "").strip() or PurePosixPath(normalized).name
        prepared.append((normalized, name, int(size), original_ordinal))

    prepared.sort(key=lambda item: item[0])
    digest = _digest([(normalized, size) for normalized, _name, size, _ord in prepared])
    manifest_id = uuid5(
        NAMESPACE_URL, f"{_MANIFEST_NAMESPACE}:{provider_resource_id}:{digest}",
    ).hex
    entries = tuple(
        CanonicalEntry(
            entry_id=entry_identity(provider_resource_id, normalized),
            ordinal=original_ordinal,
            name=name,
            relative_path=normalized,
            expected_bytes=size,
        )
        for normalized, name, size, original_ordinal in prepared
    )
    return CanonicalManifest(manifest_id, digest, provider_resource_id, entries)


@dataclass(frozen=True)
class SelectionWindowState:
    """Durable file-selection facts a neutral gate decision needs."""
    decision: str
    initially_available: bool            # provenance: resource was AVAILABLE at generation creation
    resource_available: bool             # the provider resource is executable/AVAILABLE right now
    available_grace_until: float | None  # absolute end of the 60s post-AVAILABLE manifest grace;
    #                                      None means the grace has not started (still PREPARING,
    #                                      or a usable manifest is already bound)
    hold_until: float | None             # absolute 120s user-decision deadline, anchored once to the
    #                                      first actionable multi-file manifest
    manifest_id: str | None              # a validated selectable manifest is bound
    manifest_file_count: int             # 0 when no manifest is bound
    manifest_committed_at: float | None
    auto_offer_dismissed_at: float | None
    # The bound manifest's members are independent resources
    # (``FileManifest.independent_members``): only an explicit Confirm ever
    # authorizes them -- no single-entry ALL, no decision timeout, no ALL on
    # Close. The decision waits, durably, for as long as it takes.
    explicit_only: bool = False


@dataclass(frozen=True)
class GateEvaluation:
    gate: SelectionGate
    # When a still-``pending`` decision should settle now, the neutral result:
    resolve_decision: str | None = None
    resolve_reason: str | None = None


def evaluate_gate(state: SelectionWindowState, now: float) -> GateEvaluation:
    """Pure neutral decision: may executable child fan-out proceed for a request?

    Provider-side acquisition is never governed here. Only local executable
    materialization is ever held.

    Three independent dimensions (specification sections 4-5, 32):

    * Provider preparation is eager. While the resource is not yet
      executable/AVAILABLE and no usable manifest exists, this gate WAITs with
      no countdown of any kind — the user's decision clock has not started.
    * The 120-second user-decision hold, once ``record_file_manifest`` has
      anchored it to the first actionable multi-file manifest, is honored here
      as ``WAIT_FOR_DECISION`` regardless of whether the resource was PREPARING
      or AVAILABLE when the manifest arrived. ``DECISION_TIMEOUT`` is emitted
      only when that persisted hold actually expired while still pending.
    * The 60-second post-AVAILABLE grace applies only when the resource is
      AVAILABLE but no usable multi-file manifest is obtainable yet. It never
      runs while PREPARING. ``MANIFEST_TIMEOUT`` is emitted only when that grace
      expired.
    """
    if state.manifest_committed_at is not None:
        return GateEvaluation(SelectionGate.PROCEED)
    if state.decision == SelectionDecision.EXPLICIT:
        return GateEvaluation(SelectionGate.PROCEED)
    if state.decision == SelectionDecision.ALL:
        return GateEvaluation(SelectionGate.PROCEED)

    has_manifest = state.manifest_id is not None
    has_multi = has_manifest and state.manifest_file_count > 1

    if has_manifest and state.explicit_only:
        # A collection of independent members: whatever its size, and however
        # long the operator takes, nothing settles but an explicit Confirm.
        return GateEvaluation(SelectionGate.WAIT_FOR_DECISION)

    if has_manifest and not has_multi:
        return GateEvaluation(
            SelectionGate.PROCEED, SelectionDecision.ALL, DecisionReason.SINGLE_FILE,
        )

    if has_multi and state.hold_until is not None:
        # The 120-second user-decision hold is authoritative. It is never
        # extended, restarted, or capped by any provider-side window.
        if now < state.hold_until:
            return GateEvaluation(SelectionGate.WAIT_FOR_DECISION)
        return GateEvaluation(
            SelectionGate.PROCEED, SelectionDecision.ALL, DecisionReason.DECISION_TIMEOUT,
        )

    # Either no manifest is bound yet, or (only for a pre-correction row) a
    # multi-file manifest exists without its co-established hold. In both cases
    # the user's decision clock has not started.
    if not state.resource_available:
        # Provider preparation is still in progress. No decision deadline and no
        # manifest-grace countdown — provider work proceeds on its own cadence
        # for as long as it needs (specification sections 4, 12, 19).
        return GateEvaluation(SelectionGate.WAIT_FOR_MANIFEST)

    # The resource is executable/AVAILABLE but a usable multi-file manifest is
    # not obtainable yet: the bounded post-AVAILABLE manifest-acquisition grace
    # (specification section 5, 20). ``available_grace_until`` is None until the
    # first AVAILABLE-without-manifest observation anchors it.
    if state.available_grace_until is None or now < state.available_grace_until:
        return GateEvaluation(SelectionGate.WAIT_FOR_MANIFEST)
    return GateEvaluation(
        SelectionGate.PROCEED, SelectionDecision.ALL, DecisionReason.MANIFEST_TIMEOUT,
    )


def selection_mutable(state: SelectionWindowState) -> bool:
    """Selection is mutable only until executable child materialization commits."""
    return state.manifest_committed_at is None


def auto_offer_active(state: SelectionWindowState, now: float) -> bool:
    """Whether a browser should automatically present the selector right now.

    True for the entire duration of an active user-decision hold — and only
    then — so a cold-loading or reconnecting browser still recovers the open
    offer for exactly as long as the decision can still be made. The hold is
    established the moment the first actionable multi-file manifest arrives, so
    there is no separate pre-hold presentation window.
    """
    if state.manifest_committed_at is not None or state.decision != SelectionDecision.PENDING:
        return False
    if state.auto_offer_dismissed_at is not None or state.manifest_id is None:
        return False
    if state.explicit_only:
        # No decision deadline: the offer stands until it is answered or closed.
        return state.manifest_file_count >= 1
    if state.manifest_file_count <= 1:
        return False
    return state.hold_until is not None and now < state.hold_until


def file_selection_affordance(
    manifest_id: str | None, decision: str | None, committed_at, file_count: int, *,
    explicit_only: bool = False,
) -> str:
    """Canonical ``none``/``pending_manifest``/``choose``/``change`` classification.

    Derived only from durable ``transfer_file_selections`` facts (specification
    section 10): whether a selection generation exists at all, whether it is
    still mutable (``committed_at is None``), whether a manifest is bound, its
    file count, and its decision. This is the single semantic owner shared by
    the bounded list projection (``api.operational_downloads``) and the
    fresh-click read model (``TransferRepository.file_selection_presentation``);
    the browser only renders the field this returns and never reconstructs it
    from ``eligible``/``mutable``/``manifest_id``/``file_count``/``decision``.
    """
    if manifest_id is None and decision is None:
        # No selection generation exists for this transfer at all.
        return "none"
    if committed_at is not None:
        # Executable child materialization already committed -- locked.
        return "none"
    if manifest_id is None and str(decision or "") == SelectionDecision.ALL:
        # Decided ALL before any manifest bound (a generation that was never
        # an operator selection, or a manifest that never came): nothing to
        # choose, and nothing pending to wait for.
        return "none"
    if manifest_id is None:
        # A generation exists (torrent/magnet resolution in progress) but no
        # usable manifest has arrived yet.
        return "pending_manifest"
    if file_count <= 1 and not explicit_only:
        # Single-file torrent/magnet: no picker action (Section 6.9). A
        # one-member collection is still the operator's to choose.
        return "none"
    if str(decision or "") == SelectionDecision.EXPLICIT:
        return "change"
    return "choose"


def manifest_grace_deadline(now: float) -> float:
    return float(now) + POST_AVAILABLE_MANIFEST_GRACE_SECONDS


def decision_hold_deadline(now: float) -> float:
    return float(now) + IMMEDIATE_DECISION_HOLD_SECONDS


def validate_selection_ids(manifest: CanonicalManifest, entry_ids) -> tuple[str, ...]:
    """Neutral Confirm-request validation against a specific manifest version."""
    ids = list(entry_ids or ())
    if not ids:
        raise ManifestInvalid("empty_selection")
    if len(ids) > MAX_SELECTION_ENTRIES:
        raise ManifestInvalid("too_many_selected")
    seen: set[str] = set()
    for value in ids:
        text = str(value)
        if text in seen:
            raise ManifestInvalid("duplicate_selection")
        seen.add(text)
    known = manifest.entry_ids()
    if not seen.issubset(known):
        raise ManifestInvalid("unknown_selection_entry")
    # Preserve manifest order for deterministic persistence and presentation.
    return tuple(entry.entry_id for entry in manifest.entries if entry.entry_id in seen)


def reconcile_executable_subset(
    selected: list[tuple[str, int]],
    executable_entries: tuple[SourceEntry, ...],
) -> tuple[SourceEntry, ...]:
    """Prove a confirmed explicit subset against the full executable manifest.

    ``selected`` is ``(normalized_relative_path, early_expected_bytes)`` for
    each confirmed entry. Returns the matching executable ``SourceEntry`` subset
    or raises :class:`SelectionUnprovable`. Never returns the full list, never
    broadens.
    """
    late_by_path: dict[str, SourceEntry] = {}
    for entry in executable_entries:
        try:
            normalized = normalize_relative_path(entry.relative_path)
        except ManifestInvalid as exc:
            raise SelectionUnprovable(f"executable_path_{exc.reason}") from exc
        if normalized in late_by_path:
            raise SelectionUnprovable("duplicate_executable_path")
        late_by_path[normalized] = entry

    proven: list[SourceEntry] = []
    for normalized, early_size in selected:
        match = late_by_path.get(normalized)
        if match is None:
            raise SelectionUnprovable("selected_path_missing")
        late_size = int(match.expected_bytes or 0)
        if int(early_size or 0) > 0 and late_size > 0 and int(early_size) != late_size:
            raise SelectionUnprovable("selected_size_conflict")
        proven.append(match)
    return tuple(proven)


@dataclass(frozen=True)
class InheritedMigration:
    """An inherited selection proven across a replacement resource whose
    paths differ from the established ones: ``logical`` are the executable
    members to fan out -- the replacement's own material at the ESTABLISHED
    logical coordinates -- and ``provenance`` the same members at the
    replacement manifest's own coordinates, for its selection record."""
    logical: tuple[SourceEntry, ...]
    provenance: tuple[SourceEntry, ...]


class CoordinateInterpretation(StrEnum):
    """The only coordinate interpretations the bounded correspondence proof
    (``coordinate_correspondence``) ever evaluates between two complete file
    lists of the same torrent: the paths are the same, or exactly ONE leading
    collection directory -- the same one for every member -- is present on
    one side only. Nothing else (no deeper stripping, no prefix chosen for
    looking like a name, no basename, ordinal or fuzzy matching)."""
    UNCHANGED = "unchanged"
    REPLACEMENT_WRAPPED = "replacement_wrapped"
    ESTABLISHED_WRAPPED = "established_wrapped"


@dataclass(frozen=True)
class CoordinateCorrespondence:
    """The one total bijection between two complete file lists that a single
    interpretation proved: ``mapping`` takes each member's normalized path on
    the established side to its normalized path on the replacement side."""
    interpretation: CoordinateInterpretation
    mapping: dict[str, str]


def _complete_members(entries, reason: str) -> dict[str, int]:
    """``normalized path -> exact size`` of one COMPLETE file list, or
    :class:`SelectionUnprovable` (``coordinate_<reason>_*``) when any member
    is unsafe, duplicated or of unknown (zero) size, or the list is empty."""
    members: dict[str, int] = {}
    for path, size in entries:
        try:
            normalized = normalize_relative_path(path)
        except ManifestInvalid:
            raise SelectionUnprovable(f"coordinate_{reason}_unsafe_path") from None
        if normalized in members:
            raise SelectionUnprovable(f"coordinate_{reason}_duplicate_path")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise SelectionUnprovable(f"coordinate_{reason}_unknown_size")
        members[normalized] = size
    if not members:
        raise SelectionUnprovable(f"coordinate_{reason}_manifest_missing")
    return members


def _without_collection_directory(members: dict[str, int]) -> dict[str, str] | None:
    """``inner path -> path`` when EVERY member lies under one and the same
    leading directory with at least one segment after it; ``None`` otherwise.
    The directory is whatever every member shares at depth one -- it is
    proven a collection wrapper only by the complete correspondence the
    caller then demands, never by its name."""
    leading, inner = None, {}
    for path in members:
        head, separator, rest = path.partition("/")
        if not separator or not rest or (leading is not None and head != leading):
            return None
        leading = head
        inner[rest] = path
    return inner


def coordinate_correspondence(established: list[tuple[str, int]],
                              replacement: list[tuple[str, int]]) -> CoordinateCorrespondence:
    """THE bounded coordinate-interpretation proof between two COMPLETE file
    lists already bound to one torrent identity by the caller: the one total
    one-to-one correspondence of every member, or :class:`SelectionUnprovable`
    with a bounded ``coordinate_*`` reason.

    Each ``CoordinateInterpretation`` is evaluated on the whole of both lists
    (never on a selected subset): it holds only when its paths are exactly the
    other side's -- case-sensitive, as complete sets, no member missing or
    extra -- and every corresponding pair has the same exact positive size.
    Exactly one interpretation can hold for two finite lists (a list can never
    equal itself with one more leading directory), so a second success is
    refused as ``coordinate_ambiguous`` rather than resolved by order.

    Paths and sizes prove which member is which only once the lists are known
    to describe the same torrent; they are no evidence of that by themselves
    and never of the bytes' integrity."""
    before = _complete_members(established, "established")
    after = _complete_members(replacement, "replacement")
    proven: list[CoordinateCorrespondence] = []
    sized_mismatch = False
    candidates = [(CoordinateInterpretation.UNCHANGED, {path: path for path in before}, after)]
    replacement_inner = _without_collection_directory(after)
    if replacement_inner is not None:
        candidates.append((CoordinateInterpretation.REPLACEMENT_WRAPPED,
                           {path: replacement_inner.get(path) for path in before}, after))
    established_inner = _without_collection_directory(before)
    if established_inner is not None:
        candidates.append((CoordinateInterpretation.ESTABLISHED_WRAPPED,
                           {path: inner for inner, path in established_inner.items()}, after))
    for interpretation, mapping, other in candidates:
        if len(mapping) != len(before) or any(target is None or target not in other
                                              for target in mapping.values()):
            continue
        if set(mapping.values()) != set(other) or len(set(mapping.values())) != len(mapping):
            continue
        if any(before[source] != other[target] for source, target in mapping.items()):
            sized_mismatch = True
            continue
        proven.append(CoordinateCorrespondence(interpretation, dict(mapping)))
    if len(proven) > 1:
        raise SelectionUnprovable("coordinate_ambiguous")
    if not proven:
        raise SelectionUnprovable("coordinate_size_conflict" if sized_mismatch else "coordinate_no_correspondence")
    return proven[0]


def migrate_by_correspondence(
    selected: list[tuple[str, int]],
    predecessor: list[tuple[str, int]],
    correspondence: CoordinateCorrespondence,
    executable_entries: tuple[SourceEntry, ...],
    *,
    established: list[tuple[str, int]],
) -> InheritedMigration:
    """Carry an inherited selection onto a replacement through a proven
    ``correspondence`` (predecessor path -> replacement path, complete), or
    raise :class:`SelectionUnprovable` with a bounded ``coordinate_*`` reason.

    Exactly the ``selected`` members -- never broader: each is the
    predecessor's member, its replacement member must be in the replacement's
    executable list at the corresponding path with that exact size, and its
    logical path stays where the root already established it (the
    predecessor's path where that IS an established member, otherwise the
    existing ``established_logical_paths`` rule; the predecessor's path when
    nothing is established yet). Provider-native coordinates stay the
    executable entries' own."""
    sizes = _complete_members(predecessor, "established")
    executable: dict[str, SourceEntry] = {}
    for entry in executable_entries:
        try:
            normalized = normalize_relative_path(entry.relative_path)
        except ManifestInvalid:
            raise SelectionUnprovable("coordinate_executable_unsafe_path") from None
        if normalized in executable:
            raise SelectionUnprovable("coordinate_executable_duplicate_path")
        executable[normalized] = entry
    provenance, predecessor_paths = [], []
    for path, _size in selected:
        try:
            normalized = normalize_relative_path(path)
        except ManifestInvalid:
            raise SelectionUnprovable("coordinate_selected_unsafe_path") from None
        if normalized not in sizes or normalized not in correspondence.mapping:
            raise SelectionUnprovable("coordinate_selected_not_in_predecessor")
        entry = executable.get(correspondence.mapping[normalized])
        if entry is None:
            raise SelectionUnprovable("coordinate_executable_path_missing")
        if isinstance(entry.expected_bytes, bool) or entry.expected_bytes != sizes[normalized]:
            raise SelectionUnprovable("coordinate_executable_size_conflict")
        provenance.append(entry)
        predecessor_paths.append(normalized)
    if len(set(predecessor_paths)) != len(predecessor_paths):
        raise SelectionUnprovable("coordinate_selected_duplicate_path")
    if established:
        established_paths = set()
        for path, _size in established:
            try:
                established_paths.add(normalize_relative_path(path))
            except ManifestInvalid:
                raise SelectionUnprovable("fallback_established_unsafe_path") from None
        logical_paths = (predecessor_paths if all(path in established_paths for path in predecessor_paths)
                         else established_logical_paths(selected, established))
    else:
        logical_paths = predecessor_paths
    return InheritedMigration(tuple(replace(entry, relative_path=path) for entry, path in zip(provenance, logical_paths)),
                              tuple(provenance))


def compose_correspondences(anchor_to_predecessor: CoordinateCorrespondence,
                            anchor_to_replacement: CoordinateCorrespondence) -> CoordinateCorrespondence:
    """``predecessor path -> replacement path`` through one shared anchor
    list (an uploaded torrent's own member tree) both were proven complete
    and one-to-one against. The composite records the replacement-side
    interpretation."""
    mapping = {anchor_to_predecessor.mapping[anchor]: anchor_to_replacement.mapping[anchor]
               for anchor in anchor_to_predecessor.mapping}
    return CoordinateCorrespondence(anchor_to_replacement.interpretation, mapping)


def _migration_keys(entries: list[tuple[str, int]]) -> dict[tuple[str, int], str]:
    """``(basename, exact size) -> normalized path`` of one COMPLETE manifest,
    or :class:`SelectionUnprovable` when that is not a unique identity of
    every member."""
    paths: set[str] = set()
    keys: dict[tuple[str, int], str] = {}
    for path, size in entries:
        try:
            normalized = normalize_relative_path(path)
        except ManifestInvalid:
            raise SelectionUnprovable("fallback_unsafe_path") from None
        if normalized in paths:
            raise SelectionUnprovable("fallback_duplicate_path")
        paths.add(normalized)
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise SelectionUnprovable("fallback_unknown_size")
        key = (PurePosixPath(normalized).name, size)
        if key in keys:
            raise SelectionUnprovable("fallback_duplicate_identity")
        keys[key] = normalized
    return keys


def established_logical_paths(selected: list[tuple[str, int]],
                              established: list[tuple[str, int]]) -> tuple[str, ...]:
    """The established logical path of each ``selected`` member, in order.

    ``established`` is the members the root already fanned out
    (``(recorded path, size)``): its logical decomposition. Each selected
    member is the one established member with its exact (case-sensitive
    basename, size > 0) identity, and the two sets must be exactly equal --
    never a path lookup, so a predecessor that itself reported other paths (an
    earlier migration's) can never redefine where the members live. Raises
    :class:`SelectionUnprovable` with a bounded ``fallback_established_*``
    reason otherwise."""
    paths: set[str] = set()
    recorded: dict[tuple[str, int], str] = {}
    for path, size in established:
        try:
            normalized = normalize_relative_path(path)
        except ManifestInvalid:
            raise SelectionUnprovable("fallback_established_unsafe_path") from None
        if normalized in paths:
            raise SelectionUnprovable("fallback_established_duplicate_path")
        paths.add(normalized)
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise SelectionUnprovable("fallback_established_unknown_size")
        key = (PurePosixPath(normalized).name, size)
        if key in recorded:
            raise SelectionUnprovable("fallback_established_duplicate_identity")
        recorded[key] = path
    keys = []
    for path, size in selected:
        try:
            normalized = normalize_relative_path(path)
        except ManifestInvalid:
            raise SelectionUnprovable("fallback_established_unsafe_path") from None
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise SelectionUnprovable("fallback_established_unknown_size")
        key = (PurePosixPath(normalized).name, size)
        if key not in recorded:
            raise SelectionUnprovable("fallback_established_member_missing")
        keys.append(key)
    if len(set(keys)) != len(keys):
        raise SelectionUnprovable("fallback_established_duplicate_identity")
    if set(keys) != recorded.keys():
        raise SelectionUnprovable("fallback_established_member_set_mismatch")
    return tuple(recorded[key] for key in keys)


def intent_in_predecessor(intent: list[tuple[str, int]],
                          predecessor: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """The live transfer's intent (logical coordinates) as the predecessor
    generation's own members (``(path, size)`` of its whole manifest), in
    intent order -- the selection a successor proves.

    By exact normalized path when every intent member is there; otherwise
    by the unique (case-sensitive basename, size > 0) identity of each --
    the predecessor may hold members the operator has since deselected, but
    every intent member must be exactly one of its members. A predecessor
    with no recorded manifest has nothing to map: the intent's own
    coordinates are proved as they are. Raises :class:`SelectionUnprovable`
    with a bounded ``fallback_intent_*`` reason otherwise."""
    if not predecessor:
        return list(intent)
    by_path: dict[str, tuple[str, int]] = {}
    for path, size in predecessor:
        try:
            normalized = normalize_relative_path(path)
        except ManifestInvalid:
            raise SelectionUnprovable("fallback_unsafe_path") from None
        if normalized in by_path:
            raise SelectionUnprovable("fallback_duplicate_path")
        by_path[normalized] = (path, size)
    try:
        wanted = [normalize_relative_path(path) for path, _size in intent]
    except ManifestInvalid:
        raise SelectionUnprovable("fallback_intent_unsafe_path") from None
    if all(path in by_path for path in wanted):
        return [by_path[path] for path in wanted]
    by_key: dict[tuple[str, int], tuple[str, int]] = {}
    for normalized, (path, size) in by_path.items():
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise SelectionUnprovable("fallback_unknown_size")
        key = (PurePosixPath(normalized).name, size)
        if key in by_key:
            raise SelectionUnprovable("fallback_duplicate_identity")
        by_key[key] = (path, size)
    mapped, seen = [], set()
    for normalized, (_path, size) in zip(wanted, intent):
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise SelectionUnprovable("fallback_intent_unknown_size")
        key = (PurePosixPath(normalized).name, size)
        if key in seen:
            raise SelectionUnprovable("fallback_duplicate_identity")
        seen.add(key)
        if key not in by_key:
            raise SelectionUnprovable("fallback_intent_member_missing")
        mapped.append(by_key[key])
    return mapped


def migrate_inherited_subset(
    selected: list[tuple[str, int]],
    predecessor: list[tuple[str, int]],
    replacement: list[tuple[str, int]],
    executable_entries: tuple[SourceEntry, ...],
    *,
    predecessor_fingerprints: frozenset[str],
    replacement_fingerprints: frozenset[str],
    established: list[tuple[str, int]],
) -> InheritedMigration:
    """Carry an inherited explicit selection onto a replacement resource that
    reports the same files under different paths (one that lost the
    directories, say), or raise :class:`SelectionUnprovable` with the
    bounded reason it cannot.

    Only after ``reconcile_executable_subset`` could not prove it by exact
    path, and only on proof, never on likeness:

    * the same source: each resource's own reported fingerprints are one
      value, and the same value;
    * the same COMPLETE member set: the predecessor's and the replacement's
      whole manifests (``(path, size)``) are equally long and biject on
      ``(case-sensitive basename, exact size > 0)``, every key unique;
    * each selected member's replacement is in the replacement's executable
      list at that manifest's own path with that exact size.

    Never broader than ``selected``. Where the root already fanned out
    (``established``: ``(recorded path, size)`` of each member), the logical
    paths are those members', recovered by identity
    (``established_logical_paths``) -- never by the predecessor's path; only
    a root with no established members takes the predecessor's paths."""
    before = {value.strip().casefold() for value in predecessor_fingerprints if value.strip()}
    after = {value.strip().casefold() for value in replacement_fingerprints if value.strip()}
    if not before or not after:
        raise SelectionUnprovable("fallback_missing_fingerprint")
    if len(before) != 1 or before != after:
        raise SelectionUnprovable("fallback_fingerprint_mismatch")
    if not predecessor or not replacement:
        raise SelectionUnprovable("fallback_manifest_missing")
    old = _migration_keys(predecessor)
    new = _migration_keys(replacement)
    if len(old) != len(new):
        raise SelectionUnprovable("fallback_manifest_count_mismatch")
    if old.keys() != new.keys():
        raise SelectionUnprovable("fallback_member_set_mismatch")
    key_of = {path: key for key, path in old.items()}
    executable: dict[str, SourceEntry] = {}
    for entry in executable_entries:
        try:
            normalized = normalize_relative_path(entry.relative_path)
        except ManifestInvalid:
            raise SelectionUnprovable("fallback_unsafe_path") from None
        if normalized in executable:
            raise SelectionUnprovable("fallback_duplicate_path")
        executable[normalized] = entry
    provenance, predecessor_paths = [], []
    for path, _size in selected:
        try:
            key = key_of[normalize_relative_path(path)]
        except (ManifestInvalid, KeyError):
            raise SelectionUnprovable("fallback_selected_not_in_predecessor") from None
        entry = executable.get(new[key])
        if entry is None:
            raise SelectionUnprovable("fallback_executable_path_missing")
        if isinstance(entry.expected_bytes, bool) or entry.expected_bytes != key[1]:
            raise SelectionUnprovable("fallback_executable_size_conflict")
        provenance.append(entry)
        predecessor_paths.append(old[key])
    logical_paths = established_logical_paths(selected, established) if established else predecessor_paths
    return InheritedMigration(tuple(replace(entry, relative_path=path) for entry, path in zip(provenance, logical_paths)),
                              tuple(provenance))
