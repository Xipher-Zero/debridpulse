"""Activity Log rebuild and shared desktop-shell regression contracts."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_activity_log_keeps_approved_content_but_uses_structural_runtime() -> None:
    css = read("ui-activity-log-page.css")
    runtime = read("ui-runtime.js")
    manifest = read("icons/dp/manifest.json")

    assert "decorateActivityLog" in runtime
    assert "normalizeActivityRows" in runtime
    assert "document.svg" in runtime
    assert "Activity Log" in runtime
    assert "Recent transfer activity, decisions, warnings, and errors." in runtime
    assert "Refresh activity log" in runtime
    assert '"document": "document.svg"' in manifest
    assert '.nav-item[data-view="events"] .nav-label' in css
    assert "content: 'Activity Log';" in css
    assert "Recent transfer activity, decisions, warnings, and errors." in css

    required = (
        ".dp-activity-card",
        ".dp-activity-card-title",
        ".dp-activity-search-band",
        ".dp-activity-list",
        ".dp-activity-row",
        ".dp-activity-message",
        ".dp-activity-transfer",
        ".dp-activity-time",
    )
    missing = [selector for selector in required if selector not in css]
    assert not missing, f"Activity rebuild is missing structural selectors: {missing}"


def test_activity_log_fills_shell_and_scrolls_only_rebuilt_event_viewport() -> None:
    css = read("ui-activity-log-page.css")

    assert "#content:has(#view-events.active)" in css
    assert "overflow-y: hidden" in css
    assert "#view-events.active" in css
    assert "display: flex !important" in css
    assert "height: 100% !important" in css
    assert "min-height: 0" in css
    assert "overflow: visible" in css
    assert "margin-bottom: 0 !important" in css
    assert ".dp-activity-list" in css
    assert "flex: 1 1 auto" in css
    assert "overflow-y: auto" in css
    assert "scrollbar-gutter: stable" in css
    assert "100vh" not in css
    assert "calc(100vh" not in css


def test_activity_rebuild_uses_downloads_search_band_recipe() -> None:
    activity = read("ui-activity-log-page.css")
    downloads = read("ui-downloads-page.css")

    shared_fragments = (
        "padding: 13px 16px 14px !important;",
        "border-bottom: 1px solid var(--dp-divider);",
        "color-mix(in srgb, var(--dp-accent-purple) 5.5%, transparent)",
        "color-mix(in srgb, var(--dp-accent-purple) 4.5%, transparent)",
    )
    for fragment in shared_fragments:
        assert fragment in activity
        assert fragment in downloads


def test_activity_projected_level_filter_keeps_compact_reviewed_footprint() -> None:
    css = read("ui-activity-log-page.css")

    native = css.split("#view-events #ev-level {", 1)[1].split("}", 1)[0]
    projected = css.split("#view-events #ev-level + .dp-dropdown-shell {", 1)[1].split("}", 1)[0]
    for block in (native, projected):
        assert "width: 160px;" in block
        assert "max-width: 160px;" in block
        assert "flex: 0 0 160px;" in block


def test_activity_refresh_consumes_dashboard_recover_control_recipe() -> None:
    controls = read("ui-utility-controls.css")

    assert "#view-dashboard #btn-recover-all," in controls
    assert "#view-events .dp-activity-refresh" in controls
    assert "height: 36px !important;" in controls
    assert "border-radius: 9px !important;" in controls
    assert "background: rgba(151,87,203,.018) !important;" in controls
    assert "border-color: #c5a3dc !important;" in controls
    assert "color: #714790 !important;" in controls
    assert ".dp-activity-refresh:hover" in controls


def test_transfer_row_actions_have_equal_footprint_and_shared_pause_material() -> None:
    transfer = read("ui-transfer-contract.css")
    controls = read("ui-utility-controls.css")

    assert "width: 72px !important;" in transfer
    assert "min-width: 72px !important;" in transfer
    assert "height: 36px !important;" in transfer
    shared_pause = "linear-gradient(180deg, rgba(255,251,236,.54) 0%, rgba(250,234,186,.22) 100%)"
    shared_pause_hover = "linear-gradient(180deg, rgba(255,249,226,.68) 0%, rgba(249,229,168,.30) 100%)"
    assert shared_pause in transfer
    assert shared_pause in controls
    assert shared_pause_hover in transfer
    assert shared_pause_hover in controls


def test_desktop_provider_status_bottom_datum_is_shell_owned() -> None:
    shell = read("ui-shell-structural.css")
    activity = read("ui-activity-log-page.css")

    assert "@media (min-width: 901px)" in shell
    assert "body.dp-v11-structural .sidebar-footer" in shell
    assert "bottom: 24px !important" in shell
    assert ":has(#view-torrents.active) .sidebar-footer" not in shell
    assert ".sidebar-footer" not in activity


def test_activity_log_layer_follows_canonical_shell_and_reference_stack() -> None:
    overlay = read("style-v11.css")

    shell = overlay.index("/ui-shell-structural.css?v=30")
    dashboard = overlay.index("/ui-dashboard.css?v=20")
    controls = overlay.index("/ui-utility-controls.css?v=23")
    stats = overlay.index("/ui-statistics-page.css?v=21")
    activity = overlay.index("/ui-activity-log-page.css?v=30")
    downloads = overlay.index("/ui-downloads-page.css?v=28")
    transfer = overlay.index("/ui-transfer-contract.css?v=31")

    assert shell < dashboard < controls < stats < activity < downloads < transfer
