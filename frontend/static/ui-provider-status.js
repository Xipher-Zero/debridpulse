/* Neutral provider/direct-source status presentation owner.
 *
 * DP 1.0.12 canonical flattening: this module previously also bootstrapped
 * every other bounded presentation owner, dynamically injecting their
 * <script> tags on DOMContentLoaded and tolerating a load failure silently.
 * Those modules are now direct, explicit <script defer> dependencies in
 * index.html, loaded by the browser like any other required script -- a
 * failure to load one is a visible page error, not a silently-continuing
 * "ready" application. This module owns provider-status candidate
 * discovery, observation, invalidation and rendering only.
 */
(function () {
  'use strict';

  let generation = 0;
  function invalidate() { generation += 1; return generation; }

  function candidates(settings) {
    const integrations = settings?.integrations;
    if (!integrations || typeof integrations !== 'object') return null;
    // Aggregate participation gates, keyed by the same status_group identity
    // the entries below already carry. Absent means open, so a panel served by
    // an older backend behaves exactly as it did.
    const gates = settings?.integration_groups || {};
    return Object.entries(integrations)
      // A paired provider+executor integration (one canonical enable state)
      // still presents as one provider family in this panel.
      .filter(([, integration]) => (integration.kind === 'provider' || integration.kind === 'provider_executor')
        && String(integration.presentation?.status_name || '').trim())
      .map(([id, integration]) => {
        const presentation = integration.presentation;
        return {
          id,
          name: String(presentation.status_name),
          enabled: integration.enabled !== false,
          configured: Boolean(integration.configured),
          // Canonical, backend-derived verification truth. `verifiable` says
          // whether the question applies at all, so a source that has nothing
          // to prove is never reported as unproven.
          verified: Boolean(integration.verified),
          verifiable: Boolean(integration.verification_applicable),
          premium: Boolean(presentation.premium),
          endpoint: String(presentation.status_endpoint || '').trim(),
          staticStatus: String(presentation.static_status || '').trim(),
          order: Number.isFinite(Number(presentation.display_order)) ? Number(presentation.display_order) : 100,
          groupId: String(presentation.status_group || '').trim(),
          groupLabel: String(presentation.status_group_label || '').trim(),
          groupEnabled: gates[String(presentation.status_group || '').trim()]?.enabled !== false,
          tierId: String(presentation.status_tier || '').trim(),
          tierLabel: String(presentation.status_tier_label || '').trim(),
        };
      })
      .sort((a, b) => a.order - b.order || a.name.localeCompare(b.name) || a.id.localeCompare(b.id));
  }

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
  }

  // The heading and the list are static shell markup (index.html); this owner
  // only fills the list.
  const statusHost = () => document.getElementById('provider-status-list');

  function dotClass(state) {
    return ({healthy:'ok', auth_required:'error', unhealthy:'error', unconfigured:'warn', unverified:'warn', unknown:'check', checking:'check', mixed:'warn', unavailable:'error', disabled:'error'})[state] || 'check';
  }

  /* Runtime health and verification are two different questions, asked in that
   * order.
   *
   * A real runtime/service failure outranks everything: it is what is wrong
   * right now, and verification cannot make it green. Only once the runtime is
   * reporting healthy does the second question apply -- and only to a provider
   * that can actually be asked it. A configured, verification-capable provider
   * whose current saved configuration carries no successful proof is healthy
   * but unproven, which is a warning, not readiness. A source with no
   * verification subjects has nothing to prove and stays exactly as healthy as
   * its runtime says it is.
   *
   * This is derived PRESENTATION, from facts the backend already publishes.
   * Nothing here names an integration and nothing here decides, records or
   * second-guesses what is verified. */
  function verificationAdjusted(entry) {
    if (entry.state !== 'healthy') return entry;
    if (!entry.verifiable || !entry.configured || entry.verified) return entry;
    return {...entry, state: 'unverified'};
  }

  /* The group's one reported state.
   *
   * ``gateEnabled`` is the family's aggregate participation gate. While the
   * gate is OPEN the existing health model runs unchanged, so a member with a
   * real health state still decides the colour.
   *
   * `disabled` belongs to the GATE, not to the members, and `unavailable` is
   * what an open gate with NO participating member reports. The distinction
   * matters in both directions: calling the empty open gate `disabled` would
   * make it indistinguishable from a closed one, which is the whole thing the
   * master exists to express -- but calling it `mixed` claimed that some
   * members still work, and none do, so the family cannot acquire anything.
   * Both are red; neither impersonates the other. A group with no members at
   * all likewise has nothing that could participate. */
  function aggregateState(entries, gateEnabled = true) {
    if (gateEnabled === false) return 'disabled';
    if (!entries.length) return 'unavailable';
    const enabled = entries.filter(entry => entry.enabled);
    if (!enabled.length) return 'unavailable';
    if (enabled.length !== entries.length) return 'mixed';
    if (enabled.some(entry => ['unhealthy', 'auth_required'].includes(entry.state))) return 'unhealthy';
    if (enabled.some(entry => entry.state === 'unconfigured')) return 'unconfigured';
    if (enabled.some(entry => entry.state === 'unverified')) return 'unverified';
    if (enabled.some(entry => ['unknown', 'checking'].includes(entry.state))) return 'unknown';
    if (enabled.every(entry => entry.state === 'healthy')) return 'healthy';
    return 'unknown';
  }

  function render(entries, mode = 'ready') {
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
    // Two levels, both driven purely by integration-owned presentation
    // metadata: a TIER (which acquisition family this belongs to, rendered as a
    // heading) and, inside it, the existing aggregate GROUP. Nothing here names
    // an integration, and no tier order is declared: entries arrive already
    // sorted by display_order, so a tier takes the position of its first entry.
    // An integration that declares no tier is never dropped -- it renders
    // ungrouped after the tiers, exactly as before this hierarchy existed.
    const tiers = new Map();
    const untiered = [];
    const rowFor = entry =>
      `<div class="conn-row dp-provider-status-row" data-provider-id="${esc(entry.id)}" data-provider-state="${esc(entry.state)}"><div class="dot ${dotClass(entry.state)}"></div><span class="dp-provider-status-name">${esc(entry.name)}</span></div>`;

    for (const entry of entries) {
      const bucket = entry.tierId && entry.tierLabel
        ? (tiers.get(entry.tierId)
            || (tiers.set(entry.tierId, {id:entry.tierId, label:entry.tierLabel, rows:[], groups:new Map()}),
                tiers.get(entry.tierId)))
        : null;
      const rows = bucket ? bucket.rows : untiered;
      const groups = bucket ? bucket.groups : null;
      if (entry.groupId && entry.groupLabel) {
        let group = groups ? groups.get(entry.groupId) : null;
        if (!group) {
          group = {id:entry.groupId, label:entry.groupLabel, gate:entry.groupEnabled, entries:[]};
          if (groups) groups.set(entry.groupId, group);
          rows.push(group);
        }
        group.entries.push(entry);
      } else if (entry.enabled && entry.state !== 'disabled') {
        rows.push({entry});
      }
    }

    const markup = item => {
      if (item.entry) return rowFor(item.entry);
      const state = aggregateState(item.entries, item.gate);
      return `<div class="dp-provider-status-group" data-provider-group="${esc(item.id)}"><div class="conn-row dp-provider-status-group-row" data-provider-state="${esc(state)}"><div class="dot ${dotClass(state)}"></div><span>${esc(item.label)}</span></div></div>`;
    };

    // A tier with nothing to show renders nothing: no bare heading is left behind.
    const rendered = [...tiers.values()].filter(tier => tier.rows.length).map(tier =>
      `<div class="dp-provider-status-tier" data-provider-tier="${esc(tier.id)}"><div class="dp-provider-status-tier-label">${esc(tier.label)}</div>${tier.rows.map(markup).join('')}</div>`
    ).concat(untiered.map(markup));

    host.innerHTML = rendered.length ? rendered.join('')
      : '<div class="conn-row dp-provider-status-row" data-provider-state="inactive"><div class="dot warn"></div><span>No download providers enabled</span></div>';
  }

  async function observe(candidate) {
    if (!candidate.enabled) return {...candidate, state:'disabled'};
    if (candidate.staticStatus) return verificationAdjusted({...candidate, state:candidate.staticStatus});
    if (!candidate.endpoint) return {...candidate, state:'unknown'};
    try {
      const status = await api('GET', candidate.endpoint);
      return verificationAdjusted({...candidate, state:String(status?.state || 'unknown'), status});
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

  window.DPProviderStatus = Object.freeze({refresh, invalidate, candidates, aggregateState});
  render([], 'loading');
  document.addEventListener('DOMContentLoaded', () => refresh().catch(() => render([], 'unknown')), {once:true});
})();