"""Final-state contracts for the retained Dashboard calibration stack."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
STYLE = STATIC / "style-v11.css"
BATCH5 = STATIC / "ui-dashboard-batch5.css"
POLISH = STATIC / "ui-dashboard-polish.css"
FINAL = STATIC / "ui-dashboard-polish-final.css"
UTILITY = STATIC / "ui-utility-controls.css"
CONSISTENCY = STATIC / "ui-dashboard-consistency.css"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_retained_dashboard_calibration_order_is_explicit() -> None:
    overlay = read(STYLE)
    layers = (
        "/ui-universal-language.css?v=20",
        "/ui-dashboard-structural.css?v=24",
        "/ui-dashboard-batch5.css?v=20",
        "/ui-dashboard-polish.css?v=20",
        "/ui-dashboard-polish-final.css?v=20",
        "/ui-utility-controls.css?v=23",
        "/ui-dashboard-consistency.css?v=23",
    )
    for layer in layers:
        assert layer in overlay
    assert [overlay.index(layer) for layer in layers] == sorted(overlay.index(layer) for layer in layers)
    for retired in (
        "ui-dashboard-batch1.css",
        "ui-dashboard-batch2.css",
        "ui-dashboard-batch2-final.css",
        "ui-dashboard-batch3.css",
        "ui-dashboard-batch4.css",
        "ui-dashboard-control-polish.css",
    ):
        assert retired not in overlay


def test_retained_calibration_keeps_provider_transfer_and_action_semantics() -> None:
    batch = read(BATCH5)
    required = (
        "padding: 2px 0 10px 28px !important",
        'tr[data-status="downloading"] .badge-downloading::before',
        'tr[data-status="paused"] .badge-paused::before',
        'tr[data-status="completed"] .badge-completed::before',
        'tr[data-status="error"] .badge-error::before',
        "content: '↓'",
        "content: 'Ⅱ'",
        "content: '✓'",
        "content: '×'",
        "#btn-pause-all",
        "#btn-resume-all",
        "#btn-recover-all",
        "#btn-add-transfer",
    )
    missing = [fragment for fragment in required if fragment not in batch]
    assert not missing, f"retained Dashboard calibration is missing: {missing}"


def test_dashboard_polish_keeps_topbar_provider_progress_and_activity_depth() -> None:
    css = read(POLISH)
    required = (
        "#topbar-actions #btn-pause-all.btn",
        "#aria2-cap-toggle:hover",
        "#premium-row::before",
        "#dot-api.ok",
        "#nb-active.nav-badge",
        ".dp-card-spark stop:last-child",
        "#btn-add-transfer",
        "height: 7px !important",
        ".dash-activity-table-wrap",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"Dashboard polish contract is missing: {missing}"


def test_final_dashboard_calibration_keeps_progress_and_sidebar_guards() -> None:
    css = read(FINAL)
    required = (
        '.prog-fill[style*="repeating-linear-gradient"]',
        ".nav-item.active::after",
        ':has(#content.dashboard-active) .sidebar-footer',
        ".dash-hero-stat .dhs-icon .dp-icon",
        ".prog-pct",
        "font-family: var(--mono) !important",
        "font-size: 10px !important",
        "font-weight: 500 !important",
        'button[onclick*="data-view=torrents"]',
        ".dash-hero-stat:hover",
        "transform: none !important",
        "transition: none !important",
        "#dash-error-card",
        "@media (min-width: 1440px)",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"final Dashboard calibration is missing: {missing}"


def test_shared_utility_controls_are_not_dashboard_owned() -> None:
    css = read(UTILITY)
    required = (
        "#btn-import-existing",
        "#btn-recover-all",
        "#view-events .dp-activity-refresh",
        "#view-torrents .dp-downloads-refresh",
        "height: 36px !important",
        "body.light.dp-v11-structural #topbar-actions #btn-pause-all.btn",
        ".dp-utility-icon",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"shared utility-control contract is missing: {missing}"


def test_dashboard_consistency_keeps_empty_state_and_field_focus_contracts() -> None:
    css = read(CONSISTENCY)
    assert "#dash-activity-card.dp-dashboard-activity .empty-icon" in css
    assert "width: 76px !important" in css
    assert "height: 76px !important" in css
    assert "textarea.input.direct-link-input" in css
    assert "var(--dp-field-border)" in css
    assert "var(--dp-field-surface)" in css
    assert "box-shadow: var(--dp-focus-ring) !important" in css
