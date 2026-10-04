"""Debrid-Link account semantics: native account type in, neutral entitlement out.

``GET /account/infos`` states ``accountType`` and ``premiumLeft`` -- the
seconds of premium time left, a duration, never a date.

* ``accountType`` 0 is a free account and 1 a premium account. Debrid-Link's
  own documentation shows the field (its sample is 1) but defines no values;
  the reference clients read 0 and 1 this way. JDownloader's client also
  reads 2 as a lifetime account. That is a third party's interpretation, not
  Debrid-Link's authoritative semantics, so no never-ending state is derived
  from it. What that evidence does establish is a paid, non-free account
  class -- not any particular capability -- so a type-2 account is premium
  with its end NOT REPORTED (no expiry), entitled to what a paid account's
  hoster use is, and nothing the evidence does not show. Any other value is
  not a known type and is refused as malformed.
* A premium (type 1) account is current while its premium time is ahead; one
  whose time has run out (``premiumLeft`` zero or negative) is held to the
  free baseline at once and, having lost torrents, is degraded.

What each class may begin through the API DebridPulse uses:

* premium (type 1 with time left): hoster links, magnets and ``.torrent``
  uploads;
* type 2: hoster links of every supported hoster (a premium class, so no
  ``isFree`` narrowing). Magnets and ``.torrent`` uploads -- productive remote
  seedbox acquisition -- are NOT offered: no evidence establishes them for
  this class, and an unobserved capability stays conservative until
  Debrid-Link proves it;
* free: hoster links only, and only of hosters Debrid-Link's catalogue marks
  free-usable (``isFree``) -- that per-host narrowing is the provider's
  ``entitlement_for``, read from the host catalogue. Whether a free account
  may use the seedbox is not documented, so it is not offered.

Limits (``maxLink``, ``maxData``, their per-hoster forms, ``maxTorrent``,
``maxTransfer``) are ordinary retry/failover conditions and never change
entitlement. Debrid-Link documents no refusal that states a plan excludes a
feature, so nothing here contracts entitlement.
"""
from __future__ import annotations

import time
from typing import Any, Mapping

from providers.debridlink.translation import translate_error
from transfers.entitlement import AccountServiceClass, ProviderEntitlements, account_entitlements
from transfers.errors import Category

SCHEMA_VERSION = "debridlink-account-v1"
FREE, PREMIUM, UNDATED_PAID = 0, 1, 2
ACCOUNT_TYPES = frozenset({FREE, PREMIUM, UNDATED_PAID})
TORRENTS = frozenset({"magnet", "torrent"})
HOSTERS = frozenset({"http", "https"})
PAID = TORRENTS | HOSTERS
UNPAID = HOSTERS
_CONNECTION = frozenset({Category.CREDENTIAL_INVALID, Category.CREDENTIAL_MISSING,
                         Category.CREDENTIAL_EXPIRED, Category.AUTHENTICATION_FAILED})


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def account_facts(native: Any, *, now: float) -> dict:
    """Validate an ``/account/infos`` answer -- or facts persisted earlier --
    into the only facts entitlement needs: the account type and, for a
    premium account, the instant its time ends. Nothing identifying is kept.

    ``premiumLeft`` is relative, so it is anchored to ``now`` once, on the
    minute, and persisted as that instant."""
    if not isinstance(native, Mapping):
        raise ValueError("account answer is not an object")
    if "account_type" in native:
        kind, until = native.get("account_type"), native.get("premium_until")
        if until is not None and (isinstance(until, bool) or not isinstance(until, (int, float))):
            raise ValueError("account expiry is malformed")
    else:
        kind, left = native.get("accountType"), native.get("premiumLeft")
        if kind == PREMIUM and not _integer(left):
            raise ValueError("premium account without premium time")
        until = float(int(now + left) // 60 * 60) if kind == PREMIUM else None
    if not _integer(kind) or kind not in ACCOUNT_TYPES:
        raise ValueError("account type is not a documented Debrid-Link type")
    if (kind == PREMIUM) != (until is not None):
        raise ValueError("account expiry does not match the account type")
    return {"account_type": kind, "premium_until": None if until is None else float(until)}


def entitlement(facts: Mapping[str, Any], *, offered: frozenset[str], contracted: frozenset[str] = frozenset(),
                now: float) -> ProviderEntitlements:
    kind, until = facts["account_type"], facts["premium_until"]
    if kind == UNDATED_PAID:
        # Paid, end not reported: premium hoster use only (see above).
        return account_entitlements(
            offered=offered, expected=HOSTERS, entitled=HOSTERS - contracted,
            service_class=AccountServiceClass.PREMIUM, expires_at=None, plan="Premium")
    current = kind == PREMIUM and until is not None and until > now
    lapsed = kind == PREMIUM and not current
    return account_entitlements(
        offered=offered, expected=PAID if current or lapsed else UNPAID,
        entitled=(PAID if current else UNPAID) - contracted,
        service_class=AccountServiceClass.PREMIUM if current else AccountServiceClass.STANDARD,
        expires_at=until if current else None, plan="Premium" if current else "Free")


class DebridLinkAccountTranslation:
    schema_version = SCHEMA_VERSION

    def __init__(self, client, *, clock=time.time) -> None:
        self._client = client
        self._clock = clock

    def configured(self) -> bool:
        return bool(self._client.configured)

    async def fetch(self):
        return await self._client.account()

    def facts(self, native: Any) -> dict:
        return account_facts(native, now=float(self._clock()))

    def connection_failed(self, exc: BaseException) -> bool:
        return translate_error(exc, secrets=self._client.secrets()).category in _CONNECTION

    @staticmethod
    def derive(facts, *, offered, contracted, now) -> ProviderEntitlements:
        return entitlement(facts, offered=offered, contracted=contracted, now=now)
