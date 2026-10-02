"""TorBox account administration, device authorization and status truth.

Everything here is TorBox mechanics: its device authorization, the account
check the Test and the status surface perform. Persisting a newly authorized
credential is the canonical integration-configuration owner's job; this module
only hands it the token to save.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import time

from providers.torbox.client import TorBoxAPIError, TorBoxService
from providers.torbox.translation import INTEGRATION_ID, translate_error
from transfers.errors import Category

_AUTH_REQUIRED = frozenset({Category.CREDENTIAL_INVALID, Category.CREDENTIAL_MISSING,
                            Category.CREDENTIAL_EXPIRED, Category.AUTHENTICATION_FAILED})
# TorBox states its own poll interval; never poll faster than this floor.
_MINIMUM_POLL_SECONDS = 5
# TorBox's documented plan numbers. A plan's capabilities are TorBox's to
# enforce; this only names the plan the account reports.
PLAN_NAMES = {0: "Free", 1: "Essential", 2: "Pro", 3: "Standard"}


def _instant(value) -> float | None:
    """A TorBox timestamp (``%Y-%m-%dT%H:%M:%SZ``, UTC) as epoch seconds."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


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


async def start_authorization(*, service: TorBoxService, clock=time.time) -> dict:
    """Begin a device authorization, replacing any earlier one."""
    global _pending
    async with _lock:
        native = await service.device_start()
        now = clock()
        interval = native.get("interval")
        interval = max(_MINIMUM_POLL_SECONDS, interval if isinstance(interval, int) and not isinstance(
            interval, bool) else 0)
        # TorBox documents a ten-minute code; its own expiry wins when stated.
        expires_at = _instant(native.get("expires_at")) or now + 600
        _pending = _Authorization(native["device_code"], native["code"], native["verification_url"],
                                  interval, expires_at, now + interval)
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
    token: str


async def poll_authorization(*, service: TorBoxService, clock=time.time):
    """Advance the authorization at most once per TorBox's interval.

    Returns the public state while pending, expired or idle, or ``Authorized``
    with the token to persist once the operator has approved the device.
    Not-yet-approved is the protocol's pending state, never a failure; a
    device code TorBox no longer knows is an expired authorization."""
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
        try:
            token = await service.device_token(pending.device_code)
        except TorBoxAPIError as exc:
            if exc.error.upper() == "ITEM_NOT_FOUND":
                _pending = None
                return {"state": "expired"}
            raise
        if token is None:
            return pending.public(now)
        _pending = None
        return Authorized(token)


# -- account truth ---------------------------------------------------------------

def account_facts(user: dict, *, clock=time.time) -> dict:
    """The account facts the status surfaces show: who, which plan, and until
    when that plan runs. Never a token lifetime."""
    plan = user.get("plan")
    plan = plan if isinstance(plan, int) and not isinstance(plan, bool) else None
    expires = str(user.get("premium_expires_at") or "")
    until = _instant(expires)
    return {
        "email": str(user.get("email") or ""),
        "plan": plan,
        "plan_name": PLAN_NAMES.get(plan, "") if plan is not None else "",
        # Paid time to show only for a paid plan whose expiry is still ahead.
        "premium": bool(plan and until is not None and until > clock()),
        "premium_expires_at": expires if until is not None else "",
    }


async def verify(options) -> dict:
    """Prove the SAVED credential with a fresh client, so no state a live
    client holds can stand in for a token TorBox no longer honours."""
    client = TorBoxService(options.api_token, rate_limit_per_minute=options.rate_limit_per_minute,
                           request_timeout_seconds=options.request_timeout_seconds,
                           upload_timeout_seconds=options.upload_timeout_seconds)
    return account_facts(await client.user())


async def runtime_status(provider, *, enabled: bool) -> dict:
    """Return only facts established by the currently registered provider.

    Operator disablement and the absence of a credential are resolved before
    any network I/O; a configured, enabled provider is probed directly, so no
    route activity, host snapshot or earlier proof can synthesize health."""
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
    return {"integration": INTEGRATION_ID, "state": "healthy", "checked": True, **account_facts(user)}
