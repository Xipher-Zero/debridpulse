from __future__ import annotations

from pathlib import Path
import sys

repo = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
path = repo / "backend" / "tests" / "test_v1_scope.py"
text = path.read_text(encoding="utf-8")
start = text.index("def test_statistics_history_strip_omits_duplicate_database_and_queue_health_tiles():")
end = text.index("def test_inherited_file_preview_and_block_routes_are_hardened():", start)
replacement = '''def test_statistics_history_strip_omits_duplicate_database_and_queue_health_tiles():
    index = (REPO_ROOT / "frontend/static/index.html").read_text()
    frontend = (REPO_ROOT / "frontend/static/app.js").read_text()
    dashboard_styles = (REPO_ROOT / "frontend/static/ui-dashboard.css").read_text()
    statistics_styles = (REPO_ROOT / "frontend/static/ui-statistics-page.css").read_text()

    assert 'class="dash-kpi-strip dash-kpi-strip--dashboard"' not in index
    stats_view = index[index.index('<!-- Statistics -->'):index.index('<!-- Changelog -->')]
    history = stats_view.split('<div class="dash-kpi-strip dp-stats-history-grid">', 1)[1].split(
        '<div class="scard dp-stats-chart', 1
    )[0]

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
print("Statistics scope contract rewrite hardened")
