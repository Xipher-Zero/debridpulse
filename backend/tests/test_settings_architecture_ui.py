from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_JS = ROOT / "frontend" / "static" / "ui-theme-bootstrap.js"
SETTINGS_IA_JS = ROOT / "frontend" / "static" / "ui-settings-architecture.js"
APP_JS = ROOT / "frontend" / "static" / "app.js"
TESTS_WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"


def test_settings_information_architecture_runtime_is_loaded_additively():
    bootstrap = BOOTSTRAP_JS.read_text(encoding="utf-8")
    runtime = SETTINGS_IA_JS.read_text(encoding="utf-8")
    app = APP_JS.read_text(encoding="utf-8")

    assert "ui-settings-architecture.js?v=1" in bootstrap
    assert "data-dp-settings-architecture" in bootstrap
    assert "UI only" in runtime
    assert "dp-settings-preserved-controls" in runtime
    # The inherited renderer remains present until the post-UI backend pruning pass.
    assert "Delivery Mode" in app
    assert "Agent Name" in app


def test_settings_runtime_is_owned_by_frontend_syntax_gate():
    workflow = TESTS_WORKFLOW.read_text(encoding="utf-8")
    assert "node --check frontend/static/ui-settings-architecture.js" in workflow


def test_settings_tabs_match_reviewed_ownership_order():
    source = SETTINGS_IA_JS.read_text(encoding="utf-8")
    expected = [
        "Sources & Providers",
        "Downloads",
        "Extraction",
        "Notifications",
        "Authentication",
        "Data & Maintenance",
        "Advanced",
    ]
    positions = [source.index(f"'{label}'") for label in expected]
    assert positions == sorted(positions)

    expected_ids = [
        "'tab-general'",
        "'tab-download'",
        "'tab-extract'",
        "'tab-notifications'",
        "'tab-authentication'",
        "'tab-database'",
        "'tab-advanced'",
    ]
    order_block = source[source.index("const TAB_ORDER"):source.index("let applying")]
    positions = [order_block.index(tab_id) for tab_id in expected_ids]
    assert positions == sorted(positions)


def test_sources_and_downloads_preserve_upstream_downstream_boundary():
    source = SETTINGS_IA_JS.read_text(encoding="utf-8")

    provider_block = source[source.index("function buildSourcesAndProviders"):source.index("function buildDownloads")]
    for control_id in (
        "s-alldebrid_api_key",
        "s-alldebrid_rate_limit_per_minute",
        "s-poll_interval_seconds",
        "s-full_sync_interval_minutes",
        "s-upload_fail_retry_count",
        "s-upload_fail_retry_delay_minutes",
    ):
        assert control_id in provider_block

    download_block = source[source.index("function buildDownloads"):source.index("function buildExtraction")]
    for control_id in (
        "s-aria2_mode",
        "s-aria2_url",
        "s-aria2_secret",
        "s-download_folder",
        "s-aria2_download_path",
        "s-aria2_max_active_downloads",
        "s-min_free_disk_gb",
        "s-stuck_download_timeout_hours",
        "s-aria2_error_retry_count",
        "s-filters_enabled",
        "s-torrent_labels_raw",
    ):
        assert control_id in download_block


def test_settings_pruning_hides_controls_without_deleting_serializer_inputs():
    source = SETTINGS_IA_JS.read_text(encoding="utf-8")
    app = APP_JS.read_text(encoding="utf-8")

    preservation_block = source[source.index("function preserveInternalAndOperationalControls"):source.index("function syncProviderStatus")]
    hidden_controls = (
        "s-alldebrid_agent",
        "s-download_client",
        "s-aria2_builtin_auto_start",
        "s-aria2_builtin_port",
        "s-disk_guard_interval_seconds",
        "s-aria2_operation_timeout_seconds",
        "s-aria2_poll_interval_seconds",
        "s-aria2_purge_interval_minutes",
        "s-aria2_max_download_result",
        "s-aria2_waiting_window",
        "s-aria2_stopped_window",
        "s-aria2_max_upload_limit",
        "s-aria2_start_paused",
        "s-db_backup_folder",
        "s-db_backup_enabled",
        "s-db_backup_keep_days",
    )
    for control_id in hidden_controls:
        assert control_id in preservation_block
        assert control_id in app


def test_notifications_and_maintenance_move_non_configuration_browsing_out_of_normal_settings_ui():
    source = SETTINGS_IA_JS.read_text(encoding="utf-8")

    notifications = source[source.index("function buildNotifications"):source.index("function buildDataMaintenance")]
    assert "s-discord_notify_extract" in notifications
    assert "s-stats_report_webhook_url" in notifications
    assert "Send Test Report" in notifications
    assert "loadComprehensiveStats" not in notifications
    assert "exportStats" not in notifications
    assert "triggerStatsSnapshot" not in notifications

    maintenance = source[source.index("function buildDataMaintenance"):source.index("function buildAdvanced")]
    for control_id in (
        "s-backup_enabled",
        "s-backup_folder",
        "s-backup_interval_hours",
        "s-backup_keep_days",
        "s-stats_snapshot_interval_minutes",
        "s-stats_snapshot_keep_days",
        "s-events_keep_days",
        "s-db_wipe_enabled",
        "s-db_backup_before_wipe",
    ):
        assert control_id in maintenance


def test_global_advanced_is_limited_to_transfer_engine_tuning_candidates():
    source = SETTINGS_IA_JS.read_text(encoding="utf-8")
    advanced = source[source.index("function buildAdvanced"):source.index("function preserveInternalAndOperationalControls")]
    for control_id in (
        "s-aria2_split",
        "s-aria2_min_split_size",
        "s-aria2_max_connection_per_server",
        "s-aria2_disk_cache",
        "s-aria2_file_allocation",
        "s-aria2_lowest_speed_limit",
        "s-aria2_continue_downloads",
    ):
        assert control_id in advanced

    for provider_control in (
        "s-alldebrid_rate_limit_per_minute",
        "s-poll_interval_seconds",
        "s-full_sync_interval_minutes",
        "s-upload_fail_retry_count",
    ):
        assert provider_control not in advanced


def test_provider_status_summary_is_idempotent_across_architecture_reapply():
    source = SETTINGS_IA_JS.read_text(encoding="utf-8")
    provider = source[source.index("function buildSourcesAndProviders"):source.index("function buildDownloads")]

    assert "let status = connection?.querySelector('.dp-settings-status');" in provider
    assert "if (!status && connection)" in provider
    assert "status = document.createElement('div');" in provider
    assert "const keyState = status?.querySelector('#dp-settings-ad-key-state');" in provider


def test_settings_architecture_observer_cannot_retrigger_from_its_own_dom_moves():
    source = SETTINGS_IA_JS.read_text(encoding="utf-8")
    apply_block = source[source.index("function applyArchitecture"):source.index("function scheduleApply")]
    observer_block = source[source.index("function installObserver"):source.index("function boot")]

    disconnect = apply_block.index("if (settingsObserver) settingsObserver.disconnect();")
    mutate = apply_block.index("normalizeTabs();")
    reconnect = apply_block.index("observeSettingsForm();")
    assert disconnect < mutate < reconnect
    assert "function observeSettingsForm()" in source
    assert "observeSettingsForm();" in observer_block
