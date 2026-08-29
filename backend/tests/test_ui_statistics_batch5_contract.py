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
        "/ui-settings-page.js?v=3",
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
    assert value_selector in css
    value_segment = css[css.index(value_selector):].split('}', 1)[0]
    assert "color: var(--c) !important" in value_segment


def test_batch5_is_sole_historical_kpi_presentation_owner() -> None:
    runtime = read(BATCH_RUNTIME)
    batch3 = read(BATCH3_RUNTIME)

    for value_id in ("i-last-day", "i-last-week", "i-avg-duration", "i-success-rate", "i-avg-size"):
        assert value_id in runtime
        assert value_id not in batch3

    for reviewed_copy in (
        "Last 24 Hours",
        "Completed downloads over the last 24 hours.",
        "Last 7 Days",
        "Completed downloads over the last 7 days.",
        "MEAN DOWNLOAD TIME",
        "LIFE-TIME SUCCESS RATE",
        "MEAN DOWNLOAD SIZE",
    ):
        assert reviewed_copy in runtime

    for class_name in (
        "dp-stats-kpi-day",
        "dp-stats-kpi-week",
        "dp-stats-kpi-duration",
        "dp-stats-kpi-success",
        "dp-stats-kpi-size",
    ):
        assert class_name in runtime

    assert "/icons/dp/heartbeat-outline.svg" in runtime


def test_primary_kpi_order_is_idempotent_and_event_driven_after_legacy_render() -> None:
    runtime = read(BATCH_RUNTIME)

    assert "const alreadyOrdered = ordered.every" in runtime
    assert "if (!alreadyOrdered) ordered.forEach" in runtime
    assert "document.addEventListener('debridpulse:statistics-rendered'" in runtime
    assert "normalizePrimaryOrder();" in runtime
    assert "normalizeSuccessRateCopy(period);" in runtime


def test_queue_health_cannot_resurface_in_final_statistics_layer() -> None:
    runtime = read(BATCH_RUNTIME)

    assert "function suppressQueueHealth()" in runtime
    assert "historicalCard('i-queue-health')" in runtime
    assert "queue.hidden = true" in runtime
    assert "queue.setAttribute('aria-hidden', 'true')" in runtime
    assert "queue.style.setProperty('display', 'none', 'important')" in runtime
    assert "suppressQueueHealth();" in runtime


def test_statistics_kpi_rows_share_title_value_flavor_vertical_anchors() -> None:
    css = read(BATCH_CSS)
    marker = "/* ── Shared KPI vertical anchors"
    assert marker in css
    alignment = css.split(marker, 1)[1]

    assert "grid-template-rows: 18px 48px minmax(28px, auto) !important;" in alignment
    assert "padding: 20px 16px 14px !important;" in alignment
    assert "#detail-stat-cards :is(.metric-label, .stat-label)" in alignment
    assert "#detail-stat-cards :is(.metric-value, .stat-value)" in alignment
    assert "#detail-stat-cards :is(.metric-sub, .stat-sub)" in alignment

    assert "grid-template-rows: 18px 28px minmax(24px, auto) !important;" in alignment
    assert "padding: 14px 14px 12px !important;" in alignment

    label = alignment[alignment.index(".dp-stats-history-grid .dash-kpi-lbl"):].split("}", 1)[0]
    value = alignment[alignment.index(".dp-stats-history-grid .dash-kpi-val"):].split("}", 1)[0]
    sub = alignment[alignment.index(".dp-stats-history-grid .dash-kpi-sub"):].split("}", 1)[0]
    assert "grid-row: 1 !important;" in label
    assert "grid-row: 2 !important;" in value
    assert "grid-row: 3 !important;" in sub
    assert "align-content: start !important;" in alignment


def test_success_rate_copy_distinguishes_period_and_life_time_scopes() -> None:
    runtime = read(BATCH_RUNTIME)

    for flavor in (
        "Share of finished downloads completed successfully during the last hour.",
        "Share of finished downloads completed successfully during the last 24 hours.",
        "Share of finished downloads completed successfully during the last 7 days.",
        "Share of finished downloads completed successfully during the last 30 days.",
        "Share of finished downloads completed successfully during the last year.",
        "Share of finished downloads completed successfully across all recorded history.",
    ):
        assert flavor in runtime

    assert "LIFE-TIME SUCCESS RATE" in runtime
    assert "Share of all recorded finished downloads completed successfully." in runtime
    assert "normalizeSuccessRateCopy(period)" in runtime


def test_average_duration_is_owned_once_by_statistics_orchestrator() -> None:
    orchestrator = read(ORCHESTRATOR)
    runtime = read(BATCH_RUNTIME)

    assert "function formatCompactDuration(seconds)" in orchestrator
    assert "Math.max(1, Math.round(value / 60))" in orchestrator
    assert "parts.push(days + 'D')" in orchestrator
    assert "parts.push(hours + 'H')" in orchestrator
    assert "parts.push(minutes + 'M')" in orchestrator
    assert "return parts.join(' ')" in orchestrator
    assert "window.fmtDuration = formatCompactDuration" in orchestrator
    assert "formatCompactDuration" not in runtime


def test_secondary_kpi_glyphs_are_centered_without_resizing_the_chip() -> None:
    css = read(BATCH_CSS)

    assert ".dp-stats-history-grid .dp-kpi-icon > .dp-icon" in css
    assert "top: 50% !important" in css
    assert "left: 50% !important" in css
    assert "transform: translate(-50%, -50%) !important" in css
    assert "--dp-icon-frame-size" not in css


def test_completion_chart_uses_standard_two_line_feature_header() -> None:
    runtime = read(BATCH_RUNTIME)
    css = read(BATCH_CSS)

    assert "heading.textContent = 'Completions'" in runtime
    assert "dp-stats-chart-title-icon" in runtime
    assert "'/icons/dp/card-download.svg'" in runtime
    assert "chartTitle.className = 'dp-stats-chart-subtitle'" in runtime
    for flavor in (
        "Completed downloads in the last hour.",
        "Completed downloads in the last 24 hours.",
        "Completed downloads in the last 7 days.",
        "Completed downloads in the last 30 days.",
        "Completed downloads in the last year.",
        "Completed downloads across all recorded history.",
    ):
        assert flavor in runtime

    assert "min-height: 72px !important" in css
    assert "width: var(--dp-feature-icon-size, 51px) !important" in css
    assert ".dp-stats-chart .scard-header::before" in css
    assert "content: none !important" in css


def test_breakdowns_reuse_shared_list_surface_and_five_row_cadence() -> None:
    runtime = read(BATCH_RUNTIME)
    css = read(BATCH_CSS)
    surfaces = read(SURFACES)
    overlay = read(STYLE_V11)

    assert "dp-list-workspace-surface" in surfaces
    assert ".dp-list-workspace-surface" in surfaces
    assert "chartCard.classList.add('dp-list-workspace-surface')" in runtime
    for detail_id in (
        "detail-torrent-status",
        "detail-file-status",
        "detail-event-levels",
        "detail-sources",
    ):
        assert detail_id in runtime
    assert "card.classList.add('dp-list-workspace-surface')" in runtime
    assert "line-height: 20px !important" in css
    assert "min-height: 20px !important" in css
    assert "/ui-panel-surface-treatment.css?v=22" in overlay


def test_shell_branding_is_owned_outside_statistics() -> None:
    runtime = read(BATCH_RUNTIME)
    css = read(BATCH_CSS)
    shell_runtime = read(SHELL_RUNTIME)
    shell_css = read(SHELL_BRAND)

    for forbidden in ("normalizeShellBranding", "sidebar-version", "/logo.svg?v=7"):
        assert forbidden not in runtime
    assert "#sidebar .logo-name" not in css
    assert "#sidebar-version.dp-app-version" not in css

    assert "function normalizeShellBranding()" in shell_runtime
    assert "'/logo.svg?v=7'" in shell_runtime
    assert "version.classList.add('dp-app-version')" in shell_runtime
    assert "document.body.appendChild(version)" in shell_runtime

    selector = "body.dp-v11-structural > #sidebar-version.dp-app-version"
    assert selector in shell_css
    segment = shell_css[shell_css.index(selector):].split('}', 1)[0]
    assert "position: fixed !important" in segment
    assert "right: max(" in segment
    assert "var(--dp-shell-x)" in segment
    assert "var(--dp-shell-sidebar)" in segment
    assert "var(--dp-content-max-width)" in segment
    assert "bottom: 7px !important" in segment
    assert "text-align: right" in segment
    assert "font-size: 9px" in segment

    assert "#sidebar .logo-name" in shell_css
    assert "font-size: 20px" in shell_css
    assert "body.light.dp-v11-structural #sidebar .logo-icon" in shell_css
    light_logo = shell_css[shell_css.index("body.light.dp-v11-structural #sidebar .logo-icon"):].split('}', 1)[0]
    assert "border: 0 !important" in light_logo
    assert "outline: 0 !important" in light_logo
    assert "background: transparent !important" in light_logo
    assert "box-shadow: none !important" in light_logo
    assert "drop-shadow" in light_logo


def test_ci_syntax_checks_batch5_runtime_and_version_stays_frozen() -> None:
    workflow = read(WORKFLOW)
    assert "node --check frontend/static/ui-statistics-batch5.js" in workflow
    assert "node --check frontend/static/ui-statistics-orchestrator.js" in workflow
    assert read(VERSION).strip() == "1.0.10"