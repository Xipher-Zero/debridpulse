"""Canonical transfer engine with provider-transition hard-stop enforcement.

Semantic recovery/control decisions belong solely to
``transfers.convergence_engine.TransferEngine`` (DP 1.0.12 canonical
lifecycle/recovery/completion rework, CANON-001 closure). This class and its
bases (``_engine_recovery.TransferEngine``, ``_engine_base.TransferEngine``)
contain only neutral provider-continuation, materialization, and factual
mechanics -- no lower class here decides or applies a recovery outcome. This
public owner closes the admitted-resource continuation seam so every provider
I/O path honors the registry's bound-route enablement/health contract, and
attaches transitional recovery compatibility to factual provider/executor
failures (via ``policy.compatibility``) before lifecycle policy consumes them.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import logging

from transfers import _engine_base, file_selection as fs
from transfers._repository_base import manifest_child_identity, manifest_member_requests
from transfers.input_required import split_user_supplied
from transfers._engine_recovery import TransferEngine as _RecoveryTransferEngine
from transfers.applicability import ApplicabilityUnresolved
from transfers.contracts import (
    ActiveCapacitySource, CachedResolution, Inventory, Manifest, ResourceLookup, speculative_preparation,
)
from transfers.errors import (
    Category, Domain, Origin, Recovery, Retryability, Stage, TransferError, unknown_failure,
)
from transfers.policy import recovery_action
from transfers.registry import ProviderRoute
from transfers.models import (
    ActiveCapacity, AvailabilityState, CachePresence, Capability, CleanupAuthority, NormalizedError, Ownership,
    ResolutionResult,
    ResourceState,
)

logger = logging.getLogger(__name__)

# The bound on one provider's read-only availability answer while a root is
# routed; a provider that does not answer in time is UNKNOWN for that decision.
AVAILABILITY_TIMEOUT_SECONDS = 5.0
# One observation round asks each competing provider, in one batched read,
# about at most this many unbound BitTorrent roots; the answer it gave for a
# root routed later is that root's once, and only while it is this fresh.
AVAILABILITY_ROUND_LIMIT = 100
# The bound on one provider's read-only active-capacity answer.
ACTIVE_CAPACITY_TIMEOUT_SECONDS = 5.0
AVAILABILITY_ROUND_FRESH_SECONDS = 30.0
# A root whose primary route has begun -- the only root a speculative backup
# preparation is ever made for.
_STANDBY_ROOT_STATES = frozenset({"waiting", "materializing", "resolved"})
# Refusals that say only "not now": a backup that met them is deferred, not
# failed. Neither is ever an account, route, health or transfer fact.
_STANDBY_DEFERRABLE = frozenset({Category.RATE_LIMITED, Category.CONCURRENCY_LIMITED, Category.QUOTA_EXCEEDED})
# A provider resource that no longer exists: nothing left to observe.
_STANDBY_ENDED_RESOURCE = frozenset({ResourceState.ABSENT, ResourceState.EXPIRED})


class TransferEngine(_RecoveryTransferEngine):
    """Recovery-qualified engine plus authoritative bound-provider continuation."""

    async def resolve_pending(self):
        """The resolution pass, then -- only once its primary work has drained
        -- the speculative backup preparations it leaves room for."""
        result = await super().resolve_pending()
        await self._prepare_standbys()
        return result

    async def _request_failure(self, record, error, *, attempts=None, waiting=False):
        """Attach legacy recovery fields only after factual integration output."""
        return await super()._request_failure(
            record, self.policy.compatibility(error), attempts=attempts, waiting=waiting,
        )

    def _bound_resource_provider(self, record):
        """Resolve an admitted resource owner through the canonical registry gate.

        Administrative disablement deliberately parks the existing logical work:
        the durable provider/resource binding remains intact and no provider I/O,
        retry consumption, terminal failure, or route competition occurs until
        the same provider is re-enabled.
        """
        if record.resource is None:
            return None
        provider_id = record.resource.provider_id
        try:
            return self.registry.provider_for_bound_route(provider_id, record.resolvable)
        except TransferError as exc:
            configured = self.registry.providers.get(provider_id)
            if (
                exc.error.category == Category.PROVIDER_UNAVAILABLE
                and configured is not None
                and not configured.descriptor.enabled
            ):
                return None
            raise

    @staticmethod
    def _split_member_credentials(entries):
        sanitized, supplied = [], []
        for entry in entries:
            requests = []
            for alternate, request in manifest_member_requests(entry):
                payload, values = split_user_supplied(request.payload)
                if values:
                    request = replace(request, payload=payload)
                    supplied.append((entry.relative_path, alternate, payload, values))
                requests.append(request)
            sanitized.append(replace(entry, request=requests[0], alternates=tuple(requests[1:])))
        return tuple(sanitized), supplied

    async def _resolve(self, record):
        attempt = None
        provider = None
        try:
            if record.parent_id is not None and not await self.repository.member_generation_current(record):
                # A member of a superseded, held or not yet committed
                # decomposition generation is not resolved: its root's next
                # fan-out advances it (or, held, nothing does) -- rebuilding it
                # now would only produce work no generation authorizes.
                await self.repository.poll_after(record.id, self.clock() + self.policy.resource_poll_interval)
                return
            if record.parent_id is None and record.resource is None:
                # Provider cleanup fence: a retired predecessor generation sharing
                # this transfer's source fingerprint still has outstanding,
                # not-yet-abandoned provider cleanup that could act on the shared
                # native resource. Admit stays fresh, but hold the first
                # provider-resource creation (provider.resolve) as ordinary
                # waiting/retry state — no attempt consumed, no error — until no
                # predecessor cleanup operation can execute.
                if await self.repository.predecessor_cleanup_barrier(record.transfer_id):
                    await self.repository.poll_after(
                        record.id, self.clock() + self.policy.resource_poll_interval,
                    )
                    return
            if record.resource and record.parent_id is None:
                previous_provider = self._bound_resource_provider(record)
                if previous_provider is None:
                    return
                provider = previous_provider
                if not isinstance(previous_provider, ResourceLookup):
                    raise TransferError(self._error(
                        Category.UNSUPPORTED_CAPABILITY, Stage.RECONCILIATION,
                    ))
                previous = await previous_provider.observe(record.resource)
                await self.repository.resource_observation(
                    record.transfer_id, previous.resource, previous.state,
                )
                await self._converge_root_observation_name(record, previous)
                previous_error = previous.error or None
                restartable = previous.state in {ResourceState.EXPIRED, ResourceState.ABSENT} or (
                    previous.state == ResourceState.UNAVAILABLE
                    and previous_error is not None
                    and previous_error.retryability not in {Retryability.NEVER, Retryability.UNKNOWN}
                    and previous_error.domain != Domain.SECURITY
                    and recovery_action(previous_error) in {Recovery.RETRY, Recovery.RERESOLVE, Recovery.BACKOFF}
                )
                if previous_error and not restartable:
                    raise TransferError(self.policy.compatibility(previous_error))
                if previous.state in {ResourceState.PREPARING, ResourceState.AVAILABLE}:
                    await self.repository.poll_after(record.id, self.clock(), waiting=True)
                    return
                if not restartable:
                    raise TransferError(self._error(
                        Category.UNMAPPED_PROVIDER_ERROR,
                        Stage.RECONCILIATION,
                        domain=Domain.PROVIDER,
                    ))
                if (
                    previous.state != ResourceState.ABSENT
                    and record.resource.ownership in {Ownership.CREATED, Ownership.ADOPTED}
                ):
                    await self.repository.cleanup_intent(
                        record.transfer_id, record.resource.id, CleanupAuthority.OWNED,
                    )
                    await self._cleanup_pending()
                    if any(
                        resource.id == record.resource.id and pending
                        for resource, _state, pending
                        in await self.repository.resources(record.transfer_id)
                    ):
                        raise TransferError(self._error(
                            Category.REMOTE_CLEANUP_FAILED,
                            Stage.CLEANUP,
                            domain=Domain.CLEANUP,
                        ))

            route = await self._route(record)
            await self._record_route_hold(record, route)
            provider = route.require()
            cached = False
            if record.alternative_group is not None and record.parent_id is None and not record.attempts:
                # The selected alternative of an explicit group, never yet
                # resolved: an alternative the provider already holds is
                # preferred over acquiring this one, without creating anything.
                chosen = await self._cached_alternative(record, provider)
                if chosen is not None and chosen.id != record.id:
                    if await self.repository.select_alternative(record.id, chosen.id):
                        self._resolution_opportunity(record.transfer_id)
                        return
                cached = chosen is not None and chosen.id == record.id
            async with self._resolution_slot():
                if not await self._live(record.transfer_id, admission=True):
                    return
                # An operator's explicit route replacement opened this route's
                # attempt already: resolution adopts it rather than opening another.
                attempt = (await self.repository.begin_pinned_resolution(record.id, provider.descriptor.id)
                           or await self.repository.begin_resolution(
                               record.id, provider.descriptor.id, routing_decision=self._route_evidence(record, route),
                           ))
                if attempt is None:
                    return
                # The route is chosen; a backup that provider already prepared
                # for this root is taken over instead of resolving again.
                result = None if cached else await self._promoted_standby(record, provider)
                if result is None:
                    if not cached and not await self._make_primary_room(record, provider):
                        # Full, and not (yet) freed by confirmed cleanup: no
                        # productive call above the provider's effective
                        # maximum. The constrained resource is the provider's
                        # capacity, but DebridPulse made this decision -- the
                        # provider was never asked -- so its origin is core;
                        # routing and backoff read it exactly as a refusal.
                        raise TransferError(NormalizedError(
                            Domain.PROVIDER, Category.CONCURRENCY_LIMITED, Stage.RESOLUTION, Retryability.BACKOFF,
                            origin=Origin.CORE, integration_id=provider.descriptor.id))
                    result = await self._primary_resolution(record, provider, cached)
            if cached and result is None:
                # Held when asked, no longer held now: nothing was created, and
                # the group continues from its first dormant alternative.
                if await self.repository.defer_alternative(attempt, self._error(
                        Category.RESOLUTION_TEMPORARILY_FAILED, Stage.RESOLUTION,
                        domain=Domain.RESOLUTION, retryability=Retryability.BACKOFF)):
                    self._resolution_opportunity(record.transfer_id)
                return
            await self._apply_resolution(record, attempt, provider, result)
        except ApplicabilityUnresolved:
            return
        except Exception as exc:
            error = exc.error if isinstance(exc, TransferError) else unknown_failure(
                exc,
                integration_id=provider.descriptor.id if provider else "",
                domain=Domain.PROVIDER,
                stage=Stage.RESOLUTION,
                secrets=(str(record.request.payload),),
            )
            error = self.policy.compatibility(error)
            if attempt:
                await self.repository.resolution(
                    attempt, ResolutionResult(ResourceState.UNKNOWN, error=error),
                )
            await self._request_failure(
                record, error, attempts=record.attempts + (1 if attempt else 0),
            )

    async def _route_provider(self, record):
        """The provider this request's resolution belongs to: its bound route,
        else the first of the one canonical competition."""
        return (await self._route(record)).require()

    async def _route(self, record) -> ProviderRoute:
        """``_route_provider``'s one decision with the routing decision that
        made it (none for a bound route: it is never decided again)."""
        bound_provider_id = await self.repository.bound_route_provider(record.id)
        if bound_provider_id:
            return ProviderRoute(self.registry.provider_for_bound_route(bound_provider_id, record.resolvable))
        competition = await self._competition(record)
        if record.parent_id is not None:
            # A member continues the route that decomposed it: its root's
            # current provider takes it whenever that provider is one of its
            # own legitimate claimants -- so a root whose route was replaced
            # fans its members out to the provider it is on now.
            root_provider_id = await self.repository.bound_route_provider(record.parent_id)
            following = next((provider for provider in self.registry.eligible_providers(
                record.resolvable, declined=competition["declined"], exhausted=competition["exhausted"],
                acquisition=False, generic_closed=competition["generic_closed"])
                if provider.descriptor.id == root_provider_id), None) if root_provider_id else None
            if following is not None:
                return ProviderRoute(following)
        # Only a root's new acquisition is ever asked about, and only where an
        # answer can change the order -- never merely to have it recorded.
        availability = (await self._root_availability(record, competition)
                        if record.parent_id is None and self.registry.availability_orders(record.resolvable)
                        else None)
        return self.registry.provider_route(record.resolvable, availability=availability, **competition)

    async def _competition(self, record) -> dict:
        """The facts of ``record``'s one canonical provider competition."""
        return {
            "declined": await self.repository.declined_route_providers(record.id),
            "exhausted": await self.repository.exhausted_route_providers(record.id),
            # A member continues the route that decomposed it: it is
            # never new acquisition, so account entitlement never gates it.
            "acquisition": record.parent_id is None,
            # A collection specialized authority owns never reopens generic
            # competition for any of its requests.
            "generic_closed": await self.repository.collection_route_authority(record.transfer_id),
        }

    async def _root_availability(self, record, competition) -> dict[str, AvailabilityState]:
        """Each provider competing for this unbound BitTorrent root with its
        read-only availability. The answer an earlier round already gave for
        it is used once, while fresh; otherwise one round observes it together
        with the other unbound BitTorrent roots waiting to be routed, so a
        provider that batches is asked once for all of them. An answer only
        ever prefers among the root's competitors at the moment it is routed."""
        answers = getattr(self, "_availability_answers", None)
        if answers is None:
            answers = self._availability_answers = {}
            self._availability_round_lock = asyncio.Lock()
        # Concurrent routing waits for a round in progress instead of
        # starting its own: that round may already be observing this root.
        async with self._availability_round_lock:
            now = self.clock()
            for request_id, (observed_at, _states) in list(answers.items()):
                if now - observed_at > AVAILABILITY_ROUND_FRESH_SECONDS:
                    answers.pop(request_id, None)
            earlier = answers.pop(record.id, None)
            if earlier is not None:
                return earlier[1]
            roots = [(record, competition)]
            for item in await self._availability_round_roots(record, answers):
                roots.append((item, await self._competition(item)))
            observed = await self._observe_availability(roots)
            for item, _competition in roots[1:]:
                answers[item.id] = (now, observed[item.id])
            return observed[record.id]

    async def _availability_round_roots(self, record, answers) -> list:
        """The other unbound BitTorrent roots waiting to be routed (this
        root's transfer first), up to the round's bound."""
        found = []
        transfers = sorted(await self.repository.active(), key=lambda item: item.id != record.transfer_id)
        for transfer in transfers:
            for item in await self.repository.requests(transfer.id):
                if len(found) >= AVAILABILITY_ROUND_LIMIT - 1:
                    return found
                if (item.id == record.id or item.id in answers or item.parent_id is not None
                        or item.state != "pending" or not self.registry.availability_orders(item.resolvable)
                        or await self.repository.bound_route_provider(item.id)):
                    continue
                found.append(item)
        return found

    async def _observe_availability(self, roots) -> dict[str, dict[str, AvailabilityState]]:
        """``{request_id: {provider_id: state}}`` for each root's competitors:
        each provider that declares ``AVAILABILITY`` is asked once, about every
        root it competes for, within the bound; everyone else -- and any root
        whose provider failed, timed out or answered malformed -- is UNKNOWN.
        Concurrent, creates nothing, never a routing failure. Nobody outside a
        root's competition is asked about it."""
        observed, asked = {}, {}
        for item, competition in roots:
            competitors = self.registry.eligible_providers(item.resolvable, **competition)
            observed[item.id] = {provider.descriptor.id: AvailabilityState.UNKNOWN for provider in competitors}
            for provider in competitors:
                if Capability.AVAILABILITY in provider.descriptor.capabilities:
                    asked.setdefault(provider.descriptor.id, (provider, []))[1].append(item)

        async def observe(provider, items) -> None:
            try:
                states = await asyncio.wait_for(provider.availability(tuple(item.resolvable for item in items)),
                                                AVAILABILITY_TIMEOUT_SECONDS)
            except Exception as exc:
                logger.debug("availability unknown provider=%s: %s", provider.descriptor.id, type(exc).__name__)
                return
            if (not isinstance(states, tuple) or len(states) != len(items)
                    or not all(isinstance(state, AvailabilityState) for state in states)):
                return
            for item, state in zip(items, states):
                observed[item.id][provider.descriptor.id] = state

        await asyncio.gather(*(observe(provider, items) for provider, items in asked.values()))
        return observed

    async def _prepare_standbys(self) -> None:
        """Speculative backup preparation, after the pass's primary work.

        For a root whose primary route has begun, each other provider still in
        its TASK1 competition whose account is entitled and that allows a
        speculative preparation of it (``speculative_preparation_allowed``)
        may prepare it as a backup, through that provider's ordinary
        ``resolve`` inside ``speculative_preparation``. A durable claim
        (``begin_standby``) comes first, so a root never holds two for one
        provider and an interrupted attempt is reconciled rather than
        repeated. Before each attempt the pass yields if any primary
        resolution is runnable, and it makes at most the resolution
        concurrency's worth of attempts. A backup never touches the root's
        route, request state, primary resource, entitlement or health, and is
        never a candidate. Bound backups are observed first."""
        lock = getattr(self, "_standby_lock", None)
        if lock is None:
            lock = self._standby_lock = asyncio.Lock()
        if lock.locked():
            return
        async with lock:
            if await self.repository.globally_paused():
                return
            transfers = await self.repository.active()
            capacities: dict[tuple[str, str], ActiveCapacity | None] = {}
            await self._observe_standbys(transfers)
            await self._settle_released_standbys(transfers)
            # An interrupted claim nothing proved absent is never attempted
            # again in this pass: it may already hold a resource.
            unsettled = await self._reconcile_standby_claims(transfers)
            budget = max(1, self.policy.resolution_concurrency)
            for transfer in transfers:
                if not await self._live(transfer.id, admission=True):
                    continue
                for record in await self.repository.requests(transfer.id):
                    if record.parent_id is not None or record.state not in _STANDBY_ROOT_STATES:
                        continue
                    primary = await self.repository.bound_route_provider(record.id)
                    if not primary:
                        continue
                    for provider in self._standby_providers(record, primary, await self._competition(record)):
                        if (record.id, provider.descriptor.id) in unsettled:
                            continue
                        if budget <= 0 or await self.repository.primary_resolution_runnable(self.clock()):
                            return
                        key = (provider.descriptor.id, record.resolvable.kind)
                        if key not in capacities:
                            capacities[key] = await self._active_capacity(provider, record.resolvable)
                        claimed = await self.repository.begin_standby(
                            transfer.id, record.id, provider.descriptor.id, self.clock())
                        if claimed is None:
                            continue
                        fact = capacities[key]
                        if fact is not None and fact.maximum is not None and fact.occupancy is not None \
                                and fact.occupancy >= fact.maximum:
                            # Full: no productive call at all; the backup waits
                            # through the ordinary deferral and its backoff.
                            full = self._error(Category.CONCURRENCY_LIMITED, Stage.RESOLUTION,
                                               domain=Domain.PROVIDER, retryability=Retryability.BACKOFF)
                            await self.repository.defer_standby(
                                claimed[0], full, self._standby_retry_at(full, claimed[1]), self.clock())
                            continue
                        budget -= 1
                        if await self._prepare_standby(record, provider, *claimed):
                            # What the new backup occupies is the provider's to
                            # say (a cached one occupies nothing): read again.
                            capacities.pop(key, None)

    def _standby_providers(self, record, primary: str, competition) -> list:
        """The providers that may prepare ``record`` as a backup now: its
        TASK1 competitors other than its primary, entitled, and allowing it."""
        request = record.resolvable
        return [provider for provider in self.registry.eligible_providers(request, **competition)
                if provider.descriptor.id != primary
                and self.registry.entitlement_for(provider, request) is True
                and self.registry.speculative_preparation_allowed(provider, request)]

    def _standby_retry_at(self, error: NormalizedError, attempts: int) -> float:
        """When a deferred backup may be tried again: the existing retry
        policy's doubling delay, capped, never sooner than the provider asked."""
        delay = min(self.policy.max_retry_delay, self.policy.retry_delay * 2 ** max(0, attempts))
        return self.clock() + max(delay, float(error.retry_after_seconds or 0))

    async def _prepare_standby(self, record, provider, standby_id: str, attempts: int) -> bool:
        """One speculative preparation attempt. A resource it yields is bound
        to the root as a backup (``True``); a refusal that only says "not now"
        defers the backup; any other failure ends only this backup."""
        try:
            async with self._resolution_slot():
                with speculative_preparation():
                    result = await provider.resolve(record.resolvable)
            result = self._authoritative_provider_result(provider.descriptor.id, result,
                                                         request_kind=record.resolvable.kind)
            observation = result.observation
            if observation is not None:
                # A resource the provider holds is recorded even when it
                # reports a problem: it is ours to observe and to clean up.
                await self.repository.bind_standby(standby_id, record.transfer_id, observation.resource,
                                                   observation.state, self.clock())
                return True
            raise TransferError(result.error or self._error(
                Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION, domain=Domain.PROVIDER,
                retryability=Retryability.NEVER))
        except Exception as exc:
            error = exc.error if isinstance(exc, TransferError) else unknown_failure(
                exc, integration_id=provider.descriptor.id, domain=Domain.PROVIDER, stage=Stage.RESOLUTION,
                secrets=(str(record.request.payload),))
            if error.category in _STANDBY_DEFERRABLE or error.retryability == Retryability.BACKOFF:
                await self.repository.defer_standby(standby_id, error, self._standby_retry_at(error, attempts),
                                                    self.clock())
            else:
                await self.repository.fail_standby(standby_id, error, self.clock())
            logger.debug("backup preparation provider=%s request=%s: %s", provider.descriptor.id, record.id,
                         error.category.value)
            return False

    async def _reconcile_standby_claims(self, transfers) -> set[tuple[str, str]]:
        """A ``creating`` claim found when a pass starts was interrupted
        (passes never overlap): its provider may or may not have created the
        resource. It is settled from the provider's read-only inventory --
        never by asking it to create anything:

        * exactly one of the provider's own resources for the root's
          fingerprint: that is the backup, bound as ADOPTED;
        * a complete inventory without one: it was never created -- the
          claim gets its one ordinary attempt while the provider allows a
          backup, and otherwise ends here (``RESOURCE_NOT_FOUND``);
        * anything less certain changes nothing.

        Returns the ``(request_id, provider_id)`` of every interrupted claim
        this pass could not settle and must not attempt again: only a claim
        proven absent while still allowed is left out of it.
        """
        snapshots: dict[str, object] = {}
        unsettled: set[tuple[str, str]] = set()
        for transfer in transfers:
            for item in await self.repository.standbys(transfer.id):
                if item["state"] != "creating":
                    continue
                unsettled.add((item["request_id"], item["provider_id"]))
                provider = self.registry.providers.get(item["provider_id"])
                record = next((request for request in await self.repository.requests(transfer.id)
                               if request.id == item["request_id"]), None)
                fingerprint = str(getattr(record.request, "fingerprint", "") or "").casefold() if record else ""
                if (record is None or not fingerprint or provider is None or not provider.descriptor.enabled
                        or not isinstance(provider, Inventory)):
                    continue
                if provider.descriptor.id not in snapshots:
                    try:
                        snapshots[provider.descriptor.id] = await provider.inventory()
                    except Exception as exc:
                        logger.debug("backup reconciliation provider=%s: %s", provider.descriptor.id,
                                     type(exc).__name__)
                        snapshots[provider.descriptor.id] = None
                snapshot = snapshots[provider.descriptor.id]
                if snapshot is None or snapshot.error is not None:
                    continue
                matches = [observed for observed in snapshot.observations
                           if observed.resource.provider_id == provider.descriptor.id
                           and str(observed.fingerprint or "").casefold() == fingerprint
                           and observed.state != ResourceState.ABSENT]
                if not matches and snapshot.complete:
                    if self.registry.speculative_preparation_allowed(provider, record.resolvable):
                        unsettled.discard((item["request_id"], item["provider_id"]))   # its one ordinary attempt
                    else:
                        await self.repository.fail_standby(item["id"], NormalizedError(
                            Domain.PROVIDER, Category.RESOURCE_NOT_FOUND, Stage.RECONCILIATION,
                            Retryability.NEVER, integration_id=provider.descriptor.id), self.clock())
                        unsettled.discard((item["request_id"], item["provider_id"]))
                elif len(matches) == 1:
                    found = matches[0]
                    unsettled.discard((item["request_id"], item["provider_id"]))
                    try:
                        await self.repository.bind_standby(
                            item["id"], transfer.id, replace(found.resource, ownership=Ownership.ADOPTED),
                            found.state, self.clock())
                    except Exception as exc:
                        error = exc.error if isinstance(exc, TransferError) else unknown_failure(
                            exc, integration_id=provider.descriptor.id, domain=Domain.PROVIDER,
                            stage=Stage.RECONCILIATION)
                        await self.repository.fail_standby(item["id"], error, self.clock())
        return unsettled

    async def _active_capacity(self, provider, request) -> ActiveCapacity | None:
        """``provider``'s own active-capacity fact for ``request`` (bounded,
        creates nothing): ``None`` when the request has no such capacity
        there; an unreadable answer is a capacity with nothing known."""
        if not isinstance(provider, ActiveCapacitySource):
            return None
        try:
            fact = await asyncio.wait_for(provider.active_capacity(request), ACTIVE_CAPACITY_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.debug("active capacity unknown provider=%s: %s", provider.descriptor.id, type(exc).__name__)
            return ActiveCapacity()
        return fact if isinstance(fact, ActiveCapacity) else None

    async def _make_primary_room(self, record, provider) -> bool:
        """Whether a root's productive creation may go ahead at ``provider``.

        Primary work outranks DebridPulse's own backups, never the provider's
        effective maximum (the account's, lowered by the operator's ceiling):
        when that capacity is full, exactly the backups needed to free one
        slot are given back, and the creation goes ahead only once their
        cleanup has CONFIRMED the slots free. ``False`` -- no productive call
        -- while the capacity is full and not confirmed freed (too few
        backups of DebridPulse's to give back, or their cleanup still
        pending). An unknown maximum or occupancy, or a member request,
        always goes ahead: the provider's own answer is then final."""
        if record.parent_id is not None:
            return True
        fact = await self._active_capacity(provider, record.resolvable)
        if fact is None or fact.maximum is None or fact.occupancy is None:
            return True
        needed = fact.occupancy - fact.maximum + 1
        if needed <= 0:
            return True
        held = await self.repository.reclaimable_standbys(provider.descriptor.id)
        if len(held) < needed:
            return False
        return await self._reclaim(held[:needed]) >= needed

    async def _primary_resolution(self, record, provider, cached: bool) -> ResolutionResult | None:
        """The root's productive resolution. A refusal that says the
        provider's concurrent active capacity is exhausted -- on a request
        that has such capacity there -- while DebridPulse holds a backup on
        that provider reclaims ONE backup and retries ONCE; anything else,
        or a second refusal, is the ordinary refusal."""
        def call():
            return provider.resolve_cached(record.resolvable) if cached else provider.resolve(record.resolvable)

        try:
            return await call()
        except TransferError as exc:
            if cached or not await self._reclaim_after_refusal(record, provider, exc.error):
                raise
        return await call()

    async def _reclaim_after_refusal(self, record, provider, error: NormalizedError) -> bool:
        if record.parent_id is not None or error.category != Category.CONCURRENCY_LIMITED:
            return False
        if await self._active_capacity(provider, record.resolvable) is None:
            return False
        held = await self.repository.reclaimable_standbys(provider.descriptor.id)
        return bool(held) and await self._reclaim(held[:1]) > 0

    async def _settle_released_standbys(self, transfers) -> None:
        """A backup whose resource the ordinary cleanup owner has since
        confirmed gone -- a reclamation whose cleanup finished on a later
        cadence, or a resource the provider dropped -- no longer holds
        anything: its claim returns to deferred so the ordinary cadence may
        prepare it again."""
        for transfer in transfers:
            for item in await self.repository.standbys(transfer.id):
                if (item["state"] == "bound" and not item.get("promoted_at")
                        and item["resource_state"] == ResourceState.ABSENT.value):
                    await self.repository.release_standby(item["id"], self.clock())

    async def _reclaim(self, held) -> int:
        """Give back backups through the existing cleanup authority; a slot
        counts as free only once its cleanup confirmed the resource gone.
        Returns how many were freed."""
        for item in held:
            await self.repository.cleanup_intent(item["transfer_id"], item["resource"].id, CleanupAuthority.OWNED)
        await self._cleanup_pending()
        freed = 0
        for item in held:
            if await self.repository.release_standby(item["id"], self.clock()):
                freed += 1
        return freed

    async def _promoted_standby(self, record, provider) -> ResolutionResult | None:
        """The one promotion owner. Once the router has made ``provider`` this
        root's route, a backup that provider already prepared for the root is
        taken over as its resource -- read with the provider's own
        ``observe``, never created again -- and the rest of the lifecycle is
        the ordinary sequential one. ``None`` when there is nothing to take
        over (no live backup, a provider that cannot observe one, or a backup
        the provider reports gone, which is recorded as such): ordinary
        resolution proceeds. Promotion never chooses a provider."""
        if not isinstance(provider, ResourceLookup):
            return None
        held = await self.repository.promotable_standby(record.id, provider.descriptor.id)
        if held is None:
            return None
        standby_id, resource = held
        observed = await provider.observe(resource)
        if observed.resource.provider_id != provider.descriptor.id or observed.resource.id != resource.id:
            raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION,
                                            domain=Domain.PROVIDER, retryability=Retryability.NEVER))
        if observed.state in _STANDBY_ENDED_RESOURCE:
            await self.repository.observe_standby(standby_id, record.transfer_id, observed.resource, observed.state,
                                                  self.clock())
            return None
        await self.repository.promote_standby(standby_id, self.clock())
        observation = replace(observed, request=record.resolvable)
        return ResolutionResult(observation.state, observation=observation, error=observation.error)

    async def _observe_standbys(self, transfers) -> None:
        """Each bound backup's resource as its provider observes it, at most
        once per resource poll interval. A disabled provider's backup is
        parked, exactly like a parked primary; an observation that fails
        changes nothing."""
        for transfer in transfers:
            for item in await self.repository.standbys(transfer.id):
                resource = item["resource"]
                if item["state"] != "bound" or resource is None or item.get("promoted_at"):
                    continue
                if item["resource_state"] and ResourceState(item["resource_state"]) in _STANDBY_ENDED_RESOURCE:
                    continue
                if self.clock() - float(item["observed_at"] or 0) < self.policy.resource_poll_interval:
                    continue
                provider = self.registry.providers.get(resource.provider_id)
                if provider is None or not provider.descriptor.enabled or not isinstance(provider, ResourceLookup):
                    continue
                try:
                    observed = await provider.observe(resource)
                except Exception as exc:
                    logger.debug("backup observation provider=%s: %s", resource.provider_id, type(exc).__name__)
                    continue
                if observed.resource.provider_id != resource.provider_id:
                    continue
                await self.repository.observe_standby(item["id"], transfer.id, observed.resource, observed.state,
                                                      self.clock())

    @staticmethod
    def _route_evidence(record, route: ProviderRoute) -> str | None:
        """The decision a ROOT request's route attempt records, encoded; a
        member's route belongs to the route that decomposed it. Visibility
        only: an encoding failure records nothing and changes nothing."""
        if record.parent_id is not None or route.decision is None:
            return None
        try:
            return route.decision.encode()
        except Exception as exc:
            logger.debug("routing decision not recorded request=%s: %s", record.id, type(exc).__name__)
            return None

    async def _record_route_hold(self, record, route: ProviderRoute) -> None:
        """A root decision that starts no route attempt -- held, or nothing
        can take the request -- is recorded on the request. An unchanged hold
        is written once, not every cycle. A recording failure is contained:
        it never changes what routing does next."""
        holds = getattr(self, "_route_holds", None)
        if holds is None:
            holds = self._route_holds = {}
        if route.provider is not None:
            holds.pop(record.id, None)
            return
        encoded = self._route_evidence(record, route)
        if encoded is None or holds.get(record.id) == encoded:
            return
        try:
            await self.repository.record_route_decision(record.id, encoded)
        except Exception as exc:
            logger.debug("routing hold not recorded request=%s: %s", record.id, type(exc).__name__)
            return
        holds[record.id] = encoded

    async def _cached_alternative(self, record, provider):
        """The first alternative of ``record``'s explicit group, in submitted
        order, that ``provider`` -- the route of ``record`` and of every
        alternative asked about -- already holds; ``None`` when none is, or
        the provider cannot say. Only ``record`` itself and never-attempted
        dormant alternatives are asked; nothing is created by asking."""
        if not isinstance(provider, CachedResolution):
            return None
        asked = []
        for item in await self.repository.requests(record.transfer_id):
            if item.parent_id is not None or item.alternative_group != record.alternative_group:
                continue
            if item.id != record.id:
                if item.state != "skipped" or item.attempts:
                    continue
                try:
                    if await self._route_provider(item) is not provider:
                        continue
                except (ApplicabilityUnresolved, TransferError):
                    continue
            asked.append(item)
        try:
            presence = await provider.cache_presence(tuple(item.resolvable for item in asked))
        except Exception:
            return None
        if len(presence) != len(asked):
            return None
        return next((item for item, fact in zip(asked, presence) if fact == CachePresence.HIT), None)

    async def _observe_resource(self, record):
        provider = None
        first_commitment = False
        try:
            provider = self._bound_resource_provider(record)
            if provider is None:
                return first_commitment
            if not isinstance(provider, ResourceLookup):
                raise TransferError(self._error(
                    Category.UNSUPPORTED_CAPABILITY,
                    Stage.RECONCILIATION,
                    domain=Domain.REQUEST,
                    retryability=Retryability.NEVER,
                ))
            observation = await provider.observe(record.resource)
            await self.repository.resource_observation(
                record.transfer_id, observation.resource, observation.state,
            )
            await self._converge_root_observation_name(record, observation)
            if not await self._live(record.transfer_id, admission=True):
                return first_commitment
            # Fail-closed materialization guard. Whatever path bound this
            # resource (resolution, adoption, reuse, restart reconciliation,
            # recovery, failover, a future provider), the canonical selection owner
            # decides here, before any manifest can expand, whether a generation
            # governs this (request, binding): it creates the current one if the
            # request needs selection and none exists, and reports ``held`` if it
            # cannot. A missing generation is therefore never read as ALL -- only a
            # request that genuinely never needed selection reaches the executable
            # manifest ungoverned.
            authority = await self._secure_root_selection(
                record, provider, observation, resource=record.resource,
            )
            if authority.held:
                await self.repository.poll_after(
                    record.id, self.clock() + self.policy.resource_poll_interval,
                )
                return first_commitment
            binding_id = authority.binding_id
            selecting = authority.governed

            if observation.error:
                await self._request_failure(record, observation.error, waiting=True)
            elif observation.state == ResourceState.AVAILABLE:
                if not isinstance(provider, Manifest):
                    raise TransferError(self._error(
                        Category.UNSUPPORTED_CAPABILITY,
                        Stage.CANDIDATE_PREPARATION,
                        domain=Domain.REQUEST,
                        retryability=Retryability.NEVER,
                    ))
                if selecting:
                    # Provider-side acquisition is done; only local executable
                    # materialization is held while the interactive selector's
                    # user-decision hold, or the bounded post-AVAILABLE
                    # manifest-acquisition grace, is still legitimately open. The
                    # gate decision AND the scheduling of the wait it produces
                    # are one atomic transaction (specification section 8), so a
                    # settled EXPLICIT/ALL can never have a stale WAIT recreate a
                    # future retry_at. Re-uses the ordinary resolution wakeup
                    # cadence; no new loop, no browser polling.
                    gate = await self.repository.file_selection_gate(
                        record.id, binding_id, now=self.clock(),
                        poll_interval=self.policy.resource_poll_interval,
                        resource_available=True,
                    )
                    if gate != fs.SelectionGate.PROCEED:
                        return first_commitment
                # The freshly observed provider resource, not the one frozen
                # onto the request row at its first resolution attempt:
                # ``transfer_requests.resource`` is written by
                # ``TransferRepository.resolution()`` and inventory adoption
                # only, never refreshed by ``resource_observation()``, so it can
                # lag behind whatever the provider has since learned about the
                # SAME canonical resource. Core has already accepted
                # ``observation.resource`` as the durable binding two statements
                # above; handing the provider back its own latest statement is
                # the only self-consistent input for the executable manifest.
                # Entirely provider-agnostic: core never inspects the opaque
                # ``context``, and a provider whose ``observe()`` returns the
                # resource unchanged sees no difference at all.
                entries = await provider.manifest(observation.resource)
                # Decomposition is an admission boundary: a member resource
                # that carries credentials is split exactly like a submission.
                entries, supplied = self._split_member_credentials(entries)
                entries = tuple({
                    _engine_base.codec.dump(entry): entry for entry in entries
                }.values())
                if not entries:
                    raise TransferError(self._error(
                        Category.RESOLUTION_TEMPORARILY_FAILED,
                        Stage.CANDIDATE_PREPARATION,
                        domain=Domain.RESOLUTION,
                        retryability=Retryability.BACKOFF,
                    ))
                # FILE vs COLLECTION, decided once on the FULL executable
                # manifest: a manifest whose one and only member is the
                # resource itself (its member path IS the resource's
                # authoritative name -- a single-file torrent) is a file, not a
                # collection of one, so it gets no collection folder of its own
                # name. Neutral facts only; no provider is consulted.
                if len(entries) == 1 and observation.name and entries[0].relative_path == observation.name:
                    entries = (replace(entries[0], whole_resource=True),)
                paths = [
                    str(_engine_base.destination(self.root, entry.relative_path)).casefold()
                    for entry in entries
                ]
                if len(paths) != len(set(paths)):
                    raise TransferError(self._error(
                        Category.PATH_POLICY_VIOLATION,
                        Stage.CANDIDATE_PREPARATION,
                        domain=Domain.SECURITY,
                    ))
                # Core filters the full executable manifest to the authorized
                # subset (ALL / confirmed EXPLICIT) and durably records the
                # materialization-commit fact before child fan-out. A confirmed
                # subset that can no longer be proven fails closed here.
                if selecting:
                    authorized = await self.repository.commit_selected_manifest(
                        record, entries, now=self.clock(),
                    )
                    if authorized.held:
                        # The new generation is not provably the established
                        # decomposition: the root holds, nothing fans out, and
                        # every member and file stays exactly as it is.
                        await self.repository.poll_after(
                            record.id, self.clock() + self.policy.resource_poll_interval,
                        )
                        return first_commitment
                    first_commitment = bool(getattr(authorized, "first_commitment", False))
                else:
                    authorized = entries
                await self.repository.manifest(
                    record, authorized, selection_id=getattr(authorized, "selection_id", None),
                )
                members = {entry.relative_path for entry in authorized}
                for relative_path, alternate, address, values in supplied:
                    if relative_path in members:
                        await self._admit_supplied(record.transfer_id,
                                                   manifest_child_identity(record.id, relative_path, alternate),
                                                   address, values)
            elif observation.state in {ResourceState.ABSENT, ResourceState.EXPIRED}:
                error = self._error(
                    Category.RESOURCE_EXPIRED
                    if observation.state == ResourceState.EXPIRED
                    else Category.RESOURCE_NOT_FOUND,
                    Stage.RESOLUTION,
                    domain=Domain.PROVIDER,
                    retryability=Retryability.AFTER_RERESOLUTION,
                )
                await self._request_failure(record, error, waiting=True)
            elif observation.state in {ResourceState.UNKNOWN, ResourceState.UNAVAILABLE}:
                raise TransferError(self._error(
                    Category.UNMAPPED_PROVIDER_ERROR,
                    Stage.RECONCILIATION,
                    domain=Domain.PROVIDER,
                ))
            elif observation.state == ResourceState.PREPARING:
                await self.repository.poll_after(
                    record.id, self.clock() + self.policy.resource_poll_interval,
                )
            return first_commitment
        except Exception as exc:
            error = exc.error if isinstance(exc, TransferError) else unknown_failure(
                exc,
                integration_id=provider.descriptor.id if provider else "",
                domain=Domain.PROVIDER,
                stage=Stage.RECONCILIATION,
            )
            await self._request_failure(record, error, waiting=True)