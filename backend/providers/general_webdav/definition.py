"""WebDAV registration and backend-owned configuration."""
from typing import Literal

from pydantic import BaseModel

from integrations.definition import IntegrationDefinition, IntegrationPresentation
from transfers.models import DiscoveryDepth

# How far below a submitted folder its files are collected: the folder itself,
# one to three levels of subfolders, or all of them -- each exactly one neutral
# discovery depth. A small set, because that is the useful control.
DIRECTORY_DEPTHS = {
    "current": DiscoveryDepth.CURRENT,
    "1": DiscoveryDepth.of(1),
    "2": DiscoveryDepth.of(2),
    "3": DiscoveryDepth.of(3),
    "all": DiscoveryDepth.UNLIMITED,
}


class GeneralWebdavOptions(BaseModel):
    """The provider's own enumeration policy; the executor owns the transport."""

    directory_depth: Literal["current", "1", "2", "3", "all"] = "current"


def build(options, environment):
    from providers.general_webdav.provider import GeneralWebdavProvider
    return GeneralWebdavProvider(depth=DIRECTORY_DEPTHS[options.directory_depth])


definition = IntegrationDefinition(
    # ``general_webdav`` is the durable provider identity and never changes; the
    # name is the ONE operator-facing label, read both by the Provider Status
    # panel and by the transfer-list provider badge.
    "general_webdav", "provider", "WebDAV", GeneralWebdavOptions, build,
    presentation=IntegrationPresentation(
        status_name="WebDAV",
        static_status="healthy",
        # The fifth member of the Network Sources group, inside the reserved
        # STANDARD band documented on ``general_http``.
        display_order=914,
        status_group="direct_sources",
        status_group_label="Network Sources",
        status_tier="general_family",
        status_tier_label="Standard Services",
    ),
)
