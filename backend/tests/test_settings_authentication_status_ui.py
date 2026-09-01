from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
RUNTIME = STATIC / "ui-settings-authentication.js"
STYLE = STATIC / "ui-settings-authentication.css"
SETTINGS = STATIC / "ui-settings-page.js"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")










def test_authentication_status_session_row_has_desktop_and_mobile_layout_contracts():
    css = source(STYLE)

    assert ".dp-settings-auth-status-card .dp-settings-auth-session-row" in css
    assert "grid-template-columns: minmax(220px, 1fr) minmax(270px, 1.15fr) minmax(260px, .9fr) auto;" in css
    assert "@media (max-width: 900px)" in css
    assert "grid-template-columns: minmax(0, 1fr);" in css
