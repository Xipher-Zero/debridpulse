"""Structural contract for DebridPulse custom UI SVG assets."""

from __future__ import annotations

import json
import re
from pathlib import Path
import xml.etree.ElementTree as ET


REPO_ROOT = Path(__file__).resolve().parents[2]
ICON_DIR = REPO_ROOT / "frontend" / "static" / "icons" / "dp"
MANIFEST = ICON_DIR / "manifest.json"
SVG_NS = "{http://www.w3.org/2000/svg}"
FORBIDDEN_TAGS = {"image", "foreignObject", "script", "iframe", "object", "embed"}
MAX_VECTOR_ASSET_BYTES = 256 * 1024


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def test_custom_icon_manifest_is_complete_and_resolves() -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert data["customRendering"] == "external-img"
    assert data["utilityLibrary"] == "Lucide"

    icon_files = list(data["icons"].values())
    assert len(icon_files) == len(set(icon_files)), "manifest contains duplicate SVG targets"

    missing = [name for name in icon_files if not (ICON_DIR / name).is_file()]
    assert not missing, f"manifest references missing SVG assets: {missing}"

    unlisted = sorted(
        path.name
        for path in ICON_DIR.glob("*.svg")
        if path.name not in set(icon_files)
    )
    assert not unlisted, f"custom SVG assets are not represented in manifest.json: {unlisted}"


def test_custom_svgs_are_true_vector_assets() -> None:
    failures: list[str] = []

    for path in sorted(ICON_DIR.glob("*.svg")):
        raw = path.read_text(encoding="utf-8")

        if path.stat().st_size > MAX_VECTOR_ASSET_BYTES:
            failures.append(f"{path.name}: unexpectedly large ({path.stat().st_size} bytes)")

        if "data:image/" in raw.lower():
            failures.append(f"{path.name}: contains embedded raster data:image payload")

        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            failures.append(f"{path.name}: invalid XML ({exc})")
            continue

        if _local_name(root.tag) != "svg":
            failures.append(f"{path.name}: root element is not <svg>")

        view_box = root.attrib.get("viewBox")
        if not view_box or len(view_box.replace(",", " ").split()) != 4:
            failures.append(f"{path.name}: missing or invalid viewBox")

        for element in root.iter():
            tag = _local_name(element.tag)
            if tag in FORBIDDEN_TAGS:
                failures.append(f"{path.name}: forbidden <{tag}> element")

            for attr_name, value in element.attrib.items():
                local_attr = _local_name(attr_name)
                if local_attr.lower().startswith("on"):
                    failures.append(f"{path.name}: event-handler attribute {local_attr}")
                if local_attr in {"href", "src"} and value and not value.startswith("#"):
                    failures.append(f"{path.name}: external/embedded resource reference in {local_attr}")

        for match in re.findall(r"url\(([^)]+)\)", raw, flags=re.IGNORECASE):
            target = match.strip().strip("\"'")
            if target and not target.startswith("#"):
                failures.append(f"{path.name}: non-local url() resource {target!r}")

    assert not failures, "Custom icon SVG contract failures:\n- " + "\n- ".join(failures)
