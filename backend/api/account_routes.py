"""Accounts, Integration tokens and submitter demand over HTTP.

Every route here is classified in ``auth.permissions.ACTIONS``: the account
directory, credentials and tokens are Admin; an account's own password and
display name are its own; demand is listed and withdrawn by its owner (or by
an Admin, who may act on anyone's). The account owner (``auth.accounts``)
enforces every account invariant; the transfer core decides what withdrawn
demand means for each transfer.
"""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from application.dependencies import get_application
from application.service import ApplicationService
from auth import accounts
from auth.accounts import AccountRefused, Role
from auth.permissions import Capability, demand_owner, permits
from auth.throttle import personal_notification_test_rate_limiter
from core.logging_utils import sanitize_exception
from services import account_notifications
from services import notification_service

router = APIRouter()


def _refused(exc: AccountRefused) -> HTTPException:
    status = 404 if "does not exist" in str(exc) else 409
    return HTTPException(status_code=status, detail=str(exc))


def _actor(request: Request) -> str:
    return request.state.principal.account_id


class AccountCreate(BaseModel):
    kind: Literal["local", "integration"]
    role: Literal["admin", "user", "readonly"] | None = None
    username: str | None = Field(default=None, max_length=256)
    password: str | None = Field(default=None, max_length=4096)
    display_name: str = Field(default="", max_length=256)


class AccountUpdate(BaseModel):
    role: Literal["admin", "user", "readonly", "integration"] | None = None
    enabled: bool | None = None
    display_name: str | None = Field(default=None, max_length=256)


class PasswordReset(BaseModel):
    password: str = Field(min_length=1, max_length=4096)
    username: str | None = Field(default=None, max_length=256)


class OwnPassword(BaseModel):
    current_password: str = Field(max_length=4096)
    new_password: str = Field(min_length=1, max_length=4096)


class AccountMerge(BaseModel):
    target_id: str = Field(min_length=1, max_length=64)
    source_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(min_length=1, max_length=100)


# ── the account directory (Admin) ───────────────────────────────────────────

@router.get("/accounts")
async def list_accounts():
    return {"accounts": await accounts.list_accounts()}


@router.post("/accounts")
async def create_account(body: AccountCreate):
    try:
        if body.kind == "integration":
            account = await accounts.create_integration_account(body.display_name or "Integration")
        else:
            account = await accounts.create_local_account(
                str(body.username or ""), str(body.password or ""), role=body.role or Role.USER.value,
                display_name=body.display_name)
    except AccountRefused as exc:
        raise _refused(exc) from None
    return {"ok": True, "id": account.id}


@router.patch("/accounts/{account_id}")
async def update_account(account_id: str, body: AccountUpdate, request: Request,
                         application: ApplicationService = Depends(get_application)):
    """Role, enabled state and display name. Disabling revokes the account's
    access at once and withdraws its outstanding demand in the same
    transaction; the transfer core then settles that demand."""
    withdrawn: list[int] = []
    try:
        if body.role is not None:
            await accounts.set_role(_actor(request), account_id, body.role)
        if body.enabled is not None:
            withdrawn = await accounts.set_enabled(_actor(request), account_id, body.enabled)
        if body.display_name is not None:
            await accounts.set_display_name(account_id, body.display_name)
    except AccountRefused as exc:
        raise _refused(exc) from None
    settled = await application.settle_withdrawn_demand() if withdrawn else []
    return {"ok": True, "withdrawn_transfers": withdrawn, "cancelled_transfers": settled}


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: str, request: Request,
                         application: ApplicationService = Depends(get_application)):
    try:
        withdrawn = await accounts.delete_account(_actor(request), account_id)
    except AccountRefused as exc:
        raise _refused(exc) from None
    settled = await application.settle_withdrawn_demand() if withdrawn else []
    return {"ok": True, "withdrawn_transfers": withdrawn, "cancelled_transfers": settled}


@router.post("/accounts/merge")
async def merge_account(body: AccountMerge, request: Request):
    """Fold the source accounts into the surviving ``target_id`` in one
    transaction: all of them, or none. Demand recorded for a source stays
    recorded for it and is owned through the merge redirect."""
    try:
        await accounts.merge_accounts(_actor(request), body.source_ids, body.target_id)
    except AccountRefused as exc:
        raise _refused(exc) from None
    return {"ok": True}


@router.post("/accounts/{account_id}/password")
async def reset_account_password(account_id: str, body: PasswordReset, request: Request):
    try:
        await accounts.reset_password(_actor(request), account_id, body.password, username=body.username)
    except AccountRefused as exc:
        raise _refused(exc) from None
    return {"ok": True}


@router.get("/accounts/{account_id}/impact")
async def account_impact(account_id: str, application: ApplicationService = Depends(get_application)):
    """What disabling or deleting the account would withdraw, read now from
    authoritative demand; the mutation re-reads everything itself."""
    owners = set(await accounts.owned_account_ids(account_id))
    rows = [row for row in await application.repository.demands(account_ids=owners, limit=1000)
            if row["withdrawn_at"] is None]
    shared = completed = 0
    for row in rows:
        if str(row["status"]) == "completed":
            completed += 1
        others = [item for item in await application.repository.outstanding_demand(int(row["transfer_id"]))
                  if item.get("account_id") not in owners]
        shared += bool(others)
    return {"outstanding_requests": len(rows), "shared_transfers_protected": shared,
            "completed_unaffected": completed, "bounded": len(rows) >= 1000}


@router.get("/accounts/{account_id}/tokens")
async def list_account_tokens(account_id: str):
    return {"tokens": await accounts.list_tokens(account_id)}


@router.post("/accounts/{account_id}/tokens")
async def create_account_token(account_id: str, label: Annotated[str, Body(embed=True, min_length=1, max_length=128)]):
    """The raw token is in this response only; it is never shown again."""
    try:
        record, raw = await accounts.create_token(account_id, label)
    except AccountRefused as exc:
        raise _refused(exc) from None
    return {"ok": True, "token": raw, **record}


@router.delete("/accounts/{account_id}/tokens/{token_id}")
async def revoke_account_token(account_id: str, token_id: str):
    """Revocation stops authentication at once; it withdraws nothing."""
    if not await accounts.revoke_token(account_id, token_id):
        raise HTTPException(status_code=404, detail="Token not found")
    return {"ok": True}


# ── the caller's own account ────────────────────────────────────────────────

def _verification(account) -> str | None:
    if not account.email:
        return None
    if not account.email_verified_known:
        return "unknown"
    return "verified" if account.email_verified else "unverified"


@router.get("/account")
async def own_account(request: Request):
    """The signed-in account's own profile: names it may edit, and the email
    its identity provider supplied (read-only) with the verification the
    provider stated -- never a credential, binding or claim set."""
    principal = request.state.principal
    if not principal.account_id or principal.role == Role.INTEGRATION.value:
        raise HTTPException(status_code=403, detail="Forbidden")
    account = await accounts.get_account(principal.account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"display_name": account.display_name, "actual_name": account.actual_name, "email": account.email,
            "email_verification": _verification(account), "origin": account.origin, "role": account.role.value,
            "has_password": account.has_password}


@router.put("/account/actual-name")
async def change_own_actual_name(actual_name: Annotated[str, Body(embed=True, max_length=256)], request: Request):
    principal = request.state.principal
    if not principal.account_id or principal.role == Role.INTEGRATION.value:
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        await accounts.set_actual_name(principal.account_id, actual_name)
    except AccountRefused as exc:
        raise _refused(exc) from None
    return {"ok": True}


# ── the signed-in User's own notifications ──────────────────────────────────
# Classified ``notifications.self`` (User only). The account is ALWAYS the
# authenticated principal's: no route here takes an account ID, and the body
# model refuses any key that is not a personal notification field.

async def _notifying_account(request: Request) -> str:
    principal = request.state.principal
    account_id = str(principal.account_id or "")
    if not account_id or not await account_notifications.eligible(account_id):
        raise HTTPException(status_code=403, detail="Forbidden")
    return account_id


def _own_test_admitted(account_id: str) -> None:
    if not personal_notification_test_rate_limiter.allow(account_id):
        raise HTTPException(status_code=429, detail="Too many tests; wait a minute and try again")


@router.get("/account/notifications")
async def own_notifications(request: Request):
    account_id = await _notifying_account(request)
    return account_notifications.public(await account_notifications.load(account_id))


@router.put("/account/notifications")
async def update_own_notifications(body: account_notifications.AccountNotificationUpdate, request: Request):
    account_id = await _notifying_account(request)
    try:
        saved = await account_notifications.update(account_id, body)
    except account_notifications.NotificationRefused as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return account_notifications.public(saved)


@router.post("/account/notifications/test-discord")
async def test_own_discord(request: Request):
    """Prove the User's OWN saved Discord configuration: one test message to
    their own primary webhook, as their own identity. It names no transfer."""
    account_id = await _notifying_account(request)
    settings = await account_notifications.load(account_id)
    destination = notification_service.discord_destination(settings)
    if not destination:
        raise HTTPException(status_code=400, detail="No Discord webhook configured")
    _own_test_admitted(account_id)
    fingerprint = notification_service.verification_fingerprints(settings)[notification_service.DISCORD_SUBJECT]
    try:
        if not await account_notifications.sender(settings, destination).test(strict=True):
            raise RuntimeError("Discord test did not send a notification")
    except Exception as exc:
        await account_notifications.record_verification(
            account_id, notification_service.DISCORD_SUBJECT, fingerprint, False)
        raise HTTPException(status_code=502, detail=sanitize_exception(exc, max_length=200)) from None
    saved = await account_notifications.record_verification(
        account_id, notification_service.DISCORD_SUBJECT, fingerprint, True)
    return {"ok": True, "notifications": notification_service.notification_state(saved)}


@router.post("/account/notifications/send-report")
async def send_own_report(request: Request):
    """Send the User's OWN statistics report now: their own activity only, to
    their own effective reporting destination, over their own window."""
    account_id = await _notifying_account(request)
    settings = await account_notifications.load(account_id)
    destination = notification_service.reporting_destination(settings)
    if not destination:
        raise HTTPException(status_code=400, detail="No reporting or primary Discord webhook configured")
    _own_test_admitted(account_id)
    fingerprint = notification_service.verification_fingerprints(settings)[notification_service.REPORTING_SUBJECT]
    hours = notification_service.report_window_hours(settings)
    try:
        from services.stats import send_stats_report

        result = await send_stats_report(
            hours=hours, triggered_by="manual", owner_ids=await accounts.owned_account_ids(account_id),
            sender=account_notifications.sender(settings, destination))
    except Exception as exc:
        await account_notifications.record_verification(
            account_id, notification_service.REPORTING_SUBJECT, fingerprint, False)
        raise HTTPException(status_code=502, detail=sanitize_exception(exc, max_length=200)) from None
    saved = await account_notifications.record_verification(
        account_id, notification_service.REPORTING_SUBJECT, fingerprint, True)
    return {**result, "notifications": notification_service.notification_state(saved)}


@router.put("/account/password")
async def change_own_password(body: OwnPassword, request: Request):
    principal = request.state.principal
    if not principal.authenticated or principal.role == Role.INTEGRATION.value:
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        await accounts.change_own_password(principal.account_id, body.current_password, body.new_password)
    except AccountRefused as exc:
        raise _refused(exc) from None
    return {"ok": True}


@router.put("/account/display-name")
async def change_own_display_name(display_name: Annotated[str, Body(embed=True, min_length=1, max_length=256)],
                                  request: Request):
    principal = request.state.principal
    if not principal.account_id or principal.role == Role.INTEGRATION.value:
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        await accounts.set_display_name(principal.account_id, display_name)
    except AccountRefused as exc:
        raise _refused(exc) from None
    return {"ok": True}


# ── submitter demand ────────────────────────────────────────────────────────

def _public_demand(row: dict) -> dict:
    return {key: row[key] for key in ("id", "transfer_id", "name", "status", "progress", "origin",
                                      "credential_id", "account_id", "created_at", "withdrawn_at")}


@router.get("/requests")
async def list_requests(request: Request, application: ApplicationService = Depends(get_application),
                        account_id: Annotated[str | None, Query(max_length=64)] = None,
                        limit: Annotated[int, Query(ge=1, le=1000)] = 200):
    """Submitted requests and their status: one's own, or (Admin) anyone's."""
    principal = request.state.principal
    if permits(principal, Capability.DEMAND_ANY):
        owners = await accounts.owned_account_ids(account_id) if account_id else None
    else:
        owners = await accounts.owned_account_ids(principal.account_id)
    rows = await application.repository.demands(account_ids=owners, limit=limit)
    return {"requests": [_public_demand(row) for row in rows]}


@router.post("/requests/{demand_id}/withdraw")
async def withdraw_request(demand_id: str, request: Request,
                           application: ApplicationService = Depends(get_application)):
    """Withdraw one request's demand. Idempotent. The transfer stops only if
    no other outstanding demand still needs its work; completed downloads
    and history are never touched."""
    principal = request.state.principal
    row = await application.repository.demand(demand_id)
    if row is None or not (permits(principal, Capability.DEMAND_ANY)
                           or await demand_owner(principal, row.get("account_id"))):
        # Somebody else's request is indistinguishable from a missing one.
        raise HTTPException(status_code=404, detail="Request not found")
    result = await application.withdraw_demand((row["id"],), by=f"account:{principal.account_id}")
    return {**result, "id": row["id"], "already_withdrawn": row["withdrawn_at"] is not None}
