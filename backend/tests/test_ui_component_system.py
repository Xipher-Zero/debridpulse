"""Contract tests for v1.0.11 reusable UI component primitives."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPONENTS = REPO_ROOT / "frontend" / "static" / "ui-components.css"


REQUIRED_SELECTORS = {
    ".dp-btn",
    ".dp-btn--primary",
    ".dp-btn--secondary",
    ".dp-btn--ghost",
    ".dp-btn--danger",
    ".dp-btn--caution",
    ".dp-icon-btn",
    ".dp-field",
    ".dp-select",
    ".dp-toggle",
    ".dp-tabs",
    ".dp-tab",
    ".dp-badge",
    ".dp-status",
    ".dp-status--done",
    ".dp-status--active",
    ".dp-status--processing",
    ".dp-status--caution",
    ".dp-status--error",
    ".dp-status--paused",
    ".dp-status--ready",
    ".dp-progress",
    ".dp-card",
    ".dp-table-wrap",
    ".dp-table",
    ".dp-pagination",
    ".dp-tooltip",
    ".dp-dialog",
    ".dp-drawer",
    ".dp-toast",
    ".dp-empty",
    ".dp-skeleton",
}

REQUIRED_SHARED_TOKENS = {
    "--dp-gradient-primary",
    "--dp-focus-ring",
    "--dp-state-success",
    "--dp-state-active",
    "--dp-state-processing",
    "--dp-state-caution",
    "--dp-state-error",
    "--dp-state-paused",
    "--dp-state-ready",
    "--dp-state-connectivity",
    "--dp-input-height",
    "--dp-control-height-md",
    "--dp-tab-height",
    "--dp-table-row-height",
    "--dp-progress-height",
    "--dp-radius-sm",
    "--dp-radius-lg",
}


def test_component_stylesheet_contains_required_primitives() -> None:
    css = COMPONENTS.read_text(encoding="utf-8")
    missing = sorted(selector for selector in REQUIRED_SELECTORS if selector not in css)
    assert not missing, f"missing v1.0.11 component primitives: {missing}"


def test_components_consume_shared_design_and_geometry_tokens() -> None:
    css = COMPONENTS.read_text(encoding="utf-8")
    missing = sorted(token for token in REQUIRED_SHARED_TOKENS if token not in css)
    assert not missing, f"component system bypassed shared UI tokens: {missing}"


def test_error_and_caution_components_are_separate() -> None:
    css = COMPONENTS.read_text(encoding="utf-8")
    assert ".dp-btn--danger" in css
    assert ".dp-btn--caution" in css
    assert ".dp-status--caution" in css
    assert ".dp-status--error" in css
    assert "--dp-state-caution" in css
    assert "--dp-state-error" in css


def test_accessibility_and_reduced_motion_contract_is_present() -> None:
    css = COMPONENTS.read_text(encoding="utf-8")
    assert ":focus-visible" in css
    assert "prefers-reduced-motion: reduce" in css
    assert "aria-selected" in css
    assert "aria-checked" in css
    assert "aria-current" in css
    assert "aria-invalid" in css


def test_component_system_remains_namespaced_during_migration() -> None:
    css = COMPONENTS.read_text(encoding="utf-8")
    # The new component layer should not globally seize old v1.0.10 classes yet.
    forbidden = (
        "\n.btn {",
        "\n.card {",
        "\n.input {",
        "\n.ftab {",
        "\n.stab {",
    )
    for selector in forbidden:
        assert selector not in css, f"new component stylesheet globally overrides legacy selector {selector.strip()}"
