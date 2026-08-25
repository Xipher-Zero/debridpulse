"""Contracts for the v1.0.11 Dashboard polish pass."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "frontend" / "static"
STYLE = STATIC / "style-v11.css"
POLISH = STATIC / "ui-dashboard-polish.css"
FINAL = STATIC / "ui-dashboard-polish-final.css"


def test_polish_layers_are_last_without_cache_generation_bump() -> None:
    overlay = STYLE.read_text(encoding="utf-8")
    assert "/ui-dashboard-batch5.css?v=18" in overlay
    assert "/ui-dashboard-polish.css?v=18" in overlay
    assert "/ui-dashboard-polish-final.css?v=18" in overlay
    assert overlay.rfind("/ui-dashboard-polish.css?v=18") > overlay.rfind(
        "/ui-dashboard-batch5.css?v=18"
    )
    assert overlay.rfind("/ui-dashboard-polish-final.css?v=18") > overlay.rfind(
        "/ui-dashboard-polish.css?v=18"
    )
    assert "?v=19" not in overlay


def test_topbar_pause_and_speedcap_semantics() -> None:
    css = POLISH.read_text(encoding="utf-8")
    required = (
        "#topbar-actions #btn-pause-all.btn",
        "#fff2cc",
        "#f7dda0",
        "#aria2-cap-toggle:hover",
        'aria-expanded="true"',
        "background: transparent !important",
        "color: inherit !important",
        "font-weight: 800 !important",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"topbar polish contract is missing: {missing}"


def test_sidebar_provider_and_badge_polish() -> None:
    css = POLISH.read_text(encoding="utf-8")
    required = (
        "#premium-row::before",
        "top: 17px !important",
        "#dot-api.ok",
        "rgba(52,211,130,.78)",
        "ellipse 78% 46% at 52% 50%",
        "#nb-active.nav-badge",
        "#eadcff",
        "#6623a8",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"sidebar/provider polish contract is missing: {missing}"


def test_progress_sparkline_add_and_activity_depth_polish() -> None:
    css = POLISH.read_text(encoding="utf-8")
    required = (
        ".dp-card-spark stop:last-child",
        "stop-opacity: .075 !important",
        "#btn-add-transfer",
        "#8950d4",
        "#6551c3",
        "height: 7px !important",
        "0 0 13px rgba(48,211,130,.34)",
        "font-size: 16px !important",
        "font-weight: 800 !important",
        ".dash-activity-table-wrap",
        "radial-gradient(ellipse 88% 84% at 100% 100%",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"Dashboard polish contract is missing: {missing}"


def test_indeterminate_progress_stripes_survive_semantic_glow() -> None:
    css = FINAL.read_text(encoding="utf-8")
    required = (
        '.prog-fill[style*="repeating-linear-gradient"]',
        "repeating-linear-gradient(",
        "var(--accent) 8px",
        "box-shadow: none !important",
        "opacity: .35 !important",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"progress stripe guard is missing: {missing}"
