"""1.0.13 Gate-9 remediation, finding 2: per-server identity and secret safety.

A news server is a durable, individually-editable record carrying a credential.
Editing one card must not disturb another, a redacted (blank) password must
preserve the stored one for the SAME canonical server, and removing a server
must not damage the credentials of the survivors.

Identity is canonical and stable -- never a list index, never a hostname, never
anything recovered from the service's masked readback.
"""
from __future__ import annotations

import pytest

from integrations.definition import IntegrationSettings
from integrations.usenet.definition import UsenetOptions, UsenetServer, definition as usenet
from integrations.usenet.servers import (
    SERVER_NOT_FOUND, ServerMutationError, create_server, merge_server, remove_server,
)


def options(*servers) -> UsenetOptions:
    return UsenetOptions(service_url="http://sab:8080", api_key="k", servers=list(servers))


def server(**kw) -> UsenetServer:
    base = dict(host="news.a.net", username="u", password="p", connections=8, priority=0)
    base.update(kw)
    return UsenetServer(**base)


# --- canonical identity ---------------------------------------------------

def test_every_server_gets_a_stable_canonical_id():
    one, two = server(host="a.net"), server(host="b.net")
    assert one.id and two.id and one.id != two.id
    # The id is stable across ordinary field edits.
    edited = one.model_copy(update={"host": "renamed.net", "priority": 5})
    assert edited.id == one.id


def test_identity_is_not_derived_from_index_or_host():
    first = server(host="same.net")
    second = server(host="same.net")
    assert first.id != second.id, "two servers on one host are still two records"


def test_a_supplied_id_is_preserved_through_validation():
    fixed = server(id="abc123")
    assert UsenetOptions(servers=[fixed]).servers[0].id == "abc123"


# --- blank password preserves, explicit clear erases ----------------------

def test_a_blank_password_preserves_the_stored_one_for_the_same_server():
    stored = server(password="SECRET")
    current = options(stored)
    updated = merge_server(current, stored.id, {"host": "news.b.net", "password": ""})
    assert updated.servers[0].password == "SECRET"
    assert updated.servers[0].host == "news.b.net"


def test_a_supplied_password_replaces_the_stored_one():
    stored = server(password="SECRET")
    updated = merge_server(options(stored), stored.id, {"password": "NEW"})
    assert updated.servers[0].password == "NEW"


def test_an_explicit_clear_erases_the_stored_password():
    stored = server(password="SECRET")
    updated = merge_server(options(stored), stored.id, {"password": ""}, clear_password=True)
    assert updated.servers[0].password == ""


def test_a_supplied_password_wins_over_a_contradictory_clear():
    stored = server(password="SECRET")
    updated = merge_server(options(stored), stored.id, {"password": "NEW"}, clear_password=True)
    assert updated.servers[0].password == "NEW"


# --- one card is one record ----------------------------------------------

def test_saving_one_server_never_touches_another():
    one = server(host="a.net", password="PW-A", connections=4)
    two = server(host="b.net", password="PW-B", connections=9)
    updated = merge_server(options(one, two), one.id, {"host": "a2.net", "password": ""})
    first = next(s for s in updated.servers if s.id == one.id)
    second = next(s for s in updated.servers if s.id == two.id)
    assert (first.host, first.password) == ("a2.net", "PW-A")
    # The untouched record keeps every field, including its credential.
    assert (second.host, second.password, second.connections) == ("b.net", "PW-B", 9)


def test_removing_one_server_preserves_every_survivors_credential():
    one = server(host="a.net", password="PW-A")
    two = server(host="b.net", password="PW-B")
    three = server(host="c.net", password="PW-C")
    updated = remove_server(options(one, two, three), two.id)
    assert [s.id for s in updated.servers] == [one.id, three.id]
    assert [s.password for s in updated.servers] == ["PW-A", "PW-C"]


def test_removing_the_last_server_leaves_an_empty_collection():
    only = server()
    assert remove_server(options(only), only.id).servers == []


def test_mutating_an_unknown_server_is_refused():
    for action in (lambda o: merge_server(o, "nope", {"host": "x"}),
                   lambda o: remove_server(o, "nope")):
        with pytest.raises(ServerMutationError) as caught:
            action(options(server()))
        assert caught.value.reason == SERVER_NOT_FOUND


def test_creating_a_server_appends_it_and_keeps_the_others_intact():
    existing = server(host="a.net", password="PW-A")
    updated, created = create_server(options(existing), {"host": "b.net", "password": "PW-B"})
    assert [s.id for s in updated.servers] == [existing.id, created.id]
    assert updated.servers[0].password == "PW-A"
    assert created.password == "PW-B"


# --- the public projection still hides every credential -------------------

def test_the_public_projection_exposes_ids_but_never_passwords():
    one = server(host="a.net", password="PW-A")
    public = usenet.public_options(options(one).model_dump())
    entry = public["servers"][0]
    assert entry["id"] == one.id
    assert entry["password"] == ""
    assert entry["password_configured"] is True
    import json
    assert "PW-A" not in json.dumps(public)


def test_normalize_settings_never_resurrects_a_cleared_password():
    from integrations.catalog import definitions
    from integrations.configuration import normalize_settings
    from core.config import AppSettings

    stored = server(password="SECRET")
    previous = AppSettings()
    previous.integrations = {"usenet": IntegrationSettings(
        enabled=True, options=options(stored).model_dump())}
    cleared = merge_server(options(stored), stored.id, {"password": ""}, clear_password=True)
    current = AppSettings()
    current.integrations = {"usenet": IntegrationSettings(enabled=True, options=cleared.model_dump())}
    result = normalize_settings(current, definitions, previous=previous)
    assert result.integrations["usenet"].options["servers"][0]["password"] == ""
