"""Submission-cohort canonicalization barrier for weak bounded evidence.

Cohort state is reconstructed from durable request/resolution/canonical records on
every pass. The only additional durable state owned here is a bounded, neutral
proof-retry disposition stored on the existing request lifecycle record. Remote
evidence is always gathered before canonical ownership mutation.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import logging
from types import SimpleNamespace

from db.database import get_db
from transfers.mirrors import (
    EvidenceFailureClass, EvidenceKind, EquivalenceEvidence, logical_key, self_evidence, shared_evidence,
)


logger = logging.getLogger(__name__)
_PENDING_STATES = {
    "pending", "resolving", "waiting", "waiting_parent",
}
_PROOF_RETRY_BUDGET = 2
_PROOF_RETRY_DELAY_CAP = 1.0

# DP 1.0.12 canonical equivalence/lifecycle correction, Section 4: these are
# the only durable equivalence_disposition values that authorize the caller
# (transfers._engine_recovery.TransferEngine._materialize) to allocate an
# ordinary independent physical writer -- each one reflects AFFIRMATIVE
# evidence (a genuinely proven distinction, an explicit prior release, or a
# structurally non-pairing candidate), never mere absence of proof.
_INDEPENDENT_DISPOSITIONS = frozenset({"released", "independent", "contradictory"})
# "exhausted" means automatic proof attempts stopped while identity remains
# UNRESOLVED -- it must never be read as permission to materialize. A held
# request stays in durable MATERIALIZING state; only later affirmative
# evidence (recovered) or an explicit release event can move it forward.
_HELD_DISPOSITIONS = frozenset({"exhausted"})


@dataclass(frozen=True)
class MappingResult:
    primary: object | None
    evidence: EquivalenceEvidence
    cardinality: int = 0

    @property
    def matched(self) -> bool:
        return self.primary is not None and self.evidence.proves_collection_member


def _normalized_candidates(record, candidates):
    ordered = tuple(sorted(candidates, key=lambda candidate: -candidate.priority))
    if record.entry:
        ordered = tuple(replace(
            candidate,
            name=record.entry.name,
            relative_path=record.entry.relative_path,
            expected_bytes=candidate.expected_bytes or record.entry.expected_bytes,
        ) for candidate in ordered)
    return ordered


def _with_known_size(candidate, size: int):
    if candidate.expected_bytes > 0 or size <= 0:
        return candidate
    return replace(candidate, expected_bytes=size)


def _evidence_score(evidence: EquivalenceEvidence):
    kind_rank = {
        EvidenceKind.UNAVAILABLE: 0,
        EvidenceKind.PREFIX_CONTENT_SAMPLE: 10,
        EvidenceKind.FULL_CONTENT_SAMPLE: 20,
        # Resolver-attested identity (specification section 8.3) is stronger
        # than a live content sample -- it is an authoritative fact reported
        # by the resolver itself -- but is not byte-for-byte cryptographic
        # proof, so it ranks below strong integrity verification.
        EvidenceKind.RESOLVER_ATTESTED: 25,
        EvidenceKind.STRONG_INTEGRITY: 30,
    }[evidence.kind]
    if evidence.kind != EvidenceKind.UNAVAILABLE:
        return kind_rank, 0, 0
    failure_rank = {
        EvidenceFailureClass.TRANSIENT: 4,
        EvidenceFailureClass.CONTRADICTORY: 3,
        EvidenceFailureClass.STRUCTURAL: 2,
        EvidenceFailureClass.NONE: 0,
    }.get(evidence.failure_class, 0)
    specificity = int(evidence.reason not in {"", "pairing_mismatch", "no_unique_mapping"})
    return kind_rank, failure_rank, specificity


def _better(left: EquivalenceEvidence, right: EquivalenceEvidence) -> EquivalenceEvidence:
    return right if _evidence_score(right) > _evidence_score(left) else left


async def _proof_against_primary(primary, incoming, registry):
    """Return the best proof/failure against one canonical artifact."""
    best = EquivalenceEvidence(EvidenceKind.UNAVAILABLE, reason="pairing_mismatch")
    for left in primary.candidates:
        left = _with_known_size(left, primary.expected_bytes)
        for right in incoming:
            evidence = await shared_evidence(left, right, registry)
            best = _better(best, evidence)
            if evidence.kind == EvidenceKind.STRONG_INTEGRITY:
                return evidence
    return best


async def _mapping(canonicals, incoming, registry):
    """Require one and only one canonical target; never guess around unproven peers."""
    matches = []
    failures = []
    for primary in canonicals:
        evidence = await _proof_against_primary(primary, incoming, registry)
        if evidence.proves_collection_member:
            matches.append((primary, evidence))
        else:
            failures.append(evidence)

    if len(matches) > 1:
        return MappingResult(
            None, EquivalenceEvidence(EvidenceKind.UNAVAILABLE, reason="ambiguous_mapping"), len(matches),
        )

    unresolved = [item for item in failures if item.unresolved_pairing]
    if len(matches) == 1:
        if unresolved:
            best = unresolved[0]
            for item in unresolved[1:]:
                best = _better(best, item)
            return MappingResult(None, best, 1)
        return MappingResult(matches[0][0], matches[0][1], 1)

    if failures:
        best = failures[0]
        for item in failures[1:]:
            best = _better(best, item)
        return MappingResult(None, best, 0)
    return MappingResult(None, EquivalenceEvidence(EvidenceKind.UNAVAILABLE, reason="no_unique_mapping"), 0)


async def _durable_mapping(engine, request_id: str) -> int | None:
    """DP 1.0.12 Section 6: durable canonical membership for ``request_id``,
    discoverable for both cross-transfer and same-transfer contributors via
    ``transfers.canonical.CanonicalOwnership.durable_owner_for_request`` (the
    canonical-owner/repository owner of this lookup -- never ad hoc SQL
    duplicated at this layer)."""
    return await engine.canonical.durable_owner_for_request(request_id)


def _decision(record, incoming, decision: str, reason: str = "", *, evidence=None,
              mapping_cardinality: int = 0, retry_count: int = 0) -> None:
    """Record sanitized cohort disposition without endpoint/source secrets."""
    candidate = incoming[0] if incoming else None
    logger.debug(
        "cross-transfer collection request=%s artifact=%r declared_size=%d decision=%s reason=%s "
        "evidence=%s failure_class=%s mapping_cardinality=%d retry=%d/%d",
        record.id,
        logical_key(candidate) if candidate is not None else "",
        max(0, int(candidate.expected_bytes or 0)) if candidate is not None else 0,
        decision,
        reason or "none",
        evidence.kind if evidence is not None else "none",
        evidence.failure_class if evidence is not None else "none",
        int(mapping_cardinality), int(retry_count), _PROOF_RETRY_BUDGET,
    )


def _retry_delay(engine) -> float:
    configured = float(getattr(engine.policy, "retry_delay", 1.0) or 0.0)
    return min(_PROOF_RETRY_DELAY_CAP, max(0.1, configured))


async def _disposition(request_id: str) -> str:
    async with get_db() as db:
        row = await db.fetchone(
            "SELECT equivalence_disposition FROM transfer_requests WHERE id=?",
            (request_id,),
        )
    return str(row.get("equivalence_disposition") or "") if row else ""


async def _proof_disposition(request_id: str, disposition: str, reason: str, *,
                             clear_retry=False, preserve_reason=False) -> None:
    async with get_db() as db:
        await db.execute(
            """UPDATE transfer_requests SET
                equivalence_reason=CASE
                    WHEN ? AND COALESCE(equivalence_reason,'')!='' THEN equivalence_reason
                    ELSE ? END,
                equivalence_disposition=?,retry_at=CASE WHEN ? THEN 0 ELSE retry_at END
                WHERE id=?""",
            (int(preserve_reason), str(reason or ""), disposition, int(clear_retry), request_id),
        )
        await db.commit()


async def _schedule_proof_retry(engine, record, incoming, evidence, *, mapping_cardinality: int) -> bool:
    """Persist one bounded future proof opportunity; False means budget exhausted."""
    now = float(engine.clock())
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        row = await db.fetchone(
            """SELECT state,equivalence_retry_count,retry_at,equivalence_disposition
                FROM transfer_requests WHERE id=?""",
            (record.id,),
        )
        if not row or row["state"] != "materializing":
            await db.commit()
            return True
        retries = int(row.get("equivalence_retry_count") or 0)
        scheduled_at = float(row.get("retry_at") or 0)
        if row.get("equivalence_disposition") == "pending" and scheduled_at > now:
            await db.commit()
            _decision(record, incoming, "proof_retry_already_pending", evidence.reason,
                      evidence=evidence, mapping_cardinality=mapping_cardinality, retry_count=retries)
            return True
        if retries >= _PROOF_RETRY_BUDGET:
            await db.execute(
                """UPDATE transfer_requests SET equivalence_reason=?,equivalence_disposition='exhausted',retry_at=0
                    WHERE id=?""",
                (evidence.reason or "sampler_unavailable", record.id),
            )
            await db.commit()
            _decision(record, incoming, "proof_retry_exhausted", evidence.reason or "sampler_unavailable",
                      evidence=evidence, mapping_cardinality=mapping_cardinality, retry_count=retries)
            return False
        retries += 1
        retry_at = now + _retry_delay(engine)
        await db.execute(
            """UPDATE transfer_requests SET equivalence_retry_count=?,equivalence_reason=?,
                equivalence_disposition='pending',retry_at=? WHERE id=? AND state='materializing'""",
            (retries, evidence.reason or "sampler_unavailable", retry_at, record.id),
        )
        await db.commit()
    _decision(record, incoming, "pending_proof_retry", evidence.reason,
              evidence=evidence, mapping_cardinality=mapping_cardinality, retry_count=retries)
    return True


def _must_hold(evidence: EquivalenceEvidence) -> bool:
    """Unresolved evidence from a proof that was actually attempted never
    authorizes an independent writer, whether or not it is retryable. The one
    exception is structural absence of any possible proof
    (``proof_structurally_unavailable``: no sampling capability, or no unique
    mapping): it says nothing about the material, and independence is then
    authorized by the
    existing structurally-unprovable degraded fallback (``_bootstrap_admission``
    and the steady-state precedent below) -- never inferred from
    ``retryable`` or ``unresolved_pairing``."""
    return evidence.unresolved_pairing and not evidence.proof_structurally_unavailable


async def _hold_unresolved(engine, record, incoming, evidence, *, mapping_cardinality: int) -> bool:
    """Consume unresolved-pairing evidence without ever releasing independence.

    Retryability decides only whether another automatic proof attempt is
    scheduled; it never decides whether unresolved identity becomes
    independence. True means a bounded proof retry is scheduled. False means
    automatic proof acquisition has stopped -- the retry budget is spent
    (``_schedule_proof_retry`` persisted it) or the evidence is not retryable
    at all -- and ``record`` is durably held (``exhausted``, timer cleared).
    The writer barrier stays up either way."""
    if evidence.retryable:
        return await _schedule_proof_retry(
            engine, record, incoming, evidence, mapping_cardinality=mapping_cardinality,
        )
    await _proof_disposition(record.id, "exhausted", evidence.reason or "sampler_unavailable", clear_retry=True)
    _decision(record, incoming, "hold_non_retryable", evidence.reason or "sampler_unavailable",
              evidence=evidence, mapping_cardinality=mapping_cardinality)
    return False


async def _release_cohort(records, reason: str) -> None:
    """Release proof timers without erasing a more specific stored reason."""
    if not records:
        return
    async with get_db() as db:
        for item in records:
            await db.execute(
                """UPDATE transfer_requests SET retry_at=0,
                    equivalence_disposition=CASE
                        WHEN equivalence_disposition IN ('recovered','exhausted','contradictory','independent')
                            THEN equivalence_disposition
                        ELSE 'released' END,
                    equivalence_reason=CASE
                        WHEN COALESCE(equivalence_reason,'')='' THEN ?
                        ELSE equivalence_reason END
                    WHERE id=?""",
                (reason, item.id),
            )
        await db.commit()


def _same_transfer_material_cohort(records, record):
    """Reconstruct the leaf same-transfer cohort ``record`` belongs to from
    durable request records alone -- never from arrival/scheduling order."""
    child_parents = {item.parent_id for item in records if item.parent_id is not None}
    leaves = tuple(item for item in records if item.id not in child_parents)
    if record.parent_id is not None:
        cohort = tuple(item for item in leaves if item.parent_id == record.parent_id)
    else:
        cohort = tuple(item for item in leaves if item.parent_id is None)
    return tuple(item for item in cohort if item.state != "skipped")


async def _bootstrap_self_evidence(incoming, registry) -> EquivalenceEvidence:
    """Best self-evidence across ``incoming``'s own candidate routes, acquired
    through ``transfers.mirrors.self_evidence`` -- the sole evidence-owner
    module, never a parallel sampler classifier maintained here. Any one
    affirmatively reachable route is sufficient to seed. ``incoming`` is
    already guaranteed non-empty by the caller, so ``best`` always ends up a
    real per-candidate result -- never a synthetic placeholder competing
    (and potentially winning) via ``_better``'s scoring."""
    best = None
    for candidate in incoming:
        evidence = await self_evidence(candidate, registry)
        if evidence.kind != EvidenceKind.UNAVAILABLE:
            return evidence
        best = evidence if best is None else _better(best, evidence)
    return best


# Dispositions under which a same-transfer sibling could still independently
# go on to produce a viable bootstrap seed: never yet evaluated ("") or
# actively retrying transient self-evidence ("pending"). Anything else --
# durably exhausted, already structurally unprovable itself, already
# independent/contradictory/released, or a terminal non-materializing state
# -- can never spontaneously recover without an explicit, external action, so
# waiting on it would deadlock (Case E) rather than merely delay.
_BOOTSTRAP_STILL_CAPABLE_DISPOSITIONS = frozenset({"", "pending"})


def _prospective_logical_key(record) -> str:
    """Best-effort logical key for a same-transfer sibling that has not
    necessarily resolved yet, from whichever name/path is already known
    pre-resolution -- the identical normalization ``mirrors.logical_key``
    applies to a resolved candidate, just over the request's own declared
    name when no candidate exists yet. Returns "" when no name is declared
    at all -- an UNKNOWN identity, never itself evidence of a mismatch."""
    if record.entry is not None:
        return logical_key(record.entry)
    return logical_key(SimpleNamespace(relative_path="", name=str(getattr(record.request, "name", "") or "")))


def _could_compete(candidate_key: str, record_key: str) -> bool:
    """A known key that differs from ``record_key`` proves this candidate
    could never have shared identity with it (Case G). An UNKNOWN key (no
    name declared/resolved yet) proves nothing either way -- it is not
    evidence of a mismatch, so it must default to "could still compete",
    never to "definitely unrelated"."""
    return not candidate_key or candidate_key == record_key


async def _bootstrap_sibling_capable(engine, record_key: str, sibling) -> bool:
    """True while ``sibling`` could still independently produce a viable
    bootstrap seed that would actually compete with ``record_key``'s own
    identity -- used only to decide whether a structurally self-evidence-
    incapable candidate must keep waiting (Case D: never win seed authority
    merely by arriving first while a capable sibling hasn't had its turn) or
    has reached the genuinely last-resort degraded case where no comparative
    evidence can ever exist for THIS candidate's identity.

    A same-transfer sibling whose logical identity is ALREADY KNOWN to
    differ from ``record_key`` (Case G: genuinely distinct multi-file
    members sharing nothing but a transfer id, e.g. two unrelated archive
    entries) was never a competitor for this seed decision -- it must not
    block this candidate's bootstrap turn merely for existing in the same
    transfer. An unresolved sibling with no declared name yet has an
    UNKNOWN identity, not a known-different one, and must still count as a
    potential competitor until its own name/candidate is known.
    """
    if sibling.state in _PENDING_STATES:
        return _could_compete(_prospective_logical_key(sibling), record_key)
    if sibling.state != "materializing":
        return False
    disposition = await _disposition(sibling.id)
    if disposition not in _BOOTSTRAP_STILL_CAPABLE_DISPOSITIONS:
        return False
    sibling_candidates = _normalized_candidates(sibling, await engine.repository.resolved_candidates(sibling.id))
    if not sibling_candidates:
        return _could_compete(_prospective_logical_key(sibling), record_key)
    return any(_could_compete(logical_key(candidate), record_key) for candidate in sibling_candidates)


async def _bootstrap_capable_siblings(engine, record, material, record_key: str):
    return [
        sibling for sibling in material
        if sibling.id != record.id and await _bootstrap_sibling_capable(engine, record_key, sibling)
    ]


async def _bootstrap_admission(engine, record, incoming, disposition: str) -> bool:
    """DP 1.0.12 CANON-001 follow-up bootstrap barrier.

    Gates physical-writer admission for the FIRST request of a same-transfer
    cohort to reach ``_materialize()`` while no canonical artifact exists yet
    anywhere in the cohort. An empty canonical set is not itself
    authorization to materialize once this is genuinely a multi-member
    cohort (Canonical Ownership Invariants) -- but requiring every sibling to
    resolve before ANY of them may seed would deadlock the cohort forever, so
    the barrier is evidence-based, not sibling-count-based: ``record``'s own
    candidate(s) must clear the identical sampler/executor proof seam every
    later pairwise comparison already uses before being trusted to seed --
    exactly the check production transfer 266's DNS-failing source never
    received.

    Because every ``_materialize()`` call for one transfer is already
    serialized under the existing per-transfer cohort lock
    (``_engine_recovery.TransferEngine._materialize``), at most one request
    is ever deciding this at a time: whichever sibling's own turn under the
    lock first produces affirmative self-evidence seeds the cohort's first
    canonical artifact, regardless of ordinal -- every other sibling's own
    later turn simply observes a non-empty canonical set and proceeds
    through the existing, unchanged mapping/attach flow above. No ordinal
    comparison of any kind occurs here.
    """
    records = await engine.repository.requests(record.transfer_id)
    material = _same_transfer_material_cohort(records, record)
    if len(material) < 2:
        return False  # Case A: no sibling cohort, unchanged immediate materialization.

    if disposition == "bootstrap_unprovable":
        # This record's own structural incapability is ALREADY a durable
        # fact from an earlier turn -- re-running self-evidence every tick
        # while merely waiting on a sibling would re-fingerprint a
        # permanently-unsampleable candidate forever, defeating the point
        # of recording the fact durably. Only the sibling-capability
        # question can still change tick to tick.
        record_key = logical_key(incoming[0])
        capable = await _bootstrap_capable_siblings(engine, record, material, record_key)
        if capable:
            _decision(record, incoming, "hold_unresolved", "bootstrap_unprovable", mapping_cardinality=len(capable))
            return True
        await _proof_disposition(record.id, "independent", "", clear_retry=True, preserve_reason=True)
        _decision(record, incoming, "independent", "bootstrap_unprovable_fallback", mapping_cardinality=0)
        return False

    evidence = await _bootstrap_self_evidence(incoming, engine.registry)
    if evidence.kind == EvidenceKind.UNAVAILABLE:
        if evidence.retryable:
            if await _schedule_proof_retry(engine, record, incoming, evidence, mapping_cardinality=0):
                return True
            # Bounded self-proof budget exhausted. Identity/viability remains
            # unresolved -- absence of proof is not proof of independence --
            # so this request stays held rather than ever becoming a writer.
            _decision(record, incoming, "hold_unresolved", evidence.reason or "sampler_unavailable",
                      evidence=evidence, mapping_cardinality=0)
            return True
        # Structurally non-retryable (e.g. no sampling capability at all for
        # this executor, or a candidate its sampler cannot route at all):
        # this is NOT affirmative proof of distinctness, and NOT even proof
        # that this source is a viable physical writer -- so arrival order
        # alone must never let it claim seed authority while a SIBLING that
        # could still produce genuine evidence has not yet had its own turn
        # (Case D). Record this durable fact about THIS record first (so a
        # sibling's own later capability check can tell "already tried and
        # structurally can't" apart from "hasn't tried yet" -- without that
        # distinction, an all-structurally-incapable cohort would have every
        # member wait on every other member forever, violating Case E from
        # the opposite direction).
        await _proof_disposition(record.id, "bootstrap_unprovable", evidence.reason, clear_retry=True)
        record_key = logical_key(incoming[0])
        capable = await _bootstrap_capable_siblings(engine, record, material, record_key)
        if capable:
            _decision(record, incoming, "hold_unresolved", evidence.reason or "sampler_unsupported",
                      evidence=evidence, mapping_cardinality=len(capable))
            return True
        # Every OTHER material sibling has also reached a state that can
        # never independently produce a viable seed (already structurally
        # unprovable itself, already durably exhausted, or terminally
        # failed) -- this is now a deterministic degraded fallback for a
        # cohort that can never produce comparative evidence at all, never
        # an ordinal race outcome: any later-resolving sibling that CAN
        # sample still runs the unchanged pairwise mapping against whatever
        # this seeds, and one that also cannot sample reaches this same
        # steady-state "independent" conclusion on its own later turn
        # (line ~447 below), not by copying this one's disposition.
        await _proof_disposition(record.id, "independent", evidence.reason, clear_retry=True)
        _decision(record, incoming, "independent", evidence.reason or "sampler_unsupported",
                  evidence=evidence, mapping_cardinality=0)
        return False

    if disposition == "pending":
        # DP 1.0.12 secondary-residue normalization: this seed recovered
        # after one or more transient bootstrap self-probe attempts -- clear
        # the retry timer/disposition through the existing disposition owner
        # so a successfully admitted canonical seed never keeps the stale
        # 'pending' row production transfer 266 showed on its eventual
        # canonical owner.
        await _proof_disposition(record.id, "recovered", evidence.reason, clear_retry=True, preserve_reason=True)
    _decision(record, incoming, "bootstrap_seed", evidence.reason, evidence=evidence, mapping_cardinality=0)
    return False


async def coordinate_collection(engine, record, candidates) -> bool:
    """Coordinate one request before ordinary path allocation.

    True means this invocation has either attached the request or deliberately
    left it in durable MATERIALIZING state while a viable weak-evidence cohort or
    bounded proof retry is incomplete. False means ordinary materialization may
    continue immediately.
    """
    incoming = _normalized_candidates(record, candidates)
    if not incoming:
        return False
    # Once this request has a durable disposition, re-entering full proof
    # acquisition on every scheduler tick would either re-litigate an
    # affirmative decision that already stands, or -- for "exhausted" --
    # hot-loop automatic proof sampling against a budget that has already
    # stopped (DP 1.0.12 Section 4.3, quiescence). Independent-class
    # dispositions (an affirmative prior decision) authorize materialization
    # immediately; held-class dispositions ("exhausted": identity remains
    # unresolved) keep the writer barrier up without doing any further proof
    # work this tick.
    disposition = await _disposition(record.id)
    if disposition in _INDEPENDENT_DISPOSITIONS:
        return False
    if disposition in _HELD_DISPOSITIONS:
        return True

    canonicals = tuple(
        item for item in await engine.canonical.canonical_artifacts()
        if item.request_id != record.id and item.candidates
    )
    if not canonicals:
        return await _bootstrap_admission(engine, record, incoming, disposition)

    current_mapping = await _mapping(canonicals, incoming, engine.registry)
    if not current_mapping.matched:
        evidence = current_mapping.evidence
        if _must_hold(evidence):
            if await _hold_unresolved(
                engine, record, incoming, evidence, mapping_cardinality=current_mapping.cardinality,
            ):
                return True
            # Automatic proof acquisition has stopped (retry budget spent, or
            # the evidence is not retryable) and _hold_unresolved persisted
            # equivalence_disposition='exhausted' and cleared retry_at.
            # Identity remains unresolved -- absence of proof is not proof of
            # non-equivalence (DP 1.0.12 Section 4.1) -- so this request stays
            # held rather than authorizing a writer.
            _decision(record, incoming, "hold_unresolved", evidence.reason or "sampler_unavailable",
                      evidence=evidence, mapping_cardinality=current_mapping.cardinality)
            return True
        # Affirmatively contradictory (proven distinct), structurally
        # non-pairing (a cheap pairing rejection), or structurally unprovable
        # (the existing degraded fallback). Unresolved evidence from an
        # attempted proof is never one of them.
        disposition = "contradictory" if evidence.failure_class == EvidenceFailureClass.CONTRADICTORY else "independent"
        await _proof_disposition(record.id, disposition, evidence.reason, clear_retry=True)
        _decision(record, incoming, "independent", evidence.reason or "no_unique_mapping",
                  evidence=evidence, mapping_cardinality=current_mapping.cardinality)
        return False
    current_primary, current_evidence = current_mapping.primary, current_mapping.evidence

    # Strong integrity and full first+last proof retain the existing immediate
    # single-artifact fast path and never wait for siblings.
    if current_evidence.proves_individual:
        attached = await engine.canonical.attach(
            current_primary, record, incoming, current_evidence.total_bytes,
        )
        if attached:
            await _proof_disposition(
                record.id, "recovered", current_evidence.kind, clear_retry=True, preserve_reason=True,
            )
        _decision(record, incoming, "consolidated_individual" if attached else "revalidate_retry",
                  current_evidence.kind, evidence=current_evidence, mapping_cardinality=1)
        return attached

    if current_evidence.kind != EvidenceKind.PREFIX_CONTENT_SAMPLE:
        await _proof_disposition(record.id, "independent", current_evidence.reason, clear_retry=True)
        _decision(record, incoming, "independent", current_evidence.reason,
                  evidence=current_evidence, mapping_cardinality=1)
        return False

    records = await engine.repository.requests(record.transfer_id)
    material = _same_transfer_material_cohort(records, record)
    if len(material) < 2:
        if current_evidence.retryable:
            if await _schedule_proof_retry(
                engine, record, incoming, current_evidence, mapping_cardinality=1,
            ):
                return True
            _decision(record, incoming, "hold_unresolved", "single_member_prefix",
                      evidence=current_evidence, mapping_cardinality=1)
            return True
        await _proof_disposition(record.id, "independent", "single_member_prefix", clear_retry=True)
        _decision(record, incoming, "independent", "single_member_prefix",
                  evidence=current_evidence, mapping_cardinality=1)
        return False

    mappings = {}
    pending = False
    for sibling in material:
        if sibling.state in _PENDING_STATES:
            pending = True
            continue
        if sibling.state in {"failed", "input_required"}:
            await _release_cohort(material, f"sibling_{sibling.state}")
            _decision(record, incoming, "independent", f"sibling_{sibling.state}", evidence=current_evidence)
            return False
        if sibling.state == "resolved":
            # DP 1.0.12 CANON-001 follow-up: an already-resolved sibling's
            # own durable canonical must not be trusted as "accounted for"
            # merely because it exists -- that would let a genuinely
            # divergent member (one that already independently materialized
            # BEFORE this walk ran, purely due to scheduling timing) slip
            # past the same mismatch check every other material member
            # gets, silently keeping the other siblings' consolidation alive
            # when the whole weak-evidence collection hypothesis should
            # dissolve (Section 8.1.5). Re-verify with the identical
            # evidence machinery first.
            resolved_sibling_candidates = _normalized_candidates(
                sibling, await engine.repository.resolved_candidates(sibling.id),
            )
            # A canonical whose ONLY candidate is this same sibling's own
            # (nothing else attached to it yet) can never be compared against
            # itself: `_mapping` would report a cheap `same_candidate`
            # pairing rejection, which is an absence of alternative evidence,
            # never a genuine mismatch -- excluding exactly that trivial case
            # (and only that case) still lets a real divergence show up via
            # comparison against every OTHER canonical (including a
            # genuinely-distinct sibling's own separate self-owned
            # canonical, e.g. a mismatched member that raced ahead and
            # materialized independently -- the Section 8.1.5 case this
            # re-verification exists to catch).
            own_ids = {candidate.id for candidate in resolved_sibling_candidates}
            verification_pool = tuple(
                item for item in canonicals
                if not (len(item.candidates) == 1 and item.candidates[0].id in own_ids)
            )
            if resolved_sibling_candidates and verification_pool:
                verification = await _mapping(verification_pool, resolved_sibling_candidates, engine.registry)
                if not verification.matched:
                    evidence = verification.evidence
                    if _must_hold(evidence):
                        # Bound this re-probe on the CURRENT record's own
                        # existing retry budget/timer (this sibling is
                        # already durably 'resolved', not 'materializing',
                        # so _schedule_proof_retry cannot persist a budget
                        # against ITS row -- see _schedule_proof_retry's
                        # state=='materializing' guard). Once the current
                        # record's own budget exhausts -- or immediately, for
                        # evidence that is not retryable -- its disposition
                        # becomes 'exhausted' and the top-of-function
                        # quiescence check short-circuits all future ticks
                        # without further sampling -- never an unbounded
                        # hot-loop against an already-resolved sibling.
                        if await _hold_unresolved(
                            engine, record, resolved_sibling_candidates, evidence,
                            mapping_cardinality=verification.cardinality,
                        ):
                            pending = True
                            continue
                        _decision(record, incoming, "hold_unresolved", evidence.reason or "resolved_sibling_reverify",
                                  evidence=evidence, mapping_cardinality=verification.cardinality)
                        return True
                    await _release_cohort(material, evidence.reason or "collection_mapping_incomplete")
                    _decision(record, incoming, "independent", evidence.reason or "collection_mapping_incomplete",
                              evidence=evidence, mapping_cardinality=verification.cardinality)
                    return False
            # A same-transfer sibling that is itself the established
            # canonical owner (the first of this cohort to materialize, with
            # nothing attached to it yet) has no durable origin/binding row
            # of its own until something later attaches and formalizes it
            # (transfers.canonical.CanonicalOwnership.attach()'s lazy
            # self-heal, DP 1.0.12 Section 6) -- but it is already
            # unambiguously present in ``canonicals`` by durable id, so that
            # is checked first rather than treating "no origin yet" as
            # "materialized independently."
            self_owned = next((item for item in canonicals if item.request_id == sibling.id), None)
            canonical_id = self_owned.id if self_owned is not None else await _durable_mapping(engine, sibling.id)
            if canonical_id is None:
                await _release_cohort(material, "sibling_materialized")
                _decision(record, incoming, "independent", "sibling_materialized", evidence=current_evidence)
                return False
            mappings[sibling.id] = (canonical_id, None, None, None)
            continue
        if sibling.state != "materializing":
            await _release_cohort(material, f"sibling_{sibling.state}")
            _decision(record, incoming, "independent", f"sibling_{sibling.state}", evidence=current_evidence)
            return False
        if sibling.retry_at > engine.clock():
            pending = True
            continue
        if sibling.id != record.id:
            # A sibling already durably held (exhausted: identity unresolved,
            # automatic proof retries stopped) must not be re-sampled every
            # time a DIFFERENT sibling's own coordinate_collection call walks
            # this cohort -- that would hot-loop proof acquisition against an
            # already-exhausted budget (DP 1.0.12 Section 4.3). It also must
            # not release the cohort merely for being unresolved (Section
            # 4.1/8.1.6); stay pending until it resolves or later evidence
            # recovers it.
            sibling_disposition = await _disposition(sibling.id)
            if sibling_disposition in _HELD_DISPOSITIONS:
                pending = True
                continue
            if sibling_disposition in _INDEPENDENT_DISPOSITIONS:
                await _release_cohort(material, f"sibling_{sibling_disposition}")
                _decision(record, incoming, "independent", f"sibling_{sibling_disposition}",
                          evidence=current_evidence)
                return False

        sibling_candidates = incoming if sibling.id == record.id else _normalized_candidates(
            sibling, await engine.repository.resolved_candidates(sibling.id),
        )
        if not sibling_candidates:
            pending = True
            continue
        match = current_mapping if sibling.id == record.id else await _mapping(
            canonicals, sibling_candidates, engine.registry,
        )
        if not match.matched:
            evidence = match.evidence
            if _must_hold(evidence):
                # A bounded retry is scheduled, or -- once the retry budget is
                # spent, or for evidence that is not retryable -- this ONE
                # sibling is durably held (_hold_unresolved). Identity remains
                # unresolved for it -- that alone must never release the rest
                # of the cohort to independence (Section 8.1.6): a sibling
                # that merely could not acquire fresh proof is not affirmative
                # evidence of anything. Hold the whole collection decision
                # instead; a genuinely distinct/contradictory sibling
                # (handled below) is the only thing that still releases the
                # cohort.
                await _hold_unresolved(
                    engine, sibling, sibling_candidates, evidence, mapping_cardinality=match.cardinality,
                )
                pending = True
                continue
            # Affirmatively contradictory (proven distinct), structurally
            # non-pairing, or structurally unprovable (existing degraded
            # fallback). This sibling really is independent, so the
            # weak-evidence collection hypothesis for the WHOLE cohort is
            # disproven -- release every member to independent
            # materialization (existing, unchanged behavior).
            await _proof_disposition(
                sibling.id,
                "contradictory" if evidence.failure_class == EvidenceFailureClass.CONTRADICTORY else "independent",
                evidence.reason,
                clear_retry=True,
            )
            await _release_cohort(material, evidence.reason or "collection_mapping_incomplete")
            _decision(record, incoming, "independent", evidence.reason or "collection_mapping_incomplete",
                      evidence=evidence, mapping_cardinality=match.cardinality)
            return False
        primary, evidence = match.primary, match.evidence
        mappings[sibling.id] = (primary.id, primary, evidence, sibling_candidates)

    # MATERIALIZING plus retry_at is the existing durable scheduler seam. No
    # duplicate writer is created while either sibling resolution or bounded
    # proof acquisition can still converge safely.
    if pending:
        _decision(record, incoming, "pending_collection", "sibling_or_proof_pending",
                  evidence=current_evidence, mapping_cardinality=len(mappings))
        return True
    if len(mappings) != len(material):
        await _release_cohort(material, "collection_incomplete")
        _decision(record, incoming, "independent", "collection_incomplete", evidence=current_evidence)
        return False

    canonical_ids = [item[0] for item in mappings.values()]
    if len(canonical_ids) != len(set(canonical_ids)):
        await _release_cohort(material, "ambiguous_mapping")
        _decision(record, incoming, "independent", "ambiguous_mapping",
                  evidence=EquivalenceEvidence(EvidenceKind.UNAVAILABLE, reason="ambiguous_mapping"),
                  mapping_cardinality=len(canonical_ids))
        return False

    attached_current = False
    for sibling in material:
        canonical_id, primary, evidence, sibling_candidates = mappings[sibling.id]
        if evidence is None:
            if sibling.id == record.id:
                attached_current = True
            continue
        if primary is None:
            primary = next((item for item in canonicals if item.id == canonical_id), None)
        if primary is None:
            if sibling.id == record.id:
                await _release_cohort(material, "canonical_disappeared")
                _decision(record, incoming, "independent", "canonical_disappeared", evidence=current_evidence)
                return False
            continue
        attached = await engine.canonical.attach(
            primary, sibling, sibling_candidates, evidence.total_bytes,
        )
        if attached:
            await _proof_disposition(
                sibling.id, "recovered", evidence.kind, clear_retry=True, preserve_reason=True,
            )
        if sibling.id == record.id:
            attached_current = attached

    _decision(record, incoming, "consolidated_by_collection" if attached_current else "revalidate_retry",
              f"{len(mappings)}/{len(material)}", evidence=current_evidence,
              mapping_cardinality=len(mappings))
    return attached_current