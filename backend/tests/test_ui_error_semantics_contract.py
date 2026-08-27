from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_error_semantics_runtime_is_loaded_by_first_paint_bootstrap():
    bootstrap = read("ui-theme-bootstrap.js")
    assert "/ui-error-semantics.js?v=20" in bootstrap
    assert "data-dp-error-semantics" in bootstrap


def test_concise_failure_taxonomy_is_complete():
    runtime = read("ui-error-semantics.js")
    expected = {
        "Unsupported Host",
        "Source Unavailable",
        "Invalid Link",
        "Link Unavailable",
        "Link Timeout",
        "Magnet Rejected",
        "Torrent Rejected",
        "Provider Expired",
        "Provider Unreachable",
        "Provider Sync Failed",
        "Provider Auth Failed",
        "Queue Failed",
        "Downloader Offline",
        "Download Failed",
        "Disk Full",
        "Write Failed",
        "Extraction Failed",
        "Provider Error",
    }
    for label in expected:
        assert label in runtime
    assert "LINK_HOST_NOT_SUPPORTED" in runtime
    assert "/api/torrents/" in runtime


def test_terminal_failure_progress_preserves_actual_percent():
    runtime = read("ui-error-semantics.js")
    assert "failed && actual === 0 ? 100 : actual" in runtime
    assert "data-dp-actual-progress" in runtime
    assert "data-dp-visual-progress" in runtime
    assert "actual.toFixed(0) + '%'" in runtime

    css = read("ui-live-review-batch.css")
    assert ".prog-fill.error" in css
    assert "var(--dp-state-error)" in css


def test_dark_dashboard_cards_receive_subdued_colored_shadow():
    css = read("ui-live-review-batch.css")
    assert "rgba(84, 38, 131, .18)" in css
    assert "rgba(167, 139, 250, .06)" in css
    assert "#view-dashboard .dash-hero-stat" in css
    assert "#view-dashboard .dp-dashboard-quick-add" in css
    assert "#view-dashboard .dp-dashboard-activity" in css


def test_details_scrollbar_has_no_increment_decrement_buttons():
    css = read("ui-modal-contract.css")
    assert ".modal-body::-webkit-scrollbar-button" in css
    assert "display: none" in css
    assert "width: 0" in css
    assert "height: 0" in css
