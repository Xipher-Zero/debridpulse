"""Operational Downloads and Activity Log read-model routes.

The durable transfer row for a fully absorbed source remains queryable by its
explicit CONSOLIDATED lifecycle state, but the normal operational list excludes
it alongside soft-deleted history. Pagination and totals therefore reflect the
same canonical lifecycle rule as the visible rows.

Activity Log filtering is owned here so search, severity, and rolling timeframe
constraints are applied before the hard result ceiling. The legacy /events route
is removed from the generic router at import time, preserving the public path
without retaining two long-term owners.
"""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query

from api.routes import _public_transfer_presentation, router as legacy_router
from application.dependencies import get_application
from application.service import ApplicationService
from db.database import get_db

router = APIRouter()


# The operational read model supersedes the legacy unfiltered Activity Log GET.
# Keep every unrelated generic route registered exactly as before.
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
EventTimeframe = Literal["1h", "12h", "24h", "72h", "7d", "30d", "all"]
EventLevel = Literal["info", "warn", "warning", "error"]


@router.get("/events")
async def list_activity_events(
    search: Optional[str] = None,
    level: Optional[EventLevel] = None,
    timeframe: EventTimeframe = "all",
    limit: int = Query(500, ge=1, le=500),
):
    """Return newest matching events after applying all Activity Log filters.

    ``limit + 1`` is intentionally fetched after every predicate so truncation
    is explicit and an exact-limit result is never mistaken for truncation.
    ``instr`` preserves literal browser substring semantics for ``%`` and ``_``
    while keeping all user text parameterized.
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
        query = f"""
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
        """
        rows = await db.fetchall(query, [*params, limit + 1])
        truncated = len(rows) > limit
        return {
            "items": rows[:limit],
            "truncated": truncated,
            "limit": limit,
        }


@router.get("/torrents")
async def list_operational_torrents(
    status: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(0, ge=0, le=5000),
    offset: int = 0,
    application: ApplicationService = Depends(get_application),
):
    async with get_db() as db:
        clauses = []
        params = []

        if status:
            # Explicit lifecycle queries retain access to durable history,
            # including status=consolidated for provenance/details tooling.
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
        query = f"""SELECT t.*,
                (SELECT COUNT(*) FROM download_files WHERE torrent_id=t.id) as file_count,
                (SELECT COUNT(*) FROM download_files WHERE torrent_id=t.id AND blocked=1) as blocked_count
                FROM torrents t {where}
                ORDER BY t.created_at DESC"""
        query_params = list(params)
        if limit > 0:
            query += " LIMIT ? OFFSET ?"
            query_params.extend([limit, offset])

        rows = await db.fetchall(query, query_params)
        total_row = await db.fetchone(
            f"SELECT COUNT(*) AS cnt FROM torrents t {where}", params
        )
        total = total_row["cnt"] if total_row else 0
        return {
            "items": [
                _public_transfer_presentation(
                    await application.repository.presentation(row["id"]),
                    application.definitions,
                )
                for row in rows
            ],
            "total": total,
        }
