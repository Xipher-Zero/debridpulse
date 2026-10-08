"""Premiumize registration and provider-owned settings schema."""
from pydantic import BaseModel, Field

from integrations.definition import IntegrationDefinition, IntegrationPresentation, VerificationSubject


class PremiumizeOptions(BaseModel):
    # The operator's API key. Written through the canonical integration
    # namespace, sent only as a Bearer header and never returned to the browser.
    api_key: str = Field(default="", repr=False)
    # "Use Premiumize Before Usenet": whether Premiumize is tried before native
    # Usenet for NZB downloads. A preference only: both stay eligible either way.
    use_before_usenet: bool = False
    # "Prepare Backup Torrents": whether Premiumize may also add a torrent or
    # magnet to the account's cloud as a backup source while another provider
    # delivers it. Off by default: a backup uses the account's resources.
    prepare_backup_torrents: bool = False
    # Operational allowances, never retry policy -- the same DebridPulse
    # conventions every premium provider uses: one ordinary exchange, one
    # torrent or NZB upload, and how often the service catalogue becomes due
    # for refresh. Premiumize publishes no request-rate ceiling and no
    # active-transfer maximum, so neither has a tunable.
    request_timeout_seconds: int = Field(default=30, ge=5, le=300)
    upload_timeout_seconds: int = Field(default=120, ge=30, le=900)
    host_refresh_interval_hours: int = Field(default=24, ge=1, le=168)


def canonical_options(settings) -> PremiumizeOptions:
    """Decode the canonical ``integrations.premiumize`` namespace of ``settings``."""
    entry = (getattr(settings, "integrations", None) or {}).get("premiumize")
    return PremiumizeOptions(**(getattr(entry, "options", None) or {}))


def credential_material(options: PremiumizeOptions) -> dict:
    """What the Premiumize Test proves: the API key. Preferences, timeouts and
    the refresh cadence decide nothing about authentication."""
    return {"api_key": str(options.api_key or "").strip()}


def _verification_subjects(options: PremiumizeOptions):
    if not str(options.api_key or "").strip():
        return ()
    return (VerificationSubject("credential", credential_material(options)),)


def build(options, environment):
    from integrations.account_entitlement import AccountEntitlementMaintenance
    from integrations.runtime_state import ProviderRuntimeStateStore, ScopedRuntimeStateStore, credential_scope
    from providers.premiumize.account import PremiumizeAccountTranslation
    from providers.premiumize.client import PremiumizeService
    from providers.premiumize.host_runtime import PremiumizeHostMaintenance
    from providers.premiumize.provider import PremiumizeProvider

    client = PremiumizeService(options.api_key, request_timeout_seconds=options.request_timeout_seconds,
                               upload_timeout_seconds=options.upload_timeout_seconds)
    provider = PremiumizeProvider(client, use_before_usenet=options.use_before_usenet,
                                  staged_input=getattr(environment, "staged_input", None),
                                  prepare_backup_torrents=options.prepare_backup_torrents)
    commands = getattr(environment, "commands", None)
    # Service catalogue maintenance reaches the application through the
    # generic integration lifecycle seam; composition names no provider.
    provider.hosts = PremiumizeHostMaintenance(
        provider, ProviderRuntimeStateStore(),
        notify=getattr(commands, "notify_applicability_changed", None),
        refresh_seconds=options.host_refresh_interval_hours * 3600)
    # Account truth is the connected account's: scoped to its key, so a
    # different account never inherits what this one was entitled to.
    provider.account = AccountEntitlementMaintenance(
        provider, PremiumizeAccountTranslation(client),
        ScopedRuntimeStateStore(ProviderRuntimeStateStore(), credential_scope("premiumize", client.api_key)),
        integration_id="premiumize",
        notify=getattr(commands, "notify_applicability_changed", None),
        notify_status=getattr(commands, "notify_status_changed", None))
    provider.lifecycle = (provider.hosts, provider.account)
    return provider


definition = IntegrationDefinition(
    "premiumize", "provider", "Premiumize", PremiumizeOptions, build,
    secret_fields=frozenset({"api_key"}),
    ownership_fields=frozenset({"api_key"}),
    required_options=frozenset({"api_key"}),
    # Participates only once an operator turns it on, so an install that never
    # uses Premiumize is not reported as an unconfigured provider.
    default_enabled=False,
    verification_subjects=_verification_subjects,
    presentation=IntegrationPresentation(
        status_name="Premiumize",
        premium=True,
        status_endpoint="/integration-status/premiumize",
        # No display_order: an ordinary premium entry takes the presentation
        # model's default and so the place the existing ordering gives it.
        status_tier="premium_service",
        status_tier_label="Premium Services",
        standard_status_tier="general_family",
        standard_status_tier_label="Standard Services",
    ),
)
