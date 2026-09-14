"""Final public Phase-3 unified recovery execution surface."""
from __future__ import annotations

import asyncio

from transfers._convergence_phase3_dispatch_base import TransferEngine as _DispatchQualifiedEngine
from transfers._convergence_phase3_public_base import _Step
from transfers.candidate_activation import ActivationResult, activate_candidate
from transfers.errors import NormalizedError
from transfers.models import Artifact, ExecutionObservation, ExecutionState
from transfers.policy import RecoveryAction
from transfers.recovery_execution import RecoveryClaim, RecoveryTrigger


class TransferEngine(_DispatchQualifiedEngine):
    """Production owner for every Phase-3 recovery trigger."""

    def _claim_maps(self):
        claims = getattr(self, "_phase3_active_claims", None)
        if claims is None:
            claims = self._phase3_active_claims = {}
        steps = getattr(self, "_phase3_nested_steps", None)
        if steps is None:
            steps = self._phase3_nested_steps = {}
        return claims, steps

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

    async def _reconcile_current(
        self,
        claim: RecoveryClaim,
        artifact,
        trigger: RecoveryTrigger,
        error: NormalizedError | None,
        observed: ExecutionObservation | None,
    ):
        current, error, observed, handled, retirement = await super()._reconcile_current(
            claim, artifact, trigger, error, observed,
        )
        if (
            handled is None
            and current is not None
            and observed is not None
            and observed.state == ExecutionState.CANCELLED
            and error is None
        ):
            error = self._orphaned_error(observed.handle.executor_id)
        return current, error, observed, handled, retirement

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
        if (
            current is not None
            and current.execution is not None
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
        return await super()._apply_recovery_decision(
            claim,
            artifact,
            error,
            decision,
            decision_id=decision_id,
            next_index=next_index,
        )

    async def activate_candidate_command(
        self, transfer_id: int, artifact_id: int, target_index: int,
    ) -> ActivationResult | None:
        """Operator-requested candidate activation entry point (DP 1.0.12
        recovery leveling, Sections 10/11): the manual counterpart to
        ``recover_artifact``, fenced by the SAME exclusive claim system
        (``claim_recovery`` is exclusive across every trigger, this one
        included) so a concurrent AUTO_RETRY/USER_RETRY/RESUME/scheduler
        recovery observation can never interleave with this mutation, and a
        stale claim from either side can never overwrite the other's
        outcome. Returns ``None`` only when the claim itself could not be
        acquired (a concurrent recovery trigger currently owns the artifact);
        every other outcome -- including a validation failure -- is a real
        ``ActivationResult`` with ``committed=False``, never an exception.

        The mutation itself (transfers.candidate_activation.activate_candidate)
        is the SAME one the automatic TRY_ALTERNATE_CANDIDATE decision uses
        (transfers._convergence_phase3_base.TransferEngine
        ._apply_recovery_decision); this method supplies claim acquisition
        and completion, not a second implementation of the switch itself.
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
        return await super()._dispatch(artifact)
