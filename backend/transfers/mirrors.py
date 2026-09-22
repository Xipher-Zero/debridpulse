"""Conservative logical-artifact equivalence across independent source scopes."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import socket

from transfers.models import ExecutionSubject
from transfers.models import FingerprintKind, InputRequirement
from transfers import size_evidence


logger = logging.getLogger(__name__)
_STRONG_INTEGRITY_ALGORITHMS = {"sha256", "sha512", "blake2", "blake2b", "blake2s"}
_TRANSIENT_REASONS = frozenset({
    "timeout", "dns_failure", "sampler_unavailable",
    # Real provider capabilities sometimes answer a bounded Range probe with
    # an ambiguous transport fact rather than trustworthy evidence (DP 1.0.12
    # false-negative repair, Section 4D/Case 4). Neither means the artifacts
    # differ; both must reuse the existing bounded proof-retry machinery
    # instead of becoming a permanent independent-artifact decision on the
    # first ambiguous observation.
    "range_unsupported", "incomplete_representation",
})
_CONTRADICTORY_REASONS = frozenset({"size_disagreement", "sample_mismatch", "integrity_mismatch"})
# No proof can be established for this candidate by construction, as opposed
# to a proof attempt that ran and yielded unusable material evidence:
#   * ``sampler_unsupported`` -- no sampling capability exists at all (no
#     capable executor, or a sampler that reports no fingerprint for this
#     route); nothing was sampled, so it says nothing about the material;
#   * ``ambiguous_mapping`` -- the candidate matches more than one canonical
#     target, so no unique mapping can ever be proven (never guess).
_PROOF_UNAVAILABLE_REASONS = frozenset({"sampler_unsupported", "ambiguous_mapping"})
# ``input_required`` -- the sampler reached the candidate and reports that its
# evidence exists but acquiring it definitively requires transient operator
# input. That is neither proof of difference nor absence of any possible
# proof, so it is deliberately in none of the sets above or below: it is an
# unresolved, non-retryable pairing that holds the writer barrier. For a
# request's OWN candidate the cohort owner turns it into the one INPUT_REQUIRED
# lifecycle (``EvidenceContext.requirement_for``); for a peer candidate no
# challenge can be raised on this request's behalf, so the hold stands.
_NONPAIRING_REASONS = frozenset({
    "same_candidate", "non_independent_source", "logical_pairing_mismatch",
    "size_disagreement", "sample_mismatch", "integrity_mismatch",
})
# Unresolved-proof reasons for which the bounded sampler DID reach a material
# endpoint and received an ordinary response, yet could not establish
# trustworthy identity or completeness (e.g. a short 200 that is implausible as
# the whole declared representation). Proof is unavailable; the material is not
# thereby known to be unusable, so once bounded proof has exhausted with no
# canonical writer anywhere, one provisional writer may still make progress and
# final material verification remains the arbiter. Deliberately narrow:
#   * ``range_ignored`` (e.g. a 200 with Content-Length 0) is a degenerate
#     response, not a usable representation -- never eligible (transfer 286);
#   * ``range_unsupported`` is emitted by the sampler for ANY non-200/206
#     status, HTTP errors included, so it does not show a usable endpoint was
#     reached -- not eligible;
#   * security/policy rejections (``destination_rejected``, ...) and every
#     contradictory or structural reason are never eligible.
_PROVISIONAL_WRITER_REASONS = frozenset({"incomplete_representation"})


class EvidenceKind(str):
    STRONG_INTEGRITY = "strong_integrity"
    FULL_CONTENT_SAMPLE = "full_content_sample"
    PREFIX_CONTENT_SAMPLE = "prefix_content_sample"
    # Resolver-attested identity (DP 1.0.12 canonical architecture correction,
    # Workstream B): both independent-source candidates carry a
    # ``ResolverArtifactIdentityEvidence`` whose normalized resolved name and
    # exact positive byte size agree. A durably distinguishable member of this
    # same taxonomy -- never a parallel signal invisible to code that already
    # inspects ``EvidenceKind`` for presentation/audit/diagnostics -- proving
    # one canonical artifact without live content sampling.
    RESOLVER_ATTESTED = "resolver_attested"
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
        return self.kind in {
            EvidenceKind.STRONG_INTEGRITY, EvidenceKind.FULL_CONTENT_SAMPLE, EvidenceKind.RESOLVER_ATTESTED,
        }

    @property
    def proves_collection_member(self) -> bool:
        return self.kind in {
            EvidenceKind.STRONG_INTEGRITY,
            EvidenceKind.FULL_CONTENT_SAMPLE,
            EvidenceKind.PREFIX_CONTENT_SAMPLE,
            EvidenceKind.RESOLVER_ATTESTED,
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

    @property
    def eligible_for_provisional_writer_after_exhaustion(self) -> bool:
        """True when this failed proof says "identity cannot be proven
        automatically" about a candidate whose endpoint the sampler did reach
        (``_PROVISIONAL_WRITER_REASONS``), rather than "this source is not a
        usable writer". Only meaningful once bounded proof has exhausted; the
        consumer additionally requires that no canonical writer exists. Never
        implies identity or independence."""
        return self.kind == EvidenceKind.UNAVAILABLE and self.reason in _PROVISIONAL_WRITER_REASONS

    @property
    def proof_structurally_unavailable(self) -> bool:
        """True when no proof can be established for this candidate by
        construction (no sampling capability, or no unique mapping is
        possible), as opposed to a proof attempt that ran and yielded unusable
        material evidence. Orthogonal to ``retryable`` and to
        ``unresolved_pairing``: the consumer decides what structural absence
        of proof permits."""
        return self.kind == EvidenceKind.UNAVAILABLE and self.reason in _PROOF_UNAVAILABLE_REASONS


class EvidenceContext:
    """Fingerprint acquisitions already made within ONE cohort coordination
    decision (``transfers.cohorts.coordinate_collection`` uses exactly one per
    call; the materialization owner creates it and drops it on return).

    It only avoids asking the same executor for the same candidate's
    fingerprint twice inside that decision; what a fingerprint MEANS is still
    decided, every time, by ``shared_evidence`` / ``self_evidence`` below, so
    classification is identical with or without it. It is not a truth
    authority: it is never persisted, never module-level, never shared between
    decisions, and nothing reads lifecycle state from it -- the next scheduler
    decision starts empty and re-acquires evidence normally.

    Entries are keyed by the candidate's immutable durable identity together
    with the byte size the sampler is given for it (the two inputs that select
    a sample) -- never by a name, host or other display string. A failed
    acquisition is remembered for the decision exactly like a successful one,
    so one decision observes each candidate once; whether another attempt is
    made at all remains the bounded proof-retry policy's decision.

    The same acquisition may need transient operator input. A decision that
    continues an answered evidence challenge is given that input for exactly
    the challenged candidate identity, so the SAME acquisition continues
    (``CandidateSamplingContinuation``) inside the ordinary decision. The
    context also records which candidates answered with an
    ``InputRequirement`` so the cohort owner can route a request's own
    requirement to the one INPUT_REQUIRED lifecycle. The input is borrowed:
    the context never retains, copies or discards it."""

    __slots__ = ("_fingerprints", "_inputs", "_requirements", "_proven")

    def __init__(self, inputs=None):
        self._fingerprints = {}
        self._inputs = dict(inputs or {})
        self._requirements = {}
        self._proven = {}

    async def fingerprint(self, executor, candidate):
        key = (str(candidate.id), max(0, int(candidate.expected_bytes or 0)))
        if key not in self._fingerprints:
            submitted = self._inputs.get(key[0])
            subject = ExecutionSubject.of(candidate)
            try:
                if submitted is not None and executor.capabilities.transient_input:
                    sample = await executor.fingerprint_with_input(subject, submitted)
                    if sample is not None and not isinstance(sample, InputRequirement) \
                            and _fingerprint_kind(sample) != FingerprintKind.UNAVAILABLE.value:
                        self._proven[key[0]] = sample
                else:
                    sample = _retained(candidate, await executor.fingerprint(subject))
                self._fingerprints[key] = (sample, None)
            except Exception as exc:
                self._fingerprints[key] = (None, exc)
            sample = self._fingerprints[key][0]
            # Only a candidate that advertises every requested input method may
            # ever ask its operator; anything else stays an unresolved proof.
            if isinstance(sample, InputRequirement) and {item.method for item in sample.methods} <= set(
                    candidate.accepted_input_methods):
                self._requirements[key[0]] = (executor.descriptor.id, sample)
        sample, error = self._fingerprints[key]
        if error is not None:
            raise error
        return sample

    def requirement_for(self, candidates):
        """The first of ``candidates`` (in the given order) whose evidence
        acquisition requires operator input in this decision, as
        ``(candidate, integration_id, requirement)``; ``None`` otherwise."""
        for candidate in candidates:
            found = self._requirements.get(str(candidate.id))
            if found is not None:
                return candidate, found[0], found[1]
        return None

    def proven_evidence(self, candidate_id):
        """The usable evidence this decision's transient input produced for
        ``candidate_id``, or ``None``."""
        return self._proven.get(str(candidate_id))


def _retained(candidate, sample):
    """A live acquisition that would need operator input this decision does
    not hold falls back to the candidate's retained neutral content evidence
    (``TransferCandidate.content_evidence``), when it has any. The credential
    that once proved it is never involved."""
    if isinstance(sample, InputRequirement) and candidate.content_evidence is not None:
        return candidate.content_evidence
    return sample


async def _fingerprint(executor, candidate, context: EvidenceContext | None):
    if context is None:
        return _retained(candidate, await executor.fingerprint(ExecutionSubject.of(candidate)))
    return await context.fingerprint(executor, candidate)


def _sampler(candidate, registry):
    """The core-selected claimant for the candidate's pre-materialization
    subject -- the SAME router dispatch uses -- when it declares neutral
    candidate sampling; ``None`` otherwise. Never a scheme or name lookup."""
    executor = registry.executor_for_subject(ExecutionSubject.of(candidate))
    return executor if executor is not None and executor.capabilities.candidate_sampling else None


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


def _sample_size_compatible_with_reports(actual_size, left, right) -> bool:
    """A discovered actual size must respect every *known* advance report.

    An unknown reported size (0/absent) imposes no consistency requirement of
    its own -- it is "unknown, not proof of difference" (DP 1.0.12 Section 9),
    never a reason to treat otherwise-agreeing sampled content as mismatched.
    """
    actual = size_evidence.positive_size(actual_size)
    if actual is None:
        return False
    for reported in (left.expected_bytes, right.expected_bytes):
        known = size_evidence.positive_size(reported)
        if known is not None and not size_evidence.reported_sizes_compatible(actual, known):
            return False
    return True


def pairing_failure(left, right) -> str:
    """Return the exact cheap pairing rejection reason; empty means pairable."""
    if str(left.id) == str(right.id):
        return "same_candidate"
    if _source_key(left) == _source_key(right):
        return "non_independent_source"
    if not logical_key(left) or logical_key(left) != logical_key(right):
        return "logical_pairing_mismatch"
    left_size = size_evidence.positive_size(left.expected_bytes)
    right_size = size_evidence.positive_size(right.expected_bytes)
    # An unknown reported size (common for ordinary General HTTP candidates,
    # which never populate expected_bytes) is unknown, not proof of
    # difference -- it must not permanently block a pair from ever reaching
    # bounded content evidence. Only two *known* positive reports that are
    # themselves incompatible are cheap, structural negative evidence.
    if left_size is not None and right_size is not None and not size_evidence.reported_sizes_compatible(left_size, right_size):
        return "size_disagreement"
    return ""


def comparable(left, right):
    """Cheap bounded pairing prefilter only; this never proves equivalence by itself."""
    return not pairing_failure(left, right)


def _unavailable(reason: str) -> EquivalenceEvidence:
    return EquivalenceEvidence(EvidenceKind.UNAVAILABLE, reason=reason)


def _normalized_resolver_name(value: str) -> str:
    return str(value or "").strip().casefold()


def _resolver_attested_evidence(left, right) -> EquivalenceEvidence | None:
    """Cheap direct proof from independent resolver-attested identity facts.

    Only ``ResolverArtifactIdentityEvidence`` -- a fact the resolver/provider
    itself asserted (``transfers.models.TransferCandidate
    .resolver_identity_evidence``) -- may satisfy this; ordinary reported
    ``expected_bytes``/``name`` (the generic-HTTP case) never does, because
    this function never reads them. Independence of the two source identities
    is already guaranteed by the caller (``pairing_failure`` rejects
    ``non_independent_source`` before this runs). Returns ``None`` -- not
    ``UNAVAILABLE`` -- when no resolver-attested proof applies, so the caller
    falls through to the ordinary sampling-evidence path unmodified.
    """
    left_evidence = left.resolver_identity_evidence
    right_evidence = right.resolver_identity_evidence
    if left_evidence is None or right_evidence is None:
        return None
    left_name = _normalized_resolver_name(left_evidence.resolved_name)
    right_name = _normalized_resolver_name(right_evidence.resolved_name)
    if not left_name or left_name != right_name:
        return None
    left_bytes = size_evidence.positive_size(left_evidence.exact_bytes)
    right_bytes = size_evidence.positive_size(right_evidence.exact_bytes)
    if left_bytes is None or right_bytes is None or left_bytes != right_bytes:
        return None
    return EquivalenceEvidence(EvidenceKind.RESOLVER_ATTESTED, left_bytes)


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


async def shared_evidence(left, right, registry, context: EvidenceContext | None = None) -> EquivalenceEvidence:
    """Return structured provider-neutral evidence without speculative merging.

    ``context`` only lets one coordination decision reuse a fingerprint it has
    already acquired; it never alters the evidence returned."""
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

    resolver_evidence = _resolver_attested_evidence(left, right)
    if resolver_evidence is not None:
        return _diagnose(left, right, resolver_evidence)

    try:
        # Sampling executors are selected by the one subject-claim router
        # (``registry.executor_for_subject``) used for dispatch.
        first, second = _sampler(left, registry), _sampler(right, registry)
        if first is None or second is None:
            return _diagnose(left, right, _unavailable("sampler_unsupported"))
        a, b = await asyncio.gather(_fingerprint(first, left, context), _fingerprint(second, right, context))
        # A sampler returning None has no proof capability for this candidate.
        # Temporary acquisition failures must cross the contract explicitly as
        # UNAVAILABLE with a retryable reason such as timeout/dns_failure.
        if a is None or b is None:
            return _diagnose(left, right, _unavailable("sampler_unsupported"))
        if isinstance(a, InputRequirement) or isinstance(b, InputRequirement):
            return _diagnose(left, right, _unavailable("input_required"))

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


async def self_evidence(candidate, registry, context: EvidenceContext | None = None) -> EquivalenceEvidence:
    """DP 1.0.12 CANON-001 follow-up: sample ONE candidate alone, with no peer
    to pair against yet, through the identical sampler/executor contract
    ``shared_evidence`` uses for every pairwise comparison. This module is the
    sole owner of evidence acquisition/normalization (CandidateSampling
    lookup, FingerprintKind translation, timeout/DNS/exception classification)
    for both the steady-state pairwise path and ``transfers.cohorts``'s
    bootstrap admission barrier -- there is deliberately no second,
    independently-maintained sampler classifier anywhere else.
    """
    try:
        # The one subject-claim router (``registry.executor_for_subject``).
        executor = _sampler(candidate, registry)
        if executor is None:
            return _unavailable("sampler_unsupported")
        sample = await _fingerprint(executor, candidate, context)
        if sample is None:
            return _unavailable("sampler_unsupported")
        if isinstance(sample, InputRequirement):
            return _unavailable("input_required")
        kind = _fingerprint_kind(sample)
        if kind == FingerprintKind.UNAVAILABLE.value:
            return _unavailable(str(getattr(sample, "reason", "") or "sampler_unavailable"))
        evidence_kind = (
            EvidenceKind.FULL_CONTENT_SAMPLE if kind == FingerprintKind.FULL_CONTENT_SAMPLE.value
            else EvidenceKind.PREFIX_CONTENT_SAMPLE
        )
        return EquivalenceEvidence(evidence_kind, int(sample.total_bytes), str(getattr(sample, "reason", "") or ""))
    except TimeoutError:
        return _unavailable("timeout")
    except socket.gaierror:
        return _unavailable("dns_failure")
    except Exception:
        # An exception from a sampling-capable executor may be transient --
        # identical fallback classification to shared_evidence() above.
        return _unavailable("sampler_unavailable")


async def shared_size(left, right, registry, context: EvidenceContext | None = None) -> int | None:
    """Compatibility seam: only strong/full evidence may merge one artifact."""
    evidence = await shared_evidence(left, right, registry, context)
    return evidence.total_bytes if evidence.proves_individual else None
