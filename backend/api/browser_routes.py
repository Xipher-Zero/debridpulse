"""Browser pairing, connected browsers and the browser-client API over HTTP.

Three audiences, each fenced by ``auth.permissions``:

- a browser extension with no credential yet: start and poll a pairing
  (``PUBLIC``; the poll's secret device code is the proof);
- a signed-in person: approve or deny a pairing for their OWN account, and
  list or disconnect their own browsers (``BROWSERS_OWN``);
- a browser token: the browser-client API (``BROWSER_CLIENT``, the only
  capability a browser token holds). Its first route proves a pairing worked
  and names the protocol the server speaks; the browser adapter extends it.

The extension's requests carry its own origin; ``auth.middleware``
admits extension origins under ``/api/browser/v1/`` only. The person-facing
routes live under ``/api/account/`` and keep every ordinary protection
(session, CSRF, same-origin).
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import JSONResponse

from auth import accounts
from auth.accounts import TOKEN_KIND_BROWSER, AccountRefused
from auth.browser_pairing import (
    PAIRING_SECONDS, POLL_INTERVAL_SECONDS, PairingRefused, pairing_store,
)
from auth.manager import peer_key
from auth.throttle import DualWindowRateLimiter
from application.dependencies import get_application
from application.service import ApplicationService
from api.external_routes import _call
from core.localization import DisplayError, refusal
from external.service import BROWSER_SOURCE_KINDS, ExternalClients

router = APIRouter()

BROWSER_PROTOCOL = "downloadpulse.browser/1"

# Starting a pairing allocates in-memory state for ten minutes: ten starts
# per peer and sixty-four installation-wide per ten minutes keep the store
# (``MAX_PENDING`` = 64) within capacity even before expiry runs.
pairing_start_limiter = DualWindowRateLimiter(per_peer_limit=10, global_limit=64, window_seconds=600)
# A signed-in person viewing, approving or denying pairings: generous for a
# person, a ceiling for anything scripted against a session.
pairing_answer_limiter = DualWindowRateLimiter(per_peer_limit=30, global_limit=600, window_seconds=600)


def _pairing_refused(exc: PairingRefused, status: int = 404) -> DisplayError:
    return DisplayError(status, str(exc), code=exc.code)


def _answer_allowed(request: Request) -> None:
    if not pairing_answer_limiter.allow(request.state.principal.account_id or peer_key(request)):
        raise DisplayError(429, "Too many connection requests; wait a few minutes.",
                           code="browser.pairing.rate_limited", headers={"Retry-After": "60"})


@router.post("/browser/v1/pairings")
async def start_browser_pairing(request: Request,
                                client_name: Annotated[str, Body(embed=True, max_length=256)] = ""):
    """Start a pairing. The extension shows ``user_code`` and opens
    ``verification_path`` on this server; it polls with ``device_code``."""
    if not pairing_start_limiter.allow(peer_key(request)):
        raise DisplayError(429, "Too many connection requests; wait a few minutes.",
                           code="browser.pairing.rate_limited", headers={"Retry-After": "60"})
    try:
        pairing, device_code = await pairing_store.start(client_name, peer_key(request))
    except PairingRefused as exc:
        raise _pairing_refused(exc, 503 if exc.code == "browser.pairing.busy" else 422) from None
    return {"protocol": BROWSER_PROTOCOL, "device_code": device_code, "user_code": pairing.user_code,
            "verification_path": f"/?connect-browser={pairing.user_code}",
            "expires_in": PAIRING_SECONDS, "interval": POLL_INTERVAL_SECONDS}


@router.post("/browser/v1/pairings/poll")
async def poll_browser_pairing(device_code: Annotated[str, Body(embed=True, max_length=256)] = ""):
    """``pending`` | ``slow_down`` | ``denied`` | ``expired`` | ``approved``
    (with the raw token, exactly once)."""
    return await pairing_store.poll(device_code)


@router.get("/account/browser-pairings/{user_code}")
async def pending_browser_pairing(request: Request, user_code: str):
    """What the signed-in person is asked to approve: never the device code."""
    _answer_allowed(request)
    try:
        pairing = await pairing_store.pending(user_code)
    except PairingRefused as exc:
        raise _pairing_refused(exc) from None
    return {"user_code": pairing.user_code, "client_name": pairing.client_name,
            "requested_at": pairing.created_at, "expires_in": PAIRING_SECONDS}


@router.post("/account/browser-pairings/{user_code}/approve")
async def approve_browser_pairing(request: Request, user_code: str,
                                  label: Annotated[str, Body(embed=True, max_length=128)] = ""):
    """Connect the browser to the signed-in person's own account."""
    _answer_allowed(request)
    principal = request.state.principal
    try:
        pairing = await pairing_store.approve(user_code, principal.account_id, label)
    except PairingRefused as exc:
        raise _pairing_refused(exc) from None
    except AccountRefused as exc:
        raise refusal(403 if exc.code == "account.browser_not_permitted" else 422, str(exc), exc) from None
    return {"ok": True, "browser": {"id": pairing.token_id, "label": pairing.token_label}}


@router.post("/account/browser-pairings/{user_code}/deny")
async def deny_browser_pairing(request: Request, user_code: str):
    _answer_allowed(request)
    try:
        await pairing_store.deny(user_code)
    except PairingRefused as exc:
        raise _pairing_refused(exc) from None
    return {"ok": True}


def _public_browser(row: dict) -> dict:
    return {"id": str(row["id"]), "label": str(row["label"]), "created_at": row["created_at"],
            "last_seen_at": row.get("last_seen_at")}


@router.get("/account/browsers")
async def list_own_browsers(request: Request):
    """The signed-in person's own connected browsers."""
    rows = await accounts.list_tokens(request.state.principal.account_id)
    return {"browsers": [_public_browser(row) for row in rows if row.get("kind") == TOKEN_KIND_BROWSER]}


@router.delete("/account/browsers/{token_id}")
async def disconnect_own_browser(request: Request, token_id: str):
    """Disconnect one of the signed-in person's own browsers; another
    account's token, or an Integration token, is simply not found."""
    account_id = request.state.principal.account_id
    if not await accounts.revoke_token(account_id, token_id, kind=TOKEN_KIND_BROWSER, actor_id=account_id):
        raise DisplayError(404, "Connected browser not found.", code="account.browser_token_not_found")
    return {"ok": True}


@router.get("/browser-tokens")
async def list_all_browser_tokens():
    """An Admin's view of every connected browser (Users & Access ›
    Integration Accounts): revoked through the account token route like any
    token. A listing, not an account: each browser stays its person's."""
    return {"browsers": [{"id": str(row["id"]), "label": str(row["label"]), "account_id": str(row["account_id"]),
                          "account_name": str(row["account_name"]), "created_at": row["created_at"],
                          "last_seen_at": row.get("last_seen_at")} for row in await accounts.list_browser_tokens()]}


def _video_cookies(application) -> dict | None:
    """The browser's video sign-in (external.browser_cookies), offered only while Media
    Downloads takes part (operator, 2026-11-19): otherwise the extension never sends any."""
    from core.config import get_settings
    from external import browser_cookies as held
    from integrations.configuration import effective_enabled
    media = next((item for item in getattr(application, "definitions", ()) if item.id == "media"), None)
    try:
        if media is None or not effective_enabled(get_settings(), media):
            return None
    except Exception:
        return None
    return {"max_count": held.MAX_COOKIES, "max_bytes": held.MAX_BYTES, "sources": ["links"]}


@router.get("/browser/v1/session")
async def browser_session(request: Request, application: ApplicationService = Depends(get_application)):
    """Which connection this browser token is, and the protocol this server
    speaks: how the extension verifies a pairing and a server's version; and
    its person's appearance, which the extension's own pages follow."""
    from services import account_appearance
    principal = request.state.principal
    rows = await accounts.list_tokens(principal.account_id)
    label = next((str(row["label"]) for row in rows if str(row["id"]) == principal.credential_id), "")
    capabilities = {"sources": sorted(BROWSER_SOURCE_KINDS), "controls": ["pause", "resume"],
                    "withdraw": True, "estimate": True}
    cookies = _video_cookies(application)
    if cookies:
        capabilities["cookies"] = cookies
    return {"protocol": BROWSER_PROTOCOL, "token": {"id": principal.credential_id, "label": label},
            "account": {"display_name": principal.display_name},
            "capabilities": capabilities,
            # The person's own appearance (services.account_appearance, its one owner): the
            # mode and the palette chosen for each mode, as palette IDs. The extension draws
            # its own pages in it (operator, 2026-11-18).
            "appearance": await account_appearance.load(principal.account_id)}


@router.delete("/browser/v1/session")
async def disconnect_this_browser(request: Request):
    """Disconnect the browser this token belongs to, from the browser itself
    (the extension's Disconnect): the token that carries this request is
    revoked at once, journaled as disconnected by its owner. The work it
    sent is untouched."""
    principal = request.state.principal
    if not await accounts.revoke_token(principal.account_id, principal.credential_id, kind=TOKEN_KIND_BROWSER,
                                       actor_id=principal.account_id):
        raise DisplayError(404, "Connected browser not found.", code="account.browser_token_not_found")
    return {"ok": True}


@router.post("/browser/v1/application/show")
async def show_application():
    """The extension's Open DownloadPulse, for DownloadPulse on this computer:
    the installed desktop application brings its own window forward (its
    supervisor does, as the tray's Open does, on its next look) and answers
    ``{"desktop": true}``. A server has no window: ``{"desktop": false}``,
    and the extension opens the page instead. Nothing else is touched."""
    from core.config import desktop_installation
    if not desktop_installation():
        return {"desktop": False}
    from application.desktop_control import request_show
    request_show()
    return {"desktop": True}


# ── the browser's own downloads (``external.service``, browser jobs) ────────
#
# Each route is the external-client owner's operation, called with the
# browser token's principal: the same idempotency, listing, isolation,
# withdrawal and advisory controls an Integration has, with no delivery.


@router.post("/browser/v1/jobs")
async def submit_browser_job(request: Request, body: Annotated[dict, Body()],
                             application: ApplicationService = Depends(get_application)):
    """Submit one download (``idempotency_key`` required): a retry with the
    same key answers the same job (200), never a second download."""
    job, created = await _call(ExternalClients(application).submit(request.state.principal, body))
    return JSONResponse(status_code=201 if created else 200, content=job)


@router.get("/browser/v1/jobs")
async def list_browser_jobs(request: Request, application: ApplicationService = Depends(get_application),
                            after: Annotated[int, Query(ge=0)] = 0,
                            limit: Annotated[int, Query(ge=1, le=200)] = 100,
                            phase: Annotated[str | None, Query(max_length=32)] = None):
    """This browser's own downloads, by change cursor."""
    return await _call(ExternalClients(application).jobs(request.state.principal, after=after, limit=limit,
                                                         phase=phase))


@router.get("/browser/v1/jobs/{job_id}")
async def get_browser_job(job_id: str, request: Request,
                          application: ApplicationService = Depends(get_application)):
    return await _call(ExternalClients(application).job(request.state.principal, job_id))


@router.post("/browser/v1/jobs/{job_id}/withdraw")
async def withdraw_browser_job(job_id: str, request: Request,
                               application: ApplicationService = Depends(get_application)):
    """Remove: withdraw this browser's request. The download stops only if
    nobody else still needs it; a finished download is kept."""
    return await _call(ExternalClients(application).withdraw(request.state.principal, job_id))


@router.post("/browser/v1/jobs/{job_id}/controls")
async def request_browser_control(job_id: str, request: Request, body: Annotated[dict, Body()],
                                  application: ApplicationService = Depends(get_application)):
    """Advisory pause/resume (``{kind, request_key}``): honoured only while
    this browser's request is the only one its download serves."""
    return await _call(ExternalClients(application).control(request.state.principal, job_id, body))
