"""No premium service is switched on until the operator turns it on.

A fresh installation enables its Network Sources and download engines and
leaves every premium service (debrid services and Usenet) off -- AllDebrid
included. An explicitly saved choice is always respected, whichever way it
went and whatever the default is."""
from __future__ import annotations

from core.config import AppSettings
from integrations.catalog import definitions
from integrations.configuration import effective_enabled, normalize_settings
from integrations.definition import IntegrationSettings

PREMIUM = {"alldebrid", "debridlink", "premiumize", "realdebrid", "torbox", "usenet"}
NETWORK_SOURCES = {"general_http", "general_ftp", "general_scp", "general_rsync", "general_webdav", "multimeta",
                   "media"}


def fresh():
    return normalize_settings(AppSettings(), definitions)


def test_a_fresh_installation_switches_on_network_sources_and_no_premium_service():
    settings = fresh()
    by_id = {definition.id: definition for definition in definitions}
    assert {item for item, definition in by_id.items() if definition.presentation.premium} == PREMIUM
    for item in PREMIUM:
        assert settings.integrations[item].enabled is False, item
        assert not effective_enabled(settings, by_id[item]), item
    for item in NETWORK_SOURCES:
        assert settings.integrations[item].enabled is True, item
        assert effective_enabled(settings, by_id[item]), item


def test_a_saved_choice_wins_over_the_default_in_both_directions():
    saved = fresh()
    saved.integrations["alldebrid"] = IntegrationSettings(enabled=True)
    saved.integrations["general_http"] = IntegrationSettings(enabled=False)
    loaded = normalize_settings(AppSettings(**saved.model_dump()), definitions)
    assert loaded.integrations["alldebrid"].enabled is True
    assert loaded.integrations["general_http"].enabled is False
    # Saving an unrelated change keeps both choices.
    draft = AppSettings(**{**loaded.model_dump(), "download_folder": "/elsewhere"})
    again = normalize_settings(draft, definitions, previous=loaded)
    assert again.integrations["alldebrid"].enabled is True
    assert again.integrations["general_http"].enabled is False
