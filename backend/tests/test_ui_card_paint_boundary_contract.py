from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read_static(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_downloads_outer_view_does_not_clip_universal_card_paint():
    css = read_static("ui-downloads-page.css")

    assert "body.dp-v11-structural #view-torrents.active" in css
    assert "overflow: visible;" in css
    assert "body.dp-v11-structural #view-torrents .dp-downloads-table-wrap" in css
    assert "overflow: auto !important;" in css

    desktop_rule = css.split("body.dp-v11-structural #view-torrents.active", 1)[1].split("}", 1)[0]
    assert "overflow: hidden" not in desktop_rule


def test_help_moves_scroll_boundary_inside_the_card_body():
    css = read_static("ui-help-page.css")

    assert "#view-help .help-panels-wrap" in css
    assert "overflow: visible;" in css
    assert ".help-panel.active > .card" in css
    assert ".help-panel.active > .card > .card-body" in css
    assert "overflow-y: auto;" in css


def test_help_and_downloads_corrections_do_not_redefine_card_material():
    combined = read_static("ui-downloads-page.css") + read_static("ui-help-page.css")

    forbidden = (
        "--dp-panel-surface:",
        "--dp-panel-frame:",
        "--dp-panel-shadow:",
        "box-shadow: var(--dp-panel-shadow)",
        "background: var(--dp-panel-surface)",
    )
    for token in forbidden:
        assert token not in combined


def test_card_paint_boundary_page_layers_are_loaded_after_universal_language():
    overlay = read_static("style-v11.css")

    universal = overlay.index("/ui-universal-language.css?v=20")
    downloads = overlay.index("/ui-downloads-page.css?v=22")
    help_page = overlay.index("/ui-help-page.css?v=22")

    assert universal < downloads < help_page
