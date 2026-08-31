from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OIDC_JS = ROOT / "frontend" / "static" / "ui-settings-authentication-oidc.js"
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"






def test_oidc_regrouping_runtime_is_in_frontend_syntax_gate():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "node --check frontend/static/ui-settings-authentication-oidc.js" in workflow
