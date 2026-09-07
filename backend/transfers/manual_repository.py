"""Manual-candidate-failover persistence and authoritative active-source projection."""
from __future__ import annotations

import json

from db.database import get_db
from transfers import codec
from transfers.presentation_repository import (
    TransferRepository as _PresentationRepository,
    _candidate_source,
    public_source_identity,
)


_SWITCHABLE_STATES = frozenset({
    "pending", "processing", "ready", "queued", "downloading", "paused",
    "refresh_pending", "error",
})


def _safe_source(candidate) -> dict:
    source = getattr(candidate, "source_identity", None)
    if source is None:
        return {}
    projected = _candidate_source({
        "scope": str(getattr(source, "scope", "") or ""),
        "key": str(getattr(source, "key", "") or ""),
    })
    return {"scope": "host", "key": projected["host"]} if projected else {}


def _enum_value(value) -> str:
    return str(getattr(value, "value", value))


class TransferRepository(_PresentationRepository):
    """Qualified presentation owner plus durable operator failover provenance."""

    async def record_manual_candidate_failover(
        self,
        *,
        transfer_id: int,
        artifact_id: int,
        filename: str,
        requested_candidate_id: str,
        previous_candidate,
        selected_candidate,
        source_host: str,
        outcome: str,
        execution_transition: str,
        error,
    ) -> None:
        detail = {
            "reason": "USER_REQUESTED",
            "manual_reason": "USER_REQUESTED",
            "outcome": "success" if outcome == "success" else "failure",
            "artifact_id": int(artifact_id),
            "filename": str(filename or "artifact"),
            "requested_candidate_id": str(requested_candidate_id or ""),
            "execution_transition": str(execution_transition or "unchanged"),
        }
        if previous_candidate is not None:
            detail.update({
                "previous_candidate_id": str(previous_candidate.id),
                "previous_provider_id": str(previous_candidate.provider_id or ""),
                "previous_source": _safe_source(previous_candidate),
            })
        if selected_candidate is not None:
            detail.update({
                "selected_candidate_id": str(selected_candidate.id),
                "selected_provider_id": str(selected_candidate.provider_id or ""),
                "selected_source": _safe_source(selected_candidate),
            })
        if outcome == "success":
            detail["source_host"] = str(source_host or "source")
        if error is not None:
            detail["error"] = {
                "domain": _enum_value(error.domain),
                "category": _enum_value(error.category),
                "stage": _enum_value(error.stage),
            }

        async with get_db() as db:
            await db.execute(
                "INSERT INTO application_events(transfer_id,kind,detail) VALUES(?,?,?)",
                (int(transfer_id), "manual_candidate_failover", json.dumps(detail, separators=(",", ":"))),
            )
            await db.commit()

    async def presentation(self, transfer_id: int, details: bool = False):
        result = await super().presentation(transfer_id, details=details)
        if not result:
            return result

        request_kind = ""
        selected_source = None
        selected_provider = ""
        transitions = []
        async with get_db() as db:
            root = await db.fetchone(
                """SELECT payload FROM transfer_requests
                    WHERE transfer_id=? AND parent_id IS NULL ORDER BY ordinal,id LIMIT 1""",
                (transfer_id,),
            )
            if root is None:
                root = await db.fetchone(
                    "SELECT payload FROM transfer_requests WHERE transfer_id=? ORDER BY ordinal,id LIMIT 1",
                    (transfer_id,),
                )
            if root and root.get("payload"):
                try:
                    request_kind = str(codec.request(codec.load(root["payload"])).kind or "").strip().lower()
                except (TypeError, ValueError, KeyError):
                    request_kind = ""

            if str(result.get("status") or "").lower() != "completed":
                row = await db.fetchone(
                    """SELECT b.source_scope,b.source_key,b.provider_id
                        FROM download_files f
                        JOIN canonical_candidate_bindings b
                          ON b.canonical_artifact_id=f.id
                         AND b.candidate_order=f.selected_candidate+1
                        WHERE f.torrent_id=? AND COALESCE(f.mirror_state,'')!='standby'
                          AND f.status NOT IN ('completed','cancelled','duplicate')
                        ORDER BY CASE f.status WHEN 'downloading' THEN 0 WHEN 'paused' THEN 1 WHEN 'queued' THEN 2 ELSE 3 END,
                          f.updated_at DESC,f.id DESC LIMIT 1""",
                    (transfer_id,),
                )
                if row:
                    selected_source = {"scope": row.get("source_scope"), "key": row.get("source_key")}
                    selected_provider = str(row.get("provider_id") or "")

            if details:
                rows = await db.fetchall(
                    """SELECT detail,created_at FROM application_events
                        WHERE transfer_id=? AND kind='manual_candidate_failover' ORDER BY id""",
                    (transfer_id,),
                )
                for row in rows:
                    try:
                        item = json.loads(row.get("detail") or "{}")
                    except (TypeError, ValueError):
                        continue
                    if isinstance(item, dict):
                        item["created_at"] = row.get("created_at")
                        transitions.append(item)

        if _candidate_source(selected_source) is not None:
            result["current_source_identity"] = public_source_identity(request_kind, selected_source)
        if selected_provider:
            result["current_provider_id"] = selected_provider

        if details and isinstance(result.get("files"), list):
            for file in result["files"]:
                state = str(file.get("status") or "").lower()
                candidates = file.get("acquisition_candidates")
                if not isinstance(candidates, list):
                    continue
                for candidate in candidates:
                    if not isinstance(candidate, dict):
                        continue
                    selected = bool(candidate.get("is_selected"))
                    candidate["is_active"] = selected
                    # Historical execution failure is truthful provenance, not a
                    # permanent capability verdict. The write path revalidates the
                    # exact bound candidate/provider before switching, so the UI
                    # must not suppress a retry solely because an older attempt failed.
                    candidate["switch_eligible"] = (
                        not selected
                        and state in _SWITCHABLE_STATES
                    )
            result["manual_candidate_failovers"] = transitions
        return result
