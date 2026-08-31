"""Contract tests for the v1.0.11 DebridPulse design token system."""

from __future__ import annotations

from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[2]
TOKENS = REPO_ROOT / "frontend" / "static" / "design-tokens.css"
ICON_CSS = REPO_ROOT / "frontend" / "static" / "icon-system.css"

REQUIRED_TOKENS = {
    "--dp-bg-app",
    "--dp-bg-sidebar",
    "--dp-surface-1",
    "--dp-surface-2",
    "--dp-surface-input",
    "--dp-text-primary",
    "--dp-text-secondary",
    "--dp-text-muted",
    "--dp-border-subtle",
    "--dp-border-default",
    "--dp-border-strong",
    "--dp-accent-purple",
    "--dp-accent-purple-bright",
    "--dp-accent-blue",
    "--dp-accent-cyan",
    "--dp-state-success",
    "--dp-state-active",
    "--dp-state-processing",
    "--dp-state-caution",
    "--dp-state-error",
    "--dp-state-connectivity",
    "--dp-state-paused",
    "--dp-state-ready",
    "--dp-gradient-primary",
    "--dp-focus-ring",
    "--dp-shadow-card",
    "--dp-progress-active",
    "--dp-progress-complete",
    "--dp-progress-paused",
    "--dp-progress-processing",
    "--dp-progress-error",
}

LEGACY_ALIASES = {
    "--bg",
    "--bg2",
    "--surface",
    "--surface2",
    "--surface3",
    "--border",
    "--border2",
    "--accent",
    "--accent2",
    "--text",
    "--text2",
    "--text3",
    "--blue",
    "--green",
    "--yellow",
    "--red",
    "--purple",
}


def _declared_tokens(css: str) -> set[str]:
    return set(re.findall(r"(--[a-zA-Z0-9_-]+)\s*:", css))


def test_design_token_file_contains_required_dark_and_light_contract() -> None:
    css = TOKENS.read_text(encoding="utf-8")
    declared = _declared_tokens(css)

    missing = sorted(REQUIRED_TOKENS - declared)
    assert not missing, f"design token contract is missing: {missing}"

    assert ":root" in css
    assert "body.light" in css
    assert '[data-theme="light"]' in css
    assert "color-scheme: dark" in css
    assert "color-scheme: light" in css


def test_migration_aliases_are_preserved_until_legacy_css_is_retired() -> None:
    css = TOKENS.read_text(encoding="utf-8")
    declared = _declared_tokens(css)
    missing = sorted(LEGACY_ALIASES - declared)
    assert not missing, f"legacy migration aliases disappeared prematurely: {missing}"


def test_error_and_caution_remain_distinct_semantic_tokens() -> None:
    css = TOKENS.read_text(encoding="utf-8")
    assert "--dp-state-caution:" in css
    assert "--dp-state-error:" in css
    assert "--yellow: var(--dp-state-caution)" in css
    assert "--red: var(--dp-state-error)" in css


def test_icon_frames_use_shared_semantic_tokens() -> None:
    css = ICON_CSS.read_text(encoding="utf-8")
    expected = {
        "--dp-accent-purple-bright",
        "--dp-state-success",
        "--dp-state-active",
        "--dp-state-connectivity",
        "--dp-state-caution",
        "--dp-state-error",
    }
    missing = sorted(token for token in expected if token not in css)
    assert not missing, f"icon system is not using shared design tokens: {missing}"

    # Avoid drifting back to six independently hard-coded frame palettes.
    frame_rules = re.findall(r"\.dp-icon-frame--(?:purple|green|blue|cyan|amber|red)\s*\{([^}]+)\}", css)
    assert len(frame_rules) == 6
    assert all("var(--dp-" in rule for rule in frame_rules)
