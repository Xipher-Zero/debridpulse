"""Neutral recovery/materialization primitives shared by every engine
composition (DP 1.0.12 canonical lifecycle/recovery/completion rework,
CANON-001 closure).

This layer owns no recovery or control *decision* authority: pause, resume,
bulk pause/resume, retry (operator retry and terminal-transfer reacquisition
alike), refresh, candidate-refresh scheduling, and recovery/candidate
activation each have exactly one implementation in the production
hierarchy -- ``transfers.convergence_engine.TransferEngine`` -- and no lower
class, including this one and ``_engine_base.TransferEngine`` below it,
defines any of those names at all. There is nothing to delegate to, refuse,
or fall back on: a composition that never layers
``convergence_engine.TransferEngine`` on top (e.g. a test harness) simply
lacks those operations, by design, not by an inherited stub.

What remains here are facts and mechanics every composition needs regardless
of which recovery-decision owner sits above it, none of which independently
decide or persist a recovery/lifecycle transition: collection affinity,
cohort-locked materialization, recovery-context assembly,
and next-alternate-candidate traversal. Completion is deliberately absent:
what a SUCCEEDED observation proves about the payload -- including which
compatible size is materially real -- has exactly one owner
(``_engine_base._execution_result`` over ``transfers.filesystem`` and
``transfers.size_evidence``), never a layer above it.
"""
from __future__ import annotations

import asyncio

from transfers._engine_base import TransferEngine as _QualifiedTransferEngine
from transfers.applicability import ApplicabilityUnresolved
from transfers.cohorts import coordinate_collection
from transfers.models import Artifact
from transfers.policy import RecoveryContext


class TransferEngine(_QualifiedTransferEngine):
    """Qualified lifecycle plus progress-aware universal recovery behavior."""

    def _collection_affinity_lock(self, transfer_id: int) -> asyncio.Lock:
        return self._collection_affinity_locks.setdefault(transfer_id, asyncio.Lock())

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

    def _candidate_provider_enabled(self, candidate) -> bool:
        if candidate is None or not candidate.provider_id:
            return True
        provider = self.registry.providers.get(candidate.provider_id)
        return bool(provider and provider.descriptor.enabled)

    async def _materialize(self, record, candidates):
        lock = self._cohort_locks.setdefault(record.transfer_id, asyncio.Lock())
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
        """First eligible, not-yet-attempted candidate in index order (DP 1.0.12
        recovery leveling, Section 12).

        Traversal is no longer defined by ``selected + 1`` -- the currently
        selected index says nothing about which candidates were already tried
        (an operator may have jumped directly to a high index). The durable
        ``candidate_attempt_history`` (transfers.repository.TransferRepository
        .record_candidate_attempt) is the actual attempt record; a lower-index
        candidate that was never activated remains eligible regardless of how
        far the selection has moved past it.
        """
        attempted = frozenset(
            str(item) for item in
            (await self.repository.recovery_context(artifact.id)).get("candidate_attempt_history") or ()
        )
        for index, candidate in enumerate(artifact.candidates):
            if index == artifact.selected or str(candidate.id) in attempted:
                continue
            if not self._candidate_provider_enabled(candidate):
                continue
            if not self.registry.eligible_executors(candidate):
                continue
            if await self.canonical.origin_for(artifact, candidate) is None:
                continue
            return index
        return None

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
