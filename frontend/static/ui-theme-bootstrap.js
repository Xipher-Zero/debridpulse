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

  /* The reviewed brand artwork is shipped as exact raster derivatives of the
     supplied logo. Repoint the legacy shell/favicon markup before first paint so
     stale compatibility SVGs cannot surface while the v1.0.11 HTML is still
     being migrated. */
  function installReviewedBrandAssets() {
    const vectorIcon = document.querySelector('link[rel="icon"][type="image/svg+xml"]');
    if (vectorIcon) {
      vectorIcon.type = 'image/png';
      vectorIcon.setAttribute('sizes', '128x128');
      vectorIcon.href = '/logo-128.png?v=5';
    }

    const icon32 = document.querySelector('link[rel="icon"][sizes="32x32"]');
    if (icon32) icon32.href = '/favicon-32.png?v=5';

    const apple = document.querySelector('link[rel="apple-touch-icon"]');
    if (apple) apple.href = '/apple-touch-icon.png?v=5';

    const installSidebarLogo = function () {
      const logo = document.querySelector('#sidebar .logo-icon');
      if (!logo) return false;
      if (logo.getAttribute('src') !== '/logo-128.png?v=5') {
        logo.setAttribute('src', '/logo-128.png?v=5');
      }
      return true;
    };

    if (!installSidebarLogo()) {
      const observer = new MutationObserver(function () {
        if (installSidebarLogo()) observer.disconnect();
      });
      observer.observe(document.body, {childList: true, subtree: true});
      document.addEventListener('DOMContentLoaded', function () {
        installSidebarLogo();
        observer.disconnect();
      }, {once: true});
    }
  }

  installReviewedBrandAssets();

  /* Visual-review behavior corrections are deliberately isolated from app.js.
     They own only first-paint shell hydration, action-icon semantics and
     presentation-only Statistics composition. */
  if (!document.querySelector('script[data-dp-visual-behavior-fixes]')) {
    const script = document.createElement('script');
    script.src = '/ui-visual-behavior-fixes.js?v=22';
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
