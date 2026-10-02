"""Provider-neutral account entitlement: what the CURRENT account may begin.

An account-backed provider has four separate dimensions, and routing
intersects them for new work only:

* implementation capability -- the static descriptor ``request_types``, never
  mutated by account state, because a provider that loses an entitlement has
  not forgotten how to observe or clean up what it already owns;
* health -- connectivity and authentication, owned by the registry's health
  gate; a connected account proves connection, never entitlement;
* account entitlement -- this value: which request classes the current
  account may initiate now;
* request applicability -- the provider's host/path facts, which still narrow
  whatever the account is entitled to.

Native plans, feature flags and refusal codes terminate in provider
translation. Nothing here, and nothing that consumes it, knows a plan name.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Iterable


class EntitlementReadiness(StrEnum):
    # The provider has current (or still-valid last-known-good) account truth.
    READY = "ready"
    # No authoritative account truth yet: unknown, never "not entitled".
    UNRESOLVED = "unresolved"
    # Account truth cannot be established because the connection itself is
    # failing (no credential, or the credential refused). That is a health
    # question: entitlement does not gate the provider, and its ordinary
    # failure path stays exactly what it was.
    CONNECTION_FAILED = "connection_failed"


class AccountServiceClass(StrEnum):
    STANDARD = "standard"
    PREMIUM = "premium"


@dataclass(frozen=True)
class ProviderEntitlements:
    """One provider's neutral account entitlement truth.

    ``request_types`` are the acquisition request classes the current account
    may initiate now; it is meaningful only while READY. ``degraded`` says the
    connected account has LOST acquisition capability this account and
    configuration are expected to expose (a lapsed plan, a refused feature, an
    enabled optional family the account is not entitled to) -- never that a
    narrower plan lacks what it never included. ``expires_at`` is the
    authoritative instant (UTC epoch seconds) the current service class ends,
    when the provider knows one. ``plan`` is display text only."""

    readiness: EntitlementReadiness = EntitlementReadiness.UNRESOLVED
    request_types: frozenset[str] = frozenset()
    service_class: AccountServiceClass | None = None
    degraded: bool = False
    expires_at: float | None = None
    plan: str = field(default="", compare=True)

    def admits(self, kind: str) -> bool | None:
        """``True``/``False`` for a resolved account; ``None`` while unknown;
        ``True`` while the connection fails (health decides, not entitlement)."""
        if self.readiness == EntitlementReadiness.CONNECTION_FAILED:
            return True
        if self.readiness != EntitlementReadiness.READY:
            return None
        return str(kind or "") in self.request_types

    def public(self) -> dict:
        """The neutral projection status surfaces carry; no native schema."""
        return {
            "entitlement": self.readiness.value,
            "service_class": self.service_class.value if self.service_class else None,
            "functional": "degraded" if self.degraded else "usable",
            "request_types": sorted(self.request_types),
            "plan": self.plan,
            "expires_at": self.expires_at,
        }


UNRESOLVED_ENTITLEMENTS = ProviderEntitlements()
CONNECTION_FAILED_ENTITLEMENTS = ProviderEntitlements(EntitlementReadiness.CONNECTION_FAILED)


def account_entitlements(*, offered: Iterable[str], expected: Iterable[str], entitled: Iterable[str],
                         service_class: AccountServiceClass, expires_at: float | None = None,
                         plan: str = "") -> ProviderEntitlements:
    """Compose resolved entitlement truth from provider-translated sets.

    ``offered`` is what this provider is configured to take part in (its
    descriptor ``request_types``); ``expected`` what this account and
    configuration should be able to begin; ``entitled`` what it may begin now
    (expiry and definitive refusals already applied). Only offered classes are
    ever effective, and the account is degraded exactly when something
    expected is not effective, or when nothing offered is."""
    offered = frozenset(offered)
    effective = offered & frozenset(entitled)
    expected = offered & frozenset(expected)
    return ProviderEntitlements(
        EntitlementReadiness.READY, effective, service_class,
        degraded=bool(expected - effective) or (bool(offered) and not effective),
        expires_at=expires_at, plan=str(plan or ""))
