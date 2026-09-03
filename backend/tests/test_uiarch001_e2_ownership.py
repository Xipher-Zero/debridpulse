from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_provider_status_has_one_canonical_style_owner() -> None:
    style = read("style-v11.css")
    provider = read("ui-shell-provider-status.css")
    assert not (STATIC / "ui-shell-provider-status-v2.css").exists()
    assert style.count("ui-shell-provider-status.css") == 1
    assert "ui-shell-provider-status-v2.css" not in style
    for fragment in (
        "gap: 5px !important", "width: max-content !important",
        "margin: 0 -8px !important", "background-size: 36px 36px !important",
        ".dp-provider-premium-until", ".dp-provider-premium-days",
    ):
        assert fragment in provider


def test_activity_downloads_and_provider_markup_are_final_at_source() -> None:
    index = read("index.html")
    app = read("app.js")
    assert '<span class="nav-label">Activity Log</span>' in index
    assert '<span class="nav-label">Event Log</span>' not in index
    assert "events:'Activity Log'" in app
    pagination = index[index.index('id="torrent-pagination"'):index.index('id="torrent-page-info"')]
    assert "border-top" not in pagination
    assert 'class="dp-provider-premium-until"' in app
    assert 'class="dp-provider-premium-days"' in app
    assert "AllDebrid Premium until" in app
    assert "days remaining" in app


def test_accessibility_runtime_does_not_repair_canonical_presentation() -> None:
    source = read("ui-accessibility-runtime.js")
    for forbidden in (
        "normalizeActivityNaming", "normalizeDownloadsLegacyPresentation",
        "normalizeProviderPremiumLabel", "installProviderStatusPresentation",
        "dpProviderObserved", "lbl-premium", "torrent-pagination",
    ):
        assert forbidden not in source
    for required in (
        "aria-current", "aria-pressed", "dp-dropdown-shell",
        "MutationObserver", "installUniversalSelectDropdowns",
    ):
        assert required in source


def test_e1_retired_runtimes_remain_absent() -> None:
    index = read("index.html")
    for retired in ("ui-runtime.js", "ui-downloads-runtime.js"):
        assert not (STATIC / retired).exists()
        assert retired not in index
