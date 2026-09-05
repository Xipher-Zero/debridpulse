"""Safe browser source-identity projection over the canonical transfer repository.

This extension owns no lifecycle or persistence state. It projects one neutral,
non-capability-bearing source identity from the durable root request type and the
already-persisted candidate provenance. Raw request URLs and provider-native
execution details never become browser presentation data.
"""
from __future__ import annotations

import re

from db.database import get_db
from transfers import codec
from transfers.repository import TransferRepository as _CanonicalTransferRepository


_TORRENT_REQUEST_KINDS = frozenset({"torrent", "torrent_file", "file"})
_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _public_host(value) -> str | None:
    host = str(value or "").strip().lower().removeprefix("www.").rstrip(".")
    if not host or len(host) > 253:
        return None
    labels = host.split(".")
    if any(not label or not _HOST_LABEL_RE.fullmatch(label) for label in labels):
        return None
    return host


def _candidate_source(value) -> dict[str, str] | None:
    if not isinstance(value, dict) or str(value.get("scope") or "").strip().lower() != "host":
        return None
    host = _public_host(value.get("key"))
    return {"kind": "host", "host": host} if host else None


def public_source_identity(request_kind, candidate_source=None) -> dict[str, str]:
    """Project only a safe source identity, with root request-type precedence."""
    kind = str(request_kind or "").strip().lower()
    if kind == "magnet":
        return {"kind": "magnet"}
    if kind in _TORRENT_REQUEST_KINDS:
        return {"kind": "torrent_file"}
    if kind in {"http", "https"}:
        return _candidate_source(candidate_source) or {"kind": "link"}
    return {"kind": "link"}


def _decode_source(value):
    try:
        return codec.load(value, None)
    except (TypeError, ValueError, KeyError):
        return None


class TransferRepository(_CanonicalTransferRepository):
    """Canonical production repository plus one safe presentation-only field."""

    async def presentation(self, transfer_id: int, details: bool = False):
        result = await super().presentation(transfer_id, details=details)
        if not result:
            return result

        request_kind = ""
        candidate_source = None
        async with get_db() as db:
            root = await db.fetchone(
                """SELECT payload FROM transfer_requests
                    WHERE transfer_id=? AND parent_id IS NULL
                    ORDER BY ordinal,id LIMIT 1""",
                (transfer_id,),
            )
            if root is None:
                root = await db.fetchone(
                    """SELECT payload FROM transfer_requests
                        WHERE transfer_id=? ORDER BY ordinal,id LIMIT 1""",
                    (transfer_id,),
                )
            if root and root.get("payload"):
                try:
                    request_kind = str(codec.request(codec.load(root["payload"])).kind or "").strip().lower()
                except (TypeError, ValueError, KeyError):
                    request_kind = ""

            # Magnet/torrent identities are dictated by the root request and must
            # not be replaced by provider-generated HTTP descendants.
            if request_kind not in {"magnet", *_TORRENT_REQUEST_KINDS}:
                if str(result.get("status") or "").strip().lower() == "completed":
                    row = await db.fetchone(
                        """SELECT p.candidate_source FROM execution_attempt_provenance p
                            JOIN execution_attempts e ON e.id=p.execution_attempt_id
                            WHERE p.transfer_id=? AND p.delivered=1
                            ORDER BY e.updated_at DESC,p.ordinal DESC,e.id DESC LIMIT 1""",
                        (transfer_id,),
                    )
                    if row:
                        candidate_source = _decode_source(row.get("candidate_source"))

                if _candidate_source(candidate_source) is None:
                    row = await db.fetchone(
                        """SELECT p.candidate_source FROM download_files f
                            JOIN execution_attempt_provenance p ON p.execution_attempt_id=f.execution_attempt_id
                            WHERE f.torrent_id=? AND f.execution_attempt_id IS NOT NULL
                              AND COALESCE(f.mirror_state,'')!='standby'
                            ORDER BY f.updated_at DESC,p.ordinal DESC,f.id DESC LIMIT 1""",
                        (transfer_id,),
                    )
                    if row:
                        candidate_source = _decode_source(row.get("candidate_source"))

                if _candidate_source(candidate_source) is None:
                    row = await db.fetchone(
                        """SELECT candidate_summary FROM route_attempt_provenance
                            WHERE transfer_id=? ORDER BY ordinal DESC,updated_at DESC LIMIT 1""",
                        (transfer_id,),
                    )
                    if row:
                        try:
                            candidates = codec.load(row.get("candidate_summary"), [])
                        except (TypeError, ValueError, KeyError):
                            candidates = []
                        if isinstance(candidates, list):
                            for candidate in candidates:
                                source = candidate.get("source") if isinstance(candidate, dict) else None
                                if _candidate_source(source) is not None:
                                    candidate_source = source
                                    break

        result["current_source_identity"] = public_source_identity(request_kind, candidate_source)
        return result
