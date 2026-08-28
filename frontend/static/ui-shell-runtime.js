/* DebridPulse v1.0.11 shell-brand presentation owner.
 * Runs only after core DOM initialization. Shell branding must not be owned by
 * page-specific runtimes such as Statistics or Settings.
 */
(function () {
  'use strict';

  function loadShellStyles() {
    if (document.querySelector('link[data-dp-shell-brand]')) return;
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = '/ui-shell-brand.css?v=1';
    link.dataset.dpShellBrand = '1';
    document.head.appendChild(link);
  }

  function normalizeShellBranding() {
    const vectorIcon = document.querySelector('link[rel="icon"][type="image/svg+xml"]');
    if (vectorIcon) {
      vectorIcon.type = 'image/svg+xml';
      vectorIcon.removeAttribute('sizes');
      vectorIcon.href = '/favicon.svg?v=6';
    }

    const icon32 = document.querySelector('link[rel="icon"][sizes="32x32"]');
    if (icon32) icon32.remove();

    const apple = document.querySelector('link[rel="apple-touch-icon"]');
    if (apple) apple.href = '/apple-touch-icon.png?v=5';

    const logo = document.querySelector('#sidebar .logo-icon');
    if (logo && logo.getAttribute('src') !== '/logo.svg?v=7') {
      logo.setAttribute('src', '/logo.svg?v=7');
    }

    const version = document.getElementById('sidebar-version');
    if (version) {
      version.classList.add('dp-app-version');
      version.setAttribute('aria-label', 'DebridPulse version');
      if (version.parentElement !== document.body) document.body.appendChild(version);
    }
  }

  loadShellStyles();
  normalizeShellBranding();
})();
