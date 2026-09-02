from __future__ import annotations

from pathlib import Path

import pytest

import db.database as database
from integrations.runtime_state import ProviderRuntimeStateStore
from providers.alldebrid.host_runtime import (
    AllDebridHostMaintenance,
    HOST_REFRESH_SECONDS,
)
from providers.alldebrid.provider import AllDebridProvider
from providers.general_http.provider import GeneralHttpProvider
from transfers.engine import TransferEngine
from transfers.models import TransferRequest
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


def native_hosts():
    return {
        "hosts": {
            "example-service": {
                "name": "example-service",
                "type": "premium",
                "domains": ["example.test"],
                "regexps": [r"https?://example\.test/.+"],
                "status": True,
                "quota": 10,
                "quotaMax": 100,
                "quotaType": "traffic",
                "limitSimuDl": 2,
            }
        }
    }


class Clock:
    def __init__(self, value=1000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)


class CensusClient:
    def __init__(self):
        self.host_calls = 0
        self.unlock_calls = 0
        self.magnet_calls = 0
        self.torrent_calls = 0

    async def get_user_hosts(self):
        self.host_calls += 1
        return native_hosts()

    async def unlock_link(self, link):
        self.unlock_calls += 1
        return {
            "link": "https://8.8.8.8/download.bin",
            "filename": "download.bin",
            "filesize": 1,
        }

    async def upload_magnet(self, magnet):
        self.magnet_calls += 1
        return {
            "id": "1001",
            "statusCode": 0,
            "status": "Processing",
            "size": 0,
            "downloaded": 0,
        }

    async def upload_torrent_file(self, data, filename):
        self.torrent_calls += 1
        return {
            "id": "1002",
            "statusCode": 0,
            "status": "Processing",
            "size": 0,
            "downloaded": 0,
        }


async def runtime_store(tmp_path: Path, monkeypatch, name: str):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / name)
    store = ProviderRuntimeStateStore()
    await store.start()
    return store


@pytest.mark.asyncio
async def test_daily_refresh_cadence_is_deterministic_and_not_premature(tmp_path, monkeypatch):
    store = await runtime_store(tmp_path, monkeypatch, "cadence.sqlite3")
    clock = Clock()
    client = CensusClient()
    provider = AllDebridProvider(client=client)
    maintenance = AllDebridHostMaintenance(store, clock=clock)
    maintenance.bind(provider, initial=True)

    await maintenance.start()
    await maintenance.maintain()
    assert client.host_calls == 1

    clock.advance(HOST_REFRESH_SECONDS - 1)
    await maintenance.maintain()
    assert client.host_calls == 1

    clock.advance(1)
    await maintenance.maintain()
    assert client.host_calls == 2

    await maintenance.maintain()
    assert client.host_calls == 2


@pytest.mark.asyncio
async def test_submission_and_resolution_census_never_refreshes_host_inventory(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "census.sqlite3")
    await database.init_db()
    download_root = tmp_path / "downloads"
    download_root.mkdir()

    repository = TransferRepository()
    registry = IntegrationRegistry()
    client = CensusClient()
    alldebrid = AllDebridProvider(client=client)
    general = GeneralHttpProvider()
    registry.register_provider(alldebrid)
    registry.register_provider(general)

    engine = TransferEngine(
        repository,
        registry,
        download_root=str(download_root),
        policy=TransferPolicy(),
    )
    await engine.initialize()

    store = ProviderRuntimeStateStore()
    await store.start()
    clock = Clock()
    maintenance = AllDebridHostMaintenance(store, clock=clock)
    maintenance.bind(alldebrid, initial=True)
    await maintenance.start()
    await maintenance.maintain()
    assert client.host_calls == 1

    # Make the retained snapshot stale before ordinary work is admitted. Stale
    # last-known-good claims remain usable, but only maintenance may refresh.
    clock.advance(HOST_REFRESH_SECONDS + 1)

    requests = (
        TransferRequest("https", "https://example.test/supported.bin", name="supported.bin"),
        TransferRequest("https", "https://ordinary.test/generic.bin", name="generic.bin"),
        TransferRequest("magnet", "magnet:?xt=urn:btih:" + "a" * 40, name="magnet"),
        TransferRequest("torrent", b"d4:infod4:name1:xee", name="sample.torrent"),
    )

    for request in requests:
        await engine.submit((request,), name=request.name, deduplicate=False)
        assert client.host_calls == 1

    await engine.resolve_pending()

    assert client.host_calls == 1
    assert client.unlock_calls == 1
    assert client.magnet_calls == 1
    assert client.torrent_calls == 1

    # A separate maintenance cycle, not submission/routing/resolution, owns the
    # stale refresh and therefore accounts for the next host endpoint call.
    await maintenance.maintain()
    assert client.host_calls == 2
