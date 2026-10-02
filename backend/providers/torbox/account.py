"""TorBox account semantics: native plans in, neutral entitlement out.

TorBox's own plan documentation (support.torbox.app, "Account Restrictions"):

* Free (plan 0): torrents only -- limited (one download per 24 hours, ten per
  month, 10 GB each, no private torrents) but available; no web downloads, no
  Usenet.
* Essential (1) and Standard (3): torrents and web downloads; no Usenet.
* Pro (2): torrents, web downloads and Usenet.

A paid plan is current only while its ``premium_expires_at`` is ahead; once it
has passed the account is held to the Free baseline (whatever a later refresh
or a failed one says) and, having lost what its plan included, is degraded.

Limits (``ACTIVE_LIMIT``, ``COOLDOWN_LIMIT``, ``MONTHLY_LIMIT``,
``DOWNLOAD_TOO_LARGE``) are ordinary retry/failover conditions and never change
entitlement. ``PLAN_RESTRICTED_FEATURE`` -- "restricted to users of higher
plans" -- is the one definitive refusal: it contracts exactly the acquisition
family whose creation it refused, for this account only.
"""
from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Any, Mapping

from providers.torbox.client import TorBoxAPIError
from providers.torbox.translation import translate_error
from transfers.entitlement import AccountServiceClass, ProviderEntitlements, account_entitlements
from transfers.errors import Category

SCHEMA_VERSION = "torbox-account-v1"
# TorBox's documented plan numbers (display names only cross as text).
PLAN_NAMES = {0: "Free", 1: "Essential", 2: "Pro", 3: "Standard"}
FREE_PLAN = 0

TORRENTS = frozenset({"magnet", "torrent"})
WEB_DOWNLOADS = frozenset({"http", "https"})
USENET = frozenset({"nzb"})
# What each plan may begin, per TorBox's own plan documentation.
PLAN_ENTITLEMENT = {
    0: TORRENTS,
    1: TORRENTS | WEB_DOWNLOADS,
    3: TORRENTS | WEB_DOWNLOADS,
    2: TORRENTS | WEB_DOWNLOADS | USENET,
}
# The one native refusal that states a plan does not include a feature.
PLAN_RESTRICTED = "PLAN_RESTRICTED_FEATURE"
_CONNECTION = frozenset({Category.CREDENTIAL_INVALID, Category.CREDENTIAL_MISSING,
                         Category.CREDENTIAL_EXPIRED, Category.AUTHENTICATION_FAILED})


def instant(value) -> float | None:
    """A TorBox timestamp (``%Y-%m-%dT%H:%M:%SZ``, UTC) as epoch seconds."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


def account_facts(native: Any) -> dict:
    """Validate a ``/user/me`` answer -- or facts persisted earlier -- into the
    only facts entitlement needs: the plan and when paid time ends. Nothing
    identifying is kept."""
    if not isinstance(native, Mapping):
        raise ValueError("account answer is not an object")
    plan = native.get("plan")
    if isinstance(plan, bool) or not isinstance(plan, int) or plan not in PLAN_ENTITLEMENT:
        raise ValueError("account plan is not a documented TorBox plan")
    until = native.get("premium_until") if "premium_until" in native else instant(native.get("premium_expires_at"))
    if until is not None and (isinstance(until, bool) or not isinstance(until, (int, float))):
        raise ValueError("account expiry is malformed")
    return {"plan": plan, "premium_until": None if until is None else float(until)}


def entitlement(facts: Mapping[str, Any], *, offered: frozenset[str], contracted: frozenset[str] = frozenset(),
                now: float) -> ProviderEntitlements:
    plan, until = facts["plan"], facts["premium_until"]
    paid = plan != FREE_PLAN
    current = paid and until is not None and until > now
    allowed = PLAN_ENTITLEMENT[plan] if current else PLAN_ENTITLEMENT[FREE_PLAN]
    # "Usenet via TorBox" is the operator enabling an optional family: when it
    # is on, this account is expected to take NZBs whatever its plan says.
    expected = PLAN_ENTITLEMENT[plan] | (USENET & offered)
    return account_entitlements(
        offered=offered, expected=expected, entitled=allowed - contracted,
        service_class=AccountServiceClass.PREMIUM if current else AccountServiceClass.STANDARD,
        expires_at=until if current else None, plan=PLAN_NAMES[plan] if current else PLAN_NAMES[FREE_PLAN])


def refused_family(exc: BaseException, kind: str) -> frozenset[str]:
    """The request classes a native refusal of creating ``kind`` definitively
    proves this account's plan excludes -- empty for anything else."""
    if not isinstance(exc, TorBoxAPIError) or exc.error.upper() != PLAN_RESTRICTED:
        return frozenset()
    for family in (TORRENTS, WEB_DOWNLOADS, USENET):
        if kind in family:
            return family
    return frozenset()


class TorBoxAccountTranslation:
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
