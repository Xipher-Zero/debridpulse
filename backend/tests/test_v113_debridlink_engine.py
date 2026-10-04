"""Debrid-Link through the unchanged universal core.

These controls drive the real TransferEngine with the real Debrid-Link
provider over an in-memory Debrid-Link account. They prove the provider rides
the existing machinery -- resource lifecycle, manifest fan-out, the one
CandidateRefresh issuance boundary, canonical evidence and convergence -- and
that nothing it issues reaches durable state. Nothing in core names it.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import db.database as database
from fake_integrations import MemoryExecutor
from providers.debridlink.host_runtime import (
    DebridLinkRequestApplicability, applicability_facts, parse_native_host_snapshot,
)
from providers.debridlink.provider import DebridLinkProvider
from transfers.applicability import ApplicabilityReadiness, HostClaim, HostClaimScope, ProviderApplicability
from transfers.convergence_engine import TransferEngine
from transfers.models import (
    ArtifactFingerprint, Capability, DeliveryKind, Endpoint, ExecutorCapabilities, IntegrationDescriptor,
    ResolutionResult, ResourceState, SourceIdentity, TransferCandidate, TransferRequest, TransferState,
)
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

KEY = "dl-private-api-key-engine"
SIZE = 4
MAGNET = "magnet:?xt=urn:btih:" + "e" * 40 + "&dn=Show"
HOSTS = [{"name": "hoster", "type": "host", "domains": ["hoster.example", "mirror.example"], "regexs": []}]
CORE = Path(__file__).resolve().parents[1] / "transfers"


class Account:
    """An in-memory Debrid-Link account. Every link it issues carries a fresh
    capability token, as a generated download link may."""

    configured = True
    api_key = KEY

    def __init__(self, objects):
        self.objects = objects          # hoster URL -> bytes, or a list of (name, URL) for a folder
        self.issued = 0
        self.calls = []
        self.links_by_id = {}
        self.torrents = {}

    def secrets(self):
        return (KEY,)

    def _issue(self, key):
        self.issued += 1
        return f"https://dl6.debrid.link/dl/{key}?capability=dl-token-{self.issued}"

    def _link(self, url):
        link_id = hashlib.sha1(url.encode()).hexdigest()[:12]
        record = {"id": link_id, "name": url.rsplit("/", 1)[-1], "size": SIZE, "url": url,
                  "downloadUrl": self._issue(url.split("://", 1)[1]), "expired": False}
        self.links_by_id[link_id] = record
        return record

    async def add_link(self, url):
        self.calls.append(("add_link", url))
        value = self.objects[url]
        if isinstance(value, list):
            return [self._link(child) for child in value]
        return self._link(url)

    async def links(self, ids):
        self.calls.append(("links", ids))
        return [self.links_by_id[link_id] for link_id in ids if link_id in self.links_by_id]

    async def remove_links(self, ids):
        self.calls.append(("remove_links", ids))
        return [self.links_by_id.pop(link_id) and link_id for link_id in ids if link_id in self.links_by_id]

    async def add_torrent(self, *, magnet="", metainfo=None, name=""):
        self.calls.append(("add_torrent", magnet))
        self.torrents["t0rr3nt"] = {"id": "t0rr3nt", "name": "Show", "hashString": "e" * 40, "status": 100,
                                    "totalSize": SIZE, "downloadPercent": 100, "isZip": False}
        return {"id": "t0rr3nt", "name": "Show", "status": 1, "files": []}

    async def torrent(self, torrent_id):
        self.calls.append(("torrent", torrent_id))
        native = self.torrents.get(torrent_id)
        if native is None:
            return None
        return {**native, "files": [{"id": f"{torrent_id}-1", "name": "Show/episode.mkv", "size": SIZE,
                                     "downloadPercent": 100,
                                     "downloadUrl": self._issue(f"seedbox/{torrent_id}-1")}]}

    async def remove_torrent(self, torrent_id):
        self.calls.append(("remove_torrent", torrent_id))
        return [torrent_id] if self.torrents.pop(torrent_id, None) else []

    async def account(self):
        return {"accountType": 1, "premiumLeft": 86400}


class Sampler(MemoryExecutor):
    """An HTTPS copier that samples the bytes an address serves; the capability
    token in an address selects nothing, as for a real object behind rotating
    links."""

    claim_schemes = frozenset({"https"})
    capabilities = ExecutorCapabilities(candidate_sampling=True, per_execution_pause=True)

    def __init__(self, authorize, bodies):
        super().__init__(authorize)
        self.bodies = bodies
        self.fetched = []

    def _body(self, address):
        return self.bodies[address.split("://", 1)[1].split("?", 1)[0].removeprefix("dl6.debrid.link/dl/")]

    async def fingerprint(self, subject):
        address = subject.candidate.endpoints[0].address
        assert address, "a transient endpoint is issued before it is sampled"
        body = self._body(address)
        return ArtifactFingerprint(len(body), hashlib.sha256(body).hexdigest())

    async def start(self, request, handle):
        self.fetched.append(request.work.subject.candidate.endpoints[0].address)
        return await super().start(request, handle)


class Elsewhere:
    """Another neutral provider claiming other.example, issuing durable links."""

    descriptor = IntegrationDescriptor("elsewhere", "Elsewhere", frozenset({Capability.RESOLVE}),
                                       request_types=frozenset({"https"}))

    def applicability_for(self, request):
        return ProviderApplicability(
            specialized_hosts=(HostClaim("other.example", HostClaimScope.EXACT, frozenset({"https"})),),
            specialized=True, readiness=ApplicabilityReadiness.READY)

    applicability = property(lambda self: self.applicability_for(None))

    async def resolve(self, request):
        return ResolutionResult(ResourceState.AVAILABLE, (TransferCandidate(
            "movie.bin", (Endpoint("https", str(request.payload)),), SIZE, provider_id="elsewhere",
            refresh_request=request, delivery=DeliveryKind.DIRECT,
            source_identity=SourceIdentity("host", "other.example")),))


async def lab(tmp_path, monkeypatch, objects, bodies, *extra):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "debridlink.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    account = Account(objects)
    provider = DebridLinkProvider(account)
    snapshot = parse_native_host_snapshot(HOSTS)
    provider.applicability = applicability_facts(snapshot)
    provider.applicability_for = DebridLinkRequestApplicability(snapshot)
    registry.register_provider(provider)
    for item in extra:
        registry.register_provider(item)
    executor = Sampler(repository.authorize_execution, bodies)
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "downloads"),
                            policy=TransferPolicy(retry_delay=0, adoption_stability_seconds=0,
                                                  max_active_executions=8, resolution_concurrency=8),
                            clock=lambda: 1000.0)
    await engine.initialize()
    return engine, repository, account, executor


async def drive(engine, executor, repository, transfer_id, cycles=10):
    for _ in range(cycles):
        await engine.tick()
        for attempt in await repository.executions(transfer_id):
            if attempt.state not in {"succeeded", "failed", "absent", "cancelled"}:
                executor.finish(attempt.handle)
        if (await repository.get(transfer_id)).state == TransferState.COMPLETED:
            return


async def durable_text() -> str:
    async with database.get_db() as db:
        tables = [row["name"] for row in await db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        values = []
        for table in tables:
            for row in await db.fetchall(f'SELECT * FROM "{table}"'):
                values.extend(str(value) for value in dict(row).values() if value is not None)
    return "\n".join(values)


async def test_a_magnet_completes_and_no_issued_link_or_key_is_durable(tmp_path, monkeypatch):
    engine, repository, account, executor = await lab(
        tmp_path, monkeypatch, {}, {"seedbox/t0rr3nt-1": b"ep01"})
    transfer = await engine.submit((TransferRequest("magnet", MAGNET, "Show", "e" * 40),), name="Show")

    await drive(engine, executor, repository, transfer.id)

    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert executor.fetched and all("dl-token-" in address for address in executor.fetched)
    stored = await durable_text()
    assert KEY not in stored and "dl-token-" not in stored and "dl6.debrid.link" not in stored
    [artifact] = await repository.artifacts(transfer.id)
    assert all(endpoint.transient and endpoint.address == ""
               for candidate in artifact.candidates for endpoint in candidate.endpoints)
    assert [call[0] for call in account.calls].count("add_torrent") == 1


async def test_a_folder_link_materializes_each_distinct_file_as_its_own_artifact(tmp_path, monkeypatch):
    folder = "https://hoster.example/folder/abc"
    files = ["https://hoster.example/f/one.bin", "https://hoster.example/f/two.bin"]
    engine, repository, account, executor = await lab(
        tmp_path, monkeypatch, {folder: files, **{url: url for url in files}},
        {"hoster.example/f/one.bin": b"one!", "hoster.example/f/two.bin": b"two!"})
    transfer = await engine.submit((TransferRequest("https", folder, "abc"),), name="abc")

    await drive(engine, executor, repository, transfer.id)

    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    artifacts = await repository.artifacts(transfer.id)
    assert sorted(artifact.name for artifact in artifacts) == ["one.bin", "two.bin"]
    assert all(len(artifact.candidates) == 1 for artifact in artifacts)  # files are never mirrors
    assert "dl-token-" not in await durable_text()


async def test_equal_bytes_from_debridlink_and_another_provider_converge_canonically(tmp_path, monkeypatch):
    here, there = "https://hoster.example/f/movie.bin", "https://other.example/movie.bin"
    engine, repository, account, executor = await lab(
        tmp_path, monkeypatch, {here: here}, {"hoster.example/f/movie.bin": b"same", "other.example/movie.bin": b"same"},
        Elsewhere())
    transfer = await engine.submit((TransferRequest("https", here, "movie.bin"),
                                    TransferRequest("https", there, "movie.bin")), name="movie", deduplicate=False)

    await drive(engine, executor, repository, transfer.id)

    artifacts = await repository.artifacts(transfer.id)
    assert len(artifacts) == 1
    assert {candidate.provider_id for candidate in artifacts[0].candidates} == {"debridlink", "elsewhere"}
    assert "dl-token-" not in await durable_text()


def test_core_names_no_debridlink_and_imports_nothing_of_it():
    # The provider identity, never as a branch or an import. (The pre-existing
    # generic direct-link filename fallback in transfers/requests.py happens
    # to spell "debrid-link"; it predates this provider and routes nothing.)
    for path in CORE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "debridlink" not in text.casefold() and "providers.debrid" not in text, path
    for owner in ("application/composition.py", "integrations/account_entitlement.py", "transfers/mirrors.py"):
        assert "debridlink" not in (CORE.parent / owner).read_text(encoding="utf-8").casefold()


def test_the_provider_owns_no_second_refresh_evidence_selection_or_account_lifecycle():
    package = CORE.parent / "providers" / "debridlink"
    text = "\n".join(path.read_text(encoding="utf-8") for path in package.glob("*.py"))
    for foreign in ("CandidateRefresh", "shared_evidence", "self_evidence", "materialization_authorization",
                    "commit_selected_manifest", "files-unwanted", "class AccountEntitlement",
                    "SlidingWindowRateLimiter", "import transfers.convergence_engine", "TransferEngine"):
        assert foreign not in text, foreign
    assert json.dumps(sorted(path.name for path in package.glob("*.py"))) == json.dumps(
        ["__init__.py", "account.py", "admin.py", "client.py", "definition.py", "host_runtime.py", "provider.py",
         "translation.py"])
