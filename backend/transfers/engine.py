"""WS2 P1 transfer engine extension over the qualified WS1 P2 lifecycle base.

The base module is the exact qualified WS1 P2 engine.  This public engine keeps
that lifecycle intact and overrides only the recovery/runtime-metadata seams
needed for alternate-candidate failover and execution-discovered progress.
"""
from __future__ import annotations

from dataclasses import replace

from transfers._engine_base import TransferEngine as _QualifiedTransferEngine
from transfers.contracts import CandidateRefresh
from transfers.errors import (
    Category, Domain, NormalizedError, Origin, Recovery, Retryability, Stage,
    TransferError, unknown_failure,
)
from transfers.filesystem import retire_partial
from transfers.models import (
    Artifact, ExecutionObservation, ExecutionState, OutcomeKind, ResolutionResult,
    ResourceState, TransferOutcome,
)


_RUNTIME_TOTAL_STATES = frozenset({
    ExecutionState.QUEUED,
    ExecutionState.TRANSFERRING,
    ExecutionState.PAUSED,
    ExecutionState.SUCCEEDED,
})


class TransferEngine(_QualifiedTransferEngine):
    """Qualified lifecycle plus WS2 P1 recovery and runtime-size behavior."""

    def _candidate_provider_enabled(self, candidate) -> bool:
        if candidate is None or not candidate.provider_id:
            return True
        provider = self.registry.providers.get(candidate.provider_id)
        return bool(provider and provider.descriptor.enabled)

    async def _next_alternate_index(self, artifact: Artifact) -> int | None:
        """Return the next ordered, provenance-bound, currently executable alternate."""
        for index in range(artifact.selected + 1, len(artifact.candidates)):
            candidate = artifact.candidates[index]
            if not self._candidate_provider_enabled(candidate):
                continue
            if not self.registry.eligible_executors(candidate):
                continue
            if await self.canonical.origin_for(artifact, candidate) is None:
                continue
            return index
        return None

    def _candidate_sidecars(self, artifact: Artifact) -> tuple[str, ...]:
        """Identify old-candidate sidecars without reopening routing policy."""
        if artifact.execution is not None:
            executor = self.registry.executors.get(artifact.execution.executor_id)
            return executor.resumable_paths(artifact.target) if executor is not None else ()
        if not artifact.candidates:
            return ()
        candidate = artifact.candidates[artifact.selected]
        schemes = {endpoint.scheme for endpoint in candidate.endpoints}
        executors = sorted(
            (item for item in self.registry.executors.values() if schemes & item.descriptor.schemes),
            key=lambda item: (-item.descriptor.priority, item.descriptor.id),
        )
        return executors[0].resumable_paths(artifact.target) if executors else ()

    async def _terminal_recovery(self, artifact: Artifact, error: NormalizedError) -> bool:
        """Retire terminal execution authority before exposing an artifact error."""
        return await self.repository.transition_recovery(
            artifact.id, "error", error=error, retry_at=0,
        )

    async def _activate_alternate(self, artifact: Artifact, index: int, *, retry_at: float) -> bool:
        """Atomically retire the old writer, select the alternate, then retire partial bytes."""
        if index <= artifact.selected or index >= len(artifact.candidates):
            return False
        sidecars = self._candidate_sidecars(artifact)
        replacement = artifact.candidates[index]
        accepted_size = artifact.expected_bytes if artifact.expected_bytes > 0 else replacement.expected_bytes
        if not await self.repository.transition_recovery(
            artifact.id,
            "queued",
            retry_at=retry_at,
            selected=index,
            expected_bytes=max(0, accepted_size),
            reset_budget=True,
        ):
            return False
        retire_partial(self.root, artifact.target, sidecars)
        return True

    async def _try_exhausted_alternate(self, artifact: Artifact, error: NormalizedError) -> bool:
        index = await self._next_alternate_index(artifact)
        decision = self.policy.retry(
            error,
            max(max(1, self.policy.max_attempts), artifact.retries),
            self.clock(),
            can_refresh=False,
            has_alternate=index is not None,
        )
        if (index is not None and decision.automatic
                and decision.action == Recovery.TRY_ALTERNATE_CANDIDATE):
            return await self._activate_alternate(artifact, index, retry_at=decision.retry_at)
        await self._terminal_recovery(artifact, error)
        return False

    async def _schedule_refresh(self, artifact: Artifact, error: NormalizedError):
        candidate = artifact.candidates[artifact.selected] if artifact.candidates else None
        provider = self.registry.providers.get(candidate.provider_id) if candidate else None
        if (provider is not None and provider.descriptor.enabled
                and isinstance(provider, CandidateRefresh)
                and await self.repository.consume_recovery_refresh(artifact.id)):
            return await self.repository.transition_recovery(
                artifact.id,
                "refresh_pending",
                error=error,
                retry_at=self.clock(),
            )
        return await self._try_exhausted_alternate(artifact, error)

    async def _dispatch(self, artifact: Artifact):
        candidate = artifact.candidates[artifact.selected] if artifact.candidates else None
        if candidate is not None and not self._candidate_provider_enabled(candidate):
            error = NormalizedError(
                Domain.PROVIDER,
                Category.PROVIDER_UNAVAILABLE,
                Stage.QUEUE,
                retryability=Retryability.BACKOFF,
                recovery=Recovery.TRY_ALTERNATE_CANDIDATE,
                origin=Origin.PROVIDER,
                operator_action_required=False,
                integration_id=candidate.provider_id,
            )
            await self.repository.outcome(artifact.transfer_id, TransferOutcome(OutcomeKind.FAILURE, error))
            await self._try_exhausted_alternate(artifact, error)
            return
        return await super()._dispatch(artifact)

    async def _execution_result(self, artifact, executor, observed):
        """Accept neutral runtime total-size evidence before normal reconciliation."""
        if (isinstance(observed, ExecutionObservation)
                and artifact.execution is not None
                and observed.handle == artifact.execution
                and observed.state in _RUNTIME_TOTAL_STATES
                and isinstance(observed.progress.total_bytes, int)
                and not isinstance(observed.progress.total_bytes, bool)
                and observed.progress.total_bytes > 0
                and artifact.expected_bytes <= 0):
            if await self.repository.accept_execution_total(
                artifact.id, observed.handle, observed.progress.total_bytes,
            ):
                current = await self._current_artifact(artifact.transfer_id, artifact.id)
                if current is not None:
                    artifact = current
        return await super()._execution_result(artifact, executor, observed)

    async def _recover_source_artifact(self, artifact: Artifact, error: NormalizedError):
        await self.repository.outcome(
            artifact.transfer_id,
            TransferOutcome(OutcomeKind.FAILURE, error),
            attempt_id=artifact.execution.attempt_id if artifact.execution else None,
        )
        if (error.domain == Domain.SECURITY
                or error.retryability in {Retryability.NEVER, Retryability.UNKNOWN}
                or error.recovery in {Recovery.REQUIRE_OPERATOR, Recovery.FAIL, Recovery.NONE}):
            await self._terminal_recovery(artifact, error)
            return

        failures, refreshes = await self.repository.record_source_failure(artifact.id)
        candidate = artifact.candidates[artifact.selected] if artifact.candidates else None
        provider = self.registry.providers.get(candidate.provider_id) if candidate else None

        if not self._candidate_provider_enabled(candidate):
            await self._try_exhausted_alternate(artifact, error)
            return

        definitive_expiry = error.category in {Category.CANDIDATE_EXPIRED, Category.SOURCE_EXPIRED}
        if failures == 1 and not definitive_expiry:
            delay = 0.0 if error.retryability == Retryability.IMMEDIATE else float(self.policy.retry_delay)
            if error.retry_after_seconds is not None:
                delay = max(delay, error.retry_after_seconds)
            await self.repository.transition_recovery(
                artifact.id,
                "queued",
                error=error,
                retry_at=self.clock() + delay,
            )
            return

        if (provider is not None and provider.descriptor.enabled
                and isinstance(provider, CandidateRefresh)
                and refreshes == 0
                and (definitive_expiry or failures >= 2)):
            if await self.repository.consume_recovery_refresh(artifact.id):
                await self.repository.transition_recovery(
                    artifact.id,
                    "refresh_pending",
                    error=error,
                    retry_at=self.clock(),
                )
                return

        await self._try_exhausted_alternate(artifact, error)

    async def _recover_artifact(self, artifact: Artifact, error: NormalizedError):
        if not artifact.candidates:
            return await super()._recover_artifact(artifact, error)
        if error.origin == Origin.REMOTE_SOURCE:
            return await self._recover_source_artifact(artifact, error)

        candidate = artifact.candidates[artifact.selected]
        provider = self.registry.providers.get(candidate.provider_id)
        next_index = await self._next_alternate_index(artifact)
        attempts = artifact.retries
        can_refresh = bool(provider and provider.descriptor.enabled and isinstance(provider, CandidateRefresh))
        if not self._candidate_provider_enabled(candidate):
            attempts = max(max(1, self.policy.max_attempts), attempts)
            can_refresh = False
        decision = self.policy.retry(
            error,
            attempts,
            self.clock(),
            can_refresh=can_refresh,
            has_alternate=next_index is not None,
        )
        await self.repository.outcome(
            artifact.transfer_id,
            TransferOutcome(OutcomeKind.FAILURE, error),
            attempt_id=artifact.execution.attempt_id if artifact.execution else None,
        )
        if not decision.automatic:
            await self._terminal_recovery(artifact, error)
            return
        if decision.action == Recovery.RECONCILE:
            return
        if decision.action == Recovery.TRY_ALTERNATE_CANDIDATE:
            if next_index is not None:
                await self._activate_alternate(artifact, next_index, retry_at=decision.retry_at)
            return
        if decision.action == Recovery.RERESOLVE:
            await self._schedule_refresh(artifact, error)
            return
        await self.repository.transition_recovery(
            artifact.id,
            "queued",
            error=error,
            retry_at=decision.retry_at,
        )

    async def _refresh(self, artifact: Artifact):
        candidate = artifact.candidates[artifact.selected]
        provider = self.registry.providers.get(candidate.provider_id)
        attempt = None
        record = None
        try:
            if provider is None or not provider.descriptor.enabled:
                raise TransferError(NormalizedError(
                    Domain.PROVIDER,
                    Category.PROVIDER_UNAVAILABLE,
                    Stage.CANDIDATE_PREPARATION,
                    retryability=Retryability.BACKOFF,
                    recovery=Recovery.TRY_ALTERNATE_CANDIDATE,
                    origin=Origin.PROVIDER,
                    operator_action_required=False,
                    integration_id=candidate.provider_id,
                ))
            if not isinstance(provider, CandidateRefresh):
                raise TransferError(NormalizedError(
                    Domain.REQUEST,
                    Category.UNSUPPORTED_CAPABILITY,
                    Stage.CANDIDATE_PREPARATION,
                    retryability=Retryability.BACKOFF,
                    recovery=Recovery.TRY_ALTERNATE_CANDIDATE,
                    origin=Origin.CORE,
                    operator_action_required=False,
                    integration_id=candidate.provider_id,
                ))
            if not await self._live(artifact.transfer_id, admission=True):
                return
            origin = await self.canonical.origin_for(artifact, candidate)
            if origin is None:
                raise TransferError(self._error(
                    Category.OWNERSHIP_CONFLICT,
                    Stage.CANDIDATE_PREPARATION,
                    domain=Domain.LIFECYCLE,
                    retryability=Retryability.NEVER,
                ))
            record = origin.request
            bound_candidate = replace(candidate, refresh_request=record.request)
            attempt = await self.repository.begin_refresh(record, provider.descriptor.id)
            result = self._authoritative_provider_result(
                provider.descriptor.id,
                await provider.refresh(bound_candidate),
            )
            live = await self.repository.resolution(attempt, result)
            if not live and record.transfer_id == artifact.transfer_id:
                return
            if result.error:
                raise TransferError(result.error)
            if not result.candidates:
                raise TransferError(self._error(
                    Category.NO_TRANSFER_CANDIDATE,
                    Stage.CANDIDATE_PREPARATION,
                    domain=Domain.RESOLUTION,
                ))
            if any(item.expires_at is not None and item.expires_at <= self.clock() for item in result.candidates):
                raise TransferError(self._error(
                    Category.CANDIDATE_EXPIRED,
                    Stage.CANDIDATE_PREPARATION,
                    domain=Domain.RESOLUTION,
                    retryability=Retryability.AFTER_RERESOLUTION,
                    recovery=Recovery.RERESOLVE,
                ))
            if not await self.canonical.refresh_candidate(
                artifact, origin, candidate, result.candidates,
            ):
                current = await self._current_artifact(artifact.transfer_id, artifact.id)
                if current is None or current.state == "completed":
                    return
                raise TransferError(self._error(
                    Category.OWNERSHIP_CONFLICT,
                    Stage.CANDIDATE_PREPARATION,
                    domain=Domain.LIFECYCLE,
                    retryability=Retryability.NEVER,
                ))
            size = artifact.expected_bytes if artifact.expected_bytes > 0 else result.candidates[0].expected_bytes
            await self.repository.artifact_state(
                artifact.id,
                "queued",
                selected=artifact.selected,
                expected_bytes=max(0, size),
            )
        except Exception as exc:
            error = exc.error if isinstance(exc, TransferError) else unknown_failure(
                exc,
                integration_id=provider.descriptor.id if provider else "",
                domain=Domain.PROVIDER,
                stage=Stage.CANDIDATE_PREPARATION,
            )
            if attempt:
                await self.repository.resolution(
                    attempt,
                    ResolutionResult(ResourceState.UNKNOWN, error=error),
                )
            current = await self._current_artifact(artifact.transfer_id, artifact.id)
            if record and record.parent_id and error.category in {
                    Category.RESOURCE_NOT_FOUND,
                    Category.RESOURCE_EXPIRED,
                    Category.SOURCE_EXPIRED,
                    Category.SOURCE_NOT_FOUND,
            }:
                await self._renew_source_parent(record)
            if current is not None:
                await self._try_exhausted_alternate(current, error)
