"""Accepted Statistics page contract with direct canonical base ownership."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
INDEX = STATIC / "index.html"
APP = STATIC / "app.js"
STATS = STATIC / "ui-statistics.js"
STATS_CSS = STATIC / "ui-statistics-page.css"
STATS_ICON = STATIC / "icons" / "dp" / "statistics.svg"
ICON_MANIFEST = STATIC / "icons" / "dp" / "manifest.json"

_STATS_DETAIL_IO_PATTERNS = (
    re.compile(r"""\bapi\(\s*['\"]GET['\"]\s*,\s*['\"]/stats/detail"""),
    re.compile(r"""\bfetch\(\s*['\"]/stats/detail"""),
    re.compile(r"""\brequest\(\s*['\"]GET['\"]\s*,\s*['\"]/stats/detail"""),
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def statistics_view() -> str:
    html = read(INDEX)
    start = html.index("<!-- Statistics -->")
    end = html.index("<!-- Help -->", start)
    return html[start:end]


def statistics_detail_io_owners() -> list[str]:
    owners = []
    for path in sorted(STATIC.glob("*.js")):
        source = read(path)
        if any(pattern.search(source) for pattern in _STATS_DETAIL_IO_PATTERNS):
            owners.append(path.name)
    return owners


def test_statistics_composition_is_direct_static_owner_not_runtime_convergence() -> None:
    view = statistics_view()
    source = read(STATS)
    app = read(APP)
    style = read(STATIC / "style-v11.css")
    for fragment in ('class="view card dp-statistics-master" id="view-stats"', 'class="card-header dp-stats-master-header"', 'class="card-body dp-stats-master-body"', 'class="dash-kpi-strip dp-stats-history-grid"', 'class="scard dp-stats-chart dp-list-workspace-surface"', 'class="dp-stats-breakdown-grid"', "By the Numbers", "Because vibes are not a performance metric."):
        assert fragment in view
    assert "split-grid" not in view
    assert "📈" not in view
    assert "ensureStatisticsArchitecture" not in source
    assert "decorateChartHeader" not in source
    assert "applySharedSurfaceClass" not in source
    assert "moveDashboardKpisToStatistics" not in app
    assert "decorateHistoricalKpis" not in app
    assert "dash-kpi-strip--dashboard" not in read(INDEX)
    assert not (STATIC / "ui-statistics.css").exists()
    assert "/ui-statistics.css" not in style

def test_statistics_reviewed_primary_and_historical_order_copy_are_locked_in_base() -> None:
    view = statistics_view()
    source = read(STATS)
    css = read(STATS_CSS)
    primary = [
        'data-dp-stats-metric="downloads"', 'data-dp-stats-metric="completed"',
        'data-dp-stats-metric="progress"', 'data-dp-stats-metric="success"',
        'data-dp-stats-metric="data"',
    ]
    positions = [view.index(item) for item in primary]
    assert positions == sorted(positions)
    historical = ["i-last-day", "i-last-week", "i-avg-duration", "i-success-rate", "i-avg-size"]
    positions = [view.index(item) for item in historical]
    assert positions == sorted(positions)
    for reviewed_copy in (
        "Last 24 Hours", "Completed downloads over the last 24 hours.",
        "Last 7 Days", "Completed downloads over the last 7 days.",
        "MEAN DOWNLOAD TIME", "LIFE-TIME SUCCESS RATE", "MEAN DOWNLOAD SIZE",
        "Share of all recorded finished downloads completed successfully.",
    ):
        assert reviewed_copy in view
    for flavor in (
        "Share of finished downloads completed successfully during the last hour.",
        "Share of finished downloads completed successfully during the last 24 hours.",
        "Share of finished downloads completed successfully during the last 7 days.",
        "Share of finished downloads completed successfully during the last 30 days.",
        "Share of finished downloads completed successfully during the last year.",
        "Share of finished downloads completed successfully across all recorded history.",
    ):
        assert flavor in source
    assert "grid-template-columns: repeat(5, minmax(0, 1fr))" in css
    assert "grid-template-rows: 18px 48px minmax(28px, auto)" in css
    assert "grid-template-rows: 18px 28px minmax(24px, auto)" in css


def test_queue_health_compatibility_surface_is_physically_removed() -> None:
    combined = chr(10).join(read(path) for path in (INDEX, APP, STATS))
    assert "i-queue-health" not in combined
    assert "i-queue-copy" not in combined
    assert "Queue Health" not in statistics_view()

def test_breakdowns_keep_reviewed_labels_adaptive_top_ten_and_two_column_behavior() -> None:
    source = read(STATS)
    css = read(STATS_CSS)
    view = statistics_view()
    for heading in ("Download Status", "File Status", "Monitor Levels", "Top Sources"):
        assert heading in view
    for label in ("Completed", "Deleted", "Error", "Missing", "Duplicate", "Info", "Warning", "Debrid Link", "Torrent File", "Magnet Link", "Unknown"):
        assert label in source
    assert "MAX_VISIBLE = 10" in source
    assert "TWO_COLUMN_THRESHOLD = 6" in source
    assert "entries.slice(0, MAX_VISIBLE)" in source
    assert "Math.ceil(visible.length / 2)" in source
    assert "grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)" in css
    assert "dp-stats-overflow" in source
    assert "dp-stats-overflow" in css


def test_completion_chart_header_is_static_and_runtime_only_hydrates_data_and_palette() -> None:
    source = read(STATS)
    css = read(STATS_CSS)
    view = statistics_view()
    for fragment in (
        'class="dp-icon dp-stats-chart-title-icon"', '/icons/dp/card-download.svg',
        'class="dp-stats-chart-heading">Completions</span>',
        'id="chart-title" class="dp-stats-chart-subtitle">Completed downloads in the last 7 days.',
    ):
        assert fragment in view
    for flavor in (
        "Completed downloads in the last hour.", "Completed downloads in the last 24 hours.",
        "Completed downloads in the last 7 days.", "Completed downloads in the last 30 days.",
        "Completed downloads in the last year.", "Completed downloads across all recorded history.",
    ):
        assert flavor in source
    assert "statisticsPurpleGradient" in source
    assert "min-height: 72px" in css
    assert "var(--dp-feature-icon-size, 51px)" in css


def test_statistics_detail_io_has_one_frontend_owner_without_wrapper() -> None:
    assert statistics_detail_io_owners() == ["ui-statistics.js"]
    source = read(STATS)
    assert "window.loadDetailedStats = loadDetailedStats;" in source
    assert "window.loadDetailedStats = wrapped" not in source


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
