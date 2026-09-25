"""HTTP(S) registration and backend-owned configuration."""
from pydantic import BaseModel

from integrations.definition import IntegrationDefinition, IntegrationPresentation


class GeneralHttpOptions(BaseModel):
    """Stage 5 intentionally has no provider-specific tuning."""


def build(options, environment):
    from providers.general_http.provider import GeneralHttpProvider
    return GeneralHttpProvider()


definition = IntegrationDefinition(
    # ``general_http`` is the durable provider identity and never changes; the
    # name is the ONE operator-facing label, read both by the Provider Status
    # panel and by the transfer-list provider badge.
    "general_http", "provider", "HTTP(S)", GeneralHttpOptions, build,
    presentation=IntegrationPresentation(
        status_name="HTTP(S)",
        static_status="healthy",
        # The reserved STANDARD band. `display_order` is the one ordering
        # authority for both entry order and tier order (a tier takes the
        # position of its first entry), so the bands are:
        #
        #     .. 100   ordinary PREMIUM entries, including every future one
        #              that declares no order and takes the model default
        #      900     the reserved PREMIUM tail (Usenet)
        #      910 ..  the reserved STANDARD band (this aggregate family)
        #
        # Declaring this above the reserved tail is what keeps Premium Services
        # rendered before Standard Services even when Usenet is the only
        # premium row.
        display_order=910,
        status_group="direct_sources",
        status_group_label="Network Sources",
        status_tier="general_family",
        status_tier_label="Standard Services",
    ),
)
