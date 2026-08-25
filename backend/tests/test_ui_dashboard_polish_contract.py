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


def test_final_sidebar_endpoint_is_diffuse_starburst_without_touching_count_badge() -> None:
    css = FINAL.read_text(encoding="utf-8")
    required = (
        ".nav-item.active::after",
        "right: -2px !important",
        "width: 76px !important",
        "height: 54px !important",
        "radial-gradient(circle at 98% 50%",
        "conic-gradient(from 180deg at 98% 50%",
        "mask-image: radial-gradient(ellipse 100% 86% at 98% 50%",
        "border-radius: 0 !important",
        "box-shadow: none !important",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"final sidebar starburst contract is missing: {missing}"
    assert "#nb-active" not in css


def test_final_provider_baseline_crown_and_metric_icon_lighting() -> None:
    css = FINAL.read_text(encoding="utf-8")
    required = (
        ':has(#content.dashboard-active) .sidebar-footer',
        "bottom: 24px !important",
        "#premium-row::before",
        "top: 15px !important",
        "drop-shadow(0 0 11px rgba(153,65,239,.42))",
        ".dash-hero-stat .dhs-icon .dp-icon",
        "color-mix(in srgb, var(--c) 62%, transparent)",
        "color-mix(in srgb, var(--c) 70%, transparent)",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"final provider/card-light contract is missing: {missing}"


def test_final_progress_percentage_restores_original_geometry_and_neutral_emphasis() -> None:
    css = FINAL.read_text(encoding="utf-8")
    required = (
        ".prog-pct",
        "margin-top: 3px !important",
        "font-family: var(--mono) !important",
        "font-size: 10px !important",
        "font-weight: 500 !important",
        "letter-spacing: normal !important",
        "color: #3e465f !important",
        "text-shadow: none !important",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"final progress percentage contract is missing: {missing}"


def test_recent_activity_view_all_is_removed_from_dashboard_presentation() -> None:
    css = FINAL.read_text(encoding="utf-8")
    assert 'button[onclick*="data-view=torrents"]' in css
    assert "display: none !important" in css
