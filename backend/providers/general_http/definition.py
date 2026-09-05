"""General HTTP(S) registration and backend-owned configuration."""
from pydantic import BaseModel

from integrations.definition import IntegrationDefinition, IntegrationPresentation


class GeneralHttpOptions(BaseModel):
    """Stage 5 intentionally has no provider-specific tuning."""


def build(options, environment):
    from providers.general_http.provider import GeneralHttpProvider
    return GeneralHttpProvider()


definition = IntegrationDefinition(
    "general_http", "provider", "HTTP & HTTPS", GeneralHttpOptions, build,
    presentation=IntegrationPresentation(
        status_name="HTTP & HTTPS",
        static_status="healthy",
        display_order=100,
        status_group="direct_sources",
        status_group_label="Direct Sources",
    ),
)
