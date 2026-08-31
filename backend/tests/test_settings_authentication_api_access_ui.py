from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
RUNTIME = STATIC / "ui-settings-authentication.js"
STYLE = STATIC / "ui-settings-authentication.css"
SETTINGS = STATIC / "ui-settings-page.js"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")






def test_api_access_action_and_token_field_share_control_centerline():
    css = source(STYLE)

    assert "grid-template-rows: auto var(--dp-input-height) auto;" in css
    assert ".dp-settings-api-token-layout.has-token .dp-settings-api-token-actions" in css
    assert "grid-row: 2;" in css
    assert ".dp-settings-api-token-field" in css
    assert "min-height: var(--dp-input-height);" in css
    assert "align-items: center;" in css
