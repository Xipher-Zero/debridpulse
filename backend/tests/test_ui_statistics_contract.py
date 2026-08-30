"""Accepted Statistics page contract independent of migration batch ownership."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
INDEX = STATIC / "index.html"
VERSION = ROOT / "VERSION"
STATS_ICON = STATIC / "icons" / "dp" / "statistics.svg"
ICON_MANIFEST = STATIC / "icons" / "dp" / "manifest.json"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def statistics_js() -> str:
    candidates = list(STATIC.glob("ui-statistics*.js"))
    visual = STATIC / "ui-visual-behavior-fixes.js"
    if visual.exists():
        candidates.append(visual)
    return "\n".join(read(path) for path in sorted(set(candidates)))


def statistics_css() -> str:
    candidates = list(STATIC.glob("ui-statistics*.css"))
    candidates.extend(
        path
        for path in (
            STATIC / "ui-feature-icon-contract.css",
            STATIC / "ui-panel-surface-treatment.css",
        )
        if path.exists()
    )
    return "\n".join(read(path) for path in sorted(set(candidates)))


def first_party_js_files() -> list[Path]:
    return sorted(STATIC.glob("*.js"))


def test_statistics_master_surface_period_and_primary_copy_are_locked() -> None:
    js = statistics_js()
    css = statistics_css()

    required_js = (
        "dp-statistics-master",
        "dp-stats-master-header",
        "dp-stats-master-body",
        "Historical transfer performance and completion metrics.",
        "dp-stats-period-label",
        "item.dataset.period === '7d'",
        "Downloads Added",
        "Total Data Downloaded",
        "Downloads Completed",
        "In Progress",
        "Success Rate",
        "during the last hour",
        "during the last 24 hours",
        "during the last 7 days",
        "during the last 30 days",
        "during the last year",
        "across all recorded history",
    )
    missing = [fragment for fragment in required_js if fragment not in js]
    assert not missing, f"Statistics primary contract is missing: {missing}"

    for fragment in (
        "#view-stats.active.dp-statistics-master",
        ".dp-stats-master-body",
        "overflow-y: auto",
        "font-size: 36px",
        "text-align: center",
    ):
        assert fragment in css


def test_statistics_reviewed_kpi_order_copy_and_scope_are_locked() -> None:
    js = statistics_js()
    css = statistics_css()

    assert "['downloads', 'completed', 'progress', 'success', 'data']" in js
    for value_id in (
        "i-last-day",
        "i-last-week",
        "i-avg-duration",
        "i-success-rate",
        "i-avg-size",
    ):
        assert value_id in js

    for reviewed_copy in (
        "Last 24 Hours",
        "Completed downloads over the last 24 hours.",
        "Last 7 Days",
        "Completed downloads over the last 7 days.",
        "MEAN DOWNLOAD TIME",
        "LIFE-TIME SUCCESS RATE",
        "MEAN DOWNLOAD SIZE",
        "Share of all recorded finished downloads completed successfully.",
    ):
        assert reviewed_copy in js

    for flavor in (
        "Share of finished downloads completed successfully during the last hour.",
        "Share of finished downloads completed successfully during the last 24 hours.",
        "Share of finished downloads completed successfully during the last 7 days.",
        "Share of finished downloads completed successfully during the last 30 days.",
        "Share of finished downloads completed successfully during the last year.",
        "Share of finished downloads completed successfully across all recorded history.",
    ):
        assert flavor in js

    assert "grid-template-columns: repeat(5, minmax(0, 1fr))" in css
    assert "grid-template-rows: 18px 48px minmax(28px, auto)" in css
    assert "grid-template-rows: 18px 28px minmax(24px, auto)" in css


def test_queue_health_is_not_allowed_to_become_a_visible_final_kpi() -> None:
    html = read(INDEX)
    js = statistics_js()
    combined = html + "\n" + js

    if "i-queue-health" not in combined and "Queue Health" not in combined:
        return

    assert "aria-hidden" in js
    assert "display" in js
    assert "none" in js


def test_breakdowns_keep_reviewed_labels_adaptive_top_ten_and_two_column_behavior() -> None:
    js = statistics_js()
    css = statistics_css()
    html = read(INDEX)

    for heading in ("Download Status", "File Status", "Monitor Levels", "Top Sources"):
        assert heading in html

    for label in (
        "Completed",
        "Deleted",
        "Error",
        "Missing",
        "Duplicate",
        "Info",
        "Warning",
        "Debrid Link",
        "Torrent File",
        "Magnet Link",
        "Unknown",
    ):
        assert label in js

    assert "MAX_VISIBLE = 10" in js
    assert "TWO_COLUMN_THRESHOLD = 6" in js
    assert "entries.slice(0, MAX_VISIBLE)" in js
    assert "Math.ceil(visible.length / 2)" in js
    assert "grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)" in css
    assert "dp-stats-overflow" in js
    assert "dp-stats-overflow" in css


def test_completion_chart_keeps_reviewed_header_copy_and_purple_visual_language() -> None:
    js = statistics_js()
    css = statistics_css()

    assert "heading.textContent = 'Completions'" in js
    assert "dp-stats-chart-title-icon" in js
    assert "/icons/dp/card-download.svg" in js

    for flavor in (
        "Completed downloads in the last hour.",
        "Completed downloads in the last 24 hours.",
        "Completed downloads in the last 7 days.",
        "Completed downloads in the last 30 days.",
        "Completed downloads in the last year.",
        "Completed downloads across all recorded history.",
    ):
        assert flavor in js

    assert "statisticsPurpleGradient" in js
    assert "min-height: 72px" in css
    assert "var(--dp-feature-icon-size, 51px)" in css


def test_statistics_detail_io_has_one_frontend_owner_and_no_duplicate_wrapper() -> None:
    endpoint_owners = [
        path.name
        for path in first_party_js_files()
        if "/stats/detail" in read(path)
    ]
    wrapper_owners = [
        path.name
        for path in first_party_js_files()
        if "window.loadDetailedStats = wrapped" in read(path)
    ]

    assert len(endpoint_owners) == 1, f"Statistics detail I/O owners: {endpoint_owners}"
    assert len(wrapper_owners) <= 1, f"Statistics wrapper owners: {wrapper_owners}"


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


def test_statistics_contract_keeps_backend_version_frozen() -> None:
    assert read(VERSION).strip() == "1.0.10"
