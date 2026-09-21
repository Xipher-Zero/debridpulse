"""FTP & SFTP registration and backend-owned configuration."""
from pydantic import BaseModel

from integrations.definition import IntegrationDefinition, IntegrationPresentation


class GeneralFtpOptions(BaseModel):
    """The provider has no transport tuning; the executor owns native options."""


def build(options, environment):
    from providers.general_ftp.provider import GeneralFtpProvider
    return GeneralFtpProvider()


definition = IntegrationDefinition(
    "general_ftp", "provider", "FTP & SFTP", GeneralFtpOptions, build,
    presentation=IntegrationPresentation(
        status_name="FTP & SFTP",
        static_status="healthy",
        display_order=110,
        status_group="direct_sources",
        status_group_label="Direct Sources",
    ),
)
