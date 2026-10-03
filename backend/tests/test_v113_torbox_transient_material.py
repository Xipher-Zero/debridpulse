"""TorBox execution material is transient; its member identity is durable.

Transfer 478: a single-file torrent became available, its one member was
selected, and materialization then failed because the link TorBox issued
embeds the account token. The link is execution material: the executor gets it
fresh for each execution, and it never reaches a durable row, a trace, route
provenance, a normalized error, a status payload or a log. The member address
(family, object, file) is the durable truth that regenerates it.
"""
from __future__ import annotations

import json
import logging
from dataclasses import replace

import pytest

import db.database as database
from fake_integrations import MemoryExecutor
from providers.torbox.client import TORRENT, USENET, WEBDL, member_address, parse_member_address
from providers.torbox.host_runtime import TorBoxHostMaintenance
from providers.torbox.provider import TorBoxProvider
from test_v113_torbox_provider import MAGNET, TOKEN, FakeClient, MemoryStore, torrent
from transfers import codec
from transfers.convergence_engine import TransferEngine
from transfers.errors import Domain, TransferError
from transfers.models import Endpoint, ExecutorCapabilities, TransferRequest, TransferState
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio


class TokenizedClient(FakeClient):
    """requestdl issues links that embed the exact account token, as live TorBox does."""

    async def requestdl(self, family, native_id, file_id):
        self.calls.append(("requestdl", family, native_id, file_id))
        self.links += 1
        return f"https://store-1.tb-cdn.st/dld/{family}-{native_id}-{file_id}?token={TOKEN}&n={self.links}"


class HttpCopy(MemoryExecutor):
    """A neutral HTTPS copier that records exactly what it was asked to fetch."""

    claim_schemes = frozenset({"https"})
    capabilities = ExecutorCapabilities(candidate_sampling=False, per_execution_pause=True)

    def __init__(self, authorize):
        super().__init__(authorize)
        self.fetched = []

    async def start(self, request, handle):
        self.fetched.append([endpoint.address for endpoint in request.work.subject.candidate.endpoints])
        return await super().start(request, handle)


async def durable_text() -> str:
    """Every text value of every row of every table, in one string."""
    async with database.get_db() as db:
        tables = [row["name"] for row in await db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        values = []
        for table in tables:
            for row in await db.fetchall(f'SELECT * FROM "{table}"'):
                values.extend(str(value) for value in dict(row).values() if value is not None)
    return "\n".join(values)


async def single_file_torrent(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    client = TokenizedClient()
    provider = TorBoxProvider(client)
    TorBoxHostMaintenance(provider, MemoryStore())
    repository = TransferRepository()
    registry = IntegrationRegistry()
    registry.register_provider(provider)
    executor = HttpCopy(repository.authorize_execution)
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "downloads"),
                            policy=TransferPolicy(retry_delay=0, adoption_stability_seconds=0), clock=lambda: 1000.0)
    await engine.initialize()
    original = client.create_torrent

    async def available(**kwargs):
        native_id = await original(**kwargs)
        client.objects[TORRENT][native_id].update(torrent(
            int(native_id), present=True, state="cached", name="ubuntu",
            files=[{"id": 0, "name": "ubuntu/ubuntu-desktop.iso", "size": 4}]))
        return native_id

    client.create_torrent = available
    return client, provider, repository, executor, engine


async def drive(engine, executor, repository, transfer_id, cycles=8):
    for _ in range(cycles):
        await engine.tick()
        for attempt in await repository.executions(transfer_id):
            if attempt.state not in {"succeeded", "failed", "absent", "cancelled"}:
                executor.finish(attempt.handle)
        if (await repository.get(transfer_id)).state == TransferState.COMPLETED:
            return


async def test_the_transfer_478_shape_completes_with_no_token_anywhere_durable_or_observable(
        tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    client, provider, repository, executor, engine = await single_file_torrent(tmp_path, monkeypatch)
    transfer = await engine.submit((TransferRequest("magnet", MAGNET, "ubuntu", "a" * 40),), name="ubuntu")

    await drive(engine, executor, repository, transfer.id)

    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    # The executor received a usable, fresh, provider-issued link.
    assert executor.fetched and all(TOKEN in addresses[0] for addresses in executor.fetched)
    # ...and nothing durable or observable holds the token or the link.
    stored = await durable_text()
    assert TOKEN not in stored and "tb-cdn" not in stored
    detail = await repository.presentation(transfer.id, details=True)
    assert TOKEN not in json.dumps(detail, default=str)
    assert TOKEN not in caplog.text
    # The durable member identity is credential-free and regenerates material.
    [member] = [record for record in await repository.requests(transfer.id) if record.parent_id]
    assert parse_member_address(member.request.payload) == (TORRENT, "101", "0")
    assert TOKEN not in member.request.payload
    [artifact] = await repository.artifacts(transfer.id)
    [endpoint] = artifact.candidates[artifact.selected].endpoints
    assert endpoint.transient and endpoint.address == ""
    # download_files.candidates, execution_attempts.candidate and route
    # provenance rows are all inside the whole-database scan above; name them.
    async with database.get_db() as db:
        rows = [*await db.fetchall("SELECT candidates FROM download_files"),
                *await db.fetchall("SELECT candidate FROM execution_attempts"),
                *await db.fetchall("SELECT * FROM route_attempt_provenance")]
    assert rows and not any(TOKEN in json.dumps(dict(row), default=str) for row in rows)


async def test_each_execution_gets_fresh_material_from_the_same_durable_identity(tmp_path, monkeypatch):
    client = TokenizedClient()
    provider = TorBoxProvider(client)
    member = TransferRequest("https", member_address(TORRENT, "9", "0"), "x")
    [first] = (await provider.resolve(member)).candidates
    stored = codec.candidate(codec.load(codec.dump(first)))
    [refreshed] = (await provider.refresh(stored)).candidates
    assert refreshed.endpoints[0].address != first.endpoints[0].address          # fresh link
    assert refreshed.refresh_request == stored.refresh_request == member         # same durable identity
    assert codec.dump(refreshed.refresh_request) == codec.dump(member)


@pytest.mark.parametrize("family", [TORRENT, WEBDL, USENET])
async def test_every_family_uses_the_one_provider_local_regeneration(family):
    client = TokenizedClient()
    provider = TorBoxProvider(client)
    member = TransferRequest("https", member_address(family, "3", "1"), "x")
    [candidate] = (await provider.resolve(member)).candidates
    assert candidate.endpoints[0].transient and TOKEN in candidate.endpoints[0].address
    assert TOKEN not in codec.dump(candidate)
    [again] = (await provider.refresh(codec.candidate(codec.load(codec.dump(candidate))))).candidates
    assert again.endpoints[0].transient and client.calls.count(("requestdl", family, "3", "1")) == 2


async def test_transient_material_still_crosses_network_safety():
    client = TokenizedClient()

    async def private(family, native_id, file_id):
        return f"http://10.0.0.5/dld/x?token={TOKEN}"
    client.requestdl = private
    with pytest.raises(TransferError) as caught:
        await TorBoxProvider(client).resolve(TransferRequest("https", member_address(TORRENT, "1", "0"), "x"))
    assert caught.value.error.domain == Domain.SECURITY
    assert TOKEN not in json.dumps(caught.value.error.as_dict(diagnostics=True), default=str)


async def test_the_durability_scan_would_catch_a_raw_endpoint_crossing_persistence():
    """Adversarial proof: the very same material WITHOUT the transient fact is
    ordinary durable candidate truth -- the persistence boundary, not luck,
    is what keeps the token out."""
    client = TokenizedClient()
    [candidate] = (await TorBoxProvider(client).resolve(
        TransferRequest("https", member_address(TORRENT, "1", "0"), "x"))).candidates
    raw = replace(candidate, endpoints=tuple(replace(item, transient=False) for item in candidate.endpoints))
    assert TOKEN in codec.dump(raw)
    assert TOKEN not in codec.dump(candidate)
    assert TOKEN not in codec.dump(Endpoint("https", f"https://x/?t={TOKEN}", {"Authorization": TOKEN}, True))
