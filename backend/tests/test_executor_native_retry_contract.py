"""Native-assisted retry: core decides, the executor may reuse native state.

A retry is always a new durable DP attempt. The recovery claim that decided
the retry owns the native handoff; the previous attempt loses normal control
authority before the new attempt may bind the reused native identity.
"""
from __future__ import annotations

import pytest

import db.database as database
from executor_fakes import LedgerExecutor, artifact_of, ledger_capabilities, ledger_core, submit_ledger
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage
from transfers.models import ExecutionState

pytestmark = pytest.mark.asyncio


def _retrying(**flags):
    def build(authorize):
        executor = LedgerExecutor(authorize, capabilities=ledger_capabilities(native_assisted_retry=True))
        for key, value in flags.items():
            setattr(executor, key, value)
        return (executor,)
    return build


async def _failed_once(core, *, retryability=Retryability.BACKOFF):
    transfer = await submit_ledger(core)
    first = (await artifact_of(core, transfer.id)).execution
    core.executor.run(first)
    core.executor.fail(first, NormalizedError(Domain.NETWORK, Category.REMOTE_READ_FAILED, Stage.EXECUTION,
                                              retryability=retryability, integration_id="ledger-copy"))
    for _ in range(3):
        core.now[0] += 5
        await core.engine.reconcile_executions()
    return transfer, first


async def _authorized_rows():
    async with database.get_db() as db:
        return await db.fetchall("SELECT id,handle FROM execution_attempts WHERE authorized=1")


async def test_core_decides_retry_before_executor_native_retry_is_invoked(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch, executors=_retrying())
    await _failed_once(core, retryability=Retryability.NEVER)
    assert core.executor.retries == []  # policy refused: the executor is never asked
    (tmp_path / "second").mkdir()
    other = await ledger_core(tmp_path / "second", monkeypatch, executors=_retrying())
    transfer, first = await _failed_once(other)
    assert other.executor.retries and other.executor.retries[0][0] == first.attempt_id
    context = await other.repository.recovery_context((await artifact_of(other, transfer.id)).id)
    assert context.get("execution_attempts", 0) >= 1


async def test_native_assisted_retry_creates_new_dp_attempt(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch, executors=_retrying())
    transfer, first = await _failed_once(core)
    current = (await artifact_of(core, transfer.id)).execution
    assert current.attempt_id != first.attempt_id
    attempts = await core.repository.executions(transfer.id)
    assert {item.handle.attempt_id for item in attempts} == {first.attempt_id, current.attempt_id}
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT ordinal FROM execution_attempt_provenance ORDER BY ordinal")
    assert [row["ordinal"] for row in rows] == [1, 2]


async def test_previous_attempt_loses_mutation_authority(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch, executors=_retrying())
    transfer, first = await _failed_once(core)
    current = (await artifact_of(core, transfer.id)).execution
    for action in ("observe", "pause", "resume", "start", "cancel"):
        assert not await core.repository.authorize_execution(first, action)
    assert await core.repository.authorize_execution(current, "observe")


async def test_native_retry_may_reuse_native_identity_without_reusing_dp_attempt_identity(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch, executors=_retrying())
    transfer, first = await _failed_once(core)
    current = (await artifact_of(core, transfer.id)).execution
    assert current.native == first.native and current.attempt_id != first.attempt_id
    assert len(core.executor.jobs) == 1


async def test_executor_without_native_retry_uses_normal_new_start_path(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer, first = await _failed_once(core)
    current = (await artifact_of(core, transfer.id)).execution
    assert core.executor.retries == []
    assert current.attempt_id != first.attempt_id and current.native != first.native
    assert [call[0] for call in core.executor.calls].count("start") == 2


async def test_native_retry_never_has_two_normal_authorized_dp_owners(tmp_path, monkeypatch):
    witnessed = []

    class Witness(LedgerExecutor):
        async def retry_from(self, request, prepared, previous):
            witnessed.append((await self.authorize(previous, "observe"), await self.authorize(prepared, "start")))
            return await super().retry_from(request, prepared, previous)

    core = await ledger_core(tmp_path, monkeypatch, executors=lambda authorize: (
        Witness(authorize, capabilities=ledger_capabilities(native_assisted_retry=True)),))
    transfer, first = await _failed_once(core)
    assert witnessed == [(False, True)]  # the old owner was fenced before the handoff ran
    rows = await _authorized_rows()
    natives = [__import__("json").loads(row["handle"])["native"] for row in rows]
    assert natives.count(first.native) == 1


async def test_native_retry_crash_before_mutation_recovers_without_duplicate_writer(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch, executors=_retrying(fail_before_retry_mutation=True))
    transfer, first = await _failed_once(core)
    jobs = core.executor.jobs
    restarted = await ledger_core(tmp_path, monkeypatch, executors=_retrying())
    restarted.executor.jobs = jobs
    for _ in range(4):
        restarted.now[0] += 5
        await restarted.engine.reconcile_executions()
    live = [job for job in restarted.executor.jobs.values()
            if job.state in {ExecutionState.QUEUED, ExecutionState.RUNNING, ExecutionState.PAUSED}]
    assert len(live) <= 1
    assert len(await _authorized_rows()) == 1
    del transfer, first


async def test_native_retry_lost_ack_after_mutation_reconciles_existing_native_work(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch, executors=_retrying(lose_retry_ack=True))
    transfer, first = await _failed_once(core)
    current = (await artifact_of(core, transfer.id)).execution
    core.executor.lose_retry_ack = False
    retries = list(core.executor.retries)
    for _ in range(3):
        await core.engine.reconcile_executions()
    bound = (await artifact_of(core, transfer.id)).execution
    assert bound.attempt_id == current.attempt_id and bound.native == first.native
    assert core.executor.retries == retries  # no second native handoff
    assert [call[0] for call in core.executor.calls].count("start") == 1
    assert len(core.executor.jobs) == 1


async def test_native_retry_crash_after_binding_does_not_restore_old_normal_control_authority(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch, executors=_retrying())
    transfer, first = await _failed_once(core)
    current = (await artifact_of(core, transfer.id)).execution
    jobs = core.executor.jobs
    restarted = await ledger_core(tmp_path, monkeypatch, executors=_retrying())
    restarted.executor.jobs = jobs
    await restarted.engine.reconcile_executions()
    assert not await restarted.repository.authorize_execution(first, "observe")
    assert await restarted.repository.authorize_execution(current, "pause")
    observed = [call for call in restarted.executor.calls if call[0] == "observe_many"]
    assert observed and all(first.attempt_id not in call[1] for call in observed)
