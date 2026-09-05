/* Neutral provider/direct-source status presentation owner. */
(function () {
  'use strict';

  let generation = 0;

  function invalidate() {
    generation += 1;
    return generation;
  }

  function integrationsOf(settings) {
    const value = settings && settings.integrations;
    return value && typeof value === 'object' ? value : null;
  }

  function candidates(settings) {
    const integrations = integrationsOf(settings);
    if (!integrations) return null;
    return Object.entries(integrations)
      .filter(([, integration]) => (
        integration.kind === 'provider'
        && String(integration.presentation?.status_name || '').trim()
      ))
      .map(([id, integration]) => ({
        id,
        name: String(integration.presentation.status_name),
        enabled: integration.enabled !== false,
        configured: Boolean(integration.configured),
        premium: Boolean(integration.presentation.premium),
        endpoint: String(integration.presentation.status_endpoint || '').trim(),
        staticStatus: String(integration.presentation.static_status || '').trim(),
        order: Number.isFinite(Number(integration.presentation.display_order))
          ? Number(integration.presentation.display_order)
          : 100,
        groupId: String(integration.presentation.status_group || '').trim(),
        groupLabel: String(integration.presentation.status_group_label || '').trim(),
      }))
      .sort((a, b) => a.order - b.order || a.name.localeCompare(b.name) || a.id.localeCompare(b.id));
  }

  function host() {
    let node = document.getElementById('provider-status-list');
    if (node) return node;
    node = document.createElement('div');
    node.id = 'provider-status-list';
    node.className = 'dp-provider-status-list';
    node.setAttribute('aria-label', 'Provider Status');
    const footer = document.querySelector('.sidebar-footer');
    const aria2 = document.getElementById('dot-aria2')?.closest('.conn-row');
    if (footer) footer.insertBefore(node, aria2 || footer.firstChild);
    return node;
  }

  function heading() {
    let node = document.querySelector('.sidebar-footer > .dp-provider-status-heading');
    if (node) return node;
    const footer = document.querySelector('.sidebar-footer');
    if (!footer) return null;
    node = document.createElement('div');
    node.className = 'dp-provider-status-heading';
    node.textContent = 'Provider Status';
    const premium = document.getElementById('premium-row');
    footer.insertBefore(node, premium || footer.firstChild);
    return node;
  }

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, char => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[char]);
  }

  function dotClass(state) {
    return ({
      healthy: 'ok',
      auth_required: 'error',
      unhealthy: 'error',
      unconfigured: 'warn',
      unknown: 'check',
      checking: 'check',
      mixed: 'warn',
      disabled: 'error',
    })[state] || 'check';
  }

  function entryHtml(entry) {
    const premium = entry.premium
      ? '<span class="dp-provider-premium" role="img" title="Premium provider" aria-label="Premium provider"></span>'
      : '';
    return `<div class="conn-row dp-provider-status-row" data-provider-id="${esc(entry.id)}" data-provider-state="${esc(entry.state)}"><div class="dot ${dotClass(entry.state)}"></div><span class="dp-provider-status-name">${esc(entry.name)}${premium}</span></div>`;
  }

  function aggregateState(entries) {
    const enabled = entries.filter(entry => entry.enabled);
    if (!enabled.length) return 'disabled';
    if (enabled.length !== entries.length) return 'mixed';
    if (enabled.some(entry => ['unhealthy', 'auth_required'].includes(entry.state))) return 'unhealthy';
    if (enabled.some(entry => entry.state === 'unconfigured')) return 'unconfigured';
    if (enabled.some(entry => ['unknown', 'checking'].includes(entry.state))) return 'unknown';
    if (enabled.every(entry => entry.state === 'healthy')) return 'healthy';
    return 'unknown';
  }

  function aggregateHtml(id, label, entries) {
    const state = aggregateState(entries);
    const children = entries
      .filter(entry => entry.enabled && entry.state !== 'disabled')
      .map(entryHtml)
      .join('');
    return `<div class="dp-provider-status-group" data-provider-group="${esc(id)}"><div class="conn-row dp-provider-status-group-row" data-provider-state="${esc(state)}"><div class="dot ${dotClass(state)}"></div><span>${esc(label)}</span></div>${children ? `<div class="dp-provider-status-group-items">${children}</div>` : ''}</div>`;
  }

  function render(entries, mode = 'ready') {
    heading();
    const node = host();
    if (!node) return;
    if (mode === 'loading') {
      node.innerHTML = '<div class="conn-row dp-provider-status-row" data-provider-state="checking"><div class="dot check"></div><span>Checking providers…</span></div>';
      return;
    }
    if (mode === 'unknown') {
      node.innerHTML = '<div class="conn-row dp-provider-status-row" data-provider-state="unknown"><div class="dot check"></div><span>Provider status unavailable</span></div>';
      return;
    }

    const output = [];
    const groups = new Map();
    entries.forEach(entry => {
      if (entry.groupId && entry.groupLabel) {
        let group = groups.get(entry.groupId);
        if (!group) {
          group = {id: entry.groupId, label: entry.groupLabel, entries: []};
          groups.set(entry.groupId, group);
          output.push(group);
        }
        group.entries.push(entry);
      } else if (entry.enabled && entry.state !== 'disabled') {
        output.push({entry});
      }
    });

    node.innerHTML = output.length
      ? output.map(item => item.entry ? entryHtml(item.entry) : aggregateHtml(item.id, item.label, item.entries)).join('')
      : '<div class="conn-row dp-provider-status-row" data-provider-state="inactive"><div class="dot warn"></div><span>No download providers enabled</span></div>';
  }

  async function observe(candidate) {
    if (!candidate.enabled) return {...candidate, state: 'disabled'};
    if (candidate.staticStatus) return {...candidate, state: candidate.staticStatus};
    if (!candidate.endpoint) return {...candidate, state: 'unknown'};
    try {
      const status = await api('GET', candidate.endpoint);
      return {...candidate, state: String(status?.state || 'unknown'), status};
    } catch (_) {
      return {...candidate, state: 'unknown'};
    }
  }

  async function refresh() {
    const owned = invalidate();
    let settings;
    try { settings = settingsData; } catch (_) { settings = null; }
    const providers = candidates(settings);
    if (providers === null) {
      render([], 'unknown');
      return null;
    }
    const observations = await Promise.all(providers.map(observe));
    if (owned !== generation) return null;
    render(observations);
    document.dispatchEvent(new CustomEvent('debridpulse:provider-status', {
      detail: {entries: observations, generation: owned},
    }));
    return observations;
  }

  function loadCapacityCorrection() {
    if (document.getElementById('dp-ui-correction-batch1-capacity-script')) return;
    const capacity = document.createElement('script');
    capacity.id = 'dp-ui-correction-batch1-capacity-script';
    capacity.src = '/ui-correction-batch1-capacity.js?v=1';
    capacity.defer = true;
    document.head.appendChild(capacity);
  }

  function loadBatch1() {
    if (document.getElementById('dp-ui-correction-batch1-script') || window.DPUICorrectionBatch1) {
      loadCapacityCorrection();
      return;
    }
    const script = document.createElement('script');
    script.id = 'dp-ui-correction-batch1-script';
    script.src = '/ui-correction-batch1.js?v=6';
    script.defer = true;
    script.addEventListener('load', loadCapacityCorrection, {once: true});
    document.head.appendChild(script);
  }

  window.DPProviderStatus = Object.freeze({refresh, invalidate, candidates, aggregateState});
  heading();
  render([], 'loading');
  loadBatch1();
  document.addEventListener('DOMContentLoaded', () => refresh().catch(() => render([], 'unknown')), {once: true});
})();
