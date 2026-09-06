/* DebridPulse Settings inner-card icon presentation.
 *
 * Applies reviewed custom SVG artwork to Settings inner-card headers only.
 * Provider state/status runtimes are loaded explicitly by the application shell.
 */
(function () {
  'use strict';

  const ICONS = Object.freeze({
    'Download Engine': ['downloads', '/icons/dp/settings/download-engine.svg?v=1'],
    'Download Safety & Recovery': ['downloads', '/icons/dp/settings/download-safety-recovery.svg?v=1'],
    'Built-In Download Engine State': ['downloads', '/icons/dp/settings/built-in-download-engine-state.svg?v=1'],
    'Automatic Extraction': ['extraction', '/icons/dp/settings/automatic-extraction.svg?v=1'],
    'Authentication Status': ['authentication', '/icons/dp/settings/authentication-status.svg?v=1'],
    'Username & Password': ['authentication', '/icons/dp/settings/username-password.svg?v=1'],
    'OpenID Connect': ['authentication', '/icons/dp/settings/openid-connect.svg?v=1'],
    'API Access': ['authentication', '/icons/dp/settings/api-access.svg?v=1'],
    'Discord Notifications': ['notifications', '/icons/dp/settings/discord-notifications.svg?v=1'],
    'Statistics Reporting': ['notifications', '/icons/dp/settings/statistics-reporting.svg?v=1'],
    'Backups & Retention': ['maintenance', '/icons/dp/settings/backups-retention.svg?v=1'],
    'Database Reset Controls': ['maintenance', '/icons/dp/settings/database-reset-controls.svg?v=1'],
  });
  let scheduled = false;

  const root = () => document.getElementById('view-settings');

  function normalizedTitle(title) {
    if (!title) return '';
    const clone = title.cloneNode(true);
    clone.querySelectorAll('.dp-settings-inner-card-icon, [data-dp-settings-replaced-icon="1"]').forEach(node => node.remove());
    return String(clone.textContent || '').replace(/\s+/g, ' ').trim();
  }

  function suppressExistingArtwork(title) {
    Array.from(title.children).forEach(child => {
      if (child.classList.contains('dp-settings-inner-card-icon')) return;
      if (!child.matches('[aria-hidden="true"]')) return;
      if (!child.querySelector('img,svg') && !child.matches('.dp-settings-download-engine-icon,.dp-settings-aria2-live-icon')) return;

      /* Do not remove the legacy node. The older Settings completion runtime
         watches child-list changes and would recreate it. Marking it as replaced
         keeps the DOM stable while the icon stylesheet removes it from layout. */
      child.dataset.dpSettingsReplacedIcon = '1';
      child.classList.add('dp-settings-replaced-legacy-icon');
    });
  }

  function decorateTitle(title, section, src) {
    if (!title || title.querySelector(':scope > .dp-settings-inner-card-icon')) return;

    suppressExistingArtwork(title);
    title.classList.add('dp-settings-card-title--with-icon', 'dp-settings-inner-card-title');
    title.dataset.dpSettingsIconSection = section;

    const frame = document.createElement('span');
    frame.className = 'dp-settings-inner-card-icon';
    frame.setAttribute('aria-hidden', 'true');
    frame.dataset.section = section;

    const image = document.createElement('img');
    image.src = src;
    image.alt = '';
    image.decoding = 'async';
    frame.appendChild(image);
    title.prepend(frame);
  }

  function apply() {
    const view = root();
    if (!view) return;

    view.querySelectorAll('.card-header .card-title').forEach(title => {
      const name = normalizedTitle(title);
      const definition = ICONS[name];
      if (!definition) return;
      decorateTitle(title, definition[0], definition[1]);
    });
  }

  function scheduleApply() {
    if (scheduled) return;
    scheduled = true;
    queueMicrotask(() => {
      scheduled = false;
      apply();
    });
  }

  document.addEventListener('debridpulse:settings-rendered', scheduleApply);
})();

/* Batch 1 final behavior is intentionally loaded after all Settings runtimes so
 * it can retire the last ADC-era interactions without reopening canonical page
 * ownership. */
(function loadBatch1FinalRuntime() {
  'use strict';
  if (document.getElementById('dp-ui-correction-batch1-final-script') || window.DPUICorrectionBatch1Final) return;
  const script = document.createElement('script');
  script.id = 'dp-ui-correction-batch1-final-script';
  script.src = '/ui-correction-batch1-final.js?v=1';
  script.defer = true;
  document.head.appendChild(script);
})();

/* P4 presentation repair is deliberately loaded after the final Batch 1 runtime
 * because it corrects that runtime's projected-select grouping and reveal-button
 * presentation without duplicating its backend or editor state ownership. */
(function loadP4PresentationRepair() {
  'use strict';
  const loadRepair = () => {
    if (document.getElementById('dp-ui-correction-p4-repair-script') || window.DPUICorrectionP4Repair) return;
    const script = document.createElement('script');
    script.id = 'dp-ui-correction-p4-repair-script';
    script.src = '/ui-correction-p4-repair.js?v=1';
    script.defer = true;
    document.head.appendChild(script);
  };

  if (window.DPUICorrectionBatch1Final) {
    loadRepair();
    return;
  }
  const finalScript = document.getElementById('dp-ui-correction-batch1-final-script');
  if (finalScript) finalScript.addEventListener('load', loadRepair, {once: true});
  else document.addEventListener('DOMContentLoaded', loadRepair, {once: true});
})();
