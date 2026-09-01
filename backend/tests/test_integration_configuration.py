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
    assert merged.alldebrid_agent == "custom"


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
