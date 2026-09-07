/* Settings inner-card icon presentation owner. */
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

  function normalizedTitle(title) {
    const clone = title.cloneNode(true);
    clone.querySelectorAll('.dp-settings-inner-card-icon,[data-dp-settings-replaced-icon="1"]').forEach(node => node.remove());
    return String(clone.textContent || '').replace(/\s+/g, ' ').trim();
  }

  function decorate(title, section, src) {
    if (title.querySelector(':scope > .dp-settings-inner-card-icon')) return;
    Array.from(title.children).forEach(child => {
      if (child.matches('[aria-hidden="true"]') && (child.querySelector('img,svg') || child.matches('.dp-settings-download-engine-icon,.dp-settings-aria2-live-icon'))) {
        child.dataset.dpSettingsReplacedIcon = '1';
        child.classList.add('dp-settings-replaced-legacy-icon');
      }
    });
    title.classList.add('dp-settings-card-title--with-icon', 'dp-settings-inner-card-title');
    title.dataset.dpSettingsIconSection = section;
    const frame = document.createElement('span');
    frame.className = 'dp-settings-inner-card-icon';
    frame.setAttribute('aria-hidden', 'true');
    frame.dataset.section = section;
    const img = document.createElement('img');
    img.src = src;
    img.alt = '';
    img.decoding = 'async';
    frame.appendChild(img);
    title.prepend(frame);
  }

  function apply() {
    const view = document.getElementById('view-settings');
    if (!view) return;
    view.querySelectorAll('.card-header .card-title').forEach(title => {
      const definition = ICONS[normalizedTitle(title)];
      if (definition) decorate(title, definition[0], definition[1]);
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

  window.DPSettingsCardIcons = Object.freeze({apply});
  document.addEventListener('debridpulse:settings-rendered', scheduleApply);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', scheduleApply, {once:true});
  else scheduleApply();
})();