from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
OIDC_JS = STATIC / "ui-settings-authentication-oidc.js"
OIDC_CSS = STATIC / "ui-settings-authentication-oidc.css"
LOADER = STATIC / "ui-presentation-loader.js"
SETTINGS_JS = STATIC / "ui-settings-page.js"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")
















def test_oidc_clear_secret_helper_collapses_unused_lower_half_of_input_slot():
    css = read(OIDC_CSS)
    assert "--dp-oidc-clear-copy-line: 13.75px;" in css
    assert "line-height: var(--dp-oidc-clear-copy-line);" in css
    assert "margin-top: calc(7px - ((var(--dp-input-height) - var(--dp-oidc-clear-copy-line)) / 2));" in css


def test_oidc_access_separator_is_strengthened_without_added_weight_or_accent():
    css = read(OIDC_CSS)
    assert "border-top: 1px solid color-mix(in srgb, var(--dp-border, var(--border)) 65%, var(--dp-text-muted) 35%);" in css
    assert "border-top: 2px" not in css
