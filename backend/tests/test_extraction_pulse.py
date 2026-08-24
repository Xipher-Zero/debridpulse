from pathlib import Path


def test_extracting_status_badge_pulses_and_respects_reduced_motion():
    root = Path(__file__).resolve().parents[2]
    css = (root / "frontend/static/style.css").read_text()
    html = (root / "frontend/static/index.html").read_text()

    assert ".badge-extracting { animation: pulse 1s ease-in-out infinite; }" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert ".badge-extracting { animation: none; }" in css
    assert '/style.css?v=15' in html
