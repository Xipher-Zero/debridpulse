"""Debrid-Link account administration and provider-specific status truth.

Everything here is Debrid-Link mechanics: the account check the Test and the
status surface perform. Persisting a credential is the canonical
integration-configuration owner's job.
"""
from __future__ import annotations

import time

from providers.debridlink import account as accounts
from providers.debridlink.client import DebridLinkService
from providers.debridlink.translation import INTEGRATION_ID, translate_error
from transfers.errors import Category

_AUTH_REQUIRED = frozenset({Category.CREDENTIAL_INVALID, Category.CREDENTIAL_MISSING,
                            Category.CREDENTIAL_EXPIRED, Category.AUTHENTICATION_FAILED})
# What a Debrid-Link connection takes part in.
_FAMILIES = accounts.TORRENTS | accounts.HOSTERS


def account_facts(native: dict, *, clock=time.time, entitlements=None, offered=_FAMILIES) -> dict:
    """The account facts the status surfaces show -- who is connected -- plus
    the neutral ``account`` truth the same translation gives routing
    (``providers.debridlink.account``). ``entitlements`` is the live
    provider's own truth when there is one; otherwise it is derived from
    these facts. Nothing else of the native answer leaves here."""
    if entitlements is None:
        now = clock()
        try:
            entitlements = accounts.entitlement(accounts.account_facts(native, now=now),
                                                offered=frozenset(offered), now=now)
        except ValueError:
            entitlements = None
    username = native.get("username") if isinstance(native, dict) else ""
    return {
        "username": str(username or "") if isinstance(username, str) else "",
        **({"account": entitlements.public()} if entitlements is not None else {}),
    }


async def verify(api_key: str, options) -> dict:
    """Prove ``api_key`` with a fresh client, so no state a live client holds
    can stand in for a key Debrid-Link no longer honours. Creates nothing."""
    client = DebridLinkService(api_key, request_timeout_seconds=options.request_timeout_seconds,
                               upload_timeout_seconds=options.torrent_upload_timeout_seconds)
    return account_facts(await client.account())


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
        native = await client.account()
    except Exception as exc:
        category = translate_error(exc, secrets=client.secrets()).category
        return {"integration": INTEGRATION_ID, "checked": True,
                "state": "auth_required" if category in _AUTH_REQUIRED else "unhealthy"}
    # The probe's answer is account truth: the one account owner adopts it,
    # and what this surface shows is what routing now uses.
    owner = getattr(provider, "account", None)
    entitlements = await owner.observe(native) if owner is not None else None
    return {"integration": INTEGRATION_ID, "state": "healthy", "checked": True,
            **account_facts(native, entitlements=entitlements,
                            offered=getattr(getattr(provider, "descriptor", None), "request_types", _FAMILIES))}
