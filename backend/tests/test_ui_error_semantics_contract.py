from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


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


def test_terminal_failure_progress_is_rendered_by_app_and_enriched_without_override():
    app = read("app.js")
    runtime = read("ui-error-semantics.js")

    # The canonical renderer owns first-pass terminal-failure geometry directly.
    for fragment in (
        "function progress(pct, status)",
        "const visual = actual;",
        "dp-terminal-error-progress",
        "dp-terminal-error-rail",
        "data-dp-actual-progress",
        "data-dp-visual-progress",
        "actual.toFixed(0) + '%'",
        "background:var(--dp-state-error)!important",
        "background-image:none!important",
    ):
        assert fragment in app

    # Error semantics may enrich already-rendered failed rows, but it may not
    # replace the canonical renderer or observe the page into convergence.
    assert "function installProgressOverride" not in runtime
    assert "window.progress =" not in runtime
    assert "MutationObserver" not in runtime
    assert "const visualWidth = pct;" in runtime
    assert "track.classList.add('dp-terminal-error-rail')" in runtime
    assert "fill.classList.remove('done')" in runtime
    assert "setProperty('background', 'var(--dp-state-error)', 'important')" in runtime
    assert "setProperty('background-image', 'none', 'important')" in runtime
    assert "failed && actual === 0 ? 100 : actual" not in app
    assert "failed && actual === 0 ? 100 : actual" not in runtime

    css = read("ui-visual-accents.css")
    assert ".prog.dp-terminal-error-rail" in css
    assert "overflow: visible !important" in css
    assert "border-radius: 999px !important" in css
    assert ".prog.dp-terminal-error-rail::before" in css
    assert ".prog.dp-terminal-error-rail::after" in css
    assert "width: 16px" in css
    assert "height: 9px" in css
    assert ".prog-fill.error" in css
    assert "background: var(--dp-state-error) !important" in css
    assert "background-image: none !important" in css
    assert "var(--dp-state-error) 88%" in css
    assert "var(--dp-state-error) 46%" in css


def test_error_semantics_uses_explicit_render_events_without_startup_spin():
    runtime = read("ui-error-semantics.js")
    assert "function startAfterCore()" in runtime
    assert "core render helpers unavailable" in runtime
    assert "debridpulse:dashboard-recent-rendered" in runtime
    assert "debridpulse:downloads-rendered" in runtime
    assert "setTimeout(startWhenReady" not in runtime
    assert "window.setTimeout(startWhenReady" not in runtime
    assert "function startWhenReady()" not in runtime


def test_dark_dashboard_cards_receive_subdued_colored_shadow():
    css = read("ui-visual-accents.css")
    assert "rgba(84, 38, 131, .18)" in css
    assert "rgba(167, 139, 250, .06)" in css
    assert "#view-dashboard .dash-hero-stat" in css
    assert "#view-dashboard .dp-dashboard-quick-add" in css
    assert "#view-dashboard .dp-dashboard-activity" in css


def test_details_scrollbar_has_no_increment_decrement_buttons():
    modal_css = read("ui-modal-contract.css")
    review_css = read("ui-visual-accents.css")
    assert ".modal-body::-webkit-scrollbar-button" in modal_css
    assert "@supports selector(::-webkit-scrollbar)" in review_css
    assert "::-webkit-scrollbar-button:vertical:decrement" in review_css
    assert "::-webkit-scrollbar-button:vertical:increment" in review_css
    assert "display: none !important" in review_css
    assert "width: 0 !important" in review_css
    assert "height: 0 !important" in review_css
