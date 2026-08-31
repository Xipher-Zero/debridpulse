from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
CALLBACK_JS = STATIC / "ui-settings-authentication-callback.js"
POLISH_JS = STATIC / "ui-settings-authentication-polish.js"
LOADER = STATIC / "ui-presentation-loader.js"
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")












def test_callback_runtime_is_in_frontend_syntax_gate():
    workflow = read(WORKFLOW)
    assert "node --check frontend/static/ui-settings-authentication-callback.js" in workflow
