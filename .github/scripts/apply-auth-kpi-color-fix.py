from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "frontend/static/ui-settings-page.js"
CSS = ROOT / "frontend/static/ui-settings-authentication.css"
TEST = ROOT / "backend/tests/test_settings_authentication_kpi_ui.py"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


js = JS.read_text(encoding="utf-8")
notice = '''      ${!a.authentication_required ? `
        <div class="dp-settings-caution dp-settings-auth-open-notice">
          <span><b>No interactive authentication enabled</b> — supported standalone/LAN mode; application and API are intentionally open.</span>
        </div>` : ''}
'''
js = replace_once(js, notice, "", "open auth notice")
JS.write_text(js, encoding="utf-8")

css = CSS.read_text(encoding="utf-8")
css = replace_once(css, "--c: var(--dp-semantic-success);", "--c: var(--dp-state-success);", "green KPI token")
css = replace_once(css, "--c: var(--dp-semantic-processing);", "--c: var(--dp-state-caution);", "yellow KPI token")
css = replace_once(css, "--c: var(--dp-semantic-error);", "--c: var(--dp-state-error);", "red KPI token")

old_notice_css = '''body.dp-v11-structural #view-settings .dp-settings-auth-open-notice {
  min-height: 0;
  display: block;
  padding: 7px 12px;
}

body.dp-v11-structural #view-settings .dp-settings-auth-open-notice > span {
  display: block;
  line-height: 1.4;
}

'''
css = replace_once(css, old_notice_css, "", "open auth notice CSS")

value_rules = '''body.dp-v11-structural #view-settings .dp-settings-auth-kpi[data-c="green"] .dhs-val {
  color: var(--dp-state-success);
}
body.dp-v11-structural #view-settings .dp-settings-auth-kpi[data-c="yellow"] .dhs-val {
  color: var(--dp-state-caution);
}
body.dp-v11-structural #view-settings .dp-settings-auth-kpi[data-c="red"] .dhs-val {
  color: var(--dp-state-error);
}
body.dp-v11-structural #view-settings .dp-settings-auth-kpi[data-c="neutral"] .dhs-val {
  color: var(--dp-text-muted);
}

'''
anchor = '''body.dp-v11-structural #view-settings .dp-settings-auth-kpi .dhs-body {
'''
if value_rules in css:
    raise SystemExit("KPI value color rules already present")
if css.count(anchor) != 1:
    raise SystemExit(f"KPI value color anchor: expected one match, found {css.count(anchor)}")
css = css.replace(anchor, value_rules + anchor, 1)
CSS.write_text(css, encoding="utf-8")

test = TEST.read_text(encoding="utf-8")
test = replace_once(
    test,
    '''    assert '[data-c="green"]' in css and "var(--dp-semantic-success)" in css
    assert '[data-c="yellow"]' in css and "var(--dp-semantic-processing)" in css
    assert '[data-c="red"]' in css and "var(--dp-semantic-error)" in css
    assert '[data-c="neutral"]' in css
''',
    '''    assert '[data-c="green"]' in css and "var(--dp-state-success)" in css
    assert '[data-c="yellow"]' in css and "var(--dp-state-caution)" in css
    assert '[data-c="red"]' in css and "var(--dp-state-error)" in css
    assert '[data-c="neutral"]' in css
    assert '[data-c="green"] .dhs-val' in css
    assert '[data-c="yellow"] .dhs-val' in css
    assert '[data-c="red"] .dhs-val' in css
''',
    "semantic KPI token assertions",
)
test = replace_once(
    test,
    '''def test_open_auth_notice_is_compact_single_line_copy():
    js = source(SETTINGS)
    css = source(STYLE)
    assert "No interactive authentication enabled</b> — supported standalone/LAN mode; application and API are intentionally open." in js
    assert ".dp-settings-auth-open-notice" in css
    assert "padding: 7px 12px;" in css
''',
    '''def test_open_auth_notice_is_removed_as_redundant_with_authentication_mode_kpi():
    js = source(SETTINGS)
    css = source(STYLE)
    assert "No interactive authentication enabled" not in js
    assert ".dp-settings-auth-open-notice" not in css
''',
    "open auth notice regression test",
)
TEST.write_text(test, encoding="utf-8")
