from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
STYLE_V11 = STATIC / "style-v11.css"
SPACING_CSS = STATIC / "ui-settings-download-engine-spacing.css"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_download_engine_body_uses_standard_settings_card_top_spacing():
    spacing = source(SPACING_CSS)
    imports = source(STYLE_V11)

    selector = (
        "body.dp-v11-structural #view-settings "
        ".dp-settings-download-engine-row {"
    )
    rule = spacing.split(selector, 1)[1].split("}", 1)[0]
    assert "margin-top: 0;" in rule

    page_import = "@import url('/ui-settings-page.css?v=2');"
    spacing_import = "@import url('/ui-settings-download-engine-spacing.css?v=20');"
    assert page_import in imports
    assert spacing_import in imports
    assert imports.index(spacing_import) > imports.index(page_import)
