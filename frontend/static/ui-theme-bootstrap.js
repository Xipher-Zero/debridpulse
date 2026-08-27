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

  /* Visual-review behavior corrections are deliberately isolated from app.js.
     They own only first-paint shell hydration and action-icon semantics. */
  if (!document.querySelector('script[data-dp-visual-behavior-fixes]')) {
    const script = document.createElement('script');
    script.src = '/ui-visual-behavior-fixes.js?v=21';
    script.defer = true;
    script.dataset.dpVisualBehaviorFixes = '1';
    document.head.appendChild(script);
  }

  /* Failure presentation is intentionally a separate late runtime. It waits
     until DOMContentLoaded so app.js and the canonical Lucide layer already own
     the legacy rendering functions before it installs presentation overrides. */
  if (!document.querySelector('script[data-dp-error-semantics]')) {
    const script = document.createElement('script');
    script.src = '/ui-error-semantics.js?v=20';
    script.defer = true;
    script.dataset.dpErrorSemantics = '1';
    document.head.appendChild(script);
  }
})();
