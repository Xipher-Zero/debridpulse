#!/usr/bin/env python3
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
S = ROOT / "frontend" / "static"

def rd(p):
    return Path(p).read_text(encoding="utf-8")

def wr(p, s):
    Path(p).write_text(s, encoding="utf-8")

def match_brace(src, pos):
    depth = 0
    i = pos
    state = "code"
    quote = None
    while i < len(src):
        c = src[i]
        n = src[i+1] if i+1 < len(src) else ""
        if state == "line":
            if c == "\n": state = "code"
            i += 1; continue
        if state == "block":
            if c == "*" and n == "/": state = "code"; i += 2
            else: i += 1
            continue
        if state == "string":
            if c == "\\": i += 2; continue
            if c == quote: state = "code"
            i += 1; continue
        if c == "/" and n == "/": state = "line"; i += 2; continue
        if c == "/" and n == "*": state = "block"; i += 2; continue
        if c in ("'", '"', "`"): state = "string"; quote = c; i += 1; continue
        if c == "{": depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0: return i
        i += 1
    raise RuntimeError("unmatched brace")

def span(src, name):
    m = re.search(r"(?m)^[ \t]*(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    if not m: raise RuntimeError("missing function " + name)
    op = src.find("{", m.end())
    cl = match_brace(src, op)
    end = cl + 1
    while end < len(src) and src[end] in " \t": end += 1
    if end < len(src) and src[end] == ";": end += 1
    if end < len(src) and src[end] == "\n": end += 1
    return m.start(), end

def extract(src, name):
    a,b = span(src,name)
    return src[a:b].rstrip()

def remove(src, name):
    a,b = span(src,name)
    return src[:a] + src[b:]

def replace_fn(src, name, new):
    a,b = span(src,name)
    return src[:a] + new.rstrip() + "\n" + src[b:]

def replace_div_inner(html, element_id):
    m = re.search(r'<div\b(?=[^>]*\bid=["\']' + re.escape(element_id) + r'["\'])[^>]*>', html, re.I)
    if not m: raise RuntimeError("missing div " + element_id)
    toks = re.compile(r"<div\b[^>]*>|</div\s*>", re.I)
    depth = 1
    for t in toks.finditer(html, m.end()):
        if t.group(0).lower().startswith("<div"): depth += 1
        else:
            depth -= 1
            if depth == 0:
                return html[:m.end()] + "\n      " + html[t.start():]
    raise RuntimeError("unclosed div " + element_id)

wr(ROOT / "VERSION", "1.0.11.1\n")

wr(S / "ui-theme-bootstrap.js",
"""/* DebridPulse first-paint theme bootstrap.
 * Required application modules are normal parser-deferred dependencies.
 */
(function () {
  'use strict';
  try {
    if (localStorage.getItem('theme') === 'light') document.body.classList.add('light');
  } catch (_) {}
})();
""")
(S / "ui-presentation-loader.js").unlink(missing_ok=True)

ip = S / "index.html"
index = rd(ip)
index = replace_div_inner(index, "view-settings")
index = replace_div_inner(index, "view-help")
index = index.replace('id="aria2-speed-badge" style="display:none;', 'id="aria2-speed-badge" style="display:flex;')
index = index.replace('<span id="aria2-badge-max">—</span>', '<span id="aria2-badge-max">0</span>')
index = index.replace('<div class="ftab active" data-period="24h" onclick="setStatsPeriod(this)">24h</div>',
                      '<div class="ftab" data-period="24h" onclick="setStatsPeriod(this)">24h</div>')
index = index.replace('<div class="ftab" data-period="7d" onclick="setStatsPeriod(this)">7d</div>',
                      '<div class="ftab active" data-period="7d" onclick="setStatsPeriod(this)">7d</div>')
old = """<script src="/app.js?v=15" defer></script>
<script src="/operator-title.js?v=23" defer></script>
<script src="/ui-runtime.js?v=24" defer data-dp-ui-runtime="1"></script>
<script src="/ui-downloads-runtime.js?v=22" defer data-dp-downloads-runtime="1"></script>
<script src="/ui-accessibility-runtime.js?v=21" defer></script>
<script src="/vendor/chart.umd.min.js?v=4.5.1"></script>"""
new = """<script src="/vendor/chart.umd.min.js?v=4.5.1"></script>
<script src="/app.js?v=16" defer></script>
<script src="/operator-title.js?v=23" defer></script>
<script src="/ui-runtime.js?v=25" defer data-dp-ui-runtime="1"></script>
<script src="/ui-downloads-runtime.js?v=23" defer data-dp-downloads-runtime="1"></script>
<script src="/ui-accessibility-runtime.js?v=21" defer></script>
<script src="/ui-shell-runtime.js?v=2" defer></script>
<script src="/ui-statistics.js?v=2" defer></script>
<script src="/ui-help-page.js?v=2" defer></script>
<script src="/ui-help-chrome.js?v=2" defer></script>
<script src="/ui-help-license-documents.js?v=2" defer></script>
<script src="/ui-settings-page.js?v=2" defer></script>
<script src="/ui-settings-auth-resilience.js?v=2" defer></script>
<script src="/ui-settings-maintenance-wipe.js?v=2" defer></script>
<script src="/ui-settings-notifications.js?v=2" defer></script>
<script src="/ui-settings-authentication.js?v=2" defer></script>
<script src="/ui-settings-authentication-polish.js?v=2" defer></script>
<script src="/ui-settings-authentication-oidc.js?v=2" defer></script>
<script src="/ui-settings-authentication-callback.js?v=2" defer></script>
<script src="/ui-settings-downloads-completion.js?v=2" defer></script>
<script src="/ui-settings-aria2-live.js?v=2" defer></script>
<script src="/ui-settings-card-icons.js?v=2" defer></script>
<script src="/ui-error-semantics.js?v=2" defer></script>
<script src="/ui-page-finalization.js?v=2" defer></script>"""
if old not in index: raise RuntimeError("core script block changed")
index = index.replace(old, new)
wr(ip,index)

ap = S / "app.js"
app = rd(ap)
stats_data = extract(app, "loadDetailedStats")
for name in ("loadDetailedStats","renderTorrentPagination","setFilter","loadSettings","renderSettings",
             "getFormSettings","switchSettingsTab","updateSettingsFooterActions"):
    try: app = remove(app,name)
    except RuntimeError: pass

app = app.replace("  document.getElementById('page-title').textContent = titles[v] || v;\n",
                  "  document.getElementById('page-title').textContent = titles[v] || v;\n"
                  "  document.dispatchEvent(new CustomEvent('debridpulse:navigation', {detail:{view:v,title:titles[v]||v}}));\n")
app = app.replace("  updateThemeToggle(isLight);\n}",
                  "  updateThemeToggle(isLight);\n"
                  "  document.dispatchEvent(new CustomEvent('debridpulse:theme-changed', {detail:{light:isLight}}));\n}")

a,b = span(app,"loadStats")
blk = app[a:b]
anchor = "      updateOperatorTitle(s);\n"
if anchor not in blk: raise RuntimeError("loadStats anchor missing")
blk = blk.replace(anchor, anchor + "      document.dispatchEvent(new CustomEvent('debridpulse:dashboard-stats-rendered', {detail:s}));\n",1)
app = app[:a]+blk+app[b:]

for name,event in (("loadRecent","debridpulse:dashboard-recent-rendered"),
                   ("loadTorrents","debridpulse:downloads-rendered"),
                   ("filterEvents","debridpulse:activity-rendered")):
    a,b = span(app,name); blk=app[a:b]; cl=blk.rfind("}")
    blk = blk[:cl] + "  document.dispatchEvent(new CustomEvent('" + event + "'));\n" + blk[cl:]
    app=app[:a]+blk+app[b:]
wr(ap,app)

sp = S / "ui-statistics.js"
stats = rd(sp)
stats_data = stats_data.replace("async function loadDetailedStats(","async function loadDetailedStatsData(",1)
stats = stats.replace("  'use strict';\n","  'use strict';\n\n"+stats_data+"\n\n",1)
stats = stats.replace("  const EVENT_NAME = 'debridpulse:statistics-rendered';\n","")
stats = replace_fn(stats,"install",
"""  async function loadDetailedStats(period) {
    const resolved = selectedPeriod(period);
    const result = await loadDetailedStatsData(resolved);
    applyPresentation(resolved);
    return result;
  }

  function install() {
    window.loadDetailedStats = loadDetailedStats;
    try { loadDetailedStats = window.loadDetailedStats; } catch (_) {}
    window.fmtDuration = formatCompactDuration;
    return true;
  }""")
stats = replace_fn(stats,"initialize",
"""  function initialize() {
    applyPresentation('7d');
  }""")
stats = re.sub(r"  window\.DPStatisticsLifecycle = Object\.freeze\(\{\s*event: EVENT_NAME,\s*install: install,\s*\}\);",
               "  window.DPStatisticsLifecycle = Object.freeze({load: loadDetailedStats, install});",stats)
wr(sp,stats)

dp = S / "ui-downloads-runtime.js"
d = rd(dp)
d = re.sub(r"\n\s*let titleObserver = null;\n\s*let statsObserver = null;\n\s*let downloadsEmptyObserver = null;\n\s*let recentEmptyObserver = null;\n","\n",d)

a,b=span(d,"installPaginationRenderer")
direct_pag = """  function renderTorrentPagination(total, limit, offset) {
    const normalizedTotal=Math.max(0,Number(total)||0);
    const normalizedLimit=Math.max(1,Number(limit)||25);
    const normalizedOffset=Math.max(0,Number(offset)||0);
    const totalPages=Math.max(1,Math.ceil(normalizedTotal/normalizedLimit));
    const cur=Math.min(totalPages,Math.floor(normalizedOffset/normalizedLimit)+1);
    try { torrentPage=cur; } catch (_) {}
    const info=document.getElementById('torrent-page-info');
    const btns=document.getElementById('torrent-page-btns');
    if(!info||!btns)return;
    const from=normalizedTotal===0?0:normalizedOffset+1;
    const to=Math.min(normalizedOffset+normalizedLimit,normalizedTotal);
    info.textContent=paginationSummary(normalizedTotal,from,to);
    const controls=[];
    if(cur>1)controls.push('<button type="button" class="dp-pager-btn" aria-label="Previous page" onclick="goToTorrentPage('+(cur-1)+')">'+utilitySvg('chevronLeft')+'</button>');
    controls.push('<button type="button" class="dp-pager-btn dp-pager-current" aria-current="page" aria-label="Page '+cur+', current page">'+cur+'</button>');
    if(cur<totalPages)controls.push('<button type="button" class="dp-pager-btn" aria-label="Next page" onclick="goToTorrentPage('+(cur+1)+')">'+utilitySvg('chevronRight')+'</button>');
    btns.innerHTML=controls.join('');
  }
  window.renderTorrentPagination=renderTorrentPagination;
"""
d=d[:a]+direct_pag+d[b:]

a,b=span(d,"installFilterWrapper")
direct_filter = """  function setFilter(el,status) {
    document.querySelectorAll('#view-torrents .filter-tabs .ftab').forEach(tab=>tab.classList.remove('active'));
    if(el)el.classList.add('active');
    try { currentFilter=status; torrentPage=1; } catch (_) {}
    if(typeof loadTorrents==='function')loadTorrents();
    syncFilterState();
  }
  window.setFilter=setFilter;
"""
d=d[:a]+direct_filter+d[b:]
for fn in ("observeDynamicCounts","observeEmptyStates"):
    try:d=remove(d,fn)
    except RuntimeError:pass
d=d.replace("    installPaginationRenderer();\n    installFilterWrapper();\n","")
d=d.replace("    syncFilterState();\n    observeDynamicCounts();\n    observeEmptyStates();\n",
            "    syncFilterState();\n")
d=d.replace("  document.addEventListener('DOMContentLoaded', initializeDownloadsPresentation, {once: true});\n})();",
"""  document.addEventListener('DOMContentLoaded', initializeDownloadsPresentation, {once: true});
  document.addEventListener('debridpulse:downloads-rendered', initializeDownloadsPresentation);
  document.addEventListener('debridpulse:dashboard-recent-rendered', decorateEmptyStates);
  document.addEventListener('debridpulse:dashboard-stats-rendered', decorateDownloadsHeader);
})();""")
wr(dp,d)

stp=S/"style-v11.css"
style=rd(stp)
required=["ui-help-chrome.css","ui-help-license-documents.css","ui-help-balance.css",
"ui-settings-downloads-completion.css","ui-settings-form-consistency.css","ui-settings-aria2-live.css",
"ui-settings-maintenance-wipe.css","ui-settings-notifications.css","ui-settings-authentication.css",
"ui-settings-authentication-polish.css","ui-settings-authentication-oidc.css","ui-settings-card-icons.css",
"ui-page-finalization.css","ui-statistics.css","ui-shell-brand.css"]
style += "\n/* Required canonical application/page styles. */\n"
for n in required:
    if "/"+n not in style: style += "@import url('/"+n+"?v=2');\n"
wr(stp,style)

print("phase1 complete")
