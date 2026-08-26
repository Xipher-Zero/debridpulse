"""Activity Log presentation and shared desktop shell regression contracts."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_activity_log_uses_blue_single_document_header_language() -> None:
    css = read("ui-activity-log-page.css")
    runtime = read("ui-accessibility-runtime.js")
    manifest = read("icons/dp/manifest.json")

    assert "#view-events > .card .card-title::before" in css
    assert "url('/icons/dp/document.svg')" in css
    assert "card-document-stack.svg" not in css
    assert "Recent transfer activity, decisions, warnings, and errors." in css
    assert '"document": "document.svg"' in manifest
    assert "normalizeActivityNaming" in runtime
    assert "Activity Log" in runtime


def test_activity_log_fills_shell_and_scrolls_only_event_stream() -> None:
    css = read("ui-activity-log-page.css")

    assert "#content:has(#view-events.active)" in css
    assert "overflow-y: hidden" in css
    assert "#view-events.active" in css
    assert "display: flex !important" in css
    assert "height: 100%" in css
    assert "min-height: 0" in css
    assert "margin-bottom: 0 !important" in css
    assert "#event-list" in css
    assert "flex: 1 1 auto" in css
    assert "overflow-y: auto" in css
    assert "scrollbar-gutter: stable" in css
    assert "100vh" not in css
    assert "calc(100vh" not in css


def test_desktop_provider_status_bottom_datum_is_shell_owned() -> None:
    shell = read("ui-shell-structural.css")
    activity = read("ui-activity-log-page.css")

    assert "@media (min-width: 901px)" in shell
    assert "body.dp-v11-structural .sidebar-footer" in shell
    assert "bottom: 24px !important" in shell
    assert ":has(#view-torrents.active) .sidebar-footer" not in shell
    assert ".sidebar-footer" not in activity


def test_activity_log_layer_is_page_specific_generation_27() -> None:
    overlay = read("style-v11.css")

    shell = overlay.index("/ui-shell-structural.css?v=26")
    dashboard = overlay.index("/ui-dashboard.css?v=20")
    stats = overlay.index("/ui-statistics-page.css?v=20")
    activity = overlay.index("/ui-activity-log-page.css?v=27")
    downloads = overlay.index("/ui-downloads-page.css?v=25")
    transfer = overlay.index("/ui-transfer-contract.css?v=29")

    assert shell < dashboard < stats < activity < downloads < transfer
