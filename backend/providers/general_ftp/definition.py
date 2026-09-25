"""(S)FTP registration and backend-owned configuration."""
from pydantic import BaseModel

from integrations.definition import IntegrationDefinition, IntegrationPresentation


class GeneralFtpOptions(BaseModel):
    """The provider has no transport tuning; the executor owns native options."""


def build(options, environment):
    from providers.general_ftp.provider import GeneralFtpProvider
    return GeneralFtpProvider()


definition = IntegrationDefinition(
    # ``general_ftp`` is the durable provider identity and never changes; the
    # name is the ONE operator-facing label, read both by the Provider Status
    # panel and by the transfer-list provider badge.
    "general_ftp", "provider", "(S)FTP", GeneralFtpOptions, build,
    presentation=IntegrationPresentation(
        status_name="(S)FTP",
        static_status="healthy",
        # The second member of the Network Sources group, inside the reserved
        # STANDARD band documented on ``general_http``; the GROUP renders at its
        # first member's position, so this only orders it within the group.
        display_order=911,
        status_group="direct_sources",
        status_group_label="Network Sources",
        status_tier="general_family",
        status_tier_label="Standard Services",
    ),
)
