"""Submission-cohort canonicalization barrier for weak bounded evidence.

Cohort state is reconstructed from durable request/resolution/canonical records on
every pass. The only additional durable state owned here is a bounded, neutral
proof-retry disposition stored on the existing request lifecycle record. Remote
evidence is always gathered before canonical ownership mutation.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import logging

from db.database import get_db
from transfers.mirrors import (
    EvidenceFailureClass, EvidenceKind, EquivalenceEvidence, logical_key, shared_evidence,
)


logger = logging.getLogger(__name__)
_PENDING_STATES = {
    "pending", "resolving", "waiting", "waiting_parent",
}
_PROOF_RETRY_BUDGET = 2
_PROOF_RETRY_DELAY_CAP = 1.0


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


async def _durable_mapping(request_id: str) -> int | None:
    async with get_db() as db:
        rows = await db.fetchall(
            "SELECT canonical_artifact_id FROM artifact_consolidations WHERE source_request_id=? ORDER BY canonical_artifact_id",
            (request_id,),
        )
    values = {int(row["canonical_artifact_id"]) for row in rows}
    return next(iter(values)) if len(values) == 1 else None


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
            "SELECT state,equivalence_retry_count FROM transfer_requests WHERE id=?", (record.id,),
        )
        if not row or row["state"] != "materializing":
            await db.commit()
            return True
        retries = int(row.get("equivalence_retry_count") or 0)
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


async def _release_cohort(records, reason: str) -> None:
    """Release proof timers without erasing a more specific stored reason."""
    if not records:
        return
    async with get_db() as db:
        for item in records:
            await db.execute(
                """UPDATE transfer_requests SET retry_at=0,
                    equivalence_disposition=CASE
                        WHEN equivalence_disposition='pending' THEN 'released'
                        ELSE equivalence_disposition END,
                    equivalence_reason=CASE
                        WHEN COALESCE(equivalence_reason,'')='' THEN ?
                        ELSE equivalence_reason END
                    WHERE id=?""",
                (reason, item.id),
            )
        await db.commit()


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

    canonicals = tuple(
        item for item in await engine.canonical.canonical_artifacts()
        if item.request_id != record.id and item.candidates
    )
    if not canonicals:
        return False

    current_mapping = await _mapping(canonicals, incoming, engine.registry)
    if not current_mapping.matched:
        evidence = current_mapping.evidence
        if evidence.retryable and await _schedule_proof_retry(
            engine, record, incoming, evidence, mapping_cardinality=current_mapping.cardinality,
        ):
            return True
        disposition = "contradictory" if evidence.failure_class == EvidenceFailureClass.CONTRADICTORY else (
            "exhausted" if evidence.retryable else "independent"
        )
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
    child_parents = {item.parent_id for item in records if item.parent_id is not None}
    leaves = tuple(item for item in records if item.id not in child_parents)
    if record.parent_id is not None:
        cohort = tuple(item for item in leaves if item.parent_id == record.parent_id)
    else:
        cohort = tuple(item for item in leaves if item.parent_id is None)
    material = tuple(item for item in cohort if item.state != "skipped")
    if len(material) < 2:
        if current_evidence.retryable and await _schedule_proof_retry(
            engine, record, incoming, current_evidence, mapping_cardinality=1,
        ):
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
            canonical_id = await _durable_mapping(sibling.id)
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
            if evidence.retryable and await _schedule_proof_retry(
                engine, sibling, sibling_candidates, evidence, mapping_cardinality=match.cardinality,
            ):
                pending = True
                continue
            await _proof_disposition(
                sibling.id,
                "exhausted" if evidence.retryable else (
                    "contradictory" if evidence.failure_class == EvidenceFailureClass.CONTRADICTORY else "independent"
                ),
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
