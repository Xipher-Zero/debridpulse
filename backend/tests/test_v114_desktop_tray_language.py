"""The desktop application's tray and notices speak the interface's language.

The tray's words were read once, at start, from the installation's default
language, while the language a person chooses is their account's own: so the
tray stayed English on Windows and Linux whatever was chosen, and on macOS
changed only after a restart. The backend now answers which language the
interface is shown in (op ``locale``: the default Admin's own choice, else the
installation's default -- the desktop application is always Standalone), and
the supervisor follows it while it runs, redrawing the tray when it changes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(ROOT))

from desktop import supervisor  # noqa: E402
from test_v114_desktop_control import Backend  # noqa: E402


@pytest.mark.asyncio
async def test_the_control_channel_answers_the_interfaces_language_as_it_changes(tmp_path):
    desktop = await Backend(tmp_path / "desktop", locked=False).start()
    try:
        assert await desktop.ready()
        assert (await desktop.channel.ask("locale"))["locale"] == "en"            # nothing chosen: the default
        status, body = await desktop.exchange("PUT", "/api/account/locale", json={"locale": "de"})
        assert status == 200 and body["locale"] == "de"
        assert (await desktop.channel.ask("locale"))["locale"] == "de"            # the person's own choice
        status, body = await desktop.exchange("PUT", "/api/account/locale", json={"locale": None})
        assert status == 200
        assert (await desktop.channel.ask("locale"))["locale"] == "en"            # cleared: the default again
    finally:
        await desktop.stop()


def test_the_trays_words_follow_a_chosen_language_and_ignore_an_unknown_one(tmp_path):
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    words = supervisor.Words(config)
    status = {"active": 1, "queued": 0, "completed": 0, "paused": False}
    english = supervisor.tray_menu(status, words)
    assert words.tag == "en" and words.choose("en") is False
    assert words.choose("de") is True and words.tag == "de"
    german = supervisor.tray_menu(status, words)
    assert german != english and "DownloadPulse beenden" in repr(german)
    assert words.choose("de") is False                                          # no change, no redraw
    assert words.choose("xx-unknown") is False and words.tag == "de"
    assert words.choose(None) is False and words.tag == "de"


def test_the_loop_redraws_the_tray_when_the_language_changes():
    source = (ROOT / "desktop" / "supervisor.py").read_text(encoding="utf-8")
    assert 'words.choose((await channel.ask("locale")).get("locale"))       # the interface\'s language' in source
    assert 'relabel = words.choose((await channel.ask("locale")).get("locale"))' in source
    assert "if current != status or relabel or not native.tray_shown:" in source
    # macOS: the application menu (its Quit, Cmd+Q) is set again in the new language.
    assert 'if relabel and sys.platform == "darwin":\n' in source
    relabelled = source[source.index('if relabel and sys.platform == "darwin":'):]
    assert relabelled.index('await _window(shell, "window.setMainMenu", main_menu(words))') < 200


def test_the_macos_application_menu_is_drawn_in_the_chosen_language(tmp_path):
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    words = supervisor.Words(config)
    assert supervisor.main_menu(words)[0]["menuItems"][0]["text"] == "Quit DownloadPulse"
    assert words.choose("de")
    assert supervisor.main_menu(words)[0]["menuItems"][0]["text"] == "DownloadPulse beenden"
