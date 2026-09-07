"""Canonical transfer engine with provider-transition hard-stop enforcement.

The qualified recovery implementation remains in ``_engine_recovery``.  This
public owner closes the admitted-resource continuation seam so every provider
I/O path honors the registry's bound-route enablement/health contract.
"""
from __future__ import annotations

from transfers import _engine_base, _engine_recovery
from transfers._engine_recovery import TransferEngine as _RecoveryTransferEngine
from transfers.applicability import ApplicabilityUnresolved
from transfers.contracts import Manifest, ResourceLookup
from transfers.errors import (
    Category, Domain, Recovery, Retryability, Stage, TransferError, unknown_failure,
)
from transfers.models import CleanupAuthority, Ownership, ResolutionResult, ResourceState


# Preserve the public monkeypatch seams that the qualified recovery engine exposed.
stable_payload = _engine_recovery.stable_payload
retire_partial = _engine_recovery.retire_partial


async def _stable_payload_proxy(*args, **kwargs):
    return await stable_payload(*args, **kwargs)


def _retire_partial_proxy(*args, **kwargs):
    return retire_partial(*args, **kwargs)


_engine_recovery.stable_payload = _stable_payload_proxy
_engine_recovery.retire_partial = _retire_partial_proxy


class TransferEngine(_RecoveryTransferEngine):
    """Recovery-qualified engine plus authoritative bound-provider continuation."""

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
                restartable = previous.state in {ResourceState.EXPIRED, ResourceState.ABSENT} or (
                    previous.state == ResourceState.UNAVAILABLE
                    and previous.error is not None
                    and previous.error.retryability not in {Retryability.NEVER, Retryability.UNKNOWN}
                    and previous.error.domain != Domain.SECURITY
                    and previous.error.recovery in {Recovery.RETRY, Recovery.RERESOLVE, Recovery.BACKOFF}
                )
                if previous.error and not restartable:
                    raise TransferError(previous.error)
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
                        record.resource.id, CleanupAuthority.OWNED,
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
            async with self._resolution_slots:
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
            if attempt:
                await self.repository.resolution(
                    attempt, ResolutionResult(ResourceState.UNKNOWN, error=error),
                )
            await self._request_failure(
                record, error, attempts=record.attempts + (1 if attempt else 0),
            )

    async def _observe_resource(self, record):
        provider = None
        try:
            provider = self._bound_resource_provider(record)
            if provider is None:
                return
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
            if not await self._live(record.transfer_id, admission=True):
                return
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
                        recovery=Recovery.BACKOFF,
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
                await self.repository.manifest(record, entries)
            elif observation.state in {ResourceState.ABSENT, ResourceState.EXPIRED}:
                error = self._error(
                    Category.RESOURCE_EXPIRED
                    if observation.state == ResourceState.EXPIRED
                    else Category.RESOURCE_NOT_FOUND,
                    Stage.RESOLUTION,
                    domain=Domain.PROVIDER,
                    retryability=Retryability.AFTER_RERESOLUTION,
                    recovery=Recovery.RERESOLVE,
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
        except Exception as exc:
            error = exc.error if isinstance(exc, TransferError) else unknown_failure(
                exc,
                integration_id=provider.descriptor.id if provider else "",
                domain=Domain.PROVIDER,
                stage=Stage.RECONCILIATION,
            )
            await self._request_failure(record, error, waiting=True)
