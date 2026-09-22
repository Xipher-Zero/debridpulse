from core.config import AppSettings
from integrations.catalog import definitions
from integrations.configuration import (
    clamp_persisted_namespaces,
    migrate_legacy_settings,
    normalize_settings,
    public_integrations,
)
from integrations.definition import IntegrationSettings
from transfers.settings import TransferSettings


def _settings_from_legacy(raw: dict) -> AppSettings:
    """Load a raw pre-canonical mapping exactly as ``load_settings`` does:
    translate at the one boundary, then build and complete canonical settings."""
    raw = dict(raw)
    migrate_legacy_settings(raw, definitions)
    return normalize_settings(AppSettings(**{k: v for k, v in raw.items() if k in AppSettings.model_fields}), definitions)


def test_flat_settings_migrate_without_losing_native_credentials_or_tuning():
    migrated = _settings_from_legacy({"alldebrid_api_key": "private-key", "aria2_split": 32})
    assert migrated.integrations["alldebrid"].options["api_key"] == "private-key"
    assert migrated.integrations["aria2"].options["split"] == 32
    # The settings model has no flat field to carry them: canonical namespaces
    # are the only place these values exist.
    assert not {"alldebrid_api_key", "aria2_split"} & set(migrated.model_dump())
    assert normalize_settings(migrated, definitions) == migrated
    public = public_integrations(migrated, definitions)
    assert "private-key" not in str(public)
    assert public["alldebrid"]["options"]["api_key_configured"] is True


def test_partial_namespace_update_preserves_secret_and_disabled_state():
    previous = _settings_from_legacy({"alldebrid_api_key": "private-key"})
    previous.integrations["alldebrid"].enabled = False
    draft = AppSettings(integrations={"alldebrid": IntegrationSettings(options={"api_key": "", "agent": "custom"})})
    merged = normalize_settings(draft, definitions, previous=previous)
    assert merged.integrations["alldebrid"].enabled is False
    assert merged.integrations["alldebrid"].options["api_key"] == "private-key"
    assert merged.integrations["alldebrid"].options["agent"] == "custom"


def test_explicit_secret_clear_is_supported_in_the_namespace():
    previous = _settings_from_legacy({"alldebrid_api_key": "private-key"})
    draft = AppSettings(integrations={"alldebrid": IntegrationSettings(clear_secrets=["api_key"])})
    cleared = normalize_settings(draft, definitions, previous=previous)
    assert cleared.integrations["alldebrid"].options["api_key"] == ""


def test_unknown_secret_clear_request_is_rejected():
    import pytest

    draft = AppSettings(integrations={"alldebrid": IntegrationSettings(clear_secrets=["agent"])})
    with pytest.raises(ValueError, match="Unknown integration secret clear request"):
        normalize_settings(draft, definitions)


def test_unknown_plugin_settings_remain_private_and_are_retained():
    settings = AppSettings(integrations={"future-plugin": IntegrationSettings(options={"unexpected_credential": "secret-material"})})
    normalized = normalize_settings(settings, definitions)
    assert normalized.integrations["future-plugin"].options["unexpected_credential"] == "secret-material"
    assert public_integrations(normalized, definitions)["future-plugin"]["options"] == {}


def test_transfer_policy_translates_legacy_settings_and_survives_partial_updates():
    previous = _settings_from_legacy({"aria2_error_retry_count": 0, "upload_fail_retry_delay_minutes": 0})
    assert previous.transfer_policy.execution_retry_count == 0
    assert previous.transfer_policy.resolution_retry_delay_minutes == 0
    draft = AppSettings(transfer_policy=TransferSettings(resolution_concurrency=7))
    changed = normalize_settings(draft, definitions, previous=previous)
    assert changed.transfer_policy.resolution_concurrency == 7
    assert changed.transfer_policy.execution_retry_count == 0
    # A canonical update is the only way to change the policy after load.
    update = changed.model_copy(update={"transfer_policy": TransferSettings(max_concurrent_executions=8)})
    changed = normalize_settings(update, definitions, previous=changed)
    assert changed.transfer_policy.max_concurrent_executions == 8
    assert changed.transfer_policy.execution_retry_count == 0


def test_partial_transfer_policy_update_preserves_unset_fields():
    previous = _settings_from_legacy({"aria2_error_retry_count": 5, "max_concurrent_downloads": 9})
    draft = AppSettings(transfer_policy=TransferSettings(resolution_concurrency=4))
    changed = normalize_settings(draft, definitions, previous=previous)
    assert changed.transfer_policy.resolution_concurrency == 4
    assert changed.transfer_policy.execution_retry_count == 5
    assert changed.transfer_policy.max_concurrent_executions == 9


def test_legacy_out_of_range_policy_is_clamped_at_the_migration_boundary():
    result = _settings_from_legacy({
        "max_concurrent_downloads": 500, "aria2_poll_interval_seconds": 0,
        "aria2_error_retry_count": -1, "poll_interval_seconds": 1,
    })
    assert result.transfer_policy.max_concurrent_executions == 20
    assert result.transfer_policy.execution_poll_interval_seconds == 2
    assert result.transfer_policy.execution_retry_count == 0
    assert result.transfer_policy.provider_poll_interval_seconds == 5
    # Nothing carries the raw legacy values forward.
    assert not {"max_concurrent_downloads", "aria2_poll_interval_seconds", "poll_interval_seconds"} & set(result.model_dump())


def test_canonical_value_wins_over_a_stale_flat_alias_in_the_same_file():
    raw = {
        "aria2_split": 999, "max_concurrent_downloads": 999, "aria2_max_download_limit": 999,
        "integrations": {"aria2": {"options": {"split": 32}}},
        "transfer_policy": {"max_concurrent_executions": 7},
        "execution_runtime_limits": {"max_download_bytes_per_second": 4321},
    }
    result = _settings_from_legacy(raw)
    assert result.integrations["aria2"].options["split"] == 32
    assert result.transfer_policy.max_concurrent_executions == 7
    assert result.execution_runtime_limits.max_download_bytes_per_second == 4321


def test_legacy_value_only_fills_an_option_the_canonical_namespace_does_not_carry():
    raw = {"aria2_disk_cache": "128M", "integrations": {"aria2": {"options": {"split": 32}}}}
    options = _settings_from_legacy(raw).integrations["aria2"].options
    assert options["split"] == 32
    assert options["disk_cache"] == "128M"


def test_migration_consumes_every_legacy_key_and_reports_it():
    raw = {"aria2_split": 8, "max_concurrent_downloads": 4, "aria2_max_download_limit": 10, "paused": True}
    assert migrate_legacy_settings(raw, definitions) is True
    assert set(raw) == {"paused", "integrations", "transfer_policy", "execution_runtime_limits"}
    assert migrate_legacy_settings(raw, definitions) is False


def test_persisted_out_of_range_canonical_values_are_clamped_not_fatal():
    raw = {"integrations": {"aria2": {"options": {"split": 999, "operation_timeout_seconds": 1}}},
           "transfer_policy": {"max_concurrent_executions": 99}}
    corrected = clamp_persisted_namespaces(raw, definitions)
    assert set(corrected) == {"integrations.aria2.split", "integrations.aria2.operation_timeout_seconds",
                              "transfer_policy.max_concurrent_executions"}
    assert raw["integrations"]["aria2"]["options"]["split"] == 64
    assert raw["integrations"]["aria2"]["options"]["operation_timeout_seconds"] == 5
    assert raw["transfer_policy"]["max_concurrent_executions"] == 20


def test_public_integration_metadata_exposes_canonical_provider_names():
    normalized = normalize_settings(AppSettings(), definitions)
    public = public_integrations(normalized, definitions)
    assert public["alldebrid"]["name"] == "AllDebrid"
    assert public["general_http"]["name"] == "HTTP & HTTPS"
    assert public["general_http"]["options"] == {}
