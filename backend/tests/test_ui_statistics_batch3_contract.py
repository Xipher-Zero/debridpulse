"""Reviewed Statistics Batch 3 presentation contract."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
BATCH_RUNTIME = STATIC / "ui-statistics-batch3.js"
BATCH_CSS = STATIC / "ui-statistics-batch3.css"
PRESENTATION_LOADER = STATIC / "ui-presentation-loader.js"
SHELL_RUNTIME = STATIC / "ui-shell-runtime.js"
FAVICON = STATIC / "favicon.svg"
SHELL_LOGO = STATIC / "logo.svg"
VERSION = ROOT / "VERSION"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_batch3_runtime_is_loaded_after_orchestrator_without_replacing_core_statistics_semantics() -> None:
    loader = read(PRESENTATION_LOADER)
    runtime = read(BATCH_RUNTIME)

    assert "/ui-statistics-orchestrator.js?v=1" in loader
    assert "/ui-statistics-batch3.js?v=3" in loader
    assert loader.index("/ui-statistics-orchestrator.js?v=1") < loader.index("/ui-statistics-batch3.js?v=3")
    assert "debridpulse:statistics-rendered" in runtime
    assert "window.loadDetailedStats = wrapped" not in runtime
    assert "api('GET', '/stats/detail" not in runtime
    assert 'api("GET", "/stats/detail' not in runtime
    assert "setTimeout(" not in runtime


def test_browser_tab_uses_original_compact_logo_while_shell_uses_reviewed_vector_mark() -> None:
    shell = read(SHELL_RUNTIME)
    favicon = read(FAVICON)
    shell_logo = read(SHELL_LOGO)

    assert "vectorIcon.href = '/favicon.svg?v=6'" in shell
    assert "icon32.remove()" in shell
    assert "logo.setAttribute('src', '/logo.svg?v=7')" in shell
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


def test_batch3_uses_explicit_statistics_lifecycle_instead_of_dom_observers() -> None:
    runtime = read(BATCH_RUNTIME)

    assert "function applyBatch3(period)" in runtime
    assert "document.addEventListener('debridpulse:statistics-rendered'" in runtime
    assert "new MutationObserver" not in runtime
    assert "observePrimaryMetrics" not in runtime
    assert "settle" not in runtime


def test_historical_kpi_order_labels_and_icons_are_not_owned_by_batch3() -> None:
    runtime = read(BATCH_RUNTIME)

    # Batch 5 is the sole owner. Keeping these writers out of Batch 3 prevents
    # last-writer-wins races between old and final presentation layers.
    for forbidden in (
        "i-queue-health",
        "i-last-day",
        "i-last-week",
        "i-success-rate",
        "i-avg-duration",
        "i-avg-size",
        "dp-stats-history-compat",
        "dp-stats-kpi-success",
        "heartbeat-outline.svg",
        "normalizeDurationValue",
    ):
        assert forbidden not in runtime


def test_secondary_kpi_visual_geometry_remains_available_for_final_owner() -> None:
    css = read(BATCH_CSS)

    assert "grid-template-columns: repeat(5, minmax(0, 1fr))" in css
    assert ".dp-stats-history-compat" in css
    assert "display: none !important" in css
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
    index = read(STATIC / "index.html")

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
