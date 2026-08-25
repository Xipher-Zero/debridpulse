"""Contract tests for v1.0.11 typography and geometry tokens."""

from __future__ import annotations

from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[2]
FOUNDATION = REPO_ROOT / "frontend" / "static" / "ui-foundation.css"


REQUIRED_TYPOGRAPHY_TOKENS = {
    "--dp-font-sans",
    "--dp-font-mono",
    "--dp-type-page-title-size",
    "--dp-type-page-subtitle-size",
    "--dp-type-section-title-size",
    "--dp-type-card-title-size",
    "--dp-type-body-size",
    "--dp-type-control-size",
    "--dp-type-nav-size",
    "--dp-type-nav-group-size",
    "--dp-type-table-head-size",
    "--dp-type-table-body-size",
    "--dp-type-metric-value-size",
    "--dp-type-metric-label-size",
    "--dp-type-kpi-value-size",
    "--dp-type-mono-size",
}

REQUIRED_GEOMETRY_TOKENS = {
    "--dp-sidebar-width",
    "--dp-sidebar-collapsed-width",
    "--dp-sidebar-brand-height",
    "--dp-topbar-height",
    "--dp-page-gutter-x",
    "--dp-page-gutter-y",
    "--dp-content-max-width",
    "--dp-section-gap",
    "--dp-card-gap",
    "--dp-card-padding",
    "--dp-radius-sm",
    "--dp-radius-lg",
    "--dp-control-height-md",
    "--dp-input-height",
    "--dp-tab-height",
    "--dp-table-head-height",
    "--dp-table-row-height",
    "--dp-metric-card-min-height",
    "--dp-progress-height",
}

LEGACY_ALIASES = {
    "--font",
    "--mono",
    "--radius",
    "--radius-sm",
    "--sidebar",
    "--chrome-header-height",
}


def _declared_tokens(css: str) -> set[str]:
    return set(re.findall(r"(--[a-zA-Z0-9_-]+)\s*:", css))


def test_foundation_contains_typography_and_geometry_contract() -> None:
    css = FOUNDATION.read_text(encoding="utf-8")
    declared = _declared_tokens(css)

    missing_type = sorted(REQUIRED_TYPOGRAPHY_TOKENS - declared)
    missing_geometry = sorted(REQUIRED_GEOMETRY_TOKENS - declared)

    assert not missing_type, f"missing typography tokens: {missing_type}"
    assert not missing_geometry, f"missing geometry tokens: {missing_geometry}"


def test_foundation_keeps_expected_font_families_and_numeric_scanning() -> None:
    css = FOUNDATION.read_text(encoding="utf-8")
    assert "'Outfit'" in css
    assert "'JetBrains Mono'" in css
    assert "font-variant-numeric: tabular-nums" in css


def test_geometry_matches_mockup_baseline_contract() -> None:
    css = FOUNDATION.read_text(encoding="utf-8")
    expected = {
        "--dp-sidebar-width": "252px",
        "--dp-topbar-height": "64px",
        "--dp-page-gutter-x": "28px",
        "--dp-page-gutter-y": "24px",
        "--dp-card-gap": "14px",
        "--dp-card-padding": "18px",
        "--dp-radius-lg": "12px",
        "--dp-radius-sm": "8px",
        "--dp-control-height-md": "36px",
        "--dp-input-height": "42px",
        "--dp-table-row-height": "52px",
    }

    for token, value in expected.items():
        pattern = rf"{re.escape(token)}\s*:\s*{re.escape(value)}\s*;"
        assert re.search(pattern, css), f"{token} drifted from the initial mockup baseline {value}"


def test_legacy_foundation_aliases_are_available_during_migration() -> None:
    css = FOUNDATION.read_text(encoding="utf-8")
    declared = _declared_tokens(css)
    missing = sorted(LEGACY_ALIASES - declared)
    assert not missing, f"legacy foundation aliases disappeared prematurely: {missing}"


def test_typography_role_helpers_exist() -> None:
    css = FOUNDATION.read_text(encoding="utf-8")
    for selector in (
        ".dp-type-page-title",
        ".dp-type-page-subtitle",
        ".dp-type-section-title",
        ".dp-type-card-title",
        ".dp-type-metric-value",
        ".dp-type-metric-label",
        ".dp-type-mono",
        ".dp-truncate",
    ):
        assert selector in css, f"missing canonical helper {selector}"
