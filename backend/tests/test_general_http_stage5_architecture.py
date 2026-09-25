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

    # 1.0.13: the accepted-input capability is the typed candidate field, not
    # an opaque context entry.
    assert 'accepted_input_methods=(InputMethod.USERNAME_PASSWORD,)' in provider
    assert '"accepted_input_methods"' not in provider
    assert '"no-netrc": "true"' in executor
    assert '"http-auth-challenge": "true"' in executor
    assert '"http-user": "", "http-passwd": ""' in executor
    assert 'return auth_required(username_password()) if code == "24" else None' in executor
    assert 'InputMethod.USERNAME_PASSWORD not in candidate.accepted_input_methods' in executor


def test_item10_exposes_general_http_provider_ui_without_transport_tuning():
    settings_ui = (ROOT / "frontend/static/ui-settings-page.js").read_text()
    sources_panel = settings_ui.split("function sourcesPanel", 1)[1].split("\n  function ", 1)[0]
    # DP 1.0.13 final interaction pass: the member is a compact protocol BOX,
    # derived from the group it declares and labelled from its own presentation
    # metadata. HTTP(S) therefore reaches the page without this renderer naming
    # it, and the only thing keyed by the durable identity is the box copy.
    assert "sourceProtocolBox(" in sources_panel
    assert "Network Sources" in sources_panel
    general_http = settings_ui.split("const SOURCE_BOX_COPY", 1)[1].split("});", 1)[0]
    assert "general_http" in general_http
    assert "HTTP and HTTPS URLs." in general_http
    # Still no transport tuning anywhere on the member's surface: the box offers
    # its toggle and nothing else.
    box = settings_ui.split("function sourceProtocolBox(", 1)[1].split("\n  function ", 1)[0]
    for forbidden in ("User Agent", "Timeout", "Retry", "Proxy"):
        assert forbidden not in general_http, forbidden
        assert forbidden not in box, forbidden
