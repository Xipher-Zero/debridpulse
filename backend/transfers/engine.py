"""WS2 P1 transfer engine extension over the qualified WS1 P2 lifecycle base.

The base module is the exact qualified WS1 P2 engine. This public engine keeps
that lifecycle intact and overrides only the recovery seams needed for bounded
alternate-candidate failover. Execution-discovered size ownership itself lives
in the universal repository observation path, not in any provider or executor.
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
from transfers.models import (
    Artifact, ExecutionObservation, ExecutionState, OutcomeKind, ResolutionResult,
    ResourceState, TransferOutcome,
)


stable_payload = _engine_base.stable_payload


async def _stable_payload_proxy(*args, **kwargs):
    return await stable_payload(*args, **kwargs)


_engine_base.stable_payload = _stable_payload_proxy


class TransferEngine(_QualifiedTransferEngine):
    """Qualified lifecycle plus WS2 P1 alternate-recovery behavior."""

    def _collection_affinity_lock(self, transfer_id: int) -> asyncio.Lock:
        locks = getattr(self, "_collection_affinity_locks", None)
        if locks is None:
            locks = self._collection_affinity_locks = {}
        return locks.setdefault(transfer_id, asyncio.Lock())

    async def _ensure_collection_affinity(self, transfer_id: int) -> bool:
        """Establish collection ownership before any direct-link sibling resolves.

        Non-direct-link transfers return before taking the collection lock so the
        correction cannot serialize qualified torrent/magnet/provider cohorts.
        The transfer is re-read inside the lock before classification so a stale
        precheck cannot create affinity after lifecycle state changes.
        """
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
                provider = self.registry.collection_provider_for(
                    tuple(record.request for record in roots)
                )
            except ApplicabilityUnresolved:
                return True
            if provider is not None:
                await self.repository.bind_collection_route(
                    transfer_id, provider.descriptor.id,
                )
            return False

    async def _prepare_collection_affinity(self) -> set[int]:
        """Proactively bind or hold active direct-link collections for this cycle."""
        blocked: set[int] = set()
        for transfer in await self.repository.active():
            if transfer.source == "direct_link" and await self._ensure_collection_affinity(transfer.id):
                blocked.add(transfer.id)
        return blocked

    async def resolve_pending(self):
        """Preflight collection affinity, run resolution, then aggregate direct links."""
        lock = getattr(self, "_collection_resolution_lock", None)
        if lock is None:
            lock = self._collection_resolution_lock = asyncio.Lock()
        async with lock:
            blocked = await self._prepare_collection_affinity()
            self._collection_affinity_blocked = blocked
            try:
                result = await super().resolve_pending()
                # Request failure is provider-cadence state; aggregate it here for
                # direct-link collections so an all-source terminal failure does
                # not remain externally PENDING until an execution pass happens.
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
        """Converge global controls in one deterministic capacity order.

        Per-transfer controls may touch different executors, but resume admission
        consumes one application-wide execution budget. Running those controls
        concurrently lets native pause/resume calls interleave around the shared
        capacity reservation. Serial convergence preserves the same control
        semantics while ensuring each successor observes the durable disposition
        of its predecessor before deciding whether a slot is available.
        """
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

    async def _materialize(self, record, candidates):
        """Coordinate cohort proof and first canonical creation atomically.

        Weak-prefix collection candidates are held only at the existing durable
        MATERIALIZING seam. Related requests share a small per-transfer lock so
        sibling proof and initial canonical allocation cannot race each other,
        while unrelated transfers and canonical/path ownership locks remain
        independent.
        """
        locks = getattr(self, "_cohort_locks", None)
        if locks is None:
            locks = self._cohort_locks = {}
        lock = locks.setdefault(record.transfer_id, asyncio.Lock())
        async with lock:
            if await coordinate_collection(self, record, candidates):
                return

            await super()._materialize(record, candidates)
            artifact = next(
                (item for item in await self.repository.artifacts(record.transfer_id)
                 if item.request_id == record.id),
                None,
            )
            if artifact is None or len(artifact.candidates) < 2:
                return
            for candidate in artifact.candidates:
                await self.canonical.origin_for(artifact, candidate)

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
        """Retire proven-terminal remote-source partial state after revoking its writer."""
        sidecars = self._candidate_sidecars(artifact)
        if not await self.repository.transition_recovery(
            artifact.id, "error", error=error, retry_at=0,
        ):
            return False
        if error.origin == Origin.REMOTE_SOURCE:
            retire_partial(self.root, artifact.target, sidecars)
        return True

    async def _activate_alternate(
        self,
        artifact: Artifact,
        index: int,
        *,
        retry_at: float,
        error: NormalizedError,
    ) -> bool:
        """Revoke the old writer, retire its partial state, then expose candidate B.

        The replacement candidate is not selected or schedulable until the old
        terminal attempt has lost mutation authority and candidate-A partial
        bytes/sidecars have been contained. A crash in the narrow containment
        window leaves a safe artifact error, never two writable attempts.
        """
        if index <= artifact.selected or index >= len(artifact.candidates):
            return False
        replacement = artifact.candidates[index]
        if (artifact.expected_bytes > 0 and replacement.expected_bytes > 0
                and artifact.expected_bytes != replacement.expected_bytes):
            return False
        sidecars = self._candidate_sidecars(artifact)

        if not await self.repository.transition_recovery(
            artifact.id, "error", error=error, retry_at=0,
        ):
            return False

        retire_partial(self.root, artifact.target, sidecars)

        current = await self._current_artifact(artifact.transfer_id, artifact.id)
        if current is None:
            return False
        accepted_size = current.expected_bytes if current.expected_bytes > 0 else replacement.expected_bytes
        return await self.repository.transition_recovery(
            artifact.id,
            "queued",
            retry_at=retry_at,
            selected=index,
            expected_bytes=max(0, accepted_size),
            reset_budget=True,
        )

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
            if await self._activate_alternate(
                artifact, index, retry_at=decision.retry_at, error=error,
            ):
                return True
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
        """Preserve final-verification authority over provisional runtime size.

        Live runtime totals are accepted universally by ``repository.execution``.
        If a provider declared size unknown and a later SUCCEEDED observation
        contradicts that provisional total, the final value may replace it only
        after the actual materialized payload independently verifies against the
        final total. The repository records that refinement rather than silently
        rewriting the earlier evidence.
        """
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
                    artifact.target,
                    final_total,
                    sidecars=executor.resumable_paths(artifact.target),
                    integrity=candidate.integrity,
                    delay=self.policy.adoption_stability_seconds,
                    allow_empty=final_total == 0,
                )
                if valid:
                    await self.repository.execution(observed)
                    if await self.repository.refine_execution_total(
                        artifact.id, observed.handle, final_total,
                    ):
                        await self.repository.artifact_state(
                            artifact.id, "completed", expected_bytes=final_total,
                        )
                        return
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
                await self._activate_alternate(
                    artifact, next_index, retry_at=decision.retry_at, error=error,
                )
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
            replacement_size = result.candidates[0].expected_bytes
            if (artifact.expected_bytes > 0 and replacement_size > 0
                    and artifact.expected_bytes != replacement_size):
                raise TransferError(self._error(
                    Category.SIZE_MISMATCH,
                    Stage.CANDIDATE_PREPARATION,
                    domain=Domain.INTEGRITY,
                    retryability=Retryability.AFTER_RESOURCE_CHANGE,
                    recovery=Recovery.TRY_ALTERNATE_CANDIDATE,
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
            size = artifact.expected_bytes if artifact.expected_bytes > 0 else replacement_size
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
            if current is None:
                return
            next_index = await self._next_alternate_index(current)
            parent_renewal = bool(record and record.parent_id and error.category in {
                Category.RESOURCE_NOT_FOUND,
                Category.RESOURCE_EXPIRED,
                Category.SOURCE_EXPIRED,
                Category.SOURCE_NOT_FOUND,
            })
            if parent_renewal and next_index is None:
                await self._terminal_recovery(current, error)
                await self._renew_source_parent(record)
                return
            await self._try_exhausted_alternate(current, error)
