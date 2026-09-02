"""Roadmap Item 8 deterministic initial provider routing acceptance tests."""
from __future__ import annotations

from dataclasses import replace

import pytest

import db.database as database
from fake_applicability_provider import SpecializedFixtureProvider
from providers.alldebrid.host_runtime import (
    AllDebridHost,
    AllDebridHostSnapshot,
    AllDebridRequestApplicability,
)
from providers.alldebrid.provider import AllDebridProvider
from providers.general_http.provider import GeneralHttpProvider
from transfers.engine import TransferEngine
from transfers.errors import Category, Domain, Retryability, Stage, TransferError
from transfers.models import TransferRequest, TransferState
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


SUPPORTED_URL = "https://rapidgator.net/example"
GENERIC_URL = "https://downloads.example/file"


class RoutingOnlyAllDebridClient:
    """A deterministic service double whose methods must not run in registry tests."""

    def __init__(self):
        self.unlock_calls = 0
        self.magnet_calls = 0
        self.torrent_calls = 0

    async def unlock_link(self, _url):
        self.unlock_calls += 1
        raise AssertionError("routing selection must not resolve the provider")

    async def upload_magnet(self, _magnet):
        self.magnet_calls += 1
        raise AssertionError("routing selection must not resolve the provider")

    async def upload_torrent_file(self, _data, _filename):
        self.torrent_calls += 1
        raise AssertionError("routing selection must not resolve the provider")


class CountingGeneralHttpProvider(GeneralHttpProvider):
    def __init__(self, *, enabled: bool = True):
        self.calls = 0
        self.descriptor = replace(self.descriptor, enabled=enabled)

    async def resolve(self, request):
        self.calls += 1
        return await super().resolve(request)


def alldebrid_snapshot() -> AllDebridHostSnapshot:
    """Provider-owned deterministic fixture; the universal router knows no hostnames."""
    return AllDebridHostSnapshot((
        AllDebridHost(
            service_id="rapidgator",
            name="Rapidgator",
            service_type="premium",
            domains=("rapidgator.net",),
            regexps=(r"https?://(?:www\.)?rapidgator\.net/.+",),
            available=True,
        ),
    ))


def production_registry(*, alldebrid_enabled: bool, http_enabled: bool):
    client = RoutingOnlyAllDebridClient()
    alldebrid = AllDebridProvider(client=client)
    snapshot = alldebrid_snapshot()
    alldebrid.applicability = replace(
        alldebrid.applicability,
        specialized_hosts=snapshot.claims,
    )
    alldebrid.applicability_for = AllDebridRequestApplicability(snapshot)
    alldebrid.descriptor = replace(alldebrid.descriptor, enabled=alldebrid_enabled)

    http = CountingGeneralHttpProvider(enabled=http_enabled)
    registry = IntegrationRegistry()
    registry.register_provider(alldebrid)
    registry.register_provider(http)
    return registry, alldebrid, http, client


def provider_ids_for_request(
    registry: IntegrationRegistry,
    request: TransferRequest,
) -> tuple[str, ...]:
    return tuple(
        provider.descriptor.id for provider in registry.eligible_providers(request)
    )


def provider_ids(registry: IntegrationRegistry, url: str) -> tuple[str, ...]:
    return provider_ids_for_request(
        registry,
        TransferRequest("https", url, name="fixture.bin"),
    )


@pytest.mark.parametrize(
    ("alldebrid_enabled", "http_enabled", "expected"),
    (
        (True, True, ("alldebrid",)),
        (False, True, ("general_http",)),
        (True, False, ("alldebrid",)),
        (False, False, ()),
    ),
)
def test_same_supported_url_routes_only_by_enablement(
    alldebrid_enabled,
    http_enabled,
    expected,
):
    registry, _, _, _ = production_registry(
        alldebrid_enabled=alldebrid_enabled,
        http_enabled=http_enabled,
    )
    assert provider_ids(registry, SUPPORTED_URL) == expected


@pytest.mark.parametrize(
    ("alldebrid_enabled", "http_enabled", "expected"),
    (
        (True, True, ("general_http",)),
        (False, True, ("general_http",)),
        (True, False, ()),
        (False, False, ()),
    ),
)
def test_same_generic_url_routes_only_by_enablement(
    alldebrid_enabled,
    http_enabled,
    expected,
):
    registry, _, _, _ = production_registry(
        alldebrid_enabled=alldebrid_enabled,
        http_enabled=http_enabled,
    )
    assert provider_ids(registry, GENERIC_URL) == expected


def test_specialized_set_is_filtered_before_neutral_same_class_selection():
    registry = IntegrationRegistry()
    alpha = SpecializedFixtureProvider("alpha", host="shared.example", priority=10)
    beta = SpecializedFixtureProvider("beta", host="shared.example", priority=20)
    generic = CountingGeneralHttpProvider()
    registry.register_provider(alpha)
    registry.register_provider(generic)
    registry.register_provider(beta)

    request = TransferRequest("https", "https://shared.example/file")
    assert provider_ids_for_request(registry, request) == ("beta", "alpha")
    assert registry.provider_for(request).descriptor.id == "beta"

    preferred = replace(request, preferred_provider="alpha")
    assert provider_ids_for_request(registry, preferred) == ("alpha", "beta")
    assert registry.provider_for(preferred).descriptor.id == "alpha"


def test_same_class_selection_is_registration_order_invariant():
    request = TransferRequest("https", "https://shared.example/file")

    def selected(order):
        registry = IntegrationRegistry()
        providers = {
            "alpha": SpecializedFixtureProvider(
                "alpha",
                host="shared.example",
                priority=5,
            ),
            "beta": SpecializedFixtureProvider(
                "beta",
                host="shared.example",
                priority=5,
            ),
        }
        for identity in order:
            registry.register_provider(providers[identity])
        return provider_ids_for_request(registry, request), registry.provider_for(
            request
        ).descriptor.id

    assert selected(("alpha", "beta")) == (("alpha", "beta"), "alpha")
    assert selected(("beta", "alpha")) == (("alpha", "beta"), "alpha")


def test_multiple_generic_providers_reach_same_class_policy_when_no_specialized_match():
    first = CountingGeneralHttpProvider()
    first.descriptor = replace(first.descriptor, id="generic_a", priority=1)
    second = CountingGeneralHttpProvider()
    second.descriptor = replace(second.descriptor, id="generic_b", priority=2)
    registry = IntegrationRegistry()
    registry.register_provider(first)
    registry.register_provider(second)

    request = TransferRequest("https", GENERIC_URL)
    assert provider_ids_for_request(registry, request) == ("generic_b", "generic_a")


def test_disabled_specialized_claim_does_not_participate():
    specialized = SpecializedFixtureProvider(
        "alpha",
        host="shared.example",
        priority=100,
    )
    specialized.descriptor = replace(specialized.descriptor, enabled=False)
    generic = CountingGeneralHttpProvider()
    registry = IntegrationRegistry()
    registry.register_provider(specialized)
    registry.register_provider(generic)

    request = TransferRequest("https", "https://shared.example/file")
    assert provider_ids_for_request(registry, request) == ("general_http",)


def test_existing_health_filter_removes_specialized_before_class_construction():
    specialized = SpecializedFixtureProvider(
        "alpha",
        host="shared.example",
        priority=100,
    )
    generic = CountingGeneralHttpProvider()
    registry = IntegrationRegistry()
    registry.register_provider(specialized)
    registry.register_provider(generic)
    registry.mark_health("alpha", healthy=False)

    request = TransferRequest("https", "https://shared.example/file")
    assert provider_ids_for_request(registry, request) == ("general_http",)


def test_no_eligible_provider_returns_canonical_nonretryable_unsupported_route():
    registry, _, _, _ = production_registry(
        alldebrid_enabled=False,
        http_enabled=False,
    )
    request = TransferRequest("https", SUPPORTED_URL)

    with pytest.raises(TransferError) as caught:
        registry.provider_for(request)

    error = caught.value.error
    assert error.domain == Domain.REQUEST
    assert error.category == Category.UNSUPPORTED_REQUEST
    assert error.stage == Stage.RESOLUTION
    assert error.retryability == Retryability.NEVER
    assert error.integration_id == ""


def test_unhealthy_specialized_with_no_healthy_generic_is_unsupported():
    registry = IntegrationRegistry()
    specialized = SpecializedFixtureProvider("alpha", host="shared.example")
    generic = CountingGeneralHttpProvider(enabled=False)
    registry.register_provider(specialized)
    registry.register_provider(generic)
    registry.mark_health("alpha", healthy=False)

    with pytest.raises(TransferError) as caught:
        registry.provider_for(
            TransferRequest("https", "https://shared.example/file")
        )
    assert caught.value.error.category == Category.UNSUPPORTED_REQUEST


@pytest.mark.asyncio
async def test_unsupported_route_has_no_provider_or_executor_side_effects(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "unsupported.sqlite3")
    await database.init_db()

    repository = TransferRepository()
    registry, alldebrid, http, client = production_registry(
        alldebrid_enabled=False,
        http_enabled=False,
    )
    engine = TransferEngine(
        repository,
        registry,
        download_root=str(tmp_path / "downloads"),
        policy=TransferPolicy(),
    )
    await engine.initialize()

    transfer = await engine.submit((
        TransferRequest("https", SUPPORTED_URL, name="unsupported.bin"),
    ))
    # A normal engine cycle performs initial routing, then canonical parent
    # aggregation. Unsupported selection fails before begin_resolution, so the
    # execution/reconciliation half has no external work to observe or start.
    await engine.tick()

    request = (await repository.requests(transfer.id))[0]
    current = await repository.get(transfer.id)
    assert current.state == TransferState.FAILED
    assert request.state == "failed"
    assert request.attempts == 0
    assert request.error is not None
    assert request.error.category == Category.UNSUPPORTED_REQUEST
    assert alldebrid.descriptor.enabled is False
    assert alldebrid.applicability_for(
        TransferRequest("https", SUPPORTED_URL)
    ).specialized_hosts
    assert client.unlock_calls == 0
    assert client.magnet_calls == 0
    assert client.torrent_calls == 0
    assert http.calls == 0
    assert await repository.executions(transfer.id) == ()
    assert await repository.artifacts(transfer.id) == ()


def test_magnet_and_torrent_keep_static_request_type_routing():
    registry, _, _, _ = production_registry(
        alldebrid_enabled=True,
        http_enabled=True,
    )

    magnet = TransferRequest("magnet", "magnet:?xt=urn:btih:" + "a" * 40)
    torrent = TransferRequest(
        "torrent",
        b"d4:infod4:name1:xee",
        name="sample.torrent",
    )

    assert provider_ids_for_request(registry, magnet) == ("alldebrid",)
    assert provider_ids_for_request(registry, torrent) == ("alldebrid",)
