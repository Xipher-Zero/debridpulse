"""Structural contract for the v1.0.11 application shell migration."""

from __future__ import annotations

from pathlib import Path
import json
import xml.etree.ElementTree as ET


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "frontend" / "static"
STYLE_ENTRY = STATIC / "style.css"
LEGACY_STYLE = STATIC / "style-legacy.css"
V11_STYLE = STATIC / "style-v11.css"
SHELL_STYLE = STATIC / "ui-shell.css"
SHELL_RUNTIME = STATIC / "operator-title.js"
PRESENTATION_RUNTIME = STATIC / "ui-runtime.js"
PULSE = STATIC / "icons" / "dp" / "shell-pulse.svg"
MANIFEST = STATIC / "icons" / "dp" / "manifest.json"
DEPENDENCIES = REPO_ROOT / "docs" / "DEPENDENCY_LICENSES.md"
LUCIDE_LICENSE = REPO_ROOT / "licenses" / "Lucide-ISC-MIT.txt"


def test_v11_stylesheet_stack_preserves_legacy_contract_and_layers_after_it() -> None:
    legacy_entry = STYLE_ENTRY.read_text(encoding="utf-8")
    preserved = LEGACY_STYLE.read_text(encoding="utf-8")
    overlay = V11_STYLE.read_text(encoding="utf-8")
    runtime = PRESENTATION_RUNTIME.read_text(encoding="utf-8")

    # Existing contract tests and inherited selectors continue to treat
    # style.css as the monolithic v1 stylesheet. The v1.0.11 visual layer is a
    # second stylesheet loaded after it by the presentation runtime.
    assert legacy_entry == preserved

    imports = [
        "/design-tokens.css?v=11",
        "/ui-foundation.css?v=11",
        "/ui-components.css?v=11",
        "/icon-system.css?v=11",
        "/ui-shell.css?v=11",
        "/ui-dashboard.css?v=11",
    ]
    positions = [overlay.index(value) for value in imports]
    assert positions == sorted(positions), "v1.0.11 stylesheet layering order drifted"
    assert "/style-v11.css?v=11" in runtime
    assert "data-dp-v11-styles" in runtime


def test_shell_matches_required_mockup_structure() -> None:
    css = SHELL_STYLE.read_text(encoding="utf-8")

    required_fragments = (
        ".sidebar-theme-control",
        "position: fixed",
        "right: 20px",
        "#page-title::after",
        "/icons/dp/shell-pulse.svg",
        "#aria2-speed-badge.external-control",
        "var(--dp-state-connectivity)",
        "@media (max-width: 1439px)",
        "@media (max-width: 1179px)",
        "@media (max-width: 899px)",
        "@media (max-width: 699px)",
    )

    missing = [fragment for fragment in required_fragments if fragment not in css]
    assert not missing, f"shell contract is missing: {missing}"


def test_shell_uses_local_lucide_subset_without_runtime_cdn() -> None:
    js = SHELL_RUNTIME.read_text(encoding="utf-8")

    for icon in (
        "dashboard",
        "download",
        "logs",
        "statistics",
        "settings",
        "help",
        "menu",
        "sun",
        "moon",
        "pause",
        "play",
        "chevronDown",
    ):
        assert f"{icon}:" in js

    assert "23f9abc4ed0146cffededd3d7f94c1018bfdf693" in js
    lowered = js.lower()
    assert "unpkg.com" not in lowered
    assert "jsdelivr" not in lowered
    assert "lucide.dev" not in lowered


def test_lucide_license_and_inventory_are_bundled() -> None:
    notice = LUCIDE_LICENSE.read_text(encoding="utf-8")
    inventory = DEPENDENCIES.read_text(encoding="utf-8")

    assert "ISC License" in notice
    assert "Lucide Icons and Contributors" in notice
    assert "The MIT License (MIT)" in notice
    assert "Lucide Icons UI subset" in inventory
    assert "Lucide-ISC-MIT.txt" in inventory
    assert "23f9abc4ed0146cffededd3d7f94c1018bfdf693" in inventory


def test_shell_pulse_is_registered_true_vector_art() -> None:
    raw = PULSE.read_text(encoding="utf-8")
    root = ET.fromstring(raw)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert root.tag.endswith("svg")
    assert root.attrib.get("viewBox")
    assert "<path" in raw
    assert "<image" not in raw.lower()
    assert "data:image" not in raw.lower()
    assert manifest["icons"]["shellPulse"] == "shell-pulse.svg"


def test_temporary_stylesheet_staging_files_are_not_shipped() -> None:
    junk = (
        "style-next.css",
        "style-v11-loader.css",
        "style-legacy-marker.txt",
        "STYLE_MIGRATION_NOTE.md",
        "DO_NOT_USE.txt",
        "ZZZ",
        "placeholder-cleanup-anchor",
    )
    present = [name for name in junk if (STATIC / name).exists()]
    assert not present, f"temporary migration files leaked into final tree: {present}"
