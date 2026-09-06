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


def test_provider_status_has_one_neutral_centered_presentation_owner() -> None:
    shell = read_static("ui-shell-provider-status.css")
    provider = read_static("ui-provider-state.css")
    account = read_static("ui-alldebrid-account-status.js")
    runtime = read_static("ui-accessibility-runtime.js")

    assert "#sidebar .sidebar-footer::before" not in shell
    assert ".conn-row:has(#dot-api)" not in shell
    assert "AllDebrid: Connected" not in shell
    assert ".dp-provider-status-list::before" in provider
    assert "content: 'Provider Status'" in provider
    assert ".dp-provider-status-row" in provider
    assert "justify-content: center;" in provider
    assert "text-align: center;" in provider

    assert '#premium-row[style*="display:none"]' in shell
    assert "#lbl-premium::before" in shell
    assert "content: none !important" in shell
    assert "className = 'dp-provider-premium-until'" in account
    assert "className = 'dp-provider-premium-days'" in account
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
    shared = overlay.index("/ui-shared-contract.css?v=32")
    shell = overlay.index("/ui-shell.css?v=21")
    provider = overlay.index("/ui-shell-provider-status.css?v=24")
    dashboard = overlay.index("/ui-dashboard.css?v=20")
    downloads = overlay.index("/ui-downloads-page.css?v=28")
    transfer = overlay.index("/ui-transfer-contract.css?v=31")
    visual = overlay.index("/ui-visual-accents.css?v=21")
    signal = overlay.index("/ui-shell-signal-field.css?v=20")
    assert shared < shell < provider < dashboard < downloads < transfer < visual < signal


def test_global_toast_uses_one_topbar_safe_anchor_across_pages() -> None:
    shared = read_static("ui-shared-contract.css")
    toast = read_static("ui-toast-contract.css")
    operator = read_static("operator-title.js")

    assert "--dp-toast-bottom-offset" not in shared
    assert "bottom: var(--dp-toast-bottom-offset);" not in shared
    assert "body.dp-v11-structural #toasts" in toast
    assert "bottom: auto !important;" in toast
    assert "pointer-events: none;" in toast
    assert "function toastSafeLane()" in operator
    assert "function updateToastHostPosition()" in operator
    assert "const desiredTop = lane.top + ((lane.bottom - lane.top) - renderedRect.height) / 2;" in operator
    assert "#view-settings" not in toast
    assert ".active" not in toast
