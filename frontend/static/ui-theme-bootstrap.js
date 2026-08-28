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

  /* Authentication and touch surfaces keep the reviewed raster derivative.
     The application shell uses the vector mark directly so its integrated
     outline remains the only frame in both themes. The browser tab separately
     keeps the compact favicon mark because it remains more legible at tab size. */
  function installReviewedBrandAssets() {
    const vectorIcon = document.querySelector('link[rel="icon"][type="image/svg+xml"]');
    if (vectorIcon) {
      vectorIcon.type = 'image/svg+xml';
      vectorIcon.removeAttribute('sizes');
      vectorIcon.href = '/favicon.svg?v=6';
    }

    /* Do not allow the newer 32px raster fallback to win favicon selection over
       the restored original vector mark. Modern supported browsers use SVG. */
    const icon32 = document.querySelector('link[rel="icon"][sizes="32x32"]');
    if (icon32) icon32.remove();

    const apple = document.querySelector('link[rel="apple-touch-icon"]');
    if (apple) apple.href = '/apple-touch-icon.png?v=5';

    const installSidebarLogo = function () {
      const logo = document.querySelector('#sidebar .logo-icon');
      if (!logo) return false;
      if (logo.getAttribute('src') !== '/logo.svg?v=7') {
        logo.setAttribute('src', '/logo.svg?v=7');
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

  /* These presentation runtimes are dynamically inserted classic scripts.
     `defer` does not establish execution order for dynamic scripts, so keep
     async=false on the Statistics chain. That makes the reviewed layers execute
     in insertion order rather than whichever network/cache fetch finishes first. */

  /* Visual-review behavior corrections are deliberately isolated from app.js.
     They own only first-paint shell hydration, action-icon semantics and
     presentation-only Statistics composition. */
  if (!document.querySelector('script[data-dp-visual-behavior-fixes]')) {
    const script = document.createElement('script');
    script.async = false;
    script.src = '/ui-visual-behavior-fixes.js?v=22';
    script.dataset.dpVisualBehaviorFixes = '1';
    document.head.appendChild(script);
  }

  /* Statistics Batch 3 is presentation-only. Keep it isolated from app.js so
     the existing statistics API, Chart.js lifecycle and transfer semantics stay
     authoritative while the reviewed wording/layout is applied after render. */
  if (!document.querySelector('script[data-dp-statistics-batch3]')) {
    const script = document.createElement('script');
    script.async = false;
    script.src = '/ui-statistics-batch3.js?v=2';
    script.dataset.dpStatisticsBatch3 = '1';
    document.head.appendChild(script);
  }

  /* Statistics Batch 4 owns adaptive lower-card presentation and optical
     normalization while preserving Batch 3's semantic copy. */
  if (!document.querySelector('script[data-dp-statistics-batch4]')) {
    const script = document.createElement('script');
    script.async = false;
    script.src = '/ui-statistics-batch4.js?v=1';
    script.dataset.dpStatisticsBatch4 = '1';
    document.head.appendChild(script);
  }

  /* Statistics Batch 5 is the final reviewed presentation layer. It loads after
     Batch 4 and exclusively owns historical KPI ordering/labels/icons plus
     primary-card ordering, scope-explicit copy, chart-header composition,
     shared surface opt-in and shell-brand presentation. */
  if (!document.querySelector('script[data-dp-statistics-batch5]')) {
    const script = document.createElement('script');
    script.async = false;
    script.src = '/ui-statistics-batch5.js?v=6';
    script.dataset.dpStatisticsBatch5 = '1';
    document.head.appendChild(script);
  }

  /* Settings IA is presentation-only. It leaves the inherited renderer and
     backend configuration model intact, then reorganizes the rendered controls
     into the reviewed source/downstream ownership model. */
  if (!document.querySelector('script[data-dp-settings-architecture]')) {
    const script = document.createElement('script');
    script.src = '/ui-settings-architecture.js?v=1';
    script.defer = true;
    script.dataset.dpSettingsArchitecture = '1';
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
