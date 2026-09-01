from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"


def test_dashboard_sparkline_surface_is_static_base_markup():
    html = (STATIC / "index.html").read_text()
    assert html.count('class="dp-card-spark"') == 6
    assert html.count('class="dp-card-spark-line"') == 6
    assert html.count('class="dp-card-spark-fill"') == 6
    assert '<polyline class="dp-card-spark-line"' not in html


def test_dashboard_sparkline_state_is_owned_by_load_stats_not_presentation_runtime():
    app = (STATIC / "app.js").read_text()
    runtime = (STATIC / "ui-runtime.js").read_text()
    assert "recordDashboardMetricHistory({" in app
    assert "debridpulse.dashboard.metric-history.v2" in app
    assert "dashboardSmoothSparkPath" in app
    assert " C ${fmt(cp1.x)}" in app
    assert "makeSparkline" not in runtime
    assert "installMetricHistoryHook" not in runtime
    assert "METRIC_HISTORY_KEY" not in runtime
    assert "dpDashboardMetricLifecycle" not in runtime


def test_dashboard_sparkline_samples_use_same_visible_metric_universe_as_cards():
    app = (STATIC / "app.js").read_text()
    assert ".filter(([status]) => status !== 'deleted')" in app
    assert "recordDashboardMetricHistory({\n        total,\n        completed," in app
    assert "errors: errCount" in app
    assert "downloaded: s.total_completed_bytes" in app


def test_dashboard_sparkline_curve_has_canonical_rounded_paint():
    css = (STATIC / "ui-dashboard.css").read_text()
    assert "stroke-linecap: round;" in css
    assert "stroke-linejoin: round;" in css
