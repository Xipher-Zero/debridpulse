"""Contracts for the final v1.0.11 live-review visual batch."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_live_review_overlay_loads_after_transfer_contract() -> None:
    entry = read("style-v11.css")
    transfer = entry.index("/ui-transfer-contract.css?v=31")
    review = entry.index("/ui-live-review-batch.css?v=20")
    assert review > transfer


def test_status_badges_are_lucide_only_and_glow_like_progress() -> None:
    css = read("ui-live-review-batch.css")
    assert ".badge[data-dp-status]::before" in css
    assert "content: none !important" in css
    assert "var(--dp-badge-color) 72%" in css
    assert "var(--dp-badge-color) 32%" in css
    assert "0 0 6px" in css
    assert "0 0 13px" in css


def test_activity_points_use_stronger_theme_aware_semantic_glow() -> None:
    css = read("ui-live-review-batch.css")
    assert ".dp-activity-level.info" in css
    assert "var(--dp-state-active)" in css
    assert ".dp-activity-level.warn" in css
    assert "var(--dp-state-caution)" in css
    assert ".dp-activity-level.error" in css
    assert "var(--dp-state-error)" in css
    assert "body.dp-v11-structural:not(.light) #view-events" in css
    assert "var(--dp-event-point-color) 92%" in css
    assert "var(--dp-event-point-color) 58%" in css
    assert "var(--dp-event-point-color) 28%" in css
    assert "body.light.dp-v11-structural #view-events" in css
    assert "var(--dp-event-point-color) 88%" in css
    assert "var(--dp-event-point-color) 46%" in css
    assert "var(--dp-event-point-color) 22%" in css


def test_dark_elevation_stays_subdued_and_provider_card_has_light_parity() -> None:
    css = read("ui-live-review-batch.css")
    assert "body.dp-v11-structural:not(.light)" in css
    assert "--dp-dark-surface-shadow:" in css
    assert "--dp-panel-shadow:" in css
    assert "rgba(84, 38, 131, .18)" in css
    assert "rgba(167, 139, 250, .06)" in css
    assert "#view-dashboard .dash-hero-stat" in css
    assert "#view-dashboard .dp-dashboard-quick-add" in css
    assert "#view-dashboard .dp-dashboard-activity" in css
    assert "body.dp-v11-structural:not(.light) #sidebar" in css
    assert "body.dp-v11-structural:not(.light) #sidebar .sidebar-footer" in css
    assert "body.light.dp-v11-structural #sidebar .sidebar-footer" in css
    assert "rgba(132, 40, 237, .18)" in css
    assert "rgba(45, 61, 96, .14)" in css
