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
      {id: 'alldebrid', name: 'AllDebrid', state: 'healthy', status: {isPremium: true, premiumUntil: until}}]}}));
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
const EXPIRY = () => new Date(Date.now() + 90 * 86400000).toISOString();
const adAccount = () => ({id: 'alldebrid', name: 'AllDebrid', state: 'healthy',
  status: {isPremium: true, premiumUntil: LATER()}});
const rdAccount = () => ({id: 'realdebrid', name: 'Real-Debrid', state: 'healthy',
  status: {premium: true, account_type: 'premium', expiration: EXPIRY()}});

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
  const none = await compose(page, [{...rdAccount(), status: {premium: false, account_type: 'free'}}]);
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
