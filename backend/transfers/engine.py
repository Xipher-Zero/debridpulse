"""Universal transfer lifecycle.

Integrations supply facts through contracts. This owner admits requests, creates
durable attempts, applies retry policy, confirms possession, and orchestrates
cleanup and post-processing. It imports no concrete provider or executor.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import time

from transfers.contracts import CandidateRefresh, Cleanup, Inventory, Manifest, PauseResume, ResourceLookup
from transfers.errors import (
    Category, Domain, NormalizedError, Origin, Recovery, Retryability, Stage,
    TransferError, unknown_failure,
)
from transfers.filesystem import destination, retire_partial, safe_name, stable_payload
from transfers.models import (
    Artifact, CancellationInitiator, CleanupAuthority, CleanupDirective,
    ExecutionObservation, ExecutionRequest, ExecutionState, OutcomeKind, Ownership,
    RequestRecord, ResolutionResult, ResourceState, TransferOutcome, TransferRequest,
    TransferState, new_identity,
)
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


class TransferEngine:
    def __init__(self, repository: TransferRepository, registry: IntegrationRegistry, *,
                 download_root: str, policy: TransferPolicy | None = None, postprocessors=(), clock=time.time):
        self.repository = repository
        self.registry = registry
        self.root = str(Path(download_root).resolve())
        self.policy = policy or TransferPolicy()
        self.postprocessors = tuple(postprocessors)
        self.clock = clock
        self._cycle_lock = asyncio.Lock()
        self._dispatch_lock = asyncio.Lock()
        self._paths_lock = asyncio.Lock()
        self._control_lock = asyncio.Lock()
        self._resolution_slots = asyncio.Semaphore(max(1, self.policy.resolution_concurrency))
        self._transfer_locks: dict[int, asyncio.Lock] = {}

    async def initialize(self):
        await self.repository.initialize()

    async def _live(self, transfer_id: int, *, admission=False) -> bool:
        transfer = await self.repository.get(transfer_id)
        if not transfer or transfer.state in {TransferState.DELETED, TransferState.COMPLETED}:
            return False
        return not admission or (not transfer.paused and not await self.repository.globally_paused())

    @staticmethod
    def _error(category, stage, *, domain=Domain.INTERNAL, retryability=Retryability.UNKNOWN, recovery=Recovery.REQUIRE_OPERATOR):
        return NormalizedError(domain, category, stage, retryability=retryability, recovery=recovery)

    async def submit(self, requests: tuple[TransferRequest, ...], *, name="", source="manual", priority=0, reacquire=True, deduplicate=True):
        if not requests or len(requests) > 100 or any(not isinstance(item, TransferRequest) or not item.kind or not item.payload for item in requests):
            raise TransferError(self._error(Category.INVALID_REQUEST, Stage.SUBMISSION, domain=Domain.REQUEST, retryability=Retryability.NEVER))
        transfer, created = await self.repository.admit(requests, name=safe_name(name or requests[0].name or "Transfer"), source=source, priority=priority, deduplicate=deduplicate)
        if not created and reacquire and transfer.state in {TransferState.COMPLETED, TransferState.DELETED}:
            await self.retry(transfer.id, reacquire=True)
        elif await self.repository.globally_paused():
            await self.repository.state(transfer.id, TransferState.PAUSED)
        return await self.repository.get(transfer.id)

    async def tick(self):
        """One bounded scheduling/reconciliation cycle; retry delays never sleep a lock."""
        async with self._cycle_lock:
            await self._cleanup_pending()
            transfers = await self.repository.active()
            # Priority order is also dispatch order. Resolution uses a separate
            # bounded semaphore; accepted work is durable before this cycle.
            for transfer in transfers:
                lock = self._transfer_locks.setdefault(transfer.id, asyncio.Lock())
                async with lock:
                    await self._process(transfer.id)

    async def _process(self, transfer_id: int):
        if not await self._live(transfer_id, admission=True):
            return
        for record in await self.repository.requests(transfer_id):
            if record.retry_at > self.clock() or not await self._live(transfer_id, admission=True):
                continue
            if record.state == "pending":
                await self._resolve(record)
            elif record.state == "waiting":
                await self._observe_resource(record)
            elif record.state == "resolving":
                # Process restart during a non-idempotent provider submission:
                # never blindly repeat it. Inventory reconciliation can attach
                # an observed resource; absent evidence requires operator input.
                error = self._error(Category.RECOVERY_FAILED, Stage.RECONCILIATION, domain=Domain.RECONCILIATION)
                await self.repository.request_failure(record.id, error, None)
        for artifact in await self.repository.artifacts(transfer_id):
            if not await self._live(transfer_id, admission=True):
                break
            if artifact.execution and artifact.state in {"queued", "downloading", "unknown", "verifying", "paused"}:
                executor = self.registry.executors.get(artifact.execution.executor_id)
                if executor is None:
                    error = self._error(Category.UNSUPPORTED_CAPABILITY, Stage.RECONCILIATION, domain=Domain.REQUEST, retryability=Retryability.NEVER)
                    await self.repository.artifact_state(artifact.id, "error", error=error)
                    continue
                try:
                    observed = await executor.observe(artifact.execution)
                except Exception as exc:
                    observed = ExecutionObservation(artifact.execution, ExecutionState.UNKNOWN,
                        error=unknown_failure(exc, integration_id=executor.descriptor.id, domain=Domain.EXECUTOR, stage=Stage.RECONCILIATION))
                await self._execution_result(artifact, executor, observed)
            elif artifact.state == "queued" and artifact.retry_at <= self.clock():
                await self._dispatch(artifact)
        await self._aggregate(transfer_id)

    async def _request_failure(self, record: RequestRecord, error: NormalizedError, *, attempts=None, waiting=False):
        count = record.attempts + int(waiting) if attempts is None else attempts
        decision = self.policy.retry(error, count, self.clock(), can_refresh=True)
        retry_state = "waiting" if waiting and decision.action != Recovery.RERESOLVE else "pending"
        await self.repository.request_failure(record.id, error, decision.retry_at, retry_state=retry_state, consume_attempt=waiting)
        await self.repository.outcome(record.transfer_id, TransferOutcome(OutcomeKind.FAILURE, error))

    async def _resolve(self, record: RequestRecord):
        attempt = None
        provider = None
        try:
            provider = self.registry.provider_for(record.request)
            async with self._resolution_slots:
                if not await self._live(record.transfer_id, admission=True):
                    return
                attempt = await self.repository.begin_resolution(record.id, provider.descriptor.id)
                if attempt is None:
                    return
                result = await provider.resolve(record.request)
            if not isinstance(result, ResolutionResult):
                raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION))
            live = await self.repository.resolution(attempt, result)
            if not live:
                if await self.repository.delete_remote_requested(record.transfer_id):
                    await self._cleanup_resources(record.transfer_id, explicit=True)
                return
            if result.error:
                await self._request_failure(record, result.error, attempts=record.attempts + 1)
            elif result.candidates:
                await self._materialize(record, result.candidates)
            elif result.observation:
                if result.observation.name and record.parent_id is None:
                    await self.repository.rename(record.transfer_id, safe_name(result.observation.name))
                if result.observation.state == ResourceState.AVAILABLE:
                    await self._observe_resource(replace(record, resource=result.observation.resource, state="waiting", attempts=record.attempts + 1))
            else:
                raise TransferError(self._error(Category.NO_TRANSFER_CANDIDATE, Stage.RESOLUTION, domain=Domain.RESOLUTION))
        except Exception as exc:
            error = exc.error if isinstance(exc, TransferError) else unknown_failure(
                exc, integration_id=provider.descriptor.id if provider else "", domain=Domain.PROVIDER, stage=Stage.RESOLUTION,
                secrets=(str(record.request.payload),))
            if attempt:
                await self.repository.resolution(attempt, ResolutionResult(ResourceState.UNKNOWN, error=error))
            await self._request_failure(record, error, attempts=record.attempts + (1 if attempt else 0))

    async def _observe_resource(self, record: RequestRecord):
        provider = self.registry.providers.get(record.resource.provider_id) if record.resource else None
        try:
            if not isinstance(provider, ResourceLookup):
                raise TransferError(self._error(Category.UNSUPPORTED_CAPABILITY, Stage.RECONCILIATION, domain=Domain.REQUEST, retryability=Retryability.NEVER))
            observation = await provider.observe(record.resource)
            await self.repository.resource_observation(record.transfer_id, observation.resource, observation.state)
            if not await self._live(record.transfer_id, admission=True):
                return
            if observation.error:
                await self._request_failure(record, observation.error, waiting=True)
            elif observation.state == ResourceState.AVAILABLE:
                if not isinstance(provider, Manifest):
                    raise TransferError(self._error(Category.UNSUPPORTED_CAPABILITY, Stage.CANDIDATE_PREPARATION, domain=Domain.REQUEST, retryability=Retryability.NEVER))
                entries = await provider.manifest(record.resource)
                if not entries:
                    raise TransferError(self._error(Category.RESOLUTION_TEMPORARILY_FAILED, Stage.CANDIDATE_PREPARATION,
                        domain=Domain.RESOLUTION, retryability=Retryability.BACKOFF, recovery=Recovery.BACKOFF))
                paths = [str(destination(self.root, entry.relative_path)).casefold() for entry in entries]
                if len(paths) != len(set(paths)):
                    raise TransferError(self._error(Category.PATH_POLICY_VIOLATION, Stage.CANDIDATE_PREPARATION, domain=Domain.SECURITY))
                await self.repository.manifest(record, entries)
            elif observation.state in {ResourceState.ABSENT, ResourceState.EXPIRED}:
                error = self._error(Category.RESOURCE_EXPIRED if observation.state == ResourceState.EXPIRED else Category.RESOURCE_NOT_FOUND,
                    Stage.RESOLUTION, domain=Domain.PROVIDER, retryability=Retryability.AFTER_RERESOLUTION, recovery=Recovery.RERESOLVE)
                await self._request_failure(record, error, waiting=True)
            elif observation.state in {ResourceState.UNKNOWN, ResourceState.UNAVAILABLE}:
                raise TransferError(self._error(Category.UNMAPPED_PROVIDER_ERROR, Stage.RECONCILIATION, domain=Domain.PROVIDER))
        except Exception as exc:
            error = exc.error if isinstance(exc, TransferError) else unknown_failure(exc,
                integration_id=provider.descriptor.id if provider else "", domain=Domain.PROVIDER, stage=Stage.RECONCILIATION)
            await self._request_failure(record, error, waiting=True)

    async def _materialize(self, record: RequestRecord, candidates):
        if any(not candidate.endpoints or candidate.expected_bytes < 0 for candidate in candidates):
            raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.CANDIDATE_PREPARATION))
        candidates = tuple(sorted(candidates, key=lambda candidate: -candidate.priority))
        if record.entry:
            candidates = tuple(replace(candidate, name=record.entry.name, relative_path=record.entry.relative_path,
                                       expected_bytes=candidate.expected_bytes or record.entry.expected_bytes) for candidate in candidates)
        existing = next((item for item in await self.repository.artifacts(record.transfer_id) if item.request_id == record.id), None)
        if existing:
            await self.repository.materialize(record, candidates, existing.target)
            return
        transfer = await self.repository.get(record.transfer_id)
        relative = candidates[0].relative_path or candidates[0].name
        if record.parent_id:
            relative = str(Path(safe_name(transfer.name)) / relative)
        async with self._paths_lock:
            target = destination(self.root, relative)
            occupied = await self.repository.occupied_paths()
            if record.parent_id and str(target).casefold() in occupied:
                raise TransferError(self._error(Category.LOCAL_PATH_CONFLICT, Stage.CANDIDATE_PREPARATION,
                    domain=Domain.LOCAL_RESOURCE, retryability=Retryability.AFTER_RESOURCE_CHANGE))
            if not record.parent_id:
                original, index = target, 2
                while target.exists() or target.is_symlink() or str(target).casefold() in occupied:
                    target = original.with_name(f"{original.stem} ({index}){original.suffix}")
                    index += 1
            await self.repository.materialize(record, candidates, str(target))

    async def _dispatch(self, artifact: Artifact):
        try:
            candidate = artifact.candidates[artifact.selected]
            executor = self.registry.executor_for(candidate)
            sidecars = executor.resumable_paths(artifact.target)
            if await stable_payload(artifact.target, artifact.expected_bytes, sidecars=sidecars, integrity=candidate.integrity,
                                    delay=self.policy.adoption_stability_seconds):
                await self.repository.artifact_state(artifact.id, "completed")
                return
            if candidate.expires_at is not None and candidate.expires_at <= self.clock():
                await self._refresh(artifact)
                return
            request = ExecutionRequest(candidate, artifact.target, new_identity())
            handle = executor.prepare(request)
            async with self._dispatch_lock:
                if not await self._live(artifact.transfer_id, admission=True):
                    return
                attempts = await self.repository.executions()
                occupied = sum(attempt.state in {"prepared", "queued", "transferring", "unknown"} for attempt in attempts)
                if occupied >= max(1, self.policy.max_active_executions):
                    return
                if not await self.repository.prepare_execution(artifact, handle):
                    return
            try:
                observed = await executor.start(request, handle)
            except Exception as exc:
                observed = ExecutionObservation(handle, ExecutionState.UNKNOWN,
                    error=unknown_failure(exc, integration_id=executor.descriptor.id, domain=Domain.EXECUTOR, stage=Stage.QUEUE))
            current = next(item for item in await self.repository.artifacts(artifact.transfer_id) if item.id == artifact.id)
            await self._execution_result(current, executor, observed)
        except Exception as exc:
            error = exc.error if isinstance(exc, TransferError) else unknown_failure(exc, integration_id="", domain=Domain.INTERNAL, stage=Stage.QUEUE)
            await self.repository.artifact_state(artifact.id, "error", error=error)

    async def _execution_result(self, artifact, executor, observed):
        if not isinstance(observed, ExecutionObservation) or observed.handle != artifact.execution:
            raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.RECONCILIATION))
        await self.repository.execution(observed)
        if not await self._live(artifact.transfer_id):
            await executor.cancel(observed.handle)
            return
        if observed.state == ExecutionState.SUCCEEDED:
            candidate = artifact.candidates[artifact.selected] if artifact.candidates else None
            size = artifact.expected_bytes or observed.progress.total_bytes
            valid = await stable_payload(artifact.target, size, sidecars=executor.resumable_paths(artifact.target),
                                         integrity=candidate.integrity if candidate else (), delay=self.policy.adoption_stability_seconds)
            if valid:
                await self.repository.artifact_state(artifact.id, "completed")
            else:
                error = self._error(Category.MATERIALIZATION_FAILED, Stage.VERIFICATION, domain=Domain.INTEGRITY,
                                    retryability=Retryability.AFTER_RESOURCE_CHANGE)
                await self.repository.artifact_state(artifact.id, "error", error=error)
                await self.repository.outcome(artifact.transfer_id, TransferOutcome(OutcomeKind.FAILURE, error), attempt_id=observed.handle.attempt_id)
        elif observed.state == ExecutionState.FAILED:
            error = observed.error or self._error(Category.UNMAPPED_EXECUTOR_ERROR, Stage.EXECUTION, domain=Domain.EXECUTOR)
            await self._recover_artifact(artifact, error)
        elif observed.state == ExecutionState.ABSENT:
            error = self._error(Category.ORPHANED_RESOURCE, Stage.RECONCILIATION, domain=Domain.RECONCILIATION,
                                retryability=Retryability.BACKOFF, recovery=Recovery.RETRY)
            await self._recover_artifact(artifact, error)
        elif observed.state == ExecutionState.CANCELLED:
            await self.repository.outcome(artifact.transfer_id, TransferOutcome(OutcomeKind.CANCELLED,
                cancellation_initiator=CancellationInitiator.EXECUTOR), attempt_id=observed.handle.attempt_id)

    async def _recover_artifact(self, artifact: Artifact, error: NormalizedError):
        if not artifact.candidates:
            decision = self.policy.retry(error, artifact.retries, self.clock(), can_refresh=True)
            if decision.automatic:
                await self.repository.artifact_state(artifact.id, "unresolved", release=True)
                await self.repository.retry_requests(artifact.transfer_id, request_id=artifact.request_id)
            else:
                await self.repository.artifact_state(artifact.id, "error", error=error)
            return
        candidate = artifact.candidates[artifact.selected]
        provider = self.registry.providers.get(candidate.provider_id)
        decision = self.policy.retry(error, artifact.retries, self.clock(),
            can_refresh=isinstance(provider, CandidateRefresh), has_alternate=artifact.selected + 1 < len(artifact.candidates))
        await self.repository.outcome(artifact.transfer_id, TransferOutcome(OutcomeKind.FAILURE, error),
                                      attempt_id=artifact.execution.attempt_id if artifact.execution else None)
        if not decision.automatic:
            await self.repository.artifact_state(artifact.id, "error", error=error)
            return
        if decision.action == Recovery.RECONCILE:
            return
        if artifact.execution:
            executor = self.registry.executors[artifact.execution.executor_id]
            retired = await executor.cancel(artifact.execution)
            if retired.error:
                await self.repository.artifact_state(artifact.id, "error", error=retired.error)
                return
        if decision.action == Recovery.TRY_ALTERNATE_CANDIDATE:
            sidecars = self.registry.executors[artifact.execution.executor_id].resumable_paths(artifact.target) if artifact.execution else ()
            retire_partial(self.root, artifact.target, sidecars)
            await self.repository.artifact_state(artifact.id, "queued", retry_at=decision.retry_at, release=True, selected=artifact.selected + 1)
        elif decision.action == Recovery.RERESOLVE:
            await self.repository.artifact_state(artifact.id, "queued", retry_at=decision.retry_at, release=True)
            await self._refresh(artifact)
        else:
            await self.repository.artifact_state(artifact.id, "queued", error=error, retry_at=decision.retry_at, release=True)

    async def _refresh(self, artifact: Artifact):
        candidate = artifact.candidates[artifact.selected]
        provider = self.registry.providers.get(candidate.provider_id)
        attempt = None
        try:
            if not isinstance(provider, CandidateRefresh):
                raise TransferError(self._error(Category.UNSUPPORTED_CAPABILITY, Stage.CANDIDATE_PREPARATION,
                    domain=Domain.REQUEST, retryability=Retryability.NEVER))
            if not await self._live(artifact.transfer_id, admission=True):
                return
            record = next(item for item in await self.repository.requests(artifact.transfer_id) if item.id == artifact.request_id)
            attempt = await self.repository.begin_refresh(record, provider.descriptor.id)
            result = await provider.refresh(candidate)
            live = await self.repository.resolution(attempt, result)
            if not live:
                return
            if result.error:
                raise TransferError(result.error)
            if not result.candidates:
                raise TransferError(self._error(Category.NO_TRANSFER_CANDIDATE, Stage.CANDIDATE_PREPARATION, domain=Domain.RESOLUTION))
            if any(item.expires_at is not None and item.expires_at <= self.clock() for item in result.candidates):
                raise TransferError(self._error(Category.CANDIDATE_EXPIRED, Stage.CANDIDATE_PREPARATION, domain=Domain.RESOLUTION))
            await self.repository.materialize(record, result.candidates, artifact.target)
        except Exception as exc:
            error = exc.error if isinstance(exc, TransferError) else unknown_failure(exc,
                integration_id=provider.descriptor.id if provider else "", domain=Domain.PROVIDER, stage=Stage.CANDIDATE_PREPARATION)
            if attempt:
                await self.repository.resolution(attempt, ResolutionResult(ResourceState.UNKNOWN, error=error))
            await self.repository.artifact_state(artifact.id, "error", error=error)

    async def _aggregate(self, transfer_id: int):
        if not await self._live(transfer_id):
            return
        transfer = await self.repository.get(transfer_id)
        requests = await self.repository.requests(transfer_id)
        artifacts = await self.repository.artifacts(transfer_id)
        pending = any(item.state in {"pending", "waiting", "resolving"} for item in requests)
        attempts = {item.handle.attempt_id: item for item in await self.repository.executions(transfer_id)}
        total = sum(item.expected_bytes for item in artifacts)
        completed = sum(item.expected_bytes if item.state == "completed" else min(item.expected_bytes,
            attempts[item.execution.attempt_id].progress.completed_bytes) if item.execution else 0 for item in artifacts)
        progress = min(100.0, completed / total * 100) if total else 0.0
        if transfer.paused or await self.repository.globally_paused():
            return
        if artifacts and all(item.state == "completed" for item in artifacts) and not pending:
            await self._complete(transfer_id, artifacts)
        elif any(item.state in {"downloading", "unknown", "verifying"} for item in artifacts):
            await self.repository.state(transfer_id, TransferState.TRANSFERRING, progress=progress)
        elif any(item.state == "queued" for item in artifacts):
            await self.repository.state(transfer_id, TransferState.QUEUED, progress=progress)
        elif pending:
            await self.repository.state(transfer_id, TransferState.RESOLVING, progress=progress)
        elif any(item.state == "error" for item in artifacts) or any(item.state == "failed" for item in requests):
            error = next((item.error for item in (*artifacts, *requests) if item.error), None)
            await self.repository.state(transfer_id, TransferState.FAILED, progress=progress, error=error)
        elif artifacts and all(item.state == "cancelled" for item in artifacts):
            await self.repository.state(transfer_id, TransferState.CANCELLED, progress=progress)

    async def _complete(self, transfer_id: int, artifacts):
        if self.postprocessors:
            await self.repository.state(transfer_id, TransferState.POST_PROCESSING, progress=100)
            for processor in self.postprocessors:
                if not await self._live(transfer_id):
                    return
                try:
                    outcome = await processor.process(transfer_id, tuple(item.target for item in artifacts))
                except Exception as exc:
                    outcome = TransferOutcome(OutcomeKind.FAILURE, unknown_failure(exc,
                        integration_id=processor.descriptor.id, domain=Domain.POST_PROCESSING, stage=Stage.POST_PROCESSING))
                await self.repository.outcome(transfer_id, outcome)
        if await self.repository.state(transfer_id, TransferState.COMPLETED, progress=100):
            await self.repository.outcome(transfer_id, TransferOutcome(OutcomeKind.SUCCESS))
            if self.policy.cleanup_after_completion:
                await self._cleanup_resources(transfer_id)

    async def pause(self, transfer_id: int):
        async with self._control_lock:
            await self.repository.pause_intent(transfer_id, True)
            return await self._control(transfer_id, resume=False)

    async def resume(self, transfer_id: int):
        async with self._control_lock:
            if await self.repository.globally_paused():
                for transfer in await self.repository.active():
                    if transfer.id != transfer_id:
                        await self.repository.pause_intent(transfer.id, True)
                await self.repository.global_pause(False)
            await self.repository.pause_intent(transfer_id, False)
            return await self._control(transfer_id, resume=True)

    async def _control(self, transfer_id: int, *, resume: bool):
        results = []
        for artifact in await self.repository.artifacts(transfer_id):
            if not artifact.execution or artifact.state == "completed":
                continue
            executor = self.registry.executors.get(artifact.execution.executor_id)
            if not isinstance(executor, PauseResume):
                results.append(self._error(Category.UNSUPPORTED_CAPABILITY, Stage.EXECUTION, domain=Domain.REQUEST, retryability=Retryability.NEVER))
                continue
            try:
                observed = await (executor.resume(artifact.execution) if resume else executor.pause(artifact.execution))
                await self.repository.execution(observed)
                if observed.error:
                    results.append(observed.error)
                elif resume and observed.state in {ExecutionState.ABSENT, ExecutionState.CANCELLED}:
                    await self.repository.artifact_state(artifact.id, "queued", release=True)
            except Exception as exc:
                results.append(unknown_failure(exc, integration_id=executor.descriptor.id, domain=Domain.EXECUTOR, stage=Stage.EXECUTION))
        if not results:
            await self.repository.state(transfer_id, TransferState.QUEUED if resume else TransferState.PAUSED)
        return tuple(results)

    async def pause_all(self):
        async with self._control_lock:
            await self.repository.global_pause(True)
            return {transfer.id: await self._control(transfer.id, resume=False) for transfer in await self.repository.active()}

    async def resume_all(self):
        async with self._control_lock:
            await self.repository.global_pause(False)
            results = {}
            for transfer in await self.repository.active():
                await self.repository.pause_intent(transfer.id, False)
                results[transfer.id] = await self._control(transfer.id, resume=True)
            return results

    async def retry(self, transfer_id: int, *, reacquire=False):
        lock = self._transfer_locks.setdefault(transfer_id, asyncio.Lock())
        async with lock:
            transfer = await self.repository.get(transfer_id)
            if transfer is None:
                raise KeyError(transfer_id)
            if transfer.state == TransferState.DELETED and not reacquire:
                return False
            if any(pending for _resource, _state, pending in await self.repository.resources(transfer_id)):
                return False
            plan = []
            for artifact in await self.repository.artifacts(transfer_id):
                candidate = artifact.candidates[artifact.selected] if artifact.candidates else None
                executor = self.registry.executors.get(artifact.execution.executor_id) if artifact.execution else self.registry.executor_for(candidate) if candidate else None
                if executor is None:
                    if candidate is None and artifact.execution is None:
                        plan.append((artifact, None, None, None))
                        continue
                    return False
                observation = None
                if artifact.execution:
                    observation = await executor.observe(artifact.execution)
                    if observation.state == ExecutionState.UNKNOWN:
                        return False
                plan.append((artifact, candidate, executor, observation))
            if not await self.repository.state(transfer_id, TransferState.ACCEPTED if reacquire else TransferState.QUEUED,
                                               operator=True, expected_epoch=transfer.epoch):
                return False
            for artifact, candidate, executor, observation in plan:
                if observation:
                    if observation.resumable:
                        await self.repository.execution(observation)
                        continue
                if candidate is None:
                    if artifact.execution:
                        outcome = await executor.cancel(artifact.execution)
                        if outcome.error:
                            return False
                    await self.repository.artifact_state(artifact.id, "unresolved", release=True)
                    await self.repository.retry_requests(transfer_id, request_id=artifact.request_id)
                    continue
                if await stable_payload(artifact.target, artifact.expected_bytes, sidecars=executor.resumable_paths(artifact.target),
                                        integrity=candidate.integrity, delay=self.policy.adoption_stability_seconds):
                    await self.repository.artifact_state(artifact.id, "completed")
                    continue
                if artifact.execution:
                    outcome = await executor.cancel(artifact.execution)
                    if outcome.error:
                        return False
                await self.repository.artifact_state(artifact.id, "queued", release=True)
            await self.repository.retry_requests(transfer_id)
            await self.repository.pause_intent(transfer_id, False)
            return True

    async def delete(self, transfer_id: int, *, remote=True):
        # Persist the tombstone before waiting on any integration. Every async
        # completion path rechecks it, including late provider resource creation.
        await self.repository.delete(transfer_id, remote=remote)
        for attempt in await self.repository.executions(transfer_id):
            executor = self.registry.executors.get(attempt.handle.executor_id)
            if executor:
                outcome = await executor.cancel(attempt.handle)
                await self.repository.outcome(transfer_id, outcome, attempt_id=attempt.handle.attempt_id)
        if remote:
            await self._cleanup_resources(transfer_id, explicit=True)

    async def _cleanup_resources(self, transfer_id: int, *, explicit=False):
        for resource, state, pending in await self.repository.resources(transfer_id):
            if state == ResourceState.ABSENT:
                continue
            if not explicit and resource.ownership not in {Ownership.CREATED, Ownership.ADOPTED}:
                continue
            authority = CleanupAuthority.USER_REQUEST if explicit else CleanupAuthority.OWNED
            await self.repository.cleanup_intent(resource.id, authority)
        await self._cleanup_pending()

    async def _cleanup_pending(self):
        for transfer_id, resource, authority, attempts in await self.repository.pending_cleanup(self.clock()):
            provider = self.registry.providers.get(resource.provider_id)
            if not isinstance(provider, Cleanup):
                continue
            if not await self.repository.claim_cleanup(resource.id):
                continue
            try:
                outcome = await provider.cleanup(CleanupDirective(resource, CleanupAuthority(authority)))
            except Exception as exc:
                outcome = TransferOutcome(OutcomeKind.FAILURE, unknown_failure(exc,
                    integration_id=provider.descriptor.id, domain=Domain.CLEANUP, stage=Stage.CLEANUP))
            await self.repository.outcome(transfer_id, outcome)
            if outcome.kind in {OutcomeKind.SUCCESS, OutcomeKind.SKIPPED}:
                await self.repository.cleanup_intent(resource.id, None)
                if outcome.kind == OutcomeKind.SUCCESS:
                    await self.repository.resource_observation(transfer_id, resource, ResourceState.ABSENT)
            else:
                error = outcome.error or self._error(Category.REMOTE_CLEANUP_FAILED, Stage.CLEANUP, domain=Domain.CLEANUP)
                decision = self.policy.retry(error, attempts + 1, self.clock())
                await self.repository.cleanup_retry(resource.id, error, decision.retry_at)

    async def reconcile_inventory(self):
        """Missing from an incomplete inventory is never evidence of absence."""
        reports = []
        for provider in self.registry.providers.values():
            if not provider.descriptor.enabled or not isinstance(provider, Inventory):
                continue
            try:
                snapshot = await provider.inventory()
            except Exception as exc:
                reports.append(unknown_failure(exc, integration_id=provider.descriptor.id, domain=Domain.PROVIDER, stage=Stage.RECONCILIATION))
                continue
            if snapshot.error:
                reports.append(snapshot.error)
                continue
            known = {resource.id: transfer.id for transfer in await self.repository.active()
                     for resource, _state, _pending in await self.repository.resources(transfer.id)}
            for item in snapshot.observations:
                if item.resource.id in known:
                    await self.repository.resource_observation(known[item.resource.id], item.resource, item.state)
                elif item.request:
                    transfer = await self.submit((item.request,), name=item.name, source="inventory", reacquire=False)
                    await self.repository.resource_observation(transfer.id, item.resource, item.state)
                    await self.repository.attach_inventory(transfer.id, item.resource)
            # Per-resource lookup in normal reconciliation confirms absence.
            # A short bulk window can neither delete nor restart local work.
        return tuple(reports)
