from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
SETTINGS_PAGE_JS = STATIC / "ui-settings-page.js"
SETTINGS_PAGE_CSS = STATIC / "ui-settings-page.css"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_download_engine_card_matches_reviewed_mode_and_layout_contract():
    runtime = source(SETTINGS_PAGE_JS)
    css = source(SETTINGS_PAGE_CSS)
    downloads = runtime[runtime.index("function downloadsPanel"):runtime.index("function extractionPanel")]

    assert "card('Download Engine'" in downloads
    assert "aria2 Delivery" not in downloads
    assert "'Mode Selection'" in downloads
    assert "['builtin', 'Built-in aria2']" in downloads
    assert "['external', 'External aria2']" in downloads
    assert "Choose where DebridPulse sends downloads. Built-in aria2 runs with DebridPulse; External aria2 uses your existing aria2 server." in downloads
    assert "data-download-path-mode=\"builtin\"" in downloads
    assert "data-download-path-mode=\"external\"" in downloads
    assert "Where DebridPulse saves downloads." in downloads
    assert "Path your external aria2 server uses for the shared download folder on that server." in downloads
    assert "Maximum number of downloads DebridPulse can run at the same time." in downloads
    assert "el.dataset.downloadPathMode !== mode" in runtime

    assert ".dp-settings-download-engine-mode" in css
    assert ".dp-settings-download-engine-copy" in css
    assert ".dp-settings-download-engine-row" in css
    assert "grid-template-columns: minmax(0, 60%) minmax(240px, 320px);" in css
    assert ".dp-settings-download-limit" in css
    assert "justify-self: end;" in css
