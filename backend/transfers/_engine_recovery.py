"""Canonical recovery extension over the qualified universal lifecycle base.

Recovery mechanics remain provider/executor neutral. This owner asks universal
policy for a decision using normalized failure evidence, durable progress/recovery
context and current registry/resource readiness, then applies that decision using
the existing lifecycle seams.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

from transfers import _engine_base
from transfers._engine_base import TransferEngine as _QualifiedTransferEngine
from transfers.applicability import ApplicabilityUnresolved
from transfers.cohorts import coordinate_collection
from transfers.contracts import CandidateRefresh
from transfers.errors import (
    Category, Domain, NormalizedError, Origin, Recovery, Retryability, Stage,
    TransferError, unknown_failure,
)
from transfers.filesystem import retire_partial
from transfers.mirrors import reported_sizes_compatible
from transfers.models import (
    Artifact, ExecutionObservation, ExecutionState, OutcomeKind, ResolutionResult,
    ResourceState, TransferOutcome, TransferState,
)
from transfers.policy import RecoveryAction, RecoveryContext


stable_payload = _engine_base.stable_payload


async def _stable_payload_proxy(*args, **kwargs):
    return await stable_payload(*args, **kwargs)


_engine_base.stable_payload = _stable_payload_proxy


class TransferEngine(_QualifiedTransferEngine):
    """Qualified lifecycle plus progress-aware universal recovery behavior."""

    def _collection_affinity_lock(self, transfer_id: int) -> asyncio.Lock:
        locks = getattr(self, "_collection_affinity_locks", None)
        if locks is None:
            locks = self._collection_affinity_locks = {}
        return locks.setdefault(transfer_id, asyncio.Lock())

    async def _ensure_collection_affinity(self, transfer_id: int) -> bool:
        transfer = await self.repository.get(transfer_id)
        if transfer is None or transfer.source != "direct_link":
            return False
        async with self._collection_affinity_lock(transfer_id):
            transfer = await self.repository.get(transfer_id)
            if transfer is None or transfer.source != "direct_link":
                return False
            records = await self.repository.requests(transfer_id)
            roots = tuple(record for record in records if record.parent_id is None)
            if len(roots) <= 1:
                return False
            if await self.repository.collection_route_provider(transfer_id):
                return False
            for record in records:
                if await self.repository.bound_route_provider(record.id):
                    return False
            try:
                provider = self.registry.collection_provider_for(tuple(record.request for record in roots))
            except ApplicabilityUnresolved:
                return True
            if provider is not None:
                await self.repository.bind_collection_route(transfer_id, provider.descriptor.id)
            return False

    async def _prepare_collection_affinity(self) -> set[int]:
        blocked = set()
        for transfer in await self.repository.active():
            if transfer.source == "direct_link" and await self._ensure_collection_affinity(transfer.id):
                blocked.add(transfer.id)
        return blocked

    async def resolve_pending(self):
        lock = getattr(self, "_collection_resolution_lock", None)
        if lock is None:
            lock = self._collection_resolution_lock = asyncio.Lock()
        async with lock:
            blocked = await self._prepare_collection_affinity()
            self._collection_affinity_blocked = blocked
            try:
                result = await super().resolve_pending()
                for transfer in await self.repository.active():
                    if transfer.source == "direct_link":
                        await self._aggregate(transfer.id)
                return result
            finally:
                self._collection_affinity_blocked = set()

    async def _process_request(self, record):
        if record.transfer_id in getattr(self, "_collection_affinity_blocked", set()):
            return
        if await self._ensure_collection_affinity(record.transfer_id):
            return
        return await super()._process_request(record)

    async def _serial_global_control(self, transfers):
        results = {}
        for transfer in transfers:
            results[transfer.id] = await self._control(transfer.id)
        return results

    async def pause_all(self):
        await self.repository.global_pause(True)
        return await self._serial_global_control(await self.repository.active())

    async def resume_all(self):
        await self.repository.global_pause(False)
        transfers = await self.repository.active()
        for transfer in transfers:
            await self.repository.pause_intent(transfer.id, False)
        return await self._serial_global_control(transfers)

    def _candidate_provider_enabled(self, candidate) -> bool:
        if candidate is None or not candidate.provider_id:
            return True
        provider = self.registry.providers.get(candidate.provider_id)
        return bool(provider and provider.descriptor.enabled)

    async def _wake_quiescent_recoveries(self):
        """Wake only when the durable, actionable recovery condition is satisfied."""
        now = self.clock()
        for transfer in await self.repository.active():
            for artifact in await self.repository.artifacts(transfer.id):
                if artifact.state != "recovery_wait":
                    continue
                context = await self.repository.recovery_context(artifact.id)
                reason = context.get("quiescence_reason")
                candidate = artifact.candidates[artifact.selected] if artifact.candidates else None
                wake = False
                if reason == "retry_backoff":
                    wake = artifact.retry_at <= now
                elif reason == "provider_disabled":
                    wake = self._candidate_provider_enabled(candidate)
                elif reason == "storage_unavailable":
                    wake = bool(self.dispatch_permitted)
                elif reason == "executor_unavailable":
                    wake = bool(candidate is not None and self.registry.eligible_executors(candidate))
                # input_required and recovery_exhausted require explicit operator
                # actions and therefore never wake from a scheduler tick.
                if wake:
                    await self.repository.transition_recovery(
                        artifact.id, "queued", retry_at=0, clear_quiescence=True,
                    )

    async def reconcile_executions(self):
        await self._wake_quiescent_recoveries()
        return await super().reconcile_executions()

    async def _aggregate(self, transfer_id: int):
        result = await super()._aggregate(transfer_id)
        transfer = await self.repository.get(transfer_id)
        if transfer and transfer.state not in {TransferState.DELETED, TransferState.COMPLETED,
                                                TransferState.CONSOLIDATED, TransferState.CANCELLED}:
            artifacts = await self.repository.artifacts(transfer_id)
            if any(item.state == "recovery_wait" for item in artifacts) and not transfer.paused:
                await self.repository.state(transfer_id, TransferState.QUEUED)
        return result

    async def _materialize(self, record, candidates):
        locks = getattr(self, "_cohort_locks", None)
        if locks is None:
            locks = self._cohort_locks = {}
        lock = locks.setdefault(record.transfer_id, asyncio.Lock())
        async with lock:
            if await coordinate_collection(self, record, candidates):
                return
            await super()._materialize(record, candidates)
            artifact = next((item for item in await self.repository.artifacts(record.transfer_id)
                             if item.request_id == record.id), None)
            if artifact is None or len(artifact.candidates) < 2:
                return
            for candidate in artifact.candidates:
                await self.canonical.origin_for(artifact, candidate)

    async def _next_alternate_index(self, artifact: Artifact) -> int | None:
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
        sidecars = self._candidate_sidecars(artifact)
        if not await self.repository.transition_recovery(
            artifact.id, "error", error=error, retry_at=0, clear_quiescence=True,
        ):
            return False
        if error.origin == Origin.REMOTE_SOURCE:
            retire_partial(self.root, artifact.target, sidecars)
        return True

    async def _quiesce(self, artifact: Artifact, error: NormalizedError, *, reason: str,
                       wake: str, retry_at: float = 0) -> bool:
        # Recovery exhaustion uses the established error lifecycle state while
        # retaining durable wake metadata; every other automatic wait remains a
        # nonterminal recovery_wait. This preserves presentation compatibility
        # without allowing exhausted work to consume resources.
        state = "error" if reason == "recovery_exhausted" else "recovery_wait"
        return await self.repository.transition_recovery(
            artifact.id,
            state,
            error=error,
            retry_at=retry_at,
            quiescence_reason=reason,
            wake_condition=wake,
        )

    async def _activate_alternate(self, artifact: Artifact, index: int, *, retry_at: float,
                                  error: NormalizedError) -> bool:
        if index <= artifact.selected or index >= len(artifact.candidates):
            return False
        replacement = artifact.candidates[index]
        if (artifact.expected_bytes > 0 and replacement.expected_bytes > 0
                and not reported_sizes_compatible(artifact.expected_bytes, replacement.expected_bytes)):
            return False
        sidecars = self._candidate_sidecars(artifact)
        if not await self.repository.transition_recovery(
            artifact.id, "error", error=error, retry_at=0, clear_quiescence=True,
        ):
            return False
        retire_partial(self.root, artifact.target, sidecars)
        current = await self._current_artifact(artifact.transfer_id, artifact.id)
        if current is None:
            return False
        accepted_size = current.expected_bytes if current.expected_bytes > 0 else replacement.expected_bytes
        return await self.repository.transition_recovery(
            artifact.id, "queued", retry_at=retry_at, selected=index,
            expected_bytes=max(0, accepted_size), candidate_switched=True,
            clear_quiescence=True,
        )

    async def _recovery_context(self, artifact: Artifact, *, can_refresh: bool,
                                has_alternate: bool) -> RecoveryContext:
        stored = await self.repository.recovery_context(artifact.id)
        candidate = artifact.candidates[artifact.selected] if artifact.candidates else None
        provider_ready = self._candidate_provider_enabled(candidate)
        executor_ready = bool(candidate is None or self.registry.eligible_executors(candidate))
        return RecoveryContext(
            execution_attempts=int(stored.get("execution_attempts") or 0),
            consecutive_no_progress_failures=int(stored.get("consecutive_no_progress_failures") or 0),
            failures_since_meaningful_progress=int(stored.get("failures_since_meaningful_progress") or 0),
            same_signature_failures=int(stored.get("same_signature_failures") or 0),
            candidate_refreshes=int(stored.get("candidate_refreshes") or 0),
            candidate_switches=int(stored.get("candidate_switches") or 0),
            recovery_epoch=int(stored.get("recovery_epoch") or 0),
            can_refresh=can_refresh,
            has_alternate=has_alternate,
            provider_ready=provider_ready,
            executor_ready=executor_ready,
            storage_ready=bool(self.dispatch_permitted),
        )

    async def _apply_recovery_decision(self, artifact: Artifact, error: NormalizedError,
                                       decision, *, next_index: int | None) -> bool:
        if decision.action == RecoveryAction.FAIL_PERMANENTLY:
            return await self._terminal_recovery(artifact, error)
        if decision.action == RecoveryAction.RECONCILE:
            retry_at = decision.retry_at if decision.retry_at is not None else self.clock()
            return await self._quiesce(
                artifact,
                error,
                reason=decision.quiescence_reason or "retry_backoff",
                wake=decision.wake_condition or f"retry_at:{retry_at}",
                retry_at=retry_at,
            )
        if decision.action == RecoveryAction.REFRESH_CANDIDATE:
            if await self.repository.consume_recovery_refresh(artifact.id):
                return await self.repository.transition_recovery(
                    artifact.id, "refresh_pending", error=error,
                    retry_at=decision.retry_at or self.clock(), clear_quiescence=True,
                )
            if next_index is not None:
                return await self._activate_alternate(
                    artifact, next_index, retry_at=self.clock(), error=error,
                )
            return await self._quiesce(
                artifact, error, reason="recovery_exhausted", wake="operator_retry",
            )
        if decision.action == RecoveryAction.TRY_ALTERNATE_CANDIDATE:
            if next_index is not None:
                return await self._activate_alternate(
                    artifact, next_index, retry_at=decision.retry_at or self.clock(), error=error,
                )
            return await self._quiesce(
                artifact, error, reason="recovery_exhausted", wake="operator_retry",
            )
        if decision.action == RecoveryAction.WAIT_FOR_PROVIDER:
            candidate = artifact.candidates[artifact.selected] if artifact.candidates else None
            provider_id = candidate.provider_id if candidate else ""
            return await self._quiesce(
                artifact, error, reason="provider_disabled",
                wake=f"provider_enabled:{provider_id}" if provider_id else "provider_enabled",
            )
        if decision.action == RecoveryAction.WAIT_FOR_RESOURCE:
            reason = decision.quiescence_reason or "executor_unavailable"
            wake = decision.wake_condition or ("executor_available" if reason == "executor_unavailable" else "storage_healthy:local_resource")
            return await self._quiesce(artifact, error, reason=reason, wake=wake)
        if decision.action == RecoveryAction.WAIT_FOR_OPERATOR:
            return await self._quiesce(
                artifact, error,
                reason=decision.quiescence_reason or "recovery_exhausted",
                wake=decision.wake_condition or "operator_retry",
            )
        retry_at = decision.retry_at if decision.retry_at is not None else self.clock()
        if retry_at > self.clock():
            return await self._quiesce(
                artifact, error,
                reason=decision.quiescence_reason or "retry_backoff",
                wake=decision.wake_condition or f"retry_at:{retry_at}",
                retry_at=retry_at,
            )
        return await self.repository.transition_recovery(
            artifact.id, "queued", error=error, retry_at=retry_at, clear_quiescence=True,
        )

    async def _decide_recovery(self, artifact: Artifact, error: NormalizedError) -> bool:
        candidate = artifact.candidates[artifact.selected] if artifact.candidates else None
        provider = self.registry.providers.get(candidate.provider_id) if candidate else None
        next_index = await self._next_alternate_index(artifact)
        can_refresh = bool(provider and provider.descriptor.enabled and isinstance(provider, CandidateRefresh))
        context = await self._recovery_context(
            artifact, can_refresh=can_refresh, has_alternate=next_index is not None,
        )
        decision = self.policy.recover(error, context, self.clock())
        await self.repository.record_recovery_decision(
            artifact.id, decision.action.value, decision.reason,
        )
        return await self._apply_recovery_decision(
            artifact, error, decision, next_index=next_index,
        )

    async def _try_exhausted_alternate(self, artifact: Artifact, error: NormalizedError) -> bool:
        next_index = await self._next_alternate_index(artifact)
        if next_index is not None:
            return await self._activate_alternate(
                artifact, next_index, retry_at=self.clock(), error=error,
            )
        return await self._quiesce(
            artifact, error, reason="recovery_exhausted", wake="operator_retry",
        )

    async def _schedule_refresh(self, artifact: Artifact, error: NormalizedError):
        candidate = artifact.candidates[artifact.selected] if artifact.candidates else None
        provider = self.registry.providers.get(candidate.provider_id) if candidate else None
        if (provider is not None and provider.descriptor.enabled
                and isinstance(provider, CandidateRefresh)
                and await self.repository.consume_recovery_refresh(artifact.id)):
            return await self.repository.transition_recovery(
                artifact.id, "refresh_pending", error=error, retry_at=self.clock(), clear_quiescence=True,
            )
        return await self._try_exhausted_alternate(artifact, error)

    async def _dispatch(self, artifact: Artifact):
        candidate = artifact.candidates[artifact.selected] if artifact.candidates else None
        if candidate is not None and not self._candidate_provider_enabled(candidate):
            error = NormalizedError(
                Domain.PROVIDER, Category.PROVIDER_UNAVAILABLE, Stage.QUEUE,
                retryability=Retryability.BACKOFF, origin=Origin.CORE,
            )
            await self.repository.outcome(
                artifact.transfer_id, TransferOutcome(OutcomeKind.FAILURE, error),
            )
            await self._quiesce(
                artifact, error, reason="provider_disabled",
                wake=f"provider_enabled:{candidate.provider_id}",
            )
            return
        if candidate is not None and not self.registry.eligible_executors(candidate):
            error = NormalizedError(
                Domain.EXECUTOR, Category.EXECUTOR_UNAVAILABLE, Stage.QUEUE,
                retryability=Retryability.AFTER_RESOURCE_CHANGE, origin=Origin.CORE,
            )
            await self.repository.outcome(
                artifact.transfer_id, TransferOutcome(OutcomeKind.FAILURE, error),
            )
            await self._quiesce(
                artifact, error, reason="executor_unavailable", wake="executor_available",
            )
            return
        return await super()._dispatch(artifact)

    async def _execution_result(self, artifact, executor, observed):
        if (isinstance(observed, ExecutionObservation)
                and artifact.execution is not None
                and observed.handle == artifact.execution
                and observed.state == ExecutionState.SUCCEEDED
                and artifact.candidates):
            candidate = artifact.candidates[artifact.selected]
            final_total = observed.progress.total_bytes
            if (candidate.expected_bytes <= 0 and artifact.expected_bytes > 0
                    and isinstance(final_total, int) and not isinstance(final_total, bool)
                    and final_total >= 0 and final_total != artifact.expected_bytes):
                valid = await stable_payload(
                    artifact.target, final_total,
                    sidecars=executor.resumable_paths(artifact.target),
                    integrity=candidate.integrity,
                    delay=self.policy.adoption_stability_seconds,
                    allow_empty=final_total == 0,
                )
                if valid:
                    await self.repository.execution(observed)
                    if await self.repository.refine_execution_total(artifact.id, observed.handle, final_total):
                        await self.repository.artifact_state(artifact.id, "completed", expected_bytes=final_total)
                        return
        return await super()._execution_result(artifact, executor, observed)

    async def _recover_source_artifact(self, artifact: Artifact, error: NormalizedError):
        await self.repository.outcome(
            artifact.transfer_id, TransferOutcome(OutcomeKind.FAILURE, error),
            attempt_id=artifact.execution.attempt_id if artifact.execution else None,
        )
        await self.repository.record_source_failure(artifact.id, error)
        await self._decide_recovery(artifact, error)

    async def _recover_artifact(self, artifact: Artifact, error: NormalizedError):
        if not artifact.candidates:
            return await super()._recover_artifact(artifact, error)
        if error.origin == Origin.REMOTE_SOURCE:
            return await self._recover_source_artifact(artifact, error)
        await self.repository.outcome(
            artifact.transfer_id, TransferOutcome(OutcomeKind.FAILURE, error),
            attempt_id=artifact.execution.attempt_id if artifact.execution else None,
        )
        await self.repository.record_source_failure(artifact.id, error)
        return await self._decide_recovery(artifact, error)

    async def _refresh(self, artifact: Artifact):
        candidate = artifact.candidates[artifact.selected]
        provider = self.registry.providers.get(candidate.provider_id)
        attempt = None
        record = None
        try:
            if provider is None or not provider.descriptor.enabled:
                raise TransferError(NormalizedError(
                    Domain.PROVIDER, Category.PROVIDER_UNAVAILABLE, Stage.CANDIDATE_PREPARATION,
                    retryability=Retryability.BACKOFF, origin=Origin.CORE,
                ))
            if not isinstance(provider, CandidateRefresh):
                raise TransferError(NormalizedError(
                    Domain.REQUEST, Category.UNSUPPORTED_CAPABILITY, Stage.CANDIDATE_PREPARATION,
                    retryability=Retryability.NEVER, origin=Origin.CORE,
                ))
            if not await self._live(artifact.transfer_id, admission=True):
                return
            origin = await self.canonical.origin_for(artifact, candidate)
            if origin is None:
                raise TransferError(self._error(
                    Category.OWNERSHIP_CONFLICT, Stage.CANDIDATE_PREPARATION,
                    domain=Domain.LIFECYCLE, retryability=Retryability.NEVER,
                ))
            record = origin.request
            bound_candidate = replace(candidate, refresh_request=record.request)
            attempt = await self.repository.begin_refresh(record, provider.descriptor.id)
            result = self._authoritative_provider_result(
                provider.descriptor.id, await provider.refresh(bound_candidate),
            )
            live = await self.repository.resolution(attempt, result)
            if not live and record.transfer_id == artifact.transfer_id:
                return
            if result.error:
                raise TransferError(result.error)
            if not result.candidates:
                raise TransferError(self._error(
                    Category.NO_TRANSFER_CANDIDATE, Stage.CANDIDATE_PREPARATION,
                    domain=Domain.RESOLUTION,
                ))
            if any(item.expires_at is not None and item.expires_at <= self.clock() for item in result.candidates):
                raise TransferError(self._error(
                    Category.CANDIDATE_EXPIRED, Stage.CANDIDATE_PREPARATION,
                    domain=Domain.RESOLUTION, retryability=Retryability.AFTER_RERESOLUTION,
                    recovery=Recovery.RERESOLVE,
                ))
            replacement_size = result.candidates[0].expected_bytes
            if (artifact.expected_bytes > 0 and replacement_size > 0
                    and not reported_sizes_compatible(artifact.expected_bytes, replacement_size)):
                raise TransferError(self._error(
                    Category.SIZE_MISMATCH, Stage.CANDIDATE_PREPARATION,
                    domain=Domain.INTEGRITY, retryability=Retryability.AFTER_RESOURCE_CHANGE,
                ))
            if not await self.canonical.refresh_candidate(artifact, origin, candidate, result.candidates):
                current = await self._current_artifact(artifact.transfer_id, artifact.id)
                if current is None or current.state == "completed":
                    return
                raise TransferError(self._error(
                    Category.OWNERSHIP_CONFLICT, Stage.CANDIDATE_PREPARATION,
                    domain=Domain.LIFECYCLE, retryability=Retryability.NEVER,
                ))
            size = artifact.expected_bytes if artifact.expected_bytes > 0 else replacement_size
            await self.repository.artifact_state(
                artifact.id, "queued", selected=artifact.selected, expected_bytes=max(0, size),
            )
        except Exception as exc:
            error = exc.error if isinstance(exc, TransferError) else unknown_failure(
                exc, integration_id=provider.descriptor.id if provider else "",
                domain=Domain.PROVIDER, stage=Stage.CANDIDATE_PREPARATION,
            )
            if attempt:
                await self.repository.resolution(
                    attempt, ResolutionResult(ResourceState.UNKNOWN, error=error),
                )
            current = await self._current_artifact(artifact.transfer_id, artifact.id)
            if current is None:
                return
            next_index = await self._next_alternate_index(current)
            parent_renewal = bool(record and record.parent_id and error.category in {
                Category.RESOURCE_NOT_FOUND, Category.RESOURCE_EXPIRED,
                Category.SOURCE_EXPIRED, Category.SOURCE_NOT_FOUND,
            })
            if parent_renewal and next_index is None:
                await self._terminal_recovery(current, error)
                await self._renew_source_parent(record)
                return
            await self.repository.record_source_failure(current.id, error)
            await self._decide_recovery(current, error)
