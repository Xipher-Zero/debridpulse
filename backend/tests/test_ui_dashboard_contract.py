"""Structural contract for the v1.0.11 Dashboard migration."""

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
    assert "/ui-shell.css?v=20" in entry


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
        "retry-borderless.svg",
        "heartbeat-outline.svg",
        "calendar-24.svg",
        "calendar-7.svg",
        "verified-badge.svg",
        "clock-outline.svg",
        "cube.svg",
    )
    missing = [asset for asset in expected if asset not in runtime]
    assert not missing, f"Dashboard migration is missing canonical assets: {missing}"


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
