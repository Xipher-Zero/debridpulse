from core.config import AppSettings
from integrations.catalog import definitions
from integrations.configuration import normalize_settings, public_integrations


def public(settings):
    return public_integrations(normalize_settings(settings, definitions), definitions)


def test_provider_presentation_metadata_is_neutral_safe_and_deterministic():
    result = public(AppSettings())

    alldebrid = result["alldebrid"]
    assert alldebrid["kind"] == "provider"
    assert alldebrid["configured"] is False
    assert alldebrid["presentation"] == {
        "status_name": "AllDebrid",
        "premium": True,
        "status_endpoint": "/integration-status/alldebrid",
        "static_status": None,
        "display_order": 10,
    }
    assert alldebrid["options"]["api_key"] == ""
    assert alldebrid["options"]["api_key_configured"] is False

    general = result["general_http"]
    assert general["kind"] == "provider"
    assert general["configured"] is True
    assert general["presentation"] == {
        "status_name": "General Downloads",
        "premium": False,
        "status_endpoint": None,
        "static_status": "healthy",
        "display_order": 100,
    }


def test_persisted_secret_presence_establishes_configuration_without_disclosing_secret():
    result = public(AppSettings(alldebrid_api_key="private-key"))
    alldebrid = result["alldebrid"]
    assert alldebrid["configured"] is True
    assert alldebrid["options"]["api_key_configured"] is True
    assert alldebrid["options"]["api_key"] == ""
    assert "private-key" not in str(result)


def test_whitespace_secret_does_not_claim_configured_state():
    result = public(AppSettings(alldebrid_api_key="   "))
    assert result["alldebrid"]["configured"] is False


def test_executor_does_not_become_download_provider_status_candidate():
    result = public(AppSettings())
    assert result["aria2"]["kind"] == "executor"
    assert result["aria2"]["presentation"]["status_name"] is None
