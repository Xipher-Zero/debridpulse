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
        "var(--dp-table-row-border)",
        "var(--dp-table-row-hover)",
        "var(--dp-text-primary)",
        "var(--dp-text-muted)",
    )
    missing = [fragment for fragment in required if fragment not in shared]
    assert not missing, f"shared Activity material bridge is missing: {missing}"

    # Activity remains geometry/content composition only; reusable row material
    # must not be copied back into the page layer.
    assert "var(--dp-table-row-border)" not in activity
    assert "var(--dp-table-row-hover)" not in activity


def test_activity_material_bridge_is_loaded_before_page_geometry() -> None:
    overlay = read("style-v11.css")
    shared = overlay.index("/ui-shared-contract.css?v=27")
    activity = overlay.index("/ui-activity-log-page.css?v=26")
    assert shared < activity
