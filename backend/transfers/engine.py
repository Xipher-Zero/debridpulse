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

from transfers import _engine_base, file_selection as fs
from transfers._engine_recovery import TransferEngine as _RecoveryTransferEngine
from transfers.applicability import ApplicabilityUnresolved
from transfers.contracts import Manifest, ResourceLookup
from transfers.errors import (
    Category, Domain, Recovery, Retryability, Stage, TransferError, unknown_failure,
)
from transfers.policy import recovery_action
from transfers.models import (
    CleanupAuthority, Ownership, ResolutionResult, ResourceState,
)


class TransferEngine(_RecoveryTransferEngine):
    """Recovery-qualified engine plus authoritative bound-provider continuation."""

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
            return self.registry.provider_for_bound_route(provider_id, record.request)
        except TransferError as exc:
            configured = self.registry.providers.get(provider_id)
            if (
                exc.error.category == Category.PROVIDER_UNAVAILABLE
                and configured is not None
                and not configured.descriptor.enabled
            ):
                return None
            raise

    async def _resolve(self, record):
        attempt = None
        provider = None
        try:
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

            bound_provider_id = await self.repository.bound_route_provider(record.id)
            provider = (
                self.registry.provider_for_bound_route(bound_provider_id, record.request)
                if bound_provider_id else self.registry.provider_for(record.request)
            )
            async with self._resolution_slot():
                if not await self._live(record.transfer_id, admission=True):
                    return
                attempt = await self.repository.begin_resolution(
                    record.id, provider.descriptor.id,
                )
                if attempt is None:
                    return
                result = await provider.resolve(record.request)
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
                entries = await provider.manifest(record.resource)
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
                    first_commitment = bool(getattr(authorized, "first_commitment", False))
                else:
                    authorized = entries
                await self.repository.manifest(
                    record, authorized, selection_id=getattr(authorized, "selection_id", None),
                )
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