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
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from api.routes import _public_transfer_presentation
from api.serializers import public_payload
from application.dependencies import get_application
from application.manual_candidate_failover import switch_candidate
from application.service import ApplicationService
from db.database import get_db
from transfers import codec
from transfers.errors import Category, TransferError
from transfers.presentation_repository import public_source_identity

router = APIRouter()


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
                OR LOWER(COALESCE(t.hash, '')) LIKE ?
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
        -- Transfer-level multi-source summary derived only from canonical
        -- acquisition-candidate storage. Eligibility mirrors the detail
        -- candidate projection (physical, unblocked, non-standby artifacts).
        -- Details candidate cardinality is per artifact; artifacts of one
        -- transfer may legitimately carry different candidate-set sizes, so the
        -- truthful transfer-level summary is the largest per-artifact distinct
        -- candidate count, never the sum. This stays inside the one bounded
        -- projection read: no per-row query, no comprehensive presentation.
        candidate_cardinality AS (
            SELECT
                artifact.transfer_id,
                MAX(artifact.candidate_sources) AS candidate_source_max
            FROM (
                SELECT
                    f.torrent_id AS transfer_id,
                    f.id AS artifact_id,
                    COUNT(DISTINCT b.candidate_id) AS candidate_sources
                FROM download_files f
                JOIN page
                  ON page.id = f.torrent_id
                LEFT JOIN canonical_candidate_bindings b
                  ON b.canonical_artifact_id = f.id
                WHERE f.request_id IS NOT NULL
                  AND COALESCE(f.blocked, 0) = 0
                  AND COALESCE(f.mirror_state, '') != 'standby'
                GROUP BY f.torrent_id, f.id
            ) artifact
            GROUP BY artifact.transfer_id
        )
        SELECT
            t.id,
            t.hash,
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
            COALESCE(candidate_cardinality.candidate_source_max, 0) AS candidate_source_max,
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
            root_request.payload AS _source_request_payload,
            delivered_source.candidate_source AS _delivered_candidate_source,
            active_source.candidate_source AS _active_candidate_source,
            latest_route.candidate_summary AS _route_candidate_summary
        FROM page
        JOIN torrents t
          ON t.id = page.id
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
        LEFT JOIN candidate_cardinality
          ON candidate_cardinality.transfer_id = t.id
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
        source_identity = _bounded_source_identity(projected)
        candidate_source_max = max(0, int(projected.get("candidate_source_max") or 0))
        for field in _SOURCE_PROJECTION_FIELDS:
            projected.pop(field, None)
        item = _public_transfer_presentation(projected, application.definitions)
        item["current_source_identity"] = source_identity
        # Passive multi-source indicator: the transfer genuinely exposes more
        # than one equivalent canonical acquisition candidate only when this is
        # greater than 1. Always present as a plain non-negative integer.
        item["candidate_source_max"] = candidate_source_max
        items.append(item)
    return {"items": items, "total": total}
