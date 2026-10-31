"""1.0.14: the sign-in page's appearance is the installation's.

Narrow proofs, each at its owner:

* personal appearance (``services.account_appearance.update``), through the
  real API and sessions: a mode or palette choice by any account -- an Admin's
  included -- is that account's alone and never changes the login appearance;
* publication (``account_appearance.publish``, ``PUT /api/login-appearance``):
  the one writer of the shared row. An active Admin now (read in the
  publishing transaction, whatever the session says) publishes its own saved
  active pair, and only while that is the pair its interface sends; every
  other caller, a stale pair, a missing CSRF token and a failure inside the
  transaction leave the row as it was; two Admins publishing at once
  serialize, and the last to commit wins with its whole pair;
* the row's lifetime: it names no account, survives the publishing Admin's
  demotion, disabling, removal and merge, and stays one row however often it
  is republished; the wipe keeps it;
* the anonymous read (``api.auth_routes``): every server-rendered sign-in
  page carries exactly the validated mode and palette at first paint, reads
  the one row and no account table, falls back to DebridPulse Dark, and keeps
  its CSP, hash-pinned shim and ``no-store``;
* one palette catalog and one logo source: every catalog palette resolves
  through the application's own palette stylesheet; the inline marks are
  ``logo.svg``; the waves are one field and the version follows the card.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import sqlite3
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
from services import account_appearance, db_maintenance
from account_support import admin_with_password

pytestmark = pytest.mark.asyncio

OWN = "/api/account/appearance"
PUBLISH = "/api/login-appearance"
STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"
NATIVE_DARK = {"mode": "dark", "palette": "debridpulse-dark"}
_SINGLE_READ = "SELECT mode, palette FROM login_appearance WHERE id=1"


@pytest_asyncio.fixture
async def live(tmp_path, monkeypatch):
    import main

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "login-appearance.db")
    await database.init_db()
    monkeypatch.setattr(config, "_settings", config.AppSettings(auth_password_enabled=True))

    @asynccontextmanager
    async def operation():
        yield

    monkeypatch.setattr(main.app.state, "application", SimpleNamespace(application_operation=operation),
                        raising=False)
    from api import auth_routes

    # These proofs render the sign-in page many times from one peer.
    monkeypatch.setattr(auth_routes.login_challenge_rate_limiter, "allow", lambda _peer: True)
    session_store.clear()
    await admin_with_password()
    people = {
        "ann": await accounts.create_local_account("ann", "ann-secret", role="admin", display_name="Ann Admin"),
        "admin": await accounts.get_account(DEFAULT_ADMIN_ID),
        "alice": await accounts.create_local_account("alice", "alice-secret", role="user"),
        "reader": await accounts.create_local_account("reader", "reader-secret", role="readonly"),
    }
    sessions = {name: session_store.create(Principal.password_session(person), lifetime_seconds=3600)[0]
                for name, person in people.items()}
    _record, raw = await accounts.create_token(DEFAULT_INTEGRATION_ID, "probe")
    transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        def as_(name):
            if name == "integration":
                return {"Authorization": f"Bearer {raw}"}
            return {"Cookie": f"{HTTP_SESSION_COOKIE}={sessions[name]}",
                    "X-CSRF-Token": session_store.csrf_token(sessions[name])}

        async def choose(name, **change):
            return await client.patch(OWN, headers=as_(name), json=change)

        async def publish(name, mode, palette):
            return await client.put(PUBLISH, headers=as_(name), json={"mode": mode, "palette": palette})

        yield SimpleNamespace(client=client, as_=as_, choose=choose, publish=publish, people=people)
    session_store.clear()


async def _row():
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT id, mode, palette FROM login_appearance")
    return [(row["id"], row["mode"], row["palette"]) for row in rows]


def _shown(page: str) -> dict:
    """What a rendered sign-in page shows: its root mode, colour scheme and
    body palette -- the only appearance facts it carries."""
    root = re.search(r'<html lang="en" data-theme="(dark|light)">', page)
    scheme = re.search(r'<meta name="color-scheme" content="([a-z ]+)">', page)
    body = re.search(r"<body([^>]*)>", page).group(1)
    palette = re.search(r'data-dp-palette="([a-z0-9-]+)"', body)
    assert root and scheme and scheme.group(1) == root.group(1)
    assert (' class="light"' in body) == (root.group(1) == "light")
    mode = root.group(1)
    return {"mode": mode, "palette": palette.group(1) if palette else account_appearance.PALETTES[mode][0]}


async def _login(client) -> dict:
    client.cookies.clear()                                  # a new, anonymous browser
    page = await client.get("/login")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    return _shown(page.text)


# ── personal appearance and publication ─────────────────────────────────────

async def test_no_personal_choice_changes_the_login_appearance(live):
    client, choose = live.client, live.choose
    assert await _row() == [] and await _login(client) == NATIVE_DARK          # never published: native Dark
    # Admins and every other role choose modes and palettes: each its own, never the installation's.
    for name, change in (("ann", {"mode": "light", "light_palette": "catppuccin-latte"}),
                         ("admin", {"dark_palette": "dracula"}), ("admin", {"mode": "light"}),
                         ("alice", {"mode": "light", "light_palette": "flexoki-light"}),
                         ("reader", {"dark_palette": "rose-pine"})):
        assert (await choose(name, **change)).status_code == 200
    assert await _row() == [] and await _login(client) == NATIVE_DARK
    assert await account_appearance.load(live.people["ann"].id) == {
        "mode": "light", "dark_palette": "debridpulse-dark", "light_palette": "catppuccin-latte"}
    # Published once, a later personal choice still leaves it.
    assert (await live.publish("ann", "light", "catppuccin-latte")).json() == {"mode": "light", "palette": "catppuccin-latte"}
    await choose("ann", mode="dark")
    await choose("admin", light_palette="nord-light")
    assert await _row() == [(1, "light", "catppuccin-latte")]
    assert await _login(client) == {"mode": "light", "palette": "catppuccin-latte"}


async def test_only_an_active_admin_publishes_its_own_saved_theme(live):
    client = live.client
    # Reading it is an Admin's too.
    assert (await client.get(PUBLISH, headers=live.as_("ann"))).json() == NATIVE_DARK
    for name in ("alice", "reader", "integration"):
        assert (await client.get(PUBLISH, headers=live.as_(name))).status_code == 403
    client.cookies.clear()
    assert (await client.get(PUBLISH)).status_code == 401
    assert (await client.put(PUBLISH, json={"mode": "dark", "palette": "dracula"})).status_code == 401
    # Other roles, even sending their own saved pair, are refused before anything is written.
    await live.choose("alice", dark_palette="dracula")
    for name in ("alice", "reader", "integration"):
        assert (await live.publish(name, "dark", "dracula")).status_code == 403
    # Without the session's CSRF token: refused.
    headers = dict(live.as_("ann"))
    headers.pop("X-CSRF-Token")
    assert (await client.put(PUBLISH, headers=headers, json={"mode": "dark", "palette": "debridpulse-dark"})).status_code == 403
    # A pair outside the catalog, or another field: refused.
    for body in ({"mode": "dark", "palette": "nord-light"}, {"mode": "dark", "palette": "x"},
                 {"mode": "dark", "palette": "dracula", "account_id": "x"}):
        assert (await client.put(PUBLISH, headers=live.as_("ann"), json=body)).status_code == 422
    assert await _row() == []
    # A pair that is not the Admin's saved active pair (unsaved, or overtaken): refused, unchanged.
    assert (await live.publish("ann", "dark", "dracula")).status_code == 409
    assert await _row() == []
    # The Admin's own saved active pair: published, and the server's answer is what is published.
    await live.choose("ann", dark_palette="dracula")
    response = await live.publish("ann", "dark", "dracula")
    assert response.status_code == 200 and response.json() == {"mode": "dark", "palette": "dracula"}
    assert await _row() == [(1, "dark", "dracula")]
    assert (await client.get(PUBLISH, headers=live.as_("admin"))).json() == {"mode": "dark", "palette": "dracula"}
    assert await _login(client) == {"mode": "dark", "palette": "dracula"}
    # Republishing the same pair is the same single row.
    assert (await live.publish("ann", "dark", "dracula")).status_code == 200
    assert await _row() == [(1, "dark", "dracula")]


async def test_eligibility_is_the_accounts_own_at_the_publication(live):
    ann = live.people["ann"].id
    await live.choose("ann", dark_palette="dracula")
    await live.publish("ann", "dark", "dracula")
    await live.choose("ann", dark_palette="kanagawa-wave")
    # Demoted after its session began: whatever the session says, it no longer publishes.
    await accounts.set_role(DEFAULT_ADMIN_ID, ann, "user")
    assert (await live.publish("ann", "dark", "kanagawa-wave")).status_code in {401, 403}
    with pytest.raises(account_appearance.LoginAppearanceRefused):
        await account_appearance.publish(ann, account_appearance.LoginAppearance(mode="dark", palette="kanagawa-wave"))
    # Disabled, or no such account: never a publisher, even called directly.
    await accounts.set_role(DEFAULT_ADMIN_ID, ann, "admin")
    await accounts.set_enabled(DEFAULT_ADMIN_ID, ann, False)
    for account in (ann, "00000000-0000-4000-8000-00000000dead"):
        with pytest.raises(account_appearance.LoginAppearanceRefused):
            await account_appearance.publish(account, account_appearance.LoginAppearance(mode="dark", palette="kanagawa-wave"))
    assert await _row() == [(1, "dark", "dracula")]


async def test_a_failed_publication_leaves_the_published_theme(live, monkeypatch):
    await live.choose("ann", dark_palette="dracula")
    await live.publish("ann", "dark", "dracula")
    await live.choose("ann", mode="light")

    async def broken(_db, _pair):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(account_appearance, "_project", broken)
    response = await live.publish("ann", "light", "debridpulse-light")
    assert response.status_code >= 500
    assert await _row() == [(1, "dark", "dracula")]
    assert await account_appearance.load(live.people["ann"].id) == {
        "mode": "light", "dark_palette": "dracula", "light_palette": "debridpulse-light"}


@pytest.mark.parametrize("first", ["ann", "admin"])
async def test_two_admins_publishing_at_once_serialize_and_the_last_commit_wins_whole(live, monkeypatch, first):
    """Real concurrent transactions on the real database: the first Admin's
    publication holds SQLite's write lock while the second waits in
    ``BEGIN IMMEDIATE``; the second commits last and its whole pair is the
    login appearance -- never one Admin's mode with the other's palette."""
    second = "admin" if first == "ann" else "ann"
    ids = {name: live.people[name].id for name in ("ann", "admin")}
    await account_appearance.update(ids[first], account_appearance.AppearanceUpdate(mode="light", light_palette="rose-pine-dawn"))
    await account_appearance.update(ids[second], account_appearance.AppearanceUpdate(dark_palette="synthwave-84"))
    shown = {first: account_appearance.LoginAppearance(mode="light", palette="rose-pine-dawn"),
             second: account_appearance.LoginAppearance(mode="dark", palette="synthwave-84")}
    holding, release = asyncio.Event(), asyncio.Event()
    commits = []
    project = account_appearance._project

    async def paced(db, pair):
        if not holding.is_set():
            holding.set()
            await release.wait()
        await project(db, pair)
        commits.append(pair)

    monkeypatch.setattr(account_appearance, "_project", paced)
    one = asyncio.create_task(account_appearance.publish(ids[first], shown[first]))
    await asyncio.wait_for(holding.wait(), 5)
    two = asyncio.create_task(account_appearance.publish(ids[second], shown[second]))
    await asyncio.sleep(0.3)
    assert not two.done()                                  # serialized behind the first transaction
    release.set()
    await asyncio.wait_for(asyncio.gather(one, two), 30)

    assert commits == [("light", "rose-pine-dawn"), ("dark", "synthwave-84")]
    assert await _row() == [(1, "dark", "synthwave-84")]
    assert await _login(live.client) == {"mode": "dark", "palette": "synthwave-84"}


async def test_a_choice_saved_while_a_publication_waits_is_not_published_in_its_place(live, monkeypatch):
    """The fence: the publication carries the pair the interface showed; a
    newer choice that commits first makes it stale, and it is refused rather
    than publishing a pair other than the one on screen."""
    ann = live.people["ann"].id
    await account_appearance.update(ann, account_appearance.AppearanceUpdate(dark_palette="dracula"))
    entered, release = asyncio.Event(), asyncio.Event()
    publishes = account_appearance._publishes_login

    async def held(db, account_id):
        entered.set()
        await release.wait()
        return await publishes(db, account_id)

    # A newer choice commits before the publication's transaction begins.
    await account_appearance.update(ann, account_appearance.AppearanceUpdate(dark_palette="gruvbox-dark"))
    with pytest.raises(account_appearance.LoginAppearanceStale):
        await account_appearance.publish(ann, account_appearance.LoginAppearance(mode="dark", palette="dracula"))
    # A newer choice waiting on the publication's lock commits after it: the published pair is the shown one.
    monkeypatch.setattr(account_appearance, "_publishes_login", held)
    publication = asyncio.create_task(account_appearance.publish(ann, account_appearance.LoginAppearance(
        mode="dark", palette="gruvbox-dark")))
    await asyncio.wait_for(entered.wait(), 5)
    later = asyncio.create_task(account_appearance.update(ann, account_appearance.AppearanceUpdate(dark_palette="ayu-mirage")))
    await asyncio.sleep(0.3)
    assert not later.done()
    release.set()
    assert await asyncio.wait_for(publication, 30) == {"mode": "dark", "palette": "gruvbox-dark"}
    await asyncio.wait_for(later, 30)
    assert await _row() == [(1, "dark", "gruvbox-dark")]
    assert (await account_appearance.load(ann))["dark_palette"] == "ayu-mirage"


# ── the row's lifetime ──────────────────────────────────────────────────────

async def test_the_publication_is_the_installations_after_the_admin_is_gone(live):
    ann = live.people["ann"].id
    await live.choose("ann", mode="light", light_palette="everforest-light")
    await live.publish("ann", "light", "everforest-light")
    chosen = {"mode": "light", "palette": "everforest-light"}
    await accounts.set_role(DEFAULT_ADMIN_ID, ann, "user")
    assert await _login(live.client) == chosen
    await accounts.set_role(DEFAULT_ADMIN_ID, ann, "admin")
    await accounts.set_enabled(DEFAULT_ADMIN_ID, ann, False)
    assert await _login(live.client) == chosen
    await accounts.set_enabled(DEFAULT_ADMIN_ID, ann, True)
    duplicate = (await accounts.resolve_oidc("https://id.example", "ann-subject", {"name": "Ann"},
                                             default_role="user")).id
    await accounts.merge_accounts(DEFAULT_ADMIN_ID, [duplicate], ann)
    await accounts.delete_account(DEFAULT_ADMIN_ID, ann)
    assert await _login(live.client) == chosen
    # A later Admin's publication replaces it.
    await live.choose("admin", dark_palette="oxocarbon-dark")
    await live.publish("admin", "dark", "oxocarbon-dark")
    assert await _login(live.client) == {"mode": "dark", "palette": "oxocarbon-dark"}


async def test_years_of_publications_are_one_row_of_mode_and_palette(live):
    admin = DEFAULT_ADMIN_ID
    for index in range(120):
        mode = "light" if index % 2 == 0 else "dark"
        palette = account_appearance.PALETTES[mode][index % 10]
        await account_appearance.update(admin, account_appearance.AppearanceUpdate(mode=mode, **{f"{mode}_palette": palette}))
        await account_appearance.publish(admin, account_appearance.LoginAppearance(mode=mode, palette=palette))
    with sqlite3.connect(database.DB_PATH) as conn:
        assert [row[1] for row in conn.execute("PRAGMA table_info(login_appearance)")] == ["id", "mode", "palette"]
        assert conn.execute("SELECT count(*) FROM login_appearance").fetchone() == (1,)
    assert await _row() == [(1, "dark", account_appearance.PALETTES["dark"][119 % 10])]


async def test_the_wipe_keeps_the_login_appearance(live):
    await live.choose("ann", mode="light", light_palette="ayu-light")
    await live.publish("ann", "light", "ayu-light")
    result = await db_maintenance.wipe_database(verified_quiesced=True)
    assert result["ok"] is True and "login_appearance" not in result["wiped_tables"]
    assert await _row() == [(1, "light", "ayu-light")]


# ── the anonymous read ──────────────────────────────────────────────────────

async def test_the_sign_in_pages_read_the_one_row_and_no_account(live, monkeypatch):
    await live.choose("ann", dark_palette="kanagawa-wave")
    await live.publish("ann", "dark", "kanagawa-wave")
    await accounts.delete_account(DEFAULT_ADMIN_ID, live.people["ann"].id)    # its selector is gone
    statements = []
    configure = database._configure_sqlite_connection

    async def traced(conn):
        await configure(conn)
        await conn.set_trace_callback(statements.append)

    monkeypatch.setattr(database, "_configure_sqlite_connection", traced)
    from api import auth_routes

    chosen = {"mode": "dark", "palette": "kanagawa-wave"}
    # The anonymous pages: sign-in, the state-free unavailable page and OIDC start's error page.
    anonymous = [(await live.client.get("/login")).text,
                 (await auth_routes._state_free_auth_page(message="Try again shortly.", status_code=429)).body.decode(),
                 (await live.client.get("/auth/oidc/start")).text]
    assert [_shown(text) for text in anonymous] == [chosen] * 3
    assert statements and all("account" not in sql or sql == _SINGLE_READ for sql in statements)
    assert statements.count(_SINGLE_READ) == 3
    # A refused password: authentication checks its credential; the page it renders reads the one row.
    signed = await live.client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', signed.text).group(1)
    refused = await live.client.post("/login", data={"username": "nobody", "password": "wrong", "csrf_token": token})
    assert refused.status_code == 401 and _shown(refused.text) == chosen
    assert not any("account_appearance" in sql for sql in statements)
    for text in anonymous + [refused.text]:
        for private in ("Ann Admin", live.people["ann"].id, DEFAULT_ADMIN_ID, "updated_at", "operator"):
            assert private not in text


async def test_a_retired_corrupt_missing_or_unreadable_row_is_a_safe_native_appearance(live, monkeypatch):
    async def stored(mode, palette):
        async with database.get_db() as db:
            await db.execute("INSERT OR REPLACE INTO login_appearance(id,mode,palette) VALUES(1,?,?)", (mode, palette))
            await db.commit()

    await stored("light", "retired-sunset")
    assert await _login(live.client) == {"mode": "light", "palette": "debridpulse-light"}
    await stored("dark", 'x" onload="alert(1)')
    page = (await live.client.get("/login")).text
    assert _shown(page) == NATIVE_DARK and "onload" not in page
    with sqlite3.connect(database.DB_PATH) as conn:
        conn.execute("DROP TABLE login_appearance")
    assert await _login(live.client) == NATIVE_DARK                         # missing: native, sign-in still served

    from api import auth_routes

    async def slow():
        await asyncio.sleep(30)

    monkeypatch.setattr(auth_routes, "_APPEARANCE_READ_SECONDS", 0.05)
    monkeypatch.setattr(account_appearance, "login", slow)
    started = asyncio.get_running_loop().time()
    assert await _login(live.client) == NATIVE_DARK
    assert asyncio.get_running_loop().time() - started < 5


async def test_the_page_keeps_its_security_contract_and_no_browser_authority(live):
    from api import auth_routes

    await live.choose("ann", mode="light", light_palette="bluloco-light")
    await live.publish("ann", "light", "bluloco-light")
    page = await live.client.get("/login")
    csp = page.headers["content-security-policy"]
    script = auth_routes._AUTH_PAGE_SCRIPT
    assert f"script-src 'sha256-{base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()}'" in csp
    assert f"<script>{script}</script>" in page.text and page.text.count("<script") == 1
    assert "localStorage" not in script and "prefers-color-scheme" not in page.text
    assert "default-src 'none'" in csp and "img-src data:" in csp and "connect-src" not in csp
    assert 'data-password-reveal' in page.text and page.headers["cache-control"] == "no-store"


# ── one palette catalog, one logo ───────────────────────────────────────────

# The core tokens the sign-in page's mapping and the logo read from a palette.
_CORE = ("--dp-bg-app", "--dp-bg-app-alt", "--dp-surface-1", "--dp-surface-input", "--dp-border-default",
         "--dp-border-strong", "--dp-text-primary", "--dp-text-secondary", "--dp-text-muted", "--dp-accent-purple",
         "--dp-accent-purple-bright", "--dp-accent-blue", "--dp-accent-blue-bright", "--dp-accent-rgb",
         "--dp-accent-contrast")


async def test_every_catalog_palette_is_drawn_from_the_applications_own_values(live):
    from api import auth_routes

    palettes = (STATIC / "ui-theme-palettes.css").read_text(encoding="utf-8")
    for mode, ids in account_appearance.PALETTES.items():
        for palette in ids:
            page = auth_routes._login_shell("<main></main>", {"mode": mode, "palette": palette})
            assert _shown(page) == {"mode": mode, "palette": palette}
            if palette == ids[0]:
                assert 'data-dp-palette="' not in page and palettes not in page    # native: the page's own values
                continue
            assert palettes in page                                              # the application's stylesheet, whole
            block = palettes.split(f'body[data-dp-palette="{palette}"],', 1)[1].split("}", 1)[0]
            assert all(re.search(rf"{token}:\s*[^;]+;", block) for token in _CORE), palette
    # The page maps decorative names only: the error material keeps DebridPulse's meaning.
    mapping = auth_routes._AUTH_PAGE_STYLE.split("body[data-dp-palette] {", 1)[1].split("}", 1)[0]
    assert "--danger" not in mapping and "--dp-state" not in mapping and "--dp-identity" not in mapping
    assert "--danger" not in palettes and "--dp-state-" not in palettes.split("*/", 1)[1]


async def test_the_inline_marks_are_the_canonical_logo(live):
    from api import auth_routes

    source = (STATIC / "logo.svg").read_text(encoding="utf-8")
    geometry = re.findall(r'<(?:rect|path)\b[^>]*>', source)
    assert len(geometry) == 3 and not (STATIC / "logo-128.png").exists()
    for mark, prefix in ((auth_routes._AUTH_MARK_HTML, "dp-login-logo"), (auth_routes._APP_LOGO_SVG, "dp-app-logo")):
        assert mark.startswith("<svg ") and 'aria-hidden="true"' in mark and "<title" not in mark
        assert [re.sub(rf"#{prefix}-", "#", tag) for tag in re.findall(r'<(?:rect|path)\b[^>]*>', mark)] == geometry
        assert set(re.findall(r'id="([^"]+)"', mark)) == {f"{prefix}-{name}" for name in
                                                         ("dp-outline", "dp-arrow", "dp-ekg", "dp-glow")}
    # ui-logo.css colours every hooked part of the mark, the pulse line's three stops included, and nothing else.
    hooks = re.findall(r'class="(dp-logo-[\w-]+)"', source)
    style = (STATIC / "ui-logo.css").read_text(encoding="utf-8")
    assert sorted(hooks) == sorted(re.findall(r"^\.(dp-logo-[\w-]+) \{", style, re.M))
    assert {"dp-logo-ekg-start", "dp-logo-ekg-mid", "dp-logo-ekg-end"} <= set(hooks) and "tray" not in source + style
    login = (await live.client.get("/login")).text
    assert auth_routes._AUTH_MARK_HTML in login and "data:image/png" not in login
    assert (STATIC / "ui-logo.css").read_text(encoding="utf-8") in login
    bootstrap = (await live.client.get("/ui-theme-bootstrap.js", headers=live.as_("alice"))).text
    head = bootstrap.split("\n", 1)[0]
    assert json.loads(re.fullmatch(r"window\.debridPulseLogo=(.*);", head).group(1)) == auth_routes._APP_LOGO_SVG
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '<dp-logo class="logo-icon" role="img" aria-label="DebridPulse logo"></dp-logo>' in index
    assert 'src="/logo.svg' not in index


# ── the page's artwork and version ──────────────────────────────────────────

async def test_the_wave_field_is_continuous_and_the_version_follows_the_card(live):
    from api import auth_routes

    backdrop = auth_routes._AUTH_BACKDROP_HTML
    waves = re.findall(r'<path class="wave [a-z]+" d="M(-?\d+) \d+ (.*?) (-?\d+) -?\d+"/>', backdrop)
    assert len(waves) == 15 and 'class="wave-field spark"' in backdrop
    assert "wave-purple spark" not in backdrop and "wave-blue spark" not in backdrop
    assert all(int(start) < 0 and int(end) > 1600 for start, _middle, end in waves)    # edge to edge, no gap
    assert 'stroke:url(#auth-wave)' in auth_routes._AUTH_PAGE_STYLE
    signed = await live.client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', signed.text).group(1)
    pages = [signed.text,
             (await live.client.post("/login", data={"username": "nobody", "password": "wrong", "csrf_token": token})).text,
             (await auth_routes._state_free_auth_page(message="Try again shortly.", status_code=429)).body.decode()]
    for page in pages:
        assert page.count('class="version"') == 1
        before, card, after = re.split(r'<main class="card">|</main>', page)
        assert 'class="version"' not in before + card                                  # not in the card
        assert re.match(r'\s*<div class="version">v[^<]+</div>\s*</body>', after)      # the card's next sibling
