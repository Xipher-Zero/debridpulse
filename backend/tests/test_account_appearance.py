"""1.0.14: each signed-in human account's own appearance.

Narrow proofs, each at its owner:

* the self-scoped API (``api.account_routes``) through the real boundary: the
  principal's own three choices only, native defaults for a new account,
  independent choices, every human role and no Integration, strict
  validation, CSRF, and no account appearance at all while sign-in is off;
* the first-paint projection (``api.auth_routes.theme_bootstrap_script``):
  ``browser`` exactly when sign-in is off, the signed-in account's own record
  otherwise, and a bounded native fallback that never names the browser;
* lifecycle (``auth.accounts``): Remove and Merge retire a record, disabling
  keeps it, the survivor of a merge keeps its own;
* one catalog: the server accepts exactly the interface's palette IDs.
"""
from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

import core.config as config
import db.database as database
from auth import accounts
from auth.accounts import DEFAULT_ADMIN_ID, DEFAULT_INTEGRATION_ID
from auth.models import Principal
from auth.sessions import HTTP_SESSION_COOKIE, session_store
from services import account_appearance
from account_support import admin_with_password

pytestmark = pytest.mark.asyncio

OWN = "/api/account/appearance"
SCRIPT = "/ui-theme-bootstrap.js"
NATIVE = {"mode": "dark", "dark_palette": "debridpulse-dark", "light_palette": "debridpulse-light"}
BOOTSTRAP = Path(__file__).resolve().parents[2] / "frontend" / "static" / "ui-theme-bootstrap.js"


@pytest_asyncio.fixture
async def live(tmp_path, monkeypatch):
    import main

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "appearance.db")
    await database.init_db()
    settings = {"value": config.AppSettings(auth_password_enabled=True)}
    monkeypatch.setattr(config, "_settings", settings["value"])

    @asynccontextmanager
    async def operation():
        yield

    monkeypatch.setattr(main.app.state, "application", SimpleNamespace(application_operation=operation),
                        raising=False)
    session_store.clear()
    await admin_with_password()
    people = {
        "alice": await accounts.create_local_account("alice", "alice-secret", role="user"),
        "bob": await accounts.create_local_account("bob", "bob-secret", role="user"),
        "reader": await accounts.create_local_account("reader", "reader-secret", role="readonly"),
        "admin": await accounts.get_account(DEFAULT_ADMIN_ID),
    }
    sessions = {name: session_store.create(Principal.password_session(person), lifetime_seconds=3600)[0]
                for name, person in people.items()}
    _record, raw = await accounts.create_token(DEFAULT_INTEGRATION_ID, "probe")
    transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        def as_(name, *, csrf_of=None):
            if name == "integration":
                return {"Authorization": f"Bearer {raw}"}
            token = sessions[name]
            return {"Cookie": f"{HTTP_SESSION_COOKIE}={token}",
                    "X-CSRF-Token": session_store.csrf_token(sessions[csrf_of or name])}
        yield SimpleNamespace(client=client, as_=as_, people=people)
    session_store.clear()


def _projection(script: str) -> dict:
    head, _, rest = script.partition("\n")
    match = re.fullmatch(r"window\.debridPulseAppearance=Object\.freeze\((.*)\);", head)
    assert match, head
    assert rest == BOOTSTRAP.read_text(encoding="utf-8")         # the owner itself, unchanged
    projection = json.loads(match.group(1))
    # Every page receives the one catalog, whichever authority holds its choices.
    assert projection.pop("catalog") == account_appearance.catalog()
    return projection


# ── the self-scoped API ─────────────────────────────────────────────────────

async def test_a_new_account_has_the_native_appearance_and_each_choice_is_its_own(live):
    client, as_ = live.client, live.as_
    fresh = await client.get(OWN, headers=as_("alice"))
    assert fresh.status_code == 200
    assert fresh.json() == {"account_id": live.people["alice"].id, **NATIVE}

    # Each choice changes alone; the other two keep what is stored.
    assert (await client.patch(OWN, headers=as_("alice"), json={"dark_palette": "dracula"})).json() == {
        "account_id": live.people["alice"].id, "mode": "dark", "dark_palette": "dracula",
        "light_palette": "debridpulse-light"}
    assert (await client.patch(OWN, headers=as_("alice"), json={"mode": "light"})).json()["dark_palette"] == "dracula"
    saved = await client.patch(OWN, headers=as_("alice"), json={"light_palette": "gruvbox-light"})
    assert saved.json() == {"account_id": live.people["alice"].id, "mode": "light", "dark_palette": "dracula",
                            "light_palette": "gruvbox-light"}

    # Another account sharing nothing: Bob's record is his own.
    assert (await client.get(OWN, headers=as_("bob"))).json() == {"account_id": live.people["bob"].id, **NATIVE}
    await client.patch(OWN, headers=as_("bob"), json={"mode": "light", "light_palette": "nord-light"})
    assert (await account_appearance.load(live.people["alice"].id))["light_palette"] == "gruvbox-light"
    assert (await account_appearance.load(live.people["bob"].id))["light_palette"] == "nord-light"


async def test_every_human_role_may_choose_and_no_integration_may(live):
    client, as_ = live.client, live.as_
    for name in ("admin", "reader"):
        changed = await client.patch(OWN, headers=as_(name), json={"mode": "light"})
        assert changed.status_code == 200, (name, changed.text)
        assert (await client.get(OWN, headers=as_(name))).json()["mode"] == "light"
    assert (await client.get(OWN, headers=as_("integration"))).status_code == 403
    assert (await client.patch(OWN, headers=as_("integration"), json={"mode": "light"})).status_code == 403
    assert (await client.get(SCRIPT, headers=as_("integration"))).status_code == 403
    assert (await client.get(OWN)).status_code == 401
    assert (await client.patch(OWN, json={"mode": "light"})).status_code == 401


@pytest.mark.parametrize("body", [
    {},
    {"account_id": DEFAULT_ADMIN_ID, "mode": "light"},     # no caller-chosen account, ever
    {"mode": "sepia"},
    {"mode": "Light"},
    {"dark_palette": "nord-light"},                         # a light palette is no dark choice
    {"light_palette": "dracula"},
    {"light_palette": "solarized"},
    {"mode": None},
    {"mode": 1},
    {"theme": "light"},
])
async def test_an_unsupported_or_foreign_change_is_refused_and_changes_nothing(live, body):
    client, as_ = live.client, live.as_
    await client.patch(OWN, headers=as_("alice"), json={"light_palette": "catppuccin-latte"})
    refused = await client.patch(OWN, headers=as_("alice"), json=body)
    assert refused.status_code == 422, refused.text
    assert await account_appearance.load(live.people["alice"].id) == {**NATIVE, "light_palette": "catppuccin-latte"}
    assert await account_appearance.load(DEFAULT_ADMIN_ID) == NATIVE


async def test_a_malformed_body_and_a_change_without_its_own_session_token_are_refused(live):
    client, as_ = live.client, live.as_
    malformed = await client.patch(OWN, headers={**as_("alice"), "Content-Type": "application/json"},
                                   content=b"{mode: light")
    assert malformed.status_code == 422
    # A change carried by Alice's cookie with Bob's (or no) CSRF token -- a
    # page composed for another session -- never reaches her record.
    crossed = await client.patch(OWN, headers=as_("alice", csrf_of="bob"), json={"mode": "light"})
    assert crossed.status_code == 403
    bare = {key: value for key, value in as_("alice").items() if key != "X-CSRF-Token"}
    assert (await client.patch(OWN, headers=bare, json={"mode": "light"})).status_code == 403
    assert await account_appearance.load(live.people["alice"].id) == NATIVE
    assert await account_appearance.load(live.people["bob"].id) == NATIVE


async def test_while_sign_in_is_off_there_is_no_account_appearance(live, monkeypatch):
    monkeypatch.setattr(config, "_settings", config.AppSettings())
    client = live.client
    assert (await client.get(OWN)).status_code == 409
    assert (await client.patch(OWN, json={"mode": "light"})).status_code == 409
    assert await account_appearance.load(DEFAULT_ADMIN_ID) == NATIVE
    database_rows = await _rows()
    assert database_rows == []
    script = await client.get(SCRIPT)
    assert script.status_code == 200 and script.headers["cache-control"] == "no-store"
    assert _projection(script.text) == {"authority": "browser"}


async def _rows():
    async with database.get_db() as db:
        return await db.fetchall("SELECT account_id FROM account_appearance")


# ── the first-paint projection ──────────────────────────────────────────────

async def test_the_first_paint_script_carries_only_the_signed_in_accounts_own_record(live):
    client, as_ = live.client, live.as_
    await client.patch(OWN, headers=as_("alice"), json={"mode": "light", "light_palette": "flexoki-light"})
    alice = await client.get(SCRIPT, headers=as_("alice"))
    assert alice.status_code == 200 and alice.headers["cache-control"] == "no-store"
    assert alice.headers["content-type"].startswith("application/javascript")
    assert _projection(alice.text) == {
        "authority": "account", "account_id": live.people["alice"].id,
        "preferences": {"mode": "light", "dark_palette": "debridpulse-dark", "light_palette": "flexoki-light"}}
    assert _projection((await client.get(SCRIPT, headers=as_("bob"))).text) == {
        "authority": "account", "account_id": live.people["bob"].id, "preferences": NATIVE}
    # Signed out under sign-in: no script, no appearance, no browser authority.
    assert (await client.get(SCRIPT)).status_code in {401, 403}


@pytest.mark.parametrize("failure", ["error", "slow"])
async def test_an_unreadable_record_paints_the_native_appearance_and_never_the_browser(live, monkeypatch, failure):
    from api import auth_routes

    async def unreadable(_account_id):
        if failure == "slow":
            await asyncio.sleep(30)
        raise RuntimeError("database is locked")

    monkeypatch.setattr(auth_routes, "_APPEARANCE_READ_SECONDS", 0.05)
    monkeypatch.setattr(account_appearance, "load", unreadable)
    started = asyncio.get_running_loop().time()
    script = await live.client.get(SCRIPT, headers=live.as_("alice"))
    assert asyncio.get_running_loop().time() - started < 5
    assert script.status_code == 200
    assert _projection(script.text) == {"authority": "account", "account_id": live.people["alice"].id,
                                        "preferences": None}


# ── lifecycle ───────────────────────────────────────────────────────────────

async def test_remove_and_merge_retire_a_record_and_disabling_keeps_it(live):
    alice, reader = (live.people[name].id for name in ("alice", "reader"))
    # A duplicate of Alice signed in through an identity provider (merge folds
    # only accounts without a second local credential).
    bob = (await accounts.resolve_oidc("https://id.example", "bob-subject", {"name": "Bob"},
                                       default_role="user")).id
    for account_id, palette in ((alice, "dracula"), (bob, "everforest-dark"), (reader, "kanagawa-wave")):
        await account_appearance.update(account_id, account_appearance.AppearanceUpdate(dark_palette=palette))

    await accounts.set_enabled(DEFAULT_ADMIN_ID, reader, False)
    assert (await account_appearance.load(reader))["dark_palette"] == "kanagawa-wave"
    await accounts.set_enabled(DEFAULT_ADMIN_ID, reader, True)

    await accounts.merge_accounts(DEFAULT_ADMIN_ID, [bob], alice)
    assert (await account_appearance.load(alice))["dark_palette"] == "dracula"      # the survivor's own
    await accounts.delete_account(DEFAULT_ADMIN_ID, reader)
    assert sorted(row["account_id"] for row in await _rows()) == [alice]


# ── one catalog ─────────────────────────────────────────────────────────────

# The locked 1.0.14 catalog: ten palettes per mode, the native one first.
LOCKED = {
    "dark": [("debridpulse-dark", "DebridPulse (Default)"), ("catppuccin-mocha", "Catppuccin Mocha"),
             ("dracula", "Dracula"), ("gruvbox-dark", "Gruvbox Dark"), ("everforest-dark", "Everforest Dark"),
             ("rose-pine", "Rosé Pine"), ("kanagawa-wave", "Kanagawa Wave"), ("ayu-mirage", "Ayu Mirage"),
             ("synthwave-84", "SynthWave '84"), ("oxocarbon-dark", "Oxocarbon Dark")],
    "light": [("debridpulse-light", "DebridPulse (Default)"), ("catppuccin-latte", "Catppuccin Latte"),
              ("nord-light", "Nord Light"), ("gruvbox-light", "Gruvbox Light"), ("rose-pine-dawn", "Rosé Pine Dawn"),
              ("everforest-light", "Everforest Light"), ("ayu-light", "Ayu Light"), ("flexoki-light", "Flexoki Light"),
              ("oxocarbon-light", "Oxocarbon Light"), ("bluloco-light", "Bluloco Light")],
}
RETIRED = {"dark_palette": ("nord-dark", "solarized-dark", "tokyo-night"),
           "light_palette": ("solarized-light", "tokyo-night-day")}
PALETTES_CSS = BOOTSTRAP.with_name("ui-theme-palettes.css")


async def test_the_catalog_is_the_locked_ten_per_mode_with_one_authority():
    assert {mode: list(entries) for mode, entries in account_appearance.CATALOG.items()} == LOCKED
    # The interface holds no catalog of its own: only the two native IDs (the
    # absence of a palette) are named in its theme owner.
    source = BOOTSTRAP.read_text(encoding="utf-8")
    alternatives = {palette for mode in LOCKED for palette, _label in LOCKED[mode][1:]}
    retired = {palette for ids in RETIRED.values() for palette in ids}
    assert not [palette for palette in alternatives | retired if f"'{palette}'" in source]
    # A palette block exists for exactly each alternative the catalog lists.
    declared = set(re.findall(r'^body\[data-dp-palette="([a-z0-9-]+)"\]', PALETTES_CSS.read_text(encoding="utf-8"), re.M))
    assert declared == alternatives


@pytest.mark.parametrize("field,palette", [(field, palette) for field, ids in RETIRED.items() for palette in ids])
async def test_a_retired_palette_is_refused_as_a_new_choice(live, field, palette):
    client, as_ = live.client, live.as_
    await client.patch(OWN, headers=as_("alice"), json={"dark_palette": "kanagawa-wave", "light_palette": "flexoki-light"})
    refused = await client.patch(OWN, headers=as_("alice"), json={field: palette})
    assert refused.status_code == 422, refused.text
    assert await account_appearance.load(live.people["alice"].id) == {
        "mode": "dark", "dark_palette": "kanagawa-wave", "light_palette": "flexoki-light"}


async def _stored(account_id):
    async with database.get_db() as db:
        return await db.fetchone(
            "SELECT mode, dark_palette, light_palette, updated_at FROM account_appearance WHERE account_id=?",
            (account_id,))


@pytest.mark.parametrize("stored,effective", [
    # A retired Dark palette beside a current Light one, and the reverse.
    ({"mode": "light", "dark_palette": "tokyo-night", "light_palette": "flexoki-light"},
     {"mode": "light", "dark_palette": "debridpulse-dark", "light_palette": "flexoki-light"}),
    ({"mode": "dark", "dark_palette": "rose-pine", "light_palette": "tokyo-night-day"},
     {"mode": "dark", "dark_palette": "rose-pine", "light_palette": "debridpulse-light"}),
    ({"mode": "dark", "dark_palette": "solarized-dark", "light_palette": "solarized-light"},
     {"mode": "dark", "dark_palette": "debridpulse-dark", "light_palette": "debridpulse-light"}),
])
async def test_a_stored_retired_palette_reads_as_its_modes_native_one_and_is_never_rewritten(live, stored, effective):
    """A record saved before the catalog changed: each retired choice is its
    own mode's native palette wherever it is read -- the store, the API, the
    first-paint projection -- the other mode's choice is kept, and no read
    writes anything back."""
    client, as_ = live.client, live.as_
    alice = live.people["alice"].id
    async with database.get_db() as db:
        await db.execute("INSERT INTO account_appearance(account_id,mode,dark_palette,light_palette,updated_at) "
                         "VALUES(?,?,?,?,?)", (alice, stored["mode"], stored["dark_palette"], stored["light_palette"],
                                               "2026-10-25T00:00:00Z"))
        await db.commit()
    before = await _stored(alice)

    assert await account_appearance.load(alice) == effective
    assert (await client.get(OWN, headers=as_("alice"))).json() == {"account_id": alice, **effective}
    assert _projection((await client.get(SCRIPT, headers=as_("alice"))).text)["preferences"] == effective
    assert await _stored(alice) == before                       # nothing written back by any read

    # Only an explicit choice writes: it saves the named choice with the
    # record as the account sees it (a retired choice there is its native one).
    saved = (await client.patch(OWN, headers=as_("alice"), json={"mode": "light"})).json()
    assert saved == {"account_id": alice, **effective, "mode": "light"}
    row = await _stored(alice)
    assert (row["mode"], row["dark_palette"], row["light_palette"]) == (
        "light", effective["dark_palette"], effective["light_palette"])
