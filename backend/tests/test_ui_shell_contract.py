"""Structural contract for the v1.0.11 application shell migration."""

from __future__ import annotations

from pathlib import Path
import json
import re
import xml.etree.ElementTree as ET


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "frontend" / "static"
INDEX = STATIC / "index.html"
STYLE_ENTRY = STATIC / "style.css"
LEGACY_STYLE = STATIC / "style-legacy.css"
V11_STYLE = STATIC / "style-v11.css"
DASHBOARD_STYLE = STATIC / "ui-dashboard.css"
STATISTICS_STYLE = STATIC / "ui-statistics-page.css"
SHELL_STYLE = STATIC / "ui-shell.css"
SHELL_RUNTIME = STATIC / "operator-title.js"
PRESENTATION_RUNTIME = STATIC / "ui-runtime.js"
PULSE = STATIC / "icons" / "dp" / "shell-pulse.svg"
MANIFEST = STATIC / "icons" / "dp" / "manifest.json"
DEPENDENCIES = REPO_ROOT / "docs" / "DEPENDENCY_LICENSES.md"
LUCIDE_LICENSE = REPO_ROOT / "licenses" / "Lucide-ISC-MIT.txt"


def test_v11_stylesheet_stack_preserves_legacy_contract_and_uses_universal_base() -> None:
    legacy_entry = STYLE_ENTRY.read_text(encoding="utf-8")
    preserved = LEGACY_STYLE.read_text(encoding="utf-8")
    overlay = V11_STYLE.read_text(encoding="utf-8")
    runtime = PRESENTATION_RUNTIME.read_text(encoding="utf-8")

    # Existing functional/inherited selectors continue to treat style.css as
    # the monolithic v1 compatibility stylesheet. v1.0.11 is a second visual
    # layer statically loaded after it, with a guarded runtime fallback.
    assert legacy_entry == preserved

    # The v1.0.11 cascade is architectural: semantic/design primitives first,
    # then the Dashboard-derived universal component defaults, shared audited
    # corrections, shell, reference-page calibration, and page exceptions.
    imports = [
        "/design-tokens.css?v=20",
        "/ui-language-tokens.css?v=21",
        "/ui-foundation.css?v=20",
        "/ui-components.css?v=20",
        "/icon-system.css?v=20",
        "/ui-universal-language.css?v=20",
        "/ui-shared-contract.css?v=31",
        "/ui-modal-contract.css?v=25",
        "/ui-shell.css?v=20",
        "/ui-shell-structural.css?v=26",
        "/ui-shell-provider-status.css?v=23",
        "/ui-shell-provider-status-v2.css?v=28",
        "/ui-dashboard.css?v=20",
        "/ui-dashboard-structural.css?v=20",
        "/ui-dashboard-control-polish.css?v=23",
        "/ui-dashboard-consistency.css?v=23",
        "/ui-statistics-page.css?v=21",
        "/ui-activity-log-page.css?v=29",
        "/ui-downloads-page.css?v=27",
        "/ui-downloads-desktop.css?v=28",
        "/ui-settings-page.css?v=2",
        "/ui-settings-chrome.css?v=1",
        "/ui-help-page.css?v=22",
        "/ui-feature-icon-contract.css?v=4",
        "/ui-panel-surface-treatment.css?v=22",
        "/ui-transfer-contract.css?v=31",
        "/ui-live-review-batch.css?v=21",
    ]
    positions = [overlay.index(value) for value in imports]
    assert positions == sorted(positions), "v1.0.11 stylesheet layering order drifted"

    # Targeted invalidation: only layers changed by reviewed work advance their
    # cache generations; established approved layers retain their generations.
    generations = re.findall(r"@import url\('([^']+)\?v=(\d+)'\);", overlay)
    assert generations
    version_by_path = {path: version for path, version in generations}
    expected_changed_versions = {
        "/ui-language-tokens.css": "21",
        "/ui-shared-contract.css": "31",
        "/ui-modal-contract.css": "25",
        "/ui-shell-structural.css": "26",
        "/ui-shell-provider-status.css": "23",
        "/ui-shell-provider-status-v2.css": "28",
        "/ui-dashboard-control-polish.css": "23",
        "/ui-dashboard-consistency.css": "23",
        "/ui-statistics-page.css": "21",
        "/ui-activity-log-page.css": "29",
        "/ui-downloads-page.css": "27",
        "/ui-downloads-desktop.css": "28",
        "/ui-settings-page.css": "2",
        "/ui-settings-chrome.css": "1",
        "/ui-help-page.css": "22",
        "/ui-feature-icon-contract.css": "4",
        "/ui-panel-surface-treatment.css": "22",
        "/ui-transfer-contract.css": "31",
        "/ui-live-review-batch.css": "21",
    }
    for path, version in expected_changed_versions.items():
        assert version_by_path[path] == version

    changed_paths = set(expected_changed_versions)
    unchanged_versions = {
        version for path, version in generations if path not in changed_paths
    }
    assert unchanged_versions == {"20"}

    # Old page-local material copies and the universal-last card guard must not
    # return to the active cascade.
    for retired_import in (
        "ui-card-shell-final.css",
        "ui-downloads-structural.css",
        "ui-downloads-polish.css",
        "ui-downloads-consistency.css",
        "ui-downloads-shell-sync.css",
    ):
        assert retired_import not in overlay

    assert "/style-v11.css?v=24" in runtime
    assert "data-dp-v11-styles" in runtime
    assert "dp-v11-structural" in runtime


def test_statistics_owns_geometry_not_dashboard_material() -> None:
    dashboard = DASHBOARD_STYLE.read_text(encoding="utf-8")
    statistics = STATISTICS_STYLE.read_text(encoding="utf-8")

    # Once the historical KPI strip moves to Statistics, Dashboard must no
    # longer style that page. The universal metric bridge owns card material.
    assert "#view-stats" not in dashboard
    assert ".dp-stats-history-grid" in statistics
    assert ".dash-kpi" in statistics
    assert "grid-template-columns" in statistics

    forbidden_material = (
        "background: linear-gradient(155deg, var(--dp-surface-2), var(--dp-surface-1))",
        "box-shadow: var(--dp-shadow-card)",
        "border: 1px solid var(--dp-border-default)",
    )
    present = [fragment for fragment in forbidden_material if fragment in statistics]
    assert not present, f"Statistics page reintroduced base material ownership: {present}"


def test_v11_bootstrap_cache_generation_is_coherent() -> None:
    index = INDEX.read_text(encoding="utf-8")
    operator = SHELL_RUNTIME.read_text(encoding="utf-8")
    runtime = PRESENTATION_RUNTIME.read_text(encoding="utf-8")

    assert '/style-v11.css?v=24' in index
    assert '/operator-title.js?v=23' in index
    assert '/ui-runtime.js?v=24' in index
    assert '/ui-runtime.js?v=24' in operator
    assert '/ui-downloads-runtime.js?v=22' in operator
    assert '/style-v11.css?v=24' in runtime


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


def test_temporary_or_retired_stylesheet_layers_are_not_shipped() -> None:
    junk = (
        "style-next.css",
        "style-v11-loader.css",
        "style-legacy-marker.txt",
        "STYLE_MIGRATION_NOTE.md",
        "DO_NOT_USE.txt",
        "ZZZ",
        "placeholder-cleanup-anchor",
        "ui-card-shell-final.css",
        "ui-downloads-structural.css",
        "ui-downloads-polish.css",
        "ui-downloads-consistency.css",
        "ui-downloads-shell-sync.css",
    )
    present = [name for name in junk if (STATIC / name).exists()]
    assert not present, f"temporary/retired migration files leaked into final tree: {present}"
