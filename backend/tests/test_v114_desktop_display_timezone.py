"""Whose clock the interface shows.

A server shows the time its compose file sets (the container's ``TZ``), else
UTC. A desktop application shows the computer's own: a ``TZ`` its person set,
else no zone named -- and the page then uses the zone its window knows, which
every platform's web engine reads from the system (``dpDisplayTimezone``). Each
case runs the real code in its own process, in a real environment: nothing
patched.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
STATIC = BACKEND.parent / "frontend" / "static"


def _zone(tz: str | None, desktop: bool) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("TZ", "DOWNLOADPULSE_DESKTOP")}
    if tz is not None:
        env["TZ"] = tz
    if desktop:
        env["DOWNLOADPULSE_DESKTOP"] = "1"
    done = subprocess.run([sys.executable, "-c", "from core.config import display_timezone; print(repr(display_timezone()))"],
                          cwd=BACKEND, env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    return eval(done.stdout.strip().splitlines()[-1])  # noqa: S307 -- our own repr() of a str


@pytest.mark.parametrize(("tz", "desktop", "expected"), [
    (None, False, "UTC"),                          # a server with no TZ: UTC, as before
    ("America/Phoenix", False, "America/Phoenix"),  # a server: the compose file's TZ
    (None, True, ""),                              # a desktop application: the computer's own (named by the page)
    ("Europe/Berlin", True, "Europe/Berlin"),      # a desktop application whose person set TZ
    ("  ", True, ""),
])
def test_the_display_timezone_is_the_servers_tz_or_the_desktops_own(tz, desktop, expected):
    assert _zone(tz, desktop) == expected


def test_every_timestamp_reads_the_one_owner_and_it_falls_back_to_the_windows_own_zone():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    owner = app[app.index("function dpDisplayTimezone() {"):]
    owner = owner[:owner.index("\n}\n")]
    assert "Intl.DateTimeFormat().resolvedOptions().timeZone" in owner
    assert "zone=dpDisplayTimezone();" in (STATIC / "ui-activity-log-runtime.js").read_text(encoding="utf-8")
    assert "return dpDisplayTimezone();" in (STATIC / "ui-settings-backup-manager.js").read_text(encoding="utf-8")
    for path in STATIC.glob("*.js"):
        if path.name != "app.js":
            assert "settingsData.timezone" not in path.read_text(encoding="utf-8").replace("settingsData?.timezone", "settingsData.timezone"), path.name
