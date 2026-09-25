"""DP 1.0.13 post-Usenet corrective pass, work item A.

Every top-level Services Enable/Disable toggle is an IMMEDIATE
canonical operational control: flipping it persists the canonical enabled
state through the existing generic integration-configuration mutation and
wakes the lifecycle/routing maintenance that has to act on it. A visible
toggle can never report ON while canonical state is OFF.

The repair is at the canonical owner. There is no second enable endpoint and
no per-integration bypass: the same generic route and the same frontend path
serve every toggle of this class.
"""
import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api import routes
from core.config import AppSettings
from executors.aria2.definition import definition as aria2_definition
from integrations.definition import IntegrationSettings
from integrations.usenet.definition import definition as usenet_definition
from providers.alldebrid.definition import definition as alldebrid_definition
from providers.general_ftp.definition import definition as general_ftp_definition
from providers.general_http.definition import definition as general_http_definition

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"
SETTINGS_JS = (STATIC / "ui-settings-page.js").read_text(encoding="utf-8")

DEFINITIONS = (alldebrid_definition, general_http_definition, general_ftp_definition,
               usenet_definition, aria2_definition)

# Every integration whose participation the operator switches from a
# Services header toggle.
TOGGLED_INTEGRATIONS = ("alldebrid", "usenet", "general_http", "general_ftp")


@asynccontextmanager
async def _noop():
    yield


class _Wakeups:
    def __init__(self):
        self.applicability = []

    def __call__(self, integration_id):
        self.applicability.append(integration_id)


def _application(wakeups):
    return SimpleNamespace(
        definitions=DEFINITIONS,
        application_operation=lambda: _noop(),
        configure=lambda: None,
        integration_admin=lambda _identity: SimpleNamespace(apply_memory_tuning=AsyncMock()),
        apply_integration_configuration=AsyncMock(return_value=None),
        validate_configuration=AsyncMock(),
        notify_applicability_changed=wakeups,
    )


def _settings():
    return AppSettings(integrations={
        "alldebrid": IntegrationSettings(enabled=True, options={"api_key": "secret-key"}),
        "general_http": IntegrationSettings(enabled=True, options={}),
        "general_ftp": IntegrationSettings(enabled=True, options={}),
        "usenet": IntegrationSettings(enabled=False, options={
            "operation_timeout_seconds": 30,
            "servers": [{"id": "abc", "host": "news.example.com", "port": 563, "ssl": True,
                         "username": "u", "password": "p", "connections": 8, "priority": 0,
                         "enabled": True, "display_name": ""}],
        }),
    })


async def _patch_enabled(integration_id, enabled, *, current=None, wakeups=None):
    current = current if current is not None else _settings()
    wakeups = wakeups if wakeups is not None else _Wakeups()
    application = _application(wakeups)
    saved = {}
    with patch("api.routes.get_settings", return_value=current), \
         patch("api.routes.load_settings", return_value=current), \
         patch("api.routes.save_settings", side_effect=lambda cfg: saved.__setitem__("cfg", cfg)), \
         patch("api.routes.apply_settings"):
        result = await routes.patch_integration_configuration(
            integration_id,
            routes.IntegrationConfigurationUpdate(enabled=enabled),
            application=application,
        )
    return result, saved.get("cfg"), wakeups


# --- canonical persistence, for every toggle of this class ----------------

@pytest.mark.parametrize("integration_id", TOGGLED_INTEGRATIONS)
@pytest.mark.parametrize("enabled", (True, False))
@pytest.mark.asyncio
async def test_enable_only_payload_persists_canonical_state(integration_id, enabled):
    result, saved, _ = await _patch_enabled(integration_id, enabled)
    assert saved.integrations[integration_id].enabled is enabled
    assert result["enabled"] is enabled


@pytest.mark.asyncio
async def test_enable_only_payload_preserves_every_option_of_that_namespace():
    """An immediate toggle carries no options, so the merge must keep them."""
    _, saved, _ = await _patch_enabled("usenet", True)
    servers = saved.integrations["usenet"].options["servers"]
    assert [s["host"] for s in servers] == ["news.example.com"]
    assert servers[0]["password"] == "p"
    assert saved.integrations["usenet"].options["operation_timeout_seconds"] == 30
    # And every sibling namespace is untouched.
    assert saved.integrations["alldebrid"].options["api_key"] == "secret-key"


# --- the missing lifecycle/routing wake ------------------------------------

@pytest.mark.parametrize("integration_id", TOGGLED_INTEGRATIONS)
@pytest.mark.asyncio
async def test_canonical_configuration_mutation_wakes_lifecycle_and_routing(integration_id):
    """RED before this pass: the canonical route persisted the enable state and
    reconfigured, but never woke integration maintenance, so a managed service
    converged only on the next 60 s cadence and the operator saw an unreachable
    service after enabling it."""
    _, _, wakeups = await _patch_enabled(integration_id, True)
    assert wakeups.applicability == [integration_id]


@pytest.mark.asyncio
async def test_the_wake_is_neutral_and_names_no_integration():
    source = (Path(routes.__file__)).read_text(encoding="utf-8")
    block = source[source.index("async def patch_integration_configuration("):
                   source.index("@router.get(\"/stats/comprehensive\")")]
    assert "notify_applicability_changed(integration_id)" in block
    for named in ("usenet", "sabnzbd", "alldebrid", "nzb", "nntp"):
        assert f'"{named}"' not in block.replace('integration_id == "aria2"', "")


# --- frontend: one shared immediate path, no surviving deferred writer ------

def _function(name, source=SETTINGS_JS):
    """The exact source of one named function, parameter list included."""
    start = source.index(f"function {name}(")
    # Skip the parameter list (it may itself contain braces) before counting.
    depth, cursor = 0, source.index("(", start)
    for position in range(cursor, len(source)):
        if source[position] == "(":
            depth += 1
        elif source[position] == ")":
            depth -= 1
            if depth == 0:
                cursor = position
                break
    depth, body = 0, source.index("{", cursor)
    for position in range(body, len(source)):
        if source[position] == "{":
            depth += 1
        elif source[position] == "}":
            depth -= 1
            if depth == 0:
                return source[start:position + 1]
    raise AssertionError(f"unterminated function {name}")


def test_the_enable_change_handler_persists_through_the_generic_route():
    handler = _function("providerEnableChanged")
    assert "/integrations/" in handler and "/configuration" in handler
    assert "enabled:" in handler
    # One generic path: the identity comes from the control, never a branch.
    assert "dataset.integrationEnabled" in handler
    for named in ("'usenet'", '"usenet"', "'alldebrid'", '"alldebrid"'):
        assert named not in handler


def test_a_failed_enable_mutation_restores_committed_state_and_reports_it():
    handler = _function("providerEnableChanged")
    assert "catch" in handler
    assert "notify(" in handler
    # The control is re-set from the cached canonical namespace, never left
    # holding the operator's optimistic click.
    assert "state.settings?.integrations" in handler or "state.settings" in handler


def test_apply_settings_no_longer_owns_integration_enablement():
    """No second enable writer may survive beside the immediate control."""
    # DP 1.0.13: no integration has a deferred payload at all, so none of them
    # can carry a participation field.
    for name in ("usenetConfigurationPayload", "aria2ConfigurationPayload",
                 "allDebridConfigurationPayload"):
        assert f"function {name}(" not in SETTINGS_JS, name
    table = SETTINGS_JS[SETTINGS_JS.index("const COMMIT_FIELDS"):]
    table = table[:table.index("});") + 3]
    # The invariant is that PARTICIPATION is not a declared field, so the
    # declared KEYS are what this reads -- an ordinary boolean whose name merely
    # ends in `_enabled` (Automatic Extraction) is not participation.
    declared = re.findall(r"^\s{4}([a-z0-9_]+):\s*\{", table, re.M)
    assert "enabled" not in declared, "participation became an ordinary declared field"
    persist = _function("persistNonAuth")
    assert "data-integration-enabled" not in persist
    assert "enabled: !!enabled.checked" not in persist


def test_every_current_toggle_of_this_class_uses_the_shared_path():
    """Every toggle of this class is rendered by ONE control owner, so they
    cannot drift.

    DP 1.0.13 final interaction pass: the two expandable Premium Services cards
    come from providerCard(); the Network Sources members are compact protocol
    boxes from sourceProtocolBox(), derived from the group they declare rather
    than named here. Both composers render the SAME
    ``integrationHeaderToggle()``, which is what makes the shared immediate
    path shared -- so the invariant is the one control owner, not one card
    composer."""
    cards = re.findall(r"providerCard\('([a-z_]+)'", SETTINGS_JS)
    assert sorted(cards) == ["alldebrid", "usenet"]
    assert "sourceProtocolBox(" in SETTINGS_JS
    # One toggle owner: its definition plus the two composers that render it.
    assert SETTINGS_JS.count("integrationHeaderToggle(") == 3
    for composer in ("function providerCard(", "function sourceProtocolBox("):
        block = SETTINGS_JS[SETTINGS_JS.index(composer):]
        block = block[:block.index("\n  function ", 1)]
        assert "integrationHeaderToggle(" in block, composer
        assert "data-integration-enabled" not in block, \
            f"{composer} emits its own toggle instead of the shared one"


def test_ordinary_settings_fields_remain_deferred():
    """Scope boundary: the deferred footer still owns the tabs that have not
    been migrated.

    DP 1.0.13 Settings consolidation migrated Extraction, so its four values
    left this payload and became declared field-boundary controls like every
    Downloads value before them. Notifications and Data & Maintenance still
    commit through the footer, and that is what this case holds.
    """
    persist = _function("persistNonAuth")
    assert "PUT" in persist and "/settings" in persist
    payload = _function("nonAuthPayload")
    assert "discord_notify_added: boolOf('discord_notify_added')" in payload
    assert "backup_interval_hours: intOf('backup_interval_hours', 24)" in payload
    # Nothing Extraction owns is read from the page here any more.
    for name in ("extract_enabled", "extract_delete_archive", "extract_max_concurrent",
                 "extraction_password"):
        assert name not in payload, name
