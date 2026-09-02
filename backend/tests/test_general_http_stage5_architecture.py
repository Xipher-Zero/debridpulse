"""Roadmap Item 5 architecture and credential-discovery guardrails."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_general_http_provider_remains_resolution_only_and_debrid_agnostic():
    source = (ROOT / "backend/providers/general_http/provider.py").read_text()
    assert "executors." not in source
    assert "providers.alldebrid" not in source
    assert "alldebrid" not in source.casefold()
    assert "requests." not in source
    assert "aiohttp" not in source
    assert "httpx" not in source
    assert "urlopen" not in source
    assert "curl" not in source.casefold()


def test_http_credentials_are_execution_local_and_saved_netrc_discovery_is_disabled():
    provider = (ROOT / "backend/providers/general_http/provider.py").read_text()
    executor = (ROOT / "backend/executors/aria2/executor.py").read_text()

    assert '"accepted_input_methods": ("username_password",)' in provider
    assert '"no-netrc": "true"' in executor
    assert '"http-auth-challenge": "true"' in executor
    assert '"http-user": "", "http-passwd": ""' in executor
    assert 'observed.error.native_code == "24"' in executor
    assert 'InputMethod.USERNAME_PASSWORD.value in accepted' in executor


def test_item10_exposes_general_http_provider_ui_without_transport_tuning():
    settings_ui = (ROOT / "frontend/static/ui-settings-page.js").read_text()
    sources_panel = settings_ui.split("function sourcesPanel", 1)[1].split("\n  function ", 1)[0]
    assert "general_http" in sources_panel
    assert "HTTP & HTTPS" in sources_panel
    assert "General Sources" in sources_panel
    for forbidden in ("User Agent", "Timeout", "Retry", "Proxy"):
        assert forbidden not in sources_panel
