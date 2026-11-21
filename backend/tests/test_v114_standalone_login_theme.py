"""A Standalone installation's sign-in page follows its one person's theme.

On a Standalone installation (the desktop applications, and a server set up
for one person) the theme belongs to the default Admin, signed in or not --
as the language and the presentation already do -- and each saved change of
it is the sign-in page's appearance too: there is no Login Theme to publish,
and the theme menu shows none. A theme the browser held before moves to the
record once (``carry``). A Server installation is unchanged: with sign-in off
the theme is each browser's, and the sign-in page changes only when an Admin
publishes it.

Each case runs the real code in its own process, on a fresh database and a
real config.json (DB_PATH, CONFIG_PATH): nothing patched.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
STATIC = BACKEND.parent / "frontend" / "static"

SCENARIO = r'''
import asyncio, json
from core.config import apply_settings, get_settings, load_settings
apply_settings(load_settings())
from db import database
from auth.models import Principal
from api import auth_routes
from services import account_appearance as appearance

async def main():
    await database.init_db()
    settings = get_settings()
    principal = Principal.standalone()                       # sign-in off: every request's principal
    owner = appearance.appearance_owner(principal, settings)
    before = await auth_routes._appearance(principal)
    account = owner or principal.account_id
    await appearance.update(account, appearance.AppearanceUpdate(mode="light", light_palette="catppuccin-latte"),
                            login=appearance.login_follows(owner, settings))
    after = await auth_routes._appearance(principal)
    print(json.dumps({"owner": owner, "before": {k: v for k, v in before.items() if k != "catalog"},
                      "after": {k: v for k, v in after.items() if k not in ("catalog", "preferences")},
                      "login": await appearance.login()}))

asyncio.run(main())
'''


def _run(tmp_path: Path, deployment: str) -> dict:
    (tmp_path / "config.json").write_text(json.dumps({"deployment_type": deployment}), encoding="utf-8")
    env = {**os.environ, "DB_PATH": str(tmp_path / "dp.sqlite"), "CONFIG_PATH": str(tmp_path / "config.json")}
    env.pop("DOWNLOADPULSE_DESKTOP", None)
    done = subprocess.run([sys.executable, "-c", SCENARIO], cwd=BACKEND, env=env, capture_output=True, text=True,
                          timeout=120)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_standalone_installations_sign_in_page_follows_its_one_persons_theme(tmp_path):
    from auth.accounts import DEFAULT_ADMIN_ID
    from services import account_appearance
    if "catppuccin-latte" not in account_appearance.PALETTES["light"]:
        pytest.skip("the catalog no longer has the palette this case names")
    found = _run(tmp_path, "standalone")
    assert found["owner"] == DEFAULT_ADMIN_ID                                   # signed in or not: the one person's
    assert found["before"]["authority"] == "account" and found["before"]["login_follows"] is True
    assert found["before"]["carry"] is True                                    # nothing saved yet: the browser's moves once
    assert found["login"] == {"mode": "light", "palette": "catppuccin-latte"}   # saved with the theme
    assert found["after"]["login_follows"] is True and "carry" not in found["after"]


def test_a_server_installation_keeps_the_theme_in_the_browser_and_the_sign_in_page_published(tmp_path):
    found = _run(tmp_path, "server")
    assert found["owner"] is None and found["before"] == {"authority": "browser"}
    assert found["login"] == {"mode": "dark", "palette": "downloadpulse-dark"}  # an account's change never sets it


def test_the_theme_menu_offers_no_login_theme_where_the_sign_in_page_follows_and_a_held_theme_moves_once():
    picker = (STATIC / "ui-theme-picker.js").read_text(encoding="utf-8")
    assert "&& !window.DPTheme?.loginFollows ? loginSection() : null;" in picker
    theme = (STATIC / "ui-theme-bootstrap.js").read_text(encoding="utf-8")
    assert "loginFollows: source.login_follows === true," in theme
    assert "const held = !browser && source.carry === true" in theme
    assert "Object.assign(unsaved, {mode: light ? 'light' : 'dark', dark_palette: chosen.dark, light_palette: chosen.light});" in theme
