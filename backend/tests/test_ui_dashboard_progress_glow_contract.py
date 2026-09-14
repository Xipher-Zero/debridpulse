"""DP 1.0.12 UI Finishing (Correction 9): final-state contract for the
consolidated ordinary/partial Dashboard + Downloads progress-glow owner.

Prior to this correction, a higher-specificity ``tr[data-status="downloading"]
...`` pair of rules silently overrode the general glow declaration for the
single most common case (an actively downloading row), so the "ordinary"
glow alpha a user actually saw depended on transfer status by cascade
accident, not by design. Both status-scoped duplicates were removed; the one
remaining general declaration is now the sole effective owner for both
statuses, with a relative 15% alpha reduction applied to it.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_status_scoped_duplicate_glow_blocks_are_removed() -> None:
    css = read("ui-dashboard.css")
    assert 'tr[data-status="downloading"]:not(:has(.badge-partial)) .prog-fill' not in css
    assert 'tr[data-status="downloading"]:has(.badge-partial) .prog-fill' not in css


def test_ordinary_and_partial_progress_glow_reduced_by_fifteen_percent() -> None:
    css = read("ui-dashboard.css")
    # Ordinary (green): 0.78 * 0.85 = 0.663, 0.34 * 0.85 = 0.289.
    assert "rgba(48,211,130,.663)" in css
    assert "rgba(48,211,130,.289)" in css
    assert "rgba(48,211,130,.78)" not in css
    assert "rgba(48,211,130,.34)" not in css
    # Partial (amber): 0.76 * 0.85 = 0.646, 0.32 * 0.85 = 0.272.
    assert "rgba(242,189,63,.646)" in css
    assert "rgba(242,189,63,.272)" in css
    assert "rgba(242,189,63,.76)" not in css
    assert "rgba(242,189,63,.32)" not in css


def test_progress_glow_blur_radii_and_geometry_are_unchanged() -> None:
    css = read("ui-dashboard.css")
    assert "0 0 6px rgba(48,211,130,.663)" in css
    assert "0 0 13px rgba(48,211,130,.289)" in css
    assert "height: 7px !important" in css


def test_terminal_error_progress_glow_is_untouched() -> None:
    css = read("ui-visual-accents.css")
    assert "var(--dp-state-error) 88%" in css
    assert "var(--dp-state-error) 46%" in css
