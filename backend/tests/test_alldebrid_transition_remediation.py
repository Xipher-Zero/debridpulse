"""Adversarial regressions for AllDebrid live configuration transitions."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from providers.alldebrid.definition import definition as alldebrid_definition
from providers.alldebrid.provider import AllDebridProvider
from providers.alldebrid.runtime_state import AllDebridRuntimeStateStore, credential_scope
from transfers.engine import TransferEngine
from transfers.models import ProviderResource, TransferRequest
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry


ROOT = Path(__file__).resolve().parents[1]


class CapturingRuntimeStore:
    def __init__(self):
        self.loads = []
        self.replacements = []

    async def load(self, integration_id, state_key):
        self.loads.append((integration_id, state_key))
        return None

    async def replace(self, integration_id, payload, **kwargs):
        self.replacements.append((integration_id, payload, kwargs))
        return SimpleNamespace(generation=1)


def disabled_resource_record():
    client = AsyncMock()
    provider = AllDebridProvider(client=client)
    provider.descriptor = replace(provider.descriptor, enabled=False)
    registry = IntegrationRegistry()
    registry.register_provider(provider)
    repository = AsyncMock()
    engine = TransferEngine(
        repository,
        registry,
        download_root="/tmp/debridpulse-transition-test",
        policy=TransferPolicy(),
    )
    record = SimpleNamespace(
        id=17,
        transfer_id=11,
        parent_id=None,
        attempts=0,
        request=TransferRequest("magnet", "magnet:?xt=urn:btih:" + "a" * 40),
        resource=ProviderResource("alldebrid", {"id": "123"}),
    )
    return engine, repository, provider, client, record


def test_alldebrid_api_key_is_a_durable_connection_ownership_field():
    assert alldebrid_definition.ownership_fields == frozenset({"api_key"})


@pytest.mark.asyncio
async def test_runtime_state_namespace_changes_with_credentials_without_persisting_secret():
    backing = CapturingRuntimeStore()
    first_scope = credential_scope("credential-one")
    second_scope = credential_scope("credential-two")
    assert first_scope != second_scope
    assert "credential-one" not in first_scope
    assert "credential-two" not in second_scope

    first = AllDebridRuntimeStateStore(backing, first_scope)
    second = AllDebridRuntimeStateStore(backing, second_scope)
    await first.load("alldebrid", "supported-hosts")
    await second.load("alldebrid", "supported-hosts")

    first_key = backing.loads[0][1]
    second_key = backing.loads[1][1]
    assert first_key != second_key
    assert first_key.startswith("supported-hosts:credential-v1-")
    assert second_key.startswith("supported-hosts:credential-v1-")
    assert "credential-one" not in first_key
    assert "credential-two" not in second_key


@pytest.mark.asyncio
async def test_disabled_bound_resource_observation_parks_without_provider_or_repository_io():
    engine, repository, _provider, client, record = disabled_resource_record()

    await engine._observe_resource(record)

    client.get_magnet_status.assert_not_called()
    repository.resource_observation.assert_not_awaited()
    repository.poll_after.assert_not_awaited()
    repository.outcome.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_bound_resource_reresolution_parks_without_provider_or_attempt_io():
    engine, repository, _provider, client, record = disabled_resource_record()

    await engine._resolve(record)

    client.get_magnet_status.assert_not_called()
    client.upload_magnet.assert_not_called()
    repository.begin_resolution.assert_not_awaited()
    repository.outcome.assert_not_awaited()


def test_alldebrid_rate_limiter_has_no_core_configuration_dependency_or_process_singleton():
    rate_source = (ROOT / "providers" / "alldebrid" / "rate_limit.py").read_text()
    client_source = (ROOT / "providers" / "alldebrid" / "client.py").read_text()

    assert "core.config" not in rate_source
    assert "get_settings" not in rate_source
    assert "_alldebrid_rate_limiter" not in rate_source
    assert "TokenBucketRateLimiter" in client_source
    assert "self._rate_limiter.acquire()" in client_source
