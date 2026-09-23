"""1.0.13 Usenet integration configuration: ONE namespace, ONE enable state.

Usenet is a one-to-one provider/executor pairing. `integrations.usenet` is the
only persisted authority for both halves, and `integrations.usenet.enabled` is
the only enable state -- never two booleans kept synchronized.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from integrations.catalog import definitions, register
from integrations.definition import IntegrationEnvironment, IntegrationSettings
from integrations.usenet.definition import (
    MAX_CONNECTIONS, MAX_PRIORITY, MIN_CONNECTIONS, MIN_PRIORITY, PRIORITY_HELP,
    UsenetOptions, UsenetServer, definition as usenet_definition,
)
from transfers.registry import IntegrationRegistry


def environment(tmp_path):
    return IntegrationEnvironment(SimpleNamespace(authorize_execution=None), str(tmp_path))


def registry_for(tmp_path, *, enabled=True):
    settings = SimpleNamespace(integrations={
        item.id: IntegrationSettings(enabled=(enabled if item.id == "usenet" else True))
        for item in definitions})
    registry = IntegrationRegistry()
    register(registry, settings, environment(tmp_path),
             selected=[item for item in definitions if item.id == "usenet"])
    return registry


# --- one namespace, one enable state -------------------------------------

def test_definition_is_a_single_paired_integration():
    assert usenet_definition.id == "usenet"
    assert usenet_definition.kind == "provider_executor"
    assert usenet_definition.name == "Usenet"


def test_catalog_registers_both_halves_from_one_definition(tmp_path):
    registry = registry_for(tmp_path)
    assert "usenet" in registry.providers
    assert "sabnzbd" in registry.executors


@pytest.mark.parametrize("enabled", [True, False])
def test_one_enable_state_governs_both_halves(tmp_path, enabled):
    registry = registry_for(tmp_path, enabled=enabled)
    assert registry.providers["usenet"].descriptor.enabled is enabled
    assert registry.executors["sabnzbd"].descriptor.enabled is enabled


def test_there_is_no_second_usenet_settings_namespace():
    identities = {item.id for item in definitions}
    assert "sabnzbd" not in identities
    assert not any(name.startswith("usenet_") for name in identities)


# --- server model matches characterized SAB semantics --------------------

def test_server_bounds_are_the_native_ones_except_a_deliberate_connection_floor():
    """Priority matches SABnzbd 5.1.3 exactly; connections deliberately do not.

    SAB allows 0 connections because that is how SAB switches a server off.
    DebridPulse has its own per-server Enable control, so 0 would only produce
    a server that reads as on while being unable to open a single connection --
    configured, testable, and incapable of acquiring. The floor is therefore 1.
    """
    assert (MIN_CONNECTIONS, MAX_CONNECTIONS) == (1, 500)
    assert (MIN_PRIORITY, MAX_PRIORITY) == (0, 99)
    assert PRIORITY_HELP == "Lower values have priority."


def test_server_model_has_no_api_credential_field():
    """NNTP server auth is username/password only; an API field would be dead."""
    fields = set(UsenetServer.model_fields)
    assert {"host", "port", "ssl", "username", "password", "connections", "priority"} <= fields
    assert not any("api" in name for name in fields)


def test_out_of_range_server_values_are_rejected():
    for values in ({"connections": 501}, {"priority": 100}, {"port": 0}):
        with pytest.raises(Exception):
            UsenetServer(host="news.example.net", **values)


# --- secret projection ----------------------------------------------------

def test_public_projection_hides_every_server_password():
    """There is no operator service credential at all; server secrets are
    still projected as presence only."""
    options = {"servers": [{"host": "news.a.net", "username": "u", "password": "SERVER-SECRET"}]}
    public = usenet_definition.public_options(options)
    blob = json.dumps(public)
    assert "SERVER-SECRET" not in blob
    assert public["servers"][0]["password"] == ""
    assert public["servers"][0]["password_configured"] is True
    # The internal service contributes no public field whatsoever.
    for forbidden in ("api_key", "api_key_configured", "service_url"):
        assert forbidden not in public


def test_configured_requires_a_usable_news_server():
    """The bundled service always exists, so it is never evidence of
    configuration: only an enabled, addressable news server is."""
    assert usenet_definition.configured({}) is False
    assert usenet_definition.configured({"servers": []}) is False
    assert usenet_definition.configured(
        {"servers": [{"host": "", "enabled": True}]}) is False
    assert usenet_definition.configured(
        {"servers": [{"host": "news.a.net", "enabled": False}]}) is False
    assert usenet_definition.configured(
        {"servers": [{"host": "news.a.net", "enabled": True}]}) is True


def test_presentation_uses_a_real_readiness_endpoint_not_a_static_status():
    presentation = usenet_definition.presentation
    assert presentation.status_name == "Usenet"
    # A static status could not express "enabled but not yet configured".
    assert presentation.static_status is None
    assert presentation.status_endpoint == "/integration-status/usenet"
    # DP 1.0.13 work item L: display_order is the ONE ordering authority, and
    # the Provider Status tier order is derived from it -- named premium
    # services (AllDebrid, 10) before aggregate premium families (Usenet, 20)
    # before the general families (100+). The Settings card order is owned
    # separately by ui-settings-page.js and is unaffected.
    assert presentation.display_order == 20
    assert presentation.status_tier == "premium_family"
    assert presentation.status_tier_label == "Premium"


def test_display_name_defaults_to_derived_from_host():
    server = UsenetServer(host="news.example.net")
    assert server.display_name == ""
    from executors.sabnzbd.admin import derived_display_name
    assert derived_display_name(server) == "news.example.net"
    assert derived_display_name(UsenetServer(host="news.example.net",
                                             display_name="Primary")) == "Primary"


def test_usenet_is_off_until_an_operator_turns_it_on():
    """The normal state is OFF: enabling is what makes BOTH halves participate."""
    assert usenet_definition.default_enabled is False
    # Every pre-existing integration keeps its participating default.
    for item in definitions:
        if item.id != "usenet":
            assert item.default_enabled is True


def test_a_never_decided_namespace_takes_the_definition_default():
    from core.config import AppSettings
    from integrations.configuration import normalize_settings
    settings = normalize_settings(AppSettings(), definitions)
    assert settings.integrations["usenet"].enabled is False
    assert settings.integrations["alldebrid"].enabled is True


def test_an_explicit_operator_choice_always_wins_over_the_default():
    from core.config import AppSettings
    from integrations.configuration import normalize_settings
    settings = AppSettings()
    settings.integrations = {"usenet": IntegrationSettings(enabled=True)}
    assert normalize_settings(settings, definitions).integrations["usenet"].enabled is True


def test_options_default_to_an_empty_server_collection():
    assert UsenetOptions().servers == []
    assert UsenetOptions().operation_timeout_seconds == 30
    # No endpoint or credential is configurable at all.
    for forbidden in ("service_url", "api_key"):
        assert forbidden not in UsenetOptions.model_fields
