from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_help_download_engine_tab_uses_user_facing_label():
    chrome = _read("ui-help-chrome.js")

    assert "aria2: 'Download Engine'" in chrome
    assert "LABELS[tab.dataset.tab]" in chrome


def test_license_closing_packaging_note_is_centered_and_emphasized():
    css = _read("ui-help-page.css")
    selector = "body.dp-v11-structural #view-help .dp-help-license-note p:last-child {"
    start = css.index(selector)
    end = css.index("\n}", start)
    block = css[start:end]

    assert "text-align: center" in block
    assert "font-weight: 650" in block
    assert "color: var(--dp-text-primary)" in block
