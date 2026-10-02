"""AllDebrid account semantics: native account facts in, neutral entitlement out.

AllDebrid's API documentation (docs.alldebrid.com):

* ``/user``: ``isPremium`` and ``premiumUntil`` ("0 if user is not premium,
  or timestamp until user is premium"); ``isTrial`` marks the free-days trial.
* Magnet and torrent upload are premium features: ``MAGNET_MUST_BE_PREMIUM``
  ("You must be premium to use this feature") is the definitive refusal of
  that feature for this account.
* ``/user/hosts`` types every host ``"free"`` or ``"premium"``; "premium
  hosts need a premium subscription". A non-premium account therefore keeps
  hoster links, but only for hosts typed ``free`` -- the per-host narrowing is
  applied with the host inventory (``AllDebridRequestApplicability``).
  ``MUST_BE_PREMIUM`` refuses ONE link (a premium host), never the feature.

``FREE_TRIAL_LIMIT_REACHED`` and every quota/limit code are ordinary
retry/failover conditions and never change entitlement. Premium whose
``premiumUntil`` has passed is held to the free baseline at once, whatever a
failed refresh retained, and -- having lost torrents -- is degraded.
"""
from __future__ import annotations

import time
from typing import Any, Mapping

from transfers.entitlement import AccountServiceClass, ProviderEntitlements, account_entitlements
from transfers.errors import Category

SCHEMA_VERSION = "alldebrid-account-v1"
TORRENTS = frozenset({"magnet", "torrent"})
HOSTERS = frozenset({"http", "https"})
PREMIUM = TORRENTS | HOSTERS
FREE = HOSTERS
FREE_HOST = "free"
MAGNET_MUST_BE_PREMIUM = "MAGNET_MUST_BE_PREMIUM"
_CONNECTION = frozenset({Category.CREDENTIAL_INVALID, Category.CREDENTIAL_MISSING,
                         Category.CREDENTIAL_EXPIRED, Category.AUTHENTICATION_FAILED,
                         Category.AUTHORIZATION_FAILED})


def account_facts(native: Any) -> dict:
    """Validate a ``/user`` answer (its ``user`` object, or the envelope) --
    or facts persisted earlier -- into the only facts entitlement needs."""
    if isinstance(native, Mapping) and isinstance(native.get("user"), Mapping):
        native = native["user"]
    if not isinstance(native, Mapping):
        raise ValueError("account answer is not an object")
    if "premium_account" in native:
        premium, until = native.get("premium_account"), native.get("premium_until")
    else:
        premium, until = native.get("isPremium"), native.get("premiumUntil")
        if isinstance(until, (int, float)) and not isinstance(until, bool):
            until = float(until) if until > 0 else None
    if not isinstance(premium, bool):
        raise ValueError("account premium state is malformed")
    if until is not None and (isinstance(until, bool) or not isinstance(until, (int, float))):
        raise ValueError("account expiry is malformed")
    return {"premium_account": premium, "premium_until": None if until is None else float(until)}


def entitlement(facts: Mapping[str, Any], *, offered: frozenset[str], contracted: frozenset[str] = frozenset(),
                now: float) -> ProviderEntitlements:
    until = facts["premium_until"]
    current = facts["premium_account"] and (until is None or until > now)
    lapsed = not current and until is not None and until <= now
    return account_entitlements(
        offered=offered, expected=PREMIUM if current or lapsed else FREE,
        entitled=(PREMIUM if current else FREE) - contracted,
        service_class=AccountServiceClass.PREMIUM if current else AccountServiceClass.STANDARD,
        expires_at=until if current else None, plan="Premium" if current else "Free")


def refused_family(native_code: str, kind: str) -> frozenset[str]:
    """The request classes a native refusal of creating ``kind`` definitively
    proves this account excludes -- empty for anything else."""
    if str(native_code or "").upper() == MAGNET_MUST_BE_PREMIUM and kind in TORRENTS:
        return TORRENTS
    return frozenset()


class AllDebridAccountTranslation:
    schema_version = SCHEMA_VERSION

    def __init__(self, client, *, clock=time.time) -> None:
        self._client = client
        self._clock = clock

    def configured(self) -> bool:
        return bool(str(getattr(self._client, "api_key", "") or "").strip())

    async def fetch(self):
        return await self._client.get_user()

    @staticmethod
    def facts(native: Any) -> dict:
        return account_facts(native)

    def connection_failed(self, exc: BaseException) -> bool:
        from providers.alldebrid.translation import translate_error
        return translate_error(exc).category in _CONNECTION

    @staticmethod
    def derive(facts, *, offered, contracted, now) -> ProviderEntitlements:
        return entitlement(facts, offered=offered, contracted=contracted, now=now)
