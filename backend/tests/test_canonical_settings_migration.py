"""Real load -> migrate -> save -> reload round trip for pre-canonical configuration.

``integrations.<id>``, ``transfer_policy`` and ``execution_runtime_limits`` are
the only persisted and runtime authorities. A configuration written before that
model carries flat ``aria2_*`` / policy / bandwidth / AllDebrid keys; loading it
is the single translation boundary. These tests exercise the real files, the
real loader and the real saver -- not an in-memory transformation.
"""
import json

import pytest

import core.config as config
from api.legacy_settings_view import legacy_settings_projection
from integrations.catalog import definitions
from test_executor_configuration_ownership import CURRENT_ARIA2_OPTIONS
from transfers.runtime_limits import LEGACY_INPUT_FIELDS as RUNTIME_LEGACY
from transfers.settings import LEGACY_INPUT_FIELDS as POLICY_LEGACY

# A realistic v1.0.11.1-era config: flat aliases plus unrelated settings.
LEGACY_CONFIG = {
    "alldebrid_api_key": "ad-private-key-12345",
    "alldebrid_agent": "ACDC",
    "alldebrid_rate_limit_per_minute": 120,
    "download_folder": "/mnt/downloads",
    "download_client": "aria2",
    "aria2_mode": "external",
    "aria2_url": "http://aria2.lan:6800/jsonrpc",
    "aria2_secret": "rpc-private-secret",
    "aria2_download_path": "/remote/downloads",
    "aria2_split": 32,
    "aria2_min_split_size": "20M",
    "aria2_max_connection_per_server": 8,
    "aria2_continue_downloads": False,
    "aria2_disk_cache": "128M",
    "aria2_file_allocation": "none",
    "aria2_lowest_speed_limit": "10K",
    "aria2_max_upload_limit": 500000,
    "aria2_builtin_port": 6810,
    "aria2_max_download_result": 200,
    "aria2_restart_interval_hours": 12,
    "max_concurrent_downloads": 7,
    "aria2_max_active_downloads": 7,
    "aria2_error_retry_count": 4,
    "aria2_error_retry_delay_seconds": 90,
    "upload_fail_retry_count": 2,
    "upload_fail_retry_delay_minutes": 9,
    "aria2_poll_interval_seconds": 5,
    "poll_interval_seconds": 45,
    "stuck_download_timeout_hours": 12,
    "aria2_max_download_limit": 2_500_000,
    "discord_username": "My notifier",
    "extract_enabled": True,
    "paused": False,
}
ALIASES = (
    set(POLICY_LEGACY) | set(RUNTIME_LEGACY) | {"aria2_max_active_downloads", "download_client"}
    | {legacy for definition in definitions for legacy, _option in definition.legacy_fields}
)
CANONICAL = ("integrations", "transfer_policy", "execution_runtime_limits")


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.setattr(config, "_settings", config.AppSettings())
    return path


def _canonical_state(settings):
    dumped = settings.model_dump()
    return {key: dumped[key] for key in CANONICAL}


def test_legacy_config_round_trips_through_load_save_and_reload(config_path):
    config_path.write_text(json.dumps(LEGACY_CONFIG))

    migrated = config.load_settings()

    aria2 = migrated.integrations["aria2"].options
    # Keys naming no current option are inert: the canonical namespace carries
    # exactly the current schema.
    assert set(aria2) == CURRENT_ARIA2_OPTIONS
    # A legacy value left at an older default is raised to the current default.
    assert (aria2["split"], aria2["min_split_size"], aria2["max_connection_per_server"]) == (32, "20M", 16)
    assert aria2["continue_downloads"] is False and aria2["file_allocation"] == "none"
    assert aria2["disk_cache"] == "128M" and aria2["lowest_speed_limit"] == "10K"
    assert aria2["max_upload_limit"] == 500000
    assert aria2["max_download_result"] == 200 and aria2["restart_interval_hours"] == 12
    alldebrid = migrated.integrations["alldebrid"].options
    assert alldebrid["api_key"] == "ad-private-key-12345"
    assert alldebrid["agent"] == "DebridPulse"                          # legacy identity migrated
    assert alldebrid["rate_limit_per_minute"] == 120
    policy = migrated.transfer_policy
    assert (policy.max_concurrent_executions, policy.execution_retry_count,
            policy.execution_retry_delay_seconds) == (7, 4, 90)
    assert (policy.resolution_retry_count, policy.resolution_retry_delay_minutes) == (2, 9)
    assert (policy.execution_poll_interval_seconds, policy.provider_poll_interval_seconds) == (5, 45)
    assert policy.stalled_timeout_hours == 12
    assert migrated.execution_runtime_limits.max_download_bytes_per_second == 2_500_000  # bandwidth preserved
    assert migrated.download_folder == "/mnt/downloads" and migrated.discord_username == "My notifier"

    # The loader rewrote the file in its canonical shape: no superseded alias.
    persisted = json.loads(config_path.read_text())
    assert not ALIASES & set(persisted)
    assert "rpc-private-secret" not in config_path.read_text()

    # Explicit save, then reload: canonical values unchanged, aliases not regenerated.
    before = _canonical_state(migrated)
    config.save_settings(migrated)
    assert not ALIASES & set(json.loads(config_path.read_text()))
    reloaded = config.load_settings()
    assert _canonical_state(reloaded) == before
    assert reloaded.model_dump() == migrated.model_dump()


def test_a_second_load_of_a_canonical_file_is_a_pure_read(config_path):
    config_path.write_text(json.dumps(LEGACY_CONFIG))
    config.load_settings()
    canonical_file = config_path.read_text()
    config.load_settings()
    assert config_path.read_text() == canonical_file


def test_a_stale_flat_alias_beside_canonical_state_never_wins_and_is_dropped(config_path):
    config.save_settings(config.load_settings())        # a fresh canonical file
    document = json.loads(config_path.read_text())
    document["integrations"]["aria2"]["options"]["split"] = 24
    document["transfer_policy"]["max_concurrent_executions"] = 6
    document["execution_runtime_limits"]["max_download_bytes_per_second"] = 1234
    document.update(aria2_split=99, max_concurrent_downloads=99, aria2_max_download_limit=99)   # stale mirrors
    config_path.write_text(json.dumps(document))

    loaded = config.load_settings()

    assert loaded.integrations["aria2"].options["split"] == 24
    assert loaded.transfer_policy.max_concurrent_executions == 6
    assert loaded.execution_runtime_limits.max_download_bytes_per_second == 1234
    assert not ALIASES & set(json.loads(config_path.read_text()))


def test_out_of_range_persisted_values_are_clamped_and_never_block_startup(config_path):
    config_path.write_text(json.dumps({
        "max_concurrent_downloads": 999, "aria2_split": 500, "aria2_operation_timeout_seconds": 1,
    }))
    loaded = config.load_settings()
    assert loaded.transfer_policy.max_concurrent_executions == 20
    assert loaded.integrations["aria2"].options["split"] == 64
    assert loaded.integrations["aria2"].options["operation_timeout_seconds"] == 5


def test_a_fresh_installation_starts_with_complete_canonical_defaults(config_path):
    assert not config_path.exists()
    fresh = config.load_settings()
    assert set(fresh.integrations) >= {"aria2", "alldebrid", "general_http"}
    assert set(fresh.integrations["aria2"].options) == CURRENT_ARIA2_OPTIONS
    assert fresh.transfer_policy.max_concurrent_executions == 3
    assert fresh.execution_runtime_limits.max_download_bytes_per_second == 0


def test_an_unreadable_existing_config_still_fails_closed(config_path):
    config_path.write_text("{not json")
    with pytest.raises(RuntimeError, match="could not be read safely"):
        config.load_settings()


def test_flat_names_are_read_only_compatibility_output_never_authority(config_path):
    """The read-only ``GET /settings`` projection derives the historical names
    from canonical state; it is not persisted and no field can carry it back."""
    config_path.write_text(json.dumps(LEGACY_CONFIG))
    settings = config.load_settings()

    view = legacy_settings_projection(settings, definitions)
    assert view["aria2_split"] == 32
    assert view["max_concurrent_downloads"] == view["aria2_max_active_downloads"] == 7
    assert view["aria2_max_download_limit"] == 2_500_000
    assert view["alldebrid_api_key"] == "" and view["alldebrid_api_key_configured"] is True
    assert "rpc-private-secret" not in json.dumps(view)

    # Authority check: none of those names is a settings field, so the view can
    # neither be persisted nor consumed as state.
    assert not ALIASES & set(config.AppSettings.model_fields)
    assert not ALIASES & set(settings.model_dump())
    config.save_settings(settings)
    assert not ALIASES & set(json.loads(config_path.read_text()))
