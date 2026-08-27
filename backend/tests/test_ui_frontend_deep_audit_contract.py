"""Deep frontend-contract audit for the v1.0.11 presentation branch."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "frontend" / "static"
INDEX = STATIC / "index.html"
STYLE = STATIC / "style-v11.css"
SHARED = STATIC / "ui-shared-contract.css"
SHELL_STYLE = STATIC / "ui-shell-structural.css"
ACTIVITY_STYLE = STATIC / "ui-activity-log-page.css"
DOWNLOADS_STYLE = STATIC / "ui-downloads-page.css"
THEME_BOOTSTRAP = STATIC / "ui-theme-bootstrap.js"
UI_RUNTIME = STATIC / "ui-runtime.js"
DOWNLOADS_RUNTIME = STATIC / "ui-downloads-runtime.js"
A11Y_RUNTIME = STATIC / "ui-accessibility-runtime.js"
OPERATOR_RUNTIME = STATIC / "operator-title.js"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"


def test_normal_ui_bootstrap_is_static_and_deterministic() -> None:
    html = INDEX.read_text(encoding="utf-8")

    legacy = '<link rel="stylesheet" href="/style.css?v=15">'
    overlay = (
        '<link id="debridpulse-duplicate-status-style" rel="stylesheet" '
        'href="/style-v11.css?v=24" data-dp-v11-styles="1">'
    )
    body = '<body class="dp-v11-structural">'
    theme = '<script src="/ui-theme-bootstrap.js?v=21"></script>'
    sidebar = '<aside id="sidebar">'

    for fragment in (legacy, overlay, body, theme, sidebar):
        assert fragment in html

    assert html.index(legacy) < html.index(overlay)
    assert html.index(body) < html.index(theme) < html.index(sidebar)
    assert '/ui-shared-contract.css?v=23' not in html


def test_parser_deferred_presentation_runtimes_have_one_normal_order() -> None:
    html = INDEX.read_text(encoding="utf-8")
    scripts = (
        '<script src="/app.js?v=15" defer></script>',
        '<script src="/operator-title.js?v=23" defer></script>',
        '<script src="/ui-runtime.js?v=24" defer data-dp-ui-runtime="1"></script>',
        '<script src="/ui-downloads-runtime.js?v=22" defer data-dp-downloads-runtime="1"></script>',
        '<script src="/ui-accessibility-runtime.js?v=21" defer></script>',
    )
    positions = [html.index(script) for script in scripts]
    assert positions == sorted(positions)

    # Compatibility script injectors are permitted only because the static path
    # carries the exact markers that make them no-ops during normal app boot.
    operator = OPERATOR_RUNTIME.read_text(encoding="utf-8")
    assert "script[data-dp-ui-runtime]" in operator
    assert "script[data-dp-downloads-runtime]" in operator


def test_v11_stylesheet_runtime_is_fallback_not_normal_owner() -> None:
    html = INDEX.read_text(encoding="utf-8")
    runtime = UI_RUNTIME.read_text(encoding="utf-8")
    overlay = STYLE.read_text(encoding="utf-8")

    assert 'data-dp-v11-styles="1"' in html
    assert "/style-v11.css?v=24" in html
    assert "link[data-dp-v11-styles]" in runtime
    assert "style-v11\\.css\\?v=24$" in runtime

    # Targeted invalidation stays explicit. Only layers changed by reviewed work
    # advance; established approved layers retain their reviewed generations.
    imports = [line.strip() for line in overlay.splitlines() if line.strip().startswith("@import")]
    assert imports
    expected_versions = {
        "/ui-language-tokens.css": "21",
        "/ui-shared-contract.css": "31",
        "/ui-modal-contract.css": "25",
        "/ui-shell-structural.css": "26",
        "/ui-shell-provider-status.css": "23",
        "/ui-shell-provider-status-v2.css": "28",
        "/ui-dashboard-control-polish.css": "23",
        "/ui-dashboard-consistency.css": "23",
        "/ui-statistics-page.css": "21",
        "/ui-activity-log-page.css": "28",
        "/ui-downloads-page.css": "27",
        "/ui-downloads-desktop.css": "28",
        "/ui-help-page.css": "22",
        "/ui-feature-icon-contract.css": "3",
        "/ui-panel-surface-treatment.css": "22",
        "/ui-transfer-contract.css": "31",
        "/ui-live-review-batch.css": "21",
    }
    for path, version in expected_versions.items():
        assert f"@import url('{path}?v={version}');" in imports

    changed_paths = set(expected_versions)
    unchanged = [
        line for line in imports
        if not any(f"'{path}?" in line for path in changed_paths)
    ]
    assert unchanged
    assert all("?v=20" in line for line in unchanged)

    universal_pos = overlay.index("/ui-universal-language.css?v=20")
    shared_pos = overlay.index("/ui-shared-contract.css?v=31")
    modal_pos = overlay.index("/ui-modal-contract.css?v=25")
    shell_pos = overlay.index("/ui-shell.css?v=20")
    shell_structural_pos = overlay.index("/ui-shell-structural.css?v=26")
    provider_pos = overlay.index("/ui-shell-provider-status.css?v=23")
    provider_v2_pos = overlay.index("/ui-shell-provider-status-v2.css?v=28")
    dashboard_pos = overlay.index("/ui-dashboard.css?v=20")
    dashboard_control_pos = overlay.index("/ui-dashboard-control-polish.css?v=23")
    dashboard_consistency_pos = overlay.index("/ui-dashboard-consistency.css?v=23")
    statistics_pos = overlay.index("/ui-statistics-page.css?v=21")
    activity_pos = overlay.index("/ui-activity-log-page.css?v=28")
    downloads_pos = overlay.index("/ui-downloads-page.css?v=27")
    downloads_desktop_pos = overlay.index("/ui-downloads-desktop.css?v=28")
    help_pos = overlay.index("/ui-help-page.css?v=22")
    feature_icon_pos = overlay.index("/ui-feature-icon-contract.css?v=3")
    treatment_pos = overlay.index("/ui-panel-surface-treatment.css?v=22")
    transfer_pos = overlay.index("/ui-transfer-contract.css?v=31")
    assert universal_pos < shared_pos < modal_pos < shell_pos < shell_structural_pos
    assert shell_structural_pos < provider_pos < provider_v2_pos < dashboard_pos
    assert dashboard_pos < dashboard_control_pos < dashboard_consistency_pos < statistics_pos < activity_pos < downloads_pos
    assert downloads_pos < downloads_desktop_pos < help_pos < feature_icon_pos < treatment_pos < transfer_pos


def test_shared_visual_contract_is_owned_by_css_not_runtime_javascript() -> None:
    css = SHARED.read_text(encoding="utf-8")
    operator = OPERATOR_RUNTIME.read_text(encoding="utf-8")

    assert ".badge-duplicate" in css
    assert "var(--dp-state-caution-bg)" in css
    assert ":focus-visible" in css
    assert ".dp-pager-btn" in css
    assert ".sidebar-footer" not in css

    assert "installDuplicateStatusStyle" not in operator
    assert "document.createElement('style')" not in operator
    assert "debridpulse-duplicate-status-style" not in operator


def test_page_layers_do_not_own_shell_contract() -> None:
    activity = ACTIVITY_STYLE.read_text(encoding="utf-8")
    downloads = DOWNLOADS_STYLE.read_text(encoding="utf-8")
    shell = SHELL_STYLE.read_text(encoding="utf-8")

    assert ".sidebar-footer" not in activity
    assert ".sidebar-footer" not in downloads
    assert ".sidebar-footer::before" in shell
    assert "body.dp-v11-structural .sidebar-footer" in shell
    assert "bottom: 24px !important" in shell
    assert ":has(#view-torrents.active) .sidebar-footer" not in shell


def test_activity_rebuild_runtime_is_presentation_only() -> None:
    js = UI_RUNTIME.read_text(encoding="utf-8")
    required = (
        "decorateActivityLog",
        "normalizeActivityRows",
        "dp-activity-card",
        "dp-activity-search-band",
        "dp-activity-list",
        "dp-activity-row",
        "row.classList.remove('event-item')",
    )
    missing = [fragment for fragment in required if fragment not in js]
    assert not missing, f"Activity presentation rebuild is missing: {missing}"

    for forbidden in ("fetch(", "/api/", "XMLHttpRequest", "EventSource"):
        assert forbidden not in js


def test_cross_cutting_accessibility_runtime_is_semantic_and_presentation_only() -> None:
    js = A11Y_RUNTIME.read_text(encoding="utf-8")
    required = (
        "aria-current",
        "aria-pressed",
        "role', 'group'",
        "role', 'tablist'",
        "role', 'tab'",
        "ArrowRight",
        "ArrowLeft",
        "Home",
        "End",
        "View downloads with errors",
        "Close details",
        "Activity Log",
        "normalizeProviderPremiumLabel",
        "days remaining",
        "removeProperty('border-top')",
    )
    missing = [fragment for fragment in required if fragment not in js]
    assert not missing, f"accessibility contract is missing: {missing}"

    forbidden = ("fetch(", "/api/", "api(", "XMLHttpRequest", "EventSource")
    present = [fragment for fragment in forbidden if fragment in js]
    assert not present, f"accessibility runtime crossed into application I/O: {present}"


def test_first_paint_theme_bootstrap_is_local_and_network_free() -> None:
    js = THEME_BOOTSTRAP.read_text(encoding="utf-8")
    assert "localStorage.getItem('theme')" in js
    assert "document.body.classList.add('light')" in js
    for forbidden in ("fetch(", "/api/", "XMLHttpRequest", "EventSource", "setTimeout("):
        assert forbidden not in js


def test_downloads_runtime_remains_presentational_after_static_bootstrap() -> None:
    js = DOWNLOADS_RUNTIME.read_text(encoding="utf-8")
    assert "decorateDownloadsStructure" in js
    assert "installPaginationRenderer" in js
    assert "legacySetFilter.apply" in js
    for forbidden in ("fetch(", "/api/", "XMLHttpRequest"):
        assert forbidden not in js


def test_ci_syntax_checks_every_first_party_browser_runtime() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    first_party = (
        "app.js",
        "auth.js",
        "auth-settings.js",
        "auth-help.js",
        "auth-ux.js",
        "operator-title.js",
        "ui-runtime.js",
        "ui-downloads-runtime.js",
        "ui-accessibility-runtime.js",
        "ui-theme-bootstrap.js",
        "ui-visual-behavior-fixes.js",
    )
    missing = [
        filename
        for filename in first_party
        if f"node --check frontend/static/{filename}" not in workflow
    ]
    assert not missing, f"CI does not syntax-check browser runtimes: {missing}"
