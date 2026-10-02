"""Neutral account entitlement for account-backed providers.

Implementation capability (the static descriptor), health, account
entitlement and request applicability are four separate dimensions, and the
one registry intersects them for NEW work only. Unknown entitlement is
unresolved, never absent; known absence yields cleanly; a definitive refusal
contracts only what it proved; temporary limits change nothing; account truth
is scoped to its credential and a known expiry is binding.

The neutral half uses fixtures that name no integration. The provider half
proves AllDebrid's, Real-Debrid's and TorBox's own translations, table-driven.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from integrations.account_entitlement import AccountEntitlementMaintenance
from integrations.runtime_state import ScopedRuntimeStateStore, credential_scope
from providers.alldebrid import account as alldebrid
from providers.alldebrid.client import AllDebridAPIError
from providers.alldebrid.host_runtime import AllDebridRequestApplicability, parse_native_host_snapshot
from providers.alldebrid.provider import AllDebridProvider
from providers.realdebrid import account as realdebrid
from providers.realdebrid.client import RealDebridAPIError
from providers.torbox import account as torbox
from providers.torbox.client import TorBoxAPIError
from providers.torbox.host_runtime import TorBoxHostMaintenance
from providers.torbox.provider import TorBoxProvider
from test_v113_torbox_provider import MAGNET, FakeClient
from transfers.applicability import ApplicabilityUnresolved, ProviderApplicability
from transfers.entitlement import (
    AccountServiceClass, EntitlementReadiness, ProviderEntitlements, account_entitlements,
)
from transfers.errors import TransferError
from transfers.models import Capability, IntegrationDescriptor, TransferRequest
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

NOW = 1_800_000_000.0
LATER, EARLIER = NOW + 30 * 86400, NOW - 86400
ALL = frozenset({"magnet", "torrent", "http", "https"})
READY = EntitlementReadiness.READY


# -- neutral fixtures ---------------------------------------------------------------

class Lab:
    """A neutral provider; ``entitlements`` is its account truth (or absent)."""

    def __init__(self, identity, *, kinds=("magnet",), priority=0, entitlements=..., enabled=True):
        self.descriptor = IntegrationDescriptor(identity, identity, frozenset({Capability.RESOLVE}),
                                                request_types=frozenset(kinds), priority=priority, enabled=enabled)
        self.applicability = ProviderApplicability()
        if entitlements is not ...:
            self.entitlements = entitlements

    async def resolve(self, request):  # pragma: no cover - selection only
        raise AssertionError("selection never resolves")


def ready(*kinds, service=AccountServiceClass.PREMIUM):
    return ProviderEntitlements(READY, frozenset(kinds), service)


def registry(*providers):
    value = IntegrationRegistry()
    for provider in providers:
        value.register_provider(provider)
    return value


MAGNET_REQUEST = TransferRequest("magnet", MAGNET, "Show")


# -- 16: the neutral registry -------------------------------------------------------

async def test_a_ready_entitled_provider_participates():
    first = Lab("alpha", priority=10, entitlements=ready("magnet"))
    assert registry(first, Lab("beta")).provider_for(MAGNET_REQUEST) is first


async def test_a_ready_provider_not_entitled_to_the_class_yields_cleanly():
    first = Lab("alpha", priority=10, entitlements=ready("http", service=AccountServiceClass.STANDARD))
    second = Lab("beta")
    routes = registry(first, second)
    assert routes.provider_for(MAGNET_REQUEST) is second
    assert routes.eligible_providers(MAGNET_REQUEST) == (second,)


async def test_unresolved_entitlement_blocks_only_a_premature_lower_fallback():
    unknown = Lab("alpha", priority=10, entitlements=ProviderEntitlements())
    lower = Lab("beta")
    routes = registry(unknown, lower)
    with pytest.raises(ApplicabilityUnresolved) as raised:
        routes.provider_for(MAGNET_REQUEST)
    assert raised.value.provider_ids == ("alpha",)
    # It is still a possible owner: the request waits for it, never ends.
    assert routes.eligible_providers(MAGNET_REQUEST) == (unknown, lower)
    # A class it could never own is not held back by it.
    other = Lab("gamma", kinds=("parcel",))
    assert registry(unknown, other).provider_for(TransferRequest("parcel", "box")) is other
    # A higher-ranked entitled provider is never held back by a lower unknown one.
    assert registry(Lab("zeta", priority=20), unknown).provider_for(MAGNET_REQUEST).descriptor.id == "zeta"
    # Once truth resolves to not-entitled, the lower provider competes.
    unknown.entitlements = ready("http")
    assert routes.provider_for(MAGNET_REQUEST) is lower


async def test_a_provider_without_the_contract_keeps_its_behaviour():
    plain = Lab("alpha", priority=10)
    absent = Lab("beta", priority=5, entitlements=None)
    routes = registry(plain, absent)
    assert routes.eligible_providers(MAGNET_REQUEST) == (plain, absent)
    assert IntegrationRegistry.entitlement_for(plain, MAGNET_REQUEST) is True
    assert IntegrationRegistry.entitlement_for(absent, MAGNET_REQUEST) is True


async def test_a_disabled_provider_never_participates_whatever_its_entitlement():
    disabled = Lab("alpha", priority=10, entitlements=ready("magnet"), enabled=False)
    lower = Lab("beta")
    assert registry(disabled, lower).provider_for(MAGNET_REQUEST) is lower


async def test_entitlement_never_touches_a_bound_route_or_a_member_of_one():
    refused = Lab("alpha", kinds=("magnet", "https"), entitlements=ready())
    refused.applicability = ProviderApplicability(generic_schemes=frozenset({"https"}))
    routes = registry(refused)
    # The bound owner still observes, continues and cleans up what it owns.
    assert routes.provider_for_bound_route("alpha", MAGNET_REQUEST) is refused
    assert routes.provider_for_bound_continuation("alpha", MAGNET_REQUEST) is refused
    # A member of an existing route is not new acquisition.
    member = TransferRequest("https", "https://alpha.example/member/1")
    assert routes.provider_for(member, acquisition=False) is refused
    with pytest.raises(TransferError):
        routes.provider_for(member)


async def test_health_is_not_entitlement_and_a_failing_connection_is_health_s():
    entitled = Lab("alpha", priority=10, entitlements=ready("magnet"))
    lower = Lab("beta")
    routes = registry(entitled, lower)
    routes.mark_health("alpha", healthy=False)
    assert routes.provider_for(MAGNET_REQUEST) is lower
    routes.mark_health("alpha", healthy=True)
    # A connection that cannot even establish account truth is not "unknown
    # entitlement": it competes exactly as before, and its ordinary failure
    # (credential, network) takes the established path.
    entitled.entitlements = ProviderEntitlements(EntitlementReadiness.CONNECTION_FAILED)
    assert routes.provider_for(MAGNET_REQUEST) is entitled


@pytest.mark.parametrize("offered, expected, entitled, degraded", [
    (ALL, ALL, ALL, False),                                    # everything it should have
    (ALL, {"magnet", "torrent"}, {"magnet", "torrent"}, False),  # a narrower plan is not degraded
    (ALL, ALL, {"http", "https"}, True),                        # lost what its plan includes
    (ALL, {"magnet", "torrent"}, set(), True),                  # contracted to nothing useful
    (ALL | {"nzb"}, ALL | {"nzb"}, ALL, True),                  # enabled family it may not begin
])
async def test_degraded_means_lost_capability_never_never_had(offered, expected, entitled, degraded):
    value = account_entitlements(offered=offered, expected=expected, entitled=entitled,
                                 service_class=AccountServiceClass.STANDARD)
    assert value.degraded is degraded
    assert value.request_types == frozenset(offered) & frozenset(entitled)


async def test_neutral_owners_name_no_provider_plan_or_native_code():
    root = Path(__file__).resolve().parents[1]
    neutral = [root / "transfers" / "registry.py", root / "transfers" / "entitlement.py",
               root / "integrations" / "account_entitlement.py", root / "integrations" / "runtime_state.py",
               root.parent / "frontend" / "static" / "ui-provider-status.js",
               root.parent / "frontend" / "static" / "ui-premium-account-status.js"]
    forbidden = ("alldebrid", "realdebrid", "real-debrid", "torbox", "essential", "pro plan",
                 "plan_restricted", "must_be_premium", "magnet_must_be_premium", "plan == 0", "plan === 0")
    for path in neutral:
        text = path.read_text().casefold()
        for name in forbidden:
            assert name not in text, f"{path.name} names {name}"


# -- account truth: scope, LKG, expiry, contraction, wake ---------------------------------

class Store:
    def __init__(self):
        self.records = {}

    async def load(self, integration_id, state_key):
        return self.records.get((integration_id, state_key))

    async def replace(self, integration_id, payload, *, schema_version, state_key, observed_at, successful_at,
                      stale_after, expected_generation):
        record = SimpleNamespace(payload=payload, schema_version=schema_version,
                                 generation=expected_generation + 1, stale_after=stale_after)
        self.records[(integration_id, state_key)] = record
        return record


class Account:
    """A neutral account translation: facts are {"premium": bool, "until": t}."""

    schema_version = "lab-account-v1"

    def __init__(self, answer):
        self.answer = answer
        self.fetches = 0

    def configured(self):
        return True

    async def fetch(self):
        self.fetches += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    @staticmethod
    def facts(native):
        if not isinstance(native, dict) or not isinstance(native.get("premium"), bool):
            raise ValueError("malformed")
        return {"premium": native["premium"], "until": native.get("until")}

    @staticmethod
    def connection_failed(exc):
        return isinstance(exc, PermissionError)

    @staticmethod
    def derive(facts, *, offered, contracted, now):
        current = facts["premium"] and (facts["until"] is None or facts["until"] > now)
        expected = ALL if facts["premium"] else {"http", "https"}
        return account_entitlements(
            offered=offered, expected=expected, entitled=(ALL if current else {"http", "https"}) - contracted,
            service_class=AccountServiceClass.PREMIUM if current else AccountServiceClass.STANDARD,
            expires_at=facts["until"] if current else None)


def owner(answer, store, *, scope="first", clock=None, woken=None, statuses=None):
    provider = Lab("alpha", kinds=ALL)
    translation = Account(answer)
    now = clock or [NOW]

    async def status():
        statuses.append(True)

    value = AccountEntitlementMaintenance(
        provider, translation, ScopedRuntimeStateStore(store, credential_scope("alpha", scope)),
        integration_id="alpha", notify=(woken.append if woken is not None else None),
        notify_status=(status if statuses is not None else None), clock=lambda: now[0])
    provider.entitlements_owner = value
    return provider, value, translation, now


async def test_unknown_until_account_truth_arrives_and_restored_from_scoped_lkg_without_a_fetch():
    store = Store()
    _provider, first, translation, _now = owner({"premium": True, "until": LATER}, store)
    assert first.entitlements.readiness == EntitlementReadiness.UNRESOLVED
    await first.start()
    assert translation.fetches == 0
    await first.maintain()
    assert first.entitlements.readiness == READY and first.entitlements.request_types == ALL

    _provider, restarted, again, _now = owner(RuntimeError("down"), store)
    await restarted.start()
    assert again.fetches == 0 and restarted.entitlements.request_types == ALL


async def test_a_replaced_credential_inherits_nothing():
    store = Store()
    _provider, first, _translation, _now = owner({"premium": True, "until": LATER}, store, scope="key-one")
    await first.maintain()
    _provider, other, translation, _now = owner(RuntimeError("down"), store, scope="key-two")
    await other.start()
    assert other.entitlements.readiness == EntitlementReadiness.UNRESOLVED
    translation.answer = {"premium": False}
    await other.maintain()
    assert other.entitlements.request_types == {"http", "https"}
    assert all("key-one" not in key and "key-two" not in key for _id, key in store.records)


async def test_a_known_expiry_binds_even_when_every_refresh_fails():
    store, woken, statuses = Store(), [], []
    _provider, value, translation, now = owner({"premium": True, "until": NOW + 60}, store,
                                               woken=woken, statuses=statuses)
    await value.maintain()
    assert value.entitlements.service_class == AccountServiceClass.PREMIUM
    woken.clear(), statuses.clear()
    translation.answer = RuntimeError("refresh fails from here on")
    now[0] = NOW + 61
    # Routing reads the derived truth at once -- no refresh, no timer needed.
    assert value.entitlements.service_class == AccountServiceClass.STANDARD
    assert value.entitlements.request_types == {"http", "https"} and value.entitlements.degraded
    # The existing maintenance cadence tells routing and status it changed.
    await value.maintain()
    assert woken == ["alpha"] and statuses == [True]
    await value.maintain()
    assert woken == ["alpha"]          # announced once, not every cycle


async def test_a_definitive_refusal_contracts_only_its_classes_durably_until_truth_changes():
    store, woken = Store(), []
    _provider, value, translation, _now = owner({"premium": True, "until": LATER}, store, woken=woken)
    await value.maintain()
    woken.clear()
    await value.contract({"magnet", "torrent"})
    assert value.entitlements.request_types == {"http", "https"} and value.entitlements.degraded
    assert woken == ["alpha"]
    _provider, restarted, _again, _now = owner(RuntimeError("down"), store)
    await restarted.start()
    assert restarted.entitlements.request_types == {"http", "https"}
    # Unchanged account truth keeps the proven refusal...
    await restarted.observe({"premium": True, "until": LATER})
    assert restarted.entitlements.request_types == {"http", "https"}
    # ...a changed one (a renewal) may expand capability again.
    await restarted.observe({"premium": True, "until": LATER + 86400})
    assert restarted.entitlements.request_types == ALL


async def test_a_failing_connection_without_truth_defers_to_health():
    _provider, value, _translation, _now = owner(PermissionError("credential refused"), Store())
    await value.maintain()
    assert value.entitlements.readiness == EntitlementReadiness.CONNECTION_FAILED
    assert value.entitlements.admits("magnet") is True


async def test_malformed_account_answers_change_nothing():
    _provider, value, _translation, _now = owner({"premium": True, "until": LATER}, Store())
    await value.maintain()
    await value.observe({"premium": "yes"})
    assert value.entitlements.request_types == ALL


# -- 15: TorBox --------------------------------------------------------------------

TB_OFFERED = frozenset({"magnet", "torrent", "http", "https"})


@pytest.mark.parametrize("plan, until, usenet, service, kinds, degraded, label", [
    (0, None, False, "standard", {"magnet", "torrent"}, False, "Free"),        # Free still has torrents
    (1, LATER, False, "premium", TB_OFFERED, False, "Essential"),
    (3, LATER, False, "premium", TB_OFFERED, False, "Standard"),
    (2, LATER, False, "premium", TB_OFFERED, False, "Pro"),
    (2, LATER, True, "premium", TB_OFFERED | {"nzb"}, False, "Pro"),
    (1, LATER, True, "premium", TB_OFFERED, True, "Essential"),                # Usenet toggle, plan lacks it
    (0, None, True, "standard", {"magnet", "torrent"}, True, "Free"),
    (2, EARLIER, False, "standard", {"magnet", "torrent"}, True, "Free"),      # lapsed paid plan
])
async def test_torbox_plans_translate_to_neutral_entitlement(plan, until, usenet, service, kinds, degraded, label):
    offered = TB_OFFERED | ({"nzb"} if usenet else set())
    value = torbox.entitlement({"plan": plan, "premium_until": until}, offered=offered, now=NOW)
    assert (value.service_class.value, value.request_types, value.degraded, value.plan) == (
        service, frozenset(kinds), degraded, label)
    assert value.expires_at == (until if service == "premium" else None)


@pytest.mark.parametrize("code, kind, contracted", [
    ("PLAN_RESTRICTED_FEATURE", "magnet", {"magnet", "torrent"}),
    ("PLAN_RESTRICTED_FEATURE", "https", {"http", "https"}),
    ("PLAN_RESTRICTED_FEATURE", "nzb", {"nzb"}),
    ("ACTIVE_LIMIT", "magnet", set()),
    ("COOLDOWN_LIMIT", "magnet", set()),
    ("MONTHLY_LIMIT", "magnet", set()),
    ("DOWNLOAD_TOO_LARGE", "magnet", set()),
    ("BAD_TOKEN", "magnet", set()),
])
async def test_torbox_only_a_plan_refusal_contracts_and_only_its_family(code, kind, contracted):
    assert torbox.refused_family(TorBoxAPIError(code, "", 403), kind) == frozenset(contracted)


def torbox_with_account(client, store, *, usenet=False, clock=lambda: NOW):
    provider = TorBoxProvider(client, usenet=usenet)
    TorBoxHostMaintenance(provider, Store())
    provider.account = AccountEntitlementMaintenance(
        provider, torbox.TorBoxAccountTranslation(client),
        ScopedRuntimeStateStore(store, credential_scope("torbox", client.token)),
        integration_id="torbox", clock=clock)
    return provider


async def test_torbox_free_plan_refusal_contracts_torrents_for_that_account_and_routing_yields():
    client = FakeClient()
    client.user = lambda: _answer({"plan": 0, "premium_expires_at": None})
    store = Store()
    provider = torbox_with_account(client, store)
    await provider.account.maintain()
    lower = Lab("beta", kinds=("magnet",), priority=-10)
    routes = registry(provider, lower)
    assert routes.provider_for(MAGNET_REQUEST) is provider       # docs: Free may add torrents

    client.refusal = TorBoxAPIError("PLAN_RESTRICTED_FEATURE", "higher plans only", 403)
    with pytest.raises(TransferError):
        await provider.resolve(MAGNET_REQUEST)
    assert provider.entitlements.request_types == frozenset()
    assert provider.entitlements.degraded
    assert routes.provider_for(MAGNET_REQUEST) is lower           # no known-impossible claim again

    # A temporary limit is not an entitlement change.
    other = torbox_with_account(FakeClient(token="another-account"), Store())
    other.client.user = lambda: _answer({"plan": 2, "premium_expires_at": "2099-01-01T00:00:00Z"})
    await other.account.maintain()
    other.client.refusal = TorBoxAPIError("ACTIVE_LIMIT", "", 403)
    with pytest.raises(TransferError):
        await other.resolve(MAGNET_REQUEST)
    assert "magnet" in other.entitlements.request_types


async def test_torbox_web_downloads_need_both_entitlement_and_a_usable_supported_hoster():
    client = FakeClient()
    client.user = lambda: _answer({"plan": 0, "premium_expires_at": None})
    provider = torbox_with_account(client, Store())
    maintenance = TorBoxHostMaintenance(provider, Store(), clock=lambda: NOW)
    await maintenance.maintain()
    await provider.account.maintain()
    supported = TransferRequest("https", "https://hoster.example/f/1")
    routes = registry(provider)
    # Free: the hoster is supported, but the account may not begin web downloads.
    with pytest.raises(TransferError):
        routes.provider_for(supported)
    await provider.account.observe({"plan": 1, "premium_expires_at": "2099-01-01T00:00:00Z"})
    assert routes.provider_for(supported) is provider
    # Entitled, but an unlisted host is still nobody's claim.
    with pytest.raises(TransferError):
        routes.provider_for(TransferRequest("https", "https://unlisted.example/f/1"))


async def test_torbox_usenet_needs_the_toggle_and_the_plan():
    def nzb_provider(plan, usenet):
        client = FakeClient()
        provider = torbox_with_account(client, Store(), usenet=usenet)
        provider.account._facts = torbox.account_facts({"plan": plan, "premium_expires_at": "2099-01-01T00:00:00Z"})
        return provider

    nzb = TransferRequest("nzb", "staged", "show.nzb")
    assert IntegrationRegistry.entitlement_for(nzb_provider(2, True), nzb) is True
    assert IntegrationRegistry.entitlement_for(nzb_provider(1, True), nzb) is False
    assert registry(nzb_provider(2, False)).eligible_providers(nzb) == ()


async def _answer(value):
    return value


# -- 15: Real-Debrid ---------------------------------------------------------------------

@pytest.mark.parametrize("answer, service, kinds, degraded", [
    ({"type": "premium", "expiration": "2099-01-01T00:00:00Z"}, "premium", ALL, False),
    ({"type": "free"}, "standard", {"http", "https"}, False),
    ({"type": "free", "expiration": "2001-01-01T00:00:00Z"}, "standard", {"http", "https"}, True),   # lapsed
    ({"type": "premium", "expiration": "2001-01-01T00:00:00Z"}, "standard", {"http", "https"}, True),  # stale LKG
])
async def test_realdebrid_account_types_translate_to_neutral_entitlement(answer, service, kinds, degraded):
    value = realdebrid.entitlement(realdebrid.account_facts(answer), offered=ALL, now=NOW)
    assert (value.service_class.value, value.request_types, value.degraded) == (service, frozenset(kinds), degraded)


@pytest.mark.parametrize("code, kind, contracted", [
    (9, "magnet", {"magnet", "torrent"}),      # permission denied: torrents are premium
    (9, "https", set()),
    (14, "magnet", set()),                     # a locked account is not a plan fact
    (20, "https", set()),                      # one hoster unavailable to free users
    (23, "magnet", set()), (36, "magnet", set()), (18, "https", set()), (17, "https", set()),
])
async def test_realdebrid_only_a_torrent_permission_refusal_contracts(code, kind, contracted):
    assert realdebrid.refused_family(RealDebridAPIError(code, "refused", 403), kind) == frozenset(contracted)


async def test_realdebrid_free_account_does_not_claim_torrents_but_keeps_host_applicability_separate():
    value = realdebrid.entitlement(realdebrid.account_facts({"type": "free"}), offered=ALL, now=NOW)
    lab = Lab("rd-like", kinds=ALL, priority=10, entitlements=value)
    lower = Lab("beta", kinds=("magnet",))
    assert registry(lab, lower).provider_for(MAGNET_REQUEST) is lower
    assert IntegrationRegistry.entitlement_for(lab, TransferRequest("https", "https://h.example/x")) is True


# -- 15: AllDebrid ---------------------------------------------------------------------------

@pytest.mark.parametrize("answer, service, kinds", [
    ({"user": {"isPremium": True, "premiumUntil": int(LATER)}}, "premium", ALL),
    ({"user": {"isPremium": False, "premiumUntil": 0}}, "standard", {"http", "https"}),
    ({"isPremium": True, "premiumUntil": int(EARLIER)}, "standard", {"http", "https"}),
])
async def test_alldebrid_premium_state_translates_to_neutral_entitlement(answer, service, kinds):
    value = alldebrid.entitlement(alldebrid.account_facts(answer), offered=ALL, now=NOW)
    assert (value.service_class.value, value.request_types) == (service, frozenset(kinds))


@pytest.mark.parametrize("code, kind, contracted", [
    ("MAGNET_MUST_BE_PREMIUM", "magnet", {"magnet", "torrent"}),
    ("MUST_BE_PREMIUM", "https", set()),               # one premium host's link, not the feature
    ("FREE_TRIAL_LIMIT_REACHED", "magnet", set()),
    ("MAGNET_TOO_MANY_ACTIVE", "magnet", set()),
])
async def test_alldebrid_only_the_magnet_premium_refusal_contracts(code, kind, contracted):
    assert alldebrid.refused_family(code, kind) == frozenset(contracted)


HOSTS = {"hosts": {
    "freehost": {"name": "freehost", "type": "free", "domains": ["free.example"], "regexps": [r"free\.example/.+"]},
    "paidhost": {"name": "paidhost", "type": "premium", "domains": ["paid.example"], "regexps": [r"paid\.example/.+"]},
}}


async def test_alldebrid_account_entitlement_intersects_typed_host_claims():
    provider = AllDebridProvider("key", client=SimpleNamespace(api_key="key"))
    provider.applicability_for = AllDebridRequestApplicability(parse_native_host_snapshot(HOSTS))
    free_link = TransferRequest("https", "https://free.example/file")
    paid_link = TransferRequest("https", "https://paid.example/file")
    standard = alldebrid.entitlement(alldebrid.account_facts({"isPremium": False, "premiumUntil": 0}),
                                     offered=ALL, now=NOW)
    provider.account = SimpleNamespace(entitlements=standard)
    assert provider.entitlement_for(free_link) is True
    assert provider.entitlement_for(paid_link) is False
    assert provider.entitlement_for(MAGNET_REQUEST) is False
    # Structural applicability (regexp/path) still decides which links are claims at all.
    assert provider.applicability_for(free_link).specialized_hosts
    assert not provider.applicability_for(TransferRequest("https", "https://free.example/")).specialized_hosts
    premium = alldebrid.entitlement(alldebrid.account_facts({"isPremium": True, "premiumUntil": int(LATER)}),
                                    offered=ALL, now=NOW)
    provider.account = SimpleNamespace(entitlements=premium)
    assert provider.entitlement_for(paid_link) is True and provider.entitlement_for(MAGNET_REQUEST) is True


async def test_alldebrid_magnet_premium_refusal_contracts_through_its_account_owner():
    contracted = []

    class Client:
        api_key = "key"

        async def upload_magnet(self, magnet):
            raise AllDebridAPIError("MAGNET_MUST_BE_PREMIUM", "You must be premium to use this feature.")

    provider = AllDebridProvider("key", client=Client())

    async def contract(kinds):
        contracted.append(frozenset(kinds))

    provider.account = SimpleNamespace(entitlements=None, contract=contract)
    with pytest.raises(TransferError):
        await provider.resolve(MAGNET_REQUEST)
    assert contracted == [frozenset({"magnet", "torrent"})]


# -- 14 / 12: through the real engine, beside Gap A ------------------------------------

async def _engine(tmp_path, monkeypatch, *providers):
    from test_v113_provider_exhaustion_failover import lab
    return await lab(tmp_path, monkeypatch, *providers)


async def test_unknown_entitlement_holds_the_request_until_truth_says_it_yields(tmp_path, monkeypatch):
    from test_v113_provider_exhaustion_failover import RouteLab, drive, root, submit
    unknown, lower = RouteLab("alpha-route", priority=10), RouteLab("beta-route")
    unknown.entitlements = ProviderEntitlements()
    repository, engine = await _engine(tmp_path, monkeypatch, unknown, lower)
    transfer = await submit(engine)

    await drive(engine)
    assert unknown.resolved == [] and lower.resolved == []
    assert (await root(repository, transfer.id)).state == "pending"

    unknown.entitlements = ready("http", service=AccountServiceClass.STANDARD)
    await drive(engine)
    assert unknown.resolved == [] and lower.resolved == ["logical-object"]


async def test_a_definitive_refusal_contracts_while_gap_a_fails_over_the_same_request(tmp_path, monkeypatch):
    from test_v113_provider_exhaustion_failover import RouteLab, drive, root, routes
    client = FakeClient()
    client.user = lambda: _answer({"plan": 0, "premium_expires_at": None})
    provider = torbox_with_account(client, Store())
    await provider.account.maintain()
    lower = RouteLab("beta-route", kinds=("magnet",), priority=-10)
    repository, engine = await _engine(tmp_path, monkeypatch, provider, lower)
    client.refusal = TorBoxAPIError("PLAN_RESTRICTED_FEATURE", "higher plans only", 403)

    transfer = await engine.submit((TransferRequest("magnet", MAGNET, "Show", "a" * 40),), name="Show",
                                   deduplicate=False)
    await drive(engine)
    record = await root(repository, transfer.id)
    assert [(item["provider_id"], item["resolution_state"]) for item in await routes(repository, transfer.id)][:2] == [
        ("torbox", "exhausted"), ("beta-route", "succeeded")]
    assert provider.entitlements.request_types == frozenset()
    assert await repository.exhausted_route_providers(record.id) == frozenset({"torbox"})

    calls = len(client.calls)
    second = await engine.submit((TransferRequest("magnet", MAGNET.replace("a" * 40, "b" * 40), "Other"),),
                                 name="Other", deduplicate=False)
    await drive(engine)
    assert len(client.calls) == calls                          # never asked again
    assert [item["provider_id"] for item in await routes(repository, second.id)] == ["beta-route"]


# -- 8.4: the status surface projects the routing truth ------------------------------

@pytest.mark.parametrize("answer, refused, service, functional", [
    ({"email": "a@e.net", "plan": 2, "premium_expires_at": "2099-01-01T00:00:00Z"}, False, "premium", "usable"),
    ({"email": "f@e.net", "plan": 0, "premium_expires_at": None}, False, "standard", "usable"),
    ({"email": "f@e.net", "plan": 0, "premium_expires_at": None}, True, "standard", "degraded"),
    ({"email": "l@e.net", "plan": 1, "premium_expires_at": "2001-01-01T00:00:00Z"}, False, "standard", "degraded"),
])
async def test_torbox_status_carries_the_same_neutral_account_truth_routing_uses(answer, refused, service, functional):
    from providers.torbox import admin
    client = FakeClient()
    client.user = lambda: _answer(answer)
    provider = torbox_with_account(client, Store(), clock=lambda: NOW)
    if refused:
        await provider.account.observe(answer)
        await provider.account.contract({"magnet", "torrent"})
    status = await admin.runtime_status(provider, enabled=True)
    assert status["state"] == "healthy"                       # entitlement never masquerades as offline
    account = status["account"]
    assert (account["service_class"], account["functional"], account["entitlement"]) == (service, functional, "ready")
    assert account == provider.entitlements.public()
    assert set(account) == {"entitlement", "service_class", "functional", "request_types", "plan", "expires_at"}
