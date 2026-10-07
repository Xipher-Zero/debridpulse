"""THE scoped integration-configuration mutation.

Every change of one integration's canonical namespace -- an operator's
``PATCH /integrations/{id}/configuration``, a provider route saving what it
proved, the application turning off an option the connected account is not
entitled to -- goes through ``mutate_integration_configuration``: merged,
validated, normalized, saved, applied and reconfigured under the narrow
config-write lock, then announced. The HTTP surface only maps its refusals.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from integrations.definition import IntegrationSettings


class InvalidIntegrationOptions(ValueError):
    """The merged options are not valid for the integration's options model."""


class ConfigurationRefused(ValueError):
    """``validate_configuration`` refused the change (a lifecycle invariant,
    an option the connected account is not entitled to)."""


@dataclass(frozen=True)
class SettingsStore:
    """Where the canonical settings document is read, written and published,
    and the lock that serializes every settings mutation. The settings owner
    (``core.config``) unless a caller supplies its own bound view of it."""
    current: Callable
    load: Callable
    save: Callable
    apply: Callable
    lock: Callable


def settings_store() -> SettingsStore:
    """The settings owner's own store."""
    from core import config
    return SettingsStore(config.get_settings, config.load_settings, config.save_settings, config.apply_settings,
                         config.config_write_lock)


@dataclass(frozen=True)
class MutationResult:
    settings: object
    entry: IntegrationSettings
    applied: object = None


async def mutate_integration_configuration(
        application, definition, *, options: dict | None = None, enabled: bool | None = None,
        priority: int | None = None, clear_secrets: list[str] | tuple = (), verification: list[str] | tuple = (),
        store: SettingsStore | None = None,
        apply_native: Callable[[], Awaitable[None]] | None = None) -> MutationResult:
    """Merge only the supplied option keys into ``definition``'s namespace --
    every unrelated integration and option is preserved untouched -- and
    enforce the proven configuration invariants
    (``ApplicationService.validate_configuration``) before saving.

    ``apply_native``: an integration's native apply that must run inside the
    same lock, after reconfiguring (the caller's, e.g. aria2's daemon
    lifecycle). Raises ``InvalidIntegrationOptions`` or
    ``ConfigurationRefused``; saves nothing then."""
    from integrations.configuration import accept_verification, normalize_settings
    store = store or settings_store()
    integration_id = definition.id
    # Replacing the connection itself (an ownership field) takes the exclusive
    # configuration admission: admitted work -- a productive create against
    # the current connection among it -- drains, no new work starts, and only
    # then are its references (``validate_configuration``) read, so nothing
    # can be created under one account and reconciled under another. Every
    # other option keeps the ordinary admission.
    replaces_connection = any(
        key in definition.ownership_fields
        and (key not in definition.secret_fields or (options or {})[key] not in ("", None))
        for key in (options or {})) or any(key in definition.ownership_fields for key in clear_secrets)
    admission = (application.configuration_admission() if replaces_connection
                 else application.application_operation())
    async with admission:
        # The narrow config-write lock (specification sections 9.5, 13.8)
        # serializes this load-modify-save critical section -- including the
        # ``previous`` baseline read used below -- against every other
        # settings-mutation route.
        async with store.lock():
            previous = store.current()
            current = store.load()
            existing = current.integrations.get(integration_id)
            was_enabled = bool(getattr(previous.integrations.get(integration_id), "enabled", False))
            existing_options = existing.options if isinstance(existing, IntegrationSettings) else {}
            merged_options = {**existing_options, **(options or {})}
            try:
                validated_options = definition.options_model(**merged_options).model_dump()
            except Exception as exc:
                raise InvalidIntegrationOptions(str(exc)) from exc
            entry = IntegrationSettings(
                enabled=(existing.enabled if isinstance(existing, IntegrationSettings) and enabled is None else bool(enabled)),
                priority=(existing.priority if isinstance(existing, IntegrationSettings) and priority is None else int(priority or 0)),
                options=validated_options,
                clear_secrets=list(clear_secrets),
            )
            current.integrations = {**current.integrations, integration_id: entry}
            # ``previous=previous`` (Gate 9 revision-3 rejection finding 2):
            # without it, ``normalize_settings``'s generic secret-preservation
            # branch (``old_options.get(secret)``) has no prior namespace to
            # restore a blank/omitted secret from, so an ordinary Save whose
            # already-configured-secret UI control is intentionally blank
            # (the existing UI contract: blank means "keep current") would
            # erase the stored secret. This is the SAME ``previous`` the
            # whole-settings route already threads through for exactly this
            # reason -- a scoped route is not exempt from it.
            clean = normalize_settings(current, application.definitions, previous=previous)
            # A draft the operator tested before saving it may be verified by
            # the Save that promotes it -- but only after the generic owner has
            # proven the proof describes the configuration just saved.
            clean = accept_verification(clean, definition, list(verification))
            try:
                await application.validate_configuration(previous, clean)
            except ValueError as exc:
                raise ConfigurationRefused(str(exc)) from None
            store.save(clean)
            store.apply(clean)
            # Reconfigure and the native lifecycle apply happen INSIDE the
            # config-write lock (Gate 9 revision-3 rejection finding 7):
            # previously the lock was released before this apply phase, so
            # two concurrent integration-configuration writes could apply
            # their native/lifecycle effects out of order relative to their
            # persisted revisions. Holding the lock across validate -> save
            # -> apply -> reconfigure -> lifecycle makes the last writer
            # under the lock win coherently for the durable revision AND
            # the resulting live/native state together.
            application.configure()
            if apply_native is not None:
                await apply_native()
            # An integration that owns external configuration applies it here,
            # inside the same lock, through the generic seam. No integration is
            # named: composition discovered which namespaces have appliers.
            applied = await application.apply_integration_configuration(integration_id)
    # A canonical configuration change can alter which sources are routable and
    # whether an integration's managed lifecycle component is still required.
    # Waking the neutral maintenance/resolution signals here is what makes an
    # operator-visible control IMMEDIATE rather than cadence-bound: before this,
    # an integration whose enable state had just changed converged only on the
    # 60 s integration-maintenance tick, so an operator who enabled one saw an
    # unreachable service for up to a minute. Issued AFTER the
    # application-operation block so maintenance is never woken into an
    # admission this request still holds. Neutral: it names no integration and
    # applies to every namespace.
    application.notify_applicability_changed(integration_id)
    if entry.enabled and not was_enabled:
        # Re-enabling brings the integration back into service on CURRENT
        # upstream account truth: one explicit refresh of the rebuilt live
        # owner, whatever its last-known-good freshness. Neutral (a no-op for
        # an integration without account truth); a failed check keeps the
        # operator's Enable and the owner's last-known-good. Admitted by this
        # owner, like the write: no caller's admission is assumed.
        async with application.application_operation():
            await application.refresh_account_entitlement(integration_id)
    return MutationResult(clean, entry, applied)
