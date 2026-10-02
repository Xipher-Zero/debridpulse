"""Real-Debrid account semantics: native account type in, neutral entitlement out.

Real-Debrid's API documentation (api.real-debrid.com):

* ``GET /user``: ``type`` is ``"premium"`` or ``"free"``, ``premium`` the
  seconds left as a Premium user, ``expiration`` a date.
* ``PUT /torrents/addTorrent`` and ``POST /torrents/addMagnet`` list
  ``403 "Permission denied (account locked, not premium)"``: torrents are a
  premium feature. Error code 9 is "Permission denied"; a locked account has
  its own code (14), so code 9 on a torrent creation is the definitive refusal
  of the torrent feature for this account.
* Hoster unrestriction is per hoster for a free account (error 20, "Hoster not
  available for free users"): a free account may still unrestrict some hosts,
  so it keeps hoster links, and a per-hoster refusal is an ordinary failure of
  that link -- never a contraction of the feature.

Traffic (23), fair usage (36), hoster limits (18) and maintenance (17) are
ordinary retry/failover conditions and never change entitlement. A premium
account whose expiration has passed is held to the free baseline at once,
whatever a failed refresh retained, and -- having lost torrents -- is degraded.
"""
from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Any, Mapping

from providers.realdebrid.client import RealDebridAPIError
from providers.realdebrid.translation import translate_error
from transfers.entitlement import AccountServiceClass, ProviderEntitlements, account_entitlements
from transfers.errors import Category

SCHEMA_VERSION = "realdebrid-account-v1"
TORRENTS = frozenset({"magnet", "torrent"})
HOSTERS = frozenset({"http", "https"})
PREMIUM = TORRENTS | HOSTERS
FREE = HOSTERS
_PERMISSION_DENIED = 9
_CONNECTION = frozenset({Category.CREDENTIAL_INVALID, Category.CREDENTIAL_MISSING,
                         Category.CREDENTIAL_EXPIRED, Category.AUTHENTICATION_FAILED})


def instant(value) -> float | None:
    """A Real-Debrid JSON date as UTC epoch seconds."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


def account_facts(native: Any) -> dict:
    """Validate a ``/user`` answer -- or facts persisted earlier -- into the
    only facts entitlement needs. Nothing identifying is kept."""
    if not isinstance(native, Mapping):
        raise ValueError("account answer is not an object")
    if "premium_account" in native:
        premium, until = native.get("premium_account"), native.get("premium_until")
    else:
        kind = native.get("type")
        if kind not in {"premium", "free"}:
            raise ValueError("account type is not a documented Real-Debrid type")
        premium, until = kind == "premium", instant(native.get("expiration"))
    if not isinstance(premium, bool):
        raise ValueError("account type is malformed")
    if until is not None and (isinstance(until, bool) or not isinstance(until, (int, float))):
        raise ValueError("account expiry is malformed")
    return {"premium_account": premium, "premium_until": None if until is None else float(until)}


def entitlement(facts: Mapping[str, Any], *, offered: frozenset[str], contracted: frozenset[str] = frozenset(),
                now: float) -> ProviderEntitlements:
    until = facts["premium_until"]
    # Premium is current only while its stated end is ahead; a premium answer
    # without an end date is premium (Real-Debrid states one for every account
    # that has premium time).
    current = facts["premium_account"] and (until is None or until > now)
    # An account that had premium time which has now ended lost torrents.
    lapsed = not current and until is not None and until <= now
    return account_entitlements(
        offered=offered, expected=PREMIUM if current or lapsed else FREE,
        entitled=(PREMIUM if current else FREE) - contracted,
        service_class=AccountServiceClass.PREMIUM if current else AccountServiceClass.STANDARD,
        expires_at=until if current else None, plan="Premium" if current else "Free")


def refused_family(exc: BaseException, kind: str) -> frozenset[str]:
    """The request classes a native refusal of creating ``kind`` definitively
    proves this account excludes -- empty for anything else."""
    if (isinstance(exc, RealDebridAPIError) and exc.error_code == _PERMISSION_DENIED
            and kind in TORRENTS):
        return TORRENTS
    return frozenset()


class RealDebridAccountTranslation:
    schema_version = SCHEMA_VERSION

    def __init__(self, client, *, clock=time.time) -> None:
        self._client = client
        self._clock = clock

    def configured(self) -> bool:
        return bool(self._client.configured)

    async def fetch(self):
        return await self._client.user()

    @staticmethod
    def facts(native: Any) -> dict:
        return account_facts(native)

    def connection_failed(self, exc: BaseException) -> bool:
        return translate_error(exc, secrets=self._client.secrets()).category in _CONNECTION

    @staticmethod
    def derive(facts, *, offered, contracted, now) -> ProviderEntitlements:
        return entitlement(facts, offered=offered, contracted=contracted, now=now)
