"""Reviewed Statistics Batch 3 presentation contract."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
BATCH_RUNTIME = STATIC / "ui-statistics-batch3.js"
BATCH_CSS = STATIC / "ui-statistics-batch3.css"
THEME_BOOTSTRAP = STATIC / "ui-theme-bootstrap.js"
INDEX = STATIC / "index.html"
FAVICON = STATIC / "favicon.svg"
SHELL_LOGO = STATIC / "logo.svg"
VERSION = ROOT / "VERSION"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_batch3_runtime_is_loaded_without_replacing_core_statistics_semantics() -> None:
    bootstrap = read(THEME_BOOTSTRAP)
    runtime = read(BATCH_RUNTIME)

    assert "/ui-statistics-batch3.js?v=1" in bootstrap
    assert "data-dp-statistics-batch3" in bootstrap
    assert "const result = await previous.call(this, resolved)" in runtime
    assert "window.loadDetailedStats = wrapped" in runtime
    assert "api('GET', '/stats/detail" not in runtime
    assert 'api("GET", "/stats/detail' not in runtime


def test_browser_tab_uses_original_compact_logo_while_shell_uses_reviewed_vector_mark() -> None:
    bootstrap = read(THEME_BOOTSTRAP)
    favicon = read(FAVICON)
    shell_logo = read(SHELL_LOGO)

    assert "vectorIcon.href = '/favicon.svg?v=6'" in bootstrap
    assert "icon32.remove()" in bootstrap
    assert "logo.setAttribute('src', '/logo.svg?v=7')" in bootstrap
    assert 'viewBox="0 0 512 512"' in shell_logo
    assert 'stroke="url(#dp-outline)"' in shell_logo
    assert 'viewBox="0 0 64 64"' in favicon
    assert 'transform="scale(.125)"' in favicon
    assert 'linearGradient id="field"' in favicon
    assert 'linearGradient id="mark"' in favicon
    assert "dp-outline" not in favicon


def test_primary_statistics_cards_are_centered_larger_and_use_period_aware_copy() -> None:
    runtime = read(BATCH_RUNTIME)
    css = read(BATCH_CSS)

    required_copy = (
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
    missing = [fragment for fragment in required_copy if fragment not in runtime]
    assert not missing, f"Primary Statistics copy is missing: {missing}"

    assert "font-size: 36px" in css
    assert "text-align: center" in css
    assert "align-items: center" in css
    assert "justify-content: center" in css


def test_queue_health_is_visually_removed_and_success_rate_leads_with_heart_icon() -> None:
    runtime = read(BATCH_RUNTIME)
    css = read(BATCH_CSS)

    assert "dp-stats-history-compat" in runtime
    assert "queueCard.hidden = true" in runtime
    assert "[success, day, week, duration, size]" in runtime
    assert "/icons/dp/heartbeat-outline.svg" in runtime
    assert "grid-template-columns: repeat(5, minmax(0, 1fr))" in css
    assert ".dp-stats-history-compat" in css
    assert "display: none !important" in css
    assert "Queued Downloads" not in runtime


def test_secondary_kpis_use_reviewed_option_b_copy_and_normalized_units() -> None:
    runtime = read(BATCH_RUNTIME)

    required = (
        "Share of finished downloads completed successfully.",
        "Completed downloads over the last 24 hours.",
        "Completed downloads over the last 7 days.",
        "Mean completion time for finished downloads.",
        "Mean size of completed downloads.",
        "Average Duration",
        "Average Size",
        "Last 24 Hours",
        "Last 7 Days",
        "normalizeDurationValue",
    )
    missing = [fragment for fragment in required if fragment not in runtime]
    assert not missing, f"Secondary KPI wording/formatting is missing: {missing}"


def test_secondary_kpi_icons_are_large_and_upper_left_while_text_remains_centered() -> None:
    css = read(BATCH_CSS)

    assert "top: 12px" in css
    assert "left: 12px" in css
    assert "width: 32px !important" in css
    assert "height: 32px !important" in css
    assert ".dash-kpi-val" in css
    assert ".dash-kpi-lbl" in css
    assert ".dash-kpi-sub" in css


def test_bottom_breakdowns_use_human_labels_consistent_counts_and_centered_headers() -> None:
    runtime = read(BATCH_RUNTIME)
    css = read(BATCH_CSS)
    index = read(INDEX)

    # The four card headings are structural markup owned by index.html. Batch 3
    # owns only the humanized row-label translations applied after app.js renders
    # the authoritative statistics data into those bodies.
    for heading in (
        "Download Status",
        "File Status",
        "Monitor Levels",
        "Top Sources",
    ):
        assert heading in index

    row_labels = (
        "Completed",
        "Deleted",
        "Error",
        "Missing",
        "Duplicate",
        "Info",
        "Warning",
    )
    missing = [fragment for fragment in row_labels if fragment not in runtime]
    assert not missing, f"Breakdown row wording is missing: {missing}"
    assert "text-align: center" in css


def test_top_sources_translate_all_supported_source_codes() -> None:
    runtime = read(BATCH_RUNTIME)

    for label in (
        "Debrid Link",
        "Torrent File",
        "Magnet Link",
        "Unknown",
    ):
        assert label in runtime


def test_statistics_batch3_keeps_backend_version_frozen() -> None:
    assert read(VERSION).strip() == "1.0.10"