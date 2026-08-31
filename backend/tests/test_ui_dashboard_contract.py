"""Final-state structural contract for the v1.0.11 Dashboard."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "frontend" / "static"
V11_STYLE = STATIC / "style-v11.css"
DASHBOARD_CSS = STATIC / "ui-dashboard.css"
STATISTICS_CSS = STATIC / "ui-statistics-page.css"
RUNTIME = STATIC / "ui-runtime.js"


def test_dashboard_stylesheet_is_active() -> None:
    entry = V11_STYLE.read_text(encoding="utf-8")
    assert "/ui-dashboard.css?v=20" in entry
    assert "/ui-dashboard-final.css?v=23" in entry
    assert "/ui-shell.css?v=21" in entry
    for retired in (
        "ui-dashboard-structural.css",
        "ui-dashboard-consistency.css",
        "ui-dashboard-batch1.css",
        "ui-dashboard-batch2.css",
        "ui-dashboard-batch2-final.css",
        "ui-dashboard-batch3.css",
        "ui-dashboard-batch4.css",
    ):
        assert retired not in entry
        assert not (STATIC / retired).exists()


def test_dashboard_canonical_owner_keeps_reviewed_header_and_sparkline_geometry() -> None:
    css = DASHBOARD_CSS.read_text(encoding="utf-8")

    assert "height: 31px;" in css
    assert "opacity: 1;" in css
    assert "stroke-width: 1.55;" in css
    assert ".dp-card-spark .dp-card-spark-point" in css
    assert "filter: drop-shadow(0 0 3px currentColor);" in css
    assert ".dp-dashboard-quick-add .card-header" in css
    assert "min-height: 78px;" in css
    assert ".dp-dashboard-activity .card-header" in css
    assert "min-height: 70px;" in css
    assert "width: calc(100% + 2px) !important;" in css
    assert ".dp-card-spark-fill" in css
    assert "opacity: .88 !important;" in css
    assert "opacity: .72 !important;" in css
    assert ".dp-dashboard-quick-add .card-title > .dp-icon" in css
    assert "flex: 0 0 51px !important;" in css


def test_dashboard_canonical_owner_keeps_reviewed_card_framing_and_empty_state() -> None:
    css = DASHBOARD_CSS.read_text(encoding="utf-8")

    assert "border-color: transparent !important;" in css
    assert "border-bottom: 0;" in css
    assert "-6px 8px 14px -8px rgba(0,0,0,.66)" in css
    assert "rgba(95, 48, 174, .26)" in css
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
        "#1d1930",
        "#f2eff8",
        "card-download.svg?v=11",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"Dashboard canonical surface contract is missing: {missing}"


def test_dashboard_keeps_one_primary_metric_row_and_moves_history() -> None:
    dashboard = DASHBOARD_CSS.read_text(encoding="utf-8")
    statistics = STATISTICS_CSS.read_text(encoding="utf-8")
    runtime = RUNTIME.read_text(encoding="utf-8")

    assert "grid-template-columns: repeat(6" in dashboard
    assert "#view-dashboard .dash-kpi-strip--dashboard" in dashboard
    assert "display: none !important" in dashboard
    assert "#view-stats .dp-stats-history-grid" not in dashboard
    assert "#view-stats .dp-stats-history-grid" in statistics
    assert "moveDashboardKpisToStatistics" in runtime
    assert "statsCards.insertAdjacentElement('afterend', strip)" in runtime
    assert "dp-stats-history-grid" in runtime


def test_dashboard_uses_canonical_custom_semantic_assets() -> None:
    runtime = RUNTIME.read_text(encoding="utf-8")
    expected = (
        "card-download.svg",
        "card-checkmark.svg",
        "card-play.svg",
        "card-clock.svg",
        "card-error.svg",
        "card-disk.svg",
        "card-link.svg",
        "card-document-stack.svg",
        "heartbeat-outline.svg",
        "calendar-24.svg",
        "calendar-7.svg",
        "verified-badge.svg",
        "clock-outline.svg",
        "cube.svg",
    )
    missing = [asset for asset in expected if asset not in runtime]
    assert not missing, f"Dashboard is missing canonical assets: {missing}"


def test_quick_add_preserves_existing_functional_controls() -> None:
    runtime = RUNTIME.read_text(encoding="utf-8")
    css = DASHBOARD_CSS.read_text(encoding="utf-8")

    # The runtime decorates the existing controls; it does not invent a second
    # submission path or alter the existing app.js behavior.
    assert "q-transfer-input" in runtime
    assert "btn-recover-all" in runtime
    assert "btn-add-transfer" in css
    assert "addDashboardEntries" not in runtime
    assert "recoverAll(" not in runtime


def test_dashboard_responsive_contract_preserves_desktop_mockup() -> None:
    css = DASHBOARD_CSS.read_text(encoding="utf-8")
    assert "@media (max-width: 1439px)" in css
    assert "@media (max-width: 1179px)" in css
    assert "@media (max-width: 899px)" in css
    assert "@media (max-width: 640px)" in css
    assert "grid-template-columns: repeat(3" in css
    assert "grid-template-columns: repeat(2" in css
    assert "grid-template-columns: 1fr" in css
