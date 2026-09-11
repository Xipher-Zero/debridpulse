"""DP 1.0.12 corrective task, Case 5: later proof succeeds.

Attempt 1: same logical key, independent sources, compatible reports,
sampling cannot establish trustworthy identity (a short/ambiguous transport
response -> ``incomplete_representation``). A later bounded attempt produces
trustworthy matching evidence. Required outcome: one canonical artifact, the
alternate acquisition candidate retained, the incoming request's disposition
becomes ``recovered`` -- no second authoritative canonical artifact is ever
materialized in between.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.engine import TransferEngine
from transfers.models import ArtifactFingerprint, FingerprintKind, ResolutionResult, ResourceState, TransferRequest
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


class BatchProvider(ParcelProvider):
    async def resolve(self, request):
        self.calls.append(("resolve", request.payload))
        name = request.name or "payload.bin"
        return ResolutionResult(ResourceState.AVAILABLE, (self.candidate(name, payload=f"payload:{name}"),))


@pytest_asyncio.fixture
async def pair(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    first = BatchProvider("provider-a")
    second = BatchProvider("provider-b")
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(first)
    registry.register_provider(second)
    registry.register_executor(executor)
    now = [1000.0]
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0,
                              max_active_executions=32, resolution_concurrency=32),
        clock=lambda: now[0],
    )
    await engine.initialize()
    return SimpleNamespace(engine=engine, repository=repository, a=first, b=second, executor=executor, now=now)


async def _row(transfer_id):
    async with database.get_db() as db:
        return await db.fetchone(
            """SELECT state,equivalence_reason,equivalence_disposition,equivalence_retry_count
                FROM transfer_requests WHERE transfer_id=? AND NOT EXISTS(
                    SELECT 1 FROM transfer_requests c WHERE c.parent_id=transfer_requests.id)""",
            (transfer_id,),
        )


@pytest.mark.asyncio
async def test_ambiguous_first_attempt_recovers_on_later_trustworthy_proof(pair, monkeypatch):
    first = await pair.engine.submit(
        (TransferRequest("parcel", "a1", name="same.bin", preferred_provider=pair.a.descriptor.id),),
        name="a", deduplicate=False,
    )
    await pair.engine.tick()
    assert len(await pair.repository.artifacts(first.id)) == 1

    attempts = {"count": 0}

    async def flaky(candidate):
        if candidate.provider_id == pair.b.descriptor.id:
            attempts["count"] += 1
            if attempts["count"] == 1:
                return ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "incomplete_representation", "")
        signature = f"full:{candidate.name.casefold()}"
        return ArtifactFingerprint(candidate.expected_bytes, signature, FingerprintKind.FULL_CONTENT_SAMPLE, "", signature)

    monkeypatch.setattr(pair.executor, "fingerprint", flaky)
    second = await pair.engine.submit(
        (TransferRequest("parcel", "b1", name="same.bin", preferred_provider=pair.b.descriptor.id),),
        name="b", deduplicate=False,
    )
    await pair.engine.resolve_pending()

    # First (ambiguous) attempt must not materialize a second canonical.
    assert len(await pair.repository.artifacts(second.id)) == 0
    pending = await _row(second.id)
    assert pending["state"] == "materializing"
    assert pending["equivalence_reason"] == "incomplete_representation"
    assert pending["equivalence_disposition"] == "pending"

    pair.now[0] += 1.1
    await pair.engine.resolve_pending()

    assert (await pair.repository.get(second.id)).state.value == "consolidated"
    assert len(await pair.repository.artifacts(second.id)) == 0
    canonicals = await pair.repository.artifacts(first.id)
    assert len(canonicals) == 1
    assert len(canonicals[0].candidates) == 2

    recovered = await _row(second.id)
    assert recovered["equivalence_disposition"] == "recovered"
    assert attempts["count"] == 2
