"""Canonical Phase-3 unified recovery convergence owner.

The substantial qualified mechanics live in ``_convergence_phase3_base``.  This
public owner keeps ``recover_artifact`` as a small coordinator and separates
current-state reconciliation from post-reconciliation planning.  All trigger
adapters inherited from the qualified base dispatch virtually back through this
one coordinator.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from transfers._convergence_phase3_base import TransferEngine as _Phase3BaseEngine
from transfers.contracts import CandidateRefresh, PauseResume
from transfers.errors import (
    Category, Domain, NormalizedError, Origin, Retryability, Stage, TransferError,
    unknown_failure,
)
from transfers.mirrors import reported_sizes_compatible
from transfers.models import (
    Artifact, ExecutionObservation, ExecutionState, OutcomeKind, ResolutionAttempt,
    ResolutionResult, ResourceState, TransferOutcome, TransferState,
)
from transfers.policy import RecoveryAction
from transfers.recovery_execution import RecoveryClaim, RecoveryTrigger, trigger_authority


_TERMINAL = frozenset({
    TransferState.COMPLETED,
    TransferState.CONSOLIDATED,
    TransferState.CANCELLED,
    TransferState.DELETED,
})


@dataclass(frozen=True)
class _Step:
    handled: bool
    applied: bool
    action: str
    reason: str
    outcome: str
    candidate_changed: bool = False
    reconstruction_reason: str | None = None
    retirement_reason: str | None = None


class TransferEngine(_Phase3BaseEngine):
    """Single production recovery-execution owner beneath ``TransferPolicy``."""

    async def _artifact_ref(self, artifact: Artifact | int) -> Artifact | None:
        if not isinstance(artifact, int):
            return artifact
        for transfer in await self.repository.active():
            found = next(
                (item for item in await self.repository.artifacts(transfer.id) if item.id == artifact),
                None,
            )
            if found is not None:
                return found
        return None

    async def _decision_step(
        self,
        claim: RecoveryClaim,
        artifact: Artifact,
        error: NormalizedError,
        *,
        observed: ExecutionObservation | None = None,
        count_failure: bool,
        force_provider_not_ready: bool = False,
        force_executor_not_ready: bool = False,
        force_storage_not_ready: bool = False,
        outcome: str = "decision_applied",
        retirement_reason: str | None = None,
    ) -> _Step:
        applied, action, reason, candidate_changed = await self._decide_and_apply(
            claim,
            artifact,
            error,
            observed=observed,
            count_failure=count_failure,
            force_provider_not_ready=force_provider_not_ready,
            force_executor_not_ready=force_executor_not_ready,
            force_storage_not_ready=force_storage_not_ready,
        )
        return _Step(
            True,
            applied,
            action,
            reason,
            outcome if applied else "not_applied",
            candidate_changed,
            retirement_reason=retirement_reason,
        )

    async def _decide_and_apply(
        self,
        claim: RecoveryClaim,
        artifact: Artifact,
        error: NormalizedError,
        *,
        observed: ExecutionObservation | None = None,
        count_failure: bool,
        force_provider_not_ready: bool = False,
        force_executor_not_ready: bool = False,
        force_storage_not_ready: bool = False,
    ) -> tuple[bool, str, str, bool]:
        """Use Phase-2 policy; this method owns application context, not policy."""
        error = self.policy.compatibility(error)
        failure_identity = self._failure_identity(artifact, error, observed)
        completed_bytes = observed.progress.completed_bytes if observed is not None else 0
        if count_failure:
            _failures, _refreshes, consumed = await self.repository.record_source_failure_once(
                artifact.id, error, failure_identity,
            )
            if consumed:
                await self.repository.outcome(
                    artifact.transfer_id,
                    TransferOutcome(OutcomeKind.FAILURE, error),
                    attempt_id=artifact.execution.attempt_id if artifact.execution else None,
                )

        current = await self._current_artifact(artifact.transfer_id, artifact.id)
        if current is None:
            return False, RecoveryAction.RECONCILE.value, "artifact_disappeared", False
        next_index = await self._next_alternate_index(current)
        candidate = self._candidate(current)
        provider = self.registry.providers.get(candidate.provider_id) if candidate else None
        can_refresh = bool(
            provider and provider.descriptor.enabled and isinstance(provider, CandidateRefresh)
        )
        context = await self._recovery_context(
            current,
            can_refresh=can_refresh,
            has_alternate=next_index is not None,
        )
        context = replace(
            context,
            provider_ready=False if force_provider_not_ready else context.provider_ready,
            executor_ready=False if force_executor_not_ready else context.executor_ready,
            storage_ready=False if force_storage_not_ready else context.storage_ready,
            input_required=current.state == "input_required",
        )
        decision = self.policy.recover(error, context, self.clock())
        decision_id = f"{current.id}:{context.recovery_epoch}:{failure_identity}:{decision.action.value}"
        if not await self.repository.record_phase3_decision(
            claim,
            decision_id=decision_id,
            action=decision.action.value,
            reason=decision.reason,
            error=error,
            completed_bytes=completed_bytes,
        ):
            return False, decision.action.value, decision.reason, False
        applied = await self._apply_recovery_decision(
            claim,
            current,
            error,
            decision,
            decision_id=decision_id,
            next_index=next_index,
        )
        return applied, decision.action.value, decision.reason, (
            decision.action == RecoveryAction.TRY_ALTERNATE_CANDIDATE and applied
        )

    async def _refresh_claimed(self, claim: RecoveryClaim, artifact: Artifact) -> tuple[bool, str]:
        """Single-flight refresh with a renewed fence immediately before mutation."""
        current = await self._current_artifact(artifact.transfer_id, artifact.id)
        if current is None or current.state != "refresh_pending":
            return False, "refresh_not_pending"
        candidate = self._candidate(current)
        if candidate is None:
            return False, "candidate_missing"
        provider = self.registry.providers.get(candidate.provider_id)
        if provider is None or not provider.descriptor.enabled:
            return False, "provider_unavailable"
        if not isinstance(provider, CandidateRefresh):
            return False, "refresh_unsupported"
        origin = await self.canonical.origin_for(current, candidate)
        if origin is None:
            return False, "candidate_origin_missing"
        record = origin.request
        context = await self.repository.recovery_context(current.id)
        decision_id = str(context.get("recovery_decision_id") or "")
        if not decision_id:
            decision_id = f"legacy:{current.id}:{int(context.get('recovery_epoch') or 0)}:refresh"
            if not await self.repository.record_phase3_decision(
                claim,
                decision_id=decision_id,
                action=RecoveryAction.REFRESH_CANDIDATE.value,
                reason="legacy_refresh_pending",
            ):
                return False, "claim_lost"
            if not await self.repository.reserve_recovery_refresh(
                claim,
                decision_id,
                limit=max(1, self.policy.refreshes_per_recovery_epoch),
            ):
                return False, "refresh_budget_exhausted"

        state = await self.repository.begin_recovery_refresh(
            claim, record, provider.descriptor.id, decision_id,
        )
        if state is None:
            return False, "claim_lost"
        attempt_id = state["attempt_id"]
        attempt = ResolutionAttempt(attempt_id, record.id, provider.descriptor.id, "started")

        if not state["created"]:
            if state.get("state") != "succeeded":
                return False, "refresh_outcome_unknown"
            candidates = await self.repository.resolved_candidates(record.id)
            if not candidates:
                return False, "refresh_result_empty"
            replacement_size = candidates[0].expected_bytes
            if (
                current.expected_bytes > 0
                and replacement_size > 0
                and not reported_sizes_compatible(current.expected_bytes, replacement_size)
            ):
                return False, "refresh_size_mismatch"
            if not await self.repository.renew_recovery_claim(
                claim, self.clock(), lease_seconds=max(300.0, float(self.policy.max_retry_delay)),
            ):
                return False, "claim_lost"
            if not await self.canonical.refresh_candidate(current, origin, candidate, candidates):
                return False, "refresh_replay_conflict"
            size = current.expected_bytes if current.expected_bytes > 0 else replacement_size
            await self.repository.artifact_state(
                current.id,
                "queued",
                selected=current.selected,
                expected_bytes=max(0, size),
            )
            await self.repository.clear_recovery_refresh_inflight(claim, decision_id)
            await self.repository.clear_recovery_quiescence(claim)
            return True, "refresh_replayed"

        if not await self.repository.recovery_claim_current(claim, now=self.clock()):
            return False, "claim_lost"
        bound_candidate = replace(candidate, refresh_request=record.request)
        try:
            result = self._authoritative_provider_result(
                provider.descriptor.id,
                await provider.refresh(bound_candidate),
            )
        except Exception as exc:
            error = exc.error if isinstance(exc, TransferError) else unknown_failure(
                exc,
                integration_id=provider.descriptor.id,
                domain=Domain.PROVIDER,
                stage=Stage.CANDIDATE_PREPARATION,
            )
            result = ResolutionResult(ResourceState.UNKNOWN, error=error)

        await self.repository.resolution(attempt, result)
        if not await self.repository.recovery_claim_current(claim, now=self.clock()):
            return False, "claim_lost_after_refresh"
        if result.error:
            await self.repository.clear_recovery_refresh_inflight(claim, decision_id)
            return False, "refresh_failed"
        if not result.candidates:
            await self.repository.clear_recovery_refresh_inflight(claim, decision_id)
            return False, "refresh_result_empty"
        if any(item.expires_at is not None and item.expires_at <= self.clock() for item in result.candidates):
            await self.repository.clear_recovery_refresh_inflight(claim, decision_id)
            return False, "refresh_candidate_expired"
        replacement_size = result.candidates[0].expected_bytes
        if (
            current.expected_bytes > 0
            and replacement_size > 0
            and not reported_sizes_compatible(current.expected_bytes, replacement_size)
        ):
            await self.repository.clear_recovery_refresh_inflight(claim, decision_id)
            return False, "refresh_size_mismatch"
        if not await self.repository.renew_recovery_claim(
            claim, self.clock(), lease_seconds=max(300.0, float(self.policy.max_retry_delay)),
        ):
            return False, "claim_lost_after_refresh"
        if not await self.canonical.refresh_candidate(current, origin, candidate, result.candidates):
            return False, "refresh_candidate_conflict"
        size = current.expected_bytes if current.expected_bytes > 0 else replacement_size
        await self.repository.artifact_state(
            current.id,
            "queued",
            selected=current.selected,
            expected_bytes=max(0, size),
        )
        await self.repository.clear_recovery_refresh_inflight(claim, decision_id)
        await self.repository.clear_recovery_quiescence(claim)
        return True, "refresh_applied"

    async def _reconcile_current(
        self,
        claim: RecoveryClaim,
        artifact: Artifact,
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

        candidate = self._candidate(current)
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

        if candidate is not None and not self._candidate_provider_enabled(candidate):
            wait_error = self._provider_wait_error(candidate)
            step = await self._decision_step(
                claim,
                current,
                wait_error,
                count_failure=False,
                force_provider_not_ready=True,
                outcome="provider_wait",
            )
            return current, wait_error, observed, step, None

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
                count_failure=False,
                force_storage_not_ready=True,
                outcome="storage_wait",
            )
            return current, storage_error, observed, step, None

        retirement_reason = None
        if current.execution is None:
            return current, error, observed, None, retirement_reason

        executor = self.registry.executors.get(current.execution.executor_id)
        if executor is None:
            wait_error = self._executor_wait_error(current.execution.executor_id)
            step = await self._decision_step(
                claim,
                current,
                wait_error,
                count_failure=False,
                force_executor_not_ready=True,
                outcome="executor_wait",
            )
            return current, wait_error, observed, step, None

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

        if observed.state == ExecutionState.UNKNOWN:
            await self.repository.execution(observed)
            reconcile_error = observed.error or NormalizedError(
                Domain.RECONCILIATION,
                Category.RECONCILIATION_FAILED,
                Stage.RECONCILIATION,
                retryability=Retryability.BACKOFF,
                origin=Origin.CORE,
                integration_id=current.execution.executor_id,
            )
            step = await self._decision_step(
                claim,
                current,
                reconcile_error,
                observed=observed,
                count_failure=False,
                outcome="executor_truth_unknown",
            )
            return current, reconcile_error, observed, step, None

        if observed.resumable:
            if isinstance(executor, PauseResume):
                observed = await self._converge_execution(current, executor, observed)
            else:
                await self.repository.execution(observed)
            await self.repository.clear_recovery_quiescence(claim)
            return current, error, observed, _Step(
                True, True, RecoveryAction.RECONCILE.value,
                "existing_execution_resumable", "existing_execution_reused",
            ), None

        await self.repository.execution(observed)
        current = await self._current_artifact(current.transfer_id, current.id)
        if current is None:
            return None, error, observed, _Step(
                True, False, RecoveryAction.RECONCILE.value,
                "artifact_disappeared", "terminal_or_missing",
            ), None
        retirement_reason = f"execution_{observed.state.value}"
        if observed.state == ExecutionState.SUCCEEDED:
            return current, error, observed, _Step(
                True, True, RecoveryAction.RECONCILE.value,
                "execution_completed", "execution_completed",
                retirement_reason=retirement_reason,
            ), retirement_reason
        if observed.state in {ExecutionState.ABSENT, ExecutionState.CANCELLED}:
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
        return current, error, observed, None, retirement_reason

    async def _plan_after_reconcile(
        self,
        claim: RecoveryClaim,
        artifact: Artifact,
        trigger: RecoveryTrigger,
        error: NormalizedError | None,
        observed: ExecutionObservation | None,
        retirement_reason: str | None,
    ) -> _Step:
        current = await self._current_artifact(artifact.transfer_id, artifact.id)
        if current is None:
            return _Step(True, False, RecoveryAction.RECONCILE.value,
                         "artifact_disappeared", "terminal_or_missing")
        stored = await self.repository.recovery_context(current.id)
        if stored.get("quiescence_reason") == "recovery_exhausted" and trigger != RecoveryTrigger.USER_RETRY:
            return _Step(True, True, RecoveryAction.WAIT_FOR_OPERATOR.value,
                         "recovery_exhausted", "operator_wait",
                         retirement_reason=retirement_reason)

        blocked_retry_at = max(
            float(stored.get("blocked_retry_at") or 0),
            float(current.retry_at or 0)
            if stored.get("quiescence_reason") == "retry_backoff" else 0.0,
        )
        if blocked_retry_at > self.clock():
            await self.repository.transition_recovery(
                current.id,
                "recovery_wait",
                error=current.error,
                retry_at=blocked_retry_at,
                quiescence_reason="retry_backoff",
                wake_condition=f"retry_at:{blocked_retry_at}",
            )
            return _Step(True, True, RecoveryAction.BACKOFF.value,
                         "backoff_still_active", "retry_backoff",
                         retirement_reason=retirement_reason)

        if current.state == "refresh_pending":
            refreshed, refresh_reason = await self._refresh_claimed(claim, current)
            if not refreshed and refresh_reason == "refresh_outcome_unknown":
                await self._park_existing_execution(
                    claim, current, reason="recovery_exhausted", wake="operator_retry",
                )
                return _Step(True, False, RecoveryAction.WAIT_FOR_OPERATOR.value,
                             refresh_reason, refresh_reason,
                             retirement_reason=retirement_reason)
            return _Step(True, refreshed, RecoveryAction.REFRESH_CANDIDATE.value,
                         refresh_reason, "refresh_applied" if refreshed else refresh_reason,
                         candidate_changed=refreshed,
                         retirement_reason=retirement_reason)

        if error is not None:
            return await self._decision_step(
                claim,
                current,
                error,
                observed=observed,
                count_failure=self._counts_recovery_failure(error),
                retirement_reason=retirement_reason,
            )

        if stored.get("quiescence_reason") in {
            "provider_disabled", "executor_unavailable", "storage_unavailable", "retry_backoff",
        }:
            await self.repository.clear_recovery_quiescence(claim)

        if not current.candidates:
            await self.repository.artifact_state(current.id, "unresolved", release=True)
            await self.repository.retry_requests(
                current.transfer_id,
                request_id=current.request_id,
                reset_budget=trigger == RecoveryTrigger.USER_RETRY,
            )
            return _Step(True, True, RecoveryAction.RECONCILE.value,
                         "request_reresolution_required", "request_requeued",
                         retirement_reason=retirement_reason)

        candidate = self._candidate(current)
        if candidate is not None and candidate.expires_at is not None and candidate.expires_at <= self.clock():
            expiry = NormalizedError(
                Domain.RESOLUTION,
                Category.CANDIDATE_EXPIRED,
                Stage.CANDIDATE_PREPARATION,
                retryability=Retryability.AFTER_RERESOLUTION,
                origin=Origin.CORE,
                integration_id=candidate.provider_id,
            )
            return await self._decision_step(
                claim,
                current,
                expiry,
                count_failure=True,
                retirement_reason=retirement_reason,
            )

        history = await self.repository.executions(current.transfer_id)
        reconstructed = any(item.artifact_id == current.id for item in history)
        await self.repository.artifact_state(current.id, "queued", error=None, retry_at=0)
        current = await self._current_artifact(current.transfer_id, current.id)
        if current is None:
            return _Step(True, False, RecoveryAction.RECONCILE.value,
                         "artifact_disappeared", "terminal_or_missing")
        dispatched = await self._dispatch_claimed(claim, current)
        return _Step(
            True,
            dispatched,
            RecoveryAction.RECONCILE.value,
            "existing_candidate_reused",
            "execution_reused_or_dispatched" if dispatched else "dispatch_blocked",
            reconstruction_reason="no_current_execution_after_reconciliation" if reconstructed else None,
            retirement_reason=retirement_reason,
        )

    async def recover_artifact(
        self,
        artifact: Artifact | int,
        *,
        trigger: RecoveryTrigger,
        error: NormalizedError | None = None,
        observed: ExecutionObservation | None = None,
    ) -> bool:
        """Claim, reconcile, plan/apply, persist—one path for every Phase-3 trigger."""
        trigger = RecoveryTrigger(trigger)
        artifact = await self._artifact_ref(artifact)
        if artifact is None:
            return False
        claim = await self.repository.claim_recovery(
            artifact.id,
            trigger,
            self.clock(),
            lease_seconds=max(300.0, float(self.policy.max_retry_delay)),
        )
        if claim is None:
            return False

        step = _Step(False, False, RecoveryAction.RECONCILE.value,
                     "reconciled_current_state", "coalesced")
        try:
            current, error, observed, handled, retirement = await self._reconcile_current(
                claim, artifact, trigger, error, observed,
            )
            step = handled or await self._plan_after_reconcile(
                claim, current or artifact, trigger, error, observed, retirement,
            )
            return step.applied
        except Exception as exc:
            step = _Step(True, False, RecoveryAction.RECONCILE.value,
                         f"recovery_application_error:{type(exc).__name__}", "application_error")
            return False
        finally:
            if await self.repository.recovery_claim_current(claim):
                await self.repository.record_phase3_application(
                    claim,
                    action=step.action,
                    reason=step.reason,
                    reconstruction_reason=step.reconstruction_reason,
                    retirement_reason=step.retirement_reason,
                    partial_preserved=True,
                )
                await self._finish_claim(
                    claim,
                    action=step.action,
                    reason=step.reason,
                    outcome=step.outcome,
                    artifact=artifact,
                    candidate_changed=step.candidate_changed,
                    reconstruction_reason=step.reconstruction_reason,
                    retirement_reason=step.retirement_reason,
                )

    async def retry(self, transfer_id: int, *, reacquire=False):
        """Coalesce repeated Retry after the first invocation already made work productive."""
        if reacquire:
            return await super().retry(transfer_id, reacquire=True)
        transfer = await self.repository.get(transfer_id)
        if transfer is None:
            raise KeyError(transfer_id)
        artifacts = await self.repository.artifacts(transfer_id)
        if artifacts:
            actionable = False
            for artifact in artifacts:
                context = await self.repository.recovery_context(artifact.id)
                if artifact.state in {
                    "error", "recovery_wait", "lost", "unresolved", "refresh_pending",
                } or context.get("quiescence_reason") in {
                    "recovery_exhausted", "provider_disabled", "executor_unavailable",
                    "storage_unavailable", "retry_backoff",
                }:
                    actionable = True
                    break
            if not actionable:
                return True
        return await super().retry(transfer_id, reacquire=False)
