"""Canonical Details overlay and final Downloads cleanup contracts."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_visible_download_total_excludes_soft_deleted_rows() -> None:
    app = read("app.js")
    assert ".filter(([status]) => status !== 'deleted')" in app
    assert ".reduce((sum, [, count]) => sum + (Number(count) || 0), 0)" in app
    assert "const queuePct = pct(completed, total || 0);" in app


def test_bulk_toolbar_right_side_owns_count_and_clear_selection() -> None:
    app = read("app.js")
    runtime = read("ui-downloads-runtime.js")
    page = read("ui-downloads-page.css")
    index = read("index.html")
    view = index[index.index('id="view-torrents"'):index.index('<!-- Events -->')]
    assert "_selectedIds.size + ' Selected'" in app
    assert "'Clear Selections', 'x', 'dp-downloads-bulk-action--clear'" in runtime
    assert view.index("bulkAction('pause',this)") < view.index("bulkAction('resume',this)")
    assert view.index("bulkAction('resume',this)") < view.index("bulkAction('reset',this)")
    assert view.index("bulkAction('reset',this)") < view.index("bulkAction('delete',this)")
    assert 'class="dp-downloads-bulk-status"' in view
    assert 'id="bulk-count" class="dp-downloads-bulk-count"' in view
    assert 'onclick="clearSelection()">Clear Selections</button>' in view
    assert "actions.append(" not in runtime
    assert "status.append(" not in runtime
    assert "gap: 10px;" in page


def test_details_modal_consumes_canonical_card_primitives() -> None:
    index = read("index.html")
    modal = read("ui-modal-contract.css")
    assert 'id="modal" class="dp-card dp-detail-modal"' in index
    assert 'class="modal-hdr dp-card__header"' in index
    assert 'class="modal-title dp-card__title"' in index
    assert 'class="modal-body dp-card__body"' in index
    assert 'class="btn modal-close dp-detail-close"' in index
    assert 'M18 6 6 18' in index
    assert "background: var(--dp-panel-surface) !important;" in modal
    assert "background: var(--dp-panel-header-surface);" in modal
    assert "var(--dp-panel-shadow)" in modal


def test_details_files_and_events_are_canonical_section_cards() -> None:
    app = read("app.js")
    modal = read("ui-modal-contract.css")
    assert "card dp-detail-section-card dp-detail-files-card" in app
    assert "card dp-detail-section-card dp-detail-events-card" in app
    assert '<table class="t-table">' in app
    assert "dp-detail-events-list" in app
    assert "var(--dp-table-row-border)" in modal
    assert "var(--dp-table-row-hover)" in modal
