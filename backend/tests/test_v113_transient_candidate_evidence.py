"""Transient execution material can prove equivalence through the one evidence owner.

A provider-issued candidate whose endpoint is transient keeps only that fact in
durable state; its address rotates on every issue. Evidence for it is read from
material issued in memory, for one decision, by the bound provider's neutral
``CandidateRefresh`` -- the same boundary execution uses -- and only the
neutral fingerprint survives. Bytes decide: equal bytes behind rotating
addresses converge; equal names, sizes and metadata with different bytes never
do. Neutral fixtures only: no concrete integration is named.
"""
from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

import db.database as database
from db.database import get_db
from fake_integrations import MemoryExecutor
from transfers.applicability import ProviderApplicability
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage, TransferError
from transfers.models import (
    ArtifactFingerprint, Capability, DeliveryKind, Endpoint, IntegrationDescriptor, ResolutionResult,
    ResourceState, SourceIdentity, TransferCandidate, TransferRequest,
)
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

SIZE = 4096


class RelayProvider:
    """Issues a transient address carrying a fresh capability token every time
    it is asked -- by resolution or by refresh -- for one durable object."""

    def __init__(self, identity="relay-lab"):
        self.descriptor = IntegrationDescriptor(identity, identity, frozenset({Capability.RESOLVE, Capability.REFRESH}),
                                                request_types=frozenset({"relay"}))
        self.issued = 0
        self.refreshed: list[str] = []
        self.refresh_error: Exception | None = None

    @property
    def applicability(self):
        return ProviderApplicability()

    def _issue(self, request, candidate_id=None):
        self.issued += 1
        obj, _, host = str(request.payload).partition("@")
        candidate = TransferCandidate(
            "payload.bin", (Endpoint("memory", f"memory:{obj}?token=secret-{self.issued}", transient=True),), SIZE,
            provider_id=self.descriptor.id, refresh_request=request, delivery=DeliveryKind.PROVIDER_ISSUED,
            source_identity=SourceIdentity("host", host or obj))
        return replace(candidate, id=candidate_id) if candidate_id else candidate

    async def resolve(self, request):
        return ResolutionResult(ResourceState.AVAILABLE, (self._issue(request),))

    async def refresh(self, candidate):
        self.refreshed.append(str(candidate.id))
        if self.refresh_error is not None:
            raise self.refresh_error
        return ResolutionResult(ResourceState.AVAILABLE, (self._issue(candidate.refresh_request, candidate.id),))


class ByteSampler(MemoryExecutor):
    """Samples the bytes an address serves; the capability token in the
    address selects nothing, exactly as for a real object behind rotating links."""

    def __init__(self, authorize, objects):
        super().__init__(authorize)
        self.objects = objects
        self.sampled: list[str] = []

    async def fingerprint(self, subject):
        address = subject.candidate.endpoints[0].address
        assert address, "a transient endpoint must be issued before it is sampled"
        self.sampled.append(address)
        body = self.objects[address.split(":", 1)[1].split("?", 1)[0]]
        return ArtifactFingerprint(len(body), hashlib.sha256(body).hexdigest())


async def lab(tmp_path, monkeypatch, objects):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "transient-evidence.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = RelayProvider()
    executor = ByteSampler(repository.authorize_execution, objects)
    registry.register_provider(provider)
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"),
                            policy=TransferPolicy(retry_delay=0, max_active_executions=8, resolution_concurrency=8),
                            clock=lambda: 1000.0)
    await engine.initialize()
    return engine, repository, provider, executor


async def submit(engine, *payloads):
    return await engine.submit(tuple(TransferRequest("relay", payload, name="payload.bin") for payload in payloads),
                               name="payload", deduplicate=False)


async def drive(engine, passes=6):
    for _ in range(passes):
        await engine.tick()


async def requests(repository, transfer_id):
    return {str(item.request.payload): item for item in await repository.requests(transfer_id)}


async def bindings(transfer_id):
    async with get_db() as db:
        return await db.fetchall("SELECT b.* FROM canonical_candidate_bindings b JOIN download_files f "
                                 "ON f.id=b.canonical_artifact_id WHERE f.torrent_id=?", (transfer_id,))


async def durable_text():
    """Every TEXT value DebridPulse durably holds, across every table."""
    async with get_db() as db:
        tables = [row["name"] for row in await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")]
        values = []
        for table in tables:
            for row in await db.fetchall(f'SELECT * FROM "{table}"'):
                values.extend(str(value) for value in dict(row).values() if isinstance(value, str))
    return values


def sources(rows):
    return {(row["canonical_artifact_id"], row["source_key"]) for row in rows}


# -- 14.3: equal bytes behind rotating transient addresses converge ------------------

async def test_equal_bytes_behind_rotating_transient_addresses_converge_to_one_artifact(tmp_path, monkeypatch):
    body = b"x" * SIZE
    engine, repository, provider, executor = await lab(tmp_path, monkeypatch, {"one": body, "two": body})
    transfer = await submit(engine, "one@mirror-a.test", "two@mirror-b.test")

    await drive(engine)

    artifacts = await repository.artifacts(transfer.id)
    assert len(artifacts) == 1
    assert sources(await bindings(transfer.id)) == {(artifacts[0].id, "mirror-a.test"), (artifacts[0].id, "mirror-b.test")}
    # The peer persisted with only the transient fact was read from material
    # issued again, in memory, for its own durable identity.
    assert provider.refreshed and set(provider.refreshed) <= {str(c.id) for c in artifacts[0].candidates}
    assert executor.sampled and all("?token=secret-" in address for address in executor.sampled)
    assert all(not endpoint.address for c in artifacts[0].candidates for endpoint in c.endpoints)


# -- 14.4: equal metadata, different bytes, never equivalent ------------------------

async def test_equal_name_size_and_metadata_with_different_bytes_never_converge(tmp_path, monkeypatch):
    engine, repository, provider, executor = await lab(
        tmp_path, monkeypatch, {"one": b"x" * SIZE, "two": b"y" * SIZE})
    transfer = await submit(engine, "one@mirror-a.test", "two@mirror-b.test")

    await drive(engine)

    # Both were sampled -- the decision was made from bytes, and bytes differ.
    assert {address.split("?")[0] for address in executor.sampled} == {"memory:one", "memory:two"}
    by_artifact = {}
    for artifact_id, source in sources(await bindings(transfer.id)):
        by_artifact.setdefault(artifact_id, set()).add(source)
    assert all(len(found) == 1 for found in by_artifact.values())
    assert not any(len({c.source_identity.key for c in artifact.candidates}) > 1
                   for artifact in await repository.artifacts(transfer.id))


# -- 14.5: no issued material, no guessed proof -----------------------------------

async def test_a_failed_refresh_proves_nothing_and_fabricates_no_convergence(tmp_path, monkeypatch):
    body = b"x" * SIZE
    engine, repository, provider, executor = await lab(tmp_path, monkeypatch, {"one": body, "two": body})
    provider.refresh_error = TransferError(NormalizedError(Domain.PROVIDER, Category.PROVIDER_UNAVAILABLE,
                                                           Stage.CANDIDATE_PREPARATION, Retryability.BACKOFF))
    transfer = await submit(engine, "one@mirror-a.test", "two@mirror-b.test")

    await drive(engine)

    assert provider.refreshed
    assert not any(len({c.source_identity.key for c in artifact.candidates}) > 1
                   for artifact in await repository.artifacts(transfer.id))
    assert len({source for _artifact, source in sources(await bindings(transfer.id))}) <= 1


async def test_evidence_reasons_for_unread_transient_material():
    """Structural absence stays ``sampler_unsupported``; a refresh that failed
    is a retryable ``sampler_unavailable``; neither is a proof."""
    from transfers.mirrors import EvidenceContext, shared_evidence

    registry = IntegrationRegistry()
    registry.register_executor(ByteSampler(lambda *_: True, {"one": b"x" * SIZE}))
    durable = TransferCandidate("payload.bin", (Endpoint("memory", "", transient=True),), SIZE, provider_id="relay-lab",
                                source_identity=SourceIdentity("host", "a.test"))
    peer = replace(durable, id="peer", source_identity=SourceIdentity("host", "b.test"))

    class Auth:
        def __init__(self, outcome):
            self.outcome = outcome

        async def material(self, candidate):
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

    assert (await shared_evidence(durable, peer, registry)).reason == "sampler_unsupported"
    unbound = EvidenceContext()
    assert (await shared_evidence(durable, peer, registry, unbound)).reason == "sampler_unsupported"
    nothing = EvidenceContext()
    nothing.bind(Auth(None))
    assert (await shared_evidence(durable, peer, registry, nothing)).reason == "sampler_unsupported"
    failed = EvidenceContext()
    failed.bind(Auth(RuntimeError("refresh failed")))
    failed_evidence = await shared_evidence(durable, peer, registry, failed)
    assert failed_evidence.reason == "sampler_unavailable" and not failed_evidence.proves_individual


# -- 14.6: issued material is never durable ---------------------------------------

async def test_issued_transient_addresses_never_reach_durable_state(tmp_path, monkeypatch):
    body = b"x" * SIZE
    engine, repository, provider, executor = await lab(tmp_path, monkeypatch, {"one": body, "two": body})
    await submit(engine, "one@mirror-a.test", "two@mirror-b.test")

    await drive(engine)

    assert executor.sampled
    assert not [value for value in await durable_text() if "secret-" in value]


async def test_transient_material_never_turns_a_sample_into_an_operator_question():
    from transfers.input_required import auth_required, username_password
    from transfers.mirrors import EvidenceContext

    class Asking(ByteSampler):
        async def fingerprint(self, subject):
            return auth_required(username_password())

    class Auth:
        def owns(self, candidate):
            return True

    executor = Asking(lambda *_: True, {})
    context = EvidenceContext()
    context.bind(Auth())
    issued = TransferCandidate("payload.bin", (Endpoint("memory", "memory:one?token=secret-9", transient=True),), SIZE,
                               provider_id="relay-lab",
                               accepted_input_methods=(username_password().method,))

    assert await context.fingerprint(executor, issued) is None
    assert context.requirement_for((issued,)) is None
