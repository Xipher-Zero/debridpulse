"""Final-state contracts for the retained mixed Dashboard calibration stack.

The canonical Dashboard owner now includes its structural geometry directly.
The remaining batch and polish layers stay live because they still mix Dashboard,
shell, and transfer responsibilities. These tests protect accepted behavior and
explicit ownership boundaries rather than implementation history.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
STYLE = STATIC / "style-v11.css"
BATCH5 = STATIC / "ui-dashboard-batch5.css"
POLISH = STATIC / "ui-dashboard-polish.css"
FINAL = STATIC / "ui-dashboard-polish-final.css"
UTILITY = STATIC / "ui-utility-controls.css"
DASHBOARD_FINAL = STATIC / "ui-dashboard-final.css"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def require(css: str, fragments: tuple[str, ...], label: str) -> None:
    missing = [fragment for fragment in fragments if fragment not in css]
    assert not missing, f"{label} is missing: {missing}"


def test_retained_dashboard_calibration_order_is_explicit() -> None:
    overlay = read(STYLE)
    layers = (
        "/ui-universal-language.css?v=20",
        "/ui-dashboard.css?v=20",
        "/ui-dashboard-batch5.css?v=20",
        "/ui-dashboard-polish.css?v=20",
        "/ui-dashboard-polish-final.css?v=20",
        "/ui-utility-controls.css?v=23",
        "/ui-dashboard-final.css?v=23",
    )
    for layer in layers:
        assert layer in overlay
    assert [overlay.index(layer) for layer in layers] == sorted(overlay.index(layer) for layer in layers)
    for retired in (
        "ui-dashboard-structural.css",
        "ui-dashboard-consistency.css",
        "ui-dashboard-batch1.css",
        "ui-dashboard-batch2.css",
        "ui-dashboard-batch2-final.css",
        "ui-dashboard-batch3.css",
        "ui-dashboard-batch4.css",
        "ui-dashboard-control-polish.css",
    ):
        assert retired not in overlay
        assert not (STATIC / retired).exists()


def test_retained_stage_keeps_provider_and_spotlight_geometry() -> None:
    require(
        read(BATCH5),
        (
            "padding: 2px 0 10px 28px !important",
            "left: 4px !important",
            "max-width: 164px !important",
            "transparent 100%) !important",
            "box-shadow: none !important",
        ),
        "Dashboard provider and spotlight contract",
    )


def test_retained_stage_keeps_transfer_semantics_and_progress_emphasis() -> None:
    require(
        read(BATCH5),
        (
            'tr[data-status="downloading"] .badge-downloading::before',
            'tr[data-status="downloading"] .badge-partial::before',
            'tr[data-status="paused"] .badge-paused::before',
            'tr[data-status="completed"] .badge-completed::before',
            'tr[data-status="error"] .badge-error::before',
            "content: '↓'",
            "content: 'Ⅱ'",
            "content: '✓'",
            "content: '×'",
            "height: 6px !important",
            "font-size: 15px !important",
            ":has(.badge-partial) .prog-fill",
            ":not(:has(.badge-partial)) .prog-fill",
        ),
        "Dashboard transfer semantic contract",
    )


def test_retained_stage_keeps_action_color_grammar_and_primary_add_depth() -> None:
    require(
        read(BATCH5),
        (
            "#btn-pause-all",
            'button[onclick*="pauseT("]',
            "#btn-resume-all",
            "#btn-resume-paused",
            'button[onclick*="resumeT("]',
            "#btn-recover-all",
            "#e3c5ff",
            "#btn-add-transfer",
            "#973cf4",
            "inset 0 1px rgba(255,255,255,.24)",
        ),
        "Dashboard action contract",
    )


def test_dashboard_polish_keeps_topbar_pause_and_speedcap_semantics() -> None:
    require(
        read(POLISH),
        (
            "#topbar-actions #btn-pause-all.btn",
            "#fff2cc",
            "#f7dda0",
            "#aria2-cap-toggle:hover",
            'aria-expanded="true"',
            "background: transparent !important",
            "color: inherit !important",
            "font-weight: 800 !important",
        ),
        "topbar polish contract",
    )


def test_dashboard_polish_keeps_sidebar_provider_and_badge_presentation() -> None:
    require(
        read(POLISH),
        (
            "#premium-row::before",
            "top: 17px !important",
            "#dot-api.ok",
            "rgba(52,211,130,.78)",
            "ellipse 78% 46% at 52% 50%",
            "#nb-active.nav-badge",
            "#eadcff",
            "#6623a8",
        ),
        "sidebar and provider polish contract",
    )


def test_dashboard_polish_keeps_progress_add_and_activity_depth() -> None:
    require(
        read(POLISH),
        (
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
        ),
        "Dashboard depth contract",
    )


def test_final_calibration_keeps_indeterminate_progress_stripes() -> None:
    require(
        read(FINAL),
        (
            '.prog-fill[style*="repeating-linear-gradient"]',
            "repeating-linear-gradient(",
            "var(--accent) 8px",
            "box-shadow: none !important",
            "opacity: .35 !important",
        ),
        "indeterminate progress guard",
    )


def test_final_calibration_keeps_sidebar_starburst_without_count_badge_overlap() -> None:
    css = read(FINAL)
    require(
        css,
        (
            ".nav-item.active::after",
            "right: -2px !important",
            "width: 76px !important",
            "height: 54px !important",
            "radial-gradient(circle at 98% 50%",
            "conic-gradient(from 180deg at 98% 50%",
            "mask-image: radial-gradient(ellipse 100% 86% at 98% 50%",
            "border-radius: 0 !important",
            "box-shadow: none !important",
        ),
        "sidebar starburst contract",
    )
    assert "#nb-active" not in css


def test_final_calibration_keeps_provider_baseline_crown_and_metric_icon_lighting() -> None:
    require(
        read(FINAL),
        (
            ":has(#content.dashboard-active) .sidebar-footer",
            "bottom: 24px !important",
            "#premium-row::before",
            "top: 15px !important",
            "drop-shadow(0 0 11px rgba(153,65,239,.42))",
            ".dash-hero-stat .dhs-icon .dp-icon",
            "color-mix(in srgb, var(--c) 62%, transparent)",
            "color-mix(in srgb, var(--c) 70%, transparent)",
        ),
        "provider and metric lighting contract",
    )


def test_final_calibration_keeps_original_progress_percentage_geometry() -> None:
    require(
        read(FINAL),
        (
            ".prog-pct",
            "margin-top: 3px !important",
            "font-family: var(--mono) !important",
            "font-size: 10px !important",
            "font-weight: 500 !important",
            "letter-spacing: normal !important",
            "color: #3e465f !important",
            "text-shadow: none !important",
        ),
        "progress percentage contract",
    )


def test_final_calibration_hides_recent_activity_view_all() -> None:
    css = read(FINAL)
    assert 'button[onclick*="data-view=torrents"]' in css
    assert "display: none !important" in css


def test_final_calibration_keeps_live_micro_refinements() -> None:
    require(
        read(FINAL),
        (
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
            ".badge-partial::before",
            "content: '⚠' !important",
            ".badge-completed::before",
            "transform: translateY(1px) !important",
        ),
        "Dashboard micro-refinement contract",
    )


def test_final_calibration_keeps_color_spacing_and_sidebar_hover_refinements() -> None:
    require(
        read(FINAL),
        (
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
        ),
        "Dashboard color and spacing contract",
    )


def test_shared_utility_controls_keep_integrated_outline_hierarchy() -> None:
    require(
        read(UTILITY),
        (
            "#btn-import-existing",
            "#btn-recover-all",
            "#view-events .dp-activity-refresh",
            "#view-torrents .dp-downloads-refresh",
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
        ),
        "shared utility-control contract",
    )


def test_shared_utility_controls_keep_light_pause_all_treatment() -> None:
    require(
        read(UTILITY),
        (
            "body.light.dp-v11-structural #topbar-actions #btn-pause-all.btn",
            "rgba(255,251,236,.54)",
            "rgba(250,234,186,.22)",
            "rgba(207,158,55,.62)",
            "color: #76560d !important",
            "text-shadow: none !important",
            "0 3px 8px -6px rgba(137,99,24,.18)",
        ),
        "light Pause All contract",
    )


def test_dashboard_final_owner_keeps_empty_state_and_field_focus_contracts() -> None:
    css = read(DASHBOARD_FINAL)
    assert "#dash-activity-card.dp-dashboard-activity .empty-icon" in css
    assert "width: 76px !important" in css
    assert "height: 76px !important" in css
    assert "textarea.input.direct-link-input" in css
    assert "var(--dp-field-border)" in css
    assert "var(--dp-field-surface)" in css
    assert "box-shadow: var(--dp-focus-ring) !important" in css
