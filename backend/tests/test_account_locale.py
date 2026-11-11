"""1.0.14: each human account's own interface language (WS2).

Narrow proofs at the owners (``services.account_appearance``,
``/api/account/locale``, the first-paint projection on
``/ui-theme-bootstrap.js``, ``/i18n/<tag>.js`` and ``core.localization``),
through the real authentication boundary on a scratch database with
synthetic accounts:

* English for every account that never chose; a choice is the account's own,
  durable, independent of every other account's, and changes nothing else;
* only admitted tags: English and a target the manifest marks supported
  that has its catalog -- never a planned one; pseudo locales only where test
  locales are enabled; a stored tag that stops being admitted reads as
  English, unrewritten; every human role and no Integration, anonymous or
  caller-named account. The supported targets here are a synthetic catalog
  directory (``STATIC_DIR``, the ``supported`` fixture): test doubles, not
  translations; the repository marks no target supported;
* the open installation's default Admin owns its language (as it owns its
  presentation), across deployment types;
* the first-paint script states the language, with a bounded read and an
  unknown state rather than a guess; the catalog asset is versioned;
* an error the interface shows carries a stable code beside its unchanged
  English detail.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil

import pytest

import core.config as config
import db.database as database
from auth.accounts import DEFAULT_ADMIN_ID
from auth.models import Principal
from auth.sessions import HTTP_SESSION_COOKIE, session_store
from core import localization
from services import account_appearance
from test_account_appearance import NATIVE, live  # noqa: F401  (the shared live installation)

pytestmark = pytest.mark.asyncio

OWN = "/api/account/locale"
SCRIPT = "/ui-theme-bootstrap.js"


async def _stored(account_id: str):
    async with database.get_db() as db:
        row = await db.fetchone("SELECT locale, presentation_mode FROM account_appearance WHERE account_id=?",
                                (str(account_id),))
    return None if row is None else (row["locale"], row["presentation_mode"])


SUPPORTED = ("de", "ar")       # synthetic: supported only inside the ``supported`` fixture


@pytest.fixture
def supported(tmp_path, monkeypatch):
    """A synthetic catalog directory whose manifest marks ``de`` and ``ar``
    supported, each with a one-key catalog made from the current English
    (a test double), served through ``STATIC_DIR``."""
    source = localization.catalog_dir()
    i18n = tmp_path / "static" / "i18n"
    i18n.mkdir(parents=True)
    shutil.copy(source / "en.json", i18n / "en.json")
    english = json.loads((i18n / "en.json").read_text(encoding="utf-8"))["messages"]
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    for tag in SUPPORTED:
        manifest["locales"][tag]["status"] = "supported"
        key = "shell.nav.dashboard"
        (i18n / f"{tag}.json").write_text(json.dumps({
            "schema": "debridpulse-catalog/1", "locale": tag, "messages": {key: f"[{tag}-test] Dashboard"},
            "sources": {key: localization.source_hash(english[key])}}), encoding="utf-8")
    (i18n / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("STATIC_DIR", str(tmp_path / "static"))
    return i18n


async def _store_raw(account_id: str, tag: str):
    """A stored value written past the admission rule (an older build's)."""
    async with database.get_db() as db:
        await db.execute("INSERT INTO account_appearance(account_id,mode,dark_palette,light_palette,locale,updated_at) "
                         "VALUES(?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET locale=excluded.locale",
                         (str(account_id), "dark", "debridpulse-dark", "debridpulse-light", tag, "2026-11-09T00:00:00Z"))
        await db.commit()


def _projection(script: str) -> dict:
    line = next(line for line in script.splitlines() if line.startswith("window.debridPulseLocale="))
    return json.loads(line[len("window.debridPulseLocale=Object.freeze("):-2])


async def test_every_account_is_english_until_it_chooses_and_each_choice_is_its_own(live, supported):
    client, as_ = live.client, live.as_
    for name in ("alice", "bob", "reader", "admin"):
        fresh = await client.get(OWN, headers=as_(name))
        assert fresh.status_code == 200 and fresh.json() == {"locale": "en"}
    assert await _stored(live.people["alice"].id) is None          # a read writes nothing

    chosen = await client.put(OWN, headers=as_("alice"), json={"locale": "de"})
    assert chosen.status_code == 200 and chosen.json() == {"locale": "de"}
    assert (await client.get(OWN, headers=as_("bob"))).json() == {"locale": "en"}
    # Another sign-in of the same account -- another browser or device -- reads it.
    again = session_store.create(Principal.password_session(live.people["alice"]), lifetime_seconds=3600)[0]
    assert (await client.get(OWN, headers={"Cookie": f"{HTTP_SESSION_COOKIE}={again}"})).json() == {"locale": "de"}
    assert await account_appearance.load_locale(live.people["alice"].id) == "de"
    for name, tag in (("reader", "de"), ("admin", "ar")):
        assert (await client.put(OWN, headers=as_(name), json={"locale": tag})).json() == {"locale": tag}
    assert (await client.get(OWN, headers=as_("alice"))).json() == {"locale": "de"}


async def test_a_language_choice_changes_the_language_only(live, supported):
    client, as_ = live.client, live.as_
    alice = live.people["alice"].id
    await client.patch("/api/account/appearance", headers=as_("alice"), json={"mode": "light", "light_palette": "nord-light"})
    await client.put("/api/account/presentation", headers=as_("alice"), json={"presentation_mode": "easy"})
    account_before = await localization_account(alice)
    assert (await client.put(OWN, headers=as_("alice"), json={"locale": "de"})).status_code == 200
    assert await _stored(alice) == ("de", "easy")
    assert await account_appearance.load(alice) == {"mode": "light", "dark_palette": "debridpulse-dark",
                                                    "light_palette": "nord-light"}
    assert await localization_account(alice) == account_before
    # A theme or presentation write never touches the language.
    await client.patch("/api/account/appearance", headers=as_("alice"), json={"mode": "dark"})
    await client.put("/api/account/presentation", headers=as_("alice"), json={"presentation_mode": "advanced"})
    assert await _stored(alice) == ("de", "advanced")
    # Concurrent writes of one account converge, each on its own column.
    await asyncio.gather(account_appearance.set_locale(alice, "ar"), account_appearance.set_presentation(alice, "easy"),
                         account_appearance.update(alice, account_appearance.AppearanceUpdate(light_palette="flexoki-light")))
    assert await _stored(alice) == ("ar", "easy")
    assert (await account_appearance.load(alice))["light_palette"] == "flexoki-light"
    # A bob with no record gets the native theme with his language: what he had.
    await client.put(OWN, headers=as_("bob"), json={"locale": "de"})
    assert await account_appearance.load(live.people["bob"].id) == NATIVE


async def localization_account(account_id):
    from auth import accounts

    account = await accounts.get_account(account_id)
    return (account.role, account.active, account.generation, account.display_name, account.email)


@pytest.mark.parametrize("body", [
    {}, {"locale": "xx"}, {"locale": "EN"}, {"locale": "en_US"}, {"locale": "pt"}, {"locale": "en-XA"}, {"locale": "ar-XB"},
    {"locale": "fr"}, {"locale": "ja"}, {"locale": "DE"},
    {"locale": None}, {"locale": 1}, {"locale": ["de"]}, {"locale": "de", "account_id": DEFAULT_ADMIN_ID},
    {"locale": "de", "presentation_mode": "easy"},
])
async def test_an_unknown_or_foreign_choice_is_refused_and_changes_nothing(live, supported, body):
    client, as_ = live.client, live.as_
    assert (await client.put(OWN, headers=as_("alice"), json={"locale": "ar"})).status_code == 200
    refused = await client.put(OWN, headers=as_("alice"), json=body)
    assert refused.status_code == 422, refused.text
    assert await _stored(live.people["alice"].id) == ("ar", None)
    assert await _stored(DEFAULT_ADMIN_ID) is None


async def test_production_admits_english_and_supported_targets_only_never_a_planned_one(live, tmp_path, monkeypatch):
    client, as_ = live.client, live.as_
    # The repository as shipped: every target is planned, so English only.
    assert localization.locale_tags() == ("en",)
    for tag in ("de", "ar", "ja", "es", "pt-BR", "zh-Hans"):
        refused = await client.put(OWN, headers=as_("alice"), json={"locale": tag})
        assert refused.status_code == 422, (tag, refused.text)
    assert await _stored(live.people["alice"].id) is None
    assert (await client.put(OWN, headers=as_("alice"), json={"locale": "en"})).json() == {"locale": "en"}
    # Test locales admit the pseudo locales and nothing else.
    monkeypatch.setenv(localization.TEST_LOCALES_ENV, "1")
    assert localization.locale_tags() == ("en", "en-XA", "ar-XB")
    assert (await client.put(OWN, headers=as_("alice"), json={"locale": "de"})).status_code == 422
    monkeypatch.delenv(localization.TEST_LOCALES_ENV)
    # Supported is the manifest's status AND a readable catalog: without the
    # catalog, or with an unreadable one, the target is not admitted.
    i18n = tmp_path / "static" / "i18n"
    i18n.mkdir(parents=True)
    shutil.copy(localization.catalog_dir() / "en.json", i18n / "en.json")
    manifest = json.loads((localization.catalog_dir() / "manifest.json").read_text(encoding="utf-8"))
    manifest["locales"]["de"]["status"] = "supported"
    (i18n / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("STATIC_DIR", str(tmp_path / "static"))
    assert localization.locale_tags() == ("en",)
    (i18n / "de.json").write_text("{not json", encoding="utf-8")
    assert localization.locale_tags() == ("en",)
    assert (await client.put(OWN, headers=as_("alice"), json={"locale": "de"})).status_code == 422
    (i18n / "de.json").write_text(json.dumps({"locale": "de", "messages": {}, "sources": {}}), encoding="utf-8")
    assert localization.locale_tags() == ("en", "de")
    assert (await client.put(OWN, headers=as_("alice"), json={"locale": "de"})).json() == {"locale": "de"}


async def test_a_stored_tag_that_is_not_admitted_reads_as_english_and_stays_stored(live, supported, monkeypatch):
    client, as_ = live.client, live.as_
    alice, bob = live.people["alice"].id, live.people["bob"].id
    assert (await client.put(OWN, headers=as_("alice"), json={"locale": "de"})).json() == {"locale": "de"}
    await _store_raw(bob, "ja")                     # planned: an older build's value
    assert (await client.get(OWN, headers=as_("bob"))).json() == {"locale": "en"}
    assert _projection((await client.get(SCRIPT, headers=as_("bob"))).text)["locale"] == "en"
    # The installation stops supporting de: alice reads English, her value kept.
    monkeypatch.delenv("STATIC_DIR")
    assert (await client.get(OWN, headers=as_("alice"))).json() == {"locale": "en"}
    assert _projection((await client.get(SCRIPT, headers=as_("alice"))).text) == {
        "owned": True, "locale": "en", "dir": "ltr", "status": "canonical", "catalog": None}
    assert await _stored(alice) == ("de", None) and await _stored(bob) == ("ja", None)


async def test_pseudo_locales_exist_only_where_test_locales_are_enabled(live, monkeypatch):
    client, as_ = live.client, live.as_
    assert "en-XA" not in localization.locale_tags()
    monkeypatch.setenv(localization.TEST_LOCALES_ENV, "1")
    assert {"en-XA", "ar-XB"} <= set(localization.locale_tags())
    assert (await client.put(OWN, headers=as_("alice"), json={"locale": "en-XA"})).json() == {"locale": "en-XA"}
    monkeypatch.delenv(localization.TEST_LOCALES_ENV)
    # A stored tag the interface no longer knows reads as English, unrewritten.
    assert (await client.get(OWN, headers=as_("alice"))).json() == {"locale": "en"}
    assert await _stored(live.people["alice"].id) == ("en-XA", None)


async def test_every_human_role_owns_one_and_no_integration_anonymous_or_crossed_session_does(live, supported):
    client, as_ = live.client, live.as_
    for name in ("admin", "alice", "reader"):
        assert (await client.put(OWN, headers=as_(name), json={"locale": "de"})).status_code == 200
    assert (await client.get(OWN, headers=as_("integration"))).status_code == 403
    assert (await client.put(OWN, headers=as_("integration"), json={"locale": "de"})).status_code == 403
    assert (await client.get(OWN)).status_code == 401
    assert (await client.put(OWN, json={"locale": "de"})).status_code == 401
    assert (await client.put(OWN, headers=as_("alice", csrf_of="bob"), json={"locale": "ar"})).status_code == 403
    assert await _stored(live.people["alice"].id) == ("de", None)
    assert await _stored(live.people["bob"].id) is None


async def test_the_open_default_admin_owns_its_language_across_deployment_types(live, supported, monkeypatch):
    client = live.client
    monkeypatch.setattr(config, "_settings", config.AppSettings(deployment_type="standalone"))
    assert (await client.get(OWN)).json() == {"locale": "en"}
    assert (await client.put(OWN, json={"locale": "de"})).json() == {"locale": "de"}
    assert await _stored(DEFAULT_ADMIN_ID) == ("de", None)
    # Another browser of the open installation reads it at first paint.
    assert _projection((await client.get(SCRIPT)).text)["locale"] == "de"
    monkeypatch.setattr(config, "_settings", config.AppSettings(deployment_type="server"))
    assert (await client.get(OWN)).json() == {"locale": "de"}
    # With a password on, the open principal owns nothing; the account, signed in, does.
    monkeypatch.setattr(config, "_settings", config.AppSettings(deployment_type="standalone", auth_password_enabled=True))
    assert (await client.get(OWN)).status_code == 401
    assert (await client.get(OWN, headers=live.as_("admin"))).json() == {"locale": "de"}


async def test_the_first_paint_script_states_the_language_and_an_unanswered_read_as_unknown(live, supported, monkeypatch):
    client, as_ = live.client, live.as_
    english = _projection((await client.get(SCRIPT, headers=as_("bob"))).text)
    assert english == {"owned": True, "locale": "en", "dir": "ltr", "status": "canonical", "catalog": None}
    await client.put(OWN, headers=as_("alice"), json={"locale": "ar"})
    arabic = _projection((await client.get(SCRIPT, headers=as_("alice"))).text)
    # A supported target: stated as chosen, with its versioned catalog to load.
    assert arabic == {"owned": True, "locale": "ar", "dir": "rtl", "status": "supported",
                      "catalog": f"/i18n/ar.js?v={localization.catalog_version('ar')}"}
    monkeypatch.setenv(localization.TEST_LOCALES_ENV, "1")
    await client.put(OWN, headers=as_("bob"), json={"locale": "ar-XB"})
    assert _projection((await client.get(SCRIPT, headers=as_("bob"))).text)["dir"] == "rtl"

    async def stalled(_account_id):
        await asyncio.sleep(5)

    monkeypatch.setattr(account_appearance, "load_locale", stalled)
    assert _projection((await client.get(SCRIPT, headers=as_("alice"))).text) == {
        "owned": True, "locale": None, "dir": "ltr", "status": "canonical", "catalog": None}


async def test_the_catalog_asset_is_the_catalog_versioned_by_its_content(live):
    client, as_ = live.client, live.as_
    served = await client.get("/i18n/en.js", headers=as_("reader"))
    assert served.status_code == 200 and served.headers["content-type"].startswith("application/javascript")
    tag, messages = re.fullmatch(r'window\.DPI18nCatalog\("([\w-]+)",(\{.*\})\);', served.text, re.S).groups()
    assert tag == "en" and json.loads(messages) == localization.catalog("en")
    etag = served.headers["etag"]
    assert etag == f'"{localization.catalog_version("en")}"'
    assert (await client.get("/i18n/en.js", headers={**as_("reader"), "If-None-Match": etag})).status_code == 304
    assert (await client.get("/i18n/zh-Hans.js", headers=as_("reader"))).status_code == 404     # planned: no catalog yet
    # A planned target with a catalog (WS5-A1 German) is a fetchable development asset: it is
    # delivered, but never admitted as an account's language (test_production_admits_english_and_supported_targets_only_never_a_planned_one).
    german = await client.get("/i18n/de.js", headers=as_("reader"))
    assert german.status_code == 200 and german.text.startswith('window.DPI18nCatalog("de",')
    assert (await client.get("/i18n/xx.js", headers=as_("reader"))).status_code == 404
    assert (await client.get("/i18n/en.js")).status_code == 401


async def test_an_error_the_interface_shows_keeps_its_english_detail_and_adds_its_code(live):
    client, as_ = live.client, live.as_
    refused = await client.put("/api/account/display-name", headers=as_("alice"), json={"display_name": " "})
    assert refused.status_code == 409
    assert refused.json() == {"detail": "A display name is required", "code": "account.display_name_required", "params": {}}
    wrong = await client.put("/api/account/password", headers=as_("alice"),
                             json={"current_password": "not-alice", "new_password": "a-new-password-1"})
    assert wrong.status_code == 409 and wrong.json()["code"] == "account.current_password_incorrect"
    assert wrong.json()["detail"] == "The current password is not correct"
    short = await client.put("/api/account/password", headers=as_("alice"),
                             json={"current_password": "alice-secret", "new_password": "x"})
    assert short.json()["code"] == "account.password_too_short" and short.json()["params"]["minimum"] >= 8
    invalid = await client.put("/api/account/email", headers=as_("alice"), json={"email": "nope", "expected": ""})
    assert invalid.status_code == 422 and invalid.json() == {"detail": "Enter a valid email address",
                                                             "code": "account.email_invalid", "params": {}}
    # Every code it can carry is a message in the English catalog.
    for code in ("account.display_name_required", "account.current_password_incorrect", "account.password_too_short",
                 "account.email_invalid"):
        assert f"error.{code}" in localization.catalog("en")


def _served(response) -> tuple[str, dict, dict]:
    tag, messages, info = re.fullmatch(r'window\.DPI18nCatalog\("([\w-]+)",(\{.*?\})(?:,(\{.*\}))?\);',
                                       response.text, re.S).groups()
    return tag, json.loads(messages), json.loads(info) if info else {}


async def test_a_translation_made_from_an_older_english_text_is_withheld_alone_and_its_asset_versioned_anew(live, supported):
    client, as_ = live.client, live.as_
    english_path = supported / "en.json"
    source = json.loads(english_path.read_text(encoding="utf-8"))
    english = source["messages"]
    current = {"shell.nav.dashboard": "[de-test] Dashboard", "shell.nav.settings": "[de-test] Settings"}
    (supported / "de.json").write_text(json.dumps({
        "locale": "de", "messages": current,
        "sources": {key: localization.source_hash(english[key]) for key in current}}), encoding="utf-8")
    target_bytes = (supported / "de.json").read_bytes()

    # 1. Every recorded source matches the English: every translation delivered.
    first = await client.get("/i18n/de.js", headers=as_("reader"))
    assert first.status_code == 200 and _served(first) == ("de", current, {})
    etag = first.headers["etag"]
    assert etag == f'"{localization.catalog_version("de")}"'
    assert (await client.get("/i18n/de.js", headers={**as_("reader"), "If-None-Match": etag})).status_code == 304

    # 2. English changes for one key; the target file does not change.
    english["shell.nav.settings"] = "Settings and preferences"
    english_path.write_text(json.dumps(source), encoding="utf-8")
    assert (supported / "de.json").read_bytes() == target_bytes
    second = await client.get("/i18n/de.js", headers={**as_("reader"), "If-None-Match": etag})
    # 5. The old version is not revalidated: a new body under a new ETag and URL.
    assert second.status_code == 200 and second.headers["etag"] != etag
    # 3. Only that key is withheld, and named stale; the other stays current.
    assert _served(second) == ("de", {"shell.nav.dashboard": "[de-test] Dashboard"}, {"stale": ["shell.nav.settings"]})
    assert localization.delivered("de") == {"messages": {"shell.nav.dashboard": "[de-test] Dashboard"},
                                            "stale": ["shell.nav.settings"]}
    await client.put(OWN, headers=as_("alice"), json={"locale": "de"})
    stated = _projection((await client.get(SCRIPT, headers=as_("alice"))).text)
    assert stated["catalog"] == f"/i18n/de.js?v={second.headers['etag'].strip(chr(34))}"
    # A server-rendered message shows the current English for the stale key.
    assert localization.message("shell.nav.settings", "de") == "Settings and preferences"
    assert localization.message("shell.nav.dashboard", "de") == "[de-test] Dashboard"

    # A malformed target catalog is not delivered: the page stays English.
    (supported / "de.json").write_text('{"locale": "de", "messages": [', encoding="utf-8")
    assert (await client.get("/i18n/de.js", headers=as_("reader"))).status_code == 404
    assert localization.delivered("de") is None and localization.catalog("de") == {}
    assert (await client.get(OWN, headers=as_("alice"))).json() == {"locale": "en"}
    assert _projection((await client.get(SCRIPT, headers=as_("alice"))).text)["catalog"] is None
