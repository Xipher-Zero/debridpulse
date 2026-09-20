"""The one provider-neutral owner of payload-size evidence policy.

Three related questions share one rule and therefore one module:

* what counts as a *known positive* size (``positive_size``);
* whether two positive size reports are plausibly the same payload
  (``reported_sizes_compatible`` -- a pairing/plausibility rule, never identity
  proof on its own);
* how a provider-reported size, an executor-observed size and the artifact's
  own recorded size reconcile at completion time (``classify_size_evidence``).

Nothing here knows about a provider, an executor, a path, a repository or a
presentation surface: sizes are evidence with provenance, and this module only
classifies how two such facts relate. Deciding which compatible size is
materially real is the job of local-payload verification
(``transfers.filesystem``), which consumes this classification; it is never
decided by preferring one source over the other.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# Two known positive sizes are compatible when they differ by at most 0.1 %
# of the larger one AND by at most 512 MiB.
REPORTED_SIZE_RELATIVE_SCALE = 1000
REPORTED_SIZE_MAX_DELTA_BYTES = 512 * 1024 * 1024


def positive_size(value) -> int | None:
    """``value`` as a known positive byte count, else ``None``.

    Zero, negative, non-numeric and ``bool`` inputs are absence of size
    knowledge, never a byte count: a defaulted ``0`` is "unknown", not
    affirmative evidence of an empty payload, and ``True`` is not one byte.
    """
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def reported_sizes_compatible(left_size, right_size) -> bool:
    """Return whether two known positive sizes are plausibly the same payload.

    This is deliberately only a plausibility rule. Identity still requires
    canonical strong integrity or bounded content evidence, and completion
    still requires the stable local payload to verify.
    """
    left = positive_size(left_size)
    right = positive_size(right_size)
    if left is None or right is None:
        return False
    delta = abs(left - right)
    larger = max(left, right)
    return (
        delta * REPORTED_SIZE_RELATIVE_SCALE <= larger
        and delta <= REPORTED_SIZE_MAX_DELTA_BYTES
    )


class SizeEvidenceKind(StrEnum):
    EXACT = "exact"                                  # both known, equal
    REPORTED_ONLY = "reported_only"                  # only the provider-side size is known
    OBSERVED_ONLY = "observed_only"                  # only the executor-side size is known
    RECORDED_ONLY = "recorded_only"                  # no provider report and no final total; the recorded size
    BOUNDED_CONFLICT = "bounded_conflict"            # both known, unequal, compatible
    INCOMPATIBLE_CONFLICT = "incompatible_conflict"  # both known, unequal, incompatible
    UNKNOWN = "unknown"                              # neither is known


@dataclass(frozen=True)
class SizeEvidence:
    kind: SizeEvidenceKind
    reported: int | None = None
    observed: int | None = None
    recorded: int | None = None

    @property
    def verifiable_sizes(self) -> tuple[int, ...]:
        """Positive sizes the stable local payload may legitimately equal.

        One size unless the evidence is a bounded conflict, in which case both
        asserted sizes are candidates and the payload decides between them. An
        incompatible conflict yields none: neither number may be silently
        chosen, so nothing is verifiable and the caller must fail closed.
        """
        if self.kind in {SizeEvidenceKind.EXACT, SizeEvidenceKind.REPORTED_ONLY}:
            return (self.reported,)
        if self.kind == SizeEvidenceKind.OBSERVED_ONLY:
            return (self.observed,)
        if self.kind == SizeEvidenceKind.RECORDED_ONLY:
            return (self.recorded,)
        if self.kind == SizeEvidenceKind.BOUNDED_CONFLICT:
            return (self.reported, self.observed)
        return ()


def classify_size_evidence(reported, observed, recorded=0) -> SizeEvidence:
    """Relate a provider-reported size to an executor-observed final total.

    ``reported`` is what the provider/resolver said about the selected
    candidate; ``observed`` is what the executor finally measured. ``recorded``
    is the artifact's own previously persisted size -- bookkeeping seeded from
    an earlier report, a bounded sample or the first executor progress -- never
    an upstream report. Precedence: a positive final total is reconciled with
    the provider report and is never rejected or overridden by bookkeeping;
    with no final total, a positive provider report is the only verifiable size;
    ``recorded`` verifies only when there is neither -- it may fill a complete
    absence of fresh evidence but never overrides a provider report.
    """
    known_reported = positive_size(reported)
    known_observed = positive_size(observed)
    known_recorded = positive_size(recorded)
    if known_observed is None:
        if known_reported is not None:
            # A positive provider report stays the only verifiable size;
            # recorded bookkeeping is kept for diagnostics, never as an
            # alternative accepted size.
            return SizeEvidence(SizeEvidenceKind.REPORTED_ONLY, reported=known_reported, recorded=known_recorded)
        if known_recorded is not None:
            return SizeEvidence(SizeEvidenceKind.RECORDED_ONLY, recorded=known_recorded)
        return SizeEvidence(SizeEvidenceKind.UNKNOWN)
    if known_reported is None:
        return SizeEvidence(SizeEvidenceKind.OBSERVED_ONLY, observed=known_observed, recorded=known_recorded)
    if known_reported == known_observed:
        return SizeEvidence(SizeEvidenceKind.EXACT, known_reported, known_observed, known_recorded)
    kind = (
        SizeEvidenceKind.BOUNDED_CONFLICT
        if reported_sizes_compatible(known_reported, known_observed)
        else SizeEvidenceKind.INCOMPATIBLE_CONFLICT
    )
    return SizeEvidence(kind, known_reported, known_observed, known_recorded)
