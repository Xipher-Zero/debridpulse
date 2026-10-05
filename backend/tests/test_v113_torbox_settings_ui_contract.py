"""TorBox in Settings and Provider Status: registrations of existing owners.

Static contracts over the served sources. The TorBox card is the Real-Debrid
card's structure through the one account-connection owner; its premium facts
are interpreted by the one premium-account owner; Provider Status discovers it
from metadata alone.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
SETTINGS_JS = (STATIC / "ui-settings-page.js").read_text(encoding="utf-8")
PREMIUM_JS = (STATIC / "ui-premium-account-status.js").read_text(encoding="utf-8")
STATUS_JS = (STATIC / "ui-provider-status.js").read_text(encoding="utf-8")
PAGE_CSS = (STATIC / "ui-settings-page.css").read_text(encoding="utf-8")
CHROME_CSS = (STATIC / "ui-settings-chrome.css").read_text(encoding="utf-8")


def card(identity: str) -> str:
    start = SETTINGS_JS.index(f"providerCard('{identity}',")
    return SETTINGS_JS[start:SETTINGS_JS.index("});", start)]


def test_the_supplied_icon_is_the_only_torbox_mark():
    assert (STATIC / "icons" / "providers" / "torbox.svg").is_file()
    assert set(re.findall(r"/icons/providers/torbox[^\"' )]*", SETTINGS_JS)) == {"/icons/providers/torbox.svg"}
    assert sorted(path.name for path in (STATIC / "icons" / "providers").glob("torbox*")) == ["torbox.svg"]
    assert ".dp-settings-provider-chip--torbox" in CHROME_CSS


def test_the_card_is_the_real_debrid_structure_through_one_connection_owner():
    torbox, realdebrid = card("torbox"), card("realdebrid")
    for text in (torbox, realdebrid):
        assert 'class="dp-settings-account-connection"' in text
        assert "<details class=\"dp-settings-additional\">" in text
        assert "headerAction: providerTestAction(" in text
        assert "PROVIDER_STATUS_SPACER" not in text and "dp-settings-group-separator" not in text
    assert "deviceConnectionMarkup('torbox')" in torbox
    assert "deviceConnectionMarkup('realdebrid')" in realdebrid
    # One island grammar, no provider-named copy of it.
    assert "dp-settings-realdebrid-" not in SETTINGS_JS + PAGE_CSS
    assert "dp-settings-torbox-" not in SETTINGS_JS + PAGE_CSS
    assert SETTINGS_JS.count("function deviceConnectionMarkup(") == 1


def test_the_connection_language_is_account_oriented_and_shared():
    owner = SETTINGS_JS[SETTINGS_JS.index("const DEVICE_ACCOUNTS"):SETTINGS_JS.index("function renderDeviceConnection")]
    assert "torbox: {" in owner and "name: 'TorBox'" in owner
    for phrase in ("Account Connection", "Connect ${html(name)}", "Connected as ${html(who)}", "Disconnect"):
        assert phrase in owner
    for forbidden in ("API token", "api_token\"", "Bearer"):
        assert forbidden not in owner


def test_usenet_via_torbox_is_one_precise_persisted_toggle():
    torbox = card("torbox")
    assert "tuningToggle('torbox_usenet_enabled', 'Usenet via TorBox'," in torbox
    assert "Let TorBox process NZB downloads remotely." in torbox
    assert "torbox_usenet_enabled: {scope: 'integration:torbox', option: 'usenet_enabled', commit: 'immediate'}" \
        in SETTINGS_JS
    assert "'torbox'" in SETTINGS_JS[SETTINGS_JS.index("const INTEGRATION_SCOPES"):][:200]


def test_prepare_backup_torrents_is_one_precise_persisted_toggle_off_by_default():
    torbox = card("torbox")
    assert "tuningToggle('torbox_prepare_backup_torrents', 'Prepare Backup Torrents'," in torbox
    assert ("Also add torrents to TorBox as backup sources while another provider delivers them. "
            "Uses TorBox create limits and active slots.") in torbox
    assert "torBoxOf(s).prepare_backup_torrents === true" in torbox        # absent reads as off
    assert ("torbox_prepare_backup_torrents: {scope: 'integration:torbox', option: 'prepare_backup_torrents', "
            "commit: 'immediate'}") in SETTINGS_JS
    # The one control lives in TorBox's own Additional Settings, nowhere else.
    assert SETTINGS_JS.count("prepare_backup_torrents") == 4   # mapping key + option, toggle key, value read
    for jargon in ("standby", "speculative", "prewarm"):
        assert jargon not in torbox.casefold()


def test_one_premium_owner_interprets_torbox_and_words_it_once():
    # The one premium owner reads TorBox's account exactly as every other's:
    # from the neutral account truth, never a TorBox-keyed interpreter.
    assert "torbox" not in PREMIUM_JS.casefold()
    assert PREMIUM_JS.count("function premiumAccount(") == 1 and "status?.account" in PREMIUM_JS
    assert PREMIUM_JS.count("function describe(") == 1
    # The card states the shared owner's wording and tier, never its own.
    expiry = SETTINGS_JS[SETTINGS_JS.index("function accountExpiry("):SETTINGS_JS.index("function accountIsland(")]
    assert "owner.describe(premium.until, premium.tier)" in expiry
    assert "86400000" not in SETTINGS_JS and "days remaining" not in SETTINGS_JS


def test_provider_status_discovers_torbox_from_metadata_alone():
    assert "torbox" not in STATUS_JS.casefold()
