/* DebridPulse v1.0.11 first-paint theme bootstrap.
 *
 * This synchronous bootstrap has exactly one first-paint responsibility: apply
 * the stored palette before the visible shell is parsed. All other presentation
 * runtimes are loaded only after DOMContentLoaded has finished so a page-local
 * presentation failure cannot prevent app.js or the parser-deferred core
 * runtimes from initializing.
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

  function loadPresentationRuntime() {
    if (document.querySelector('script[data-dp-presentation-loader]')) return;

    const script = document.createElement('script');
    script.src = '/ui-presentation-loader.js?v=1';
    script.async = false;
    script.dataset.dpPresentationLoader = '1';
    script.onerror = function () {
      console.error('[DebridPulse] presentation loader failed; core application remains available.');
    };
    document.head.appendChild(script);
  }

  function schedulePresentationRuntime() {
    /* The zero-delay task runs after all DOMContentLoaded listeners, including
       app.js initialization. It is intentionally the only timer in this file. */
    window.setTimeout(loadPresentationRuntime, 0);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', schedulePresentationRuntime, {once: true});
  } else {
    schedulePresentationRuntime();
  }
})();
