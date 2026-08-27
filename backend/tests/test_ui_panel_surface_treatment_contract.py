"""Contracts for the shared v1.0.11 large-panel surface treatment."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_panel_surface_treatment_loads_after_page_geometry() -> None:
    entry = read("style-v11.css")
    downloads = entry.index("/ui-downloads-desktop.css?v=28")
    treatment = entry.index("/ui-panel-surface-treatment.css?v=21")
    transfer = entry.index("/ui-transfer-contract.css?v=31")
    assert downloads < treatment < transfer


def test_treatment_targets_large_content_surfaces_only() -> None:
    css = read("ui-panel-surface-treatment.css")
    assert "#view-dashboard .dp-dashboard-activity" in css
    assert "#view-dashboard .dp-dashboard-quick-add" in css
    assert "#view-torrents > .card" in css
    assert "#view-events > .dp-activity-card" in css
    assert "#view-help .help-panel.active > .card" in css
    assert "#modal .dp-detail-section-card" in css
    assert ".dash-hero-stat" not in css
    assert ".dp-downloads-bulk-card" not in css


def test_dark_surface_has_stronger_varied_luminance_clouds() -> None:
    css = read("ui-panel-surface-treatment.css")
    assert "radial-gradient(ellipse 112% 86%" in css
    assert "var(--dp-accent-purple-bright) 13.5%" in css
    assert "var(--dp-state-active) 8.3%" in css
    assert "radial-gradient(ellipse 70% 52% at 56% 14%" in css
    assert "var(--dp-accent-purple) 7.2%" in css
    assert "var(--dp-panel-surface) !important" in css


def test_light_surface_remains_clean_but_is_no_longer_barely_visible() -> None:
    css = read("ui-panel-surface-treatment.css")
    assert "body.light.dp-v11-structural" in css
    assert "var(--dp-accent-purple-bright) 8.5%" in css
    assert "var(--dp-state-active) 5.2%" in css
    assert "radial-gradient(ellipse 72% 54% at 56% 14%" in css
    assert "var(--dp-accent-purple) 4.6%" in css
    assert "rgba(105, 90, 145, .040)" in css
    assert "rgba(255,255,255,.62)" in css
