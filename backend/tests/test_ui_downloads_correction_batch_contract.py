"""Canonical Dashboard / Downloads / Activity ownership contracts."""
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
