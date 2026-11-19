"""1.0.14 (operator, 2026-11-19): the holder of a connected browser's per-job
video sign-in (``external.browser_cookies``) hands it out only by the rules
the operator set, through the real engine: to the resolution unit and the
execution attempt of a transfer whose every outstanding demand is the cookie
owner's, while that browser's token is live; ``settle`` releases it when the
job ends. Each scenario is its own process (``browser_cookie_world.py``)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

WORLD = Path(__file__).with_name("browser_cookie_world.py")


def run(scenario: str, work: Path) -> dict:
    done = subprocess.run([sys.executable, str(WORLD), scenario, str(work)], capture_output=True, text=True,
                          timeout=120, check=False)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_owner_s_own_job_gets_it_in_resolution_and_execution_until_it_ends(tmp_path):
    report = run("one", tmp_path)
    assert report["outside a unit"] is None
    assert report["resolution saw the sign-in"] == [True]
    assert report["attempt gets the sign-in"] is True and report["unknown attempt gets"] is None
    assert report["settled while running"] == 0
    assert report["settled after withdrawal"] == 1 and report["held"] is False
    assert report["attempt gets after withdrawal"] is False


@pytest.mark.parametrize("scenario", ["merged", "revoked", "unregistered"])
def test_a_shared_transfer_a_revoked_browser_or_no_registration_gets_none(tmp_path, scenario):
    report = run(scenario, tmp_path)
    assert report["resolution saw the sign-in"] == [False]
    assert report["attempt gets the sign-in"] is False
    if scenario == "merged":
        assert report["merged into the same transfer"] is True
    if scenario == "revoked":
        assert report["settled while running"] == 1


def test_capacity_refuses_rather_than_drops_and_unreserved_entries_are_released(tmp_path):
    assert run("capacity", tmp_path) == {"outside a unit": None, "over capacity": "refused",
                                         "settled": 64, "held after settle": 0}
