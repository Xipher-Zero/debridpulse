from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "backend" / "tests"


def read(name: str) -> str:
    return (TESTS / name).read_text(encoding="utf-8")


def write(name: str, source: str) -> None:
    (TESTS / name).write_text(source, encoding="utf-8")


def replace_test(source: str, name: str, replacement: str) -> str:
    pattern = re.compile(rf"^def {re.escape(name)}\([^\n]*\).*?(?=^def |\Z)", re.MULTILINE | re.DOTALL)
    source, count = pattern.subn(replacement.rstrip() + "\n\n", source, count=1)
    if count != 1:
        raise RuntimeError(f"could not replace {name}: {count}")
    return source


# Dashboard: final composition is static/index + app.js, not ui-runtime.js.
name = "test_ui_dashboard_contract.py"
s = read(name).replace('RUNTIME = STATIC / "ui-runtime.js"\n', '')
s = replace_test(s, "test_dashboard_keeps_one_primary_metric_row_and_statistics_owns_history_directly", '''def test_dashboard_keeps_one_primary_metric_row_and_statistics_owns_history_directly() -> None:
    dashboard = DASHBOARD_CSS.read_text(encoding="utf-8")
    statistics = STATISTICS_CSS.read_text(encoding="utf-8")
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "grid-template-columns: repeat(6" in dashboard
    assert "#view-dashboard .dash-kpi-strip--dashboard" not in dashboard
    assert "#view-stats .dp-stats-history-grid" not in dashboard
    assert "#view-stats .dp-stats-history-grid" in statistics
    assert "moveDashboardKpisToStatistics" not in app
    assert "decorateHistoricalKpis" not in app
    assert 'class="dash-kpi-strip dash-kpi-strip--dashboard"' not in index
    stats_view = index[index.index('<!-- Statistics -->'):index.index('<!-- Help -->')]
    assert 'class="dash-kpi-strip dp-stats-history-grid"' in stats_view''')
s = replace_test(s, "test_dashboard_and_statistics_use_canonical_custom_semantic_assets", '''def test_dashboard_and_statistics_use_canonical_custom_semantic_assets() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    dashboard_view = index[index.index('id="view-dashboard"'):index.index('id="view-torrents"')]
    dashboard_assets = (
        "card-download.svg", "card-checkmark.svg", "card-play.svg", "card-clock.svg",
        "card-error.svg", "card-disk.svg", "card-link.svg", "card-document-stack.svg",
    )
    missing = [asset for asset in dashboard_assets if asset not in dashboard_view]
    assert not missing, f"Dashboard is missing canonical assets: {missing}"
    statistics_assets = ("heartbeat-outline.svg", "calendar-24.svg", "calendar-7.svg", "clock-outline.svg", "cube.svg")
    stats_view = index[index.index('<!-- Statistics -->'):index.index('<!-- Help -->')]
    missing = [asset for asset in statistics_assets if asset not in stats_view]
    assert not missing, f"Statistics is missing canonical assets: {missing}"
    assert "verified-badge.svg" not in stats_view''')
s = replace_test(s, "test_quick_add_preserves_existing_functional_controls", '''def test_quick_add_preserves_existing_functional_controls() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    css = DASHBOARD_CSS.read_text(encoding="utf-8")
    dashboard_view = index[index.index('id="view-dashboard"'):index.index('id="view-torrents"')]
    assert "q-transfer-input" in dashboard_view
    assert "btn-recover-all" in dashboard_view
    assert 'data-default-label="Recover All"' in dashboard_view
    assert 'data-dp-lucide="refresh"' in dashboard_view
    assert "btn-add-transfer" in css
    assert "addDashboardEntries" not in app
    assert "function recoverAll(" in app''')
write(name, s)

# Activity Log: primary E1 makes its structure and refresh control static.
name = "test_ui_activity_log_page_contract.py"
s = read(name)
s = replace_test(s, "test_activity_log_keeps_approved_content_but_uses_structural_runtime", '''def test_activity_log_keeps_approved_content_with_direct_structural_owners() -> None:
    css = read("ui-activity-log-page.css")
    index = read("index.html")
    app = read("app.js")
    manifest = read("icons/dp/manifest.json")
    view = index[index.index('id="view-events"'):index.index('<!-- Statistics -->')]
    assert "dp-activity-card" in view
    assert "document.svg" in view
    assert "Activity Log" in view
    assert "Everything DebridPulse thought was worth mentioning." in view
    assert "Refresh activity log" in view
    assert 'data-dp-lucide="refresh"' in view
    assert "function loadEvents(" in app
    assert "function filterEvents(" in app
    assert '"document": "document.svg"' in manifest
    required = (".dp-activity-card", ".dp-activity-card-title", ".dp-activity-search-band", ".dp-activity-list", ".dp-activity-row", ".dp-activity-message", ".dp-activity-transfer", ".dp-activity-time")
    missing = [selector for selector in required if selector not in css]
    assert not missing, f"Activity rebuild is missing structural selectors: {missing}"''')
write(name, s)

# Statistics: final structure is already direct static ownership.
name = "test_ui_statistics_contract.py"
s = read(name).replace('RUNTIME = STATIC / "ui-runtime.js"\n', '')
s = replace_test(s, "test_statistics_composition_is_direct_static_owner_not_runtime_convergence", '''def test_statistics_composition_is_direct_static_owner_not_runtime_convergence() -> None:
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
    assert "/ui-statistics.css" not in style''')
s = replace_test(s, "test_queue_health_compatibility_surface_is_physically_removed", '''def test_queue_health_compatibility_surface_is_physically_removed() -> None:
    combined = "\\n".join(read(path) for path in (INDEX, APP, STATS))
    assert "i-queue-health" not in combined
    assert "i-queue-copy" not in combined
    assert "Queue Health" not in statistics_view()''')
write(name, s)

# Lucide: canonical glyph authority remains operator-title.js; page consumers are
# direct index/app owners after E1 rather than two presentation correction runtimes.
name = "test_ui_lucide_canonicalization_contract.py"
s = read(name).replace('PRESENTATION_RUNTIME = STATIC / "ui-runtime.js"\n', '').replace('DOWNLOADS_RUNTIME = STATIC / "ui-downloads-runtime.js"\n', '')
s = replace_test(s, "test_page_runtimes_consume_canonical_icons_without_private_svg_maps", '''def test_page_owners_consume_canonical_icons_without_private_svg_maps() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    shell = SHELL_RUNTIME.read_text(encoding="utf-8")
    for icon in ("refresh", "chevronLeft", "chevronRight", "trash2", "pause", "play"):
        assert f"{icon}:" in shell
    assert 'data-dp-lucide="refresh"' in index
    assert 'data-dp-lucide="pause"' in index
    assert 'data-dp-lucide="play"' in index
    assert 'data-dp-lucide="trash2"' in index
    assert "window.DPIcons.svg" in app
    assert "const paths =" not in app
    assert not (STATIC / "ui-runtime.js").exists()
    assert not (STATIC / "ui-downloads-runtime.js").exists()''')
write(name, s)

# Sparkline: prove canonical app owner and physical absence of repair runtime.
name = "test_dashboard_sparkline_canonical_owner.py"
s = read(name)
s = replace_test(s, "test_dashboard_sparkline_state_is_owned_by_load_stats_not_presentation_runtime", '''def test_dashboard_sparkline_state_is_owned_by_load_stats_not_presentation_runtime():
    app = (STATIC / "app.js").read_text()
    assert "recordDashboardMetricHistory({" in app
    assert "debridpulse.dashboard.metric-history.v2" in app
    assert "dashboardMonotoneSparkPath" in app
    assert " C ${fmt(cp1.x)}" in app
    assert "const intervals = points.slice(0, -1).map" in app
    assert "left.slope * right.slope <= 0" in app
    assert "endpointTangent" in app
    assert "clamp(start.y + (tangents[index] * width) / 3" in app
    assert "dashboardSmoothSparkPath" not in app
    assert "const tension = 0.82;" not in app
    assert not (STATIC / "ui-runtime.js").exists()''')
write(name, s)

# Shell: unused historical runtime constant must not survive E1 contract source.
name = "test_ui_shell_contract.py"
s = read(name).replace('PRESENTATION_RUNTIME = STATIC / "ui-runtime.js"\n', '')
write(name, s)

# Shared utility geometry is now visible directly in canonical markup.
name = "test_ui_feature_icon_contract.py"
s = read(name)
s = replace_test(s, "test_dashboard_recover_all_uses_exact_activity_refresh_utility_geometry", '''def test_dashboard_recover_all_uses_exact_activity_refresh_utility_geometry() -> None:
    index = read("index.html")
    controls = read("ui-utility-controls.css")
    dashboard = index[index.index('id="view-dashboard"'):index.index('id="view-torrents"')]
    events = index[index.index('id="view-events"'):index.index('<!-- Statistics -->')]
    assert 'id="btn-recover-all"' in dashboard
    assert 'data-dp-lucide="refresh"' in dashboard
    assert 'class="btn btn-ghost btn-sm dp-activity-refresh"' in events
    assert 'data-dp-lucide="refresh"' in events
    assert "#btn-recover-all .dp-utility-icon" in controls
    assert ".dp-activity-refresh .dp-utility-icon" in controls''')
write(name, s)

# Details/Downloads: bulk count + clear control are static/app owners after E1.
name = "test_ui_detail_overlay_cleanup_contract.py"
s = read(name)
s = replace_test(s, "test_bulk_toolbar_right_side_owns_count_and_clear_selection", '''def test_bulk_toolbar_right_side_owns_count_and_clear_selection() -> None:
    app = read("app.js")
    page = read("ui-downloads-page.css")
    index = read("index.html")
    view = index[index.index('id="view-torrents"'):index.index('<!-- Events -->')]
    assert "_selectedIds.size + ' Selected'" in app
    assert view.index("bulkAction('pause',this)") < view.index("bulkAction('resume',this)")
    assert view.index("bulkAction('resume',this)") < view.index("bulkAction('reset',this)")
    assert view.index("bulkAction('reset',this)") < view.index("bulkAction('delete',this)")
    assert 'class="dp-downloads-bulk-status"' in view
    assert 'id="bulk-count" class="dp-downloads-bulk-count"' in view
    assert 'data-default-label="Clear Selections"' in view
    assert 'data-dp-lucide="x"' in view
    assert 'onclick="clearSelection()"' in view
    assert "gap: 10px;" in page
    assert not (STATIC / "ui-downloads-runtime.js").exists()''')
write(name, s)
