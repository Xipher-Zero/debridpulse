"""Contracts for the canonical Lucide/status presentation ownership."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
APP = STATIC / "app.js"
SHELL_RUNTIME = STATIC / "operator-title.js"
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

    # One status projection (statusShown) feeds the runtime's badge, and the
    # handheld Transfer Summary's glyph: both drawn by DPIcons alone.
    assert "return {tone: s, label: category ? semantics.label(category) : '', category, projected: ''};" in app
    assert "window.DPIcons.statusBadge(shown.tone, shown.label, shown.category)" in app
    assert "return window.DPIcons.statusGlyph(shown.tone, shown.label, shown.category);" in app
    assert "return window.DPIcons.toast(msg, type);" in app
    assert "window.DPIcons.renderThemeGlyph(!!isLight);" in app


def test_transfer_status_mapping_matches_debridpulse_semantics() -> None:
    from i18n_node import english

    shell = SHELL_RUNTIME.read_text(encoding="utf-8")
    # Each status keeps its glyph and class; its words are the catalog's
    # status.<name> (1.0.14 localization), the same English as before.
    mappings = (
        ("downloading", "download", "Downloading", None), ("paused", "pause", "Paused", None),
        ("completed", "circleCheck", "Done", None), ("error", "x", "Error", None),
        ("uploading", "upload", "Uploading", None), ("queued", "clock3", "Queued", None),
        ("pending", "clock3", "Pending", None), ("processing", "loaderCircle", "Processing", None),
        ("input_required", "triangleAlert", "Input Required", "input_required"),
        ("extracting", "packageOpen", "Extracting", None), ("partial", "triangleAlert", "Partial", None),
        ("ready", "play", "Ready", None), ("deleted", "trash2", "Deleted", None),
    )
    missing = []
    for status, icon, words, class_name in mappings:
        fragment = f"{status}: {{icon: '{icon}', label: 'status.{status}'" + (f", className: '{class_name}'" if class_name else "")
        if fragment not in shell or english(f"status.{status}") != words:
            missing.append(status)
    assert not missing, f"canonical transfer mappings are missing: {missing}"


def test_page_owners_consume_canonical_icons_without_private_svg_maps() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    shell = SHELL_RUNTIME.read_text(encoding="utf-8")
    for icon in ("refresh", "chevronLeft", "chevronRight", "trash2", "pause", "play"):
        assert f"{icon}:" in shell
    assert 'data-dp-lucide="refresh"' in index
    assert 'data-dp-lucide="pause"' in index
    assert 'data-dp-lucide="play"' in index
    assert 'data-dp-lucide="trash2"' in index
    assert "window.DPIcons.svg" in app
    assert "const paths =" not in app
    assert not (STATIC / "ui-runtime.js").exists()
    assert not (STATIC / "ui-downloads-runtime.js").exists()

def test_status_badges_are_rectangular_and_include_details_overlay() -> None:
    css = TRANSFER.read_text(encoding="utf-8")
    assert ":is(#dash-tbody, #t-tbody, .dp-downloads-styled-tbody, #modal-body) .badge" in css
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
