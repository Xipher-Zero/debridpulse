"""Routing decision trace (canonical visibility corrective, A).

The canonical selector records, at the moment it decides, one bounded neutral
disposition for every provider it considered for a root request: the route
attempt it starts carries that decision, and a decision that started no
attempt (held, or nothing can take the request) stays on the request. Nothing
is reconstructed later, nothing is evaluated for the trace, and a recording
failure never changes routing.
"""
from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import db.database as database
from integrations.account_entitlement import AccountEntitlementMaintenance
from integrations.runtime_state import ScopedRuntimeStateStore, credential_scope
from providers.debridlink import account as dl_accounts
from providers.debridlink.host_runtime import (
    DebridLinkRequestApplicability, applicability_facts, parse_native_host_snapshot,
)
from providers.debridlink.provider import DebridLinkProvider
from providers.general_http.provider import GeneralHttpProvider
from providers.realdebrid.provider import RealDebridProvider
from services import transfer_trace
from test_v113_debridlink_provider import KEY, NOW, Store, ok, service
from transfers import registry as registry_module
from transfers.applicability import ApplicabilityReadiness, HostClaim, HostClaimScope, ProviderApplicability
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage
from transfers.models import ResolutionResult, ResourceState, TransferRequest
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

PREMIUM_ONLY = "https://paid.example/f/abc123?token=private-capability"
FREE_LINK = "https://free.example/f/xyz789"
HOSTERS = [
    {"name": "paid", "type": "host", "domains": ["paid.example"], "regexs": [], "isFree": False},
    {"name": "free", "type": "host", "domains": ["free.example"], "regexs": [], "isFree": True},
]
_WAIT = ResolutionResult(ResourceState.UNKNOWN, error=NormalizedError(
    Domain.PROVIDER, Category.PROVIDER_UNAVAILABLE, Stage.RESOLUTION, retryability=Retryability.BACKOFF))


def _debridlink(account=None, *, hosters=HOSTERS, enabled=True):
    client, transport = service({("GET", "account/infos"): [ok(account or {"accountType": 0, "premiumLeft": 0})] * 4},
                                key=KEY if enabled else "")
    provider = DebridLinkProvider(client)
    snapshot = parse_native_host_snapshot(hosters)
    provider.applicability = applicability_facts(snapshot)
    provider.applicability_for = DebridLinkRequestApplicability(snapshot)
    provider.account = AccountEntitlementMaintenance(
        provider, dl_accounts.DebridLinkAccountTranslation(client, clock=lambda: NOW),
        ScopedRuntimeStateStore(Store(), credential_scope("debridlink", KEY)), integration_id="debridlink",
        clock=lambda: NOW)
    provider.resolve = AsyncMock(return_value=_WAIT)
    return provider, transport


def _general():
    provider = GeneralHttpProvider()
    provider.resolve = AsyncMock(return_value=_WAIT)
    return provider


def _realdebrid():
    provider = RealDebridProvider(SimpleNamespace(configured=True, secrets=lambda: ()))
    facts = ProviderApplicability(specialized_hosts=(HostClaim("paid.example", HostClaimScope.EXACT,
                                                               frozenset({"https"})),),
                                  specialized=True, readiness=ApplicabilityReadiness.READY)
    provider.applicability = facts
    provider.applicability_for = lambda _request: facts
    provider.resolve = AsyncMock(return_value=_WAIT)
    return provider


async def _engine(tmp_path, monkeypatch, *providers):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    for provider in providers:
        registry.register_provider(provider)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"),
                            policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0), clock=lambda: 1000.0)
    await engine.initialize()
    return engine


async def _submit(engine, *urls):
    transfer = await engine.submit(tuple(TransferRequest("https", url, name=f"f{index}.bin")
                                         for index, url in enumerate(urls)), deduplicate=False)
    await engine.resolve_pending()
    return transfer


async def _route_rows(transfer_id):
    async with database.get_db() as db:
        return [dict(row) for row in await db.fetchall(
            """SELECT a.provider_id,p.* FROM route_attempt_provenance p
               JOIN resolution_attempts a ON a.id=p.resolution_attempt_id WHERE p.transfer_id=? ORDER BY p.ordinal""",
            (transfer_id,))]


async def _requests(transfer_id):
    async with database.get_db() as db:
        return [dict(row) for row in await db.fetchall(
            "SELECT * FROM transfer_requests WHERE transfer_id=? ORDER BY ordinal", (transfer_id,))]


def _dispositions(encoded):
    decision = json.loads(encoded)
    return {item["provider_id"]: item["disposition"] for item in decision["providers"]}


async def _traced(transfer_id):
    trace = await transfer_trace.build(transfer_id, None)
    rows = [entry for entry in json.loads(json.dumps(trace["data"]["route_attempt_provenance"], default=str))]
    return trace, rows


# -- the transfer 498 shape -----------------------------------------------------------------

async def test_a_free_account_premium_only_hoster_is_traced_as_not_entitled_where_generic_wins(tmp_path, monkeypatch):
    debridlink, _ = _debridlink()
    await debridlink.account.refresh_now()
    engine = await _engine(tmp_path, monkeypatch, debridlink, _general())
    transfer = await _submit(engine, PREMIUM_ONLY)
    [route] = await _route_rows(transfer.id)
    assert route["provider_id"] == "general_http"
    # The exported trace answers why Debrid-Link did not take it, from the
    # decision itself -- no integration runtime state is needed.
    trace, rows = await _traced(transfer.id)
    assert "integration_runtime_state" not in trace["data"]
    exported = json.dumps(rows)
    assert "debridlink" in exported and "not_entitled" in exported
    assert _dispositions(route["routing_decision"]) == {"debridlink": "not_entitled", "general_http": "selected"}
    decision = json.loads(route["routing_decision"])
    assert decision["outcome"] == "selected"
    assert {item["provider_id"]: item.get("class") for item in decision["providers"]}["general_http"] == "generic"


async def test_an_eligible_specialized_provider_wins_and_generic_is_held_by_its_authority(tmp_path, monkeypatch):
    debridlink, _ = _debridlink()
    await debridlink.account.refresh_now()
    engine = await _engine(tmp_path, monkeypatch, debridlink, _realdebrid(), _general())
    transfer = await _submit(engine, PREMIUM_ONLY)
    [route] = await _route_rows(transfer.id)
    assert route["provider_id"] == "realdebrid"
    assert _dispositions(route["routing_decision"]) == {
        "realdebrid": "selected", "debridlink": "not_entitled", "general_http": "held_by_specialized_authority"}


async def test_a_free_account_free_hoster_is_admitted_and_selected(tmp_path, monkeypatch):
    debridlink, _ = _debridlink()
    await debridlink.account.refresh_now()
    engine = await _engine(tmp_path, monkeypatch, debridlink, _general())
    transfer = await _submit(engine, FREE_LINK)
    [route] = await _route_rows(transfer.id)
    assert route["provider_id"] == "debridlink"
    decision = json.loads(route["routing_decision"])
    assert _dispositions(route["routing_decision"]) == {
        "debridlink": "selected", "general_http": "held_by_specialized_authority"}
    assert {item["provider_id"]: item.get("class") for item in decision["providers"]}["debridlink"] == "specialized"


async def test_a_disabled_provider_is_a_routing_no_op_the_selector_still_names(tmp_path, monkeypatch):
    debridlink, _ = _debridlink(enabled=False)
    assert not debridlink.descriptor.enabled
    engine = await _engine(tmp_path, monkeypatch, debridlink, _general())
    transfer = await _submit(engine, FREE_LINK)
    [route] = await _route_rows(transfer.id)
    assert route["provider_id"] == "general_http"
    assert _dispositions(route["routing_decision"]) == {"debridlink": "disabled", "general_http": "selected"}


async def test_unresolved_entitlement_holds_the_request_and_says_so(tmp_path, monkeypatch):
    debridlink, _ = _debridlink()                    # account truth never arrived
    engine = await _engine(tmp_path, monkeypatch, debridlink, _general())
    writes = []
    record = engine.repository.record_route_decision

    async def counted(*args, **kwargs):
        writes.append(args)
        return await record(*args, **kwargs)

    engine.repository.record_route_decision = counted
    transfer = await _submit(engine, FREE_LINK)
    await engine.resolve_pending()                     # a second cycle, same decision
    assert await _route_rows(transfer.id) == []        # generic fallback stays closed: no attempt at all
    [request] = await _requests(transfer.id)
    assert request["state"] == "pending"
    decision = json.loads(request["routing_decision"])
    assert decision["outcome"] == "held"
    assert _dispositions(request["routing_decision"]) == {
        "debridlink": "entitlement_unresolved", "general_http": "held_by_specialized_authority"}
    assert len(writes) == 1                            # an unchanged hold is written once
    # Once account truth arrives the request routes, and the hold is cleared.
    await debridlink.account.refresh_now()
    await engine.resolve_pending()
    [route] = await _route_rows(transfer.id)
    assert route["provider_id"] == "debridlink"
    assert (await _requests(transfer.id))[0]["routing_decision"] is None


async def test_nothing_can_take_the_request_records_why_on_the_failed_request(tmp_path, monkeypatch):
    debridlink, _ = _debridlink()
    await debridlink.account.refresh_now()
    engine = await _engine(tmp_path, monkeypatch, debridlink)
    transfer = await _submit(engine, PREMIUM_ONLY)
    [request] = await _requests(transfer.id)
    assert request["state"] == "failed"
    assert json.loads(request["routing_decision"])["outcome"] == "unsupported"
    assert _dispositions(request["routing_decision"]) == {"debridlink": "not_entitled"}


async def test_a_structurally_nonmatching_provider_is_not_applicable_and_other_kinds_are_absent(tmp_path, monkeypatch):
    debridlink, _ = _debridlink({"accountType": 1, "premiumLeft": 86400})
    await debridlink.account.refresh_now()
    engine = await _engine(tmp_path, monkeypatch, debridlink, _general())
    transfer = await _submit(engine, "https://unclaimed.example/file.bin")
    [route] = await _route_rows(transfer.id)
    assert _dispositions(route["routing_decision"]) == {"debridlink": "not_applicable", "general_http": "selected"}


# -- campaign facts at the selector ----------------------------------------------------------

def _registry(*providers):
    registry = IntegrationRegistry()
    for provider in providers:
        registry.register_provider(provider)
    return registry


async def test_exhaustion_and_specialized_closure_are_campaign_facts_and_selection_is_unchanged():
    debridlink, _ = _debridlink({"accountType": 1, "premiumLeft": 86400})
    await debridlink.account.refresh_now()
    realdebrid, general = _realdebrid(), _general()
    registry = _registry(debridlink, realdebrid, general)
    request = TransferRequest("https", PREMIUM_ONLY)
    route = registry.provider_route(request, exhausted=frozenset({"realdebrid"}))
    assert route.provider is registry.provider_for(request, exhausted=frozenset({"realdebrid"}))
    assert _dispositions(route.decision.encode()) == {
        "realdebrid": "exhausted", "debridlink": "selected", "general_http": "held_by_specialized_authority"}
    # Every specialized claimant exhausted inside a collection a specialized
    # route owns: generic stays closed by that ownership (TASK1 unchanged).
    closed = registry.provider_route(request, exhausted=frozenset({"realdebrid", "debridlink"}), generic_closed=True)
    assert closed.provider is None and closed.decision.outcome.value == "unsupported"
    assert _dispositions(closed.decision.encode()) == {
        "realdebrid": "exhausted", "debridlink": "exhausted", "general_http": "held_by_specialized_authority"}
    with pytest.raises(Exception):
        closed.require()


# -- secrecy, history, inertness, bounds ------------------------------------------------------

async def test_no_secret_or_native_fact_reaches_the_decision_or_the_trace(tmp_path, monkeypatch):
    debridlink, _ = _debridlink()
    await debridlink.account.refresh_now()
    engine = await _engine(tmp_path, monkeypatch, debridlink, _general())
    transfer = await _submit(engine, PREMIUM_ONLY)
    [route] = await _route_rows(transfer.id)
    raw = route["routing_decision"]
    for forbidden in (KEY, "private-capability", "paid.example", "isFree", "accountType", "token"):
        assert forbidden not in raw
    decision = json.loads(raw)
    assert set(decision) == {"v", "outcome", "providers"}
    assert all(set(item) <= {"provider_id", "disposition", "class"} for item in decision["providers"])
    _trace, rows = await _traced(transfer.id)
    assert KEY not in json.dumps(rows) and "private-capability" not in json.dumps(rows)


async def test_the_trace_keeps_the_decision_time_facts_after_runtime_state_changes(tmp_path, monkeypatch):
    debridlink, _ = _debridlink()
    await debridlink.account.refresh_now()
    engine = await _engine(tmp_path, monkeypatch, debridlink, _general())
    transfer = await _submit(engine, PREMIUM_ONLY)
    _trace, before = await _traced(transfer.id)
    # The account becomes premium and the catalogue changes afterwards.
    await debridlink.account.observe({"accountType": 1, "premiumLeft": 86400})
    snapshot = parse_native_host_snapshot([{"name": "paid", "type": "host", "domains": ["paid.example"],
                                            "regexs": [], "isFree": True}])
    debridlink.applicability_for = DebridLinkRequestApplicability(snapshot)
    _trace, after = await _traced(transfer.id)
    assert json.dumps(after) == json.dumps(before)
    assert "not_entitled" in json.dumps(after)


async def test_a_failing_decision_recorder_never_changes_routing(tmp_path, monkeypatch):
    from transfers.registry import RoutingDecision
    debridlink, _ = _debridlink()
    await debridlink.account.refresh_now()
    engine = await _engine(tmp_path, monkeypatch, debridlink, _general())
    working = await _submit(engine, PREMIUM_ONLY)

    def broken(_self):
        raise RuntimeError("recorder unavailable")

    monkeypatch.setattr(RoutingDecision, "encode", broken)
    failing = await _submit(engine, PREMIUM_ONLY.replace("abc123", "def456"))
    [ok_route] = await _route_rows(working.id)
    [failed_route] = await _route_rows(failing.id)
    assert failed_route["provider_id"] == ok_route["provider_id"] == "general_http"
    assert failed_route["outcome"] == ok_route["outcome"]
    assert failed_route["routing_decision"] is None and ok_route["routing_decision"] is not None
    assert (await _requests(failing.id))[0]["state"] == (await _requests(working.id))[0]["state"]


async def test_a_failing_hold_write_never_changes_routing(tmp_path, monkeypatch):
    debridlink, _ = _debridlink()
    engine = await _engine(tmp_path, monkeypatch, debridlink, _general())
    engine.repository.record_route_decision = AsyncMock(side_effect=RuntimeError("database unavailable"))
    transfer = await _submit(engine, FREE_LINK)
    assert await _route_rows(transfer.id) == []
    [request] = await _requests(transfer.id)
    assert (request["state"], request["routing_decision"], request["error"]) == ("pending", None, None)


class _Counting:
    """A provider double that counts every routing fact it is asked for."""

    def __init__(self, identity, kinds, *, claims=()):
        from transfers.models import Capability, IntegrationDescriptor
        self.descriptor = IntegrationDescriptor(identity, identity, frozenset({Capability.RESOLVE}),
                                                request_types=frozenset(kinds))
        self.asked = {"applicability": 0, "entitlement": 0}
        self._facts = ProviderApplicability(
            specialized_hosts=tuple(HostClaim(host, HostClaimScope.EXACT, frozenset({"https"})) for host in claims),
            specialized=bool(claims), generic_schemes=frozenset() if claims else frozenset({"https"}))

    async def resolve(self, request):
        return _WAIT

    def applicability_for(self, request):
        self.asked["applicability"] += 1
        return self._facts

    def entitlement_for(self, request):
        self.asked["entitlement"] += 1
        return True


async def test_tracing_evaluates_nothing_the_selector_would_not():
    specialized = _Counting("special", {"https"}, claims=("paid.example",))
    generic = _Counting("generic", {"https"})
    elsewhere = _Counting("usenet-like", {"nzb"})
    registry = _registry(specialized, generic, elsewhere)
    request = TransferRequest("https", PREMIUM_ONLY)
    registry.provider_for(request)
    plain = {provider.descriptor.id: dict(provider.asked) for provider in (specialized, generic, elsewhere)}
    for provider in (specialized, generic, elsewhere):
        provider.asked = {"applicability": 0, "entitlement": 0}
    route = registry.provider_route(request)
    traced = {provider.descriptor.id: dict(provider.asked) for provider in (specialized, generic, elsewhere)}
    assert traced == plain
    assert elsewhere.asked == {"applicability": 0, "entitlement": 0}
    assert "usenet-like" not in _dispositions(route.decision.encode())


async def test_tracing_does_no_account_catalogue_or_network_work(tmp_path, monkeypatch):
    debridlink, transport = _debridlink()
    await debridlink.account.refresh_now()
    calls = len(transport.calls)
    engine = await _engine(tmp_path, monkeypatch, debridlink, _general())
    await _submit(engine, PREMIUM_ONLY)
    assert len(transport.calls) == calls


async def test_decision_evidence_is_bounded_by_registered_providers_never_by_catalogues(tmp_path, monkeypatch):
    big = [{"name": f"h{index}", "type": "host", "domains": [f"h{index}.example"], "regexs": [], "isFree": False}
           for index in range(3000)] + HOSTERS
    small, _ = _debridlink()
    large, _ = _debridlink(hosters=big)
    for provider in (small, large):
        await provider.account.refresh_now()
    request = TransferRequest("https", PREMIUM_ONLY)
    small_decision = _registry(small, _general()).provider_route(request).decision.encode()
    large_decision = _registry(large, _general()).provider_route(request).decision.encode()
    assert small_decision == large_decision and len(small_decision) < 256

    debridlink, _ = _debridlink()
    await debridlink.account.refresh_now()
    engine = await _engine(tmp_path, monkeypatch, debridlink, _realdebrid(), _general())
    transfers = []
    for batch in range(3):                             # a submission takes at most 100 roots
        roots = [f"https://paid.example/f/{batch}-{index:03d}" for index in range(100)]
        transfers.append(await engine.submit(tuple(TransferRequest("https", url, name=f"{batch}-{index}.bin")
                                                   for index, url in enumerate(roots)), deduplicate=False))
    await engine.resolve_pending()
    rows = [row for transfer in transfers for row in await _route_rows(transfer.id)]
    assert len(rows) == 300
    sizes = {len(row["routing_decision"]) for row in rows}
    assert max(sizes) < 256                            # per decision: provider ids and enums only


async def test_route_history_without_a_decision_remains_valid(tmp_path, monkeypatch):
    engine = await _engine(tmp_path, monkeypatch, _general())
    transfer = await engine.submit((TransferRequest("https", "https://old.example/f.bin", name="f.bin"),),
                                   deduplicate=False)
    [record] = await engine.repository.requests(transfer.id)
    attempt = await engine.repository.begin_resolution(record.id, "general_http")
    assert attempt is not None
    [route] = await _route_rows(transfer.id)
    assert route["routing_decision"] is None              # no decision is ever fabricated
    trace, _rows = await _traced(transfer.id)
    assert trace["metadata"]["trace_format_version"] == transfer_trace.TRACE_FORMAT_VERSION


async def test_the_canonical_selector_and_engine_routing_name_no_provider():
    from transfers import engine as engine_module
    sources = (inspect.getsource(registry_module), inspect.getsource(engine_module.TransferEngine._route),
               inspect.getsource(engine_module.TransferEngine._record_route_hold))
    for source in sources:
        for name in ("debridlink", "alldebrid", "realdebrid", "torbox", "general_http"):
            assert name not in source.casefold()
