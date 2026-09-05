"""Safe browser source-identity projection over the canonical transfer repository.

This extension owns no lifecycle or persistence state. It projects one neutral,
non-capability-bearing source identity from the durable root transfer request
while the qualified repository remains the sole owner of transfer state.
"""
from __future__ import annotations

from urllib.parse import urlparse

from db.database import get_db
from transfers import codec
from transfers.repository import TransferRepository as _CanonicalTransferRepository


_TORRENT_REQUEST_KINDS = frozenset({"torrent", "torrent_file", "file"})


def public_root_source_identity(request) -> dict[str, str]:
    """Return only the root source type and, for HTTP(S), a normalized hostname."""
    if request is None:
        return {"kind": "link"}

    kind = str(getattr(request, "kind", "") or "").strip().lower()
    payload = getattr(request, "payload", "")

    if kind == "magnet" or (isinstance(payload, str) and payload.lower().startswith("magnet:?")):
        return {"kind": "magnet"}

    if isinstance(payload, (bytes, bytearray)) or kind in _TORRENT_REQUEST_KINDS:
        return {"kind": "torrent_file"}

    if kind in {"http", "https"} and isinstance(payload, str):
        parsed = urlparse(payload)
        host = str(parsed.hostname or "").strip().lower().rstrip(".")
        if host:
            return {"kind": "host", "host": host}

    return {"kind": "link"}


class TransferRepository(_CanonicalTransferRepository):
    """Canonical production repository plus one safe presentation-only field."""

    async def presentation(self, transfer_id: int, details: bool = False):
        result = await super().presentation(transfer_id, details=details)
        if not result:
            return result

        async with get_db() as db:
            row = await db.fetchone(
                """SELECT payload FROM transfer_requests
                    WHERE transfer_id=? AND parent_id IS NULL
                    ORDER BY ordinal,id LIMIT 1""",
                (transfer_id,),
            )
            if row is None:
                row = await db.fetchone(
                    """SELECT payload FROM transfer_requests
                        WHERE transfer_id=? ORDER BY ordinal,id LIMIT 1""",
                    (transfer_id,),
                )

        request = None
        if row and row.get("payload"):
            try:
                request = codec.request(codec.load(row["payload"]))
            except (TypeError, ValueError, KeyError):
                request = None
        result["current_source_identity"] = public_root_source_identity(request)
        return result
