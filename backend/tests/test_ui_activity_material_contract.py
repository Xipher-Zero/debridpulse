"""Activity Log material-ownership regression contracts."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_activity_composition_uses_existing_shared_material_tokens() -> None:
    shared = read("ui-shared-contract.css")
    activity = read("ui-activity-log-page.css")
    tokens = read("ui-language-tokens.css")

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
    )
    missing = [fragment for fragment in required if fragment not in activity]
    assert not missing, f"Activity composition contract is missing: {missing}"

    # The structural band is not an Activity invention. It is the same shared
    # internal table/header material already calibrated for both themes.
    assert "--dp-table-head-surface:" in tokens
    assert "linear-gradient(180deg, #1d1930 0%, #171528 100%)" in tokens
    assert "linear-gradient(180deg, #f2eff8 0%, #ebe8f3 100%)" in tokens

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

    # Do not reintroduce any of the failed surface strategies.
    assert "--dp-operational-surface" not in activity
    assert "--dp-operational-toolbar-surface" not in activity
    assert "background: var(--dp-surface-1)" not in activity
    assert "radial-gradient" not in activity


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
    shared = overlay.index("/ui-shared-contract.css?v=31")
    activity = overlay.index("/ui-activity-log-page.css?v=26")
    assert shared < activity
