from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "backend" / "tests"


def write(name: str, content: str) -> None:
    (TESTS / name).write_text(content, encoding="utf-8")


# E1 deliberately deletes the two post-render correction runtimes. Rewrite the
# architecture contract around direct canonical owners while preserving the
# independent bootstrap, statistics-I/O, Settings-owner, and error-semantics
# invariants that remain valid after the ownership change.
write(
    "test_ui_runtime_architecture_contract.py",
    r'''"""Canonical frontend runtime ownership contracts for v1.0.12."""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"

_STATS_DETAIL_IO_PATTERNS = (
    re.compile(r"""\bapi\(\s*['\"]GET['\"]\s*,\s*['\"]/stats/detail"""),
    re.compile(r"""\bfetch\(\s*['\"]/stats/detail"""),
    re.compile(r"""\brequest\(\s*['\"]GET['\"]\s*,\s*['\"]/stats/detail"""),
)


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def all_js_files() -> list[Path]:
    return sorted(STATIC.glob("*.js"))


def statistics_detail_io_owners() -> list[str]:
    owners = []
    for path in all_js_files():
        source = path.read_text(encoding="utf-8")
        if any(pattern.search(source) for pattern in _STATS_DETAIL_IO_PATTERNS):
            owners.append(path.name)
    return owners


def normalized(path: str) -> str:
    return urlsplit(path).path


def test_first_paint_bootstrap_does_not_own_application_io_or_page_state() -> None:
    bootstrap = read("ui-theme-bootstrap.js")
    assert "localStorage.getItem('theme')" in bootstrap
    for forbidden in (
        "fetch(", "/api/", "XMLHttpRequest", "EventSource", "MutationObserver",
        "loadDetailedStats", "loadSettings",
    ):
        assert forbidden not in bootstrap


def test_statistics_detail_endpoint_has_one_frontend_io_owner() -> None:
    owners = statistics_detail_io_owners()
    assert len(owners) == 1, f"Statistics detail I/O has multiple owners: {owners}"


def test_settings_page_is_authoritative_clean_room_owner() -> None:
    settings = read("ui-settings-page.js")
    assert "window.DPSettingsPage = Object.freeze({load});" in settings
    assert "window.loadSettings = load;" in settings
    assert "view.innerHTML =" in settings
    assert "request('GET', '/settings'" in settings
    assert "request('GET', '/auth/config'" in settings
    for forbidden in (
        "window.renderSettings =", "window.getFormSettings =",
        "window.switchSettingsTab =", "settingsObserver",
        "observeSettingsForm", "new MutationObserver",
    ):
        assert forbidden not in settings


def test_loaded_runtime_markers_are_unique_when_a_presentation_loader_exists() -> None:
    loader_path = STATIC / "ui-presentation-loader.js"
    if not loader_path.exists():
        return
    loader = loader_path.read_text(encoding="utf-8")
    runtime_paths = re.findall(r"""src:\s*['\"]([^'\"]+\.js(?:\?[^'\"]*)?)['\"]""", loader)
    style_paths = re.findall(r"""href:\s*['\"]([^'\"]+\.css(?:\?[^'\"]*)?)['\"]""", loader)
    markers = re.findall(r"""marker:\s*['\"]([^'\"]+)['\"]""", loader)
    normalized_runtimes = [normalized(path) for path in runtime_paths]
    normalized_styles = [normalized(path) for path in style_paths]
    assert len(normalized_runtimes) == len(set(normalized_runtimes))
    assert len(normalized_styles) == len(set(normalized_styles))
    assert len(markers) == len(set(markers))


def test_error_semantics_does_not_busy_poll_for_core_helpers() -> None:
    error = read("ui-error-semantics.js")
    assert "window.DPFailureSemantics = Object.freeze" in error
    assert "setTimeout" not in error
    assert "addEventListener" not in error


def test_shell_and_downloads_have_direct_canonical_owners() -> None:
    index = read("index.html")
    app = read("app.js")
    icons = read("operator-title.js")
    for retired in ("ui-runtime.js", "ui-downloads-runtime.js"):
        assert not (STATIC / retired).exists()
        assert retired not in index
        assert retired not in icons
    for forbidden in (
        "new MutationObserver", "createElement('script')", "bindThemeToggle",
        "decorateNavigation", "ensureRuntime",
    ):
        assert forbidden not in icons
    assert "function renderTorrentPagination(" in app
    assert "function setFilter(" in app
    assert "function updateDownloadsTrackedCopy(" in app
    assert "dp-downloads-detail-row" in app
    assert 'draggable="true"' not in app
    assert 'data-dp-ui="v1.0.12-canonical"' in index


def test_canonical_icon_insertions_use_one_lucide_geometry_owner() -> None:
    index = read("index.html")
    app = read("app.js")
    icons = read("operator-title.js")
    for icon in ("upload", "refresh", "arrowRight", "pause", "play", "trash2", "x"):
        assert f'data-dp-lucide="{icon}"' in index
        assert f"{icon}:" in icons
    assert "window.DPIcons.svg" in app
    assert "const LUCIDE" in icons


def test_archived_runtime_layers_do_not_reappear() -> None:
    index = read("index.html")
    joined = "\n".join(path.read_text(encoding="utf-8") for path in all_js_files())
    for retired in (
        "sidebar-v2.js", "hamburger-v2.js", "provider-ui.js",
        "ui-runtime.js", "ui-downloads-runtime.js",
    ):
        assert retired not in index
        assert retired not in joined
''',
)


write(
    "test_ui_downloads_correction_batch_contract.py",
    r'''"""Canonical Dashboard / Downloads / Activity ownership contracts."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_activity_document_keeps_51px_box_with_optical_padding_only() -> None:
    css = read("ui-feature-icon-contract.css")
    assert "--dp-feature-icon-size: 51px" in css
    assert "#view-events .dp-activity-title-icon" in css
    assert "padding: 3px !important" in css


def test_downloads_refresh_uses_shared_canonical_icon_owner() -> None:
    controls = read("ui-utility-controls.css")
    page = read("ui-downloads-page.css")
    index = read("index.html")
    icons = read("operator-title.js")
    assert "const LUCIDE" in icons
    assert "refresh:" in icons
    assert 'class="btn btn-ghost btn-sm dp-downloads-refresh"' in index
    assert 'data-default-label="Refresh"' in index
    assert 'data-dp-lucide="refresh"' in index
    assert "#view-torrents .dp-downloads-refresh" in controls
    assert "width: 38px" not in page
    assert "display: inline-grid !important" not in page
    assert ".dp-downloads-refresh svg {" not in page


def test_bulk_selection_is_integrated_static_band_with_reviewed_action_order() -> None:
    icons = read("operator-title.js")
    page = read("ui-downloads-page.css")
    transfer = read("ui-transfer-contract.css")
    index = read("index.html")
    downloads = index[index.index('id="view-torrents"'):index.index('<!-- Events -->')]
    assert 'class="dp-card dp-downloads-bulk-card dp-downloads-bulk-integrated" id="bulk-bar"' in downloads
    assert downloads.index('id="torrent-search"') < downloads.index('id="bulk-bar"') < downloads.index('class="dp-downloads-table-wrap"')
    assert downloads.index("bulkAction('pause',this)") < downloads.index("bulkAction('resume',this)") < downloads.index("bulkAction('reset',this)") < downloads.index("bulkAction('delete',this)")
    assert 'id="bulk-count" class="dp-downloads-bulk-count"' in downloads
    for label, icon in (("Pause", "pause"), ("Resume", "play"), ("Reset", "refresh"), ("Delete", "trash2"), ("Clear Selections", "x")):
        assert f'data-default-label="{label}"' in downloads
        assert f'data-dp-lucide="{icon}"' in downloads
        assert f"{icon}:" in icons
    assert "#bulk-bar.dp-downloads-bulk-card.visible" in page
    assert "dp-downloads-bulk-separator" in page
    assert "dp-downloads-bulk-status" in page
    assert "dp-downloads-bulk-action--pause" in transfer
    assert "dp-downloads-bulk-action--resume" in transfer
    assert "dp-downloads-bulk-action--reset" in transfer


def test_downloads_behavior_is_owned_directly_by_app() -> None:
    app = read("app.js")
    for fragment in (
        "function renderTorrentPagination(", "function setFilter(",
        "function updateDownloadsTrackedCopy(", "function downloadEmptyMessage(",
        "No Downloads Currently Processing", "No Downloads Completed Yet",
        "dp-downloads-detail-row", 'data-default-label="Pause"',
        'data-default-label="Resume"', 'data-default-label="Retry"',
        'data-default-label="Remove"',
    ):
        assert fragment in app
    assert 'draggable="true"' not in app
    assert "ondragstart=" not in app


def test_e1_correction_runtimes_are_retired() -> None:
    index = read("index.html")
    icons = read("operator-title.js")
    for name in ("ui-runtime.js", "ui-downloads-runtime.js"):
        assert not (STATIC / name).exists()
        assert name not in index
        assert name not in icons
''',
)


write(
    "test_ui_downloads_final_contract.py",
    r'''"""Final desktop Downloads consistency contracts after E1 canonicalization."""
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
''',
)


# This focused E1 test is generated by the source applicator. Rewrite it
# completely rather than depending on quotation shape in an intermediate file.
write(
    "test_uiarch001_e1_ownership.py",
    r'''from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_e1_correction_layers_are_physically_absent_and_unreferenced() -> None:
    joined = "\n".join(path.read_text(encoding="utf-8") for path in STATIC.glob("*.js"))
    index = read("index.html")
    for retired in ("ui-runtime.js", "ui-downloads-runtime.js"):
        assert not (STATIC / retired).exists()
        assert retired not in index
        assert retired not in joined


def test_icon_owner_has_no_loader_observer_or_dom_reparenting() -> None:
    icons = read("operator-title.js")
    for forbidden in (
        "MutationObserver", "createElement('script')", "appendChild(script)",
        "bindThemeToggle", "decorateNavigation",
    ):
        assert forbidden not in icons


def test_shell_structure_is_static_and_download_rows_are_final_at_render_time() -> None:
    index = read("index.html")
    app = read("app.js")
    assert 'data-dp-ui="v1.0.12-canonical"' in index
    assert "topbar-theme-control" in index
    assert "dp-dashboard-quick-add" in index
    assert "dp-dashboard-activity" in index
    assert "dp-activity-card" in index
    assert "dp-downloads-card-title" in index
    assert "dp-downloads-detail-row" in app
    assert 'draggable="true"' not in app
    assert "ondragstart=" not in app
    assert "function renderTorrentPagination(" in app
    assert "function setFilter(" in app
''',
)
