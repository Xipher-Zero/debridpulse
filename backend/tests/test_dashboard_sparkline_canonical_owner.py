from pathlib import Path
import re

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"


def test_dashboard_sparkline_surface_is_static_base_markup():
    """Each card has one line -- the signal itself -- resting flat, stroked by its
    own pixel-space gradient; no second pulse path is drawn over it. Beneath it
    one glow fill, the area under the same line, masked by the same gradient."""
    html = (STATIC / "index.html").read_text()
    assert html.count('class="dp-card-spark" viewBox="0 0 100 29.42"') == 6
    for i in range(6):
        assert (f'<path class="dp-card-spark-line" stroke="url(#dp-card-spark-signal-{i})" d="M 0 16 H 100"></path>') in html
        assert f'<linearGradient id="dp-card-spark-signal-{i}" gradientUnits="userSpaceOnUse"' in html
        assert (f'<path class="dp-card-spark-fill" fill="url(#dp-card-spark-glow-{i})" mask="url(#dp-card-spark-glow-mask-{i})" '
                'd="M 0 16 H 100 V 29.42 H 0 Z"></path>') in html
        # Masked by the line's own gradient, sheared about the baseline so the
        # glow's edge slants back to the right as it falls away.
        assert (f'<rect x="-100000" y="-100" width="200000" height="200" transform="matrix(1 0 1.5 1 -24 0)" '
                f'fill="url(#dp-card-spark-signal-{i})"></rect>') in html
    assert html.count('<stop class="dp-card-spark-rest" offset="0"></stop>') == 6
    assert html.count('class="dp-card-spark-fill"') == 6
    for retired in ("dp-card-spark-pulse", "dp-card-spark-point", "pathLength"):
        assert retired not in html
    assert '<polyline class="dp-card-spark-line"' not in html


def test_dashboard_heartbeat_is_one_angular_ecg_complex_on_the_baseline():
    """Straight segments only: a 48 px PQRST complex rising to y=2 and dropping
    to y=23 from the y=16 baseline, starting and ending on it."""
    app = (STATIC / "app.js").read_text()
    block = app[app.index("const DASHBOARD_HEARTBEAT_COMPLEX = ["):app.index("const DASHBOARD_HEARTBEAT_WIDTH = 48;")]
    points = [(float(x), float(y)) for x, y in re.findall(r"\[(-?[\d.]+), (-?[\d.]+)\]", block)]
    assert points[0] == (0, 16) and points[-1] == (48, 16)
    assert [x for x, _ in points] == sorted(x for x, _ in points)
    assert min(y for _, y in points) == 2 and max(y for _, y in points) == 23


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
    assert "const DASHBOARD_HEARTBEAT_BPM = [7, 90];" in app
    assert "const DASHBOARD_HEARTBEAT_TRAIL = 416;" in app
    assert "const DASHBOARD_HEARTBEAT_EASE_MS = 4000;" in app
    assert "const DASHBOARD_HEARTBEAT_SPEED = 0.24;" in app
    assert "setDashboardHeartbeatThroughput(data.download_bytes_per_second, data.sustained_peak_bytes_per_second);" in app
    assert "matchMedia('(prefers-reduced-motion: reduce)')" in app
    assert app.count("syncDashboardSparkPulses();") == 2  # nav() and boot
    assert "document.addEventListener('visibilitychange', syncDashboardSparkPulses);" in app
    assert "new ResizeObserver(remeasureDashboardHeartbeat).observe(document.getElementById('dash-hero'));" in app
    assert app.count("setTimeout(dashboardHeartbeat,") == 1  # one timer chain, scheduled in one place
    assert app.count("requestAnimationFrame(dashboardHeartbeatFrame)") == 2
    # Global Pause All reaches the owner from one place: loadStats, after it reads /stats.
    assert app.count("processingPaused = !!s.paused;\n      syncDashboardHeartbeatPause();") == 1
    assert app.count("syncDashboardHeartbeatPause()") == 3   # loadStats, the held-timer guard, its definition


def test_dashboard_sparkline_curve_has_canonical_rounded_paint():
    css = (STATIC / "ui-dashboard.css").read_text()
    assert "stroke-linecap: round;" in css
    assert "stroke-linejoin: round;" in css
