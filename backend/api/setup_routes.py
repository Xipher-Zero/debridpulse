"""The setup wizard's lifecycle endpoints (``services.setup_lifecycle``).

Every one is an Admin action (``auth.permissions``): while no sign-in is
enabled that is the open principal, the default Admin every request acts as
then, and afterwards a signed-in Admin. Each page the wizard shows commits its
own configuration through that configuration's owner; these record only where
the wizard resumes, the one named Administrator of a new Server installation,
Finish, and an Admin's own rerun.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from auth.accounts import USERNAME_LIMIT, AccountRefused
from auth.models import Principal
from auth.policy import standalone_deployment
from core.config import get_settings
from core.localization import DisplayError, refusal
from services import setup_lifecycle
from services.setup_lifecycle import SetupRefused

router = APIRouter()


class InitialPhase(BaseModel):
    model_config = ConfigDict(strict=True)
    phase: Literal[setup_lifecycle.INITIAL_PHASES]


class RerunPhase(BaseModel):
    model_config = ConfigDict(strict=True)
    phase: Literal[setup_lifecycle.RERUN_PHASES]


class InitialAdministrator(BaseModel):
    model_config = ConfigDict(strict=True)
    username: str = Field(max_length=USERNAME_LIMIT)
    password: str = Field(max_length=4096)


def _refused(exc: Exception) -> DisplayError:
    status = exc.status if isinstance(exc, SetupRefused) else 409
    return refusal(status, str(exc), exc)


def _no_store(body: dict) -> JSONResponse:
    response = JSONResponse(body)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/setup/state")
async def setup_state(request: Request):
    return _no_store(await setup_lifecycle.state(request.state.principal.account_id))


@router.put("/setup/initial/phase")
async def advance_initial_setup(body: InitialPhase, request: Request):
    try:
        await setup_lifecycle.advance_initial(body.phase)
    except SetupRefused as exc:
        raise _refused(exc) from None
    return _no_store(await setup_lifecycle.state(request.state.principal.account_id))


@router.post("/setup/initial/administrator")
async def establish_setup_administrator(body: InitialAdministrator, request: Request):
    """A new Server installation's named Administrator, with Username &
    Password enabled for it (``setup_lifecycle.establish_administrator``).
    Only the open principal -- no sign-in is enabled yet -- may ask; the
    response signs the browser in as the new Administrator with an ordinary
    password session, so setup continues as that account."""
    principal = request.state.principal
    from auth.accounts import DEFAULT_ADMIN_ID

    if principal.authenticated or principal.mechanism is not None or principal.account_id != DEFAULT_ADMIN_ID:
        raise DisplayError(403, "Forbidden", code="auth.forbidden")
    from auth.manager import PasswordAuthenticationBusy
    from auth.sessions import session_cookie_token, session_store, set_session_cookie
    from api.auth_routes import _session_lifetime_seconds

    try:
        administrator = await setup_lifecycle.establish_administrator(request, body.username, body.password)
    except PasswordAuthenticationBusy:
        raise DisplayError(429, "Too many password checks are already being processed. Try again shortly.",
                           code="account.password_checks_busy", headers={"Retry-After": "2"}) from None
    except (SetupRefused, AccountRefused) as exc:
        raise _refused(exc) from None

    old_token = session_cookie_token(request)
    if old_token:
        session_store.revoke(old_token)
    lifetime = _session_lifetime_seconds(get_settings())
    signed_in = Principal.password_session(administrator)
    token, _record = session_store.create(signed_in, lifetime_seconds=lifetime,
                                          credential_version=signed_in.credential_version)
    response = _no_store(await setup_lifecycle.state(administrator.id))
    set_session_cookie(response, request, token, max_age=lifetime)
    return response


@router.post("/setup/initial/finish")
async def finish_initial_setup(request: Request):
    """Finish the initial setup: complete is written first. A Server
    installation finishes only for a signed-in Administrator while Username &
    Password is enabled; its wizard session then ends -- every session of
    that Administrator -- and the browser signs in again through the ordinary
    sign-in page."""
    from auth.sessions import clear_session_cookie, session_store

    principal = request.state.principal
    try:
        await setup_lifecycle.finish_initial(principal)
    except SetupRefused as exc:
        raise _refused(exc) from None
    sign_in = not standalone_deployment(get_settings())
    response = _no_store({"complete": True, "sign_in_required": sign_in})
    if sign_in:
        session_store.revoke_account(principal.account_id)
        clear_session_cookie(response, request)
    return response


@router.put("/setup/rerun")
async def continue_own_rerun(body: RerunPhase, request: Request):
    """Start or continue the signed-in Admin's own rerun."""
    account_id = request.state.principal.account_id
    try:
        await setup_lifecycle.set_rerun(account_id, body.phase)
    except SetupRefused as exc:
        raise _refused(exc) from None
    return _no_store(await setup_lifecycle.state(account_id))


@router.delete("/setup/rerun")
async def end_own_rerun(request: Request):
    """End the signed-in Admin's own rerun, finished or dismissed."""
    account_id = request.state.principal.account_id
    await setup_lifecycle.end_rerun(account_id)
    return _no_store(await setup_lifecycle.state(account_id))
