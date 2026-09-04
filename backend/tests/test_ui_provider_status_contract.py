"""Static guards for the neutral Provider Status presentation contract."""
from pathlib import Path


ROOT = Path(__file__).parents[2]
STATUS = (ROOT / "frontend" / "static" / "ui-provider-status.js").read_text()
ACCOUNT = (ROOT / "frontend" / "static" / "ui-alldebrid-account-status.js").read_text()
CARDS = (ROOT / "frontend" / "static" / "ui-provider-cards.js").read_text()
BOOTSTRAP = (ROOT / "frontend" / "static" / "ui-settings-card-icons.js").read_text()
ALLDEBRID_DEF = (ROOT / "backend" / "providers" / "alldebrid" / "definition.py").read_text()
GENERAL_DEF = (ROOT / "backend" / "providers" / "general_http" / "definition.py").read_text()


def test_provider_status_owner_is_provider_neutral():
    assert "alldebrid" not in STATUS.lower()
    assert "integration.kind === 'provider'" in STATUS
    assert "integration.enabled !== false" in STATUS
    assert "presentation.status_name" in STATUS
    assert "presentation.status_endpoint" in STATUS
    assert "presentation.static_status" in STATUS
    assert "No download providers enabled" in STATUS


def test_operational_health_is_not_synthesized_from_enabled_or_configured_state():
    assert "configured ? 'healthy'" not in STATUS
    assert "enabled ? 'healthy'" not in STATUS
    assert "staticStatus" in STATUS
    assert "await api('GET', candidate.endpoint)" in STATUS
    assert "state !== 'disabled'" in STATUS


def test_provider_specific_account_detail_is_isolated_from_neutral_owner():
    assert "alldebrid" in ACCOUNT.lower()
    assert "premiumUntil" in ACCOUNT
    assert "alldebrid" not in STATUS.lower()


def test_presentation_identity_and_health_sources_are_provider_owned():
    assert 'status_name="AllDebrid"' in ALLDEBRID_DEF
    assert 'status_endpoint="/integration-status/alldebrid"' in ALLDEBRID_DEF
    assert 'status_name="General Downloads"' in GENERAL_DEF
    assert 'static_status="healthy"' in GENERAL_DEF


def test_premium_card_owner_uses_persisted_configured_metadata_and_independent_disclosure():
    assert "integration.configured" in CARDS
    assert "data-integration-enabled" in CARDS
    assert "dp-settings-provider-disclosure" in CARDS
    assert "Configuration required" in CARDS
    assert "Provider configured" in CARDS
    assert "dirty(context)" in CARDS


def test_existing_settings_decorator_only_bootstraps_separate_provider_owners():
    assert "/ui-provider-status.js?v=1" in BOOTSTRAP
    assert "/ui-provider-cards.js?v=1" in BOOTSTRAP
    assert "/ui-provider-state.css?v=1" in BOOTSTRAP
    assert "DPProviderStatus" not in BOOTSTRAP
    assert "DPProviderCardState" not in BOOTSTRAP
