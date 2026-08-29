"""Transient validation and editable Settings helper endpoints.

Validation routes deliberately test candidate connection values without
persisting or applying them. The extraction-password route is a narrow Settings
read surface for the operator-maintained archive-password list; operational
credentials remain write-only through the normal public Settings payload.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.config import get_settings
from core.logging_utils import sanitize_exception
from services.alldebrid import AllDebridService
from services.aria2 import Aria2Service
from services.notifications import NotificationService
from services.transfer_service import transfer_service


router = APIRouter()


class AllDebridValidationRequest(BaseModel):
    api_key: str = Field(default="", max_length=4096)
    clear_api_key: bool = False


class Aria2ValidationRequest(BaseModel):
    mode: Literal["builtin", "external"]
    url: str = Field(default="", max_length=4096)
    secret: str = Field(default="", max_length=4096)
    clear_secret: bool = False


class DiscordValidationRequest(BaseModel):
    webhook_url: str = Field(default="", max_length=8192)
    clear_webhook: bool = False


def _safe_failure(exc: Exception) -> str:
    return sanitize_exception(exc, max_length=200)


@router.get("/settings/extraction-passwords")
async def get_extraction_passwords():
    """Return the operator-maintained archive-password list for editing.

    Archive passwords are content-unlock data rather than operational service
    credentials. The general Settings response continues to redact this field;
    only this purpose-built Settings surface returns the actual newline list.
    """
    return {"passwords": str(get_settings().extraction_password or "")}


@router.post("/settings/validate-alldebrid")
async def validate_alldebrid(payload: AllDebridValidationRequest):
    cfg = get_settings()
    if payload.clear_api_key:
        api_key = ""
    else:
        api_key = payload.api_key.strip() or str(cfg.alldebrid_api_key or "").strip()
    if not api_key:
        raise HTTPException(400, "No API key configured or entered")

    try:
        service = AllDebridService(api_key, cfg.alldebrid_agent)
        user = await service.get_user()
        user_data = user.get("user", user)
        return {
            "ok": True,
            "username": user_data.get("username", ""),
            "isPremium": user_data.get("isPremium", False),
            "premiumUntil": user_data.get("premiumUntil", user_data.get("premium_until", 0)),
        }
    except Exception as exc:
        raise HTTPException(502, _safe_failure(exc)) from exc


@router.post("/settings/validate-aria2")
async def validate_aria2(payload: Aria2ValidationRequest):
    cfg = get_settings()
    try:
        if payload.mode == "builtin":
            if str(getattr(cfg, "aria2_mode", "external")) != "builtin":
                raise HTTPException(
                    400,
                    "Built-in aria2 starts after Apply Settings; apply the mode change before testing it",
                )
            result = await transfer_service.test_aria2()
        else:
            url = payload.url.strip() or str(cfg.aria2_url or "").strip()
            if not url:
                raise HTTPException(400, "No external aria2 RPC URL configured or entered")
            secret = "" if payload.clear_secret else (
                payload.secret.strip() or str(cfg.aria2_secret or "").strip()
            )
            service = Aria2Service(
                url,
                secret,
                getattr(cfg, "aria2_operation_timeout_seconds", 15),
            )
            result = await service.test()
        return {"ok": True, **result}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, _safe_failure(exc)) from exc


@router.post("/settings/validate-discord")
async def validate_discord(payload: DiscordValidationRequest):
    cfg = get_settings()
    if payload.clear_webhook:
        webhook_url = ""
    else:
        webhook_url = payload.webhook_url.strip() or str(cfg.discord_webhook_url or "").strip()
    if not webhook_url:
        raise HTTPException(400, "No Discord webhook configured or entered")

    try:
        sent = await NotificationService(webhook_url).test()
        if not sent:
            raise RuntimeError("Discord test did not send a notification")
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(502, _safe_failure(exc)) from exc
