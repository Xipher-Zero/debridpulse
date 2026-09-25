import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
BOOTSTRAP_JS = STATIC / "ui-theme-bootstrap.js"
PRESENTATION_LOADER_JS = STATIC / "ui-presentation-loader.js"
SETTINGS_PAGE_JS = STATIC / "ui-settings-page.js"
SETTINGS_PAGE_CSS = STATIC / "ui-settings-page.css"
STYLE_V11 = STATIC / "style.css"
AUTH_BOOTSTRAP_JS = STATIC / "auth.js"
APP_JS = STATIC / "app.js"
INDEX_HTML = STATIC / "index.html"
TESTS_WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
MODAL_JS = STATIC / "ui-settings-modal.js"
DIRECTORY_PICKER_JS = STATIC / "ui-settings-directory-picker.js"
DOWNLOADS_JS = STATIC / "ui-downloads.js"
AUTH_REQUIRED_JS = STATIC / "ui-auth-required.js"
MODAL_CSS = STATIC / "ui-modal-contract.css"
DIRECTORY_BROWSER_CSS = STATIC / "ui-settings-directory-browser.css"
PROVIDER_STATUS_JS = STATIC / "ui-provider-status.js"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")




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
        "'/settings/test-aria2'",
        "'/settings/validate-discord'",
        "request('POST', '/settings/upload-avatar'",
        "request('POST', '/admin/backup'",
        "request('GET', '/admin/backups'",
        "request('POST', '/admin/database/wipe'",
        "request('POST', '/settings/send-stats-report'",
    )
    missing = [item for item in required if item not in runtime]
    assert not missing, f"clean Settings runtime is missing backend contracts: {missing}"




def test_old_authentication_settings_augmentations_are_not_loaded():
    bootstrap = source(AUTH_BOOTSTRAP_JS)

    assert "/auth-settings.js" not in bootstrap
    assert "/auth-ux.js" not in bootstrap
    assert "auth-help" not in bootstrap
    # auth-ux.css remains only for the authenticated sidebar stack. The clean
    # Settings runtime intentionally uses different ids/classes so those old
    # Settings selectors cannot match it.
    assert "auth-ux" not in bootstrap


def test_settings_tabs_match_the_reviewed_order_and_glyph_inventory():
    runtime = source(SETTINGS_PAGE_JS)
    expected = [
        "['sources', 'Services', 'zap']",
        "['downloads', 'Downloads', 'download']",
        "['extraction', 'Extraction', 'package-open']",
        "['authentication', 'Authentication', 'shield-check']",
        "['notifications', 'Notifications', 'bell']",
        "['maintenance', 'Data & Maintenance', 'database-backup']",
    ]
    positions = [runtime.index(item) for item in expected]
    assert positions == sorted(positions)
    assert "['advanced', 'Advanced', 'sliders-horizontal']" not in runtime


def test_sources_panel_uses_source_type_master_group_before_provider_cards():
    runtime = source(SETTINGS_PAGE_JS)
    sources = runtime[runtime.index("function sourcesPanel"):runtime.index("function downloadsPanel")]

    assert "function groupCard(" in runtime
    assert "groupCard('Premium Services'," in sources
    assert "usenetCard + PREMIUM_SEPARATOR + provider," in sources
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
    # The API-key control is emitted by allDebridApiKeyField(), which owns the
    # ``alldebrid_api_key`` control name.
    assert "allDebridApiKeyField(" in sources
    assert "const key = 'alldebrid_api_key';" in runtime
    for key in (
        "alldebrid_rate_limit_per_minute",
        "poll_interval_seconds",
        "full_sync_interval_minutes",
        "upload_fail_retry_count",
        "upload_fail_retry_delay_minutes",
    ):
        assert key in sources

    # The Downloads section is assembled from the Executor Tuning helpers plus
    # downloadsPanel, so the inventory slice starts at the first of them.
    downloads = runtime[runtime.index("function executorTuningCard"):runtime.index("function extractionPanel")]
    for key in (
        "download_folder",
        "aria2_max_active_downloads",
        "min_free_disk_gb",
        "disk_guard_resume_hysteresis_gb",
        "stuck_download_timeout_hours",
        "aria2_error_retry_count",
        "aria2_error_retry_delay_seconds",
        "aria2_split",
        "aria2_min_split_size",
        "aria2_max_connection_per_server",
        "aria2_disk_cache",
        "aria2_file_allocation",
        "aria2_lowest_speed_limit",
        "aria2_continue_downloads",
    ):
        assert key in downloads

    for retired in (
        "filters_enabled",
        "blocked_extensions",
        "blocked_keywords",
        "min_file_size_mb",
        "block_samples",
        "block_extras",
        "torrent_labels_raw",
    ):
        assert retired not in downloads

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


def test_settings_page_holds_no_hand_maintained_legacy_alias_list():
    """DP 1.0.12 final audit, Workstream C: the page used to strip every flat
    alias of a canonicalized field from the whole-settings snapshot using lists
    that had to be kept in sync with the backend by hand. Canonical namespaces
    are now simply never part of that write, so no alias list exists to drift."""
    runtime = source(SETTINGS_PAGE_JS)
    for retired in (
        "ARIA2_CANONICAL_LEGACY_FIELDS", "TRANSFER_POLICY_CANONICAL_LEGACY_FIELDS",
        "RUNTIME_LIMIT_CANONICAL_LEGACY_FIELDS", "function integrationPayload(",
    ):
        assert retired not in runtime, f"{retired!r} is a retired alias-synchronization mechanism"


def test_non_auth_serializer_never_writes_a_canonical_namespace_or_flat_alias():
    runtime = source(SETTINGS_PAGE_JS)
    serializer = runtime[runtime.index("function nonAuthPayload()"):runtime.index("// Each scoped surface answers")]
    assert "...current" in serializer
    # The writable whole-settings document has ONE owner, shared by this
    # serializer and by the single-field settings-document commit.
    assert "settingsDocument(state.settings)" in serializer
    document = runtime[runtime.index("function settingsDocument("):]
    document = document[:document.index("\n  }") + 4]
    for namespace in ("integrations", "transfer_policy", "execution_runtime_limits"):
        assert f"delete document.{namespace};" in document
    # The read-only compatibility names are dropped using the list the server
    # supplies, never a list kept in the page.
    assert "for (const name of document.compatibility_fields || []) delete document[name];" in document
    # Integration-owned secret clears travel with their own scoped request.
    assert "clearSecrets().filter(control => !INTEGRATION_SECRET_CONTROLS[control])" in serializer
    # None of the canonicalized fields is re-added as an override of the
    # whole-settings payload under either its canonical or its flat name.
    assignment_region = serializer[serializer.index("return {"):]
    for forbidden in (
        "max_concurrent_downloads:", "aria2_max_active_downloads:", "aria2_split:", "transfer_policy:",
        "aria2_max_download_limit:", "execution_runtime_limits:", "alldebrid_api_key:",
        "alldebrid_rate_limit_per_minute:", "poll_interval_seconds:", "upload_fail_retry_count:",
        "upload_fail_retry_delay_minutes:", "stuck_download_timeout_hours:", "integrations:",
    ):
        assert forbidden not in assignment_region, f"{forbidden!r} must not be re-added to the whole-settings payload"


def test_settings_page_reads_only_canonical_namespaces():
    runtime = source(SETTINGS_PAGE_JS)
    for accessor in (
        "const aria2Of = s => s?.integrations?.aria2?.options || {};",
        "const allDebridOf = s => s?.integrations?.alldebrid?.options || {};",
        "const policyOf = s => s?.transfer_policy || {};",
    ):
        assert accessor in runtime
    panels = runtime[runtime.index("function sourcesPanel"):runtime.index("function extractionPanel")]
    for flat_read in (
        "s.aria2_", "s.alldebrid_", "s.max_concurrent_downloads", "s.poll_interval_seconds",
        "s.upload_fail_retry", "s.stuck_download_timeout_hours", "s.aria2_max_download_limit",
    ):
        assert flat_read not in panels, f"panel still reads flat alias {flat_read!r}"
    assert "policy.max_concurrent_executions" in panels
    assert "policy.stalled_timeout_hours" in panels
    assert "policyOf(s).provider_poll_interval_seconds" in panels


def test_aria2_configuration_and_transfer_policy_use_scoped_patch_surfaces():
    """Specification section 9.5: provider/executor configuration and universal
    concurrency/retry/poll/stall policy are written through their own scoped
    namespace mutations, never through the whole-settings snapshot."""
    runtime = source(SETTINGS_PAGE_JS)
    assert "function aria2ConfigurationPayload()" in runtime
    assert "function transferPolicyPayload()" in runtime
    persist = runtime[runtime.index("async function persistNonAuth"):runtime.index("async function persistAuth")]
    assert "request('PATCH', '/integrations/aria2/configuration', aria2ConfigurationPayload()" in persist
    assert "request('PATCH', '/transfer-policy', transferPolicyPayload()" in persist
    policy = runtime[runtime.index("function transferPolicyPayload()"):runtime.index("function nonAuthPayload()")]
    for canonical in (
        "max_concurrent_executions", "execution_retry_count", "execution_retry_delay_seconds",
        "stalled_timeout_hours",
    ):
        assert f"{canonical}:" in policy
    # The Services policy fields are locally owned changed-blur
    # controls; they are written through the SAME scoped surface, one field at
    # a time, and are deliberately absent from the deferred payload so it can
    # never replay them.
    blur = runtime[runtime.index("const CHANGED_BLUR_FIELDS"):]
    blur = blur[:blur.index("});") + 3]
    for canonical in ("provider_poll_interval_seconds", "resolution_retry_count",
                      "resolution_retry_delay_minutes"):
        assert canonical not in policy, canonical
        assert canonical in blur, canonical
    assert "'/transfer-policy'" in runtime[runtime.index("function registerCommitScopes("):]


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
    assert "@import url('/ui-settings-page.css?v=3');" in styles


def test_settings_page_runtime_is_owned_by_dynamic_frontend_syntax_gate():
    workflow = source(TESTS_WORKFLOW)
    assert "find frontend/static -maxdepth 1 -name '*.js' -print0" in workflow
    assert "xargs -0 -n1 node --check" in workflow
    assert SETTINGS_PAGE_JS.exists()

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



# ---------------------------------------------------------------------------
# Canonical modal ownership (Canonical Release Remediation, Workstream A).
#
# One neutral application-dialog owner (ui-settings-modal.js) owns the shell,
# focus trap, Escape, focus restoration, settlement and body scroll lock.
# Confirmation and directory browsing are direct clients. These tests prove the
# ABSENCE of every competitor (old owner, post-render mutation, wrappers,
# observers, fallbacks, duplicate traps) as well as the presence of the owner.
# ---------------------------------------------------------------------------

MODAL_CONSUMERS = (SETTINGS_PAGE_JS, DIRECTORY_PICKER_JS, DOWNLOADS_JS)
# Shell material is shared, by CSS class only, with the separately-owned
# authentication-required dialog (its own singleton lifecycle; not a DPSettingsModal client).
SHELL_CLASS_JS_ALLOWLIST = {MODAL_JS.name, AUTH_REQUIRED_JS.name}


def _static_js() -> list[Path]:
    return sorted(STATIC.glob("*.js"))


def test_modal_shell_has_exactly_one_owner_and_one_global_assignment():
    assert MODAL_JS.exists(), "the canonical modal owner must exist"
    assignments = []
    for path in _static_js():
        text = source(path)
        for pattern in (
            r"window\s*\.\s*DPSettingsModal\s*=(?!=)",
            r"window\s*\[\s*['\"]DPSettingsModal['\"]\s*\]\s*=(?!=)",
            r"(?<![\w.])DPSettingsModal\s*=(?!=)",
            r"defineProperty\(\s*window\s*,\s*['\"]DPSettingsModal['\"]",
            r"Object\.assign\(\s*window\.DPSettingsModal",
        ):
            assignments.extend((path.name, pattern) for _ in re.finditer(pattern, text))
    assert [name for name, _ in assignments] == [MODAL_JS.name], assignments

    owner = source(MODAL_JS)
    exported = re.search(r"window\.DPSettingsModal = Object\.freeze\(\{([^}]*)\}\);", owner)
    assert exported, "the canonical global must be one frozen API"
    # DP 1.0.13 work item B: the ONE owner exposes the three dialog shapes the
    # application needs -- the generic shell, a confirmation, and a single
    # text field. A browser-native prompt() is not an option, and a second
    # implementation would be a second owner.
    assert {name.strip() for name in exported.group(1).split(",") if name.strip()} == {"open", "confirm", "prompt"}
    # No other read/rebind of the global inside the owner (no self-wrapping, no late replacement).
    assert owner.count("DPSettingsModal") == 1


def test_old_confirm_owner_is_physically_removed():
    for path in _static_js():
        assert "confirmAction" not in source(path), path.name
    settings = source(SETTINGS_PAGE_JS)
    assert "createElement('div')" not in settings.split("function syncGlobalSettings")[0]
    assert "overlay" not in settings.lower().replace("overlaid", "")
    assert "dp-settings-confirm" not in "".join(source(p) for p in [*_static_js(), *STATIC.glob("*.css"), INDEX_HTML])


def test_modal_shell_markup_and_traps_live_only_in_the_canonical_owner():
    shell_tokens = ("dp-modal-overlay", "dp-modal-dialog", "dp-modal-header", "dp-modal-footer",
                    'role="alertdialog"', "aria-modal", "data-modal-cancel", "data-modal-accept")
    for path in MODAL_CONSUMERS:
        text = source(path)
        for token in shell_tokens:
            assert token not in text, f"{path.name} must not carry shell markup: {token}"
        assert "'Tab'" not in text and '"Tab"' not in text, f"{path.name} must not own a focus trap"
    for path in (SETTINGS_PAGE_JS, DIRECTORY_PICKER_JS):
        assert "'Escape'" not in source(path), f"{path.name} must not own modal Escape handling"
    for path in _static_js():
        if path.name in SHELL_CLASS_JS_ALLOWLIST:
            continue
        assert "dp-modal-overlay" not in source(path), path.name

    owner = source(MODAL_JS)
    assert owner.count("'Escape'") == 1 and owner.count("'Tab'") == 1, "one Escape owner and one focus-trap owner"
    assert owner.count("addEventListener('keydown'") == 1 + owner.count("typedInput.addEventListener('keydown'")


def test_directory_picker_is_a_first_class_client_not_a_post_render_mutator():
    picker = source(DIRECTORY_PICKER_JS)
    assert "DPSettingsModal.open(" in picker
    assert ".confirm(" not in picker
    for forbidden in (
        "overlay", "dp-modal", "data-modal", "dp-settings-confirm", "alertdialog",
        "setAttribute('role'", ".removeAttribute(", "dataset.directoryCancel", "dataset.directoryConfirm",
        "classList.add('dp-settings-directory-dialog')", "keydown",
    ):
        assert forbidden not in picker, forbidden
    # The body slot is only ever the parameter the owner hands to this client's own mount callback: it is
    # filled once, at creation, and never obtained by querying (and rewriting) a rendered dialog.
    mount = picker.index("mount(body)")
    assert picker.count("body.innerHTML") == 1
    assert mount < picker.index("body.innerHTML") < picker.index("view.up.addEventListener('click'")
    # The picker asks the shell for state; it never reaches into the shell DOM.
    assert not re.search(r"document\.querySelector(All)?\(\s*['\"][^'\"]*(dialog|modal|overlay|confirm)", picker)


def test_modal_owner_and_consumers_have_no_repair_patch_wrapper_or_timer_patterns():
    owner = source(MODAL_JS)
    for path in (MODAL_JS, *MODAL_CONSUMERS):
        text = source(path)
        assert "MutationObserver" not in text, path.name
        assert not re.search(r"\.prototype\.\w+\s*=(?!=)", text), path.name
        for forbidden in ("Object.setPrototypeOf", "__proto__", "Object.defineProperty("):
            assert forbidden not in text, (path.name, forbidden)
    # Focus/lifecycle correctness is an explicit lifecycle boundary, never timer convergence.
    for forbidden in ("setTimeout", "setInterval", "requestAnimationFrame", "queueMicrotask"):
        assert forbidden not in owner, forbidden
    # Consumers only read the global; nobody wraps or saves it.
    for path in MODAL_CONSUMERS:
        assert not re.search(r"=\s*window\.DPSettingsModal\.(open|confirm)\s*;", source(path)), path.name


def test_modal_owner_is_loaded_directly_and_before_every_consumer():
    html = source(INDEX_HTML)
    tag = re.findall(r'<script src="/ui-settings-modal\.js\?v=\d+" defer></script>', html)
    assert len(tag) == 1, "the canonical modal owner is a direct <script>, exactly once"
    position = html.index("/ui-settings-modal.js")
    for consumer in ("/ui-downloads.js", "/ui-settings-page.js", "/ui-settings-directory-picker.js"):
        assert position < html.index(consumer), consumer
    assert "ui-settings-modal" not in source(PROVIDER_STATUS_JS), "not a lazily-loaded presentation owner"
    assert "ui-settings-modal" not in source(PRESENTATION_LOADER_JS) if PRESENTATION_LOADER_JS.exists() else True


def test_modal_css_is_one_neutral_contract_that_directory_browsing_extends():
    modal_css = source(MODAL_CSS)
    for selector in (".dp-modal-overlay", ".dp-modal-dialog", ".dp-modal-header", ".dp-modal-title",
                     ".dp-modal-body", ".dp-modal-footer", "body.dp-modal-open"):
        assert selector in modal_css, selector
    directory_css = source(DIRECTORY_BROWSER_CSS)
    assert "dp-settings-confirm" not in directory_css
    assert ".dp-modal-header" in directory_css and ".dp-modal-footer" in directory_css
    assert "dp-settings-confirm" not in modal_css
    assert "dp-settings-confirm" not in source(STATIC / "ui-auth-required.css")
