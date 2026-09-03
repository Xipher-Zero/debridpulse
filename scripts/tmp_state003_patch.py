from __future__ import annotations

import re
from pathlib import Path


def replace_regex(path: str, pattern: str, replacement: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE | re.DOTALL)
    if count != 1:
        raise SystemExit(f"{path}: expected one replacement for {pattern!r}, found {count}")
    file.write_text(updated, encoding="utf-8")


repository_cleanup = '''    async def pending_execution_cleanup(self, now: float, *, transfer_id: int | None = None):
        async with get_db() as db:
            where_transfer = " AND e.transfer_id=?" if transfer_id is not None else ""
            params = (now, transfer_id) if transfer_id is not None else (now,)
            rows = await db.fetchall(
                """SELECT e.* FROM execution_attempts e JOIN torrents t ON t.id=e.transfer_id
                    WHERE e.cleanup_state IN ('pending','blocked') AND e.cleanup_retry_at<=?
                    AND t.status IN ('cancelled','deleted')""" + where_transfer +
                " ORDER BY e.transfer_id,e.created_at,e.id",
                params,
            )
        return tuple((self._execution_attempt(row), int(row["cleanup_attempts"] or 0), codec.error(row["cleanup_error"])) for row in rows)

    async def claim_execution_cleanup(self, attempt_id: str, *, now: float, lease_until: float) -> bool:
        async with get_db() as db:
            cursor = await db.execute(
                """UPDATE execution_attempts SET cleanup_state='pending',cleanup_attempts=cleanup_attempts+1,
                    cleanup_retry_at=?,updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND cleanup_state IN ('pending','blocked') AND cleanup_retry_at<=?""",
                (lease_until, attempt_id, now),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def execution_cleanup_retry(self, attempt_id: str, error: NormalizedError, retry_at: float) -> None:
        async with get_db() as db:
            await db.execute(
                """UPDATE execution_attempts SET cleanup_state='pending',cleanup_error=?,cleanup_retry_at=?,
                    updated_at=CURRENT_TIMESTAMP WHERE id=? AND cleanup_state='pending'""",
                (codec.dump(error), retry_at, attempt_id),
            )
            await db.commit()

    async def execution_cleanup_recheck(self, attempt_id: str, retry_at: float) -> None:
        async with get_db() as db:
            await db.execute(
                """UPDATE execution_attempts SET cleanup_state='pending',cleanup_retry_at=?,
                    updated_at=CURRENT_TIMESTAMP WHERE id=? AND cleanup_state='pending'""",
                (retry_at, attempt_id),
            )
            await db.commit()

'''
replace_regex(
    "backend/transfers/repository.py",
    r"^    async def pending_execution_cleanup\(.*?(?=^    async def execution_cleanup_complete)",
    repository_cleanup,
)

repository_delete = '''    async def delete_with_execution_cleanup(self, transfer_id: int, *, remote: bool, now: float) -> bool:
        """Atomically tombstone a transfer without abandoning launched executor work."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("SELECT id FROM torrents WHERE id=?", (transfer_id,))
            if not row:
                await db.commit()
                return False
            await db.execute(
                """UPDATE execution_attempts SET cleanup_state='pending',
                    cleanup_retry_at=CASE
                        WHEN cleanup_state IN ('pending','blocked') THEN MIN(cleanup_retry_at,?)
                        ELSE ? END,
                    cleanup_error=CASE WHEN cleanup_state IN ('pending','blocked') THEN cleanup_error ELSE NULL END
                    WHERE transfer_id=? AND authorized=1
                    AND state IN ('prepared','queued','transferring','paused','unknown')""",
                (now, now, transfer_id),
            )
            await db.execute(
                """UPDATE torrents SET status='deleted',delete_remote=?,lifecycle_epoch=lifecycle_epoch+1,
                    updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (int(remote), transfer_id),
            )
            await db.commit()
        return True

    async def delete(self, transfer_id: int, *, remote: bool):
        async with get_db() as db:
            await db.execute("""UPDATE torrents SET status='deleted',delete_remote=?,lifecycle_epoch=lifecycle_epoch+1,
                updated_at=CURRENT_TIMESTAMP WHERE id=?""", (int(remote), transfer_id))
            await db.commit()

'''
replace_regex(
    "backend/transfers/repository.py",
    r"^    async def delete\(self, transfer_id: int, \*, remote: bool\):.*?(?=^    async def delete_remote_requested)",
    repository_delete,
)

engine_cleanup = '''    def _cleanup_recheck_at(self, now: float) -> float:
        # Destructive cancellation pressure is bounded, but reconciliation ownership
        # is not. Long-lived uncertainty is revisited at a bounded cadence.
        return now + max(1.0, float(self.policy.max_retry_delay))

    def _cleanup_retry_at(self, error: NormalizedError, attempts: int, now: float) -> float:
        decision = self.policy.retry(error, attempts, now)
        if decision.retry_at is None:
            return self._cleanup_recheck_at(now)
        # Even an IMMEDIATE/zero-delay policy must not create a cleanup hot loop.
        return max(float(decision.retry_at), now + 1.0)

    async def _cleanup_executions_pending(self, *, transfer_id: int | None = None):
        errors = []
        now = self.clock()
        for attempt, attempts, _previous_error in await self.repository.pending_execution_cleanup(
            now, transfer_id=transfer_id,
        ):
            handle = attempt.handle
            executor = self.registry.executors.get(handle.executor_id)
            if executor is None:
                error = self._error(
                    Category.UNSUPPORTED_CAPABILITY, Stage.CLEANUP, domain=Domain.CLEANUP,
                    retryability=Retryability.NEVER, recovery=Recovery.REQUIRE_OPERATOR,
                )
                await self.repository.execution_cleanup_retry(
                    handle.attempt_id, error, self._cleanup_retry_at(error, attempts + 1, now),
                )
                errors.append(error)
                continue

            lease_until = now + max(1.0, float(self.policy.retry_delay))
            if not await self.repository.claim_execution_cleanup(
                handle.attempt_id, now=now, lease_until=lease_until,
            ):
                continue
            try:
                observed = await executor.observe(handle)
                if not isinstance(observed, ExecutionObservation) or observed.handle != handle:
                    raise TransferError(self._error(
                        Category.INVALID_ADAPTER_RESPONSE, Stage.CLEANUP, domain=Domain.EXECUTOR,
                        retryability=Retryability.NEVER, recovery=Recovery.REQUIRE_OPERATOR,
                    ))
                await self.repository.execution(observed)
                if observed.state in {
                    ExecutionState.ABSENT, ExecutionState.CANCELLED,
                    ExecutionState.SUCCEEDED, ExecutionState.FAILED,
                }:
                    await self.repository.execution_cleanup_complete(handle.attempt_id)
                    continue

                # cleanup_attempts counts claimed reconciliation cycles. Only the initial
                # policy budget may issue destructive cancellation RPCs; later cycles remain
                # observation-authoritative indefinitely.
                if attempts >= max(1, int(self.policy.max_attempts)):
                    await self.repository.execution_cleanup_recheck(
                        handle.attempt_id, self._cleanup_recheck_at(self.clock()),
                    )
                    continue

                outcome = await executor.cancel(handle)
                if not isinstance(outcome, TransferOutcome):
                    raise TransferError(self._error(
                        Category.INVALID_ADAPTER_RESPONSE, Stage.CLEANUP, domain=Domain.EXECUTOR,
                        retryability=Retryability.NEVER, recovery=Recovery.REQUIRE_OPERATOR,
                    ))
                await self.repository.outcome(attempt.transfer_id, outcome, attempt_id=handle.attempt_id)
                if outcome.kind not in {OutcomeKind.SUCCESS, OutcomeKind.CANCELLED, OutcomeKind.SKIPPED}:
                    error = outcome.error or self._error(
                        Category.REMOTE_CLEANUP_FAILED, Stage.CLEANUP, domain=Domain.CLEANUP,
                        retryability=Retryability.BACKOFF, recovery=Recovery.RETRY,
                    )
                    raise TransferError(error)

                # A successful/cancelled executor outcome is the executor contract's
                # termination acknowledgement. Do not require a second observation: a
                # concurrent late-completion observation is history, not grounds to reopen
                # cleanup or the already-terminal logical transfer.
                await self.repository.execution_cleanup_complete(handle.attempt_id)
            except Exception as exc:
                error = self._executor_cleanup_exception(handle.executor_id, exc)
                retry_now = self.clock()
                await self.repository.execution_cleanup_retry(
                    handle.attempt_id, error, self._cleanup_retry_at(error, attempts + 1, retry_now),
                )
                errors.append(error)
        return tuple(errors)

'''
replace_regex(
    "backend/transfers/engine.py",
    r"^    async def _cleanup_executions_pending\(.*?(?=^    async def delete\()",
    engine_cleanup,
)

engine_delete = '''    async def delete(self, transfer_id: int, *, remote=True):
        lock = self._transfer_locks.setdefault(transfer_id, asyncio.Lock())
        async with lock:
            challenge = await self.challenges.current(transfer_id)
            if challenge:
                await self.inputs.clear(challenge.id)
            await self.challenges.clear_transfer(transfer_id)
            # Tombstoning and executor-cleanup ownership are one durable fact:
            # deleting/hiding the logical record cannot abandon launched work.
            deleted = await self.repository.delete_with_execution_cleanup(
                transfer_id, remote=remote, now=self.clock(),
            )
        if not deleted:
            return
        await self._cleanup_executions_pending(transfer_id=transfer_id)
        if remote:
            await self._cleanup_resources(transfer_id, explicit=True)

'''
replace_regex(
    "backend/transfers/engine.py",
    r"^    async def delete\(self, transfer_id: int, \*, remote=True\):.*?(?=^    async def _cleanup_resources)",
    engine_delete,
)


def replace_test(name: str, replacement: str) -> None:
    replace_regex(
        "backend/tests/test_second_pass_state002.py",
        rf"^@pytest\.mark\.asyncio\nasync def {re.escape(name)}\(core\):.*?(?=^@pytest\.mark\.asyncio|\Z)",
        replacement.rstrip() + "\n\n\n",
    )


test_file = Path("backend/tests/test_second_pass_state002.py")
test_text = test_file.read_text(encoding="utf-8")
test_text = test_text.replace(
    '"""Second-pass STATE-002 durable external-execution cleanup contracts."""',
    '"""STATE-002/STATE-003 durable external-execution cleanup contracts."""',
    1,
)
if "from dataclasses import replace\n" not in test_text:
    test_text = test_text.replace("import asyncio\n", "import asyncio\nfrom dataclasses import replace\n", 1)
test_file.write_text(test_text, encoding="utf-8")

replace_test(
    "test_state002_failed_cancel_survives_restart_and_converges",
    '''@pytest.mark.asyncio
async def test_state002_failed_cancel_survives_restart_and_converges(core):
    transfer, artifact = await executing(core)
    error = transient_cleanup_error()
    original_cancel = core.executor.cancel
    calls = 0

    async def fail_once(handle):
        nonlocal calls
        calls += 1
        if calls == 1:
            return TransferOutcome(OutcomeKind.FAILURE, error)
        return await original_cancel(handle)

    core.executor.cancel = fail_once
    assert await core.engine.cancel(transfer.id) == (error,)
    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    core.clock.value = status["retry_at"]
    restarted = TransferEngine(
        TransferRepository(), core.registry, download_root=core.engine.root, policy=core.engine.policy, clock=core.clock,
    )
    await restarted.initialize()
    await restarted.reconcile_executions()
    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert calls == 2
    assert status["state"] == "complete"
    assert status["authorized"] is False
    assert (await core.repository.get(transfer.id)).state == TransferState.CANCELLED''',
)

replace_test(
    "test_state002_repeated_failures_are_bounded_and_obligation_remains_truthful",
    '''@pytest.mark.asyncio
async def test_state003_destructive_failures_are_bounded_but_reconciliation_remains_live(core):
    transfer, artifact = await executing(core)
    error = transient_cleanup_error()
    cancel_calls = 0

    async def fail_cancel(_handle):
        nonlocal cancel_calls
        cancel_calls += 1
        return TransferOutcome(OutcomeKind.FAILURE, error)

    core.executor.cancel = fail_cancel
    for index in range(3):
        assert await core.engine.cancel(transfer.id) == (error,)
        status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
        if index < 2:
            core.clock.value = status["retry_at"]

    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert status["attempts"] == 3
    assert status["state"] == "pending"
    assert status["error"] == error
    assert status["authorized"] is True
    assert status["retry_at"] > core.clock.value
    assert cancel_calls == 3
    assert (await core.repository.get(transfer.id)).state == TransferState.CANCELLED

    core.clock.value = status["retry_at"]
    due = await core.repository.pending_execution_cleanup(core.clock())
    assert [item[0].handle.attempt_id for item in due] == [artifact.execution.attempt_id]
    await core.engine.reconcile_executions()
    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert status["attempts"] == 4
    assert status["state"] == "pending"
    assert status["authorized"] is True
    assert status["retry_at"] > core.clock.value
    assert cancel_calls == 3''',
)

replace_test(
    "test_state002_late_executor_success_only_reconciles_external_cleanup",
    '''@pytest.mark.asyncio
async def test_state002_late_executor_success_only_reconciles_external_cleanup(core):
    transfer, artifact = await executing(core)
    error = transient_cleanup_error()

    async def fail_cancel(_handle):
        return TransferOutcome(OutcomeKind.FAILURE, error)

    core.executor.cancel = fail_cancel
    await core.engine.cancel(transfer.id)
    core.executor.finish(artifact.execution, materialize=False)
    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    core.clock.value = status["retry_at"]
    restarted = TransferEngine(
        TransferRepository(), core.registry, download_root=core.engine.root, policy=core.engine.policy, clock=core.clock,
    )
    await restarted.initialize()
    await restarted.reconcile_executions()
    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert status["state"] == "complete"
    assert status["execution_state"] == ExecutionState.SUCCEEDED
    assert (await core.repository.get(transfer.id)).state == TransferState.CANCELLED
    assert (await core.repository.artifacts(transfer.id))[0].state == "cancelled"''',
)

replace_test(
    "test_state002_repeated_user_cancel_does_not_duplicate_cleanup_obligation",
    '''@pytest.mark.asyncio
async def test_state002_repeated_user_cancel_does_not_duplicate_cleanup_obligation(core):
    transfer, artifact = await executing(core)
    error = transient_cleanup_error()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_failure(_handle):
        entered.set()
        await release.wait()
        return TransferOutcome(OutcomeKind.FAILURE, error)

    core.executor.cancel = blocked_failure
    first = asyncio.create_task(core.engine.cancel(transfer.id))
    await entered.wait()
    assert (await core.repository.get(transfer.id)).state == TransferState.CANCELLED
    release.set()
    assert await first == (error,)
    cleanup = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    core.clock.value = cleanup["retry_at"]
    await core.engine.cancel(transfer.id)
    statuses = [item for item in await core.repository.executions(transfer.id) if item.handle.attempt_id == artifact.execution.attempt_id]
    assert len(statuses) == 1
    cleanup = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert cleanup["attempts"] == 2''',
)

with test_file.open("a", encoding="utf-8") as handle:
    handle.write(r'''

@pytest.mark.asyncio
async def test_state003_legacy_blocked_obligation_is_recovered_into_live_reconciliation(core):
    transfer, artifact = await executing(core)
    error = transient_cleanup_error()

    async def fail_cancel(_handle):
        return TransferOutcome(OutcomeKind.FAILURE, error)

    core.executor.cancel = fail_cancel
    await core.engine.cancel(transfer.id)
    async with database.get_db() as db:
        await db.execute(
            "UPDATE execution_attempts SET cleanup_state='blocked',cleanup_retry_at=0 WHERE id=?",
            (artifact.execution.attempt_id,),
        )
        await db.commit()

    due = await core.repository.pending_execution_cleanup(core.clock())
    assert [item[0].handle.attempt_id for item in due] == [artifact.execution.attempt_id]
    await core.engine.reconcile_executions()
    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert status["state"] == "pending"
    assert status["attempts"] == 2
    assert status["authorized"] is True


@pytest.mark.asyncio
async def test_state003_restart_after_budget_exhaustion_eventually_observes_absence(core):
    transfer, artifact = await executing(core)
    error = transient_cleanup_error()

    async def fail_cancel(_handle):
        return TransferOutcome(OutcomeKind.FAILURE, error)

    core.executor.cancel = fail_cancel
    for index in range(3):
        await core.engine.cancel(transfer.id)
        status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
        if index < 2:
            core.clock.value = status["retry_at"]
    core.clock.value = status["retry_at"]
    core.executor.jobs.pop(artifact.execution.attempt_id, None)

    restarted = TransferEngine(
        TransferRepository(), core.registry, download_root=core.engine.root,
        policy=core.engine.policy, clock=core.clock,
    )
    await restarted.initialize()
    await restarted.reconcile_executions()
    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert status["state"] == "complete"
    assert status["authorized"] is False
    assert status["execution_state"] == ExecutionState.ABSENT
    assert (await core.repository.get(transfer.id)).state == TransferState.CANCELLED


@pytest.mark.asyncio
async def test_state003_terminal_completion_after_budget_exhaustion_settles_cleanup_only(core):
    transfer, artifact = await executing(core)
    error = transient_cleanup_error()

    async def fail_cancel(_handle):
        return TransferOutcome(OutcomeKind.FAILURE, error)

    core.executor.cancel = fail_cancel
    for index in range(3):
        await core.engine.cancel(transfer.id)
        status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
        if index < 2:
            core.clock.value = status["retry_at"]
    core.executor.finish(artifact.execution, materialize=False)
    core.clock.value = status["retry_at"]
    await core.engine.reconcile_executions()

    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert status["state"] == "complete"
    assert status["execution_state"] == ExecutionState.SUCCEEDED
    assert status["authorized"] is False
    assert (await core.repository.get(transfer.id)).state == TransferState.CANCELLED
    assert (await core.repository.artifacts(transfer.id))[0].state == "cancelled"


@pytest.mark.asyncio
async def test_state003_terminal_cancel_after_budget_exhaustion_settles_without_resurrection(core):
    transfer, artifact = await executing(core)
    error = transient_cleanup_error()

    async def fail_cancel(_handle):
        return TransferOutcome(OutcomeKind.FAILURE, error)

    core.executor.cancel = fail_cancel
    for index in range(3):
        await core.engine.cancel(transfer.id)
        status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
        if index < 2:
            core.clock.value = status["retry_at"]
    current = core.executor.jobs[artifact.execution.attempt_id]
    core.executor.jobs[artifact.execution.attempt_id] = replace(current, state=ExecutionState.CANCELLED)
    core.clock.value = status["retry_at"]
    await core.engine.reconcile_executions()
    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert status["state"] == "complete"
    assert status["authorized"] is False
    assert (await core.repository.get(transfer.id)).state == TransferState.CANCELLED


@pytest.mark.asyncio
async def test_state003_prolonged_observation_outage_backs_off_without_hot_loop(core):
    transfer, artifact = await executing(core)
    error = transient_cleanup_error()

    async def fail_cancel(_handle):
        return TransferOutcome(OutcomeKind.FAILURE, error)

    core.executor.cancel = fail_cancel
    for index in range(3):
        await core.engine.cancel(transfer.id)
        status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
        if index < 2:
            core.clock.value = status["retry_at"]
    core.clock.value = status["retry_at"]
    observe_calls = 0

    async def unavailable(_handle):
        nonlocal observe_calls
        observe_calls += 1
        raise asyncio.TimeoutError("executor unavailable")

    core.executor.observe = unavailable
    await core.engine.reconcile_executions()
    first = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert first["state"] == "pending"
    assert first["retry_at"] > core.clock.value
    assert observe_calls == 1
    await core.engine.reconcile_executions()
    second = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert second["attempts"] == first["attempts"]
    assert second["retry_at"] == first["retry_at"]
    assert observe_calls == 1


@pytest.mark.asyncio
async def test_state003_concurrent_cleanup_workers_do_not_duplicate_rpc_pressure(core):
    transfer, artifact = await executing(core)
    error = transient_cleanup_error()

    async def fail_cancel(_handle):
        return TransferOutcome(OutcomeKind.FAILURE, error)

    core.executor.cancel = fail_cancel
    await core.engine.cancel(transfer.id)
    status = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    core.clock.value = status["retry_at"]
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def blocked_failure(_handle):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return TransferOutcome(OutcomeKind.FAILURE, error)

    core.executor.cancel = blocked_failure
    first = asyncio.create_task(core.engine.reconcile_executions())
    await entered.wait()
    await core.engine.reconcile_executions()
    release.set()
    await first
    cleanup = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert calls == 1
    assert cleanup["attempts"] == 2
    assert cleanup["state"] == "pending"


@pytest.mark.asyncio
async def test_state003_user_delete_retains_cleanup_and_path_until_external_absence(core):
    transfer, artifact = await executing(core)
    error = transient_cleanup_error()

    async def fail_cancel(_handle):
        return TransferOutcome(OutcomeKind.FAILURE, error)

    core.executor.cancel = fail_cancel
    await core.engine.delete(transfer.id, remote=False)
    cleanup = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert (await core.repository.get(transfer.id)).state == TransferState.DELETED
    assert cleanup["state"] == "pending"
    assert cleanup["authorized"] is True
    assert artifact.target.casefold() in await core.repository.occupied_paths()

    core.clock.value = cleanup["retry_at"]
    core.executor.jobs.pop(artifact.execution.attempt_id, None)
    await core.engine.reconcile_executions()
    cleanup = await core.repository.execution_cleanup_status(artifact.execution.attempt_id)
    assert cleanup["state"] == "complete"
    assert cleanup["authorized"] is False
    assert artifact.target.casefold() not in await core.repository.occupied_paths()
    assert (await core.repository.get(transfer.id)).state == TransferState.DELETED
''')

architecture = Path("docs/architecture/UNIVERSAL_TRANSFER_CORE.md")
architecture_text = architecture.read_text(encoding="utf-8")
marker = "### Database startup and migration\n"
addition = '''### Executor cleanup reconciliation liveness

Logical cancellation or deletion is independent of executor cleanup completion. Once an external execution has launched, the core retains a durable cleanup/reconciliation obligation until that execution is authoritatively terminal or absent. Destructive executor cancellation RPC pressure is bounded by the universal retry budget and durable backoff, but exhausting that pressure never ends reconciliation ownership. A degraded or previously `blocked` cleanup record remains discoverable by maintenance and restart recovery and is returned to live `pending` reconciliation; it is not cleanup completion.

After the destructive cancellation budget is exhausted, maintenance continues bounded observation-only reconciliation. Late external completion can settle external uncertainty but cannot revive a logically cancelled or deleted transfer, publish ordinary successful delivery, or rewrite its logical outcome. While external execution remains uncertain, its authorized execution record continues to reserve the destination path; authoritative terminality clears that authority and allows normal path convergence. User deletion atomically records the tombstone together with any required executor-cleanup responsibility, so hiding the logical record cannot abandon already-launched work.

`execution_attempts.authorized` is current core authority to contact or reconcile the persisted execution handle. Cancellation and deletion do not revoke that authority while external work remains uncertain; `execution_cleanup_complete()` revokes it only after external terminality or absence is authoritative.

'''
if architecture_text.count(marker) != 1:
    raise SystemExit("architecture insertion marker not unique")
architecture.write_text(architecture_text.replace(marker, addition + marker, 1), encoding="utf-8")

architecture_test = Path("backend/tests/test_post_audit_architecture_documentation.py")
architecture_test_text = architecture_test.read_text(encoding="utf-8")
old = '''    assert "Logical cancellation authority is committed on the parent transfer before remote executor cancellation" in doc
    assert "cannot revive it" in doc
    assert "Normal repository initialization" in doc
'''
new = '''    assert "Logical cancellation authority is committed on the parent transfer before remote executor cancellation" in doc
    assert "cannot revive it" in doc
    assert "exhausting that pressure never ends reconciliation ownership" in doc
    assert "observation-only reconciliation" in doc
    assert "User deletion atomically records the tombstone" in doc
    assert "execution_attempts.authorized" in doc
    assert "Normal repository initialization" in doc
'''
if architecture_test_text.count(old) != 1:
    raise SystemExit("architecture test insertion target not unique")
architecture_test.write_text(architecture_test_text.replace(old, new, 1), encoding="utf-8")
