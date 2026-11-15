"""Cancel, then Delete: the deleted transfer reclaims what its stopped
execution owned (WS3 I-2), through the one core retirement owner.

Cancel stops the writer and reclaims nothing. Delete then retires the
execution-owned material -- the partial target and the declared transient
material -- once the durable record proves the writer stopped and no cleanup
obligation is open. A writer whose stop is unconfirmed keeps its material and
its obligation; unowned, unrelated and delivered material is never touched;
Delete is idempotent; a restart resolves a retained obligation from durable
state.

Every case runs ``cancel_delete_world.py`` as its own process: the product's
``DB_PATH`` names that process's database, the engine, repository, ownership
records and retirement are production code, and the executor is the existing
ledger fake. Nothing is patched.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

WORLD = Path(__file__).with_name("cancel_delete_world.py")
STOPPED = {"cleanup_state": "complete", "authorized": 0, "state": "cancelled"}


def world(scenario: str, work: Path) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([sys.executable, str(WORLD), scenario, str(work)], capture_output=True, text=True,
                            timeout=120, cwd=WORLD.parents[1], env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_partial_transfers_off_cancel_keeps_the_transient_material_and_delete_reclaims_it(tmp_path):
    report = world("partial-off", tmp_path)
    assert report["owned"]
    assert report["after cancel"]["material"] and report["after cancel"]["cleanup"] == STOPPED
    after = report["after delete"]
    assert after["transfer"] == "deleted" and not after["journal"] and after["target"] is None
    assert after["unrelated"] and after["files"] == ["unrelated.bin"]


def test_partial_transfers_on_cancel_keeps_the_partial_target_and_delete_reclaims_it(tmp_path):
    report = world("partial-on", tmp_path)
    assert report["owned"]
    assert report["after cancel"]["material"] and report["after cancel"]["cleanup"] == STOPPED
    after = report["after delete"]
    assert after["transfer"] == "deleted" and after["target"] is None
    assert after["unrelated"] and after["files"] == ["unrelated.bin"]


def test_a_writer_that_may_still_be_running_keeps_its_material_through_delete(tmp_path):
    report = world("live", tmp_path)
    after = report["after delete"]
    assert after["writer"] == "running"
    assert after["material"]
    assert after["cleanup"]["cleanup_state"] in {"pending", "blocked"}


def test_an_unconfirmed_stop_keeps_material_and_obligation_until_the_stop_is_observed(tmp_path):
    report = world("lost-ack", tmp_path)
    assert report["after delete"]["material"]
    assert report["after delete"]["cleanup"]["cleanup_state"] in {"pending", "blocked"}
    later = report["after the retained obligation's next pass"]
    assert not later["material"] and later["cleanup"]["cleanup_state"] == "complete"


def test_material_the_execution_does_not_own_survives_cancel_and_delete(tmp_path):
    report = world("unowned", tmp_path)
    assert not report["owned"]
    after = report["after delete"]
    assert after["target"] == repr(b"operator material") and after["unrelated"]


def test_a_delivered_sibling_survives_while_the_cancelled_files_partial_is_reclaimed(tmp_path):
    report = world("completed", tmp_path)
    assert report["sibling before cancel"] == "completed"
    after = report["after delete"]
    assert after["sibling"] == {"state": "completed", "payload": repr(b"done")}
    assert after["target"] is None
    assert after["files"] == ["delivered", "unrelated.bin"]


def test_a_repeated_delete_is_idempotent(tmp_path):
    report = world("repeat", tmp_path)
    assert report["after delete"]["files"] == ["unrelated.bin"]
    again = report["after second delete"]
    assert not again["material"] and again["unrelated"] and again["files"] == ["unrelated.bin"]
    assert again["cleanup"] == STOPPED


def test_a_retained_obligation_is_resolved_from_durable_state_after_a_restart(tmp_path):
    before = world("restart-before", tmp_path)
    assert before["after delete"]["material"] and before["after delete"]["writer"] == "running"
    assert before["after delete"]["cleanup"]["cleanup_state"] in {"pending", "blocked"}
    after = world("restart-after", tmp_path)["after restart"]
    assert not after["material"] and after["cleanup"]["cleanup_state"] == "complete"
    assert after["unrelated"] and after["files"] == ["unrelated.bin"]
