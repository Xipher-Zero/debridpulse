"""Final-state frontend architecture contracts for the v1.0.11 UI branch.

These tests intentionally describe accepted behavior and ownership boundaries rather
than the migration batches that happened to produce the accepted baseline.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "frontend" / "static"
INDEX = STATIC / "index.html"
STYLE = STATIC / "style-v11.css"
THEME_BOOTSTRAP = STATIC / "ui-theme-bootstrap.js"
PRESENTATION_LOADER = STATIC / "ui-presentation-loader.js"
SETTINGS_RUNTIME = STATIC / "ui-settings-page.js"
A11Y_RUNTIME = STATIC / "ui-accessibility-runtime.js"
ERROR_RUNTIME = STATIC / "ui-error-semantics.js"
SHARED = STATIC / "ui-shared-contract.css"
OPERATOR_RUNTIME = STATIC / "operator-title.js"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"
MAIN = REPO_ROOT / "backend" / "main.py"
VERSION = REPO_ROOT / "VERSION"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def normalized_asset(path: str) -> str:
    return urlsplit(path).path


def direct_script_assets() -> list[str]:
    html = read(INDEX)
    return re.findall(r'<script[^>]+src=["\']([^"\']+\.js(?:\?[^"\']*)?)["\']', html)


def bootstrap_script_assets() -> list[str]:
    bootstrap = read(THEME_BOOTSTRAP)
    return re.findall(r'["\'](/[^"\']+\.js(?:\?[^"\']*)?)["\']', bootstrap)


def loader_assets(kind: str) -> list[str]:
    if not PRESENTATION_LOADER.exists():
        return []
    loader = read(PRESENTATION_LOADER)
    suffix = r"\.js" if kind == "js" else r"\.css"
    return re.findall(
        rf'(?:src|href):\s*["\']([^"\']+{suffix}(?:\?[^"\']*)?)["\']',
        loader,
    )


def overlay_style_assets() -> list[str]:
    if not STYLE.exists():
        return []
    return re.findall(
        r"@import\s+url\(['\"]([^'\"]+\.css(?:\?[^'\"]*)?)['\"]\)",
        read(STYLE),
    )


def direct_style_assets() -> list[str]:
    html = read(INDEX)
    return re.findall(r'<link[^>]+href=["\']([^"\']+\.css(?:\?[^"\']*)?)["\']', html)


def first_party_js() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(STATIC.glob("*.js"))
    )


def test_normal_ui_bootstrap_keeps_core_first_paint_static_and_bounded() -> None:
    html = read(INDEX)
    bootstrap = read(THEME_BOOTSTRAP)

    assert '<body class="dp-v11-structural">' in html
    assert "/style.css" in html
    assert "/style-v11.css" in html
    assert "/ui-theme-bootstrap.js" in html
    assert "localStorage.getItem('theme')" in bootstrap
    assert "document.body.classList.add('light')" in bootstrap

    for forbidden in ("MutationObserver", "fetch(", "/api/", "XMLHttpRequest", "EventSource"):
        assert forbidden not in bootstrap


def test_loaded_first_party_assets_are_unique_across_the_effective_boot_graph() -> None:
    js_assets = direct_script_assets() + bootstrap_script_assets() + loader_assets("js")
    css_assets = direct_style_assets() + overlay_style_assets() + loader_assets("css")

    normalized_js = [normalized_asset(path) for path in js_assets]
    normalized_css = [normalized_asset(path) for path in css_assets]

    assert len(normalized_js) == len(set(normalized_js)), (
        "A first-party browser runtime is loaded more than once: "
        f"{normalized_js}"
    )
    assert len(normalized_css) == len(set(normalized_css)), (
        "A first-party stylesheet is loaded more than once: "
        f"{normalized_css}"
    )


def test_settings_authoritative_renderer_is_clean_room_and_direct() -> None:
    source = read(SETTINGS_RUNTIME)

    assert "clean-room Settings page" in source
    assert "window.DPSettingsPage = Object.freeze({load});" in source
    assert "window.loadSettings = load;" in source
    assert "view.innerHTML =" in source
    assert "Promise.all([" in source
    assert "request('GET', '/settings'" in source
    assert "request('GET', '/auth/config'" in source

    for forbidden in (
        "window.renderSettings =",
        "window.getFormSettings =",
        "window.switchSettingsTab =",
        "settingsObserver",
        "observeSettingsForm",
        "scheduleApply",
        "new MutationObserver",
        "setTimeout(boot",
        "dp-settings-preserved",
    ):
        assert forbidden not in source


def test_accepted_authentication_structure_and_copy_exist_independent_of_layering() -> None:
    js = first_party_js()

    required = (
        "Configure local credentials for browser sign-in and HTTP Basic API access.",
        "Username used for browser and HTTP Basic authentication.",
        "Leave blank to keep the current password. Enter a new password to replace it.",
        "auth_password_enabled",
        "auth_username",
        "clear-password",
        "OpenID Connect",
        "API Access",
    )
    missing = [fragment for fragment in required if fragment not in js]
    assert not missing, f"Accepted Authentication contract is missing: {missing}"


def test_cross_cutting_accessibility_runtime_remains_semantic_and_io_free() -> None:
    source = read(A11Y_RUNTIME)

    required = (
        "aria-current",
        "aria-pressed",
        "role', 'group'",
        "role', 'tablist'",
        "role', 'tab'",
        "ArrowRight",
        "ArrowLeft",
        "Home",
        "End",
        "View downloads with errors",
        "Close details",
    )
    missing = [fragment for fragment in required if fragment not in source]
    assert not missing, f"Accessibility semantics are missing: {missing}"

    for forbidden in ("fetch(", "/api/", "XMLHttpRequest", "EventSource"):
        assert forbidden not in source


def test_error_semantics_startup_is_bounded_and_not_busy_polled() -> None:
    source = read(ERROR_RUNTIME)
    assert "function startAfterCore()" in source
    assert "core render helpers unavailable" in source
    assert "setTimeout(startWhenReady" not in source
    assert "window.setTimeout(startWhenReady" not in source


def test_shared_visual_contract_is_css_owned_not_runtime_injected() -> None:
    css = read(SHARED)
    operator = read(OPERATOR_RUNTIME)

    assert ".badge-duplicate" in css
    assert "var(--dp-state-caution-bg)" in css
    assert ":focus-visible" in css
    assert ".dp-pager-btn" in css

    assert "installDuplicateStatusStyle" not in operator
    assert "document.createElement('style')" not in operator
    assert "debridpulse-duplicate-status-style" not in operator


def test_static_frontend_resources_are_forced_to_revalidate() -> None:
    main = read(MAIN)
    assert 'path.endswith((".html", ".js", ".css"))' in main
    assert '"no-store" not in existing_cache.lower()' in main
    assert 'response.headers["Cache-Control"] = "no-cache, must-revalidate"' in main


def test_ci_syntax_checks_every_runtime_in_the_effective_load_graph() -> None:
    workflow = read(WORKFLOW)
    loaded = {
        normalized_asset(path).removeprefix("/")
        for path in direct_script_assets() + bootstrap_script_assets() + loader_assets("js")
        if normalized_asset(path).endswith(".js")
        and not normalized_asset(path).removeprefix("/").startswith("vendor/")
    }

    missing = [
        path
        for path in sorted(loaded)
        if f"node --check frontend/static/{path}" not in workflow
    ]
    assert not missing, f"Loaded first-party runtimes missing node --check coverage: {missing}"


def test_ui_track_does_not_advance_backend_version() -> None:
    assert read(VERSION).strip() == "1.0.10"
