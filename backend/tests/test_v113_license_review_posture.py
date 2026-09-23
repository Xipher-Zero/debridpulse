"""1.0.13 Gate-9 rev-5, item 5: record licence facts, not a legal verdict.

The inventory must keep stating what ships and what each package's licence is.
It must NOT encode an engineering assertion that the combined-work analysis has
been approved -- that conclusion belongs to a project/licence review, and
writing it down as settled is how an unreviewed obligation ships.

`hachoir` is characterized, not assumed: it is a hard, unguarded top-level
import in the service's own `sabnzbd/misc.py`, so the service cannot start
without it. It is therefore required by DebridPulse's supported
acquisition + PAR2-repair path and is retained.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INVENTORY = REPO / "docs" / "DEPENDENCY_LICENSES.md"
MANIFEST = REPO / "licenses" / "python-runtime.json"
LOCK = REPO / "backend" / "requirements.txt"


def text():
    return INVENTORY.read_text()


# --- the posture ----------------------------------------------------------

def test_the_inventory_does_not_claim_an_approved_or_proven_conclusion():
    lowered = text().lower()
    forbidden = (
        "legally approved",
        "legal approval",
        "has been reviewed by counsel",
        "compatibility is proven",
        "proven compatible",
        "no legal risk",
        "cleared for distribution",
    )
    found = [phrase for phrase in forbidden if phrase in lowered]
    assert not found, f"the inventory asserts a legal verdict it cannot: {found}"


def test_the_copyleft_section_defers_to_project_license_review():
    lowered = text().lower()
    assert "project/licence review" in lowered or "project/license review" in lowered, (
        "the copyleft conclusion must be marked as requiring project/licence review"
    )
    assert "requires" in lowered or "pending" in lowered


def test_the_copyleft_section_still_records_the_facts():
    """Deferring the verdict must not delete the obligations."""
    body = text()
    assert "## Copyleft review" in body
    for package, licence in (("sabctools", "GPL-2.0-or-later"),
                             ("hachoir", "GPL-2.0-only"),
                             ("guessit", "LGPL-3.0-or-later")):
        assert package in body and licence in body


# --- hachoir is retained, and the reason is recorded ----------------------

def test_hachoir_is_still_shipped_because_the_service_cannot_start_without_it():
    assert re.search(r"^hachoir==", LOCK.read_text(), re.M), "hachoir must remain in the lock"
    inventoried = {item["name"].lower() for item in json.loads(MANIFEST.read_text())["packages"]}
    assert "hachoir" in inventoried


def test_the_inventory_records_why_hachoir_is_required():
    body = text().lower()
    assert "hachoir" in body
    assert "misc.py" in body or "cannot start" in body or "unguarded" in body, (
        "record the characterization, not an assumption from upstream's requirements file"
    )


# --- the accepted rev-4 correction stays ---------------------------------

def test_the_inventory_is_still_the_current_1_0_13_closure():
    assert "inventory for the current `1.0.13` development tree" in text()
    assert "inventory for **final v1.0.12**" not in text()
