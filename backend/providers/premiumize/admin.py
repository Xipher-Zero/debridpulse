"""Premiumize account administration and provider-specific status truth.

Everything here is Premiumize mechanics: the account check the Test and the
status surface perform. Persisting a credential is the canonical
integration-configuration owner's job.
"""
from __future__ import annotations

import time

from providers.premiumize import account as accounts
from providers.premiumize.client import PremiumizeService
from providers.premiumize.translation import INTEGRATION_ID, translate_error
from transfers.errors import Category

_AUTH_REQUIRED = frozenset({Category.CREDENTIAL_INVALID, Category.CREDENTIAL_MISSING,
                            Category.CREDENTIAL_EXPIRED, Category.AUTHENTICATION_FAILED})
# What a Premiumize connection takes part in.
_FAMILIES = frozenset({"http", "https", "magnet", "torrent", "nzb"})


def _fraction(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def account_facts(native: dict, *, clock=time.time, entitlements=None, offered=_FAMILIES) -> dict:
    """The account facts the status surfaces show -- whether paid time runs and
    until when, the fair-use fraction spent and the booster balance -- plus
    the neutral ``account`` truth the same translation gives routing
    (``providers.premiumize.account``). Shown, never decided on. Nothing
    identifying leaves here."""
    now = clock()
    if entitlements is None:
        try:
            entitlements = accounts.entitlement(accounts.account_facts(native), offered=frozenset(offered), now=now)
        except ValueError:
            entitlements = None
    native = native if isinstance(native, dict) else {}
    until = native.get("premium_until")
    until = float(until) if isinstance(until, (int, float)) and not isinstance(until, bool) else None
    return {
        "premium": until is not None and until > now,
        "premium_until": until,
        "limit_used": _fraction(native.get("limit_used")),
        "booster_points": _fraction(native.get("booster_points")),
        **({"account": entitlements.public()} if entitlements is not None else {}),
    }


async def verify(api_key: str, options) -> dict:
    """Prove ``api_key`` with a fresh client, so no state a live client holds
    can stand in for a key Premiumize no longer honours. Creates nothing."""
    client = PremiumizeService(api_key, request_timeout_seconds=options.request_timeout_seconds,
                               upload_timeout_seconds=options.upload_timeout_seconds)
    return account_facts(await client.account_info())


async def runtime_status(provider, *, enabled: bool) -> dict:
    """Return only facts established by the currently registered provider.

    Operator disablement and the absence of a key are resolved before any
    network I/O; a configured, enabled provider is probed directly, so no
    route activity, host snapshot or earlier proof can synthesize health."""
    if not enabled:
        return {"integration": INTEGRATION_ID, "state": "disabled", "checked": False}
    client = getattr(provider, "client", None)
    if client is None or not client.configured:
        return {"integration": INTEGRATION_ID, "state": "unconfigured", "checked": False}
    try:
        native = await client.account_info()
    except Exception as exc:
        category = translate_error(exc, secrets=client.secrets()).category
        return {"integration": INTEGRATION_ID, "checked": True,
                "state": "auth_required" if category in _AUTH_REQUIRED else "unhealthy"}
    # The probe's answer is account truth: the one account owner adopts it,
    # and what this surface shows is what routing now uses.
    owner = getattr(provider, "account", None)
    if owner is not None:
        await owner.observe(native)
    entitlements = provider.entitlements if owner is not None else None
    return {"integration": INTEGRATION_ID, "state": "healthy", "checked": True,
            **account_facts(native, entitlements=entitlements,
                            offered=getattr(getattr(provider, "descriptor", None), "request_types", _FAMILIES))}
