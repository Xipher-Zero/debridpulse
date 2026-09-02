"""Contracts for the canonical Lucide/status presentation ownership."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
APP = STATIC / "app.js"
SHELL_RUNTIME = STATIC / "operator-title.js"
PRESENTATION_RUNTIME = STATIC / "ui-runtime.js"
DOWNLOADS_RUNTIME = STATIC / "ui-downloads-runtime.js"
TRANSFER = STATIC / "ui-transfer-contract.css"
ICONS = STATIC / "icon-system.css"


def test_lucide_runtime_is_single_semantic_glyph_authority_without_global_replacement() -> None:
    app = APP.read_text(encoding="utf-8")
    shell = SHELL_RUNTIME.read_text(encoding="utf-8")
    required = (
        "window.DPIcons",
        "statusBadge: statusBadge",
        "statusMap: STATUS",
        "toastMap: TOAST_ICON",
        "decorateButton: decorateButton",
        "toast: canonicalToast",
        "renderThemeGlyph: renderThemeGlyph",
        "circleCheck:",
        "circleX:",
        "clock3:",
        "loaderCircle:",
        "packageOpen:",
        "triangleAlert:",
        "trash2:",
        "fileInput:",
        "23f9abc4ed0146cffededd3d7f94c1018bfdf693",
    )
    missing = [fragment for fragment in required if fragment not in shell]
    assert not missing, f"canonical Lucide runtime is missing: {missing}"

    for forbidden in (
        "window.badge = statusBadge",
        "window.toast = canonicalToast",
        "window.updateThemeToggle =",
        "window.toggleTheme =",
    ):
        assert forbidden not in shell

    assert "return window.DPIcons.statusBadge(s, category ? semantics.labels[category] : '', category);" in app
    assert "return window.DPIcons.toast(msg, type);" in app
    assert "window.DPIcons.renderThemeGlyph(!!isLight);" in app


def test_transfer_status_mapping_matches_debridpulse_semantics() -> None:
    shell = SHELL_RUNTIME.read_text(encoding="utf-8")
    mappings = (
        "downloading: {icon: 'download', label: 'Downloading'",
        "paused: {icon: 'pause', label: 'Paused'",
        "completed: {icon: 'circleCheck', label: 'Done'",
        "error: {icon: 'x', label: 'Error'",
        "uploading: {icon: 'upload', label: 'Uploading'",
        "queued: {icon: 'clock3', label: 'Queued'",
        "pending: {icon: 'clock3', label: 'Pending'",
        "processing: {icon: 'loaderCircle', label: 'Processing'",
        "input_required: {icon: 'triangleAlert', label: 'Input Required', className: 'input_required'",
        "extracting: {icon: 'packageOpen', label: 'Extracting'",
        "partial: {icon: 'triangleAlert', label: 'Partial'",
        "ready: {icon: 'play', label: 'Ready'",
        "deleted: {icon: 'trash2', label: 'Deleted'",
    )
    missing = [fragment for fragment in mappings if fragment not in shell]
    assert not missing, f"canonical transfer mappings are missing: {missing}"


def test_page_runtimes_consume_canonical_icons_without_private_svg_maps() -> None:
    dashboard = PRESENTATION_RUNTIME.read_text(encoding="utf-8")
    downloads = DOWNLOADS_RUNTIME.read_text(encoding="utf-8")

    for runtime in (dashboard, downloads):
        assert "window.DPIcons" in runtime
        assert "const paths =" not in runtime
        assert "data-dp-lucide" in runtime

    assert "arrow.innerHTML = utilitySvg('chevronDown')" in dashboard
    assert "normalizeUtilityButton(document.getElementById('btn-recover-all'), 'refresh')" in dashboard
    assert "normalizeUtilityButton(refresh, 'refresh')" in dashboard
    assert "utilitySvg('refresh')" in downloads
    assert "utilitySvg('chevronLeft')" in downloads
    assert "utilitySvg('chevronRight')" in downloads
    assert "'Delete', 'trash2'" in downloads
    assert "label = 'Now'; iconName = 'download'" in downloads


def test_status_badges_are_rectangular_and_include_details_overlay() -> None:
    css = TRANSFER.read_text(encoding="utf-8")
    assert ":is(#dash-tbody, #t-tbody, #modal-body) .badge" in css
    assert "border-radius: 6px !important" in css
    assert "gap: 6px !important" in css
    assert ".badge .dp-status-icon" in css

    # Symbol-font content must not compete with the canonical inline Lucide SVG.
    forbidden = (
        "Segoe UI Symbol",
        "Noto Sans Symbols",
        "content: '↓'",
        "content: 'Ⅱ'",
        "content: '✓'",
        "content: '×'",
    )
    present = [fragment for fragment in forbidden if fragment in css]
    assert not present, f"legacy transfer glyph injection remains: {present}"


def test_toasts_use_lucide_semantics_without_layout_redesign() -> None:
    shell = SHELL_RUNTIME.read_text(encoding="utf-8")
    icons = ICONS.read_text(encoding="utf-8")

    assert "success: 'circleCheck'" in shell
    assert "warning: 'triangleAlert'" in shell
    assert "warn: 'triangleAlert'" in shell
    assert "error: 'circleX'" in shell
    assert "info: 'info'" in shell
    assert "dp-toast-icon" in shell
    assert ".dp-toast-icon" in icons
    assert ".toast.success .dp-toast-icon" in icons
    assert ".toast.warn .dp-toast-icon" in icons
    assert ".toast.error .dp-toast-icon" in icons
    assert ".toast.info .dp-toast-icon" in icons
