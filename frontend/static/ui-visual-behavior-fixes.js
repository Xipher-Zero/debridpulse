/* DebridPulse v1.0.11 live-review behavior corrections.
 *
 * Scope is intentionally narrow:
 *   1. Keep the aria2 topbar control structurally present at first paint with
 *      neutral values, then hydrate it asynchronously once settings/runtime are
 *      available.
 *   2. Make the theme icon describe the action/destination: sun while dark,
 *      moon while light.
 *
 * Core transfer behavior and aria2 ownership remain in app.js/backend code.
 */
(function () {
  'use strict';

  function syncThemeActionIcon() {
    const button = document.getElementById('theme-toggle');
    if (!button) return;

    const isLight = document.body.classList.contains('light');
    const action = isLight ? 'Switch to dark mode' : 'Switch to light mode';
    const icon = isLight ? '☾' : '☀︎';

    if (button.textContent !== icon) button.textContent = icon;
    if (button.title !== action) button.title = action;
    if (button.getAttribute('aria-label') !== action) {
      button.setAttribute('aria-label', action);
    }
  }

  function installThemeActionSemantics() {
    const button = document.getElementById('theme-toggle');
    if (!button || button.dataset.dpActionSemantics === '1') return;

    button.dataset.dpActionSemantics = '1';
    syncThemeActionIcon();

    const observer = new MutationObserver(syncThemeActionIcon);
    observer.observe(document.body, {
      attributes: true,
      attributeFilter: ['class'],
    });
    observer.observe(button, {
      childList: true,
      characterData: true,
      subtree: true,
    });
  }

  function initializeAria2TopbarPlaceholder() {
    const badge = document.getElementById('aria2-speed-badge');
    if (!badge) return;

    /* app.js owns this state via `var`, so it is safe to seed neutral values
       here. Using string "0" for maxDl preserves a visible zero through the
       legacy `s.maxDl || '—'` renderer until real runtime data replaces it. */
    if (typeof window._aria2BadgeState === 'object' && window._aria2BadgeState) {
      window._aria2BadgeState.active = 0;
      window._aria2BadgeState.maxDl = '0';
      window._aria2BadgeState.liveBps = 0;
      window._aria2BadgeState.limitBps = 0;
      window._aria2BadgeState.externalControl = false;
    }

    const active = document.getElementById('aria2-badge-active');
    const max = document.getElementById('aria2-badge-max');
    const speed = document.getElementById('aria2-badge-speed');
    const limit = document.getElementById('aria2-badge-limit');

    if (active) active.textContent = '0';
    if (max) max.textContent = '0';
    if (speed) speed.textContent = '0 KB/s';
    if (limit) limit.textContent = 'Unlimited';

    badge.style.display = 'flex';
    badge.dataset.dpInitialPlaceholder = '1';
  }

  function hydrateAria2TopbarSoon() {
    let attempts = 0;
    const maxAttempts = 40;

    const attempt = function () {
      attempts += 1;

      let hasSettings = false;
      try {
        hasSettings =
          typeof settingsData !== 'undefined' &&
          settingsData &&
          Object.keys(settingsData).length > 0;
      } catch (_) {
        hasSettings = false;
      }

      if (!hasSettings) {
        if (attempts < maxAttempts) setTimeout(attempt, 100);
        return;
      }

      if (typeof window.loadAria2Runtime === 'function') {
        Promise.resolve(window.loadAria2Runtime()).catch(function () {});
      }
    };

    setTimeout(attempt, 0);
  }

  function initialize() {
    installThemeActionSemantics();
    initializeAria2TopbarPlaceholder();
    hydrateAria2TopbarSoon();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initialize, {once: true});
  } else {
    initialize();
  }
})();
