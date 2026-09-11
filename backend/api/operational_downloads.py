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
from application.dependencies import get_application
from application.manual_candidate_failover import switch_candidate
from application.service import ApplicationService
from db.database import get_db
from transfers import codec
from transfers.display_name import normalized_transfer_display_name
from transfers.errors import Category, TransferError
from transfers.presentation_repository import (
    ARTIFACT_PRESENTATION_SNAPSHOT_KEYS,
    effective_presentation,
    public_source_identity,
    recovery_presentation,
)
from transfers.repository import _SWITCHABLE_ARTIFACT_STATES

router = APIRouter()

# The bounded list's remaining-work signal (Section 10 of the DP 1.0.12
# presentation task) reuses the SAME cheap artifact-state classification the
# comprehensive Details presentation already uses for its own read-time
# ``switch_eligible`` approximation (transfers/repository.py
# ``_SWITCHABLE_ARTIFACT_STATES``, mirrored in manual_repository.py and
# manual_failover.py) rather than a fourth hand-copied literal list. A
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
    """
    registry = getattr(getattr(application, "engine", None), "registry", None)
    providers = getattr(registry, "providers", None)
    if not providers:
        return frozenset()
    return frozenset(
        provider_id
        for provider_id, provider in providers.items()
        if not getattr(getattr(provider, "descriptor", None), "enabled", True)
    )


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


def _bounded_child_presentations(raw_facts, *, paused, input_required):
    """Project the page's per-artifact child presentations for the shared owner.

    ``raw_facts`` is the JSON array the bounded projection built from raw durable
    facts (each artifact's status plus its latest recovery-snapshot fields). Each
    entry is passed straight through ``recovery_presentation`` — the same shared
    per-artifact projector the comprehensive Details path uses — so
    ``effective_presentation`` aggregates identical child truth on both surfaces.
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
        presentations.append(recovery_presentation(
            fact.get("status"), context, paused=paused, input_required=input_required,
        ))
    return presentations


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


@router.get("/torrents")
async def list_operational_torrents(
    status: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(0, ge=0, le=5000),
    offset: int = 0,
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
        -- Per-artifact presentation facts for the page, folded into the one
        -- bounded read as a JSON array per transfer (one row per transfer, no
        -- per-row query, no comprehensive presentation call). ONLY raw durable
        -- facts are projected here — the artifact's own status plus the fields
        -- of its latest durable recovery snapshot; no presentation or precedence
        -- logic lives in SQL. In Python these feed the SHARED pure owner
        -- transfers.presentation_repository.effective_presentation exactly as the
        -- comprehensive Details projection feeds it, so the bounded list can
        -- never derive a processing truth that disagrees with Details.
        artifact_presentation_facts AS (
            SELECT
                f.torrent_id AS transfer_id,
                json_group_array(json_object(
                    'status', f.status,
                    'quiescence_reason', json_extract(snap.detail, '$.quiescence_reason'),
                    'decision_action', json_extract(snap.detail, '$.decision_action'),
                    'last_applied_action', json_extract(snap.detail, '$.last_applied_action'),
                    'decision_reason', json_extract(snap.detail, '$.decision_reason'),
                    'last_applied_reason', json_extract(snap.detail, '$.last_applied_reason'),
                    'wake_condition', json_extract(snap.detail, '$.wake_condition'),
                    'recovery_claim_token', json_extract(snap.detail, '$.recovery_claim_token')
                )) AS artifacts
            FROM download_files f
            JOIN page ON page.id = f.torrent_id
            LEFT JOIN application_events snap
              ON snap.id = (
                SELECT ae.id FROM application_events ae
                WHERE ae.kind = 'transfer_recovery:' || f.id
                ORDER BY ae.id DESC LIMIT 1
              )
            GROUP BY f.torrent_id
        ),
        input_challenge AS (
            SELECT DISTINCT c.transfer_id
            FROM transfer_input_challenges c JOIN page ON page.id = c.transfer_id
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
            WHERE f.request_id IS NOT NULL
              AND COALESCE(f.blocked, 0) = 0
              AND COALESCE(f.mirror_state, '') != 'standby'
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
            latest_route.candidate_summary AS _route_candidate_summary
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
        ORDER BY t.created_at DESC
    """

    async with get_db() as db:
        rows = await db.fetchall(query, query_params)
        total_row = await db.fetchone(
            f"SELECT COUNT(*) AS cnt FROM torrents t {where}", params
        )
        total = total_row["cnt"] if total_row else 0

    items = []
    for row in rows:
        projected = dict(row)
        current_root_resource_state = projected.pop("_current_root_resource_state", None)
        paused = bool(int(projected.pop("_paused_intent", 0) or 0))
        input_required = bool(int(projected.pop("_has_input_challenge", 0) or 0))
        file_presentations = _bounded_child_presentations(
            projected.pop("_artifact_presentation_facts", None),
            paused=paused, input_required=input_required,
        )
        source_identity = _bounded_source_identity(projected)
        common_candidate_count = max(0, int(projected.get("common_candidate_count") or 0))
        group_remaining_count = max(0, int(projected.get("group_remaining_count") or 0))
        raw_filenames = _decode_projection_value(projected.pop("_group_member_filenames", None), [])
        artifact_filenames = raw_filenames if isinstance(raw_filenames, list) else []
        display_name = normalized_transfer_display_name(
            artifact_filenames, root_name=projected.get("name"),
        )
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
        # Canonical human-facing transfer title (Section 15-20): normalized
        # artifact-derived name first, root/request ``name`` fallback only.
        # Dashboard Recent and Downloads both render this SAME field — no
        # per-surface normalization duplication.
        item["display_name"] = display_name
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
    return {"items": items, "total": total}
