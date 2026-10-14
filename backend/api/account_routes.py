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


class IdentityLink(BaseModel):
    issuer: str = Field(min_length=1, max_length=2048)
    subject: str = Field(min_length=1, max_length=1024)


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


@router.post("/accounts/{account_id}/merge")
async def merge_account(account_id: str, request: Request,
                        target_id: Annotated[str, Body(embed=True, min_length=1, max_length=64)]):
    """Fold ``account_id`` into ``target_id``. Demand recorded for the source
    stays recorded for it and is owned through the merge redirect."""
    try:
        await accounts.merge_accounts(_actor(request), account_id, target_id)
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


@router.post("/accounts/{account_id}/oidc-identities")
async def link_account_identity(account_id: str, body: IdentityLink, request: Request):
    try:
        await accounts.link_oidc_identity(_actor(request), account_id, body.issuer, body.subject)
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
