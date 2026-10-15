"""Premiumize's read-only cache answer as routing readiness (1.0.13).

Premiumize's existing ``cache/check`` -- one batched read that creates nothing
-- is its ``Capability.AVAILABILITY`` answer for torrent roots: held is READY,
not held is NOT_READY, anything it cannot be asked about is UNKNOWN, and a
failed read is UNKNOWN to routing (bounded by the engine). READY only prefers,
never outranks an explicitly preferred provider, and no other provider's
readiness is inferred from it. Native HTTP is a scripted local transport.
"""
from __future__ import annotations

import pytest
from test_v113_premiumize_provider import HASH, HOSTER, MAGNET, ok, provider_with, refused, routes
from test_v113_provider_availability import Claimant, availability_of, winner

import transfers.engine as engine_module
from transfers.errors import TransferError
from transfers.models import AvailabilityState, Capability, TransferRequest
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

READY, NOT_READY, UNKNOWN = AvailabilityState.READY, AvailabilityState.NOT_READY, AvailabilityState.UNKNOWN
OTHER = "f" * 40
TORRENT = TransferRequest("torrent", b"d4:infod4:name4:Showee", "Show", OTHER)
NZB = TransferRequest("nzb", b"<nzb/>", "Show.nzb")


async def test_premiumize_declares_availability_and_answers_torrents_in_one_batched_read():
    provider, transport = provider_with({("POST", "cache/check"): [ok(response=[True, False])]})
    assert Capability.AVAILABILITY in provider.descriptor.capabilities
    states = await provider.availability((MAGNET, HOSTER, TORRENT, NZB, MAGNET))
    assert states == (READY, UNKNOWN, NOT_READY, UNKNOWN, READY)
    assert routes(transport) == [("POST", "cache/check")]                 # one read, nothing created
    assert transport.calls[0]["data"] == [("items[]", HASH), ("items[]", OTHER)]


async def test_nothing_askable_reads_nothing():
    provider, transport = provider_with({})
    assert await provider.availability((HOSTER, NZB, TransferRequest("magnet", "magnet:?dn=x", "x"))) == (
        UNKNOWN, UNKNOWN, UNKNOWN)
    assert transport.calls == []


async def test_a_failed_read_is_a_normalized_failure_never_a_guess():
    provider, _transport = provider_with({("POST", "cache/check"): [refused("auth_failed", status=401)]})
    with pytest.raises(TransferError):
        await provider.availability((MAGNET,))


class PremiumizeLike(Claimant):
    """A neutral claimant answering with Premiumize's adapter over a
    scripted transport, so the engine's bounded consumption is exercised."""

    def __init__(self, identity, script, **options):
        super().__init__(identity, **options)
        self.adapter, self.transport = provider_with(script)

    async def availability(self, requests):
        self.asked.append(tuple(str(request.payload) for request in requests))
        return await self.adapter.availability(requests)


async def test_a_premiumize_cache_hit_is_preferred_over_an_unknown_winner(tmp_path, monkeypatch):
    alldebrid = Claimant("alldebrid", observes=False)
    premiumize = PremiumizeLike("premiumize", {("POST", "cache/check"): [ok(response=[True])] * 3})
    taken, decision, *_ = await winner(tmp_path, monkeypatch, alldebrid, premiumize, request=MAGNET)
    assert taken == ["premiumize"]
    assert availability_of(decision) == {"alldebrid": "unknown", "premiumize": "ready"}


async def test_a_premiumize_miss_or_failure_changes_nothing_and_infers_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_module, "AVAILABILITY_TIMEOUT_SECONDS", 0.5)
    alldebrid = Claimant("alldebrid", observes=False)
    premiumize = PremiumizeLike("premiumize", {("POST", "cache/check"): [refused("auth_failed", status=401)] * 3})
    taken, decision, *_ = await winner(tmp_path, monkeypatch, alldebrid, premiumize, request=MAGNET)
    assert taken == ["alldebrid"]                                          # the established winner
    assert availability_of(decision) == {"alldebrid": "unknown", "premiumize": "unknown"}


async def test_an_explicit_preference_is_never_outranked_by_a_cache_answer():
    registry = IntegrationRegistry()
    for identity in ("alldebrid", "premiumize"):
        registry.register_provider(Claimant(identity))
    preferred = TransferRequest("magnet", MAGNET.payload, "Show", HASH, preferred_provider="alldebrid")
    route = registry.provider_route(preferred, availability={"premiumize": READY, "alldebrid": UNKNOWN})
    assert route.provider.descriptor.id == "alldebrid"
    # Without the preference the READY answer is what orders.
    route = registry.provider_route(MAGNET, availability={"premiumize": READY, "alldebrid": UNKNOWN})
    assert route.provider.descriptor.id == "premiumize"
