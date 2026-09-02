from __future__ import annotations

import pytest

import db.database as database
from integrations.runtime_state import ProviderRuntimeStateStore
from providers.alldebrid.host_runtime import AllDebridHostMaintenance
from providers.alldebrid.provider import AllDebridProvider
from providers.general_http.provider import GeneralHttpProvider
from transfers.models import TransferRequest
from transfers.registry import IntegrationRegistry


class PatternClient:
    def __init__(self):
        self.host_calls = 0

    async def get_user_hosts(self):
        self.host_calls += 1
        return {
            "hosts": {
                "patterned": {
                    "name": "patterned",
                    "type": "premium",
                    "domains": ["example.test"],
                    "regexps": [r"https?://(?:www\.)?example\.test/files/[A-Za-z0-9_-]+$"],
                    "status": False,
                    "quota": 0,
                    "quotaMax": 100,
                    "quotaType": "traffic",
                    "limitSimuDl": 0,
                }
            }
        }


def provider_ids(registry, request):
    return tuple(item.descriptor.id for item in registry.eligible_providers(request))


@pytest.mark.asyncio
async def test_native_regex_is_interpreted_inside_alldebrid_before_neutral_routing(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "patterns.sqlite3")
    store = ProviderRuntimeStateStore()
    await store.start()

    client = PatternClient()
    alldebrid = AllDebridProvider(client=client)
    generic = GeneralHttpProvider()
    registry = IntegrationRegistry()
    registry.register_provider(alldebrid)
    registry.register_provider(generic)

    maintenance = AllDebridHostMaintenance(store)
    maintenance.bind(alldebrid, initial=True)
    await maintenance.start()
    await maintenance.maintain()
    assert client.host_calls == 1

    # The native regexp is path-sensitive. Only URLs it validates may become
    # neutral SPECIALIZED claims; a same-host unsupported path must remain on
    # the generic provider instead of being overclaimed by AllDebrid.
    assert provider_ids(
        registry,
        TransferRequest("https", "https://example.test/files/abc_123", name="ok"),
    ) == ("alldebrid",)
    assert provider_ids(
        registry,
        TransferRequest("https", "https://www.example.test/files/abc_123", name="www"),
    ) == ("alldebrid",)
    assert provider_ids(
        registry,
        TransferRequest("https", "https://example.test/account/profile", name="not-supported"),
    ) == ("general_http",)

    # Provider-local domain-boundary validation prevents a native unanchored
    # expression from turning a substring lookalike into a specialized claim.
    assert provider_ids(
        registry,
        TransferRequest("https", "https://evil-example.test/files/abc_123", name="lookalike"),
    ) == ("general_http",)

    # Transient availability/quota facts remain orthogonal to structural URL
    # support: status=false and exhausted limits above do not erase the valid
    # structural regexp match.
    assert client.host_calls == 1


@pytest.mark.asyncio
async def test_native_pattern_interpretation_does_not_change_static_magnet_torrent_claims(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "patterns-static.sqlite3")
    store = ProviderRuntimeStateStore()
    await store.start()

    alldebrid = AllDebridProvider(client=PatternClient())
    generic = GeneralHttpProvider()
    registry = IntegrationRegistry()
    registry.register_provider(alldebrid)
    registry.register_provider(generic)

    maintenance = AllDebridHostMaintenance(store)
    maintenance.bind(alldebrid, initial=True)
    await maintenance.start()

    assert provider_ids(
        registry,
        TransferRequest("magnet", "magnet:?xt=urn:btih:" + "a" * 40, name="magnet"),
    ) == ("alldebrid",)
    assert provider_ids(
        registry,
        TransferRequest("torrent", b"d4:infod4:name1:xee", name="sample.torrent"),
    ) == ("alldebrid",)
