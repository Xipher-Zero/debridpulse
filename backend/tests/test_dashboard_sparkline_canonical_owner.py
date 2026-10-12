from pathlib import Path
import re

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"


def test_dashboard_sparkline_surface_is_static_base_markup():
    html = (STATIC / "index.html").read_text()
    assert html.count('class="dp-card-spark"') == 6
    assert html.count('class="dp-card-spark-line"') == 6
    assert html.count('class="dp-card-spark-fill"') == 6
    assert html.count('class="dp-card-spark-pulse" pathLength="100"') == 6
    assert "dp-card-spark-point" not in html
    assert '<polyline class="dp-card-spark-line"' not in html


def test_dashboard_sparklines_are_fixed_decorative_waveforms():
    """Each card's waveform is fixed artwork in the markup: line, fill and pulse
    share one path, and no two cards share a shape."""
    html = (STATIC / "index.html").read_text()
    lines = re.findall(r'<path class="dp-card-spark-line" d="([^"]+)"', html)
    pulses = re.findall(r'<path class="dp-card-spark-pulse" pathLength="100" d="([^"]+)"', html)
    fills = re.findall(r'<path class="dp-card-spark-fill" fill="url\(#dp-card-spark-fill-\d\)" d="([^"]+)"', html)
    assert len(lines) == 6 and len(set(lines)) == 6
    assert pulses == lines
    assert fills == [f"{line} L 100 24 L 0 24 Z" for line in lines]
    assert all(line.startswith("M 0.00 ") and " 100.00 " in line for line in lines)


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
    assert "const DASHBOARD_SPARK_PULSE_IDLE_MS = [3000, 10000];" in app
    assert "const DASHBOARD_SPARK_PULSE_TRAVEL_MS = [1000, 1500];" in app
    assert "matchMedia('(prefers-reduced-motion: reduce)')" in app
    assert app.count("syncDashboardSparkPulses();") == 2  # nav() and boot
    assert "document.addEventListener('visibilitychange', syncDashboardSparkPulses);" in app


def test_dashboard_sparkline_curve_has_canonical_rounded_paint():
    css = (STATIC / "ui-dashboard.css").read_text()
    assert "stroke-linecap: round;" in css
    assert "stroke-linejoin: round;" in css
