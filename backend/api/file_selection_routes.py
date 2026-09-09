"""Dedicated universal file-selection API (specification sections 35-40).

Kept out of the large generic route file. The browser-visible responses carry
only the neutral Section 38 facts the selector needs: manifest / entry ids,
sanitized names and paths, expected sizes, decision + mutability state, the
persisted deadlines, the initial-availability fact, and ``server_now``.

Internal provenance never crosses this boundary. The durable read model keeps
``selection_id`` / ``provider_resource_id`` / ``request_id`` for core use, but
this layer projects a fixed public whitelist, so a new internal field cannot
leak. The browser identifies the mutable selection state by transfer context
(the URL) plus the canonical ``manifest_id``; Confirm and Dismiss use
``manifest_id`` as the stale-generation authority. A re-resolution onto a new
provider resource produces a fresh generation and the offers / read model
surface only that current one.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from application.dependencies import get_application
from application.service import ApplicationService
from transfers import file_selection as fs

router = APIRouter()

_MAX_ENTRY_ID = 128

# The only keys allowed to cross the HTTP/browser boundary.
_PUBLIC_SELECTION_FIELDS = (
    "eligible", "mutable", "manifest_id", "decision", "decision_reason",
    "file_count", "total_size_bytes", "entries", "selected_entry_ids",
    "auto_offer", "auto_offer_until", "decision_deadline", "initially_available",
    "server_now",
)
_PUBLIC_OFFER_FIELDS = (
    "transfer_id", "manifest_id", "file_count", "decision_deadline", "auto_offer_until",
)
# Each public entry exposes only these neutral fields (entry_id is a
# core-generated UUIDv5, not a provider or execution identifier).
_PUBLIC_ENTRY_FIELDS = ("entry_id", "name", "relative_path", "size_bytes")


def _public_selection_view(view: dict) -> dict:
    projected = {key: view[key] for key in _PUBLIC_SELECTION_FIELDS if key in view}
    projected["entries"] = [
        {key: entry[key] for key in _PUBLIC_ENTRY_FIELDS if key in entry}
        for entry in view.get("entries", [])
    ]
    return projected


def _public_offer(offer: dict) -> dict:
    return {key: offer[key] for key in _PUBLIC_OFFER_FIELDS if key in offer}


class ConfirmRequest(BaseModel):
    manifest_id: str = Field(min_length=1, max_length=_MAX_ENTRY_ID)
    entry_ids: list[Annotated[str, Field(min_length=1, max_length=_MAX_ENTRY_ID)]] = Field(
        min_length=1, max_length=fs.MAX_SELECTION_ENTRIES,
    )


class DismissRequest(BaseModel):
    manifest_id: str = Field(min_length=1, max_length=_MAX_ENTRY_ID)


def _raise_for(result: "fs.SelectionCommandResult") -> None:
    """Map a neutral command outcome to the specification's transport codes."""
    if result.outcome == fs.SelectionOutcome.NOT_FOUND:
        raise HTTPException(status_code=404, detail="Transfer not found")
    if result.outcome == fs.SelectionOutcome.CONFLICT:
        raise HTTPException(status_code=409, detail=result.detail or "Selection is no longer mutable")
    if result.outcome == fs.SelectionOutcome.INVALID:
        raise HTTPException(status_code=422, detail=result.detail or "Invalid file selection")


@router.get("/file-selections/offers")
async def list_file_selection_offers(
    application: ApplicationService = Depends(get_application),
):
    """Currently auto-presentable multi-file offers, for cold-load / reconnect
    recovery. Bounded to live pending generations; no deadline is restarted."""
    offers = await application.file_selection_offers()
    return {"offers": [_public_offer(offer) for offer in offers]}


@router.get("/torrents/{transfer_id}/file-selection")
async def get_file_selection(
    transfer_id: int,
    application: ApplicationService = Depends(get_application),
):
    """Safe idempotent read model. The browser queries this on every Details
    open, so a transfer with no file-selection generation — the common case,
    and an unknown transfer id alike — is reported as ``{"eligible": false}``
    with 200 rather than 404. The response is identical for both, so transfer
    existence is not disclosed. Confirm and Dismiss keep their 404 for a
    missing transfer, where "not found" is a real mutation error.
    """
    try:
        view = await application.file_selection(transfer_id)
    except KeyError:
        return {"eligible": False}
    if view is None:
        return {"eligible": False}
    return _public_selection_view(view)


@router.post("/torrents/{transfer_id}/file-selection/confirm")
async def confirm_file_selection(
    transfer_id: int,
    body: ConfirmRequest,
    application: ApplicationService = Depends(get_application),
):
    try:
        result = await application.confirm_file_selection(
            transfer_id, body.manifest_id, body.entry_ids,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Transfer not found") from None
    _raise_for(result)
    return {
        "ok": True,
        "decision": result.decision,
        "manifest_id": result.manifest_id,
        "detail": result.detail,
    }


@router.post("/torrents/{transfer_id}/file-selection/dismiss")
async def dismiss_file_selection(
    transfer_id: int,
    body: DismissRequest,
    application: ApplicationService = Depends(get_application),
):
    try:
        result = await application.dismiss_file_selection(transfer_id, body.manifest_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Transfer not found") from None
    _raise_for(result)
    return {
        "ok": True,
        "decision": result.decision,
        "manifest_id": result.manifest_id,
        "detail": result.detail,
    }
