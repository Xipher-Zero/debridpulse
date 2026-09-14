"""Operational Downloads and Activity Log read-model routes.

The durable transfer row for a fully absorbed source remains queryable by its
explicit CONSOLIDATED lifecycle state, but the normal operational list excludes
it alongside soft-deleted history. Pagination and totals therefore reflect the
same canonical lifecycle rule as the visible rows.

Activity Log filtering lives here so optional search, severity, and timeframe
predicates are applied before the result ceiling. This module is the sole
declaring owner of GET /api/events; api.routes no longer declares an unfiltered
variant. The default response remains the historical JSON list; the UI opts into
metadata when it needs an explicit truncation signal.
"""
import asyncio
import json
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from api.routes import _public_transfer_presentation
from api.serializers import public_payload
from application import dispatch_admission as live_admission
from application.dependencies import get_application
from application.manual_candidate_failover import switch_candidate
from application.service import ApplicationService
from db.database import get_db
from transfers import codec
from transfers._repository_base import canonical_artifact_membership_sql
from transfers.display_name import normalized_transfer_display_name
from transfers.errors import Category, TransferError
from transfers.presentation_repository import (
    ARTIFACT_PRESENTATION_SNAPSHOT_KEYS,
    _CAPACITY_WAIT_PRESENTATION,
    _RAW_PRESENTATION,
    _TERMINAL_PRESENTATION,
    _WAIT_PRESENTATION,
    effective_presentation,
    public_source_identity,
    recovery_presentation,
)
from transfers.repository import _SWITCHABLE_ARTIFACT_STATES

router = APIRouter()

# The bounded list's remaining-work signal (Section 10 of the DP 1.0.12
# presentation task) reuses the SAME cheap artifact-state classification the
# comprehensive Details presentation already uses for its own read-time
# ``switch_eligible`` approximation -- ``transfers.repository
# ._SWITCHABLE_ARTIFACT_STATES``, itself a re-export of the ONE canonical
# owner ``transfers.manual_failover.SWITCH_ELIGIBLE_LIFECYCLE_STATES``
# (DP 1.0.12 recovery leveling, Section 31) -- rather than a fourth
# hand-copied literal list. A
# non-completed artifact whose status is NOT in this set (e.g. ``cancelled``,
# ``input_required``) has no acquisition path a candidate switch could ever
# help with, so it must not count as remaining work; a status of ``error`` IS
# included, matching this codebase's own established distinction between a
# recoverable failure (still switchable) and a terminal one. This is
# deliberately NOT the full authoritative actionability check — live
# provider health / route binding / candidate-expiry are out of bounded-SQL
# scope by design (see the group_common_sources comment below) and are only
# ever re-validated at actual switch time by manual_candidate_failover.py.
_SWITCHABLE_STATES_SQL = ", ".join(
    f"'{state}'" for state in sorted(_SWITCHABLE_ARTIFACT_STATES)
)

# The one canonical actionable-artifact filter (DP 1.0.12 recovery leveling,
# Section 7), shared with transfers._repository_base.TransferRepository.artifacts()
# so lifecycle aggregation and this bounded list's presentation-vote facts can
# never again silently diverge onto different child sets.
_CANONICAL_ARTIFACT_SQL = canonical_artifact_membership_sql("f")

# DP 1.0.12 UI Finishing (Correction 2 -- Dashboard Recent Activity priority).
#
# Candidate ACQUISITION (which raw rows are even fetched) is split by raw
# ``torrents.status`` into two small, closed, fixed sets -- every raw value
# transfers.models.TransferState can persist, partitioned by the one
# structural fact that actually determines the split: transfer_pause_intents
# and transfer_input_challenges rows (the two facts that can make
# effective_presentation diverge from raw status into "paused"/
# "input_required") are only ever written against, and only ever read for,
# a transfer while its lifecycle is still open -- see
# transfers.engine/convergence_engine's terminal-transition paths, which
# retire both before/at the same transition that raises torrents.status into
# one of _SETTLED_RAW_STATUSES. A transfer already at a settled raw status
# cannot carry a live pause intent or input challenge (regression-proven in
# test_dashboard_recent_activity_priority.py::
# test_settled_raw_status_transfer_cannot_carry_a_live_pause_or_input_challenge).
# This is what makes the raw-status split below safe to use for candidate
# ACQUISITION while effective_presentation() remains the sole authority for
# priority CLASSIFICATION (see _activity_priority_tier below) -- acquisition
# only ever needs to decide "is this row even worth fetching", never "which
# cohort does it ultimately belong to".
_ACTIVITY_LIVE_RAW_STATUSES = (
    "pending", "processing", "input_required", "ready", "queued",
    "downloading", "paused", "verifying", "extracting",
)
_SETTLED_RAW_STATUSES = ("completed", "error", "failed", "lost", "cancelled", "deleted", "consolidated")

# Priority-ordering classification of the real EFFECTIVE presentation output
# (never raw status, never badge color). Reuses presentation_repository's own
# canonical vocabulary instead of a hand-copied duplicate table, so this
# never drifts into a second parallel taxonomy:
#
# - _SETTLED_PRESENTATION_STATUSES reuses presentation_repository's own
#   _TERMINAL_PRESENTATION set (imported, not re-derived) plus "failed".
#   presentation_repository deliberately excludes "failed" from
#   _TERMINAL_PRESENTATION because a failed transfer remains operator-
#   actionable (manual retry/switch); this ordering nonetheless places it in
#   settled/history per the explicit product requirement: "Failed must not
#   outrank active/current work merely because it has an error badge."
# - _KNOWN_LIVE_PRESENTATION_STATUSES is derived from every state
#   presentation_repository._RAW_PRESENTATION / _WAIT_PRESENTATION /
#   _CAPACITY_WAIT_PRESENTATION can itself emit, minus the settled ones,
#   plus "input_required"/"requires_attention" (operator-gated overrides
#   _RAW_PRESENTATION never enumerates) and "extracting" (the durable
#   TransferState.POST_PROCESSING raw value, echoed as-is by
#   recovery_presentation's fallback since _RAW_PRESENTATION has no explicit
#   entry for it).
# - A presentation_status in NEITHER set -- something this ordering has
#   genuinely never seen -- gets its own middle tier: it can never outrank a
#   KNOWN live/actionable item (the literal product requirement), but it is
#   not assumed to be settled either. See _activity_priority_tier.
_SETTLED_PRESENTATION_STATUSES = frozenset(_TERMINAL_PRESENTATION) | {"failed"}
_KNOWN_LIVE_PRESENTATION_STATUSES = (
    frozenset(state for state, _label, _badge in _RAW_PRESENTATION.values())
    | frozenset(state for state, _label, _badge in _WAIT_PRESENTATION.values())
    | {_CAPACITY_WAIT_PRESENTATION[0], "input_required", "requires_attention", "extracting"}
) - _SETTLED_PRESENTATION_STATUSES


def _activity_priority_tier(presentation_status) -> int:
    """0 = known live/actionable, 1 = unrecognized (never outranks tier 0,
    never assumed settled), 2 = known settled/history."""
    status = str(presentation_status or "").strip().lower()
    if status in _SETTLED_PRESENTATION_STATUSES:
        return 2
    if status in _KNOWN_LIVE_PRESENTATION_STATUSES:
        return 0
    return 1


def _disabled_provider_ids(application) -> frozenset[str]:
    """Durable, PAGE-GLOBAL provider-enablement facts, read once per request.

    ``descriptor.enabled`` is computed once at composition time from
    AppSettings (e.g. ``bool(api_key)``) and held on the provider object the
    already-injected ``application.engine.registry`` carries for the whole
    request — no extra DB round-trip, no per-row/live check. This is NOT the
    full authoritative actionability gate: live route/candidate-expiry and
    provider *health* (``registry._unhealthy``) have no durable stored
    representation anywhere in this codebase today (confirmed: no production
    caller ever populates either), so they cannot be bounded-SQL-joined
    without either fabricating a stale guess or duplicating a live provider
    call per row — manual_candidate_failover.py re-validates those at actual
    switch time instead, and always has, even against the comprehensive
    Details read. Provider *enablement*, in contrast, is exactly the kind of
    durable fact that closes a real false-positive: a transfer whose only
    common hosts route through a disabled/unconfigured provider has no
    possible target, and the list must say so.

    Delegates to ``application.dispatch_admission.disabled_provider_ids`` --
    the same durable snapshot the Section 9 capacity-wait admission
    assessment below reuses -- so there is exactly one owner of "which
    provider ids are currently disabled."
    """
    return live_admission.disabled_provider_ids(getattr(application, "engine", None))


_EVENT_TIMEFRAME_MODIFIERS = {
    "1h": "-1 hour",
    "12h": "-12 hours",
    "24h": "-24 hours",
    "72h": "-72 hours",
    "7d": "-7 days",
    "30d": "-30 days",
}
EventTimeframe = Literal["all", "1h", "12h", "24h", "72h", "7d", "30d"]
EventLevel = Literal["info", "warning", "warn", "error"]
_SOURCE_PROJECTION_FIELDS = (
    "_source_request_payload",
    "_delivered_candidate_source",
    "_active_candidate_source",
    "_route_candidate_summary",
)


def _decode_projection_value(value, default=None):
    try:
        return codec.load(value, default)
    except (TypeError, ValueError, KeyError):
        return default


def _bounded_source_identity(row) -> dict[str, str]:
    """Derive the safe list icon identity without comprehensive presentation."""
    request_kind = ""
    request_payload = _decode_projection_value(row.get("_source_request_payload"), None)
    if isinstance(request_payload, dict):
        try:
            request_kind = str(codec.request(request_payload).kind or "").strip().lower()
        except (TypeError, ValueError, KeyError):
            request_kind = ""

    base_identity = public_source_identity(request_kind)
    if base_identity.get("kind") in {"magnet", "torrent_file"}:
        return base_identity

    candidate_source = None
    if str(row.get("status") or "").strip().lower() == "completed":
        delivered = _decode_projection_value(row.get("_delivered_candidate_source"), None)
        if public_source_identity(request_kind, delivered).get("kind") == "host":
            candidate_source = delivered

    if candidate_source is None:
        active = _decode_projection_value(row.get("_active_candidate_source"), None)
        if public_source_identity(request_kind, active).get("kind") == "host":
            candidate_source = active

    if candidate_source is None:
        candidates = _decode_projection_value(row.get("_route_candidate_summary"), [])
        if isinstance(candidates, list):
            for candidate in candidates:
                source = candidate.get("source") if isinstance(candidate, dict) else None
                if public_source_identity(request_kind, source).get("kind") == "host":
                    candidate_source = source
                    break

    return public_source_identity(request_kind, candidate_source)


def _bounded_child_presentations(raw_facts, *, paused, input_required, capacity_only_blocked_ids):
    """Project the page's per-artifact child presentations for the shared owner.

    ``raw_facts`` is the JSON array the bounded projection built from raw
    durable facts (each artifact's id, status, plus its latest recovery-
    snapshot fields) already restricted to canonical artifact membership
    (Section 7). Each entry is passed through the same
    ``recovery_presentation`` projector the comprehensive Details path uses,
    with the Section 9 capacity-wait fact reduced to a single set-membership
    check against ``capacity_only_blocked_ids`` -- the execution-admission
    owner's own positive record (``transfers.convergence_engine
    .TransferEngine.capacity_only_blocked_ids``) -- so this can never derive a
    capacity-wait classification the real dispatch path didn't itself assert.
    """
    try:
        facts = json.loads(raw_facts) if raw_facts else []
    except (TypeError, ValueError):
        facts = []
    if not isinstance(facts, list):
        return []
    presentations = []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        context = {key: fact.get(key) for key in ARTIFACT_PRESENTATION_SNAPSHOT_KEYS}
        artifact_id = fact.get("artifact_id")
        presentations.append(recovery_presentation(
            fact.get("status"), context, paused=paused, input_required=input_required,
            capacity_only_blocked=artifact_id in capacity_only_blocked_ids,
        ))
    return presentations


# DP 1.0.12 Workstream B (Section 10): the one canonical file-selection
# affordance semantic, shared by Details, Dashboard Recent, and Downloads —
# never three independent per-surface eligibility heuristics. Derived only
# from durable transfers.file_selection facts (the same ones
# transfers.repository.TransferRepository.file_selection_presentation exposes
# through the fresh-click read endpoint): whether a selection generation
# exists at all, whether it is still mutable (fs.selection_mutable ==
# manifest_committed_at IS NULL), whether a manifest is bound, its file
# count, and its decision. Never inferred from provider name, filename shape,
# UI glyph, or an open connection.
_FILE_SELECTION_EXPLICIT_DECISION = "explicit"


def _file_selection_affordance(manifest_id, decision, committed_at, file_count: int) -> str:
    if manifest_id is None and decision is None:
        # No selection generation exists for this transfer at all.
        return "none"
    if committed_at is not None:
        # Executable child materialization already committed -- locked.
        return "none"
    if manifest_id is None:
        # A generation exists (torrent/magnet resolution in progress) but no
        # usable manifest has arrived yet.
        return "pending_manifest"
    if file_count <= 1:
        # Single-file torrent/magnet: no picker action (Section 6.9).
        return "none"
    if str(decision or "") == _FILE_SELECTION_EXPLICIT_DECISION:
        return "change"
    return "choose"


@router.post("/torrents/{transfer_id}/artifacts/{artifact_id}/candidate")
async def activate_artifact_candidate(
    transfer_id: int,
    artifact_id: int,
    candidate_id: Annotated[str, Body(embed=True, min_length=1, max_length=128)],
    application: ApplicationService = Depends(get_application),
):
    """Request activation of one exact existing canonical acquisition candidate."""
    try:
        return await switch_candidate(application, transfer_id, artifact_id, candidate_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Transfer not found") from None
    except TransferError as exc:
        missing = exc.error.category in {Category.RESOURCE_NOT_FOUND, Category.SOURCE_NOT_FOUND}
        raise HTTPException(
            status_code=404 if missing else 409,
            detail=exc.error.as_dict(),
        ) from None


@router.get("/events")
async def list_activity_events(
    search: Optional[str] = None,
    level: Optional[EventLevel] = None,
    timeframe: EventTimeframe = "all",
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    include_meta: bool = False,
):
    """Return newest matching events with filters applied before LIMIT.

    ``limit + 1`` is fetched after every predicate so a metadata caller can
    distinguish an exact-limit result from a capped result. ``instr`` keeps the
    browser's literal substring semantics for ``%`` and ``_`` while all user
    supplied values remain SQL parameters.
    """
    async with get_db() as db:
        clauses = []
        params = []

        if timeframe != "all":
            clauses.append("datetime(e.created_at) >= datetime('now', ?)")
            params.append(_EVENT_TIMEFRAME_MODIFIERS[timeframe])

        if level:
            normalized_level = str(level).lower()
            if normalized_level in {"warn", "warning"}:
                clauses.append("LOWER(COALESCE(e.level, 'info')) IN ('warn', 'warning')")
            else:
                clauses.append("LOWER(COALESCE(e.level, 'info')) = ?")
                params.append(normalized_level)

        if search is not None and search.strip():
            needle = search.strip().lower()
            clauses.append(
                """(
                    instr(LOWER(COALESCE(e.message, '')), ?) > 0
                    OR instr(LOWER(COALESCE(t.name, '')), ?) > 0
                )"""
            )
            params.extend([needle, needle])

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await db.fetchall(
            f"""
            SELECT
                e.level,
                e.message,
                e.created_at,
                t.name AS torrent_name
            FROM events e
            LEFT JOIN torrents t ON t.id = e.torrent_id
            {where}
            ORDER BY e.created_at DESC, e.id DESC
            LIMIT ?
            """,
            [*params, limit + 1],
        )
        # Match the browser-facing serialization the generic router applied to
        # this collection before it moved here: naive SQLite UTC timestamps gain
        # an explicit "Z" designator and known capability fields are stripped.
        items = public_payload(rows[:limit])
        if include_meta:
            return {"items": items, "truncated": len(rows) > limit, "limit": limit}
        return items


_ACTIVITY_ORDER = "activity"


async def _bounded_status_candidates(db, base_where: str, params: list, statuses: tuple, limit: int) -> list:
    """One indexed SEEK per raw status value, via ``idx_torrents_status_created
    (status, created_at)`` -- never a scan across the whole excluded/included
    status set. Each of the ``len(statuses)`` queries SEEKs directly to that
    status's slice of the index and walks it in ``created_at`` DESC order,
    stopping at ``LIMIT`` -- work bounded by ``len(statuses) x limit``, a
    small fixed constant, never by total table size. ``len(statuses)`` is
    itself fixed (9 live raw statuses or 7 settled raw statuses -- see
    ``_ACTIVITY_LIVE_RAW_STATUSES`` / ``_SETTLED_RAW_STATUSES`` above), so
    this is still a bounded, fixed number of SQL statements regardless of
    history size. Returns ``(id, created_at)`` pairs (not yet re-sorted
    across statuses or truncated -- the caller merges/truncates once all
    cohort sources are collected).
    """
    rows = []
    for status in statuses:
        sql = f"""SELECT t.id, t.created_at
            FROM torrents t INDEXED BY idx_torrents_status_created
            WHERE {base_where} AND t.status = ?
            ORDER BY t.created_at DESC LIMIT ?"""
        found = await db.fetchall(sql, [*params, status, limit])
        rows.extend((row["id"], row["created_at"]) for row in found)
    return rows


_RECOVERY_NET_SCAN_CAP = 200


async def _recovery_net_candidate_ids(db, base_where: str, params: list, limit: int) -> list:
    """Bounded net for the one confirmed way the raw-status acquisition split
    above can miss a genuinely live/actionable transfer.

    ``transfers.presentation_repository._aggregate_presentation`` reads
    ``paused``/``input_required`` from ``transfer_pause_intents`` /
    ``transfer_input_challenges`` independently of ``torrents.status``, and
    (pre-existing engine behavior, confirmed during this correction, entirely
    unrelated to this task) neither table is guaranteed to be cleared on
    every terminal lifecycle transition -- only the manual cancel/delete
    paths clear them; a ``fail_permanently``/error/lost transition or
    consolidation can leave a stale row behind. A transfer whose raw
    ``torrents.status`` has therefore reached a settled value while still
    carrying one of these rows resolves to effective presentation "paused" /
    "input_required" (live/actionable), not its raw settled status -- see
    test_settled_raw_status_transfer_with_lingering_pause_intent_is_still_
    projected_as_live in test_dashboard_recent_activity_priority.py.

    These two auxiliary tables have NO structural cap of their own -- the
    same engine gap that motivates this net at all means a transfer can
    leave a row behind forever, so table size can grow with total
    operational history, not just with currently-relevant rows. A first
    version of this function joined FROM the raw table (``transfer_pause_
    intents`` / ``transfer_input_challenges``) directly; that is bounded by
    those tables' size, not by ``limit`` -- decoupled from ``torrents``, but
    not genuinely bounded. Each query here instead first SEEKs, via
    ``idx_transfer_pause_intents_paused_updated`` /
    ``idx_transfer_input_challenges_updated``, to only the
    ``_RECOVERY_NET_SCAN_CAP`` most-recently-touched rows of the auxiliary
    table -- a fixed, structural cap enforced by an indexed ORDER BY + LIMIT
    on the auxiliary table itself, before ever joining to ``torrents`` --
    and only then CROSS JOINs that already-bounded candidate set to
    ``torrents``. Cost is therefore bounded by ``_RECOVERY_NET_SCAN_CAP``,
    a fixed constant, regardless of how large either table grows -- proven
    via EXPLAIN QUERY PLAN and by growing the auxiliary tables themselves
    (not just ``torrents``) in test_dashboard_recent_activity_priority.py.
    """
    # CROSS JOIN (not a plain JOIN) is load-bearing here, not stylistic: SQLite
    # is free to reorder a plain JOIN's tables, and measurement during this
    # correction proved it chose to drive from `torrents` and sort every
    # settled-status row before LIMIT could be applied -- exactly the
    # pathology this correction exists to eliminate. CROSS JOIN pins the
    # written table order, forcing the already-capped auxiliary-table subquery
    # to drive the join and an indexed primary-key probe into torrents per row.
    settled_placeholders = ", ".join("?" for _ in _SETTLED_RAW_STATUSES)
    # INDEXED BY on each auxiliary-table subquery is load-bearing, not
    # decoration: measurement proved the challenge subquery (no equality
    # predicate to seek on, only an ORDER BY) is exactly the same trap as the
    # original torrents query -- SQLite's cost estimator chose a full SCAN +
    # temp-b-tree sort of transfer_input_challenges instead of walking
    # idx_transfer_input_challenges_updated in order, silently defeating the
    # LIMIT cap this subquery exists to enforce. Pinning the index forces the
    # ordered-walk-then-stop-at-LIMIT shape deterministically, for both
    # auxiliary tables, regardless of their current size or content.
    pause_sql = f"""SELECT t.id, t.created_at
        FROM (
            SELECT torrent_id, updated_at FROM transfer_pause_intents
            INDEXED BY idx_transfer_pause_intents_paused_updated
            WHERE paused = 1
            ORDER BY updated_at DESC LIMIT ?
        ) p
        CROSS JOIN torrents t ON t.id = p.torrent_id
        WHERE {base_where} AND t.status IN ({settled_placeholders})
        ORDER BY t.created_at DESC LIMIT ?"""
    challenge_sql = f"""SELECT t.id, t.created_at
        FROM (
            SELECT transfer_id, updated_at FROM transfer_input_challenges
            INDEXED BY idx_transfer_input_challenges_updated
            ORDER BY updated_at DESC LIMIT ?
        ) c
        CROSS JOIN torrents t ON t.id = c.transfer_id
        WHERE {base_where} AND t.status IN ({settled_placeholders})
        ORDER BY t.created_at DESC LIMIT ?"""
    pause_rows = await db.fetchall(
        pause_sql, [_RECOVERY_NET_SCAN_CAP, *params, *_SETTLED_RAW_STATUSES, limit]
    )
    challenge_rows = await db.fetchall(
        challenge_sql, [_RECOVERY_NET_SCAN_CAP, *params, *_SETTLED_RAW_STATUSES, limit]
    )
    return [(row["id"], row["created_at"]) for row in (*pause_rows, *challenge_rows)]


def _newest_first_unique_ids(pairs: list, limit: int) -> list:
    """Merge ``(id, created_at)`` pairs from multiple bounded sources,
    de-duplicate (a transfer can legitimately appear in more than one
    source query), sort newest-first, and cap at ``limit``. Pure in-memory
    work over an already-bounded input set -- no additional DB round trip.
    """
    by_id = {}
    for transfer_id, created_at in pairs:
        if transfer_id not in by_id or created_at > by_id[transfer_id]:
            by_id[transfer_id] = created_at
    ordered = sorted(by_id.items(), key=lambda pair: pair[1], reverse=True)
    return [transfer_id for transfer_id, _created_at in ordered[:limit]]


async def _activity_cohort_candidate_ids(where_clauses: list, params: list, limit: int) -> list:
    """Bounded candidate-id acquisition for Dashboard Recent Activity
    priority ordering (Correction 2). Every query below is a fixed, bounded
    number of indexed SEEK-and-walk statements (never a scan/sort
    proportional to total history size -- see the boundedness proof in the
    finishing-pass report), and the total candidate id count returned is
    always ``<= limit``.

    1. One SEEK per known live raw status (``_bounded_status_candidates``
       against ``_ACTIVITY_LIVE_RAW_STATUSES``).
    2. The recovery net (``_recovery_net_candidate_ids``) -- always run,
       small and independently bounded -- folded into the live set since a
       hit there is live/actionable in truth regardless of its raw status.
    3. Only if steps 1-2 together did not already reach ``limit``: one SEEK
       per settled raw status (``_bounded_status_candidates`` against
       ``_SETTLED_RAW_STATUSES``), to fill the remaining room with recent
       settled/history rows.
    """
    base_where = " AND ".join(where_clauses)
    async with get_db() as db:
        live_pairs = await _bounded_status_candidates(
            db, base_where, params, _ACTIVITY_LIVE_RAW_STATUSES, limit
        )
        live_pairs += await _recovery_net_candidate_ids(db, base_where, params, limit)
        live_ids = _newest_first_unique_ids(live_pairs, limit)

        remaining = limit - len(live_ids)
        settled_ids = []
        if remaining > 0:
            settled_pairs = await _bounded_status_candidates(
                db, base_where, params, _SETTLED_RAW_STATUSES, remaining
            )
            settled_ids = _newest_first_unique_ids(settled_pairs, remaining)

    return live_ids + settled_ids


def _apply_activity_priority_order(items: list) -> list:
    """Stable-sort an already recency-ordered candidate page into three tiers
    -- known live/actionable, unrecognized, known settled/history (see
    ``_activity_priority_tier``) -- preserving recency inside each tier
    (Python's ``sorted`` is stable). Classification uses the real
    ``effective_presentation()`` output already computed per item by the
    caller -- never badge color, never ``presentation_badge_status``, never a
    re-derived raw-status guess. A presentation status this ordering has
    never seen before can never outrank a known live/actionable item, and is
    never assumed to be settled either.
    """
    return sorted(
        items,
        key=lambda item: _activity_priority_tier(item.get("presentation_status")),
    )


@router.get("/torrents")
async def list_operational_torrents(
    status: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(0, ge=0, le=5000),
    offset: int = 0,
    order: Optional[str] = None,
    application: ApplicationService = Depends(get_application),
):
    clauses = []
    params = []

    if status:
        clauses.append("t.status = ?")
        params.append(status)
    else:
        clauses.append("t.status NOT IN ('deleted', 'consolidated')")

    if search:
        clauses.append(
            """(
                LOWER(COALESCE(t.name, '')) LIKE ?
                OR LOWER(COALESCE(t.source_fingerprint, t.hash, '')) LIKE ?
                OR LOWER(COALESCE(t.source, '')) LIKE ?
                OR LOWER(COALESCE(t.label, '')) LIKE ?
                OR LOWER(COALESCE(t.error_message, '')) LIKE ?
            )"""
        )
        needle = f"%{search.strip().lower()}%"
        params.extend([needle, needle, needle, needle, needle])

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    # DP 1.0.12 UI Finishing (Correction 2): a narrow, additive ordering mode
    # for Dashboard Recent Activity. It only engages for the plain default
    # bounded-page shape (no explicit status filter, first page, a positive
    # limit) -- exactly what the Dashboard's own dynamic-capacity request
    # always sends. Any other combination (explicit status filter, a nonzero
    # offset, no limit) falls straight through to the unchanged ordinary
    # page_sql below, so default Downloads/list behavior and pagination
    # semantics are untouched.
    activity_mode = bool(
        order == _ACTIVITY_ORDER and status is None and offset == 0 and limit > 0
    )

    if activity_mode:
        candidate_ids = await _activity_cohort_candidate_ids(clauses, params, limit)
        if candidate_ids:
            values_sql = ", ".join(f"({int(cid)})" for cid in candidate_ids)
            page_sql = f"SELECT column1 AS id FROM (VALUES {values_sql})"
        else:
            page_sql = "SELECT NULL AS id WHERE 0"
        query_params = []
    else:
        page_sql = f"""SELECT t.id
            FROM torrents t {where}
            ORDER BY t.created_at DESC"""
        query_params = list(params)
        if limit > 0:
            page_sql += " LIMIT ? OFFSET ?"
            query_params.extend([limit, offset])

    # Explicit consolidated status is a durable-history diagnostic view, not
    # the normal Downloads collection. Preserve its established comprehensive
    # presentation semantics while keeping the default/current list bounded.
    if status == "consolidated":
        async with get_db() as db:
            history_rows = await db.fetchall(page_sql, query_params)
            total_row = await db.fetchone(
                f"SELECT COUNT(*) AS cnt FROM torrents t {where}", params
            )
            total = total_row["cnt"] if total_row else 0

        presentations = await asyncio.gather(
            *(
                application.repository.presentation(row["id"])
                for row in history_rows
            )
        )
        items = [
            _public_transfer_presentation(item, application.definitions)
            for item in presentations
            if item is not None
        ]
        return {"items": items, "total": total}

    # Durable, page-global provider-enablement facts (see _disabled_provider_ids
    # docstring) — computed once per request from the already-injected
    # application, never per-row and never an extra DB round-trip.
    disabled_provider_ids = _disabled_provider_ids(application)
    disabled_provider_clause = (
        "AND movement.provider_id NOT IN ({})".format(
            ", ".join(f"'{pid}'" for pid in sorted(disabled_provider_ids))
        )
        if disabled_provider_ids
        else ""
    )

    # The normal Downloads collection is a bounded read model. It intentionally
    # does not reconstruct the comprehensive per-transfer presentation used by
    # the detail route. All list-only enrichment is computed in this one SQL read.
    query = f"""
        WITH page AS (
            {page_sql}
        ),
        latest_route AS (
            SELECT transfer_id, provider_id, candidate_summary
            FROM (
                SELECT
                    p.transfer_id,
                    a.provider_id,
                    p.candidate_summary,
                    ROW_NUMBER() OVER (
                        PARTITION BY p.transfer_id
                        ORDER BY p.ordinal DESC, p.updated_at DESC
                    ) AS row_number
                FROM route_attempt_provenance p
                JOIN resolution_attempts a
                  ON a.id = p.resolution_attempt_id
                JOIN page
                  ON page.id = p.transfer_id
            )
            WHERE row_number = 1
        ),
        delivery AS (
            SELECT
                p.transfer_id,
                COUNT(DISTINCT p.provider_id) AS provider_count,
                MIN(p.provider_id) AS provider_id
            FROM execution_attempt_provenance p
            JOIN page
              ON page.id = p.transfer_id
            WHERE p.delivered = 1
              AND p.provider_id IS NOT NULL
            GROUP BY p.transfer_id
        ),
        delivered_source AS (
            SELECT transfer_id, candidate_source
            FROM (
                SELECT
                    p.transfer_id,
                    p.candidate_source,
                    ROW_NUMBER() OVER (
                        PARTITION BY p.transfer_id
                        ORDER BY e.updated_at DESC, p.ordinal DESC, e.id DESC
                    ) AS row_number
                FROM execution_attempt_provenance p
                JOIN execution_attempts e
                  ON e.id = p.execution_attempt_id
                JOIN page
                  ON page.id = p.transfer_id
                WHERE p.delivered = 1
            )
            WHERE row_number = 1
        ),
        active_source AS (
            SELECT transfer_id, candidate_source
            FROM (
                SELECT
                    f.torrent_id AS transfer_id,
                    p.candidate_source,
                    ROW_NUMBER() OVER (
                        PARTITION BY f.torrent_id
                        ORDER BY f.updated_at DESC, p.ordinal DESC, f.id DESC
                    ) AS row_number
                FROM download_files f
                JOIN execution_attempt_provenance p
                  ON p.execution_attempt_id = f.execution_attempt_id
                JOIN page
                  ON page.id = f.torrent_id
                WHERE f.execution_attempt_id IS NOT NULL
                  AND COALESCE(f.mirror_state, '') != 'standby'
            )
            WHERE row_number = 1
        ),
        root_request AS (
            SELECT transfer_id, payload
            FROM (
                SELECT
                    r.transfer_id,
                    r.payload,
                    ROW_NUMBER() OVER (
                        PARTITION BY r.transfer_id
                        ORDER BY CASE WHEN r.parent_id IS NULL THEN 0 ELSE 1 END,
                                 r.ordinal,
                                 r.id
                    ) AS row_number
                FROM transfer_requests r
                JOIN page
                  ON page.id = r.transfer_id
            )
            WHERE row_number = 1
        ),
        request_failures AS (
            SELECT
                r.transfer_id,
                COUNT(*) AS failure_count
            FROM transfer_requests r
            JOIN page
              ON page.id = r.transfer_id
            WHERE r.state = 'failed'
            GROUP BY r.transfer_id
        ),
        -- Durable state of the CURRENT authoritative root provider-resource
        -- binding only. The root request's own ``resource`` payload id is
        -- matched to this transfer's binding row: transfer-scoped, so a
        -- predecessor/tombstoned resource on another transfer is excluded, and
        -- a historical binding of THIS transfer is excluded because the current
        -- root request points only at the current resource. ``resource_key``
        -- NULL is the pre-split historical form (primary key IS the canonical
        -- id). Consumed only as a presentation override, never a status mutation.
        current_root_resource AS (
            SELECT rr.transfer_id, pr.state AS resource_state
            FROM (
                SELECT
                    r.transfer_id,
                    r.resource,
                    ROW_NUMBER() OVER (
                        PARTITION BY r.transfer_id
                        ORDER BY r.ordinal, r.id
                    ) AS row_number
                FROM transfer_requests r
                JOIN page
                  ON page.id = r.transfer_id
                WHERE r.parent_id IS NULL AND r.resource IS NOT NULL
            ) rr
            JOIN provider_resources pr
              ON pr.transfer_id = rr.transfer_id
             AND (pr.resource_key = json_extract(rr.resource, '$.id')
                  OR (pr.resource_key IS NULL
                      AND pr.id = json_extract(rr.resource, '$.id')))
            WHERE rr.row_number = 1
        ),
        -- Every current CANONICAL artifact on the page: the exact same
        -- membership predicate transfers._repository_base.TransferRepository
        -- .artifacts() uses for lifecycle aggregation (DP 1.0.12 recovery
        -- leveling, Section 7 -- this CTE previously included blocked/
        -- standby/non-request-bound rows too, which let a non-actionable
        -- child vote in this bounded list's aggregate presentation truth
        -- even though lifecycle aggregation already excluded it; that drift
        -- is exactly what Section 7 requires eliminating). Feeds ONLY the
        -- recovery-snapshot join below and the presentation-vote facts; never
        -- a second definition of current-artifact membership, and never used
        -- for Details' historical/provenance file listing, which still shows
        -- every row.
        page_artifacts AS (
            SELECT f.id AS artifact_id, f.torrent_id AS transfer_id, f.status AS status
            FROM download_files f
            JOIN page ON page.id = f.torrent_id
            WHERE {_CANONICAL_ARTIFACT_SQL}
        ),
        -- Per-artifact presentation facts for the page, folded into the one
        -- bounded read as a JSON array per transfer (one row per transfer, no
        -- per-row query, no comprehensive presentation call). ONLY raw durable
        -- facts are projected here — the artifact's own id/status plus the
        -- fields of its current durable recovery state; no presentation or
        -- precedence logic lives in SQL. Section 9's capacity-wait fact is
        -- NOT projected here at all -- it is a live, execution-admission-owned
        -- fact (transfers.convergence_engine.TransferEngine
        -- .capacity_only_blocked_ids), looked up by artifact_id in Python,
        -- never reconstructed from durable columns. In Python these feed the
        -- SHARED pure owner transfers.presentation_repository
        -- .effective_presentation exactly as the comprehensive Details
        -- projection feeds it, so the bounded list can never derive a
        -- processing truth that disagrees with Details.
        --
        -- DP 1.0.12 recovery leveling, Section 14/20: this is a plain JOIN
        -- against artifact_recovery_state's primary key -- one current row
        -- per artifact -- rather than the pre-leveling window-function
        -- reduction over an unbounded, ever-growing application_events
        -- history (that shape existed only because "current" state used to
        -- be reconstructed by picking the latest of many snapshot events per
        -- artifact; current state is now a single row, so there is nothing
        -- left to rank/partition). Still page-bounded via page_artifacts —
        -- no work proportional to rows outside the requested page.
        artifact_presentation_facts AS (
            SELECT
                pa.transfer_id AS transfer_id,
                json_group_array(json_object(
                    'artifact_id', pa.artifact_id,
                    'status', pa.status,
                    'quiescence_reason', ars.quiescence_reason,
                    'decision_action', ars.decision_action,
                    'last_applied_action', ars.last_applied_action,
                    'decision_reason', ars.decision_reason,
                    'last_applied_reason', ars.last_applied_reason,
                    'wake_condition', ars.wake_condition,
                    'recovery_claim_token', ars.recovery_claim_token
                )) AS artifacts
            FROM page_artifacts pa
            LEFT JOIN artifact_recovery_state ars
              ON ars.artifact_id = pa.artifact_id
            GROUP BY pa.transfer_id
        ),
        input_challenge AS (
            SELECT DISTINCT c.transfer_id
            FROM transfer_input_challenges c JOIN page ON page.id = c.transfer_id
        ),
        -- DP 1.0.12 Workstream B: bounded file-selection affordance hint.
        -- The transfer's CURRENT selection generation only (newest by
        -- created_at/id — the exact same "newest wins" rule
        -- transfers.repository.TransferRepository._current_generation uses,
        -- never re-derived), joined once per page, never per-row. Only the
        -- three durable facts the Section 10 classifier needs are projected;
        -- no manifest/entry list crosses into this bounded read.
        page_current_file_selection AS (
            SELECT transfer_id, manifest_id, decision, manifest_committed_at
            FROM (
                SELECT
                    s.transfer_id,
                    s.manifest_id,
                    s.decision,
                    s.manifest_committed_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY s.transfer_id ORDER BY s.created_at DESC, s.id DESC
                    ) AS row_number
                FROM transfer_file_selections s
                JOIN page ON page.id = s.transfer_id
            )
            WHERE row_number = 1
        ),
        -- Entry count of that current generation's bound manifest (0/absent
        -- when no manifest is bound yet) — the same durable manifest-size
        -- fact the fresh-click file-selection read model exposes, never a
        -- filename-shape guess.
        page_file_selection_manifest_counts AS (
            SELECT e.manifest_id, COUNT(*) AS manifest_entry_count
            FROM transfer_file_manifest_entries e
            JOIN page_current_file_selection sel ON sel.manifest_id = e.manifest_id
            GROUP BY e.manifest_id
        ),
        -- Transfer-level COMMON-SOURCE group MEMBERSHIP summary derived only
        -- from canonical acquisition-candidate storage. Membership is the raw
        -- whole-transfer canonical-host intersection: a host counts as common
        -- when EVERY current authoritative artifact (physical, unblocked,
        -- non-standby — the same current-artifact identity the detail candidate
        -- projection already uses) carries ANY canonical host-scoped candidate
        -- for that host. Membership is deliberately independent of
        -- switch-eligibility, artifact operational state, or which candidate is
        -- currently selected — those are GROUP-ACTIONABILITY facts, computed
        -- lazily from Details when the chooser opens, never here.
        -- ``common_candidate_count`` is the size of that raw intersection; the
        -- group launcher shows only for 2 or more. Host normalization is kept
        -- byte-identical to transfers.repository._group_source_host so this
        -- bounded count and the Details-derived group set agree. Computed once
        -- here: no per-row query, no comprehensive presentation, never a full
        -- candidate/actionability matrix.
        group_member_artifacts AS (
            SELECT
                f.torrent_id AS transfer_id,
                f.id AS artifact_id,
                f.status AS status,
                f.filename AS filename,
                f.candidates AS candidates,
                f.selected_candidate AS selected_candidate
            FROM download_files f
            JOIN page
              ON page.id = f.torrent_id
            WHERE {_CANONICAL_ARTIFACT_SQL}
        ),
        group_member_counts AS (
            SELECT transfer_id, COUNT(*) AS artifact_total
            FROM group_member_artifacts
            GROUP BY transfer_id
        ),
        -- Per-artifact currently-SELECTED candidate id, decoded from the same
        -- durable JSON array + index the comprehensive per-file presentation
        -- reads (transfers.repository._candidate_presentation:
        -- ``candidates[selected_candidate].id``). Pure function of already-
        -- stored columns -- no live call, no extra table. Feeds ONLY the
        -- movement check below; membership stays untouched by it.
        group_member_selected_candidate AS (
            SELECT
                transfer_id,
                artifact_id,
                json_extract(candidates, '$[' || selected_candidate || '].id') AS selected_candidate_id
            FROM group_member_artifacts
        ),
        -- Remaining-work membership count: current authoritative artifacts of
        -- the transfer (same identity as group_member_artifacts above) whose
        -- own artifact-lifecycle state is one a candidate switch could ever
        -- apply to (see _SWITCHABLE_STATES_SQL above — reused from the exact
        -- classification the comprehensive Details presentation already
        -- uses). Independent of common-source MEMBERSHIP/count — this only
        -- tells the list surfaces whether the group launcher should render
        -- as an interactive action or a static history indicator (Section 10
        -- of the DP 1.0.12 presentation pass); the PER-HOST actionability
        -- verdict (which of the common hosts this remaining work can
        -- actually converge to right now) is still resolved lazily from
        -- Details when the chooser opens, never here.
        group_remaining_counts AS (
            SELECT transfer_id, COUNT(*) AS remaining_count
            FROM group_member_artifacts
            WHERE LOWER(TRIM(COALESCE(status, ''))) IN ({_SWITCHABLE_STATES_SQL})
            GROUP BY transfer_id
        ),
        -- Current authoritative artifact filenames for the transfer, in the
        -- same current-artifact scope as group_member_artifacts above (never
        -- a divergent definition). Feeds the pure display-name normalizer in
        -- Python — no filename parsing happens in SQL. Row order inside the
        -- aggregate follows the artifact id, matching the same ordering
        -- convention already used for artifact_presentation_facts below.
        group_member_filenames AS (
            SELECT transfer_id, json_group_array(filename) AS filenames
            FROM (
                SELECT transfer_id, filename
                FROM group_member_artifacts
                ORDER BY artifact_id
            )
            GROUP BY transfer_id
        ),
        group_member_hosts AS (
            SELECT DISTINCT
                a.transfer_id,
                a.artifact_id,
                b.provider_id AS provider_id,
                b.candidate_id AS candidate_id,
                rtrim(
                    CASE
                        WHEN lower(trim(b.source_key)) LIKE 'www.%'
                        THEN substr(lower(trim(b.source_key)), 5)
                        ELSE lower(trim(b.source_key))
                    END,
                    '.'
                ) AS host
            FROM group_member_artifacts a
            JOIN canonical_candidate_bindings b
              ON b.canonical_artifact_id = a.artifact_id
             AND lower(COALESCE(b.source_scope, '')) = 'host'
             AND length(trim(COALESCE(b.source_key, ''))) > 0
        ),
        group_true_common_hosts AS (
            SELECT gmh.transfer_id, gmh.host
            FROM group_member_hosts gmh
            JOIN group_member_counts gmc
              ON gmc.transfer_id = gmh.transfer_id
            WHERE length(gmh.host) BETWEEN 1 AND 253
            GROUP BY gmh.transfer_id, gmh.host
            HAVING COUNT(DISTINCT gmh.artifact_id) = MAX(gmc.artifact_total)
        ),
        group_common_sources AS (
            SELECT common.transfer_id, COUNT(*) AS common_candidate_count
            FROM group_true_common_hosts common
            GROUP BY common.transfer_id
        ),
        -- A common host requires MOVEMENT for the remaining-work set when at
        -- least one remaining-work (switchable-lifecycle, non-completed)
        -- artifact's binding for that host is NOT its currently-selected
        -- candidate. This is the piece a bare "host is common + enabled"
        -- check misses: the already-uniform ACTIVE source is trivially
        -- common and trivially enabled but requires ZERO artifact movement,
        -- so it cannot by itself make the transfer list-actionable (Section
        -- 8/10 of the DP 1.0.12 presentation task) — offering it as the only
        -- "interactive" choice would open a chooser that can never actually
        -- switch anything. is_selected is decoded the same way the
        -- comprehensive per-file presentation already does (candidate id at
        -- the durable ``selected_candidate`` index) — pure stored data, no
        -- live call, no extra table.
        group_remaining_host_movement AS (
            SELECT DISTINCT gmh.transfer_id, gmh.host, gmh.provider_id
            FROM group_member_hosts gmh
            JOIN group_member_artifacts gma
              ON gma.transfer_id = gmh.transfer_id
             AND gma.artifact_id = gmh.artifact_id
             AND LOWER(TRIM(COALESCE(gma.status, ''))) IN ({_SWITCHABLE_STATES_SQL})
            JOIN group_member_selected_candidate gmsc
              ON gmsc.transfer_id = gmh.transfer_id
             AND gmsc.artifact_id = gmh.artifact_id
            WHERE gmh.candidate_id IS NOT gmsc.selected_candidate_id
        ),
        -- Universal-convergence VETO, matching the shipped chooser's own
        -- participant scope exactly (ui-group-candidates.js computeGroup:
        -- actionParticipants excludes ONLY completed files — nothing else).
        -- A common host H is vetoed when some non-completed participant is
        -- BOTH (a) not currently selected on H, AND (b) not in a switchable
        -- lifecycle state — i.e. it can never move to H by a candidate
        -- switch, exactly like the chooser's own actionableHosts check
        -- (every actionParticipant must already be selected on H, or
        -- switch_eligible for H — switch_eligible is false whenever the
        -- artifact's own state disqualifies it, regardless of the target).
        -- Membership already guarantees every participant has a binding for
        -- every true common host, so a missing join row cannot silently
        -- hide a veto.
        group_host_vetoes AS (
            SELECT DISTINCT gmh.transfer_id, gmh.host
            FROM group_member_hosts gmh
            JOIN group_member_artifacts gma
              ON gma.transfer_id = gmh.transfer_id
             AND gma.artifact_id = gmh.artifact_id
             AND LOWER(TRIM(COALESCE(gma.status, ''))) != 'completed'
             AND LOWER(TRIM(COALESCE(gma.status, ''))) NOT IN ({_SWITCHABLE_STATES_SQL})
            JOIN group_member_selected_candidate gmsc
              ON gmsc.transfer_id = gmh.transfer_id
             AND gmsc.artifact_id = gmh.artifact_id
            WHERE gmh.candidate_id IS NOT gmsc.selected_candidate_id
        ),
        -- Does this transfer have at least one TRUE common host (the exact
        -- same set group_common_sources counts, unmodified) that (a)
        -- requires at least one artifact movement for the remaining-work set
        -- (excludes the already-uniform ACTIVE source), (b) is NOT vetoed by
        -- a participant that can never converge there (the shipped chooser's
        -- own universal-convergence rule, above), AND (c) is backed by a
        -- currently-ENABLED provider (a durable, page-global fact — see
        -- _disabled_provider_ids docstring; live provider health/candidate-
        -- expiry have no durable stored representation anywhere in this
        -- codebase and are only ever re-validated at actual switch time by
        -- manual_candidate_failover.py, even against the comprehensive
        -- Details read — the bounded list is a conservative AFFORDANCE
        -- projection, never more permissive than the chooser, and the write
        -- endpoint remains the final authority). Feeds ONLY
        -- group_remaining_count below; group_common_sources/
        -- common_candidate_count above is completely untouched by this —
        -- membership/history stays independent of actionability, unchanged.
        group_actionable_common_sources AS (
            SELECT DISTINCT common.transfer_id
            FROM group_true_common_hosts common
            JOIN group_remaining_host_movement movement
              ON movement.transfer_id = common.transfer_id
             AND movement.host = common.host
             {disabled_provider_clause}
            LEFT JOIN group_host_vetoes veto
              ON veto.transfer_id = common.transfer_id
             AND veto.host = common.host
            WHERE veto.transfer_id IS NULL
        ),
        -- ── Candidate-action scope (DP 1.0.12 Contextual Candidate Action
        -- Scope task, §3-4) ── a THIRD, orthogonal read fact alongside
        -- membership (group_common_sources/common_candidate_count) and
        -- remaining-work (group_remaining_counts/group_remaining_count):
        -- the smallest unambiguous candidate-switch OPERATION SCOPE the
        -- operator can act on right now (none|artifact|group). Never fed
        -- back into membership/remaining-work and never derived from a
        -- narrowed "movable subset" intersection.
        --
        -- Per-artifact canonical candidate IDENTITY count: protocol/provider-
        -- neutral (no source_scope filter, unlike the host-scoped
        -- group_member_hosts above), counting DISTINCT candidate ids so a
        -- duplicate binding row can never inflate it. Feeds ONLY the
        -- classifier below.
        artifact_candidate_counts AS (
            SELECT
                b.canonical_artifact_id AS artifact_id,
                COUNT(DISTINCT b.candidate_id) AS candidate_count
            FROM canonical_candidate_bindings b
            JOIN group_member_artifacts gma
              ON gma.artifact_id = b.canonical_artifact_id
            GROUP BY b.canonical_artifact_id
        ),
        -- MOVABLE artifacts: an operation-CLASSIFICATION SUBSET of
        -- group_member_artifacts (current authoritative, non-blocked, non-
        -- standby -- inherited, never redefined here). Additionally in a
        -- switchable lifecycle state (the same _SWITCHABLE_STATES_SQL
        -- classification group_remaining_counts already uses) and backed by
        -- at least two distinct canonical candidate identities -- the same
        -- bar generic Details acquisition_candidates.switch_eligible uses
        -- (transfers/repository.py). This NEVER feeds group membership,
        -- common_candidate_count, or which siblings exist -- it is consulted
        -- ONLY by the classifier below.
        movable_artifacts AS (
            SELECT
                gma.transfer_id AS transfer_id,
                gma.artifact_id AS artifact_id,
                acc.candidate_count AS candidate_count
            FROM group_member_artifacts gma
            JOIN artifact_candidate_counts acc
              ON acc.artifact_id = gma.artifact_id
            WHERE LOWER(TRIM(COALESCE(gma.status, ''))) IN ({_SWITCHABLE_STATES_SQL})
              AND acc.candidate_count >= 2
        ),
        movable_artifact_counts AS (
            SELECT transfer_id, COUNT(*) AS movable_count
            FROM movable_artifacts
            GROUP BY transfer_id
        ),
        -- Deterministic single-movable-artifact identity/count. Only ever
        -- meaningful in Python when movable_artifact_counts.movable_count = 1
        -- for that transfer (enforced in Python, not here): MIN/MAX over a
        -- one-row group is then exact, never a guess.
        single_movable_artifact AS (
            SELECT
                transfer_id,
                MIN(artifact_id) AS artifact_id,
                MAX(candidate_count) AS candidate_count
            FROM movable_artifacts
            GROUP BY transfer_id
        ),
        -- Strict common targets (group_true_common_hosts -- UNTOUCHED, the
        -- exact same set group_common_sources counts) that are additionally
        -- actionable AND movement-producing -- the identical predicate
        -- group_actionable_common_sources already uses (reused, not re-
        -- derived) -- kept at HOST granularity so they can be counted once
        -- per normalized host instead of collapsed to one existence row per
        -- transfer. group_actionable_common_sources itself is untouched;
        -- this is a purely additive COUNT extension for the classifier.
        group_actionable_common_source_hosts AS (
            SELECT DISTINCT common.transfer_id, common.host
            FROM group_true_common_hosts common
            JOIN group_remaining_host_movement movement
              ON movement.transfer_id = common.transfer_id
             AND movement.host = common.host
             {disabled_provider_clause}
            LEFT JOIN group_host_vetoes veto
              ON veto.transfer_id = common.transfer_id
             AND veto.host = common.host
            WHERE veto.transfer_id IS NULL
        ),
        group_actionable_common_source_counts AS (
            SELECT transfer_id, COUNT(*) AS actionable_count
            FROM group_actionable_common_source_hosts
            GROUP BY transfer_id
        )
        SELECT
            t.id,
            CASE WHEN t.hash LIKE 'deleted:%' THEN COALESCE(t.source_fingerprint, '') ELSE t.hash END AS hash,
            t.name,
            t.status,
            t.size_bytes,
            t.progress,
            t.source,
            t.label,
            t.error_message,
            t.extraction_status,
            t.extraction_error,
            t.created_at,
            t.updated_at,
            t.completed_at,
            COALESCE(request_failures.failure_count, 0) AS source_failure_count,
            COALESCE(group_common_sources.common_candidate_count, 0) AS common_candidate_count,
            CASE
                WHEN group_actionable_common_sources.transfer_id IS NULL THEN 0
                ELSE COALESCE(group_remaining_counts.remaining_count, 0)
            END AS group_remaining_count,
            group_member_filenames.filenames AS _group_member_filenames,
            latest_route.provider_id AS current_provider_id,
            CASE
                WHEN COALESCE(delivery.provider_count, 0) = 1
                THEN delivery.provider_id
                ELSE NULL
            END AS delivering_provider_id,
            CASE
                WHEN COALESCE(delivery.provider_count, 0) > 0 THEN 'recorded'
                WHEN t.status = 'completed' THEN 'unknown_legacy'
                ELSE 'pending'
            END AS provider_provenance_status,
            current_root_resource.resource_state AS _current_root_resource_state,
            artifact_presentation_facts.artifacts AS _artifact_presentation_facts,
            CASE WHEN input_challenge.transfer_id IS NOT NULL THEN 1 ELSE 0 END AS _has_input_challenge,
            COALESCE(pause_intent.paused, 0) AS _paused_intent,
            root_request.payload AS _source_request_payload,
            delivered_source.candidate_source AS _delivered_candidate_source,
            active_source.candidate_source AS _active_candidate_source,
            latest_route.candidate_summary AS _route_candidate_summary,
            COALESCE(movable_artifact_counts.movable_count, 0) AS _movable_artifact_count,
            single_movable_artifact.artifact_id AS _single_movable_artifact_id,
            single_movable_artifact.candidate_count AS _single_movable_artifact_candidate_count,
            COALESCE(group_actionable_common_source_counts.actionable_count, 0) AS _actionable_common_target_count,
            page_current_file_selection.manifest_id AS _file_selection_manifest_id,
            page_current_file_selection.decision AS _file_selection_decision,
            page_current_file_selection.manifest_committed_at AS _file_selection_committed_at,
            COALESCE(page_file_selection_manifest_counts.manifest_entry_count, 0) AS _file_selection_entry_count
        FROM page
        JOIN torrents t
          ON t.id = page.id
        LEFT JOIN current_root_resource
          ON current_root_resource.transfer_id = t.id
        LEFT JOIN artifact_presentation_facts
          ON artifact_presentation_facts.transfer_id = t.id
        LEFT JOIN input_challenge
          ON input_challenge.transfer_id = t.id
        LEFT JOIN transfer_pause_intents pause_intent
          ON pause_intent.torrent_id = t.id
        LEFT JOIN latest_route
          ON latest_route.transfer_id = t.id
        LEFT JOIN delivery
          ON delivery.transfer_id = t.id
        LEFT JOIN delivered_source
          ON delivered_source.transfer_id = t.id
        LEFT JOIN active_source
          ON active_source.transfer_id = t.id
        LEFT JOIN root_request
          ON root_request.transfer_id = t.id
        LEFT JOIN request_failures
          ON request_failures.transfer_id = t.id
        LEFT JOIN group_common_sources
          ON group_common_sources.transfer_id = t.id
        LEFT JOIN group_remaining_counts
          ON group_remaining_counts.transfer_id = t.id
        LEFT JOIN group_actionable_common_sources
          ON group_actionable_common_sources.transfer_id = t.id
        LEFT JOIN group_member_filenames
          ON group_member_filenames.transfer_id = t.id
        LEFT JOIN movable_artifact_counts
          ON movable_artifact_counts.transfer_id = t.id
        LEFT JOIN single_movable_artifact
          ON single_movable_artifact.transfer_id = t.id
        LEFT JOIN group_actionable_common_source_counts
          ON group_actionable_common_source_counts.transfer_id = t.id
        LEFT JOIN page_current_file_selection
          ON page_current_file_selection.transfer_id = t.id
        LEFT JOIN page_file_selection_manifest_counts
          ON page_file_selection_manifest_counts.manifest_id = page_current_file_selection.manifest_id
        ORDER BY t.created_at DESC
    """

    async with get_db() as db:
        rows = await db.fetchall(query, query_params)
        total_row = await db.fetchone(
            f"SELECT COUNT(*) AS cnt FROM torrents t {where}", params
        )
        total = total_row["cnt"] if total_row else 0

    # Section 9: read once for the whole page, never per row. This is a
    # plain passthrough to the execution-admission owner's own positive
    # record (transfers.convergence_engine.TransferEngine
    # .capacity_only_blocked_ids) -- the same live fact the comprehensive
    # Details path reads via api.routes.get_torrent -- never a
    # presentation-side reconstruction from durable columns.
    capacity_only_blocked_ids = live_admission.capacity_only_blocked_ids(getattr(application, "engine", None))

    items = []
    for row in rows:
        projected = dict(row)
        current_root_resource_state = projected.pop("_current_root_resource_state", None)
        paused = bool(int(projected.pop("_paused_intent", 0) or 0))
        input_required = bool(int(projected.pop("_has_input_challenge", 0) or 0))
        file_presentations = _bounded_child_presentations(
            projected.pop("_artifact_presentation_facts", None),
            paused=paused, input_required=input_required,
            capacity_only_blocked_ids=capacity_only_blocked_ids,
        )
        source_identity = _bounded_source_identity(projected)
        common_candidate_count = max(0, int(projected.get("common_candidate_count") or 0))
        group_remaining_count = max(0, int(projected.get("group_remaining_count") or 0))
        raw_filenames = _decode_projection_value(projected.pop("_group_member_filenames", None), [])
        artifact_filenames = raw_filenames if isinstance(raw_filenames, list) else []
        # A torrent/magnet submission's root name is its real canonical
        # identity (durable submission-kind fact, from the same
        # source_identity the icon already uses — never inferred from
        # filename shape/count/provider). It must win over any single member
        # artifact filename (Workstream C name-regression correction).
        display_name = normalized_transfer_display_name(
            artifact_filenames,
            root_name=projected.get("name"),
            root_is_canonical_identity=source_identity.get("kind") in {"magnet", "torrent_file"},
        )
        # Candidate-action scope classifier (DP 1.0.12 Contextual Candidate
        # Action Scope task, §4/§10): orthogonal to common_candidate_count/
        # group_remaining_count above -- a THIRD read fact, never a
        # replacement. Precedence is intentional and must not be reordered:
        # exactly one movable artifact always wins over a group target, even
        # when a (necessarily disjoint, since STRICT_COMMON_TARGETS is never
        # derived from the movable subset) group target also exists.
        movable_artifact_count = int(projected.pop("_movable_artifact_count", 0) or 0)
        single_movable_artifact_id = projected.pop("_single_movable_artifact_id", None)
        single_movable_artifact_candidate_count = projected.pop(
            "_single_movable_artifact_candidate_count", None
        )
        actionable_common_target_count = int(
            projected.pop("_actionable_common_target_count", 0) or 0
        )
        file_selection_manifest_id = projected.pop("_file_selection_manifest_id", None)
        file_selection_decision = projected.pop("_file_selection_decision", None)
        file_selection_committed_at = projected.pop("_file_selection_committed_at", None)
        file_selection_entry_count = int(projected.pop("_file_selection_entry_count", 0) or 0)
        if movable_artifact_count == 1:
            candidate_action_scope = "artifact"
            candidate_action_count = int(single_movable_artifact_candidate_count or 0)
            candidate_action_artifact_id = (
                int(single_movable_artifact_id) if single_movable_artifact_id is not None else None
            )
        elif movable_artifact_count > 1 and actionable_common_target_count > 0:
            candidate_action_scope = "group"
            candidate_action_count = actionable_common_target_count
            candidate_action_artifact_id = None
        else:
            candidate_action_scope = "none"
            candidate_action_count = 0
            candidate_action_artifact_id = None
        for field in _SOURCE_PROJECTION_FIELDS:
            projected.pop(field, None)
        item = _public_transfer_presentation(projected, application.definitions)
        item["current_source_identity"] = source_identity
        # Transfer-level common-source MEMBERSHIP summary. ``common_candidate_count``
        # is the number of canonical hosts common to every current authoritative
        # artifact of the transfer — the raw intersection, independent of
        # switch-eligibility or artifact state. The group Candidates launcher
        # appears only when this is 2 or more; whether any given common host is
        # currently actionable is a separate fact derived lazily from Details
        # when the chooser opens, never encoded in this bounded field. Feeds
        # Downloads and Dashboard Recent Items identically.
        item["common_candidate_count"] = common_candidate_count
        # Remaining-work membership count (Section 10): 0 means the list
        # surfaces render the common-source indicator as static history
        # instead of an interactive launcher. Independent of actionability.
        item["group_remaining_count"] = group_remaining_count
        # Canonical human-facing transfer title (Section 15-20; narrowed by
        # the DP 1.0.12 Workstream C name-regression correction). Dashboard
        # Recent and Downloads both render this SAME field — no per-surface
        # normalization duplication.
        item["display_name"] = display_name
        # Candidate-action scope/count/target (Section 3): the smallest
        # unambiguous candidate-switch operation scope the operator can act
        # on right now. Orthogonal to common_candidate_count/
        # group_remaining_count above -- never overwrites them, never derived
        # from them, never derived from a narrowed "movable" membership set.
        item["candidate_action_scope"] = candidate_action_scope
        item["candidate_action_count"] = candidate_action_count
        item["candidate_action_artifact_id"] = candidate_action_artifact_id
        # File-selection affordance hint (Section 10): an AFFORDANCE HINT
        # only, never mutation authority -- the browser always performs one
        # fresh authoritative GET .../file-selection read on click before
        # opening any picker (Section 6.8).
        item["file_selection_affordance"] = _file_selection_affordance(
            file_selection_manifest_id,
            file_selection_decision,
            file_selection_committed_at,
            file_selection_entry_count,
        )
        # Effective processing presentation via the ONE shared owner
        # (transfers.presentation_repository.effective_presentation), fed the same
        # logical inputs as the comprehensive Details projection: the durable
        # transfer status, the per-artifact child presentations, the pause /
        # input-required signals, and the state of the CURRENT authoritative root
        # provider-resource binding. The durable ``status`` is never mutated;
        # Dashboard, Downloads and Details cannot disagree for the same facts.
        item.update(effective_presentation(
            str(projected.get("status") or ""),
            file_presentations,
            paused=paused,
            input_required=input_required,
            current_resource_state=current_root_resource_state,
        ))
        items.append(item)
    if activity_mode:
        items = _apply_activity_priority_order(items)
    return {"items": items, "total": total}
