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
from dataclasses import dataclass
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
    if state.manifest_id is None or state.manifest_file_count <= 1:
        return False
    if state.auto_offer_dismissed_at is not None:
        return False
    return state.hold_until is not None and now < state.hold_until


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
