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

from fastapi import APIRouter, Body, Request

from auth import accounts
from auth.accounts import TOKEN_KIND_BROWSER, AccountRefused
from auth.browser_pairing import (
    PAIRING_SECONDS, POLL_INTERVAL_SECONDS, PairingRefused, pairing_store,
)
from auth.manager import peer_key
from auth.throttle import DualWindowRateLimiter
from core.localization import DisplayError, refusal

router = APIRouter()

BROWSER_PROTOCOL = "debridpulse.browser/1"

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


@router.get("/browser/v1/session")
async def browser_session(request: Request):
    """Which connection this browser token is, and the protocol this server
    speaks: how the extension verifies a pairing and a server's version."""
    principal = request.state.principal
    rows = await accounts.list_tokens(principal.account_id)
    label = next((str(row["label"]) for row in rows if str(row["id"]) == principal.credential_id), "")
    return {"protocol": BROWSER_PROTOCOL, "token": {"id": principal.credential_id, "label": label},
            "account": {"display_name": principal.display_name}}
