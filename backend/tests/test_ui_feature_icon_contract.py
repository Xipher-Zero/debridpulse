"""Shared feature icon, refresh utility, and provider-group presentation contracts."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_feature_icon_class_uses_one_51px_visual_footprint() -> None:
    css = read("ui-feature-icon-contract.css")
    assert "--dp-feature-icon-size: 51px" in css
    for selector in (
        ".dash-hero-stat .dhs-icon",
        ".dash-hero-stat .dhs-icon .dp-icon",
        ".dp-dashboard-quick-add .card-title > .dp-icon",
        ".dp-dashboard-activity .card-title > .dp-icon",
        ".dp-activity-title-icon",
        ".dp-downloads-title-icon",
    ):
        assert selector in css
    assert "width: var(--dp-feature-icon-size) !important" in css
    assert "height: var(--dp-feature-icon-size) !important" in css


def test_kpi_and_isolated_glows_keep_distinct_color_ownership() -> None:
    feature = read("ui-feature-icon-contract.css")
    dashboard = read("ui-dashboard.css")
    assert "var(--c) 62%" in dashboard
    assert "var(--c) 70%" in dashboard
    for color in ("#8a5ad6", "#8b3fff", "#4c8fff", "#b866f5"):
        assert color in feature
    assert "var(--dp-feature-icon-glow) 62%" in feature
    assert "var(--dp-feature-icon-glow) 70%" in feature


def test_dashboard_recover_all_uses_exact_activity_refresh_utility_geometry() -> None:
    index = read("index.html")
    controls = read("ui-utility-controls.css")
    dashboard = index[index.index('id="view-dashboard"'):index.index('id="view-torrents"')]
    events = index[index.index('id="view-events"'):index.index('<!-- Statistics -->')]
    assert 'id="btn-recover-all"' in dashboard
    assert 'data-dp-lucide="refresh"' in dashboard
    assert 'class="btn btn-ghost btn-sm dp-activity-refresh"' in events
    assert 'data-dp-lucide="refresh"' in events
    assert "#btn-recover-all .dp-utility-icon" in controls
    assert ".dp-activity-refresh .dp-utility-icon" in controls

def test_provider_premium_group_centers_visible_crown_and_copy_as_one_block() -> None:
    css = read("ui-shell-provider-status.css")
    assert not (STATIC / "ui-shell-provider-status-v2.css").exists()
    assert "display: flex !important" in css
    assert "width: max-content !important" in css
    assert "margin: 0 auto 6px !important" in css
    assert "flex: 0 0 36px !important" in css
    assert "width: 36px !important" in css
    assert "margin: 0 -8px !important" in css
    assert "background-size: 36px 36px !important" in css
    assert "flex: 0 0 auto !important" in css
    assert "translateX" not in css
