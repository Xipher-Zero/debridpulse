/* DebridPulse v1.0.11 first-paint theme bootstrap.
 * Runs immediately after <body> exists so the stored palette is applied before
 * the visible application shell is parsed. app.js remains the authoritative
 * runtime theme controller after DOMContentLoaded.
 */
(function () {
  'use strict';
  try {
    if (localStorage.getItem('theme') === 'light') {
      document.body.classList.add('light');
    }
  } catch (_) {
    /* Storage can be unavailable in hardened/private browser contexts. */
  }
})();
