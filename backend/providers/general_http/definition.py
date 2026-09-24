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
        # The GENERAL tier's second reserved position, after Usenet (20) and
        # below the presentation default (100) that a later entry inherits.
        display_order=30,
        status_group="direct_sources",
        status_group_label="General Sources",
        status_tier="general_family",
        status_tier_label="General",
    ),
)
