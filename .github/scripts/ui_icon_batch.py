from pathlib import Path


def replace_exact(path, old, new, expected=1):
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != expected:
        raise SystemExit(f"{path}: expected {expected} occurrence(s) of {old!r}, found {count}")
    p.write_text(text.replace(old, new), encoding="utf-8")


# Runtime: Recover All consumes the exact same utility refresh SVG as Activity.
replace_exact(
    "frontend/static/ui-runtime.js",
    "    if (!/style-v11\\.css\\?v=21$/.test(link.href)) link.href = '/style-v11.css?v=21';",
    "    if (!/style-v11\\.css\\?v=22$/.test(link.href)) link.href = '/style-v11.css?v=22';",
)
replace_exact(
    "frontend/static/ui-runtime.js",
    "    normalizeDpButton(document.getElementById('btn-recover-all'), 'retry-borderless.svg');",
    "    normalizeUtilityButton(document.getElementById('btn-recover-all'), 'refresh');",
)

# Static bootstrap/cache coherence for the changed runtime and overlay.
replace_exact("frontend/static/index.html", 'href="/style-v11.css?v=21"', 'href="/style-v11.css?v=22"')
replace_exact("frontend/static/index.html", '<script src="/operator-title.js?v=20" defer></script>', '<script src="/operator-title.js?v=21" defer></script>')
replace_exact("frontend/static/index.html", '<script src="/ui-runtime.js?v=20" defer data-dp-ui-runtime="1"></script>', '<script src="/ui-runtime.js?v=22" defer data-dp-ui-runtime="1"></script>')
replace_exact("frontend/static/operator-title.js", "  script.src = '/ui-runtime.js?v=21';", "  script.src = '/ui-runtime.js?v=22';")

controls = Path("frontend/static/ui-dashboard-control-polish.css")
controls_text = controls.read_text(encoding="utf-8")
marker = "/* ── Recover All / Activity Refresh exact icon parity ───────────────── */"
if marker in controls_text:
    raise SystemExit("ui-dashboard-control-polish.css: icon parity section already exists")
controls_text += r'''

/* ── Recover All / Activity Refresh exact icon parity ───────────────── */
/* Both controls now render the same utilitySvg('refresh') geometry. Keep the
   icon dimensions, stroke, inherited violet color and glow identical. */
body.dp-v11-structural #view-dashboard #btn-recover-all .dp-utility-icon,
body.dp-v11-structural #view-events .dp-activity-refresh .dp-utility-icon {
  width: 18px !important;
  height: 18px !important;
  stroke-width: 2 !important;
  color: inherit !important;
  filter: drop-shadow(0 0 3px rgba(160,88,215,.14)) !important;
}
body.light.dp-v11-structural #view-dashboard #btn-recover-all .dp-utility-icon,
body.light.dp-v11-structural #view-events .dp-activity-refresh .dp-utility-icon {
  filter: drop-shadow(0 0 3px rgba(139,72,188,.12)) !important;
}
'''
controls.write_text(controls_text, encoding="utf-8")

# Provider Status: center crown visible art + label as one visual block. The
# 48px crown SVG paints about 27px horizontally; rendered at 36px that is ~20px.
# Use that painted width for layout while keeping the 36px asset centered.
Path("frontend/static/ui-shell-provider-status-v2.css").write_text(r'''/* DebridPulse v1.0.11 Provider Status centered subscription composition.
 * Crown visible art + two-line subscription copy are one centered visual unit.
 */

body.dp-v11-structural #sidebar .sidebar-footer #premium-row[style*="display:none"],
body.dp-v11-structural #sidebar .sidebar-footer #premium-row[style*="display: none"] {
  display: none !important;
}

body.dp-v11-structural #sidebar .sidebar-footer #premium-row:not([style*="display:none"]):not([style*="display: none"]) {
  display: flex !important;
  align-items: center !important;
  justify-content: center !important;
  gap: 5px !important;
  width: max-content !important;
  max-width: 100% !important;
  margin: 0 auto 6px !important;
  padding: 0 0 9px !important;
}

body.dp-v11-structural #sidebar .sidebar-footer #premium-row::before {
  position: static !important;
  inset: auto !important;
  transform: none !important;
  flex: 0 0 20px !important;
  width: 20px !important;
  min-width: 20px !important;
  height: 36px !important;
  margin: 0 !important;
  background-position: center !important;
  background-size: 36px 36px !important;
  overflow: visible !important;
}

body.dp-v11-structural #sidebar .sidebar-footer #lbl-premium {
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
}

body.dp-v11-structural #sidebar .sidebar-footer .dp-provider-premium-until,
body.dp-v11-structural #sidebar .sidebar-footer .dp-provider-premium-days {
  display: block !important;
  white-space: nowrap !important;
}
''', encoding="utf-8")

# Shared feature-icon contract. KPI glow remains card-accent driven elsewhere;
# isolated icons source their glow from dominant colors in their actual SVGs.
Path("frontend/static/ui-feature-icon-contract.css").write_text(r'''/* DebridPulse v1.0.11 shared feature-icon contract.
 * One visual footprint across KPI and isolated card/page feature icons.
 */

:root {
  --dp-feature-icon-size: 51px;
}

body.dp-v11-structural #view-dashboard .dash-hero-stat .dhs-icon {
  width: var(--dp-feature-icon-size) !important;
  min-width: var(--dp-feature-icon-size) !important;
  height: var(--dp-feature-icon-size) !important;
  flex: 0 0 var(--dp-feature-icon-size) !important;
}

body.dp-v11-structural #view-dashboard .dash-hero-stat .dhs-icon .dp-icon,
body.dp-v11-structural #view-dashboard .dp-dashboard-quick-add .card-title > .dp-icon,
body.dp-v11-structural #view-dashboard .dp-dashboard-activity .card-title > .dp-icon,
body.dp-v11-structural #view-events .dp-activity-title-icon,
body.dp-v11-structural #view-torrents .dp-downloads-title-icon {
  width: var(--dp-feature-icon-size) !important;
  min-width: var(--dp-feature-icon-size) !important;
  height: var(--dp-feature-icon-size) !important;
  flex: 0 0 var(--dp-feature-icon-size) !important;
  object-fit: contain !important;
}

/* Dominant SVG colors, taken from the actual custom artwork. */
body.dp-v11-structural #view-dashboard .dp-dashboard-quick-add .card-title > .dp-icon {
  --dp-feature-icon-glow: #8a5ad6;
}
body.dp-v11-structural #view-dashboard .dp-dashboard-activity .card-title > .dp-icon {
  --dp-feature-icon-glow: #8b3fff;
}
body.dp-v11-structural #view-events .dp-activity-title-icon {
  --dp-feature-icon-glow: #4c8fff;
}
body.dp-v11-structural #view-torrents .dp-downloads-title-icon {
  --dp-feature-icon-glow: #b866f5;
}

body.dp-v11-structural #view-dashboard .dp-dashboard-quick-add .card-title > .dp-icon,
body.dp-v11-structural #view-dashboard .dp-dashboard-activity .card-title > .dp-icon,
body.dp-v11-structural #view-events .dp-activity-title-icon,
body.dp-v11-structural #view-torrents .dp-downloads-title-icon {
  filter:
    drop-shadow(0 0 5px color-mix(in srgb, var(--dp-feature-icon-glow) 62%, transparent))
    drop-shadow(0 0 11px color-mix(in srgb, var(--dp-feature-icon-glow) 27%, transparent)) !important;
}

body.light.dp-v11-structural #view-dashboard .dp-dashboard-quick-add .card-title > .dp-icon,
body.light.dp-v11-structural #view-dashboard .dp-dashboard-activity .card-title > .dp-icon,
body.light.dp-v11-structural #view-events .dp-activity-title-icon,
body.light.dp-v11-structural #view-torrents .dp-downloads-title-icon {
  filter:
    drop-shadow(0 0 6px color-mix(in srgb, var(--dp-feature-icon-glow) 70%, transparent))
    drop-shadow(0 0 15px color-mix(in srgb, var(--dp-feature-icon-glow) 32%, transparent)) !important;
}
''', encoding="utf-8")

replace_exact("frontend/static/style-v11.css", "/ui-shell-provider-status-v2.css?v=27", "/ui-shell-provider-status-v2.css?v=28")
replace_exact("frontend/static/style-v11.css", "/ui-dashboard-control-polish.css?v=21", "/ui-dashboard-control-polish.css?v=22")
replace_exact(
    "frontend/static/style-v11.css",
    "@import url('/ui-help-page.css?v=22');\n\n/* Final shared transfer-row semantics.",
    "@import url('/ui-help-page.css?v=22');\n@import url('/ui-feature-icon-contract.css?v=1');\n\n/* Final shared transfer-row semantics.",
)

# Existing cache/layer contracts advance only for files changed above.
shell_test = Path("backend/tests/test_ui_shell_contract.py")
text = shell_test.read_text(encoding="utf-8")
text = text.replace('/ui-shell-provider-status-v2.css?v=27', '/ui-shell-provider-status-v2.css?v=28')
text = text.replace('/ui-dashboard-control-polish.css?v=21', '/ui-dashboard-control-polish.css?v=22')
text = text.replace('        "/ui-help-page.css?v=22",\n        "/ui-transfer-contract.css?v=30",', '        "/ui-help-page.css?v=22",\n        "/ui-feature-icon-contract.css?v=1",\n        "/ui-transfer-contract.css?v=30",')
text = text.replace('        "/ui-help-page.css": "22",\n        "/ui-transfer-contract.css": "30",', '        "/ui-help-page.css": "22",\n        "/ui-feature-icon-contract.css": "1",\n        "/ui-transfer-contract.css": "30",')
text = text.replace('assert "/style-v11.css?v=21" in runtime', 'assert "/style-v11.css?v=22" in runtime')
text = text.replace("assert '/style-v11.css?v=21' in index", "assert '/style-v11.css?v=22' in index")
text = text.replace("assert '/operator-title.js?v=20' in index", "assert '/operator-title.js?v=21' in index\n    assert '/ui-runtime.js?v=22' in index")
text = text.replace("assert '/ui-runtime.js?v=21' in operator", "assert '/ui-runtime.js?v=22' in operator")
text = text.replace("assert '/style-v11.css?v=21' in runtime", "assert '/style-v11.css?v=22' in runtime")
shell_test.write_text(text, encoding="utf-8")

deep = Path("backend/tests/test_ui_frontend_deep_audit_contract.py")
text = deep.read_text(encoding="utf-8")
text = text.replace('href="/style-v11.css?v=21"', 'href="/style-v11.css?v=22"')
text = text.replace('<script src="/operator-title.js?v=20" defer></script>', '<script src="/operator-title.js?v=21" defer></script>')
text = text.replace('<script src="/ui-runtime.js?v=20" defer data-dp-ui-runtime="1"></script>', '<script src="/ui-runtime.js?v=22" defer data-dp-ui-runtime="1"></script>')
text = text.replace('/style-v11.css?v=21', '/style-v11.css?v=22')
text = text.replace('style-v11\\\\.css\\\\?v=21$', 'style-v11\\\\.css\\\\?v=22$')
text = text.replace('/ui-shell-provider-status-v2.css?v=27', '/ui-shell-provider-status-v2.css?v=28')
text = text.replace('/ui-dashboard-control-polish.css?v=21', '/ui-dashboard-control-polish.css?v=22')
text = text.replace('        "/ui-help-page.css": "22",\n        "/ui-transfer-contract.css": "30",', '        "/ui-help-page.css": "22",\n        "/ui-feature-icon-contract.css": "1",\n        "/ui-transfer-contract.css": "30",')
text = text.replace('    help_pos = overlay.index("/ui-help-page.css?v=22")\n    transfer_pos', '    help_pos = overlay.index("/ui-help-page.css?v=22")\n    feature_icon_pos = overlay.index("/ui-feature-icon-contract.css?v=1")\n    transfer_pos')
text = text.replace('    assert downloads_pos < downloads_desktop_pos < help_pos < transfer_pos', '    assert downloads_pos < downloads_desktop_pos < help_pos < feature_icon_pos < transfer_pos')
deep.write_text(text, encoding="utf-8")

activity_test = Path("backend/tests/test_ui_activity_log_page_contract.py")
text = activity_test.read_text(encoding="utf-8").replace('/ui-dashboard-control-polish.css?v=21', '/ui-dashboard-control-polish.css?v=22')
activity_test.write_text(text, encoding="utf-8")

Path("backend/tests/test_ui_feature_icon_contract.py").write_text(r'''"""Shared feature icon, refresh utility, and provider-group presentation contracts."""

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
    dashboard = read("ui-dashboard-polish-final.css")
    assert "var(--c) 62%" in dashboard
    assert "var(--c) 70%" in dashboard
    for color in ("#8a5ad6", "#8b3fff", "#4c8fff", "#b866f5"):
        assert color in feature
    assert "var(--dp-feature-icon-glow) 62%" in feature
    assert "var(--dp-feature-icon-glow) 70%" in feature


def test_dashboard_recover_all_uses_exact_activity_refresh_utility_geometry() -> None:
    runtime = read("ui-runtime.js")
    controls = read("ui-dashboard-control-polish.css")
    assert "normalizeUtilityButton(document.getElementById('btn-recover-all'), 'refresh');" in runtime
    assert "normalizeUtilityButton(refresh, 'refresh');" in runtime
    assert "normalizeDpButton(document.getElementById('btn-recover-all'), 'retry-borderless.svg');" not in runtime
    assert "#btn-recover-all .dp-utility-icon" in controls
    assert ".dp-activity-refresh .dp-utility-icon" in controls


def test_provider_premium_group_centers_visible_crown_and_copy_as_one_block() -> None:
    css = read("ui-shell-provider-status-v2.css")
    assert "display: flex !important" in css
    assert "width: max-content !important" in css
    assert "margin: 0 auto 6px !important" in css
    assert "flex: 0 0 20px !important" in css
    assert "background-size: 36px 36px !important" in css
    assert "flex: 0 0 auto !important" in css
    assert "translateX" not in css
''', encoding="utf-8")
