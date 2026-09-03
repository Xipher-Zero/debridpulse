from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend" / "static"
TESTS = ROOT / "backend" / "tests"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}")
    return source.replace(old, new, 1)


def replace_function(source: str, name: str, replacement: str = "") -> str:
    pattern = re.compile(
        rf"\n  function {re.escape(name)}\([^\n]*\) \{{.*?\n  \}}\n",
        re.DOTALL,
    )
    source, count = pattern.subn("\n" + replacement.rstrip() + ("\n" if replacement else ""), source, count=1)
    if count != 1:
        raise RuntimeError(f"function replacement failed: {name} ({count})")
    return source


def replace_test(source: str, name: str, replacement: str) -> str:
    pattern = re.compile(
        rf"^def {re.escape(name)}\([^\n]*\).*?(?=^def |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    source, count = pattern.subn(replacement.rstrip() + "\n\n", source, count=1)
    if count != 1:
        raise RuntimeError(f"test replacement failed: {name} ({count})")
    return source


# ---------------------------------------------------------------------------
# Provider Status: collapse the v2 override generation into the canonical file.
# ---------------------------------------------------------------------------
provider = STATIC / "ui-shell-provider-status.css"
provider_v2 = STATIC / "ui-shell-provider-status-v2.css"
provider_source = read(provider)
if not provider_v2.exists():
    raise RuntimeError("provider-status v2 layer is already absent before E2")

old_row = '''body.dp-v11-structural #sidebar .sidebar-footer #premium-row:not([style*="display:none"]):not([style*="display: none"]) {
  display: flex !important;
  width: 100%;
  align-items: center !important;
  justify-content: center !important;
  gap: 10px !important;
  margin: 0 0 6px !important;
  padding: 0 0 9px !important;
}'''
new_row = '''body.dp-v11-structural #sidebar .sidebar-footer #premium-row:not([style*="display:none"]):not([style*="display: none"]) {
  display: flex !important;
  align-items: center !important;
  justify-content: center !important;
  gap: 5px !important;
  width: max-content !important;
  max-width: 100% !important;
  margin: 0 auto 6px !important;
  padding: 0 0 9px !important;
}'''
provider_source = replace_once(provider_source, old_row, new_row, "provider premium row final geometry")

old_crown = '''body.dp-v11-structural #sidebar .sidebar-footer #premium-row::before {
  position: static !important;
  grid-column: auto !important;
  justify-self: auto !important;
  flex: 0 0 36px !important;
  width: 36px !important;
  min-width: 36px !important;
  height: 36px !important;
  margin: 0 !important;
  top: auto !important;
  background-size: 36px 36px !important;
}'''
new_crown = '''body.dp-v11-structural #sidebar .sidebar-footer #premium-row::before {
  position: static !important;
  inset: auto !important;
  transform: none !important;
  flex: 0 0 36px !important;
  width: 36px !important;
  min-width: 36px !important;
  height: 36px !important;
  margin: 0 -8px !important;
  background-position: center !important;
  background-size: 36px 36px !important;
  background-repeat: no-repeat !important;
}'''
provider_source = replace_once(provider_source, old_crown, new_crown, "provider crown final geometry")

old_label = '''body.dp-v11-structural #sidebar .sidebar-footer #lbl-premium {
  grid-column: auto !important;
  display: flex !important;
  flex: 0 1 auto;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  width: auto !important;
  max-width: 156px !important;
  margin: 0 !important;
  white-space: normal !important;
  text-align: center !important;
  font-size: 10.5px !important;
  line-height: 1.38 !important;
  letter-spacing: -.012em !important;
}'''
new_label = '''body.dp-v11-structural #sidebar .sidebar-footer #lbl-premium {
  display: flex !important;
  flex: 0 0 auto !important;
  flex-direction: column !important;
  align-items: center !important;
  justify-content: center !important;
  width: max-content !important;
  max-width: none !important;
  margin: 0 !important;
  text-align: center !important;
  white-space: normal !important;
  font-size: 10.5px !important;
  line-height: 1.38 !important;
  letter-spacing: -.012em !important;
}'''
provider_source = replace_once(provider_source, old_label, new_label, "provider premium copy final geometry")
provider_source = provider_source.replace(
    "/* The presentation runtime owns the complete visible copy so the two-line\n   subscription state is deterministic. Suppress the older CSS prefix. */",
    "/* app.js emits the complete two-line subscription state directly. */",
    1,
)
provider_source = provider_source.replace(
    '''body.dp-v11-structural #sidebar .sidebar-footer .dp-provider-premium-until,
body.dp-v11-structural #sidebar .sidebar-footer .dp-provider-premium-days {
  display: block;
  white-space: nowrap;
}''',
    '''body.dp-v11-structural #sidebar .sidebar-footer .dp-provider-premium-until,
body.dp-v11-structural #sidebar .sidebar-footer .dp-provider-premium-days {
  display: block !important;
  white-space: nowrap !important;
}''',
    1,
)
write(provider, provider_source)
provider_v2.unlink()

style_path = STATIC / "style-v11.css"
style = read(style_path)
style = replace_once(
    style,
    "@import url('/ui-shell-provider-status-v2.css?v=28');\n",
    "",
    "provider-status v2 import",
)
write(style_path, style)


# ---------------------------------------------------------------------------
# Canonical markup: stop relying on accessibility runtime presentation repair.
# ---------------------------------------------------------------------------
index_path = STATIC / "index.html"
index = read(index_path)
index = replace_once(
    index,
    '<span class="nav-label">Event Log</span>',
    '<span class="nav-label">Activity Log</span>',
    "sidebar Activity Log naming",
)
index = replace_once(
    index,
    'padding:10px 14px;border-top:1px solid var(--border);flex-wrap:wrap;gap:8px',
    'padding:10px 14px;flex-wrap:wrap;gap:8px',
    "Downloads pagination inline divider",
)
write(index_path, index)

app_path = STATIC / "app.js"
app = read(app_path)
app = replace_once(app, "events:'Event Log',", "events:'Activity Log',", "Activity Log page title")
old_premium = "  const daysLabel = days > 0 ? `${days} days` : 'expired';\n  lbl.innerHTML = `Premium until ${dd}.${mm}.${yyyy} (${daysLabel})`;"
new_premium = "  const daysLabel = days > 0 ? `(${days} days remaining)` : '(expired)';\n  lbl.innerHTML = `<span class=\"dp-provider-premium-until\">AllDebrid Premium until ${dd}.${mm}.${yyyy}</span><span class=\"dp-provider-premium-days\">${daysLabel}</span>`;"
app = replace_once(app, old_premium, new_premium, "provider premium final markup")
write(app_path, app)


# ---------------------------------------------------------------------------
# Accessibility runtime: retain semantics/custom-select behavior, remove visual
# and naming repair functions now owned by index.html/app.js.
# ---------------------------------------------------------------------------
a11y_path = STATIC / "ui-accessibility-runtime.js"
a11y = read(a11y_path)
for function in (
    "normalizeActivityNaming",
    "normalizeDownloadsLegacyPresentation",
    "normalizeProviderPremiumLabel",
    "installProviderStatusPresentation",
):
    a11y = replace_function(a11y, function)
for call in (
    "normalizeActivityNaming();",
    "normalizeDownloadsLegacyPresentation();",
    "installProviderStatusPresentation();",
):
    a11y = re.sub(rf"^[ \t]*{re.escape(call)}[ \t]*\n", "", a11y, flags=re.MULTILINE)
write(a11y_path, a11y)


# ---------------------------------------------------------------------------
# Migrate historical tests from v2/runtime-repair shape to canonical ownership.
# ---------------------------------------------------------------------------
desktop_path = TESTS / "test_ui_desktop_downloads_batch_contract.py"
desktop = read(desktop_path)
desktop = replace_test(
    desktop,
    "test_provider_subscription_is_one_centered_crown_and_copy_unit",
    '''def test_provider_subscription_is_one_centered_crown_and_copy_unit():
    css = read("ui-shell-provider-status.css")
    assert not (STATIC / "ui-shell-provider-status-v2.css").exists()
    assert "display: flex !important" in css
    assert "justify-content: center !important" in css
    assert "gap: 5px !important" in css
    assert "flex: 0 0 36px !important" in css
    assert "width: 36px !important" in css
    assert "margin: 0 -8px !important" in css
    assert "background-size: 36px 36px !important" in css
    assert "width: max-content !important" in css
    assert "transform: none !important" in css
    assert ".dp-provider-premium-days" in css
    assert "white-space: nowrap !important" in css''',
)
desktop = replace_test(
    desktop,
    "test_new_contract_layers_live_in_correct_cascade_sections",
    '''def test_new_contract_layers_live_in_correct_cascade_sections():
    style = read("style-v11.css")
    modal = style.index("ui-modal-contract.css?v=25")
    shell = style.index("ui-shell.css?v=21")
    provider = style.index("ui-shell-provider-status.css?v=24")
    downloads_base = style.index("ui-downloads-page.css?v=28")
    downloads_desktop = style.index("ui-downloads-desktop.css?v=28")
    transfer = style.index("ui-transfer-contract.css?v=31")
    assert modal < shell < provider
    assert downloads_base < downloads_desktop < transfer
    assert "ui-shell-provider-status-v2.css" not in style
    assert style.count("ui-shell-provider-status.css") == 1''',
)
write(desktop_path, desktop)

# Strengthen the existing deep-audit contract: the cross-cutting accessibility
# runtime may project accessibility/dropdown semantics, but may not repair
# canonical page copy, provider presentation, or Downloads geometry.
deep_path = TESTS / "test_ui_frontend_deep_audit_contract.py"
deep = read(deep_path)
deep = replace_test(
    deep,
    "test_cross_cutting_accessibility_runtime_remains_semantic_and_io_free",
    '''def test_cross_cutting_accessibility_runtime_remains_semantic_and_io_free() -> None:
    source = read(A11Y_RUNTIME)
    required = (
        "aria-current", "aria-pressed", "role', 'group'", "role', 'tablist'",
        "role', 'tab'", "ArrowRight", "ArrowLeft", "Home", "End",
        "View downloads with errors", "Close details", "dp-dropdown-shell",
    )
    missing = [fragment for fragment in required if fragment not in source]
    assert not missing, f"Accessibility semantics are missing: {missing}"
    for forbidden in (
        "fetch(", "/api/", "XMLHttpRequest", "EventSource",
        "normalizeActivityNaming", "normalizeDownloadsLegacyPresentation",
        "normalizeProviderPremiumLabel", "installProviderStatusPresentation",
        "dpProviderObserved", "lbl-premium", "torrent-pagination",
    ):
        assert forbidden not in source''',
)
write(deep_path, deep)

# New bounded E2 ownership contract.
e2_test = TESTS / "test_uiarch001_e2_ownership.py"
write(e2_test, r'''from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_provider_status_has_one_canonical_style_owner() -> None:
    style = read("style-v11.css")
    provider = read("ui-shell-provider-status.css")
    assert not (STATIC / "ui-shell-provider-status-v2.css").exists()
    assert style.count("ui-shell-provider-status.css") == 1
    assert "ui-shell-provider-status-v2.css" not in style
    for fragment in (
        "gap: 5px !important", "width: max-content !important",
        "margin: 0 -8px !important", "background-size: 36px 36px !important",
        ".dp-provider-premium-until", ".dp-provider-premium-days",
    ):
        assert fragment in provider


def test_activity_downloads_and_provider_markup_are_final_at_source() -> None:
    index = read("index.html")
    app = read("app.js")
    assert '<span class="nav-label">Activity Log</span>' in index
    assert '<span class="nav-label">Event Log</span>' not in index
    assert "events:'Activity Log'" in app
    pagination = index[index.index('id="torrent-pagination"'):index.index('id="torrent-page-info"')]
    assert "border-top" not in pagination
    assert 'class="dp-provider-premium-until"' in app
    assert 'class="dp-provider-premium-days"' in app
    assert "AllDebrid Premium until" in app
    assert "days remaining" in app


def test_accessibility_runtime_does_not_repair_canonical_presentation() -> None:
    source = read("ui-accessibility-runtime.js")
    for forbidden in (
        "normalizeActivityNaming", "normalizeDownloadsLegacyPresentation",
        "normalizeProviderPremiumLabel", "installProviderStatusPresentation",
        "dpProviderObserved", "lbl-premium", "torrent-pagination",
    ):
        assert forbidden not in source
    for required in (
        "aria-current", "aria-pressed", "dp-dropdown-shell",
        "MutationObserver", "installUniversalSelectDropdowns",
    ):
        assert required in source


def test_e1_retired_runtimes_remain_absent() -> None:
    index = read("index.html")
    for retired in ("ui-runtime.js", "ui-downloads-runtime.js"):
        assert not (STATIC / retired).exists()
        assert retired not in index
''')
