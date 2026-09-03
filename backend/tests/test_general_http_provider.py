"""Stage 5 General HTTP(S) provider and pre-classifier routing boundary."""
from dataclasses import replace
from pathlib import Path

import pytest

from integrations.definition import IntegrationEnvironment, IntegrationSettings
from providers.general_http.definition import definition
from providers.general_http.provider import GeneralHttpProvider
from transfers.applicability import ProviderApplicability
from transfers.errors import Category, TransferError
from transfers.models import Capability, IntegrationDescriptor, TransferRequest
from transfers.registry import IntegrationRegistry
from transfers.requests import normalize_direct_links


class StubHttpProvider:
    def __init__(self, identity, *, priority=0, enabled=True):
        self.descriptor = IntegrationDescriptor(
            identity, identity, frozenset({Capability.RESOLVE}),
            request_types=frozenset({"http", "https"}), enabled=enabled, priority=priority,
        )

    @property
    def applicability(self):
        return ProviderApplicability(generic_schemes=frozenset({"http", "https"}))

    async def resolve(self, request):
        raise AssertionError("routing tests must not execute providers")


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "http://example.com/archive.bin?token=a%2Fb&part=1",
    "https://example.com/a%20b.bin?signature=opaque%2Bvalue",
])
async def test_provider_resolves_http_and_https_without_mutating_resource_url(url):
    provider = GeneralHttpProvider()
    request = TransferRequest(url.split(":", 1)[0], url)
    result = await provider.resolve(request)
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.provider_id == "general_http"
    assert len(candidate.endpoints) == 1
    assert candidate.endpoints[0].scheme == request.kind
    assert candidate.endpoints[0].address == url
    assert candidate.name.endswith("archive.bin") if request.kind == "http" else candidate.name == "a b.bin"


@pytest.mark.asyncio
async def test_provider_uses_canonical_filename_fallback_for_root_url():
    result = await GeneralHttpProvider().resolve(TransferRequest("https", "https://downloads.example/?sig=secret"))
    assert result.candidates[0].name == "downloads.example"


@pytest.mark.asyncio
async def test_provider_defensively_rejects_userinfo_and_invalid_scheme():
    provider = GeneralHttpProvider()
    with pytest.raises(TransferError) as userinfo:
        await provider.resolve(TransferRequest("https", "https://user:password@example.com/file"))
    assert userinfo.value.error.category == Category.SECURITY_POLICY_REJECTED
    with pytest.raises(TransferError) as invalid:
        await provider.resolve(TransferRequest("https", "http://example.com/file"))
    assert invalid.value.error.category == Category.INVALID_REQUEST


def test_direct_link_admission_rejects_userinfo_before_request_persistence():
    with pytest.raises(ValueError, match="Credentials embedded"):
        normalize_direct_links(["https://user:password@example.com/file"])


def test_definition_uses_existing_backend_enablement_and_priority_model():
    environment = IntegrationEnvironment(repository=None, download_root="/tmp")
    disabled = definition.build(IntegrationSettings(enabled=False, priority=17), environment)
    enabled = definition.build(IntegrationSettings(enabled=True, priority=23), environment)
    assert disabled.descriptor.enabled is False and disabled.descriptor.priority == 17
    assert enabled.descriptor.enabled is True and enabled.descriptor.priority == 23
    assert enabled.descriptor.request_types == frozenset({"http", "https"})


def test_stage5_overlap_uses_existing_neutral_registry_priority_and_enabled_state():
    request = TransferRequest("https", "https://example.com/file")
    registry = IntegrationRegistry()
    alldebrid = StubHttpProvider("alldebrid", priority=20)
    general = StubHttpProvider("general_http", priority=10)
    registry.register_provider(alldebrid)
    registry.register_provider(general)
    assert registry.provider_for(request) is alldebrid

    registry = IntegrationRegistry()
    registry.register_provider(StubHttpProvider("alldebrid", priority=20, enabled=False))
    general = StubHttpProvider("general_http", priority=10)
    registry.register_provider(general)
    assert registry.provider_for(request) is general

    registry = IntegrationRegistry()
    registry.register_provider(StubHttpProvider("alldebrid", priority=20))
    general = StubHttpProvider("general_http", priority=30)
    registry.register_provider(general)
    assert registry.provider_for(request) is general


def test_general_http_provider_has_no_alldebrid_or_aria2_implementation_dependency():
    source = (Path(__file__).parents[1] / "providers" / "general_http" / "provider.py").read_text()
    assert "providers.alldebrid" not in source
    assert "executors.aria2" not in source
    assert "requests." not in source and "aiohttp" not in source and "curl" not in source
