"""Final desktop Downloads consistency contracts after E1 canonicalization."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_downloads_rows_use_row_level_details_and_retire_drag_semantics() -> None:
    app = read("app.js")
    required = (
        "dp-downloads-detail-row", 'tabindex="0"',
        "event.key==='Enter'", "showDetail(${t.id})",
        "event.target.closest('button,input,a,select,textarea,label,[role=button]')",
    )
    missing = [fragment for fragment in required if fragment not in app]
    assert not missing, f"row detail contract is missing: {missing}"
    for forbidden in ('draggable="true"', "ondragstart=", "ondragover=", "ondrop="):
        assert forbidden not in app


def test_downloads_rows_emit_final_status_and_action_language() -> None:
    app = read("app.js")
    transfer = read("ui-transfer-contract.css")
    desktop = read("ui-downloads-desktop.css")
    for fragment in (
        'data-default-label="Pause"', 'data-default-label="Resume"',
        'data-default-label="Remove"', 'data-default-label="Retry"',
        "pauseT(${t.id},this)", "resumeT(${t.id},this)",
        "deleteT(${t.id},event,this)", "retryT(${t.id},this)",
    ):
        assert fragment in app
    for obsolete in ("⏸ Pause", "▶ Resume", "✕ Remove", "↻ Retry"):
        assert obsolete not in app
    assert 'button[onclick*="retryT("]' not in desktop
    assert "font-size: 0 !important" not in desktop
    assert "content: 'Retry'" not in desktop
    assert '[onclick*="retryT("]' in transfer
    assert "background: var(--dp-state-active-bg) !important" in transfer
    assert "border-color: color-mix(in srgb, var(--dp-state-active) 34%, transparent) !important" in transfer
    assert "color: var(--dp-state-active) !important" in transfer
    assert "box-shadow: none !important" in transfer
    required_geometry = (
        "min-height: 25px !important", "padding: 0 9px !important",
        "border-radius: 6px !important", "font-size: 10.5px !important",
        "width: 72px !important", "min-width: 72px !important",
        "min-height: 36px !important", "height: 36px !important",
        "padding: 0 8px !important", "border-radius: 8px !important",
        "font-size: 11.5px !important",
    )
    missing = [fragment for fragment in required_geometry if fragment not in transfer]
    assert not missing, f"shared row language is missing: {missing}"


def test_downloads_footer_language_tracks_selected_filter() -> None:
    app = read("app.js")
    matrix = (
        "No Items Added Yet", "Showing 1 Added Item", "Added Items",
        "No Active Downloads", "1 Active Download", "Active Downloads",
        "No Paused Downloads", "1 Paused Download", "Paused Downloads",
        "No Downloads Currently Processing", "1 Download Currently Processing", "Downloads Currently Processing",
        "No Downloads in Ready State", "1 Download in Ready State", "Downloads in Ready State",
        "No Downloads Completed Yet", "1 Download Completed", "Downloads Completed",
        "No Downloads Have Errors", "1 Download Has Errors", "Downloads Have Errors",
    )
    missing = [fragment for fragment in matrix if fragment not in app]
    assert not missing, f"filter footer language is missing: {missing}"
    assert "downloadPaginationSummary(normalizedTotal, from, to)" in app


def test_downloads_header_uses_download_art_not_recent_activity_art() -> None:
    index = read("index.html")
    section = index[index.index('id="view-torrents"'):index.index('<!-- Events -->')]
    assert "/icons/dp/card-download.svg?v=11" in section
    assert "card-document-stack.svg" not in section


def test_downloads_desktop_columns_preserve_provider_identity_status_progress_and_actions() -> None:
    css = read("ui-downloads-desktop.css")
    expected = (
        "nth-child(2) { width: 25%; }", "nth-child(3) { width: 13%; }",
        "nth-child(4) { width: 13%; }", "nth-child(5) { width: 20%; }",
        "nth-child(6) { width: 6%; }", "nth-child(7) { width: 8%; }",
        "nth-child(8) { width: 190px; }", "gap: 7px;",
    )
    for fragment in expected:
        assert fragment in css


def test_downloads_uses_shell_height_and_has_no_legacy_card_bottom_margin() -> None:
    css = read("ui-downloads-page.css")
    assert "height: 100% !important" in css
    assert "margin-bottom: 0 !important" in css
    assert "calc(100vh - var(--dp-shell-header)" not in css


def test_e1_removes_post_render_downloads_owner() -> None:
    index = read("index.html")
    assert not (STATIC / "ui-downloads-runtime.js").exists()
    assert "ui-downloads-runtime.js" not in index
