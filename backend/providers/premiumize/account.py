"""Premiumize account semantics: native account facts in, neutral entitlement out.

``GET /account/info`` states ``premium_until`` -- the instant (Unix seconds)
paid time ends, ``null`` for a free account -- with ``limit_used`` (the
fair-use fraction spent) and ``booster_points``. Premiumize names no plan, so
none is invented here: the account is premium while its paid time is ahead and
free otherwise. ``space_used`` is deprecated and decides nothing.

What each state may begin through the API DebridPulse uses:

* premium (``premium_until`` ahead): every family this connection offers --
  hoster links, magnets, ``.torrent`` uploads and NZBs;
* free, or premium time run out: nothing. What a free account may do through
  this API is not documented, so no family is offered on evidence that does
  not exist; the account stays connected, standard and degraded.

``limit_used`` and ``booster_points`` are shown, never decided on: Premiumize
refuses work it cannot take now (``account_limit_reached``: fair use, booster
points or active jobs), and that refusal is an ordinary retry/failover
condition. Premiumize documents no refusal stating that an account excludes a
feature, so nothing here contracts entitlement.
"""
from __future__ import annotations

import time
from typing import Any, Mapping

from providers.premiumize.translation import translate_error
from transfers.entitlement import AccountServiceClass, ProviderEntitlements, account_entitlements
from transfers.errors import Category

SCHEMA_VERSION = "premiumize-account-v1"
_CONNECTION = frozenset({Category.CREDENTIAL_INVALID, Category.CREDENTIAL_MISSING,
                         Category.CREDENTIAL_EXPIRED, Category.AUTHENTICATION_FAILED})


def account_facts(native: Any) -> dict:
    """Validate an ``/account/info`` answer -- or facts persisted earlier --
    into the only fact entitlement needs: when paid time ends. Nothing
    identifying is kept."""
    if not isinstance(native, Mapping) or "premium_until" not in native:
        raise ValueError("account answer has no premium_until")
    until = native.get("premium_until")
    if until is not None and (isinstance(until, bool) or not isinstance(until, (int, float))):
        raise ValueError("account expiry is malformed")
    return {"premium_until": None if until is None else float(until)}


def entitlement(facts: Mapping[str, Any], *, offered: frozenset[str], contracted: frozenset[str] = frozenset(),
                now: float) -> ProviderEntitlements:
    until = facts["premium_until"]
    current = until is not None and until > now
    return account_entitlements(
        offered=offered, expected=offered, entitled=(offered if current else frozenset()) - contracted,
        service_class=AccountServiceClass.PREMIUM if current else AccountServiceClass.STANDARD,
        expires_at=until if current else None)


class PremiumizeAccountTranslation:
    schema_version = SCHEMA_VERSION

    def __init__(self, client, *, clock=time.time) -> None:
        self._client = client
        self._clock = clock

    def configured(self) -> bool:
        return bool(self._client.configured)

    async def fetch(self):
        return await self._client.account_info()

    @staticmethod
    def facts(native: Any) -> dict:
        return account_facts(native)

    def connection_failed(self, exc: BaseException) -> bool:
        return translate_error(exc, secrets=self._client.secrets()).category in _CONNECTION

    @staticmethod
    def derive(facts, *, offered, contracted, now) -> ProviderEntitlements:
        return entitlement(facts, offered=offered, contracted=contracted, now=now)
