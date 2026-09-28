"""rsync source registration and backend-owned configuration."""
from pydantic import BaseModel

from integrations.definition import IntegrationDefinition, IntegrationPresentation


class GeneralRsyncOptions(BaseModel):
    """The provider has no transport tuning; the executor owns native options."""


def build(options, environment):
    from providers.general_rsync.provider import GeneralRsyncProvider
    return GeneralRsyncProvider()


definition = IntegrationDefinition(
    # ``general_rsync`` is the durable provider identity and never changes; the
    # name is the ONE operator-facing label, read both by the Provider Status
    # panel and by the transfer-list provider badge. It stays rsync whether the
    # source is a daemon or reached over SSH, and it is distinct from the
    # ``rsync`` executor identity (identities are unique across both classes).
    "general_rsync", "provider", "rsync", GeneralRsyncOptions, build,
    presentation=IntegrationPresentation(
        status_name="rsync",
        static_status="healthy",
        # The fourth member of the Network Sources group, inside the reserved
        # STANDARD band documented on ``general_http``.
        display_order=913,
        status_group="direct_sources",
        status_group_label="Network Sources",
        status_tier="general_family",
        status_tier_label="Standard Services",
    ),
)
