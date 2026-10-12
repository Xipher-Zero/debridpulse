from pathlib import Path
import re

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"


def test_dashboard_sparkline_surface_is_static_base_markup():
    html = (STATIC / "index.html").read_text()
    assert html.count('class="dp-card-spark"') == 6
    assert html.count('class="dp-card-spark-line" d="M 0 16 H 100"') == 6
    assert html.count('class="dp-card-spark-pulse" stroke="url(#dp-card-spark-beat-') == 6
    for retired in ("dp-card-spark-point", "dp-card-spark-fill", "pathLength"):
        assert retired not in html
    assert '<polyline class="dp-card-spark-line"' not in html


def test_dashboard_heartbeat_is_one_angular_ecg_complex_on_the_baseline():
    """Every card shares one beat: straight segments only (no curves), a
    24-unit PQRST complex rising to y=2 and dropping to y=23 from the y=16
    baseline, then a 100-unit trail along that baseline."""
    html = (STATIC / "index.html").read_text()
    beats = re.findall(r'<path class="dp-card-spark-pulse" stroke="url\(#dp-card-spark-beat-(\d)\)" d="([^"]+)"', html)
    assert [int(i) for i, _ in beats] == list(range(6))
    assert len({d for _, d in beats}) == 1
    d = beats[0][1]
    assert d.startswith("M 0 16 ") and d.endswith(" L 24 16 L 124 16")
    assert not re.search(r"[CcQqSsTtAa]", d)
    ys = [float(y) for y in re.findall(r"-?\d+(?:\.\d+)? (-?\d+(?:\.\d+)?)", d)]
    assert min(ys) == 2 and max(ys) == 23


def test_dashboard_sparklines_have_no_metric_history_owner():
    app = (STATIC / "app.js").read_text()
    for retired in ("metric-history", "recordDashboardMetricHistory", "renderDashboardMetricHistory",
                    "DASHBOARD_HERO_METRICS", "dashboardMonotoneSparkPath", "dashboardCumulativeDeltas",
                    "sample interval"):
        assert retired not in app
    assert not (STATIC / "ui-runtime.js").exists()


def test_dashboard_kpi_hover_copy_describes_the_numbers():
    html = (STATIC / "index.html").read_text()
    titles = re.findall(r'<div class="dash-hero-stat"[^\n]*? title="([^"]+)">\n', html)
    assert len(titles) == 6
    assert not any(word in title.lower() for title in titles for word in ("trend", "sample", "recent"))


def test_dashboard_pulse_owner_is_lifecycle_bound():
    app = (STATIC / "app.js").read_text()
    assert "const DASHBOARD_SPARK_PULSE_INTERVAL_MS = [1000, 3000];" in app
    assert "const DASHBOARD_SPARK_PULSE_TRAVEL_MS = 800;" in app
    assert "const DASHBOARD_SPARK_PULSE_SETTLE_MS = 60;" in app
    assert "matchMedia('(prefers-reduced-motion: reduce)')" in app
    assert app.count("syncDashboardSparkPulses();") == 2  # nav() and boot
    assert "document.addEventListener('visibilitychange', syncDashboardSparkPulses);" in app


def test_dashboard_sparkline_curve_has_canonical_rounded_paint():
    css = (STATIC / "ui-dashboard.css").read_text()
    assert "stroke-linecap: round;" in css
    assert "stroke-linejoin: round;" in css
