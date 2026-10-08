"""TorBox registration and provider-owned settings schema."""
from pydantic import BaseModel, Field

from integrations.definition import (
    EntitlementOption, IntegrationDefinition, IntegrationPresentation, VerificationSubject,
)
from providers.torbox.rate_limit import SERVICE_LIMIT_PER_MINUTE


class TorBoxOptions(BaseModel):
    # The account's API token, as TorBox's device authorization issued it.
    # Never typed by an operator and never returned to the browser.
    api_token: str = Field(default="", repr=False)
    # "Use TorBox Before Usenet": whether TorBox is tried before native Usenet
    # for NZB downloads. A preference only -- TorBox takes NZBs either way,
    # when its plan includes them -- and native Usenet is never affected by it.
    use_before_usenet: bool = False
    # TorBox documents 300 requests per minute per token; the default leaves
    # headroom and the ceiling is the service's own.
    rate_limit_per_minute: int = Field(default=240, ge=1, le=SERVICE_LIMIT_PER_MINUTE)
    # Operational allowances, never retry policy: one ordinary exchange, one
    # torrent or NZB upload, and how often the supported-host catalogue
    # becomes due for refresh.
    request_timeout_seconds: int = Field(default=30, ge=5, le=300)
    upload_timeout_seconds: int = Field(default=120, ge=30, le=900)
    host_refresh_interval_hours: int = Field(default=24, ge=1, le=168)
    # "Prepare Backup Torrents": whether TorBox may also add a torrent or
    # magnet as a backup source while another provider delivers it. Off by
    # default: every such addition uses TorBox create limits and active slots.
    prepare_backup_torrents: bool = False
    # "Maximum Active Torrents": the operator's own ceiling on active torrent
    # slots. Absent (None) follows the account plan's maximum; a value can
    # only lower it -- the plan maximum is never exceeded.
    max_active_torrents: int | None = Field(default=None, ge=1, le=10)


def canonical_options(settings) -> TorBoxOptions:
    """Decode the canonical ``integrations.torbox`` namespace of ``settings``."""
    entry = (getattr(settings, "integrations", None) or {}).get("torbox")
    return TorBoxOptions(**(getattr(entry, "options", None) or {}))


def credential_material(options: TorBoxOptions) -> dict:
    """What the TorBox Test proves: the saved API token. Tunables and the
    Usenet preference decide nothing about authentication."""
    return {"api_token": str(options.api_token or "")}


def _verification_subjects(options: TorBoxOptions):
    if not options.api_token:
        return ()
    return (VerificationSubject("credential", credential_material(options)),)


def build(options, environment):
    from integrations.account_entitlement import AccountEntitlementMaintenance
    from integrations.runtime_state import ProviderRuntimeStateStore, ScopedRuntimeStateStore, credential_scope
    from providers.torbox.account import TorBoxAccountTranslation
    from providers.torbox.client import TorBoxService
    from providers.torbox.host_runtime import TorBoxHostMaintenance
    from providers.torbox.provider import TorBoxProvider

    client = TorBoxService(options.api_token, rate_limit_per_minute=options.rate_limit_per_minute,
                           request_timeout_seconds=options.request_timeout_seconds,
                           upload_timeout_seconds=options.upload_timeout_seconds)
    provider = TorBoxProvider(client, use_before_usenet=options.use_before_usenet,
                              staged_input=getattr(environment, "staged_input", None),
                              prepare_backup_torrents=options.prepare_backup_torrents,
                              max_active_torrents=options.max_active_torrents)
    commands = getattr(environment, "commands", None)
    # Host inventory maintenance reaches the application through the generic
    # integration lifecycle seam; composition names no provider.
    provider.hosts = TorBoxHostMaintenance(
        provider, ProviderRuntimeStateStore(),
        notify=getattr(commands, "notify_applicability_changed", None),
        refresh_seconds=options.host_refresh_interval_hours * 3600)
    # Account truth is the connected account's: scoped to its token, so a
    # different account never inherits what this one was entitled to.
    provider.account = AccountEntitlementMaintenance(
        provider, TorBoxAccountTranslation(client),
        ScopedRuntimeStateStore(ProviderRuntimeStateStore(), credential_scope("torbox", options.api_token)),
        integration_id="torbox",
        notify=getattr(commands, "notify_applicability_changed", None),
        notify_status=getattr(commands, "notify_status_changed", None))
    provider.lifecycle = (provider.hosts, provider.account)
    return provider


definition = IntegrationDefinition(
    "torbox", "provider", "TorBox", TorBoxOptions, build,
    secret_fields=frozenset({"api_token"}),
    ownership_fields=frozenset({"api_token"}),
    required_options=frozenset({"api_token"}),
    # Participates only once an operator turns it on, so an install that never
    # uses TorBox is not reported as an unconfigured provider.
    default_enabled=False,
    verification_subjects=_verification_subjects,
    # "Use TorBox Before Usenet" prefers TorBox for NZBs, which only a plan
    # with Usenet takes (``providers.torbox.account.PLAN_ENTITLEMENT``: Pro).
    entitlement_options=(EntitlementOption("use_before_usenet", frozenset({"nzb"}), "Requires TorBox Pro."),),
    # The preference was first saved as "usenet_enabled" (participation).
    renamed_options=(("usenet_enabled", "use_before_usenet"),),
    presentation=IntegrationPresentation(
        status_name="TorBox",
        premium=True,
        status_endpoint="/integration-status/torbox",
        display_order=12,
        status_tier="premium_service",
        status_tier_label="Premium Services",
        standard_status_tier="general_family",
        standard_status_tier_label="Standard Services",
    ),
)
