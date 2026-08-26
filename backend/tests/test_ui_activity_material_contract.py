"""Activity Log material-ownership regression contracts."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_activity_composition_uses_shared_tall_panel_material() -> None:
    shared = read("ui-shared-contract.css")
    activity = read("ui-activity-log-page.css")
    tokens = read("ui-language-tokens.css")
    universal = read("ui-universal-language.css")
    downloads = read("ui-downloads-page.css")

    required = (
        "#view-events > .card > .card-header + div",
        "background: var(--dp-table-head-surface);",
        "border-bottom: 1px solid var(--dp-table-head-border);",
        "#view-events #event-list",
        "background: transparent;",
        "#event-list > .event-item",
        "color-mix(in srgb, var(--dp-table-row-border) 78%, transparent)",
        "var(--dp-table-row-hover)",
        "var(--dp-text-primary)",
        "var(--dp-text-muted)",
        "@media (min-width: 901px)",
        "#view-events.active > .card",
        "background: var(--dp-panel-surface-tall);",
    )
    missing = [fragment for fragment in required if fragment not in activity]
    assert not missing, f"Activity composition contract is missing: {missing}"

    # The structural band is not an Activity invention. It is the same shared
    # internal table/header material already calibrated for both themes.
    assert "--dp-table-head-surface:" in tokens
    assert "linear-gradient(180deg, #1d1930 0%, #171528 100%)" in tokens
    assert "linear-gradient(180deg, #f2eff8 0%, #ebe8f3 100%)" in tokens

    # Normal cards keep the same shared semantic gradient. Tall workspaces use
    # the same start/end primitives, reach the exact normal endpoint at 320px,
    # and then hold that exact endpoint instead of stretching it to viewport
    # height. Downloads must remain on the normal card material.
    assert "background: var(--dp-panel-surface);" in universal
    for token in (
        "--dp-panel-start:",
        "--dp-panel-end:",
        "--dp-panel-surface:",
        "--dp-panel-surface-tall:",
    ):
        assert token in tokens
    assert "var(--dp-panel-end) 320px" in tokens
    assert "var(--dp-panel-end) 100%" in tokens
    assert tokens.count("--dp-panel-surface-tall:") == 2
    assert "--dp-panel-surface-tall" not in downloads
    assert "background: transparent;" in downloads

    # Light endpoints and both derived surfaces are redeclared in the light
    # scope. This guards the exact root-derived alias bug that previously made a
    # light Activity page consume the dark panel surface.
    light = tokens.split(".theme-light {", 1)[1]
    assert "--dp-panel-start: #ffffff;" in light
    assert "--dp-panel-end: #f8f9fd;" in light
    assert "--dp-panel-surface: linear-gradient(180deg, var(--dp-panel-start), var(--dp-panel-end));" in light
    assert "--dp-panel-surface-tall: linear-gradient(180deg," in light
    assert "var(--dp-panel-end) 320px" in light
    assert "var(--dp-panel-end) 100%" in light

    # The old cross-cutting Activity bridge was the ownership error. Shared CSS
    # must no longer target Activity IDs or the globally reused .event-item.
    for forbidden in (
        "#view-events",
        "#event-list",
        ".event-item",
        ".dp-card-toolbar",
        ".dp-operational-list",
        ".dp-operational-row",
    ):
        assert forbidden not in shared

    # Do not reintroduce any disproven page-local paint strategies. Activity may
    # select a shared material token, but it may not define/rescale gradients.
    for forbidden in (
        "background-size:",
        "background-repeat:",
        "background-color: var(--dp-bg-app-alt);",
        "background-color: var(--dp-surface-2);",
        "--dp-operational-surface",
        "--dp-operational-toolbar-surface",
        "background: var(--dp-surface-1)",
        "linear-gradient(",
        "radial-gradient",
    ):
        assert forbidden not in activity


def test_activity_row_material_is_scoped_away_from_details_events() -> None:
    shared = read("ui-shared-contract.css")
    activity = read("ui-activity-log-page.css")
    app = read("app.js")

    # app.js deliberately uses .event-item in both Activity and transfer Details.
    assert app.count('class="event-item"') >= 2

    # Activity may normalize only its own event stream. A global .event-item
    # bridge would silently alter Details-modal rows as well.
    assert "#event-list > .event-item" in activity
    assert ".event-item" not in shared


def test_activity_material_composition_is_owned_after_shared_defaults() -> None:
    overlay = read("style-v11.css")
    tokens = overlay.index("/ui-language-tokens.css?v=21")
    shared = overlay.index("/ui-shared-contract.css?v=31")
    activity = overlay.index("/ui-activity-log-page.css?v=27")
    assert tokens < shared < activity
