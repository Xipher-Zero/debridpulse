"""WebDAV registration and backend-owned configuration."""
from pydantic import BaseModel, Field, field_validator

from integrations.definition import (
    DIRECTORY_DEPTHS, DirectoryDepthSetting, IntegrationDefinition, IntegrationPresentation,
)
from transfers.models import DiscoveryLimits


class GeneralWebdavOptions(BaseModel):
    """The provider's own enumeration policy; the executor owns the transport."""

    # How far below a submitted folder its files are collected.
    directory_depth: DirectoryDepthSetting = "current"
    # The most files one collection may contain for DebridPulse to accept it;
    # a larger one fails rather than arriving partly. The default and the
    # ceiling are the neutral listing bound every discovery already has.
    max_files: int = Field(default=10_000, ge=1, le=10_000)
    # How long listing one collection, subfolders included, may take before
    # it fails as unavailable: 0 is no limit for the scan as a whole (every
    # listing request keeps its own bound) -- what every collection has always
    # had -- and a nonzero value (10-3600 seconds) sets that deadline.
    # Discovery only: never a transfer, retry or lifecycle timeout.
    collection_scan_timeout_seconds: int = Field(default=0, ge=0, le=3600)

    @field_validator("collection_scan_timeout_seconds")
    @classmethod
    def _no_limit_or_bounded(cls, value: int) -> int:
        if 0 < value < 10:
            raise ValueError("A collection scan timeout is 0 (no limit) or 10 to 3600 seconds")
        return value


def build(options, environment):
    from providers.general_webdav.provider import GeneralWebdavProvider
    return GeneralWebdavProvider(depth=DIRECTORY_DEPTHS[options.directory_depth], limits=DiscoveryLimits(
        max_files=options.max_files, timeout_seconds=options.collection_scan_timeout_seconds or None))


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
