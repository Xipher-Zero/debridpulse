"""Static guards for the neutral Provider Status presentation contract."""
from pathlib import Path


ROOT = Path(__file__).parents[2]
STATUS = (ROOT / "frontend" / "static" / "ui-provider-status.js").read_text()
ACCOUNT = (ROOT / "frontend" / "static" / "ui-alldebrid-account-status.js").read_text()
CARDS = (ROOT / "frontend" / "static" / "ui-provider-cards.js").read_text()
BOOTSTRAP = (ROOT / "frontend" / "static" / "ui-settings-card-icons.js").read_text()
APP = (ROOT / "frontend" / "static" / "app.js").read_text()
INDEX = (ROOT / "frontend" / "static" / "index.html").read_text()
ALLDEBRID_DEF = (ROOT / "backend" / "providers" / "alldebrid" / "definition.py").read_text()
GENERAL_DEF = (ROOT / "backend" / "providers" / "general_http" / "definition.py").read_text()


def test_provider_status_owner_is_provider_neutral():
    lower = STATUS.lower()
    assert "alldebrid.com" not in lower
    assert "candidate.id === 'alldebrid'" not in lower
    assert "entry.id === 'alldebrid'" not in lower
    assert 'data-provider-id="alldebrid"' not in lower
    assert "integration.kind === 'provider'" in STATUS
    assert "integration.enabled !== false" in STATUS
    assert "presentation?.status_name" in STATUS
    assert "presentation.status_endpoint" in STATUS
    assert "presentation.static_status" in STATUS
    assert "No download providers enabled" in STATUS


def test_operational_health_is_derived_from_member_observations_not_enablement():
    assert "configured ? 'healthy'" not in STATUS
    assert "enabled ? 'healthy'" not in STATUS
    assert "enabled.every(entry => entry.state === 'healthy')" in STATUS
    assert "enabled.some(entry => ['unhealthy', 'auth_required'].includes(entry.state))" in STATUS
    assert "enabled.length !== entries.length" in STATUS
    assert "await api('GET', candidate.endpoint)" in STATUS
    assert "candidate.staticStatus" in STATUS


def test_provider_specific_account_detail_is_isolated_from_neutral_owner():
    assert "candidate.id === 'alldebrid'" in ACCOUNT
    assert "premiumUntil" in ACCOUNT
    assert "AllDebrid Premium until" in ACCOUNT
    assert "alldebrid.com" not in ACCOUNT.lower()
    assert "alldebrid.com" not in STATUS.lower()
    assert "candidate.id === 'alldebrid'" not in STATUS.lower()


def test_presentation_identity_and_direct_source_group_are_provider_owned():
    assert 'status_name="AllDebrid"' in ALLDEBRID_DEF
    assert 'status_endpoint="/integration-status/alldebrid"' in ALLDEBRID_DEF
    assert 'status_name="HTTP & HTTPS"' in GENERAL_DEF
    assert 'status_group="direct_sources"' in GENERAL_DEF
    assert 'status_group_label="Direct Sources"' in GENERAL_DEF
    assert 'static_status="healthy"' in GENERAL_DEF


def test_premium_card_owner_uses_persisted_configured_metadata_and_independent_disclosure():
    assert "integration.configured" in CARDS
    assert "data-integration-enabled" in CARDS
    assert "dp-settings-provider-disclosure" in CARDS
    assert "Configuration required" in CARDS
    assert "Provider configured" in CARDS
    assert "const dirty=" in CARDS or "const dirty =" in CARDS
    assert "signature(" in CARDS


def test_application_shell_explicitly_loads_the_single_provider_owners():
    assert 'id="provider-status-list"' in INDEX
    assert INDEX.count('id="provider-status-list"') == 1
    assert '/ui-provider-status.js?v=2' in INDEX
    assert '/ui-alldebrid-account-status.js?v=2' in INDEX
    assert '/ui-provider-cards.js?v=2' in INDEX
    assert '/ui-provider-state.css?v=2' in INDEX
    assert '/ui-provider-status.js' not in BOOTSTRAP
    assert '/ui-provider-cards.js' not in BOOTSTRAP


def test_legacy_alldebrid_provider_status_owner_is_removed():
    for symbol in (
        'allDebridStatusGeneration',
        'invalidateAllDebridStatus',
        'renderAllDebridStatus',
        'loadAllDebridStatus',
        '_updatePremiumLabel',
    ):
        assert symbol not in APP
    assert "setDot('api'" not in APP
    assert 'AllDebrid: checking' not in INDEX
    assert 'id="dot-api"' not in INDEX
    assert 'href="https://alldebrid.com"' not in INDEX
    assert 'loadAllDebridStatus' not in STATUS
    assert 'invalidateAllDebridStatus' not in STATUS
    assert 'invalidateProviderStatus();' in APP
    assert 'await refreshProviderStatus();' in APP
