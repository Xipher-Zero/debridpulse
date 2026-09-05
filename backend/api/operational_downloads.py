"""Operational Downloads read-model route.

The durable transfer row for a fully absorbed source remains queryable by its
explicit CONSOLIDATED lifecycle state, but the normal operational list excludes
it alongside soft-deleted history.  Pagination and totals therefore reflect the
same canonical lifecycle rule as the visible rows.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query

from api.routes import _public_transfer_presentation
from application.dependencies import get_application
from application.service import ApplicationService
from db.database import get_db

router = APIRouter()


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
