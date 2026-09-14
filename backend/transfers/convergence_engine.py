"""Canonical recovery-execution and lifecycle-convergence coordinator.

Single production owner for the durable-claim recovery coordinator and
trigger adapters: every recovery trigger (``AUTO_RETRY``, ``USER_RETRY``,
``RESUME``, ``STARTUP_RECONCILE``, ``PROVIDER_RECOVERY``,
``EXECUTOR_RECOVERY``, ``USER_CANDIDATE_SWITCH``) enters ``recover_artifact``
or ``activate_candidate_command``, both fenced by the same exclusive
``transfers.recovery_repository.TransferRepository.claim_recovery`` system.
The universal ``TransferPolicy.recover`` remains the only recovery-policy
owner; this class owns application/execution of that decision, not policy
itself.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from transfers.candidate_activation import ActivationResult, activate_candidate
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
from transfers.policy import RecoveryAction, TERMINAL_TRANSFER_STATES, failure_signature
from transfers.recovery_execution import RecoveryClaim, RecoveryTrigger, trigger_authority


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


class TransferEngine(_QualifiedTransferEngine):
    """Single production owner for every recovery trigger and the durable
    recovery-execution/lifecycle-convergence coordinator (DP 1.0.12 leveling
    remediation, ARCH-001)."""

    # ------------------------------------------------------------------
    # Startup / static classification helpers
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Claim coalescing (same-task nested recovery reuses the active claim)
    # ------------------------------------------------------------------

    def _claim_maps(self):
        claims = getattr(self, "_phase3_active_claims", None)
        if claims is None:
            claims = self._phase3_active_claims = {}
        steps = getattr(self, "_phase3_nested_steps", None)
        if steps is None:
            steps = self._phase3_nested_steps = {}
        return claims, steps

    # ------------------------------------------------------------------
    # Artifact reference / decision-step plumbing
    # ------------------------------------------------------------------

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
        """Use canonical policy; this method owns application context, not policy."""
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

    # ------------------------------------------------------------------
    # Existing-execution settlement (park/retire while a blocker applies)
    # ------------------------------------------------------------------

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
            return await self._settle_parked_execution(
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
            return await self._settle_parked_execution(
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
        return await self._settle_parked_execution(
            claim, current, reason=reason, wake=wake, retry_at=retry_at,
        )

    async def _settle_parked_execution(
        self,
        claim: RecoveryClaim,
        artifact: Artifact,
        *,
        reason: str,
        wake: str,
        retry_at: float = 0.0,
    ) -> bool:
        """Final settlement once any resumable/pausable execution has already
        been converged by ``_park_existing_execution`` above: observe a
        remaining terminal execution once more, then durably record the
        parked/retired outcome."""
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

    # ------------------------------------------------------------------
    # Executor truth reconciliation
    # ------------------------------------------------------------------

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
        if current is None or transfer is None or transfer.state in TERMINAL_TRANSFER_STATES:
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

        # Top-level adjustment: an executor-cancelled observation reaching
        # here with no other error is treated as an orphaned resource, not a
        # silent no-op reconciliation.
        if observed is not None and observed.state == ExecutionState.CANCELLED and error is None:
            error = self._orphaned_error(observed.handle.executor_id)
        return current, error, observed, None, retirement_reason

    # ------------------------------------------------------------------
    # Recovery decision application
    # ------------------------------------------------------------------

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

        if (
            current.execution is not None
            and current.state == "unknown"
            and decision.action in {RecoveryAction.RECONCILE, RecoveryAction.BACKOFF}
        ):
            retry_at = decision.retry_at if decision.retry_at is not None else self.clock()
            reason = decision.quiescence_reason or "retry_backoff"
            wake = decision.wake_condition or f"retry_at:{retry_at}"
            return await self.repository.record_recovery_quiescence(
                claim,
                reason=reason,
                wake_condition=wake,
                blocked_retry_at=retry_at,
            )

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
            # next_index is not guaranteed to be > current.selected --
            # _next_alternate_index searches by attempt history, not
            # "selected + 1", so a lower index the operator never tried is a
            # legitimate target. Only bounds and "not the artifact's own
            # current selection" (which activate_candidate itself also
            # refuses) remain invalid here.
            if next_index is None or next_index == current.selected or next_index >= len(current.candidates):
                return await self._park_existing_execution(
                    claim,
                    current,
                    reason="recovery_exhausted",
                    wake="operator_retry",
                )
            # One canonical candidate-activation operation: the SAME mutation
            # an operator-requested switch uses
            # (transfers.convergence_engine.TransferEngine
            # .activate_candidate_command), including the partial-file/resume
            # policy this inline branch previously skipped. ``current``'s own
            # execution here is already confirmed terminal by
            # _reconcile_current before this decision is ever reached, so
            # retirement is a no-op confirmation, not a fresh cancel.
            #
            # This call is already running inside ``claim`` -- the real
            # recovery claim (AUTO_RETRY, EXECUTOR_RECOVERY,
            # PROVIDER_RECOVERY, STARTUP_RECONCILE, USER_RETRY, or RESUME)
            # ``recover_artifact`` acquired for its own genuine trigger. Pass
            # that SAME claim through so this activation is attributed to its
            # real recovery authority in provenance, instead of a second,
            # nested claim or a borrowed USER_CANDIDATE_SWITCH identity that
            # would misrepresent an automatic decision as an operator one.
            result = await activate_candidate(
                self, current, next_index,
                retry_at=decision.retry_at or self.clock(), error=error, claim=claim,
            )
            if not result.committed:
                return await self._park_existing_execution(
                    claim,
                    current,
                    reason="recovery_exhausted",
                    wake="operator_retry",
                )
            return True

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

    # ------------------------------------------------------------------
    # Manual retry trigger adaptation
    # ------------------------------------------------------------------

    async def _retry_actionable(self, artifacts) -> bool:
        for artifact in artifacts:
            if artifact.state == "completed":
                continue
            context = await self.repository.recovery_context(artifact.id)
            if artifact.state in {
                "error", "recovery_wait", "lost", "unresolved", "refresh_pending",
            }:
                return True
            if context.get("quiescence_reason") in {
                "recovery_exhausted", "provider_disabled", "executor_unavailable",
                "storage_unavailable", "retry_backoff",
            }:
                return True
        return False

    async def retry(self, transfer_id: int, *, reacquire=False):
        """Operator Retry is a serialized trigger adapter, not a recovery algorithm."""
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

            artifacts = await self.repository.artifacts(transfer_id)
            if artifacts and not await self._retry_actionable(artifacts):
                return True

            if not await self.repository.state(
                transfer_id,
                TransferState.QUEUED,
                operator=True,
                expected_epoch=transfer.epoch,
            ):
                return False

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
                if not applied:
                    current = next(
                        (
                            item for item in await self.repository.artifacts(transfer_id)
                            if item.id == artifact.id
                        ),
                        None,
                    )
                    # A concurrent canonical owner may have made the work
                    # productive while this operator request lost/coalesced its
                    # claim. Treat that as success without reapplying authority.
                    if current is None or await self._retry_actionable((current,)):
                        ok = False
            await self.repository.retry_requests(transfer_id, reset_budget=True)
            await self._aggregate(transfer_id)
            return ok

    # ------------------------------------------------------------------
    # Recovery entry point
    # ------------------------------------------------------------------

    async def _run_claimed_recovery(
        self,
        claim: RecoveryClaim,
        artifact: Artifact,
        trigger: RecoveryTrigger,
        error: NormalizedError | None,
        observed: ExecutionObservation | None,
    ) -> _Step:
        current, error, observed, handled, retirement = await self._reconcile_current(
            claim, artifact, trigger, error, observed,
        )
        return handled or await self._plan_after_reconcile(
            claim, current or artifact, trigger, error, observed, retirement,
        )

    async def recover_artifact(
        self,
        artifact: Artifact | int,
        *,
        trigger: RecoveryTrigger,
        error: NormalizedError | None = None,
        observed: ExecutionObservation | None = None,
    ) -> bool:
        """One durable claim per artifact; same-task nested failure reuses it."""
        trigger = RecoveryTrigger(trigger)
        artifact = await self._artifact_ref(artifact)
        if artifact is None:
            return False
        task = asyncio.current_task()
        claims, nested_steps = self._claim_maps()
        key = (task, artifact.id)
        existing = claims.get(key)
        if existing is not None:
            if not await self.repository.recovery_claim_current(existing, now=self.clock()):
                return False
            step = await self._run_claimed_recovery(
                existing, artifact, trigger, error, observed,
            )
            nested_steps[key] = step
            return step.applied

        claim = await self.repository.claim_recovery(
            artifact.id,
            trigger,
            self.clock(),
            lease_seconds=max(300.0, float(self.policy.max_retry_delay)),
        )
        if claim is None:
            return False
        claims[key] = claim
        step = _Step(False, False, RecoveryAction.RECONCILE.value,
                     "reconciled_current_state", "coalesced")
        try:
            step = await self._run_claimed_recovery(
                claim, artifact, trigger, error, observed,
            )
            nested = nested_steps.pop(key, None)
            if nested is not None:
                step = nested
            return step.applied
        except Exception as exc:
            step = _Step(
                True, False, RecoveryAction.RECONCILE.value,
                f"recovery_application_error:{type(exc).__name__}", "application_error",
            )
            return False
        finally:
            claims.pop(key, None)
            nested_steps.pop(key, None)
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

    # ------------------------------------------------------------------
    # Manual candidate activation (operator path; shares the same claim
    # system and mutation as the automatic TRY_ALTERNATE_CANDIDATE decision)
    # ------------------------------------------------------------------

    async def activate_candidate_command(
        self, transfer_id: int, artifact_id: int, target_index: int,
    ) -> ActivationResult | None:
        """Operator-requested candidate activation entry point: the manual
        counterpart to ``recover_artifact``, fenced by the SAME exclusive
        claim system (``claim_recovery`` is exclusive across every trigger,
        this one included) so a concurrent AUTO_RETRY/USER_RETRY/RESUME/
        scheduler recovery observation can never interleave with this
        mutation, and a stale claim from either side can never overwrite the
        other's outcome. Returns ``None`` only when the claim itself could
        not be acquired (a concurrent recovery trigger currently owns the
        artifact); every other outcome -- including a validation failure --
        is a real ``ActivationResult`` with ``committed=False``, never an
        exception.

        The mutation itself (transfers.candidate_activation.activate_candidate)
        is the SAME one the automatic TRY_ALTERNATE_CANDIDATE decision uses
        (this class's own ``_apply_recovery_decision``); this method supplies
        claim acquisition and completion, not a second implementation of the
        switch itself.
        """
        claim = await self.repository.claim_recovery(
            artifact_id, RecoveryTrigger.USER_CANDIDATE_SWITCH, self.clock(),
            lease_seconds=max(300.0, float(self.policy.max_retry_delay)),
        )
        if claim is None:
            return None
        result = None
        try:
            artifact = await self._current_artifact(transfer_id, artifact_id)
            if artifact is None:
                result = ActivationResult(False, "not_found", transfer_id=transfer_id, artifact_id=artifact_id)
                return result
            result = await activate_candidate(self, artifact, target_index, retry_at=0, claim=claim)
            return result
        finally:
            outcome = result.reason if result is not None else "application_error"
            await self._finish_claim(
                claim,
                action="user_candidate_switch",
                reason=outcome,
                outcome="activated" if (result is not None and result.committed) else "not_applied",
                artifact=await self._current_artifact(transfer_id, artifact_id),
                candidate_changed=bool(result is not None and result.committed),
                retirement_reason=result.retirement if result is not None else None,
            )

    # ------------------------------------------------------------------
    # Dispatch readiness routing
    # ------------------------------------------------------------------

    async def _dispatch(self, artifact: Artifact):
        """Route pre-execution readiness failures through canonical recovery."""
        candidate = self._candidate(artifact)
        if candidate is not None and not self._candidate_provider_enabled(candidate):
            return await self.recover_artifact(
                artifact,
                trigger=RecoveryTrigger.AUTO_RETRY,
                error=self._provider_wait_error(candidate),
            )
        if candidate is not None and not self.registry.eligible_executors(candidate):
            return await self.recover_artifact(
                artifact,
                trigger=RecoveryTrigger.AUTO_RETRY,
                error=self._executor_wait_error(),
            )
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

    # ------------------------------------------------------------------
    # Automatic recovery adapters / wake / startup / pause-resume
    # ------------------------------------------------------------------

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
