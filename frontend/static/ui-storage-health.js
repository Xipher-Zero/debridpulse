/* Canonical global storage-health presentation and transition owner. */
(function () {
  'use strict';

  const POLL_MS = 60000;
  const DOMAIN_DEFINITIONS = Object.freeze([
    Object.freeze({key: 'application_state', label: 'Application Storage'}),
    Object.freeze({key: 'download', label: 'Download Storage'}),
  ]);
  const STATE_LABELS = Object.freeze({
    healthy: 'Healthy',
    low_space: 'Low space',
    full: 'Full',
    read_only: 'Read only',
    unavailable: 'Unavailable',
  });

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
    if (value === 0) return '0 B';

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
    return `${amount.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
  }

  function formatCapacity(snapshot) {
    const free = formatBytes(snapshot && snapshot.free_bytes);
    const total = formatBytes(snapshot && snapshot.total_bytes);
    if (free && total) return `${free} free of ${total}`;
    if (free) return `${free} free`;
    if (total) return `${total} total`;
    return '';
  }

  function isDegraded(snapshot) {
    return Boolean(snapshot && snapshot.state && snapshot.state !== 'healthy');
  }

  function stateLabel(state) {
    return STATE_LABELS[state] || 'Unavailable';
  }

  function explanation(domainKey, state) {
    const application = domainKey === 'application_state';
    if (state === 'low_space') {
      return application
        ? 'Application Storage is running low on space. DebridPulse may be unable to persist state safely.'
        : 'Download Storage is running low on space. New storage-consuming work is paused until the recovery threshold is reached.';
    }
    if (state === 'full') {
      return application
        ? 'Application Storage is full. DebridPulse cannot safely persist state.'
        : 'Download Storage is full. New downloads are paused until space is available.';
    }
    if (state === 'read_only') {
      return application
        ? 'Application Storage is read only. DebridPulse cannot safely persist state.'
        : 'Download Storage is read only. New downloads are paused until write access is restored.';
    }
    return application
      ? 'Application Storage is unavailable. DebridPulse cannot safely persist state.'
      : 'Download Storage is unavailable or not writable.';
  }

  function recoveryMessage(domainKey) {
    return domainKey === 'download'
      ? 'Download Storage has recovered. New downloads can resume.'
      : 'Application Storage has recovered.';
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
    region.setAttribute('aria-label', 'Storage health warnings');
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
    title.textContent = definition.label;

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
    const response = await fetch('/api/storage/health', {
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

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, {once: true});
  } else {
    start();
  }
})();
