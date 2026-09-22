"""Recovery, settings and archive behavior exercised at canonical boundaries."""
import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import zipfile

import pytest

from test_universal_lifecycle import canonical_core, core, submit  # noqa: F401 -- pytest fixture re-export
from test_aria2_executor_contract import execution  # noqa: F401 -- pytest fixture re-export
from application.service import ApplicationService
from core.config import AppSettings
from executors.aria2.executor import Aria2Executor
from integrations.catalog import definitions
from integrations.configuration import normalize_settings
from services.maintenance_gate import ApplicationMaintenanceGate
from transfers.errors import Category
from transfers.models import (
    ExecutionObservation, ExecutionState, TransferProgress, ResolutionResult, ResourceState, TransferState,
    SourceEntry, TransferRequest,
)


@pytest.mark.asyncio
async def test_stall_recovery_confirms_cancellation_then_waits_for_retry_budget(canonical_core):
    # DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
    # closure): stall recovery calls _recover_artifact with a candidate-
    # bearing, non-REMOTE_SOURCE error, which is now exclusively a
    # canonical-stack decision.
    core = canonical_core
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
async def test_executor_binding_refuses_a_changed_download_root(execution):
    other_root = execution.executor.configuration.local_root + "-other"
    changed = Aria2Executor(execution.daemon, replace(execution.executor.configuration, local_root=other_root), execution.executor.authorize)
    result = await changed.observe(execution.handle)
    assert result.state == ExecutionState.UNKNOWN
    assert result.error.category == Category.EXECUTOR_UNAVAILABLE
    assert execution.daemon.lookups == 0
    assert execution.daemon.calls == []


@pytest.mark.asyncio
async def test_download_folder_change_with_live_references_is_rejected_before_save(core):
    application = ApplicationService(core.engine)
    application.definitions = definitions
    application.repository.has_integration_references = AsyncMock(return_value=True)
    before = normalize_settings(AppSettings(download_folder="/download"), definitions)
    after = normalize_settings(before.model_copy(update={"download_folder": "/elsewhere"}), definitions, previous=before)
    with pytest.raises(ValueError, match="before changing the download folder"):
        await application.validate_configuration(before, after)
    application.repository.has_integration_references.assert_awaited_once_with()


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
    # The executor observation must be truthful about the payload it "downloaded":
    # ``finish()`` reports a fixed 4-byte total, which is an incompatible size
    # conflict against this real zip's length (and is no longer silently
    # resolved in favour of the provider's report).
    job = core.executor.jobs[artifact.execution.attempt_id]
    core.executor.jobs[artifact.execution.attempt_id] = replace(
        job, state=ExecutionState.SUCCEEDED, progress=TransferProgress(len(payload), len(payload)),
    )
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


@pytest.mark.asyncio
async def test_successful_execution_establishes_size_when_provider_size_is_unknown(core):
    """A candidate whose own size was unknown at resolution time still
    completes correctly once the executor reports a genuinely known
    positive final total -- SIZE_KNOWN(N>0), consumed via
    ``transfers.filesystem.known_positive_size``."""
    size = 4
    candidate = replace(core.provider.candidate(), expected_bytes=0)
    core.provider.responses = [ResolutionResult(ResourceState.AVAILABLE, (candidate,))]
    transfer = await submit(core)
    await core.engine.tick()
    artifact = (await core.repository.artifacts(transfer.id))[0]
    target = Path(artifact.target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x" * size)
    job = core.executor.jobs[artifact.execution.attempt_id]
    core.executor.jobs[artifact.execution.attempt_id] = replace(job, state=ExecutionState.SUCCEEDED, progress=TransferProgress(size, size))
    await core.engine.tick()
    assert (await core.repository.get(transfer.id)).state == TransferState.COMPLETED
    assert (await core.repository.artifacts(transfer.id))[0].expected_bytes == size


@pytest.mark.asyncio
async def test_unknown_size_zero_byte_success_never_completes(core):
    """DP 1.0.12 canonical lifecycle/recovery/completion rework, Section 5:
    when NEITHER the candidate's own expected size NOR the executor's final
    total is positive, size is SIZE_UNKNOWN -- a SUCCEEDED observation must
    not silently collapse into an affirmative zero-byte completion (a
    zero-byte target file existing is not evidence either; it is exactly the
    absence-of-size-knowledge signature transfer 265 proved core must not
    trust). The artifact must instead route through ordinary verification-
    failure/recovery, never ``completed`` and never a durable zero-byte
    delivered artifact."""
    candidate = replace(core.provider.candidate(), expected_bytes=0)
    core.provider.responses = [ResolutionResult(ResourceState.AVAILABLE, (candidate,))]

    # MemoryExecutor.start() hardcodes a positive TransferProgress(4, 1, 1)
    # regardless of candidate size, which would otherwise "reveal" a known
    # positive size through the ordinary early-progress
    # repository.execution()/accept_execution_total() path before this test
    # ever reaches SUCCEEDED. Replace it with a start() that reports the
    # SAME unknown (zero) total the real degenerate executor path reported
    # in production, so the artifact's own expected size genuinely stays
    # unknown throughout -- matching transfer 265's shape exactly.
    async def unknown_size_start(request, handle):
        assert await core.executor.authorize(handle, "start")
        core.executor.calls.append(("start", handle))
        result = ExecutionObservation(handle, ExecutionState.TRANSFERRING, TransferProgress(0, 0, 0), (request.target,), None)
        core.executor.jobs[handle.attempt_id] = result
        return result
    core.executor.start = unknown_size_start

    transfer = await submit(core)
    await core.engine.tick()
    artifact = (await core.repository.artifacts(transfer.id))[0]
    assert artifact.expected_bytes == 0
    target = Path(artifact.target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"")
    job = core.executor.jobs[artifact.execution.attempt_id]
    core.executor.jobs[artifact.execution.attempt_id] = replace(job, state=ExecutionState.SUCCEEDED, progress=TransferProgress(0, 0))
    await core.engine.tick()
    settled = (await core.repository.artifacts(transfer.id))[0]
    assert settled.state != "completed"
    assert settled.expected_bytes == 0
    assert (await core.repository.get(transfer.id)).state != TransferState.COMPLETED


@pytest.mark.asyncio
async def test_payload_disappearing_after_member_verification_blocks_final_completion(core, monkeypatch):
    # DP 1.0.12 leveling remediation (ARCH-001) + Transfer 291 correction: the
    # true owner of the stable_payload() call exercised here is
    # transfers.filesystem -- completion verification is the one canonical
    # ``stable_material_size`` operation there (never proxied through another
    # module now that the transitional cross-module monkeypatch seam is gone).
    import transfers.filesystem as module
    result = core.provider.parcel(state=ResourceState.AVAILABLE)
    resource = result.observation.resource
    core.provider.members[resource.id] = tuple(SourceEntry(f"{name}.bin", 4, f"{name}.bin", TransferRequest("parcel-member", name)) for name in ("first", "second"))
    core.provider.responses = [result]
    transfer = await submit(core)
    await core.engine.tick()
    await core.engine.tick()
    first, second = await core.repository.artifacts(transfer.id)
    core.executor.finish(first.execution)
    core.executor.finish(second.execution)
    original = module.stable_payload
    async def verify(path, *args, **kwargs):
        valid = await original(path, *args, **kwargs)
        if path == second.target:
            Path(first.target).unlink(missing_ok=True)
        return valid
    monkeypatch.setattr(module, "stable_payload", verify)
    await core.engine.tick()
    assert (await core.repository.get(transfer.id)).state != TransferState.COMPLETED
    latest = {item.id: item for item in await core.repository.artifacts(transfer.id)}
    assert latest[first.id].state == "queued"
    assert latest[first.id].target == first.target
    assert latest[second.id].state == "completed"


@pytest.mark.asyncio
async def test_extraction_notification_flag_is_independent_of_download_notifications(core, monkeypatch):
    from application.observability import Observability
    from postprocessors.archive.processor import ArchivePostProcessor
    core.engine.postprocessors = (ArchivePostProcessor(),)
    transfer = await submit(core, name="invalid.zip")
    await core.engine.tick()
    core.executor.finish((await core.repository.artifacts(transfer.id))[0].execution)
    await core.engine.tick()
    notifier = SimpleNamespace(send_extract_failed=AsyncMock(), send_complete=AsyncMock())
    monkeypatch.setattr("application.observability.NotificationService", lambda: SimpleNamespace(client=lambda: notifier))
    monkeypatch.setattr("application.observability.get_settings", lambda: SimpleNamespace(discord_notify_added=False, discord_notify_finished=False, discord_notify_error=False, discord_notify_extract=True))
    await Observability(core.repository).deliver()
    notifier.send_extract_failed.assert_awaited_once()
    notifier.send_complete.assert_not_awaited()
