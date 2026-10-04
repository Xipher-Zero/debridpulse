"""Effective acquisition availability (canonical visibility corrective, B).

Health (the provider answered, the key is good) and functional usefulness (the
current account can actually acquire something through this provider) are
separate facts. Usefulness is account entitlement intersected with the
provider's own account-specific applicability, per acquisition class, OR'd
across classes. An authoritatively empty surface is degraded; unknown is
unresolved -- never zero and never green. Routing is unchanged throughout.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from integrations.account_entitlement import AccountEntitlementMaintenance
from integrations.runtime_state import ScopedRuntimeStateStore, credential_scope
from providers.alldebrid import account as ad_accounts
from providers.alldebrid import admin as ad_admin
from providers.alldebrid.host_runtime import (
    AllDebridHostSnapshotError, AllDebridRequestApplicability, parse_native_host_snapshot as ad_snapshot,
)
from providers.alldebrid.provider import AllDebridProvider
from providers.debridlink import account as dl_accounts
from providers.debridlink import admin as dl_admin
from providers.debridlink.host_runtime import (
    HOST_SCHEMA_VERSION, HOST_SOURCE, HOST_STATE_KEY, DebridLinkHostMaintenance, DebridLinkRequestApplicability,
    applicability_facts, decode_host_snapshot, encode_host_snapshot, parse_native_host_snapshot,
)
from providers.debridlink.provider import DebridLinkProvider
from providers.realdebrid.provider import RealDebridProvider
from test_v113_debridlink_provider import KEY, NOW, Store, ok, service
from transfers.entitlement import AccountServiceClass, account_entitlements
from transfers.models import TransferRequest

pytestmark = pytest.mark.asyncio

FREE_ACCOUNT = {"accountType": 0, "premiumLeft": 0}
PREMIUM_ACCOUNT = {"accountType": 1, "premiumLeft": 86400}


def _hoster(domain, free):
    record = {"name": domain, "type": "host", "domains": [domain], "regexs": []}
    if free is not ...:
        record["isFree"] = free
    return record


def _debridlink(account, hosters, *, store=None):
    client, _ = service({("GET", "account/infos"): [ok(account)] * 4})
    provider = DebridLinkProvider(client)
    if hosters is not None:
        snapshot = parse_native_host_snapshot(hosters)
        provider.applicability = applicability_facts(snapshot)
        provider.applicability_for = DebridLinkRequestApplicability(snapshot)
    else:
        provider.applicability_for = DebridLinkRequestApplicability(None)
    provider.account = AccountEntitlementMaintenance(
        provider, dl_accounts.DebridLinkAccountTranslation(client, clock=lambda: NOW),
        ScopedRuntimeStateStore(store or Store(), credential_scope("debridlink", KEY)), integration_id="debridlink",
        clock=lambda: NOW)
    return provider


async def _status(provider):
    return await dl_admin.runtime_status(provider, enabled=True)


# -- Debrid-Link ----------------------------------------------------------------------------

async def test_a_healthy_free_account_with_no_usable_hoster_is_degraded_not_green():
    provider = _debridlink(FREE_ACCOUNT, [_hoster("paid.example", False), _hoster("other.example", False)])
    status = await _status(provider)
    # Health: the account answered with a valid key.
    assert status["state"] == "healthy"
    # Truthful account facts: a standard (Free) account with HTTP(S) classes.
    assert status["account"]["service_class"] == "standard" and status["account"]["plan"] == "Free"
    assert status["account"]["request_types"] == ["http", "https"]
    # ...through which this account can acquire nothing.
    assert status["account"]["functional"] == "degraded"


async def test_one_free_hoster_keeps_the_free_account_usable():
    provider = _debridlink(FREE_ACCOUNT, [_hoster("paid.example", False), _hoster("free.example", True)])
    status = await _status(provider)
    assert (status["state"], status["account"]["functional"]) == ("healthy", "usable")


async def test_malformed_or_missing_isfree_is_unknown_for_status_and_still_refused_by_routing():
    provider = _debridlink(FREE_ACCOUNT, [_hoster("paid.example", False), _hoster("odd.example", "yes"),
                                          _hoster("unsaid.example", ...)])
    status = await _status(provider)
    assert status["state"] == "healthy"
    assert status["account"]["functional"] == "unresolved"          # never authoritative zero, never green
    # Routing stays fail-closed: only an explicit true lets a free account begin.
    assert provider.entitlement_for(TransferRequest("https", "https://odd.example/f/1")) is False
    assert provider.entitlement_for(TransferRequest("https", "https://unsaid.example/f/1")) is False
    assert provider.entitlement_for(TransferRequest("https", "https://paid.example/f/1")) is False
    # A known-true entry decides usefulness whatever else is unknown.
    usable = _debridlink(FREE_ACCOUNT, [_hoster("odd.example", "yes"), _hoster("free.example", True)])
    assert (await _status(usable))["account"]["functional"] == "usable"


async def test_an_unresolved_catalogue_is_unresolved_never_zero():
    provider = _debridlink(FREE_ACCOUNT, None)
    status = await _status(provider)
    assert status["state"] == "healthy" and status["account"]["functional"] == "unresolved"


async def test_a_premium_account_with_a_catalogue_remains_usable():
    provider = _debridlink(PREMIUM_ACCOUNT, [_hoster("paid.example", False)])
    status = await _status(provider)
    assert status["account"]["service_class"] == "premium"
    assert status["account"]["functional"] == "usable"
    # Premium hoster use is never narrowed by the free flag: unknown catalogue
    # truth does not touch it either.
    assert (await _status(_debridlink(PREMIUM_ACCOUNT, None)))["account"]["functional"] == "usable"


async def test_restart_restores_the_same_usefulness_from_last_known_good_truth():
    store = Store()
    before = _debridlink(FREE_ACCOUNT, [_hoster("paid.example", False)], store=store)
    await before.account.refresh_now()
    snapshot = parse_native_host_snapshot([_hoster("paid.example", False)])
    store.records[("debridlink", HOST_STATE_KEY)] = SimpleNamespace(
        payload=encode_host_snapshot(snapshot), schema_version=HOST_SCHEMA_VERSION, generation=1,
        stale_after=NOW + 3600, is_stale=lambda now: False)
    expected = before.entitlements.public()["functional"]
    assert expected == "degraded"

    client, transport = service({})
    restarted = DebridLinkProvider(client)
    hosts = DebridLinkHostMaintenance(restarted, store, clock=lambda: NOW)
    restarted.account = AccountEntitlementMaintenance(
        restarted, dl_accounts.DebridLinkAccountTranslation(client, clock=lambda: NOW),
        ScopedRuntimeStateStore(store, credential_scope("debridlink", KEY)), integration_id="debridlink",
        clock=lambda: NOW)
    # Before any current or valid last-known-good truth: unknown -- never
    # usable, never degraded.
    assert restarted.entitlements.public()["entitlement"] == "unresolved"
    assert restarted.entitlements.public()["functional"] == "unresolved"
    await hosts.start()
    await restarted.account.start()                                         # restore only; fetch nothing
    assert transport.calls == []
    assert restarted.entitlements.public()["functional"] == expected
    # A fresh refresh that fails keeps the last-known-good truth it derived.
    await restarted.account.refresh_now()                                   # the scripted service has no answer
    assert restarted.entitlements.public()["functional"] == expected


async def test_restart_restores_usable_truth_too():
    store = Store()
    before = _debridlink(FREE_ACCOUNT, [_hoster("free.example", True)], store=store)
    await before.account.refresh_now()
    store.records[("debridlink", HOST_STATE_KEY)] = SimpleNamespace(
        payload=encode_host_snapshot(parse_native_host_snapshot([_hoster("free.example", True)])),
        schema_version=HOST_SCHEMA_VERSION, generation=1, stale_after=NOW + 3600, is_stale=lambda now: False)
    client, _ = service({})
    restarted = DebridLinkProvider(client)
    hosts = DebridLinkHostMaintenance(restarted, store, clock=lambda: NOW)
    restarted.account = AccountEntitlementMaintenance(
        restarted, dl_accounts.DebridLinkAccountTranslation(client, clock=lambda: NOW),
        ScopedRuntimeStateStore(store, credential_scope("debridlink", KEY)), integration_id="debridlink",
        clock=lambda: NOW)
    assert restarted.entitlements.public()["functional"] == "unresolved"
    await hosts.start()
    await restarted.account.start()
    assert restarted.entitlements.public()["functional"] == "usable"
    await restarted.account.refresh_now()                                   # fails; keeps the LKG truth
    assert restarted.entitlements.public()["functional"] == "usable"


async def test_account_truth_that_is_not_ready_projects_unresolved_and_routing_is_unchanged():
    from transfers.entitlement import (
        CONNECTION_FAILED_ENTITLEMENTS, UNRESOLVED_ENTITLEMENTS, EntitlementReadiness, ProviderEntitlements)
    assert UNRESOLVED_ENTITLEMENTS.public()["functional"] == "unresolved"
    assert UNRESOLVED_ENTITLEMENTS.public()["entitlement"] == "unresolved"
    assert CONNECTION_FAILED_ENTITLEMENTS.public()["functional"] == "unresolved"
    assert CONNECTION_FAILED_ENTITLEMENTS.public()["entitlement"] == "connection_failed"
    # Routing reads admits() only, exactly as before.
    for kind in ("http", "https", "magnet", "torrent"):
        assert UNRESOLVED_ENTITLEMENTS.admits(kind) is None
        assert CONNECTION_FAILED_ENTITLEMENTS.admits(kind) is True
    ready = ProviderEntitlements(EntitlementReadiness.READY, frozenset({"magnet"}))
    assert ready.public()["functional"] == "usable" and ready.admits("magnet") is True


async def test_a_last_known_good_snapshot_written_before_tristate_flags_never_claims_zero():
    legacy = json.dumps({"source": HOST_SOURCE, "hosters": [
        {"domains": ["paid.example"], "regexs": [], "isFree": False}]}).encode()
    snapshot = decode_host_snapshot(legacy)
    provider = _debridlink(FREE_ACCOUNT, None)
    provider.applicability = applicability_facts(snapshot)
    provider.applicability_for = DebridLinkRequestApplicability(snapshot)
    await provider.account.refresh_now()
    # A pre-tristate false may have been a malformed native flag: unknown.
    assert provider.entitlements.public()["functional"] == "unresolved"
    assert provider.entitlement_for(TransferRequest("https", "https://paid.example/f/1")) is False
    # A snapshot written now round-trips its explicit false as authoritative.
    current = parse_native_host_snapshot([_hoster("paid.example", False)])
    assert decode_host_snapshot(encode_host_snapshot(current)) == current


async def test_routing_is_identical_whatever_the_usefulness_projection():
    for hosters in ([_hoster("paid.example", False)], [_hoster("free.example", True)],
                    [_hoster("odd.example", "yes")]):
        provider = _debridlink(FREE_ACCOUNT, hosters)
        await provider.account.refresh_now()
        owner = provider.account.entitlements
        projected = provider.entitlements
        for kind in ("http", "https", "magnet", "torrent"):
            assert projected.admits(kind) == owner.admits(kind)
        assert (projected.readiness, projected.request_types, projected.service_class, projected.degraded) \
            == (owner.readiness, owner.request_types, owner.service_class, owner.degraded)


# -- the neutral rule -----------------------------------------------------------------------

async def test_any_usable_class_keeps_the_provider_usable():
    truth = account_entitlements(offered={"http", "https", "magnet", "torrent"}, expected={"http", "https", "magnet"},
                                 entitled={"http", "https", "magnet"}, service_class=AccountServiceClass.STANDARD)
    assert truth.with_surface({"http", "https"}, False).public()["functional"] == "usable"     # magnet remains
    hosters_only = account_entitlements(offered={"http", "https"}, expected={"http", "https"},
                                        entitled={"http", "https"}, service_class=AccountServiceClass.STANDARD)
    assert hosters_only.with_surface({"http", "https"}, False).public()["functional"] == "degraded"
    assert hosters_only.with_surface({"http", "https"}, None).public()["functional"] == "unresolved"
    assert hosters_only.with_surface({"http", "https"}, True).public()["functional"] == "usable"
    assert hosters_only.with_surface({"http", "https"}, True) == hosters_only
    # A surface never widens or narrows routing.
    narrowed = hosters_only.with_surface({"http", "https"}, False)
    assert narrowed.admits("https") is True and narrowed.request_types == hosters_only.request_types
    assert set(narrowed.public()) == set(hosters_only.public())


# -- AllDebrid control ----------------------------------------------------------------------

class _AllDebridClient:
    api_key = KEY

    async def get_user(self):
        return {"user": {"username": "someone", "isPremium": False, "premiumUntil": 0}}


def _ad_hosts(*types):
    return {"hosts": {f"svc{index}": {"name": f"svc{index}", "type": kind, "domains": [f"h{index}.example"],
                                      "regexps": [rf"https?://h{index}\.example/.+"]}
                      for index, kind in enumerate(types)}}


def _alldebrid(snapshot):
    client = _AllDebridClient()
    provider = AllDebridProvider(KEY, client=client)
    provider.applicability_for = AllDebridRequestApplicability(snapshot)
    provider.account = AccountEntitlementMaintenance(
        provider, ad_accounts.AllDebridAccountTranslation(client, clock=lambda: NOW),
        ScopedRuntimeStateStore(Store(), credential_scope("alldebrid", KEY)), integration_id="alldebrid",
        clock=lambda: NOW)
    return provider


async def test_alldebrid_non_premium_with_no_free_host_is_degraded():
    status = await ad_admin.runtime_status(_alldebrid(ad_snapshot(_ad_hosts("premium", "premium"))))
    assert status["state"] == "healthy" and status["account"]["service_class"] == "standard"
    assert status["account"]["functional"] == "degraded"


async def test_alldebrid_non_premium_with_one_free_host_is_usable():
    status = await ad_admin.runtime_status(_alldebrid(ad_snapshot(_ad_hosts("premium", "free"))))
    assert status["account"]["functional"] == "usable"


async def test_alldebrid_malformed_host_truth_is_unresolved_and_routing_stays_closed():
    with pytest.raises(AllDebridHostSnapshotError):
        ad_snapshot(_ad_hosts("premium", "sometimes"))           # refused whole: never a partial zero
    provider = _alldebrid(None)                                    # no authoritative host truth
    status = await ad_admin.runtime_status(provider)
    assert status["state"] == "healthy" and status["account"]["functional"] == "unresolved"
    # Without host truth AllDebrid is an unresolved specialized claimant: it
    # cannot win, and it holds generic fallback (unchanged).
    assert provider.applicability_for(TransferRequest("https", "https://h0.example/x")).readiness.value == "unresolved"


# -- unchanged providers and presentation ----------------------------------------------------

async def test_a_provider_without_account_specific_applicability_projects_its_account_truth_unchanged():
    provider = RealDebridProvider(SimpleNamespace(configured=True, secrets=lambda: ()))
    owner = SimpleNamespace(entitlements=account_entitlements(
        offered={"http", "https"}, expected={"http", "https"}, entitled={"http", "https"},
        service_class=AccountServiceClass.STANDARD))
    provider.account = owner
    assert provider.entitlements is owner.entitlements


async def test_no_provider_name_decides_usefulness_or_its_presentation():
    root = Path(__file__).resolve().parents[1]
    neutral = (root / "transfers" / "entitlement.py").read_text().casefold()
    status_js = (root.parent / "frontend" / "static" / "ui-provider-status.js").read_text()
    adjusted = status_js[status_js.index("function accountAdjusted"):]
    adjusted = adjusted[:adjusted.index("\n  }\n") + 4].casefold()
    for name in ("debridlink", "debrid-link", "alldebrid", "realdebrid", "torbox", "isfree"):
        assert name not in neutral and name not in adjusted
    # Unknown usefulness is never the healthy (green) state.
    assert re.search(r"functional\s*===\s*'unresolved'", adjusted)
