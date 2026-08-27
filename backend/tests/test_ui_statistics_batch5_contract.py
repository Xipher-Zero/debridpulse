"""Reviewed Statistics Batch 5 presentation contract."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
BATCH_RUNTIME = STATIC / "ui-statistics-batch5.js"
BATCH_CSS = STATIC / "ui-statistics-batch5.css"
SURFACES = STATIC / "ui-panel-surface-treatment.css"
THEME_BOOTSTRAP = STATIC / "ui-theme-bootstrap.js"
STYLE_V11 = STATIC / "style-v11.css"
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
VERSION = ROOT / "VERSION"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_batch5_loads_after_batch4_and_preserves_statistics_io_ownership() -> None:
    bootstrap = read(THEME_BOOTSTRAP)
    runtime = read(BATCH_RUNTIME)

    assert "/ui-statistics-batch4.js?v=1" in bootstrap
    assert "/ui-statistics-batch5.js?v=3" in bootstrap
    assert bootstrap.index("/ui-statistics-batch4.js?v=1") < bootstrap.index("/ui-statistics-batch5.js?v=3")
    assert "data-dp-statistics-batch5" in bootstrap
    assert "previous.dpStatisticsBatch4 !== '1'" in runtime
    assert "wrapped.dpStatisticsBatch5 = '1'" in runtime
    assert "window.loadDetailedStats = wrapped" in runtime
    assert "'/ui-statistics-batch5.css?v=2'" in runtime
    for forbidden in ("fetch(", "api(", "/stats/detail", "XMLHttpRequest", "EventSource"):
        assert forbidden not in runtime


def test_kpi_rows_use_reviewed_order_and_semantic_color_families() -> None:
    runtime = read(BATCH_RUNTIME)
    css = read(BATCH_CSS)

    assert "['downloads', 'completed', 'progress', 'success', 'data']" in runtime
    for value_id in ("i-last-day", "i-last-week", "i-success-rate", "i-avg-duration", "i-avg-size"):
        assert "'" + value_id + "'" in runtime
    assert runtime.index("'i-last-day'") < runtime.index("'i-last-week'") < runtime.index("'i-success-rate'")
    assert runtime.index("'i-success-rate'") < runtime.index("'i-avg-duration'") < runtime.index("'i-avg-size'")

    expected_primary = {
        'downloads': 'var(--dp-accent-purple-bright)',
        'completed': 'var(--dp-state-success)',
        'progress': 'var(--dp-state-active)',
        'success': 'var(--dp-state-success)',
        'data': 'var(--dp-state-active)',
    }
    for metric, color in expected_primary.items():
        selector = '[data-dp-stats-metric="' + metric + '"]'
        assert selector in css
        segment = css[css.index(selector):]
        assert color in segment.split('}', 1)[0]

    assert ".dp-stats-kpi-day," in css
    assert ".dp-stats-kpi-week," in css
    assert ".dp-stats-kpi-success" in css
    assert "--c: var(--dp-state-success) !important" in css
    assert "--c: var(--dp-state-caution) !important" in css
    assert "--c: var(--dp-state-active) !important" in css

    value_selector = ".dp-stats-history-grid .dash-kpi-val"
    assert value_selector in css
    value_segment = css[css.index(value_selector):].split('}', 1)[0]
    assert "color: var(--c) !important" in value_segment


def test_success_rate_copy_distinguishes_period_and_life_time_scopes() -> None:
    runtime = read(BATCH_RUNTIME)

    for flavor in (
        "Share of finished downloads completed successfully during the last hour.",
        "Share of finished downloads completed successfully during the last 24 hours.",
        "Share of finished downloads completed successfully during the last 7 days.",
        "Share of finished downloads completed successfully during the last 30 days.",
        "Share of finished downloads completed successfully during the last year.",
        "Share of finished downloads completed successfully across all recorded history.",
    ):
        assert flavor in runtime

    assert "LIFE-TIME SUCCESS RATE" in runtime
    assert "Share of all recorded finished downloads completed successfully." in runtime
    assert "normalizeSuccessRateCopy(period)" in runtime


def test_secondary_kpi_glyphs_are_centered_without_resizing_the_chip() -> None:
    css = read(BATCH_CSS)

    assert ".dp-stats-history-grid .dp-kpi-icon > .dp-icon" in css
    assert "top: 50% !important" in css
    assert "left: 50% !important" in css
    assert "transform: translate(-50%, -50%) !important" in css
    assert "--dp-icon-frame-size" not in css


def test_completion_chart_uses_standard_two_line_feature_header() -> None:
    runtime = read(BATCH_RUNTIME)
    css = read(BATCH_CSS)

    assert "heading.textContent = 'Completions'" in runtime
    assert "dp-stats-chart-title-icon" in runtime
    assert "'/icons/dp/card-download.svg'" in runtime
    assert "chartTitle.className = 'dp-stats-chart-subtitle'" in runtime
    for flavor in (
        "Completed downloads in the last hour.",
        "Completed downloads in the last 24 hours.",
        "Completed downloads in the last 7 days.",
        "Completed downloads in the last 30 days.",
        "Completed downloads in the last year.",
        "Completed downloads across all recorded history.",
    ):
        assert flavor in runtime

    assert "min-height: 72px !important" in css
    assert "width: var(--dp-feature-icon-size, 51px) !important" in css
    assert ".dp-stats-chart .scard-header::before" in css
    assert "content: none !important" in css


def test_breakdowns_reuse_shared_list_surface_and_five_row_cadence() -> None:
    runtime = read(BATCH_RUNTIME)
    css = read(BATCH_CSS)
    surfaces = read(SURFACES)
    overlay = read(STYLE_V11)

    assert "dp-list-workspace-surface" in surfaces
    assert ".dp-list-workspace-surface" in surfaces
    assert "chartCard.classList.add('dp-list-workspace-surface')" in runtime
    for detail_id in (
        "detail-torrent-status",
        "detail-file-status",
        "detail-event-levels",
        "detail-sources",
    ):
        assert detail_id in runtime
    assert "card.classList.add('dp-list-workspace-surface')" in runtime
    assert "line-height: 20px !important" in css
    assert "min-height: 20px !important" in css
    assert "/ui-panel-surface-treatment.css?v=22" in overlay


def test_shell_branding_uses_fixed_global_version_datum_and_unframed_light_logo() -> None:
    runtime = read(BATCH_RUNTIME)
    css = read(BATCH_CSS)

    assert "function normalizeShellBranding()" in runtime
    assert "'/logo.svg?v=7'" in runtime
    assert "version.classList.add('dp-app-version')" in runtime
    assert "document.body.appendChild(version)" in runtime

    selector = "body.dp-v11-structural > #sidebar-version.dp-app-version"
    assert selector in css
    segment = css[css.index(selector):].split('}', 1)[0]
    assert "position: fixed !important" in segment
    assert "right: max(" in segment
    assert "var(--dp-shell-x)" in segment
    assert "var(--dp-shell-sidebar)" in segment
    assert "var(--dp-content-max-width)" in segment
    assert "bottom: 7px !important" in segment
    assert "text-align: right" in segment
    assert "font-size: 9px" in segment

    assert "#sidebar .logo-name" in css
    assert "font-size: 20px" in css
    assert "body.light.dp-v11-structural #sidebar .logo-icon" in css
    light_logo = css[css.index("body.light.dp-v11-structural #sidebar .logo-icon"):].split('}', 1)[0]
    assert "border: 0 !important" in light_logo
    assert "outline: 0 !important" in light_logo
    assert "background: transparent !important" in light_logo
    assert "box-shadow: none !important" in light_logo
    assert "drop-shadow" in light_logo


def test_ci_syntax_checks_batch5_runtime_and_version_stays_frozen() -> None:
    workflow = read(WORKFLOW)
    assert "node --check frontend/static/ui-statistics-batch5.js" in workflow
    assert read(VERSION).strip() == "1.0.10"
