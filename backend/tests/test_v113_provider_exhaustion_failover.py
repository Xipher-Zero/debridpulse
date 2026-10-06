"""Neutral provider failover after bound-provider exhaustion.

A provider is one attempt to satisfy a logical request, not its identity. The
bound provider's own retry budget comes first; only a provider-attributable
failure that exhausts it hands the SAME request to the next provider of the one
canonical competition, with the exhausted provider excluded for the current
routing campaign. Request-global failures stay terminal, owned cleanup runs
through the one cleanup cadence, and the operator's Retry begins a new campaign.

Every provider here is a neutral fixture: no concrete integration is named.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

import db.database as database
from fake_integrations import MemoryExecutor
from transfers.applicability import ApplicabilityReadiness, HostClaim, HostClaimScope, ProviderApplicability
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Origin, Permanence, Recovery, Retryability, Stage, TransferError
from transfers.models import (
    Capability, Endpoint, IntegrationDescriptor, OutcomeKind, Ownership, ProviderObservation, ProviderResource,
    ResolutionResult, ResourceState, SourceEntry, TransferCandidate, TransferOutcome, TransferRequest,
)
from transfers.policy import TransferPolicy, provider_attributable
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository

pytestmark = pytest.mark.asyncio

NOW = 1_000.0


def error(domain, category, retryability, *, origin=Origin.PROVIDER, permanence=Permanence.UNKNOWN):
    return NormalizedError(domain, category, Stage.RESOLUTION, retryability, origin=origin, permanence=permanence)


# Failure classes, as the existing translators emit them.
PROVIDER_FINAL = error(Domain.PROVIDER, Category.ACCOUNT_LIMITED, Retryability.NEVER, permanence=Permanence.PERMANENT)
PROVIDER_RETRYABLE = error(Domain.PROVIDER, Category.PROVIDER_UNAVAILABLE, Retryability.BACKOFF)
CREDENTIAL = error(Domain.PROVIDER, Category.CREDENTIAL_INVALID, Retryability.AFTER_REAUTH)
PROVIDER_NETWORK = error(Domain.NETWORK, Category.CONNECTION_FAILED, Retryability.BACKOFF)
UNMAPPED = error(Domain.PROVIDER, Category.UNMAPPED_PROVIDER_ERROR, Retryability.UNKNOWN)
# A provider rejecting a malformed input (AllDebrid BAD_LINK, Real-Debrid code 2)
# is emitted in the provider domain -- it is still the request's own failure.
MALFORMED = error(Domain.PROVIDER, Category.INVALID_REQUEST, Retryability.NEVER, permanence=Permanence.PERMANENT)
CONTENT = error(Domain.PROVIDER, Category.CONTENT_INVALID, Retryability.NEVER, permanence=Permanence.PERMANENT)
UNSAFE_PATH = error(Domain.SECURITY, Category.PATH_POLICY_VIOLATION, Retryability.NEVER)
SOURCE_GONE = error(Domain.RESOLUTION, Category.SOURCE_NOT_FOUND, Retryability.NEVER, origin=Origin.REMOTE_SOURCE,
                    permanence=Permanence.PERMANENT)
SOURCE_NETWORK = error(Domain.NETWORK, Category.CONNECTION_FAILED, Retryability.NEVER, origin=Origin.REMOTE_SOURCE)
# A provider-domain failure its emitter attributes to the source is the source's.
PROVIDER_SAYS_SOURCE = error(Domain.PROVIDER, Category.RESOLUTION_FAILED, Retryability.NEVER,
                             origin=Origin.REMOTE_SOURCE)
# Core stamps a provider's unusable answer in the provider domain (origin CORE).
ADAPTER = NormalizedError(Domain.PROVIDER, Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION, Retryability.NEVER)


class RouteLab:
    """A neutral provider: answers each resolution with its next scripted
    outcome (an error raised, or a resource it created/adopted/observed),
    else a plain executable candidate."""

    def __init__(self, identity, *, kinds=("parcel",), priority=0, applicability=None):
        self.descriptor = IntegrationDescriptor(
            identity, identity,
            frozenset({Capability.RESOLVE, Capability.RESOURCE_LOOKUP, Capability.CLEANUP}),
            request_types=frozenset(kinds), priority=priority)
        self.applicability = applicability or ProviderApplicability()
        self.script: list = []
        self.always: NormalizedError | None = None
        self.observed_error: NormalizedError | None = None
        self.resolved: list[str] = []
        self.cleaned: list[str] = []
        self.cleanup_outcome = TransferOutcome(OutcomeKind.SUCCESS)

    async def resolve(self, request):
        self.resolved.append(str(request.payload))
        step = self.script.pop(0) if self.script else self.always
        if isinstance(step, NormalizedError):
            raise TransferError(replace(step, integration_id=self.descriptor.id))
        if isinstance(step, Ownership):
            resource = ProviderResource(self.descriptor.id, {"ticket": str(request.payload)}, step,
                                        id=f"{self.descriptor.id}:{request.payload}")
            return ResolutionResult(ResourceState.PREPARING,
                                    observation=ProviderObservation(resource, ResourceState.PREPARING, "remote"))
        return ResolutionResult(ResourceState.AVAILABLE, (TransferCandidate(
            "payload.bin", (Endpoint("memory", f"memory:{self.descriptor.id}"),), expected_bytes=4,
            provider_id=self.descriptor.id),))

    async def observe(self, resource):
        if self.observed_error is not None:
            failure = replace(self.observed_error, integration_id=self.descriptor.id)
            return ProviderObservation(resource, ResourceState.UNAVAILABLE, "remote", error=failure)
        return ProviderObservation(resource, ResourceState.PREPARING, "remote")

    async def cleanup(self, directive):
        self.cleaned.append(directive.resource.id)
        return self.cleanup_outcome


def url_lab(identity, *, host=None, generic=False, priority=0):
    claims = () if host is None else (HostClaim(host, HostClaimScope.DOMAIN, frozenset({"https"})),)
    return RouteLab(identity, kinds=("https",), priority=priority, applicability=ProviderApplicability(
        generic_schemes=frozenset({"https"}) if generic else frozenset(), specialized_hosts=claims,
        specialized=bool(host), readiness=ApplicabilityReadiness.READY))


async def lab(tmp_path, monkeypatch, *providers, fresh=True):
    if fresh:
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "failover.sqlite3")
        await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    for provider in providers:
        registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "downloads"),
                            policy=TransferPolicy(retry_delay=0.0, max_attempts=3), clock=lambda: NOW)
    await engine.initialize()
    return repository, engine


async def submit(engine, kind="parcel", payload="logical-object"):
    return await engine.submit((TransferRequest(kind, payload, name="payload.bin"),), name="logical",
                               deduplicate=False)


async def root(repository, transfer_id):
    return next(item for item in await repository.requests(transfer_id) if item.parent_id is None)


async def drive(engine, passes=6):
    for _ in range(passes):
        await engine.resolve_pending()


async def routes(repository, transfer_id):
    return (await repository.presentation(transfer_id, details=True))["route_attempts"]


# -- the decision ----------------------------------------------------------------

@pytest.mark.parametrize("failure, attributable", [
    (PROVIDER_FINAL, True), (PROVIDER_RETRYABLE, True), (CREDENTIAL, True), (PROVIDER_NETWORK, True),
    (UNMAPPED, True), (ADAPTER, True), (MALFORMED, False), (CONTENT, False), (UNSAFE_PATH, False),
    (SOURCE_GONE, False), (SOURCE_NETWORK, False), (PROVIDER_SAYS_SOURCE, False),
    (error(Domain.PROVIDER, Category.SOURCE_NOT_FOUND, Retryability.NEVER), False),
])
async def test_provider_attribution_is_read_from_normalized_facts(failure, attributable):
    assert provider_attributable(failure) is attributable


@pytest.mark.parametrize("native, attributable", [
    ("MAGNET_TOO_LARGE", True), ("LINK_HOST_NOT_SUPPORTED", True), ("AUTH_BAD_APIKEY", True),
    ("MAINTENANCE", True), ("BAD_LINK", False), ("MAGNET_INVALID_URI", False), ("LINK_DOWN", False),
    ("MAGNET_PROCESSING_FAILED", False), ("LINK_HOST_UNAVAILABLE", False),
])
async def test_existing_alldebrid_translations_classify_as_the_contract_requires(native, attributable):
    from providers.alldebrid.translation import error_from_code
    assert provider_attributable(error_from_code(native)) is attributable


@pytest.mark.parametrize("code, attributable", [
    (23, True), (20, True), (16, True), (8, True), (25, True), (2, False), (24, False), (35, True), (17, False),
])
async def test_existing_realdebrid_translations_classify_as_the_contract_requires(code, attributable):
    from providers.realdebrid.client import RealDebridAPIError
    from providers.realdebrid.translation import error_from_native
    assert provider_attributable(error_from_native(RealDebridAPIError(code, "native", 400))) is attributable


async def test_exhaustion_is_only_ever_the_policy_s_final_decision():
    policy = TransferPolicy(retry_delay=0.0, max_attempts=3)
    # The provider's own budget comes first.
    assert policy.retry_resolution(PROVIDER_RETRYABLE, 1, NOW).action == Recovery.RETRY
    # Spent: the provider is exhausted, never retried. It is a fact about the
    # provider alone -- the policy is not even told whether another remains.
    spent = policy.retry_resolution(PROVIDER_RETRYABLE, 3, NOW)
    assert (spent.action, spent.retry_at) == (Recovery.TRY_ALTERNATE_PROVIDER, None)
    final = policy.retry_resolution(PROVIDER_FINAL, 1, NOW)
    assert (final.action, final.retry_at) == (Recovery.TRY_ALTERNATE_PROVIDER, None)
    # A request-global failure never exhausts a provider.
    for failure in (MALFORMED, CONTENT, UNSAFE_PATH, SOURCE_GONE):
        decision = policy.retry_resolution(failure, 1, NOW)
        assert decision.action != Recovery.TRY_ALTERNATE_PROVIDER and decision.retry_at is None


# -- handoff ------------------------------------------------------------------------

async def test_provider_final_failure_hands_the_same_request_to_the_next_provider(tmp_path, monkeypatch):
    first, second = RouteLab("alpha-route"), RouteLab("beta-route")
    first.always = PROVIDER_FINAL
    repository, engine = await lab(tmp_path, monkeypatch, first, second)
    transfer = await submit(engine)
    record = await root(repository, transfer.id)

    await drive(engine)

    assert first.resolved == ["logical-object"]
    assert second.resolved == ["logical-object"]
    after = await root(repository, transfer.id)
    assert after.id == record.id and after.transfer_id == transfer.id
    assert len(await repository.requests(transfer.id)) == 1
    assert await repository.bound_route_provider(record.id) == "beta-route"
    assert await repository.exhausted_route_providers(record.id) == frozenset({"alpha-route"})
    history = await routes(repository, transfer.id)
    assert [(item["provider_id"], item["resolution_state"]) for item in history] == [
        ("alpha-route", "exhausted"), ("beta-route", "succeeded")]
    assert history[0]["outcome"] == "failed"
    assert (history[1]["transition_kind"], history[1]["transition_reason"]) == ("provider_change", "account_limited")
    presentation = await repository.presentation(transfer.id, details=True)
    assert presentation["current_provider_id"] == "beta-route"


async def test_retryable_failure_spends_the_bound_provider_s_budget_before_any_handoff(tmp_path, monkeypatch):
    first, second = RouteLab("alpha-route"), RouteLab("beta-route")
    first.always = PROVIDER_RETRYABLE
    repository, engine = await lab(tmp_path, monkeypatch, first, second)
    transfer = await submit(engine)

    await engine.resolve_pending()
    assert (len(first.resolved), second.resolved) == (1, [])
    await engine.resolve_pending()
    assert (len(first.resolved), second.resolved) == (2, [])
    await drive(engine)

    assert len(first.resolved) == 3
    assert second.resolved == ["logical-object"]
    assert await repository.bound_route_provider((await root(repository, transfer.id)).id) == "beta-route"


@pytest.mark.parametrize("failure", [MALFORMED, CONTENT, UNSAFE_PATH, SOURCE_GONE, SOURCE_NETWORK],
                         ids=["malformed", "content", "unsafe-path", "source-gone", "source-network"])
async def test_request_global_failure_is_terminal_through_every_provider(tmp_path, monkeypatch, failure):
    first, second = RouteLab("alpha-route"), RouteLab("beta-route")
    first.always = failure
    repository, engine = await lab(tmp_path, monkeypatch, first, second)
    transfer = await submit(engine)

    await drive(engine)

    assert second.resolved == []
    record = await root(repository, transfer.id)
    assert record.state == "failed" and record.error.category == failure.category
    assert await repository.exhausted_route_providers(record.id) == frozenset()


async def test_credential_state_exhausts_the_provider_without_an_automatic_retry(tmp_path, monkeypatch):
    first, second = RouteLab("alpha-route"), RouteLab("beta-route")
    first.always = CREDENTIAL
    repository, engine = await lab(tmp_path, monkeypatch, first, second)
    await submit(engine)

    await drive(engine)

    # Existing policy grants an operator-actionable credential failure no
    # automatic retry; another account may still serve the request.
    assert len(first.resolved) == 1
    assert second.resolved == ["logical-object"]


async def test_specialized_then_specialized_then_generic_in_canonical_order(tmp_path, monkeypatch):
    preferred = url_lab("special-high", host="hoster.test", priority=10)
    fallback = url_lab("special-low", host="hoster.test", priority=5)
    generic = url_lab("generic-route", generic=True)
    preferred.always = PROVIDER_FINAL
    fallback.always = PROVIDER_FINAL
    repository, engine = await lab(tmp_path, monkeypatch, generic, fallback, preferred)
    transfer = await submit(engine, "https", "https://hoster.test/file.bin")

    await engine.resolve_pending()
    await engine.resolve_pending()
    # Specialized still beats generic while a specialized provider remains.
    assert (len(preferred.resolved), len(fallback.resolved), generic.resolved) == (1, 1, [])
    await drive(engine)

    assert generic.resolved == ["https://hoster.test/file.bin"]
    history = await routes(repository, transfer.id)
    assert [item["provider_id"] for item in history] == ["special-high", "special-low", "generic-route"]


async def test_all_providers_exhausted_fails_truthfully_without_looping(tmp_path, monkeypatch):
    first, second = RouteLab("alpha-route"), RouteLab("beta-route")
    first.always = PROVIDER_FINAL
    second.always = replace(PROVIDER_FINAL, category=Category.QUOTA_EXCEEDED)
    repository, engine = await lab(tmp_path, monkeypatch, first, second)
    transfer = await submit(engine)

    await engine.resolve_pending()
    record = await root(repository, transfer.id)
    # A is exhausted; the request has not terminated while B remains.
    assert record.state != "failed" and second.resolved == []
    await drive(engine, passes=10)

    assert first.resolved == ["logical-object"] and second.resolved == ["logical-object"]
    record = await root(repository, transfer.id)
    assert record.state == "failed" and record.error.category == Category.QUOTA_EXCEEDED
    # The last provider is exhausted exactly like the first: no provider is
    # privileged by happening to fail when nothing else remained.
    assert await repository.exhausted_route_providers(record.id) == frozenset({"alpha-route", "beta-route"})
    assert await repository.bound_route_provider(record.id) is None
    assert [(item["provider_id"], item["resolution_state"]) for item in await routes(repository, transfer.id)] == [
        ("alpha-route", "exhausted"), ("beta-route", "exhausted")]


async def test_restart_after_every_provider_exhausted_retries_nothing_before_a_new_campaign(tmp_path, monkeypatch):
    first, second = RouteLab("alpha-route"), RouteLab("beta-route")
    first.always = PROVIDER_FINAL
    second.always = PROVIDER_FINAL
    repository, engine = await lab(tmp_path, monkeypatch, first, second)
    transfer = await submit(engine)
    await drive(engine)
    record = await root(repository, transfer.id)
    assert record.state == "failed"

    restarted_first, restarted_second = RouteLab("alpha-route"), RouteLab("beta-route")
    restarted, restarted_engine = await lab(tmp_path, monkeypatch, restarted_first, restarted_second, fresh=False)
    await drive(restarted_engine)

    assert restarted_first.resolved == [] and restarted_second.resolved == []
    assert (await root(restarted, transfer.id)).state == "failed"
    assert await restarted.exhausted_route_providers(record.id) == frozenset({"alpha-route", "beta-route"})
    assert await restarted.bound_route_provider(record.id) is None


async def test_administrative_disablement_is_a_hard_stop_never_exhaustion(tmp_path, monkeypatch):
    first, second = RouteLab("alpha-route"), RouteLab("beta-route")
    first.always = PROVIDER_RETRYABLE
    repository, engine = await lab(tmp_path, monkeypatch, first, second)
    transfer = await submit(engine)
    await engine.resolve_pending()
    first.descriptor = replace(first.descriptor, enabled=False)

    await drive(engine)

    assert second.resolved == []
    record = await root(repository, transfer.id)
    assert await repository.bound_route_provider(record.id) == "alpha-route"
    assert record.error.category == Category.PROVIDER_UNAVAILABLE


# -- persistence and campaigns ------------------------------------------------------

async def test_exhaustion_survives_restart_within_the_campaign(tmp_path, monkeypatch):
    first, second = RouteLab("alpha-route"), RouteLab("beta-route")
    first.always = PROVIDER_FINAL
    repository, engine = await lab(tmp_path, monkeypatch, first, second)
    transfer = await submit(engine)
    # One resolution outside a running cycle: the handoff commits, nothing
    # re-enters before the "crash".
    await engine._resolve(await root(repository, transfer.id))
    assert second.resolved == []
    record = await root(repository, transfer.id)
    assert record.state == "pending" and record.resource is None

    restarted_first, restarted_second = RouteLab("alpha-route"), RouteLab("beta-route")
    restarted, restarted_engine = await lab(tmp_path, monkeypatch, restarted_first, restarted_second, fresh=False)
    await drive(restarted_engine)

    assert restarted_first.resolved == []
    assert restarted_second.resolved == ["logical-object"]
    assert await restarted.exhausted_route_providers(record.id) == frozenset({"alpha-route"})


async def test_operator_retry_begins_a_new_campaign_that_reconsiders_every_provider(tmp_path, monkeypatch):
    first, second = RouteLab("alpha-route"), RouteLab("beta-route")
    first.always = PROVIDER_FINAL
    second.always = PROVIDER_FINAL
    repository, engine = await lab(tmp_path, monkeypatch, first, second)
    transfer = await submit(engine)
    await drive(engine)
    record = await root(repository, transfer.id)
    assert record.state == "failed"

    first.always = None  # both could serve now
    second.always = None
    assert await engine.retry(transfer.id)
    assert await repository.exhausted_route_providers(record.id) == frozenset()
    assert await repository.bound_route_provider(record.id) is None
    await drive(engine)

    # Campaign N+1 starts again from canonical order and current truth: the
    # first provider in order serves; the one that happened to fail last has
    # no preference.
    assert first.resolved == ["logical-object", "logical-object"]
    assert second.resolved == ["logical-object"]
    assert await repository.bound_route_provider(record.id) == "alpha-route"
    states = [(item["provider_id"], item["resolution_state"]) for item in await routes(repository, transfer.id)]
    assert states == [("alpha-route", "released"), ("beta-route", "released"), ("alpha-route", "succeeded")]


# -- cleanup ownership ---------------------------------------------------------------

@pytest.mark.parametrize("ownership, cleaned", [
    (Ownership.CREATED, True), (Ownership.ADOPTED, True), (Ownership.OBSERVED, False),
])
async def test_handoff_cleans_up_only_what_debridpulse_positively_owns(tmp_path, monkeypatch, ownership, cleaned):
    first, second = RouteLab("alpha-route"), RouteLab("beta-route")
    first.script = [ownership]
    repository, engine = await lab(tmp_path, monkeypatch, first, second)
    transfer = await submit(engine)
    await engine.resolve_pending()
    record = await root(repository, transfer.id)
    assert record.resource is not None and record.resource.provider_id == "alpha-route"

    first.observed_error = PROVIDER_FINAL
    await drive(engine)

    assert second.resolved == ["logical-object"]
    assert first.cleaned == (["alpha-route:logical-object"] if cleaned else [])
    after = await root(repository, transfer.id)
    assert after.resource is None or after.resource.provider_id == "beta-route"
    # Cleanup ran once and nothing is still owed; an unowned resource is
    # simply retained, with no cleanup responsibility ever attached to it.
    assert not any(pending for _resource, _state, pending in await repository.resources(transfer.id))
    await engine._cleanup_pending()
    assert len(first.cleaned) == (1 if cleaned else 0)


@pytest.mark.parametrize("ownership, cleaned", [
    (Ownership.CREATED, True), (Ownership.ADOPTED, True), (Ownership.OBSERVED, False),
])
async def test_the_final_provider_is_exhausted_and_cleaned_up_like_any_other(tmp_path, monkeypatch, ownership, cleaned):
    only = RouteLab("alpha-route")
    only.script = [ownership]
    repository, engine = await lab(tmp_path, monkeypatch, only)
    transfer = await submit(engine)
    await engine.resolve_pending()
    only.observed_error = PROVIDER_FINAL

    await drive(engine)

    record = await root(repository, transfer.id)
    assert record.state == "failed" and record.error.category == Category.ACCOUNT_LIMITED
    assert record.resource is None
    assert await repository.exhausted_route_providers(record.id) == frozenset({"alpha-route"})
    assert only.cleaned == (["alpha-route:logical-object"] if cleaned else [])
    assert not any(pending for _resource, _state, pending in await repository.resources(transfer.id))


async def test_a_failed_cleanup_never_blocks_the_handoff_and_stays_owed(tmp_path, monkeypatch):
    first, second = RouteLab("alpha-route"), RouteLab("beta-route")
    first.script = [Ownership.CREATED]
    first.cleanup_outcome = TransferOutcome(OutcomeKind.FAILURE, PROVIDER_RETRYABLE)
    repository, engine = await lab(tmp_path, monkeypatch, first, second)
    transfer = await submit(engine)
    await engine.resolve_pending()
    first.observed_error = PROVIDER_FINAL

    await drive(engine)

    assert second.resolved == ["logical-object"]
    assert first.cleaned and set(first.cleaned) == {"alpha-route:logical-object"}
    owed = {resource.id for resource, _state, pending in await repository.resources(transfer.id) if pending}
    assert owed == {"alpha-route:logical-object"}


async def test_a_decomposed_route_is_never_handed_off(tmp_path, monkeypatch):
    class Decomposing(RouteLab):
        async def observe(self, resource):
            if self.observed_error is not None:
                return await super().observe(resource)
            return ProviderObservation(resource, ResourceState.AVAILABLE, "remote")

        async def manifest(self, resource):
            return (SourceEntry("member.bin", 4, "member.bin",
                                TransferRequest("parcel-member", "member", preferred_provider=self.descriptor.id)),)

    first = Decomposing("alpha-route", kinds=("parcel", "parcel-member"))
    first.descriptor = replace(first.descriptor, capabilities=first.descriptor.capabilities | {Capability.METADATA})
    second = RouteLab("beta-route")
    first.script = [Ownership.CREATED]
    repository, engine = await lab(tmp_path, monkeypatch, first, second)
    transfer = await submit(engine)
    await drive(engine, passes=3)
    record = await root(repository, transfer.id)
    assert any(item.parent_id == record.id for item in await repository.requests(transfer.id))

    first.observed_error = PROVIDER_FINAL
    await engine._resolve(replace(await root(repository, transfer.id), state="pending"))

    assert second.resolved == []
    assert await repository.exhausted_route_providers(record.id) == frozenset()
    assert await repository.bound_route_provider(record.id) == "alpha-route"


# -- current provider read model ------------------------------------------------------

async def current_providers(repository, transfer_id):
    """``current_provider_id`` as Details and the bounded list each project it."""
    from types import SimpleNamespace
    import api.operational_downloads as downloads
    details = (await repository.presentation(transfer_id, details=True))["current_provider_id"]
    listed = await downloads.list_operational_torrents(
        status=None, search=None, limit=0, offset=0, order=None,
        application=SimpleNamespace(repository=None, definitions=[]))
    item = next(row for row in listed["items"] if row["id"] == transfer_id)
    return details, item["current_provider_id"]


async def test_current_provider_follows_live_routes_in_details_and_the_bounded_list(tmp_path, monkeypatch):
    first, second = RouteLab("alpha-route"), RouteLab("beta-route")
    first.always = PROVIDER_RETRYABLE
    second.always = PROVIDER_RETRYABLE
    repository, engine = await lab(tmp_path, monkeypatch, first, second)
    transfer = await submit(engine)
    record = await root(repository, transfer.id)

    # 1. A owns the live route while it retries.
    await engine.resolve_pending()
    assert await current_providers(repository, transfer.id) == ("alpha-route", "alpha-route")

    # 2. A exhausted, B active.
    for _ in range(10):
        if second.resolved:
            break
        await engine.resolve_pending()
    assert await repository.exhausted_route_providers(record.id) == frozenset({"alpha-route"})
    assert await current_providers(repository, transfer.id) == ("beta-route", "beta-route")

    # 3. Every provider exhausted: history keeps both, nobody is current.
    await drive(engine, passes=10)
    assert await repository.exhausted_route_providers(record.id) == frozenset({"alpha-route", "beta-route"})
    assert await current_providers(repository, transfer.id) == (None, None)
    presentation = await repository.presentation(transfer.id, details=True)
    assert presentation["historical_providers"] == ["alpha-route", "beta-route"]
    assert [item["provider_id"] for item in presentation["route_attempts"]][-1] == "beta-route"

    # 6. A restart with every provider exhausted changes nothing.
    restarted, _restarted_engine = await lab(tmp_path, monkeypatch, RouteLab("alpha-route"),
                                             RouteLab("beta-route"), fresh=False)
    assert await current_providers(restarted, transfer.id) == (None, None)

    # 4. A new campaign releases every route; until one rebinds, nobody is current.
    first.always = None
    assert await engine.retry(transfer.id)
    assert await repository.exhausted_route_providers(record.id) == frozenset()
    assert await current_providers(repository, transfer.id) == (None, None)

    # 5. The new campaign binds the first provider in canonical order.
    await drive(engine)
    assert await current_providers(repository, transfer.id) == ("alpha-route", "alpha-route")
