from __future__ import annotations

from pathlib import Path
import re
import sys

repo = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
static = repo / "frontend" / "static"
tests = repo / "backend" / "tests"

# Remove the two now-obsolete Dashboard hiding rules. The KPI band is no longer
# born under Dashboard, so there is nothing to hide before a runtime reparents it.
dashboard = static / "ui-dashboard.css"
text = dashboard.read_text(encoding="utf-8")
text = re.sub(
    r"\n/\* The former second Dashboard KPI row is physically moved into Statistics by\n"
    r"   ui-runtime\.js\. If the runtime has not initialized yet, avoid a flash of the\n"
    r"   old dense Dashboard arrangement\. \*/\n"
    r"#view-dashboard \.dash-kpi-strip--dashboard \{\n  display: none !important;\n\}\n",
    "\n",
    text,
    count=1,
)
text = text.replace("\nbody.dp-v11-structural #view-dashboard .dash-kpi-strip--dashboard { display: none !important; }\n", "\n")
dashboard.write_text(text, encoding="utf-8")

# Cache-generation assertions must follow the intentional Statistics owner bump.
for path in tests.glob("test_*.py"):
    text = path.read_text(encoding="utf-8")
    updated = text.replace("/ui-statistics-page.css?v=21", "/ui-statistics-page.css?v=22")
    updated = updated.replace("/style-v11.css?v=25", "/style-v11.css?v=26")
    if updated != text:
        path.write_text(updated, encoding="utf-8")

# Dashboard no longer owns, hides, decorates, or reparents Statistics history.
path = tests / "test_ui_dashboard_contract.py"
text = path.read_text(encoding="utf-8")
start = text.index("def test_dashboard_keeps_one_primary_metric_row_and_moves_history() -> None:")
end = text.index("def test_quick_add_preserves_existing_functional_controls() -> None:", start)
replacement = '''def test_dashboard_keeps_one_primary_metric_row_and_statistics_owns_history_directly() -> None:
    dashboard = DASHBOARD_CSS.read_text(encoding="utf-8")
    statistics = STATISTICS_CSS.read_text(encoding="utf-8")
    runtime = RUNTIME.read_text(encoding="utf-8")
    index = (ROOT / "frontend" / "static" / "index.html").read_text(encoding="utf-8")

    assert "grid-template-columns: repeat(6" in dashboard
    assert "#view-dashboard .dash-kpi-strip--dashboard" not in dashboard
    assert "#view-stats .dp-stats-history-grid" not in dashboard
    assert "#view-stats .dp-stats-history-grid" in statistics
    assert "moveDashboardKpisToStatistics" not in runtime
    assert "decorateHistoricalKpis" not in runtime
    assert 'class="dash-kpi-strip dash-kpi-strip--dashboard"' not in index
    stats_view = index[index.index('<!-- Statistics -->'):index.index('<!-- Changelog -->')]
    assert 'class="dash-kpi-strip dp-stats-history-grid"' in stats_view


def test_dashboard_and_statistics_use_canonical_custom_semantic_assets() -> None:
    runtime = RUNTIME.read_text(encoding="utf-8")
    index = (ROOT / "frontend" / "static" / "index.html").read_text(encoding="utf-8")

    dashboard_assets = (
        "card-download.svg",
        "card-checkmark.svg",
        "card-play.svg",
        "card-clock.svg",
        "card-error.svg",
        "card-disk.svg",
        "card-link.svg",
        "card-document-stack.svg",
    )
    missing = [asset for asset in dashboard_assets if asset not in runtime]
    assert not missing, f"Dashboard is missing canonical assets: {missing}"

    statistics_assets = (
        "heartbeat-outline.svg",
        "calendar-24.svg",
        "calendar-7.svg",
        "clock-outline.svg",
        "cube.svg",
    )
    stats_view = index[index.index('<!-- Statistics -->'):index.index('<!-- Changelog -->')]
    missing = [asset for asset in statistics_assets if asset not in stats_view]
    assert not missing, f"Statistics is missing canonical assets: {missing}"
    assert "verified-badge.svg" not in stats_view


'''
path.write_text(text[:start] + replacement + text[end:], encoding="utf-8")

# queuePct existed only to feed the removed Queue Health compatibility tile.
path = tests / "test_ui_detail_overlay_cleanup_contract.py"
text = path.read_text(encoding="utf-8")
text = text.replace(
    '    assert "const queuePct = pct(completed, total || 0);" in app\n',
    '    assert "const queuePct = pct(completed, total || 0);" not in app\n',
)
path.write_text(text, encoding="utf-8")

# The old scope test inspected a Dashboard-only KPI substrate. Replace it with
# the actual product contract: history is directly owned by Statistics.
path = tests / "test_v1_scope.py"
text = path.read_text(encoding="utf-8")
start = text.index("def test_dashboard_kpi_strip_omits_duplicate_database_tile_and_stays_centered():")
end = text.index("def test_inherited_file_preview_and_block_routes_are_hardened():", start)
replacement = '''def test_statistics_history_strip_omits_duplicate_database_and_queue_health_tiles():
    index = (REPO_ROOT / "frontend/static/index.html").read_text()
    frontend = (REPO_ROOT / "frontend/static/app.js").read_text()
    dashboard_styles = (REPO_ROOT / "frontend/static/ui-dashboard.css").read_text()
    statistics_styles = (REPO_ROOT / "frontend/static/ui-statistics-page.css").read_text()

    assert 'class="dash-kpi-strip dash-kpi-strip--dashboard"' not in index
    stats_view = index[index.index('<!-- Statistics -->'):index.index('<!-- Changelog -->')]
    history = stats_view.split('<div class="dash-kpi-strip dp-stats-history-grid">', 1)[1].split('</div>\n        </div>', 1)[0]

    assert history.count('class="dash-kpi ') == 5
    assert 'id="i-db-type"' not in history
    assert '<div class="dash-kpi-lbl">Database</div>' not in history
    assert 'id="i-queue-health"' not in history
    assert 'id="i-queue-copy"' not in history
    assert "getElementById('i-db-type')" not in frontend
    assert "getElementById('i-queue-health')" not in frontend
    assert "setDot('db'" in frontend
    assert ".dash-kpi-strip--dashboard" not in dashboard_styles
    assert "#view-stats .dp-stats-history-grid" in statistics_styles



'''
path.write_text(text[:start] + replacement + text[end:], encoding="utf-8")

print("Statistics stale contract migration applied")
