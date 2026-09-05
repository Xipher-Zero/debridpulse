"""Submission-cohort canonicalization barrier for weak bounded evidence.

This module adds no persistence authority. It reconstructs cohort state from the
existing durable request/resolution/canonical records on every pass, gathers
remote evidence before any ownership mutation, and delegates every attachment to
the established canonical owner.
"""
from __future__ import annotations

from dataclasses import replace
import logging

from db.database import get_db
from transfers.mirrors import EvidenceKind, EquivalenceEvidence, logical_key, shared_evidence


logger = logging.getLogger(__name__)
_PENDING_STATES = {
    "pending", "resolving", "waiting", "waiting_parent",
}


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


async def _proof_against_primary(primary, incoming, registry):
    """Return the best proof against one canonical artifact, if any."""
    best = EquivalenceEvidence(EvidenceKind.UNAVAILABLE, reason="pairing_mismatch")
    rank = {
        EvidenceKind.UNAVAILABLE: 0,
        EvidenceKind.PREFIX_CONTENT_SAMPLE: 1,
        EvidenceKind.FULL_CONTENT_SAMPLE: 2,
        EvidenceKind.STRONG_INTEGRITY: 3,
    }
    for left in primary.candidates:
        left = _with_known_size(left, primary.expected_bytes)
        for right in incoming:
            evidence = await shared_evidence(left, right, registry)
            if rank[evidence.kind] > rank[best.kind]:
                best = evidence
            if evidence.kind == EvidenceKind.STRONG_INTEGRITY:
                return best
    return best


async def _mapping(canonicals, incoming, registry):
    """Require exactly one canonical artifact with usable content evidence."""
    matches = []
    for primary in canonicals:
        evidence = await _proof_against_primary(primary, incoming, registry)
        if evidence.proves_collection_member:
            matches.append((primary, evidence))
    if len(matches) != 1:
        return None
    return matches[0]


async def _durable_mapping(request_id: str) -> int | None:
    async with get_db() as db:
        rows = await db.fetchall(
            "SELECT canonical_artifact_id FROM artifact_consolidations WHERE source_request_id=? ORDER BY canonical_artifact_id",
            (request_id,),
        )
    values = {int(row["canonical_artifact_id"]) for row in rows}
    return next(iter(values)) if len(values) == 1 else None


def _decision(record, incoming, decision: str, reason: str = "") -> None:
    """Record sanitized cohort disposition without endpoint/source secrets."""
    candidate = incoming[0] if incoming else None
    logger.debug(
        "cross-transfer collection request=%s candidate=%r declared_size=%d decision=%s reason=%s",
        record.id,
        logical_key(candidate) if candidate is not None else "",
        max(0, int(candidate.expected_bytes or 0)) if candidate is not None else 0,
        decision,
        reason or "none",
    )


async def coordinate_collection(engine, record, candidates) -> bool:
    """Coordinate one request before ordinary path allocation.

    True means this invocation has either attached the request or deliberately
    left it in durable MATERIALIZING state while a viable weak-evidence cohort is
    incomplete. False means the qualified ordinary materialization path should
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
    if current_mapping is None:
        _decision(record, incoming, "independent", "no_unique_mapping")
        return False
    current_primary, current_evidence = current_mapping

    # Strong integrity and full first+last proof retain the existing immediate
    # single-artifact fast path and never wait for siblings.
    if current_evidence.proves_individual:
        attached = await engine.canonical.attach(
            current_primary, record, incoming, current_evidence.total_bytes,
        )
        _decision(record, incoming, "consolidated_individual" if attached else "revalidate_retry",
                  current_evidence.kind)
        return attached

    if current_evidence.kind != EvidenceKind.PREFIX_CONTENT_SAMPLE:
        _decision(record, incoming, "independent", current_evidence.reason)
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
        _decision(record, incoming, "independent", "single_member_prefix")
        return False

    mappings = {}
    pending = False
    for sibling in material:
        if sibling.state in _PENDING_STATES:
            pending = True
            continue
        if sibling.state in {"failed", "input_required"}:
            # Input-required is operator-owned and may be unbounded. It cannot
            # hold an unrelated weak-prefix member behind the cohort barrier.
            _decision(record, incoming, "independent", f"sibling_{sibling.state}")
            return False
        if sibling.state == "resolved":
            canonical_id = await _durable_mapping(sibling.id)
            if canonical_id is None:
                # An ordinary already-materialized member makes complete weak
                # collection proof impossible; do not rewrite it retrospectively.
                _decision(record, incoming, "independent", "sibling_materialized")
                return False
            mappings[sibling.id] = (canonical_id, None, None, None)
            continue
        if sibling.state != "materializing":
            _decision(record, incoming, "independent", f"sibling_{sibling.state}")
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
        if match is None:
            _decision(record, incoming, "independent", "collection_mapping_incomplete")
            return False
        primary, evidence = match
        if not evidence.proves_collection_member:
            _decision(record, incoming, "independent", evidence.reason)
            return False
        mappings[sibling.id] = (primary.id, primary, evidence, sibling_candidates)

    # A weak current member waits only while another sibling can still provide
    # the missing corroboration. MATERIALIZING is already durable and is retried
    # by the normal resolution cycle, including after restart.
    if pending:
        _decision(record, incoming, "pending_collection", "sibling_pending")
        return True
    if len(mappings) != len(material):
        _decision(record, incoming, "independent", "collection_incomplete")
        return False

    canonical_ids = [item[0] for item in mappings.values()]
    if len(canonical_ids) != len(set(canonical_ids)):
        # Ambiguous duplicate names/paths or many-to-one mapping: never guess.
        _decision(record, incoming, "independent", "ambiguous_mapping")
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
                _decision(record, incoming, "independent", "canonical_disappeared")
                return False
            continue
        attached = await engine.canonical.attach(
            primary, sibling, sibling_candidates, evidence.total_bytes,
        )
        if sibling.id == record.id:
            attached_current = attached

    _decision(record, incoming, "consolidated_by_collection" if attached_current else "revalidate_retry",
              f"{len(mappings)}/{len(material)}")
    return attached_current
