"""Durable transfer, request, resource and attempt identities.

The existing parent/artifact table names remain a database-format obligation.
Their integration-specific columns are not read by this repository. Native data
is persisted only as opaque context on a resource or execution attempt.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from core.presentation_safety import safe_public_host, safe_route_endpoint
from db.database import get_db, validate_transfer_repository_schema
from transfers import codec
from transfers.cohorts import _HELD_DISPOSITIONS
from transfers.errors import Category, Domain, NormalizedError, Stage, TransferError
from transfers.input_required import public_challenge
from transfers.mirrors import logical_key
from transfers.models import (
    BITTORRENT_REQUEST_KINDS, Artifact, CachePresence, DeliveryKind, ExecutionAttempt, ExecutionHandle,
    ExecutionState,
    OutcomeKind, ProviderResource, RequestRecord, ResolutionAttempt, ResolutionResult,
    ResourceState, SourceEntry, Transfer, TransferCandidate, TransferOutcome, TransferRequest,
    TransferState, TransferProgress, new_identity,
)
from transfers.policy import SIDE_STATE_RETIRING_TRANSFER_STATES, TERMINAL_TRANSFER_STATES, transition_allowed


# DP 1.0.12 recovery leveling, Section 21/22: parent lifecycle terminal states
# where aggregation has nothing left to decide. The canonical definition now
# lives in transfers.policy (FUNC-001) so this module owns no independent
# literal.
_AGGREGATE_TERMINAL_STATES = TERMINAL_TRANSFER_STATES

# DP 1.0.12 canonical lifecycle/recovery/completion rework, Section 7.2:
# execution-attempt states that mean a durable native writer might still be
# doing something -- while paused, the parent may not claim PAUSED until
# none of a transfer's recorded attempts are in one of these.
_UNSETTLED_EXECUTION_STATES = frozenset({
    "prepared", ExecutionState.QUEUED.value, ExecutionState.TRANSFERRING.value, ExecutionState.UNKNOWN.value,
})


async def _retire_transfer_auxiliary_state_in_db(db, transfer_id: int) -> None:
    """Transaction-local: retire a settled transfer's old pause intent and
    INPUT_REQUIRED challenge (FUNC-001). Called from inside every write path
    that can settle a parent into ``policy.SIDE_STATE_RETIRING_TRANSFER_STATES``
    -- ``_write_lifecycle_transition`` and the direct status-update paths that
    intentionally bypass it (``delete``, ``cancel_with_execution_cleanup``,
    ``transfers.canonical.CanonicalOwnership._finalize_transfer``) -- so a
    settled transfer can never present stale actionable side state from a
    prior lifecycle generation. Always safe to call (no-op when nothing is
    stored); never a second, independent cleanup path."""
    await db.execute("DELETE FROM transfer_pause_intents WHERE torrent_id=?", (transfer_id,))
    await db.execute("DELETE FROM transfer_input_challenges WHERE transfer_id=?", (transfer_id,))


@dataclass(frozen=True)
class AggregateLifecycleOutcome:
    """Result of one atomic ``TransferRepository.aggregate_lifecycle`` call.

    ``artifacts`` is exactly the voting canonical-membership snapshot the
    decision used -- canonical-membership rows (Section 7) minus any failed
    row whose own logical delivery obligation a different, already-completed
    canonical artifact durably satisfies (DP 1.0.12 Root Cause B, Section 5)
    -- the caller must reuse it (never re-read a second, independently timed
    set) for any follow-up step such as completion verification.
    ``should_complete`` is True only when every VOTING canonical artifact is
    already "completed" and nothing is still resolving; the caller runs the
    separate, non-transactional, executor-touching completion sequence in
    that case (verifying payloads, cancelling stray writers, queuing
    post-processing) since that work cannot happen inside a single bounded
    SQLite transaction.
    """
    should_complete: bool
    artifacts: tuple


def canonical_artifact_membership_sql(alias: str = "f") -> str:
    """The one definition of a canonical actionable ``download_files`` row.

    Lifecycle aggregation (``artifacts()`` below), transfer-level presentation
    voting, recovery eligibility, and current candidate-group operational
    status must all filter on exactly this predicate so they can never
    silently diverge onto different child sets again (DP 1.0.12 recovery
    leveling, Section 7). A blocked, standby, or non-request-bound row may
    still be read and shown historically; it must never vote here.
    """
    return (
        f"{alias}.request_id IS NOT NULL AND COALESCE({alias}.blocked,0)=0 "
        f"AND COALESCE({alias}.mirror_state,'')!='standby'"
    )


def is_canonical_artifact_row(row) -> bool:
    """Python-side twin of ``canonical_artifact_membership_sql`` for a row
    (or dict) already carrying ``request_id``/``blocked``/``mirror_state``."""
    return (
        row["request_id"] is not None
        and not bool(row["blocked"])
        and str(row["mirror_state"] or "") != "standby"
    )


async def _durable_canonical_targets_for_request(db, request_id) -> set[int]:
    """DP 1.0.12 canonical equivalence/lifecycle correction, Section 6: every
    canonical artifact id, if any, that ``request_id``'s own candidate
    provenance is durably bound to.

    Same-transfer canonical convergence intentionally never writes an
    ``artifact_consolidations`` row (that table is cross-transfer provenance
    only), so ``canonical_candidate_origins``/``canonical_candidate_bindings``
    -- populated for both same- and cross-transfer contributors by
    ``transfers.canonical.CanonicalOwnership.attach()`` -- is the durable
    source of same-transfer membership; ``artifact_consolidations`` remains
    an additional valid cross-transfer mapping source. Never inferred from
    URL/filename/hostname text or current provider state. This is the ONE
    owner of this lookup; ``transfers.canonical.CanonicalOwnership
    .durable_owner_for_request`` and this module's own lifecycle-voting
    check both call it rather than duplicating the SQL."""
    if not request_id:
        return set()
    rows = await db.fetchall(
        """SELECT DISTINCT b.canonical_artifact_id AS canonical_artifact_id
            FROM canonical_candidate_origins o
            JOIN canonical_candidate_bindings b ON b.id=o.binding_id
            WHERE o.request_id=?
            UNION
            SELECT canonical_artifact_id FROM artifact_consolidations WHERE source_request_id=?""",
        (request_id, request_id),
    )
    return {int(row["canonical_artifact_id"]) for row in rows}


async def _satisfied_elsewhere(db, artifact) -> bool:
    """DP 1.0.12 Root Cause B (Section 5): True when ``artifact`` is a failed
    representation of a logical delivery obligation that a DIFFERENT,
    already-completed canonical artifact durably satisfies.

    This governs only whether the row may cast an operational FAILED vote --
    it remains fully visible in presentation/details for provenance, and its
    own state/history is never mutated here (Section 5: "without deleting
    history"). A row with no durable canonical mapping -- a genuinely
    independent artifact -- always still votes (Section 5.2)."""
    targets = await _durable_canonical_targets_for_request(db, artifact.request_id)
    targets.discard(int(artifact.id))
    if not targets:
        return False
    placeholders = ",".join("?" * len(targets))
    row = await db.fetchone(
        f"SELECT 1 AS ok FROM download_files WHERE id IN ({placeholders}) AND status='completed' LIMIT 1",
        tuple(targets),
    )
    return row is not None


def _logical_slot_key_for_request(record) -> str:
    """DP 1.0.12 CANON-001 exhausted-identity completion policy, Section 9:
    the durable logical-delivery-slot key for a request -- its own resolved
    ``SourceEntry`` when one exists, else its declared
    ``TransferRequest.name`` -- through the ONE ``transfers.mirrors
    .logical_key`` pairing-key algorithm. This is the SAME dispatch
    ``transfers.cohorts._prospective_logical_key`` already uses to derive a
    same-transfer sibling's best-effort identity before it has necessarily
    resolved; reusing ``mirrors.logical_key`` directly here (never a second,
    independently-maintained normalizer) is what keeps this module and
    ``transfers.cohorts`` from silently drifting onto two different
    interpretations of "logical slot" over time. Empty ("") is a genuinely
    UNKNOWN identity -- never itself evidence of a match."""
    if record.entry is not None:
        return logical_key(record.entry)
    return logical_key(SimpleNamespace(relative_path="", name=str(getattr(record.request, "name", "") or "")))


def _logical_slot_key_for_artifact(artifact) -> str:
    """Same normalization, read directly off a completed canonical artifact's
    own durably stored candidates -- never reconstructed from
    ``artifact.name`` alone with an artificially empty ``relative_path``.
    ``download_files`` has no ``relative_path`` column of its own, but each
    of the artifact's candidates already carries the exact relative_path/name
    facts a resolved ``SourceEntry`` stamped onto it at materialization time
    (``transfers._engine_base.TransferEngine._materialize``); re-deriving the
    key from the bare filename would silently collapse a pathful logical slot
    (e.g. ``disc1/file.iso``) down to its basename, letting an unrelated
    ``file.iso`` in a different directory falsely appear to share its slot.

    Requires exactly one distinct, non-empty logical key across every
    candidate the artifact carries -- any ambiguity (candidates disagreeing)
    or absence (no candidates, or every one keying empty) is conservatively
    UNKNOWN, never a match (Section 10: unknown blocks completion)."""
    keys = {logical_key(candidate) for candidate in artifact.candidates}
    keys.discard("")
    return next(iter(keys)) if len(keys) == 1 else ""


async def _completion_obligation_satisfied(db, record, completed_canonical_keys) -> bool:
    """DP 1.0.12 CANON-001 exhausted-identity completion policy, Sections 6.2/
    8/9/10: True only when ``record`` -- a quiescently-held (``_HELD_
    DISPOSITIONS``), proof-exhausted request -- has its logical delivery
    obligation ALREADY durably satisfied by a different, completed canonical
    artifact in the SAME transfer.

    This is a one-directional delivery-truth read only (Section 9): it never
    creates a canonical binding, never proves source equivalence, never
    merges artifacts, and never touches ``record``'s own equivalence
    disposition -- identity for this alternate source remains genuinely
    unresolved. Conservative by construction (Section 10): a request with no
    derivable logical key never matches (unknown blocks completion), and a
    request that ever produced its own ``download_files`` row -- any status,
    not only a currently-voting one; a real materialized artifact/writer of
    its own is conclusive evidence this is NOT a mere identity-unproven
    alternate for someone else's already-delivered slot -- is never excused
    this way either."""
    key = _logical_slot_key_for_request(record)
    if not key or key not in completed_canonical_keys:
        return False
    own_artifact = await db.fetchone(
        "SELECT 1 AS ok FROM download_files WHERE request_id=? LIMIT 1", (record.id,),
    )
    return own_artifact is None


async def _voting_artifacts(db, artifacts):
    """DP 1.0.12 Root Cause B (Section 5): the canonical-membership artifacts
    that may actually cast a lifecycle vote (completion/FAILED) for the
    parent transfer -- ``artifacts`` minus a failed row whose own logical
    delivery obligation a different, completed canonical artifact already
    satisfies. A genuinely independent failed artifact -- with no durable
    canonical mapping -- still votes (Section 5.2); this is not a blanket
    "completed wins" rule."""
    result = []
    for item in artifacts:
        if item.state == "error" and await _satisfied_elsewhere(db, item):
            continue
        result.append(item)
    return tuple(result)


# Route History's middle value is the logical source route, not whatever URL an
# executor was handed. Only these two labels are produced for BitTorrent-class
# lineage; everything about them comes from durable request lineage and the
# durable neutral cache fact, never from a provider name or endpoint domain.
_BITTORRENT_ROUTE_IDENTITY = "BitTorrent"
_TORRENT_CACHE_ROUTE_IDENTITY = "Torrent cache"


def _decode_resolution_result(raw_result):
    """One durable ``resolution_attempts.result`` as a plain mapping, or ``{}``
    when it is absent, undecodable, or not an object. Decodes with the SAME
    ``codec.load`` pattern ``_backfill_provenance`` already uses for this exact
    column -- never a second decode convention."""
    if not raw_result:
        return {}
    try:
        payload = codec.load(raw_result, {})
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _single_route_candidate(payload):
    """The one ``TransferCandidate`` a historical resolution attempt produced,
    or ``None`` when it produced zero, several, or an undecodable set -- all
    ambiguous, never guessed (DP 1.0.12 Route History identity correction,
    Section 16 and Gate 9 revision 2)."""
    try:
        candidates = tuple(codec.candidate(item) for item in payload.get("candidates", []))
    except (TypeError, ValueError, KeyError):
        return None
    return candidates[0] if len(candidates) == 1 else None


def _route_endpoint_projection(candidate):
    """DP 1.0.12 Route History identity correction: the durable historical
    route endpoint of an ORDINARY (direct) route, derived from that SAME
    resolution attempt's own durably stored candidate -- never from current
    artifact/candidate state, current execution, canonical binding, or
    request filename.

    Returns ``(route_origin, route_location)`` via the existing
    ``core.presentation_safety.safe_route_endpoint`` sanitizer, or
    ``(None, None)`` when there is no single candidate, that candidate carries
    zero or more than one endpoint (nothing durable identifies which endpoint
    WITHIN a candidate represented the actual route, so ``endpoints[0]`` would
    be exactly the positional inference this correction eliminates; General
    HTTP's stated invariant is one candidate with exactly one endpoint, so
    this never narrows the target case; Gate 9 revision 2), or it has no
    safely representable endpoint."""
    if candidate is None or len(candidate.endpoints) != 1:
        return None, None
    return safe_route_endpoint(candidate.endpoints[0].address)


def _request_roots(requests):
    """``{request_id: (root_request_id, root_kind)}`` from the transfer's own
    durable ``parent_id`` lineage.

    A transfer may hold several roots, so lineage is resolved per request, not
    read off "the first request". A request whose parent chain never reaches a
    root inside this transfer (orphaned or cyclic) is omitted: its lineage is
    unproven and is never guessed."""
    by_id = {item["id"]: item for item in requests}
    kinds = {}
    roots = {}
    for item in requests:
        current, hops = item, 0
        while current is not None and current.get("parent_id") is not None and hops <= len(by_id):
            current, hops = by_id.get(current["parent_id"]), hops + 1
        if current is None or current.get("parent_id") is not None:
            continue
        root_id = current["id"]
        if root_id not in kinds:
            try:
                kinds[root_id] = str(codec.load(current["payload"], {}).get("kind") or "").strip().lower()
            except (TypeError, ValueError, AttributeError):
                kinds[root_id] = ""
        roots[item["id"]] = (root_id, kinds[root_id])
    return roots


def _root_cache_presence(route_attempts, payloads, roots):
    """``{(root_request_id, provider_id): CachePresence}``: the FIRST
    authoritative (HIT/MISS) cache observation a provider recorded for a
    root's own acquisition.

    ``route_attempts`` arrive ordered by the durable per-transfer route
    ordinal, so "first" is chronological acquisition order, deterministically.
    It is the observation that established that root's provider acquisition --
    never the latest one, so a torrent that was a MISS cannot become a HIT
    because a retry or re-resolution found it ready later. Only attempts on
    the root request itself count: descendants describe provider-generated
    materialization, not the acquisition. UNKNOWN never establishes a fact,
    and the fact is per provider because cache presence is a provider's
    statement."""
    facts = {}
    for row, payload in zip(route_attempts, payloads):
        root = roots.get(row["request_id"])
        if root is None or root[0] != row["request_id"]:
            continue
        key = (root[0], row["provider_id"])
        if key in facts:
            continue
        observation = payload.get("observation")
        presence = codec.cache_presence(observation.get("cache_presence") if isinstance(observation, dict) else None)
        if presence is not CachePresence.UNKNOWN:
            facts[key] = presence
    return facts


def _logical_route_identity(row, candidate, roots, root_cache):
    """``(True, identity)`` when this attempt's route identity is its LOGICAL
    source rather than its execution endpoint, else ``(False, None)`` for an
    ordinary native/direct route whose endpoint is the route.

    * Lineage from a BitTorrent-class root wins over the request's own kind: a
      provider-generated HTTP(S) descendant still belongs to its magnet/torrent
      root. Identity is "Torrent cache" only for an authoritative HIT on that
      root's acquisition, otherwise "BitTorrent".
    * A provider-issued delivery endpoint is an execution capability. The
      route is the upstream host durably attested by the candidate's own
      ``SourceIdentity``; without a provable safe host the identity is unknown
      (``None``) -- never the delivery endpoint, and never a guess."""
    root = roots.get(row["request_id"])
    if root is not None and root[1] in BITTORRENT_REQUEST_KINDS:
        hit = root_cache.get((root[0], row["provider_id"])) is CachePresence.HIT
        return True, _TORRENT_CACHE_ROUTE_IDENTITY if hit else _BITTORRENT_ROUTE_IDENTITY
    if candidate is not None and candidate.delivery is DeliveryKind.PROVIDER_ISSUED:
        source = candidate.source_identity
        if source is not None and str(source.scope or "").strip().lower() == "host":
            return True, safe_public_host(source.key)
        return True, None
    return False, None


def _assign_route_identities(route_attempts):
    """DP 1.0.12 Route History identity correction, Section 10 (same-origin
    disambiguation): compute each ORDINARY row's display-ready
    ``route_identity`` once, in this one presentation pass over the
    transfer's own route history.

    A ``route_origin`` shared by 2+ rows in this SAME list falls back to the
    more specific ``route_location`` for exactly those rows; a uniquely-
    occurring origin uses the bare origin. A row with no safely representable
    origin gets ``route_identity=None`` -- never fabricated."""
    origin_counts = Counter(item["route_origin"] for item in route_attempts if item.get("route_origin"))
    for item in route_attempts:
        origin = item.get("route_origin")
        if not origin:
            item["route_identity"] = None
        elif origin_counts[origin] > 1:
            item["route_identity"] = item.get("route_location") or origin
        else:
            item["route_identity"] = origin


def _project_route_history(route_attempts, requests):
    """The one Route History presentation owner: sets ``route_origin``,
    ``route_location`` and ``route_identity`` on every historical route row.

    Everything derives from the exact historical ``resolution_attempts.result``
    (carried on each row as ``resolution_result`` and consumed here) plus the
    transfer's durable request lineage -- never from current provider
    enablement/applicability, current cache contents, current artifact binding,
    or current execution state. A logical-identity row (see
    ``_logical_route_identity``) carries no endpoint origin/location, so a
    provider-issued capability path can never reach the browser or its tooltip;
    ordinary rows keep the endpoint projection and same-origin disambiguation
    exactly."""
    payloads = [_decode_resolution_result(row.pop("resolution_result", None)) for row in route_attempts]
    roots = _request_roots(requests)
    root_cache = _root_cache_presence(route_attempts, payloads, roots)
    ordinary = []
    for row, payload in zip(route_attempts, payloads):
        candidate = _single_route_candidate(payload)
        logical, identity = _logical_route_identity(row, candidate, roots, root_cache)
        if logical:
            row["route_origin"], row["route_location"], row["route_identity"] = None, None, identity
        else:
            row["route_origin"], row["route_location"] = _route_endpoint_projection(candidate)
            ordinary.append(row)
    _assign_route_identities(ordinary)


class TransferRepository:
    async def has_integration_references(self, identity=None):
        """Connection changes cannot abandon live jobs or unresolved resources."""
        async with get_db() as db:
            params = () if identity is None else (identity,)
            executor_filter = "" if identity is None else " AND executor_id=?"
            provider_filter = "" if identity is None else " AND provider_id=?"
            if await db.fetchone("SELECT id FROM execution_attempts WHERE authorized=1 AND state IN ('prepared','queued','transferring','paused','unknown')" + executor_filter + " LIMIT 1", params):
                return True
            return bool(await db.fetchone("SELECT id FROM provider_resources WHERE state!='absent'" + provider_filter + " LIMIT 1", params))

    async def pending_events(self):
        async with get_db() as db:
            return await db.fetchall("SELECT * FROM application_events WHERE claimed=0 ORDER BY id LIMIT 100")

    async def claim_event(self, event_id):
        async with get_db() as db:
            cursor = await db.execute("UPDATE application_events SET claimed=1 WHERE id=? AND claimed=0", (event_id,))
            await db.commit()
            return bool(cursor.rowcount)

    async def queue_postprocessing(self, transfer_id, processors, paths):
        async with get_db() as db:
            created = False
            for processor in processors:
                cursor = await db.execute("INSERT OR IGNORE INTO postprocess_attempts(transfer_id,processor_id,paths) VALUES(?,?,?)",
                    (transfer_id, processor.descriptor.id, codec.dump(paths)))
                created = created or bool(cursor.rowcount)
            if created:
                await db.execute("UPDATE torrents SET extraction_status='pending' WHERE id=? AND status NOT IN ('completed','consolidated','deleted') AND COALESCE(extraction_status,'')!='extracting'", (transfer_id,))
            await db.commit()

    async def postprocessing_jobs(self):
        async with get_db() as db:
            return await db.fetchall("""SELECT p.* FROM postprocess_attempts p JOIN torrents t ON t.id=p.transfer_id
                WHERE p.state='pending' AND t.status='extracting' ORDER BY t.priority DESC,t.id""")

    async def claim_postprocessing(self, transfer_id, processor_id):
        async with get_db() as db:
            cursor = await db.execute("UPDATE postprocess_attempts SET state='processing' WHERE transfer_id=? AND processor_id=? AND state='pending'", (transfer_id, processor_id))
            await db.execute("UPDATE torrents SET extraction_status='extracting' WHERE id=? AND status='extracting'", (transfer_id,))
            await db.commit()
            return bool(cursor.rowcount)

    async def finish_postprocessing(self, transfer_id, processor_id, outcome):
        async with get_db() as db:
            await db.execute("UPDATE postprocess_attempts SET state='finished',outcome=? WHERE transfer_id=? AND processor_id=?", (codec.dump(outcome), transfer_id, processor_id))
            message = f"Post-processing {processor_id}: " + (outcome.error.message if outcome.error else outcome.detail or outcome.kind)
            await db.execute("INSERT INTO events(torrent_id,level,message) VALUES(?,?,?)", (transfer_id, "error" if outcome.error else "info", message))
            jobs = await db.fetchall("SELECT state,outcome FROM postprocess_attempts WHERE transfer_id=?", (transfer_id,))
            finished = all(job["state"] == "finished" for job in jobs)
            if finished:
                outcomes = [codec.load(job["outcome"]) for job in jobs if job["outcome"]]
                errors = [outcome.get("error") for outcome in outcomes]
                error = next((NormalizedError.from_dict(item) for item in errors if item), None)
                status = "error" if error else "skipped" if all(item["kind"] == "skipped" for item in outcomes) else "completed"
                await db.execute("UPDATE torrents SET extraction_status=?,extraction_error=? WHERE id=? AND status NOT IN ('deleted','consolidated')", (status, error.message if error else None, transfer_id))
            await db.commit()
            return finished

    async def interrupted_postprocessing(self):
        async with get_db() as db:
            return await db.fetchall("SELECT transfer_id,processor_id FROM postprocess_attempts WHERE state='processing'")

    async def initialize(self) -> None:
        # Runtime repositories consume the schema; database bootstrap/migration
        # is the sole authority allowed to create or repair it.
        await validate_transfer_repository_schema()

    @staticmethod
    def _safe_candidate_source(candidate):
        source = candidate.source_identity if candidate else None
        if source is not None:
            return {"scope": str(source.scope), "key": str(source.key)}
        return {"scope": "candidate", "key": str(candidate.id)} if candidate else None

    @classmethod
    def _candidate_summary(cls, candidates):
        return codec.dump([
            {
                "candidate_id": str(candidate.id),
                "provider_id": str(candidate.provider_id or ""),
                "ordinal": ordinal,
                "source": cls._safe_candidate_source(candidate),
            }
            for ordinal, candidate in enumerate(candidates, start=1)
        ])

    @staticmethod
    def _execution_outcome(state):
        value = str(state)
        if value == "succeeded":
            return "succeeded"
        if value in {"failed", "absent"}:
            return "failed"
        if value == "cancelled":
            return "cancelled"
        if value == "unknown":
            return "unknown"
        return "active"

    @classmethod
    async def _candidate_route(cls, db, transfer_id, candidate, *, artifact_id=None):
        if candidate is None or not candidate.id or not candidate.provider_id:
            return None
        if artifact_id is not None:
            bound = await db.fetchone(
                """SELECT o.resolution_attempt_id FROM canonical_candidate_bindings b
                    JOIN canonical_candidate_origins o ON o.binding_id=b.id
                    WHERE b.canonical_artifact_id=? AND b.candidate_id=? AND b.provider_id=?
                    ORDER BY CASE WHEN o.discovered_candidate_id=b.candidate_id THEN 0 ELSE 1 END,o.id LIMIT 1""",
                (artifact_id, str(candidate.id), str(candidate.provider_id)),
            )
            if bound:
                return bound["resolution_attempt_id"]
        rows = await db.fetchall("""SELECT p.resolution_attempt_id,p.ordinal,p.candidate_summary,a.provider_id
            FROM route_attempt_provenance p JOIN resolution_attempts a ON a.id=p.resolution_attempt_id
            WHERE p.transfer_id=? AND a.provider_id=? ORDER BY p.ordinal DESC,a.updated_at DESC,a.id DESC""",
            (transfer_id, candidate.provider_id))
        for row in rows:
            for item in codec.load(row["candidate_summary"], []):
                if str(item.get("candidate_id") or "") == str(candidate.id):
                    return row["resolution_attempt_id"]
        return None

    @classmethod
    async def _backfill_provenance(cls, db):
        """Idempotently migrate only facts already durably present before Item 9."""
        route_rows = await db.fetchall("""SELECT a.*,r.transfer_id FROM resolution_attempts a
            JOIN transfer_requests r ON r.id=a.request_id
            LEFT JOIN route_attempt_provenance p ON p.resolution_attempt_id=a.id
            WHERE p.resolution_attempt_id IS NULL
            ORDER BY r.transfer_id,a.created_at,a.id""")
        for row in route_rows:
            ordinal_row = await db.fetchone("SELECT COALESCE(MAX(ordinal),0) AS n FROM route_attempt_provenance WHERE transfer_id=?", (row["transfer_id"],))
            ordinal = int(ordinal_row["n"] or 0) + 1
            candidates = ()
            if row.get("result"):
                try:
                    payload = codec.load(row["result"], {})
                    candidates = tuple(codec.candidate(value) for value in payload.get("candidates", []))
                except (TypeError, ValueError, KeyError):
                    candidates = ()
            outcome = "failed" if row["state"] == "failed" else "resolved" if row["state"] == "succeeded" else "unknown"
            await db.execute("""INSERT OR IGNORE INTO route_attempt_provenance(
                resolution_attempt_id,transfer_id,request_id,ordinal,operation,candidate_summary,outcome,history_quality)
                VALUES(?,?,?,?,?,?,?,'legacy_known')""",
                (row["id"], row["transfer_id"], row["request_id"], ordinal, "legacy", cls._candidate_summary(candidates), outcome))

        execution_rows = await db.fetchall("""SELECT e.*,f.status AS artifact_status,f.execution_attempt_id AS current_execution_id,
                f.candidates AS artifact_candidates,f.selected_candidate
            FROM execution_attempts e JOIN download_files f ON f.id=e.artifact_id
            LEFT JOIN execution_attempt_provenance p ON p.execution_attempt_id=e.id
            WHERE p.execution_attempt_id IS NULL
            ORDER BY e.transfer_id,e.artifact_id,e.created_at,e.id""")
        for row in execution_rows:
            candidate = None
            if row.get("candidate"):
                try:
                    candidate = codec.candidate(codec.load(row["candidate"]))
                except (TypeError, ValueError, KeyError):
                    candidate = None
            if candidate is None and row.get("current_execution_id") == row["id"] and row.get("artifact_candidates"):
                try:
                    candidates = [codec.candidate(value) for value in codec.load(row["artifact_candidates"], [])]
                    selected = int(row.get("selected_candidate") or 0)
                    candidate = candidates[selected] if 0 <= selected < len(candidates) else None
                except (TypeError, ValueError, KeyError, IndexError):
                    candidate = None
            provider_id = str(candidate.provider_id) if candidate and candidate.provider_id else None
            route_attempt_id = await cls._candidate_route(
                db, row["transfer_id"], candidate, artifact_id=row["artifact_id"]
            ) if candidate else None
            ordinal_row = await db.fetchone("SELECT COALESCE(MAX(ordinal),0) AS n FROM execution_attempt_provenance WHERE artifact_id=?", (row["artifact_id"],))
            ordinal = int(ordinal_row["n"] or 0) + 1
            delivered = bool(provider_id and row["state"] == "succeeded" and row.get("artifact_status") == "completed" and row.get("current_execution_id") == row["id"])
            await db.execute("""INSERT OR IGNORE INTO execution_attempt_provenance(
                execution_attempt_id,route_attempt_id,transfer_id,artifact_id,ordinal,provider_id,candidate_id,candidate_source,
                outcome,delivered,history_quality) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (row["id"], route_attempt_id, row["transfer_id"], row["artifact_id"], ordinal, provider_id,
                 str(candidate.id) if candidate else None, codec.dump(cls._safe_candidate_source(candidate)) if candidate else None,
                 "completed" if delivered else cls._execution_outcome(row["state"]), int(delivered),
                 "legacy_known" if provider_id else "legacy_unknown"))
            if delivered and route_attempt_id:
                await db.execute("UPDATE route_attempt_provenance SET outcome='completed',updated_at=CURRENT_TIMESTAMP WHERE resolution_attempt_id=?", (route_attempt_id,))

    @classmethod
    async def _begin_route_provenance(cls, db, attempt_id, transfer_id, request_id, provider_id, *, operation):
        previous = await db.fetchone("""SELECT a.id,a.provider_id,a.error,p.ordinal,p.outcome
            FROM resolution_attempts a JOIN route_attempt_provenance p ON p.resolution_attempt_id=a.id
            WHERE a.request_id=? AND a.id!=? ORDER BY p.ordinal DESC LIMIT 1""", (request_id, attempt_id))
        ordinal_row = await db.fetchone(
            "SELECT COALESCE(MAX(ordinal),0) AS n FROM route_attempt_provenance WHERE transfer_id=?", (transfer_id,)
        )
        ordinal = int(ordinal_row["n"] or 0) + 1
        previous_id = previous["id"] if previous else None
        transition_kind = None
        transition_reason = None
        if previous:
            if operation == "refresh":
                transition_kind = "candidate_refresh"
                transition_reason = "candidate_refresh"
            elif previous["provider_id"] != provider_id:
                transition_kind = "provider_change"
                transition_reason = "route_reselected"
            else:
                transition_kind = "resolution_retry"
                transition_reason = "retry"
            error = codec.error(previous.get("error"))
            if error is not None:
                transition_reason = str(error.category.value)
            if previous.get("outcome") in {"started", "resolved", "unknown"}:
                await db.execute("UPDATE route_attempt_provenance SET outcome='superseded',updated_at=CURRENT_TIMESTAMP WHERE resolution_attempt_id=?", (previous_id,))
        await db.execute("""INSERT INTO route_attempt_provenance(
            resolution_attempt_id,transfer_id,request_id,ordinal,operation,previous_attempt_id,transition_kind,transition_reason,
            candidate_summary,outcome,history_quality) VALUES(?,?,?,?,?,?,?,?,?,'started','recorded')""",
            (attempt_id, transfer_id, request_id, ordinal, operation, previous_id, transition_kind, transition_reason, codec.dump([])))

    @staticmethod
    def _transfer(row) -> Transfer | None:
        if not row:
            return None
        raw_hash = str(row["hash"] or "")
        # A retired (deleted) transfer's active dedupe key is a tombstone; present
        # the original logical fingerprint instead.
        display_hash = str(row.get("source_fingerprint") or "") if raw_hash.startswith("deleted:") else raw_hash
        return Transfer(int(row["id"]), str(row["name"] or ""), TransferState(row["status"]),
                        display_hash, str(row["source"] or ""), int(row["priority"] or 0),
                        bool(row.get("paused_intent")), float(row["progress"] or 0), codec.error(row.get("normalized_error")), int(row.get("lifecycle_epoch") or 0))

    async def get(self, transfer_id: int) -> Transfer | None:
        async with get_db() as db:
            row = await db.fetchone("""SELECT t.*, COALESCE(p.paused,0) AS paused_intent FROM torrents t
                LEFT JOIN transfer_pause_intents p ON p.torrent_id=t.id WHERE t.id=?""", (transfer_id,))
        return self._transfer(row)

    async def presentation(self, transfer_id: int, *, details=False):
        """Explicit canonical read model; opaque integration context stays private."""
        async with get_db() as db:
            row = await db.fetchone("""SELECT id,
                CASE WHEN hash LIKE 'deleted:%' THEN COALESCE(source_fingerprint,'') ELSE hash END AS hash,
                name,status,size_bytes,progress,local_path,source,label,priority,
                error_message,normalized_error,extraction_status,extraction_error,created_at,updated_at,completed_at
                FROM torrents WHERE id=?""", (transfer_id,))
            if not row:
                return None
            files = await db.fetchall("""SELECT f.id,f.torrent_id,f.request_id,f.filename,f.size_bytes,f.local_path,f.status,f.download_client,
                f.blocked,f.block_reason,f.retry_count,f.mirror_group_id,f.mirror_state,f.updated_at,f.normalized_error,
                e.progress AS execution_progress FROM download_files f
                LEFT JOIN execution_attempts e ON e.id=f.execution_attempt_id WHERE f.torrent_id=? ORDER BY f.id""", (transfer_id,))
            requests = await db.fetchall("""SELECT id,parent_id,state,error,payload,metadata FROM transfer_requests
                WHERE transfer_id=? ORDER BY CASE WHEN parent_id IS NULL THEN 0 ELSE 1 END,ordinal,id""", (transfer_id,))
            resources = await db.fetchall("SELECT id,provider_id,state FROM provider_resources WHERE transfer_id=?", (transfer_id,))
            providers = await db.fetchall("SELECT DISTINCT a.provider_id FROM resolution_attempts a JOIN transfer_requests r ON r.id=a.request_id WHERE r.transfer_id=?", (transfer_id,))
            # ``a.result`` (the durable historical ResolutionResult, used
            # below to derive the safe per-row Route History endpoint) rides
            # along in this SAME join, but only when ``details`` actually
            # needs it (Gate 9 revision 3): the list path calls presentation()
            # for every row on every poll purely for lightweight facts like
            # current_provider_id, so unconditionally pulling the full
            # historical result blob -- materially larger than
            # candidate_summary -- for every historical route attempt on
            # every list row would be a real, avoidable list-path I/O/memory
            # cost for a fact the list projection never reads. This is a
            # trusted internal literal switch, never untrusted input, and
            # keeps the query a single set-based join either way -- no
            # per-route query, no N+1.
            resolution_result_column = ",a.result AS resolution_result" if details else ""
            route_attempts = await db.fetchall(f"""SELECT p.resolution_attempt_id AS id,p.request_id,p.ordinal,p.operation,p.previous_attempt_id,
                p.transition_kind,p.transition_reason,p.candidate_summary,p.outcome,p.history_quality,a.provider_id,
                a.state AS resolution_state{resolution_result_column},a.created_at,a.updated_at FROM route_attempt_provenance p
                JOIN resolution_attempts a ON a.id=p.resolution_attempt_id WHERE p.transfer_id=?
                ORDER BY p.ordinal,p.resolution_attempt_id""", (transfer_id,))
            execution_history = await db.fetchall("""SELECT e.id,e.artifact_id,e.executor_id,e.state AS execution_state,e.created_at,e.updated_at,
                p.route_attempt_id,p.provider_id,p.candidate_id,p.candidate_source,p.ordinal,p.outcome,p.delivered,p.history_quality
                FROM execution_attempt_provenance p JOIN execution_attempts e ON e.id=p.execution_attempt_id
                WHERE p.transfer_id=? ORDER BY p.created_at,p.artifact_id,p.ordinal,p.execution_attempt_id""", (transfer_id,))
            consolidations = await db.fetchall("""SELECT a.contributing_artifact_id,a.source_request_id,a.canonical_artifact_id,
                c.torrent_id AS canonical_transfer_id FROM artifact_consolidations a
                JOIN download_files c ON c.id=a.canonical_artifact_id
                WHERE a.source_transfer_id=? ORDER BY a.contributing_artifact_id""", (transfer_id,))
            candidate_bindings = await db.fetchall("""SELECT b.* FROM canonical_candidate_bindings b
                JOIN download_files f ON f.id=b.canonical_artifact_id
                WHERE f.torrent_id=? AND COALESCE(f.mirror_state,'')!='standby'
                ORDER BY b.canonical_artifact_id,b.candidate_order,b.id""", (transfer_id,))
            candidate_origins = await db.fetchall("""SELECT o.*,b.canonical_artifact_id FROM canonical_candidate_origins o
                JOIN canonical_candidate_bindings b ON b.id=o.binding_id
                JOIN download_files f ON f.id=b.canonical_artifact_id
                WHERE f.torrent_id=? AND COALESCE(f.mirror_state,'')!='standby'
                ORDER BY b.canonical_artifact_id,b.candidate_order,o.id""", (transfer_id,)) if details else []
            events = await db.fetchall("SELECT id,torrent_id,level,message,created_at FROM events WHERE torrent_id=? ORDER BY id DESC LIMIT 50", (transfer_id,)) if details else []
            input_challenge = await db.fetchone("SELECT * FROM transfer_input_challenges WHERE transfer_id=?", (transfer_id,))
        def normalized(item, field="normalized_error"):
            error = codec.error(item.pop(field, None))
            item["error"] = error.as_dict() if error else None
            item["error_message"] = error.message if error else None
            return item
        result = normalized(dict(row))
        result["file_count"] = len(files)
        result["blocked_count"] = sum(bool(item["blocked"]) for item in files)
        result["source_failure_count"] = sum(item["state"] == "failed" for item in requests)
        result["resources"] = [dict(item) for item in resources]
        historical_providers = sorted({item["provider_id"] for item in (*resources, *providers) if item.get("provider_id")})
        delivering_providers = sorted({item["provider_id"] for item in execution_history if item.get("delivered") and item.get("provider_id")})
        current_provider_id = next((item["provider_id"] for item in reversed(route_attempts) if item.get("provider_id")), None)
        result["historical_providers"] = historical_providers
        result["current_provider_id"] = current_provider_id
        result["delivering_provider_ids"] = delivering_providers
        result["delivering_provider_id"] = delivering_providers[0] if len(delivering_providers) == 1 else None
        result["provider_provenance_status"] = "recorded" if delivering_providers else "unknown_legacy" if result["status"] == "completed" else "pending"
        result["providers"] = delivering_providers if result["status"] == "completed" else historical_providers
        result["executors"] = sorted({item["download_client"] for item in files if item["download_client"]})
        result["input_required"] = public_challenge(input_challenge)
        targets = sorted({int(item["canonical_transfer_id"]) for item in consolidations})
        complete_consolidation = result["status"] == "consolidated"
        result["consolidation"] = {
            "state": "complete" if complete_consolidation else "partial" if consolidations else "none",
            "consolidated_into": targets[0] if complete_consolidation and len(targets) == 1 else None,
            "canonical_transfer_ids": targets,
            "artifact_mappings": [dict(item) for item in consolidations],
        }
        if details:
            result["request"] = codec.load(requests[0]["payload"], {}) if requests else None
            result["files"] = []
            for row in files:
                item = normalized(dict(row))
                # Historical/inactive rows (blocked, standby, or not bound to a
                # live request) remain visible here for provenance, but must
                # never be mistaken for current operational truth by a caller
                # that doesn't separately re-check membership (Section 7).
                item["is_canonical"] = is_canonical_artifact_row(item)
                progress = TransferProgress(**codec.load(item.pop("execution_progress", None), {}))
                item["download_speed"] = progress.bytes_per_second if item["status"] == "downloading" else 0
                item["progress"] = 100 if item["status"] == "completed" else min(100, progress.completed_bytes / item["size_bytes"] * 100) if item["size_bytes"] else 0
                result["files"].append(item)
            result["source_outcomes"] = []
            for item in requests:
                if item["state"] == "failed":
                    request = codec.request(codec.load(item["payload"]))
                    error = codec.error(item["error"])
                    result["source_outcomes"].append({"id": item["id"], "name": request.name or "Source request", "status": "error",
                        "error": error.as_dict() if error else None, "error_message": error.message if error else None})
            result["route_attempts"] = []
            for row in route_attempts:
                item = dict(row)
                item["candidates"] = codec.load(item.pop("candidate_summary"), [])
                result["route_attempts"].append(item)
            _project_route_history(result["route_attempts"], requests)
            result["execution_attempts"] = []
            for row in execution_history:
                item = dict(row)
                item["candidate_source"] = codec.load(item.get("candidate_source"), None)
                item["delivered"] = bool(item.get("delivered"))
                result["execution_attempts"].append(item)
            origins_by_binding = {}
            for origin in candidate_origins:
                origins_by_binding.setdefault(int(origin["binding_id"]), []).append({
                    "contributing_artifact_id": int(origin["contributing_artifact_id"]),
                    "contributing_transfer_id": int(origin["contributing_transfer_id"]),
                    "request_id": origin["request_id"],
                    "resolution_attempt_id": origin["resolution_attempt_id"],
                    "discovered_candidate_id": origin["discovered_candidate_id"],
                })
            result["candidate_bindings"] = []
            for binding in candidate_bindings:
                source_identity = None
                if binding.get("source_scope") and binding.get("source_key"):
                    source_identity = {"scope": binding["source_scope"], "key": binding["source_key"]}
                result["candidate_bindings"].append({
                    "canonical_artifact_id": int(binding["canonical_artifact_id"]),
                    "candidate_id": binding["candidate_id"],
                    "provider_id": binding["provider_id"],
                    "source_identity": source_identity,
                    "role": binding["role"],
                    "candidate_order": int(binding["candidate_order"]),
                    "origins": origins_by_binding.get(int(binding["id"]), []),
                })
            result["events"] = [dict(item) for item in events]
        return result

    async def aggregate_lifecycle(self, transfer_id: int, *, input_required: bool) -> AggregateLifecycleOutcome | None:
        """DP 1.0.12 recovery leveling, Sections 21-22: one atomic read-decide-
        write for ordinary parent-lifecycle aggregation.

        Every fact this decision depends on -- transfer status/pause intent,
        canonical artifacts (Section 7's membership predicate), requests,
        execution progress, and the global-pause flag -- is read from ONE
        ``BEGIN IMMEDIATE`` transaction, and the resulting status/progress
        write (when one applies) happens inside that SAME transaction. A
        concurrent aggregation call for the same transfer, or any other
        mutation that touches this transfer's row, serializes behind this one
        (SQLite's immediate write lock, backed by this codebase's existing
        ``busy_timeout``) rather than racing it -- so this can neither read a
        torn cross-connection snapshot (previously: four independent
        ``get_db()`` calls, each its own connection) nor overwrite a newer
        mutation with a decision computed from facts that were already stale
        by the time the old code's separate final write ran.

        ``input_required`` is the caller's own in-memory input-challenge fact
        (``transfers.input_required.InputChallengeStore``, not database
        state); when true this mirrors the historical short-circuit exactly:
        no metadata/progress recompute, no branch evaluation below, only the
        INPUT_REQUIRED transition when not already there.

        Returns ``None`` when the transfer no longer exists or is already
        terminal (nothing to aggregate).
        """
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                """SELECT t.*, COALESCE(p.paused,0) AS paused_intent FROM torrents t
                   LEFT JOIN transfer_pause_intents p ON p.torrent_id=t.id WHERE t.id=?""",
                (transfer_id,),
            )
            transfer = self._transfer(row)
            if transfer is None or transfer.state in _AGGREGATE_TERMINAL_STATES:
                await db.rollback()
                return None

            async def _transition(target, *, progress=None, error=None, verified=False):
                if not transition_allowed(transfer.state, target, verified=verified):
                    return
                await self._write_lifecycle_transition(
                    db, transfer_id, row["status"], row["progress"], row["normalized_error"],
                    target, progress=progress, error=error,
                )

            if input_required:
                if transfer.state != TransferState.INPUT_REQUIRED:
                    await _transition(TransferState.INPUT_REQUIRED)
                await db.commit()
                return AggregateLifecycleOutcome(False, ())

            request_rows = await db.fetchall(
                "SELECT * FROM transfer_requests WHERE transfer_id=? ORDER BY parent_id,ordinal", (transfer_id,),
            )
            requests = tuple(
                RequestRecord(r["id"], transfer_id, codec.request(codec.load(r["payload"])), r["state"],
                              r["parent_id"], codec.resource(codec.load(r["resource"])), r["attempts"],
                              r["retry_at"], codec.error(r["error"]), codec.entry(codec.load(r["metadata"])))
                for r in request_rows
            )
            artifact_rows = await db.fetchall(
                f"""SELECT f.*,e.handle FROM download_files f
                    LEFT JOIN execution_attempts e ON e.id=f.execution_attempt_id
                    WHERE f.torrent_id=? AND {canonical_artifact_membership_sql('f')} ORDER BY f.id""",
                (transfer_id,),
            )
            artifacts = tuple(
                Artifact(a["id"], transfer_id, a["request_id"], a["filename"], a["local_path"], a["size_bytes"] or 0,
                         a["status"], tuple(codec.candidate(item) for item in codec.load(a["candidates"], [])),
                         a["selected_candidate"], codec.handle(codec.load(a["handle"])), a["retry_count"] or 0,
                         a["retry_at"], codec.error(a["normalized_error"]))
                for a in artifact_rows
            )
            execution_rows = await db.fetchall("SELECT * FROM execution_attempts WHERE transfer_id=?", (transfer_id,))
            attempts_by_id = {e["id"]: self._execution_attempt(e) for e in execution_rows}
            # DP 1.0.12 Root Cause B (Section 5): a failed artifact whose own
            # logical delivery obligation a different, already-completed
            # canonical artifact durably satisfies must not vote toward
            # FAILED, or block completion, merely because it is present.
            voting_artifacts = await _voting_artifacts(db, artifacts)

            # DP 1.0.12 canonical lifecycle/recovery/completion rework,
            # Section 6: a request's lifecycle state (does autonomous
            # materialization work exist?) and its equivalence disposition
            # (what did identity proof establish?) are orthogonal durable
            # facts, both read in this SAME transaction. A ``materializing``
            # request whose bounded automatic proof-retry budget is durably
            # exhausted (``transfers.cohorts._HELD_DISPOSITIONS``) has no
            # scheduled autonomous work left -- it must not count as
            # ``pending`` (which would falsely stick the parent RESOLVING, or
            # block completion of the artifacts that already voted). It is
            # equally not equivalent to "nothing is happening": the identity
            # remains genuinely unresolved, so it durably holds the parent at
            # QUEUED (a quiescent, non-resolving wait -- see ``quiescent_hold``
            # below) rather than reporting either false active resolution or
            # a silent, misleading "no change".
            #
            # This ``(state='materializing', equivalence_disposition in
            # _HELD_DISPOSITIONS)`` pair is not a special case invented here:
            # it is the SAME canonical contract ``transfers.cohorts
            # .coordinate_collection`` already established and documents
            # (its own "held-class dispositions... keep the writer barrier up
            # without doing any further proof work" comment) as the sole
            # durable state that stops autonomous materialization for a held
            # request:
            #   - ``_process_request`` (transfers._engine_base.TransferEngine)
            #     routes every ``state='materializing'`` row through
            #     ``_materialize`` -> ``coordinate_collection`` on every tick;
            #     that function reads the SAME disposition and returns
            #     immediately for a held row (no proof work, no writer) --
            #     this aggregation reads the identical fact, never a second
            #     interpretation of it.
            #   - restart: both readers re-derive disposition fresh from this
            #     same durable column on every pass: there is no in-memory
            #     state to lose, so a held row cannot silently resume
            #     autonomous work nor lose its hold across a restart.
            #   - wake: the only writer of a `_HELD_DISPOSITIONS` value is the
            #     bounded proof-retry budget in ``cohorts._schedule_proof_
            #     retry``; the only path back out is an explicit operator
            #     retry (``TransferRepository.retry_requests`` resets
            #     ``equivalence_retry_count``/``equivalence_disposition``) or
            #     new proof evidence re-running the mapping -- never an
            #     automatic scheduler tick re-consuming budget or creating a
            #     writer.
            #   - presentation: a held request never materializes an artifact
            #     (``coordinate_collection`` returns before
            #     ``super()._materialize`` runs), so there is no artifact-
            #     level row for presentation to misrepresent as active; only
            #     the parent's own truthful QUEUED state (this branch)
            #     surfaces the hold.
            def _quiescently_held(row) -> bool:
                return row["state"] == "materializing" and str(row["equivalence_disposition"] or "") in _HELD_DISPOSITIONS

            pending = any(
                r["state"] in {"pending", "waiting", "waiting_parent", "resolving", "materializing"}
                and not _quiescently_held(r)
                for r in request_rows
            )
            held_pairs = tuple(
                (row, requests[index]) for index, row in enumerate(request_rows) if _quiescently_held(row)
            )
            quiescent_hold = bool(held_pairs)
            # DP 1.0.12 CANON-001 exhausted-identity completion policy,
            # Section 8: ``scheduler_held``/``quiescent_hold`` above keeps its
            # existing meaning untouched (still the sole scheduler/
            # materialization fact -- unavailable for writer allocation,
            # unavailable for ``pending``). ``completion_blocking_hold`` is a
            # SEPARATE, narrower question asked only of parent-completion
            # eligibility: does this same held request ALSO still represent
            # an unsatisfied delivery obligation? It starts equal to
            # ``quiescent_hold`` (the conservative default -- Section 10: if
            # this cannot be proven, the hold remains blocking) and is
            # lowered only when every held request's own logical slot is
            # already durably satisfied by a completed canonical artifact
            # (Section 9), which cannot be true when no canonical artifact
            # has completed at all -- skipping the per-row check entirely in
            # that (the common, still-in-flight) case.
            completion_blocking_hold = quiescent_hold
            if quiescent_hold:
                completed_canonical_keys = {
                    key for key in (
                        _logical_slot_key_for_artifact(item) for item in artifacts if item.state == "completed"
                    ) if key
                }
                if completed_canonical_keys:
                    completion_blocking_hold = False
                    for _, held_record in held_pairs:
                        if not await _completion_obligation_satisfied(db, held_record, completed_canonical_keys):
                            completion_blocking_hold = True
                            break
            total = sum(item.expected_bytes for item in artifacts)
            # DP 1.0.12 canonical transfer-detail materialized-path
            # correction: the durable "current materialized target"
            # (rendered by frontend/static/app.js as "Local Path") must be
            # the durable canonical owner's own target, never a failed
            # non-owner's -- so this reuses ``voting_artifacts`` (computed
            # just above), the SAME durable-binding-derived set that already
            # excludes a failed row whose logical delivery obligation a
            # different, completed canonical artifact durably satisfies
            # (``_voting_artifacts``/``_satisfied_elsewhere``). Falling back
            # to the full canonical-membership ``artifacts`` only when
            # nothing currently votes preserves prior behavior for that
            # otherwise-untouched edge case rather than inventing a new one.
            path_source = voting_artifacts or artifacts
            local_path = str(Path(path_source[0].target).parent) if path_source else ""
            await db.execute(
                "UPDATE torrents SET size_bytes=?,local_path=? WHERE id=? AND status NOT IN ('deleted','consolidated')",
                (total, local_path, transfer_id),
            )
            completed = sum(
                item.expected_bytes if item.state == "completed" else
                (min(item.expected_bytes, attempts_by_id[item.execution.attempt_id].progress.completed_bytes)
                 if item.execution else 0)
                for item in artifacts
            )
            progress = min(100.0, completed / total * 100) if total else 0.0

            should_complete = False
            paused = transfer.paused or await self._globally_paused(db)
            if not paused:
                # DP 1.0.12 canonical lifecycle/recovery/completion rework,
                # Section 6.4 (Gate 9 revision 2), refined by the CANON-001
                # exhausted-identity completion policy (Section 6.2/8):
                # completion requires every logical delivery obligation to be
                # satisfied -- a durably quiescent, identity-unresolved held
                # request is an unsatisfied one (identity remains UNPROVEN,
                # not proven equivalent) UNLESS its own logical delivery slot
                # is already durably satisfied by a different, completed
                # canonical artifact in this same transfer
                # (``completion_blocking_hold``, Section 9/10) -- never merely
                # an inert placeholder to retire from consideration. It can
                # never appear in ``voting_artifacts`` (a held request has no
                # artifact at all), so without this guard every real artifact
                # completing would silently terminalize the parent while an
                # UNSATISFIED hold sits unresolved -- the exact inference the
                # equivalence correction exists to forbid. An unsatisfied hold
                # must first be resolved (recovered/released) or the request
                # explicitly retried before completion may ever be reached.
                if artifacts and not pending and not completion_blocking_hold and all(
                    item.state == "completed" for item in voting_artifacts
                ):
                    should_complete = True
                elif any(item.state in {"downloading", "verifying"} for item in artifacts):
                    await _transition(TransferState.TRANSFERRING, progress=progress)
                elif any(item.state == "unknown" for item in artifacts):
                    await _transition(TransferState.QUEUED, progress=progress)
                # DP 1.0.12 canonical lifecycle/recovery/completion rework,
                # Section 7 (CANON-001): an artifact autonomously waiting on
                # recovery is exactly one more input FACT to this one
                # canonical decision, at the same precedence tier as the
                # other non-transferring "there is still queued-ish work"
                # states -- never a second, independently-timed read-decide-
                # write layered on top of this transaction's own conclusion
                # (the removed ``force_queued_for_autonomous_wait``). A real
                # active download (the ``downloading``/``verifying`` branch
                # above) still always wins.
                elif any(item.state in {"queued", "paused", "refresh_pending", "recovery_wait"} for item in artifacts):
                    await _transition(TransferState.QUEUED, progress=progress)
                elif pending:
                    await _transition(TransferState.RESOLVING, progress=progress)
                # DP 1.0.12 canonical lifecycle/recovery/completion rework,
                # Section 6.4 (Gate 9 revision 3): a genuine, independent
                # terminal failure is its own unsatisfied logical obligation
                # -- it belongs to a DIFFERENT voting artifact/request than
                # whichever one is quiescently held, and the hold must never
                # erase it. Evaluated BEFORE the quiescent-hold fallback
                # below: a prior ordering let ``elif quiescent_hold`` win
                # first, so one artifact's genuinely terminal ERROR sat
                # forever masked behind an unrelated sibling's unresolved
                # equivalence hold, reporting a truthless perpetual QUEUED
                # instead of the real FAILED outcome. The hold must prevent
                # false COMPLETED and false RESOLVING; it must not launder an
                # actual terminal failure into a nonterminal wait.
                elif any(item.state == "error" for item in voting_artifacts) or any(item.state == "failed" for item in requests):
                    error = next((item.error for item in (*voting_artifacts, *requests) if item.error), None)
                    await _transition(TransferState.FAILED, progress=progress, error=error)
                elif artifacts and all(item.state == "cancelled" for item in artifacts):
                    await _transition(TransferState.CANCELLED, progress=progress)
                # DP 1.0.12 canonical lifecycle/recovery/completion rework,
                # Section 6.4: no artifact is actively working, no artifact/
                # request has genuinely failed or been cancelled, and no
                # request has genuine autonomous work left, but identity
                # remains durably unresolved for at least one quiescently-
                # held request -- truthfully a non-resolving wait, not
                # perpetual "processing" and not silent staleness.
                elif quiescent_hold:
                    await _transition(TransferState.QUEUED, progress=progress)
                elif not artifacts:
                    blocked_row = await db.fetchone(
                        "SELECT COUNT(*) AS n FROM download_files WHERE torrent_id=? AND blocked=1", (transfer_id,),
                    )
                    if int((blocked_row or {}).get("n") or 0) and transition_allowed(transfer.state, TransferState.COMPLETED, verified=True):
                        await self._write_lifecycle_transition(
                            db, transfer_id, row["status"], row["progress"], row["normalized_error"],
                            TransferState.COMPLETED, progress=0,
                        )
                        skip_outcome = TransferOutcome(OutcomeKind.SKIPPED, detail="No selected artifacts")
                        await db.execute(
                            "INSERT INTO transfer_outcomes(transfer_id,attempt_id,kind,payload) VALUES(?,?,?,?)",
                            (transfer_id, None, skip_outcome.kind, codec.dump(skip_outcome)),
                        )
                        await db.execute(
                            "INSERT INTO events(torrent_id,level,message) VALUES(?,?,?)",
                            (transfer_id, "info", str(skip_outcome.kind)),
                        )
            elif not any(str(item.get("state")) in _UNSETTLED_EXECUTION_STATES for item in execution_rows):
                # DP 1.0.12 canonical lifecycle/recovery/completion rework,
                # Section 7.2: durable paused truth folded into this SAME
                # atomic decision (previously a separate post-aggregate
                # repair in ``transfers.engine.TransferEngine._aggregate``,
                # run as its own read-decide-write after this transaction had
                # already committed -- a second parent-lifecycle authority
                # forbidden by Section 3.1). Pause intent alone is not enough
                # to claim parent PAUSED while a durable execution
                # observation is still active/unknown; once every recorded
                # attempt is quiescent, this is metadata-only -- it does not
                # dispatch, refresh, replace a GID, or consume recovery
                # authority.
                await _transition(TransferState.PAUSED)
            await db.commit()
        # DP 1.0.12 Root Cause B: the caller's completion-verification sweep
        # (_engine_base.TransferEngine._complete) must never be handed a
        # satisfied-elsewhere failed row to verify -- its own target is not
        # expected to be a valid payload, and the logical obligation it
        # represents was already verified when the OTHER canonical completed.
        return AggregateLifecycleOutcome(should_complete, voting_artifacts)

    @staticmethod
    async def _globally_paused(db) -> bool:
        row = await db.fetchone("SELECT value FROM transfer_controls WHERE key='paused'")
        return bool(row and row["value"] == "1")

    async def update_metadata(self, transfer_id, *, label=None, priority=None):
        async with get_db() as db:
            if not await db.fetchone("SELECT id FROM torrents WHERE id=?", (transfer_id,)):
                raise KeyError(transfer_id)
            await db.execute("UPDATE torrents SET label=COALESCE(?,label),priority=COALESCE(?,priority),updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (label, priority, transfer_id))
            await db.commit()

    async def active(self) -> tuple[Transfer, ...]:
        async with get_db() as db:
            rows = await db.fetchall("""SELECT t.*, COALESCE(p.paused,0) AS paused_intent FROM torrents t
                LEFT JOIN transfer_pause_intents p ON p.torrent_id=t.id
                WHERE t.status NOT IN ('completed','consolidated','deleted','cancelled')
                AND EXISTS(SELECT 1 FROM transfer_requests r WHERE r.transfer_id=t.id)
                ORDER BY t.priority DESC,t.id""")
        return tuple(self._transfer(row) for row in rows)

    @staticmethod
    def _tombstone_hash(transfer_id: int, source_fingerprint: str) -> str:
        """Deterministic, per-transfer, non-recursive retired dedupe key."""
        return f"deleted:{int(transfer_id)}:{source_fingerprint}"

    @classmethod
    async def _retire_active_fingerprint(cls, db, row) -> None:
        """Within an open transaction: preserve the original logical fingerprint
        and retire the active unique ``hash`` key to the tombstone form. Idempotent
        and never recursively prefixed."""
        transfer_id = int(row["id"])
        current_hash = str(row["hash"] or "")
        original = str(row["source_fingerprint"] or current_hash)
        if current_hash.startswith("deleted:"):
            if row["source_fingerprint"] is None:
                await db.execute(
                    "UPDATE torrents SET source_fingerprint=? WHERE id=? AND source_fingerprint IS NULL",
                    (original, transfer_id),
                )
            return
        await db.execute(
            "UPDATE torrents SET source_fingerprint=COALESCE(source_fingerprint,?), hash=? WHERE id=?",
            (original, cls._tombstone_hash(transfer_id, original), transfer_id),
        )

    @staticmethod
    async def _predecessor_cleanup_blocks(db, transfer_id: int) -> bool:
        """The ONE predecessor-cleanup fence predicate, evaluated inside the
        caller's transaction/session so every same-object resource creation or
        reuse decision (a fresh root's first resolution, an inventory adoption)
        reads the same fact."""
        row = await db.fetchone("SELECT source_fingerprint FROM torrents WHERE id=?", (transfer_id,))
        fingerprint = row["source_fingerprint"] if row else None
        if not fingerprint:
            return False
        blocker = await db.fetchone(
            """SELECT 1 FROM provider_resources r
               JOIN torrents t ON t.id=r.transfer_id
               WHERE t.id != ? AND t.status='deleted' AND t.source_fingerprint=?
                 AND r.cleanup_authority IS NOT NULL
                 AND COALESCE(r.cleanup_abandoned, 0) = 0
                 AND r.state != 'absent'
               LIMIT 1""",
            (transfer_id, fingerprint),
        )
        return blocker is not None

    async def predecessor_cleanup_barrier(self, transfer_id: int) -> bool:
        """True while a retired (deleted) predecessor generation sharing this
        transfer's ``source_fingerprint`` still owns a provider resource whose
        cleanup responsibility is outstanding and has not been permanently
        abandoned.

        The fresh generation is admitted immediately but must not perform a
        conflicting provider-resource creation/reuse until no cleanup operation
        belonging to the predecessor can subsequently act on the shared native
        resource. The predicate deliberately does not distinguish
        pending / leased-in-flight / expired-claim / scheduled-retry — every one
        of those can still act, so every one blocks. Completion (authority
        cleared), an ABSENT resource, and terminal abandonment
        (``cleanup_abandoned``, set only after the provider cleanup call returned
        and policy gave up) release the block, so a fresh transfer is never
        deadlocked. A claim whose owner is lost is not a permanent block: its
        lease expires and the ordinary cleanup cadence re-claims it (see
        :meth:`claim_cleanup`).
        """
        async with get_db() as db:
            return await self._predecessor_cleanup_blocks(db, transfer_id)

    async def admit(self, requests: tuple[TransferRequest, ...], *, name: str, source: str = "manual", priority=0, deduplicate=True) -> tuple[Transfer, bool]:
        fingerprint = requests[0].fingerprint if len(requests) == 1 else ""
        # Routing preferences and display names are not logical source identity.
        # The same accepted request can be resolved through another integration.
        fingerprint = fingerprint or hashlib.sha256(codec.dump(tuple(
            (item.kind, item.payload, item.fingerprint) for item in requests)).encode()).hexdigest()
        if not deduplicate:
            fingerprint = "request:" + new_identity()
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("SELECT * FROM torrents WHERE hash=?", (fingerprint,))
            if row and str(row["status"]) == "deleted":
                # A user-deleted transfer must never remain the active dedupe /
                # recovery identity. A legacy deleted row (or a tombstone race)
                # still holding the active key is retired transactionally here so
                # this submission is a genuinely fresh lifecycle, never a silent
                # ``retry(..., reacquire=True)`` of the deleted transfer.
                await self._retire_active_fingerprint(db, row)
                row = None
            if row:
                transfer_id, created = int(row["id"]), False
            else:
                transfer_id = await db.execute_returning_id(
                    """INSERT INTO torrents(hash,name,status,source,priority,download_client,source_fingerprint)
                    VALUES(?,?,'pending',?,?,'',?)""", (fingerprint, name, source, priority, fingerprint))
                created = True
            existing = await db.fetchone("SELECT id FROM transfer_requests WHERE transfer_id=? LIMIT 1", (transfer_id,))
            if not existing:
                for ordinal, request in enumerate(requests):
                    await db.execute("INSERT INTO transfer_requests(id,transfer_id,ordinal,payload) VALUES(?,?,?,?)",
                                     (new_identity(), transfer_id, ordinal, codec.dump(request)))
            if created:
                await db.execute("INSERT INTO events(torrent_id,level,message) VALUES(?,'info','Transfer accepted')", (transfer_id,))
                await db.execute("INSERT INTO application_events(transfer_id,kind) VALUES(?,'accepted')", (transfer_id,))
            await db.commit()
        return await self.get(transfer_id), created

    @staticmethod
    async def _write_lifecycle_transition(
        db, transfer_id: int, current_status: str, current_progress, current_normalized_error,
        target: TransferState, *, progress=None, error=None,
    ) -> None:
        """Shared UPDATE+event-log body for a transfer status transition
        already validated by the caller (``transition_allowed``/
        ``expected_epoch``/no-op checks all happen before this is called).
        Shared by ``state()`` and ``aggregate_lifecycle()`` /
        ``force_queued_for_autonomous_wait()`` so the two paths can never
        silently diverge on what "the same transition" durably records."""
        if current_status == target and (progress is None or current_progress == progress) and current_normalized_error == (codec.dump(error) if error else None):
            return
        await db.execute("""UPDATE torrents SET status=?, progress=COALESCE(?,progress), normalized_error=?,
            error_message=?, updated_at=CURRENT_TIMESTAMP,
            completed_at=CASE WHEN ?='completed' THEN COALESCE(completed_at,CURRENT_TIMESTAMP)
                WHEN ? IN ('pending','queued') THEN NULL ELSE completed_at END WHERE id=?""",
            (target, progress, codec.dump(error) if error else None, error.message if error else None, target, target, transfer_id))
        if target in SIDE_STATE_RETIRING_TRANSFER_STATES:
            await _retire_transfer_auxiliary_state_in_db(db, transfer_id)
        if current_status != target or current_normalized_error != (codec.dump(error) if error else None):
            message = f"Transfer {target}" + (f": {error.message}" if error else "")
            await db.execute("INSERT INTO events(torrent_id,level,message) VALUES(?,?,?)", (transfer_id, "error" if error else "info", message))
            await db.execute("INSERT INTO application_events(transfer_id,kind,detail) VALUES(?,?,?)", (transfer_id, target, error.message if error else None))

    async def state(self, transfer_id: int, target: TransferState, *, progress=None, error=None, operator=False, expected_epoch=None, verified=False) -> bool:
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("SELECT status,progress,lifecycle_epoch,normalized_error FROM torrents WHERE id=?", (transfer_id,))
            if not row or not transition_allowed(TransferState(row["status"]), target, operator=operator, verified=verified):
                return False
            if expected_epoch is not None and row["lifecycle_epoch"] != expected_epoch:
                return False
            await self._write_lifecycle_transition(
                db, transfer_id, row["status"], row["progress"], row["normalized_error"], target, progress=progress, error=error,
            )
            await db.commit()
        return True

    async def cancel_with_execution_cleanup(self, transfer_id: int, *, expected_epoch: int, now: float) -> bool:
        """Atomically close logical lifecycle and persist external cleanup responsibility."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                "SELECT status,progress,lifecycle_epoch,normalized_error FROM torrents WHERE id=?",
                (transfer_id,),
            )
            if not row:
                return False
            current = TransferState(row["status"])
            if current == TransferState.CANCELLED:
                await db.commit()
                return True
            if not transition_allowed(current, TransferState.CANCELLED) or row["lifecycle_epoch"] != expected_epoch:
                return False

            await db.execute(
                """UPDATE torrents SET status='cancelled',normalized_error=NULL,error_message=NULL,
                    updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (transfer_id,),
            )
            # FUNC-001: this path settles the parent into CANCELLED without
            # going through _write_lifecycle_transition, so it must invoke the
            # same transaction-local auxiliary-state retirement directly.
            await _retire_transfer_auxiliary_state_in_db(db, transfer_id)
            await db.execute(
                "INSERT INTO events(torrent_id,level,message) VALUES(?,'info','Transfer cancelled')",
                (transfer_id,),
            )
            await db.execute(
                "INSERT INTO application_events(transfer_id,kind,detail) VALUES(?,'cancelled',NULL)",
                (transfer_id,),
            )
            await db.execute(
                """UPDATE execution_attempts SET cleanup_state='pending',cleanup_attempts=0,
                    cleanup_retry_at=?,cleanup_error=NULL
                    WHERE transfer_id=? AND authorized=1
                    AND state IN ('prepared','queued','transferring','paused','unknown')
                    AND id IN (SELECT execution_attempt_id FROM download_files
                        WHERE torrent_id=? AND execution_attempt_id IS NOT NULL)""",
                (now, transfer_id, transfer_id),
            )
            await db.execute(
                """UPDATE download_files SET status='cancelled',normalized_error=NULL,
                    continuation_reservation_expires_at=NULL,updated_at=CURRENT_TIMESTAMP
                    WHERE torrent_id=? AND status!='completed'""",
                (transfer_id,),
            )
            await db.commit()
        return True

    async def pending_execution_cleanup(self, now: float, *, transfer_id: int | None = None):
        async with get_db() as db:
            where_transfer = " AND e.transfer_id=?" if transfer_id is not None else ""
            params = (now, transfer_id) if transfer_id is not None else (now,)
            rows = await db.fetchall(
                """SELECT e.* FROM execution_attempts e JOIN torrents t ON t.id=e.transfer_id
                    WHERE e.cleanup_state IN ('pending','blocked') AND e.cleanup_retry_at<=?
                    AND t.status IN ('cancelled','deleted')""" + where_transfer +
                " ORDER BY e.transfer_id,e.created_at,e.id",
                params,
            )
        return tuple((self._execution_attempt(row), int(row["cleanup_attempts"] or 0), codec.error(row["cleanup_error"])) for row in rows)

    async def claim_execution_cleanup(self, attempt_id: str, *, now: float, lease_until: float) -> bool:
        async with get_db() as db:
            cursor = await db.execute(
                """UPDATE execution_attempts SET cleanup_state='pending',cleanup_retry_at=?,updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND cleanup_state IN ('pending','blocked') AND cleanup_retry_at<=?""",
                (lease_until, attempt_id, now),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def execution_cleanup_attempt(self, attempt_id: str) -> bool:
        """Consume one destructive executor-cancel attempt after a cleanup lease is held."""
        async with get_db() as db:
            cursor = await db.execute(
                """UPDATE execution_attempts SET cleanup_attempts=cleanup_attempts+1,updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND cleanup_state='pending'""",
                (attempt_id,),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def execution_cleanup_retry(self, attempt_id: str, error: NormalizedError, retry_at: float | None) -> None:
        async with get_db() as db:
            await db.execute(
                """UPDATE execution_attempts SET cleanup_state=?,cleanup_error=?,cleanup_retry_at=?,
                    updated_at=CURRENT_TIMESTAMP WHERE id=? AND cleanup_state='pending'""",
                ("pending" if retry_at is not None else "blocked", codec.dump(error), retry_at or 0, attempt_id),
            )
            await db.commit()

    async def execution_cleanup_complete(self, attempt_id: str) -> None:
        async with get_db() as db:
            await db.execute(
                """UPDATE execution_attempts SET cleanup_state='complete',cleanup_error=NULL,cleanup_retry_at=0,
                    authorized=0,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (attempt_id,),
            )
            await db.commit()

    async def execution_cleanup_status(self, attempt_id: str):
        async with get_db() as db:
            row = await db.fetchone(
                "SELECT cleanup_state,cleanup_attempts,cleanup_retry_at,cleanup_error,authorized,state FROM execution_attempts WHERE id=?",
                (attempt_id,),
            )
        if not row:
            return None
        return {
            "state": row["cleanup_state"],
            "attempts": int(row["cleanup_attempts"] or 0),
            "retry_at": float(row["cleanup_retry_at"] or 0),
            "error": codec.error(row["cleanup_error"]),
            "authorized": bool(row["authorized"]),
            "execution_state": row["state"],
        }

    async def pause_intent(self, transfer_id: int, paused: bool) -> None:
        async with get_db() as db:
            await db.execute("""INSERT INTO transfer_pause_intents(torrent_id,paused) VALUES(?,?)
                ON CONFLICT(torrent_id) DO UPDATE SET paused=excluded.paused,updated_at=CURRENT_TIMESTAMP""", (transfer_id, int(paused)))
            await db.commit()

    async def requests(self, transfer_id: int) -> tuple[RequestRecord, ...]:
        async with get_db() as db:
            rows = await db.fetchall("SELECT * FROM transfer_requests WHERE transfer_id=? ORDER BY parent_id,ordinal", (transfer_id,))
        return tuple(RequestRecord(row["id"], transfer_id, codec.request(codec.load(row["payload"])), row["state"],
                                   row["parent_id"], codec.resource(codec.load(row["resource"])), row["attempts"],
                                   row["retry_at"], codec.error(row["error"]), codec.entry(codec.load(row["metadata"]))) for row in rows)

    async def bound_route_provider(self, request_id: str) -> str | None:
        """Return the provider owning this request's route: the latest durable
        route attempt, else the transfer's collection route binding."""
        async with get_db() as db:
            row = await db.fetchone(
                """SELECT a.provider_id FROM route_attempt_provenance p
                JOIN resolution_attempts a ON a.id=p.resolution_attempt_id
                WHERE a.request_id=? ORDER BY p.ordinal DESC LIMIT 1""",
                (request_id,),
            )
            if row and row.get("provider_id"):
                return str(row["provider_id"])
            row = await db.fetchone(
                """SELECT t.collection_route_provider_id FROM transfer_requests r
                JOIN torrents t ON t.id=r.transfer_id WHERE r.id=?""",
                (request_id,),
            )
        value = str((row or {}).get("collection_route_provider_id") or "").strip()
        return value or None

    async def begin_resolution(self, request_id: str, provider_id: str) -> ResolutionAttempt | None:
        identity = new_identity()
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("""SELECT r.* FROM transfer_requests r JOIN torrents t ON t.id=r.transfer_id
                LEFT JOIN transfer_pause_intents p ON p.torrent_id=t.id WHERE r.id=? AND r.state='pending'
                AND t.status NOT IN ('deleted','completed','consolidated','cancelled') AND COALESCE(p.paused,0)=0""", (request_id,))
            if not row:
                return None
            await db.execute("UPDATE transfer_requests SET state='resolving',attempts=attempts+1 WHERE id=?", (request_id,))
            await db.execute("INSERT INTO resolution_attempts(id,request_id,provider_id,state) VALUES(?,?,?,'started')", (identity, request_id, provider_id))
            await self._begin_route_provenance(db, identity, row["transfer_id"], request_id, provider_id, operation="resolve")
            await db.commit()
        return ResolutionAttempt(identity, request_id, provider_id, "started")

    @staticmethod
    def _resource_binding_id(transfer_id: int, resource_key: str) -> str:
        """Durable (transfer, canonical-resource) binding-generation identity.

        Derived only from the neutral canonical resource id (``ProviderResource.id``),
        never a provider-native field. Distinct transfers that bind the *same*
        canonical resource get distinct binding ids, so an identical native
        resource and identical file tree can never alias one transfer's
        manifest/selection generation into another's.
        """
        return uuid5(NAMESPACE_URL, f"transfer-provider-resource:{int(transfer_id)}:{resource_key}").hex

    @classmethod
    async def _resolve_binding(cls, db, transfer_id: int, resource_key: str) -> str | None:
        """The persisted binding-generation id for (transfer, canonical resource).

        Matches a new-model row by ``resource_key`` and a pre-split historical row
        (whose primary key *is* the canonical id) by ``id``.
        """
        row = await db.fetchone(
            "SELECT id FROM provider_resources WHERE transfer_id=? "
            "AND (resource_key=? OR (resource_key IS NULL AND id=?))",
            (transfer_id, resource_key, resource_key),
        )
        return row["id"] if row else None

    async def resource_binding_id(self, transfer_id: int, resource_key: str) -> str:
        """The binding-generation id core keys file-selection state on. Falls back
        to the computed id when the binding is not yet persisted."""
        async with get_db() as db:
            existing = await self._resolve_binding(db, transfer_id, resource_key)
        return existing or self._resource_binding_id(transfer_id, resource_key)

    @classmethod
    async def _resource(cls, db, transfer_id: int, resource: ProviderResource, state: ResourceState) -> str:
        """Persist/refresh the (transfer, canonical-resource) binding row.

        ``resource.id`` is the canonical, transfer-independent DP resource identity
        and is stored as ``resource_key`` and inside the payload unchanged. The
        row primary key is the binding-generation id. Returns that binding id.
        """
        resource_key = resource.id
        binding_id = await cls._resolve_binding(db, transfer_id, resource_key)
        if binding_id is None:
            # First binding for this (transfer, canonical resource). Two *live*
            # transfers may never share one native resource; a retired predecessor
            # (deleted / cancelled / consolidated) sharing it is the ordinary
            # delete/re-add generation case and is allowed to coexist.
            other = await db.fetchone(
                "SELECT r.transfer_id, t.status FROM provider_resources r JOIN torrents t ON t.id=r.transfer_id "
                "WHERE (r.resource_key=? OR (r.resource_key IS NULL AND r.id=?)) AND r.transfer_id != ?",
                (resource_key, resource_key, transfer_id),
            )
            if other and str(other["status"]) not in {"deleted", "cancelled", "consolidated"}:
                raise TransferError(NormalizedError(Domain.LIFECYCLE, Category.OWNERSHIP_CONFLICT, Stage.RESOLUTION))
            binding_id = cls._resource_binding_id(transfer_id, resource_key)
        existing = await db.fetchone(
            "SELECT transfer_id, provider_id, payload FROM provider_resources WHERE id=?", (binding_id,),
        )
        if existing and (existing["transfer_id"] != transfer_id or existing["provider_id"] != resource.provider_id):
            raise TransferError(NormalizedError(Domain.LIFECYCLE, Category.OWNERSHIP_CONFLICT, Stage.RESOLUTION))
        if existing:
            resource = replace(resource, ownership=codec.resource(codec.load(existing["payload"])).ownership)
        await db.execute(
            """INSERT INTO provider_resources(id,transfer_id,provider_id,payload,state,resource_key) VALUES(?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,state=excluded.state,
                   resource_key=COALESCE(provider_resources.resource_key, excluded.resource_key),
                   updated_at=CURRENT_TIMESTAMP""",
            (binding_id, transfer_id, resource.provider_id, codec.dump(resource), state, resource_key),
        )
        return binding_id

    async def resolution(self, attempt: ResolutionAttempt, result: ResolutionResult) -> bool:
        # Defense in depth: route identity is selected by the universal core.
        identities = [candidate.provider_id for candidate in result.candidates]
        identities.extend(candidate.resource.provider_id for candidate in result.candidates if candidate.resource)
        if result.observation:
            identities.append(result.observation.resource.provider_id)
        if any(identity and identity != attempt.provider_id for identity in identities):
            raise TransferError(NormalizedError(
                Domain.PROVIDER, Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION,
                integration_id=attempt.provider_id,
            ))
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("""SELECT r.transfer_id,t.status FROM transfer_requests r JOIN torrents t ON t.id=r.transfer_id
                WHERE r.id=?""", (attempt.request_id,))
            if not row:
                return False
            if result.observation:
                # Persist even after Delete so late-created remote resources can
                # be cleaned up without reviving the transfer.
                await self._resource(db, row["transfer_id"], result.observation.resource, result.observation.state)
            error = codec.dump(result.error) if result.error else None
            status = "failed" if result.error else "succeeded"
            await db.execute("UPDATE resolution_attempts SET state=?,error=?,result=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, error, codec.dump(result), attempt.id))
            await db.execute("""UPDATE route_attempt_provenance SET outcome=?,candidate_summary=?,updated_at=CURRENT_TIMESTAMP
                WHERE resolution_attempt_id=?""",
                ("failed" if result.error else "resolved", self._candidate_summary(result.candidates), attempt.id))
            resource = codec.dump(result.observation.resource) if result.observation else None
            request_state = "failed" if result.error else "waiting" if result.observation and not result.candidates else "materializing" if result.candidates else "resolved"
            if row["status"] not in {"deleted", "completed", "consolidated", "cancelled"}:
                await db.execute("UPDATE transfer_requests SET state=?,resource=COALESCE(?,resource),error=? WHERE id=?",
                                 (request_state, resource, error, attempt.request_id))
            await db.commit()
        return row["status"] not in {"deleted", "completed", "consolidated", "cancelled"}

    async def request_failure(self, request_id: str, error: NormalizedError, retry_at: float | None, *, retry_state="pending", consume_attempt=False) -> None:
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            error_blob = codec.dump(error)
            started = await db.fetchall("SELECT id FROM resolution_attempts WHERE request_id=? AND state='started'", (request_id,))
            for item in started:
                await db.execute("UPDATE resolution_attempts SET state='failed',error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (error_blob, item["id"]))
                await db.execute("UPDATE route_attempt_provenance SET outcome='failed',updated_at=CURRENT_TIMESTAMP WHERE resolution_attempt_id=?", (item["id"],))
            await db.execute("""UPDATE transfer_requests SET state=?,error=?,retry_at=?,attempts=attempts+? WHERE id=?
                AND transfer_id IN (SELECT id FROM torrents WHERE status NOT IN ('deleted','completed','consolidated','cancelled'))""",
                (retry_state if retry_at is not None else "failed", error_blob, retry_at or 0, int(consume_attempt), request_id))
            await db.commit()

    async def manifest(self, record: RequestRecord, entries: tuple[SourceEntry, ...], *, selection_id: str | None = None) -> None:
        """``selection_id``, when a file-selection generation authorized this
        fan-out (``TransferRepository.commit_selected_manifest
        .selection_id``), is durably stamped onto each child so materialization
        admission can trace it back without re-deriving the generation from
        the root's current (rebindable) resource -- DP 1.0.12 canonical
        architecture correction, Workstream A, specification section 7.2.

        A non-``None`` ``selection_id`` ALWAYS advances a child's stamp to
        the newly supplied generation, never merely preserves whatever it
        already held (a child materialized under a superseded generation A
        must be able to advance to the current generation B, or it would
        report STALE forever -- ``manifest()`` is called again on every
        successful root resolution pass at the SAME generation, so a
        COALESCE that only ever fills a NULL never lets a genuinely new
        generation replace an old one for a path both generations share).
        ``None`` never touches the existing stamp -- non-interactive
        resolution passes that carry no generation at all must not erase a
        previously stamped one.

        Advancing a child from one non-``None`` generation to a DIFFERENT
        one must never detach a still-live (non-terminal) execution's
        ``download_files`` pointer itself -- doing so here would orphan an
        authorized native writer without ever cancelling it (Gate 9
        revision-3 rejection finding 1). Real cancellation requires the
        executor, which this pure-repository method does not hold. Instead,
        when a live execution is found under the OLD generation, the stamp
        advance for that one child is SKIPPED entirely -- the child's stamp
        stays at its old (superseded) generation, which is exactly what
        makes ``materialization_authorization`` keep reporting STALE for it
        (it compares against the transfer's independently-tracked CURRENT
        generation via ``_current_generation``, never against the child's
        own stamp -- so leaving the stamp untouched here does not delay
        STALE detection at all). The existing canonical STALE-retirement
        machinery (``ConvergenceEngine._retire_stale_execution``, which owns
        the executor) then genuinely cancels and confirms the native writer
        on the next dispatch/recovery pass. Once that retirement has run,
        the child request is requeued and reprocessed through ordinary
        resolution, calling ``manifest()`` again for the SAME generation --
        at that point no live execution remains, so the stamp advances
        safely and this method itself now performs the (already-safe)
        detach-and-requeue below. A child whose old execution is already
        terminal (or absent) is retired and advanced immediately, since
        there is nothing left to orphan.
        """
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            parent = await db.fetchone("SELECT status FROM torrents WHERE id=?", (record.transfer_id,))
            if not parent or parent["status"] in {"deleted", "completed", "consolidated", "cancelled"}:
                return
            for ordinal, entry in enumerate(entries):
                identity = uuid5(NAMESPACE_URL, f"request:{record.id}:{entry.relative_path}").hex
                await db.execute("""INSERT OR IGNORE INTO transfer_requests(id,transfer_id,parent_id,ordinal,payload,metadata,materialized_selection_id)
                    VALUES(?,?,?,?,?,?,?)""",
                    (identity, record.transfer_id, record.id, ordinal, codec.dump(entry.request), codec.dump(entry), selection_id))
                advance_selection_id = selection_id
                if selection_id is not None:
                    existing = await db.fetchone(
                        "SELECT materialized_selection_id FROM transfer_requests WHERE id=?", (identity,),
                    )
                    previous_generation = existing["materialized_selection_id"] if existing else None
                    if previous_generation and str(previous_generation) != str(selection_id):
                        live_execution = await db.fetchone(
                            """SELECT f.id FROM download_files f JOIN execution_attempts e ON e.id=f.execution_attempt_id
                                WHERE f.request_id=? AND e.state IN ('prepared','queued','transferring','paused','unknown')""",
                            (identity,),
                        )
                        if live_execution:
                            advance_selection_id = None
                        else:
                            artifact_row = await db.fetchone(
                                "SELECT id, execution_attempt_id FROM download_files WHERE request_id=?", (identity,),
                            )
                            if artifact_row:
                                await db.execute("""UPDATE download_files SET status='unresolved',
                                    execution_attempt_id=NULL,normalized_error=NULL,retry_at=0,
                                    continuation_reservation_expires_at=NULL,updated_at=CURRENT_TIMESTAMP
                                    WHERE id=? AND status!='completed'""", (artifact_row["id"],))
                                if artifact_row["execution_attempt_id"]:
                                    await db.execute("""UPDATE execution_attempts SET authorized=0,updated_at=CURRENT_TIMESTAMP
                                        WHERE id=? AND state IN ('failed','absent','cancelled','succeeded')""",
                                        (artifact_row["execution_attempt_id"],))
                            await db.execute("""UPDATE transfer_requests SET state='pending',retry_at=0,error=NULL
                                WHERE id=?""", (identity,))
                await db.execute("""UPDATE transfer_requests SET payload=?,metadata=?,state=CASE WHEN state='waiting_parent' THEN 'pending' ELSE state END,
                    materialized_selection_id=COALESCE(?,materialized_selection_id)
                    WHERE id=?""", (codec.dump(entry.request), codec.dump(entry), advance_selection_id, identity))
            missing_error = NormalizedError(Domain.RESOLUTION, Category.SOURCE_NOT_FOUND, Stage.RESOLUTION)
            missing = await db.fetchall("SELECT id FROM transfer_requests WHERE parent_id=? AND state='waiting_parent'", (record.id,))
            for child in missing:
                await db.execute("UPDATE transfer_requests SET state='failed',error=? WHERE id=?", (codec.dump(missing_error), child["id"]))
                await db.execute("UPDATE download_files SET status='error',normalized_error=? WHERE request_id=? AND status!='completed'", (codec.dump(missing_error), child["id"]))
            await db.execute("UPDATE transfer_requests SET state='resolved',error=NULL WHERE id=?", (record.id,))
            await db.commit()

    async def resource_observation(self, transfer_id: int, resource: ProviderResource, state: ResourceState):
        async with get_db() as db:
            await self._resource(db, transfer_id, resource, state)
            await db.commit()

    async def materialize(self, record: RequestRecord, candidates: tuple[TransferCandidate, ...], target: str) -> Artifact | None:
        if not candidates:
            return None
        chosen = candidates[0]
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            parent = await db.fetchone("SELECT status FROM torrents WHERE id=?", (record.transfer_id,))
            if not parent or parent["status"] in {"deleted", "completed", "consolidated", "cancelled"}:
                return None
            previous = await db.fetchone("SELECT id FROM download_files WHERE request_id=?", (record.id,))
            if previous:
                await db.execute("""UPDATE download_files SET candidates=?,selected_candidate=0,
                    size_bytes=?,normalized_error=NULL,
                    status=CASE WHEN status='unresolved' AND execution_attempt_id IS NULL THEN 'queued' ELSE status END
                    WHERE id=?""", (codec.dump(candidates), chosen.expected_bytes, previous["id"]))
            else:
                await db.execute("""INSERT INTO download_files(torrent_id,request_id,filename,size_bytes,local_path,status,candidates,download_client)
                    VALUES(?,?,?,?,?,'queued',?,'')""",
                    (record.transfer_id, record.id, chosen.name, chosen.expected_bytes, target, codec.dump(candidates)))
            await db.execute("UPDATE transfer_requests SET state='resolved',error=NULL WHERE id=?", (record.id,))
            await db.commit()
        return next(item for item in await self.artifacts(record.transfer_id) if item.request_id == record.id)

    async def artifacts(self, transfer_id: int) -> tuple[Artifact, ...]:
        async with get_db() as db:
            rows = await db.fetchall(f"""SELECT f.*,e.handle FROM download_files f
                LEFT JOIN execution_attempts e ON e.id=f.execution_attempt_id
                WHERE f.torrent_id=? AND {canonical_artifact_membership_sql('f')} ORDER BY f.id""", (transfer_id,))
        return tuple(Artifact(row["id"], transfer_id, row["request_id"], row["filename"], row["local_path"], row["size_bytes"] or 0,
                              row["status"], tuple(codec.candidate(item) for item in codec.load(row["candidates"], [])),
                              row["selected_candidate"], codec.handle(codec.load(row["handle"])), row["retry_count"] or 0,
                              row["retry_at"], codec.error(row["normalized_error"])) for row in rows)

    async def occupied_paths(self) -> set[str]:
        async with get_db() as db:
            rows = await db.fetchall("""SELECT f.local_path FROM download_files f JOIN torrents t ON t.id=f.torrent_id
                WHERE f.local_path IS NOT NULL AND COALESCE(f.mirror_state,'')!='standby'
                AND (t.status NOT IN ('deleted','completed','consolidated','error')
                    OR EXISTS (SELECT 1 FROM execution_attempts e WHERE e.id=f.execution_attempt_id AND e.authorized=1
                        AND e.state IN ('prepared','queued','transferring','paused','unknown')))""")
        return {str(row["local_path"]).casefold() for row in rows}

    async def prepare_execution(self, artifact: Artifact, handle: ExecutionHandle, *, from_input_required: bool = False) -> bool:
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("""SELECT f.* FROM download_files f JOIN torrents t ON t.id=f.torrent_id
                LEFT JOIN transfer_pause_intents p ON p.torrent_id=t.id
                WHERE f.id=? AND f.status=? AND f.execution_attempt_id IS NULL
                AND t.status NOT IN ('deleted','completed','consolidated','cancelled') AND COALESCE(p.paused,0)=0""",
                (artifact.id, "input_required" if from_input_required else "queued"))
            if not row:
                return False
            candidate = artifact.candidates[artifact.selected] if artifact.candidates else None
            route_attempt_id = await self._candidate_route(
                db, artifact.transfer_id, candidate, artifact_id=artifact.id
            )
            ordinal_row = await db.fetchone("SELECT COALESCE(MAX(ordinal),0) AS n FROM execution_attempt_provenance WHERE artifact_id=?", (artifact.id,))
            ordinal = int(ordinal_row["n"] or 0) + 1
            await db.execute("""INSERT INTO execution_attempts(id,transfer_id,artifact_id,executor_id,handle,state,candidate)
                VALUES(?,?,?,?,?,'prepared',?)""", (handle.attempt_id, artifact.transfer_id, artifact.id, handle.executor_id, codec.dump(handle),
                codec.dump(candidate) if candidate else None))
            await db.execute("""INSERT INTO execution_attempt_provenance(
                execution_attempt_id,route_attempt_id,transfer_id,artifact_id,ordinal,provider_id,candidate_id,candidate_source,
                outcome,delivered,history_quality) VALUES(?,?,?,?,?,?,?,?, 'prepared',0,'recorded')""",
                (handle.attempt_id, route_attempt_id, artifact.transfer_id, artifact.id, ordinal,
                 candidate.provider_id if candidate and candidate.provider_id else None, str(candidate.id) if candidate else None,
                 codec.dump(self._safe_candidate_source(candidate)) if candidate else None))
            await db.execute("""UPDATE download_files SET execution_attempt_id=?,download_client=?,retry_count=retry_count+1,
                status=CASE WHEN ? THEN 'queued' ELSE status END,normalized_error=NULL,
                continuation_reservation_expires_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (handle.attempt_id, handle.executor_id, int(from_input_required), artifact.id))
            # DP 1.0.12 recovery leveling, Section 29: durably link this new
            # execution back to the candidate-activation record that selected
            # it, if any -- a committed activation cannot know the replacement
            # execution's id at commit time (it doesn't exist yet), so the
            # audit link is completed here instead, the first time this
            # artifact actually dispatches afterward. Scans backward for the
            # most recent still-unlinked "activated" record for this artifact;
            # bounded, since only a just-activated, not-yet-dispatched
            # artifact ever has one pending.
            activation_rows = await db.fetchall(
                "SELECT id,detail FROM application_events WHERE transfer_id=? AND kind='candidate_activation' ORDER BY id DESC LIMIT 50",
                (artifact.transfer_id,),
            )
            for activation_row in activation_rows:
                detail = codec.load(activation_row["detail"], {})
                if (detail.get("artifact_id") == artifact.id and detail.get("outcome") == "activated"
                        and detail.get("new_execution_id") is None):
                    detail["new_execution_id"] = handle.attempt_id
                    await db.execute(
                        "UPDATE application_events SET detail=? WHERE id=?",
                        (codec.dump(detail), activation_row["id"]),
                    )
                    break
            await db.commit()
        return True

    async def authorize_execution(self, handle: ExecutionHandle, action: str) -> bool:
        async with get_db() as db:
            row = await db.fetchone("""SELECT e.*,t.status AS transfer_status,COALESCE(p.paused,0) AS paused_intent,
                f.execution_attempt_id AS current_execution_id
                FROM execution_attempts e JOIN torrents t ON t.id=e.transfer_id
                JOIN download_files f ON f.id=e.artifact_id
                LEFT JOIN transfer_pause_intents p ON p.torrent_id=t.id WHERE e.id=?""", (handle.attempt_id,))
        if not row or not row["authorized"] or row["executor_id"] != handle.executor_id or codec.load(row["handle"]) != codec.load(codec.dump(handle)):
            return False
        is_current = row.get("current_execution_id") == handle.attempt_id
        if action in {"start", "resume", "pause"} and not is_current:
            return False
        if action == "cancel" and not is_current:
            cleanup_owned = row["transfer_status"] in {"deleted", "cancelled"} and row.get("cleanup_state") in {"pending", "blocked"}
            if not cleanup_owned:
                return False
        if action in {"start", "resume"} and (row["transfer_status"] in {"deleted", "completed", "consolidated", "cancelled"} or row["paused_intent"]):
            return False
        if action in {"start", "resume"} and await self.globally_paused():
            return False
        return action != "start" or row["state"] == "prepared"

    async def artifact_state(self, artifact_id: int, state: str, *, error=None, retry_at=0, release=False, selected=None, expected_bytes=None):
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            current = await db.fetchone("SELECT execution_attempt_id FROM download_files WHERE id=?", (artifact_id,))
            # Section 13: artifact_state() is never used to hold a continuation
            # reservation across a writer-replacement handoff -- only
            # transition_recovery()'s explicit continuation_reservation_until
            # sets one -- so every artifact_state() write releases it
            # unconditionally (cancellation, deletion, terminal failure, and
            # plain completion all funnel through here).
            cursor = await db.execute("""UPDATE download_files SET status=?,normalized_error=?,retry_at=?,
                execution_attempt_id=CASE WHEN ? THEN NULL ELSE execution_attempt_id END,
                selected_candidate=COALESCE(?,selected_candidate),size_bytes=COALESCE(?,size_bytes),
                continuation_reservation_expires_at=NULL,updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND torrent_id IN (SELECT id FROM torrents
                    WHERE status NOT IN ('deleted','consolidated') AND (status!='cancelled' OR ?='cancelled'))""",
                (state, codec.dump(error) if error else None, retry_at, release, selected, expected_bytes, artifact_id, state))
            if cursor.rowcount and release and current and current.get("execution_attempt_id"):
                await db.execute("""UPDATE execution_attempts SET authorized=0,updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND state IN ('failed','absent','cancelled','succeeded')""", (current["execution_attempt_id"],))
            if cursor.rowcount and state == "completed" and current and current.get("execution_attempt_id"):
                execution_id = current["execution_attempt_id"]
                await db.execute("""UPDATE execution_attempt_provenance SET delivered=1,outcome='completed',updated_at=CURRENT_TIMESTAMP
                    WHERE execution_attempt_id=?""", (execution_id,))
                route = await db.fetchone("SELECT route_attempt_id FROM execution_attempt_provenance WHERE execution_attempt_id=?", (execution_id,))
                if route and route.get("route_attempt_id"):
                    await db.execute("UPDATE route_attempt_provenance SET outcome='completed',updated_at=CURRENT_TIMESTAMP WHERE resolution_attempt_id=?",
                                     (route["route_attempt_id"],))
            await db.commit()

    async def executions(self, transfer_id: int | None = None) -> tuple[ExecutionAttempt, ...]:
        async with get_db() as db:
            rows = await db.fetchall("SELECT * FROM execution_attempts" + (" WHERE transfer_id=?" if transfer_id is not None else ""),
                                     (transfer_id,) if transfer_id is not None else ())
        return tuple(self._execution_attempt(row) for row in rows)

    @staticmethod
    def _execution_attempt(row):
        return ExecutionAttempt(codec.handle(codec.load(row["handle"])), row["transfer_id"], row["artifact_id"], row["state"],
                                TransferProgress(**codec.load(row["progress"], {})), codec.error(row["error"]),
                                codec.candidate(codec.load(row["candidate"])) if row.get("candidate") else None)

    async def live_executions(self):
        async with get_db() as db:
            rows = await db.fetchall("""SELECT e.* FROM execution_attempts e
                JOIN download_files f ON f.execution_attempt_id=e.id JOIN torrents t ON t.id=e.transfer_id
                WHERE t.status NOT IN ('deleted','completed','consolidated','cancelled') AND e.authorized=1""")
        return tuple(self._execution_attempt(row) for row in rows)

    _OCCUPYING_EXECUTION_STATES = ("prepared", "queued", "transferring", "unknown")

    async def occupied_execution_slots(self, now: float, *, exclude_artifact_id: int | None = None) -> int:
        """DP 1.0.12 recovery leveling, Section 13: the ONE canonical execution-
        admission occupancy count -- genuinely live authorized writers plus any
        durable, unexpired continuation reservation (an artifact whose old
        writer was already retired but whose replacement has not yet
        dispatched). Every capacity gate in the engine must call this instead
        of counting live executions alone, or a reserved-but-not-yet-live slot
        could be stolen by unrelated queued work during the short
        writer-replacement handoff. ``exclude_artifact_id`` lets an artifact
        about to consume its own reservation check admission without
        self-blocking on it.
        """
        placeholders = ",".join("?" for _ in self._OCCUPYING_EXECUTION_STATES)
        async with get_db() as db:
            live_row = await db.fetchone(
                f"""SELECT COUNT(*) AS n FROM execution_attempts e
                    JOIN download_files f ON f.execution_attempt_id=e.id JOIN torrents t ON t.id=e.transfer_id
                    WHERE t.status NOT IN ('deleted','completed','consolidated','cancelled') AND e.authorized=1
                    AND e.state IN ({placeholders}) AND (? IS NULL OR f.id!=?)""",
                (*self._OCCUPYING_EXECUTION_STATES, exclude_artifact_id, exclude_artifact_id),
            )
            reserved_row = await db.fetchone(
                """SELECT COUNT(*) AS n FROM download_files f JOIN torrents t ON t.id=f.torrent_id
                    WHERE f.execution_attempt_id IS NULL AND f.continuation_reservation_expires_at IS NOT NULL
                    AND f.continuation_reservation_expires_at>? AND t.status NOT IN ('deleted','completed','consolidated','cancelled')
                    AND (? IS NULL OR f.id!=?)""",
                (now, exclude_artifact_id, exclude_artifact_id),
            )
        return int((live_row or {}).get("n") or 0) + int((reserved_row or {}).get("n") or 0)

    async def continuation_reservation(self, artifact_id: int) -> float | None:
        """Current raw reservation expiry for one artifact, or None. Test/
        provenance introspection only -- admission gates use
        ``occupied_execution_slots`` instead."""
        async with get_db() as db:
            row = await db.fetchone(
                "SELECT continuation_reservation_expires_at FROM download_files WHERE id=?", (artifact_id,),
            )
        return float(row["continuation_reservation_expires_at"]) if row and row.get("continuation_reservation_expires_at") is not None else None

    async def resources(self, transfer_id: int):
        async with get_db() as db:
            rows = await db.fetchall("SELECT * FROM provider_resources WHERE transfer_id=?", (transfer_id,))
        return tuple((codec.resource(codec.load(row["payload"])), ResourceState(row["state"]), row["cleanup_authority"]) for row in rows)

    async def cleanup_intent(self, transfer_id: int, resource_key: str, authority: str | None, *, error=None):
        """Set/clear cleanup responsibility for the (transfer, canonical resource)
        binding. A fresh non-null intent also clears any prior terminal-abandon
        marker so the fence and the cleanup cadence treat it as live again.
        Clearing responsibility (``authority is None``) also withdraws any claim
        lease: with nothing left to clean, no owner token can still be current."""
        async with get_db() as db:
            await db.execute(
                "UPDATE provider_resources SET cleanup_authority=?, cleanup_error=?, "
                "cleanup_abandoned=CASE WHEN ? IS NOT NULL THEN 0 ELSE cleanup_abandoned END, "
                "cleanup_claim_token=CASE WHEN ? IS NULL THEN NULL ELSE cleanup_claim_token END, "
                "cleanup_claim_until=CASE WHEN ? IS NULL THEN 0 ELSE cleanup_claim_until END, "
                "updated_at=CURRENT_TIMESTAMP "
                "WHERE transfer_id=? AND (resource_key=? OR (resource_key IS NULL AND id=?))",
                (authority, codec.dump(error) if error else None, authority, authority, authority,
                 transfer_id, resource_key, resource_key),
            )
            await db.commit()

    # -------------------------------------------------------------------------
    # Provider-cleanup claim: the ONE lease/token owner.
    #
    # A claim is (``cleanup_claim_token``, ``cleanup_claim_until``). It is current
    # while the token is set and the absolute engine-clock expiry has not passed;
    # nothing else (no boolean, no generic timestamp) means "claimed". Every
    # outcome converges: COMPLETED (authority cleared), RETRYABLE at
    # ``cleanup_retry_at`` (claim released), PERMANENTLY ABANDONED (claim
    # released), or LEASED until the bounded expiry. The lease tracks LIVE
    # ownership: an owner whose provider call is still running renews it with its
    # own token (``renew_cleanup_claim``), so a live owner is never reclaimed. An
    # owner that is cancelled, crashes, or fails between acquisition and
    # finalization stops renewing and therefore cannot strand the row: once the
    # lease expires the same ordinary cadence (``pending_cleanup`` ->
    # ``claim_cleanup``) takes it over atomically, and the loser's late
    # finalization is rejected because its token is no longer current.
    # -------------------------------------------------------------------------

    async def pending_cleanup(self, now):
        """Rows the cleanup cadence may claim now: cleanup owed, not abandoned,
        retry time reached, and no *current* claim (unclaimed, or claim expired)."""
        async with get_db() as db:
            rows = await db.fetchall(
                "SELECT * FROM provider_resources WHERE cleanup_authority IS NOT NULL "
                "AND COALESCE(cleanup_abandoned, 0) = 0 AND cleanup_retry_at<=? "
                "AND (cleanup_claim_token IS NULL OR cleanup_claim_until<=?)",
                (now, now),
            )
        return tuple(
            (row["transfer_id"], codec.resource(codec.load(row["payload"])),
             row["cleanup_authority"], row["cleanup_attempts"], row["id"])
            for row in rows
        )

    async def claim_cleanup(self, binding_id: str, *, now: float, lease_until: float) -> str | None:
        """Atomically acquire the cleanup claim; returns the new owner token, or
        ``None`` when another current claim owns it (or nothing is owed). The
        conditional UPDATE is the sole arbiter, so two workers can never both
        hold a current claim; an expired claim is taken over here."""
        token = new_identity()
        async with get_db() as db:
            result = await db.execute(
                "UPDATE provider_resources SET cleanup_attempts=cleanup_attempts+1, "
                "cleanup_claim_token=?, cleanup_claim_until=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND cleanup_authority IS NOT NULL AND COALESCE(cleanup_abandoned, 0) = 0 "
                "AND cleanup_retry_at<=? AND (cleanup_claim_token IS NULL OR cleanup_claim_until<=?)",
                (token, float(lease_until), binding_id, now, now),
            )
            await db.commit()
        return token if result.rowcount == 1 else None

    async def renew_cleanup_claim(self, binding_id: str, token: str, *, now: float, lease_until: float) -> bool:
        """Owner heartbeat: extend the lease of the claim ``token`` currently holds.

        The lease means LIVE ownership, not merely elapsed time: while its
        ``provider.cleanup()`` call runs, the owner renews with the SAME token, so
        no other worker can reclaim (and start a second simultaneous remote
        cleanup on) the same native resource. Conditional on the current token, so
        an owner that has already lost the claim gets ``False`` -- and must stand
        down -- rather than resurrect it. The expiry only ever moves forward. A
        dead owner stops renewing, its lease runs out, and the ordinary cadence
        reclaims it (:meth:`claim_cleanup`)."""
        async with get_db() as db:
            result = await db.execute(
                "UPDATE provider_resources SET cleanup_claim_until=MAX(cleanup_claim_until, ?), "
                "updated_at=CURRENT_TIMESTAMP WHERE id=? AND cleanup_claim_token=? "
                "AND cleanup_authority IS NOT NULL AND COALESCE(cleanup_abandoned, 0) = 0",
                (float(lease_until), binding_id, token),
            )
            await db.commit()
        return result.rowcount == 1

    async def cleanup_complete(self, binding_id: str, token: str, *, absent: ProviderResource | None = None) -> bool:
        """Token-conditional completion (SUCCESS / SKIPPED): clear the cleanup
        responsibility and the claim in one transaction; a SUCCESS also records the
        binding ABSENT. Returns ``False`` (and changes nothing) when ``token`` is no
        longer the current claim -- a stale worker never overwrites a newer owner."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                "SELECT transfer_id FROM provider_resources WHERE id=? AND cleanup_claim_token=?",
                (binding_id, token),
            )
            if not row:
                await db.rollback()
                return False
            await db.execute(
                "UPDATE provider_resources SET cleanup_authority=NULL, cleanup_error=NULL, cleanup_retry_at=0, "
                "cleanup_claim_token=NULL, cleanup_claim_until=0, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (binding_id,),
            )
            if absent is not None:
                await self._resource(db, row["transfer_id"], absent, ResourceState.ABSENT)
            await db.commit()
        return True

    async def cleanup_retry(self, binding_id: str, token: str, error, retry_at) -> bool:
        """Token-conditional record of a failed provider cleanup call, releasing
        the claim. ``retry_at is None`` means policy has permanently given up: mark
        ``cleanup_abandoned`` so the fence releases and the cadence stops
        re-driving it. Returns ``False`` (and changes nothing) for a stale token."""
        terminal = retry_at is None
        async with get_db() as db:
            result = await db.execute(
                "UPDATE provider_resources SET cleanup_error=?, cleanup_retry_at=?, cleanup_abandoned=?, "
                "cleanup_claim_token=NULL, cleanup_claim_until=0, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND cleanup_claim_token=?",
                (codec.dump(error) if error else None, retry_at or 0, int(terminal), binding_id, token),
            )
            await db.commit()
        return result.rowcount == 1

    async def outcome(self, transfer_id: int, outcome, *, attempt_id=None):
        async with get_db() as db:
            await db.execute("INSERT INTO transfer_outcomes(transfer_id,attempt_id,kind,payload) VALUES(?,?,?,?)",
                             (transfer_id, attempt_id, outcome.kind, codec.dump(outcome)))
            message = outcome.error.message if outcome.error else str(outcome.kind)
            await db.execute("INSERT INTO events(torrent_id,level,message) VALUES(?,?,?)",
                             (transfer_id, "error" if outcome.error else "info", message))
            await db.commit()

    async def retry_requests(self, transfer_id: int, *, request_id=None, reset_budget=False):
        async with get_db() as db:
            await db.execute("UPDATE transfer_requests SET state='pending',retry_at=0,error=NULL,attempts=CASE WHEN ? THEN 0 ELSE attempts END WHERE transfer_id=? AND transfer_id IN (SELECT id FROM torrents WHERE status NOT IN ('completed','consolidated','deleted','cancelled')) AND " +
                             ("id=?" if request_id else "state='failed'"), (reset_budget, transfer_id, request_id) if request_id else (reset_budget, transfer_id))
            await db.commit()

    async def renew_parent(self, record, retry_at, *, reset_budget=False):
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute("""UPDATE transfer_requests SET state='pending',retry_at=?,error=NULL,attempts=CASE WHEN ? THEN 0 ELSE attempts END
                WHERE id=? AND transfer_id IN (SELECT id FROM torrents WHERE status NOT IN ('completed','consolidated','deleted','cancelled'))""",
                (retry_at, reset_budget, record.id))
            await db.execute("""UPDATE transfer_requests SET state='waiting_parent',error=NULL WHERE parent_id=? AND id IN
                (SELECT request_id FROM download_files WHERE status IN ('error','unresolved','lost','cancelled'))
                AND transfer_id IN (SELECT id FROM torrents WHERE status NOT IN ('completed','consolidated','deleted','cancelled'))""", (record.id,))
            await db.commit()

    async def recovery_budget(self, artifact_id: int) -> tuple[int, int]:
        async with get_db() as db:
            row = await db.fetchone("SELECT recovery_failures,recovery_refreshes FROM download_files WHERE id=?", (artifact_id,))
        if not row:
            raise KeyError(artifact_id)
        return int(row["recovery_failures"] or 0), int(row["recovery_refreshes"] or 0)

    async def reset_postprocessing(self, transfer_id):
        async with get_db() as db:
            if await db.fetchone("SELECT transfer_id FROM postprocess_attempts WHERE transfer_id=? AND state IN ('pending','processing')", (transfer_id,)):
                return False
            await db.execute("DELETE FROM postprocess_attempts WHERE transfer_id=?", (transfer_id,))
            await db.commit()
        return True

    async def globally_paused(self) -> bool:
        async with get_db() as db:
            row = await db.fetchone("SELECT value FROM transfer_controls WHERE key='paused'")
        return bool(row and row["value"] == "1")

    async def global_pause(self, paused: bool):
        async with get_db() as db:
            await db.execute("""INSERT INTO transfer_controls(key,value) VALUES('paused',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""", ("1" if paused else "0",))
            await db.commit()

    async def delete(self, transfer_id: int, *, remote: bool, now: float = 0) -> None:
        """Atomically tombstone a transfer and retain responsibility for launched executions.

        Delete also permanently retires the transfer's active dedupe identity: the
        original logical fingerprint is preserved in ``source_fingerprint`` and the
        unique ``hash`` key is replaced with a deterministic transfer-specific
        tombstone. The historical row, its provider/resource/execution provenance,
        outstanding cleanup responsibility, and any file-selection generations all
        remain scoped to this transfer; re-submitting the same source afterwards
        creates a genuinely fresh transfer (see ``admit``).
        """
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("SELECT id,hash,source_fingerprint FROM torrents WHERE id=?", (transfer_id,))
            if row:
                current_hash = str(row["hash"] or "")
                original = str(row["source_fingerprint"] or current_hash)
                tombstone = current_hash if current_hash.startswith("deleted:") else self._tombstone_hash(transfer_id, original)
                await db.execute(
                    """UPDATE torrents SET status='deleted',delete_remote=?,lifecycle_epoch=lifecycle_epoch+1,
                        source_fingerprint=COALESCE(source_fingerprint,?), hash=?,
                        updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (int(remote), original, tombstone, transfer_id),
                )
            else:
                await db.execute("""UPDATE torrents SET status='deleted',delete_remote=?,lifecycle_epoch=lifecycle_epoch+1,
                    updated_at=CURRENT_TIMESTAMP WHERE id=?""", (int(remote), transfer_id))
            # FUNC-001: this path settles the parent into DELETED without
            # going through _write_lifecycle_transition, so it must invoke the
            # same transaction-local auxiliary-state retirement directly.
            await _retire_transfer_auxiliary_state_in_db(db, transfer_id)
            await db.execute(
                """UPDATE execution_attempts SET cleanup_state='pending',
                    cleanup_attempts=CASE WHEN cleanup_state IN ('pending','blocked') THEN cleanup_attempts ELSE 0 END,
                    cleanup_retry_at=CASE
                        WHEN cleanup_state IN ('pending','blocked') AND cleanup_retry_at>? THEN cleanup_retry_at
                        ELSE ? END,
                    cleanup_error=CASE WHEN cleanup_state IN ('pending','blocked') THEN cleanup_error ELSE NULL END,
                    updated_at=CURRENT_TIMESTAMP
                    WHERE transfer_id=? AND authorized=1
                    AND state IN ('prepared','queued','transferring','paused','unknown')""",
                (now, now, transfer_id),
            )
            # DP 1.0.12 recovery leveling, Section 13: DELETED is not a true
            # dead end -- transfers.policy.transition_allowed permits an
            # operator to resurrect a deleted transfer back to ACCEPTED. Relying
            # solely on occupied_execution_slots()'s torrents.status join filter
            # would let a reservation that predates the delete silently
            # reappear and consume capacity the moment that resurrection
            # happens, well before its TTL would have expired on its own.
            # Durably clear it here instead of only excluding it structurally.
            await db.execute(
                "UPDATE download_files SET continuation_reservation_expires_at=NULL WHERE torrent_id=?",
                (transfer_id,),
            )
            await db.commit()

    async def delete_remote_requested(self, transfer_id: int) -> bool:
        async with get_db() as db:
            row = await db.fetchone("SELECT delete_remote FROM torrents WHERE id=?", (transfer_id,))
        return bool(row and row["delete_remote"])

    async def rename(self, transfer_id: int, name: str):
        async with get_db() as db:
            await db.execute("UPDATE torrents SET name=? WHERE id=? AND status!='deleted'", (name, transfer_id))
            await db.commit()

    async def adopt_inventory_resource(self, transfer_id: int, resource: ProviderResource, state: ResourceState) -> bool:
        """The ONE inventory-adoption transition: bind an observed provider
        resource to the root request of a transfer that INVENTORY ITSELF created
        (``torrents.source = 'inventory'``), in one transaction.

        Adoption is a binding that happens outside a resolution attempt, so it
        deliberately records no ``resolution_attempts`` / ``route_attempt_provenance``
        row (fabricating an attempt for something that never resolved would be
        untruthful). Its provenance is instead the durable, queryable pair
        ``transfers.source = 'inventory'`` + binding ``ownership = observed``.

        Only such an inventory-created import may be bound this way. A transfer
        that a user submitted owns its own resolution lifecycle (attempt, route
        provenance, cleanup fence, file-selection generation); silently rebinding
        it to whatever the provider happens to list is an unowned transition and is
        refused here, leaving that transfer to resolve through its own route. The
        predecessor-cleanup fence is honoured too: an adoption is a same-object
        provider-resource reuse, so it must wait exactly as a first resolution
        does. Returns whether the resource was adopted.
        """
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            root = await db.fetchone(
                """SELECT r.id FROM transfer_requests r JOIN torrents t ON t.id=r.transfer_id
                   WHERE r.transfer_id=? AND r.parent_id IS NULL AND r.state IN ('pending','resolving','failed')
                   AND t.source='inventory' AND t.status NOT IN ('completed','consolidated','deleted')""",
                (transfer_id,),
            )
            if not root or await self._predecessor_cleanup_blocks(db, transfer_id):
                await db.rollback()
                return False
            await self._resource(db, transfer_id, resource, state)
            await db.execute(
                "UPDATE transfer_requests SET state='waiting',resource=?,error=NULL WHERE id=?",
                (codec.dump(resource), root["id"]),
            )
            await db.commit()
        return True

    async def begin_refresh(self, record: RequestRecord, provider_id: str):
        identity = new_identity()
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            # Refresh is deliberately allowed for a request owned by a fully
            # consolidated source transfer: that request still owns the foreign
            # candidate's acquisition provenance even though its submission has
            # no independent scheduling authority.
            await db.execute("INSERT INTO resolution_attempts(id,request_id,provider_id,state) VALUES(?,?,?,'started')",
                             (identity, record.id, provider_id))
            await self._begin_route_provenance(db, identity, record.transfer_id, record.id, provider_id, operation="refresh")
            await db.commit()
        return ResolutionAttempt(identity, record.id, provider_id, "started")

    async def resolved_candidates(self, request_id: str):
        async with get_db() as db:
            row = await db.fetchone("""SELECT a.result FROM resolution_attempts a
                LEFT JOIN route_attempt_provenance p ON p.resolution_attempt_id=a.id
                WHERE a.request_id=? AND a.state='succeeded'
                ORDER BY COALESCE(p.ordinal,0) DESC,a.updated_at DESC,a.id DESC LIMIT 1""", (request_id,))
        result = codec.load(row["result"], {}) if row else {}
        return tuple(codec.candidate(value) for value in result.get("candidates", []))

    async def poll_after(self, request_id: str, timestamp: float, *, waiting=False):
        # Generic re-poll scheduling for the provider cadence (PREPARING re-poll,
        # cleanup-barrier hold, adopted-resource observation). It never touches
        # ``error``: a request left ``state='waiting' AND error IS NULL`` is by
        # construction a benign poll cadence, which is exactly the distinction
        # confirm_file_selection / dismiss_file_selection / the atomic
        # file-selection gate rely on (see the retry_at multi-purpose note in
        # transfers.repository). The interactive file-selection wait is now
        # scheduled inside the atomic gate transaction, not here.
        async with get_db() as db:
            await db.execute(
                "UPDATE transfer_requests SET retry_at=?,"
                "state=CASE WHEN ? THEN 'waiting' ELSE state END WHERE id=?",
                (timestamp, waiting, request_id),
            )
            await db.commit()

    async def add_alternate(self, primary: Artifact, record: RequestRecord, candidates, size: int):
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            current = await db.fetchone("""SELECT f.* FROM download_files f JOIN torrents t ON t.id=f.torrent_id
                WHERE f.id=? AND t.status NOT IN ('deleted','consolidated')""", (primary.id,))
            if not current:
                return False
            retained = [replace(codec.candidate(item), expected_bytes=size) for item in codec.load(current["candidates"], [])]
            alternatives = tuple(replace(item, expected_bytes=size) for item in candidates)
            retained.extend(alternatives)
            await db.execute("UPDATE download_files SET candidates=?,size_bytes=?,mirror_group_id=?,mirror_state='primary' WHERE id=?",
                             (codec.dump(retained), size, primary.id, primary.id))
            await db.execute("""INSERT INTO download_files(torrent_id,request_id,filename,size_bytes,local_path,status,blocked,
                mirror_group_id,mirror_state,candidates,download_client) VALUES(?,?,?,?,?,'duplicate',NULL,?,'standby',?,'')""",
                (record.transfer_id, record.id, alternatives[0].name, size, primary.target, primary.id, codec.dump(alternatives)))
            await db.execute("UPDATE transfer_requests SET state='resolved',error=NULL WHERE id=?", (record.id,))
            await db.commit()
        return True

    async def select_artifact(self, transfer_id: int, artifact_id: int, selected: bool):
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("SELECT * FROM download_files WHERE id=? AND torrent_id=?", (artifact_id, transfer_id))
            if not row or not row["request_id"]:
                raise KeyError(artifact_id)
            transfer = await db.fetchone("SELECT status FROM torrents WHERE id=?", (transfer_id,))
            if not transfer or transfer["status"] == "consolidated":
                raise TransferError(NormalizedError(Domain.LIFECYCLE, Category.RESOURCE_STATE_CONFLICT, Stage.QUEUE))
            if bool(row["blocked"]) == (not selected):
                return
            if row["execution_attempt_id"] or row["status"] not in {"queued", "unresolved", "paused", "blocked", "pending"}:
                raise TransferError(NormalizedError(Domain.LIFECYCLE, Category.RESOURCE_STATE_CONFLICT, Stage.QUEUE))
            ready = bool(codec.load(row["candidates"], []))
            await db.execute("UPDATE download_files SET blocked=?,status=? WHERE id=?",
                             (int(not selected), ("queued" if ready else "unresolved") if selected else "blocked", artifact_id))
            await db.execute("UPDATE transfer_requests SET state=? WHERE id=?",
                             (("resolved" if ready else "pending") if selected else "skipped", row["request_id"]))
            await db.commit()
