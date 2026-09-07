/* DebridPulse first-paint theme and runtime dependency bootstrap.
 * Parser-deferred application modules initialize before DOMContentLoaded. Load
 * the topbar concurrency projection at that boundary so it can wrap app.js's
 * badge renderer instead of remaining an orphaned static asset.
 */
(function () {
  'use strict';

  try {
    if (localStorage.getItem('theme') === 'light') document.body.classList.add('light');
  } catch (_) {}

  function loadTopbarConcurrency() {
    if (window.DPTopbarConcurrency) return;
    if (document.querySelector('script[data-dp-topbar-concurrency="1"]')) return;

    const script = document.createElement('script');
    script.src = '/ui-topbar-concurrency.js?v=1';
    script.dataset.dpTopbarConcurrency = '1';
    document.head.appendChild(script);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', loadTopbarConcurrency, {once: true});
  } else {
    loadTopbarConcurrency();
  }
})();
