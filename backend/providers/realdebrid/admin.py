"""Real-Debrid account administration, device authorization and status truth.

Everything here is Real-Debrid mechanics: the open-source OAuth device flow,
the persistence of a refresh token Real-Debrid rotated, the account check the
Test and the status surface perform. Persisting a newly authorized credential
is the canonical integration-configuration owner's job; this module only hands
it the credential to save.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from providers.realdebrid.account import PREMIUM, account_facts as account_record, entitlement as account_entitlement
from providers.realdebrid.client import Credential, RealDebridService
from providers.realdebrid.translation import INTEGRATION_ID, translate_error
from transfers.errors import Category

_AUTH_REQUIRED = frozenset({Category.CREDENTIAL_INVALID, Category.CREDENTIAL_MISSING,
                            Category.CREDENTIAL_EXPIRED, Category.AUTHENTICATION_FAILED})
# Real-Debrid suggests polling every 5 seconds; never faster than it asks.
_MINIMUM_POLL_SECONDS = 5


# -- device authorization ------------------------------------------------------

@dataclass
class _Authorization:
    """One transient device authorization in progress. The device code lives
    only here, only in memory, and only until it is authorized, cancelled or
    expired -- it is never persisted and never sent to the browser."""
    device_code: str
    user_code: str
    verification_url: str
    interval: int
    expires_at: float
    next_poll_at: float

    def public(self, now: float) -> dict:
        return {"state": "pending", "user_code": self.user_code, "verification_url": self.verification_url,
                "interval": self.interval, "expires_in": max(0, int(self.expires_at - now))}


# Process-local: one authorization at a time, surviving the provider rebuild a
# configuration save causes. Never durable truth.
_pending: _Authorization | None = None
_lock = asyncio.Lock()


def _service(service: RealDebridService | None) -> RealDebridService:
    return service if service is not None else RealDebridService()


async def start_authorization(*, service: RealDebridService | None = None, clock=time.time) -> dict:
    """Begin a device authorization, replacing any earlier one."""
    global _pending
    async with _lock:
        native = await _service(service).device_code()
        now = clock()
        interval = max(_MINIMUM_POLL_SECONDS, int(native["interval"]))
        _pending = _Authorization(native["device_code"], native["user_code"], native["verification_url"],
                                  interval, now + int(native["expires_in"]), now + interval)
        return _pending.public(now)


def authorization_state(*, clock=time.time) -> dict:
    pending = _pending
    if pending is None:
        return {"state": "idle"}
    now = clock()
    if now >= pending.expires_at:
        return {"state": "expired"}
    return pending.public(now)


async def cancel_authorization() -> dict:
    """Abandon the transient authorization. An established credential is untouched."""
    global _pending
    async with _lock:
        _pending = None
    return {"state": "idle"}


@dataclass(frozen=True)
class Authorized:
    credential: Credential


async def poll_authorization(*, service: RealDebridService | None = None, clock=time.time):
    """Advance the authorization at most once per Real-Debrid's interval.

    Returns the public state while pending, expired or idle, or ``Authorized``
    with the credential to persist once the operator has approved the device.
    Not-yet-authorized is the protocol's pending state, never a failure."""
    global _pending
    async with _lock:
        pending = _pending
        if pending is None:
            return {"state": "idle"}
        now = clock()
        if now >= pending.expires_at:
            _pending = None
            return {"state": "expired"}
        if now < pending.next_poll_at:
            return pending.public(now)
        pending.next_poll_at = now + pending.interval
        client = _service(service)
        granted = await client.device_credentials(pending.device_code)
        if granted is None:
            return pending.public(now)
        tokens = await client.token(granted["client_id"], granted["client_secret"], pending.device_code)
        _pending = None
        return Authorized(Credential(granted["client_id"], granted["client_secret"], tokens["refresh_token"]))


# -- persistence of a rotated refresh token ------------------------------------

async def persist_refreshed_credential(credential: Credential) -> None:
    """Write a refresh token Real-Debrid rotated into the saved namespace.

    Only for the credential that is still the saved one (a disconnect or a new
    connection in between wins), through the one settings owner under its
    write lock. It is the same grant -- no ownership or verification material
    changed -- so nothing is rebuilt."""
    from core.config import apply_settings, config_write_lock, load_settings, save_settings

    async with config_write_lock():
        settings = load_settings()
        entry = (settings.integrations or {}).get(INTEGRATION_ID)
        options = dict(getattr(entry, "options", None) or {})
        if entry is None or options.get("client_id") != credential.client_id:
            return
        options["refresh_token"] = credential.refresh_token
        updated = settings.model_copy(update={"integrations": {
            **settings.integrations, INTEGRATION_ID: entry.model_copy(update={"options": options})}})
        save_settings(updated)
        apply_settings(updated)


# -- account truth ---------------------------------------------------------------

def account_facts(user: dict, *, entitlements=None, offered=PREMIUM, clock=time.time) -> dict:
    """The account facts the status surface shows -- never a token lifetime --
    plus the neutral ``account`` truth the same account translation gives
    routing (``providers.realdebrid.account``). ``entitlements`` is the live
    provider's own truth when there is one."""
    premium = user.get("premium")
    if entitlements is None:
        try:
            entitlements = account_entitlement(account_record(user), offered=frozenset(offered), now=clock())
        except ValueError:
            entitlements = None
    return {
        "username": str(user.get("username") or ""),
        "account_type": str(user.get("type") or ""),
        "premium": user.get("type") == "premium",
        "premium_seconds": premium if isinstance(premium, int) and not isinstance(premium, bool) else 0,
        "expiration": str(user.get("expiration") or ""),
        **({"account": entitlements.public()} if entitlements is not None else {}),
    }


async def verify(options) -> dict:
    """Prove the SAVED credential: a fresh client refreshes it and reads /user.

    A fresh client, not the live one, so a still-valid cached access token can
    never stand in for a grant Real-Debrid no longer honours."""
    credential = Credential(options.client_id, options.client_secret, options.refresh_token)
    client = RealDebridService(credential, rate_limit_per_minute=options.rate_limit_per_minute,
                               request_timeout_seconds=options.request_timeout_seconds,
                               upload_timeout_seconds=options.torrent_upload_timeout_seconds,
                               on_refresh=persist_refreshed_credential)
    return account_facts(await client.user())


async def runtime_status(provider, *, enabled: bool) -> dict:
    """Return only facts established by the currently registered provider.

    Operator disablement and the absence of a credential are resolved before
    any network I/O; a configured, enabled provider is probed directly, so no
    route activity, host snapshot or executor state can synthesize health."""
    if not enabled:
        return {"integration": INTEGRATION_ID, "state": "disabled", "checked": False}
    client = getattr(provider, "client", None)
    if client is None or not client.configured:
        return {"integration": INTEGRATION_ID, "state": "unconfigured", "checked": False}
    try:
        user = await client.user()
    except Exception as exc:
        category = translate_error(exc, secrets=client.secrets()).category
        return {"integration": INTEGRATION_ID, "checked": True,
                "state": "auth_required" if category in _AUTH_REQUIRED else "unhealthy"}
    # The probe's answer is account truth: the one account owner adopts it,
    # and what this surface shows is what routing now uses.
    owner = getattr(provider, "account", None)
    entitlements = await owner.observe(user) if owner is not None else None
    return {"integration": INTEGRATION_ID, "state": "healthy", "checked": True,
            **account_facts(user, entitlements=entitlements, offered=getattr(getattr(provider, "descriptor", None), "request_types", PREMIUM))}
