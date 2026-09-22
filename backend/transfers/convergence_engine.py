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
import contextlib
from dataclasses import dataclass, replace

from transfers.candidate_activation import ActivationResult, activate_candidate
from transfers.cohorts import reopen_unverified_associations, unverified_association_count
from transfers.contracts import CandidateRefresh, ResourceLookup
from transfers.engine import TransferEngine as _QualifiedTransferEngine
from transfers.errors import (
    Category, Domain, NormalizedError, Origin, Retryability, Stage, TransferError,
    unknown_failure,
)
from transfers.filesystem import adoptable_material
from transfers.models import (
    Artifact, CleanupAuthority, ExecutionControl, ExecutionHandle, ExecutionObservation, ExecutionState,
    ExecutionSubject, ExecutorRuntimeCapability, MaterializationAdmissionKind, Ownership,
    OutcomeKind, ResolutionAttempt, ResolutionResult, ResourceState, TransferOutcome, TransferState,
)
from transfers.policy import RecoveryAction, TERMINAL_TRANSFER_STATES, failure_signature
from transfers.recovery_execution import RecoveryClaim, RecoveryTrigger, trigger_authority
from transfers.size_evidence import reported_sizes_compatible


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
            # A fact, not a decision: only bytes the executor itself reported
            # for this attempt. A missing observation, or one synthesized
            # because the executor could not report (UNKNOWN/ABSENT), carries a
            # default zero that is NOT an observed zero.
            observed_completed_bytes=(
                observed.progress.completed_bytes
                if observed is not None
                and observed.state not in {ExecutionState.UNKNOWN, ExecutionState.ABSENT}
                else None
            ),
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

    # States in which the native writer is confirmed to have actually
    # stopped producing bytes -- the only states safe to detach/deauthorize
    # from (Gate 9 revision-4 rejection finding 2). QUEUED/RUNNING/
    # PAUSED/UNKNOWN must never reach ``_retire_stale_materialization``: the
    # native process may still be live and would be orphaned (DB association
    # dropped while the writer keeps running untracked).
    _STALE_RETIREMENT_CONFIRMED_TERMINAL_STATES = frozenset({
        ExecutionState.CANCELLED, ExecutionState.ABSENT, ExecutionState.FAILED, ExecutionState.SUCCEEDED,
    })

    # Explicit outcomes for ``_retire_stale_execution`` (Gate 9 revision-5
    # rejection finding 2): the caller must be able to tell confirmed
    # detachment apart from every case where the native writer's stopped
    # state could not be proven, so it never records "retired" provenance
    # for a retirement that did not actually happen.
    _RETIRED = "retired"
    _DEFERRED = "deferred"
    _CLAIM_LOST = "claim_lost"

    async def _cancel_and_confirm_stopped(
        self, claim: RecoveryClaim, artifact: Artifact, executor,
    ) -> tuple[str, ExecutionObservation | None]:
        """Single fenced cancel/confirm primitive shared by STALE retirement
        (``_retire_stale_execution``) and HOLD's unpausable-executor
        retirement (``_park_existing_execution``) -- Gate 9 revision-6
        rejection findings 1/2: neither caller may declare a native writer
        quiesced/retired while it may still be productive, and both must
        close the same claim-loss window.

        Proves, via a terminal observation whose handle matches the
        execution being retired, that the native writer actually stopped --
        a terminal state reported for a different handle proves nothing
        about THIS writer. The claim is revalidated once before the first
        native operation and once more immediately before returning
        ``_RETIRED`` (i.e. immediately before any caller's durable
        detach/requeue mutation), closing the window between those two
        checks where a concurrent owner (e.g. a pause/resume control call)
        could invalidate the fence mid-flight -- the earlier revision only
        checked before the first operation, so a claim lost during
        cancel/observe could still let the stale owner perform the
        detach/requeue afterward.

        Returns ``(_RETIRED, confirmed_observation)`` only when cancellation
        succeeded AND a matching-handle terminal observation was persisted
        AND the claim was still current immediately afterward;
        ``(_DEFERRED, None)`` when the writer's stopped state could not be
        (yet) proven; ``(_CLAIM_LOST, observation_or_None)`` when the fence
        was lost at any point -- the factual terminal observation may still
        have been persisted (truthful executor state), but callers must NOT
        treat ``_CLAIM_LOST`` as license to apply their own policy/lifecycle
        mutation.
        """
        if not await self.repository.recovery_claim_current(claim, now=self.clock()):
            return self._CLAIM_LOST, None
        handle = artifact.execution
        try:
            # The executor reports observed stop truth, never a bare
            # acknowledgement; a terminal state reported against any other
            # handle is refused by the one acceptance function.
            confirmed = await self._cancel_execution(executor, handle)
        except TransferError:
            return self._DEFERRED, None
        await self.repository.outcome(
            artifact.transfer_id, self._cancellation_outcome(confirmed), attempt_id=handle.attempt_id,
        )
        await self.repository.execution(confirmed)
        if confirmed.state not in self._STALE_RETIREMENT_CONFIRMED_TERMINAL_STATES:
            # UNKNOWN or still-active: cannot yet prove the writer stopped.
            return self._DEFERRED, confirmed
        # Revalidate immediately before the caller's durable detach/requeue
        # mutation -- the factual terminal observation above is already
        # persisted regardless of this outcome.
        if not await self.repository.recovery_claim_current(claim, now=self.clock()):
            return self._CLAIM_LOST, confirmed
        return self._RETIRED, confirmed

    async def _retire_stale_execution(self, claim: RecoveryClaim, artifact: Artifact) -> str:
        """Cancel an execution superseded by a newer materialization
        authority (STALE admission, specification section 7.5) and requeue
        its request for reconciliation against current authorized state --
        the same cancel-and-reconcile primitives ``_park_existing_execution``
        already uses when a blocker makes an execution unpausable, never a
        new selection-specific retirement path.

        Detach/deauthorize/requeue runs ONLY after ``_cancel_and_confirm_
        stopped`` reports ``_RETIRED`` -- a lost claim, a missing executor, a
        cancel failure, a handle mismatch, or a nonterminal (UNKNOWN/still-
        active) post-cancel observation all leave the artifact's execution
        association untouched instead -- STALE keeps blocking generation-B
        reconstruction regardless (admission compares against the transfer's
        independently-tracked current generation, not this helper's
        completion), so nothing unauthorized can dispatch while retirement is
        retried on the next reconciliation pass.

        Gate 9 revision-7 rejection: the mutation itself
        (``TransferRepository.retire_stale_materialization_if_claim_current``)
        re-verifies the SAME claim token/generation atomically, inside the
        SAME transaction as the detach/release/requeue write -- closing the
        window between "the claim was current when checked" and "the mutation
        actually committed" where a concurrent recovery owner (e.g. a
        pause/resume fence) could advance ``recovery_generation`` in between
        and let a now-stale caller still perform the durable mutation.

        Returns ``_RETIRED`` only when detach actually happened, ``_DEFERRED``
        when the writer's stopped state could not be (yet) proven, or
        ``_CLAIM_LOST`` when the fence was lost (either during cancel/observe,
        or atomically at the final detach/requeue commit) -- callers must use
        this result rather than assuming a call to this method retired
        anything.
        """
        if artifact.execution is None:
            # No executor association exists at all -- nothing to orphan.
            # Still atomically claim-fenced: a claim lost since this helper
            # was entered must not let a stale owner requeue the request.
            if not await self.repository.retire_stale_materialization_if_claim_current(
                claim, artifact.id, artifact.transfer_id, artifact.request_id,
            ):
                return self._CLAIM_LOST
            return self._RETIRED
        executor = self.registry.executors.get(artifact.execution.executor_id)
        if executor is None:
            # Cannot confirm the native writer stopped without an executor;
            # preserve the association/fence rather than detach blind.
            return self._DEFERRED
        result, _confirmed = await self._cancel_and_confirm_stopped(claim, artifact, executor)
        if result != self._RETIRED:
            return result
        if not await self.repository.retire_stale_materialization_if_claim_current(
            claim, artifact.id, artifact.transfer_id, artifact.request_id,
        ):
            return self._CLAIM_LOST
        return self._RETIRED

    async def _dispatch_claimed(self, claim: RecoveryClaim, artifact: Artifact) -> bool:
        if not await self.repository.recovery_claim_current(claim, now=self.clock()):
            return False
        transfer = await self.repository.get(artifact.transfer_id)
        current = await self._current_artifact(artifact.transfer_id, artifact.id)
        if transfer is None or current is None or transfer.paused or await self.repository.globally_paused():
            return False
        if current.state == "completed":
            return True
        if current.execution is not None:
            # Universal execution-admission invariant (Workstream A,
            # specification section 7.5): an existing execution handle is not
            # proof of authorization. A superseded manifest generation can
            # never resume/reuse as "already fine."
            admission = await self.repository.materialization_authorization(current)
            if admission.kind == MaterializationAdmissionKind.HOLD:
                return False
            if admission.kind == MaterializationAdmissionKind.STALE:
                await self._retire_stale_execution(claim, current)
                return False
            return True
        candidate = self._candidate(current)
        if candidate is None:
            await self.repository.artifact_state(current.id, "unresolved", release=True)
            await self.repository.retry_requests(current.transfer_id, request_id=current.request_id)
            return True
        if not self._candidate_provider_enabled(candidate):
            return False
        if not self.registry.claimants(ExecutionSubject.of(candidate)):
            return False
        if candidate.expires_at is not None and candidate.expires_at <= self.clock():
            return False
        # This dispatch runs under the recovery claim that decided to reuse the
        # same candidate: the one place a native-assisted retry may hand the
        # previous (already fenced) attempt's native state to the new attempt.
        await super()._dispatch(current, retry_from=await self._native_retry_predecessor(current, candidate))
        return True

    async def _native_retry_predecessor(self, artifact: Artifact, candidate) -> ExecutionHandle | None:
        """The previous attempt a native-assisted retry may continue: this
        artifact's most recent attempt, for the same candidate identity, whose
        normal control authority is already revoked and whose native work is
        terminal-but-unsuccessful. ``None`` when core did not decide a
        same-candidate retry of such an attempt, or when the selected claimant
        does not declare native-assisted retry."""
        claimants = self.registry.claimants(ExecutionSubject.of(candidate))
        if not claimants or not claimants[0].capabilities.native_assisted_retry:
            return None
        latest = None
        for attempt in await self.repository.executions(artifact.transfer_id):
            if attempt.artifact_id == artifact.id:
                latest = attempt
        if (latest is None or latest.state not in {"failed", "cancelled", "absent"} or latest.candidate is None
                or str(latest.candidate.id) != str(candidate.id)
                or await self.repository.authorize_execution(latest.handle, "observe")):
            return None
        return latest.handle

    async def _refresh_claimed(
        self, claim: RecoveryClaim, artifact: Artifact,
    ) -> tuple[bool, str, NormalizedError | None]:
        """Single-flight refresh with a renewed fence immediately before mutation.

        Every reason this method can return is one of exactly three kinds,
        each proven rather than assumed (DP 1.0.12 canonical lifecycle/
        recovery/completion rework, CANON-001 closure, Gate 9 revision 8 --
        two reviews found successive reasons here silently classified as
        "self-resolves on a later tick" with nothing to actually cause that):

        1. Genuinely self-resolving without any decision or durable state
           change, because something ELSE already made the condition
           obsolete: ``refresh_not_pending`` (the artifact left
           ``refresh_pending`` via a different path -- there is nothing left
           for this call to do). ``claim_lost``/``claim_lost_after_refresh``
           (a concurrent claim now owns this artifact's recovery; ITS own
           progress, not another tick of this one, is what moves things
           forward). ``refresh_replay_conflict``/``refresh_candidate_conflict``
           (a concurrent writer already applied a different, valid outcome
           for this exact decision).
        2. A real, potentially-persistent failure of the refresh RESULT,
           carried in the third element as a ``NormalizedError`` so the one
           caller (``_plan_after_reconcile``) can re-enter the ordinary
           decision cycle (``policy.recover``, which already has bounded,
           budget-aware handling for expiry/integrity-class errors) instead
           of leaving the artifact stuck in ``refresh_pending`` forever:
           ``refresh_failed`` (the provider call itself failed),
           ``refresh_result_empty`` (succeeded but returned nothing),
           ``refresh_candidate_expired`` (returned an already-expired
           candidate), ``refresh_size_mismatch`` (returned a
           size-incompatible candidate), and ``refresh_unsupported`` (the
           bound provider does not implement ``CandidateRefresh`` at all --
           a permanent fact about this exact candidate, mapped to the same
           ``CANDIDATE_EXPIRED``-shaped error ``manual_failover
           ._refresh_exact`` already uses for the identical structural case,
           so both refresh entry points treat "this provider cannot refresh
           this candidate" identically). None of these self-resolve merely
           by trying again unchanged.
        3. A durable but non-error-shaped disposition applied directly by
           this method's caller rather than through ``policy.recover``
           (there is no candidate-level policy question to ask -- either
           there is no candidate/provenance to act on, or the artifact's OWN
           bounded budget for this decision is already spent):
           ``candidate_missing`` (no selected candidate at all),
           ``candidate_origin_missing`` (the candidate exists but its
           provenance/origin record does not -- a durable data-shape gap,
           not a retryable fact), and ``refresh_budget_exhausted`` (this
           decision's one-time refresh reservation is already consumed;
           the budget does not replenish by ticking again). All three are
           parked by the caller exactly like ``refresh_outcome_unknown``
           (``_park_existing_execution(reason="recovery_exhausted",
           wake="operator_retry")`` + ``WAIT_FOR_OPERATOR``), and
           ``provider_unavailable`` is applied via ``_decision_step``'s
           existing ``force_provider_not_ready`` path (the SAME
           ``quiescence_reason="provider_disabled"``/
           ``wake_condition="provider_enabled"`` mechanism proven by
           ``test_provider_disablement_is_quiescent_and_reenable_wakes_
           same_work``) -- both are real, durably wakeable dispositions,
           never a silent no-op.
        """
        current = await self._current_artifact(artifact.transfer_id, artifact.id)
        if current is None or current.state != "refresh_pending":
            return False, "refresh_not_pending", None
        candidate = self._candidate(current)
        if candidate is None:
            return False, "candidate_missing", None
        provider = self.registry.providers.get(candidate.provider_id)
        if provider is None or not provider.descriptor.enabled:
            return False, "provider_unavailable", None
        if not isinstance(provider, CandidateRefresh):
            return False, "refresh_unsupported", self._error(
                Category.CANDIDATE_EXPIRED, Stage.CANDIDATE_PREPARATION, domain=Domain.RESOLUTION,
                retryability=Retryability.AFTER_RERESOLUTION,
            )
        origin = await self.canonical.origin_for(current, candidate)
        if origin is None:
            return False, "candidate_origin_missing", None
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
                return False, "claim_lost", None
            if not await self.repository.reserve_recovery_refresh(
                claim,
                decision_id,
                limit=max(1, self.policy.refreshes_per_recovery_epoch),
            ):
                return False, "refresh_budget_exhausted", None

        state = await self.repository.begin_recovery_refresh(
            claim, record, provider.descriptor.id, decision_id,
        )
        if state is None:
            return False, "claim_lost", None
        attempt_id = state["attempt_id"]
        attempt = ResolutionAttempt(attempt_id, record.id, provider.descriptor.id, "started")

        if not state["created"]:
            if state.get("state") != "succeeded":
                return False, "refresh_outcome_unknown", None
            candidates = await self.repository.resolved_candidates(record.id)
            if not candidates:
                return False, "refresh_result_empty", self._error(
                    Category.NO_TRANSFER_CANDIDATE, Stage.CANDIDATE_PREPARATION, domain=Domain.RESOLUTION,
                )
            if any(item.expires_at is not None and item.expires_at <= self.clock() for item in candidates):
                return False, "refresh_candidate_expired", self._error(
                    Category.CANDIDATE_EXPIRED, Stage.CANDIDATE_PREPARATION, domain=Domain.RESOLUTION,
                    retryability=Retryability.AFTER_RERESOLUTION,
                )
            replacement_size = candidates[0].expected_bytes
            if (
                current.expected_bytes > 0
                and replacement_size > 0
                and not reported_sizes_compatible(current.expected_bytes, replacement_size)
            ):
                return False, "refresh_size_mismatch", self._error(
                    Category.SIZE_MISMATCH, Stage.CANDIDATE_PREPARATION, domain=Domain.INTEGRITY,
                )
            if not await self.repository.renew_recovery_claim(
                claim, self.clock(), lease_seconds=max(300.0, float(self.policy.max_retry_delay)),
            ):
                return False, "claim_lost", None
            if not await self.canonical.refresh_candidate(current, origin, candidate, candidates):
                return False, "refresh_replay_conflict", None
            size = current.expected_bytes if current.expected_bytes > 0 else replacement_size
            await self.repository.artifact_state(
                current.id,
                "queued",
                selected=current.selected,
                expected_bytes=max(0, size),
            )
            await self.repository.clear_recovery_refresh_inflight(claim, decision_id)
            await self.repository.clear_recovery_quiescence(claim)
            return True, "refresh_replayed", None

        if not await self.repository.recovery_claim_current(claim, now=self.clock()):
            return False, "claim_lost", None
        bound_candidate = replace(candidate, refresh_request=record.request)
        try:
            result = self._authoritative_provider_result(
                provider.descriptor.id,
                await provider.refresh(bound_candidate),
                request_kind=record.request.kind,
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
            return False, "claim_lost_after_refresh", None
        if result.error:
            await self.repository.clear_recovery_refresh_inflight(claim, decision_id)
            return False, "refresh_failed", result.error
        if not result.candidates:
            await self.repository.clear_recovery_refresh_inflight(claim, decision_id)
            return False, "refresh_result_empty", self._error(
                Category.NO_TRANSFER_CANDIDATE, Stage.CANDIDATE_PREPARATION, domain=Domain.RESOLUTION,
            )
        if any(item.expires_at is not None and item.expires_at <= self.clock() for item in result.candidates):
            await self.repository.clear_recovery_refresh_inflight(claim, decision_id)
            return False, "refresh_candidate_expired", self._error(
                Category.CANDIDATE_EXPIRED, Stage.CANDIDATE_PREPARATION, domain=Domain.RESOLUTION,
                retryability=Retryability.AFTER_RERESOLUTION,
            )
        replacement_size = result.candidates[0].expected_bytes
        if (
            current.expected_bytes > 0
            and replacement_size > 0
            and not reported_sizes_compatible(current.expected_bytes, replacement_size)
        ):
            await self.repository.clear_recovery_refresh_inflight(claim, decision_id)
            return False, "refresh_size_mismatch", self._error(
                Category.SIZE_MISMATCH, Stage.CANDIDATE_PREPARATION, domain=Domain.INTEGRITY,
            )
        if not await self.repository.renew_recovery_claim(
            claim, self.clock(), lease_seconds=max(300.0, float(self.policy.max_retry_delay)),
        ):
            return False, "claim_lost_after_refresh", None
        if not await self.canonical.refresh_candidate(current, origin, candidate, result.candidates):
            return False, "refresh_candidate_conflict", None
        size = current.expected_bytes if current.expected_bytes > 0 else replacement_size
        await self.repository.artifact_state(
            current.id,
            "queued",
            selected=current.selected,
            expected_bytes=max(0, size),
        )
        await self.repository.clear_recovery_refresh_inflight(claim, decision_id)
        await self.repository.clear_recovery_quiescence(claim)
        return True, "refresh_applied", None

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
            refreshed, refresh_reason, refresh_error = await self._refresh_claimed(claim, current)
            if refreshed:
                return _Step(True, True, RecoveryAction.REFRESH_CANDIDATE.value,
                             refresh_reason, "refresh_applied",
                             candidate_changed=True,
                             retirement_reason=retirement_reason)
            if refresh_reason in {
                "refresh_outcome_unknown", "refresh_budget_exhausted",
                "candidate_missing", "candidate_origin_missing",
            }:
                # DP 1.0.12 canonical lifecycle/recovery/completion rework
                # (CANON-001 closure, Gate 9 revision 8): none of these four
                # is a candidate-level policy question `policy.recover` can
                # meaningfully answer -- there is no candidate/provenance to
                # act on, or this decision's own bounded refresh reservation
                # is already spent and does not replenish by ticking again
                # (a review correctly rejected leaving `refresh_budget_
                # exhausted` classified as self-resolving). Park exactly like
                # the pre-existing `refresh_outcome_unknown` case: a durable,
                # explicitly wakeable operator-wait, never a silent no-op.
                await self._park_existing_execution(
                    claim, current, reason="recovery_exhausted", wake="operator_retry",
                )
                return _Step(True, False, RecoveryAction.WAIT_FOR_OPERATOR.value,
                             refresh_reason, refresh_reason,
                             retirement_reason=retirement_reason)
            if refresh_reason == "provider_unavailable":
                # Reuse the SAME durably-wakeable provider-quiescence path
                # `_reconcile_current` already uses elsewhere in this class
                # (`force_provider_not_ready=True` -> `quiescence_reason=
                # "provider_disabled"` / `wake_condition="provider_enabled"`,
                # proven by test_provider_disablement_is_quiescent_and_
                # reenable_wakes_same_work) rather than a bespoke park or a
                # silent no-op.
                candidate = self._candidate(current)
                return await self._decision_step(
                    claim, current, self._provider_wait_error(candidate),
                    count_failure=False,
                    force_provider_not_ready=True, outcome="provider_wait",
                    retirement_reason=retirement_reason,
                )
            if refresh_error is not None:
                # DP 1.0.12 canonical lifecycle/recovery/completion rework
                # (CANON-001 closure): a genuine, potentially-persistent
                # factual failure of the refresh RESULT itself -- the
                # provider call failed (`refresh_failed`), returned nothing
                # (`refresh_result_empty`), returned an already-expired
                # candidate (`refresh_candidate_expired`), returned a
                # size-incompatible candidate (`refresh_size_mismatch`), or
                # the bound provider does not implement `CandidateRefresh`
                # at all (`refresh_unsupported`, Gate 9 revision 8) -- is a
                # real, actionable failure of this recovery
                # attempt. It must re-enter the ordinary decision cycle
                # (`policy.recover`, which already has bounded, budget-aware
                # handling for expiry/integrity-class errors) so the artifact
                # can retry, switch candidates, or (once every alternative is
                # exhausted) reach a deliberate terminal/wait disposition,
                # exactly like any other execution failure. Without this, the
                # artifact was left stuck in refresh_pending forever with no
                # further progress or wake mechanism -- a genuine gap this
                # CANON-001 closure surfaced (by removing the lower,
                # non-claim-fenced stack's different, working remote-source
                # retry/refresh path that had been masking it for any test
                # built on that composition) and fixes here rather than
                # leaving unfixed.
                return await self._decision_step(
                    claim, current, refresh_error, count_failure=True,
                    retirement_reason=retirement_reason,
                )
            return _Step(True, False, RecoveryAction.REFRESH_CANDIDATE.value,
                         refresh_reason, refresh_reason,
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
        """Park without losing reusable native work; retire only when pause is impossible."""
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
        observed = await self._observe_execution(executor, current.execution)
        handle = observed.handle

        if observed.resumable:
            if executor.capabilities.per_execution_pause:
                if observed.state != ExecutionState.PAUSED:
                    if ExecutionControl.PAUSE not in self._controls(executor, observed):
                        # Pausable in general but not right now: never guess
                        # and never retire a live writer for it -- keep it
                        # owned and let the next pass converge.
                        await self.repository.execution(observed)
                        return False
                    if not await self.repository.recovery_claim_current(claim, now=self.clock()):
                        return False
                    observed = await self._accept_observation(handle, await executor.pause(handle))
                await self.repository.execution(observed)
                return await self.repository.record_recovery_quiescence(
                    claim,
                    reason=reason,
                    wake_condition=wake,
                    blocked_retry_at=retry_at,
                )

            # A blocker requires productive execution to stop. If this executor
            # cannot pause, retirement is necessary; cancellation does not delete
            # the artifact target or partial bytes. Gate 9 revision-6 rejection
            # finding 1: this must never declare the writer quiesced while it
            # may still be productive, so it uses the SAME fenced cancel/confirm
            # primitive ``_retire_stale_execution`` uses rather than a looser
            # local cancel-then-proceed sequence -- only a confirmed, matching-
            # handle terminal observation (with the claim still current
            # immediately beforehand) reaches the park/settle mutation below.
            result, _confirmed = await self._cancel_and_confirm_stopped(claim, current, executor)
            if result != self._RETIRED:
                # Not (yet) confirmed stopped, or the fence was lost -- leave
                # the association untouched; the next reconciliation pass
                # retries rather than parking a possibly-still-live writer.
                return False
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
                    observed = await self._observe_execution(executor, current.execution)
                except TransferError:
                    observed = None
                if observed is not None and observed.state == ExecutionState.UNKNOWN:
                    observed = None
            if observed is not None:
                if observed.resumable and executor.capabilities.per_execution_pause:
                    if observed.state != ExecutionState.PAUSED:
                        if ExecutionControl.PAUSE not in self._controls(executor, observed):
                            # Not pausable right now: stay owned, unsettled.
                            await self.repository.execution(observed)
                            return False
                        if not await self.repository.recovery_claim_current(claim, now=self.clock()):
                            return False
                        observed = await self._accept_observation(
                            observed.handle, await executor.pause(observed.handle))
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
        # Gate 9 revision-7 rejection: pass ``claim`` through so this final
        # park/settle mutation is atomically claim-fenced too -- not only the
        # "no other active execution" check ``transition_recovery`` already
        # performed. This closes the same window for the unpausable-HOLD path
        # (reached via ``_park_existing_execution`` -> ``_cancel_and_confirm_
        # stopped`` -> here) that ``_retire_stale_execution`` closes for
        # STALE: a claim lost after ``_cancel_and_confirm_stopped`` already
        # returned ``_RETIRED`` must not let this settlement commit.
        applied = await self.repository.transition_recovery(
            current.id,
            state,
            error=current.error,
            retry_at=retry_at,
            quiescence_reason=reason,
            wake_condition=wake,
            claim=claim,
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
                if executor is not None:
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
                    observed = await self._observe_execution(executor, current.execution)
                else:
                    observed = await self._accept_observation(current.execution, observed)
                current = replace(current, execution=observed.handle)

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
                observed = await self._converge_execution(current, executor, observed)
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
        applied = await self.repository.transition_recovery(
            current.id,
            "queued",
            error=error,
            retry_at=retry_at,
            clear_quiescence=True,
        )
        if applied:
            # A same-candidate retry decided now is dispatched under THIS
            # claim when the executor can carry native state across attempts;
            # otherwise the ordinary scheduler dispatches a fresh start.
            requeued = await self._current_artifact(current.transfer_id, current.id)
            candidate = self._candidate(requeued) if requeued is not None else None
            if (requeued is not None and candidate is not None and requeued.execution is None
                    and await self._native_retry_predecessor(requeued, candidate) is not None):
                await self._dispatch_claimed(claim, requeued)
        return applied

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
        """Operator Retry is a serialized trigger adapter, not a recovery
        algorithm. ``reacquire=True`` (DP 1.0.12 canonical lifecycle/recovery/
        completion rework, CANON-001 closure, Gate 9 revision 6) is a
        DIFFERENT, exceptional lifecycle transition -- "resume tracking a
        transfer a duplicate submission found already COMPLETED/DELETED" --
        not the ordinary operator-retry decision below; ``submit()`` reaches
        it only for that specific dedupe outcome, never unconditionally. Both
        branches are owned here, by the one canonical semantic owner; see
        ``_reacquire_transfer`` for the reacquisition branch's own claim/
        fencing rationale."""
        if reacquire:
            return await self._reacquire_transfer(transfer_id)

        lock = self._transfer_locks.setdefault(transfer_id, asyncio.Lock())
        async with lock:
            transfer = await self.repository.get(transfer_id)
            if transfer is None:
                raise KeyError(transfer_id)
            if transfer.state == TransferState.CONSOLIDATED:
                return await self._reconsider_unverified_associations(transfer)
            if transfer.state == TransferState.DELETED:
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
            if artifacts:
                await self._aggregate(transfer_id)
        # Requests were durably requeued, and the transfer lock -- which a
        # running resolution cycle will not admit this transfer past -- is
        # released: the scheduler reconsiders the transfer now. Every earlier
        # return changed no resolution eligibility and wakes nothing.
        self._resolution_opportunity(transfer_id)
        return ok

    async def _reconsider_unverified_associations(self, transfer) -> bool:
        """DP 1.0.12 consolidation corrective, Round 3: the operator-retry
        branch for a transfer that settled CONSOLIDATED while holding at least
        one terminal UNVERIFIED association.

        Remediation 4 made UNVERIFIED terminal for ORDINARY SCHEDULING -- no
        writer, no failover membership, parent may settle -- but never a
        permanent proof lockout: explicit operator reconsideration was always
        part of the contract (only AUTOMATIC/background reconsideration is
        deferred). Once the parent settled, every path back to the ordinary
        equivalence machinery was closed (``TransferRepository.active()``,
        ``_live()``, ``CanonicalOwnership.attach()``'s incoming guard and this
        method's own former blanket refusal), so an operator-reopened request
        could never address the canonical artifact it was already associated
        with.

        Rather than punching an exception into each of those owners -- which
        would also strand a source that turns out to be genuinely DISTINCT,
        since materializing it needs a live parent -- this returns the
        transfer to the ordinary lifecycle exactly once, through the SAME
        epoch-fenced canonical transition owner ``_reacquire_transfer`` uses
        for its own (equally exceptional) terminal reopening. Everything
        afterwards is unchanged, unexceptional machinery: the scheduler sees
        the transfer again, ``coordinate_collection`` re-runs ordinary proof,
        and settlement is re-established by the existing
        ``_finalize_transfer``/aggregation owners -- back to CONSOLIDATED once
        the reconsidered leaf attaches or settles UNVERIFIED again, or by
        ordinary completion if it proves distinct and materializes.

        Settled transfers are NOT broadly reopened: an ordinary consolidated
        transfer holds no such association, is refused here before any
        transition is attempted, and keeps the previous behaviour exactly.
        Nothing autonomous can reach this -- only the operator action can.
        """
        if not await unverified_association_count(transfer.id):
            return False
        if await self.challenges.current(transfer.id):
            return False
        if not await reopen_unverified_associations(transfer.id):
            return False
        if not await self.repository.state(
            transfer.id, TransferState.QUEUED, operator=True, expected_epoch=transfer.epoch,
        ):
            return False
        # The requests are durably reopened and the transfer is live again:
        # wake resolution exactly as the ordinary operator-retry path does.
        self._resolution_opportunity(transfer.id)
        return True

    async def _reacquire_transfer(self, transfer_id: int) -> bool:
        """Resume tracking a transfer a duplicate submission found already
        durably COMPLETED or DELETED (DP 1.0.12 canonical lifecycle/recovery/
        completion rework, CANON-001 closure, Gate 9 revision 7: moved here
        from ``_engine_base.TransferEngine``, the sole remaining lower-layer
        semantic method, on the grounds that ``submit()`` needed it
        "universally" -- corrected: ``submit()`` reaches this only for the
        specific dedupe-onto-a-terminal-transfer outcome, an exceptional
        lifecycle transition like any other, not a neutral primitive).

        Precondition is authoritative here, not merely assumed from the
        caller: only a transfer CURRENTLY COMPLETED or DELETED is eligible
        (rev. 6 checked only for CONSOLIDATED, which under-enforced this).

        Concurrency (Gate 9 rev. 8 correction -- rev. 7 fenced the MUTATING
        native calls per-attempt but still performed the initial native
        executor observation in the plan-building loop before acquiring
        EITHER ``_execution_cycle_lock`` or the per-attempt
        ``_convergence_lock``; a review correctly found this left a real
        unfenced-observe window a concurrent ``pause()``/``resume()`` could
        race via its own, separately-fenced ``_converge_execution()`` call
        on the SAME handle). Only genuinely static, non-native facts --
        which artifact ids exist for this transfer -- are gathered before
        the critical section now; NO native call happens there. Once inside
        ``self._execution_cycle_lock`` (the SAME lock ``reconcile_executions()``
        holds for its own whole cycle, closing the "scheduler observes/
        mutates artifacts this method is still rewriting" race exactly as
        before) and, per artifact, its own ``self._convergence_lock(handle
        .attempt_id)`` (the SAME per-attempt lock ``_converge_execution``
        uses for every native pause/resume/cancel), this method re-reads the
        artifact fresh and performs its OWN executor observation for the
        first time -- there is no unfenced observation left to carry across
        the boundary, because none is taken before it. A concurrent
        ``pause()``/``resume()`` racing this exact handle therefore cannot
        interleave with this method's observe/cancel/persist sequence for
        it at any point: whichever side wins the per-attempt lock completes
        first, and the other safely detects the ownership/handle mismatch
        via ``_converge_execution``'s own existing check rather than
        double-mutating or racing a native call. Operator-initiated
        ``retry()`` on this SAME transfer id cannot race this method at all
        -- both are branches of one ``retry()`` call serialized by the SAME
        ``self._transfer_locks`` entry acquired below. The only race this
        method's own lock protects directly is therefore what remains
        genuine: two concurrent duplicate submissions racing to reacquire
        the SAME terminal transfer.

        This method no longer clears durable pause intent at the end (Gate 9
        revision 9 correction). It used to call ``pause_intent(transfer_id,
        False)`` unconditionally after the mutation loop, on the assumption
        a reacquired transfer should simply start unpaused. That assumption
        was already false in the ordinary case -- terminal settlement
        (``_retire_transfer_auxiliary_state_in_db``, run when the transfer
        first became COMPLETED/DELETED/CANCELLED/CONSOLIDATED) already
        DELETES the ``transfer_pause_intents`` row entirely, so there is
        normally nothing left to clear by the time this method runs. The
        only case where the call had any effect at all was a concurrent
        operator ``pause()`` landing mid-reacquisition: ``pause()`` sets the
        intent row immediately (``set_pause_and_fence``, unguarded by any
        lock this method holds) before it ever reaches the per-attempt
        ``_convergence_lock`` this method also holds for that same artifact
        -- so the unconditional clear, running after this method releases
        that lock, could silently overwrite a genuine, newer user pause
        request with a stale ``False``. ``pause_intent`` is a plain upsert
        with no generation/CAS of its own, so there was nothing to detect
        the conflict. Simply removing the call (rather than reintroducing it
        behind a canonical claim/fence) is correct precisely because it was
        never doing anything useful in the case it was meant to handle.
        """
        lock = self._transfer_locks.setdefault(transfer_id, asyncio.Lock())
        async with lock:
            transfer = await self.repository.get(transfer_id)
            if transfer is None:
                raise KeyError(transfer_id)
            if transfer.state not in {TransferState.COMPLETED, TransferState.DELETED}:
                return False
            if await self.challenges.current(transfer_id):
                return False
            if any(pending for _resource, _state, pending in await self.repository.resources(transfer_id)):
                return False
            if not await self.repository.reset_postprocessing(transfer_id):
                return False
            artifact_ids = [artifact.id for artifact in await self.repository.artifacts(transfer_id)]
            async with self._execution_cycle_lock:
                if not await self.repository.state(transfer_id, TransferState.ACCEPTED,
                                                   operator=True, expected_epoch=transfer.epoch):
                    return False
                for artifact_id in artifact_ids:
                    artifact = await self._current_artifact(transfer_id, artifact_id)
                    if artifact is None:
                        return False
                    handle = artifact.execution
                    handle_lock = self._convergence_lock(handle.attempt_id) if handle else contextlib.nullcontext()
                    async with handle_lock:
                        if handle is not None:
                            current = await self._current_artifact(transfer_id, artifact_id)
                            if (current is None or current.execution is None
                                    or current.execution.attempt_id != handle.attempt_id):
                                return False
                            artifact = current
                        candidate = artifact.candidates[artifact.selected] if artifact.candidates else None
                        executor = self.registry.executors.get(artifact.execution.executor_id) if artifact.execution else None
                        if executor is None and artifact.execution is not None:
                            return False
                        observation = None
                        if executor is not None and artifact.execution:
                            observation = await self._observe_execution(executor, artifact.execution)
                            if observation.state == ExecutionState.UNKNOWN:
                                return False
                            artifact = replace(artifact, execution=observation.handle)
                        if observation:
                            if observation.resumable:
                                await self.repository.execution(observation)
                                continue
                        if candidate is None:
                            if artifact.execution and not await self._stop_confirmed(executor, artifact.execution):
                                return False
                            await self.repository.artifact_state(artifact.id, "unresolved", release=True)
                            await self.repository.retry_requests(transfer_id, request_id=artifact.request_id)
                            continue
                        try:
                            executor, work, footprint = self._artifact_work(artifact, executor)
                        except TransferError:
                            return False
                        if await adoptable_material(work.materialization, footprint, artifact.expected_bytes,
                                                    candidate.integrity, delay=self.policy.adoption_stability_seconds):
                            await self.repository.artifact_state(artifact.id, "completed")
                            continue
                        if artifact.execution and not await self._stop_confirmed(executor, artifact.execution):
                            return False
                        await self.repository.reset_retry_budget(artifact.id)
                        await self.repository.artifact_state(artifact.id, "unresolved", release=True)
                        origin = await self.canonical.origin_for(artifact, candidate)
                        if origin is not None:
                            record = origin.request
                        else:
                            record = next(item for item in await self.repository.requests(transfer_id) if item.id == artifact.request_id)
                        if not record.parent_id or not await self._renew_source_parent(record, operator=True):
                            await self.repository.artifact_state(artifact.id, "queued", release=True)
                await self.repository.retry_requests(transfer_id, reset_budget=True)
                return True

    async def _stop_confirmed(self, executor, handle) -> bool:
        """Cancel native work and persist the observed truth; only a
        positively observed stop confirms."""
        stopped = await self._cancel_execution(executor, handle)
        await self.repository.execution(stopped)
        return stopped.stopped

    async def _renew_source_parent(self, record, *, operator=False):
        """Re-observe a manifest-member request's parent resource and renew
        it when durably expired/absent (moved here from
        ``_engine_base.TransferEngine`` alongside ``_reacquire_transfer``,
        its only caller -- DP 1.0.12 canonical lifecycle/recovery/completion
        rework, CANON-001 closure, Gate 9 revision 6)."""
        parent = next((item for item in await self.repository.requests(record.transfer_id) if item.id == record.parent_id), None)
        if parent is None or parent.resource is None:
            return False
        provider = self.registry.providers.get(parent.resource.provider_id)
        if not isinstance(provider, ResourceLookup):
            return False
        try:
            observation = await provider.observe(parent.resource)
        except Exception as exc:
            error = exc.error if isinstance(exc, TransferError) else unknown_failure(exc,
                integration_id=provider.descriptor.id, domain=Domain.PROVIDER, stage=Stage.RECONCILIATION)
            await self.repository.outcome(record.transfer_id, TransferOutcome(OutcomeKind.FAILURE, error))
            return False
        await self.repository.resource_observation(record.transfer_id, observation.resource, observation.state)
        if observation.state not in {ResourceState.ABSENT, ResourceState.EXPIRED}:
            return False
        error = self._error(Category.RESOURCE_EXPIRED, Stage.RESOLUTION, domain=Domain.PROVIDER,
            retryability=Retryability.AFTER_RERESOLUTION)
        decision = self.policy.retry_resolution(error, 0 if operator else parent.attempts, self.clock())
        if not decision.automatic:
            return False
        if observation.state != ResourceState.ABSENT and parent.resource.ownership in {Ownership.CREATED, Ownership.ADOPTED}:
            await self.repository.cleanup_intent(parent.transfer_id, parent.resource.id, CleanupAuthority.OWNED)
            await self._cleanup_pending()
            if any(resource.id == parent.resource.id and pending for resource, _state, pending in await self.repository.resources(record.transfer_id)):
                return False
        await self.repository.renew_parent(parent, self.clock() if operator else decision.retry_at, reset_budget=operator)
        return True

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
        if candidate is not None and not self.registry.claimants(ExecutionSubject.of(candidate)):
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
                        and self.registry.claimants(ExecutionSubject.of(candidate))
                    ):
                        await self.recover_artifact(
                            artifact, trigger=RecoveryTrigger.EXECUTOR_RECOVERY,
                        )
                elif reason == "storage_unavailable" and self.dispatch_permitted:
                    await self.recover_artifact(artifact, trigger=RecoveryTrigger.AUTO_RETRY)
                elif reason == "retry_backoff" and artifact.retry_at <= self.clock():
                    await self.recover_artifact(artifact, trigger=RecoveryTrigger.AUTO_RETRY)
                elif reason == "materialization_hold":
                    # Gate 9 revision-6 rejection finding 1: HOLD parking
                    # (``_reconcile_unauthorized_existing_execution`` /
                    # ``_park_existing_execution``) leaves the artifact
                    # quiesced as ``recovery_wait`` with
                    # ``quiescence_reason="materialization_hold"`` -- the
                    # base execution loop only reprocesses existing
                    # executions in queued/downloading/unknown/verifying/
                    # paused, so without this branch a HOLD-parked writer
                    # would stay parked indefinitely once authorization
                    # became PROCEED again. Re-derive readiness from the SAME
                    # admission authority that parked it (never a cached/
                    # forced value) and, once it reports PROCEED, route
                    # through the ordinary recovery-trigger adapter: a
                    # paused, resumable execution is resumed by the existing
                    # admission-gated ``_converge_execution`` PAUSED branch,
                    # never a fresh dispatch -- no user Retry, no manually
                    # acquired claim.
                    admission = await self.repository.materialization_authorization(artifact)
                    if admission.kind == MaterializationAdmissionKind.PROCEED:
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
        # DP 1.0.12 canonical lifecycle/recovery/completion rework (CANON-001
        # closure, Gate 9 revision): this class is now the sole owner of BOTH
        # the wake decision (_wake_quiescent_recoveries) and the scheduling
        # trigger that invokes it every reconcile cycle -- previously invoked
        # via a passthrough wrapper in transfers._engine_recovery.py, which
        # also carried a second, shadowed _wake_quiescent_recoveries
        # implementation of its own.
        await self._wake_quiescent_recoveries()
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
                # Universal execution-admission invariant (Workstream A,
                # specification section 7.5) on the ORDINARY scheduler
                # cadence, not only when some other caller happens to route
                # this artifact through an explicit recovery trigger. Without
                # this, a generation-A execution that is merely still
                # transferring (no error, no candidate/executor problem) is
                # never revalidated against a newer materialization
                # generation and can keep writing indefinitely after
                # generation B becomes authoritative.
                admission = await self.repository.materialization_authorization(artifact)
                if admission.kind != MaterializationAdmissionKind.PROCEED:
                    await self._reconcile_unauthorized_existing_execution(artifact)
                    continue
            await super()._process_executions(
                transfer_id,
                (artifact,),
                observations,
                dispatch_allowed=dispatch_allowed,
            )

    async def _reconcile_unauthorized_existing_execution(self, artifact: Artifact) -> None:
        """Route a HOLD/STALE existing execution discovered on the ordinary
        reconciliation cadence into the same claim-fenced machinery an
        explicit recovery trigger already uses (specification section 7.5).

        STALE is retired through ``_retire_stale_execution`` -- the same
        cancel/confirm/detach primitive ``_dispatch_claimed`` uses -- so a
        stale writer is cancelled and deauthorized without requiring a user
        Retry or any other manually manufactured recovery claim. Retirement
        provenance (``retirement_reason`` / ``outcome="retired"``) is only
        ever recorded when ``_retire_stale_execution`` reports back that
        detach actually happened (Gate 9 revision-5 rejection finding 2); a
        deferred/claim-lost result leaves the association durably intact and
        is reported as such, never as a false "retired".

        HOLD reuses the SAME existing pause/retire machinery
        ``_park_existing_execution`` already applies for every other
        non-error quiescence category (provider-disabled,
        executor-unavailable, storage-unavailable): pause the native writer
        if it is resumable/pausable, or cancel it if it is not -- an already
        active writer must not keep producing unauthorized materialization
        merely because HOLD, unlike STALE, is not itself a retirement
        decision (Gate 9 revision-5 rejection finding 1). Like those other
        quiescence categories, parking consumes no retry budget and records
        no failure/error; the artifact resumes ordinary reconciliation on its
        own the next time admission reports PROCEED.

        A missed claim (another trigger currently owns the artifact) is not
        an error; the very next scheduler tick re-evaluates admission.
        """
        claim = await self.repository.claim_recovery(
            artifact.id,
            RecoveryTrigger.AUTO_RETRY,
            self.clock(),
            lease_seconds=max(300.0, float(self.policy.max_retry_delay)),
        )
        if claim is None:
            return
        action, reason, retirement_reason = RecoveryAction.RECONCILE.value, "materialization_hold", None
        try:
            current = await self._current_artifact(artifact.transfer_id, artifact.id)
            if current is None or current.execution is None:
                return
            # Revalidate under the claim immediately before acting (closes
            # the TOCTOU window between the pre-claim read and claim
            # acquisition -- specification section 7.4).
            admission = await self.repository.materialization_authorization(current)
            if admission.kind == MaterializationAdmissionKind.STALE:
                result = await self._retire_stale_execution(claim, current)
                if result == self._RETIRED:
                    reason, retirement_reason = "materialization_stale", "materialization_superseded"
                else:
                    reason = f"materialization_stale_{result}"
            elif admission.kind == MaterializationAdmissionKind.HOLD:
                await self._park_existing_execution(
                    claim, current, reason="materialization_hold", wake="materialization_authorized",
                )
                reason = "materialization_hold"
        finally:
            if await self.repository.recovery_claim_current(claim):
                await self.repository.record_phase3_application(
                    claim, action=action, reason=reason,
                    retirement_reason=retirement_reason, partial_preserved=True,
                )
                await self._finish_claim(
                    claim, action=action, reason=reason,
                    outcome="retired" if retirement_reason else "quiesced",
                    artifact=artifact, retirement_reason=retirement_reason,
                )

    async def pause(self, transfer_id: int):
        transfer = await self.repository.get(transfer_id)
        if transfer is None:
            raise KeyError(transfer_id)
        # Pause intent is written under the execution-admission lock, so it is
        # ordered against any admission that hands transient input to a start.
        async with self._dispatch_lock:
            await self.repository.set_pause_and_fence(transfer_id, True)
        errors = []
        globally_paused = await self.repository.globally_paused()
        for artifact in await self.repository.artifacts(transfer_id):
            if artifact.execution is None:
                continue
            # An engaged executor-wide acquisition gate prevents network
            # acquisition for global pause even where one execution cannot be
            # paused individually right now; it never proves full quiescence.
            covered = globally_paused and artifact.execution.executor_id in self._acquisition_gated
            executor = self.registry.executors.get(artifact.execution.executor_id)
            if executor is None or not executor.capabilities.per_execution_pause:
                if not covered:
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
            elif observed is not None and observed.state in {ExecutionState.QUEUED, ExecutionState.RUNNING} \
                    and not covered:
                # Pause is not currently offered for this execution: the
                # intent is durable and convergence continues, but the pause is
                # reported as unconfirmed rather than guessed successful.
                errors.append(self._error(
                    Category.RECONCILIATION_FAILED,
                    Stage.EXECUTION,
                    domain=Domain.RECONCILIATION,
                    retryability=Retryability.BACKOFF,
                ))
        await self._aggregate(transfer_id)
        return tuple(errors)

    async def resume(self, transfer_id: int):
        transfer = await self.repository.get(transfer_id)
        if transfer is None:
            raise KeyError(transfer_id)
        if await self.repository.globally_paused():
            for other in await self.repository.active():
                if other.id != transfer_id:
                    async with self._dispatch_lock:
                        await self.repository.set_pause_and_fence(other.id, True)
            await self.repository.global_pause(False)
        await self.repository.set_pause_and_fence(transfer_id, False)
        transfer = await self.repository.get(transfer_id)
        if transfer and transfer.state == TransferState.PAUSED:
            await self.repository.state(transfer_id, TransferState.QUEUED)
        # The transfer is durably admissible and this method holds no transfer
        # lock, so its request-level work need not wait for the artifact
        # recovery below: a running resolution cycle reconsiders it now.
        self._resolution_opportunity(transfer_id)
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

    async def _set_acquisition_gates(self, paused: bool) -> tuple:
        """Engage/release every executor-wide acquisition gate an executor
        declares AND currently reports available. Core already recorded the
        durable global intent (and blocks new admission itself); a gate is an
        executor-local enforcement of it, never proof of quiescence. A gate
        the runtime-limit owner holds because a finite cap cannot currently be
        proven is not released by global resume."""
        errors = []
        for executor in tuple(self.registry.executors.values()):
            identity = executor.descriptor.id
            if not executor.capabilities.acquisition_gate:
                continue
            if not paused and identity in self.runtime.gated:
                self._acquisition_gated.discard(identity)
                continue
            try:
                health = await executor.health()
                if ExecutorRuntimeCapability.ACQUISITION_GATE not in health.available_runtime_capabilities:
                    continue
                result = await executor.set_acquisition_paused(paused)
                if result.error is not None or result.effective_paused is not paused:
                    errors.append(result.error or self._executor_wait_error(identity))
                elif paused:
                    self._acquisition_gated.add(identity)
                else:
                    self._acquisition_gated.discard(identity)
            except Exception:
                errors.append(self._executor_wait_error(identity))
        return tuple(errors)

    async def pause_all(self):
        async with self._dispatch_lock:
            await self.repository.global_pause(True)
            await self._set_acquisition_gates(True)
        results = {}
        for transfer in await self.repository.active():
            results[transfer.id] = await self.pause(transfer.id)
        return results

    async def resume_all(self):
        async with self._dispatch_lock:
            await self.repository.global_pause(False)
            await self._set_acquisition_gates(False)
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
        # One wake for the batch: every transfer above is durably admissible,
        # and a running resolution cycle reconsiders them all now.
        self._resolution_opportunity(*results)
        return results
