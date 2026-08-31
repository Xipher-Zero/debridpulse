"""Final-state shell ownership and bootstrap contracts."""

from __future__ import annotations

from pathlib import Path
import json
import re
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
INDEX = STATIC / "index.html"
STYLE_ENTRY = STATIC / "style.css"
LEGACY_STYLE = STATIC / "style-legacy.css"
V11_STYLE = STATIC / "style-v11.css"
SHELL_STYLE = STATIC / "ui-shell.css"
SHELL_STRUCTURAL = STATIC / "ui-shell-structural.css"
SHELL_PROVIDER = STATIC / "ui-shell-provider-status.css"
SHELL_RUNTIME = STATIC / "operator-title.js"
PRESENTATION_RUNTIME = STATIC / "ui-runtime.js"
PULSE = STATIC / "icons" / "dp" / "shell-pulse.svg"
MANIFEST = STATIC / "icons" / "dp" / "manifest.json"
DEPENDENCIES = ROOT / "docs" / "DEPENDENCY_LICENSES.md"
LUCIDE_LICENSE = ROOT / "licenses" / "Lucide-ISC-MIT.txt"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_v11_cascade_uses_deliberate_final_ownership_order() -> None:
    assert read(STYLE_ENTRY) == read(LEGACY_STYLE)
    overlay = read(V11_STYLE)
    imports = (
        "/design-tokens.css?v=20",
        "/ui-language-tokens.css?v=21",
        "/ui-foundation.css?v=20",
        "/ui-components.css?v=20",
        "/ui-universal-language.css?v=20",
        "/ui-shared-contract.css?v=31",
        "/ui-modal-contract.css?v=25",
        "/ui-shell.css?v=21",
        "/ui-shell-structural.css?v=30",
        "/ui-shell-provider-status.css?v=24",
        "/ui-dashboard.css?v=20",
        "/ui-dashboard-batch5.css?v=20",
        "/ui-dashboard-polish.css?v=20",
        "/ui-dashboard-polish-final.css?v=20",
        "/ui-utility-controls.css?v=23",
        "/ui-dashboard-final.css?v=23",
        "/ui-statistics-page.css?v=21",
        "/ui-activity-log-page.css?v=30",
        "/ui-downloads-page.css?v=27",
        "/ui-settings-page.css?v=2",
        "/ui-help-page.css?v=22",
        "/ui-panel-surface-treatment.css?v=22",
        "/ui-transfer-contract.css?v=31",
        "/ui-visual-accents.css?v=21",
        "/ui-shell-signal-field.css?v=20",
    )
    positions = [overlay.index(item) for item in imports]
    assert positions == sorted(positions)

    for retired in (
        "ui-card-shell-final.css",
        "ui-downloads-structural.css",
        "ui-downloads-polish.css",
        "ui-downloads-consistency.css",
        "ui-downloads-shell-sync.css",
        "ui-regression-fixes.css",
        "ui-dashboard-structural.css",
        "ui-dashboard-consistency.css",
        "ui-dashboard-batch1.css",
        "ui-dashboard-batch2.css",
        "ui-dashboard-batch2-final.css",
        "ui-dashboard-batch3.css",
        "ui-dashboard-batch4.css",
        "ui-dashboard-control-polish.css",
        "ui-live-review-batch.css",
        "ui-sidequest-polish.css",
    ):
        assert retired not in overlay
        assert not (STATIC / retired).exists()


def test_v11_cache_generations_remain_targeted() -> None:
    overlay = read(V11_STYLE)
    generations = dict(re.findall(r"@import url\('([^']+)\?v=(\d+)'\);", overlay))
    expected = {
        "/ui-language-tokens.css": "21",
        "/ui-shared-contract.css": "31",
        "/ui-modal-contract.css": "25",
        "/ui-shell.css": "21",
        "/ui-shell-structural.css": "30",
        "/ui-shell-provider-status.css": "24",
        "/ui-shell-provider-status-v2.css": "28",
        "/ui-utility-controls.css": "23",
        "/ui-dashboard-final.css": "23",
        "/ui-statistics-page.css": "21",
        "/ui-activity-log-page.css": "30",
        "/ui-downloads-page.css": "27",
        "/ui-downloads-desktop.css": "28",
        "/ui-settings-page.css": "2",
        "/ui-settings-chrome.css": "2",
        "/ui-help-page.css": "22",
        "/ui-feature-icon-contract.css": "4",
        "/ui-panel-surface-treatment.css": "22",
        "/ui-transfer-contract.css": "31",
        "/ui-visual-accents.css": "21",
    }
    for path, version in expected.items():
        assert generations[path] == version


def test_bootstrap_cache_generation_and_runtime_fallbacks_are_coherent() -> None:
    index = read(INDEX)
    operator = read(SHELL_RUNTIME)
    runtime = read(PRESENTATION_RUNTIME)
    assert "/style-v11.css?v=24" in index
    assert "/operator-title.js?v=23" in index
    assert "/ui-runtime.js?v=24" in index
    assert "/ui-runtime.js?v=24" in operator
    assert "/ui-downloads-runtime.js?v=22" in operator
    assert "/style-v11.css?v=24" in runtime


def test_shell_owns_topbar_navigation_canvas_and_provider_geometry() -> None:
    shell = read(SHELL_STYLE)
    structural = read(SHELL_STRUCTURAL)
    provider = read(SHELL_PROVIDER)
    for fragment in (
        ".sidebar-theme-control",
        "#page-title::after",
        "#aria2-speed-badge.external-control",
        ".aria2-cap-options button:hover",
        "@media (max-width: 899px)",
    ):
        assert fragment in shell
    for fragment in (
        "margin-left: 0 !important;",
        ".sidebar-theme-control.topbar-theme-control",
        "body.dp-v11-structural .nav-item.active::after",
        "body.light.dp-v11-structural #page-title",
        "radial-gradient(920px 540px at 36% 4%",
    ):
        assert fragment in structural
    for fragment in (
        "content: 'Provider Status';",
        "/icons/dp/crown.svg?v=11",
        "content: 'AllDebrid: Connected';",
        ".conn-row:has(#dot-aria2)",
        ".conn-row:has(#dot-db)",
    ):
        assert fragment in provider


def test_shell_uses_local_lucide_subset_and_bundled_license() -> None:
    js = read(SHELL_RUNTIME)
    for icon in (
        "dashboard", "download", "logs", "statistics", "settings", "help",
        "menu", "sun", "moon", "pause", "play", "chevronDown",
    ):
        assert f"{icon}:" in js
    assert "23f9abc4ed0146cffededd3d7f94c1018bfdf693" in js
    lowered = js.lower()
    assert "unpkg.com" not in lowered
    assert "jsdelivr" not in lowered
    assert "lucide.dev" not in lowered

    notice = read(LUCIDE_LICENSE)
    inventory = read(DEPENDENCIES)
    assert "ISC License" in notice
    assert "Lucide Icons and Contributors" in notice
    assert "Lucide Icons UI subset" in inventory
    assert "Lucide-ISC-MIT.txt" in inventory


def test_shell_pulse_is_registered_true_vector_art() -> None:
    raw = read(PULSE)
    root = ET.fromstring(raw)
    manifest = json.loads(read(MANIFEST))
    assert root.tag.endswith("svg")
    assert root.attrib.get("viewBox")
    assert "<path" in raw
    assert "<image" not in raw.lower()
    assert "data:image" not in raw.lower()
    assert manifest["icons"]["shellPulse"] == "shell-pulse.svg"
