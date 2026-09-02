"""AllDebrid registration and provider-owned settings schema."""
from pydantic import BaseModel, Field

from core.branding import APP_SHORT_NAME
from integrations.definition import IntegrationDefinition
from transfers.applicability import ProviderApplicability


class AllDebridOptions(BaseModel):
    api_key: str = Field(default="", repr=False)
    agent: str = APP_SHORT_NAME
    rate_limit_per_minute: int = Field(default=60, ge=0)


def build(options, environment):
    from providers.alldebrid.provider import AllDebridProvider
    provider = AllDebridProvider(options.api_key, options.agent)
    # URL applicability is populated from AllDebrid's persisted/native host
    # inventory. Magnet/torrent remain neutral descriptor request-type claims.
    provider.applicability = ProviderApplicability()
    return provider


definition = IntegrationDefinition(
    "alldebrid", "provider", "AllDebrid", AllDebridOptions, build,
    secret_fields=frozenset({"api_key"}),
    legacy_fields=(("alldebrid_api_key", "api_key"), ("alldebrid_agent", "agent"),
                   ("alldebrid_rate_limit_per_minute", "rate_limit_per_minute")),
)
