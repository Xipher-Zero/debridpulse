from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
BOOTSTRAP_JS = STATIC / "ui-theme-bootstrap.js"
PRESENTATION_LOADER_JS = STATIC / "ui-presentation-loader.js"
SETTINGS_PAGE_JS = STATIC / "ui-settings-page.js"
SETTINGS_PAGE_CSS = STATIC / "ui-settings-page.css"
STYLE_V11 = STATIC / "style-v11.css"
AUTH_BOOTSTRAP_JS = STATIC / "auth.js"
APP_JS = STATIC / "app.js"
INDEX_HTML = STATIC / "index.html"
TESTS_WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_settings_has_one_post_core_clean_room_runtime():
    bootstrap = source(BOOTSTRAP_JS)
    loader = source(PRESENTATION_LOADER_JS)
    runtime = source(SETTINGS_PAGE_JS)

    assert "ui-settings-page.js" not in bootstrap
    assert "/ui-settings-page.js?v=4" in loader
    assert "data-dp-settings-page" in loader
    assert "clean-room Settings page" in runtime
    assert "window.DPSettingsPage = Object.freeze({load});" in runtime


def test_settings_clean_room_runtime_owns_only_the_navigation_entry_hook():
    runtime = source(SETTINGS_PAGE_JS)

    # app.js still owns generic page navigation, so its single Settings entry
    # hook is replaced. No inherited Settings renderer/serializer/action is used.
    assert "window.loadSettings = load;" in runtime
    assert "loadSettings = load;" in runtime

    for forbidden in (
        "window.renderSettings =",
        "window.getFormSettings =",
        "window.switchSettingsTab =",
        "baseRenderSettings",
        "baseGetFormSettings",
        "baseSaveSettings",
        "previous.apply",
        "legacyRender",
        "renderSettingsWithAuthentication",
        "removeLegacyAuthenticationControls",
        "new MutationObserver",
        "settingsObserver",
        "preservationContainer",
        "dp-settings-preserved",
    ):
        assert forbidden not in runtime


def test_settings_runtime_rejects_legacy_shell_state_but_uses_normal_full_height_content_contract():
    runtime = source(SETTINGS_PAGE_JS)
    assert "document.getElementById('content')?.classList.remove('settings-active');" in runtime
    assert runtime.count("classList.remove('settings-active')") >= 2

    css = source(SETTINGS_PAGE_CSS)
    assert "#content.settings-active" not in css
    assert "#content:has(#view-settings.active)" in css
    assert "overflow-y: hidden;" in css.split("#content:has(#view-settings.active)", 1)[1].split("}", 1)[0]
    assert "#main" not in css
    assert "#sidebar" not in css
    assert "#topbar" not in css


def test_settings_runtime_does_not_consume_legacy_settings_dom_ids_or_functions():
    runtime = source(SETTINGS_PAGE_JS)

    for forbidden in (
        'id="settings-tabs"',
        'id="settings-form"',
        'id="tab-general"',
        'id="tab-download"',
        'id="tab-extract"',
        'id="tab-notifications"',
        'id="tab-authentication"',
        'id="tab-database"',
        'id="tab-advanced"',
        'id="s-',
        "renderSettings(",
        "getFormSettings(",
        "switchSettingsTab(",
        "saveSettings(",
        "testAD(",
        "testAria2(",
        "testDiscord(",
        "initExtractionPasswordList(",
        "loadDatabaseBackupList(",
        "loadAria2Downloads(",
        "loadAria2Runtime(",
    ):
        assert forbidden not in runtime


def test_settings_runtime_directly_uses_backend_api_contracts():
    runtime = source(SETTINGS_PAGE_JS)

    required = (
        "request('GET', '/settings'",
        "request('PUT', '/settings'",
        "request('GET', '/auth/config'",
        "request('PUT', '/auth/config'",
        "request('POST', '/auth/oidc/verify-config'",
        "request('PUT', '/auth/api-token'",
        "request('POST', '/auth/api-token'",
        "request('DELETE', '/auth/api-token'",
        "'/settings/validate-alldebrid'",
        "'/settings/validate-aria2'",
        "'/settings/validate-discord'",
        "request('POST', '/settings/upload-avatar'",
        "request('POST', '/admin/backup'",
        "request('GET', '/admin/backups'",
        "request('POST', '/admin/database/wipe'",
        "request('POST', `/stats/report/send?hours=${hours}`",
    )
    missing = [item for item in required if item not in runtime]
    assert not missing, f"clean Settings runtime is missing backend contracts: {missing}"


def test_settings_authentication_is_clean_implemented_and_secret_safe():
    runtime = source(SETTINGS_PAGE_JS)

    required = (
        "function authPayload()",
        "function persistAuth(",
        "function clearPassword(",
        "function setApiTokenEnabled(",
        "function generateToken(",
        "function clearToken(",
        "function verifyOidc(",
        "function finishOidc(",
        "return_to: '/oidc-verify-complete.html'",
        "confirm_open_mode",
        "clear_password",
        "clear_oidc_client_secret",
        "Copy this token now — it will not be shown again.",
    )
    missing = [item for item in required if item not in runtime]
    assert not missing, f"clean authentication implementation is incomplete: {missing}"

    assert "oneTimeToken" in runtime
    assert "localStorage" not in runtime
    assert "sessionStorage" not in runtime


def test_old_authentication_settings_augmentations_are_not_loaded():
    bootstrap = source(AUTH_BOOTSTRAP_JS)

    assert "/auth-settings.js" not in bootstrap
    assert "/auth-ux.js" not in bootstrap
    assert "/auth-help.js?v=1" in bootstrap
    # auth-ux.css remains only for the authenticated sidebar stack. The clean
    # Settings runtime intentionally uses different ids/classes so those old
    # Settings selectors cannot match it.
    assert "/auth-ux.css?v=1" in bootstrap


def test_settings_tabs_match_the_reviewed_order_and_glyph_inventory():
    runtime = source(SETTINGS_PAGE_JS)
    expected = [
        "['sources', 'Sources & Providers', 'zap']",
        "['downloads', 'Downloads', 'download']",
        "['extraction', 'Extraction', 'package-open']",
        "['notifications', 'Notifications', 'bell']",
        "['authentication', 'Authentication', 'shield-check']",
        "['maintenance', 'Data & Maintenance', 'database-backup']",
    ]
    positions = [runtime.index(item) for item in expected]
    assert positions == sorted(positions)
    assert "['advanced', 'Advanced', 'sliders-horizontal']" not in runtime


def test_sources_panel_uses_source_type_master_group_before_provider_cards():
    runtime = source(SETTINGS_PAGE_JS)
    sources = runtime[runtime.index("function sourcesPanel"):runtime.index("function downloadsPanel")]

    assert "function groupCard(" in runtime
    assert "groupCard('Debrid Services', provider," in sources
    assert "provider + recovery" not in sources
    assert "const recovery =" not in sources
    assert "dp-settings-source-group dp-settings-debrid-services" in sources
    assert "dp-settings-provider-card dp-settings-provider-card--alldebrid" in sources
    assert "dp-settings-provider-recovery-card" not in sources
    assert '<details class="dp-settings-additional">' in sources
    assert "Additional Settings" in sources
    assert "upload_fail_retry_count" in sources
    assert "upload_fail_retry_delay_minutes" in sources
    # Source-type artwork is presentation-owned by CSS; runtime needs no icon class.
    assert "dp-settings-debrid-services-icon" not in sources


def test_settings_groups_keep_the_reviewed_field_inventory():
    runtime = source(SETTINGS_PAGE_JS)

    sources = runtime[runtime.index("function sourcesPanel"):runtime.index("function downloadsPanel")]
    for key in (
        "alldebrid_api_key",
        "alldebrid_rate_limit_per_minute",
        "poll_interval_seconds",
        "full_sync_interval_minutes",
        "upload_fail_retry_count",
        "upload_fail_retry_delay_minutes",
    ):
        assert key in sources

    downloads = runtime[runtime.index("function downloadsPanel"):runtime.index("function extractionPanel")]
    for key in (
        "aria2_mode",
        "aria2_url",
        "aria2_secret",
        "download_folder",
        "aria2_download_path",
        "aria2_max_active_downloads",
        "min_free_disk_gb",
        "disk_guard_resume_hysteresis_gb",
        "stuck_download_timeout_hours",
        "aria2_error_retry_count",
        "aria2_error_retry_delay_seconds",
        "filters_enabled",
        "blocked_extensions",
        "blocked_keywords",
        "min_file_size_mb",
        "block_samples",
        "block_extras",
        "torrent_labels_raw",
        "aria2_split",
        "aria2_min_split_size",
        "aria2_max_connection_per_server",
        "aria2_disk_cache",
        "aria2_file_allocation",
        "aria2_lowest_speed_limit",
        "aria2_continue_downloads",
    ):
        assert key in downloads

    extraction = runtime[runtime.index("function extractionPanel"):runtime.index("function notificationsPanel")]
    for key in ("extract_enabled", "extract_delete_archive", "extract_max_concurrent", "extraction_password"):
        assert key in extraction

    notifications = runtime[runtime.index("function notificationsPanel"):runtime.index("function authStatusCard")]
    for key in (
        "discord_username",
        "discord_avatar_url",
        "discord_webhook_url",
        "discord_webhook_added",
        "discord_notify_added",
        "discord_notify_finished",
        "discord_notify_error",
        "discord_notify_extract",
        "discord_notify_update",
        "update_check_interval_hours",
        "stats_report_webhook_url",
        "stats_report_interval_hours",
        "stats_report_window_hours",
    ):
        assert key in notifications

    maintenance = runtime[runtime.index("function maintenancePanel"):runtime.index("function panel(")]
    for key in (
        "backup_enabled",
        "backup_folder",
        "backup_interval_hours",
        "backup_keep_days",
        "stats_snapshot_interval_minutes",
        "stats_snapshot_keep_days",
        "events_keep_days",
        "db_wipe_enabled",
        "db_backup_before_wipe",
    ):
        assert key in maintenance

    assert "function advancedPanel" not in runtime
    assert "panel('advanced'" not in runtime


def test_non_auth_serializer_starts_from_server_state_and_preserves_hidden_settings():
    runtime = source(SETTINGS_PAGE_JS)
    serializer = runtime[runtime.index("function nonAuthPayload()"):runtime.index("async function persistNonAuth")]

    assert "const current = state.settings || {};" in serializer
    assert "...current," in serializer
    assert "clear_secrets: clearSecrets()," in serializer
    assert "max_concurrent_downloads: maxDownloads" in serializer
    assert "aria2_max_active_downloads: maxDownloads" in serializer


def test_settings_is_one_master_card_with_internal_header_body_and_footer():
    runtime = source(SETTINGS_PAGE_JS)

    assert '<section class="card dp-settings-master-card"' in runtime
    assert '<div class="card-header dp-settings-master-header">' in runtime
    assert '<div class="dp-settings-master-body">' in runtime
    assert '<div class="dp-settings-scroll">' in runtime
    assert '<div class="dp-settings-master-footer"' in runtime
    assert 'class="card dp-settings-card' in runtime
    assert 'class="card dp-settings-group-card' in runtime

    # The header/footer are regions of the master card, never independent cards.
    assert 'class="card dp-settings-header-card"' not in runtime
    assert 'class="card dp-settings-footer"' not in runtime
    assert 'class="card dp-settings-master-footer"' not in runtime
    assert 'class="card dp-settings-panel"' not in runtime
    assert 'class="card dp-settings-scroll"' not in runtime


def test_settings_master_card_fills_shell_datum_and_body_is_the_only_scroll_region():
    css = source(SETTINGS_PAGE_CSS)

    assert "#view-settings.dp-settings-clean-view.active" in css
    active = css.split("#view-settings.dp-settings-clean-view.active", 1)[1].split("}", 1)[0]
    assert "height: 100% !important;" in active
    assert "min-height: 0;" in active
    assert "overflow: visible;" in active

    master = css.split("#view-settings > .dp-settings-master-card", 1)[1].split("}", 1)[0]
    assert "flex: 1 1 auto;" in master
    assert "min-height: 0;" in master
    assert "margin-bottom: 0 !important;" in master

    body = css.split("#view-settings .dp-settings-master-body", 1)[1].split("}", 1)[0]
    assert "flex: 1 1 auto;" in body
    assert "min-height: 0;" in body
    assert "overflow: hidden;" in body

    scroll = css.split("#view-settings .dp-settings-scroll", 1)[1].split("}", 1)[0]
    assert "overflow-y: auto;" in scroll
    assert "overscroll-behavior: contain;" in scroll

    panels = css.split("#view-settings .dp-settings-panels", 1)[1].split("}", 1)[0]
    assert "padding: 12px 12px 16px;" in panels

    footer = css.split("#view-settings .dp-settings-master-footer", 1)[1].split("}", 1)[0]
    assert "flex: 0 0 auto;" in footer
    assert "border-top: 1px solid var(--dp-divider);" in footer
    assert "position: fixed" not in footer
    assert "position: absolute" not in footer

    # Settings owns geometry only. Shared card material must stay universal.
    for forbidden in (
        "radial-gradient",
        "--dp-panel-frame",
        "--dp-panel-surface",
        "--dp-panel-shadow",
        "box-shadow:",
        "backdrop-filter:",
    ):
        assert forbidden not in css
    for forbidden_selector in (
        ".dp-settings-master-card::after",
        ".dp-settings-card::after",
        ".dp-settings-group-card::after",
    ):
        assert forbidden_selector not in css


def test_settings_page_css_is_loaded_as_a_normal_page_contract():
    styles = source(STYLE_V11)
    assert "@import url('/ui-settings-page.css?v=2');" in styles


def test_settings_page_runtime_is_owned_by_frontend_syntax_gate():
    workflow = source(TESTS_WORKFLOW)
    assert "node --check frontend/static/ui-settings-page.js" in workflow


def test_static_settings_dom_is_never_a_runtime_dependency():
    runtime = source(SETTINGS_PAGE_JS)
    index = source(INDEX_HTML)

    # The old placeholder may remain in index.html during monolith cleanup, but
    # the clean runtime replaces #view-settings wholesale and does not query any
    # of its descendants.
    assert 'id="view-settings"' in index
    assert "view.innerHTML =" in runtime
    assert "getElementById('settings-tabs')" not in runtime
    assert "getElementById('settings-form')" not in runtime


def test_legacy_app_settings_implementation_is_dead_from_the_clean_runtime_path():
    app = source(APP_JS)
    runtime = source(SETTINGS_PAGE_JS)

    # app.js may retain dead code until the monolith cleanup pass, but the single
    # navigation entry is replaced before Settings is opened.
    assert "function renderSettings()" in app
    assert "window.loadSettings = load;" in runtime
    assert "renderSettings(" not in runtime
