"""rsync source registration and backend-owned configuration."""
from pydantic import BaseModel

from integrations.definition import (
    DIRECTORY_DEPTHS, DirectoryDepthSetting, IntegrationDefinition, IntegrationPresentation,
)


class GeneralRsyncOptions(BaseModel):
    """The provider's own discovery policy only; the executor owns native
    transport options and realizes the neutral depth itself."""

    # How far below a submitted directory its files are collected. "all" is
    # the whole tree -- what every rsync source has always meant.
    directory_depth: DirectoryDepthSetting = "all"


def build(options, environment):
    from providers.general_rsync.provider import GeneralRsyncProvider
    return GeneralRsyncProvider(depth=DIRECTORY_DEPTHS[options.directory_depth])


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
