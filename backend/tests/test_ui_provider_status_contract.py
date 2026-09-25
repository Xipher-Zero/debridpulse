"""Static guards for the neutral Provider Status presentation contract."""
from pathlib import Path


ROOT = Path(__file__).parents[2]
STATUS = (ROOT / "frontend" / "static" / "ui-provider-status.js").read_text()
ACCOUNT = (ROOT / "frontend" / "static" / "ui-alldebrid-account-status.js").read_text()
# The provider card (structure, status, collapse control) belongs to the Settings owner.
CARDS = (ROOT / "frontend" / "static" / "ui-settings-page.js").read_text()
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
    assert 'status_name="HTTP(S)"' in GENERAL_DEF
    assert 'status_group="direct_sources"' in GENERAL_DEF
    assert 'status_group_label="Network Sources"' in GENERAL_DEF
    assert 'static_status="healthy"' in GENERAL_DEF


def test_premium_card_owner_uses_persisted_configured_metadata_and_independent_disclosure():
    assert "entry.configured" in CARDS
    assert "data-integration-enabled" in CARDS
    # DP 1.0.13 work item C: ONE canonical disclosure component, shared with
    # the executor-tuning cards and placed immediately after the title.
    assert "dp-settings-disclosure" in CARDS
    assert "settingsDisclosure(" in CARDS
    # DP 1.0.13 Services final corrective pass: the header reports
    # the three CONFIGURATION states and never repeats what the Enable toggle
    # beside it already says. ``Verified`` is durable canonical truth about the
    # current saved configuration, not a local memory of a Test.
    assert "Unconfigured" in CARDS
    assert "Configured" in CARDS
    assert "Verified" in CARDS
    assert "Configuration required" not in CARDS
    assert "Provider configured" not in CARDS
    assert "const dirty =" in CARDS
    assert "controlSignature(" in CARDS


def test_application_shell_explicitly_loads_the_single_provider_owners():
    # DP 1.0.12 canonical flattening (Gate 9 round 3 fix-forward): the styling
    # owner is loaded through style.css's @import graph, not a second
    # top-level <link>; index.html only carries the JS runtimes directly.
    style = (ROOT / "frontend" / "static" / "style.css").read_text()
    assert 'id="provider-status-list"' in INDEX
    assert INDEX.count('id="provider-status-list"') == 1
    assert '/ui-provider-status.js?v=4' in INDEX
    assert '/ui-alldebrid-account-status.js?v=2' in INDEX
    assert 'ui-provider-cards' not in INDEX
    assert not (ROOT / "frontend" / "static" / "ui-provider-cards.js").exists()
    assert "ui-provider-state.css" not in INDEX
    assert "@import url('/ui-provider-state.css" in style


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
