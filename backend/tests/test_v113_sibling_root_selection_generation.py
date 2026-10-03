"""A sibling root's selection generation never stales another root's work.

Materialization admission (``TransferRepository.materialization_authorization``)
retires a child artifact as STALE once its root re-resolved onto a newer
provider resource -- a newer selection generation of THAT root. In a transfer
with several independently submitted roots, each root owns its own generation
(``transfer_file_selections`` is keyed on the root request and its binding):
another root resolving later, or still preparing, is not a re-resolution of
this one and must not make its already-authorized work stale.

Neutral fixtures only (``file_selection_support``): no concrete integration.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

import db.database as database
from db.database import get_db
from file_selection_support import executable, rebind_resource, seed_window
from transfers import codec
from transfers.models import (
    Artifact, MaterializationAdmissionKind, Ownership, ProviderResource, RequestRecord, TransferRequest,
)
from transfers.repository import TransferRepository

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "sibling-generation.db")
    await database.init_db()
    return TransferRepository()


def _artifact(transfer_id, request_id):
    return Artifact(id=1, transfer_id=transfer_id, request_id=request_id, name="payload.bin",
                    target="/tmp/payload.bin", expected_bytes=4, state="queued", candidates=())


async def _child(transfer_id, parent_id):
    async with get_db() as db:
        row = await db.fetchone("SELECT id FROM transfer_requests WHERE transfer_id=? AND parent_id=? "
                                "ORDER BY ordinal LIMIT 1", (transfer_id, parent_id))
    return row["id"]


async def _sibling_root(seed, *, ordinal: int, key: str) -> RequestRecord:
    """A second, independently submitted root of the SAME transfer, bound to
    its own provider resource."""
    request = TransferRequest("parcel", f"box-{key}", name=f"payload-{key}")
    resource = ProviderResource(seed.provider_id, {"box_ticket": key}, Ownership.CREATED,
                                id=f"{seed.provider_id}:{key}")
    request_id = f"req-sibling-{key}"
    async with get_db() as db:
        await db.execute("INSERT INTO transfer_requests(id,transfer_id,ordinal,payload,state,resource) "
                         "VALUES(?,?,?,?,'waiting',?)",
                         (request_id, seed.transfer_id, ordinal, codec.dump(request), codec.dump(resource)))
        await db.execute("INSERT INTO provider_resources(id,transfer_id,provider_id,payload,state) "
                         "VALUES(?,?,?,?,'preparing')",
                         (resource.id, seed.transfer_id, seed.provider_id, codec.dump(resource)))
        await db.commit()
    return RequestRecord(request_id, seed.transfer_id, request, "waiting", None, resource, 0, 0.0, None, None)


async def _authorized_child(repo, seed, *, now=1000.0):
    await repo.begin_file_selection_window(seed.request_id, seed.transfer_id, seed.provider_resource_id,
                                           seed.provider_id, initially_available=True, now=now)
    generation = await repo.commit_selected_manifest(seed.record, executable(("a", "a", 1)), now=now)
    await repo.manifest(seed.record, generation, selection_id=generation.selection_id)
    return generation, await _child(seed.transfer_id, seed.request_id)


async def test_a_preparing_sibling_root_never_stales_an_authorized_child(repo):
    seed = await seed_window(transfer_hash="e" * 40)
    generation, child_id = await _authorized_child(repo, seed)
    sibling = await _sibling_root(seed, ordinal=1, key="preparing")
    # The sibling's own generation opens later and stays uncommitted (its
    # provider resource is still preparing).
    await repo.begin_file_selection_window(sibling.id, seed.transfer_id, sibling.resource.id, seed.provider_id,
                                           initially_available=False, now=2000.0)

    admission = await repo.materialization_authorization(_artifact(seed.transfer_id, child_id))

    assert admission.kind == MaterializationAdmissionKind.PROCEED
    assert admission.authority_generation == generation.selection_id


async def test_a_later_committed_sibling_root_never_stales_an_authorized_child(repo):
    seed = await seed_window(transfer_hash="f" * 40)
    generation, child_id = await _authorized_child(repo, seed)
    sibling = await _sibling_root(seed, ordinal=1, key="later")
    await repo.begin_file_selection_window(sibling.id, seed.transfer_id, sibling.resource.id, seed.provider_id,
                                           initially_available=True, now=2000.0)
    later = await repo.commit_selected_manifest(sibling, executable(("b", "b", 1)), now=2000.0)
    await repo.manifest(sibling, later, selection_id=later.selection_id)

    first = await repo.materialization_authorization(_artifact(seed.transfer_id, child_id))
    second = await repo.materialization_authorization(
        _artifact(seed.transfer_id, await _child(seed.transfer_id, sibling.id)))

    assert (first.kind, first.authority_generation) == (MaterializationAdmissionKind.PROCEED, generation.selection_id)
    assert (second.kind, second.authority_generation) == (MaterializationAdmissionKind.PROCEED, later.selection_id)


async def test_the_same_root_re_resolving_still_stales_its_own_children(repo):
    """Genuine invalidation control: a newer generation of the SAME root is
    still authoritative over that root's earlier children, even with a
    sibling root present."""
    seed = await seed_window(transfer_hash="0" * 40)
    _generation, child_id = await _authorized_child(repo, seed)
    await _sibling_root(seed, ordinal=1, key="bystander")
    rebound = await rebind_resource(seed, suffix="regen")
    await repo.begin_file_selection_window(rebound.request_id, rebound.transfer_id, rebound.provider_resource_id,
                                           rebound.provider_id, initially_available=True, now=3000.0)
    newer = await repo.commit_selected_manifest(rebound.record, executable(("a", "a", 1)), now=3000.0)

    admission = await repo.materialization_authorization(_artifact(seed.transfer_id, child_id))

    assert admission.kind == MaterializationAdmissionKind.STALE
    assert admission.authority_generation == newer.selection_id


# -- §10A re-characterization: the engine end to end ----------------------------------

async def _engine(tmp_path, monkeypatch):
    from fake_integrations import MemoryExecutor, ParcelProvider
    from transfers.convergence_engine import TransferEngine
    from transfers.policy import TransferPolicy
    from transfers.recovery_repository import TransferRepository as RecoveryRepository
    from transfers.registry import IntegrationRegistry

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "sibling-engine.db")
    await database.init_db()
    repository = RecoveryRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider("parcel-lab", file_manifest=True)
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"),
                            policy=TransferPolicy(retry_delay=0, adoption_stability_seconds=0,
                                                  max_active_executions=8, resolution_concurrency=8),
                            clock=lambda: 1000.0)
    await engine.initialize()
    return engine, repository, provider, executor


async def test_a_usable_root_keeps_its_writer_while_a_sibling_root_stays_preparing(tmp_path, monkeypatch):
    """The transfer-488 shape: root A's resource is available and its single
    file starts writing; root B's resource stays PREPARING across many passes
    (its generation opens and is never committed). A's writer is never
    retired, its member is resolved once, and B keeps being observed."""
    from transfers.models import ResourceState

    engine, repository, provider, executor = await _engine(tmp_path, monkeypatch)
    provider.responses.append(provider.parcel("ready", state=ResourceState.AVAILABLE,
                                              files=(("ready.bin", "ready.bin", 4),)))
    provider.responses.append(provider.parcel("slow", state=ResourceState.PREPARING,
                                              files=(("slow.bin", "slow.bin", 4),)))
    transfer = await engine.submit(
        (TransferRequest("parcel", "ready", name="ready", selection_mode="interactive"),
         TransferRequest("parcel", "slow", name="slow", selection_mode="interactive")),
        name="two-roots", deduplicate=False)

    for _ in range(8):
        await engine.tick()

    starts = [call for call in executor.calls if call[0] == "start"]
    cancels = [call for call in executor.calls if call[0] == "cancel"]
    member_resolves = [call for call in provider.calls if call == ("resolve", "ready:ready.bin")]
    artifacts = await repository.artifacts(transfer.id)
    async with get_db() as db:
        generations = await db.fetchall("SELECT request_id, manifest_committed_at FROM transfer_file_selections "
                                        "WHERE transfer_id=?", (transfer.id,))
    assert len(generations) == 2 and any(row["manifest_committed_at"] is None for row in generations)
    assert len(starts) == 1 and cancels == []
    assert len(member_resolves) == 1
    assert [artifact.name for artifact in artifacts] == ["ready.bin"]
    assert artifacts[0].execution is not None
    assert ("observe", "parcel-lab:slow") in provider.calls


async def test_transient_execution_material_still_offers_no_sampling(tmp_path, monkeypatch):
    """§10A sampler characterization: endpoint transience alone still yields
    ``sampler_unsupported`` -- the A correction does not touch evidence."""
    from fake_integrations import MemoryExecutor
    from transfers.mirrors import shared_evidence
    from transfers.models import Endpoint, TransferCandidate
    from transfers.registry import IntegrationRegistry

    registry = IntegrationRegistry()
    registry.register_executor(MemoryExecutor(lambda *_: True))
    left = TransferCandidate("x.bin", (Endpoint("memory", "", transient=True),), 4, provider_id="lab")
    right = TransferCandidate("x.bin", (Endpoint("memory", "", transient=True),), 4, provider_id="lab")

    evidence = await shared_evidence(left, right, registry)

    assert evidence.reason == "sampler_unsupported"
