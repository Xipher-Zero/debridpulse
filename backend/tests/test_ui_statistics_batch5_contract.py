"""Reviewed Statistics Batch 5 presentation contract."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
BATCH3_RUNTIME = STATIC / "ui-statistics-batch3.js"
BATCH_RUNTIME = STATIC / "ui-statistics-batch5.js"
BATCH_CSS = STATIC / "ui-statistics-batch5.css"
SURFACES = STATIC / "ui-panel-surface-treatment.css"
PRESENTATION_LOADER = STATIC / "ui-presentation-loader.js"
ORCHESTRATOR = STATIC / "ui-statistics-orchestrator.js"
SHELL_RUNTIME = STATIC / "ui-shell-runtime.js"
SHELL_BRAND = STATIC / "ui-shell-brand.css"
STYLE_V11 = STATIC / "style-v11.css"
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
VERSION = ROOT / "VERSION"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_batch5_loads_after_batch4_and_preserves_statistics_io_ownership() -> None:
    loader = read(PRESENTATION_LOADER)
    runtime = read(BATCH_RUNTIME)

    assert "/ui-statistics-batch4.js?v=2" in loader
    assert "/ui-statistics-batch5.js?v=7" in loader
    assert loader.index("/ui-statistics-batch4.js?v=2") < loader.index("/ui-statistics-batch5.js?v=7")
    assert "debridpulse:statistics-rendered" in runtime
    assert "window.loadDetailedStats = wrapped" not in runtime
    assert "'/ui-statistics-batch5.css?v=3'" in runtime
    for forbidden in ("fetch(", "api(", "/stats/detail", "XMLHttpRequest", "EventSource", "setTimeout(", "new MutationObserver"):
        assert forbidden not in runtime


def test_statistics_presentation_chain_has_one_explicit_sequential_loader() -> None:
    loader = read(PRESENTATION_LOADER)

    expected = [
        "/ui-shell-runtime.js?v=1",
        "/ui-visual-behavior-fixes.js?v=23",
        "/ui-statistics-orchestrator.js?v=1",
        "/ui-statistics-batch3.js?v=3",
        "/ui-statistics-batch4.js?v=2",
        "/ui-statistics-batch5.js?v=7",
        "/ui-settings-page.js?v=1",
        "/ui-error-semantics.js?v=21",
    ]
    positions = [loader.index(item) for item in expected]
    assert positions == sorted(positions)
    assert "await loadRuntime(runtime);" in loader
    assert "script.async = false;" in loader


def test_kpi_rows_use_reviewed_order_and_semantic_color_families() -> None:
    runtime = read(BATCH_RUNTIME)
    css = read(BATCH_CSS)

    assert "['downloads', 'completed', 'progress', 'success', 'data']" in runtime
    for value_id in ("i-last-day", "i-last-week", "i-avg-duration", "i-success-rate", "i-avg-size"):
        assert "'" + value_id + "'" in runtime
    assert runtime.index("'i-last-day'") < runtime.index("'i-last-week'") < runtime.index("'i-avg-duration'")
    assert runtime.index("'i-avg-duration'") < runtime.index("'i-success-rate'") < runtime.index("'i-avg-size'")

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
    value_block = css[css.index(value_selector):]
    value_block = value_block[:value_block.index("}")]
    assert "font-size: 31px" in value_block


def test_batch5_is_sole_historical_kpi_presentation_owner() -> None:
    batch3 = read(BATCH3_RUNTIME)
    batch5 = read(BATCH_RUNTIME)

    assert "HISTORY_ORDER" not in batch3
    assert "i-last-day" not in batch3
    assert "i-last-week" not in batch3
    assert "i-avg-duration" not in batch3
    assert "i-success-rate" not in batch3
    assert "i-avg-size" not in batch3

    assert "HISTORY_ORDER" in batch5
    assert "Last 24 Hours" in batch5
    assert "Last 7 Days" in batch5
    assert "MEAN DOWNLOAD TIME" in batch5
    assert "LIFE-TIME SUCCESS RATE" in batch5
    assert "MEAN DOWNLOAD SIZE" in batch5
    assert "/icons/dp/heartbeat-outline.svg" in batch5


def test_primary_kpi_order_is_idempotent_and_event_driven_after_legacy_render() -> None:
    runtime = read(BATCH_RUNTIME)

    assert "const PRIMARY_ORDER = ['downloads', 'completed', 'progress', 'success', 'data'];" in runtime
    assert "debridpulse:statistics-rendered" in runtime
    assert "new MutationObserver" not in runtime
    assert "setTimeout(" not in runtime
    assert "window.loadDetailedStats = wrapped" not in runtime
    assert "currentOrder === desiredOrder" in runtime


def test_queue_health_cannot_resurface_in_final_statistics_layer() -> None:
    runtime = read(BATCH_RUNTIME)
    assert "Queue Health" in runtime
    assert "aria-hidden" in runtime
    assert "display:none !important" in runtime


def test_statistics_kpi_rows_share_title_value_flavor_vertical_anchors() -> None:
    css = read(BATCH_CSS)
    assert ".dp-stats-primary-grid .dash-kpi-label" in css
    assert ".dp-stats-primary-grid .dash-kpi-val" in css
    assert ".dp-stats-primary-grid .dash-kpi-sub" in css
    assert ".dp-stats-history-grid .dash-kpi-label" in css
    assert ".dp-stats-history-grid .dash-kpi-sub" in css


def test_success_rate_copy_distinguishes_period_and_life_time_scopes() -> None:
    runtime = read(BATCH_RUNTIME)
    assert "Share of finished downloads completed successfully during" in runtime
    assert "Share of all recorded finished downloads completed successfully." in runtime


def test_average_duration_is_owned_once_by_statistics_orchestrator() -> None:
    orchestrator = read(ORCHESTRATOR)
    batch5 = read(BATCH_RUNTIME)
    assert "i-avg-duration" in orchestrator
    assert "formatDurationCompact" in orchestrator
    assert "formatDurationCompact" not in batch5


def test_secondary_kpi_glyphs_are_centered_without_resizing_the_chip() -> None:
    css = read(BATCH_CSS)
    assert ".dp-stats-history-grid .dash-kpi-icon" in css
    assert "display: grid" in css
    assert "place-items: center" in css


def test_completion_chart_uses_standard_two_line_feature_header() -> None:
    runtime = read(BATCH_RUNTIME)
    assert "Downloads completed over the selected period." in runtime
    assert "/icons/dp/card-download.svg" in runtime


def test_breakdowns_reuse_shared_list_surface_and_five_row_cadence() -> None:
    runtime = read(BATCH_RUNTIME)
    surfaces = read(SURFACES)
    assert "dp-stats-breakdown-list" in runtime
    assert "slice(0, 10)" in runtime
    assert "dp-stats-breakdown-list" in surfaces


def test_shell_branding_is_owned_outside_statistics() -> None:
    runtime = read(BATCH_RUNTIME)
    shell_runtime = read(SHELL_RUNTIME)
    shell_brand = read(SHELL_BRAND)

    assert "normalizeShellBranding" not in runtime
    assert "sidebar-version" not in runtime
    assert "normalizeShellBranding" in shell_runtime
    assert "#sidebar-version.dp-app-version" in shell_brand


def test_ci_syntax_checks_batch5_runtime_and_version_stays_frozen() -> None:
    workflow = read(WORKFLOW)
    assert "node --check frontend/static/ui-statistics-batch5.js" in workflow
    assert read(VERSION).strip() == "1.0.10"
