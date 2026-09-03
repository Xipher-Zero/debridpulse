"""Final-state cross-page consistency contracts."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read_static(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_recent_activity_empty_icon_matches_downloads_size() -> None:
    dashboard = read_static("ui-dashboard.css")
    downloads = read_static("ui-downloads-page.css")
    assert "#dash-activity-card.dp-dashboard-activity .empty-icon" in dashboard
    assert "width: 76px !important" in dashboard
    assert "height: 76px !important" in dashboard
    assert "#view-torrents .empty-icon" in downloads
    assert "width: 76px" in downloads
    assert "height: 76px" in downloads


def test_downloads_footer_has_no_separator_and_uses_shared_bottom_datum() -> None:
    downloads = read_static("ui-downloads-page.css")
    runtime = read_static("ui-accessibility-runtime.js")
    assert "height: 100% !important" in downloads
    assert "margin-bottom: 0 !important" in downloads
    assert "calc(100vh - var(--dp-shell-header)" not in downloads
    assert "#torrent-pagination" in downloads
    assert "border-top: 0;" in downloads
    assert "removeProperty('border-top')" not in runtime


def test_downloads_pager_uses_canonical_material_bridge() -> None:
    shared = read_static("ui-shared-contract.css")
    assert ".dp-pager-btn" in shared
    assert "var(--dp-secondary-surface)" in shared
    assert "var(--dp-segment-active-surface)" in shared
    assert '[aria-current="page"]' in shared
    assert "var(--dp-focus-ring)" in shared


def test_provider_status_is_three_centered_shell_zones() -> None:
    provider = read_static("ui-shell-provider-status.css")
    app = read_static("app.js")
    runtime = read_static("ui-accessibility-runtime.js")
    assert "#sidebar .sidebar-footer::before" in provider
    assert "justify-content: center !important" in provider
    assert ".conn-row:has(#dot-api)" in provider
    assert '#premium-row[style*="display:none"]' in provider
    assert "#lbl-premium::before" in provider
    assert "content: none !important" in provider
    assert "dp-provider-premium-until" in app
    assert "dp-provider-premium-days" in app
    assert "normalizeProviderPremiumLabel" not in runtime
    assert "AllDebrid Premium until " not in runtime


def test_quick_add_focus_resets_to_universal_field_language() -> None:
    dashboard = read_static("ui-dashboard.css")
    assert "textarea.input.direct-link-input" in dashboard
    assert "var(--dp-field-border)" in dashboard
    assert "var(--dp-field-surface)" in dashboard
    assert "color-mix(in srgb, var(--dp-accent-purple-bright) 72%, var(--dp-border-strong))" in dashboard
    assert "box-shadow: var(--dp-focus-ring) !important" in dashboard


def test_cross_page_owners_remain_in_deliberate_cascade_order() -> None:
    overlay = read_static("style-v11.css")
    shared = overlay.index("/ui-shared-contract.css?v=31")
    shell = overlay.index("/ui-shell.css?v=21")
    provider = overlay.index("/ui-shell-provider-status.css?v=24")
    dashboard = overlay.index("/ui-dashboard.css?v=20")
    downloads = overlay.index("/ui-downloads-page.css?v=28")
    transfer = overlay.index("/ui-transfer-contract.css?v=31")
    visual = overlay.index("/ui-visual-accents.css?v=21")
    signal = overlay.index("/ui-shell-signal-field.css?v=20")
    assert shared < shell < provider < dashboard < downloads < transfer < visual < signal

def test_global_toast_uses_one_footer_safe_anchor_across_pages() -> None:
    shared = read_static("ui-shared-contract.css")
    toast = shared[shared.index("/* Global toast position") :]

    assert "--dp-toast-bottom-offset: 96px;" in toast
    assert "body.dp-v11-structural #toasts" in toast
    assert "bottom: var(--dp-toast-bottom-offset);" in toast
    assert "@media (max-width: 900px)" in toast
    assert "--dp-toast-bottom-offset: 160px;" in toast
    assert "#view-settings" not in toast
    assert ".active" not in toast
