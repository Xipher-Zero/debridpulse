"""Manual-candidate-failover persistence.

The active-source and per-candidate failover projection is part of the one
``presentation()`` owner in ``transfers.presentation_repository``."""
from __future__ import annotations

import json

from db.database import get_db
from transfers.presentation_repository import (
    TransferRepository as _PresentationRepository,
    _candidate_source,
)


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
    """Presentation owner plus durable operator failover provenance."""

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
