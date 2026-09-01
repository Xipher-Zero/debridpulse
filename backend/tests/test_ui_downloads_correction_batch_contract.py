"""Final Dashboard / Downloads / Activity correction batch contracts."""

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


def test_downloads_refresh_uses_shared_recovery_control_and_exact_glyph() -> None:
    controls = read("ui-utility-controls.css")
    page = read("ui-downloads-page.css")
    downloads = read("ui-downloads-runtime.js")
    runtime = read("ui-runtime.js")
    icons = read("operator-title.js")
    geometry = 'M20 11a8.1 8.1 0 0 0-15.5-2M4 4v5h5'

    # Exact geometry lives once in the canonical Lucide runtime. Page runtimes
    # consume that authority instead of carrying their own duplicate SVG paths.
    assert geometry in icons
    assert "window.DPIcons" in runtime
    assert "window.DPIcons" in downloads
    assert "const paths =" not in runtime
    assert "const paths =" not in downloads
    assert "normalizeUtilityButton(document.getElementById('btn-recover-all'), 'refresh')" in runtime
    assert "normalizeUtilityButton(refresh, 'refresh')" in runtime
    assert "refresh.innerHTML = utilitySvg('refresh') + '<span>Refresh</span>';" in downloads
    assert "#view-torrents .dp-downloads-refresh" in controls
    assert "width: 38px" not in page
    assert "display: inline-grid !important" not in page
    assert ".dp-downloads-refresh svg {" not in page


def test_bulk_selection_is_integrated_static_band_with_reviewed_action_order() -> None:
    runtime = read("ui-downloads-runtime.js")
    icons = read("operator-title.js")
    page = read("ui-downloads-page.css")
    transfer = read("ui-transfer-contract.css")
    index = read("index.html")
    downloads = index[index.index('id="view-torrents"'):index.index('<!-- Events -->')]
    assert 'class="dp-card dp-downloads-bulk-card dp-downloads-bulk-integrated" id="bulk-bar"' in downloads
    assert downloads.index('id="torrent-search"') < downloads.index('id="bulk-bar"') < downloads.index('class="dp-downloads-table-wrap"')
    assert downloads.index("bulkAction('pause',this)") < downloads.index("bulkAction('resume',this)") < downloads.index("bulkAction('reset',this)") < downloads.index("bulkAction('delete',this)")
    assert 'class="dp-downloads-bulk-status"' in downloads
    assert 'id="bulk-count" class="dp-downloads-bulk-count"' in downloads
    assert 'onclick="clearSelection()">Clear Selections</button>' in downloads
    bulk_owner = runtime[runtime.index('function decorateBulkSelectionToolbar'):runtime.index('function filterStatusFromTab')]
    assert "bar.replaceChildren(header)" not in runtime
    assert "bar.classList.add('dp-card'" not in runtime
    assert "insertBefore(bar" not in runtime
    assert "document.createElement('div')" not in bulk_owner
    for icon_name in ("pause:", "play:", "refresh:", "trash2:", "x:"):
        assert icon_name in icons
    for usage in ("'Pause', 'pause'", "'Resume', 'play'", "'Reset', 'refresh'", "'Delete', 'trash2'", "'Clear Selections', 'x'"):
        assert usage in runtime
    assert "const paths =" not in runtime
    assert "#bulk-bar.dp-downloads-bulk-card.visible" in page
    assert "Canonical integrated multi-selection strip" not in page
    assert "dp-downloads-bulk-separator" in page
    assert "dp-downloads-bulk-status" in page
    assert "dp-downloads-bulk-action--pause" in transfer
    assert "dp-downloads-bulk-action--resume" in transfer
    assert "dp-downloads-bulk-action--reset" in transfer



def test_batch_cache_generations_are_explicit() -> None:
    style = read("style-v11.css")
    operator = read("operator-title.js")
    index = read("index.html")
    assert "/style-v11.css?v=25" in index
    assert "/ui-runtime.js?v=24" in operator
    assert "/ui-utility-controls.css?v=23" in style
    assert "/ui-downloads-page.css?v=28" in style
    assert "/ui-feature-icon-contract.css?v=4" in style
    assert "/ui-transfer-contract.css?v=31" in style
    assert "data-dp-downloads-runtime" not in operator
    assert "/ui-downloads-runtime.js?v=24" in index
    assert "/operator-title.js?v=24" in index
