from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def write(name: str, text: str) -> None:
    (STATIC / name).write_text(text.rstrip() + "\n", encoding="utf-8")


def function_span(source: str, name: str) -> tuple[int, int]:
    marker = f"function {name}("
    start = source.find(marker)
    if start < 0:
        raise RuntimeError(f"missing JS function {name}")
    brace = source.find("{", start)
    if brace < 0:
        raise RuntimeError(f"missing opening brace for {name}")

    depth = 0
    i = brace
    quote: str | None = None
    line_comment = False
    block_comment = False
    escape = False
    while i < len(source):
        ch = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ""
        if line_comment:
            if ch == "\n":
                line_comment = False
            i += 1
            continue
        if block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 2
                continue
            i += 1
            continue
        if quote:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch == "/" and nxt == "/":
            line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            block_comment = True
            i += 2
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return start, i + 1
        i += 1
    raise RuntimeError(f"unterminated JS function {name}")


def replace_function(source: str, name: str, replacement: str) -> str:
    start, end = function_span(source, name)
    return source[:start] + replacement.rstrip() + source[end:]


def remove_function(source: str, name: str) -> str:
    start, end = function_span(source, name)
    while end < len(source) and source[end] in " \t":
        end += 1
    if end < len(source) and source[end] == "\n":
        end += 1
    return source[:start] + source[end:]


def require_once(source: str, needle: str, label: str) -> None:
    count = source.count(needle)
    if count != 1:
        raise RuntimeError(f"expected one {label}, found {count}")


def canonicalize_app() -> None:
    source = read("app.js")

    source = replace_function(source, "badge", r'''function badge(s) {
  if (!window.DPIcons || typeof window.DPIcons.statusBadge !== 'function') {
    throw new Error('DebridPulse icon runtime is unavailable');
  }
  return window.DPIcons.statusBadge(s);
}''')

    source = replace_function(source, "toast", r'''function toast(msg, type = 'info') {
  if (!window.DPIcons || typeof window.DPIcons.toast !== 'function') {
    throw new Error('DebridPulse icon runtime is unavailable');
  }
  return window.DPIcons.toast(msg, type);
}''')

    source = replace_function(source, "progress", r'''function progress(pct, status) {
  const state = String(status || '').toLowerCase();
  const done = state === 'completed';
  const failed = state === 'error';
  const active = state === 'downloading';
  const raw = Number(pct);
  const actual = done ? 100 : Math.min(Math.max(Number.isFinite(raw) ? raw : 0, 0), 100);
  const showStripe = active && actual === 0;
  const visual = actual;
  let fillStyle = showStripe
    ? 'width:100%;opacity:.35;background:repeating-linear-gradient(90deg,var(--accent) 0,var(--accent) 8px,transparent 8px,transparent 16px)'
    : 'width:' + visual + '%';
  if (failed) {
    fillStyle += ';opacity:1;background:var(--dp-state-error)!important;background-color:var(--dp-state-error)!important;background-image:none!important;box-shadow:0 0 8px color-mix(in srgb,var(--dp-state-error) 88%,transparent),0 0 17px color-mix(in srgb,var(--dp-state-error) 46%,transparent)!important;filter:saturate(1.12) brightness(1.08)';
  }
  const cls = done ? 'done' : (failed ? 'error dp-terminal-error-progress' : '');
  const trackCls = failed ? 'prog dp-terminal-error-rail' : 'prog';
  const label = done ? '100%' : (showStripe ? '…' : actual.toFixed(0) + '%');
  const attrs = failed
    ? ' data-dp-actual-progress="' + actual + '" data-dp-visual-progress="' + visual + '"'
    : '';
  return '<div class="' + trackCls + '"' + (failed ? ' data-dp-actual-progress="' + actual + '"' : '') + '><div class="prog-fill ' + cls + '" style="' + fillStyle + '"' + attrs + '></div></div>' +
         '<span class="prog-pct">' + label + '</span>';
}''')

    source = replace_function(source, "updateThemeToggle", r'''function updateThemeToggle(isLight) {
  const btn = document.getElementById('theme-toggle');
  if (!btn) return;
  if (!window.DPIcons || typeof window.DPIcons.renderThemeGlyph !== 'function') {
    throw new Error('DebridPulse icon runtime is unavailable');
  }
  const action = isLight ? 'Switch to dark mode' : 'Switch to light mode';
  btn.title = action;
  btn.setAttribute('aria-label', action);
  window.DPIcons.renderThemeGlyph(!!isLight);
}''')

    source = replace_function(source, "updateOperatorTitle", r'''function updateOperatorTitle(stats) {
  const byStatus = stats && stats.by_status && typeof stats.by_status === 'object' ? stats.by_status : null;
  const nonNegativeCount = value => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? Math.max(0, Math.trunc(parsed)) : 0;
  };
  const logicalActive = byStatus
    ? nonNegativeCount(byStatus.downloading) + nonNegativeCount(byStatus.queued)
    : nonNegativeCount(stats && stats.operator_active_downloads);

  updateOperatorTitle._latestLogicalActive = logicalActive;

  const cancelIdle = () => {
    if (updateOperatorTitle._idleTimer != null) {
      clearTimeout(updateOperatorTitle._idleTimer);
      updateOperatorTitle._idleTimer = null;
    }
  };

  if (stats && stats.paused) {
    cancelIdle();
    _operatorTitleState.active = 0;
    _operatorTitleState.progress = 0;
    renderOperatorTitle();
    return;
  }

  if (logicalActive > 0) {
    cancelIdle();
    _operatorTitleState.active = logicalActive;
    const rawProgress = stats && stats.operator_active_progress_pct;
    const value = rawProgress == null ? NaN : Number(rawProgress);
    if (Number.isFinite(value)) {
      _operatorTitleState.progress = Math.min(100, Math.max(0, Math.round(value)));
    }
    renderOperatorTitle();
    return;
  }

  if (_operatorTitleState.active === 0) {
    cancelIdle();
    renderOperatorTitle();
    return;
  }

  if (updateOperatorTitle._idleTimer == null) {
    updateOperatorTitle._idleTimer = setTimeout(() => {
      updateOperatorTitle._idleTimer = null;
      if (updateOperatorTitle._latestLogicalActive === 0) {
        _operatorTitleState.active = 0;
        _operatorTitleState.progress = 0;
        renderOperatorTitle();
      }
    }, 1500);
  }
  renderOperatorTitle();
}''')

    write("app.js", source)


def canonicalize_operator_runtime() -> None:
    source = read("operator-title.js")
    marker = "/* DebridPulse v1.0.11 canonical Lucide presentation integration."
    pos = source.find(marker)
    if pos < 0:
        raise RuntimeError("canonical Lucide marker missing")
    source = source[pos:]

    source = source.replace("    decorateButton: decorateButton\n", "    decorateButton: decorateButton,\n    toast: canonicalToast,\n    renderThemeGlyph: renderThemeGlyph\n")
    if "toast: canonicalToast" not in source or "renderThemeGlyph: renderThemeGlyph" not in source:
        raise RuntimeError("DPIcons canonical exports were not installed")

    source = source.replace("\n  window.badge = statusBadge;\n  window.toast = canonicalToast;\n", "\n")
    require_once(source, "  const legacyUpdateThemeToggle = window.updateThemeToggle;", "legacy theme wrapper block")
    start = source.index("  const legacyUpdateThemeToggle = window.updateThemeToggle;")
    end_marker = "  function bindThemeToggle()"
    end = source.index(end_marker, start)
    source = source[:start] + source[end:]

    source = replace_function(source, "bindThemeToggle", r'''function bindThemeToggle() {
    const button = document.getElementById('theme-toggle');
    const control = button && button.closest('.sidebar-theme-control');
    const topbar = document.getElementById('topbar');
    if (!button || !control || !topbar) return;
    control.classList.add('topbar-theme-control');
    if (control.parentElement !== topbar) topbar.appendChild(control);
    renderThemeGlyph(document.body.classList.contains('light'));
  }''')

    if re.search(r"window\.(?:updateOperatorTitle|badge|toast|updateThemeToggle|toggleTheme)\s*=", source):
        raise RuntimeError("operator-title runtime still replaces canonical app owners")
    write("operator-title.js", source)


def canonicalize_error_semantics() -> None:
    source = read("ui-error-semantics.js")
    source = remove_function(source, "installProgressOverride")
    source = remove_function(source, "observeTransferTables")
    source = replace_function(source, "initialize", r'''function initialize() {
    if (installed) {
      enrichVisibleFailures();
      return;
    }
    installed = true;
    document.addEventListener('debridpulse:dashboard-recent-rendered', enrichVisibleFailures);
    document.addEventListener('debridpulse:downloads-rendered', enrichVisibleFailures);
    enrichVisibleFailures();
    window.DPFailureSemantics = Object.freeze({
      labels: FAILURE_LABELS,
      classify: classifyFailure
    });
  }''')
    source = source.replace(
        "    /* The sequential presentation loader runs only after parser-deferred core\n       runtimes. Do not spin the event loop waiting for dependencies: a missing\n       core helper is an explicit architecture failure, not a timing condition. */",
        "    /* Required direct scripts execute in parser order. Missing core helpers are\n       an explicit architecture failure, not a timing condition. */",
    )
    if "MutationObserver" in source:
        raise RuntimeError("error semantics still observes rendered transfer tables")
    if "window.progress =" in source:
        raise RuntimeError("error semantics still replaces canonical progress owner")
    write("ui-error-semantics.js", source)


def canonicalize_accessibility() -> None:
    source = read("ui-accessibility-runtime.js")

    source = replace_function(source, "installNavigationSemantics", r'''function installNavigationSemantics() {
    syncNavigationState();
  }''')
    source = remove_function(source, "installNavigationNamingHook")
    source = remove_function(source, "observeFilterGroup")
    source = replace_function(source, "installFilterSemantics", r'''function installFilterSemantics() {
    syncFilterGroup(
      document.querySelector('#view-torrents .filter-tabs'),
      'Download status filter'
    );
    syncFilterGroup(
      document.getElementById('stats-period-tabs'),
      'Statistics period'
    );
  }''')
    source = remove_function(source, "observeTablist")
    source = replace_function(source, "installTabSemantics", r'''function installTabSemantics() {
    syncTablist(document.getElementById('help-tabs'), 'Help sections');
    syncTablist(document.getElementById('settings-tabs'), 'Settings sections');
  }''')

    lifecycle = r'''
  function installAccessibilityLifecycle() {
    if (document.documentElement.dataset.dpAccessibilityLifecycle === '1') return;
    document.documentElement.dataset.dpAccessibilityLifecycle = '1';

    document.addEventListener('debridpulse:navigation', function () {
      queueMicrotask(function () {
        normalizeActivityNaming();
        installNavigationSemantics();
        installFilterSemantics();
        installTabSemantics();
      });
    });
    document.addEventListener('debridpulse:downloads-rendered', installFilterSemantics);
    document.addEventListener('debridpulse:settings-rendered', installTabSemantics);
    document.addEventListener('click', function (event) {
      if (!event.target.closest('.filter-tabs .ftab, .dp-help-tabs .stab, .dp-settings-tabs .stab')) return;
      queueMicrotask(function () {
        installFilterSemantics();
        installTabSemantics();
      });
    });
  }

'''
    init_marker = "  function initializeAccessibilityContract()"
    require_once(source, init_marker, "accessibility initializer")
    source = source.replace(init_marker, lifecycle + init_marker, 1)
    source = source.replace("    installNavigationNamingHook();\n", "")
    source = source.replace("    installUniversalSelectDropdowns();\n", "    installUniversalSelectDropdowns();\n    installAccessibilityLifecycle();\n")

    if "window.nav =" in source or "previous.apply(this, arguments)" in source:
        raise RuntimeError("accessibility runtime still wraps navigation")
    observer_count = source.count("new MutationObserver")
    if observer_count != 3:
        raise RuntimeError(f"expected exactly three localized dynamic observers, found {observer_count}")
    write("ui-accessibility-runtime.js", source)


def append_css(owner: str, donors: list[str], banner: str) -> None:
    owner_text = read(owner).rstrip()
    chunks = [owner_text, "", banner]
    for donor in donors:
        donor_text = read(donor).strip()
        chunks.extend(["", f"/* Consolidated from {donor}. */", donor_text])
    write(owner, "\n".join(chunks))
    for donor in donors:
        (STATIC / donor).unlink()


def canonicalize_css() -> None:
    dashboard_donors = [
        "ui-dashboard-batch5.css",
        "ui-dashboard-polish.css",
        "ui-dashboard-polish-final.css",
        "ui-dashboard-final.css",
    ]
    append_css(
        "ui-dashboard.css",
        dashboard_donors,
        "/* Canonical Dashboard continuation: accepted v1.0.11 calibration folded in source order. */",
    )
    append_css(
        "ui-settings-authentication.css",
        ["ui-settings-authentication-oidc.css"],
        "/* Canonical Authentication continuation: OIDC presentation belongs to Authentication. */",
    )

    style = read("style-v11.css")
    for donor in dashboard_donors + ["ui-settings-authentication-oidc.css"]:
        pattern = re.compile(rf"^@import url\('/{re.escape(donor)}\?v=[^']+'\);\n?", re.MULTILINE)
        style, count = pattern.subn("", style)
        if count != 1:
            raise RuntimeError(f"expected one import for {donor}, removed {count}")
    style = style.replace(
        "/* Canonical Dashboard presentation owns base and structural geometry directly.\n   The remaining batch/polish files are live mixed calibration layers because\n   they still contain shell and transfer responsibilities. The final Dashboard\n   override remains late so its accepted precedence over those mixed layers and\n   shared utility controls is preserved. */",
        "/* Canonical Dashboard presentation owns its accepted v1.0.11 calibration directly.\n   Shared utility controls remain a distinct cross-page contract. */",
    )
    write("style-v11.css", style)


def verify() -> None:
    retired = [
        "ui-dashboard-batch5.css",
        "ui-dashboard-polish.css",
        "ui-dashboard-polish-final.css",
        "ui-dashboard-final.css",
        "ui-settings-authentication-oidc.css",
    ]
    for name in retired:
        if (STATIC / name).exists():
            raise RuntimeError(f"retired stylesheet remains: {name}")
        if name in read("style-v11.css"):
            raise RuntimeError(f"retired stylesheet is still imported: {name}")

    accessibility = read("ui-accessibility-runtime.js")
    error = read("ui-error-semantics.js")
    operator = read("operator-title.js")
    app = read("app.js")
    if accessibility.count("new MutationObserver") != 3:
        raise RuntimeError("accessibility observer count drifted")
    if "MutationObserver" in error:
        raise RuntimeError("error semantics retains a convergence observer")
    if re.search(r"window\.(?:nav|progress|badge|toast|updateOperatorTitle|updateThemeToggle|toggleTheme)\s*=", accessibility + error + operator):
        raise RuntimeError("presentation runtime still monkey-patches canonical app owners")
    for required in [
        "function updateOperatorTitle(stats)",
        "dp-terminal-error-progress",
        "window.DPIcons.statusBadge",
        "window.DPIcons.toast",
        "window.DPIcons.renderThemeGlyph",
    ]:
        if required not in app:
            raise RuntimeError(f"canonical app owner missing {required}")


def main() -> None:
    canonicalize_app()
    canonicalize_operator_runtime()
    canonicalize_error_semantics()
    canonicalize_accessibility()
    canonicalize_css()
    verify()
    print("canonical frontend runtime and straightforward historical CSS ownership applied")


if __name__ == "__main__":
    main()
