"""Contracts for v1.0.11 universal frontend language and Downloads integration."""

from __future__ import annotations

import json
from pathlib import Path
import xml.etree.ElementTree as ET


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "frontend" / "static"
STYLE = STATIC / "style-v11.css"
TOKENS = STATIC / "ui-language-tokens.css"
UNIVERSAL = STATIC / "ui-universal-language.css"
DOWNLOADS = STATIC / "ui-downloads-page.css"
APP = STATIC / "app.js"
OPERATOR = STATIC / "operator-title.js"
ICON = STATIC / "icons" / "dp" / "card-download.svg"
REMOVED_ICON = STATIC / "icons" / "dp" / "green-download-button.svg"
MANIFEST = STATIC / "icons" / "dp" / "manifest.json"


def test_dashboard_derived_material_is_a_base_layer_not_a_last_guard() -> None:
    overlay = STYLE.read_text(encoding="utf-8")
    tokens = "/ui-language-tokens.css?v=21"
    universal = "/ui-universal-language.css?v=20"
    dashboard = "/ui-dashboard.css?v=20"
    statistics = "/ui-statistics-page.css?v=22"
    downloads = "/ui-downloads-page.css?v=28"
    help_page = "/ui-help-page.css?v=22"
    for layer in (tokens, universal, dashboard, statistics, downloads, help_page):
        assert layer in overlay
    assert overlay.index(tokens) < overlay.index(universal)
    assert overlay.index(universal) < overlay.index(dashboard)
    assert overlay.index(dashboard) < overlay.index(statistics)
    assert overlay.index(statistics) < overlay.index(downloads)
    assert overlay.index(downloads) < overlay.index(help_page)
    assert "ui-card-shell-final.css" not in overlay
    assert "ui-downloads-structural.css" not in overlay
    assert "ui-downloads-polish.css" not in overlay
    assert "ui-downloads-consistency.css" not in overlay
    assert "ui-downloads-shell-sync.css" not in overlay


def test_material_tokens_capture_approved_dashboard_surface_language() -> None:
    css = TOKENS.read_text(encoding="utf-8")
    required = (
        "--dp-panel-surface",
        "rgba(14, 19, 44, .94)",
        "--dp-panel-header-surface",
        "rgba(95, 48, 174, .26)",
        "--dp-panel-shadow",
        "-6px 8px 14px -8px rgba(0,0,0,.66)",
        "--dp-field-surface",
        "--dp-table-head-surface",
        "#1d1930",
        "#f2eff8",
        "--dp-segment-active-surface",
        "#7440bb",
        "--dp-primary-surface",
        "#8d48db",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"Dashboard-derived material tokens are missing: {missing}"


def test_universal_language_bridges_all_major_legacy_component_families() -> None:
    css = UNIVERSAL.read_text(encoding="utf-8")
    required = (
        ".dp-card, .card, .scard, .list-card",
        ".dp-metric-card, .metric-card, .stat-card, .dash-hero-stat, .dash-kpi",
        ".dp-field, .input",
        ".dp-tabs, .filter-tabs, .stabs",
        ".dp-tab, .ftab, .stab",
        ".dp-btn, .btn",
        ".dp-table, .t-table",
        ".badge",
        ".prog",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"Universal legacy bridge is missing: {missing}"
    assert "#view-dashboard" not in css
    assert "#view-torrents" not in css
    assert "#view-settings" not in css
    assert "#view-stats" not in css


def test_downloads_page_layer_is_page_specific_only() -> None:
    css = DOWNLOADS.read_text(encoding="utf-8")
    required = (
        "#view-torrents.active",
        ".dp-downloads-heading",
        ".filter-tabs",
        ".dp-downloads-refresh",
        ".dp-downloads-table-wrap",
        "#torrent-pagination",
        ".dp-pager-btn",
        "#torrent-page-info",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"Downloads page contract is missing: {missing}"

    forbidden = (
        "#7440bb",
        "#6336a9",
        "#553291",
        "#f2eff8",
        "#ebe8f3",
        "rgba(14, 19, 44, .94)",
        "rgba(142, 92, 225, .14)",
        "#view-torrents > .card::after",
    )
    present = [fragment for fragment in forbidden if fragment in css]
    assert not present, f"Downloads page reintroduced copied base material: {present}"


def test_downloads_app_carries_header_copy_search_and_empty_language() -> None:
    app = APP.read_text(encoding="utf-8")
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    required_app = (
        "download tracked", "downloads tracked", "No downloads yet. Add a link, magnet, or torrent file to get started.",
        "No downloads match your current filters.", "No downloads match your search.",
        "Showing all ", "matching downloads", "dp-pager-current", "chevronLeft", "chevronRight",
        "function renderTorrentPagination(", "function setFilter(",
    )
    missing = [fragment for fragment in required_app if fragment not in app]
    assert not missing, f"Downloads direct app owner is missing: {missing}"
    for fragment in ("card-download.svg?v=11", "Download Queue", "Search downloads…", "Refresh downloads", "dp-downloads-table-wrap"):
        assert fragment in index
    assert "card-document-stack.svg" not in index[index.index('id="view-torrents"'):index.index('<!-- Events -->')]
    assert "green-download-button.svg" not in index
    assert "api('POST'" in app
    assert "'DELETE'," in app
    assert "const confirmedIds = await confirmDownloadRemoval([id]);" in app
    assert "const targetId = confirmedIds[0];" in app
    assert "`/torrents/${targetId}?from_alldebrid=true`" in app

def test_downloads_correction_runtime_is_absent_from_canonical_document() -> None:
    operator = OPERATOR.read_text(encoding="utf-8")
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert not (STATIC / "ui-downloads-runtime.js").exists()
    assert "/ui-downloads-runtime.js" not in index
    assert "data-dp-downloads-runtime" not in index
    assert "data-dp-downloads-runtime" not in operator
    assert "/ui-downloads-runtime.js" not in operator

def test_downloads_header_uses_registered_download_true_vector() -> None:
    text = ICON.read_text(encoding="utf-8")
    root = ET.fromstring(text)
    assert root.tag.endswith("svg")
    assert "<image" not in text.lower()
    assert "data:image" not in text.lower()

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["icons"]["cardDownload"] == "card-download.svg"
    assert "greenDownloadButton" not in manifest["icons"]
    assert not REMOVED_ICON.exists()
