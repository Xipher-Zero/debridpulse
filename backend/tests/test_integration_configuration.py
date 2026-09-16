from core.config import AppSettings
from integrations.catalog import definitions
from integrations.configuration import normalize_settings, public_integrations
from integrations.definition import IntegrationSettings


def test_flat_settings_migrate_without_losing_native_credentials_or_remote_paths():
    before = AppSettings(alldebrid_api_key="private-key", aria2_mode="external", aria2_secret="rpc-private", aria2_download_path="/remote")
    migrated = normalize_settings(before, definitions)
    assert migrated.integrations["alldebrid"].options["api_key"] == "private-key"
    assert migrated.integrations["aria2"].options["download_path"] == "/remote"
    assert migrated.aria2_secret == "rpc-private"
    assert normalize_settings(migrated, definitions) == migrated
    public = public_integrations(migrated, definitions)
    assert "private-key" not in str(public)
    assert "rpc-private" not in str(public)
    assert public["alldebrid"]["options"]["api_key_configured"] is True


def test_partial_namespace_update_preserves_secret_and_disabled_state():
    previous = normalize_settings(AppSettings(alldebrid_api_key="private-key"), definitions)
    previous.integrations["alldebrid"].enabled = False
    draft = AppSettings(integrations={"alldebrid": IntegrationSettings(options={"api_key": "", "agent": "custom"})})
    merged = normalize_settings(draft, definitions, previous=previous, supplied_fields={"integrations"})
    assert merged.integrations["alldebrid"].enabled is False
    assert merged.integrations["alldebrid"].options["api_key"] == "private-key"
    assert merged.integrations["alldebrid"].options["agent"] == "custom"
    # Legacy flat field is migration input only -- not regenerated as a
    # persisted mirror of the canonical namespace value (specification
    # section 9.2).
    assert merged.alldebrid_agent == "DebridPulse"


def test_explicit_secret_clear_is_supported_in_namespace_and_legacy_input():
    previous = normalize_settings(AppSettings(alldebrid_api_key="private-key"), definitions)
    draft = AppSettings(integrations={"alldebrid": IntegrationSettings(clear_secrets=["api_key"])})
    cleared = normalize_settings(draft, definitions, previous=previous, supplied_fields={"integrations"})
    assert cleared.alldebrid_api_key == ""
    assert cleared.integrations["alldebrid"].options["api_key"] == ""
    legacy = normalize_settings(AppSettings(), definitions, previous=previous, supplied_fields={"alldebrid_api_key"}, clear_legacy_secrets={"alldebrid_api_key"})
    assert legacy.integrations["alldebrid"].options["api_key"] == ""


def test_unknown_plugin_settings_remain_private_and_are_retained():
    settings = AppSettings(integrations={"future-plugin": IntegrationSettings(options={"unexpected_credential": "secret-material"})})
    normalized = normalize_settings(settings, definitions)
    assert normalized.integrations["future-plugin"].options["unexpected_credential"] == "secret-material"
    assert public_integrations(normalized, definitions)["future-plugin"]["options"] == {}


def test_transfer_policy_translates_legacy_settings_and_survives_partial_updates():
    from transfers.settings import TransferSettings
    previous = normalize_settings(AppSettings(aria2_error_retry_count=0, upload_fail_retry_delay_minutes=0), definitions)
    assert previous.transfer_policy.execution_retry_count == 0
    assert previous.transfer_policy.resolution_retry_delay_minutes == 0
    draft = AppSettings(transfer_policy=TransferSettings(resolution_concurrency=7))
    changed = normalize_settings(draft, definitions, previous=previous, supplied_fields={"transfer_policy"})
    assert changed.transfer_policy.resolution_concurrency == 7
    assert changed.transfer_policy.execution_retry_count == 0
    # Legacy flat fields are migration/compatibility input only -- a partial
    # canonical update must NOT regenerate them as an authoritative persisted
    # mirror (specification section 9.2); the flat field simply keeps
    # whatever it already held (here, the AppSettings default).
    assert changed.aria2_error_retry_count == 3
    legacy = changed.model_copy(update={"max_concurrent_downloads": 8})
    changed = normalize_settings(legacy, definitions, previous=changed, supplied_fields={"max_concurrent_downloads"})
    assert changed.transfer_policy.max_concurrent_executions == 8
    # aria2_max_active_downloads is never a second writable authority
    # (specification sections 9.2, 9.7).
    assert changed.aria2_max_active_downloads == 3


def test_legacy_out_of_range_policy_clamps_canonical_values_without_mirroring_back():
    result = normalize_settings(AppSettings(max_concurrent_downloads=500, aria2_poll_interval_seconds=0,
        aria2_error_retry_count=-1, poll_interval_seconds=1), definitions)
    assert result.transfer_policy.max_concurrent_executions == 20
    assert result.transfer_policy.execution_poll_interval_seconds == 2
    assert result.transfer_policy.execution_retry_count == 0
    assert result.transfer_policy.provider_poll_interval_seconds == 5
    # Legacy flat fields are migration input only -- the canonical clamp is
    # never regenerated back onto them (specification section 9.2); each
    # retains the raw value that was supplied as input.
    assert result.max_concurrent_downloads == 500
    assert result.aria2_poll_interval_seconds == 0
    assert result.aria2_error_retry_count == -1
    assert result.poll_interval_seconds == 1

def test_public_integration_metadata_exposes_canonical_provider_names():
    normalized = normalize_settings(AppSettings(), definitions)
    public = public_integrations(normalized, definitions)
    assert public["alldebrid"]["name"] == "AllDebrid"
    assert public["general_http"]["name"] == "HTTP & HTTPS"
    assert public["general_http"]["options"] == {}
