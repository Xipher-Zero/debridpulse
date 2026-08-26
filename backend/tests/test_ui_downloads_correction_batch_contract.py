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
    controls = read("ui-dashboard-control-polish.css")
    page = read("ui-downloads-page.css")
    downloads = read("ui-downloads-runtime.js")
    runtime = read("ui-runtime.js")
    geometry = 'M20 11a8.1 8.1 0 0 0-15.5-2M4 4v5h5'
    assert geometry in runtime
    assert geometry in downloads
    assert "#view-torrents .dp-downloads-refresh" in controls
    assert "refresh.innerHTML = utilitySvg('refresh') + '<span>Refresh</span>';" in downloads
    assert "width: 38px" not in page
    assert "display: inline-grid !important" not in page
    assert ".dp-downloads-refresh svg {" not in page


def test_bulk_selection_is_header_only_card_with_reviewed_action_order() -> None:
    runtime = read("ui-downloads-runtime.js")
    page = read("ui-downloads-page.css")
    transfer = read("ui-transfer-contract.css")
    assert "bar.classList.add('dp-card', 'dp-downloads-bulk-card')" in runtime
    assert "header.className = 'dp-card__header dp-downloads-bulk-toolbar'" in runtime
    assert "actions.append(pause, resume, reset, separator, remove);" in runtime
    assert "status.append(count, clear);" in runtime
    for icon_name in ("pause:", "play:", "trash:", "x:"):
        assert icon_name in runtime
    for cls in (
        "dp-downloads-bulk-action--pause",
        "dp-downloads-bulk-action--resume",
        "dp-downloads-bulk-action--reset",
        "dp-downloads-bulk-action--delete",
        "dp-downloads-bulk-action--clear",
    ):
        assert cls in runtime
    assert "#bulk-bar.dp-downloads-bulk-card.visible" in page
    assert "dp-downloads-bulk-separator" in page
    assert "dp-downloads-bulk-status" in page
    assert "dp-downloads-bulk-action--pause" in transfer
    assert "dp-downloads-bulk-action--resume" in transfer
    assert "dp-downloads-bulk-action--reset" in transfer


def test_pagination_renders_only_applicable_neighbors_and_current_page() -> None:
    runtime = read("ui-downloads-runtime.js")
    assert "if (cur > 1)" in runtime
    assert "if (cur < totalPages)" in runtime
    assert "aria-current=\"page\"" in runtime
    assert "btns.innerHTML = controls.join('');" in runtime
    assert "const pages = []" not in runtime
    assert "cur <= 1 ? ' disabled'" not in runtime
    assert "cur >= totalPages ? ' disabled'" not in runtime


def test_batch_cache_generations_are_explicit() -> None:
    style = read("style-v11.css")
    operator = read("operator-title.js")
    index = read("index.html")
    assert "/style-v11.css?v=24" in index
    assert "/ui-runtime.js?v=24" in operator
    assert "/ui-dashboard-control-polish.css?v=23" in style
    assert "/ui-downloads-page.css?v=27" in style
    assert "/ui-feature-icon-contract.css?v=2" in style
    assert "/ui-transfer-contract.css?v=31" in style
    assert "/ui-downloads-runtime.js?v=22" in operator
    assert "/operator-title.js?v=23" in index
