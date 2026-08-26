/* DebridPulse v1.0.6 operator-title state stabilization.
 *
 * The backend already exposes authoritative transfer counts in stats.by_status.
 * Treat queued + downloading as one logical download phase so brief aria2 file
 * handoffs do not make the browser tab appear idle. Throughput remains live and
 * may truthfully fall to zero during a handoff. A short, cancelable idle delay
 * absorbs transient state ordering without masking a real pause.
 */
(function () {
  'use strict';

  const IDLE_CONFIRM_MS = 1500;
  let idleTimer = null;
  let latestLogicalActive = 0;

  function removeLegacyStartupDebugSurface() {
    // app.js still contains a defensive startup retry/debug helper from the
    // inherited UI. The helper already no-ops when this node is absent, so
    // remove only the dashboard presentation while preserving retry behavior,
    // connection indicators, Event Log reporting and backend diagnostics.
    const debugStatus = document.getElementById('debug-status');
    if (debugStatus) debugStatus.remove();
  }

  removeLegacyStartupDebugSurface();

  function nonNegativeCount(value) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return 0;
    return Math.max(0, Math.trunc(parsed));
  }

  function logicalActiveCount(stats) {
    const byStatus = stats && stats.by_status;

    if (byStatus && typeof byStatus === 'object') {
      return nonNegativeCount(byStatus.downloading) +
        nonNegativeCount(byStatus.queued);
    }

    // Compatibility fallback for an older/incomplete stats payload.
    return nonNegativeCount(stats && stats.operator_active_downloads);
  }

  function cancelIdleTimer() {
    if (idleTimer !== null) {
      clearTimeout(idleTimer);
      idleTimer = null;
    }
  }

  function setIdleNow() {
    cancelIdleTimer();
    latestLogicalActive = 0;
    _operatorTitleState.active = 0;
    _operatorTitleState.progress = 0;
    renderOperatorTitle();
  }

  window.updateOperatorTitle = function updateOperatorTitle(stats) {
    const logicalActive = logicalActiveCount(stats);
    latestLogicalActive = logicalActive;

    // A global pause is explicit operator intent; do not delay its presentation.
    if (stats && stats.paused) {
      setIdleNow();
      return;
    }

    if (logicalActive > 0) {
      cancelIdleTimer();
      _operatorTitleState.active = logicalActive;

      const rawProgress = stats && stats.operator_active_progress_pct;
      const progress = rawProgress == null ? NaN : Number(rawProgress);
      if (Number.isFinite(progress)) {
        _operatorTitleState.progress = Math.min(
          100,
          Math.max(0, Math.round(progress))
        );
      }

      renderOperatorTitle();
      return;
    }

    if (_operatorTitleState.active === 0) {
      cancelIdleTimer();
      renderOperatorTitle();
      return;
    }

    // Do not let one transient zero sample erase an active logical transfer.
    // If queued/downloading work reappears before the delay expires, the timer
    // is cancelled above. Otherwise the title settles to idle promptly.
    if (idleTimer === null) {
      idleTimer = setTimeout(function () {
        idleTimer = null;
        if (latestLogicalActive === 0) {
          _operatorTitleState.active = 0;
          _operatorTitleState.progress = 0;
          renderOperatorTitle();
        }
      }, IDLE_CONFIRM_MS);
    }

    renderOperatorTitle();
  };
})();

/* DebridPulse v1.0.11 shell presentation integration.
 *
 * This is a deliberately tiny locally vendored Lucide subset rather than a
 * runtime CDN dependency. Geometry is sourced from lucide-icons/lucide commit
 * 23f9abc4ed0146cffededd3d7f94c1018bfdf693. The corresponding ISC/MIT notices
 * are bundled in licenses/Lucide-ISC-MIT.txt.
 */
(function () {
  'use strict';

  const LUCIDE = {
    dashboard: '<rect width="7" height="9" x="3" y="3" rx="1"/><rect width="7" height="5" x="14" y="3" rx="1"/><rect width="7" height="9" x="14" y="12" rx="1"/><rect width="7" height="5" x="3" y="16" rx="1"/>',
    download: '<path d="M12 15V3"/><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/>',
    logs: '<path d="M3 5h1"/><path d="M3 12h1"/><path d="M3 19h1"/><path d="M8 5h1"/><path d="M8 12h1"/><path d="M8 19h1"/><path d="M13 5h8"/><path d="M13 12h8"/><path d="M13 19h8"/>',
    statistics: '<path d="M5 21v-6"/><path d="M12 21V9"/><path d="M19 21V3"/>',
    settings: '<path d="M9.671 4.136a2.34 2.34 0 0 1 4.659 0 2.34 2.34 0 0 0 3.319 1.915 2.34 2.34 0 0 1 2.33 4.033 2.34 2.34 0 0 0 0 3.831 2.34 2.34 0 0 1-2.33 4.033 2.34 2.34 0 0 0-3.319 1.915 2.34 2.34 0 0 1-4.659 0 2.34 2.34 0 0 0-3.32-1.915 2.34 2.34 0 0 1-2.33-4.033 2.34 2.34 0 0 0 0-3.831A2.34 2.34 0 0 1 6.35 6.051a2.34 2.34 0 0 0 3.319-1.915"/><circle cx="12" cy="12" r="3"/>',
    help: '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/>',
    menu: '<path d="M4 5h16"/><path d="M4 12h16"/><path d="M4 19h16"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/>',
    moon: '<path d="M20.985 12.486a9 9 0 1 1-9.473-9.472c.405-.022.617.46.402.803a6 6 0 0 0 8.268 8.268c.344-.215.825-.004.803.401"/>',
    pause: '<rect x="14" y="3" width="5" height="18" rx="1"/><rect x="5" y="3" width="5" height="18" rx="1"/>',
    play: '<path d="M5 5a2 2 0 0 1 3.008-1.728l11.997 6.998a2 2 0 0 1 .003 3.458l-12 7A2 2 0 0 1 5 19z"/>',
    chevronDown: '<path d="m6 9 6 6 6-6"/>'
  };

  function lucideSvg(name, extraClass) {
    const geometry = LUCIDE[name];
    if (!geometry) return '';
    const cls = ['lucide', 'dp-utility-icon', extraClass || ''].filter(Boolean).join(' ');
    return '<svg class="' + cls + '" data-dp-lucide="' + name + '" xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' + geometry + '</svg>';
  }

  function decorateNavigation() {
    const iconByView = {
      dashboard: 'dashboard',
      torrents: 'download',
      events: 'logs',
      stats: 'statistics',
      settings: 'settings',
      help: 'help'
    };

    document.querySelectorAll('#sidebar .nav-item[data-view]').forEach(function (item) {
      const holder = item.querySelector('.icon');
      const iconName = iconByView[item.dataset.view];
      if (holder && iconName) holder.innerHTML = lucideSvg(iconName);
    });
  }

  function decorateMobileMenu() {
    const button = document.getElementById('mobile-menu-btn');
    if (button) button.innerHTML = lucideSvg('menu');
  }

  function renderThemeGlyph(isLight) {
    const button = document.getElementById('theme-toggle');
    if (!button) return;
    button.innerHTML = lucideSvg(isLight ? 'sun' : 'moon');
  }

  function decorateAria2CapChevron() {
    const arrow = document.querySelector('#aria2-cap-toggle span[aria-hidden="true"]');
    if (arrow && !arrow.querySelector('[data-dp-lucide="chevronDown"]')) {
      arrow.innerHTML = lucideSvg('chevronDown');
    }
  }

  function decorateActionButton(id, iconName) {
    const button = document.getElementById(id);
    if (!button || button.dataset.pending === '1') return;

    const label = button.dataset.defaultLabel || button.textContent.trim();
    const existingIcon = button.querySelector('[data-dp-lucide="' + iconName + '"]');
    const existingLabel = button.querySelector('[data-dp-shell-label]');
    if (existingIcon && existingLabel && existingLabel.textContent === label) return;

    button.innerHTML = lucideSvg(iconName);
    const span = document.createElement('span');
    span.dataset.dpShellLabel = '1';
    span.textContent = label;
    button.appendChild(span);
  }

  function decorateTopbarActions() {
    decorateActionButton('btn-pause-all', 'pause');
    decorateActionButton('btn-resume-all', 'play');
    decorateActionButton('btn-resume-paused', 'play');
    decorateAria2CapChevron();
  }

  const legacyUpdateThemeToggle = window.updateThemeToggle;
  if (typeof legacyUpdateThemeToggle === 'function') {
    window.updateThemeToggle = function updateThemeToggleV11(isLight) {
      legacyUpdateThemeToggle(isLight);
      renderThemeGlyph(!!isLight);
    };
  }

  const legacyToggleTheme = window.toggleTheme;
  if (typeof legacyToggleTheme === 'function') {
    window.toggleTheme = function toggleThemeV11() {
      legacyToggleTheme();
      renderThemeGlyph(document.body.classList.contains('light'));
    };
  }

  function bindThemeToggle() {
    const button = document.getElementById('theme-toggle');
    const control = button && button.closest('.sidebar-theme-control');
    const topbar = document.getElementById('topbar');
    if (!button || !control || !topbar) return;

    /* Put the control in the topbar in the DOM as well as visually. The prior
       fixed-position descendant of the scrolling sidebar/nav could render in
       the right place while still having an unreliable pointer hit target. */
    control.classList.add('topbar-theme-control');
    if (control.parentElement !== topbar) topbar.appendChild(control);

    if (button.dataset.dpThemeBound === '1') return;

    /* Own exactly one click path. Bypass any inherited inline/global toggle
       indirection, then call the established update routine so chart colors,
       labels, accessibility text, and the Lucide glyph all stay synchronized. */
    button.removeAttribute('onclick');
    button.onclick = function (event) {
      event.preventDefault();
      event.stopPropagation();
      const isLight = document.body.classList.toggle('light');
      localStorage.setItem('theme', isLight ? 'light' : 'dark');
      if (typeof window.updateThemeToggle === 'function') {
        window.updateThemeToggle(isLight);
      } else {
        renderThemeGlyph(isLight);
      }
    };
    button.dataset.dpThemeBound = '1';
  }

  function initializeShellPresentation() {
    decorateNavigation();
    decorateMobileMenu();
    renderThemeGlyph(document.body.classList.contains('light'));
    decorateTopbarActions();
    bindThemeToggle();

    const actionHost = document.getElementById('topbar-actions');
    if (actionHost && !actionHost.dataset.dpShellObserved) {
      actionHost.dataset.dpShellObserved = '1';
      new MutationObserver(function () {
        decorateTopbarActions();
      }).observe(actionHost, {childList: true, subtree: true, characterData: true});
    }
  }

  initializeShellPresentation();
  document.addEventListener('DOMContentLoaded', initializeShellPresentation, {once: true});
})();

/* Keep page-specific v1.0.11 presentation code out of the stabilized operator
 * title shim. The dedicated runtime is local and cache-versioned with the new UI. */
(function () {
  'use strict';
  if (document.querySelector('script[data-dp-ui-runtime]')) return;
  const script = document.createElement('script');
  script.src = '/ui-runtime.js?v=24';
  script.defer = true;
  script.dataset.dpUiRuntime = '1';
  document.head.appendChild(script);
})();

/* Downloads uses a separate presentation shim so the stabilized application
 * loader remains untouched while page-by-page v1.0.11 migration continues. */
(function () {
  'use strict';
  if (document.querySelector('script[data-dp-downloads-runtime]')) return;
  const script = document.createElement('script');
  script.src = '/ui-downloads-runtime.js?v=22';
  script.defer = true;
  script.dataset.dpDownloadsRuntime = '1';
  document.head.appendChild(script);
})();
