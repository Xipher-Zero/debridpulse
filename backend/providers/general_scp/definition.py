"""SCP registration and backend-owned configuration."""
from pydantic import BaseModel

from integrations.definition import IntegrationDefinition, IntegrationPresentation


class ScpOptions(BaseModel):
    """The provider has no transport tuning; the executor owns native options."""


def build(options, environment):
    from providers.general_scp.provider import ScpProvider
    return ScpProvider()


definition = IntegrationDefinition(
    # ``general_scp`` is the durable provider identity and never changes; the
    # name is the ONE operator-facing label, read both by the Provider Status
    # panel and by the transfer-list provider badge. What this provider claims
    # stays SCP there even though it executes over SFTP.
    "general_scp", "provider", "SCP", ScpOptions, build,
    presentation=IntegrationPresentation(
        status_name="SCP",
        static_status="healthy",
        # The third member of the Network Sources group, inside the reserved
        # STANDARD band documented on ``general_http``.
        display_order=912,
        status_group="direct_sources",
        status_group_label="Network Sources",
        status_tier="general_family",
        status_tier_label="Standard Services",
    ),
)
