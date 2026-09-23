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
V11_STYLE = STATIC / "style.css"  # DP 1.0.12 canonical flattening: style-v11.css was folded into style.css.
SHELL_STYLE = STATIC / "ui-shell.css"
SHELL_STRUCTURAL = STATIC / "ui-shell-structural.css"
SHELL_PROVIDER = STATIC / "ui-shell-provider-status.css"
PROVIDER_STATE = STATIC / "ui-provider-state.css"
SHELL_RUNTIME = STATIC / "operator-title.js"
PULSE = STATIC / "icons" / "dp" / "shell-pulse.svg"
MANIFEST = STATIC / "icons" / "dp" / "manifest.json"
DEPENDENCIES = ROOT / "docs" / "DEPENDENCY_LICENSES.md"
LUCIDE_LICENSE = ROOT / "licenses" / "Lucide-ISC-MIT.txt"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_v11_cascade_uses_deliberate_final_ownership_order() -> None:
    assert STYLE_ENTRY.is_file()
    assert not (STATIC / "style-legacy.css").exists()
    assert not (STATIC / "style-v11.css").exists()
    index = read(INDEX)
    assert "/style.css?v=18" in index
    assert index.count('<link rel="stylesheet" href="/style.css?v=18">') == 1
    assert "style-v11.css" not in index

    overlay = read(V11_STYLE)
    imports = (
        "/design-tokens.css?v=21",
        "/ui-language-tokens.css?v=21",
        "/ui-foundation.css?v=21",
        "/ui-components.css?v=20",
        "/ui-universal-language.css?v=21",
        "/ui-shared-contract.css?v=33",
        "/ui-modal-contract.css?v=26",
        "/ui-shell.css?v=22",
        "/ui-shell-structural.css?v=31",
        "/ui-shell-provider-status.css?v=25",
        "/ui-dashboard.css?v=22",
        "/ui-utility-controls.css?v=25",
        "/ui-statistics-page.css?v=23",
        "/ui-activity-log-page.css?v=31",
        "/ui-downloads-page.css?v=30",
        "/ui-settings-page.css?v=3",
        "/ui-help-page.css?v=23",
        "/ui-panel-surface-treatment.css?v=23",
        "/ui-transfer-contract.css?v=33",
        "/ui-visual-accents.css?v=22",
        "/ui-shell-signal-field.css?v=21",
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
        "ui-dashboard-batch5.css",
        "ui-dashboard-polish.css",
        "ui-dashboard-polish-final.css",
        "ui-dashboard-final.css",
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
        "/ui-shared-contract.css": "33",
        "/ui-modal-contract.css": "26",
        "/ui-shell.css": "22",
        "/ui-shell-structural.css": "31",
        "/ui-shell-provider-status.css": "25",
        "/ui-utility-controls.css": "25",
        "/ui-statistics-page.css": "23",
        "/ui-activity-log-page.css": "31",
        "/ui-downloads-page.css": "30",
        "/ui-settings-page.css": "3",
        "/ui-settings-chrome.css": "4",
        "/ui-help-page.css": "23",
        "/ui-feature-icon-contract.css": "5",
        "/ui-panel-surface-treatment.css": "23",
        "/ui-transfer-contract.css": "33",
        "/ui-detail-candidates.css": "4",
        "/ui-visual-accents.css": "22",
    }
    for path, version in expected.items():
        assert generations[path] == version
    assert "/ui-shell-provider-status-v2.css" not in generations


def test_shell_owns_topbar_navigation_canvas_and_provider_support_geometry() -> None:
    shell = read(SHELL_STYLE)
    structural = read(SHELL_STRUCTURAL)
    provider = read(SHELL_PROVIDER)
    provider_state = read(PROVIDER_STATE)
    for fragment in (
        ".sidebar-theme-control",
        "#page-title::after",
        ".runtime-cap-options button:hover",
        "@media (max-width: 899px)",
    ):
        assert fragment in shell
    for fragment in (
        "margin-left: 0 !important;",
        ".sidebar-theme-control.topbar-theme-control",
        ".nav-item.active::after",
        "body.light #page-title",
        "radial-gradient(920px 540px at 36% 4%",
    ):
        assert fragment in structural
    for fragment in (
        "/icons/dp/crown.svg?v=11",
        ".conn-row:has(#dot-aria2)",
        ".conn-row:has(#dot-db)",
    ):
        assert fragment in provider
    for legacy in (
        "content: 'Provider Status';",
        "content: 'AllDebrid: Connected';",
        ".conn-row:has(#dot-api)",
    ):
        assert legacy not in provider
    # DP 1.0.12 canonical flattening (Gate 9 round 3 fix-forward): the old
    # ::before pseudo-title was always suppressed by a correction layer in
    # the (now-deleted) ui-provider-summary.css and never rendered; the real
    # heading is the explicit .dp-provider-status-heading element that the
    # shell markup (index.html) renders.
    assert ".dp-provider-status-list::before" not in provider_state
    for fragment in (
        ".dp-provider-status-heading",
        ".dp-provider-status-row",
        "justify-content: center;",
        "text-align: center;",
    ):
        assert fragment in provider_state


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


def test_provider_status_heading_and_list_are_static_shell_markup_the_owner_only_fills() -> None:
    index = (ROOT / "frontend" / "static" / "index.html").read_text(encoding="utf-8")
    owner = (ROOT / "frontend" / "static" / "ui-provider-status.js").read_text(encoding="utf-8")
    assert '<div class="dp-provider-status-heading">Provider Status</div>' in index
    assert 'id="provider-status-list"' in index
    for created in ("ensureHeading", "createElement", "insertBefore", "dp-provider-status-heading"):
        assert created not in owner, created
