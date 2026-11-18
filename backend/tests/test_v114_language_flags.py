"""The language list's flags are artwork, drawn the same on every platform.

They were emoji (regional-indicator pairs), which Windows draws as two letters.
Now the one place a flag is drawn (ui-language-picker.js ``face``) shows Noto's
waved region flag, icons/flags/<region>.svg (public domain), for every language
the list can offer.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
FLAGS = STATIC / "icons" / "flags"


def _regions() -> dict[str, str]:
    picker = (STATIC / "ui-language-picker.js").read_text(encoding="utf-8")
    table = picker[picker.index("const FLAGS = Object.freeze({"):picker.index("});", picker.index("const FLAGS"))]
    return {tag: region for tag, region in re.findall(r"'?([A-Za-z-]+)'?: '([a-z]{2})'", table)}


def test_every_language_the_list_can_offer_has_its_flag_file():
    regions = _regions()
    manifest = json.loads((STATIC / "i18n" / "manifest.json").read_text(encoding="utf-8"))["locales"]
    offered = {tag for tag, info in manifest.items() if info.get("status") in {"canonical", "supported", "planned"}
               and not tag.startswith("en-X")}
    assert offered <= set(regions), offered - set(regions)
    assert sorted(path.stem for path in FLAGS.glob("*.svg")) == sorted(set(regions.values()))


def test_the_flags_are_plain_artwork_and_no_emoji_flag_remains():
    for path in FLAGS.glob("*.svg"):
        svg = path.read_text(encoding="utf-8")
        assert set(re.findall(r"<([a-zA-Z]+)", svg)) <= {"svg", "defs", "path"}, path.name
        assert "href" not in svg and "<script" not in svg.lower(), path.name
    picker = (STATIC / "ui-language-picker.js").read_text(encoding="utf-8")
    assert not re.search("[\U0001F1E6-\U0001F1FF]", picker)                  # no regional-indicator emoji
    assert "flag.src = `/icons/flags/${region}.svg`;" in picker
    assert "flag.setAttribute('aria-hidden', 'true');" in picker


def test_the_flags_provenance_and_licence_are_recorded():
    inventory = (ROOT / "docs" / "DEPENDENCY_LICENSES.md").read_text(encoding="utf-8")
    assert "## Language flags" in inventory and "b3e3051a088047d19fd4d49b1c3ac42fb8c3aaf8" in inventory
    licence = (ROOT / "licenses" / "noto-region-flags-public-domain.txt").read_text(encoding="utf-8")
    assert "Public Domain" in licence
