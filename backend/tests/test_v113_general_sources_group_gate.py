"""1.0.13: the canonical integration-GROUP gate.

`Network Sources` gains a master Enable that gates whether the group's children
participate at all, without ever editing a child's own stored preference.

    child desired      = integrations.<child>.enabled
    group desired      = integration_groups.<group>.enabled
    effective          = child desired AND group desired   (derived, read-only)

Nothing here names HTTP, FTP or "Network Sources" as an implementation fact: the
group identity is the one already published as
``presentation.status_group`` / ``status_group_label``.
"""
from __future__ import annotations

import pytest

from core.config import AppSettings
from integrations.catalog import definitions, register
from integrations.configuration import (
    normalize_settings, public_integrations,
)
from integrations.definition import IntegrationEnvironment, IntegrationSettings
from transfers.registry import IntegrationRegistry


GROUPED = tuple(d for d in definitions if d.presentation.status_group)


def group_of(identity: str) -> str:
    return next(d.presentation.status_group for d in definitions if d.id == identity)


def settings_with(**integrations) -> AppSettings:
    base = AppSettings(integrations={
        identity: IntegrationSettings(enabled=value) for identity, value in integrations.items()})
    return normalize_settings(base, definitions)


# --- the namespace exists and defaults open ------------------------------------

def test_the_group_gate_is_a_canonical_namespace():
    assert "integration_groups" in AppSettings.model_fields, \
        "no canonical integration-group namespace exists"


def test_an_upgraded_config_with_no_group_entry_defaults_enabled():
    """Absent means enabled, so upgrading preserves pre-master behaviour."""
    from integrations.configuration import group_enabled
    settings = settings_with()
    assert settings.integration_groups == {} or all(
        entry.enabled for entry in settings.integration_groups.values())
    for definition in GROUPED:
        assert group_enabled(settings, definition.presentation.status_group) is True


def test_known_groups_come_from_presentation_metadata_not_a_second_list():
    from integrations.configuration import known_groups
    groups = known_groups(definitions)
    assert groups == {d.presentation.status_group: d.presentation.status_group_label
                      for d in definitions if d.presentation.status_group}
    assert groups, "no integration declares a status group"


# --- the master never edits children -------------------------------------------

def test_master_off_leaves_child_preferences_byte_for_byte_unchanged():
    from integrations.configuration import set_group_enabled
    settings = settings_with(general_http=True, general_ftp=False)
    before = {d.id: settings.integrations[d.id].model_dump() for d in GROUPED}

    gated = set_group_enabled(settings, group_of("general_http"), False)

    after = {d.id: gated.integrations[d.id].model_dump() for d in GROUPED}
    assert after == before, "the master rewrote a child's stored preference"


def test_master_on_restores_participation_from_the_existing_child_booleans():
    from integrations.configuration import effective_enabled, set_group_enabled
    settings = settings_with(general_http=True, general_ftp=False)
    group = group_of("general_http")

    off = set_group_enabled(settings, group, False)
    assert effective_enabled(off, _d("general_http")) is False
    assert effective_enabled(off, _d("general_ftp")) is False

    on = set_group_enabled(off, group, True)
    assert effective_enabled(on, _d("general_http")) is True, "child ON did not return"
    assert effective_enabled(on, _d("general_ftp")) is False, "child OFF was forced ON"
    assert on.integrations["general_http"].enabled is True
    assert on.integrations["general_ftp"].enabled is False


def test_a_child_toggle_while_the_master_is_off_changes_only_the_child():
    from integrations.configuration import set_group_enabled
    group = group_of("general_ftp")
    off = set_group_enabled(settings_with(general_http=True, general_ftp=False), group, False)

    current = off.integrations["general_ftp"].model_dump()
    current["enabled"] = True
    flipped = off.model_copy(update={"integrations": {
        **off.integrations, "general_ftp": IntegrationSettings(**current)}})
    flipped = normalize_settings(flipped, definitions, previous=off)

    assert flipped.integrations["general_ftp"].enabled is True
    assert flipped.integration_groups[group].enabled is False, "the child toggle moved the master"


# --- effective participation is derived and read-only --------------------------

def test_public_projection_keeps_desired_state_and_adds_derived_effective_state():
    from integrations.configuration import set_group_enabled
    group = group_of("general_http")
    settings = set_group_enabled(settings_with(general_http=True, general_ftp=False), group, False)
    public = public_integrations(settings, definitions)

    assert public["general_http"]["enabled"] is True, \
        "the child toggle would visually flip OFF because the master is OFF"
    assert public["general_http"]["effective_enabled"] is False
    assert public["general_ftp"]["enabled"] is False
    assert public["general_ftp"]["effective_enabled"] is False


def test_public_group_projection_exposes_master_truth_and_its_members():
    from integrations.configuration import public_integration_groups, set_group_enabled
    group = group_of("general_http")
    settings = set_group_enabled(settings_with(), group, False)
    groups = public_integration_groups(settings, definitions)
    assert groups[group]["enabled"] is False
    assert groups[group]["label"] == _d("general_http").presentation.status_group_label
    assert set(groups[group]["members"]) >= {"general_http", "general_ftp"}


# --- runtime registration obeys the gate ---------------------------------------

def _d(identity):
    return next(d for d in definitions if d.id == identity)


def _registered(settings):
    """Real production registration, restricted to the resolution-only
    providers so the test needs no repository or managed service."""
    registry = IntegrationRegistry()
    selected = tuple(d for d in definitions if d.id in {"general_http", "general_ftp", "alldebrid"})
    register(registry, settings, IntegrationEnvironment(object(), "/tmp/dp-group-gate"),
             selected=selected)
    return {provider.descriptor.id: provider.descriptor.enabled
            for provider in registry.providers.values()}


def test_runtime_participation_respects_the_group_gate():
    from integrations.configuration import set_group_enabled
    group = group_of("general_http")
    on = settings_with(general_http=True, general_ftp=True)
    open_gate = _registered(on)
    assert open_gate["general_http"] is True
    assert open_gate["general_ftp"] is True

    off = set_group_enabled(on, group, False)
    gated = _registered(off)
    assert gated["general_http"] is False, "runtime ignored the group gate"
    assert gated["general_ftp"] is False
    # An integration outside the group is untouched -- whatever its own
    # participation was, the gate did not move it.
    assert gated["alldebrid"] == open_gate["alldebrid"], "an ungrouped integration was gated"
    assert off.integrations["general_http"].enabled is True, \
        "runtime gating rewrote the persisted child namespace"


def test_an_unknown_group_is_rejected():
    from integrations.configuration import set_group_enabled
    with pytest.raises(ValueError):
        set_group_enabled(settings_with(), "not_a_real_group", False)


# --- the broad settings write cannot stale-overwrite the gate ------------------

def test_the_broad_settings_route_carries_the_group_namespace_forward():
    import inspect
    from api import routes
    body = inspect.getsource(routes.update_settings)
    assert 'merged["integration_groups"] = previous.integration_groups' in body, \
        "a stale whole-settings write can replay an old group master"


def test_a_generic_group_mutation_route_exists():
    paths = {getattr(route, "path", "") for route in __import__("api.routes", fromlist=["router"]).router.routes}
    assert "/integration-groups/{group_id}/configuration" in paths, \
        "no generic scoped group mutation surface exists"
    assert not any("general" in path or "direct-sources" in path for path in paths), \
        "a group-specific endpoint was introduced"
