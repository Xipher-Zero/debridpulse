"""DP 1.0.13 Transfer Trace: the bounded consolidation-component closure.

A consolidated artifact is only intelligible together with every transfer that
directly takes part in it. A trace of the canonical owner therefore carries the
transfers consolidated into it, and a trace of any contributor carries the
owner and its sibling contributors -- each with its own transfer-scoped rows
(scope ``component``), never by recursive graph crawling. The expansion is
bounded; hitting a bound is declared, with the participants it had to omit,
never presented as complete. Sanitization is unchanged.
"""
from dataclasses import replace
import json
import re
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from application.service import ApplicationService
from fake_integrations import MemoryExecutor, ParcelProvider
from services import transfer_trace
from transfers.engine import TransferEngine
from transfers.models import Endpoint, ResolutionResult, ResourceState, TransferRequest
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository

CAPABILITY = "https://cdn.example.org/dl/CAPTOKEN-X/part.rar?exp=1&sig=SIGSECRET"
OTHER = "https://cdn.example.org/dl/CAPTOKEN-Y/other.rar?sig=SIGSECRET-Y"
SOURCES = {
    "owner": "https://alice:S3cretPass@files.example.com/d/CAPTOKEN-A/part.rar?sig=QSECRET-A",
    "b": "https://mirror-b.example.net/get/CAPTOKEN-B?token=QSECRET-B",
    "c": "https://mirror-c.example.net/get/CAPTOKEN-C?token=QSECRET-C",
    "d": "https://mirror-d.example.net/get/CAPTOKEN-D?token=QSECRET-D",
    "e": "https://unrelated-e.example.net/get/CAPTOKEN-E?token=QSECRET-E",
    "f": "https://unrelated-f.example.net/get/CAPTOKEN-F?token=QSECRET-F",
}
SECRETS = ("alice", "S3cretPass", "SIGSECRET", "HDR-SECRET-123", "CAPTOKEN-X", "CAPTOKEN-Y",
           *[token for source in SOURCES.values() for token in re.findall(r"(?:CAPTOKEN|QSECRET)-[A-Z]", source)])


class Resolves(ParcelProvider):
    """Resolves every request to one capability URL (so equal URLs are one
    provable artifact) behind a secret header."""

    def __init__(self, identity, capability):
        super().__init__(identity)
        self.capability = capability

    async def resolve(self, request):
        candidate = self.candidate("part.rar", payload="unused")
        endpoint = Endpoint("memory", self.capability, {"Authorization": "Bearer HDR-SECRET-123"})
        return ResolutionResult(ResourceState.AVAILABLE, (replace(candidate, endpoints=(endpoint,)),))


@pytest_asyncio.fixture
async def component(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    for name in ("owner", "b", "c", "d"):
        registry.register_provider(Resolves(f"provider-{name}", CAPABILITY))
    for name in ("e", "f"):
        registry.register_provider(Resolves(f"provider-{name}", OTHER))
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"),
                            policy=TransferPolicy(adoption_stability_seconds=0), clock=lambda: 1000.0)
    await engine.initialize()
    transfers = {}
    for name in ("owner", "b", "c", "d", "e", "f"):
        transfers[name] = await engine.submit((TransferRequest(
            "parcel", SOURCES[name], name="part.rar", preferred_provider=f"provider-{name}"),),
            name=name, deduplicate=False)
        await engine.tick()
    canonical = (await repository.artifacts(transfers["owner"].id))[0]
    unrelated = (await repository.artifacts(transfers["e"].id))[0]
    return SimpleNamespace(ids={name: item.id for name, item in transfers.items()}, canonical=canonical,
                           unrelated=unrelated, application=ApplicationService(engine))


def _scoped(trace, table, column, value):
    return {item["scope"] for item in trace["data"][table] if item["row"][column] == value}


def _transfer_ids(trace):
    return {item["row"]["id"] for item in trace["data"]["torrents"]}


@pytest.mark.asyncio
async def test_the_fixture_is_one_component_of_four_and_one_unrelated_pair(component):
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT source_transfer_id,canonical_artifact_id FROM artifact_consolidations")
    by_canonical = {}
    for row in rows:
        by_canonical.setdefault(row["canonical_artifact_id"], set()).add(row["source_transfer_id"])
    ids = component.ids
    assert by_canonical == {component.canonical.id: {ids["b"], ids["c"], ids["d"]},
                            component.unrelated.id: {ids["f"]}}


@pytest.mark.asyncio
async def test_the_canonical_owner_trace_carries_every_direct_contributor(component):
    ids = component.ids
    trace = await transfer_trace.build(ids["owner"], component.application)
    for name in ("b", "c", "d"):
        assert _scoped(trace, "torrents", "id", ids[name]) == {"component"}
        # Each contributor's OWN transfer-scoped rows, not just a reference to it.
        assert _scoped(trace, "events", "torrent_id", ids[name]) == {"component"}
        assert "component" in _scoped(trace, "transfer_requests", "transfer_id", ids[name])
    closure = trace["metadata"]["closure"]["component"]
    assert closure["type"] == "consolidation_component" and closure["truncated"] is False
    assert closure["canonical_artifact_ids"] == [component.canonical.id]
    assert closure["transfer_ids"] == sorted(ids[name] for name in ("owner", "b", "c", "d"))
    assert closure["transfer_count"] == 4 and closure["artifact_count"] == 1
    assert closure["omitted_transfer_ids"] == [] and closure["omitted_artifact_ids"] == []
    assert trace["metadata"]["component_transfer_ids"] == sorted(ids[name] for name in ("b", "c", "d"))


@pytest.mark.asyncio
async def test_a_contributor_trace_carries_the_owner_and_its_sibling_contributors(component):
    ids = component.ids
    trace = await transfer_trace.build(ids["c"], component.application)
    assert _scoped(trace, "torrents", "id", ids["c"]) == {"primary"}
    for name in ("owner", "b", "d"):
        assert _scoped(trace, "torrents", "id", ids[name]) == {"component"}
        assert _scoped(trace, "events", "torrent_id", ids[name]) == {"component"}
    assert trace["metadata"]["closure"]["component"]["canonical_artifact_ids"] == [component.canonical.id]


@pytest.mark.asyncio
async def test_an_unrelated_consolidation_group_never_appears(component):
    ids = component.ids
    for name in ("owner", "c"):
        trace = await transfer_trace.build(ids[name], component.application)
        assert not _transfer_ids(trace) & {ids["e"], ids["f"]}
        assert component.unrelated.id not in {item["row"]["id"] for item in trace["data"]["download_files"]}


@pytest.mark.asyncio
async def test_a_bounded_expansion_declares_truncation_and_what_it_omitted(component, monkeypatch):
    ids = component.ids
    monkeypatch.setattr(transfer_trace, "COMPONENT_MAX_TRANSFERS", 2)
    trace = await transfer_trace.build(ids["owner"], component.application)
    closure = trace["metadata"]["closure"]["component"]
    assert closure["truncated"] is True
    assert closure["limits"]["max_transfers"] == 2
    participants = sorted(ids[name] for name in ("owner", "b", "c", "d"))
    assert closure["transfer_ids"] == participants[:2] and closure["omitted_transfer_ids"] == participants[2:]
    assert closure["transfer_count"] == 2
    # The omitted participants are reported, never silently dropped.
    assert set(closure["omitted_transfer_ids"]) | set(closure["transfer_ids"]) == set(participants)


@pytest.mark.asyncio
async def test_component_expansion_never_leaks_credentials(component):
    for name in ("owner", "c"):
        trace = await transfer_trace.build(component.ids[name], component.application)
        text = json.dumps(trace)
        for secret in SECRETS:
            assert secret not in text, secret
        # One capability URL, however many participants store it: one token.
        addresses = {endpoint["address"] for item in trace["data"]["download_files"]
                     for candidate in json.loads(item["row"]["candidates"]) for endpoint in candidate["endpoints"]}
        assert len(addresses) == 1
