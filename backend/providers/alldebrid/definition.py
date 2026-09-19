"""AllDebrid registration and provider-owned settings schema."""
from pydantic import BaseModel, Field

from core.branding import APP_SHORT_NAME
from integrations.definition import IntegrationDefinition, IntegrationPresentation
from transfers.applicability import ProviderApplicability


class AllDebridOptions(BaseModel):
    api_key: str = Field(default="", repr=False)
    agent: str = APP_SHORT_NAME
    rate_limit_per_minute: int = Field(default=60, ge=0, le=600)


_LEGACY_AGENT_IDENTITIES = frozenset({
    "ACDC",
    "AllDebrid Control & Download Center",
    "AllDebrid-Client",
    "AllDebrid-Torrent-Client",
})


def _upgrade_legacy_options(legacy: dict, existing: dict) -> dict:
    """Replace a pre-DebridPulse application identity carried by legacy input."""
    upgraded = dict(legacy)
    if upgraded.get("agent") in _LEGACY_AGENT_IDENTITIES:
        upgraded["agent"] = APP_SHORT_NAME
    return upgraded


def canonical_options(settings) -> AllDebridOptions:
    """Decode the canonical ``integrations.alldebrid`` namespace of ``settings``."""
    entry = (getattr(settings, "integrations", None) or {}).get("alldebrid")
    return AllDebridOptions(**(getattr(entry, "options", None) or {}))


def build(options, environment):
    from providers.alldebrid.provider import AllDebridProvider
    provider = AllDebridProvider(
        options.api_key, options.agent,
        rate_limit_per_minute=options.rate_limit_per_minute,
    )
    # URL applicability is populated from AllDebrid's persisted/native host
    # inventory. Magnet/torrent remain neutral descriptor request-type claims.
    provider.applicability = ProviderApplicability()
    return provider


definition = IntegrationDefinition(
    "alldebrid", "provider", "AllDebrid", AllDebridOptions, build,
    secret_fields=frozenset({"api_key"}),
    ownership_fields=frozenset({"api_key"}),
    legacy_fields=(("alldebrid_api_key", "api_key"), ("alldebrid_agent", "agent"),
                   ("alldebrid_rate_limit_per_minute", "rate_limit_per_minute")),
    legacy_upgrade=_upgrade_legacy_options,
    required_options=frozenset({"api_key"}),
    presentation=IntegrationPresentation(
        status_name="AllDebrid",
        premium=True,
        status_endpoint="/integration-status/alldebrid",
        display_order=10,
    ),
)
