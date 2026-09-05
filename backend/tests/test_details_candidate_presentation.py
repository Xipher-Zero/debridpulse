"""Focused Details candidate read-model qualification."""
from dataclasses import replace

import pytest
import pytest_asyncio

import db.database as database
from api.serializers import public_payload
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Recovery, Retryability, Stage
from transfers.models import ExecutionState, IntegrityMetadata, ResolutionResult, ResourceState, SourceIdentity, TransferRequest
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


_SHA256 = "a4c3ed04a95a3da14a9d235c83d868bed7c0f45cf7f3faa751ee8f50598d2211"


class HostParcelProvider(ParcelProvider):
    def candidate_for(self, request):
        candidate = super().candidate(request.name or "same.bin", payload="shared")
        host = "rapidgator.net" if request.payload == "rapidgator" else "1fichier.com"
        return replace(
            candidate,
            integrity=(IntegrityMetadata("sha256", _SHA256),),
            source_identity=SourceIdentity("host", host),
        )

    async def resolve(self, request):
        self.calls.append(("resolve", request.payload))
        return ResolutionResult(ResourceState.AVAILABLE, (self.candidate_for(request),))


@pytest_asyncio.fixture
async def details_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = HostParcelProvider("provider-a")
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    engine = TransferEngine(
        repository,
        registry,
        download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0, max_active_executions=8, resolution_concurrency=8),
    )
    await engine.initialize()
    return engine, repository, provider, executor


async def submit(engine, payload):
    return await engine.submit(
        (TransferRequest("parcel", payload, name="same.bin", preferred_provider="provider-a"),),
        name=payload,
        deduplicate=False,
    )


def file_projection(details):
    assert len(details["files"]) == 1
    return details["files"][0]


@pytest.mark.asyncio
async def test_single_candidate_exposes_count_without_multiplicity_records(details_runtime):
    engine, repository, _provider, _executor = details_runtime
    canonical = await submit(engine, "rapidgator")
    await engine.resolve_pending()

    file_row = file_projection(await repository.presentation(canonical.id, details=True))
    assert file_row["candidate_count"] == 1
    assert "acquisition_candidates" not in file_row


@pytest.mark.asyncio
async def test_two_candidates_project_durable_source_provider_and_relationship(details_runtime):
    engine, repository, _provider, _executor = details_runtime
    canonical = await submit(engine, "rapidgator")
    await engine.resolve_pending()
    consolidated = await submit(engine, "1fichier")
    await engine.resolve_pending()

    details = await repository.presentation(canonical.id, details=True)
    file_row = file_projection(details)
    assert file_row["candidate_count"] == 2
    candidates = file_row["acquisition_candidates"]
    assert [item["relationship"] for item in candidates] == ["Original", "Consolidated"]
    assert [item["provider_id"] for item in candidates] == ["provider-a", "provider-a"]
    assert [item["source_label"] for item in candidates] == ["rapidgator.net", "1fichier.com"]
    assert len({item["candidate_id"] for item in candidates}) == 2
    assert (await repository.get(consolidated.id)).state.value == "consolidated"


@pytest.mark.asyncio
async def test_selected_failed_and_delivering_candidate_come_from_execution_provenance(details_runtime):
    engine, repository, _provider, executor = details_runtime
    canonical = await submit(engine, "rapidgator")
    await engine.resolve_pending()
    await submit(engine, "1fichier")
    await engine.resolve_pending()

    await engine.tick()
    artifact = (await repository.artifacts(canonical.id))[0]
    first_id = artifact.candidates[artifact.selected].id
    failure = NormalizedError(
        Domain.NETWORK,
        Category.REMOTE_READ_FAILED,
        Stage.EXECUTION,
        Retryability.BACKOFF,
        Recovery.TRY_ALTERNATE_CANDIDATE,
    )
    executor.jobs[artifact.execution.attempt_id] = replace(
        executor.jobs[artifact.execution.attempt_id], state=ExecutionState.FAILED, error=failure,
    )
    await engine.tick()
    await engine.tick()

    current = (await repository.artifacts(canonical.id))[0]
    second_id = current.candidates[current.selected].id
    assert second_id != first_id
    projected = {item["candidate_id"]: item for item in file_projection(
        await repository.presentation(canonical.id, details=True)
    )["acquisition_candidates"]}
    assert projected[first_id]["dispositions"] == ["Failed"]
    assert projected[second_id]["is_selected"] is True
    assert projected[second_id]["dispositions"] in (["Active"], ["Selected"])

    executor.finish(current.execution)
    await engine.tick()
    delivered = {item["candidate_id"]: item for item in file_projection(
        await repository.presentation(canonical.id, details=True)
    )["acquisition_candidates"]}
    assert delivered[second_id]["is_delivering"] is True
    assert delivered[second_id]["dispositions"] == ["Delivering"]


@pytest.mark.asyncio
async def test_public_projection_hides_raw_bindings_and_capabilities(details_runtime):
    engine, repository, _provider, _executor = details_runtime
    canonical = await submit(engine, "rapidgator")
    await engine.resolve_pending()
    await submit(engine, "1fichier")
    await engine.resolve_pending()

    internal = await repository.presentation(canonical.id, details=True)
    assert internal.get("candidate_bindings")
    public = public_payload(internal)
    assert "candidate_bindings" not in public
    file_row = file_projection(public)
    assert file_row["candidate_count"] == 2
    for candidate in file_row["acquisition_candidates"]:
        assert set(candidate) == {
            "candidate_id", "source_label", "provider_id", "relationship",
            "dispositions", "is_selected", "is_delivering",
        }
        serialized = repr(candidate).lower()
        assert "http://" not in serialized
        assert "https://" not in serialized
        assert "headers" not in serialized
        assert "context" not in serialized
        assert "refresh" not in serialized
        assert "endpoint" not in serialized
