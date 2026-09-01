"""Recovery, settings and archive behavior exercised at canonical boundaries."""
import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import zipfile

import pytest

from test_universal_lifecycle import core, submit
from test_aria2_executor_contract import execution
from application.service import ApplicationService
from core.config import AppSettings
from executors.aria2.executor import Aria2Executor
from integrations.catalog import definitions
from integrations.configuration import normalize_settings
from services.maintenance_gate import ApplicationMaintenanceGate
from transfers.errors import Category
from transfers.models import ExecutionState, TransferProgress, ResolutionResult, ResourceState, TransferState


@pytest.mark.asyncio
async def test_stall_recovery_confirms_cancellation_then_waits_for_retry_budget(core):
    core.engine.policy = replace(core.engine.policy, stalled_after_seconds=10)
    transfer = await submit(core)
    await core.engine.tick()
    artifact = (await core.repository.artifacts(transfer.id))[0]
    original = artifact.execution
    core.now[0] += 11
    await core.engine.tick()
    latest = (await core.repository.artifacts(transfer.id))[0]
    assert latest.error.category == Category.TRANSFER_STALLED
    assert latest.execution is None
    assert core.executor.jobs[original.attempt_id].state == ExecutionState.CANCELLED
    assert len(core.executor.jobs) == 1
    core.now[0] += 1
    await core.engine.tick()
    assert len(core.executor.jobs) == 2


@pytest.mark.asyncio
async def test_unknown_observation_never_authorizes_stall_cancellation(core):
    core.engine.policy = replace(core.engine.policy, stalled_after_seconds=10)
    transfer = await submit(core)
    await core.engine.tick()
    artifact = (await core.repository.artifacts(transfer.id))[0]
    core.executor.jobs[artifact.execution.attempt_id] = replace(core.executor.jobs[artifact.execution.attempt_id], state=ExecutionState.UNKNOWN)
    core.now[0] += 100
    await core.engine.tick()
    assert not any(operation == "cancel" for operation, _ in core.executor.calls)
    assert len(core.executor.jobs) == 1


@pytest.mark.asyncio
async def test_progress_resets_stall_clock_but_repeated_observations_do_not(core):
    core.engine.policy = replace(core.engine.policy, stalled_after_seconds=10)
    transfer = await submit(core)
    await core.engine.tick()
    artifact = (await core.repository.artifacts(transfer.id))[0]
    core.now[0] += 9
    core.executor.jobs[artifact.execution.attempt_id] = replace(core.executor.jobs[artifact.execution.attempt_id], progress=TransferProgress(4, 2))
    await core.engine.tick()
    core.now[0] += 9
    await core.engine.tick()
    assert not any(operation == "cancel" for operation, _ in core.executor.calls)
    core.now[0] += 2
    await core.engine.tick()
    assert any(operation == "cancel" for operation, _ in core.executor.calls)


@pytest.mark.asyncio
async def test_executor_binding_refuses_a_changed_daemon_or_path_mapping(execution):
    changed = Aria2Executor(execution.daemon, replace(execution.executor.configuration, remote_root="/different"), execution.executor.authorize)
    result = await changed.observe(execution.handle)
    assert result.state == ExecutionState.UNKNOWN
    assert result.error.category == Category.EXECUTOR_UNAVAILABLE
    assert execution.daemon.lookups == 0
    assert execution.daemon.calls == []


@pytest.mark.asyncio
async def test_connection_change_with_live_references_is_rejected_before_save(core):
    application = ApplicationService(core.engine)
    application.definitions = definitions
    application.repository.has_integration_references = AsyncMock(return_value=True)
    before = normalize_settings(AppSettings(aria2_mode="external"), definitions)
    after = normalize_settings(before.model_copy(update={"aria2_url": "http://other:6800/jsonrpc"}), definitions, previous=before, supplied_fields={"aria2_url"})
    with pytest.raises(ValueError, match="existing aria2 resources"):
        await application.validate_configuration(before, after)
    application.repository.has_integration_references.assert_awaited_once_with("aria2")


@pytest.mark.asyncio
async def test_configuration_gate_upgrades_own_admission_and_drains_other_work():
    gate = ApplicationMaintenanceGate()
    entered, release = asyncio.Event(), asyncio.Event()
    async def existing_work():
        async with gate.operation():
            entered.set()
            await release.wait()
    task = asyncio.create_task(existing_work())
    await entered.wait()
    async def configure():
        async with gate.operation():
            async with gate.maintenance():
                assert task.done()
    configuring = asyncio.create_task(configure())
    await asyncio.sleep(0)
    assert not configuring.done()
    release.set()
    await asyncio.wait_for(asyncio.gather(task, configuring), 1)
    async with gate.operation():
        assert not gate.active


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_archive", [False, True])
async def test_real_zip_postprocessing_preserves_transfer_success_and_retention(core, tmp_path, monkeypatch, delete_archive):
    from postprocessors.archive.processor import ArchivePostProcessor
    archive = tmp_path / "fixture.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("verified.txt", b"archive payload")
    payload = archive.read_bytes()
    candidate = replace(core.provider.candidate("fixture.zip"), expected_bytes=len(payload))
    core.provider.responses = [ResolutionResult(ResourceState.AVAILABLE, (candidate,))]
    core.engine.postprocessors = (ArchivePostProcessor(),)
    monkeypatch.setattr("postprocessors.archive.processor.get_settings", lambda: SimpleNamespace(extract_max_concurrent=1, extract_delete_archive=delete_archive))
    transfer = await submit(core, name="fixture.zip")
    await core.engine.tick()
    artifact = (await core.repository.artifacts(transfer.id))[0]
    target = Path(artifact.target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    core.executor.finish(artifact.execution, materialize=False)
    await core.engine.tick()
    view = await core.repository.presentation(transfer.id, details=True)
    assert view["status"] == "completed"
    assert view["extraction_status"] == "completed"
    assert target.exists() is not delete_archive
    extracted = list(target.parent.rglob("verified.txt"))
    assert len(extracted) == 1 and extracted[0].read_bytes() == b"archive payload"


@pytest.mark.asyncio
async def test_no_archive_skips_postprocessing_without_false_extraction_success(core):
    from postprocessors.archive.processor import ArchivePostProcessor
    core.engine.postprocessors = (ArchivePostProcessor(),)
    transfer = await submit(core)
    await core.engine.tick()
    artifact = (await core.repository.artifacts(transfer.id))[0]
    core.executor.finish(artifact.execution)
    await core.engine.tick()
    view = await core.repository.presentation(transfer.id)
    assert view["status"] == "completed"
    assert view["extraction_status"] == "skipped"


@pytest.mark.asyncio
async def test_invalid_archive_retains_payload_and_reports_postprocessing_failure(core):
    from postprocessors.archive.processor import ArchivePostProcessor
    core.engine.postprocessors = (ArchivePostProcessor(),)
    transfer = await submit(core, name="invalid.zip")
    await core.engine.tick()
    artifact = (await core.repository.artifacts(transfer.id))[0]
    core.executor.finish(artifact.execution)
    await core.engine.tick()
    view = await core.repository.presentation(transfer.id)
    assert view["status"] == TransferState.COMPLETED
    assert view["extraction_status"] == "error"
    assert Path(artifact.target).read_bytes() == b"done"
