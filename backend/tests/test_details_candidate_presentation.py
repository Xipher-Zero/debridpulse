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
        policy=TransferPolicy(retry_delay=0, adoption_stability_seconds=0, max_active_executions=8, resolution_concurrency=8),
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

    failure = NormalizedError(
        Domain.NETWORK,
        Category.REMOTE_READ_FAILED,
        Stage.EXECUTION,
        Retryability.BACKOFF,
        Recovery.TRY_ALTERNATE_CANDIDATE,
    )
    await engine.tick()
    artifact = (await repository.artifacts(canonical.id))[0]
    first_id = artifact.candidates[artifact.selected].id

    executor.jobs[artifact.execution.attempt_id] = replace(
        executor.jobs[artifact.execution.attempt_id], state=ExecutionState.FAILED, error=failure,
    )
    await engine.tick()
    await engine.tick()
    retry = (await repository.artifacts(canonical.id))[0]
    assert retry.selected == 0 and retry.execution is not None

    executor.jobs[retry.execution.attempt_id] = replace(
        executor.jobs[retry.execution.attempt_id], state=ExecutionState.FAILED, error=failure,
    )
    await engine.tick()
    assert (await repository.artifacts(canonical.id))[0].state == "refresh_pending"
    await engine.tick()
    await engine.tick()
    refreshed = (await repository.artifacts(canonical.id))[0]
    assert refreshed.selected == 0 and refreshed.execution is not None

    executor.jobs[refreshed.execution.attempt_id] = replace(
        executor.jobs[refreshed.execution.attempt_id], state=ExecutionState.FAILED, error=failure,
    )
    await engine.tick()
    switched = (await repository.artifacts(canonical.id))[0]
    assert switched.selected == 1 and switched.execution is None
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
async def test_group_source_candidates_expose_canonical_host_without_touching_per_file_ui(details_runtime):
    engine, repository, _provider, _executor = details_runtime
    canonical = await submit(engine, "rapidgator")
    await engine.resolve_pending()
    await submit(engine, "1fichier")
    await engine.resolve_pending()

    file_row = file_projection(await repository.presentation(canonical.id, details=True))
    # The ungated per-file group projection carries a normalized canonical host,
    # the artifact-specific candidate id, and the existing per-file switch flags.
    entries = file_row["source_candidates"]
    assert {entry["source_host"] for entry in entries} == {"rapidgator.net", "1fichier.com"}
    for entry in entries:
        assert set(entry) == {"source_host", "candidate_id", "is_selected", "switch_eligible"}
        serialized = repr(entry).lower()
        assert "http://" not in serialized and "https://" not in serialized
    selected = [entry for entry in entries if entry["is_selected"]]
    assert len(selected) == 1 and selected[0]["switch_eligible"] is False
    other = [entry for entry in entries if not entry["is_selected"]]
    assert other and all(entry["switch_eligible"] for entry in other)
    # The individual candidate disclosure contract is unchanged.
    assert file_row["candidate_count"] == 2
    assert [item["source_label"] for item in file_row["acquisition_candidates"]] == [
        "rapidgator.net", "1fichier.com",
    ]


@pytest.mark.asyncio
async def test_generic_acquisition_candidates_carry_switch_eligible_matching_source_candidates(details_runtime):
    """DP 1.0.12 Contextual Candidate Action Scope task, §6.1/§10.1: generic
    (non-host-scoped-only) acquisition_candidates carry the same backend-owned
    switch_eligible fact source_candidates already does -- false for the
    selected candidate, true for a switchable-state non-selected one -- so
    ui-detail-candidates.js can render per-candidate switch actions without a
    JS lifecycle whitelist."""
    engine, repository, _provider, _executor = details_runtime
    canonical = await submit(engine, "rapidgator")
    await engine.resolve_pending()
    await submit(engine, "1fichier")
    await engine.resolve_pending()

    file_row = file_projection(await repository.presentation(canonical.id, details=True))
    acquisition = {item["candidate_id"]: item for item in file_row["acquisition_candidates"]}
    source = {item["candidate_id"]: item for item in file_row["source_candidates"]}
    assert set(acquisition) == set(source)
    for candidate_id, entry in acquisition.items():
        assert entry["switch_eligible"] == source[candidate_id]["switch_eligible"]
    selected = [item for item in acquisition.values() if item["is_selected"]]
    assert len(selected) == 1 and selected[0]["switch_eligible"] is False
    other = [item for item in acquisition.values() if not item["is_selected"]]
    assert other and all(item["switch_eligible"] for item in other)


@pytest.mark.asyncio
async def test_single_candidate_artifact_carries_group_projection_but_no_multiplicity_records(details_runtime):
    engine, repository, _provider, _executor = details_runtime
    canonical = await submit(engine, "rapidgator")
    await engine.resolve_pending()

    file_row = file_projection(await repository.presentation(canonical.id, details=True))
    assert file_row["candidate_count"] == 1
    assert "acquisition_candidates" not in file_row
    # A lone unconsolidated source has no canonical alternate bindings, so the
    # group projection is present but empty — it can never seed a group host.
    assert file_row["source_candidates"] == []


@pytest.mark.asyncio
async def test_group_projection_present_even_when_every_candidate_is_switch_ineligible(details_runtime):
    """Membership data must be ungated: a current file whose every candidate
    currently has switch_eligible == False must still expose ALL of its
    canonical host candidates in ``source_candidates`` — never fewer, and
    never none, just because none of them is currently actionable."""
    engine, repository, _provider, _executor = details_runtime
    canonical = await submit(engine, "rapidgator")
    await engine.resolve_pending()
    await submit(engine, "1fichier")
    await engine.resolve_pending()

    artifact = (await repository.artifacts(canonical.id))[0]
    # Force the artifact into a non-switchable state without touching its
    # candidate bindings — a real "completed while still consolidated" shape.
    async with database.get_db() as db:
        await db.execute("UPDATE download_files SET status=? WHERE id=?", ("completed", artifact.id))
        await db.commit()

    file_row = file_projection(await repository.presentation(canonical.id, details=True))
    entries = file_row["source_candidates"]
    assert {entry["source_host"] for entry in entries} == {"rapidgator.net", "1fichier.com"}
    assert len(entries) == 2
    # Every candidate is currently switch-ineligible (the selected one because
    # it is selected, the other because the artifact state forbids switching)
    # yet both remain present — membership never depends on this flag.
    assert all(entry["switch_eligible"] is False for entry in entries)


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
        # DP 1.0.12 Contextual Candidate Action Scope task (§6.1/§10.1):
        # generic acquisition-candidate presentation now also carries
        # ``switch_eligible`` (the same backend-owned _SWITCHABLE_ARTIFACT_STATES
        # rule the host-scoped ``source_candidates`` projection already uses),
        # so the frontend can render per-candidate switch actions without
        # recreating lifecycle policy in JS. Deliberate, documented shape
        # change; no other field/behavior here is affected.
        assert set(candidate) == {
            "candidate_id", "source_label", "provider_id", "relationship",
            "dispositions", "is_selected", "is_delivering", "switch_eligible",
        }
        serialized = repr(candidate).lower()
        assert "http://" not in serialized
        assert "https://" not in serialized
        assert "headers" not in serialized
        assert "context" not in serialized
        assert "refresh" not in serialized
        assert "endpoint" not in serialized
