from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
STYLE_V11 = STATIC / "style-v11.css"
SETTINGS_CSS = STATIC / "ui-settings-page.css"
SPACING_CSS = STATIC / "ui-settings-download-engine-spacing.css"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_download_engine_spacing_is_owned_by_canonical_settings_stylesheet():
    settings = source(SETTINGS_CSS)
    imports = source(STYLE_V11)

    selector = (
        "body.dp-v11-structural #view-settings "
        ".dp-settings-download-engine-row {"
    )
    assert selector in settings
    base_rule = settings.split(selector, 1)[1].split("}", 1)[0]
    assert "margin-top: 0;" in base_rule
    assert "margin-top: 18px;" not in base_rule

    # A responsive media-query rule legitimately reuses the canonical selector
    # for grid-column behavior. Consolidation removes only the external override.
    assert "ui-settings-download-engine-spacing.css" not in imports
    assert not SPACING_CSS.exists()
