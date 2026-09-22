"""Lifecycle regression for bounded reported-size artifact consolidation."""
from dataclasses import replace
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.engine import TransferEngine
from transfers.models import (
    ArtifactFingerprint, ExecutionState, IntegrityMetadata, ResolutionResult, ResourceState, SourceIdentity,
    TransferProgress, TransferRequest, TransferState,
)
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository
from transfers.size_evidence import reported_sizes_compatible


REPORT_A = 3_597_035_110
REPORT_B = 3_595_501_360
REPORT_C = 3_596_250_000
ACTUAL = 3_595_501_360


class NearSizeProvider(ParcelProvider):
    def __init__(self, identity, reported_size):
        super().__init__(identity)
        self.reported_size = reported_size

    def candidate(self, name="payload.bin", *, payload="parcel"):
        return replace(
            super().candidate(name, payload="shared-content"),
            expected_bytes=self.reported_size,
            source_identity=SourceIdentity("test-provider", self.descriptor.id),
        )

    async def resolve(self, request):
        self.calls.append(("resolve", request.payload))
        return ResolutionResult(
            ResourceState.AVAILABLE,
            (self.candidate(request.name or "payload.bin", payload=str(request.payload)),),
        )


@pytest_asyncio.fixture
async def near_size_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    providers = (
        NearSizeProvider("provider-a", REPORT_A),
        NearSizeProvider("provider-b", REPORT_B),
        NearSizeProvider("provider-c", REPORT_C),
    )
    executor = MemoryExecutor(repository.authorize_execution)

    async def fingerprint(subject):
        candidate = subject.candidate
        return ArtifactFingerprint(ACTUAL, "bounded-shared-content")

    monkeypatch.setattr(executor, "fingerprint", fingerprint)
    for provider in providers:
        registry.register_provider(provider)
    registry.register_executor(executor)
    engine = TransferEngine(
        repository,
        registry,
        download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(
            retry_delay=1,
            adoption_stability_seconds=0,
            max_active_executions=32,
            resolution_concurrency=32,
        ),
        clock=lambda: 1000.0,
    )
    await engine.initialize()
    return SimpleNamespace(
        engine=engine,
        repository=repository,
        registry=registry,
        providers=providers,
        executor=executor,
        root=tmp_path / "payloads",
    )


async def submit_one(ctx, provider, payload):
    return await ctx.engine.submit(
        (TransferRequest(
            "parcel",
            payload,
            name="GF200826-TMNTSFS-RN.rar",
            preferred_provider=provider.descriptor.id,
        ),),
        name="GF200826-TMNTSFS-RN.rar",
        deduplicate=False,
    )


@pytest.mark.asyncio
async def test_three_near_size_sibling_requests_consolidate_to_one_artifact_and_writer(near_size_engine):
    ctx = near_size_engine
    requests = tuple(
        TransferRequest(
            "parcel",
            f"submission-{index}",
            name="GF200826-TMNTSFS-RN.rar",
            preferred_provider=provider.descriptor.id,
        )
        for index, provider in enumerate(ctx.providers, start=1)
    )
    transfer = await ctx.engine.submit(requests, name="near-size cohort", deduplicate=False)

    # Resolve until all sibling requests have left the resolution/materialization
    # barrier, then allow normal execution dispatch.
    for _ in range(4):
        await ctx.engine.resolve_pending()
    await ctx.engine.reconcile_executions()

    artifacts = await ctx.repository.artifacts(transfer.id)
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert len(artifact.candidates) == 3
    assert {candidate.provider_id for candidate in artifact.candidates} == {
        "provider-a", "provider-b", "provider-c",
    }
    assert reported_sizes_compatible(REPORT_A, REPORT_B)
    assert reported_sizes_compatible(REPORT_A, REPORT_C)
    assert len([call for call in ctx.executor.calls if call[0] == "start"]) == 1
    assert "(2)" not in artifact.target

    async with database.get_db() as db:
        physical = await db.fetchall(
            """SELECT id,local_path,mirror_group_id,mirror_state,status
                FROM download_files WHERE torrent_id=? ORDER BY id""",
            (transfer.id,),
        )
    primaries = [row for row in physical if row["mirror_state"] != "standby"]
    standbys = [row for row in physical if row["mirror_state"] == "standby"]
    assert len(primaries) == 1
    assert primaries[0]["id"] == artifact.id
    assert len(standbys) == 2
    assert {row["mirror_group_id"] for row in standbys} == {artifact.id}
    assert {row["local_path"] for row in physical} == {artifact.target}
    assert {row["status"] for row in standbys} == {"duplicate"}


@pytest.mark.asyncio
async def test_later_near_size_transfer_attaches_to_existing_canonical_writer(near_size_engine):
    ctx = near_size_engine
    first = await submit_one(ctx, ctx.providers[0], "submission-a")
    await ctx.engine.tick()
    primary = (await ctx.repository.artifacts(first.id))[0]
    original_target = primary.target
    original_execution = primary.execution

    second = await submit_one(ctx, ctx.providers[1], "submission-b")
    await ctx.engine.tick()

    canonical = (await ctx.repository.artifacts(first.id))[0]
    assert canonical.id == primary.id
    assert canonical.target == original_target
    assert canonical.execution == original_execution
    assert [candidate.provider_id for candidate in canonical.candidates] == ["provider-a", "provider-b"]
    assert await ctx.repository.artifacts(second.id) == ()
    assert len([call for call in ctx.executor.calls if call[0] == "start"]) == 1


# --- Transfer 291: bounded provider/executor size drift at materialization ---
#
# Production: the provider reported 11,038,065,950 bytes, aria2 completed with
# (and the stable local file is exactly) 11,035,235,262 -- ~0.0256 % apart,
# well inside the reported-size tolerance -- yet completion trusted the
# provider's number as absolute truth and failed the artifact with
# ``materialization_failed``. The same relative drift, scaled down:
SCALED_REPORTED = 1_000_000
SCALED_OBSERVED = 999_744
SCALED_OUTSIDE = 990_000  # materially outside the 0.1 % tolerance of SCALED_REPORTED.


class ScaledProvider(NearSizeProvider):
    def __init__(self, identity, reported_size, integrity=()):
        super().__init__(identity, reported_size)
        self.integrity = integrity

    def candidate(self, name="payload.bin", *, payload="parcel"):
        return replace(super().candidate(name, payload=payload), integrity=self.integrity)


async def _start_request(ctx, identity, *, reported, integrity=()):
    """Admit one request and let the engine start its execution. Returns the
    transfer id and the running artifact."""
    provider = ScaledProvider(identity, reported, integrity)
    ctx.registry.register_provider(provider)
    transfer = await ctx.engine.submit(
        (TransferRequest("parcel", identity, name="scaled.rar", preferred_provider=provider.descriptor.id),),
        name="scaled.rar", deduplicate=False,
    )
    await ctx.engine.tick()
    return transfer.id, (await ctx.repository.artifacts(transfer.id))[0]


async def _succeed(ctx, artifact, *, observed, actual):
    """The executor ends SUCCEEDED with total ``observed`` while the stable
    local file holds ``actual`` bytes."""
    target = Path(artifact.target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x" * actual)
    job = ctx.executor.jobs[artifact.execution.attempt_id]
    ctx.executor.jobs[artifact.execution.attempt_id] = replace(
        job, state=ExecutionState.SUCCEEDED, progress=TransferProgress(observed, observed),
    )
    await ctx.engine.tick()


async def _succeed_with_payload(ctx, identity, *, reported, observed, actual, integrity=()):
    """One request whose executor ends SUCCEEDED with total ``observed`` while
    the stable local file holds ``actual`` bytes and the provider reported
    ``reported``. Returns (transfer id, artifact id)."""
    transfer_id, artifact = await _start_request(ctx, identity, reported=reported, integrity=integrity)
    await _succeed(ctx, artifact, observed=observed, actual=actual)
    return transfer_id, artifact.id


async def _artifact_row(artifact_id):
    async with database.get_db() as db:
        return await db.fetchone("SELECT status,size_bytes FROM download_files WHERE id=?", (artifact_id,))


@pytest.mark.asyncio
async def test_bounded_provider_executor_size_drift_completes_at_the_verified_material_size(near_size_engine):
    ctx = near_size_engine
    assert reported_sizes_compatible(SCALED_REPORTED, SCALED_OBSERVED)
    transfer_id, artifact_id = await _succeed_with_payload(
        ctx, "drift-ok", reported=SCALED_REPORTED, observed=SCALED_OBSERVED, actual=SCALED_OBSERVED,
    )

    transfer = await ctx.repository.get(transfer_id)
    assert transfer.state == TransferState.COMPLETED
    assert transfer.error is None  # no materialization_failed outcome.
    row = await _artifact_row(artifact_id)
    assert row["status"] == "completed"
    assert int(row["size_bytes"]) == SCALED_OBSERVED  # accepted MATERIAL size, not the provider report.
    artifact = (await ctx.repository.artifacts(transfer_id))[0]
    assert artifact.expected_bytes == SCALED_OBSERVED
    # The provider's original report stays in the durable candidate history.
    assert artifact.candidates[artifact.selected].expected_bytes == SCALED_REPORTED


@pytest.mark.asyncio
async def test_bounded_drift_is_decided_by_the_payload_not_by_preferring_either_source(near_size_engine):
    """The local payload equals the provider's report while the executor
    total drifted: the payload proves that size, so it -- not the executor
    total -- becomes the accepted material size."""
    ctx = near_size_engine
    transfer_id, artifact_id = await _succeed_with_payload(
        ctx, "drift-reported", reported=SCALED_REPORTED, observed=SCALED_OBSERVED, actual=SCALED_REPORTED,
    )
    assert (await ctx.repository.get(transfer_id)).state == TransferState.COMPLETED
    assert int((await _artifact_row(artifact_id))["size_bytes"]) == SCALED_REPORTED


@pytest.mark.asyncio
@pytest.mark.parametrize("actual", [SCALED_OUTSIDE, SCALED_REPORTED])
async def test_out_of_tolerance_size_conflict_is_never_silently_accepted(near_size_engine, actual):
    """Incompatible provider/executor sizes are not silently resolved -- not
    even when the local file happens to equal one of the two numbers."""
    ctx = near_size_engine
    assert not reported_sizes_compatible(SCALED_REPORTED, SCALED_OUTSIDE)
    transfer_id, artifact_id = await _succeed_with_payload(
        ctx, f"drift-outside-{actual}", reported=SCALED_REPORTED, observed=SCALED_OUTSIDE, actual=actual,
    )
    assert (await ctx.repository.get(transfer_id)).state != TransferState.COMPLETED
    assert (await _artifact_row(artifact_id))["status"] != "completed"


@pytest.mark.asyncio
async def test_bounded_drift_never_overrides_an_integrity_mismatch_at_materialization(near_size_engine):
    ctx = near_size_engine
    wrong = (IntegrityMetadata("sha256", "0" * 64),)
    transfer_id, artifact_id = await _succeed_with_payload(
        ctx, "drift-bad-integrity", reported=SCALED_REPORTED, observed=SCALED_OBSERVED,
        actual=SCALED_OBSERVED, integrity=wrong,
    )
    assert (await ctx.repository.get(transfer_id)).state != TransferState.COMPLETED
    assert (await _artifact_row(artifact_id))["status"] != "completed"

    right = (IntegrityMetadata("sha256", hashlib.sha256(b"x" * SCALED_OBSERVED).hexdigest()),)
    transfer_id, artifact_id = await _succeed_with_payload(
        ctx, "drift-good-integrity", reported=SCALED_REPORTED, observed=SCALED_OBSERVED,
        actual=SCALED_OBSERVED, integrity=right,
    )
    assert (await ctx.repository.get(transfer_id)).state == TransferState.COMPLETED
    assert int((await _artifact_row(artifact_id))["size_bytes"]) == SCALED_OBSERVED


# --- Candidate size unknown + recorded bookkeeping + executor final total ---
#
# MemoryExecutor.start() reports an early positive total of 4 bytes, which the
# repository records as the artifact's size when the candidate itself reported
# none (``accept_execution_total``). That recorded size is bookkeeping learned
# from executor progress -- never a provider report -- so a different final
# total must complete through the SAME canonical material verification as every
# other case (there is no separate completion-size layer above it).

@pytest.mark.asyncio
async def test_unknown_provider_size_completes_at_the_executors_final_total_through_the_canonical_owner(near_size_engine):
    ctx = near_size_engine
    transfer_id, artifact = await _start_request(ctx, "unknown-final", reported=0)
    assert artifact.candidates[artifact.selected].expected_bytes == 0  # no provider report at all...
    assert artifact.expected_bytes == 4  # ...only bookkeeping learned from early executor progress.

    await _succeed(ctx, artifact, observed=SCALED_OBSERVED, actual=SCALED_OBSERVED)

    assert (await ctx.repository.get(transfer_id)).state == TransferState.COMPLETED
    row = await _artifact_row(artifact.id)
    assert row["status"] == "completed"
    assert int(row["size_bytes"]) == SCALED_OBSERVED  # the verified final total, not the stale bookkeeping.


@pytest.mark.asyncio
async def test_stale_bookkeeping_size_never_stands_in_for_a_final_total(near_size_engine):
    ctx = near_size_engine
    transfer_id, artifact = await _start_request(ctx, "stale-bookkeeping", reported=0)
    assert artifact.expected_bytes == 4
    await _succeed(ctx, artifact, observed=SCALED_OBSERVED, actual=4)  # the payload matches only the old bookkeeping.
    assert (await ctx.repository.get(transfer_id)).state != TransferState.COMPLETED
    assert (await _artifact_row(artifact.id))["status"] != "completed"


@pytest.mark.asyncio
async def test_executor_zero_total_never_creates_a_zero_byte_success(near_size_engine):
    ctx = near_size_engine
    transfer_id, artifact = await _start_request(ctx, "zero-final", reported=0)
    assert artifact.expected_bytes == 4
    await _succeed(ctx, artifact, observed=0, actual=0)  # executor total unknown + an empty file.
    assert (await ctx.repository.get(transfer_id)).state != TransferState.COMPLETED
    assert (await _artifact_row(artifact.id))["status"] != "completed"


@pytest.mark.asyncio
async def test_unknown_provider_size_final_total_still_requires_integrity(near_size_engine):
    ctx = near_size_engine
    wrong = (IntegrityMetadata("sha256", "0" * 64),)
    transfer_id, artifact = await _start_request(ctx, "unknown-final-integrity", reported=0, integrity=wrong)
    await _succeed(ctx, artifact, observed=SCALED_OBSERVED, actual=SCALED_OBSERVED)
    assert (await ctx.repository.get(transfer_id)).state != TransferState.COMPLETED
    assert (await _artifact_row(artifact.id))["status"] != "completed"
