"""Debrid-Link registration and provider-owned settings schema."""
from pydantic import BaseModel, Field

from integrations.definition import IntegrationDefinition, IntegrationPresentation, VerificationSubject


class DebridLinkOptions(BaseModel):
    # The operator's private API key (debrid-link.com/webapp/apikey). Written
    # through the canonical integration namespace and never returned to the
    # browser.
    api_key: str = Field(default="", repr=False)
    # Operational allowances, never retry policy: how long one ordinary
    # exchange and one torrent-file upload may take, and how often the
    # supported-host catalogue becomes due for refresh. Debrid-Link documents
    # no request-rate ceiling, so there is no local rate limit to tune.
    request_timeout_seconds: int = Field(default=30, ge=5, le=300)
    torrent_upload_timeout_seconds: int = Field(default=120, ge=30, le=900)
    host_refresh_interval_hours: int = Field(default=24, ge=1, le=168)


def canonical_options(settings) -> DebridLinkOptions:
    """Decode the canonical ``integrations.debridlink`` namespace of ``settings``."""
    entry = (getattr(settings, "integrations", None) or {}).get("debridlink")
    return DebridLinkOptions(**(getattr(entry, "options", None) or {}))


def credential_material(options: DebridLinkOptions) -> dict:
    """What the Debrid-Link Test proves: the API key. Timeouts and the host
    refresh cadence decide nothing about authentication."""
    return {"api_key": str(options.api_key or "").strip()}


def _verification_subjects(options: DebridLinkOptions):
    if not str(options.api_key or "").strip():
        return ()
    return (VerificationSubject("credential", credential_material(options)),)


def build(options, environment):
    from integrations.account_entitlement import AccountEntitlementMaintenance
    from integrations.runtime_state import ProviderRuntimeStateStore, ScopedRuntimeStateStore, credential_scope
    from providers.debridlink.account import DebridLinkAccountTranslation
    from providers.debridlink.client import DebridLinkService
    from providers.debridlink.host_runtime import DebridLinkHostMaintenance
    from providers.debridlink.provider import DebridLinkProvider

    client = DebridLinkService(options.api_key, request_timeout_seconds=options.request_timeout_seconds,
                               upload_timeout_seconds=options.torrent_upload_timeout_seconds)
    provider = DebridLinkProvider(client)
    commands = getattr(environment, "commands", None)
    # Host inventory maintenance reaches the application through the generic
    # integration lifecycle seam; composition names no provider.
    provider.hosts = DebridLinkHostMaintenance(
        provider, ProviderRuntimeStateStore(),
        notify=getattr(commands, "notify_applicability_changed", None),
        refresh_seconds=options.host_refresh_interval_hours * 3600)
    # Account truth is the connected account's: scoped to its key, so a
    # different account never inherits what this one was entitled to.
    provider.account = AccountEntitlementMaintenance(
        provider, DebridLinkAccountTranslation(client),
        ScopedRuntimeStateStore(ProviderRuntimeStateStore(), credential_scope("debridlink", client.api_key)),
        integration_id="debridlink",
        notify=getattr(commands, "notify_applicability_changed", None),
        notify_status=getattr(commands, "notify_status_changed", None))
    provider.lifecycle = (provider.hosts, provider.account)
    return provider


definition = IntegrationDefinition(
    "debridlink", "provider", "Debrid-Link", DebridLinkOptions, build,
    secret_fields=frozenset({"api_key"}),
    ownership_fields=frozenset({"api_key"}),
    required_options=frozenset({"api_key"}),
    # Participates only once an operator turns it on, so an install that never
    # uses Debrid-Link is not reported as an unconfigured provider.
    default_enabled=False,
    verification_subjects=_verification_subjects,
    presentation=IntegrationPresentation(
        status_name="Debrid-Link",
        premium=True,
        status_endpoint="/integration-status/debridlink",
        # No display_order: an ordinary premium entry takes the presentation
        # model's default and so the place the existing ordering gives it.
        status_tier="premium_service",
        status_tier_label="Premium Services",
        standard_status_tier="general_family",
        standard_status_tier_label="Standard Services",
    ),
)
