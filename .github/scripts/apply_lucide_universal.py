from __future__ import annotations

from pathlib import Path
import re
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
TESTS = ROOT / "backend" / "tests"
PIN = "23f9abc4ed0146cffededd3d7f94c1018bfdf693"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write(path: Path, text: str) -> None:
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def replace_exact(path: Path, old: str, new: str, count: int = 1) -> None:
    text = read(path)
    actual = text.count(old)
    if actual != count:
        raise SystemExit(f"{path}: expected {count} occurrence(s) of {old!r}, found {actual}")
    write(path, text.replace(old, new))


def replace_regex(path: Path, pattern: str, replacement: str, count: int = 1, flags: int = 0) -> None:
    text = read(path)
    updated, actual = re.subn(pattern, replacement, text, count=count, flags=flags)
    if actual != count:
        raise SystemExit(f"{path}: expected {count} regex replacement(s) for {pattern!r}, found {actual}")
    write(path, updated)


def fetch_lucide(name: str) -> str:
    url = f"https://raw.githubusercontent.com/lucide-icons/lucide/{PIN}/icons/{name}.svg"
    with urllib.request.urlopen(url, timeout=20) as response:
        raw = response.read().decode("utf-8")
    match = re.search(r"<svg\b[^>]*>(.*)</svg>", raw, flags=re.S)
    if not match:
        raise SystemExit(f"Could not extract Lucide geometry for {name}")
    geometry = re.sub(r"\s+", " ", match.group(1)).strip()
    if not geometry or "<script" in geometry.lower() or "<image" in geometry.lower():
        raise SystemExit(f"Unsafe/empty Lucide geometry for {name}")
    return geometry


icon_names = [
    "activity", "arrow-right", "bell", "camera", "chart-no-axes-column-increasing",
    "chevron-down", "chevron-left", "chevron-right", "circle-check", "circle-help",
    "circle-x", "clock-3", "database", "database-backup", "download", "globe", "import",
    "info", "key", "layout-dashboard", "link", "list", "loader-circle", "lock-keyhole",
    "menu", "monitor", "moon", "package", "package-open", "pause", "play", "plus",
    "refresh-cw", "save", "search", "send", "settings", "shield-check", "sun", "tag",
    "trash-2", "triangle-alert", "upload", "user-round", "wrench", "x", "zap",
]
geometry = {name: fetch_lucide(name) for name in icon_names}

js_items = ",\n".join(
    f"    {name!r}: {geometry[name]!r}" for name in sorted(geometry)
)

lucide_runtime = f'''/* DebridPulse v1.0.11 canonical Lucide runtime.
 * Geometry is locally vendored from lucide-icons/lucide commit {PIN}.
 * Licenses: licenses/Lucide-ISC-MIT.txt.
 *
 * This is the single ordinary-UI glyph source for navigation, transfer status,
 * notifications, utility controls, settings navigation, and action buttons.
 * Prominent DebridPulse card/brand artwork remains intentionally custom.
 */
(function () {{
  'use strict';

  const GEOMETRY = Object.freeze({{
{js_items}
  }});

  const ALIASES = Object.freeze({{
    dashboard: 'layout-dashboard',
    logs: 'list',
    statistics: 'chart-no-axes-column-increasing',
    help: 'circle-help',
    chevronDown: 'chevron-down',
    chevronLeft: 'chevron-left',
    chevronRight: 'chevron-right',
    arrowRight: 'arrow-right',
    refresh: 'refresh-cw',
    trash: 'trash-2'
  }});

  const STATUS = Object.freeze({{
    pending:                 {{label:'Pending', icon:'clock-3'}},
    uploading:               {{label:'Uploading', icon:'upload'}},
    processing:              {{label:'Processing', icon:'loader-circle'}},
    extracting:              {{label:'Extracting', icon:'package-open'}},
    queued:                  {{label:'Queued', icon:'clock-3'}},
    paused:                  {{label:'Paused', icon:'pause'}},
    downloading:             {{label:'Downloading', icon:'download'}},
    ready:                   {{label:'Ready', icon:'play'}},
    completed:               {{label:'Done', icon:'circle-check'}},
    downloading_with_errors: {{label:'Downloading', icon:'triangle-alert'}},
    completed_with_errors:   {{label:'Completed with errors', icon:'triangle-alert'}},
    error:                   {{label:'Error', icon:'x'}},
    missing:                 {{label:'Missing file', icon:'x'}},
    provider_failed:         {{label:'Provider download failed', icon:'x'}},
    provider_missing:        {{label:'Removed from provider', icon:'x'}},
    failed:                  {{label:'Provider download failed', icon:'x'}},
    deleted:                 {{label:'Deleted', icon:'trash-2'}},
    imported:                {{label:'Imported', icon:'import'}},
    partial:                 {{label:'Partial', icon:'triangle-alert'}}
  }});

  function resolve(name) {{
    return ALIASES[name] || name;
  }}

  function lucideSvg(name, extraClass) {{
    const resolved = resolve(name);
    const geometry = GEOMETRY[resolved];
    if (!geometry) return '';
    const cls = ['lucide', 'dp-utility-icon', extraClass || ''].filter(Boolean).join(' ');
    return '<svg class="' + cls + '" data-dp-lucide="' + resolved + '" xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' + geometry + '</svg>';
  }}

  function statusDefinition(status) {{
    const key = String(status || '').trim();
    return STATUS[key] || {{label: key || 'Unknown', icon: 'info'}};
  }}

  function statusIcon(status, extraClass) {{
    return lucideSvg(statusDefinition(status).icon, extraClass || 'dp-status-icon');
  }}

  function stripLegacyGlyphPrefix(value) {{
    return String(value || '')
      .replace(/^[\\s\\u2000-\\u2BFF\\u{1F000}-\\u{1FAFF}]+/u, '')
      .trim();
  }}

  function decorateButton(button, iconName, options) {{
    if (!button || button.dataset.pending === '1' || button.getAttribute('aria-busy') === 'true') return;
    const opts = options || {{}};
    const iconOnly = !!opts.iconOnly;
    const fallback = opts.label != null ? String(opts.label) : stripLegacyGlyphPrefix(button.textContent);
    const label = button.dataset.defaultLabel || fallback;
    const resolved = resolve(iconName);
    const current = button.querySelector('[data-dp-lucide="' + resolved + '"]');
    const currentLabel = button.querySelector('[data-dp-lucide-label]');
    if (current && (iconOnly || (currentLabel && currentLabel.textContent === label))) return;

    button.textContent = '';
    button.insertAdjacentHTML('beforeend', lucideSvg(iconName, 'dp-button-icon'));
    if (!iconOnly && label) {{
      const span = document.createElement('span');
      span.dataset.dpLucideLabel = '1';
      span.textContent = label;
      button.appendChild(span);
      button.dataset.defaultLabel = label;
    }}
    if (iconOnly && !button.getAttribute('aria-label')) {{
      button.setAttribute('aria-label', opts.label || button.title || iconName);
    }}
    button.dataset.dpLucideButton = resolved;
  }}

  const BUTTON_RULES = [
    ['#btn-test-alldebrid', 'key', 'Test AllDebrid'],
    ['#btn-test-aria2', 'download', 'Test Aria2'],
    ['#btn-test-discord', 'bell', 'Test Discord'],
    ['#btn-save-settings', 'save', 'Save Settings'],
    ['button[onclick*="triggerFullSync("]', 'refresh-cw', 'Full AllDebrid Sync Now'],
    ['button[onclick*="runDeepSync("]', 'search', 'Run Deep Sync Now'],
    ['button[onclick*="clearDiscordAvatar("]', 'x', 'Remove'],
    ['button[onclick*="loadComprehensiveStats("]', 'chart-no-axes-column-increasing', 'Load Report'],
    ['button[onclick*="exportStats("]', 'download', 'Export JSON'],
    ['button[onclick*="triggerStatsSnapshot("]', 'camera', 'Snapshot Now'],
    ['button[onclick*="sendStatsReport("]', 'send', 'Send Webhook Now'],
    ['button[onclick*="triggerBackup("]', 'database-backup', 'Run Backup Now'],
    ['button[onclick*="loadBackupList("]', 'list', 'List Backups'],
    ['button[onclick*="triggerDatabaseBackup("]', 'database-backup', 'Run DB Backup Now'],
    ['button[onclick*="loadDatabaseBackupList("]', 'list', 'List DB Backups'],
    ['button[onclick*="wipeDatabase("]', 'trash-2', 'Wipe Database'],
    ['button[onclick*="addExtractionPassword("]', 'plus', 'Add password'],
    ['button[onclick*="removeExtractionPassword("]', 'x', 'Remove'],
    ['button[onclick*="loadAria2QueueView("]', 'refresh-cw', 'Refresh'],
    ['button[onclick*="runAria2Housekeeping("]', 'trash-2', null]
  ];

  function refreshKnownButtons(root) {{
    const host = root && root.querySelectorAll ? root : document;
    BUTTON_RULES.forEach(function (rule) {{
      host.querySelectorAll(rule[0]).forEach(function (button) {{
        decorateButton(button, rule[1], {{label: rule[2]}});
      }});
    }});

    host.querySelectorAll('button[data-act]').forEach(function (button) {{
      const action = String(button.dataset.act || '');
      if (action === 'pause') decorateButton(button, 'pause', {{iconOnly:true, label:'Pause'}});
      else if (action === 'resume') decorateButton(button, 'play', {{iconOnly:true, label:'Resume'}});
      else if (action === 'remove') decorateButton(button, 'trash-2', {{iconOnly:true, label:'Remove'}});
    }});
  }}

  window.dpLucideSvg = lucideSvg;
  window.dpLucideStatusDefinition = statusDefinition;
  window.dpLucideStatusIcon = statusIcon;
  window.dpLucideDecorateButton = decorateButton;
  window.dpRefreshLucideButtons = refreshKnownButtons;
  window.DP_LUCIDE_PIN = {PIN!r};

  function initialize() {{
    refreshKnownButtons(document);
    const body = document.body;
    if (!body || body.dataset.dpLucideObserved === '1') return;
    body.dataset.dpLucideObserved = '1';
    new MutationObserver(function (records) {{
      for (const record of records) {{
        for (const node of record.addedNodes) {{
          if (node.nodeType === 1) refreshKnownButtons(node.matches && node.matches('button') ? node.parentElement || document : node);
        }}
      }}
    }}).observe(body, {{childList:true, subtree:true}});
  }}

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize, {{once:true}});
  else initialize();
}})();
'''
write(STATIC / "ui-lucide-runtime.js", lucide_runtime)

lucide_css = '''/* DebridPulse v1.0.11 universal Lucide presentation contract. */
body.dp-v11-structural .dp-button-icon {
  --dp-icon-size: 16px;
  stroke-width: 2;
}
body.dp-v11-structural .btn > .dp-button-icon + [data-dp-lucide-label],
body.dp-v11-structural .btn > .dp-button-icon + span {
  min-width: 0;
}
body.dp-v11-structural .dp-status-icon {
  --dp-icon-size: 15px;
  width: 15px;
  height: 15px;
  stroke-width: 2;
}
body.dp-v11-structural .dp-status-label {
  line-height: 1;
}
body.dp-v11-structural .toast .dp-toast-icon-wrap {
  display: inline-grid;
  place-items: center;
  width: 20px;
  min-width: 20px;
  height: 20px;
}
body.dp-v11-structural .toast .dp-toast-icon {
  --dp-icon-size: 18px;
  width: 18px;
  height: 18px;
  stroke-width: 2;
}
body.dp-v11-structural .toast.success .dp-toast-icon-wrap { color: var(--dp-state-success); }
body.dp-v11-structural .toast.error .dp-toast-icon-wrap { color: var(--dp-state-error); }
body.dp-v11-structural .toast.warn .dp-toast-icon-wrap { color: var(--dp-state-caution); }
body.dp-v11-structural .toast.info .dp-toast-icon-wrap { color: var(--dp-state-active); }
body.dp-v11-structural .stab .dp-tab-icon {
  --dp-icon-size: 16px;
  width: 16px;
  height: 16px;
  stroke-width: 2;
}
body.dp-v11-structural .auth-status-icon .lucide,
body.dp-v11-structural .auth-origin-header .lucide {
  width: 20px;
  height: 20px;
  stroke-width: 2;
}
body.dp-v11-structural #aria2-speed-icon {
  display: inline-flex;
  align-items: center;
  color: var(--green);
}
body.dp-v11-structural #aria2-speed-icon .lucide {
  width: 14px;
  height: 14px;
}
'''
write(STATIC / "ui-lucide-iconography.css", lucide_css)

app = STATIC / "app.js"
replace_exact(
    app,
    "function toast(msg, type = 'info') {\n  const icons = {success:'✅',error:'❌',warn:'⚠️',info:'ℹ️'};\n  const el = document.createElement('div');\n  el.className = `toast ${type}`;\n  const icon = document.createElement('span');\n  icon.textContent = icons[type] || '·';\n  const text = document.createElement('span');\n  text.textContent = String(msg ?? '');\n  el.append(icon, text);\n  document.getElementById('toasts').appendChild(el);\n  setTimeout(() => el.style.opacity = '0', 3000);\n  setTimeout(() => el.remove(), 3400);\n}",
    "function toast(msg, type = 'info') {\n  const iconNames = {success:'circle-check',error:'circle-x',warn:'triangle-alert',info:'info'};\n  const el = document.createElement('div');\n  el.className = `toast ${type}`;\n  const icon = document.createElement('span');\n  icon.className = 'dp-toast-icon-wrap';\n  icon.setAttribute('aria-hidden', 'true');\n  if (typeof window.dpLucideSvg === 'function') {\n    icon.innerHTML = window.dpLucideSvg(iconNames[type] || 'info', 'dp-toast-icon');\n  }\n  const text = document.createElement('span');\n  text.textContent = String(msg ?? '');\n  el.append(icon, text);\n  document.getElementById('toasts').appendChild(el);\n  setTimeout(() => el.style.opacity = '0', 3000);\n  setTimeout(() => el.remove(), 3400);\n}"
)

replace_regex(
    app,
    r"function badge\(s\) \{.*?\n\}\nfunction transferDisplayStatus",
    """function badge(s) {
  const key = String(s || '');
  const requestedCls = key === 'missing' || key === 'provider_failed' || key === 'provider_missing' || key === 'failed'
    ? 'error'
    : key === 'completed_with_errors' || key === 'downloading_with_errors'
      ? 'partial'
      : key;
  const cls = /^[a-z0-9_-]+$/i.test(requestedCls) ? requestedCls : 'unknown';
  const definition = typeof window.dpLucideStatusDefinition === 'function'
    ? window.dpLucideStatusDefinition(key)
    : {label: key || 'Unknown', icon: 'info'};
  const icon = typeof window.dpLucideStatusIcon === 'function'
    ? window.dpLucideStatusIcon(key, 'dp-status-icon')
    : '';
  return `<span class="badge badge-${cls}" data-dp-status="${esc(key)}">${icon}<span class="dp-status-label">${esc(definition.label)}</span></span>`;
}
function transferDisplayStatus""",
    flags=re.S,
)

# Remove legacy glyphs from button templates; runtime decorators supply Lucide.
button_replacements = {
    '>⏸ Pause</button>': '>Pause</button>',
    '>▶ Resume</button>': '>Resume</button>',
    '>⬇ Now</button>': '>Now</button>',
    '>⏸</button>': '>Pause</button>',
    '>▶</button>': '>Resume</button>',
    '>↻</button>': '>Retry</button>',
    '>✕</button>': '>Remove</button>',
    '>🔄 Full AllDebrid Sync Now</button>': '>Full AllDebrid Sync Now</button>',
    '>🔍 Run Deep Sync Now</button>': '>Run Deep Sync Now</button>',
    '>✕ Remove</button>': '>Remove</button>',
    '>📊 Load Report</button>': '>Load Report</button>',
    '>⬇ Export JSON</button>': '>Export JSON</button>',
    '>📸 Snapshot Now</button>': '>Snapshot Now</button>',
    '>📨 Send Webhook Now</button>': '>Send Webhook Now</button>',
    '>💾 Run Backup Now</button>': '>Run Backup Now</button>',
    '>📋 List Backups</button>': '>List Backups</button>',
    '>💽 Run DB Backup Now</button>': '>Run DB Backup Now</button>',
    '>📋 List DB Backups</button>': '>List DB Backups</button>',
    '>🗑️ Wipe Database</button>': '>Wipe Database</button>',
}
text = read(app)
for old, new in button_replacements.items():
    if old in text:
        text = text.replace(old, new)
write(app, text)

# Metadata glyphs become Lucide too.
replace_exact(app, "${t.source === 'direct_link' ? `<div class=\"t-hash\" style=\"font-size:10px;color:var(--text3)\" title=\"Direct debrid link transfer\">🔗 Direct link</div>` : ''}", "${t.source === 'direct_link' ? `<div class=\"t-hash\" style=\"font-size:10px;color:var(--text3)\" title=\"Direct debrid link transfer\">${window.dpLucideSvg ? window.dpLucideSvg('link','dp-icon--xs') : ''} Direct link</div>` : ''}")
replace_exact(app, "${t.label?`<span class=\"lbl-badge\">🏷 ${esc(t.label)}</span>`:''}", "${t.label?`<span class=\"lbl-badge\">${window.dpLucideSvg ? window.dpLucideSvg('tag','dp-icon--xs') : ''}${esc(t.label)}</span>`:''}")

# Settings tab navigation becomes Lucide rather than emoji.
replace_exact(
    app,
    "  const tabs = [\n    { id:'tab-general',       label:'⚡ General' },\n    { id:'tab-download',      label:'⬇️ Download' },\n    { id:'tab-extract',       label:'📦 Extract' },\n    { id:'tab-notifications', label:'🔔 Notifications' },\n    { id:'tab-database',      label:'🗄 Database' },\n    { id:'tab-advanced',      label:'🛠️ Advanced' },\n  ];\n  document.getElementById('settings-tabs').innerHTML = tabs.map((t,i)=>\n    `<div class=\"stab${i===0?' active':''}\" data-tab=\"${t.id}\" onclick=\"switchSettingsTab('${t.id}')\">${t.label}</div>`\n  ).join('');",
    "  const tabs = [\n    { id:'tab-general',       label:'General',       icon:'zap' },\n    { id:'tab-download',      label:'Download',      icon:'download' },\n    { id:'tab-extract',       label:'Extract',       icon:'package-open' },\n    { id:'tab-notifications', label:'Notifications', icon:'bell' },\n    { id:'tab-database',      label:'Database',      icon:'database' },\n    { id:'tab-advanced',      label:'Advanced',      icon:'wrench' },\n  ];\n  document.getElementById('settings-tabs').innerHTML = tabs.map((t,i)=>\n    `<div class=\"stab${i===0?' active':''}\" data-tab=\"${t.id}\" onclick=\"switchSettingsTab('${t.id}')\">${window.dpLucideSvg ? window.dpLucideSvg(t.icon,'dp-tab-icon') : ''}<span>${t.label}</span></div>`\n  ).join('');"
)

# Success glyph inside a toast message is redundant after the toast gets Circle Check.
replace_exact(app, "toast(`Deep sync done in ${r.elapsed_seconds}s ✓`, 'success');", "toast(`Deep sync done in ${r.elapsed_seconds}s`, 'success');")
replace_exact(app, "`aria2: ${r.version||'online'} ✓`,", "`aria2: ${r.version||'online'}`,", count=1)

# After async pending text is cleared, restore canonical button decoration.
replace_exact(
    app,
    "  if (button.dataset.defaultLabel) {\n    button.textContent = button.dataset.defaultLabel;\n  }\n}",
    "  if (button.dataset.defaultLabel) {\n    button.textContent = button.dataset.defaultLabel;\n  }\n  if (typeof window.dpRefreshLucideButtons === 'function') {\n    queueMicrotask(() => window.dpRefreshLucideButtons(document));\n  }\n}"
)

# Centralize local runtime SVG helpers.
operator = STATIC / "operator-title.js"
replace_regex(
    operator,
    r"  const LUCIDE = \{.*?\n  \};\n\n  function lucideSvg\(name, extraClass\) \{.*?\n  \}",
    "  function lucideSvg(name, extraClass) {\n    return typeof window.dpLucideSvg === 'function' ? window.dpLucideSvg(name, extraClass) : '';\n  }",
    flags=re.S,
)
replace_exact(
    operator,
    "    help: 'help'",
    "    help: 'help'"
)
replace_exact(
    operator,
    "  function decorateAria2CapChevron() {",
    "  function decorateAria2SpeedIcon() {\n    const holder = document.getElementById('aria2-speed-icon');\n    if (holder) holder.innerHTML = lucideSvg('download');\n  }\n\n  function decorateAria2CapChevron() {"
)
replace_exact(operator, "    decorateTopbarActions();\n    bindThemeToggle();", "    decorateTopbarActions();\n    decorateAria2SpeedIcon();\n    bindThemeToggle();")

ui_runtime = STATIC / "ui-runtime.js"
replace_regex(
    ui_runtime,
    r"  function utilitySvg\(kind\) \{.*?\n  \}",
    "  function utilitySvg(kind) {\n    return typeof window.dpLucideSvg === 'function' ? window.dpLucideSvg(kind, 'dp-utility-icon') : '';\n  }",
    flags=re.S,
)

ui_downloads = STATIC / "ui-downloads-runtime.js"
replace_regex(
    ui_downloads,
    r"  function utilitySvg\(name\) \{.*?\n  \}",
    "  function utilitySvg(name) {\n    return typeof window.dpLucideSvg === 'function' ? window.dpLucideSvg(name, 'dp-utility-icon') : '';\n  }",
    flags=re.S,
)
replace_exact(ui_downloads, "else if (onclick.includes('downloadNow(')) label = '⬇ Now';", "else if (onclick.includes('downloadNow(')) label = 'Now';")

# Authentication status cards use the same Lucide source instead of handwritten SVGs.
auth = STATIC / "auth-ux.js"
replace_regex(
    auth,
    r"  const icons = \{.*?\n  \};",
    "  const icons = Object.freeze({\n    mode: 'shield-check',\n    oidc: 'lock-keyhole',\n    runtime: 'activity',\n    provider: 'user-round',\n    token: 'key',\n    session: 'monitor',\n    globe: 'globe'\n  });",
    flags=re.S,
)
replace_exact(auth, "      icon.innerHTML = icons[kind] || icons.session;", "      icon.innerHTML = typeof window.dpLucideSvg === 'function'\n        ? window.dpLucideSvg(icons[kind] || icons.session, 'auth-status-icon-svg')\n        : '';")
replace_exact(auth, "        ${icons.globe}", "        ${typeof window.dpLucideSvg === 'function' ? window.dpLucideSvg(icons.globe, 'auth-status-icon-svg') : ''}")
replace_exact(auth, "    if (sessionHeader) sessionHeader.textContent = '🕒 Session Details';", "    if (sessionHeader) sessionHeader.innerHTML = `${typeof window.dpLucideSvg === 'function' ? window.dpLucideSvg('clock-3','auth-status-icon-svg') : ''}<span>Session Details</span>`;")

# Canonical transfer badge material now covers Details and uses inline Lucide SVG.
transfer = STATIC / "ui-transfer-contract.css"
text = read(transfer)
start = text.index("/* ── Status tags")
end = text.index("/* ── Row action language")
status_css = '''/* ── Status tags ─────────────────────────────────────────────────────── */
body.dp-v11-structural :is(#dash-tbody, #t-tbody, #modal) .badge {
  display: inline-flex !important;
  align-items: center !important;
  gap: 6px !important;
  min-height: 25px !important;
  padding: 0 9px !important;
  border-radius: 6px !important;
  font-size: 10.5px !important;
  line-height: 1 !important;
  font-weight: 600 !important;
  border-color: color-mix(in srgb, var(--dp-badge-color) 22%, transparent) !important;
  background: color-mix(in srgb, var(--dp-badge-color) 9%, transparent) !important;
  color: var(--dp-badge-color) !important;
}

/* Retire the inherited symbol-font/pseudo-glyph path. Status glyphs are now
   exact locally vendored Lucide SVGs emitted by the canonical badge renderer. */
body.dp-v11-structural :is(#dash-tbody, #t-tbody, #modal) .badge::before {
  content: none !important;
  display: none !important;
}
body.dp-v11-structural :is(#dash-tbody, #t-tbody, #modal) .badge .dp-status-icon {
  width: 15px !important;
  min-width: 15px !important;
  height: 15px !important;
  flex: 0 0 15px !important;
  stroke: currentColor;
  stroke-width: 2;
  filter: drop-shadow(0 0 4px color-mix(in srgb, currentColor 28%, transparent));
}

body.dp-v11-structural :is(#dash-tbody, #t-tbody, #modal) .badge-downloading,
body.dp-v11-structural :is(#dash-tbody, #t-tbody, #modal) .badge-completed {
  --dp-badge-color: var(--dp-state-success);
}
body.dp-v11-structural :is(#dash-tbody, #t-tbody, #modal) .badge-uploading,
body.dp-v11-structural :is(#dash-tbody, #t-tbody, #modal) .badge-queued,
body.dp-v11-structural :is(#dash-tbody, #t-tbody, #modal) .badge-imported {
  --dp-badge-color: var(--dp-state-active);
}
body.dp-v11-structural :is(#dash-tbody, #t-tbody, #modal) .badge-processing,
body.dp-v11-structural :is(#dash-tbody, #t-tbody, #modal) .badge-extracting {
  --dp-badge-color: var(--dp-accent-purple-bright);
}
body.dp-v11-structural :is(#dash-tbody, #t-tbody, #modal) .badge-paused,
body.dp-v11-structural :is(#dash-tbody, #t-tbody, #modal) .badge-ready,
body.dp-v11-structural :is(#dash-tbody, #t-tbody, #modal) .badge-partial {
  --dp-badge-color: var(--dp-state-caution);
}
body.dp-v11-structural :is(#dash-tbody, #t-tbody, #modal) .badge-error {
  --dp-badge-color: var(--dp-state-error);
}
body.dp-v11-structural :is(#dash-tbody, #t-tbody, #modal) .badge-pending {
  --dp-badge-color: var(--dp-state-ready);
}
body.dp-v11-structural :is(#dash-tbody, #t-tbody, #modal) .badge-deleted,
body.dp-v11-structural :is(#dash-tbody, #t-tbody, #modal) .badge-unknown {
  --dp-badge-color: var(--dp-text-muted);
}

'''
write(transfer, text[:start] + status_css + text[end:])

# Static fallbacks no longer render a second glyph vocabulary.
index = STATIC / "index.html"
text = read(index)
static_replacements = {
    '<span class="icon">&#x25EB;&#xFE0E;</span>': '<span class="icon" aria-hidden="true"></span>',
    '<span class="icon">&#x25BD;&#xFE0E;</span>': '<span class="icon" aria-hidden="true"></span>',
    '<span class="icon">&#x2263;&#xFE0E;</span>': '<span class="icon" aria-hidden="true"></span>',
    '<span class="icon">&#x2206;&#xFE0E;</span>': '<span class="icon" aria-hidden="true"></span>',
    '<span class="icon">&#x2699;&#xFE0E;</span>': '<span class="icon" aria-hidden="true"></span>',
    '<span class="icon">&#x003F;&#xFE0E;</span>': '<span class="icon" aria-hidden="true"></span>',
    'aria-label="Switch to light mode">&#x263E;&#xFE0E;</button>': 'aria-label="Switch to light mode"></button>',
    'aria-label="Menu">☰</button>': 'aria-label="Menu"></button>',
    '<span style="color:var(--green)">&#9660;</span><span id="aria2-badge-active">': '<span id="aria2-speed-icon" aria-hidden="true"></span><span id="aria2-badge-active">',
    '<div class="dhs-icon">📦</div>': '<div class="dhs-icon"></div>',
    '<div class="dhs-icon">✅</div>': '<div class="dhs-icon"></div>',
    '<div class="dhs-icon">⬇</div>': '<div class="dhs-icon"></div>',
    '<div class="dhs-icon">⚙</div>': '<div class="dhs-icon"></div>',
    '<div class="dhs-icon">⚠</div>': '<div class="dhs-icon"></div>',
    '<div class="dhs-icon">💾</div>': '<div class="dhs-icon"></div>',
    '<span class="card-title">⬇️ Add Links, Magnets, or Torrent File</span>': '<span class="card-title">Add Links, Magnets, or Torrent File</span>',
    '>⬇ Import</button>': '>Import</button>',
    '>⟳ Recover All</button>': '>Recover All</button>',
    '>View All →</button>': '>View All</button>',
    '>✕ Delete</button>': '>Delete</button>',
    '>↺ Reset</button>': '>Reset</button>',
    '>⏸ Pause</button>': '>Pause</button>',
    '>▶ Resume</button>': '>Resume</button>',
    '>✕ Clear</button>': '>Clear</button>',
    'style="margin-left:8px">↻</button>': 'style="margin-left:8px">Refresh</button>',
    'onclick="loadEvents()">↻ Refresh</button>': 'onclick="loadEvents()">Refresh</button>',
    '>🔑 Test AllDebrid</button>': '>Test AllDebrid</button>',
    '>⬇️ Test Aria2</button>': '>Test Aria2</button>',
    '>🔔 Test Discord</button>': '>Test Discord</button>',
    '>💾 Save Settings</button>': '>Save Settings</button>',
    'use the <b>↻</b> retry button on the row or <b>⟳ Recover All</b>.': 'use the <b>Retry</b> button on the row or <b>Recover All</b>.',
}
for old, new in static_replacements.items():
    if old not in text:
        raise SystemExit(f"index.html: missing expected legacy fragment {old!r}")
    text = text.replace(old, new)
# Central Lucide runtime executes before any consumer runtime.
old_scripts = '<script src="/app.js?v=15" defer></script>\n<script src="/operator-title.js?v=23" defer></script>\n<script src="/ui-runtime.js?v=24" defer data-dp-ui-runtime="1"></script>\n<script src="/ui-downloads-runtime.js?v=22" defer data-dp-downloads-runtime="1"></script>'
new_scripts = '<script src="/ui-lucide-runtime.js?v=1" defer></script>\n<script src="/app.js?v=16" defer></script>\n<script src="/operator-title.js?v=24" defer></script>\n<script src="/ui-runtime.js?v=25" defer data-dp-ui-runtime="1"></script>\n<script src="/ui-downloads-runtime.js?v=23" defer data-dp-downloads-runtime="1"></script>'
if text.count(old_scripts) != 1:
    raise SystemExit('index.html: parser runtime chain did not match expected generation')
text = text.replace(old_scripts, new_scripts)
text = text.replace('/style-v11.css?v=24', '/style-v11.css?v=25')
write(index, text)

# Base stylesheet no longer requests symbol fonts for navigation fallbacks.
style = STATIC / "style.css"
replace_exact(style, "  font-family:'Segoe UI Symbol','Noto Sans Symbols 2','Noto Sans Symbols',sans-serif;\n  font-variant-emoji:text; font-weight:400;", "  font-family:inherit; font-weight:400;")

# Cascade: Lucide iconography is the final cross-cutting presentation layer.
style_v11 = STATIC / "style-v11.css"
replace_exact(style_v11, "@import url('/ui-transfer-contract.css?v=31');", "@import url('/ui-transfer-contract.css?v=32');\n@import url('/ui-lucide-iconography.css?v=1');")

# Operator fallback loader generations follow the explicit parser chain.
replace_exact(operator, "/ui-runtime.js?v=24", "/ui-runtime.js?v=25")
replace_exact(operator, "/ui-downloads-runtime.js?v=22", "/ui-downloads-runtime.js?v=23")

# Documentation: status and notification glyphs now join the canonical Lucide tier.
docs = ROOT / "docs" / "UI_ICON_SYSTEM.md"
replace_exact(
    docs,
    "2. **Lucide** for ordinary navigation, controls, form adornments, and utility actions.",
    "2. **Lucide** for ordinary navigation, controls, form adornments, transfer/status indicators, notifications, and utility actions."
)
replace_exact(
    docs,
    "### Tier 3 — Lucide utility icons\n\nLucide is the canonical utility/navigation family. Use it for ordinary interface vocabulary, including:",
    "### Tier 3 — Lucide universal UI glyphs\n\nLucide is the canonical ordinary-UI glyph family. Use it for navigation, transfer/status indicators, notifications, action buttons, and other standard interface vocabulary, including:"
)
replace_exact(
    docs,
    "- Notifications/bell\n- Database",
    "- Notifications/bell\n- Transfer status (download, pause, Circle Check for Done, error, queue, processing, extraction, caution)\n- Toast status (Circle Check, Circle X, Triangle Alert, Info)\n- Database"
)
replace_exact(
    docs,
    "Everything else should default to Lucide unless a later approved mockup establishes a new DP-specific semantic treatment.",
    "Everything else should default to Lucide unless a later approved mockup establishes a new DP-specific semantic treatment. Do not introduce symbol-font, emoji, or handwritten SVG alternatives for ordinary UI glyphs when Lucide supplies the concept."
)

# New contract tests focus on the architectural invariant rather than snapshots.
test = '''"""Universal Lucide iconography contract."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_canonical_lucide_runtime_is_loaded_before_consumers() -> None:
    html = read("index.html")
    chain = (
        '<script src="/ui-lucide-runtime.js?v=1" defer></script>',
        '<script src="/app.js?v=16" defer></script>',
        '<script src="/operator-title.js?v=24" defer></script>',
        '<script src="/ui-runtime.js?v=25" defer data-dp-ui-runtime="1"></script>',
        '<script src="/ui-downloads-runtime.js?v=23" defer data-dp-downloads-runtime="1"></script>',
    )
    positions = [html.index(item) for item in chain]
    assert positions == sorted(positions)


def test_status_mapping_uses_lucide_and_done_is_circle_check() -> None:
    runtime = read("ui-lucide-runtime.js")
    app = read("app.js")
    assert "completed:               {label:'Done', icon:'circle-check'}" in runtime
    for state, icon in (
        ("downloading", "download"), ("paused", "pause"), ("uploading", "upload"),
        ("processing", "loader-circle"), ("extracting", "package-open"),
        ("partial", "triangle-alert"), ("ready", "play"), ("deleted", "trash-2"),
    ):
        assert f"{state}:" in runtime and f"icon:'{icon}'" in runtime
    assert "window.dpLucideStatusDefinition" in app
    assert "window.dpLucideStatusIcon" in app
    for legacy in ("✅ Done", "❌ Error", "⏸ Paused", "⬇ Downloading", "⚠ Partial"):
        assert legacy not in app


def test_transfer_badges_share_one_geometry_across_lists_and_details() -> None:
    css = read("ui-transfer-contract.css")
    assert ":is(#dash-tbody, #t-tbody, #modal) .badge" in css
    assert "border-radius: 6px !important" in css
    assert ".badge .dp-status-icon" in css
    assert "content: none !important" in css
    assert "Segoe UI Symbol" not in css
    assert "Noto Sans Symbols" not in css
    for glyph in ("content: '↓'", "content: 'Ⅱ'", "content: '✓'", "content: '×'"):
        assert glyph not in css


def test_toasts_use_lucide_semantic_icons_not_emoji() -> None:
    app = read("app.js")
    css = read("ui-lucide-iconography.css")
    assert "success:'circle-check'" in app
    assert "error:'circle-x'" in app
    assert "warn:'triangle-alert'" in app
    assert "info:'info'" in app
    assert "dp-toast-icon" in app and "dp-toast-icon-wrap" in css
    for emoji in ("✅", "❌", "⚠️", "ℹ️"):
        assert emoji not in app[app.index("function toast"):app.index("function setButtonPending")]


def test_page_runtimes_delegate_to_one_lucide_source() -> None:
    operator = read("operator-title.js")
    ui = read("ui-runtime.js")
    downloads = read("ui-downloads-runtime.js")
    for source in (operator, ui, downloads):
        assert "window.dpLucideSvg" in source
    assert "const LUCIDE =" not in operator
    assert "const paths =" not in ui
    assert "const paths =" not in downloads


def test_auth_status_cards_delegate_to_canonical_lucide_source() -> None:
    auth = read("auth-ux.js")
    assert "window.dpLucideSvg" in auth
    assert "mode: 'shield-check'" in auth
    assert "runtime: 'activity'" in auth
    assert "<svg viewBox=" not in auth


def test_existing_glyph_bearing_actions_are_lucide_normalized() -> None:
    runtime = read("ui-lucide-runtime.js")
    for action in (
        "triggerFullSync", "runDeepSync", "clearDiscordAvatar", "loadComprehensiveStats",
        "exportStats", "triggerStatsSnapshot", "sendStatsReport", "triggerBackup",
        "loadBackupList", "triggerDatabaseBackup", "loadDatabaseBackupList", "wipeDatabase",
        "addExtractionPassword", "removeExtractionPassword", "loadAria2QueueView",
        "runAria2Housekeeping",
    ):
        assert action in runtime
    for element_id in ("btn-test-alldebrid", "btn-test-aria2", "btn-test-discord", "btn-save-settings"):
        assert element_id in runtime


def test_settings_navigation_uses_lucide_not_emoji() -> None:
    app = read("app.js")
    for icon in ("zap", "download", "package-open", "bell", "database", "wrench"):
        assert f"icon:'{icon}'" in app
    for emoji in ("⚡ General", "⬇️ Download", "📦 Extract", "🔔 Notifications", "🗄 Database", "🛠️ Advanced"):
        assert emoji not in app


def test_lucide_pin_and_backend_version_are_stable() -> None:
    runtime = read("ui-lucide-runtime.js")
    assert "23f9abc4ed0146cffededd3d7f94c1018bfdf693" in runtime
    assert (ROOT / "VERSION").read_text(encoding="utf-8").strip() == "1.0.10"
'''
write(TESTS / "test_ui_lucide_universal_contract.py", test)

print("Lucide universal iconography changes applied.")
