/* DebridPulse v1.0.11 Settings inner-card icon presentation.
 *
 * Applies the reviewed custom SVG artwork to Settings inner-card headers only.
 * The clean Settings renderer and existing presentation runtimes remain the
 * owners of card structure and behavior. This layer only replaces/places the
 * visual identity artwork and reapplies after clean-room Settings rerenders.
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

  let observer = null;
  let scheduled = false;

  const root = () => document.getElementById('view-settings');

  function normalizedTitle(title) {
    if (!title) return '';
    const clone = title.cloneNode(true);
    clone.querySelectorAll('.dp-settings-inner-card-icon, [data-dp-settings-replaced-icon="1"]').forEach(node => node.remove());
    return String(clone.textContent || '').replace(/\s+/g, ' ').trim();
  }

  function removeExistingArtwork(title) {
    Array.from(title.children).forEach(child => {
      if (child.classList.contains('dp-settings-inner-card-icon')) return;
      if (!child.matches('[aria-hidden="true"]')) return;
      if (!child.querySelector('img,svg') && !child.matches('.dp-settings-download-engine-icon,.dp-settings-aria2-live-icon')) return;
      child.dataset.dpSettingsReplacedIcon = '1';
      child.remove();
    });
  }

  function decorateTitle(title, section, src) {
    if (!title || title.querySelector(':scope > .dp-settings-inner-card-icon')) return;

    removeExistingArtwork(title);
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

  function observe(view) {
    if (!observer || !view) return;
    observer.observe(view, {childList: true, subtree: false});
  }

  function applyWithoutSelfObservation() {
    const view = root();
    if (!view) return;
    observer?.disconnect();
    try {
      apply();
    } finally {
      observe(view);
    }
  }

  function scheduleApply() {
    if (scheduled) return;
    scheduled = true;
    queueMicrotask(() => {
      scheduled = false;
      applyWithoutSelfObservation();
    });
  }

  function bind() {
    const view = root();
    if (!view) return;

    observer?.disconnect();
    observer = new MutationObserver(scheduleApply);
    applyWithoutSelfObservation();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind, {once: true});
  } else {
    bind();
  }
})();
