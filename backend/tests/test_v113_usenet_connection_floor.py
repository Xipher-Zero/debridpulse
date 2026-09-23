"""1.0.13 Gate-9 rev-5, item 2: zero connections is never "ready".

SAB's own bound allows 0, and 0 is meaningful there because it is how SAB
switches a server off. DebridPulse does not need that meaning: it has an
explicit per-server Enable control, so a DP server carrying 0 connections is a
server the operator believes is on while the native side cannot open a single
connection to it.

The floor is therefore 1 for every configurable DP server, enforced on the
model, the mutation API, the frontend, the configured/readiness predicate and
Test Server -- and Test Server must NOT quietly coerce a stored 0 to 1, which
is exactly how a configuration tests green and then cannot acquire.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from integrations.usenet.definition import (
    MAX_CONNECTIONS, MIN_CONNECTIONS, UsenetOptions, UsenetServer, usable_servers,
)


# --- the model ------------------------------------------------------------

def test_the_connection_floor_is_one():
    assert MIN_CONNECTIONS == 1
    assert MAX_CONNECTIONS == 500


def test_a_server_cannot_be_built_with_zero_connections():
    with pytest.raises(ValidationError):
        UsenetServer(host="news.a.net", connections=0)


def test_a_server_cannot_be_built_above_the_ceiling():
    with pytest.raises(ValidationError):
        UsenetServer(host="news.a.net", connections=MAX_CONNECTIONS + 1)


def test_the_bounds_themselves_are_accepted():
    assert UsenetServer(host="news.a.net", connections=1).connections == 1
    assert UsenetServer(host="news.a.net", connections=500).connections == 500


# --- the readiness predicate ---------------------------------------------

def test_a_zero_connection_server_is_not_usable():
    """Hand-built dict, as a pre-1.0.13 config.json could still carry."""
    options = {"servers": [{"host": "news.a.net", "enabled": True, "connections": 0}]}
    assert usable_servers(options) == 0


def test_a_zero_connection_server_leaves_usenet_unconfigured():
    options = UsenetOptions.model_construct(
        servers=[UsenetServer.model_construct(host="news.a.net", enabled=True, connections=0,
                                              port=563, ssl=True, username="", password="",
                                              priority=0, display_name="")])
    assert options.configured() is False


def test_a_one_connection_server_is_usable():
    options = UsenetOptions(servers=[UsenetServer(host="news.a.net", connections=1)])
    assert usable_servers(options.model_dump()) == 1
    assert options.configured() is True


# --- the mutation API -----------------------------------------------------

def test_the_server_mutation_model_refuses_zero():
    from api.routes import UsenetServerUpdate

    with pytest.raises(ValidationError):
        UsenetServerUpdate(host="news.a.net", connections=0)


def test_the_test_server_request_model_refuses_zero():
    from api.settings_validation_routes import UsenetServerDraft

    with pytest.raises(ValidationError):
        UsenetServerDraft(host="news.a.net", connections=0)


# --- Test Server ----------------------------------------------------------

def test_test_server_does_not_coerce_a_stored_zero_to_one():
    """The decisive property: no "it tested fine" for a configuration that
    cannot acquire."""
    import inspect
    import textwrap

    from executors.sabnzbd.admin import SabnzbdAdministration

    source = textwrap.dedent(inspect.getsource(SabnzbdAdministration.test_server))
    body = source.split('"""')[-1] if '"""' in source else source
    assert "max(1," not in body.replace(" ", "").replace("max(1,", "max(1,"), (
        "Test Server must not silently raise a stored 0 to 1"
    )


@pytest.mark.asyncio
async def test_testing_a_zero_connection_server_is_refused():
    from executors.sabnzbd.admin import SabnzbdAdministration
    from sab_fakes import FakeSab

    admin = SabnzbdAdministration(FakeSab(), UsenetOptions(), "/download")
    result = await admin.test_server(host="news.a.net", port=563, ssl=True,
                                     username="u", password="p", connections=0)
    assert result.get("ok") is False, result
    assert "connection" in str(result.get("detail", "")).lower()


# --- the frontend ---------------------------------------------------------

def test_the_frontend_connection_input_has_a_floor_of_one():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2]
              / "frontend" / "static" / "ui-settings-usenet-servers.js").read_text()
    assert 'data-usenet-field="connections"' in source
    connection_input = [line for line in source.splitlines()
                        if 'data-usenet-field="connections"' in line]
    assert connection_input, "the connections input must exist"
    for line in connection_input:
        assert 'min="1"' in line, f'connections input must carry min="1": {line.strip()}'
        assert 'min="0"' not in line
