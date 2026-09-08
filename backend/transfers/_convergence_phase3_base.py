"""Unified Phase-3 transfer recovery execution and lifecycle convergence.

This is the production recovery-execution owner. It extends the qualified
Phase-2 engine, but every Phase-3 recovery trigger enters ``recover_artifact``.
The universal ``TransferPolicy.recover`` remains the only recovery-policy owner.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

from transfers.contracts import CandidateRefresh, PauseResume
from transfers.engine import TransferEngine as _QualifiedTransferEngine
from transfers.errors import (
    Category, Domain, NormalizedError, Origin, Retryability, Stage, TransferError,
    unknown_failure,
)
from transfers.mirrors import reported_sizes_compatible
from transfers.models import (
    Artifact, ExecutionObservation, ExecutionState, OutcomeKind, ResolutionAttempt,
    ResolutionResult, ResourceState, TransferOutcome, TransferState,
)
from transfers.policy import RecoveryAction, failure_signature
from transfers.recovery_execution import RecoveryClaim, RecoveryTrigger, trigger_authority


_TERMINAL_TRANSFER_STATES = frozenset({
    TransferState.COMPLETED,
    TransferState.CONSOLIDATED,
    TransferState.CANCELLED,
    TransferState.DELETED,
})
_INFRASTRUCTURE_CATEGORIES = frozenset({
    Category.PROVIDER_UNAVAILABLE,
    Category.EXECUTOR_UNAVAILABLE,
    Category.ORPHANED_RESOURCE,
    Category.DISK_FULL,
    Category.LOCAL_RESOURCE_EXHAUSTED,
    Category.APPLICATION_STORAGE_FULL,
    Category.APPLICATION_STORAGE_READ_ONLY,
    Category.APPLICATION_STORAGE_UNAVAILABLE,
    Category.DOWNLOAD_STORAGE_FULL,
    Category.DOWNLOAD_STORAGE_READ_ONLY,
    Category.DOWNLOAD_STORAGE_UNAVAILABLE,
    Category.PATH_UNAVAILABLE,
})


class TransferEngine(_QualifiedTransferEngine):
    """Qualified engine plus one canonical recovery-convergence entry point."""

    async def initialize(self):
        result = await super().initialize()
        existing = set()
        for transfer in await self.repository.active():
            for artifact in await self.repository.artifacts(transfer.id):
                existing.add(artifact.id)
        self._startup_recovery_artifacts = existing
        return result

    @staticmethod
    def _candidate(artifact: Artifact):
        if not artifact.candidates:
            return None
        if artifact.selected < 0 or artifact.selected >= len(artifact.candidates):
            return None
        return artifact.candidates[artifact.selected]

    def _provider_wait_error(self, candidate) -> NormalizedError:
        return NormalizedError(
            Domain.PROVIDER,
            Category.PROVIDER_UNAVAILABLE,
            Stage.RECONCILIATION,
            retryability=Retryability.BACKOFF,
            origin=Origin.CORE,
            integration_id=candidate.provider_id if candidate else "",
        )

    @staticmethod
    def _executor_wait_error(executor_id: str = "") -> NormalizedError:
        return NormalizedError(
            Domain.EXECUTOR,
            Category.EXECUTOR_UNAVAILABLE,
            Stage.RECONCILIATION,
            retryability=Retryability.AFTER_RESOURCE_CHANGE,
            origin=Origin.CORE,
            integration_id=executor_id,
        )

    @staticmethod
    def _orphaned_error(executor_id: str = "") -> NormalizedError:
        return NormalizedError(
            Domain.RECONCILIATION,
            Category.ORPHANED_RESOURCE,
            Stage.RECONCILIATION,
            retryability=Retryability.BACKOFF,
            origin=Origin.CORE,
            integration_id=executor_id,
        )

    @staticmethod
    def _failure_identity(
        artifact: Artifact,
        error: NormalizedError,
        observed: ExecutionObservation | None = None,
    ) -> str:
        attempt = artifact.execution.attempt_id if artifact.execution else "no-execution"
        completed = observed.progress.completed_bytes if observed is not None else 0
        return f"{attempt}:{completed}:{failure_signature(error)}"

    @staticmethod
    def _counts_recovery_failure(error: NormalizedError) -> bool:
        if error.domain == Domain.LOCAL_RESOURCE:
            return False
        if error.category in _INFRASTRUCTURE_CATEGORIES:
            return False
        return True

    async def _finish_claim(
        self,
        claim: RecoveryClaim,
        *,
        action: str,
        reason: str,
        outcome: str,
        artifact: Artifact | None = None,
        candidate_changed: bool = False,
        reconstruction_reason: str | None = None,
        retirement_reason: str | None = None,
    ) -> bool:
        execution_attempt = None
        execution_identity = None
        candidate_id = None
        if artifact is not None:
            current = await self._current_artifact(artifact.transfer_id, artifact.id)
            if current is not None:
                artifact = current
            if artifact.execution is not None:
                execution_attempt = artifact.execution.attempt_id
                execution_identity = artifact.execution.executor_id
            candidate = self._candidate(artifact)
            candidate_id = candidate.id if candidate is not None else None
        return await self.repository.finish_recovery_claim(
            claim,
            action=action,
            reason=reason,
            outcome=outcome,
            execution_attempt=execution_attempt,
            execution_identity=execution_identity,
            candidate_id=candidate_id,
            candidate_changed=candidate_changed,
            reconstruction_reason=reconstruction_reason,
            retirement_reason=retirement_reason,
        )

    async def _park_existing_execution(
        self,
        claim: RecoveryClaim,
        artifact: Artifact,
        *,
        reason: str,
        wake: str,
        retry_at: float = 0.0,
    ) -> bool:
        current = await self._current_artifact(artifact.transfer_id, artifact.id)
        if current is None:
            return False
        if current.execution is not None:
            executor = self.registry.executors.get(current.execution.executor_id)
            observed = None
            if executor is not None:
                if not await self.repository.recovery_claim_current(claim, now=self.clock()):
                    return False
                try:
                    observed = await executor.observe(current.execution)
                except Exception:
                    observed = None
            if observed is not None and observed.handle == current.execution:
                if observed.resumable and isinstance(executor, PauseResume):
                    if observed.state != ExecutionState.PAUSED:
                        if not await self.repository.recovery_claim_current(claim, now=self.clock()):
                            return False
                        observed = await executor.pause(current.execution)
                    if observed is not None and observed.handle == current.execution:
                        await self.repository.execution(observed)
                    await self.repository.record_recovery_quiescence(
                        claim,
                        reason=reason,
                        wake_condition=wake,
                        blocked_retry_at=max(
                            float(retry_at or 0),
                            float((await self.repository.recovery_context(current.id)).get("blocked_retry_at") or 0),
                        ),
                    )
                    return True
                if observed.state in {
                    ExecutionState.FAILED,
                    ExecutionState.ABSENT,
                    ExecutionState.CANCELLED,
                    ExecutionState.SUCCEEDED,
                }:
                    await self.repository.execution(observed)
                    current = await self._current_artifact(current.transfer_id, current.id)
                    if current is None:
                        return False
            elif executor is None:
                await self.repository.record_recovery_quiescence(
                    claim,
                    reason=reason,
                    wake_condition=wake,
                    blocked_retry_at=max(
                        float(retry_at or 0),
                        float((await self.repository.recovery_context(current.id)).get("blocked_retry_at") or 0),
                    ),
                )
                return True
        state = "error" if reason == "recovery_exhausted" else "recovery_wait"
        applied = await self.repository.transition_recovery(
            current.id,
            state,
            error=current.error,
            retry_at=retry_at,
            quiescence_reason=reason,
            wake_condition=wake,
        )
        if applied and retry_at:
            await self.repository.record_recovery_quiescence(
                claim,
                reason=reason,
                wake_condition=wake,
                blocked_retry_at=retry_at,
            )
        return applied

    async def _apply_recovery_decision(
        self,
        claim: RecoveryClaim,
        artifact: Artifact,
        error: NormalizedError,
        decision,
        *,
        decision_id: str,
        next_index: int | None,
    ) -> bool:
        current = await self._current_artifact(artifact.transfer_id, artifact.id)
        if current is None:
            return False

        if decision.action == RecoveryAction.FAIL_PERMANENTLY:
            return await self.repository.transition_recovery(
                current.id, "error", error=error, retry_at=0, clear_quiescence=True,
            )

        if decision.action == RecoveryAction.WAIT_FOR_PROVIDER:
            candidate = self._candidate(current)
            provider_id = candidate.provider_id if candidate else ""
            return await self._park_existing_execution(
                claim,
                current,
                reason="provider_disabled",
                wake=f"provider_enabled:{provider_id}" if provider_id else "provider_enabled",
                retry_at=current.retry_at,
            )

        if decision.action == RecoveryAction.WAIT_FOR_RESOURCE:
            reason = decision.quiescence_reason or "executor_unavailable"
            wake = decision.wake_condition or (
                "executor_available"
                if reason == "executor_unavailable"
                else "storage_healthy:local_resource"
            )
            return await self._park_existing_execution(
                claim, current, reason=reason, wake=wake, retry_at=current.retry_at,
            )

        if decision.action == RecoveryAction.WAIT_FOR_OPERATOR:
            return await self._park_existing_execution(
                claim,
                current,
                reason=decision.quiescence_reason or "recovery_exhausted",
                wake=decision.wake_condition or "operator_retry",
                retry_at=current.retry_at,
            )

        if decision.action in {RecoveryAction.RECONCILE, RecoveryAction.BACKOFF}:
            retry_at = decision.retry_at if decision.retry_at is not None else self.clock()
            return await self.repository.transition_recovery(
                current.id,
                "recovery_wait" if retry_at > self.clock() else "queued",
                error=error,
                retry_at=retry_at,
                quiescence_reason=(decision.quiescence_reason or "retry_backoff")
                if retry_at > self.clock() else None,
                wake_condition=(decision.wake_condition or f"retry_at:{retry_at}")
                if retry_at > self.clock() else None,
                clear_quiescence=retry_at <= self.clock(),
            )

        if decision.action == RecoveryAction.REFRESH_CANDIDATE:
            if not await self.repository.reserve_recovery_refresh(
                claim,
                decision_id,
                limit=max(1, self.policy.refreshes_per_recovery_epoch),
            ):
                if next_index is not None:
                    return await self._apply_recovery_decision(
                        claim,
                        current,
                        error,
                        replace(decision, action=RecoveryAction.TRY_ALTERNATE_CANDIDATE),
                        decision_id=decision_id,
                        next_index=next_index,
                    )
                return await self._park_existing_execution(
                    claim,
                    current,
                    reason="recovery_exhausted",
                    wake="operator_retry",
                )
            return await self.repository.transition_recovery(
                current.id,
                "refresh_pending",
                error=error,
                retry_at=decision.retry_at or self.clock(),
                clear_quiescence=True,
            )

        if decision.action == RecoveryAction.TRY_ALTERNATE_CANDIDATE:
            if next_index is None or next_index <= current.selected or next_index >= len(current.candidates):
                return await self._park_existing_execution(
                    claim,
                    current,
                    reason="recovery_exhausted",
                    wake="operator_retry",
                )
            replacement = current.candidates[next_index]
            if (
                current.expected_bytes > 0
                and replacement.expected_bytes > 0
                and not reported_sizes_compatible(current.expected_bytes, replacement.expected_bytes)
            ):
                return await self._park_existing_execution(
                    claim,
                    current,
                    reason="recovery_exhausted",
                    wake="operator_retry",
                )
            accepted_size = current.expected_bytes if current.expected_bytes > 0 else replacement.expected_bytes
            return await self.repository.transition_recovery(
                current.id,
                "queued",
                error=error,
                retry_at=decision.retry_at or self.clock(),
                selected=next_index,
                expected_bytes=max(0, accepted_size),
                candidate_switched=True,
                clear_quiescence=True,
            )

        retry_at = decision.retry_at if decision.retry_at is not None else self.clock()
        if retry_at > self.clock():
            return await self.repository.transition_recovery(
                current.id,
                "recovery_wait",
                error=error,
                retry_at=retry_at,
                quiescence_reason=decision.quiescence_reason or "retry_backoff",
                wake_condition=decision.wake_condition or f"retry_at:{retry_at}",
            )
        return await self.repository.transition_recovery(
            current.id,
            "queued",
            error=error,
            retry_at=retry_at,
            clear_quiescence=True,
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
        error = self.policy.compatibility(error)
        failure_identity = self._failure_identity(artifact, error, observed)
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

    async def _dispatch_claimed(self, claim: RecoveryClaim, artifact: Artifact) -> bool:
        if not await self.repository.recovery_claim_current(claim, now=self.clock()):
            return False
        transfer = await self.repository.get(artifact.transfer_id)
        current = await self._current_artifact(artifact.transfer_id, artifact.id)
        if transfer is None or current is None or transfer.paused or await self.repository.globally_paused():
            return False
        if current.execution is not None or current.state == "completed":
            return True
        candidate = self._candidate(current)
        if candidate is None:
            await self.repository.artifact_state(current.id, "unresolved", release=True)
            await self.repository.retry_requests(current.transfer_id, request_id=current.request_id)
            return True
        if not self._candidate_provider_enabled(candidate):
            return False
        if not self.registry.eligible_executors(candidate):
            return False
        if candidate.expires_at is not None and candidate.expires_at <= self.clock():
            return False
        await super()._dispatch(current)
        return True

    async def _refresh_claimed(self, claim: RecoveryClaim, artifact: Artifact) -> tuple[bool, str]:
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
            if state.get("state") == "succeeded":
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
            return False, "refresh_outcome_unknown"

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

    async def recover_artifact(
        self,
        artifact: Artifact | int,
        *,
        trigger: RecoveryTrigger,
        error: NormalizedError | None = None,
        observed: ExecutionObservation | None = None,
    ) -> bool:
        """Canonical recovery execution entry point for all Phase-3 triggers."""
        trigger = RecoveryTrigger(trigger)
        if isinstance(artifact, int):
            found = None
            for transfer in await self.repository.active():
                found = next(
                    (item for item in await self.repository.artifacts(transfer.id) if item.id == artifact),
                    None,
                )
                if found is not None:
                    break
            if found is None:
                return False
            artifact = found

        claim = await self.repository.claim_recovery(
            artifact.id,
            trigger,
            self.clock(),
            lease_seconds=max(300.0, float(self.policy.max_retry_delay)),
        )
        if claim is None:
            return False

        action = RecoveryAction.RECONCILE.value
        reason = "reconciled_current_state"
        outcome = "coalesced"
        candidate_changed = False
        try:
            current = await self._current_artifact(artifact.transfer_id, artifact.id)
            transfer = await self.repository.get(artifact.transfer_id)
            if current is None or transfer is None or transfer.state in _TERMINAL_TRANSFER_STATES:
                outcome = "terminal_or_missing"
                return False

            authority = trigger_authority(trigger)
            if authority.reset_exhaustion:
                await self.repository.reset_retry_budget(current.id)
                current = await self._current_artifact(current.transfer_id, current.id)
                if current is None:
                    return False

            if transfer.paused and trigger != RecoveryTrigger.RESUME:
                if current.execution is not None:
                    executor = self.registry.executors.get(current.execution.executor_id)
                    if executor is not None and isinstance(executor, PauseResume):
                        await self._converge_execution(current, executor)
                outcome = "paused"
                return True

            candidate = self._candidate(current)
            if current.state == "input_required":
                input_error = error or NormalizedError(
                    Domain.REQUEST,
                    Category.CREDENTIAL_MISSING,
                    Stage.QUEUE,
                    retryability=Retryability.AFTER_REAUTH,
                    origin=Origin.CORE,
                )
                applied, action, reason, candidate_changed = await self._decide_and_apply(
                    claim, current, input_error, count_failure=False,
                )
                outcome = "input_required" if applied else "not_applied"
                return applied

            if candidate is not None and not self._candidate_provider_enabled(candidate):
                applied, action, reason, candidate_changed = await self._decide_and_apply(
                    claim,
                    current,
                    self._provider_wait_error(candidate),
                    count_failure=False,
                    force_provider_not_ready=True,
                )
                outcome = "provider_wait" if applied else "not_applied"
                return applied

            if not self.dispatch_permitted:
                storage_error = error or NormalizedError(
                    Domain.LOCAL_RESOURCE,
                    Category.DOWNLOAD_STORAGE_UNAVAILABLE,
                    Stage.EXECUTION,
                    retryability=Retryability.AFTER_RESOURCE_CHANGE,
                    origin=Origin.LOCAL_SYSTEM,
                )
                applied, action, reason, candidate_changed = await self._decide_and_apply(
                    claim,
                    current,
                    storage_error,
                    count_failure=False,
                    force_storage_not_ready=True,
                )
                outcome = "storage_wait" if applied else "not_applied"
                return applied

            if current.execution is not None:
                executor = self.registry.executors.get(current.execution.executor_id)
                if executor is None:
                    applied, action, reason, candidate_changed = await self._decide_and_apply(
                        claim,
                        current,
                        self._executor_wait_error(current.execution.executor_id),
                        count_failure=False,
                        force_executor_not_ready=True,
                    )
                    outcome = "executor_wait" if applied else "not_applied"
                    return applied
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
                    outcome = "executor_truth_unknown"
                    return True
                if observed.resumable:
                    if isinstance(executor, PauseResume):
                        observed = await self._converge_execution(current, executor, observed)
                    elif observed is not None:
                        await self.repository.execution(observed)
                    await self.repository.clear_recovery_quiescence(claim)
                    outcome = "existing_execution_reused"
                    reason = "existing_execution_resumable"
                    return True
                await self.repository.execution(observed)
                current = await self._current_artifact(current.transfer_id, current.id)
                if current is None:
                    return False
                if observed.state == ExecutionState.SUCCEEDED:
                    outcome = "execution_completed"
                    return True
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

            current = await self._current_artifact(artifact.transfer_id, artifact.id)
            if current is None:
                return False
            stored = await self.repository.recovery_context(current.id)
            if stored.get("quiescence_reason") == "recovery_exhausted" and trigger != RecoveryTrigger.USER_RETRY:
                action = RecoveryAction.WAIT_FOR_OPERATOR.value
                reason = "recovery_exhausted"
                outcome = "operator_wait"
                return True

            blocked_retry_at = max(
                float(stored.get("blocked_retry_at") or 0),
                float(current.retry_at or 0) if stored.get("quiescence_reason") == "retry_backoff" else 0.0,
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
                action = RecoveryAction.BACKOFF.value
                reason = "backoff_still_active"
                outcome = "retry_backoff"
                return True

            if current.state == "refresh_pending":
                refreshed, refresh_reason = await self._refresh_claimed(claim, current)
                action = RecoveryAction.REFRESH_CANDIDATE.value
                reason = refresh_reason
                outcome = "refresh_applied" if refreshed else refresh_reason
                candidate_changed = refreshed
                if not refreshed and refresh_reason == "refresh_outcome_unknown":
                    await self._park_existing_execution(
                        claim,
                        current,
                        reason="recovery_exhausted",
                        wake="operator_retry",
                    )
                    action = RecoveryAction.WAIT_FOR_OPERATOR.value
                return refreshed

            if error is not None:
                applied, action, reason, candidate_changed = await self._decide_and_apply(
                    claim,
                    current,
                    error,
                    observed=observed,
                    count_failure=self._counts_recovery_failure(error),
                )
                outcome = "decision_applied" if applied else "not_applied"
                return applied

            stored = await self.repository.recovery_context(current.id)
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
                action = RecoveryAction.RECONCILE.value
                reason = "request_reresolution_required"
                outcome = "request_requeued"
                return True

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
                applied, action, reason, candidate_changed = await self._decide_and_apply(
                    claim, current, expiry, count_failure=True,
                )
                outcome = "decision_applied" if applied else "not_applied"
                return applied

            await self.repository.artifact_state(current.id, "queued", error=None, retry_at=0)
            current = await self._current_artifact(current.transfer_id, current.id)
            if current is None:
                return False
            dispatched = await self._dispatch_claimed(claim, current)
            action = RecoveryAction.RECONCILE.value
            reason = "existing_candidate_reused"
            outcome = "execution_reused_or_dispatched" if dispatched else "dispatch_blocked"
            return dispatched
        except Exception as exc:
            reason = f"recovery_application_error:{type(exc).__name__}"
            outcome = "application_error"
            return False
        finally:
            if await self.repository.recovery_claim_current(claim):
                await self._finish_claim(
                    claim,
                    action=action,
                    reason=reason,
                    outcome=outcome,
                    artifact=artifact,
                    candidate_changed=candidate_changed,
                )

    async def _recover_artifact(self, artifact: Artifact, error: NormalizedError):
        return await self.recover_artifact(
            artifact,
            trigger=RecoveryTrigger.AUTO_RETRY,
            error=self.policy.compatibility(error),
        )

    async def _schedule_refresh(self, artifact: Artifact, error: NormalizedError):
        return await self.recover_artifact(
            artifact,
            trigger=RecoveryTrigger.AUTO_RETRY,
            error=self.policy.compatibility(error),
        )

    async def _refresh(self, artifact: Artifact):
        return await self.recover_artifact(artifact, trigger=RecoveryTrigger.AUTO_RETRY)

    async def _dispatch(self, artifact: Artifact):
        candidate = self._candidate(artifact)
        if candidate is not None and candidate.expires_at is not None and candidate.expires_at <= self.clock():
            error = NormalizedError(
                Domain.RESOLUTION,
                Category.CANDIDATE_EXPIRED,
                Stage.CANDIDATE_PREPARATION,
                retryability=Retryability.AFTER_RERESOLUTION,
                origin=Origin.CORE,
                integration_id=candidate.provider_id,
            )
            return await self.recover_artifact(
                artifact,
                trigger=RecoveryTrigger.AUTO_RETRY,
                error=error,
            )
        return await super()._dispatch(artifact)

    async def _wake_quiescent_recoveries(self):
        for transfer in await self.repository.active():
            for artifact in await self.repository.artifacts(transfer.id):
                context = await self.repository.recovery_context(artifact.id)
                reason = context.get("quiescence_reason")
                if not reason:
                    continue
                if transfer.paused or reason in {"input_required", "recovery_exhausted"}:
                    continue
                candidate = self._candidate(artifact)
                provider_ready = self._candidate_provider_enabled(candidate)
                if candidate is not None and not provider_ready:
                    if reason != "provider_disabled":
                        await self.recover_artifact(artifact, trigger=RecoveryTrigger.AUTO_RETRY)
                    continue
                if reason == "provider_disabled":
                    await self.recover_artifact(
                        artifact, trigger=RecoveryTrigger.PROVIDER_RECOVERY,
                    )
                elif reason == "executor_unavailable":
                    if (
                        artifact.execution is not None
                        and self.registry.executors.get(artifact.execution.executor_id) is not None
                    ) or (
                        artifact.execution is None
                        and candidate is not None
                        and self.registry.eligible_executors(candidate)
                    ):
                        await self.recover_artifact(
                            artifact, trigger=RecoveryTrigger.EXECUTOR_RECOVERY,
                        )
                elif reason == "storage_unavailable" and self.dispatch_permitted:
                    await self.recover_artifact(artifact, trigger=RecoveryTrigger.AUTO_RETRY)
                elif reason == "retry_backoff" and artifact.retry_at <= self.clock():
                    await self.recover_artifact(artifact, trigger=RecoveryTrigger.AUTO_RETRY)

    async def reconcile_executions(self):
        startup = set(getattr(self, "_startup_recovery_artifacts", set()))
        if startup:
            self._startup_recovery_artifacts = set()
            for transfer in await self.repository.active():
                for artifact in await self.repository.artifacts(transfer.id):
                    if artifact.id in startup:
                        await self.recover_artifact(
                            artifact,
                            trigger=RecoveryTrigger.STARTUP_RECONCILE,
                        )
        return await super().reconcile_executions()

    async def _process_executions(
        self,
        transfer_id,
        artifacts,
        observations=None,
        *,
        dispatch_allowed=True,
    ):
        observations = observations or {}
        for artifact in artifacts:
            candidate = self._candidate(artifact)
            if artifact.execution is not None:
                if candidate is not None and not self._candidate_provider_enabled(candidate):
                    await self.recover_artifact(
                        artifact,
                        trigger=RecoveryTrigger.AUTO_RETRY,
                        error=self._provider_wait_error(candidate),
                    )
                    continue
                if self.registry.executors.get(artifact.execution.executor_id) is None:
                    await self.recover_artifact(
                        artifact,
                        trigger=RecoveryTrigger.AUTO_RETRY,
                        error=self._executor_wait_error(artifact.execution.executor_id),
                    )
                    continue
            await super()._process_executions(
                transfer_id,
                (artifact,),
                observations,
                dispatch_allowed=dispatch_allowed,
            )

    async def pause(self, transfer_id: int):
        transfer = await self.repository.get(transfer_id)
        if transfer is None:
            raise KeyError(transfer_id)
        await self.repository.set_pause_and_fence(transfer_id, True)
        errors = []
        for artifact in await self.repository.artifacts(transfer_id):
            if artifact.execution is None:
                continue
            executor = self.registry.executors.get(artifact.execution.executor_id)
            if not isinstance(executor, PauseResume):
                errors.append(self._error(
                    Category.UNSUPPORTED_CAPABILITY,
                    Stage.EXECUTION,
                    domain=Domain.REQUEST,
                    retryability=Retryability.NEVER,
                ))
                continue
            observed = await self._converge_execution(artifact, executor)
            if observed and observed.error:
                errors.append(observed.error)
        await self._aggregate(transfer_id)
        return tuple(errors)

    async def resume(self, transfer_id: int):
        transfer = await self.repository.get(transfer_id)
        if transfer is None:
            raise KeyError(transfer_id)
        if await self.repository.globally_paused():
            for other in await self.repository.active():
                if other.id != transfer_id:
                    await self.repository.set_pause_and_fence(other.id, True)
            await self.repository.global_pause(False)
        await self.repository.set_pause_and_fence(transfer_id, False)
        transfer = await self.repository.get(transfer_id)
        if transfer and transfer.state == TransferState.PAUSED:
            await self.repository.state(transfer_id, TransferState.QUEUED)
        errors = []
        for artifact in await self.repository.artifacts(transfer_id):
            if artifact.state == "completed":
                continue
            if not await self.recover_artifact(artifact, trigger=RecoveryTrigger.RESUME):
                current = await self._current_artifact(transfer_id, artifact.id)
                if current and current.error:
                    errors.append(current.error)
        await self._aggregate(transfer_id)
        return tuple(errors)

    async def pause_all(self):
        await self.repository.global_pause(True)
        results = {}
        for transfer in await self.repository.active():
            results[transfer.id] = await self.pause(transfer.id)
        return results

    async def resume_all(self):
        await self.repository.global_pause(False)
        results = {}
        for transfer in await self.repository.active():
            await self.repository.set_pause_and_fence(transfer.id, False)
            errors = []
            for artifact in await self.repository.artifacts(transfer.id):
                if artifact.state == "completed":
                    continue
                if not await self.recover_artifact(artifact, trigger=RecoveryTrigger.RESUME):
                    current = await self._current_artifact(transfer.id, artifact.id)
                    if current and current.error:
                        errors.append(current.error)
            await self._aggregate(transfer.id)
            results[transfer.id] = tuple(errors)
        return results

    async def retry(self, transfer_id: int, *, reacquire=False):
        if reacquire:
            return await super().retry(transfer_id, reacquire=True)

        lock = self._transfer_locks.setdefault(transfer_id, asyncio.Lock())
        async with lock:
            transfer = await self.repository.get(transfer_id)
            if transfer is None:
                raise KeyError(transfer_id)
            if transfer.state in {TransferState.CONSOLIDATED, TransferState.DELETED}:
                return False
            if await self.challenges.current(transfer_id):
                return False
            if any(
                pending for _resource, _state, pending
                in await self.repository.resources(transfer_id)
            ):
                return False
            if not await self.repository.reset_postprocessing(transfer_id):
                return False
            if not await self.repository.state(
                transfer_id,
                TransferState.QUEUED,
                operator=True,
                expected_epoch=transfer.epoch,
            ):
                return False

            artifacts = await self.repository.artifacts(transfer_id)
            if not artifacts:
                await self.repository.retry_requests(transfer_id, reset_budget=True)
                return True

            ok = True
            for artifact in artifacts:
                if artifact.state == "completed":
                    continue
                applied = await self.recover_artifact(
                    artifact,
                    trigger=RecoveryTrigger.USER_RETRY,
                )
                ok = ok and applied
            await self.repository.retry_requests(transfer_id, reset_budget=True)
            await self._aggregate(transfer_id)
            return ok
