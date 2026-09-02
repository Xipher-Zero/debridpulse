"""Universal orchestration settings and the supported flat-config translation."""
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


_LEGACY_FIELDS = {
    "max_concurrent_downloads": "max_concurrent_executions",
    "aria2_error_retry_count": "execution_retry_count",
    "aria2_error_retry_delay_seconds": "execution_retry_delay_seconds",
    "upload_fail_retry_count": "resolution_retry_count",
    "upload_fail_retry_delay_minutes": "resolution_retry_delay_minutes",
    "aria2_poll_interval_seconds": "execution_poll_interval_seconds",
    "poll_interval_seconds": "provider_poll_interval_seconds",
    "stuck_download_timeout_hours": "stalled_timeout_hours",
}


def normalize_transfer_settings(settings, *, previous=None, supplied_fields=None):
    older = getattr(previous, "transfer_policy", None)
    entry = settings.transfer_policy
    options = older.model_dump() if older is not None else {}
    if entry is not None:
        options.update(entry.model_dump(exclude_unset=True))
    for legacy, canonical in _LEGACY_FIELDS.items():
        if (entry is None and older is None) or (supplied_fields is not None and legacy in supplied_fields):
            value = getattr(settings, legacy)
            # Preserve legacy clamping without weakening the typed new API.
            for bound in TransferSettings.model_fields[canonical].metadata:
                if hasattr(bound, "ge"):
                    value = max(bound.ge, value)
                if hasattr(bound, "le"):
                    value = min(bound.le, value)
            options[canonical] = value
    policy = TransferSettings(**options)
    translated = {legacy: getattr(policy, canonical) for legacy, canonical in _LEGACY_FIELDS.items()}
    translated["aria2_max_active_downloads"] = policy.max_concurrent_executions
    return settings.model_copy(update={**translated, "transfer_policy": policy})
