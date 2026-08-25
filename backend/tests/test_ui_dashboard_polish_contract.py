"""Contracts for the v1.0.11 Dashboard polish pass."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "frontend" / "static"
STYLE = STATIC / "style-v11.css"
POLISH = STATIC / "ui-dashboard-polish.css"
FINAL = STATIC / "ui-dashboard-polish-final.css"
CONTROL_POLISH = STATIC / "ui-dashboard-control-polish.css"


def test_polish_layers_finish_dashboard_after_universal_base() -> None:
    overlay = STYLE.read_text(encoding="utf-8")
    universal = "/ui-universal-language.css?v=19"
    batch5 = "/ui-dashboard-batch5.css?v=18"
    polish = "/ui-dashboard-polish.css?v=18"
    final = "/ui-dashboard-polish-final.css?v=18"
    control = "/ui-dashboard-control-polish.css?v=18"
    for layer in (universal, batch5, polish, final, control):
        assert layer in overlay
    assert overlay.index(universal) < overlay.index(batch5)
    assert overlay.index(batch5) < overlay.index(polish) < overlay.index(final) < overlay.index(control)


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


def test_live_review_micro_refinements_are_locked() -> None:
    css = FINAL.read_text(encoding="utf-8")
    required = (
        "height: calc(100% - 12px) !important",
        "max-height: 38px !important",
        "ellipse 100% 74% at 98% 50%",
        "#aria2-badge-speed",
        "color: #087a46 !important",
        ".dash-hero-stat:hover",
        "transform: none !important",
        "transition: none !important",
        "min-width: 91px !important",
        "min-height: 56px !important",
        "height: 56px !important",
        '.badge-partial::before',
        "content: '⚠' !important",
        '.badge-completed::before',
        "transform: translateY(1px) !important",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"live-review micro-refinement contract is missing: {missing}"


def test_latest_color_spacing_and_sidebar_hover_refinements_are_locked() -> None:
    css = FINAL.read_text(encoding="utf-8")
    required = (
        "#dash-error-card",
        "--c: #ff4854 !important",
        "saturate(1.28)",
        "height: 4.5px !important",
        "@media (min-width: 1440px)",
        "table-layout: fixed !important",
        "width: 13% !important",
        "width: 21% !important",
        ".nav-item:not(.active):hover",
        "rgba(136,76,228,.13)",
        "rgba(143,91,222,.17)",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"latest dashboard balance contract is missing: {missing}"


def test_quick_add_secondary_utilities_use_integrated_outline_hierarchy() -> None:
    css = CONTROL_POLISH.read_text(encoding="utf-8")
    required = (
        "#btn-import-existing",
        "#btn-recover-all",
        "height: 36px !important",
        "min-height: 36px !important",
        "box-shadow: none !important",
        "rgba(255,255,255,.018)",
        "#d3cedd",
        "rgba(157,91,213,.028)",
        "#c5a3dc",
        "#714790",
        "hue-rotate(215deg)",
        ".dp-utility-icon",
        "filter: none !important",
        "transform: none !important",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"integrated secondary utility-control contract is missing: {missing}"


def test_light_pause_all_uses_integrated_amber_tint_not_heavy_slab() -> None:
    css = CONTROL_POLISH.read_text(encoding="utf-8")
    required = (
        "body.light.dp-v11-structural #topbar-actions #btn-pause-all.btn",
        "rgba(255,251,236,.82)",
        "rgba(250,234,186,.42)",
        "rgba(207,158,55,.72)",
        "color: #76560d !important",
        "text-shadow: none !important",
        "0 3px 8px -6px rgba(137,99,24,.28)",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"light Pause All integration contract is missing: {missing}"
