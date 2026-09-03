"""Static UIARCH-002 guards against generic provider-truth synthesis."""
from pathlib import Path
import re


APP = (Path(__file__).parents[2] / "frontend" / "static" / "app.js").read_text()


def body(name: str, next_name: str) -> str:
    pattern = rf"(?:async )?function {re.escape(name)}\([^)]*\) \{{(.*?)(?=(?:async )?function {re.escape(next_name)}\()"
    match = re.search(pattern, APP, re.S)
    assert match, f"could not isolate {name}()"
    return match.group(1)


def test_stats_success_never_sets_alldebrid_status():
    section = body("loadStats", "goToTorrentPage")
    assert "setDot('api'" not in section
    assert 'AllDebrid:' not in section


def test_connection_check_uses_provider_specific_status_endpoint():
    section = body("checkConnections", "setDot")
    assert "await loadAllDebridStatus()" in section
    assert "/stats" not in section


def test_provider_endpoint_failure_renders_unknown_not_unhealthy():
    section = body("loadAllDebridStatus", "checkConnections")
    assert "renderAllDebridStatus({state:'unknown'})" in section
    assert "setDot('api', 'error'" not in section


def test_generic_stats_startup_failure_cannot_mark_alldebrid_error():
    init = APP[APP.index("// ── Init") : APP.index("// ── Extraction Password List")]
    assert "AllDebrid: Error" not in init
    assert "checkPremiumStatus" not in init


def test_no_legacy_generic_online_literal_or_stats_comment_remains():
    assert "AllDebrid: online" not in APP
    assert "AllDebrid dot is already set by loadStats" not in APP


def test_renderer_has_truthful_neutral_and_provider_states():
    section = body("renderAllDebridStatus", "loadAllDebridStatus")
    for state in ("disabled", "unconfigured", "auth_required", "healthy", "unhealthy", "unknown"):
        assert state in section
