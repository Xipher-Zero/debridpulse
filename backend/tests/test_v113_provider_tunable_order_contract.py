"""A/B: one tunable grammar across the five Premium Services provider cards.

Prepare Backup Torrents first, Maximum Active Torrents second -- the two in
one existing ``tuningGroup`` where both exist -- then any provider-before-
Usenet preference, then the remaining operational tunables. Read from the
one Settings markup owner; no provider gets CSS of its own for it."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"
SETTINGS_JS = (STATIC / "ui-settings-page.js").read_text(encoding="utf-8")
CONTROL = re.compile(r"(?:input|tuningToggle)\('([a-z0-9_]+)'")


def tunables(identity: str) -> str:
    start = SETTINGS_JS.index(f"providerCard('{identity}'")
    cells = SETTINGS_JS.index("${tuningCells(", start)
    return SETTINGS_JS[cells:SETTINGS_JS.index("          )}", cells)]


def groups(block: str) -> list[list[str]]:
    found, index = [], 0
    while (index := block.find("tuningGroup(", index)) >= 0:
        depth, end = 0, index + len("tuningGroup")
        for end in range(end, len(block)):
            depth += {"(": 1, ")": -1}.get(block[end], 0)
            if depth == 0:
                break
        found.append(CONTROL.findall(block[index:end]))
        index = end
    return found


ORDER = {
    "alldebrid": ["alldebrid_prepare_backup_torrents", "alldebrid_max_active_torrents",
                  "alldebrid_rate_limit_per_minute", "poll_interval_seconds", "full_sync_interval_minutes",
                  "upload_fail_retry_count", "upload_fail_retry_delay_minutes"],
    "debridlink": ["debridlink_prepare_backup_torrents", "debridlink_request_timeout_seconds",
                   "debridlink_torrent_upload_timeout_seconds", "debridlink_host_refresh_interval_hours"],
    "premiumize": ["premiumize_prepare_backup_torrents", "premiumize_use_before_usenet",
                   "premiumize_request_timeout_seconds", "premiumize_upload_timeout_seconds",
                   "premiumize_host_refresh_interval_hours"],
    "realdebrid": ["realdebrid_prepare_backup_torrents", "realdebrid_max_active_torrents",
                   "realdebrid_rate_limit_per_minute", "realdebrid_request_timeout_seconds",
                   "realdebrid_torrent_upload_timeout_seconds", "realdebrid_host_refresh_interval_hours"],
    "torbox": ["torbox_prepare_backup_torrents", "torbox_max_active_torrents", "torbox_use_before_usenet",
               "torbox_rate_limit_per_minute", "torbox_request_timeout_seconds", "torbox_upload_timeout_seconds",
               "torbox_host_refresh_interval_hours"],
}
GROUPS = {
    "alldebrid": [["alldebrid_prepare_backup_torrents", "alldebrid_max_active_torrents"],
                  ["upload_fail_retry_count", "upload_fail_retry_delay_minutes"]],
    "debridlink": [],
    "premiumize": [],
    "realdebrid": [["realdebrid_prepare_backup_torrents", "realdebrid_max_active_torrents"]],
    "torbox": [["torbox_prepare_backup_torrents", "torbox_max_active_torrents"]],
}


@pytest.mark.parametrize("identity", sorted(ORDER))
def test_each_card_follows_the_locked_order_and_grouping(identity):
    block = tunables(identity)
    assert CONTROL.findall(block) == ORDER[identity]
    assert groups(block) == GROUPS[identity]                 # no one-item group; the Usenet preference outside


def test_the_grouping_is_the_existing_owner_with_no_provider_css():
    assert SETTINGS_JS.count("function tuningGroup(") == 1
    css = "".join(path.read_text(encoding="utf-8") for path in STATIC.glob("*.css"))
    for identity in ORDER:
        assert f"dp-settings-provider-card--{identity} .dp-settings-tuning-group" not in css
