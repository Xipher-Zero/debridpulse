from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_JS = ROOT / "frontend" / "static" / "ui-theme-bootstrap.js"
PRESENTATION_LOADER_JS = ROOT / "frontend" / "static" / "ui-presentation-loader.js"
SETTINGS_PAGE_JS = ROOT / "frontend" / "static" / "ui-settings-page.js"
SETTINGS_PAGE_CSS = ROOT / "frontend" / "static" / "ui-settings-page.css"
STYLE_V11 = ROOT / "frontend" / "static" / "style-v11.css"
APP_JS = ROOT / "frontend" / "static" / "app.js"
TESTS_WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"


def test_settings_has_one_post_core_authoritative_page_runtime():
    bootstrap = BOOTSTRAP_JS.read_text(encoding="utf-8")
    loader = PRESENTATION_LOADER_JS.read_text(encoding="utf-8")

    assert "ui-settings-page.js" not in bootstrap
    assert "/ui-settings-page.js?v=2" in loader
    assert "data-dp-settings-page" in loader
    assert "ui-settings-architecture.js" not in loader
    assert "ui-settings-presentation.js" not in loader


def test_settings_page_css_is_loaded_as_a_normal_page_contract():
    styles = STYLE_V11.read_text(encoding="utf-8")
    assert "@import url('/ui-settings-page.css?v=1');" in styles


def test_settings_page_runtime_is_owned_by_frontend_syntax_gate():
    workflow = TESTS_WORKFLOW.read_text(encoding="utf-8")
    assert "node --check frontend/static/ui-settings-page.js" in workflow
    assert "ui-settings-architecture.js" not in workflow
    assert "ui-settings-presentation.js" not in workflow


def test_settings_renderer_is_direct_and_does_not_call_or_transform_legacy_dom():
    source = SETTINGS_PAGE_JS.read_text(encoding="utf-8")

    assert "window.renderSettings = render;" in source
    assert "render.dpSettingsPage = '1';" in source
    assert "view.innerHTML = `" in source
    assert "previous.apply" not in source
    assert "baseRenderSettings" not in source
    assert "appendChild(unit" not in source
    assert "preservationContainer" not in source
    assert "dp-settings-preserved" not in source
    assert "new MutationObserver" not in source
    assert "setTimeout(" not in source
    assert "setInterval(" not in source


def test_settings_page_owns_tab_lifecycle_without_legacy_side_effects():
    source = SETTINGS_PAGE_JS.read_text(encoding="utf-8")
    lifecycle = source[
        source.index("function switchSettingsTabOwned"):
        source.index("function activeTabBeforeRender")
    ]

    assert "window.switchSettingsTab = switchSettingsTabOwned;" in source
    assert "switchSettingsTabOwned.dpSettingsPage = '1';" in source
    assert "aria-selected" in lifecycle
    assert "tab.tabIndex = active ? 0 : -1;" in lifecycle
    assert "panel.hidden = !active;" in lifecycle
    assert "data-settings-test-tab" in lifecycle
    assert "initExtractionPasswordList();" in lifecycle

    # A tab transition must never resurrect legacy page work. Runtime status is
    # explicit/operator-driven; aria2 queue polling and backup listing do not
    # belong to tab activation.
    for forbidden in (
        "loadAria2Downloads",
        "loadAria2Runtime",
        "loadDatabaseBackupList",
        "setInterval(",
        "aria2DownloadsTimer",
    ):
        assert forbidden not in lifecycle


def test_auth_refresh_preserves_visible_one_time_api_token_without_persistence():
    source = SETTINGS_PAGE_JS.read_text(encoding="utf-8")
    refresh = source[
        source.index("async function refreshAuthenticationView"):
        source.index("function panelHtml")
    ]

    assert "function visibleOneTimeApiToken()" in source
    assert "function restoreOneTimeApiToken(token)" in source
    assert "const oneTimeToken = visibleOneTimeApiToken();" in refresh
    assert "panel.innerHTML = authenticationCards(authViewData);" in refresh
    assert "restoreOneTimeApiToken(oneTimeToken);" in refresh
    assert refresh.index("const oneTimeToken = visibleOneTimeApiToken();") < refresh.index("panel.innerHTML = authenticationCards(authViewData);")
    assert refresh.index("panel.innerHTML = authenticationCards(authViewData);") < refresh.index("restoreOneTimeApiToken(oneTimeToken);")
    assert "localStorage" not in source
    assert "sessionStorage" not in source


def test_settings_serializer_preserves_non_operator_state_without_hidden_controls():
    source = SETTINGS_PAGE_JS.read_text(encoding="utf-8")

    assert "const data = {\n      ...current," in source
    assert "window.getFormSettings = serialize;" in source
    assert "serialize.dpSettingsPage = '1';" in source

    # These legacy/internal settings are deliberately not rendered as controls.
    # Their existing values survive because the serializer begins with settingsData.
    for hidden_id in (
        'id="s-alldebrid_agent"',
        'id="s-download_client"',
        'id="s-aria2_builtin_auto_start"',
        'id="s-aria2_operation_timeout_seconds"',
        'id="s-aria2_poll_interval_seconds"',
        'id="s-aria2_purge_interval_minutes"',
        'id="s-aria2_max_download_result"',
        'id="s-aria2_waiting_window"',
        'id="s-aria2_stopped_window"',
        'id="s-aria2_max_upload_limit"',
        'id="s-aria2_start_paused"',
        'id="s-db_backup_folder"',
        'id="s-db_backup_enabled"',
        'id="s-db_backup_keep_days"',
    ):
        assert hidden_id not in source


def test_settings_tabs_match_reviewed_order_in_the_authoritative_renderer():
    source = SETTINGS_PAGE_JS.read_text(encoding="utf-8")
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
    tab_block = source[source.index("const TABS"):source.index("let authViewData")]
    positions = [tab_block.index(tab_id) for tab_id in expected_ids]
    assert positions == sorted(positions)


def test_settings_renderer_owns_final_master_card_and_separate_footer_directly():
    source = SETTINGS_PAGE_JS.read_text(encoding="utf-8")

    assert 'class="card dp-settings-master"' in source
    assert 'class="card-header dp-settings-master-header"' in source
    assert 'class="stabs dp-settings-tabs" id="settings-tabs"' in source
    assert 'class="card-body dp-settings-master-body"' in source
    assert 'id="settings-form"' in source
    assert 'class="card save-bar dp-settings-footer"' in source
    assert 'aria-label="Settings actions"' in source


def test_settings_nested_sections_use_shared_card_header_and_body_material():
    source = SETTINGS_PAGE_JS.read_text(encoding="utf-8")

    card_fn = source[source.index("function card("):source.index("function providerStatusText")]
    assert 'class="card dp-settings-section-card"' in card_fn
    assert 'class="card-header"' in card_fn
    assert 'class="card-body"' in card_fn
    assert "scard" not in card_fn
    assert "scard-header" not in source
    assert "scard-body" not in source


def test_settings_page_does_not_redefine_shared_master_header_material():
    css = SETTINGS_PAGE_CSS.read_text(encoding="utf-8")

    selector = ".dp-settings-master > .dp-settings-master-header"
    start = css.index(selector)
    block = css[start:css.index("}", start)]
    assert "background:" not in block
    assert "box-shadow:" not in block
    assert "border-bottom:" not in block
    assert "radial-gradient" not in css


def test_settings_master_tabs_are_centered_on_the_whole_card():
    css = SETTINGS_PAGE_CSS.read_text(encoding="utf-8")

    assert "grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);" in css
    assert ".dp-settings-tabs" in css
    assert "grid-column: 2;" in css
    assert "justify-self: center;" in css
    assert ".dp-settings-master-balance" in css


def test_settings_master_scrolls_above_persistent_footer_on_shared_lower_datum():
    css = SETTINGS_PAGE_CSS.read_text(encoding="utf-8")

    assert "#content.settings-active" in css
    assert "padding-bottom: 24px !important;" in css
    assert "#view-settings.dp-settings-page.active" in css
    assert "flex-direction: column;" in css
    assert ".dp-settings-master-body" in css
    assert "overflow: hidden;" in css
    assert "#settings-form" in css
    assert "overflow-y: auto;" in css
    assert ".dp-settings-footer" in css
    assert "position: static !important;" in css
    assert "flex: 0 0 auto;" in css


def test_sources_and_downloads_are_rendered_in_final_reviewed_ownership_groups():
    source = SETTINGS_PAGE_JS.read_text(encoding="utf-8")

    provider = source[source.index("function sourcesPanel"):source.index("function downloadsPanel")]
    for control_id in (
        "s-alldebrid_api_key",
        "s-alldebrid_rate_limit_per_minute",
        "s-poll_interval_seconds",
        "s-full_sync_interval_minutes",
        "s-upload_fail_retry_count",
        "s-upload_fail_retry_delay_minutes",
    ):
        assert control_id in provider

    downloads = source[source.index("function downloadsPanel"):source.index("function extractionPanel")]
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
        assert control_id in downloads


def test_notifications_maintenance_and_advanced_keep_reviewed_scope():
    source = SETTINGS_PAGE_JS.read_text(encoding="utf-8")

    notifications = source[source.index("function notificationsPanel"):source.index("function dataMaintenancePanel")]
    assert "s-discord_notify_extract" in notifications
    assert "s-stats_report_webhook_url" in notifications
    assert "Send Test Report" in notifications
    assert "loadComprehensiveStats" not in notifications
    assert "exportStats" not in notifications
    assert "triggerStatsSnapshot" not in notifications

    maintenance = source[source.index("function dataMaintenancePanel"):source.index("function advancedPanel")]
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

    advanced = source[source.index("function advancedPanel"):source.index("function authFallbackData")]
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


def test_authentication_is_rendered_as_shared_cards_without_legacy_settings_augmentation():
    source = SETTINGS_PAGE_JS.read_text(encoding="utf-8")

    auth = source[source.index("function authenticationCards"):source.index("function authenticationPanel")]
    for control_id in (
        "auth-password-enabled",
        "auth-username",
        "auth-new-password",
        "auth-oidc-enabled",
        "auth-oidc-provider",
        "auth-oidc-issuer",
        "auth-oidc-client-id",
        "auth-api-token-enabled",
        "auth-public-base-url",
        "auth-session-hours",
    ):
        assert control_id in auth
    assert "card('Authentication Status'" in auth
    assert "card('Username & Password'" in auth
    assert "card('OpenID Connect'" in auth
    assert "card('API Access'" in auth
    assert "card('Sessions & Security'" in auth


def test_legacy_settings_renderer_remains_dead_code_until_cleanup_not_a_render_dependency():
    app = APP_JS.read_text(encoding="utf-8")
    source = SETTINGS_PAGE_JS.read_text(encoding="utf-8")

    assert "function renderSettings()" in app
    assert "Delivery Mode" in app
    assert "Agent Name" in app
    assert "window.renderSettings = render;" in source
    assert "window.switchSettingsTab = switchSettingsTabOwned;" in source
    assert "legacyRender" not in source
    assert "renderSettingsWithAuthentication" not in source