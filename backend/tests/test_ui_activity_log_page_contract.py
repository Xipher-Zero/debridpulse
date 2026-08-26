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


def test_desktop_provider_status_bottom_datum_is_shell_owned() -> None:
    shell = read("ui-shell-structural.css")
    activity = read("ui-activity-log-page.css")

    assert "@media (min-width: 901px)" in shell
    assert "body.dp-v11-structural .sidebar-footer" in shell
    assert "bottom: 24px !important" in shell
    assert ":has(#view-torrents.active) .sidebar-footer" not in shell
    assert ".sidebar-footer" not in activity


def test_activity_log_rebuild_layer_is_generation_28() -> None:
    overlay = read("style-v11.css")

    shell = overlay.index("/ui-shell-structural.css?v=26")
    dashboard = overlay.index("/ui-dashboard.css?v=20")
    stats = overlay.index("/ui-statistics-page.css?v=20")
    activity = overlay.index("/ui-activity-log-page.css?v=28")
    downloads = overlay.index("/ui-downloads-page.css?v=25")
    transfer = overlay.index("/ui-transfer-contract.css?v=29")

    assert shell < dashboard < stats < activity < downloads < transfer
