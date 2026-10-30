"""Final-state structural contract for the v1.0.11 Dashboard."""

from __future__ import annotations

from pathlib import Path

from ui_chrome_tokens import require_token


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "frontend" / "static"
V11_STYLE = STATIC / "style.css"
DASHBOARD_CSS = STATIC / "ui-dashboard.css"
STATISTICS_CSS = STATIC / "ui-statistics-page.css"


def test_dashboard_stylesheet_is_active() -> None:
    entry = V11_STYLE.read_text(encoding="utf-8")
    assert "/ui-dashboard.css?v=22" in entry
    assert "/ui-shell.css?v=22" in entry
    for retired in (
        "ui-dashboard-structural.css",
        "ui-dashboard-consistency.css",
        "ui-dashboard-batch1.css",
        "ui-dashboard-batch2.css",
        "ui-dashboard-batch2-final.css",
        "ui-dashboard-batch3.css",
        "ui-dashboard-batch4.css",
        "ui-dashboard-batch5.css",
        "ui-dashboard-polish.css",
        "ui-dashboard-polish-final.css",
        "ui-dashboard-final.css",
    ):
        assert retired not in entry
        assert not (STATIC / retired).exists()


def test_dashboard_canonical_owner_keeps_reviewed_header_and_sparkline_geometry() -> None:
    css = DASHBOARD_CSS.read_text(encoding="utf-8")

    assert "height: 38px;" in css  # 31 px of line art over a 7 px raised floor
    assert "opacity: 1;" in css
    assert "stroke-width: 1.55;" in css
    assert ".dp-card-spark .dp-card-spark-rest" in css
    assert "filter: drop-shadow(0 0 3px currentColor);" in css
    assert ".dp-dashboard-quick-add .card-header" in css
    assert "min-height: 78px;" in css
    assert ".dp-dashboard-activity .card-header" in css
    assert "min-height: 70px;" in css
    assert "width: calc(100% + 2px) !important;" in css
    assert "#view-dashboard #dash-hero.dash-hero--flatline .dp-card-spark-fill { mask: none; opacity: var(--dp-card-spark-rest); }" in css
    assert ".dp-dashboard-quick-add .card-title > .dp-icon" in css
    assert "flex: 0 0 51px !important;" in css


def test_dashboard_canonical_owner_keeps_reviewed_card_framing_and_empty_state() -> None:
    css = DASHBOARD_CSS.read_text(encoding="utf-8")

    assert "border-color: transparent !important;" in css
    assert "border-bottom: 0;" in css
    assert "-6px 8px 14px -8px rgba(0,0,0,.66)" in css
    # The reviewed header glow is the shared panel-header material's native value.
    require_token(css, "--dp-panel-header-surface", "rgba(95, 48, 174, .26)")
    assert "#dash-tbody tr:not([data-torrent-id])" in css
    assert "background: transparent !important;" in css


def test_dashboard_canonical_owner_keeps_reviewed_surface_calibration() -> None:
    css = DASHBOARD_CSS.read_text(encoding="utf-8")

    required = (
        "width: calc(100% - 13px) !important",
        "border-top-left-radius: 12px !important",
        "#000 74%",
        "ellipse 105% 88% at 1% -8%",
        "transparent 98%",
        "background-clip: padding-box !important",
        "card-download.svg?v=11",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"Dashboard canonical surface contract is missing: {missing}"
    require_token(css, "--dp-table-head-surface", "#1d1930", "#f2eff8")


def test_dashboard_keeps_one_primary_metric_row_and_statistics_history_is_retired() -> None:
    dashboard = DASHBOARD_CSS.read_text(encoding="utf-8")
    statistics = STATISTICS_CSS.read_text(encoding="utf-8")
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "grid-template-columns: repeat(6" in dashboard
    assert "#view-dashboard .dash-kpi-strip--dashboard" not in dashboard
    # 1.0.14: the Statistics history cards are retired with their presentation.
    assert "dp-stats-history-grid" not in dashboard and "dp-stats-history-grid" not in statistics
    assert "dash-kpi" not in dashboard and "dash-kpi" not in statistics
    assert "moveDashboardKpisToStatistics" not in app
    assert "decorateHistoricalKpis" not in app
    assert 'class="dash-kpi-strip dash-kpi-strip--dashboard"' not in index
    stats_view = index[index.index('<!-- Statistics -->'):index.index('<!-- Help -->')]
    assert "dp-stats-history-grid" not in stats_view and "dash-kpi" not in stats_view

def test_dashboard_and_statistics_use_canonical_custom_semantic_assets() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    dashboard_view = index[index.index('id="view-dashboard"'):index.index('id="view-torrents"')]
    dashboard_assets = (
        "card-download.svg", "card-checkmark.svg", "card-play.svg", "card-clock.svg",
        "card-error.svg", "card-disk.svg", "card-link.svg", "card-document-stack.svg",
    )
    missing = [asset for asset in dashboard_assets if asset not in dashboard_view]
    assert not missing, f"Dashboard is missing canonical assets: {missing}"
    stats_view = index[index.index('<!-- Statistics -->'):index.index('<!-- Help -->')]
    assert "statistics.svg" in stats_view
    # 1.0.14: the five history cards and their artwork are retired from Statistics.
    for retired in ("heartbeat-outline.svg", "calendar-24.svg", "calendar-7.svg", "clock-outline.svg", "cube.svg"):
        assert retired not in stats_view
    assert "verified-badge.svg" not in stats_view

def test_quick_add_preserves_existing_functional_controls() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    css = DASHBOARD_CSS.read_text(encoding="utf-8")
    dashboard_view = index[index.index('id="view-dashboard"'):index.index('id="view-torrents"')]
    assert "q-transfer-input" in dashboard_view
    assert "btn-recover-all" in dashboard_view
    assert 'data-default-label="Recover All"' in dashboard_view
    assert 'data-dp-lucide="refresh"' in dashboard_view
    assert "btn-add-transfer" in css
    assert "function addDashboardEntries()" in app
    assert "function recoverAll(" in app

def test_dashboard_responsive_contract_preserves_desktop_mockup() -> None:
    css = DASHBOARD_CSS.read_text(encoding="utf-8")
    assert "@media (max-width: 1439px)" in css
    assert "@media (max-width: 1179px)" in css
    assert "@media (max-width: 899px)" in css
    # 1.0.14: the handheld Dashboard (779px and below) keeps Quick Add's rows
    # (dashboard-mobile-composition.spec.js); the former 640px column mode is retired.
    assert "@media (max-width: 779px)" in css
    assert "@media (max-width: 640px)" not in css
    assert "grid-template-columns: repeat(3" in css
    assert "grid-template-columns: repeat(2" in css
    # From 780px each card is its icon at the left and, beside it, its title
    # over its value (and any secondary line). The handheld strip is two equal
    # cards on one row at every width, each its title centred across the top
    # over its icon and value centred beneath it (1.0.14; the former handheld
    # side-by-side and data-dp-kpi stacked arrangements are retired); there the
    # title may wrap between its words and the value is drawn compact where it
    # does not fit beside the icon.
    assert "#view-dashboard .dash-hero { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }" in css
    assert "  grid-template-columns: auto minmax(0, 1fr);\n  grid-template-rows: auto auto minmax(0, 1fr);\n" in css
    assert "#view-dashboard .dhs-icon {\n  grid-column: 1;\n  grid-row: 1 / -1;" in css
    assert "#view-dashboard .dhs-body {\n  grid-column: 2;\n  grid-row: 2;" in css
    assert "#view-dashboard .dhs-label {\n  grid-column: 2;\n  grid-row: 1;" in css
    assert "    grid-template-columns: minmax(0, 1fr) auto auto minmax(0, 1fr);\n" in css
    assert ("  #view-dashboard .dhs-label { grid-column: 1 / -1; grid-row: 1; justify-self: center; margin-top: 0; text-align: center; }") in css
    assert "  #view-dashboard .dhs-icon { grid-column: 2; grid-row: 2; margin-top: 0; }" in css
    assert "  #view-dashboard .dhs-body { grid-column: 3; grid-row: 2; margin-left: 8px; }" in css
    assert "  #view-dashboard .dhs-label { white-space: normal; }" in css
    assert "data-dp-kpi" not in css
    assert "flex-wrap: wrap; gap: 8px" not in css and "min-width: max-content" not in css
    assert "grid-template-columns: 1fr;" not in css
