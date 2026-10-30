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
    style = read(STATIC / "style.css")
    for fragment in ('class="view card dp-statistics-master" id="view-stats"', 'class="card-header dp-stats-master-header"', 'class="card-body dp-stats-master-body"', 'class="scard dp-stats-chart dp-list-workspace-surface"', 'class="dp-stats-breakdown-grid"', ">By The Numbers<", ">Vibes are not a performance metric.<"):
        assert fragment in view
    # The shorter wordings the header draws where the full ones do not fit.
    assert 'data-short="Statistics" data-shortest="Stats">By The Numbers<' in view
    assert 'data-short="Vibes aren\'t metrics." data-shortest="Just data.">Vibes are not a performance metric.<' in view
    assert "By the Numbers" not in view
    for retired in ("dp-stats-history-grid", "dp-stats-row-separator", "Overall &amp; Rolling Metrics", "dash-kpi"):
        assert retired not in view
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


def test_statistics_period_labels_use_canonical_debridpulse_copy_without_changing_values() -> None:
    view = statistics_view()
    expected = (
        ('1h', '1Hour'),
        ('24h', '1Day'),
        ('7d', '7Days'),
        ('30d', '30Days'),
        ('1y', '1Year'),
        ('all', 'All Time'),
    )
    positions = []
    for period, label in expected:
        fragment = f'data-period="{period}" onclick="setStatsPeriod(this)">{label}</div>'
        assert fragment in view
        positions.append(view.index(fragment))
    assert positions == sorted(positions)
    for legacy_label in ('>1h</div>', '>24h</div>', '>7d</div>', '>7Day</div>', '>30d</div>', '>1y</div>', '>All time</div>'):
        assert legacy_label not in view


def test_statistics_has_exactly_the_two_period_metrics_and_no_retired_cards_or_writers() -> None:
    view = statistics_view()
    source = read(STATS)
    app = read(APP)
    css = read(STATS_CSS)
    assert re.findall(r'data-dp-stats-metric="([a-z]+)"', view) == ["downloads", "data"]
    assert "PRIMARY_ORDER = Object.freeze(['downloads', 'data'])" in source
    # The data card: Total Data Downloaded from 780px, Data Transferred below it (copy only).
    assert ('<div class="metric-label"><span class="dp-stats-label-wide">Total Data Downloaded</span>'
            '<span class="dp-stats-label-handheld">Data Transferred</span></div>') in view
    assert "data: fmtSize(totals.completed_size || 0)" in source
    # The retired cards, their writers and their presentation are gone, not hidden.
    combined = chr(10).join((view, source, app, css))
    for retired in ("i-last-day", "i-last-week", "i-avg-duration", "i-success-rate", "i-avg-size",
                    'data-dp-stats-metric="completed"', 'data-dp-stats-metric="progress"', 'data-dp-stats-metric="success"',
                    "SUCCESS_FLAVOR", "formatCompactDuration", "fmtDuration", "dp-stats-history-grid", "dp-stats-kpi-",
                    "dp-stats-row-separator", "Downloads Completed", "Success Rate", "MEAN DOWNLOAD", "LIFETIME SUCCESS"):
        assert retired not in combined, retired
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in css
    assert "repeat(5, minmax(0, 1fr))" not in css
    assert "grid-template-rows: 18px 48px minmax(28px, auto)" in css


def test_statistics_period_has_one_authority_and_a_handheld_control_projecting_it() -> None:
    view = statistics_view()
    source = read(STATS)
    app = read(APP)
    css = read(STATS_CSS)
    # setStatsPeriod is the one way the period changes: the active filter, then its one load.
    assert "function setStatsPeriod" not in app
    assert "window.setStatsPeriod = setStatsPeriod;" in source
    assert source.count("loadDetailedStats(tab.dataset.period)") == 1
    assert "const active = document.querySelector('#stats-period-tabs .ftab.active');" in source
    # The handheld control: a popover listbox read from the filters each time it opens.
    assert ('<button type="button" id="stats-period" class="btn btn-ghost dp-stats-period-trigger" popovertarget="stats-period-menu" '
            'aria-haspopup="listbox" aria-expanded="false" aria-label="Statistics period">') in view
    assert 'data-dp-lucide="clock"' in view
    assert '<div id="stats-period-menu" class="dp-dropdown-menu dp-stats-period-menu" popover="auto" role="listbox" aria-label="Statistics period"></div>' in view
    assert "if (tab) setStatsPeriod(tab);" in source
    # From 780px the filters, below it the control: never both.
    assert "#view-stats .dp-stats-period-trigger {\n  display: none;\n}" in css
    assert "  #view-stats #stats-period-tabs {\n    display: none;\n  }" in css


def test_queue_health_compatibility_surface_is_physically_removed() -> None:
    combined = chr(10).join(read(path) for path in (INDEX, APP, STATS))
    assert "i-queue-health" not in combined
    assert "i-queue-copy" not in combined
    assert "Queue Health" not in statistics_view()

def test_breakdowns_keep_reviewed_labels_adaptive_top_ten_and_two_column_behavior() -> None:
    source = read(STATS)
    css = read(STATS_CSS)
    view = statistics_view()
    for heading in ("Transfer Disposition", "File Status", "Events Severity", "Transfer Methods"):
        assert heading in view
    for old in ("Monitor Levels", "Most Used Sources"):
        assert old not in view and old not in source
    for label in ("Completed", "Deleted", "Error", "Missing", "Duplicate", "Unknown"):
        assert label in source
    # Events Severity: fixed order and its own labels; the stored values stay.
    assert "[['error', 'Errors'], ['warning', 'Warnings'], ['info', 'Informational']]" in source
    # Transfer Methods: the server names each method; no submission-channel labels remain.
    for old in ("Debrid Link", "Magnet Link", "Uploaded File", "Unknown Source", "SOURCE_LABELS"):
        assert old not in source
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


def test_statistics_is_live_through_the_one_stream_with_bounded_reads_and_no_toggle() -> None:
    stats = read(STATS)
    app = read(APP)
    index = read(INDEX)
    # One application EventSource; app.js only forwards neutral hints.
    assert app.count("new EventSource(") == 1
    for forwarded in ("'stats_changed'", "'statistics_changed'", "'ping'"):
        listener = app[app.index(forwarded):]
        listener = listener[:listener.index("\n        );")]
        assert "debridpulse:statistics-invalidated" in listener or "debridpulse:pulse-heartbeat" in listener, forwarded
        assert "loadDetailedStats" not in listener and "/stats/detail" not in listener, forwarded
    # 1.0.14: the heartbeat no longer re-reads /stats for Statistics -- the
    # Overall & Rolling row it aged is retired; the ping is only forwarded.
    ping = app[app.index("'ping'"):]
    ping = ping[:ping.index("\n        );")]
    assert "loadStats()" not in ping and "view-stats" not in ping
    # The Statistics owner opens no stream, runs no timer loop, stores nothing.
    for absent in ("EventSource", "setInterval", "localStorage", "sessionStorage"):
        assert absent not in stats, absent
    for listened in ("debridpulse:statistics-invalidated", "debridpulse:pulse-connected",
                     "debridpulse:pulse-heartbeat", "debridpulse:navigation"):
        assert f"document.addEventListener('{listened}'" in stats, listened
    # Admission: one request in flight, measured spacing, a queued re-read never lost.
    assert "REFRESH_SPACING_MS = 2000, REFRESH_SPACING_MAX_MS = 10000, LATENCY_SPACING_FACTOR = 5" in stats
    assert "if (inflight) {" in stats and "if (queued) {" in stats
    # A live re-read moves the existing chart; only an explicit load draws it anew.
    assert "if (live && canvas._ci) { updateCompletionChart(canvas._ci, daily); return; }" in stats
    live = stats[stats.index("function updateCompletionChart("):stats.index("function renderCompletionChart(")]
    assert "destroy" not in live and "new Chart" not in live
    assert "reducedMotion.matches" in live and "chart.update('none')" in live
    # No live toggle or control was added to the page.
    view = index[index.index('id="view-stats"'):]
    view = view[:view.index('<!--', 1)] if '<!--' in view[1:] else view
    assert "Auto Refresh" not in view and 'role="switch"' not in view
