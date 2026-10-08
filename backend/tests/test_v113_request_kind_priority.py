"""The request-kind ordering seam at the one provider-selection owner.

``IntegrationDescriptor.request_priority`` replaces ``priority`` for one kind
of request, only to order providers already admitted to the same competition.
Empty -- every existing descriptor -- orders exactly as before. Premiumize's
"Use Premiumize Before Usenet" is its one user: an NZB-only rank against native
Usenet, both staying eligible."""
from __future__ import annotations

from dataclasses import replace

import pytest

from providers.premiumize.client import PremiumizeService
from providers.premiumize.provider import PremiumizeProvider
from providers.usenet.provider import UsenetProvider
from transfers.applicability import ApplicabilityReadiness, HostClaim, HostClaimScope, ProviderApplicability
from transfers.entitlement import AccountServiceClass, account_entitlements
from transfers.models import Capability, IntegrationDescriptor, TransferRequest
from transfers.registry import IntegrationRegistry

HOSTER = TransferRequest("https", "https://hoster.example/f/1", "file.bin")
NZB = TransferRequest("nzb", b"<nzb/>", "posting.nzb")
CLAIM = ProviderApplicability(specialized=True, specialized_hosts=(
    HostClaim("hoster.example", HostClaimScope.DOMAIN, frozenset({"http", "https"})),))


class Stub:
    def __init__(self, identity, *, priority=0, request_priority=(), kinds=("https", "nzb"),
                 applicability=CLAIM, enabled=True, entitled=None):
        self.descriptor = IntegrationDescriptor(identity, identity, frozenset({Capability.RESOLVE}),
                                                frozenset(kinds), enabled, priority, request_priority)
        self.applicability = applicability
        if entitled is not None:
            self.entitlements = account_entitlements(offered=frozenset(kinds), expected=frozenset(kinds),
                                                     entitled=entitled, service_class=AccountServiceClass.PREMIUM)

    async def resolve(self, request):
        raise AssertionError("ordering only")


def registry(*providers):
    owner = IntegrationRegistry()
    for provider in providers:
        owner.register_provider(provider)
    return owner


def order(owner, request, **kwargs):
    return [provider.descriptor.id for provider in owner.eligible_providers(request, **kwargs)]


def test_descriptors_without_a_request_priority_order_exactly_as_before():
    """1: the established key is ``-priority`` then id; an empty map changes
    nothing, for every kind."""
    stubs = [Stub("c", priority=2), Stub("a"), Stub("b", priority=2), Stub("d", priority=-1)]
    for request in (HOSTER, NZB):
        assert order(registry(*stubs), request) == ["b", "c", "a", "d"]
        assert all(stub.descriptor.priority_for(request.kind) == stub.descriptor.priority for stub in stubs)


def test_a_request_priority_orders_only_the_kind_it_names():
    """2."""
    owner = registry(Stub("a"), Stub("z", request_priority=(("nzb", 5),)))
    assert order(owner, NZB) == ["z", "a"]
    assert order(owner, HOSTER) == ["a", "z"]


def test_an_explicit_preferred_provider_still_outranks_a_request_priority():
    """3."""
    owner = registry(Stub("a"), Stub("z", request_priority=(("https", 99),)))
    assert order(owner, replace(HOSTER, preferred_provider="a")) == ["a", "z"]


@pytest.mark.parametrize("held, arguments", [
    (Stub("z", request_priority=(("https", 99),), enabled=False), {}),
    (Stub("z", request_priority=(("https", 99),), entitled=frozenset()), {}),
    (Stub("z", request_priority=(("https", 99),)), {"exhausted": frozenset({"z"})}),
    (Stub("z", request_priority=(("https", 99),)), {"declined": frozenset({"z"})}),
    (Stub("z", request_priority=(("https", 99),),
          applicability=ProviderApplicability(specialized=True, readiness=ApplicabilityReadiness.UNRESOLVED)), {}),
    (Stub("z", request_priority=(("https", 99),),
          applicability=ProviderApplicability(generic_schemes=frozenset({"https"}))), {"generic_closed": True}),
], ids=["disabled", "not-entitled", "exhausted", "declined", "applicability-unresolved", "generic-closed"])
def test_a_request_priority_never_admits_a_provider_the_competition_excluded(held, arguments):
    """4: it reorders only those already admitted."""
    assert order(registry(Stub("a"), held), HOSTER, **arguments) == ["a"]


def test_an_unhealthy_provider_is_never_promoted_by_its_request_priority():
    """4 (health)."""
    owner = registry(Stub("a"), Stub("z", request_priority=(("https", 99),)))
    owner.mark_health("z", healthy=False)
    assert order(owner, HOSTER) == ["a"]


@pytest.mark.parametrize("before, expected", [(False, ["usenet", "premiumize"]), (True, ["premiumize", "usenet"])],
                         ids=["off", "on"])
def test_use_premiumize_before_usenet_orders_nzb_only_and_both_stay_eligible(before, expected):
    """5/6/7: the option ranks Premiumize after or before native Usenet for an
    NZB; HTTP(S) ordering is the integration's own either way, and exhausting
    the preferred one falls through to the other through the existing route
    decision -- the seam owns no retry or failover."""
    premiumize = PremiumizeProvider(PremiumizeService("key"), use_before_usenet=before)
    owner = registry(UsenetProvider(), premiumize)
    assert order(owner, NZB) == expected
    route = owner.provider_route(NZB, exhausted=frozenset({expected[0]}))
    assert route.provider.descriptor.id == expected[1]
    assert premiumize.descriptor.priority_for("https") == premiumize.descriptor.priority_for("magnet") == 0
