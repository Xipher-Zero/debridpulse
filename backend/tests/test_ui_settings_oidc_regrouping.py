from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"


def test_frontend_syntax_gate_tracks_shipped_top_level_javascript():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "find frontend/static -maxdepth 1 -name '*.js' -print0" in workflow
    assert "xargs -0 -n1 node --check" in workflow
    for retired in (
        "ui-presentation-loader.js",
        "ui-shell-runtime.js",
        "ui-help-chrome.js",
        "ui-settings-auth-resilience.js",
        "ui-settings-authentication.js",
        "ui-settings-authentication-polish.js",
        "ui-settings-authentication-oidc.js",
        "ui-settings-authentication-callback.js",
        "ui-visual-behavior-fixes.js",
        "ui-page-finalization.js",
    ):
        assert f"node --check frontend/static/{retired}" not in workflow
