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
from weakref import WeakValueDictionary

from transfers.contracts import (BatchObservation, CandidateRefresh, Cleanup, ExecutorInputContinuation, ExecutorInputRecovery,
    Inventory, Manifest, PauseResume, ProviderInputContinuation, ResourceLookup)
from transfers import codec
from transfers.errors import (
    Category, Domain, NormalizedError, Origin, Recovery, Retryability, Stage,
    TransferError, unknown_failure,
)
from transfers.filesystem import destination, payload_matches, retire_partial, safe_name, stable_payload, validate_target
from transfers.input_required import EphemeralInputBroker, InputChallengeStore, InputSubmissionRejected
from transfers.models import (
    Artifact, CancellationInitiator, CleanupAuthority, CleanupDirective,
    ExecutionHandle, ExecutionObservation, ExecutionRequest, ExecutionState, InputChallenge, InputOrigin, InputRequirement,
    OutcomeKind, Ownership, RequestRecord, ResolutionAttempt, ResolutionResult, ResourceState, TransferOutcome, TransferRequest,
    TransferCandidate, TransferState, new_identity,
)
from transfers.mirrors import shared_size
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


class TransferEngine:
    def __init__(self, repository: TransferRepository, registry: IntegrationRegistry, *,
                 download_root: str, policy: TransferPolicy | None = None, postprocessors=(), clock=time.time):
        self.repository = repository
        self.registry = registry
        self.challenges = InputChallengeStore(clock=clock)
        self.inputs = EphemeralInputBroker(clock=clock)
        self.root = str(Path(download_root).resolve())
        self.policy = policy or TransferPolicy()
        self.postprocessors = tuple(postprocessors)
        self.clock = clock
        self._cycle_lock = asyncio.Lock()
        self._resolution_cycle_lock = asyncio.Lock()
        self._execution_cycle_lock = asyncio.Lock()
        self._dispatch_lock = asyncio.Lock()
        self._paths_lock = asyncio.Lock()
        self._control_lock = asyncio.Lock()
        self._postprocess_lock = asyncio.Lock()
        self._resolution_slots = asyncio.Semaphore(max(1, self.policy.resolution_concurrency))
        self._transfer_locks = WeakValueDictionary()
        self.dispatch_permitted = True

    async def initialize(self):
        await self.repository.initialize()
        await self.challenges.initialize()

    def configure_policy(self, policy):
        """Called only after application admission has drained active work."""
        self.policy = policy
        self._resolution_slots = asyncio.Semaphore(max(1, policy.resolution_concurrency))

    async def _live(self, transfer_id: int, *, admission=False) -> bool:
        transfer = await self.repository.get(transfer_id)
        if not transfer or transfer.state in {TransferState.DELETED, TransferState.COMPLETED, TransferState.CANCELLED}:
            return False
        return not admission or (not transfer.paused and not await self.repository.globally_paused())

    @staticmethod
    def _error(category, stage, *, domain=Domain.INTERNAL, retryability=Retryability.UNKNOWN, recovery=Recovery.REQUIRE_OPERATOR):
        return NormalizedError(domain, category, stage, retryability=retryability, recovery=recovery)

    @classmethod
    def _authoritative_provider_result(cls, provider_id: str, result: ResolutionResult) -> ResolutionResult:
        """Validate and stamp provider output with the selected route identity."""
        if not isinstance(result, ResolutionResult):
            raise TransferError(cls._error(
                Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION, domain=Domain.PROVIDER,
                retryability=Retryability.NEVER,
            ))

        def authoritative_resource(value):
            if value is None:
                return None
            if value.provider_id and value.provider_id != provider_id:
                raise TransferError(cls._error(
                    Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION, domain=Domain.PROVIDER,
                    retryability=Retryability.NEVER,
                ))
            return value if value.provider_id == provider_id else replace(value, provider_id=provider_id)

        candidates = []
        for candidate in result.candidates:
            if not isinstance(candidate, TransferCandidate):
                raise TransferError(cls._error(
                    Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION, domain=Domain.PROVIDER,
                    retryability=Retryability.NEVER,
                ))
            if candidate.provider_id and candidate.provider_id != provider_id:
                raise TransferError(cls._error(
                    Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION, domain=Domain.PROVIDER,
                    retryability=Retryability.NEVER,
                ))
            candidates.append(replace(
                candidate, provider_id=provider_id, resource=authoritative_resource(candidate.resource),
            ))

        observation = result.observation
        if observation is not None:
            observation = replace(observation, resource=authoritative_resource(observation.resource))
        return replace(result, candidates=tuple(candidates), observation=observation)

    async def submit(self, requests: tuple[TransferRequest, ...], *, name="", source="manual", priority=0, reacquire=True, deduplicate=True):
        if not requests or len(requests) > 100 or any(not isinstance(item, TransferRequest) or not item.kind or not item.payload for item in requests):
            raise TransferError(self._error(Category.INVALID_REQUEST, Stage.SUBMISSION, domain=Domain.REQUEST, retryability=Retryability.NEVER))
        transfer, created = await self.repository.admit(requests, name=safe_name(name or requests[0].name or "Transfer"), source=source, priority=priority, deduplicate=deduplicate)
        if not created and reacquire and transfer.state in {TransferState.COMPLETED, TransferState.DELETED}:
            if not await self.retry(transfer.id, reacquire=True):
                raise TransferError(self._error(Category.RECOVERY_FAILED, Stage.RECONCILIATION, domain=Domain.RECONCILIATION))
        elif await self.repository.globally_paused():
            await self.repository.state(transfer.id, TransferState.PAUSED)
        return await self.repository.get(transfer.id)

    async def tick(self):
        """One bounded scheduling/reconciliation cycle; retry delays never sleep a lock."""
        async with self._cycle_lock:
            await self.resolve_pending()
            await self.reconcile_executions()
            await self.process_postprocessors()

    async def resolve_pending(self):
        """Provider cadence can run independently of fast execution observation."""
        async with self._resolution_cycle_lock:
            await self._cleanup_pending()
            transfers = await self.repository.active()
            async def resolve_transfer(transfer):
                lock = self._transfer_locks.setdefault(transfer.id, asyncio.Lock())
                async with lock:
                    if not await self._live(transfer.id, admission=True):
                        return
                    challenge = await self.challenges.current(transfer.id)
                    if challenge:
                        if challenge.origin == InputOrigin.PROVIDER:
                            await self._continue_provider_input(challenge)
                        return
                    records = await self.repository.requests(transfer.id)
                    await asyncio.gather(*(self._process_request(record) for record in records))
            await asyncio.gather(*(resolve_transfer(transfer) for transfer in transfers))

    async def _process_request(self, record: RequestRecord):
        if record.retry_at > self.clock() or not await self._live(record.transfer_id, admission=True):
            return
        try:
            if record.state == "pending":
                await self._resolve(record)
            elif record.state == "waiting":
                async with self._resolution_slots:
                    await self._observe_resource(record)
            elif record.state == "materializing":
                candidates = await self.repository.resolved_candidates(record.id)
                if candidates:
                    await self._materialize(record, candidates)
                else:
                    raise TransferError(self._error(Category.RECOVERY_FAILED, Stage.RECONCILIATION, domain=Domain.RECONCILIATION))
            elif record.state == "resolving":
                # Process restart during a non-idempotent provider submission:
                # never blindly repeat it. Inventory reconciliation can attach
                # an observed resource; absent evidence requires operator input.
                error = self._error(Category.RECOVERY_FAILED, Stage.RECONCILIATION, domain=Domain.RECONCILIATION)
                await self.repository.request_failure(record.id, error, None)
        except Exception as exc:
            error = exc.error if isinstance(exc, TransferError) else unknown_failure(exc, integration_id="", domain=Domain.INTERNAL, stage=Stage.RECONCILIATION)
            await self._request_failure(record, error)

    async def reconcile_executions(self):
        """Observe each current attempt once, then dispatch in transfer priority order."""
        async with self._execution_cycle_lock:
            transfers = await self.repository.active()
            artifacts_by_transfer = {transfer.id: await self.repository.artifacts(transfer.id) for transfer in transfers}
            challenges = {transfer.id: await self.challenges.current(transfer.id) for transfer in transfers}
            grouped = {}
            for transfer in transfers:
                for artifact in artifacts_by_transfer[transfer.id]:
                    if artifact.execution and artifact.state in {"queued", "downloading", "unknown", "verifying", "paused"}:
                        grouped.setdefault(artifact.execution.executor_id, []).append(artifact.execution)
            observations = {}
            for executor_id, handles in grouped.items():
                executor = self.registry.executors.get(executor_id)
                if not isinstance(executor, BatchObservation):
                    continue
                try:
                    snapshot = await executor.observe_many(tuple(handles))
                    if snapshot.error:
                        for handle in handles:
                            observations[handle.attempt_id] = ExecutionObservation(handle, ExecutionState.UNKNOWN, error=snapshot.error)
                    else:
                        requested = {handle.attempt_id: handle for handle in handles}
                        for observation in snapshot.observations:
                            if requested.get(observation.handle.attempt_id) != observation.handle:
                                raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.RECONCILIATION))
                            observations[observation.handle.attempt_id] = observation
                        if any(handle.attempt_id not in observations for handle in handles):
                            raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.RECONCILIATION))
                except Exception as exc:
                    error = exc.error if isinstance(exc, TransferError) else unknown_failure(exc, integration_id=executor_id, domain=Domain.EXECUTOR, stage=Stage.RECONCILIATION)
                    for handle in handles:
                        observations[handle.attempt_id] = ExecutionObservation(handle, ExecutionState.UNKNOWN, error=error)
            for transfer in transfers:
                challenge = challenges[transfer.id]
                await self._process_executions(transfer.id, artifacts_by_transfer[transfer.id], observations,
                                               dispatch_allowed=challenge is None)
                if challenge and challenge.origin == InputOrigin.EXECUTOR and await self._live(transfer.id, admission=True):
                    await self._continue_executor_input(challenge, await self.repository.artifacts(transfer.id))

    async def _process_executions(self, transfer_id, artifacts, observations, *, dispatch_allowed=True):
        for artifact in artifacts:
            if not await self._live(transfer_id, admission=True):
                break
            try:
                if artifact.execution and artifact.state in {"queued", "downloading", "unknown", "verifying", "paused"}:
                    executor = self.registry.executors.get(artifact.execution.executor_id)
                    if executor is None:
                        error = self._error(Category.UNSUPPORTED_CAPABILITY, Stage.RECONCILIATION, domain=Domain.REQUEST, retryability=Retryability.NEVER)
                        await self.repository.artifact_state(artifact.id, "error", error=error)
                        continue
                    try:
                        observed = observations.get(artifact.execution.attempt_id)
                        if observed is None:
                            observed = await executor.observe(artifact.execution)
                    except Exception as exc:
                        observed = ExecutionObservation(artifact.execution, ExecutionState.UNKNOWN,
                            error=unknown_failure(exc, integration_id=executor.descriptor.id, domain=Domain.EXECUTOR, stage=Stage.RECONCILIATION))
                    if not isinstance(observed, ExecutionObservation) or observed.handle != artifact.execution:
                        raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.RECONCILIATION))
                    if observed.state == ExecutionState.PAUSED and isinstance(executor, PauseResume):
                        observed = await self._resume_execution(artifact, executor)
                    await self._execution_result(artifact, executor, observed)
                elif dispatch_allowed and artifact.state == "queued" and artifact.retry_at <= self.clock():
                    await self._dispatch(artifact)
                elif dispatch_allowed and artifact.state == "refresh_pending" and artifact.retry_at <= self.clock():
                    await self._refresh(artifact)
            except Exception as exc:
                error = exc.error if isinstance(exc, TransferError) else unknown_failure(exc,
                    integration_id=artifact.execution.executor_id if artifact.execution else "",
                    domain=Domain.RECONCILIATION, stage=Stage.RECONCILIATION)
                await self.repository.artifact_state(artifact.id, "error", error=error)
                await self.repository.outcome(transfer_id, TransferOutcome(OutcomeKind.FAILURE, error))
        await self._aggregate(transfer_id)

    async def _request_failure(self, record: RequestRecord, error: NormalizedError, *, attempts=None, waiting=False):
        count = record.attempts + int(waiting) if attempts is None else attempts
        decision = self.policy.retry_resolution(error, count, self.clock())
        retry_state = "waiting" if waiting and decision.action != Recovery.RERESOLVE else "pending"
        await self.repository.request_failure(record.id, error, decision.retry_at, retry_state=retry_state, consume_attempt=waiting)
        await self.repository.outcome(record.transfer_id, TransferOutcome(OutcomeKind.FAILURE, error))

    async def _resolve(self, record: RequestRecord):
        attempt = None
        provider = None
        try:
            if record.resource and record.parent_id is None:
                previous_provider = self.registry.providers.get(record.resource.provider_id)
                if not isinstance(previous_provider, ResourceLookup):
                    raise TransferError(self._error(Category.UNSUPPORTED_CAPABILITY, Stage.RECONCILIATION))
                previous = await previous_provider.observe(record.resource)
                await self.repository.resource_observation(record.transfer_id, previous.resource, previous.state)
                restartable = previous.state in {ResourceState.EXPIRED, ResourceState.ABSENT} or (
                    previous.state == ResourceState.UNAVAILABLE and previous.error is not None
                    and previous.error.retryability not in {Retryability.NEVER, Retryability.UNKNOWN}
                    and previous.error.domain != Domain.SECURITY
                    and previous.error.recovery in {Recovery.RETRY, Recovery.RERESOLVE, Recovery.BACKOFF})
                if previous.error and not restartable:
                    raise TransferError(previous.error)
                if previous.state in {ResourceState.PREPARING, ResourceState.AVAILABLE}:
                    # Retry observes an existing resource rather than uploading a
                    # second copy when it may still be preparing or available.
                    await self.repository.poll_after(record.id, self.clock(), waiting=True)
                    return
                if not restartable:
                    raise TransferError(self._error(Category.UNMAPPED_PROVIDER_ERROR, Stage.RECONCILIATION, domain=Domain.PROVIDER))
                if previous.state != ResourceState.ABSENT and record.resource.ownership in {Ownership.CREATED, Ownership.ADOPTED}:
                    await self.repository.cleanup_intent(record.resource.id, CleanupAuthority.OWNED)
                    await self._cleanup_pending()
                    if any(resource.id == record.resource.id and pending for resource, _state, pending in await self.repository.resources(record.transfer_id)):
                        raise TransferError(self._error(Category.REMOTE_CLEANUP_FAILED, Stage.CLEANUP, domain=Domain.CLEANUP))
            bound_provider_id = await self.repository.bound_route_provider(record.id)
            provider = (
                self.registry.provider_for_bound_route(bound_provider_id, record.request)
                if bound_provider_id else self.registry.provider_for(record.request)
            )
            async with self._resolution_slots:
                if not await self._live(record.transfer_id, admission=True):
                    return
                attempt = await self.repository.begin_resolution(record.id, provider.descriptor.id)
                if attempt is None:
                    return
                result = await provider.resolve(record.request)
            await self._apply_resolution(record, attempt, provider, result)
        except Exception as exc:
            error = exc.error if isinstance(exc, TransferError) else unknown_failure(
                exc, integration_id=provider.descriptor.id if provider else "", domain=Domain.PROVIDER, stage=Stage.RESOLUTION,
                secrets=(str(record.request.payload),))
            if attempt:
                await self.repository.resolution(attempt, ResolutionResult(ResourceState.UNKNOWN, error=error))
            await self._request_failure(record, error, attempts=record.attempts + (1 if attempt else 0))


    async def _apply_resolution(self, record: RequestRecord, attempt: ResolutionAttempt, provider, result: ResolutionResult,
                                *, challenge: InputChallenge | None = None):
        result = self._authoritative_provider_result(provider.descriptor.id, result)
        if result.input_required:
            if result.error or result.candidates or result.observation or not isinstance(result.input_required, InputRequirement):
                raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION))
            if not isinstance(provider, ProviderInputContinuation):
                raise TransferError(self._error(Category.UNSUPPORTED_CAPABILITY, Stage.RESOLUTION, domain=Domain.REQUEST,
                                                retryability=Retryability.NEVER))
            if challenge:
                await self.challenges.replace(challenge, result.input_required)
            else:
                await self.challenges.wait_provider(attempt, result.input_required, provider.descriptor.id)
            return
        live = await self.repository.resolution(attempt, result)
        if challenge:
            await self.challenges.clear(challenge)
            await self.inputs.clear(challenge.id)
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

    async def _continue_provider_input(self, challenge: InputChallenge):
        if not await self.inputs.has(challenge) or not await self._live(challenge.transfer_id, admission=True):
            return
        records = await self.repository.requests(challenge.transfer_id)
        record = next((item for item in records if item.id == challenge.request_id), None)
        if record is None or record.state != "input_required":
            await self.challenges.clear(challenge)
            await self.inputs.clear(challenge.id)
            return
        eligible = {item.descriptor.id: item for item in self.registry.eligible_providers(record.request)}
        provider = eligible.get(challenge.integration_id)
        if not isinstance(provider, ProviderInputContinuation):
            return
        submitted = None
        try:
            # Waiting for input releases provider capacity. Reacquire the normal
            # resolution slot before consuming the transient secret bundle so
            # continuation cannot bypass provider concurrency or retain secrets
            # while merely waiting for capacity.
            async with self._resolution_slots:
                if not await self._live(challenge.transfer_id, admission=True):
                    return
                submitted = await self.inputs.take(challenge)
                if submitted is None:
                    return
                result = await provider.resolve_with_input(record.request, submitted)
            attempt = ResolutionAttempt(challenge.operation_id, record.id, challenge.integration_id, "input_required")
            await self._apply_resolution(record, attempt, provider, result, challenge=challenge)
        except Exception as exc:
            secrets = submitted.secret_values() if submitted else ()
            error = exc.error if isinstance(exc, TransferError) else unknown_failure(
                exc, integration_id=challenge.integration_id, domain=Domain.PROVIDER, stage=Stage.RESOLUTION, secrets=secrets)
            attempt = ResolutionAttempt(challenge.operation_id, record.id, challenge.integration_id, "input_required")
            await self.repository.resolution(attempt, ResolutionResult(ResourceState.UNKNOWN, error=error))
            await self.challenges.clear(challenge)
            await self._request_failure(record, error, attempts=record.attempts + 1)
        finally:
            if submitted:
                submitted.discard()

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
                entries = tuple({codec.dump(entry): entry for entry in entries}.values())
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
            elif observation.state == ResourceState.PREPARING:
                await self.repository.poll_after(record.id, self.clock() + self.policy.resource_poll_interval)
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
            if not record.parent_id:
                root_ids = {item.id for item in await self.repository.requests(record.transfer_id) if item.parent_id is None}
                for primary in await self.repository.artifacts(record.transfer_id):
                    if primary.request_id not in root_ids or not primary.candidates:
                        continue
                    size = await shared_size(primary.candidates[0], candidates[0], self.registry)
                    if size is not None and await self.repository.add_alternate(primary, record, candidates, size):
                        return
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
            validate_target(self.root, artifact.target)
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
            prepared = executor.prepare(request)
            if isinstance(prepared, InputRequirement):
                if not isinstance(executor, ExecutorInputContinuation):
                    raise TransferError(self._error(Category.UNSUPPORTED_CAPABILITY, Stage.QUEUE, domain=Domain.REQUEST, retryability=Retryability.NEVER))
                await self.challenges.wait_executor(artifact, executor.descriptor.id, request.attempt_id, prepared)
                return
            if not isinstance(prepared, ExecutionHandle) or prepared.executor_id != executor.descriptor.id or prepared.attempt_id != request.attempt_id:
                raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.QUEUE))
            handle = prepared
            async with self._dispatch_lock:
                if not self.dispatch_permitted or not await self._live(artifact.transfer_id, admission=True):
                    return
                attempts = await self.repository.live_executions()
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


    async def _continue_executor_input(self, challenge: InputChallenge, artifacts):
        if not await self.inputs.has(challenge) or not await self._live(challenge.transfer_id, admission=True):
            return
        artifact = next((item for item in artifacts if item.id == challenge.artifact_id), None)
        if artifact is None or artifact.state != "input_required" or not artifact.candidates:
            await self.challenges.clear(challenge)
            await self.inputs.clear(challenge.id)
            return
        candidate = artifact.candidates[artifact.selected]
        eligible = {item.descriptor.id: item for item in self.registry.eligible_executors(candidate)}
        executor = eligible.get(challenge.integration_id)
        request = ExecutionRequest(candidate, artifact.target, challenge.operation_id)
        submitted = None

        if (artifact.execution is not None
                and artifact.execution.attempt_id == challenge.operation_id
                and isinstance(executor, ExecutorInputRecovery)):
            try:
                async with self._dispatch_lock:
                    if not self.dispatch_permitted or not await self._live(challenge.transfer_id, admission=True):
                        return
                    attempts = await self.repository.live_executions()
                    occupied = sum(item.state in {"prepared", "queued", "transferring", "unknown"} for item in attempts)
                    if occupied >= max(1, self.policy.max_active_executions):
                        return
                    submitted = await self.inputs.take(challenge)
                    if submitted is None:
                        return
                    observed = await executor.start_with_input(request, artifact.execution, submitted)
                    current = next(item for item in await self.repository.artifacts(challenge.transfer_id) if item.id == artifact.id)
                    await self._execution_result(current, executor, observed)
                    await self.challenges.current(challenge.transfer_id)
            except Exception as exc:
                secrets = submitted.secret_values() if submitted else ()
                error = exc.error if isinstance(exc, TransferError) else unknown_failure(
                    exc, integration_id=challenge.integration_id, domain=Domain.EXECUTOR, stage=Stage.QUEUE, secrets=secrets)
                await self.challenges.clear(challenge)
                await self.repository.artifact_state(artifact.id, "error", error=error)
                await self.repository.outcome(challenge.transfer_id, TransferOutcome(OutcomeKind.FAILURE, error))
            finally:
                if submitted:
                    submitted.discard()
            return

        if not isinstance(executor, ExecutorInputContinuation):
            return
        try:
            async with self._dispatch_lock:
                if not self.dispatch_permitted or not await self._live(challenge.transfer_id, admission=True):
                    return
                attempts = await self.repository.live_executions()
                occupied = sum(item.state in {"prepared", "queued", "transferring", "unknown"} for item in attempts)
                if occupied >= max(1, self.policy.max_active_executions):
                    return
                submitted = await self.inputs.take(challenge)
                if submitted is None:
                    return
                prepared = executor.prepare_with_input(request, submitted)
                if isinstance(prepared, InputRequirement):
                    await self.challenges.replace(challenge, prepared)
                    return
                if not isinstance(prepared, ExecutionHandle) or prepared.executor_id != challenge.integration_id or prepared.attempt_id != challenge.operation_id:
                    raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.QUEUE))
                if not await self.repository.prepare_execution(artifact, prepared, from_input_required=True):
                    return
                handle = prepared
            await self.challenges.clear(challenge)
            try:
                observed = await executor.start(request, handle)
            except Exception as exc:
                observed = ExecutionObservation(handle, ExecutionState.UNKNOWN,
                    error=unknown_failure(exc, integration_id=executor.descriptor.id, domain=Domain.EXECUTOR,
                                          stage=Stage.QUEUE, secrets=submitted.secret_values()))
            current = next(item for item in await self.repository.artifacts(challenge.transfer_id) if item.id == artifact.id)
            await self._execution_result(current, executor, observed)
        except Exception as exc:
            secrets = submitted.secret_values() if submitted else ()
            error = exc.error if isinstance(exc, TransferError) else unknown_failure(
                exc, integration_id=challenge.integration_id, domain=Domain.EXECUTOR, stage=Stage.QUEUE, secrets=secrets)
            await self.challenges.clear(challenge)
            await self.repository.artifact_state(artifact.id, "error", error=error)
            await self.repository.outcome(challenge.transfer_id, TransferOutcome(OutcomeKind.FAILURE, error))
        finally:
            if submitted:
                submitted.discard()

    async def _execution_result(self, artifact, executor, observed):
        if not isinstance(observed, ExecutionObservation) or observed.handle != artifact.execution:
            raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.RECONCILIATION))
        idle_seconds = await self.repository.execution_idle_seconds(observed, self.clock())
        await self.repository.execution(observed)
        if (artifact.candidates and isinstance(executor, ExecutorInputRecovery)):
            requirement = executor.input_requirement(artifact.candidates[artifact.selected], observed)
            if requirement is not None:
                if not isinstance(requirement, InputRequirement):
                    raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.RECONCILIATION))
                await self.challenges.wait_executor(artifact, executor.descriptor.id, observed.handle.attempt_id, requirement)
                return
        if not await self._live(artifact.transfer_id):
            await executor.cancel(observed.handle)
            return
        transfer = await self.repository.get(artifact.transfer_id)
        if observed.occupies_slot and (transfer.paused or await self.repository.globally_paused()):
            # Pause may win while start is awaiting the executor's response.
            # The accepted late creation must satisfy the durable pause intent.
            if isinstance(executor, PauseResume):
                parked = await executor.pause(observed.handle)
                await self.repository.execution(parked)
                if parked.error:
                    await self.repository.outcome(artifact.transfer_id, TransferOutcome(OutcomeKind.FAILURE, parked.error))
            return
        if (observed.state == ExecutionState.TRANSFERRING and observed.error is None
                and self.policy.stalled_after_seconds > 0 and idle_seconds >= self.policy.stalled_after_seconds):
            # Confirm release before allowing a successor. A timeout alone does
            # not establish that the old execution stopped writing.
            cancelled = await executor.cancel(observed.handle)
            if cancelled.kind == OutcomeKind.FAILURE:
                await self.repository.outcome(artifact.transfer_id, cancelled, attempt_id=observed.handle.attempt_id)
                return
            confirmed = await executor.observe(observed.handle)
            await self.repository.execution(confirmed)
            if confirmed.error or confirmed.state not in {ExecutionState.ABSENT, ExecutionState.CANCELLED}:
                return
            error = self._error(Category.TRANSFER_STALLED, Stage.EXECUTION, domain=Domain.EXECUTOR,
                retryability=Retryability.BACKOFF, recovery=Recovery.RETRY)
            await self._recover_artifact(artifact, error)
        elif observed.state == ExecutionState.SUCCEEDED:
            validate_target(self.root, artifact.target)
            candidate = artifact.candidates[artifact.selected] if artifact.candidates else None
            size = artifact.expected_bytes or observed.progress.total_bytes
            valid = await stable_payload(artifact.target, size, sidecars=executor.resumable_paths(artifact.target),
                                         integrity=candidate.integrity if candidate else (), delay=self.policy.adoption_stability_seconds,
                                         allow_empty=size == 0)
            if valid:
                await self.repository.artifact_state(artifact.id, "completed", expected_bytes=size)
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
            await self.repository.artifact_state(artifact.id, "queued", retry_at=decision.retry_at, release=True,
                selected=artifact.selected + 1, expected_bytes=artifact.candidates[artifact.selected + 1].expected_bytes)
        elif decision.action == Recovery.RERESOLVE:
            await self.repository.artifact_state(artifact.id, "refresh_pending", retry_at=decision.retry_at, release=True)
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
            retained = artifact.candidates[:artifact.selected] + result.candidates + artifact.candidates[artifact.selected + 1:]
            await self.repository.materialize(record, retained, artifact.target)
            await self.repository.artifact_state(artifact.id, "queued", selected=artifact.selected,
                expected_bytes=result.candidates[0].expected_bytes)
        except Exception as exc:
            error = exc.error if isinstance(exc, TransferError) else unknown_failure(exc,
                integration_id=provider.descriptor.id if provider else "", domain=Domain.PROVIDER, stage=Stage.CANDIDATE_PREPARATION)
            if attempt:
                await self.repository.resolution(attempt, ResolutionResult(ResourceState.UNKNOWN, error=error))
            await self.repository.artifact_state(artifact.id, "error", error=error)
            record = next(item for item in await self.repository.requests(artifact.transfer_id) if item.id == artifact.request_id)
            if record.parent_id and error.category in {Category.RESOURCE_NOT_FOUND, Category.RESOURCE_EXPIRED, Category.SOURCE_EXPIRED, Category.SOURCE_NOT_FOUND}:
                if await self._renew_source_parent(record):
                    return
            decision = self.policy.retry_resolution(error, record.attempts, self.clock())
            if decision.automatic:
                await self.repository.artifact_state(artifact.id, "refresh_pending", error=error, retry_at=decision.retry_at, release=True)

    async def _renew_source_parent(self, record, *, operator=False):
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
            retryability=Retryability.AFTER_RERESOLUTION, recovery=Recovery.RERESOLVE)
        decision = self.policy.retry_resolution(error, 0 if operator else parent.attempts, self.clock())
        if not decision.automatic:
            return False
        if observation.state != ResourceState.ABSENT and parent.resource.ownership in {Ownership.CREATED, Ownership.ADOPTED}:
            await self.repository.cleanup_intent(parent.resource.id, CleanupAuthority.OWNED)
            await self._cleanup_pending()
            if any(resource.id == parent.resource.id and pending for resource, _state, pending in await self.repository.resources(record.transfer_id)):
                return False
        await self.repository.renew_parent(parent, self.clock() if operator else decision.retry_at, reset_budget=operator)
        return True

    async def _aggregate(self, transfer_id: int):
        if not await self._live(transfer_id):
            return
        transfer = await self.repository.get(transfer_id)
        challenge = await self.challenges.current(transfer_id)
        if challenge:
            if transfer.state != TransferState.INPUT_REQUIRED:
                await self.repository.state(transfer_id, TransferState.INPUT_REQUIRED)
            return
        requests = await self.repository.requests(transfer_id)
        artifacts = await self.repository.artifacts(transfer_id)
        pending = any(item.state in {"pending", "waiting", "waiting_parent", "resolving", "materializing"} for item in requests)
        attempts = {item.handle.attempt_id: item for item in await self.repository.executions(transfer_id)}
        total = sum(item.expected_bytes for item in artifacts)
        await self.repository.aggregate_metadata(transfer_id, total_bytes=total,
            local_path=str(Path(artifacts[0].target).parent) if artifacts else "")
        completed = sum(item.expected_bytes if item.state == "completed" else min(item.expected_bytes,
            attempts[item.execution.attempt_id].progress.completed_bytes) if item.execution else 0 for item in artifacts)
        progress = min(100.0, completed / total * 100) if total else 0.0
        if transfer.paused or await self.repository.globally_paused():
            return
        if artifacts and all(item.state == "completed" for item in artifacts) and not pending:
            await self._complete(transfer_id, artifacts)
        elif any(item.state in {"downloading", "unknown", "verifying"} for item in artifacts):
            await self.repository.state(transfer_id, TransferState.TRANSFERRING, progress=progress)
        elif any(item.state in {"queued", "paused", "refresh_pending"} for item in artifacts):
            await self.repository.state(transfer_id, TransferState.QUEUED, progress=progress)
        elif pending:
            await self.repository.state(transfer_id, TransferState.RESOLVING, progress=progress)
        elif any(item.state == "error" for item in artifacts) or any(item.state == "failed" for item in requests):
            error = next((item.error for item in (*artifacts, *requests) if item.error), None)
            await self.repository.state(transfer_id, TransferState.FAILED, progress=progress, error=error)
        elif artifacts and all(item.state == "cancelled" for item in artifacts):
            await self.repository.state(transfer_id, TransferState.CANCELLED, progress=progress)
        elif not artifacts and await self.repository.blocked_artifact_count(transfer_id):
            if await self.repository.state(transfer_id, TransferState.COMPLETED, progress=0, verified=True):
                await self.repository.outcome(transfer_id, TransferOutcome(OutcomeKind.SKIPPED, detail="No selected artifacts"))

    async def _complete(self, transfer_id: int, artifacts):
        if (await self.repository.get(transfer_id)).state == TransferState.POST_PROCESSING:
            return
        # Earlier members can disappear while later members are being verified.
        # Recheck possession at the completion boundary before publishing success
        # or letting a processor consume the paths.
        for artifact in artifacts:
            try:
                validate_target(self.root, artifact.target)
            except TransferError as exc:
                await self.repository.artifact_state(artifact.id, "error", error=exc.error)
                await self.repository.state(transfer_id, TransferState.FAILED, error=exc.error)
                return
            executor = None
            try:
                candidate = artifact.candidates[artifact.selected] if artifact.candidates else None
                executor = self.registry.executors.get(artifact.execution.executor_id) if artifact.execution else self.registry.executor_for(candidate) if candidate else None
                if executor is None:
                    raise TransferError(self._error(Category.UNSUPPORTED_CAPABILITY, Stage.VERIFICATION,
                        domain=Domain.REQUEST, retryability=Retryability.NEVER))
                sidecars = executor.resumable_paths(artifact.target)
                if await asyncio.to_thread(payload_matches, artifact.target, artifact.expected_bytes,
                                           sidecars, allow_empty=artifact.execution is not None):
                    continue
                if artifact.execution:
                    result = await executor.cancel(artifact.execution)
                    if not isinstance(result, TransferOutcome) or result.kind not in {OutcomeKind.SUCCESS, OutcomeKind.CANCELLED}:
                        error = result.error if isinstance(result, TransferOutcome) and result.error else self._error(
                            Category.INVALID_ADAPTER_RESPONSE, Stage.RECONCILIATION)
                        raise TransferError(error)
                await self.repository.artifact_state(artifact.id, "queued", release=True)
                await self.repository.state(transfer_id, TransferState.QUEUED)
                return
            except Exception as exc:
                error = exc.error if isinstance(exc, TransferError) else unknown_failure(exc,
                    integration_id=executor.descriptor.id if executor else "", domain=Domain.EXECUTOR, stage=Stage.VERIFICATION)
                await self.repository.artifact_state(artifact.id, "error", error=error)
                await self.repository.state(transfer_id, TransferState.FAILED, error=error)
                await self.repository.outcome(transfer_id, TransferOutcome(OutcomeKind.FAILURE, error))
                return
        if self.postprocessors:
            await self.repository.state(transfer_id, TransferState.POST_PROCESSING, progress=100, verified=True)
            await self.repository.queue_postprocessing(transfer_id, self.postprocessors, tuple(item.target for item in artifacts))
            return
        await self._delivered(transfer_id)

    async def _delivered(self, transfer_id):
        if await self.repository.state(transfer_id, TransferState.COMPLETED, progress=100, verified=True):
            await self.repository.outcome(transfer_id, TransferOutcome(OutcomeKind.SUCCESS))
            if self.policy.cleanup_after_completion:
                await self._cleanup_resources(transfer_id)

    async def recover_postprocessing(self):
        # Extraction may have published output or removed its input before a
        # crash. Do not repeat a non-idempotent processor without evidence.
        for job in await self.repository.interrupted_postprocessing():
            error = self._error(Category.RECOVERY_FAILED, Stage.POST_PROCESSING, domain=Domain.POST_PROCESSING)
            outcome = TransferOutcome(OutcomeKind.FAILURE, error)
            await self.repository.outcome(job["transfer_id"], outcome)
            if await self.repository.finish_postprocessing(job["transfer_id"], job["processor_id"], outcome):
                await self._delivered(job["transfer_id"])

    async def process_postprocessors(self):
        async with self._postprocess_lock:
            processors = {item.descriptor.id: item for item in self.postprocessors}
            for job in await self.repository.postprocessing_jobs():
                transfer_id, processor_id = job["transfer_id"], job["processor_id"]
                if not await self._live(transfer_id, admission=True):
                    continue
                if not await self.repository.claim_postprocessing(transfer_id, processor_id):
                    continue
                processor = processors.get(processor_id)
                try:
                    if processor is None:
                        raise TransferError(self._error(Category.UNSUPPORTED_CAPABILITY, Stage.POST_PROCESSING, domain=Domain.POST_PROCESSING))
                    outcome = await processor.process(transfer_id, tuple(codec.load(job["paths"])))
                    if not isinstance(outcome, TransferOutcome):
                        raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.POST_PROCESSING, domain=Domain.POST_PROCESSING))
                except Exception as exc:
                    error = exc.error if isinstance(exc, TransferError) else unknown_failure(exc,
                        integration_id=processor_id, domain=Domain.POST_PROCESSING, stage=Stage.POST_PROCESSING)
                    outcome = TransferOutcome(OutcomeKind.FAILURE, error)
                await self.repository.outcome(transfer_id, outcome)
                if await self.repository.finish_postprocessing(transfer_id, processor_id, outcome):
                    await self._delivered(transfer_id)

    async def pause(self, transfer_id: int):
        async with self._control_lock:
            await self.repository.pause_intent(transfer_id, True)
            return await self._control(transfer_id, resume=False)

    async def select_artifact(self, transfer_id: int, artifact_id: int, *, selected: bool):
        transfer = await self.repository.get(transfer_id)
        if not transfer or transfer.state == TransferState.DELETED:
            raise KeyError(transfer_id)
        await self.repository.select_artifact(transfer_id, artifact_id, selected)
        if selected and transfer.state == TransferState.COMPLETED:
            await self.repository.state(transfer_id, TransferState.QUEUED, operator=True, expected_epoch=transfer.epoch)

    async def cancel_artifact(self, transfer_id: int, artifact_id: int):
        artifact = next((item for item in await self.repository.artifacts(transfer_id) if item.id == artifact_id), None)
        if artifact is None:
            raise KeyError(artifact_id)
        if artifact.execution:
            executor = self.registry.executors[artifact.execution.executor_id]
            outcome = await executor.cancel(artifact.execution)
            await self.repository.outcome(transfer_id, outcome, attempt_id=artifact.execution.attempt_id)
            if outcome.error:
                raise TransferError(outcome.error)
        await self.repository.artifact_state(artifact_id, "cancelled")
        await self._aggregate(transfer_id)

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
                if resume:
                    observed = await executor.observe(artifact.execution)
                    if observed.state == ExecutionState.PAUSED:
                        observed = await self._resume_execution(artifact, executor)
                else:
                    observed = await executor.pause(artifact.execution)
                await self.repository.execution(observed)
                if observed.error:
                    results.append(observed.error)
                elif resume and observed.state in {ExecutionState.ABSENT, ExecutionState.CANCELLED}:
                    await self.repository.artifact_state(artifact.id, "queued", release=True)
            except Exception as exc:
                results.append(unknown_failure(exc, integration_id=executor.descriptor.id, domain=Domain.EXECUTOR, stage=Stage.EXECUTION))
        if not results and not await self.challenges.current(transfer_id):
            await self.repository.state(transfer_id, TransferState.QUEUED if resume else TransferState.PAUSED)
        return tuple(results)

    async def _resume_execution(self, artifact, executor):
        async with self._dispatch_lock:
            if not self.dispatch_permitted or not await self._live(artifact.transfer_id, admission=True):
                return ExecutionObservation(artifact.execution, ExecutionState.PAUSED)
            live = await self.repository.live_executions()
            occupied = sum(item.state in {"prepared", "queued", "transferring", "unknown"} for item in live)
            if occupied >= max(1, self.policy.max_active_executions):
                return ExecutionObservation(artifact.execution, ExecutionState.PAUSED)
            # Reserve capacity durably before an unpause that can lose its ack.
            await self.repository.execution(ExecutionObservation(artifact.execution, ExecutionState.QUEUED))
        try:
            return await executor.resume(artifact.execution)
        except Exception as exc:
            return ExecutionObservation(artifact.execution, ExecutionState.UNKNOWN,
                error=unknown_failure(exc, integration_id=executor.descriptor.id, domain=Domain.EXECUTOR, stage=Stage.EXECUTION))

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
            if await self.challenges.current(transfer_id):
                return False
            if transfer.state == TransferState.DELETED and not reacquire:
                return False
            if any(pending for _resource, _state, pending in await self.repository.resources(transfer_id)):
                return False
            if not await self.repository.reset_postprocessing(transfer_id):
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
                await self.repository.reset_retry_budget(artifact.id)
                await self.repository.artifact_state(artifact.id, "unresolved", release=True)
                record = next(item for item in await self.repository.requests(transfer_id) if item.id == artifact.request_id)
                if not record.parent_id or not await self._renew_source_parent(record, operator=True):
                    await self.repository.artifact_state(artifact.id, "queued", release=True)
            await self.repository.retry_requests(transfer_id, reset_budget=True)
            await self.repository.pause_intent(transfer_id, False)
            return True


    async def submit_input(self, transfer_id: int, challenge_id: str, method: str, values):
        transfer = await self.repository.get(transfer_id)
        if transfer is None:
            raise KeyError(transfer_id)
        challenge = await self.challenges.current(transfer_id)
        if transfer.state != TransferState.INPUT_REQUIRED or challenge is None or challenge.id != challenge_id:
            raise InputSubmissionRejected("Input challenge is stale")
        await self.inputs.submit(challenge, method, values)
        return challenge

    async def cancel(self, transfer_id: int):
        transfer = await self.repository.get(transfer_id)
        if transfer is None:
            raise KeyError(transfer_id)
        challenge = await self.challenges.current(transfer_id)
        if challenge:
            await self.inputs.clear(challenge.id)
            await self.challenges.clear_transfer(transfer_id)
        for artifact in await self.repository.artifacts(transfer_id):
            if artifact.execution:
                executor = self.registry.executors.get(artifact.execution.executor_id)
                if executor:
                    outcome = await executor.cancel(artifact.execution)
                    await self.repository.outcome(transfer_id, outcome, attempt_id=artifact.execution.attempt_id)
            if artifact.state != "completed":
                await self.repository.artifact_state(artifact.id, "cancelled")
        await self.repository.state(transfer_id, TransferState.CANCELLED)
        return True

    async def delete(self, transfer_id: int, *, remote=True):
        challenge = await self.challenges.current(transfer_id)
        if challenge:
            await self.inputs.clear(challenge.id)
        await self.challenges.clear_transfer(transfer_id)
        # Persist the tombstone before waiting on any integration. Every async
        # completion path rechecks it, including late provider resource creation.
        await self.repository.delete(transfer_id, remote=remote)
        for attempt in await self.repository.executions(transfer_id):
            executor = self.registry.executors.get(attempt.handle.executor_id)
            if executor:
                outcome = await executor.cancel(attempt.handle)
                await self.repository.outcome(transfer_id, outcome, attempt_id=attempt.handle.attempt_id)
                if outcome.kind == OutcomeKind.CANCELLED:
                    await self.repository.execution(ExecutionObservation(attempt.handle, ExecutionState.CANCELLED, attempt.progress))
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

    async def cleanup_pending(self):
        """Retry durable cleanup intents; this never invents cleanup authority."""
        await self._cleanup_pending()

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
