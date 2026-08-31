from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'frontend' / 'static'


def read(path):
    return (ROOT / path).read_text()


def write(path, text):
    (ROOT / path).write_text(text)


def replace_once(text, old, new, label):
    if text.count(old) != 1:
        raise RuntimeError(f'expected one match for {label}, found {text.count(old)}')
    return text.replace(old, new, 1)


def replace_function(text, name, new_source):
    for prefix in ('function ', 'async function '):
        start = text.find(prefix + name + '(')
        if start >= 0:
            break
    else:
        raise RuntimeError(f'function {name} not found')
    brace = text.find('{', start)
    depth = 0
    quote = None
    escape = False
    i = brace
    while i < len(text):
        ch = text[i]
        if quote:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == quote:
                quote = None
        else:
            if ch in ('\"', "'", '`'):
                quote = ch
            elif ch == '/' and i + 1 < len(text) and text[i + 1] == '/':
                nl = text.find('\n', i + 2)
                i = len(text) - 1 if nl < 0 else nl
            elif ch == '/' and i + 1 < len(text) and text[i + 1] == '*':
                end = text.find('*/', i + 2)
                if end < 0:
                    raise RuntimeError(f'unclosed comment in {name}')
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


# The first transformer intentionally stopped here on the phase1 lifecycle tail.
path = 'frontend/static/ui-downloads-runtime.js'
text = read(path)
old = """  initializeDownloadsPresentation();
  document.addEventListener('DOMContentLoaded', initializeDownloadsPresentation, {once: true});
  document.addEventListener('debridpulse:downloads-rendered', initializeDownloadsPresentation);
  document.addEventListener('debridpulse:dashboard-recent-rendered', decorateEmptyStates);
  document.addEventListener('debridpulse:dashboard-stats-rendered', decorateDownloadsHeader);
})();"""
new = """  initializeDownloadsPresentation();
  document.addEventListener('DOMContentLoaded', initializeDownloadsPresentation, {once: true});
  document.addEventListener('debridpulse:downloads-rendered', initializeDownloadsPresentation);
  document.addEventListener('debridpulse:dashboard-recent-rendered', decorateEmptyStates);
  document.addEventListener('debridpulse:dashboard-stats-rendered', decorateDownloadsHeader);
  document.addEventListener('debridpulse:downloads-bulk-action-settled', function () {
    syncBulkButtonPresentation(document.getElementById('bulk-bar'));
  });
})();"""
text = replace_once(text, old, new, 'downloads bulk lifecycle listener')
write(path, text)


# Help emits canonical structure directly from its own render transaction.
path = 'frontend/static/ui-help-page.js'
text = read(path)
old_tabs = """  const TABS = Object.freeze([
    ['quickstart', 'Quick Start'],
    ['howitworks', 'How it works'],
    ['aria2', 'aria2'],
    ['integrations', 'Integrations'],
    ['settings', 'Settings'],
    ['trouble', 'Troubleshooting'],
    ['license', 'License'],
  ]);"""
new_tabs = """  const TABS = Object.freeze([
    ['quickstart', 'Quick Start', 'rocket'],
    ['howitworks', 'How it works', 'workflow'],
    ['aria2', 'Download Engine', 'download'],
    ['integrations', 'Integrations', 'plug'],
    ['settings', 'Settings', 'settings'],
    ['trouble', 'Troubleshooting', 'wrench'],
    ['license', 'License', 'scale'],
  ]);"""
text = replace_once(text, old_tabs, new_tabs, 'help tabs')
replacements = {
    '<a class="dp-btn dp-btn--primary" href="https://github.com/Xipher-Zero/debridpulse/blob/main/LICENSE" target="_blank" rel="noopener">Read GPL-2.0-or-later</a>': '<button type="button" class="dp-btn dp-btn--primary dp-help-local-document-button" data-legal-document="gpl">Read GPL-2.0-or-later</button>',
    '<a class="dp-btn dp-btn--ghost" href="https://github.com/Xipher-Zero/debridpulse/blob/main/NOTICE" target="_blank" rel="noopener">Attribution notice</a>': '<button type="button" class="dp-btn dp-btn--ghost dp-help-local-document-button" data-legal-document="notice">Attribution notice</button>',
    '<a class="dp-btn dp-btn--ghost" href="https://github.com/Xipher-Zero/debridpulse/blob/main/LICENSES/MIT.txt" target="_blank" rel="noopener">Upstream MIT license</a>': '<button type="button" class="dp-btn dp-btn--ghost dp-help-local-document-button" data-legal-document="upstream-mit">Upstream MIT license</button>',
    '<a class="dp-btn dp-btn--ghost" href="https://github.com/Xipher-Zero/debridpulse/blob/main/SOURCE_OFFER.md" target="_blank" rel="noopener">Source offer</a>': '<button type="button" class="dp-btn dp-btn--ghost dp-help-local-document-button" data-legal-document="source-offer">Source offer</button>',
    '<a class="dp-btn dp-btn--ghost" href="https://github.com/Xipher-Zero/debridpulse/blob/main/docs/DEPENDENCY_LICENSES.md" target="_blank" rel="noopener">Third-party licenses</a>': '<button type="button" class="dp-btn dp-btn--ghost dp-help-local-document-button" data-legal-document="third-party">Third-party licenses</button>',
}
for old_link, new_button in replacements.items():
    text = replace_once(text, old_link, new_button, 'help legal action')

marker = '  function panel(name, body) {'
helper = r'''  function canonicalDocumentMarkup(markup) {
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
if marker not in text:
    raise RuntimeError('help panel marker missing')
text = text.replace(marker, helper + marker, 1)
text = replace_once(text, '        ${body}\n', '        ${canonicalDocumentMarkup(body)}\n', 'help detached document canonicalization')
text = replace_once(text, 'const tabs = TABS.map(([id, label]) => {', 'const tabs = TABS.map(([id, label, icon]) => {', 'help tab renderer signature')
old_markup = """                aria-selected="${active ? 'true' : 'false'}"
                tabindex="${active ? '0' : '-1'}">${label}</button>`;"""
new_markup = """                aria-selected="${active ? 'true' : 'false'}"
                tabindex="${active ? '0' : '-1'}">
          <span class="dp-help-tab-chip" aria-hidden="true"><img class="dp-help-tab-glyph" src="/icons/lucide/${icon}.svg" alt=""></span>
          <span class="dp-help-tab-label">${label}</span>
        </button>`;"""
text = replace_once(text, old_markup, new_markup, 'help tab markup')
text = replace_once(text, '      <section class="dp-card dp-help-master-card" aria-label="Help & Documentation">', '      <section class="dp-card dp-help-master-card dp-list-workspace-surface" aria-label="Help & Documentation">', 'help shared surface')
old_header = """          <div class="dp-help-header-copy">
            <img class="dp-help-title-icon" src="/icons/dp/document.svg" alt="" aria-hidden="true">
            <div class="dp-help-header-title">Help &amp; Documentation</div>
          </div>"""
new_header = """          <div class="dp-help-header-copy">
            <img class="dp-help-title-icon" src="/icons/dp/document.svg" alt="" aria-hidden="true">
            <div class="dp-help-header-text">
              <div class="dp-help-header-title">Field Manual</div>
              <div class="dp-help-header-subtitle">When intuition fails.</div>
            </div>
          </div>"""
text = replace_once(text, old_header, new_header, 'help final header')
write(path, text)


# Legal runtime is modal behavior only. Help owns its action buttons.
path = 'frontend/static/ui-help-license-documents.js'
text = read(path)
start = text.find('  const DOCUMENT_PATHS')
end = text.find('  function focusableElements', start)
if start < 0 or end < 0:
    raise RuntimeError('legal conversion block missing')
text = text[:start] + text[end:]
text = replace_function(text, 'enhance', r'''function enhance() {
    const view = helpRoot();
    if (!view || !view.querySelector('.dp-help-license-actions')) return false;
    bindEvents(view);
    view.dataset.dpHelpLegalDocumentsReady = '1';
    return true;
  }''')
write(path, text)


# Statistics owns the final master architecture and chart presentation.
path = 'frontend/static/ui-statistics.js'
text = read(path)
marker = '  function applyPresentation(period) {'
helper = r'''  function ensureStatisticsArchitecture() {
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
    chart.data.datasets[0].borderColor = accent;
    chart.data.datasets[0].backgroundColor = accent;
    chart.data.datasets[0].fill = true;
    chart.update('none');
  }

'''
if marker not in text:
    raise RuntimeError('statistics marker missing')
text = text.replace(marker, helper + marker, 1)
text = replace_once(text, '  function applyPresentation(period) {\n    normalizePrimaryMetrics(period);', '  function applyPresentation(period) {\n    ensureStatisticsArchitecture();\n    normalizePrimaryMetrics(period);', 'statistics architecture ownership')
text = replace_once(text, '    applySharedSurfaceClass();\n  }', '    applySharedSurfaceClass();\n    applyChartPalette();\n  }', 'statistics palette')
old_tail = """  install();
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initialize, {once: true});
  } else {
    initialize();
  }
})();"""
new_tail = """  install();
  document.addEventListener('debridpulse:theme-changed', applyChartPalette);
  document.addEventListener('debridpulse:navigation', function (event) {
    if (event.detail && event.detail.view === 'stats') applyPresentation(selectedPeriod());
  });
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initialize, {once: true});
  } else {
    initialize();
  }
})();"""
text = replace_once(text, old_tail, new_tail, 'statistics lifecycle')
write(path, text)


# Settings owner emits final shell classes and an explicit render lifecycle.
path = 'frontend/static/ui-settings-page.js'
text = read(path)
text = replace_once(text, "    ['notifications', 'Notifications', 'bell'],\n    ['authentication', 'Authentication', 'shield-check'],", "    ['authentication', 'Authentication', 'shield-check'],\n    ['notifications', 'Notifications', 'bell'],", 'settings tab order')
text = text.replace('      <section class="card dp-settings-card ${options.className || \'\'}">', '      <section class="card dp-settings-card dp-large-panel-surface ${options.className || \'\'}">')
text = text.replace('      <section class="card dp-settings-group-card ${options.className || \'\'}">', '      <section class="card dp-settings-group-card dp-large-panel-surface ${options.className || \'\'}">')
text = replace_once(text, '<div class="dp-settings-header-title">Settings</div>\n              <div class="dp-settings-header-subtitle">Configure providers, downloads, automation, authentication, and maintenance.</div>', '<div class="dp-settings-header-title">Tuning Deck</div>\n              <div class="dp-settings-header-subtitle">Your rules, your defaults.</div>', 'settings final header')
text = text.replace("      notify('API token cleared', 'success');", "      notify('API token revoked', 'success');")
text = replace_once(text, '    updateModeState();\n  }\n\n  function activateTab(name)', "    updateModeState();\n    document.dispatchEvent(new CustomEvent('debridpulse:settings-rendered', {detail:{tab: state.activeTab}}));\n  }\n\n  function activateTab(name)", 'settings rendered event')
write(path, text)


def replace_settings_observer(path, pattern, replacement):
    text = read(path)
    text, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f'failed observer replacement in {path}')
    write(path, text)

for rel in (
    'frontend/static/ui-settings-authentication.js',
    'frontend/static/ui-settings-authentication-polish.js',
    'frontend/static/ui-settings-authentication-oidc.js',
    'frontend/static/ui-settings-maintenance-wipe.js',
    'frontend/static/ui-settings-notifications.js',
):
    replace_settings_observer(
        rel,
        r"\n\s*schedule\(\);\n\n\s*const view = document\.getElementById\('view-settings'\);.*?\n\s*\}\n\}\)\(\);\s*$",
        "\n\n  document.addEventListener('debridpulse:settings-rendered', schedule);\n})();\n",
    )

path = 'frontend/static/ui-settings-authentication-callback.js'
text = read(path)
text, count = re.subn(
    r"\n\s*const observer = new MutationObserver\(.*?\n\s*observer\.observe\(view, \{childList: true, subtree: true\}\);\n\n\s*schedule\(\);",
    "\n\n  document.addEventListener('debridpulse:settings-rendered', schedule);\n  schedule();",
    text,
    count=1,
    flags=re.S,
)
if count != 1:
    raise RuntimeError('callback observer replacement failed')
write(path, text)

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
text, count = re.subn(r"\n\s*if \(document\.readyState === 'loading'\) \{.*?\n\s*\}\n\}\)\(\);\s*$", "\n\n  document.addEventListener('debridpulse:settings-rendered', scheduleApply);\n})();\n", text, count=1, flags=re.S)
if count != 1:
    raise RuntimeError('card icon lifecycle replacement failed')
write(path, text)

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
text, count = re.subn(r"\n\s*if \(document\.readyState === 'loading'\) \{.*?\n\s*\}\n\}\)\(\);\s*$", "\n\n  document.addEventListener('debridpulse:settings-rendered', scheduleApply);\n})();\n", text, count=1, flags=re.S)
if count != 1:
    raise RuntimeError('downloads completion lifecycle replacement failed')
write(path, text)

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


# Move page-finalization CSS to semantic owners, then retire the late file.
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
text = replace_once(text, "@import url('/ui-help-balance.css?v=2');", "@import url('/ui-help-license-balance.css?v=2');", 'broken Help balance import')
text, count = re.subn(r"^@import url\('/ui-page-finalization\.css[^\n]*\n", '', text, count=1, flags=re.M)
if count != 1:
    raise RuntimeError('page finalization CSS import missing')
write(path, text)

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

prior_audit = ROOT / '.github/workflows/frontend-audit-hotfix.yml'
if prior_audit.exists():
    prior_audit.unlink()

# Acceptance guards for this intermediate pass.
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

print('phase2 resume canonicalization applied')
