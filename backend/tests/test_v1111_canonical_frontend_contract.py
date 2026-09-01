from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
INDEX = STATIC / "index.html"
APP = STATIC / "app.js"
SETTINGS = STATIC / "ui-settings-page.js"
HELP = STATIC / "ui-help-page.js"
HELP_LEGAL = STATIC / "ui-help-license-documents.js"
STATS = STATIC / "ui-statistics.js"
RUNTIME = STATIC / "ui-runtime.js"
DOWNLOADS = STATIC / "ui-downloads-runtime.js"
BOOTSTRAP = STATIC / "ui-theme-bootstrap.js"
VERSION = ROOT / "VERSION"
README = ROOT / "README.md"
COMPOSE = ROOT / "docker-compose.yml"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def script_position(html: str, name: str) -> int:
    match = re.search(rf'<script[^>]+src=["\'][^"\']*{re.escape(name)}(?:\?[^"\']*)?["\']', html)
    assert match, f"missing direct script dependency: {name}"
    return match.start()


def test_canonical_pages_are_direct_required_dependencies_without_legacy_loader() -> None:
    html = read(INDEX)
    bootstrap = read(BOOTSTRAP)
    retired = (
        "ui-presentation-loader.js",
        "ui-shell-runtime.js",
        "ui-page-finalization.js",
        "ui-page-finalization.css",
        "ui-visual-behavior-fixes.js",
        "ui-help-chrome.js",
        "ui-settings-auth-resilience.js",
        "ui-settings-authentication.js",
        "ui-settings-authentication-polish.js",
        "ui-settings-authentication-polish.css",
        "ui-settings-authentication-oidc.js",
        "ui-settings-authentication-callback.js",
    )
    for name in retired:
        assert not (STATIC / name).exists(), f"retired presentation layer returned: {name}"
        assert name not in html

    order = [
        script_position(html, "app.js"),
        script_position(html, "ui-runtime.js"),
        script_position(html, "ui-downloads-runtime.js"),
        script_position(html, "ui-statistics.js"),
        script_position(html, "ui-help-page.js"),
        script_position(html, "ui-help-license-documents.js"),
        script_position(html, "ui-settings-page.js"),
    ]
    assert order == sorted(order)
    assert "createElement('script')" not in bootstrap
    assert 'createElement("script")' not in bootstrap
    assert "createElement('link')" not in bootstrap
    assert 'createElement("link")' not in bootstrap


def test_settings_and_help_have_no_inherited_static_substrate() -> None:
    html = read(INDEX)
    for view_id in ("view-settings", "view-help"):
        match = re.search(rf'<div class="view" id="{view_id}">\s*</div>', html)
        assert match, f"{view_id} must be an empty canonical render root"


def test_app_does_not_regain_page_specific_owners() -> None:
    app = read(APP)
    forbidden = (
        "function renderSettings(",
        "function getFormSettings(",
        "function switchSettingsTab(",
        "function loadDetailedStats(",
        "function renderTorrentPagination(",
        "function setFilter(",
    )
    for fragment in forbidden:
        assert fragment not in app


def test_settings_is_the_single_clean_room_owner_and_emits_explicit_lifecycle() -> None:
    source = read(SETTINGS)
    assert "window.loadSettings = load;" in source
    assert "window.DPSettingsPage = Object.freeze({load});" in source
    assert "debridpulse:settings-rendered" in source
    assert "view.innerHTML =" in source
    assert "new MutationObserver" not in source
    assert "window.api =" not in source
    assert "window.confirm =" not in source
    assert "window.toast =" not in source

    expected = [
        "['sources', 'Sources & Providers', 'zap']",
        "['downloads', 'Downloads', 'download']",
        "['extraction', 'Extraction', 'package-open']",
        "['notifications', 'Notifications', 'bell']",
        "['authentication', 'Authentication', 'shield-check']",
        "['maintenance', 'Data & Maintenance', 'database-backup']",
    ]
    positions = [source.index(item) for item in expected]
    assert positions == sorted(positions)


def test_settings_authentication_renders_final_cards_directly() -> None:
    source = read(SETTINGS)
    for fragment in (
        "Authentication Status",
        "Username & Password",
        "API Access",
        "OpenID Connect",
        "Browser Session Lifetime",
        "Public DebridPulse Base URL",
        "OIDC Callback URL",
        "Test OIDC Sign-In",
        "dp-settings-auth-header-enable",
        "dp-settings-auth-credentials-row",
        "dp-settings-auth-status-card",
    ):
        assert fragment in source
    assert "renderSettingsWithAuthentication" not in source
    assert "removeLegacyAuthenticationControls" not in source


def test_auth_loading_is_resilient_without_wrapping_global_api_or_settings() -> None:
    source = read(SETTINGS)
    for fragment in (
        "let loadGeneration = 0;",
        "let authGeneration = 0;",
        "fallbackAuthFromSettings(settings)",
        "const settingsPromise = request('GET', '/settings'",
        "const authPromise = request('GET', '/auth/config'",
        "if (generation !== loadGeneration || !settingsActive()) return;",
        "markAuthUnavailable(error)",
        "probeOidcRuntime(auth, authGen)",
    ):
        assert fragment in source
    assert source.index("settings = await settingsPromise") < source.index("void authPromise.then")


def test_oidc_origin_callback_and_verified_email_policy_are_owned_by_settings() -> None:
    source = read(SETTINGS)
    for fragment in (
        "function callbackFromPublicBase(value)",
        "function updateOidcCallbackPreview()",
        "function copyOidcCallback()",
        "id=\"dp-auth-public-base-url\"",
        "id=\"dp-auth-oidc-callback\"",
        "readonly",
        "Requires email_verified=true.",
        "oidc_allowed_emails",
        "oidc_allowed_subjects",
        "oidc_allowed_groups",
        "oidc_group_claim",
        "oidc_allow_all",
        "clear_oidc_client_secret",
    ):
        assert fragment in source
    assert "request('PUT', '/auth/config'" in source


def test_password_session_and_api_token_secret_semantics_survive_canonicalization() -> None:
    source = read(SETTINGS)
    for fragment in (
        "function authPayload()",
        "function persistAuth(",
        "function clearPassword(",
        "clear_password",
        "auth_session_lifetime_hours",
        "logoutSession(",
        "function setApiTokenEnabled(",
        "function generateToken(",
        "function clearToken(",
        "request('PUT', '/auth/api-token'",
        "request('POST', '/auth/api-token'",
        "request('DELETE', '/auth/api-token'",
        "oneTimeToken",
        "Copy this token now",
    ):
        assert fragment in source
    assert "localStorage" not in source
    assert "sessionStorage" not in source


def test_non_auth_settings_contracts_remain_in_canonical_owner_or_semantic_modules() -> None:
    combined = "\n".join(read(path) for path in sorted(STATIC.glob("ui-settings*.js")))
    for fragment in (
        "alldebrid_api_key",
        "aria2_mode",
        "aria2_secret",
        "extract_enabled",
        "extract_delete_archive",
        "discord_webhook_url",
        "backup_enabled",
        "db_wipe_enabled",
        "clear_secrets",
        "/settings/validate-alldebrid",
        "/settings/validate-aria2",
        "/settings/validate-discord",
    ):
        assert fragment in combined


def test_help_is_direct_final_owner_with_seven_reviewed_sections() -> None:
    source = read(HELP)
    for tab in (
        "['quickstart', 'Quick Start', 'rocket']",
        "['howitworks', 'How it works', 'workflow']",
        "['aria2', 'Download Engine', 'download']",
        "['integrations', 'Integrations', 'plug']",
        "['settings', 'Settings', 'settings']",
        "['trouble', 'Troubleshooting', 'wrench']",
        "['license', 'License', 'scale']",
    ):
        assert tab in source
    assert "Field Manual" in source
    assert "When intuition fails." in source
    assert "dp-help-master-card" in source
    assert "dp-help-section-card" in source
    assert "window.loadHelp = load;" in source
    assert "window.DPHelpPage = Object.freeze({load, activateTab});" in source
    assert "debridpulse:help-rendered" in source
    assert "DOMContentLoaded" not in source
    assert "new MutationObserver" not in source
    assert "if (v === 'help')      loadHelp();" in read(APP)
    assert (STATIC / "icons" / "lucide" / "scale.svg").is_file()


def test_help_legal_attribution_and_bundled_document_actions_are_preserved() -> None:
    help_source = read(HELP)
    legal = read(HELP_LEGAL)
    for fragment in (
        "Copyright &copy; 2026 Chris Moore",
        "kroeberd/alldebrid-client v1.9.9",
        "GPL-2.0-or-later",
        'data-legal-document="gpl"',
        'data-legal-document="notice"',
        'data-legal-document="upstream-mit"',
        'data-legal-document="source-offer"',
        'data-legal-document="third-party"',
    ):
        assert fragment in help_source
    assert "/api/legal-documents/" in legal
    assert "credentials: 'same-origin'" in legal
    assert "trapDialogKeydown" in legal
    assert "const helpRoot = () => document.getElementById('view-help');" in legal
    assert "let activeBackdrop = null;" in legal
    assert "let activeOpener = null;" in legal
    assert "debridpulse:help-rendered" in legal
    assert "requestAnimationFrame" not in legal
    assert "DOMContentLoaded" not in legal


def test_statistics_is_final_owner_with_reviewed_copy_default_and_palette() -> None:
    source = read(STATS)
    html = read(INDEX)
    for fragment in (
        "window.loadDetailedStats = loadDetailedStats;",
        "window.DPStatisticsLifecycle = Object.freeze({load: loadDetailedStats, install});",
        "By the Numbers",
        "Because vibes are not a performance metric.",
        "dp-statistics-master",
        "dp-statistics-master-header",
        "dp-statistics-master-body",
        "['downloads', 'completed', 'progress', 'success', 'data']",
        "statisticsPurpleGradient",
        "debridpulse:theme-changed",
        "debridpulse:navigation",
    ):
        assert fragment in source
    assert "|| '7d'" in source
    assert 'data-period="7d" class="ftab active"' in html or 'class="ftab active" data-period="7d"' in html
    assert "Completions — last 7 days" in html
    assert "window.loadDetailedStats = wrapped" not in source


def test_statistics_keeps_reviewed_kpis_breakdowns_and_chart_header() -> None:
    source = read(STATS)
    for fragment in (
        "Last 24 Hours",
        "Completed downloads over the last 24 hours.",
        "Last 7 Days",
        "Completed downloads over the last 7 days.",
        "MEAN DOWNLOAD TIME",
        "LIFE-TIME SUCCESS RATE",
        "MEAN DOWNLOAD SIZE",
        "MAX_VISIBLE = 10",
        "TWO_COLUMN_THRESHOLD = 6",
        "entries.slice(0, MAX_VISIBLE)",
        "Math.ceil(visible.length / 2)",
        "heading.textContent = 'Completions'",
        "/icons/dp/card-download.svg",
    ):
        assert fragment in source


def test_runtime_coordination_uses_explicit_events_not_page_convergence_observation() -> None:
    runtime = read(RUNTIME)
    for event in (
        "debridpulse:navigation",
        "debridpulse:dashboard-stats-rendered",
        "debridpulse:dashboard-recent-rendered",
        "debridpulse:activity-rendered",
    ):
        assert event in runtime
    assert "new MutationObserver" not in runtime
    assert "window.loadStats =" not in runtime


def test_downloads_pagination_filtering_are_not_owned_by_app() -> None:
    app = read(APP)
    downloads = read(DOWNLOADS)
    assert "function renderTorrentPagination(" not in app
    assert "function setFilter(" not in app
    assert "renderTorrentPagination" in downloads
    assert "setFilter" in downloads
    assert "debridpulse:downloads-rendered" in app or "debridpulse:downloads-rendered" in downloads



def test_downloads_static_owner_is_the_accepted_integrated_composition() -> None:
    html = read(INDEX)
    source = read(DOWNLOADS)
    page_css = read(STATIC / "ui-downloads-page.css")
    operator = read(STATIC / "operator-title.js")
    view = html[html.index('id="view-torrents"'):html.index('<!-- Events -->')]
    assert 'Download Queue' in view
    assert 'data-dp-filter-contract="desktop-v24"' in view
    assert 'class="bulk-bar dp-downloads-bulk-card dp-downloads-bulk-integrated" id="bulk-bar"' in view
    assert view.index('id="torrent-search"') < view.index('id="bulk-bar"') < view.index('class="dp-downloads-table-wrap"')
    assert 'id="torrent-page-size"' not in view
    assert 'Most of them followed instructions.' in source
    assert "bar.replaceChildren(header)" not in source
    assert "insertBefore(bar" not in source
    assert "bar.classList.add('dp-card'" not in source
    assert "Canonical integrated multi-selection strip" not in page_css
    assert "data-dp-downloads-runtime" not in operator
    assert '/ui-downloads-runtime.js?v=24' in html


def test_dashboard_has_no_inherited_startup_status_surface_or_writer() -> None:
    html = read(INDEX)
    app = read(APP)
    for fragment in ('debug-status', 'dash-health-bar', 'dash-health-recovery', 'dash-health-deadlock', 'dash-health-aging'):
        assert fragment not in html
        assert fragment not in app
    assert 'function dbg(' not in app
    assert 'dbg(' not in app
    assert 'function updateHealthBar(' not in app
    assert 'function runRecovery(' in app
    assert "'/recovery/run'" in app

def test_release_surfaces_follow_v1111_without_advancing_production_state() -> None:
    version = read(VERSION).strip()
    assert version == "1.0.11.1"
    tag = "ghcr.io/xipher-zero/debridpulse:v1.0.11.1"
    assert tag in read(COMPOSE)
    assert read(README).count(tag) >= 2


def test_core_canonical_owners_do_not_reintroduce_historical_wrapper_patterns() -> None:
    sources = {
        "settings": read(SETTINGS),
        "help": read(HELP),
        "statistics": read(STATS),
        "runtime": read(RUNTIME),
        "downloads": read(DOWNLOADS),
    }
    forbidden = (
        "baseRenderSettings",
        "legacyRender",
        "previous.apply",
        "window.loadSettings = wrapped",
        "window.loadDetailedStats = wrapped",
        "window.api = wrapped",
    )
    for name, source in sources.items():
        for fragment in forbidden:
            assert fragment not in source, f"{name} regained wrapper pattern {fragment}"
