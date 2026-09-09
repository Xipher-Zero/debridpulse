/* Neutral provider/direct-source status presentation owner. */
(function () {
  'use strict';

  const PRESENTATION_OWNERS = Object.freeze([
    ['/ui-toast-contract.js?v=2', 'DPToastContract'],
    ['/ui-processing-presentation.js?v=1', 'DPProcessingPresentation'],
    ['/ui-dashboard-transfer-presentation.js?v=3', 'DPDashboardTransferPresentation'],
    ['/ui-downloads-presentation.js?v=2', 'DPDownloadsPresentation'],
    ['/ui-activity-log-runtime.js?v=1', 'DPActivityLog'],
    ['/ui-settings-archive-passwords.js?v=1', 'DPArchivePasswords'],
  ]);

  let generation = 0;
  function invalidate() { generation += 1; return generation; }

  function candidates(settings) {
    const integrations = settings?.integrations;
    if (!integrations || typeof integrations !== 'object') return null;
    return Object.entries(integrations)
      .filter(([, integration]) => integration.kind === 'provider' && String(integration.presentation?.status_name || '').trim())
      .map(([id, integration]) => {
        const presentation = integration.presentation;
        return {
          id,
          name: String(presentation.status_name),
          enabled: integration.enabled !== false,
          configured: Boolean(integration.configured),
          premium: Boolean(presentation.premium),
          endpoint: String(presentation.status_endpoint || '').trim(),
          staticStatus: String(presentation.static_status || '').trim(),
          order: Number.isFinite(Number(presentation.display_order)) ? Number(presentation.display_order) : 100,
          groupId: String(presentation.status_group || '').trim(),
          groupLabel: String(presentation.status_group_label || '').trim(),
        };
      })
      .sort((a, b) => a.order - b.order || a.name.localeCompare(b.name) || a.id.localeCompare(b.id));
  }

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
  }

  function statusHost() {
    let node = document.getElementById('provider-status-list');
    if (node) return node;
    const footer = document.querySelector('.sidebar-footer');
    if (!footer) return null;
    node = document.createElement('div');
    node.id = 'provider-status-list';
    node.className = 'dp-provider-status-list';
    node.setAttribute('aria-label', 'Provider Status');
    const aria2 = document.getElementById('dot-aria2')?.closest('.conn-row');
    footer.insertBefore(node, aria2 || footer.firstChild);
    return node;
  }

  function ensureHeading() {
    const footer = document.querySelector('.sidebar-footer');
    if (!footer) return null;
    let node = footer.querySelector(':scope > .dp-provider-status-heading');
    if (!node) {
      node = document.createElement('div');
      node.className = 'dp-provider-status-heading';
      node.textContent = 'Provider Status';
      footer.insertBefore(node, document.getElementById('premium-row') || footer.firstChild);
    }
    return node;
  }

  function dotClass(state) {
    return ({healthy:'ok', auth_required:'error', unhealthy:'error', unconfigured:'warn', unknown:'check', checking:'check', mixed:'warn', disabled:'error'})[state] || 'check';
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

  function render(entries, mode = 'ready') {
    ensureHeading();
    const host = statusHost();
    if (!host) return;
    if (mode === 'loading') {
      host.innerHTML = '<div class="conn-row dp-provider-status-row" data-provider-state="checking"><div class="dot check"></div><span>Checking providers…</span></div>';
      return;
    }
    if (mode === 'unknown') {
      host.innerHTML = '<div class="conn-row dp-provider-status-row" data-provider-state="unknown"><div class="dot check"></div><span>Provider status unavailable</span></div>';
      return;
    }
    const output = [];
    const groups = new Map();
    for (const entry of entries) {
      if (entry.groupId && entry.groupLabel) {
        let group = groups.get(entry.groupId);
        if (!group) {
          group = {id:entry.groupId, label:entry.groupLabel, entries:[]};
          groups.set(entry.groupId, group);
          output.push(group);
        }
        group.entries.push(entry);
      } else if (entry.enabled && entry.state !== 'disabled') {
        output.push({entry});
      }
    }
    host.innerHTML = output.length ? output.map(item => {
      if (item.entry) {
        const entry = item.entry;
        return `<div class="conn-row dp-provider-status-row" data-provider-id="${esc(entry.id)}" data-provider-state="${esc(entry.state)}"><div class="dot ${dotClass(entry.state)}"></div><span class="dp-provider-status-name">${esc(entry.name)}</span></div>`;
      }
      const state = aggregateState(item.entries);
      return `<div class="dp-provider-status-group" data-provider-group="${esc(item.id)}"><div class="conn-row dp-provider-status-group-row" data-provider-state="${esc(state)}"><div class="dot ${dotClass(state)}"></div><span>${esc(item.label)}</span></div></div>`;
    }).join('') : '<div class="conn-row dp-provider-status-row" data-provider-state="inactive"><div class="dot warn"></div><span>No download providers enabled</span></div>';
  }

  async function observe(candidate) {
    if (!candidate.enabled) return {...candidate, state:'disabled'};
    if (candidate.staticStatus) return {...candidate, state:candidate.staticStatus};
    if (!candidate.endpoint) return {...candidate, state:'unknown'};
    try {
      const status = await api('GET', candidate.endpoint);
      return {...candidate, state:String(status?.state || 'unknown'), status};
    } catch (_) {
      return {...candidate, state:'unknown'};
    }
  }

  async function refresh() {
    const owned = invalidate();
    let settings = null;
    try { settings = settingsData; } catch (_) {}
    const providers = candidates(settings);
    if (providers === null) { render([], 'unknown'); return null; }
    const observations = await Promise.all(providers.map(observe));
    if (owned !== generation) return null;
    render(observations);
    document.dispatchEvent(new CustomEvent('debridpulse:provider-status', {detail:{entries:observations, generation:owned}}));
    return observations;
  }

  function bootPresentationOwners() {
    let chain = Promise.resolve();
    for (const [src, marker] of PRESENTATION_OWNERS) {
      chain = chain.then(() => new Promise(resolve => {
        if (window[marker]) { resolve(); return; }
        const path = src.split('?')[0];
        const existing = Array.from(document.scripts).find(node => {
          try { return new URL(node.src, location.href).pathname === path; } catch (_) { return false; }
        });
        if (existing) {
          if (window[marker]) { resolve(); return; }
          existing.addEventListener('load', resolve, {once:true});
          existing.addEventListener('error', resolve, {once:true});
          return;
        }
        const script = document.createElement('script');
        script.src = src;
        script.async = false;
        script.dataset.dpPresentationOwner = marker;
        script.addEventListener('load', resolve, {once:true});
        script.addEventListener('error', () => {
          console.error('Unable to load bounded presentation owner:', src);
          resolve();
        }, {once:true});
        document.head.appendChild(script);
      }));
    }
    chain.finally(() => document.dispatchEvent(new CustomEvent('debridpulse:presentation-ready')));
    return chain;
  }

  window.DPProviderStatus = Object.freeze({refresh, invalidate, candidates, aggregateState});
  ensureHeading();
  render([], 'loading');
  bootPresentationOwners();
  document.addEventListener('DOMContentLoaded', () => refresh().catch(() => render([], 'unknown')), {once:true});
})();