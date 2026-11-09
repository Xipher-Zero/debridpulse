"""1.0.14: each human account's own Easy / Advanced presentation.

Narrow proofs, each at its owner (``services.account_appearance``, the
self-scoped ``/api/account/presentation`` and the ``/app.js`` bundle's
first-paint projection), through the real authentication boundary on a
scratch database with synthetic accounts:

* Advanced for every account that never chose; a choice is the account's own,
  durable and independent of every other account's;
* only the one column changes: the theme record is never written by a
  presentation choice, nor the presentation by a theme choice;
* strict validation; every human role and no Integration, no anonymous caller
  and no caller-chosen account;
* the open installation's default Admin owns its presentation (unlike its
  theme), across Server and Standalone and the first password;
* the bundle states the choice before the interface is composed, and a read
  that does not answer in time is stated as unknown, never as a choice;
* a stored value that is no choice reads Advanced and is not rewritten;
  removing an account retires its choice.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import core.config as config
import db.database as database
from auth import accounts
from auth.accounts import DEFAULT_ADMIN_ID
from auth.models import Principal
from auth.sessions import HTTP_SESSION_COOKIE, session_store
from services import account_appearance
from test_account_appearance import NATIVE, live  # noqa: F401  (the shared live installation)

pytestmark = pytest.mark.asyncio

OWN = "/api/account/presentation"
APPEARANCE = "/api/account/appearance"


async def _stored(account_id: str):
    async with database.get_db() as db:
        row = await db.fetchone("SELECT presentation_mode FROM account_appearance WHERE account_id=?",
                                (str(account_id),))
    return None if row is None else row["presentation_mode"]


def _bundle_presentation(script: str) -> dict:
    _authority, _, rest = script.partition("\n")
    head, _, _ = rest.partition("\n")
    assert head.startswith("window.debridPulsePresentation=Object.freeze(") and head.endswith(");"), head[:80]
    return json.loads(head[len("window.debridPulsePresentation=Object.freeze("):-2])


async def test_every_account_is_advanced_until_it_chooses_and_each_choice_is_its_own(live):
    client, as_ = live.client, live.as_
    for name in ("alice", "bob", "reader", "admin"):
        fresh = await client.get(OWN, headers=as_(name))
        assert fresh.status_code == 200 and fresh.json() == {"presentation_mode": "advanced"}
    assert await _stored(live.people["alice"].id) is None          # a read writes nothing

    chosen = await client.put(OWN, headers=as_("alice"), json={"presentation_mode": "easy"})
    assert chosen.status_code == 200 and chosen.json() == {"presentation_mode": "easy"}
    assert (await client.get(OWN, headers=as_("bob"))).json() == {"presentation_mode": "advanced"}
    # Another sign-in of the same account -- another browser -- reads the same choice.
    again = session_store.create(Principal.password_session(live.people["alice"]), lifetime_seconds=3600)[0]
    other = await client.get(OWN, headers={"Cookie": f"{HTTP_SESSION_COOKIE}={again}"})
    assert other.json() == {"presentation_mode": "easy"}
    # Durable: what the record holds, read on a fresh connection.
    assert await account_appearance.load_presentation(live.people["alice"].id) == "easy"
    await client.put(OWN, headers=as_("alice"), json={"presentation_mode": "advanced"})
    assert await _stored(live.people["alice"].id) == "advanced"


async def test_a_presentation_choice_and_a_theme_choice_never_write_each_other(live):
    client, as_ = live.client, live.as_
    alice = live.people["alice"].id
    await client.patch(APPEARANCE, headers=as_("alice"), json={"mode": "light", "light_palette": "nord-light"})
    await client.put(OWN, headers=as_("alice"), json={"presentation_mode": "easy"})
    assert await account_appearance.load(alice) == {"mode": "light", "dark_palette": "debridpulse-dark",
                                                    "light_palette": "nord-light"}
    await client.patch(APPEARANCE, headers=as_("alice"), json={"dark_palette": "dracula"})
    assert await _stored(alice) == "easy"
    # A choice for an account without a record gives it the native theme, which
    # is exactly what it had.
    await client.put(OWN, headers=as_("bob"), json={"presentation_mode": "easy"})
    assert await account_appearance.load(live.people["bob"].id) == NATIVE
    # Concurrent theme and presentation writes of one account both land.
    await asyncio.gather(
        account_appearance.update(alice, account_appearance.AppearanceUpdate(mode="dark")),
        account_appearance.set_presentation(alice, "advanced"),
        account_appearance.update(alice, account_appearance.AppearanceUpdate(light_palette="flexoki-light")))
    assert await account_appearance.load(alice) == {"mode": "dark", "dark_palette": "dracula",
                                                    "light_palette": "flexoki-light"}
    assert await _stored(alice) == "advanced"


@pytest.mark.parametrize("body", [
    {},
    {"presentation_mode": "Easy"},
    {"presentation_mode": "simple"},
    {"presentation_mode": None},
    {"presentation_mode": 1},
    {"presentation_mode": ["easy"]},
    {"presentation_mode": "easy", "account_id": DEFAULT_ADMIN_ID},   # no caller-chosen account, ever
    {"presentation_mode": "easy", "role": "admin"},
    {"presentation_mode": "easy", "mode": "light"},                    # never a theme key
])
async def test_an_unsupported_or_foreign_choice_is_refused_and_changes_nothing(live, body):
    client, as_ = live.client, live.as_
    await client.put(OWN, headers=as_("alice"), json={"presentation_mode": "easy"})
    refused = await client.put(OWN, headers=as_("alice"), json=body)
    assert refused.status_code == 422, refused.text
    assert await _stored(live.people["alice"].id) == "easy"
    assert await _stored(DEFAULT_ADMIN_ID) is None
    assert await account_appearance.load(live.people["alice"].id) == NATIVE


async def test_every_human_role_owns_one_and_no_integration_anonymous_or_crossed_session_does(live):
    client, as_ = live.client, live.as_
    for name in ("admin", "alice", "reader"):
        assert (await client.put(OWN, headers=as_(name), json={"presentation_mode": "easy"})).status_code == 200
    assert (await client.get(OWN, headers=as_("integration"))).status_code == 403
    assert (await client.put(OWN, headers=as_("integration"), json={"presentation_mode": "advanced"})).status_code == 403
    assert (await client.get(OWN)).status_code == 401
    assert (await client.put(OWN, json={"presentation_mode": "advanced"})).status_code == 401
    # Alice's cookie with Bob's CSRF token, or none, never writes her choice.
    assert (await client.put(OWN, headers=as_("alice", csrf_of="bob"),
                             json={"presentation_mode": "advanced"})).status_code == 403
    bare = {key: value for key, value in as_("alice").items() if key != "X-CSRF-Token"}
    assert (await client.put(OWN, headers=bare, json={"presentation_mode": "advanced"})).status_code == 403
    assert await _stored(live.people["alice"].id) == "easy"
    assert await _stored(live.people["bob"].id) is None
    # Nothing about the account but its presentation moved.
    account = await accounts.get_account(live.people["alice"].id)
    assert (account.role, account.active, account.generation) == (
        live.people["alice"].role, True, live.people["alice"].generation)
    assert account_appearance.presentation_owner(Principal.anonymous(), config.get_settings()) is None


async def test_the_open_default_admin_owns_a_durable_choice_across_deployment_types_and_its_first_password(
        live, monkeypatch):
    client, as_ = live.client, live.as_
    monkeypatch.setattr(config, "_settings", config.AppSettings(deployment_type="standalone"))
    assert (await client.get(OWN)).json() == {"presentation_mode": "advanced"}
    assert (await client.put(OWN, json={"presentation_mode": "easy"})).json() == {"presentation_mode": "easy"}
    assert await _stored(DEFAULT_ADMIN_ID) == "easy"
    # Its theme stays the browser's: that admission is unchanged.
    assert (await client.get(APPEARANCE)).status_code == 409
    assert account_appearance.presentation_owner(Principal.standalone(), config.get_settings()) == DEFAULT_ADMIN_ID
    # Another browser of the open installation reads the same choice.
    assert _bundle_presentation((await client.get("/app.js")).text) == {"owned": True, "mode": "easy"}
    # A Server installation while sign-in is off acts as the same default Admin.
    monkeypatch.setattr(config, "_settings", config.AppSettings(deployment_type="server"))
    assert (await client.get(OWN)).json() == {"presentation_mode": "easy"}
    # With a password the same account, signed in, reads and changes it; the
    # open principal no longer owns anything.
    monkeypatch.setattr(config, "_settings", config.AppSettings(deployment_type="standalone",
                                                                auth_password_enabled=True))
    assert (await client.get(OWN, headers=as_("admin"))).json() == {"presentation_mode": "easy"}
    assert account_appearance.presentation_owner(Principal.standalone(), config.get_settings()) is None
    assert (await client.get(OWN)).status_code == 401


async def test_the_bundle_states_the_choice_and_an_unanswered_read_as_unknown(live, monkeypatch):
    client, as_ = live.client, live.as_
    await client.put(OWN, headers=as_("alice"), json={"presentation_mode": "easy"})
    assert _bundle_presentation((await client.get("/app.js", headers=as_("alice"))).text) == {
        "owned": True, "mode": "easy"}
    assert _bundle_presentation((await client.get("/app.js", headers=as_("bob"))).text) == {
        "owned": True, "mode": "advanced"}

    async def stalled(_account_id):
        await asyncio.sleep(5)

    monkeypatch.setattr(account_appearance, "load_presentation", stalled)
    assert _bundle_presentation((await client.get("/app.js", headers=as_("alice"))).text) == {
        "owned": True, "mode": None}


async def test_a_stored_value_that_is_no_choice_reads_advanced_unrewritten_and_removal_retires_it(live):
    client, as_ = live.client, live.as_
    alice = live.people["alice"].id
    await client.put(OWN, headers=as_("alice"), json={"presentation_mode": "easy"})
    async with database.get_db() as db:
        await db.execute("UPDATE account_appearance SET presentation_mode='simple' WHERE account_id=?", (alice,))
        await db.commit()
    assert (await client.get(OWN, headers=as_("alice"))).json() == {"presentation_mode": "advanced"}
    assert await _stored(alice) == "simple"
    await client.put(OWN, headers=as_("bob"), json={"presentation_mode": "easy"})
    await accounts.delete_account(DEFAULT_ADMIN_ID, live.people["bob"].id)
    assert await _stored(live.people["bob"].id) is None
