"""Provider-neutral file-selection manifest policy and normalization.

This is the canonical owner of ALL-vs-explicit acquisition policy, the
60-second automatic manifest-presentation window, the 120-second cached
decision hold, neutral manifest/entry identity, early-manifest validation, and
executable-manifest reconciliation.

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


# Two distinct, non-overlapping timing windows (specification section 4).
AUTO_MANIFEST_WINDOW_SECONDS = 60.0
IMMEDIATE_DECISION_HOLD_SECONDS = 120.0

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
    initially_available: bool
    manifest_wait_until: float          # absolute end of the 60s auto-offer window
    hold_until: float | None            # absolute 120s cached decision deadline
    manifest_id: str | None             # a validated selectable manifest is bound
    manifest_file_count: int            # 0 when no manifest is bound
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
    materialization is ever held, and only for an initially-available,
    file-manifest-capable resource inside the bounded windows.
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

    if state.initially_available:
        if has_multi:
            if state.hold_until is not None and now < state.hold_until:
                return GateEvaluation(SelectionGate.WAIT_FOR_DECISION)
            return GateEvaluation(
                SelectionGate.PROCEED, SelectionDecision.ALL, DecisionReason.DECISION_TIMEOUT,
            )
        if now < state.manifest_wait_until:
            return GateEvaluation(SelectionGate.WAIT_FOR_MANIFEST)
        return GateEvaluation(
            SelectionGate.PROCEED, SelectionDecision.ALL, DecisionReason.MANIFEST_TIMEOUT,
        )

    # Uncached / preparing resources are never locally held for selection. When
    # the engine finally has an executable manifest and no explicit subset was
    # confirmed, default ALL settles here.
    return GateEvaluation(
        SelectionGate.PROCEED, SelectionDecision.ALL, DecisionReason.DEFAULT_MATERIALIZATION,
    )


def selection_mutable(state: SelectionWindowState) -> bool:
    """Selection is mutable only until executable child materialization commits."""
    return state.manifest_committed_at is None


def auto_offer_active(state: SelectionWindowState, now: float) -> bool:
    """Whether a browser should automatically present the selector right now.

    True inside the 60-second window for any eligible multi-file manifest, and
    for the entire duration of an active cached decision hold so a cold-loading
    or reconnecting browser still recovers the open offer.
    """
    if state.manifest_committed_at is not None or state.decision != SelectionDecision.PENDING:
        return False
    if state.manifest_id is None or state.manifest_file_count <= 1:
        return False
    if state.auto_offer_dismissed_at is not None:
        return False
    if state.hold_until is not None and now < state.hold_until:
        return True
    return now < state.manifest_wait_until


def manifest_wait_deadline(now: float) -> float:
    return float(now) + AUTO_MANIFEST_WINDOW_SECONDS


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
