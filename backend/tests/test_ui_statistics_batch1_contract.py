"""Reviewed Statistics Batch 1 presentation contract."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
STATS_CSS = STATIC / "ui-statistics-page.css"
VISUAL_RUNTIME = STATIC / "ui-visual-behavior-fixes.js"
STATS_ICON = STATIC / "icons" / "dp" / "statistics.svg"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_statistics_master_card_preserves_page_heading_and_builds_internal_header() -> None:
    runtime = read(VISUAL_RUNTIME)
    css = read(STATS_CSS)

    required_runtime = (
        "ensureStatisticsArchitecture",
        "dp-statistics-master",
        "dp-stats-master-header",
        "dp-stats-master-body",
        "dp-stats-master-title",
        "dp-stats-heading",
        "dp-stats-subtitle",
        "Historical transfer performance and completion metrics.",
        "dp-stats-period-label",
        "/icons/dp/statistics.svg",
    )
    missing = [fragment for fragment in required_runtime if fragment not in runtime]
    assert not missing, f"Statistics master-card runtime is missing: {missing}"

    assert "#view-stats.active.dp-statistics-master" in css
    assert "height: 100% !important" in css
    assert "#content:has(#view-stats.active)" in css
    assert "overflow-y: hidden" in css
    assert ".dp-stats-master-body" in css
    assert "overflow-y: auto" in css


def test_statistics_period_moves_to_header_and_defaults_to_seven_days() -> None:
    runtime = read(VISUAL_RUNTIME)

    assert "item.dataset.period === '7d'" in runtime
    assert "tabs.dataset.dpDefaultPeriod = '7d'" in runtime
    assert "|| '7d'" in runtime
    assert "aria-selected" in runtime
    assert "window.loadDetailedStats = wrapped" in runtime


def test_statistics_bottom_breakdowns_are_one_four_column_desktop_row() -> None:
    runtime = read(VISUAL_RUNTIME)
    css = read(STATS_CSS)

    assert "ensureStatisticsBreakdownGrid" in runtime
    assert "dp-stats-breakdown-grid" in runtime
    assert "grid-template-columns: repeat(4, minmax(0, 1fr))" in css
    assert "min-height: 142px" in css


def test_statistics_completion_chart_uses_dark_purple_gradient_only() -> None:
    runtime = read(VISUAL_RUNTIME)

    for stop in ("#211044", "#3a176e", "#6427a5", "#7b39c9"):
        assert stop in runtime
    assert "statisticsPurpleGradient" in runtime
    assert "dataset.backgroundColor = function" in runtime
    assert "rgba(56,210,125,.48)" not in runtime
    assert "#38d27d" not in runtime


def test_statistics_kpi_icon_chips_are_dark_and_omnidirectionally_glowing() -> None:
    css = read(STATS_CSS)

    assert "--dp-icon-frame-bg: color-mix(in srgb, var(--dp-icon-frame-fg) 12%, #080b18)" in css
    assert "position: relative" in css
    assert "z-index: 8" in css
    assert "0 0 6px color-mix" in css
    assert "0 0 14px color-mix" in css
    assert "0 0 24px color-mix" in css


def test_statistics_supplied_feature_art_is_true_vector() -> None:
    raw = read(STATS_ICON)
    lowered = raw.lower()

    assert "<svg" in lowered
    assert "viewbox=" in lowered
    assert "<path" in lowered
    assert "<image" not in lowered
    assert "data:image" not in lowered
