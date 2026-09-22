"""1.0.13: the FTP & SFTP Direct Sources provider consumes existing machinery.

The provider resolves ``ftp://``/``sftp://`` requests into one ordinary neutral
candidate with a typed accepted-input capability. Routing, executor selection,
enablement and direct-link admission are the existing generic owners; nothing
here teaches the core about FTP or SFTP.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.config import AppSettings
from executors.aria2.executor import Aria2Configuration, Aria2Executor
from integrations.catalog import definitions, register
from integrations.configuration import normalize_settings, public_integrations
from integrations.definition import IntegrationSettings
from providers.general_ftp.definition import definition as general_ftp_definition
from providers.general_ftp.provider import GeneralFtpProvider
from providers.general_http.provider import GeneralHttpProvider
from transfers import codec
from transfers.errors import Category, TransferError
from transfers.models import (
    Endpoint, InputMethod, ResourceState, SourceIdentity, TransferCandidate, TransferRequest,
)
from transfers.registry import IntegrationRegistry

ROOT = Path(__file__).resolve().parents[2]


def _request(url: str, kind: str | None = None, name: str = "") -> TransferRequest:
    return TransferRequest(kind or url.split(":", 1)[0], url, name=name)


# ── 1. Descriptor / applicability ─────────────────────────────────────────────

def test_descriptor_is_a_resolution_only_direct_source_for_exactly_ftp_and_sftp() -> None:
    provider = GeneralFtpProvider()
    assert provider.descriptor.id == "general_ftp"
    assert provider.descriptor.name == "FTP & SFTP"
    assert provider.descriptor.request_types == frozenset({"ftp", "sftp"})
    assert provider.applicability.generic_schemes == frozenset({"ftp", "sftp"})
    assert {item.value for item in provider.descriptor.capabilities} == {"resolve"}


def test_integration_definition_is_an_independent_direct_sources_provider() -> None:
    assert general_ftp_definition.id == "general_ftp"
    assert general_ftp_definition.kind == "provider"
    presentation = general_ftp_definition.presentation
    assert presentation.status_name == "FTP & SFTP"
    assert presentation.static_status == "healthy"
    assert presentation.status_group == "direct_sources"
    assert presentation.status_group_label == "Direct Sources"
    assert presentation.display_order == 110
    http = next(item for item in definitions if item.id == "general_http")
    assert http.presentation.display_order < presentation.display_order


def test_catalog_registers_general_ftp_alongside_the_existing_integrations() -> None:
    assert [item.id for item in definitions] == ["alldebrid", "general_http", "general_ftp", "aria2"]


# ── 2. Resolution ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("url,host,name", [
    ("ftp://files.example.org/pub/linux/image.iso", "files.example.org", "image.iso"),
    ("sftp://Mirror.Example.org:2222/data/My%20File.tar.gz", "mirror.example.org", "My File.tar.gz"),
    ("ftp://files.example.org:2121/archive.zip?type=i", "files.example.org", "archive.zip"),
])
async def test_resolution_emits_one_ordinary_candidate(url, host, name) -> None:
    provider = GeneralFtpProvider()
    result = await provider.resolve(_request(url))
    assert result.state == ResourceState.AVAILABLE
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.endpoints == (Endpoint(url.split(":", 1)[0], url),)
    assert candidate.provider_id == "general_ftp"
    assert candidate.source_identity == SourceIdentity("host", host)
    assert candidate.name == name
    assert candidate.accepted_input_methods == (InputMethod.USERNAME_PASSWORD,)
    assert InputMethod.USERNAME_PRIVATE_KEY not in candidate.accepted_input_methods
    assert dict(candidate.context) == {}
    assert candidate.resource is None
    assert candidate.expected_bytes == 0


@pytest.mark.asyncio
async def test_resolution_preserves_an_explicit_request_name_and_falls_back_safely() -> None:
    provider = GeneralFtpProvider()
    named = await provider.resolve(_request("ftp://files.example.org/pub/x.bin", name="Chosen.bin"))
    assert named.candidates[0].name == "Chosen.bin"
    bare = await provider.resolve(_request("ftp://files.example.org/"))
    assert bare.candidates[0].name == "files.example.org"


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "ftp://user@files.example.org/f.bin",
    "sftp://user:secret@files.example.org/f.bin",
    "ftp://:secret@files.example.org/f.bin",
])
async def test_embedded_userinfo_is_rejected_as_a_security_policy_failure(url) -> None:
    with pytest.raises(TransferError) as raised:
        await GeneralFtpProvider().resolve(_request(url))
    assert raised.value.error.category == Category.SECURITY_POLICY_REJECTED


@pytest.mark.asyncio
@pytest.mark.parametrize("url,kind", [
    ("ftp://files.example.org/f.bin", "sftp"),     # kind/scheme mismatch
    ("ftp:///f.bin", "ftp"),                        # no authority
    ("ftp://files.example.org:notaport/f.bin", "ftp"),
    ("ftp://files.example.org:99999/f.bin", "ftp"),
    ("files.example.org/f.bin", "ftp"),             # not absolute
])
async def test_malformed_requests_are_invalid(url, kind) -> None:
    with pytest.raises(TransferError) as raised:
        await GeneralFtpProvider().resolve(_request(url, kind))
    assert raised.value.error.category == Category.INVALID_REQUEST


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["http", "https", "magnet", "ftps", "scp"])
async def test_other_request_kinds_are_unsupported(kind) -> None:
    with pytest.raises(TransferError) as raised:
        await GeneralFtpProvider().resolve(_request(f"{kind}://files.example.org/f.bin", kind))
    assert raised.value.error.category == Category.UNSUPPORTED_REQUEST


def test_provider_source_performs_no_network_or_credential_work() -> None:
    source = (ROOT / "backend/providers/general_ftp/provider.py").read_text().casefold()
    for forbidden in ("aiohttp", "httpx", "urlopen", "socket", "asyncssh", "paramiko", "ftplib",
                      "getaddrinfo", "executors.", "ssh-host-key", "ftp-user", "ftp-passwd",
                      "keyscan", "known_hosts", "username_private_key", "context="):
        assert forbidden not in source


# ── 3. Typed accepted-input capability (one owner, no opaque convention) ──────

@pytest.mark.asyncio
async def test_general_http_is_migrated_to_the_typed_capability() -> None:
    result = await GeneralHttpProvider().resolve(_request("https://files.example.org/f.bin"))
    candidate = result.candidates[0]
    assert candidate.accepted_input_methods == (InputMethod.USERNAME_PASSWORD,)
    assert "accepted_input_methods" not in candidate.context


def test_no_production_code_reads_or_writes_the_opaque_context_convention() -> None:
    for path in (ROOT / "backend").rglob("*.py"):
        if "tests" in path.parts:
            continue
        text = path.read_text()
        assert 'context={"accepted_input_methods"' not in text, path
        assert 'context.get("accepted_input_methods"' not in text, path


def test_typed_capability_round_trips_through_the_canonical_codec() -> None:
    candidate = TransferCandidate("f.bin", (Endpoint("ftp", "ftp://files.example.org/f.bin"),),
                                  accepted_input_methods=(InputMethod.USERNAME_PASSWORD,))
    stored = json.loads(codec.dump(candidate))
    assert stored["accepted_input_methods"] == ["username_password"]
    assert "accepted_input_methods" not in stored["context"]
    assert codec.candidate(stored) == candidate


def test_legacy_durable_candidates_decode_one_way_into_the_typed_field() -> None:
    legacy = json.loads(codec.dump(TransferCandidate("f.bin", (Endpoint("https", "https://files.example.org/f.bin"),))))
    legacy.pop("accepted_input_methods", None)
    legacy["context"] = {"accepted_input_methods": ["username_password"]}
    decoded = codec.candidate(legacy)
    assert decoded.accepted_input_methods == (InputMethod.USERNAME_PASSWORD,)
    assert "accepted_input_methods" not in decoded.context
    rewritten = json.loads(codec.dump(decoded))
    assert rewritten["accepted_input_methods"] == ["username_password"]
    assert rewritten["context"] == {}


def test_candidates_without_the_capability_decode_to_none_accepted() -> None:
    stored = json.loads(codec.dump(TransferCandidate("f.bin", (Endpoint("https", "https://x.example/f"),))))
    stored.pop("accepted_input_methods", None)
    assert codec.candidate(stored).accepted_input_methods == ()


@pytest.mark.parametrize("methods", [
    ("username_password",),
    (InputMethod.USERNAME_PASSWORD, InputMethod.USERNAME_PASSWORD),
])
def test_typed_capability_is_strict_and_duplicate_free(methods) -> None:
    with pytest.raises((TypeError, ValueError)):
        TransferCandidate("f.bin", (Endpoint("ftp", "ftp://x.example/f"),), accepted_input_methods=methods)


# ── 4. Routing through the existing registry ─────────────────────────────────

def _settings(**enabled) -> AppSettings:
    settings = normalize_settings(AppSettings(), definitions)
    for identity, value in enabled.items():
        settings.integrations[identity].enabled = value
    return settings


def _registry(tmp_path, settings) -> IntegrationRegistry:
    registry = IntegrationRegistry()
    environment = SimpleNamespace(
        aria2=None, download_root=str(tmp_path), aria2_configuration=Aria2Configuration(str(tmp_path)),
    )
    selected = tuple(item for item in definitions if item.kind == "provider")
    register(registry, settings, environment, selected=selected)
    registry.register_executor(Aria2Executor(None, Aria2Configuration(str(tmp_path)), AsyncMock(return_value=True),
                                             egress=SimpleNamespace(ensure_started=AsyncMock(), job_options=lambda *a, **k: {})))
    return registry


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["ftp://files.example.org/f.bin", "sftp://files.example.org/f.bin"])
async def test_enabled_provider_resolves_and_the_candidate_routes_to_aria2(tmp_path, url) -> None:
    registry = _registry(tmp_path, _settings())
    provider = registry.provider_for(_request(url))
    assert provider.descriptor.id == "general_ftp"
    candidate = (await provider.resolve(_request(url))).candidates[0]
    assert registry.executor_for(candidate).descriptor.id == "aria2"


@pytest.mark.parametrize("url", ["ftp://files.example.org/f.bin", "sftp://files.example.org/f.bin"])
def test_disabled_provider_yields_the_existing_unsupported_semantics(tmp_path, url) -> None:
    registry = _registry(tmp_path, _settings(general_ftp=False))
    with pytest.raises(TransferError) as raised:
        registry.provider_for(_request(url))
    assert raised.value.error.category in {Category.UNSUPPORTED_REQUEST, Category.UNSUPPORTED_CAPABILITY, Category.PROVIDER_UNAVAILABLE}
    # Executor capability is a separate fact: aria2 still truthfully claims both.
    assert {"ftp", "sftp"} <= Aria2Executor.descriptor.schemes


def test_enablement_is_independent_between_http_and_ftp(tmp_path) -> None:
    registry = _registry(tmp_path, _settings(general_http=False))
    assert registry.provider_for(_request("ftp://files.example.org/f.bin")).descriptor.id == "general_ftp"
    registry = _registry(tmp_path, _settings(general_ftp=False))
    assert registry.provider_for(_request("https://files.example.org/f.bin")).descriptor.id == "general_http"


def test_registry_and_applicability_carry_no_transport_names() -> None:
    for relative in ("backend/transfers/registry.py", "backend/transfers/applicability.py", "backend/transfers/policy.py"):
        source = (ROOT / relative).read_text().casefold()
        assert '"ftp"' not in source and '"sftp"' not in source


# ── 5. Settings: one generic integration toggle ──────────────────────────────

def test_general_ftp_enablement_persists_through_the_generic_namespace() -> None:
    previous = normalize_settings(AppSettings(), definitions)
    assert previous.integrations["general_ftp"].enabled is True
    draft = AppSettings(integrations={"general_ftp": IntegrationSettings(enabled=False)})
    merged = normalize_settings(draft, definitions, previous=previous)
    assert merged.integrations["general_ftp"].enabled is False
    assert merged.integrations["general_http"].enabled is True
    public = public_integrations(merged, definitions)
    assert public["general_ftp"]["name"] == "FTP & SFTP"
    assert public["general_ftp"]["enabled"] is False
    assert public["general_ftp"]["options"] == {}
    assert "ftp_enabled" not in AppSettings.model_fields and "sftp_enabled" not in AppSettings.model_fields
