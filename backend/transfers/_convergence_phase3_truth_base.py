"""Production Phase-3 recovery convergence owner.

Executor truth is reconciled before readiness blockers can hide completion or a
terminal failure. Resumable work is only resumed after current pause/provider/
storage gates are re-evaluated. Unknown executor truth remains fenced and
quiescent rather than causing replacement execution.
"""
from __future__ import annotations

from transfers._convergence_phase3_public_base import _Step
from transfers._convergence_phase3_retry_base import TransferEngine as _RetryQualifiedEngine
from transfers.contracts import PauseResume
from transfers.errors import (
    Category, Domain, NormalizedError, Origin, Retryability, Stage, TransferError,
    unknown_failure,
)
from transfers.models import ExecutionObservation, ExecutionState, TransferState
from transfers.policy import RecoveryAction
from transfers.recovery_execution import RecoveryClaim, RecoveryTrigger, trigger_authority


_TERMINAL = frozenset({
    TransferState.COMPLETED,
    TransferState.CONSOLIDATED,
    TransferState.CANCELLED,
    TransferState.DELETED,
})


class TransferEngine(_RetryQualifiedEngine):
    """Final public Phase-3 transfer engine."""

    async def _park_existing_execution(
        self,
        claim: RecoveryClaim,
        artifact,
        *,
        reason: str,
        wake: str,
        retry_at: float = 0.0,
    ) -> bool:
        """Park without losing a usable GID; retire only when pause is impossible."""
        current = await self._current_artifact(artifact.transfer_id, artifact.id)
        if current is None:
            return False
        if current.execution is None:
            return await super()._park_existing_execution(
                claim, current, reason=reason, wake=wake, retry_at=retry_at,
            )

        executor = self.registry.executors.get(current.execution.executor_id)
        if executor is None:
            return await self.repository.record_recovery_quiescence(
                claim,
                reason=reason,
                wake_condition=wake,
                blocked_retry_at=retry_at,
            )
        if not await self.repository.recovery_claim_current(claim, now=self.clock()):
            return False
        try:
            observed = await executor.observe(current.execution)
        except Exception:
            observed = ExecutionObservation(
                current.execution,
                ExecutionState.UNKNOWN,
                error=self._executor_wait_error(current.execution.executor_id),
            )
        if observed.handle != current.execution:
            raise TransferError(NormalizedError(
                Domain.EXECUTOR,
                Category.INVALID_ADAPTER_RESPONSE,
                Stage.RECONCILIATION,
                retryability=Retryability.NEVER,
                origin=Origin.CORE,
            ))

        if observed.resumable:
            if isinstance(executor, PauseResume):
                if observed.state != ExecutionState.PAUSED:
                    if not await self.repository.recovery_claim_current(claim, now=self.clock()):
                        return False
                    observed = await executor.pause(current.execution)
                    if observed.handle != current.execution:
                        raise TransferError(NormalizedError(
                            Domain.EXECUTOR,
                            Category.INVALID_ADAPTER_RESPONSE,
                            Stage.RECONCILIATION,
                            retryability=Retryability.NEVER,
                            origin=Origin.CORE,
                        ))
                await self.repository.execution(observed)
                return await self.repository.record_recovery_quiescence(
                    claim,
                    reason=reason,
                    wake_condition=wake,
                    blocked_retry_at=retry_at,
                )

            # A blocker requires productive execution to stop. If this executor
            # cannot pause, retirement is necessary; cancellation does not delete
            # the artifact target or partial bytes.
            if not await self.repository.recovery_claim_current(claim, now=self.clock()):
                return False
            outcome = await executor.cancel(current.execution)
            await self.repository.outcome(
                current.transfer_id,
                outcome,
                attempt_id=current.execution.attempt_id,
            )
            confirmed = await executor.observe(current.execution)
            if confirmed.handle == current.execution:
                await self.repository.execution(confirmed)
            await self.repository.record_phase3_application(
                claim,
                action=RecoveryAction.RECONCILE.value,
                reason=reason,
                retirement_reason=f"{reason}:executor_not_pausable",
                partial_preserved=True,
            )
            current = await self._current_artifact(current.transfer_id, current.id)
            if current is None:
                return False
            return await super()._park_existing_execution(
                claim, current, reason=reason, wake=wake, retry_at=retry_at,
            )

        if observed.state == ExecutionState.UNKNOWN:
            await self.repository.execution(observed)
            return await self.repository.record_recovery_quiescence(
                claim,
                reason=reason,
                wake_condition=wake,
                blocked_retry_at=retry_at,
            )

        await self.repository.execution(observed)
        current = await self._current_artifact(current.transfer_id, current.id)
        if current is None:
            return False
        return await super()._park_existing_execution(
            claim, current, reason=reason, wake=wake, retry_at=retry_at,
        )

    async def _reconcile_current(
        self,
        claim: RecoveryClaim,
        artifact,
        trigger: RecoveryTrigger,
        error: NormalizedError | None,
        observed: ExecutionObservation | None,
    ):
        current = await self._current_artifact(artifact.transfer_id, artifact.id)
        transfer = await self.repository.get(artifact.transfer_id)
        if current is None or transfer is None or transfer.state in _TERMINAL:
            return None, error, observed, _Step(
                True, False, RecoveryAction.RECONCILE.value,
                "terminal_or_missing", "terminal_or_missing",
            ), None

        authority = trigger_authority(trigger)
        if authority.reset_exhaustion:
            await self.repository.reset_retry_budget(current.id)
            current = await self._current_artifact(current.transfer_id, current.id)
            if current is None:
                return None, error, observed, _Step(
                    True, False, RecoveryAction.RECONCILE.value,
                    "artifact_disappeared", "terminal_or_missing",
                ), None

        if transfer.paused and trigger != RecoveryTrigger.RESUME:
            if current.execution is not None:
                executor = self.registry.executors.get(current.execution.executor_id)
                if isinstance(executor, PauseResume):
                    await self._converge_execution(current, executor)
            return current, error, observed, _Step(
                True, True, RecoveryAction.RECONCILE.value,
                "paused_intent_current", "paused",
            ), None

        if current.state == "input_required":
            input_error = error or NormalizedError(
                Domain.REQUEST,
                Category.CREDENTIAL_MISSING,
                Stage.QUEUE,
                retryability=Retryability.AFTER_REAUTH,
                origin=Origin.CORE,
            )
            step = await self._decision_step(
                claim, current, input_error, count_failure=False, outcome="input_required",
            )
            return current, input_error, observed, step, None

        executor = None
        retirement_reason = None
        factual_terminal_error = False
        if current.execution is not None:
            executor = self.registry.executors.get(current.execution.executor_id)
            if executor is not None:
                if observed is None:
                    try:
                        observed = await executor.observe(current.execution)
                    except Exception as exc:
                        observed = ExecutionObservation(
                            current.execution,
                            ExecutionState.UNKNOWN,
                            error=unknown_failure(
                                exc,
                                integration_id=current.execution.executor_id,
                                domain=Domain.EXECUTOR,
                                stage=Stage.RECONCILIATION,
                            ),
                        )
                if observed.handle != current.execution:
                    raise TransferError(NormalizedError(
                        Domain.EXECUTOR,
                        Category.INVALID_ADAPTER_RESPONSE,
                        Stage.RECONCILIATION,
                        retryability=Retryability.NEVER,
                        origin=Origin.CORE,
                    ))

                if observed.state == ExecutionState.SUCCEEDED:
                    await self._execution_result(current, executor, observed)
                    current = await self._current_artifact(current.transfer_id, current.id)
                    if current is None:
                        return None, error, observed, _Step(
                            True, False, RecoveryAction.RECONCILE.value,
                            "artifact_disappeared", "terminal_or_missing",
                        ), None
                    if current.state == "completed":
                        return current, None, observed, _Step(
                            True, True, RecoveryAction.RECONCILE.value,
                            "execution_completed", "execution_completed",
                            retirement_reason="execution_succeeded",
                        ), "execution_succeeded"
                    if current.error is not None:
                        error = current.error
                        factual_terminal_error = True
                    retirement_reason = "execution_succeeded_but_verification_failed"

                elif observed.state in {
                    ExecutionState.FAILED, ExecutionState.ABSENT, ExecutionState.CANCELLED,
                }:
                    await self.repository.execution(observed)
                    current = await self._current_artifact(current.transfer_id, current.id)
                    if current is None:
                        return None, error, observed, _Step(
                            True, False, RecoveryAction.RECONCILE.value,
                            "artifact_disappeared", "terminal_or_missing",
                        ), None
                    retirement_reason = f"execution_{observed.state.value}"
                    factual_terminal_error = observed.state != ExecutionState.CANCELLED
                    if observed.state == ExecutionState.ABSENT:
                        error = error or self._orphaned_error(observed.handle.executor_id)
                    elif observed.state == ExecutionState.FAILED:
                        error = error or observed.error or NormalizedError(
                            Domain.EXECUTOR,
                            Category.TRANSFER_FAILED,
                            Stage.EXECUTION,
                            retryability=Retryability.BACKOFF,
                            origin=Origin.EXECUTOR,
                            integration_id=observed.handle.executor_id,
                        )

                elif observed.state == ExecutionState.UNKNOWN:
                    await self.repository.execution(observed)
                    error = error or observed.error or NormalizedError(
                        Domain.RECONCILIATION,
                        Category.RECONCILIATION_FAILED,
                        Stage.RECONCILIATION,
                        retryability=Retryability.BACKOFF,
                        origin=Origin.CORE,
                        integration_id=current.execution.executor_id,
                    )

        candidate = self._candidate(current)
        provider_ready = candidate is None or self._candidate_provider_enabled(candidate)
        if not provider_ready:
            wait_error = error or self._provider_wait_error(candidate)
            step = await self._decision_step(
                claim,
                current,
                wait_error,
                observed=observed if factual_terminal_error else None,
                count_failure=factual_terminal_error and self._counts_recovery_failure(wait_error),
                force_provider_not_ready=True,
                outcome="provider_wait",
                retirement_reason=retirement_reason,
            )
            return current, wait_error, observed, step, retirement_reason

        if not self.dispatch_permitted:
            storage_error = error or NormalizedError(
                Domain.LOCAL_RESOURCE,
                Category.DOWNLOAD_STORAGE_UNAVAILABLE,
                Stage.EXECUTION,
                retryability=Retryability.AFTER_RESOURCE_CHANGE,
                origin=Origin.LOCAL_SYSTEM,
            )
            step = await self._decision_step(
                claim,
                current,
                storage_error,
                observed=observed if factual_terminal_error else None,
                count_failure=factual_terminal_error and self._counts_recovery_failure(storage_error),
                force_storage_not_ready=True,
                outcome="storage_wait",
                retirement_reason=retirement_reason,
            )
            return current, storage_error, observed, step, retirement_reason

        if current.execution is not None and executor is None:
            wait_error = error or self._executor_wait_error(current.execution.executor_id)
            step = await self._decision_step(
                claim,
                current,
                wait_error,
                count_failure=False,
                force_executor_not_ready=True,
                outcome="executor_wait",
                retirement_reason=retirement_reason,
            )
            return current, wait_error, observed, step, retirement_reason

        if current.execution is not None and observed is not None:
            if observed.state == ExecutionState.UNKNOWN:
                step = await self._decision_step(
                    claim,
                    current,
                    error,
                    observed=observed,
                    count_failure=False,
                    outcome="executor_truth_unknown",
                    retirement_reason=retirement_reason,
                )
                return current, error, observed, step, retirement_reason
            if observed.resumable:
                if isinstance(executor, PauseResume):
                    observed = await self._converge_execution(current, executor, observed)
                else:
                    await self.repository.execution(observed)
                await self.repository.clear_recovery_quiescence(claim)
                return current, error, observed, _Step(
                    True, True, RecoveryAction.RECONCILE.value,
                    "existing_execution_resumable", "existing_execution_reused",
                ), retirement_reason

        return current, error, observed, None, retirement_reason
