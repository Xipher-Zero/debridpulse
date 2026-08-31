"""Activity Log rebuild material-ownership regression contracts."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_activity_rebuild_consumes_universal_card_material_without_override() -> None:
    activity = read("ui-activity-log-page.css")
    universal = read("ui-universal-language.css")

    assert "background: var(--dp-panel-surface);" in universal
    assert ".dp-activity-card" in activity
    assert "background: var(--dp-panel-surface-tall)" not in activity
    assert "background: var(--dp-panel-surface)" not in activity
    assert "--dp-panel-surface:" not in activity
    assert "--dp-panel-surface-tall:" not in activity
    assert "--dp-surface-1" not in activity
    assert "background-size" not in activity
    assert "background-repeat" not in activity


def test_activity_body_is_transparent_over_shared_card_and_rows_are_page_specific() -> None:
    css = read("ui-activity-log-page.css")

    assert ".dp-activity-list" in css
    assert ".dp-activity-row" in css
    assert css.count("background: transparent;") >= 2
    assert "border-bottom: 1px solid var(--dp-table-row-border);" in css
    assert "background: var(--dp-table-row-hover);" in css
    assert "#event-list > .event-item" not in css
    assert "body.dp-v11-structural .event-item" not in css


def test_activity_runtime_reclassifies_only_main_page_event_rows() -> None:
    runtime = read("ui-runtime.js")
    app = read("app.js")

    required = (
        "document.getElementById('event-list')",
        "row.classList.add('dp-activity-row')",
        "row.classList.remove('event-item')",
        "level.classList.add('dp-activity-level')",
        "message.classList.add('dp-activity-message')",
        "transfer.classList.add('dp-activity-transfer')",
        "time.classList.add('dp-activity-time')",
        "new MutationObserver(normalizeActivityRows)",
    )
    missing = [fragment for fragment in required if fragment not in runtime]
    assert not missing, f"Activity runtime normalization is incomplete: {missing}"

    # app.js still emits the legacy class for both Activity and Details; only
    # direct children of #event-list are reclassified by the presentation layer.
    assert app.count('class="event-item"') >= 2
    assert "querySelectorAll('.event-item')" not in runtime


def test_activity_functional_controls_are_unchanged() -> None:
    html = read("index.html")
    app = read("app.js")

    for fragment in (
        'id="ev-search"',
        'oninput="filterEvents()"',
        'id="ev-level"',
        'onchange="filterEvents()"',
        'onclick="loadEvents()"',
        'id="event-list"',
    ):
        assert fragment in html

    assert "function filterEvents()" in app
    assert "async function loadEvents()" in app


def test_failed_tall_panel_experiment_is_not_consumed_by_operational_pages() -> None:
    activity = read("ui-activity-log-page.css")
    downloads = read("ui-downloads-page.css")

    assert "--dp-panel-surface-tall" not in activity
    assert "--dp-panel-surface-tall" not in downloads


def test_shared_layers_do_not_reach_into_activity_or_details_rows() -> None:
    shared = read("ui-shared-contract.css")
    universal = read("ui-universal-language.css")

    combined = shared + universal
    assert "#view-events" not in combined
    assert "#event-list" not in combined
    assert ".event-item" not in combined
