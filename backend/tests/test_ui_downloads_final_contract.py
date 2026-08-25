"""Final desktop Downloads consistency contracts."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_downloads_rows_use_row_level_details_and_retire_drag_semantics() -> None:
    runtime = read("ui-downloads-runtime.js")
    required = (
        "row.classList.add('dp-downloads-detail-row')",
        "row.removeAttribute(attribute)",
        "['draggable', 'ondragstart', 'ondragover', 'ondragleave', 'ondrop']",
        "row.addEventListener('click'",
        "window.showDetail(id)",
        "rowTargetIsInteractive",
        "row.tabIndex = 0",
    )
    missing = [fragment for fragment in required if fragment not in runtime]
    assert not missing, f"row detail contract is missing: {missing}"


def test_downloads_rows_normalize_status_and_action_language() -> None:
    runtime = read("ui-downloads-runtime.js")
    transfer = read("ui-transfer-contract.css")

    for label in ("⏸ Pause", "▶ Resume", "↻ Retry", "✕ Remove", "⬇ Now"):
        assert label in runtime
    assert "replace(/^[^A-Za-z0-9]+/" in runtime
    assert "badge.classList.contains('badge-completed') ? 'Done'" in runtime

    required_geometry = (
        "min-height: 25px !important",
        "padding: 0 9px !important",
        "border-radius: 6px !important",
        "font-size: 10.5px !important",
        "min-height: 36px !important",
        "height: 36px !important",
        "padding: 0 14px !important",
        "border-radius: 8px !important",
        "font-size: 11.5px !important",
    )
    missing = [fragment for fragment in required_geometry if fragment not in transfer]
    assert not missing, f"shared row language is missing: {missing}"
    assert "[onclick*=\"pauseTorrent(\"]" in transfer
    assert "[onclick*=\"resumeTorrent(\"]" in transfer


def test_downloads_header_uses_download_art_not_recent_activity_art() -> None:
    runtime = read("ui-downloads-runtime.js")
    assert "/icons/dp/card-download.svg?v=11" in runtime
    assert "card-document-stack.svg" not in runtime


def test_downloads_desktop_columns_make_room_for_rectangular_actions() -> None:
    css = read("ui-downloads-desktop.css")
    expected = (
        "nth-child(2) { width: 25%; }",
        "nth-child(3) { width: 11%; }",
        "nth-child(4) { width: 10%; }",
        "nth-child(5) { width: 25%; }",
        "nth-child(6) { width: 6%; }",
        "nth-child(7) { width: 8%; }",
        "nth-child(8) { width: 190px; }",
    )
    for fragment in expected:
        assert fragment in css


def test_downloads_uses_shell_height_and_has_no_legacy_card_bottom_margin() -> None:
    css = read("ui-downloads-page.css")
    assert "height: 100% !important" in css
    assert "margin-bottom: 0 !important" in css
    assert "calc(100vh - var(--dp-shell-header)" not in css


def test_final_downloads_corrections_are_targeted_generation_25() -> None:
    overlay = read("style-v11.css")
    downloads = overlay.index("/ui-downloads-page.css?v=25")
    desktop = overlay.index("/ui-downloads-desktop.css?v=25")
    help_page = overlay.index("/ui-help-page.css?v=22")
    transfer = overlay.index("/ui-transfer-contract.css?v=25")
    assert downloads < desktop < help_page < transfer
