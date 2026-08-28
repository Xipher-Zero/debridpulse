"""Reviewed Statistics Batch 1/2 presentation contract."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
STATS_CSS = STATIC / "ui-statistics-page.css"
VISUAL_RUNTIME = STATIC / "ui-visual-behavior-fixes.js"
ORCHESTRATOR = STATIC / "ui-statistics-orchestrator.js"
STATS_ICON = STATIC / "icons" / "dp" / "statistics.svg"
ICON_MANIFEST = STATIC / "icons" / "dp" / "manifest.json"


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
    orchestrator = read(ORCHESTRATOR)

    assert "item.dataset.period === '7d'" in runtime
    assert "tabs.dataset.dpDefaultPeriod = '7d'" in runtime
    assert "|| '7d'" in orchestrator
    assert "aria-selected" in runtime
    assert "window.loadDetailedStats = wrapped" in orchestrator
    assert "window.loadDetailedStats = wrapped" not in runtime
    assert "debridpulse:statistics-rendered" in runtime


def test_statistics_bottom_breakdowns_are_one_four_column_desktop_row_with_five_row_capacity() -> None:
    runtime = read(VISUAL_RUNTIME)
    css = read(STATS_CSS)

    assert "ensureStatisticsBreakdownGrid" in runtime
    assert "dp-stats-breakdown-grid" in runtime
    assert "grid-template-columns: repeat(4, minmax(0, 1fr))" in css
    assert "min-height: 170px" in css
    assert "min-height: 122px" in css


def test_statistics_completion_chart_uses_theme_aware_translucent_purple_gradients() -> None:
    runtime = read(VISUAL_RUNTIME)

    required = (
        "document.body.classList.contains('light')",
        "rgba(210, 195, 239, .28)",
        "rgba(171, 137, 221, .42)",
        "rgba(139, 91, 203, .58)",
        "rgba(45, 19, 84, .46)",
        "rgba(91, 38, 151, .60)",
        "rgba(166, 70, 244, .72)",
        "statisticsPurpleGradient",
        "dataset.backgroundColor = function",
        "applyStatisticsChartPalette();",
    )
    missing = [fragment for fragment in required if fragment not in runtime]
    assert not missing, f"Statistics chart palette is missing: {missing}"
    assert "rgba(56,210,125,.48)" not in runtime
    assert "#38d27d" not in runtime


def test_statistics_chart_fills_available_space_and_uses_approved_download_art() -> None:
    css = read(STATS_CSS)

    assert "flex: 1 1 240px" in css
    assert "min-height: 228px" in css
    assert ".dp-stats-chart > .scard-body" in css
    assert "height: auto !important" in css
    assert ".chart-wrap > canvas" in css
    assert "url('/icons/dp/card-download.svg')" in css
    assert "font-size: 0" in css


def test_statistics_kpi_icon_chips_are_dark_and_omnidirectionally_glowing_in_both_themes() -> None:
    css = read(STATS_CSS)

    assert "--dp-icon-frame-bg: color-mix(in srgb, var(--dp-icon-frame-fg) 12%, #080b18)" in css
    assert "body.light.dp-v11-structural #view-stats .dp-stats-history-grid .dp-kpi-icon" in css
    assert "--dp-icon-frame-bg: color-mix(in srgb, var(--dp-icon-frame-fg) 11%, #080b18)" in css
    assert "position: relative" in css
    assert "z-index: 8" in css
    assert "0 0 6px color-mix" in css
    assert "0 0 14px color-mix" in css
    assert "0 0 24px color-mix" in css


def test_statistics_supplied_feature_art_is_true_vector_and_registered() -> None:
    raw = read(STATS_ICON)
    lowered = raw.lower()
    manifest = json.loads(read(ICON_MANIFEST))

    assert "<svg" in lowered
    assert "viewbox=" in lowered
    assert "<path" in lowered
    assert "<image" not in lowered
    assert "data:image" not in lowered
    assert manifest["icons"]["statistics"] == "statistics.svg"
