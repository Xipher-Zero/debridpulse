"""DP 1.0.13 Generalized Extension C -- origin provider presentation.

A transfer's ORIGIN provider is the provider owning its root request's route:
derived from durable root route truth (never persisted as a second provider
identity), and projected beside -- never instead of -- the existing current,
delivering, route-attempt and execution provider facts. A decomposed
collection (root provider A, members delivered by provider B) therefore shows
A as its compact origin while every other fact keeps telling the truth about
B. Both read models (the comprehensive Details presentation and the bounded
list projection) derive the same fact; the browser only consumes it.

Provider-neutral: two fake providers over unrelated ``vaultdir``/``vault``
transports.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import pytest_asyncio

import db.database as database
from test_v113_post_probe_provider_fallthrough import PlainSource, ProbeTransport, ProbingSource
from test_v113_transfer_auth_context import CountingVault
from transfers.applicability import ProviderApplicability
from transfers.convergence_engine import TransferEngine
from transfers.models import (
    Capability, Endpoint, FileManifest, FileManifestEntry, InputMethod, IntegrationDescriptor, Ownership,
    ProviderObservation, ProviderResource, ResolutionResult, ResourceState, SourceEntry, SourceIdentity,
    TransferCandidate, TransferRequest, TransferState,
)
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

ROOT = Path(__file__).resolve().parents[2]
MEMBERS = ("a.bin", "b.bin")


class CollectionSource:
    """Root-only provider: a ``vaultdir`` collection whose members are
    ordinary ``vault`` requests some other provider resolves."""

    applicability = ProviderApplicability(generic_schemes=frozenset({"vaultdir"}))

    def __init__(self):
        self.descriptor = IntegrationDescriptor(
            "collection-source", "Collection source",
            frozenset({Capability.RESOLVE, Capability.RESOURCE_LOOKUP, Capability.FILE_MANIFEST}),
            request_types=frozenset({"vaultdir"}))

    def _observation(self, resource):
        return ProviderObservation(resource, ResourceState.AVAILABLE, "collection", file_manifest=FileManifest(
            tuple(FileManifestEntry(name, name, 4) for name in MEMBERS)))

    async def resolve(self, request):
        resource = ProviderResource(self.descriptor.id, {"base": request.payload.replace("vaultdir", "vault", 1)},
                                    Ownership.OBSERVED, id=f"collection:{request.payload}")
        return ResolutionResult(ResourceState.AVAILABLE, observation=self._observation(resource))

    async def observe(self, resource):
        return self._observation(resource)

    async def manifest(self, resource):
        base = resource.context["base"]
        return tuple(SourceEntry(name, 4, name, TransferRequest("vault", base + name, name=name)) for name in MEMBERS)


class MemberSource:
    applicability = ProviderApplicability(generic_schemes=frozenset({"vault"}))

    def __init__(self):
        self.descriptor = IntegrationDescriptor("member-source", "Member source", frozenset({Capability.RESOLVE}),
                                                request_types=frozenset({"vault"}))

    async def resolve(self, request):
        return ResolutionResult(ResourceState.AVAILABLE, (TransferCandidate(
            request.name, (Endpoint("vault", request.payload),), expected_bytes=4, provider_id=self.descriptor.id,
            source_identity=SourceIdentity("host", urlsplit(request.payload).hostname),
            accepted_input_methods=(InputMethod.USERNAME_PASSWORD,)),))


DEFINITIONS = [SimpleNamespace(id=identity, name=name) for identity, name in (
    ("collection-source", "Collection"), ("member-source", "Member"),
    ("probe-source", "Probing"), ("plain-source", "Plain"))]


@pytest_asyncio.fixture
async def stage(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "origin.sqlite3")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"),
                            policy=TransferPolicy(retry_delay=0, adoption_stability_seconds=0,
                                                  max_active_executions=5))
    await engine.initialize()
    return repository, registry, engine


async def _complete(engine, repository, transfer_id):
    for _ in range(60):
        await engine.tick()
        if (await repository.get(transfer_id)).state in {TransferState.COMPLETED, TransferState.FAILED}:
            break
    assert (await repository.get(transfer_id)).state == TransferState.COMPLETED


def _decomposition(stage):
    repository, registry, engine = stage
    registry.register_provider(CollectionSource())
    registry.register_provider(MemberSource())
    registry.register_executor(CountingVault(repository.authorize_execution, objects={
        f"h.example/dir/{name}": b"four" for name in MEMBERS}))
    return repository, engine


async def _listed(transfer_id):
    from api import operational_downloads as downloads
    page = await downloads.list_operational_torrents(
        status=None, search=None, limit=25, offset=0,
        application=SimpleNamespace(repository=None, definitions=DEFINITIONS, engine=None))
    return next(item for item in page["items"] if int(item["id"]) == transfer_id)


async def test_a_decomposed_collection_keeps_its_origin_beside_the_truthful_delivery_facts(stage):
    repository, engine = _decomposition(stage)
    transfer = await engine.submit((TransferRequest("vaultdir", "vaultdir://h.example/dir/"),), deduplicate=False)
    await _complete(engine, repository, transfer.id)

    details = await repository.presentation(transfer.id, details=True)
    assert details["origin_provider_id"] == "collection-source"
    # Nothing about the existing route model is redefined.
    assert details["current_provider_id"] == "member-source"
    assert details["delivering_provider_id"] == "member-source"
    assert {item["provider_id"] for item in details["route_attempts"]} == {"collection-source", "member-source"}

    listed = await _listed(transfer.id)
    assert listed["origin_provider_id"] == "collection-source"
    assert listed["origin_provider_name"] == "Collection"
    assert (listed["current_provider_id"], listed["delivering_provider_id"]) == ("member-source", "member-source")
    assert (listed["current_provider_name"], listed["delivering_provider_name"]) == ("Member", "Member")


async def test_the_public_detail_projection_names_the_origin(stage):
    from api.routes import _public_transfer_presentation
    repository, engine = _decomposition(stage)
    transfer = await engine.submit((TransferRequest("vaultdir", "vaultdir://h.example/dir/"),), deduplicate=False)
    await _complete(engine, repository, transfer.id)
    public = _public_transfer_presentation(await repository.presentation(transfer.id, details=True), DEFINITIONS)
    assert public["origin_provider_id"] == "collection-source" and public["origin_provider_name"] == "Collection"
    assert public["current_provider_name"] == "Member"


async def test_a_single_provider_transfer_has_the_same_origin_and_current_provider(stage):
    repository, registry, engine = stage
    registry.register_provider(MemberSource())
    registry.register_executor(CountingVault(repository.authorize_execution, objects={"h.example/solo.bin": b"four"}))
    transfer = await engine.submit((TransferRequest("vault", "vault://h.example/solo.bin", name="solo.bin"),),
                                   deduplicate=False)
    await _complete(engine, repository, transfer.id)
    details = await repository.presentation(transfer.id, details=True)
    listed = await _listed(transfer.id)
    assert details["origin_provider_id"] == listed["origin_provider_id"] == details["current_provider_id"] \
        == "member-source"


async def test_roots_owned_by_different_providers_have_no_single_origin(stage):
    repository, engine = _decomposition(stage)
    transfer = await engine.submit((TransferRequest("vaultdir", "vaultdir://h.example/dir/"),
                                    TransferRequest("vault", "vault://h.example/dir/a.bin", name="a.bin")),
                                   deduplicate=False)
    await _complete(engine, repository, transfer.id)
    details = await repository.presentation(transfer.id, details=True)
    listed = await _listed(transfer.id)
    assert details["origin_provider_id"] is None and listed["origin_provider_id"] is None


async def test_a_provider_that_declined_is_never_the_origin(stage):
    repository, registry, engine = stage
    registry.register_provider(PlainSource())
    registry.register_provider(ProbingSource())
    registry.register_executor(ProbeTransport(repository.authorize_execution, answers={"opaque.example": "opaque"},
                                              objects={"opaque.example/dir/": b"four"}))
    transfer = await engine.submit((TransferRequest("vault", "vault://opaque.example/dir/"),), deduplicate=False)
    await _complete(engine, repository, transfer.id)
    details = await repository.presentation(transfer.id, details=True)
    listed = await _listed(transfer.id)
    assert details["origin_provider_id"] == listed["origin_provider_id"] == "plain-source"


async def test_the_browser_consumes_the_projected_origin_and_infers_nothing():
    app = (ROOT / "frontend/static/app.js").read_text()
    body = app.split("function transferProviderPresentation(t) {", 1)[1].split("\n}\n", 1)[0]
    assert "t?.origin_provider_name" in body
    # Origin is never reconstructed from parents, manifests, URLs or route walks.
    for forbidden in ("route_attempts", "parent", "manifest", "request_kinds", "original_resource", "http"):
        assert forbidden not in body, forbidden
