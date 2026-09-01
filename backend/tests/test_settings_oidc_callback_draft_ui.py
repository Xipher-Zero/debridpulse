from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
CALLBACK_JS = STATIC / "ui-settings-authentication-callback.js"
POLISH_JS = STATIC / "ui-settings-authentication-polish.js"
LOADER = STATIC / "ui-presentation-loader.js"
SETTINGS = STATIC / "ui-settings-page.js"
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_oidc_callback_behavior_is_canonical_and_syntax_gate_is_dynamic():
    assert not CALLBACK_JS.exists()
    assert not POLISH_JS.exists()
    assert not LOADER.exists()
    settings = read(SETTINGS)
    assert "function callbackFromPublicBase(value)" in settings
    assert "function updateOidcCallbackPreview()" in settings
    workflow = read(WORKFLOW)
    assert "find frontend/static -maxdepth 1 -name '*.js' -print0" in workflow
    assert "xargs -0 -n1 node --check" in workflow
