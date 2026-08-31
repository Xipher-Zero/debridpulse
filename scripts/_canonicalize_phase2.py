from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'frontend' / 'static'


def read(path):
    return (ROOT / path).read_text()


def write(path, text):
    (ROOT / path).write_text(text)


def replace_once(text, old, new, label):
    if old not in text:
        raise RuntimeError(f'missing expected text for {label}')
    if text.count(old) != 1:
        raise RuntimeError(f'expected one match for {label}, found {text.count(old)}')
    return text.replace(old, new, 1)


def replace_function(text, name, new_source):
    marker = f'function {name}('
    start = text.find(marker)
    if start < 0:
        marker = f'async function {name}('
        start = text.find(marker)
    if start < 0:
        raise RuntimeError(f'function {name} not found')
    brace = text.find('{', start)
    if brace < 0:
        raise RuntimeError(f'function {name} missing body')
    depth = 0
    quote = None
    escape = False
    template_depth = 0
    i = brace
    while i < len(text):
        ch = text[i]
        if quote:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif quote == '`' and ch == '$' and i + 1 < len(text) and text[i + 1] == '{':
                template_depth += 1
                i += 1
            elif ch == quote and template_depth == 0:
                quote = None
            elif quote == '`' and ch == '}' and template_depth:
                template_depth -= 1
        else:
            if ch in ('\"', "'", '`'):
                quote = ch
            elif ch == '/' and i + 1 < len(text) and text[i + 1] == '/':
                nl = text.find('\n', i + 2)
                if nl < 0:
                    i = len(text) - 1
                else:
                    i = nl
            elif ch == '/' and i + 1 < len(text) and text[i + 1] == '*':
                end = text.find('*/', i + 2)
                if end < 0:
                    raise RuntimeError(f'unclosed comment while parsing {name}')
                i = end + 1
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return text[:start] + new_source.rstrip() + text[i + 1:]
        i += 1
    raise RuntimeError(f'unclosed function {name}')


def remove_function(text, name):
    return replace_function(text, name, '')


def remove_script_line(text, filename):
    pattern = re.compile(r'^\s*<script[^>]+src="/' + re.escape(filename) + r'[^>]*></script>\s*\n?', re.M)
    text, count = pattern.subn('', text, count=1)
    if count != 1:
        raise RuntimeError(f'expected script {filename} exactly once, found {count}')
    return text


# ---------------------------------------------------------------------------
# index.html: make shell/page-heading structure direct and remove late owners.
# ---------------------------------------------------------------------------
path = 'frontend/static/index.html'
text = read(path)
text = text.replace('/favicon.svg?v=4', '/favicon.svg?v=6')
text = re.sub(r'^<link rel="icon" type="image/png" sizes="32x32"[^\n]*\n', '', text, count=1, flags=re.M)
text = text.replace('/apple-touch-icon.png?v=4', '/apple-touch-icon.png?v=5')
text = text.replace('/logo.svg?v=4', '/logo.svg?v=7')
text = replace_once(text, '      <div class="logo-ver" id="sidebar-version">v…</div>\n', '', 'sidebar version old location')
text = replace_once(
    text,
    '    <h1 id="page-title">Dashboard</h1>\n',
    '    <div class="dp-page-heading">\n'
    '      <h1 id="page-title">Dashboard</h1>\n'
    '      <p id="page-subtitle" class="dp-page-subtitle">Overview of your download activities and system status.</p>\n'
    '    </div>\n',
    'direct page heading',
)
text = replace_once(
    text,
    '<div id="mobile-overlay" onclick="closeSidebar()"></div>\n',
    '<div class="logo-ver dp-app-version" id="sidebar-version" aria-label="DebridPulse version">v…</div>\n'
    '<div id="mobile-overlay" onclick="closeSidebar()"></div>\n',
    'direct app version location',
)
for script in ('ui-shell-runtime.js', 'ui-help-chrome.js', 'ui-page-finalization.js'):
    text = remove_script_line(text, script)
write(path, text)


# ---------------------------------------------------------------------------
# app.js: direct theme semantics + explicit lifecycle events for dynamic work.
# ---------------------------------------------------------------------------
path = 'frontend/static/app.js'
text = read(path)
old = "  btn.textContent = isLight ? '☀︎' : '☾';"
text = replace_once(text, old, "  btn.textContent = isLight ? '☾' : '☀︎';", 'theme action icon semantics')

old = "  } finally {\n    setButtonPending(button, false);\n  }\n}\n\nasync function setLabel(id)"
new = "  } finally {\n    setButtonPending(button, false);\n    document.dispatchEvent(new CustomEvent('debridpulse:downloads-bulk-action-settled', {detail:{action}}));\n  }\n}\n\nasync function setLabel(id)"
text = replace_once(text, old, new, 'bulk action lifecycle')

old = "  } finally {\n    setButtonPending(button, false);\n  }\n}\n\nasync function loadAria2Runtime()"
new = "  } finally {\n    setButtonPending(button, false);\n    document.dispatchEvent(new CustomEvent('debridpulse:aria2-engine-action-settled', {detail:{gid, action}}));\n  }\n}\n\nasync function loadAria2Runtime()"
text = replace_once(text, old, new, 'aria2 action lifecycle')

old = "  if (!evs.length) { el.innerHTML='<div class=\"empty\">No events match the filter.</div>'; return; }"
new = "  if (!evs.length) {\n    el.innerHTML='<div class=\"empty\">No events match the filter.</div>';\n    document.dispatchEvent(new CustomEvent('debridpulse:activity-rendered'));\n    return;\n  }"
text = replace_once(text, old, new, 'empty activity lifecycle')
write(path, text)


# ---------------------------------------------------------------------------
# ui-runtime.js: static structure is direct; dynamic content uses app lifecycle.
# ---------------------------------------------------------------------------
path = 'frontend/static/ui-runtime.js'
text = read(path)
text = remove_function(text, 'loadV11Styles')
text = replace_function(text, 'ensurePageHeading', r'''function ensurePageHeading() {
    const title = document.getElementById('page-title');
    const subtitle = document.getElementById('page-subtitle');
    if (!title || !subtitle) return;
    subtitle.textContent = SUBTITLES[title.textContent.trim()] || '';
  }''')
text = replace_function(text, 'installMetricHistoryHook', r'''function installMetricHistoryHook() {
    if (document.documentElement.dataset.dpDashboardMetricLifecycle === '1') return;
    document.documentElement.dataset.dpDashboardMetricLifecycle = '1';
    document.addEventListener('debridpulse:dashboard-stats-rendered', function (event) {
      recordDashboardMetricHistory(event.detail && event.detail.stats);
    });
  }''')
text = re.sub(
    r"\n\s*if \(tbody && !tbody\.dataset\.dpStructuralObserved\) \{.*?\n\s*\}\n\s*updateRecentCount\(\);",
    "\n    updateRecentCount();",
    text,
    count=1,
    flags=re.S,
)
text = re.sub(
    r"\n\s*if \(!list\.dataset\.dpActivityObserved\) \{.*?\n\s*\}\n\s*\}\n\s*normalizeActivityRows\(\);",
    "\n    }\n    normalizeActivityRows();",
    text,
    count=1,
    flags=re.S,
)
text = text.replace(
    "copy.innerHTML = '<span class=\"dp-activity-heading\">Activity Log</span><span class=\"dp-activity-subtitle\">Recent transfer activity, decisions, warnings, and errors.</span>';",
    "copy.innerHTML = '<span class=\"dp-activity-heading\">Activity Log</span><span class=\"dp-activity-subtitle\">Everything DebridPulse thought was worth mentioning.</span>';",
)
text = text.replace('    loadV11Styles();\n', '')
text = replace_once(
    text,
    "  initialize();\n  document.addEventListener('DOMContentLoaded', initialize, {once: true});\n})();",
    "  initialize();\n"
    "  document.addEventListener('DOMContentLoaded', initialize, {once: true});\n"
    "  document.addEventListener('debridpulse:navigation', ensurePageHeading);\n"
    "  document.addEventListener('debridpulse:dashboard-recent-rendered', function () { updateRecentCount(); normalizeDashboardBadges(); });\n"
    "  document.addEventListener('debridpulse:activity-rendered', normalizeActivityRows);\n"
    "})();",
    'runtime lifecycle listeners',
)
write(path, text)


# ---------------------------------------------------------------------------
# Downloads: final header/bulk placement owned here; no correction observer.
# ---------------------------------------------------------------------------
path = 'frontend/static/ui-downloads-runtime.js'
text = read(path)
text = re.sub(
    r"\n\s*new MutationObserver\(function \(\) \{ syncBulkButtonPresentation\(bar\); \}\)\n\s*\.observe\(header, \{childList: true, subtree: true, characterData: true\}\);",
    '',
    text,
    count=1,
)
text = replace_function(text, 'trackedCopy', r'''function trackedCopy(count) {
    return count === 1
      ? '1 download tracked. It followed instructions.'
      : count + ' downloads tracked. Most of them followed instructions.';
  }''')
text = text.replace("title.setAttribute('aria-label', 'All Downloads. ' + copy + '.');", "title.setAttribute('aria-label', 'Download Queue. ' + copy);")
text = text.replace("'<span class=\"dp-downloads-heading\">All Downloads</span>'", "'<span class=\"dp-downloads-heading\">Download Queue</span>'")
text = text.replace("title.setAttribute('aria-label', 'All Downloads. ' + copy + '.');", "title.setAttribute('aria-label', 'Download Queue. ' + copy);")
old = "    ensureDownloadFilters();\n    normalizeDownloadRowActions();\n  }"
new = "    ensureDownloadFilters();\n    normalizeDownloadRowActions();\n\n    const bulk = document.getElementById('bulk-bar');\n    if (bulk && tableWrap && bulk.nextElementSibling !== tableWrap) {\n      bulk.classList.add('dp-downloads-bulk-integrated');\n      tableWrap.parentNode.insertBefore(bulk, tableWrap);\n    }\n  }"
text = replace_once(text, old, new, 'downloads bulk canonical placement')
text = replace_once(
    text,
    "  initialize();\n  document.addEventListener('DOMContentLoaded', initialize, {once: true});\n})();",
    "  initialize();\n"
    "  document.addEventListener('DOMContentLoaded', initialize, {once: true});\n"
    "  document.addEventListener('debridpulse:downloads-bulk-action-settled', function () { syncBulkButtonPresentation(document.getElementById('bulk-bar')); });\n"
    "})();",
    'downloads lifecycle listener',
)
write(path, text)


# ---------------------------------------------------------------------------
# Help: emit accepted chrome before insertion; legal buttons are direct.
# ---------------------------------------------------------------------------
path = 'frontend/static/ui-help-page.js'
text = read(path)
old_tabs = """  const TABS = Object.freeze([\n    ['quickstart', 'Quick Start'],\n    ['howitworks', 'How it works'],\n    ['aria2', 'aria2'],\n    ['integrations', 'Integrations'],\n    ['settings', 'Settings'],\n    ['trouble', 'Troubleshooting'],\n    ['license', 'License'],\n  ]);"""
new_tabs = """  const TABS = Object.freeze([\n    ['quickstart', 'Quick Start', 'rocket'],\n    ['howitworks', 'How it works', 'workflow'],\n    ['aria2', 'Download Engine', 'download'],\n    ['integrations', 'Integrations', 'plug'],\n    ['settings', 'Settings', 'settings'],\n    ['trouble', 'Troubleshooting', 'wrench'],\n    ['license', 'License', 'scale'],\n  ]);"""
text = replace_once(text, old_tabs, new_tabs, 'help canonical tabs')
text = text.replace(
    '<a class="dp-btn dp-btn--primary" href="https://github.com/Xipher-Zero/debridpulse/blob/main/LICENSE" target="_blank" rel="noopener">Read GPL-2.0-or-later</a>',
    '<button type="button" class="dp-btn dp-btn--primary dp-help-local-document-button" data-legal-document="gpl">Read GPL-2.0-or-later</button>',
)
text = text.replace(
    '<a class="dp-btn dp-btn--ghost" href="https://github.com/Xipher-Zero/debridpulse/blob/main/NOTICE" target="_blank" rel="noopener">Attribution notice</a>',
    '<button type="button" class="dp-btn dp-btn--ghost dp-help-local-document-button" data-legal-document="notice">Attribution notice</button>',
)
text = text.replace(
    '<a class="dp-btn dp-btn--ghost" href="https://github.com/Xipher-Zero/debridpulse/blob/main/LICENSES/MIT.txt" target="_blank" rel="noopener">Upstream MIT license</a>',
    '<button type="button" class="dp-btn dp-btn--ghost dp-help-local-document-button" data-legal-document="upstream-mit">Upstream MIT license</button>',
)
text = text.replace(
    '<a class="dp-btn dp-btn--ghost" href="https://github.com/Xipher-Zero/debridpulse/blob/main/SOURCE_OFFER.md" target="_blank" rel="noopener">Source offer</a>',
    '<button type="button" class="dp-btn dp-btn--ghost dp-help-local-document-button" data-legal-document="source-offer">Source offer</button>',
)
text = text.replace(
    '<a class="dp-btn dp-btn--ghost" href="https://github.com/Xipher-Zero/debridpulse/blob/main/docs/DEPENDENCY_LICENSES.md" target="_blank" rel="noopener">Third-party licenses</a>',
    '<button type="button" class="dp-btn dp-btn--ghost dp-help-local-document-button" data-legal-document="third-party">Third-party licenses</button>',
)

panel_marker = "  function panel(name, body) {"
insert = r'''  function canonicalDocumentMarkup(markup) {
    const template = document.createElement('template');
    template.innerHTML = String(markup || '').trim();
    const section = template.content.firstElementChild;
    if (!section || !section.classList.contains('dp-help-document')) return markup;

    section.classList.add('card', 'dp-help-section-card', 'dp-large-panel-surface');
    const heading = section.querySelector(':scope > .dp-help-section-heading');
    if (!heading) return template.innerHTML;

    const header = document.createElement('div');
    header.className = 'card-header dp-help-section-card-header';
    heading.before(header);
    header.appendChild(heading);

    const body = document.createElement('div');
    body.className = 'card-body dp-help-section-card-body';
    while (header.nextSibling) body.appendChild(header.nextSibling);
    section.appendChild(body);
    return template.innerHTML;
  }

'''
if panel_marker not in text:
    raise RuntimeError('help panel marker missing')
text = text.replace(panel_marker, insert + panel_marker, 1)
text = text.replace('        ${body}\n', '        ${canonicalDocumentMarkup(body)}\n', 1)
text = text.replace("if (!TABS.some(([id]) => id === name))", "if (!TABS.some(([id]) => id === name))")
text = text.replace("const tabs = TABS.map(([id, label]) => {", "const tabs = TABS.map(([id, label, icon]) => {")
old_tab_markup = """                aria-selected=\"${active ? 'true' : 'false'}\"\n                tabindex=\"${active ? '0' : '-1'}\">${label}</button>`;"""
new_tab_markup = """                aria-selected=\"${active ? 'true' : 'false'}\"\n                tabindex=\"${active ? '0' : '-1'}\">\n          <span class=\"dp-help-tab-chip\" aria-hidden=\"true\"><img class=\"dp-help-tab-glyph\" src=\"/icons/lucide/${icon}.svg\" alt=\"\"></span>\n          <span class=\"dp-help-tab-label\">${label}</span>\n        </button>`;"""
text = replace_once(text, old_tab_markup, new_tab_markup, 'help tab icon markup')
text = text.replace('      <section class="dp-card dp-help-master-card" aria-label="Help & Documentation">', '      <section class="dp-card dp-help-master-card dp-list-workspace-surface" aria-label="Help & Documentation">')
old_header = """          <div class=\"dp-help-header-copy\">\n            <img class=\"dp-help-title-icon\" src=\"/icons/dp/document.svg\" alt=\"\" aria-hidden=\"true\">\n            <div class=\"dp-help-header-title\">Help &amp; Documentation</div>\n          </div>"""
new_header = """          <div class=\"dp-help-header-copy\">\n            <img class=\"dp-help-title-icon\" src=\"/icons/dp/document.svg\" alt=\"\" aria-hidden=\"true\">\n            <div class=\"dp-help-header-text\">\n              <div class=\"dp-help-header-title\">Field Manual</div>\n              <div class=\"dp-help-header-subtitle\">When intuition fails.</div>\n            </div>\n          </div>"""
text = replace_once(text, old_header, new_header, 'help final title')
write(path, text)


# Legal-document runtime keeps only modal behavior; Help emits buttons directly.
path = 'frontend/static/ui-help-license-documents.js'
text = read(path)
start = text.find('  const DOCUMENT_PATHS')
end = text.find('  function focusableElements', start)
if start < 0 or end < 0:
    raise RuntimeError('help legal conversion block not found')
text = text[:start] + text[end:]
text = replace_function(text, 'enhance', r'''function enhance() {
    const view = helpRoot();
    if (!view || !view.querySelector('.dp-help-license-actions')) return false;
    bindEvents(view);
    view.dataset.dpHelpLegalDocumentsReady = '1';
    return true;
  }''')
write(path, text)


# ---------------------------------------------------------------------------
# Statistics: own master-card geometry, final copy, palette, and theme refresh.
# ---------------------------------------------------------------------------
path = 'frontend/static/ui-statistics.js'
text = read(path)
insert_marker = '  function applyPresentation(period) {'
insert = r'''  function ensureStatisticsArchitecture() {
    const view = document.getElementById('view-stats');
    const cards = document.getElementById('detail-stat-cards');
    const chart = document.getElementById('daily-chart');
    if (!view || !cards || !chart) return false;

    const chartCard = chart.closest('.dp-stats-chart');
    const breakdownCards = BREAKDOWNS.map(function (definition) {
      const body = document.getElementById(definition.id);
      return body && body.closest('.list-card');
    }).filter(Boolean);
    if (!chartCard || breakdownCards.length !== BREAKDOWNS.length) return false;

    let master = view.querySelector(':scope > .dp-statistics-master');
    if (!master) {
      master = document.createElement('section');
      master.className = 'card dp-statistics-master dp-list-workspace-surface';
      master.innerHTML =
        '<div class="card-header dp-statistics-master-header">' +
          '<div class="dp-statistics-header-copy">' +
            '<img class="dp-statistics-title-icon" src="/icons/dp/card-statistics.svg" alt="" aria-hidden="true">' +
            '<div><div class="dp-statistics-header-title">By the Numbers</div>' +
            '<div class="dp-statistics-header-subtitle">Because vibes are not a performance metric.</div></div>' +
          '</div>' +
          '<div class="dp-statistics-header-tabs"></div>' +
        '</div>' +
        '<div class="card-body dp-statistics-master-body"></div>';
      view.prepend(master);
    }

    const tabsHost = master.querySelector('.dp-statistics-header-tabs');
    const periodTabs = document.getElementById('stats-period-tabs');
    if (tabsHost && periodTabs && periodTabs.parentElement !== tabsHost) tabsHost.appendChild(periodTabs);

    const body = master.querySelector('.dp-statistics-master-body');
    let top = body.querySelector(':scope > .dp-statistics-top');
    if (!top) {
      top = document.createElement('div');
      top.className = 'dp-statistics-top';
      body.appendChild(top);
    }
    if (cards.parentElement !== top) top.appendChild(cards);
    if (chartCard.parentElement !== top) top.appendChild(chartCard);

    let breakdown = body.querySelector(':scope > .dp-statistics-breakdown-grid');
    if (!breakdown) {
      breakdown = document.createElement('div');
      breakdown.className = 'dp-statistics-breakdown-grid';
      body.appendChild(breakdown);
    }
    breakdownCards.forEach(function (card) {
      card.classList.add('dp-large-panel-surface');
      if (card.parentElement !== breakdown) breakdown.appendChild(card);
    });
    return true;
  }

  function applyChartPalette() {
    const chart = document.getElementById('daily-chart')?._ci;
    if (!chart || !chart.data || !chart.data.datasets || !chart.data.datasets[0]) return;
    const styles = getComputedStyle(document.body);
    const accent = styles.getPropertyValue('--accent').trim() || '#a855f7';
    chart.data.datasets[0].backgroundColor = 'color-mix(in srgb, ' + accent + ' 48%, transparent)';
    chart.data.datasets[0].borderColor = accent;
    chart.update('none');
  }

'''
if insert_marker not in text:
    raise RuntimeError('statistics presentation marker missing')
text = text.replace(insert_marker, insert + insert_marker, 1)
text = text.replace('  function applyPresentation(period) {\n    normalizePrimaryMetrics(period);', '  function applyPresentation(period) {\n    ensureStatisticsArchitecture();\n    normalizePrimaryMetrics(period);')
text = text.replace('    applySharedSurfaceClass();\n  }', '    applySharedSurfaceClass();\n    applyChartPalette();\n  }', 1)
text = replace_once(
    text,
    "  install();\n  if (document.readyState === 'loading') {\n    document.addEventListener('DOMContentLoaded', initialize, {once: true});\n  } else {\n    initialize();\n  }\n})();",
    "  install();\n"
    "  document.addEventListener('debridpulse:theme-changed', applyChartPalette);\n"
    "  document.addEventListener('debridpulse:navigation', function (event) { if (event.detail && event.detail.view === 'stats') applyPresentation(selectedPeriod()); });\n"
    "  if (document.readyState === 'loading') {\n"
    "    document.addEventListener('DOMContentLoaded', initialize, {once: true});\n"
    "  } else {\n"
    "    initialize();\n"
    "  }\n"
    "})();",
    'statistics lifecycle',
)
write(path, text)


# ---------------------------------------------------------------------------
# Settings owner: direct final shell semantics and lifecycle hook for subowners.
# ---------------------------------------------------------------------------
path = 'frontend/static/ui-settings-page.js'
text = read(path)
text = text.replace("    ['notifications', 'Notifications', 'bell'],\n    ['authentication', 'Authentication', 'shield-check'],", "    ['authentication', 'Authentication', 'shield-check'],\n    ['notifications', 'Notifications', 'bell'],")
text = text.replace('      <section class="card dp-settings-card ${options.className || \'\'}">', '      <section class="card dp-settings-card dp-large-panel-surface ${options.className || \'\'}">')
text = text.replace('      <section class="card dp-settings-group-card ${options.className || \'\'}">', '      <section class="card dp-settings-group-card dp-large-panel-surface ${options.className || \'\'}">')
text = text.replace('<div class="dp-settings-header-title">Settings</div>\n              <div class="dp-settings-header-subtitle">Configure providers, downloads, automation, authentication, and maintenance.</div>', '<div class="dp-settings-header-title">Tuning Deck</div>\n              <div class="dp-settings-header-subtitle">Your rules, your defaults.</div>')
text = text.replace("      notify('API token cleared', 'success');", "      notify('API token revoked', 'success');")
text = replace_once(
    text,
    '    updateModeState();\n  }\n\n  function activateTab(name)',
    "    updateModeState();\n    document.dispatchEvent(new CustomEvent('debridpulse:settings-rendered', {detail:{tab: state.activeTab}}));\n  }\n\n  function activateTab(name)",
    'settings rendered lifecycle',
)
write(path, text)


# Replace Settings structural MutationObservers with explicit render lifecycle.
def replace_settings_observer(path, old_tail_pattern, new_tail):
    text = read(path)
    text, count = re.subn(old_tail_pattern, new_tail, text, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f'failed replacing Settings observer tail in {path}')
    write(path, text)

replace_settings_observer(
    'frontend/static/ui-settings-authentication.js',
    r"\n\s*schedule\(\);\n\n\s*const view = document\.getElementById\('view-settings'\);.*?\n\s*\}\n\}\)\(\);\s*$",
    "\n\n  document.addEventListener('debridpulse:settings-rendered', schedule);\n})();\n",
)
replace_settings_observer(
    'frontend/static/ui-settings-authentication-polish.js',
    r"\n\s*schedule\(\);\n\n\s*const view = document\.getElementById\('view-settings'\);.*?\n\s*\}\n\}\)\(\);\s*$",
    "\n\n  document.addEventListener('debridpulse:settings-rendered', schedule);\n})();\n",
)
replace_settings_observer(
    'frontend/static/ui-settings-authentication-oidc.js',
    r"\n\s*schedule\(\);\n\n\s*const view = document\.getElementById\('view-settings'\);.*?\n\s*\}\n\}\)\(\);\s*$",
    "\n\n  document.addEventListener('debridpulse:settings-rendered', schedule);\n})();\n",
)
replace_settings_observer(
    'frontend/static/ui-settings-maintenance-wipe.js',
    r"\n\s*schedule\(\);\n\n\s*const view = document\.getElementById\('view-settings'\);.*?\n\s*\}\n\}\)\(\);\s*$",
    "\n\n  document.addEventListener('debridpulse:settings-rendered', schedule);\n})();\n",
)
replace_settings_observer(
    'frontend/static/ui-settings-notifications.js',
    r"\n\s*schedule\(\);\n\n\s*const view = document\.getElementById\('view-settings'\);.*?\n\s*\}\n\}\)\(\);\s*$",
    "\n\n  document.addEventListener('debridpulse:settings-rendered', schedule);\n})();\n",
)

# Callback module: keep delegated copy handler and draft input binding, trigger on render.
path = 'frontend/static/ui-settings-authentication-callback.js'
text = read(path)
text = re.sub(
    r"\n\s*const observer = new MutationObserver\(.*?\n\s*observer\.observe\(view, \{childList: true, subtree: true\}\);\n\n\s*schedule\(\);",
    "\n\n  document.addEventListener('debridpulse:settings-rendered', schedule);\n  schedule();",
    text,
    count=1,
    flags=re.S,
)
write(path, text)

# Card icons: apply after canonical Settings render only.
path = 'frontend/static/ui-settings-card-icons.js'
text = read(path)
text = re.sub(r"\n\s*let observer = null;\n\s*let scheduled = false;", "\n  let scheduled = false;", text, count=1)
for fn in ('observe', 'applyWithoutSelfObservation', 'bind'):
    text = remove_function(text, fn)
text = replace_function(text, 'scheduleApply', r'''function scheduleApply() {
    if (scheduled) return;
    scheduled = true;
    queueMicrotask(() => {
      scheduled = false;
      apply();
    });
  }''')
text = re.sub(
    r"\n\s*if \(document\.readyState === 'loading'\) \{.*?\n\s*\}\n\}\)\(\);\s*$",
    "\n\n  document.addEventListener('debridpulse:settings-rendered', scheduleApply);\n})();\n",
    text,
    count=1,
    flags=re.S,
)
write(path, text)

# Downloads/extraction completion: retain stateful editor, explicit Settings lifecycle.
path = 'frontend/static/ui-settings-downloads-completion.js'
text = read(path)
text = re.sub(r"\n\s*let scheduled = false;\n\s*let observer = null;", "\n  let scheduled = false;", text, count=1)
for fn in ('observe', 'applyWithoutSelfObservation', 'attach'):
    text = remove_function(text, fn)
text = replace_function(text, 'scheduleApply', r'''function scheduleApply() {
    if (scheduled) return;
    scheduled = true;
    queueMicrotask(() => {
      scheduled = false;
      apply();
    });
  }''')
text = re.sub(
    r"\n\s*if \(document\.readyState === 'loading'\) \{.*?\n\s*\}\n\}\)\(\);\s*$",
    "\n\n  document.addEventListener('debridpulse:settings-rendered', scheduleApply);\n})();\n",
    text,
    count=1,
    flags=re.S,
)
write(path, text)

# aria2 live: polling is real dynamic behavior; structural observation/global wrapping are not.
path = 'frontend/static/ui-settings-aria2-live.js'
text = read(path)
text = re.sub(r"\n\s*let observer = null;", '', text, count=1)
for fn in ('observe', 'applyWithoutSelfObservation', 'scheduleApply', 'wrapEngineActionRefresh'):
    text = remove_function(text, fn)
text = replace_function(text, 'attach', r'''function attach() {
    const view = root();
    if (!view) return;
    bindInteractions(view);
    apply();

    document.addEventListener('debridpulse:settings-rendered', apply);
    document.addEventListener('debridpulse:aria2-engine-action-settled', function () {
      if (shouldRunLiveQueue()) {
        void refreshQueue(false);
        schedulePoll(POLL_MS);
      }
    });
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) stopPolling();
      else startVisibleQueue();
    });
  }''')
write(path, text)


# ---------------------------------------------------------------------------
# Retire page-finalization CSS by moving each rule to its semantic owner.
# ---------------------------------------------------------------------------
settings_css = ROOT / 'frontend/static/ui-settings-page.css'
settings_css.write_text(settings_css.read_text() + r'''

/* Canonical Settings master subtitle. */
body.dp-v11-structural #view-settings .dp-settings-header-subtitle {
  font-size: 11px !important;
  line-height: 1.35;
}
body.dp-v11-structural #view-settings .dp-settings-header-subtitle::after { content: none !important; }
''')

help_css = ROOT / 'frontend/static/ui-help-page.css'
help_css.write_text(help_css.read_text() + r'''

/* Canonical Help master icon glow. */
body.dp-v11-structural #view-help .dp-help-title-icon {
  --dp-feature-icon-glow: #4c8fff;
  filter: drop-shadow(0 0 5px color-mix(in srgb, var(--dp-feature-icon-glow) 62%, transparent))
          drop-shadow(0 0 11px color-mix(in srgb, var(--dp-feature-icon-glow) 27%, transparent));
}
body.light.dp-v11-structural #view-help .dp-help-title-icon {
  filter: drop-shadow(0 0 6px color-mix(in srgb, var(--dp-feature-icon-glow) 70%, transparent))
          drop-shadow(0 0 15px color-mix(in srgb, var(--dp-feature-icon-glow) 32%, transparent));
}
''')

downloads_css = ROOT / 'frontend/static/ui-downloads-page.css'
downloads_css.write_text(downloads_css.read_text() + r'''

/* Canonical integrated multi-selection strip. */
body.dp-v11-structural #view-torrents #bulk-bar.dp-downloads-bulk-card.dp-downloads-bulk-integrated {
  margin: 0 !important;
  border-left: 0 !important;
  border-right: 0 !important;
  border-top: 0 !important;
  border-bottom: 1px solid var(--dp-divider) !important;
  border-radius: 0 !important;
  box-shadow: none !important;
  overflow: visible;
}
body.dp-v11-structural #view-torrents #bulk-bar.dp-downloads-bulk-card.dp-downloads-bulk-integrated > .dp-downloads-bulk-toolbar {
  min-height: 54px;
  border-radius: 0 !important;
}
body.dp-v11-structural #view-torrents #bulk-bar.dp-downloads-bulk-card.dp-downloads-bulk-integrated.visible {
  display: block !important;
}
''')

path = 'frontend/static/style-v11.css'
text = read(path)
text = text.replace("@import url('/ui-help-balance.css?v=2');", "@import url('/ui-help-license-balance.css?v=2');")
text = re.sub(r"^@import url\('/ui-page-finalization\.css[^\n]*\n", '', text, count=1, flags=re.M)
write(path, text)


# Files whose responsibilities are now direct or migrated.
for rel in (
    'frontend/static/ui-shell-runtime.js',
    'frontend/static/ui-help-chrome.js',
    'frontend/static/ui-page-finalization.js',
    'frontend/static/ui-page-finalization.css',
    'frontend/static/ui-visual-behavior-fixes.js',
):
    p = ROOT / rel
    if p.exists():
        p.unlink()

# Temporary prior audit machinery is not product architecture.
prior_audit = ROOT / '.github/workflows/frontend-audit-hotfix.yml'
if prior_audit.exists():
    prior_audit.unlink()

# Guard the central acceptance properties this pass is meant to establish.
index = read('frontend/static/index.html')
for forbidden in ('ui-shell-runtime.js', 'ui-help-chrome.js', 'ui-page-finalization.js'):
    if forbidden in index:
        raise RuntimeError(f'forbidden runtime remains loaded: {forbidden}')

for rel in (
    'frontend/static/ui-runtime.js',
    'frontend/static/ui-downloads-runtime.js',
    'frontend/static/ui-settings-authentication.js',
    'frontend/static/ui-settings-authentication-polish.js',
    'frontend/static/ui-settings-authentication-oidc.js',
    'frontend/static/ui-settings-authentication-callback.js',
    'frontend/static/ui-settings-maintenance-wipe.js',
    'frontend/static/ui-settings-notifications.js',
    'frontend/static/ui-settings-downloads-completion.js',
    'frontend/static/ui-settings-aria2-live.js',
    'frontend/static/ui-settings-card-icons.js',
):
    if 'MutationObserver' in read(rel):
        raise RuntimeError(f'correction observer remains in {rel}')

style = read('frontend/static/style-v11.css')
for match in re.finditer(r"@import url\('/([^?']+)", style):
    target = STATIC / match.group(1)
    if not target.exists():
        raise RuntimeError(f'broken style import: {match.group(1)}')

print('phase2 canonicalization applied')
