const { test, expect } = require('@playwright/test');

/* DP 1.0.13 -- Provider Status renders two metadata-driven tiers:
 * Premium Services -> General. The standalone Premium tier is retired; Usenet
 * belongs to the GENERAL tier, whose first two positions are permanently
 * reserved -- Usenet, then General Sources -- so any later GENERAL entry
 * naturally follows both.
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
      static_status: 'healthy', display_order: 20, status_group: null, status_group_label: null,
      status_tier: 'general_family', status_tier_label: 'General'},
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
    enabled: true, priority: 0, name: 'HTTP & HTTPS', kind: 'provider', configured: true,
    presentation: {status_name: 'HTTP & HTTPS', premium: false, status_endpoint: null,
      static_status: 'healthy', display_order: 30, status_group: 'direct_sources',
      status_group_label: 'General Sources', status_tier: 'general_family', status_tier_label: 'General'},
    options: {},
  },
  general_ftp: {
    enabled: true, priority: 0, name: 'FTP & SFTP', kind: 'provider', configured: true,
    presentation: {status_name: 'FTP & SFTP', premium: false, status_endpoint: null,
      static_status: 'healthy', display_order: 31, status_group: 'direct_sources',
      status_group_label: 'General Sources', status_tier: 'general_family', status_tier_label: 'General'},
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

test('the hierarchy is Premium Services, then General', async ({page}) => {
  await renderWith(page);
  const rendered = await tiers(page);
  expect(rendered.map(tier => tier.label)).toEqual(['Premium Services', 'General']);
  expect(rendered[0].rows.join(' ')).toContain('AllDebrid');
  expect(rendered[1].rows.join(' ')).toContain('Usenet');
  expect(rendered[1].rows.join(' ')).toContain('General Sources');
});

test('no standalone Premium tier is rendered', async ({page}) => {
  await renderWith(page);
  const rendered = await tiers(page);
  expect(rendered.map(tier => tier.id)).not.toContain('premium_family');
  expect(rendered.map(tier => tier.label)).not.toContain('Premium');
});

test('the GENERAL tier is Usenet first, then General Sources', async ({page}) => {
  await renderWith(page);
  const general = (await tiers(page)).find(tier => tier.id === 'general_family');
  expect(general.rows.length).toBe(2);
  expect(general.rows[0]).toContain('Usenet');
  expect(general.rows[1]).toContain('General Sources');
});

test('a later GENERAL entry sorts after both reserved positions', async ({page}) => {
  // A future integration that declares no explicit order takes the
  // presentation model's default, and lands after Usenet and General Sources
  // without the renderer knowing any of their names.
  await renderWith(page, entries => {
    entries.future_general = {
      enabled: true, priority: 0, name: 'Future General', kind: 'provider', configured: true,
      presentation: {status_name: 'Future General', premium: false, status_endpoint: null,
        static_status: 'healthy', display_order: 100, status_group: null, status_group_label: null,
        status_tier: 'general_family', status_tier_label: 'General'},
      options: {},
    };
    return entries;
  });
  const general = (await tiers(page)).find(tier => tier.id === 'general_family');
  expect(general.rows.map(row => row.replace(/\s+/g, ' ').trim()))
    .toEqual(['Usenet', 'General Sources', 'Future General']);
});

test('tier order follows the ordering metadata, not the declaration order', async ({page}) => {
  // The panel receives General first and Premium Services last; the rendered
  // order still comes from display_order, so no renderer knows any tier's name.
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
  const general = rendered.find(tier => tier.id === 'general_family');
  expect(general.rows.filter(row => row.includes('Usenet')).length).toBe(1);
  const panel = await page.locator('#provider-status-list').textContent();
  expect(panel).not.toContain('news-one.example.com');
  expect(panel).not.toContain('news-two.example.com');
});

test('General Sources stays one aggregate row, never duplicate HTTP and FTP rows', async ({page}) => {
  await renderWith(page);
  const general = (await tiers(page)).find(tier => tier.id === 'general_family');
  expect(general.rows.filter(row => row.includes('General Sources')).length).toBe(1);
  const panel = await page.locator('#provider-status-list').textContent();
  expect(panel).not.toContain('HTTP & HTTPS');
  expect(panel).not.toContain('FTP & SFTP');
});

test('PREMIUM SERVICES and AllDebrid are unchanged', async ({page}) => {
  await renderWith(page);
  const premium = (await tiers(page)).find(tier => tier.id === 'premium_service');
  expect(premium.label).toBe('Premium Services');
  expect(premium.rows.length).toBe(1);
  expect(premium.rows[0]).toContain('AllDebrid');
});

test('a tier whose only entry is disabled leaves no bare heading behind', async ({page}) => {
  await renderWith(page, entries => {
    entries.alldebrid.enabled = false;
    return entries;
  });
  const rendered = await tiers(page);
  expect(rendered.some(tier => tier.id === 'premium_service')).toBe(false);
  expect(rendered.map(tier => tier.label)).toEqual(['General']);
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
  const geometry = await page.evaluate(() =>
    Array.from(document.querySelectorAll('#provider-status-list .dp-provider-status-tier')).map(tier => {
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
    }));
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
