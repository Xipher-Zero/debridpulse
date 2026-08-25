"""Contracts for the v1.0.11 Downloads live-review polish and shell-sync passes."""

from __future__ import annotations

import json
from pathlib import Path
import xml.etree.ElementTree as ET


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "frontend" / "static"
STYLE = STATIC / "style-v11.css"
POLISH = STATIC / "ui-downloads-polish.css"
CONSISTENCY = STATIC / "ui-downloads-consistency.css"
SHELL_SYNC = STATIC / "ui-downloads-shell-sync.css"
RUNTIME = STATIC / "ui-downloads-runtime.js"
OPERATOR = STATIC / "operator-title.js"
ICON = STATIC / "icons" / "dp" / "card-document-stack.svg"
REMOVED_ICON = STATIC / "icons" / "dp" / "green-download-button.svg"
MANIFEST = STATIC / "icons" / "dp" / "manifest.json"


def test_downloads_layers_are_ordered_without_cache_generation_bump() -> None:
    overlay = STYLE.read_text(encoding="utf-8")
    structural = "/ui-downloads-structural.css?v=18"
    polish = "/ui-downloads-polish.css?v=18"
    consistency = "/ui-downloads-consistency.css?v=18"
    shell_sync = "/ui-downloads-shell-sync.css?v=18"
    for layer in (structural, polish, consistency, shell_sync):
        assert layer in overlay
    assert overlay.rfind(polish) > overlay.rfind(structural)
    assert overlay.rfind(consistency) > overlay.rfind(polish)
    assert overlay.rfind(shell_sync) > overlay.rfind(consistency)
    assert "?v=19" not in overlay


def test_downloads_polish_captures_reviewed_workspace_and_controls() -> None:
    css = POLISH.read_text(encoding="utf-8")
    required = (
        "#view-torrents.active",
        ":has(#view-torrents.active) .sidebar-footer",
        "bottom: 24px !important",
        ".dp-downloads-table-wrap",
        "position: sticky",
        ".dp-downloads-heading",
        "font-size: 16px",
        ".filter-tabs",
        ".ftab.active",
        "#7440bb",
        ".dp-downloads-refresh",
        "#torrent-pagination",
        "grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr)",
        ".dp-pager-current",
        "#8d55c1",
        "#torrent-page-info",
        "font-size: 13px !important",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"Downloads polish contract is missing: {missing}"


def test_downloads_shell_sync_matches_final_dashboard_section_card_contract() -> None:
    css = SHELL_SYNC.read_text(encoding="utf-8")
    required = (
        "background: linear-gradient(180deg, #ffffff, #f8f9fd) !important",
        "-6px 8px 14px -8px rgba(35, 41, 66, .40)",
        "border-bottom: 0 !important",
        "rgba(0,0,0,.18) 93%",
        "transparent 98%",
        "min-height: 70px !important",
        "width: 51px !important",
        "font-size: 10.5px !important",
        "#torrent-pagination",
        "border-top: 0 !important",
        "background: transparent !important",
        "#t-tbody tr:not([data-torrent-id]):hover",
        "width: 76px !important",
        "height: 76px !important",
        "height: calc(100vh - var(--dp-shell-header) - 14px) !important",
        ".sidebar-footer::before",
        "text-align: center !important",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"Downloads final shell-sync contract is missing: {missing}"


def test_downloads_runtime_carries_header_copy_search_and_empty_language() -> None:
    runtime = RUNTIME.read_text(encoding="utf-8")
    required = (
        "card-document-stack.svg?v=18",
        "All Downloads",
        "download tracked",
        "downloads tracked",
        "Search downloads…",
        "No downloads yet. Add a link, magnet, or torrent file to get started.",
        "No downloads match your current filters.",
        "No downloads match your search.",
        "document.getElementById('dash-tbody')",
        "torrent-page-size",
        "wrapper.remove()",
        "Showing all ",
        "matching downloads",
        "dp-pager-current",
        "chevronLeft",
        "chevronRight",
        "Refresh downloads",
        "dp-downloads-table-wrap",
    )
    missing = [fragment for fragment in required if fragment not in runtime]
    assert not missing, f"Downloads runtime contract is missing: {missing}"
    assert "green-download-button.svg" not in runtime
    assert "api('POST'" not in runtime
    assert "api('DELETE'" not in runtime


def test_downloads_runtime_is_loaded_without_touching_application_cache_generation() -> None:
    operator = OPERATOR.read_text(encoding="utf-8")
    assert "/ui-downloads-runtime.js?v=18" in operator
    assert "data-dp-downloads-runtime" in operator
    assert "?v=19" not in operator


def test_downloads_header_reuses_registered_recent_activity_true_vector() -> None:
    text = ICON.read_text(encoding="utf-8")
    root = ET.fromstring(text)
    assert root.tag.endswith("svg")
    assert "<image" not in text.lower()
    assert "data:image" not in text.lower()

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["icons"]["cardDocumentStack"] == "card-document-stack.svg"
    assert "greenDownloadButton" not in manifest["icons"]
    assert not REMOVED_ICON.exists()
