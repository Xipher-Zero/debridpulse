const { test, expect } = require('@playwright/test');

/* DP 1.0.13 -- Provider Status renders two metadata-driven tiers:
 * Premium Services -> Standard Services. Usenet is a PREMIUM service and is
 * the reserved LAST row of that tier, so a future debrid entry that declares
 * no order at all still lands before it; Standard Services is the one
 * aggregate Network Sources row.
 *
 * The renderer is the unit under test, so its input is supplied as canonical
 * presentation metadata rather than by mutating the shared backend: that keeps
 * every case deterministic and leaves no state behind for another spec. The
 * metadata each shipped integration actually declares is asserted separately,
 * in backend/tests/test_v113_provider_status_hierarchy.py. */

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

const INTEGRATIONS = {
  usenet: {
    enabled: true, priority: 0, name: 'Usenet', kind: 'provider_executor', configured: true,
    presentation: {status_name: 'Usenet', premium: true, status_endpoint: null,
      static_status: 'healthy', display_order: 900, status_group: null, status_group_label: null,
      status_tier: 'premium_service', status_tier_label: 'Premium Services'},
    options: {servers: [{id: 'a', host: 'news-one.example.com'}, {id: 'b', host: 'news-two.example.com'}]},
  },
  alldebrid: {
    enabled: true, priority: 0, name: 'AllDebrid', kind: 'provider', configured: true,
    presentation: {status_name: 'AllDebrid', premium: true, status_endpoint: null,
      static_status: 'healthy', display_order: 10, status_group: null, status_group_label: null,
      status_tier: 'premium_service', status_tier_label: 'Premium Services'},
    options: {},
  },
  general_http: {
    enabled: true, priority: 0, name: 'HTTP(S)', kind: 'provider', configured: true,
    presentation: {status_name: 'HTTP(S)', premium: false, status_endpoint: null,
      static_status: 'healthy', display_order: 910, status_group: 'direct_sources',
      status_group_label: 'Network Sources', status_tier: 'general_family',
      status_tier_label: 'Standard Services'},
    options: {},
  },
  general_ftp: {
    enabled: true, priority: 0, name: '(S)FTP', kind: 'provider', configured: true,
    presentation: {status_name: '(S)FTP', premium: false, status_endpoint: null,
      static_status: 'healthy', display_order: 911, status_group: 'direct_sources',
      status_group_label: 'Network Sources', status_tier: 'general_family',
      status_tier_label: 'Standard Services'},
    options: {},
  },
};

const clone = value => JSON.parse(JSON.stringify(value));

/** Render the status panel from one explicit set of canonical integrations. */
async function renderWith(page, mutate = entries => entries) {
  const integrations = mutate(clone(INTEGRATIONS)) || {};
  await page.evaluate(next => {
    settingsData = {...(settingsData || {}), integrations: next};
  }, integrations);
  await page.evaluate(() => window.DPProviderStatus.refresh());
}

const tiers = page => page.evaluate(() =>
  Array.from(document.querySelectorAll('#provider-status-list .dp-provider-status-tier')).map(tier => ({
    id: tier.dataset.providerTier,
    label: tier.querySelector('.dp-provider-status-tier-label').textContent.trim(),
    rows: Array.from(tier.querySelectorAll('.conn-row')).map(row => row.textContent.trim()),
  })));

test.beforeEach(async ({page}) => {
  await isolateExternalFonts(page);
  await page.goto('/');
  await expect(page.locator('#provider-status-list')).toBeVisible();
});

test('the hierarchy is Premium Services, then Standard Services', async ({page}) => {
  await renderWith(page);
  const rendered = await tiers(page);
  expect(rendered.map(tier => tier.label)).toEqual(['Premium Services', 'Standard Services']);
  expect(rendered[0].rows.join(' ')).toContain('AllDebrid');
  expect(rendered[0].rows.join(' ')).toContain('Usenet');
  expect(rendered[1].rows.join(' ')).toContain('Network Sources');
});

test('no standalone Premium tier is rendered', async ({page}) => {
  await renderWith(page);
  const rendered = await tiers(page);
  expect(rendered.map(tier => tier.id)).not.toContain('premium_family');
  expect(rendered.map(tier => tier.label)).not.toContain('Premium');
  expect(rendered.map(tier => tier.label)).not.toContain('General');
});

test('PREMIUM SERVICES is the debrid providers, then Usenet last', async ({page}) => {
  await renderWith(page);
  const premium = (await tiers(page)).find(tier => tier.id === 'premium_service');
  expect(premium.label).toBe('Premium Services');
  expect(premium.rows.map(row => row.replace(/\s+/g, ' ').trim()))
    .toEqual(['AllDebrid', 'Usenet']);
});

test('STANDARD SERVICES is the one aggregate Network Sources row', async ({page}) => {
  await renderWith(page);
  const standard = (await tiers(page)).find(tier => tier.id === 'general_family');
  expect(standard.label).toBe('Standard Services');
  expect(standard.rows.map(row => row.replace(/\s+/g, ' ').trim()))
    .toEqual(['Network Sources']);
});

test('a future default-order debrid entry lands before Usenet', async ({page}) => {
  // The reserved band, proved through the renderer: an integration that
  // declares no explicit order takes the presentation model's default and
  // still sorts ahead of the reserved Usenet tail -- and the renderer names
  // neither of them.
  await renderWith(page, entries => {
    entries.future_debrid = {
      enabled: true, priority: 0, name: 'Future Debrid', kind: 'provider', configured: true,
      presentation: {status_name: 'Future Debrid', premium: true, status_endpoint: null,
        static_status: 'healthy', display_order: 100, status_group: null, status_group_label: null,
        status_tier: 'premium_service', status_tier_label: 'Premium Services'},
      options: {},
    };
    return entries;
  });
  const premium = (await tiers(page)).find(tier => tier.id === 'premium_service');
  expect(premium.rows.map(row => row.replace(/\s+/g, ' ').trim()))
    .toEqual(['AllDebrid', 'Future Debrid', 'Usenet']);
});

test('Premium stays before Standard when Usenet is the only premium row', async ({page}) => {
  await renderWith(page, entries => {
    delete entries.alldebrid;
    return entries;
  });
  const rendered = await tiers(page);
  expect(rendered.map(tier => tier.id)).toEqual(['premium_service', 'general_family']);
  expect(rendered[0].rows.map(row => row.replace(/\s+/g, ' ').trim())).toEqual(['Usenet']);
});

test('tier order follows the ordering metadata, not the declaration order', async ({page}) => {
  // The panel receives the Standard members first and the premium ones last;
  // the rendered order still comes from display_order, so no renderer knows
  // any tier's name.
  await renderWith(page, entries => ({
    general_http: entries.general_http, general_ftp: entries.general_ftp,
    usenet: entries.usenet, alldebrid: entries.alldebrid,
  }));
  expect((await tiers(page)).map(tier => tier.id))
    .toEqual(['premium_service', 'general_family']);
});

test('Usenet is one aggregate row and never lists a news server', async ({page}) => {
  await renderWith(page);
  const rendered = await tiers(page);
  const premium = rendered.find(tier => tier.id === 'premium_service');
  expect(premium.rows.filter(row => row.includes('Usenet')).length).toBe(1);
  const panel = await page.locator('#provider-status-list').textContent();
  expect(panel).not.toContain('news-one.example.com');
  expect(panel).not.toContain('news-two.example.com');
});

test('Network Sources stays one aggregate row, never separate HTTP and FTP rows',
  async ({page}) => {
    await renderWith(page);
    const standard = (await tiers(page)).find(tier => tier.id === 'general_family');
    expect(standard.rows.filter(row => row.includes('Network Sources')).length).toBe(1);
    const panel = await page.locator('#provider-status-list').textContent();
    expect(panel).not.toContain('HTTP(S)');
    expect(panel).not.toContain('(S)FTP');
  });

test('a tier whose only entry is disabled leaves no bare heading behind', async ({page}) => {
  await renderWith(page, entries => {
    delete entries.alldebrid;
    entries.usenet.enabled = false;
    return entries;
  });
  const rendered = await tiers(page);
  expect(rendered.some(tier => tier.id === 'premium_service')).toBe(false);
  expect(rendered.map(tier => tier.label)).toEqual(['Standard Services']);
});

test('an integration that declares no tier is still rendered', async ({page}) => {
  await renderWith(page, entries => {
    entries.future = {
      enabled: true, priority: 0, name: 'Future Source', kind: 'provider', configured: true,
      presentation: {status_name: 'Future Source', premium: false, status_endpoint: null,
        static_status: 'healthy', display_order: 500, status_group: null, status_group_label: null,
        status_tier: null, status_tier_label: null},
      options: {},
    };
    return entries;
  });
  const panel = await page.locator('#provider-status-list').textContent();
  expect(panel).toContain('Future Source');
});

test('health dots and the AllDebrid subscription row survive the hierarchy', async ({page}) => {
  await renderWith(page, entries => {
    entries.alldebrid.presentation.static_status = 'auth_required';
    entries.general_http.presentation.static_status = 'unconfigured';
    return entries;
  });
  const states = await page.evaluate(() => Array.from(
    document.querySelectorAll('#provider-status-list [data-provider-state]'))
    .map(el => ({state: el.dataset.providerState, dot: el.querySelector('.dot')?.className})));
  expect(states.length).toBeGreaterThan(0);
  for (const entry of states) {
    expect(['healthy', 'unhealthy', 'unconfigured', 'unknown', 'checking', 'mixed', 'disabled', 'auth_required'])
      .toContain(entry.state);
    expect(entry.dot).toBeTruthy();
  }
  expect(states.find(entry => entry.state === 'auth_required').dot).toContain('error');
  // The premium/subscription row is sibling shell markup and is untouched.
  await expect(page.locator('#premium-row')).toHaveCount(1);
});

/* DP 1.0.13 Item 2 -- tier labels are centred; the rows beneath are not. */

test('each tier label is horizontally centred within the status region', async ({page}) => {
  await renderWith(page);
  // Headings show beneath a shown premium account; publish one and measure in
  // the same task.
  const geometry = await page.evaluate(until => {
    document.dispatchEvent(new CustomEvent('debridpulse:provider-status', {detail: {entries: [
      {id: 'alldebrid', name: 'AllDebrid', state: 'healthy', status: {account: {entitlement: 'ready',
        service_class: 'premium', functional: 'usable', plan: 'Premium', expires_at: until}}}]}}));
    return Array.from(document.querySelectorAll('#provider-status-list .dp-provider-status-tier')).map(tier => {
      const label = tier.querySelector('.dp-provider-status-tier-label');
      // A block label's own box spans the tier, so the RENDERED text is measured.
      const range = document.createRange();
      range.selectNodeContents(label);
      const text = range.getBoundingClientRect();
      const box = tier.getBoundingClientRect();
      const row = tier.querySelector('.conn-row');
      return {
        label: label.textContent.trim(),
        offset: ((text.left + text.right) / 2) - ((box.left + box.right) / 2),
        rowLeft: row ? row.getBoundingClientRect().left - box.left : null,
      };
    });
  }, Math.floor(Date.now() / 1000) + 40 * 86400);
  expect(geometry.length).toBeGreaterThanOrEqual(2);
  for (const tier of geometry) {
    expect(Math.abs(tier.offset), `${tier.label} is not centred`).toBeLessThanOrEqual(1);
  }
});

test('centring the tier label does not centre the provider rows beneath it', async ({page}) => {
  await renderWith(page);
  const rows = await page.evaluate(() =>
    Array.from(document.querySelectorAll('#provider-status-list .conn-row')).map(row => {
      const tier = row.closest('.dp-provider-status-tier') || row.parentElement;
      return row.getBoundingClientRect().left - tier.getBoundingClientRect().left;
    }));
  expect(rows.length).toBeGreaterThan(0);
  const spread = Math.max(...rows) - Math.min(...rows);
  expect(spread).toBeLessThanOrEqual(1);
  expect(Math.max(...rows)).toBeLessThanOrEqual(2);
});

/* DP 1.0.13 -- Provider Status is contextual. The premium-account owner
 * composes every displayable premium account under ONE crown (one account: the
 * full two-line block; several: one compact line each, no date) and publishes
 * how many it shows; the tier headings beneath follow from that and from the
 * neutral premium-tier fact. Each case dispatches the neutral publication and
 * measures in the same task, so no background refresh can interleave. */
const LATER = () => Math.floor(Date.now() / 1000) + 40 * 86400;
const EXPIRY = () => Math.floor(Date.now() / 1000) + 90 * 86400;
// The neutral account truth every account-backed status surface publishes.
const account = (serviceClass, expiresAt, plan = 'Premium', functional = 'usable') => ({entitlement: 'ready',
  service_class: serviceClass, functional, plan, expires_at: expiresAt});
const adAccount = () => ({id: 'alldebrid', name: 'AllDebrid', state: 'healthy',
  status: {account: account('premium', LATER())}});
const rdAccount = () => ({id: 'realdebrid', name: 'Real-Debrid', state: 'healthy',
  status: {account: account('premium', EXPIRY())}});

const compose = (page, entries) => page.evaluate(next => {
  document.dispatchEvent(new CustomEvent('debridpulse:provider-status', {detail: {entries: next}}));
  const footer = document.querySelector('#sidebar .sidebar-footer');
  const row = document.getElementById('premium-row');
  const label = document.getElementById('lbl-premium');
  const shown = node => !!node && node.getClientRects().length > 0;
  const blocks = [...label.querySelectorAll('.dp-provider-premium-account')];
  const rowBox = row.getBoundingClientRect();
  const labelBox = label.getBoundingClientRect();
  const padBottom = parseFloat(getComputedStyle(row).paddingBottom) || 0;
  // Everything a reader sees, top to bottom: headings, the account row, rows.
  const flow = [...footer.querySelectorAll(
    '.dp-provider-status-heading, #premium-row, .dp-provider-status-tier-label, '
    + '.dp-provider-status-row, .dp-provider-status-group-row')]
    .filter(shown)
    .map(node => node.id === 'premium-row' ? 'PREMIUM-ACCOUNTS'
      : node.classList.contains('dp-provider-status-tier-label') ? `# ${node.textContent.trim()}`
        : node.textContent.trim());
  return {
    visible: shown(row),
    separator: shown(row) && getComputedStyle(row).borderBottomStyle !== 'none',
    blocks: blocks.map(block => ({compact: block.classList.contains('dp-provider-premium-account--compact'),
                                  lines: [...block.children].map(child => child.textContent),
                                  text: block.textContent})),
    crowns: [row, label, ...blocks].filter(node => !['none', 'normal']
      .includes(getComputedStyle(node, '::before').content)).map(node => node.id || node.className),
    align: getComputedStyle(row).alignItems,
    offset: Math.abs((labelBox.top + labelBox.bottom) / 2 - (rowBox.top + rowBox.bottom - padBottom) / 2),
    flow,
  };
}, entries);

test('one premium account: one crown beside the full two-line block, tiers kept', async ({page}) => {
  await renderWith(page);
  const one = await compose(page, [{...adAccount(), state: 'auth_required'}, rdAccount()]);
  expect(one.visible).toBe(true);
  expect(one.blocks).toHaveLength(1);
  expect(one.blocks[0].compact).toBe(false);
  expect(one.blocks[0].lines[0]).toMatch(/^Real-Debrid Premium until \d\d\.\d\d\.\d{4}$/);
  expect(one.blocks[0].lines[1]).toMatch(/^\(\d+ days remaining\)$/);
  expect(one.crowns).toEqual(['premium-row']);
  expect(one.align).toBe('center');
  expect(one.offset).toBeLessThanOrEqual(1);
  expect(one.flow.slice(0, 3)).toEqual(['Provider Status', 'PREMIUM-ACCOUNTS', '# Premium Services']);
  expect(one.flow).toContain('# Standard Services');
});

test('several premium accounts: one crown beside one compact line each, no date', async ({page}) => {
  await renderWith(page);
  const both = await compose(page, [adAccount(), rdAccount()]);
  expect(both.visible).toBe(true);
  expect(both.blocks.map(block => block.compact)).toEqual([true, true]);
  expect(both.blocks[0].text).toMatch(/^AllDebrid Premium \d+ days remaining$/);
  expect(both.blocks[1].text).toMatch(/^Real-Debrid Premium \d+ days remaining$/);
  for (const block of both.blocks) expect(block.text).not.toMatch(/\d\d\.\d\d\.\d{4}|until/);
  expect(both.crowns).toEqual(['premium-row']);
  expect(both.align).toBe('center');
  expect(both.offset).toBeLessThanOrEqual(1);
  expect(both.flow.slice(0, 3)).toEqual(['Provider Status', 'PREMIUM-ACCOUNTS', '# Premium Services']);
  expect(both.flow).toContain('# Standard Services');
});

test('no premium account beside Usenet: no row, crown, separator or Premium heading', async ({page}) => {
  await renderWith(page, entries => { delete entries.alldebrid; return entries; });
  const none = await compose(page, [{...rdAccount(), status: {account: account('standard', null, 'Free')}}]);
  expect(none.visible).toBe(false);
  expect(none.separator).toBe(false);
  expect(none.blocks).toHaveLength(0);
  expect(none.flow).toEqual(['Provider Status', 'Usenet', '# Standard Services', 'Network Sources']);
});

test('no premium account and no premium tier: Provider Status flows straight to Network Sources',
  async ({page}) => {
    await renderWith(page, entries => { delete entries.alldebrid; delete entries.usenet; return entries; });
    const bare = await compose(page, []);
    expect(bare.visible).toBe(false);
    expect(bare.separator).toBe(false);
    expect(bare.flow).toEqual(['Provider Status', 'Network Sources']);
  });

/* DP 1.0.13 -- an ACCOUNT-tiered provider's tier, crown and colour follow its
 * current account's neutral facts (status.account), observed live from its own
 * status endpoint. The renderer names no provider; the endpoints are stubbed so
 * every case is deterministic and leaves no backend state behind. */

const accountTiered = () => ({
  enabled: true, priority: 0, name: 'TorBox', kind: 'provider', configured: true,
  presentation: {status_name: 'TorBox', premium: true, status_endpoint: '/integration-status/torbox',
    static_status: null, display_order: 12, status_group: null, status_group_label: null,
    status_tier: 'premium_service', status_tier_label: 'Premium Services',
    standard_status_tier: 'general_family', standard_status_tier_label: 'Standard Services'},
  options: {},
});

async function liveStatus(page, status) {
  await page.unroute('**/api/integration-status/torbox').catch(() => {});
  await page.route('**/api/integration-status/torbox', route =>
    route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(status)}));
}

const panel = page => page.evaluate(() => {
  const footer = document.querySelector('#sidebar .sidebar-footer');
  const shown = node => !!node && node.getClientRects().length > 0;
  const rows = [...document.querySelectorAll('#provider-status-list [data-provider-id], #provider-status-list [data-provider-group] .conn-row')];
  return {
    flow: [...footer.querySelectorAll('#premium-row, .dp-provider-status-tier-label, .dp-provider-status-row, .dp-provider-status-group-row')]
      .filter(shown).map(node => node.id === 'premium-row' ? 'CROWN'
        : node.classList.contains('dp-provider-status-tier-label') ? `# ${node.textContent.trim()}` : node.textContent.trim()),
    tiers: [...document.querySelectorAll('#provider-status-list .dp-provider-status-tier')].map(tier => ({
      id: tier.dataset.providerTier, premium: tier.hasAttribute('data-provider-tier-premium'),
      rows: [...tier.querySelectorAll('.conn-row')].map(row => row.textContent.trim())})),
    torbox: (() => { const row = document.querySelector('[data-provider-id="torbox"]');
      return row ? {state: row.dataset.providerState, dot: row.querySelector('.dot').className} : null; })(),
    names: rows.map(row => row.textContent.trim()),
    crown: shown(document.getElementById('premium-row')) ? document.getElementById('lbl-premium').textContent : '',
  };
});

const withAccountTiered = (keepUsenet = false) => entries => {
  delete entries.alldebrid;
  if (!keepUsenet) delete entries.usenet;
  entries.torbox = accountTiered();
  return entries;
};
const days = n => Math.floor(Date.now() / 1000) + n * 86400;

test('a standard (free) account presents under Standard Services, green, with no crown', async ({page}) => {
  await liveStatus(page, {state: 'healthy', account: account('standard', null, 'Free')});
  await renderWith(page, withAccountTiered(true));
  const shown = await panel(page);
  expect(shown.tiers.find(tier => tier.id === 'general_family').rows).toEqual(['TorBox', 'Network Sources']);
  expect(shown.tiers.find(tier => tier.id === 'premium_service').rows).toEqual(['Usenet']);   // static tier kept
  expect(shown.torbox).toEqual({state: 'healthy', dot: 'dot ok'});
  expect(shown.crown).toBe('');
  expect(shown.flow).toEqual(['Usenet', '# Standard Services', 'TorBox', 'Network Sources']);
  expect(shown.names.filter(name => name === 'TorBox')).toHaveLength(1);
});

test('a real standard provider makes Standard Services visible even with no premium tier', async ({page}) => {
  await liveStatus(page, {state: 'healthy', account: account('standard', null, 'Free')});
  await renderWith(page, withAccountTiered());
  const shown = await panel(page);
  expect(shown.tiers.map(tier => tier.id)).toEqual(['general_family']);
  expect(shown.flow).toEqual(['# Standard Services', 'TorBox', 'Network Sources']);
});

test('the heading still folds away when Network Sources is the sole standard content', async ({page}) => {
  await renderWith(page, entries => { delete entries.alldebrid; delete entries.usenet; return entries; });
  expect((await panel(page)).flow).toEqual(['Network Sources']);
});

/* Only a functionally healthy premium account contributes to the crown. */
const crowned = (page, entries) => page.evaluate(next => {
  document.dispatchEvent(new CustomEvent('debridpulse:provider-status', {detail: {entries: next}}));
  const row = document.getElementById('premium-row');
  return {visible: !!row && row.getClientRects().length > 0,
          blocks: [...document.querySelectorAll('#lbl-premium .dp-provider-premium-account')].map(block => block.textContent)};
}, entries);
const premiumEntry = (id, name, state) => ({id, name, state, status: {account: account('premium', days(30))}});

test('a healthy premium account is crowned', async ({page}) => {
  await renderWith(page);
  const shown = await crowned(page, [premiumEntry('alldebrid', 'AllDebrid', 'healthy')]);
  expect(shown.visible).toBe(true);
  expect(shown.blocks).toHaveLength(1);
  expect(shown.blocks[0]).toMatch(/^AllDebrid Premium until/);
});

test('a degraded premium account is a yellow row and never crowned', async ({page}) => {
  await liveStatus(page, {state: 'healthy', account: account('premium', days(30), 'Pro', 'degraded')});
  await renderWith(page, withAccountTiered(true));
  const shown = await panel(page);
  expect(shown.torbox).toEqual({state: 'degraded', dot: 'dot warn'});
  expect(shown.tiers.find(tier => tier.id === 'premium_service').rows).toEqual(['TorBox', 'Usenet']);
  expect(shown.crown).toBe('');
});

test('mixed healthy and degraded premium accounts crown only the healthy one', async ({page}) => {
  await renderWith(page);
  const shown = await crowned(page, [premiumEntry('alldebrid', 'AllDebrid', 'degraded'),
    premiumEntry('realdebrid', 'Real-Debrid', 'healthy'), premiumEntry('torbox', 'TorBox', 'degraded')]);
  expect(shown.visible).toBe(true);
  expect(shown.blocks).toHaveLength(1);                     // one account: the full block layout
  expect(shown.blocks[0]).toMatch(/^Real-Debrid Premium until/);
  const two = await crowned(page, [premiumEntry('alldebrid', 'AllDebrid', 'healthy'),
    premiumEntry('realdebrid', 'Real-Debrid', 'degraded'), premiumEntry('torbox', 'TorBox', 'healthy')]);
  expect(two.blocks).toEqual([expect.stringMatching(/^AllDebrid Premium \d+ days remaining$/),
                              expect.stringMatching(/^TorBox Premium \d+ days remaining$/)]);
});

test('a connected but capability-degraded account is yellow, never red, in its current tier', async ({page}) => {
  await liveStatus(page, {state: 'healthy', account: account('standard', null, 'Free', 'degraded')});
  await renderWith(page, withAccountTiered(true));
  const shown = await panel(page);
  expect(shown.torbox).toEqual({state: 'degraded', dot: 'dot warn'});
  expect(shown.tiers.find(tier => tier.id === 'general_family').rows[0]).toBe('TorBox');
  expect(shown.crown).toBe('');
});

test('a connection failure stays red whatever account truth it last had', async ({page}) => {
  await liveStatus(page, {state: 'auth_required'});
  await renderWith(page, withAccountTiered(true));
  expect((await panel(page)).torbox).toEqual({state: 'auth_required', dot: 'dot error'});
});

test('premium lapsing and returning moves tier and crown with no provider-specific logic', async ({page}) => {
  await liveStatus(page, {state: 'healthy', account: account('premium', days(30), 'Pro')});
  await renderWith(page, withAccountTiered(true));
  let shown = await panel(page);
  expect(shown.tiers.find(tier => tier.id === 'premium_service').rows).toEqual(['TorBox', 'Usenet']);
  expect(shown.torbox.dot).toBe('dot ok');
  expect(shown.crown).toMatch(/^TorBox Pro until \d\d\.\d\d\.\d{4}\(\d+ days remaining\)$/);
  expect(shown.flow.slice(0, 2)).toEqual(['CROWN', '# Premium Services']);

  // Premium ends while DebridPulse runs: the backend announces it, the panel re-observes.
  await liveStatus(page, {state: 'healthy', account: account('standard', null, 'Free', 'degraded')});
  await page.evaluate(() => document.dispatchEvent(new CustomEvent('debridpulse:integration-status-changed')));
  await expect.poll(async () => (await panel(page)).torbox?.state).toBe('degraded');
  shown = await panel(page);
  expect(shown.tiers.find(tier => tier.id === 'premium_service').rows).toEqual(['Usenet']);
  expect(shown.tiers.find(tier => tier.id === 'general_family').rows).toEqual(['TorBox', 'Network Sources']);
  expect(shown.crown).toBe('');
  expect(shown.flow).not.toContain('# Premium Services');

  // Renewed: the same composition restores tier, crown and green.
  await liveStatus(page, {state: 'healthy', account: account('premium', days(60), 'Pro')});
  await page.evaluate(() => document.dispatchEvent(new CustomEvent('debridpulse:integration-status-changed')));
  await expect.poll(async () => (await panel(page)).torbox?.state).toBe('healthy');
  shown = await panel(page);
  expect(shown.tiers.find(tier => tier.id === 'premium_service').rows).toEqual(['TorBox', 'Usenet']);
  expect(shown.crown).toMatch(/^TorBox Pro until/);
});
