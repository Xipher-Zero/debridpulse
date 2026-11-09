/* Canonical global storage-health presentation and transition owner. */
(function () {
  'use strict';

  const POLL_MS = 60000;
  // Words are the catalog's (storage.*), said when drawn.
  const t = (key, values) => window.DPI18n.t(key, values);
  const DOMAIN_DEFINITIONS = Object.freeze([
    Object.freeze({key: 'application_state'}),
    Object.freeze({key: 'download'}),
  ]);
  const STATES = new Set(['healthy', 'low_space', 'full', 'read_only', 'unavailable']);

  const observed = new Map();
  let currentHealth = null;
  let refreshInFlight = null;
  let pollTimer = null;
  let started = false;

  function isFiniteNumber(value) {
    return typeof value === 'number' && Number.isFinite(value);
  }

  function formatBytes(value) {
    if (!isFiniteNumber(value)) return null;
    if (value === 0) return t('format.size.b', {value: window.DPI18n.number(0)});

    if (typeof window.fmtSize === 'function') {
      const formatted = window.fmtSize(value);
      if (formatted && formatted !== '—') return formatted;
    }

    const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    let amount = Math.max(0, value);
    let unit = 0;
    while (amount >= 1024 && unit < units.length - 1) {
      amount /= 1024;
      unit += 1;
    }
    return t(`format.size.${units[unit].toLowerCase()}`, {value: window.DPI18n.number(amount, unit === 0 ? 0 : 1)});
  }

  function formatCapacity(snapshot) {
    const free = formatBytes(snapshot && snapshot.free_bytes);
    const total = formatBytes(snapshot && snapshot.total_bytes);
    if (free && total) return t('storage.capacity.free_of', {free, total});
    if (free) return t('storage.capacity.free', {free});
    if (total) return t('storage.capacity.total', {total});
    return '';
  }

  function isDegraded(snapshot) {
    return Boolean(snapshot && snapshot.state && snapshot.state !== 'healthy');
  }

  function stateLabel(state) {
    return t(`storage.state.${STATES.has(state) ? state : 'unavailable'}`);
  }

  // One whole sentence per storage and degraded state (unknown states read as unavailable).
  function explanation(domainKey, state) {
    const domain = domainKey === 'application_state' ? 'application' : 'download';
    return t(`storage.explanation.${domain}.${['low_space', 'full', 'read_only'].includes(state) ? state : 'unavailable'}`);
  }

  function recoveryMessage(domainKey) {
    return t(domainKey === 'download' ? 'storage.recovered.download' : 'storage.recovered.application');
  }

  function transitionIdentity(snapshot) {
    if (!snapshot) return 'missing';
    const generation = snapshot.generation == null ? 'na' : String(snapshot.generation);
    const transitioned = snapshot.transitioned_at == null ? 'na' : String(snapshot.transitioned_at);
    return `${String(snapshot.state || 'unknown')}|${generation}|${transitioned}`;
  }

  function notify(message, type, attempt) {
    const tries = Number(attempt || 0);
    if (typeof window.toast === 'function') {
      window.toast(message, type);
      return;
    }
    if (tries < 20) {
      window.setTimeout(() => notify(message, type, tries + 1), 50);
    }
  }

  function processTransition(definition, snapshot) {
    if (!snapshot || typeof snapshot !== 'object') return;

    const state = String(snapshot.state || 'healthy');
    const identity = transitionIdentity(snapshot);
    const previous = observed.get(definition.key);

    if (!previous) {
      observed.set(definition.key, {state, identity});
      if (isDegraded(snapshot)) {
        notify(explanation(definition.key, state), 'error');
      }
      return;
    }

    const materiallyChanged = previous.state !== state || previous.identity !== identity;
    if (materiallyChanged && isDegraded(snapshot)) {
      notify(explanation(definition.key, state), 'error');
    } else if (materiallyChanged && state === 'healthy' && previous.state !== 'healthy') {
      notify(recoveryMessage(definition.key), 'success');
    }

    observed.set(definition.key, {state, identity});
  }

  function ensureStylesheet() {
    if (document.querySelector('link[data-dp-storage-health-style]')) return;
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = '/ui-storage-health.css?v=1';
    link.dataset.dpStorageHealthStyle = '1';
    document.head.appendChild(link);
  }

  function ensureRegion() {
    let region = document.getElementById('dp-storage-health');
    if (region) return region;

    const main = document.getElementById('main');
    const content = document.getElementById('content');
    if (!main || !content) return null;

    region = document.createElement('section');
    region.id = 'dp-storage-health';
    region.className = 'dp-storage-health-region';
    region.hidden = true;
    region.setAttribute('role', 'status');
    region.setAttribute('aria-live', 'polite');
    region.setAttribute('aria-atomic', 'false');
    region.setAttribute('aria-label', t('storage.region_label'));
    main.insertBefore(region, content);
    return region;
  }

  function createWarning(definition, snapshot) {
    const article = document.createElement('article');
    article.className = 'dp-storage-warning';
    article.dataset.storageDomain = definition.key;
    article.dataset.storageState = String(snapshot.state || 'unavailable');

    const header = document.createElement('div');
    header.className = 'dp-storage-warning__header';

    const title = document.createElement('strong');
    title.className = 'dp-storage-warning__domain';
    title.textContent = t(definition.key === 'application_state' ? 'storage.domain.application' : 'storage.domain.download');

    const condition = document.createElement('span');
    condition.className = 'dp-storage-warning__state';
    condition.textContent = stateLabel(String(snapshot.state || 'unavailable'));

    header.append(title, condition);

    const copy = document.createElement('p');
    copy.className = 'dp-storage-warning__copy';
    copy.textContent = explanation(definition.key, String(snapshot.state || 'unavailable'));

    article.append(header, copy);

    const capacity = formatCapacity(snapshot);
    if (capacity) {
      const capacityLine = document.createElement('p');
      capacityLine.className = 'dp-storage-warning__capacity';
      capacityLine.textContent = capacity;
      article.appendChild(capacityLine);
    }

    return article;
  }

  function render(health) {
    const region = ensureRegion();
    if (!region) return;

    const warnings = [];
    for (const definition of DOMAIN_DEFINITIONS) {
      const snapshot = health && health[definition.key];
      if (isDegraded(snapshot)) warnings.push(createWarning(definition, snapshot));
    }

    region.replaceChildren(...warnings);
    region.hidden = warnings.length === 0;
  }

  // A language change says the warnings (and the region's name) again.
  document.addEventListener('debridpulse:locale-changed', () => {
    const region = document.getElementById('dp-storage-health');
    if (region) region.setAttribute('aria-label', t('storage.region_label'));
    if (currentHealth) render(currentHealth);
  });

  function validateHealth(payload) {
    return Boolean(
      payload
      && typeof payload === 'object'
      && payload.application_state
      && typeof payload.application_state === 'object'
      && payload.download
      && typeof payload.download === 'object'
    );
  }

  async function requestHealth() {
    const response = await window.debridPulseAuth.fetch('/api/storage/health', {
      method: 'GET',
      headers: {'Accept': 'application/json'},
      cache: 'no-store',
    });
    if (!response.ok) throw new Error(`Storage health request failed (${response.status})`);
    const payload = await response.json();
    if (!validateHealth(payload)) throw new Error('Storage health response is incomplete');
    return payload;
  }

  function refresh() {
    if (refreshInFlight) return refreshInFlight;

    refreshInFlight = requestHealth()
      .then(payload => {
        for (const definition of DOMAIN_DEFINITIONS) {
          processTransition(definition, payload[definition.key]);
        }
        currentHealth = payload;
        render(payload);
        return payload;
      })
      .finally(() => {
        refreshInFlight = null;
      });

    return refreshInFlight;
  }

  function start() {
    if (started) return;
    started = true;
    ensureStylesheet();
    ensureRegion();
    refresh().catch(() => {});
    pollTimer = window.setInterval(() => {
      refresh().catch(() => {});
    }, POLL_MS);
  }

  function stop() {
    if (pollTimer != null) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  window.DPStorageHealth = Object.freeze({
    refresh,
    snapshot: () => currentHealth,
    stop,
  });

  // Storage health is administration: an account without it never polls.
  if (!window.debridPulseAuth?.can('administer')) return;
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, {once: true});
  } else {
    start();
  }
})();
