"""Operational Downloads and Activity Log read-model routes.

The durable transfer row for a fully absorbed source remains queryable by its
explicit CONSOLIDATED lifecycle state, but the normal operational list excludes
it alongside soft-deleted history. Pagination and totals therefore reflect the
same canonical lifecycle rule as the visible rows.

Activity Log filtering lives here so optional search, severity, and timeframe
predicates are applied before the result ceiling. The legacy unfiltered GET is
removed from the generic router at import time so /api/events keeps one owner.
The default response remains the historical JSON list; the UI opts into metadata
when it needs an explicit truncation signal.
"""
import asyncio
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from api.routes import _public_transfer_presentation, router as legacy_router
from application.dependencies import get_application
from application.manual_candidate_failover import switch_candidate
from application.service import ApplicationService
from db.database import get_db
from transfers.errors import Category, TransferError

router = APIRouter()


legacy_router.routes[:] = [
    route
    for route in legacy_router.routes
    if not (
        getattr(route, "path", None) == "/events"
        and "GET" in (getattr(route, "methods", set()) or set())
    )
]

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
        items = rows[:limit]
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
            SELECT transfer_id, provider_id
            FROM (
                SELECT
                    p.transfer_id,
                    a.provider_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY p.transfer_id
                        ORDER BY p.ordinal DESC
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
        request_failures AS (
            SELECT
                r.transfer_id,
                COUNT(*) AS failure_count
            FROM transfer_requests r
            JOIN page
              ON page.id = r.transfer_id
            WHERE r.state = 'failed'
            GROUP BY r.transfer_id
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
            END AS provider_provenance_status
        FROM page
        JOIN torrents t
          ON t.id = page.id
        LEFT JOIN latest_route
          ON latest_route.transfer_id = t.id
        LEFT JOIN delivery
          ON delivery.transfer_id = t.id
        LEFT JOIN request_failures
          ON request_failures.transfer_id = t.id
        ORDER BY t.created_at DESC
    """

    async with get_db() as db:
        rows = await db.fetchall(query, query_params)
        total_row = await db.fetchone(
            f"SELECT COUNT(*) AS cnt FROM torrents t {where}", params
        )
        total = total_row["cnt"] if total_row else 0

    items = [
        _public_transfer_presentation(row, application.definitions)
        for row in rows
    ]
    return {"items": items, "total": total}
