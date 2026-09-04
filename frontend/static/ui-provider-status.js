/* Neutral download-provider status presentation.
 *
 * Canonical integration metadata supplies display identity/order and declares
 * how operational truth is obtained. This renderer never treats enabled or
 * configured state as proof of health.
 */
(function () {
  'use strict';

  let generation = 0;

  function invalidate() {
    generation += 1;
    return generation;
  }

  function integrationsOf(settings) {
    const integrations = settings && settings.integrations;
    return integrations && typeof integrations === 'object' ? integrations : null;
  }

  function candidates(settings) {
    const integrations = integrationsOf(settings);
    if (!integrations) return null;
    return Object.entries(integrations)
      .filter(([, integration]) => {
        const presentation = integration && integration.presentation;
        return integration && integration.kind === 'provider' && integration.enabled !== false &&
          presentation && String(presentation.status_name || '').trim();
      })
      .map(([id, integration]) => ({
        id,
        name: String(integration.presentation.status_name),
        configured: !!integration.configured,
        premium: !!integration.presentation.premium,
        endpoint: String(integration.presentation.status_endpoint || '').trim(),
        staticStatus: String(integration.presentation.static_status || '').trim(),
        order: Number.isFinite(Number(integration.presentation.display_order))
          ? Number(integration.presentation.display_order) : 100,
      }))
      .sort((left, right) => left.order - right.order || left.name.localeCompare(right.name) || left.id.localeCompare(right.id));
  }

  function host() {
    let node = document.getElementById('provider-status-list');
    if (node) return node;

    node = document.createElement('div');
    node.id = 'provider-status-list';
    node.className = 'dp-provider-status-list';
    node.setAttribute('aria-label', 'Provider Status');
    const footer = document.querySelector('.sidebar-footer');
    const aria2Row = document.getElementById('dot-aria2')?.closest('.conn-row');
    if (footer) footer.insertBefore(node, aria2Row || footer.firstChild);
    return node;
  }

  function dotState(state) {
    return ({
      healthy: 'ok',
      auth_required: 'error',
      unhealthy: 'error',
      unconfigured: 'warn',
      unknown: 'check',
    })[state] || 'check';
  }

  function render(entries, mode = 'ready') {
    const node = host();
    if (!node) return;
    if (mode === 'loading') {
      node.innerHTML = '<div class="conn-row dp-provider-status-row" data-provider-state="checking"><div class="dot check"></div><span>Provider Status: checking…</span></div>';
      return;
    }
    if (mode === 'unknown') {
      node.innerHTML = '<div class="conn-row dp-provider-status-row" data-provider-state="unknown"><div class="dot check"></div><span>Provider Status unavailable</span></div>';
      return;
    }
    if (!entries.length) {
      node.innerHTML = '<div class="conn-row dp-provider-status-row" data-provider-state="inactive"><div class="dot warn"></div><span>No download providers enabled</span></div>';
      return;
    }
    node.innerHTML = entries.map(entry =>
      `<div class="conn-row dp-provider-status-row" data-provider-id="${escapeAttribute(entry.id)}" data-provider-state="${escapeAttribute(entry.state)}">` +
      `<div class="dot ${dotState(entry.state)}"></div><span>${escapeText(entry.name)}</span></div>`
    ).join('');
  }

  function escapeText(value) {
    return String(value ?? '').replace(/[&<>"']/g, character => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[character]);
  }

  function escapeAttribute(value) {
    return escapeText(value);
  }

  async function observe(candidate) {
    if (candidate.staticStatus) return {...candidate, state: candidate.staticStatus, status: {state: candidate.staticStatus}};
    if (!candidate.endpoint) return {...candidate, state: 'unknown', status: {state: 'unknown'}};
    try {
      const status = await api('GET', candidate.endpoint);
      return {...candidate, state: String(status?.state || 'unknown'), status};
    } catch (_) {
      return {...candidate, state: 'unknown', status: {state: 'unknown'}};
    }
  }

  async function refresh() {
    const ownedGeneration = invalidate();
    let settings;
    try { settings = settingsData; } catch (_) { settings = null; }
    const providers = candidates(settings);
    if (providers === null) {
      render([], 'unknown');
      return null;
    }

    const observations = await Promise.all(providers.map(observe));
    if (ownedGeneration !== generation) return null;

    const entries = observations.filter(entry => entry.state !== 'disabled');
    render(entries);
    document.dispatchEvent(new CustomEvent('debridpulse:provider-status', {
      detail: {entries, generation: ownedGeneration},
    }));
    return entries;
  }

  window.DPProviderStatus = Object.freeze({refresh, invalidate, candidates});

  render([], 'loading');
  document.addEventListener('DOMContentLoaded', () => {
    refresh().catch(() => render([], 'unknown'));
  }, {once: true});
})();
