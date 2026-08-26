"""Shared material contracts for the Activity Log event stream."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_activity_event_stream_uses_shared_operational_list_material() -> None:
    shared = read("ui-shared-contract.css")
    activity = read("ui-activity-log-page.css")

    required = (
        "#view-events > .card > .card-header + div",
        "#event-list",
        ".event-item",
        "background: var(--dp-surface-1) !important",
        "color-mix(in srgb, var(--dp-table-row-border) 78%, transparent)",
        "var(--dp-table-row-hover)",
        "var(--dp-text-primary)",
        "var(--dp-text-muted)",
    )
    missing = [fragment for fragment in required if fragment not in shared]
    assert not missing, f"shared Activity material bridge is missing: {missing}"

    # The toolbar/list terminate the outer card gradient. Only individual rows
    # may remain transparent above that solid theme-aware surface.
    assert shared.count("background: var(--dp-surface-1) !important;") == 2
    assert shared.count("background: transparent !important;") == 1
    assert "--dp-operational-surface" not in shared
    assert "--dp-operational-toolbar-surface" not in shared
    assert "radial-gradient(ellipse 92% 120% at 0% 0%" not in shared
    assert "color-mix(in srgb, var(--dp-accent-purple) 8%, transparent)" not in shared

    # Activity remains geometry/content composition only; reusable row/panel
    # material must not be copied back into the page layer.
    assert "var(--dp-table-row-border)" not in activity
    assert "var(--dp-table-row-hover)" not in activity
    assert "var(--dp-panel-surface)" not in activity
    assert "var(--dp-surface-1)" not in activity


def test_activity_surface_resolves_at_body_theme_scope() -> None:
    shared = read("ui-shared-contract.css")
    tokens = read("design-tokens.css")
    bootstrap = read("ui-theme-bootstrap.js")
    app = read("app.js")

    # The application theme is body-scoped. A derived alias declared on :root
    # would therefore compute against the dark root token before body.light can
    # supply the light value. Keep Activity on the direct inherited surface.
    assert "document.body.classList.add('light')" in bootstrap
    assert "document.body.classList.toggle('light')" in app
    light_scope = tokens.index("body.light,")
    light_surface = tokens.index("--dp-surface-1: #ffffff;", light_scope)
    assert light_surface > light_scope
    assert "background: var(--dp-surface-1) !important;" in shared
    assert "--dp-operational-surface" not in shared
    assert "--dp-operational-toolbar-surface" not in shared


def test_activity_material_bridge_is_loaded_before_page_geometry() -> None:
    overlay = read("style-v11.css")
    shared = overlay.index("/ui-shared-contract.css?v=31")
    activity = overlay.index("/ui-activity-log-page.css?v=26")
    assert shared < activity
