"""Accounts, Integration tokens and submitter demand over HTTP.

Every route here is classified in ``auth.permissions.ACTIONS``: the account
directory, credentials and tokens are Admin; an account's own password and
display name are its own; demand is listed and withdrawn by its owner (or by
an Admin, who may act on anyone's). The account owner (``auth.accounts``)
enforces every account invariant; the transfer core decides what withdrawn
demand means for each transfer.
"""
from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from application.dependencies import get_application
from application.service import ApplicationService
from auth import accounts
from auth.accounts import AccountRefused, Role
from auth.permissions import Capability, demand_owner, permits
from auth.models import AuthMechanism
from auth.policy import interactive_auth_enabled, password_auth_enabled, standalone_deployment, standalone_open
from auth.throttle import personal_notification_test_rate_limiter
from core.config import apply_settings, config_write_lock, get_settings, journal_configuration_change, save_settings
from core.logging_utils import sanitize_exception
from core.localization import DisplayError
from services import account_appearance
from services import account_notifications
from services import notification_service

router = APIRouter()
logger = logging.getLogger("debridpulse.auth")


def _refused(exc: AccountRefused, status: int | None = None) -> HTTPException:
    status = status or (404 if "does not exist" in str(exc) else 409)
    if exc.code:
        return DisplayError(status, str(exc), code=exc.code, params=exc.params)
    return HTTPException(status_code=status, detail=str(exc))


def _actor(request: Request) -> str:
    return request.state.principal.account_id


class AccountCreate(BaseModel):
    # Strict: a value of the wrong JSON type ("yes" for a boolean) is refused,
    # never coerced.
    model_config = ConfigDict(strict=True)
    kind: Literal["local", "integration"]
    role: Literal["admin", "user", "readonly"] | None = None
    username: str | None = Field(default=None, max_length=256)
    password: str | None = Field(default=None, max_length=4096)
    display_name: str = Field(default="", max_length=256)


class AccountUpdate(BaseModel):
    model_config = ConfigDict(strict=True)
    role: Literal["admin", "user", "readonly", "integration"] | None = None
    enabled: bool | None = None
    display_name: str | None = Field(default=None, max_length=256)


class PasswordReset(BaseModel):
    model_config = ConfigDict(strict=True)
    password: str = Field(min_length=1, max_length=4096)
    username: str | None = Field(default=None, max_length=256)


class OwnPassword(BaseModel):
    model_config = ConfigDict(strict=True)
    current_password: str = Field(max_length=4096)
    new_password: str = Field(min_length=1, max_length=4096)


class OwnFirstPassword(BaseModel):
    # Strict, and nothing else: the account is always the signed-in one.
    model_config = ConfigDict(strict=True, extra="forbid")
    new_password: str = Field(min_length=1, max_length=4096)


class OwnPasswordClear(BaseModel):
    # Strict, and nothing else: the account is always the signed-in one.
    model_config = ConfigDict(strict=True, extra="forbid")
    current_password: str = Field(max_length=4096)


class AccountMerge(BaseModel):
    model_config = ConfigDict(strict=True)
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


@router.put("/accounts/{account_id}/username")
async def rename_account_username(account_id: str,
                                  username: Annotated[str, Body(embed=True, min_length=1, max_length=256)],
                                  request: Request):
    """Change the account's username; a stored password is kept."""
    try:
        await accounts.set_username(_actor(request), account_id, username)
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
async def create_account_token(account_id: str, label: Annotated[str, Body(embed=True, min_length=1, max_length=128)],
                               succeeds: Annotated[str | None, Body(embed=True, max_length=64)] = None):
    """The raw token is in this response only; it is never shown again.
    ``succeeds`` rotates an earlier token of this account: the new token
    continues that token's external client (its jobs, deliveries and
    controls) and the earlier token is revoked at once. Without it the token
    is a new external client that can see no other token's jobs."""
    try:
        record, raw = await accounts.create_token(account_id, label, succeeds=succeeds)
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
    """The signed-in account's own profile: its username (read-only: an Admin
    changes it), the names it may edit, and its email
    -- supplied by its identity provider (read-only here) or entered by the
    account itself (``email_self_entered``; the account may correct it) --
    with the verification the provider stated (a self-entered email is
    unverified), never a credential, binding or claim set. With it, read
    from the live settings, the installation's ``deployment_type`` and
    ``sign_in_required``: whether signing in is required now (interactive
    authentication enabled), not whether a password is stored."""
    principal = request.state.principal
    if not principal.account_id or principal.role == Role.INTEGRATION.value:
        raise HTTPException(status_code=403, detail="Forbidden")
    account = await accounts.get_account(principal.account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    settings = get_settings()
    return {"display_name": account.display_name, "actual_name": account.actual_name, "email": account.email,
            "email_verification": _verification(account), "email_self_entered": account.email_self_entered,
            "origin": account.origin, "role": account.role.value, "has_password": account.has_password,
            "username": account.username, "deployment_type": settings.deployment_type,
            "sign_in_required": interactive_auth_enabled(settings)}


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


@router.put("/account/email")
async def record_own_email(email: Annotated[str, Body(embed=True, max_length=320)], request: Request,
                           expected: Annotated[str, Body(embed=True, max_length=320)] = ""):
    """An account without an email records one; an account corrects an email
    it entered itself. ``expected`` is the email the form showed ('' for
    none): a stale or concurrent save, and any change to an email the identity
    provider supplied, is refused (409) rather than overwritten. The account
    is always the session's own; the address is stored unverified and never
    identifies, links or admits anything. An empty address is invalid (422):
    there is no clearing."""
    principal = request.state.principal
    if not principal.account_id or principal.role == Role.INTEGRATION.value:
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        await accounts.set_own_email(principal.account_id, email, expected)
    except accounts.InvalidEmail as exc:
        raise _refused(exc, 422) from None
    except AccountRefused as exc:
        raise _refused(exc) from None
    return {"ok": True}


# ── the signed-in account's own appearance ─────────────────────────────────
# Classified ``interface``: every human role (Admin, User, ReadOnly User), and
# no Integration. The account is ALWAYS the authenticated principal's: no route
# here takes an account ID, and the body model refuses any other key. While
# sign-in is off there is no account to own an appearance: the standalone
# principal is refused, and the interface keeps its choices in the browser.

def _appearance_owner(request: Request) -> str:
    principal = request.state.principal
    if not principal.authenticated or not principal.account_id:
        raise HTTPException(status_code=409, detail="Appearance is kept in this browser while sign-in is off")
    return str(principal.account_id)


@router.get("/account/appearance")
async def own_appearance(request: Request):
    account_id = _appearance_owner(request)
    return {"account_id": account_id, **await account_appearance.load(account_id)}


@router.patch("/account/appearance")
async def update_own_appearance(body: account_appearance.AppearanceUpdate, request: Request):
    account_id = _appearance_owner(request)
    return {"account_id": account_id, **await account_appearance.update(account_id, body)}


# ── the account's own presentation: Easy or Advanced ───────────────────────
# Classified ``interface``: every human role, and no Integration. The account
# is ALWAYS the request's own (``account_appearance.presentation_owner``): the
# signed-in account, or the default Admin that every request acts as while
# sign-in is off -- never an account the request names, and the body model
# refuses any other key. It changes what the interface shows, nothing the
# account may do.

def _presentation_owner(request: Request) -> str:
    account_id = account_appearance.presentation_owner(request.state.principal, get_settings())
    if account_id is None:
        raise HTTPException(status_code=403, detail="Forbidden")
    return account_id


@router.get("/account/presentation")
async def own_presentation(request: Request):
    account_id = _presentation_owner(request)
    return {"presentation_mode": await account_appearance.load_presentation(account_id)}


@router.put("/account/presentation")
async def choose_own_presentation(body: account_appearance.PresentationUpdate, request: Request):
    account_id = _presentation_owner(request)
    return {"presentation_mode": await account_appearance.set_presentation(account_id, body.presentation_mode)}


# ── the account's own interface language ────────────────────────────────────
# Classified ``interface`` and owned exactly as the presentation is
# (``account_appearance.presentation_owner``): the request's own human
# account, or the default Admin while sign-in is off. There is no language
# selector yet: this is the contract a later one uses.

@router.get("/account/locale")
async def own_locale(request: Request):
    account_id = _presentation_owner(request)
    return {"locale": await account_appearance.load_locale(account_id)}


@router.put("/account/locale")
async def choose_own_locale(body: account_appearance.LocaleUpdate, request: Request):
    account_id = _presentation_owner(request)
    return {"locale": await account_appearance.set_locale(account_id, body.locale)}


# ── the installation's login appearance ─────────────────────────────────────
# Classified ``administer``: an Admin reads what the sign-in page shows and
# publishes its own active theme there. The publication is checked again in
# its transaction (an active Admin now; the pair the interface shows is the
# one saved for that Admin), so a stale role or an unsaved choice is refused.

@router.get("/login-appearance")
async def login_appearance(request: Request):
    _appearance_owner(request)
    return await account_appearance.login()


@router.put("/login-appearance")
async def publish_login_appearance(body: account_appearance.LoginAppearance, request: Request):
    account_id = _appearance_owner(request)
    try:
        return await account_appearance.publish(account_id, body)
    except account_appearance.LoginAppearanceRefused:
        raise HTTPException(status_code=403, detail="Forbidden") from None
    except account_appearance.LoginAppearanceStale:
        raise HTTPException(status_code=409, detail="Your theme is not saved as shown; the login theme is unchanged") from None


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
    from auth.manager import PasswordAuthenticationBusy, peer_key

    try:
        await accounts.change_own_password(principal.account_id, body.current_password, body.new_password,
                                           peer=peer_key(request))
    except PasswordAuthenticationBusy:
        raise DisplayError(429, "Too many password checks are already being processed. Try again shortly.",
                           code="account.password_checks_busy", headers={"Retry-After": "2"}) from None
    except AccountRefused as exc:
        raise _refused(exc) from None
    return {"ok": True}


@router.post("/account/first-password")
async def set_own_first_password(body: OwnFirstPassword, request: Request):
    """The signed-in human account's FIRST local password (an account that
    signs in through its identity provider and has none): no current password
    to prove, refused once any password exists. Its sessions then end.

    The one other caller is a Standalone installation's open principal -- the
    default Admin, which authenticated nothing -- setting the default Admin's
    first password: that also enables Username & Password, in the same
    transaction (``_standalone_first_password``)."""
    principal = request.state.principal
    open_admin = (not principal.authenticated and principal.mechanism is None
                  and principal.account_id == accounts.DEFAULT_ADMIN_ID)
    if principal.role == Role.INTEGRATION.value or not (principal.authenticated or open_admin):
        raise HTTPException(status_code=403, detail="Forbidden")
    if open_admin and not standalone_open(get_settings()):
        raise HTTPException(status_code=403, detail="Forbidden")
    from auth.manager import PasswordAuthenticationBusy, peer_key

    try:
        if open_admin:
            await _standalone_first_password(body.new_password, peer=peer_key(request))
        else:
            await accounts.set_own_first_password(principal.account_id, body.new_password, peer=peer_key(request))
    except PasswordAuthenticationBusy:
        raise DisplayError(429, "Too many password checks are already being processed. Try again shortly.",
                           code="account.password_checks_busy", headers={"Retry-After": "2"}) from None
    except AccountRefused as exc:
        raise _refused(exc) from None
    return {"ok": True}


async def _standalone_first_password(new_password: str, *, peer: str) -> None:
    """The default Admin's first password on an open Standalone installation,
    and Username & Password enabled with it: one change, the password and the
    settings committed together (``accounts.commit_first_password``), or
    neither. Argon2 runs first, outside any lock. Then, in the established
    order -- the authentication configuration lock, then the configuration
    write lock every scoped Settings writer holds -- the installation must
    still be open Standalone, and the settings it writes are the ones current
    under both, so no concurrent Settings write can be lost or undo this."""
    from auth.transitions import authentication_configuration_lock

    verifier = await accounts.first_password_verifier(accounts.DEFAULT_ADMIN_ID, new_password, peer=peer)
    async with authentication_configuration_lock, config_write_lock():
        current = get_settings()
        if not standalone_open(current):
            raise AccountRefused("Authentication changed meanwhile; nothing was changed", code="account.auth_changed")
        enabled = current.model_copy(update={"auth_password_enabled": True}, deep=True)
        attempted = []

        def persist():
            attempted.append(True)
            save_settings(enabled)

        try:
            await accounts.commit_first_password(accounts.DEFAULT_ADMIN_ID, verifier, settings=enabled,
                                                 persist=persist)
        except AccountRefused:
            raise
        except Exception as exc:  # noqa: BLE001 - a persistence failure is reported, never half-applied
            if attempted:
                try:
                    save_settings(current)
                except Exception as restore_exc:  # noqa: BLE001
                    logger.critical("Previous authentication settings could not be restored: %s",
                                    type(restore_exc).__name__)
            logger.error("The first password was not set: %s", type(exc).__name__)
            raise DisplayError(500, "The password could not be saved; nothing was changed",
                               code="account.password_not_saved") from None
        apply_settings(enabled)
    await journal_configuration_change(current, enabled)


@router.post("/account/clear-password")
async def clear_own_standalone_password(body: OwnPasswordClear, request: Request):
    """A password-protected Standalone installation's default Admin, signed in
    by its password, removes that password, proving it: Username & Password
    is disabled with it, in the same change (``_clear_standalone_password``),
    and every browser session ends. The installation is then open, as its
    default Admin, until a first password is set again. Nothing else is
    touched. No other account, principal or deployment type may do this."""
    principal = request.state.principal
    if not (principal.authenticated and principal.mechanism is AuthMechanism.PASSWORD_SESSION
            and principal.account_id == accounts.DEFAULT_ADMIN_ID):
        raise HTTPException(status_code=403, detail="Forbidden")
    if not standalone_deployment(get_settings()):
        raise HTTPException(status_code=403, detail="Forbidden")
    from auth.manager import PasswordAuthenticationBusy, peer_key
    from auth.sessions import clear_session_cookie

    try:
        await _clear_standalone_password(body.current_password, peer=peer_key(request))
    except PasswordAuthenticationBusy:
        raise DisplayError(429, "Too many password checks are already being processed. Try again shortly.",
                           code="account.password_checks_busy", headers={"Retry-After": "2"}) from None
    except AccountRefused as exc:
        raise _refused(exc) from None
    response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
    clear_session_cookie(response, request)
    return response


async def _clear_standalone_password(current_password: str, *, peer: str) -> None:
    """The default Admin's password removed and Username & Password disabled:
    one change, the credential and the settings committed together
    (``accounts.commit_password_clear``), or neither. The proof (Argon2) runs
    first, outside any lock. Then, in the established order -- the
    authentication configuration lock, then the configuration write lock
    every scoped Settings writer holds -- the installation must still be
    password-protected Standalone and the stored password still the one
    proven, and the settings written are the ones current under both, so no
    concurrent Settings write is lost. Every browser session then ends."""
    from auth.passwords import basic_verification_cache
    from auth.sessions import session_store
    from auth.throttle import password_failure_throttle
    from auth.transitions import authentication_configuration_lock

    proven, identity = await accounts.prove_own_password(accounts.DEFAULT_ADMIN_ID, current_password, peer=peer)
    async with authentication_configuration_lock, config_write_lock():
        current = get_settings()
        if not (standalone_deployment(current) and password_auth_enabled(current)):
            raise AccountRefused("Authentication changed meanwhile; nothing was changed", code="account.auth_changed")
        cleared = current.model_copy(update={"auth_password_enabled": False}, deep=True)
        attempted = []

        def persist():
            attempted.append(True)
            save_settings(cleared)

        try:
            await accounts.commit_password_clear(accounts.DEFAULT_ADMIN_ID, proven, settings=cleared,
                                                 persist=persist)
        except AccountRefused:
            raise
        except Exception as exc:  # noqa: BLE001 - a persistence failure is reported, never half-applied
            if attempted:
                try:
                    save_settings(current)
                except Exception as restore_exc:  # noqa: BLE001
                    logger.critical("Previous authentication settings could not be restored: %s",
                                    type(restore_exc).__name__)
            logger.error("The password was not cleared: %s", type(exc).__name__)
            raise DisplayError(500, "The password could not be cleared; nothing was changed",
                               code="account.password_not_cleared") from None
        apply_settings(cleared)
    password_failure_throttle.record_success(peer, identity)
    basic_verification_cache.clear()
    session_store.clear()
    await journal_configuration_change(current, cleared)
    logger.warning("The Standalone password was cleared; password sign-in is disabled")


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
