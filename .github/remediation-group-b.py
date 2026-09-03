"""Temporary STATE-001 remediation applicator. Removed by successful runner."""
from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one match, found {count}")
    target.write_text(text.replace(old, new, 1))


# Cancellation obtains durable lifecycle authority before any remote wait.
replace_once(
    "backend/transfers/engine.py",
    '''    async def cancel(self, transfer_id: int):\n        transfer = await self.repository.get(transfer_id)\n        if transfer is None:\n            raise KeyError(transfer_id)\n        challenge = await self.challenges.current(transfer_id)\n        if challenge:\n            await self.inputs.clear(challenge.id)\n            await self.challenges.clear_transfer(transfer_id)\n        for artifact in await self.repository.artifacts(transfer_id):\n            if artifact.execution:\n                executor = self.registry.executors.get(artifact.execution.executor_id)\n                if executor:\n                    outcome = await executor.cancel(artifact.execution)\n                    await self.repository.outcome(transfer_id, outcome, attempt_id=artifact.execution.attempt_id)\n            if artifact.state != "completed":\n                await self.repository.artifact_state(artifact.id, "cancelled")\n        await self.repository.state(transfer_id, TransferState.CANCELLED)\n        return True\n''',
    '''    async def cancel(self, transfer_id: int):\n        lock = self._transfer_locks.setdefault(transfer_id, asyncio.Lock())\n        async with lock:\n            transfer = await self.repository.get(transfer_id)\n            if transfer is None:\n                raise KeyError(transfer_id)\n            if transfer.state == TransferState.CANCELLED:\n                return ()\n\n            # The durable lifecycle decision wins before executor I/O. The epoch\n            # closes the delete/retry race and state() supplies the canonical\n            # compare-and-transition boundary used by the rest of the engine.\n            if not await self.repository.state(\n                transfer_id, TransferState.CANCELLED, expected_epoch=transfer.epoch,\n            ):\n                current = await self.repository.get(transfer_id)\n                if current and current.state == TransferState.CANCELLED:\n                    return ()\n                raise TransferError(self._error(\n                    Category.RESOURCE_STATE_CONFLICT, Stage.EXECUTION, domain=Domain.LIFECYCLE,\n                    retryability=Retryability.NEVER, recovery=Recovery.REQUIRE_OPERATOR,\n                ))\n\n            challenge = await self.challenges.current(transfer_id)\n            if challenge:\n                await self.inputs.clear(challenge.id)\n                await self.challenges.clear_transfer(transfer_id)\n\n            cleanup_errors = []\n            for artifact in await self.repository.artifacts(transfer_id):\n                if artifact.execution:\n                    executor = self.registry.executors.get(artifact.execution.executor_id)\n                    if executor is None:\n                        error = self._error(\n                            Category.UNSUPPORTED_CAPABILITY, Stage.EXECUTION, domain=Domain.REQUEST,\n                            retryability=Retryability.NEVER,\n                        )\n                        outcome = TransferOutcome(OutcomeKind.FAILURE, error)\n                    else:\n                        try:\n                            outcome = await executor.cancel(artifact.execution)\n                            if not isinstance(outcome, TransferOutcome):\n                                raise TransferError(self._error(\n                                    Category.INVALID_ADAPTER_RESPONSE, Stage.EXECUTION, domain=Domain.EXECUTOR,\n                                    retryability=Retryability.NEVER,\n                                ))\n                        except Exception as exc:\n                            error = exc.error if isinstance(exc, TransferError) else unknown_failure(\n                                exc, integration_id=executor.descriptor.id, domain=Domain.EXECUTOR, stage=Stage.EXECUTION,\n                            )\n                            outcome = TransferOutcome(OutcomeKind.FAILURE, error)\n                    await self.repository.outcome(transfer_id, outcome, attempt_id=artifact.execution.attempt_id)\n                    if outcome.kind in {OutcomeKind.SUCCESS, OutcomeKind.CANCELLED}:\n                        attempt = next((item for item in await self.repository.executions(transfer_id)\n                                        if item.handle == artifact.execution), None)\n                        progress = attempt.progress if attempt else None\n                        await self.repository.execution(ExecutionObservation(\n                            artifact.execution, ExecutionState.CANCELLED, progress or TransferProgress(),\n                        ))\n                    elif outcome.error:\n                        cleanup_errors.append(outcome.error)\n                if artifact.state != "completed":\n                    await self.repository.artifact_state(artifact.id, "cancelled")\n            return tuple(cleanup_errors)\n''',
)
# TransferProgress is now needed when canonicalizing confirmed executor cancellation.
replace_once(
    "backend/transfers/engine.py",
    '''    TransferCandidate, TransferState, new_identity,\n''',
    '''    TransferCandidate, TransferProgress, TransferState, new_identity,\n''',
)

# API reports logical cancellation separately from remote cleanup success.
replace_once(
    "backend/application/service.py",
    '''    async def cancel(self, transfer_id):\n        async with self.application_operation():\n            await self.require(transfer_id)\n            await self.engine.cancel(transfer_id)\n            await self._publish(transfer_id)\n            return {"ok": True}\n''',
    '''    async def cancel(self, transfer_id):\n        async with self.application_operation():\n            await self.require(transfer_id)\n            errors = await self.engine.cancel(transfer_id)\n            await self._publish(transfer_id)\n            return {\n                "ok": not errors,\n                "cancelled": True,\n                "cleanup_errors": [error.as_dict() for error in errors],\n            }\n''',
)

# Once cancellation is durable, no admission/materialization path may revive it.
replace_once(
    "backend/transfers/repository.py",
    "AND t.status NOT IN ('deleted','completed') AND COALESCE(p.paused,0)=0",
    "AND t.status NOT IN ('deleted','completed','cancelled') AND COALESCE(p.paused,0)=0",
)
replace_once(
    "backend/transfers/repository.py",
    '''            if row["status"] != "deleted":\n                await db.execute("UPDATE transfer_requests SET state=?,resource=COALESCE(?,resource),error=? WHERE id=?",\n                                 (request_state, resource, error, attempt.request_id))\n            await db.commit()\n        return row["status"] != "deleted"\n''',
    '''            if row["status"] not in {"deleted", "completed", "cancelled"}:\n                await db.execute("UPDATE transfer_requests SET state=?,resource=COALESCE(?,resource),error=? WHERE id=?",\n                                 (request_state, resource, error, attempt.request_id))\n            await db.commit()\n        return row["status"] not in {"deleted", "completed", "cancelled"}\n''',
)
replace_once(
    "backend/transfers/repository.py",
    '''                AND transfer_id IN (SELECT id FROM torrents WHERE status!='deleted')""",\n''',
    '''                AND transfer_id IN (SELECT id FROM torrents WHERE status NOT IN ('deleted','completed','cancelled'))""",\n''',
)
replace_once(
    "backend/transfers/repository.py",
    '''            if not parent or parent["status"] == "deleted":\n                return\n''',
    '''            if not parent or parent["status"] in {"deleted", "completed", "cancelled"}:\n                return\n''',
)
replace_once(
    "backend/transfers/repository.py",
    '''            if not parent or parent["status"] == "deleted":\n                return None\n''',
    '''            if not parent or parent["status"] in {"deleted", "completed", "cancelled"}:\n                return None\n''',
)
replace_once(
    "backend/transfers/repository.py",
    '''        if action in {"start", "resume"} and (row["transfer_status"] in {"deleted", "completed"} or row["paused_intent"]):\n''',
    '''        if action in {"start", "resume"} and (row["transfer_status"] in {"deleted", "completed", "cancelled"} or row["paused_intent"]):\n''',
)
replace_once(
    "backend/transfers/repository.py",
    '''                WHERE execution_attempt_id=? AND torrent_id IN (SELECT id FROM torrents WHERE status!='deleted')""",\n''',
    '''                WHERE execution_attempt_id=? AND torrent_id IN (SELECT id FROM torrents WHERE status NOT IN ('deleted','cancelled'))""",\n''',
)
replace_once(
    "backend/transfers/repository.py",
    '''            current = await db.fetchone("SELECT execution_attempt_id FROM download_files WHERE id=?", (artifact_id,))\n            cursor = await db.execute("""UPDATE download_files SET status=?,normalized_error=?,retry_at=?,\n                execution_attempt_id=CASE WHEN ? THEN NULL ELSE execution_attempt_id END,\n                selected_candidate=COALESCE(?,selected_candidate),size_bytes=COALESCE(?,size_bytes),updated_at=CURRENT_TIMESTAMP\n                WHERE id=? AND torrent_id IN (SELECT id FROM torrents WHERE status!='deleted')""",\n                (state, codec.dump(error) if error else None, retry_at, release, selected, expected_bytes, artifact_id))\n''',
    '''            current = await db.fetchone("SELECT execution_attempt_id FROM download_files WHERE id=?", (artifact_id,))\n            cursor = await db.execute("""UPDATE download_files SET status=?,normalized_error=?,retry_at=?,\n                execution_attempt_id=CASE WHEN ? THEN NULL ELSE execution_attempt_id END,\n                selected_candidate=COALESCE(?,selected_candidate),size_bytes=COALESCE(?,size_bytes),updated_at=CURRENT_TIMESTAMP\n                WHERE id=? AND torrent_id IN (SELECT id FROM torrents\n                    WHERE status!='deleted' AND (status!='cancelled' OR ?='cancelled'))""",\n                (state, codec.dump(error) if error else None, retry_at, release, selected, expected_bytes, artifact_id, state))\n''',
)
replace_once(
    "backend/transfers/repository.py",
    '''                WHERE t.status NOT IN ('deleted','completed') AND e.authorized=1""")\n''',
    '''                WHERE t.status NOT IN ('deleted','completed','cancelled') AND e.authorized=1""")\n''',
)

Path("backend/tests/test_audit_remediation_state.py").write_text(r'''"""Deterministic STATE-001 cancellation/completion race contracts."""
import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Recovery, Retryability, Stage, TransferError
from transfers.models import (
    ExecutionObservation, ExecutionState, OutcomeKind, TransferOutcome,
    TransferProgress, TransferRequest, TransferState,
)
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


@pytest_asyncio.fixture
async def core(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state-audit.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider()
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(retry_delay=0, resolution_retry_delay=0, adoption_stability_seconds=0),
        clock=lambda: 1000.0,
    )
    await engine.initialize()
    return SimpleNamespace(engine=engine, repository=repository, registry=registry, provider=provider, executor=executor)


async def executing(core):
    transfer = await core.engine.submit((TransferRequest("parcel", "box", name="payload.bin"),), deduplicate=False)
    await core.engine.tick()
    artifact = (await core.repository.artifacts(transfer.id))[0]
    assert artifact.execution is not None
    return transfer, artifact


@pytest.mark.asyncio
async def test_cancellation_is_durable_before_blocked_executor_cancel_and_late_completion_cannot_win(core):
    transfer, artifact = await executing(core)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_cancel(handle):
        assert await core.repository.authorize_execution(handle, "cancel")
        entered.set()
        await release.wait()
        return TransferOutcome(OutcomeKind.CANCELLED)

    core.executor.cancel = blocked_cancel
    task = asyncio.create_task(core.engine.cancel(transfer.id))
    await entered.wait()
    assert (await core.repository.get(transfer.id)).state == TransferState.CANCELLED

    # A reconciliation observation can still be retained as remote history, but
    # it cannot rewrite the cancelled artifact or parent into completion.
    await core.repository.execution(ExecutionObservation(
        artifact.execution, ExecutionState.SUCCEEDED, TransferProgress(4, 4),
    ))
    await core.repository.artifact_state(artifact.id, "completed", expected_bytes=4)
    assert (await core.repository.get(transfer.id)).state == TransferState.CANCELLED
    assert (await core.repository.artifacts(transfer.id))[0].state != "completed"

    release.set()
    assert await task == ()
    assert (await core.repository.artifacts(transfer.id))[0].state == "cancelled"


@pytest.mark.asyncio
async def test_completion_that_wins_before_cancel_authority_is_truthful_conflict(core):
    transfer, artifact = await executing(core)
    core.executor.finish(artifact.execution)
    await core.engine.reconcile_executions()
    assert (await core.repository.get(transfer.id)).state == TransferState.COMPLETED
    before = len([call for call in core.executor.calls if call[0] == "cancel"])
    with pytest.raises(TransferError) as captured:
        await core.engine.cancel(transfer.id)
    assert captured.value.error.category == Category.RESOURCE_STATE_CONFLICT
    assert (await core.repository.get(transfer.id)).state == TransferState.COMPLETED
    assert len([call for call in core.executor.calls if call[0] == "cancel"]) == before


@pytest.mark.asyncio
async def test_executor_cancel_failure_preserves_logical_cancel_and_reports_cleanup_error(core):
    transfer, artifact = await executing(core)
    error = NormalizedError(
        Domain.EXECUTOR, Category.REMOTE_CLEANUP_FAILED, Stage.EXECUTION,
        retryability=Retryability.NEVER, recovery=Recovery.REQUIRE_OPERATOR,
    )

    async def failed_cancel(_handle):
        return TransferOutcome(OutcomeKind.FAILURE, error)

    core.executor.cancel = failed_cancel
    errors = await core.engine.cancel(transfer.id)
    assert errors == (error,)
    assert (await core.repository.get(transfer.id)).state == TransferState.CANCELLED
    assert (await core.repository.artifacts(transfer.id))[0].state == "cancelled"
    attempt = (await core.repository.executions(transfer.id))[0]
    assert attempt.state != ExecutionState.CANCELLED


@pytest.mark.asyncio
async def test_executor_cancel_timeout_preserves_logical_cancel_and_reports_error(core):
    transfer, _artifact = await executing(core)

    async def timed_out(_handle):
        raise asyncio.TimeoutError("executor cancel timed out")

    core.executor.cancel = timed_out
    errors = await core.engine.cancel(transfer.id)
    assert len(errors) == 1
    assert (await core.repository.get(transfer.id)).state == TransferState.CANCELLED


@pytest.mark.asyncio
async def test_repeated_cancel_is_idempotent_and_does_not_repeat_remote_cancel(core):
    transfer, _artifact = await executing(core)
    assert await core.engine.cancel(transfer.id) == ()
    first = len([call for call in core.executor.calls if call[0] == "cancel"])
    assert await core.engine.cancel(transfer.id) == ()
    assert len([call for call in core.executor.calls if call[0] == "cancel"]) == first


@pytest.mark.asyncio
async def test_restart_after_durable_cancel_does_not_reconcile_cancelled_execution(core):
    transfer, _artifact = await executing(core)
    await core.engine.cancel(transfer.id)
    before = len([call for call in core.executor.calls if call[0] == "observe"])
    restarted = TransferEngine(
        TransferRepository(), core.registry, download_root=core.engine.root,
        policy=core.engine.policy, clock=lambda: 1000.0,
    )
    await restarted.initialize()
    await restarted.reconcile_executions()
    assert (await core.repository.get(transfer.id)).state == TransferState.CANCELLED
    assert len([call for call in core.executor.calls if call[0] == "observe"]) == before


@pytest.mark.asyncio
async def test_cancelled_parent_rejects_new_resolution_materialization_and_start(core):
    transfer = await core.engine.submit((TransferRequest("parcel", "box", name="payload.bin"),), deduplicate=False)
    record = (await core.repository.requests(transfer.id))[0]
    assert await core.repository.state(transfer.id, TransferState.CANCELLED)
    assert await core.repository.begin_resolution(record.id, core.provider.descriptor.id) is None
    assert await core.repository.materialize(record, (core.provider.candidate("payload.bin"),), "payload.bin") is None
    assert not await core.repository.live_executions()
''')
