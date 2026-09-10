"""Canonical transfer engine with provider-transition hard-stop enforcement.

The qualified recovery implementation remains in ``_engine_recovery``.  This
public owner closes the admitted-resource continuation seam so every provider
I/O path honors the registry's bound-route enablement/health contract. It is
also the universal boundary that attaches transitional recovery compatibility
to factual provider/executor failures before lifecycle policy consumes them.
"""
from __future__ import annotations

from transfers import _engine_base, _engine_recovery, file_selection as fs
from transfers._engine_recovery import TransferEngine as _RecoveryTransferEngine
from transfers.applicability import ApplicabilityUnresolved
from transfers.contracts import Manifest, ResourceLookup
from transfers.errors import (
    Category, Domain, Recovery, Retryability, Stage, TransferError, unknown_failure,
)
from transfers.models import (
    Capability, CleanupAuthority, ExecutionState, Ownership, ResolutionResult, ResourceState,
    TransferState,
)


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

    async def _request_failure(self, record, error, *, attempts=None, waiting=False):
        """Attach legacy recovery fields only after factual integration output."""
        return await super()._request_failure(
            record, self.policy.compatibility(error), attempts=attempts, waiting=waiting,
        )

    async def _recover_artifact(self, artifact, error):
        """Enter recovery with a core-derived compatibility action."""
        return await super()._recover_artifact(artifact, self.policy.compatibility(error))

    async def _aggregate(self, transfer_id: int):
        """Repair durable paused truth after crash/restart convergence windows."""
        result = await super()._aggregate(transfer_id)
        transfer = await self.repository.get(transfer_id)
        terminal = {
            TransferState.DELETED,
            TransferState.COMPLETED,
            TransferState.CONSOLIDATED,
            TransferState.CANCELLED,
        }
        if transfer is None or transfer.state in terminal:
            return result

        paused = transfer.paused or await self.repository.globally_paused()
        if not paused:
            return result

        # Pause intent alone is not enough to claim parent PAUSED while a durable
        # execution observation is still active/unknown. Once every recorded
        # attempt is quiescent, repairing the parent is metadata-only: it does
        # not dispatch, refresh, replace a GID, or consume recovery authority.
        unsettled = {
            "prepared",
            ExecutionState.QUEUED.value,
            ExecutionState.TRANSFERRING.value,
            ExecutionState.UNKNOWN.value,
        }
        executions = await self.repository.executions(transfer_id)
        if not any(str(item.state) in unsettled for item in executions):
            await self.repository.state(transfer_id, TransferState.PAUSED)
        return result

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

    @staticmethod
    def _file_manifest_root(record, provider) -> bool:
        """A root request routed to a provider DP trusts for a neutral file
        manifest. This is capability only — it says nothing about whether the
        interactive lifecycle is engaged (that is generation existence, not
        policy; see ``repository.selection_generation_exists``).
        """
        return (
            record.parent_id is None
            and Capability.FILE_MANIFEST in provider.descriptor.capabilities
        )

    async def _after_resolution_persisted(self, record, provider, result):
        """Open a file-selection generation for this (request, provider resource)
        binding, once the resource is durably known and its initial availability
        is still observable.

        ``selection_mode`` gates ONLY this creation step. A new generation is
        opened when the submitter explicitly opted into interactive selection
        (``selection_mode == "interactive"``) OR when the transfer already owns a
        durable selection generation (an earlier interactive submission, or a
        database that predates ``selection_mode``) — in which case a
        re-resolution onto a new provider resource stays interactive and opens a
        fresh generation for the new binding, never inheriting the prior subset
        (specification section 13). It is never inferred from browser presence
        (correction section 6). Every engine step past this point checks
        generation existence, not the request's policy field.
        """
        if not self._file_manifest_root(record, provider):
            return
        observation = result.observation
        if observation is None or observation.resource is None:
            return
        wants_new = getattr(record.request, "selection_mode", fs.SELECTION_MODE_ALL) == fs.SELECTION_MODE_INTERACTIVE
        if not wants_new and not await self.repository.transfer_has_selection_generation(record.transfer_id):
            return
        now = self.clock()
        # File-selection state is keyed on the durable (transfer, resource)
        # binding-generation id, never the transfer-independent canonical resource
        # id, so an identical native resource on another transfer can never alias
        # into this generation's manifest/selection rows.
        binding_id = await self.repository.resource_binding_id(
            record.transfer_id, observation.resource.id,
        )
        await self.repository.begin_file_selection_window(
            record.id, record.transfer_id, binding_id, provider.descriptor.id,
            initially_available=(observation.state == ResourceState.AVAILABLE), now=now,
        )
        if observation.file_manifest is not None:
            await self.repository.record_file_manifest(
                record.id, binding_id, observation.file_manifest, now=now,
            )

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
                previous_error = self.policy.compatibility(previous.error) if previous.error else None
                restartable = previous.state in {ResourceState.EXPIRED, ResourceState.ABSENT} or (
                    previous.state == ResourceState.UNAVAILABLE
                    and previous_error is not None
                    and previous_error.retryability not in {Retryability.NEVER, Retryability.UNKNOWN}
                    and previous_error.domain != Domain.SECURITY
                    and previous_error.recovery in {Recovery.RETRY, Recovery.RERESOLVE, Recovery.BACKOFF}
                )
                if previous_error and not restartable:
                    raise TransferError(previous_error)
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
            file_manifest_capable = self._file_manifest_root(record, provider)
            binding_id = (
                await self.repository.resource_binding_id(record.transfer_id, record.resource.id)
                if file_manifest_capable else None
            )
            # ``selection_mode`` decided whether a generation was created in
            # ``_after_resolution_persisted``. From here on the engine is bound
            # by generation EXISTENCE, never the request's current/defaulted
            # policy field: a pre-``selection_mode`` database whose request now
            # deserializes as ``selection_mode="all"`` must still have its
            # durable PENDING hold / EXPLICIT subset / PREPARING selection
            # opportunity honored. ``selection_mode=all`` with no generation
            # skips straight to the executable manifest.
            selecting = bool(binding_id) and await self.repository.selection_generation_exists(
                record.id, binding_id,
            )
            if selecting and observation.file_manifest is not None:
                await self.repository.record_file_manifest(
                    record.id, binding_id, observation.file_manifest, now=self.clock(),
                )

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
                        return
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
                # Core filters the full executable manifest to the authorized
                # subset (ALL / confirmed EXPLICIT) and durably records the
                # materialization-commit fact before child fan-out. A confirmed
                # subset that can no longer be proven fails closed here.
                authorized = await self.repository.commit_selected_manifest(
                    record, entries, now=self.clock(),
                ) if selecting else entries
                await self.repository.manifest(record, authorized)
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