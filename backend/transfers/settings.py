"""Universal orchestration settings: the sole authority for execution/resolution policy."""
from pydantic import BaseModel, Field


class TransferSettings(BaseModel):
    max_concurrent_executions: int = Field(default=3, ge=1, le=20)
    resolution_concurrency: int = Field(default=3, ge=1, le=20)
    execution_retry_count: int = Field(default=3, ge=0, le=20)
    execution_retry_delay_seconds: int = Field(default=60, ge=0, le=3600)
    resolution_retry_count: int = Field(default=3, ge=0, le=20)
    resolution_retry_delay_minutes: int = Field(default=5, ge=0, le=1440)
    execution_poll_interval_seconds: int = Field(default=2, ge=2, le=300)
    provider_poll_interval_seconds: int = Field(default=30, ge=5, le=3600)
    stalled_timeout_hours: int = Field(default=6, ge=0, le=168)


# Migration INPUT only. These pre-canonical flat configuration names are read
# exclusively by ``integrations.configuration.migrate_legacy_settings`` (the one
# load-time translation boundary) and by the read-only ``GET /settings``
# compatibility projection. They are never a runtime or persisted authority:
# ``transfer_policy`` is.
LEGACY_INPUT_FIELDS = {
    "max_concurrent_downloads": "max_concurrent_executions",
    "aria2_error_retry_count": "execution_retry_count",
    "aria2_error_retry_delay_seconds": "execution_retry_delay_seconds",
    "upload_fail_retry_count": "resolution_retry_count",
    "upload_fail_retry_delay_minutes": "resolution_retry_delay_minutes",
    "aria2_poll_interval_seconds": "execution_poll_interval_seconds",
    "poll_interval_seconds": "provider_poll_interval_seconds",
    "stuck_download_timeout_hours": "stalled_timeout_hours",
}
