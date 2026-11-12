const { test, expect } = require('@playwright/test');

/* DP 1.0.14 setup wizard, WS3a and WS3b: Premium Services.
 *
 * The page lists the six services in their fixed order, each with what the
 * server says it is (configured, verified, disabled: three facts). Choosing an
 * API-key provider (AllDebrid, Debrid-Link, Premiumize) shows that provider's
 * own Services card, built by its Settings owner, in the SAME dialog, always
 * as Easy shows it -- even for an Advanced reader, whose Settings stays
 * Advanced. Its fields wait for Save, which commits through the provider's
 * canonical scope (PATCH /api/integrations/<id>/configuration); Cancel
 * discards them; Enable acts at once and Cancel does not undo it; Test is
 * offered only while nothing is unsaved. Real open runtime; every change a
 * case makes is put back. Synthetic values only. */

const SERVICES = ['usenet', 'alldebrid', 'debridlink', 'premiumize', 'realdebrid', 'torbox'];
const KEY = 'synthetic-wizard-key-0001';

async function load(page, {width = 1280, height = 900} = {}) {
  await page.setViewportSize({width, height});
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.goto('/');
  await expect(page.locator('#sidebar .nav-item[data-view="dashboard"]')).toBeAttached();
  await page.evaluate(() => window.DPI18n.ready);
}

const wizard = page => page.locator('.dp-modal-dialog.dp-setup-dialog');
const call = (page, method, path, body) => page.evaluate(([method, path, body]) => window.debridPulseAuth.fetch(path, {
  method, headers: {'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : undefined,
}).then(async response => ({status: response.status, body: await response.json().catch(() => null)})), [method, path, body]);
const integration = async (page, id) => (await call(page, 'GET', '/api/settings')).body.integrations[id] || {};
const t = (page, key) => page.evaluate(key => window.DPI18n.t(key), key);

/* An Admin's rerun, at Premium Services. */
async function premium(page) {
  await call(page, 'DELETE', '/api/setup/rerun');
  if ((page.viewportSize()?.width || 1440) < 780) await page.locator('#topbar-account-button').click();
  else await page.locator('#sidebar-auth-row .dp-sidebar-profile').click();
  await page.locator('.dp-account-dialog [data-account-setup-rerun]').click();
  await expect(wizard(page)).toBeVisible();
  const deployment = await page.evaluate(() => window.debridPulseAuth.authority.deployment_type);
  for (let i = 0; i < (deployment === 'standalone' ? 2 : 3); i += 1) await wizard(page).locator('[data-modal-accept]').click();
  await expect(wizard(page).locator('.dp-setup-premium')).toBeVisible();
}

async function restore(page, id, before) {
  if (!before.configured) await call(page, 'PATCH', `/api/integrations/${id}/configuration`, {options: {}, clear_secrets: ['api_key']});
  await call(page, 'PATCH', `/api/integrations/${id}/configuration`, {enabled: before.enabled !== false});
  await call(page, 'DELETE', '/api/setup/rerun');
}

test('P01: six services in the fixed order, each with the server\'s three facts; Next advances with none configured', async ({page}) => {
  await load(page);
  await premium(page);
  const shown = await wizard(page).locator('[data-service]').evaluateAll(nodes => nodes.map(node => node.dataset.service));
  expect(shown).toEqual(SERVICES);
  await expect(wizard(page).locator('[data-service="alldebrid"] [data-fact]').first()).toBeAttached();
  const settings = (await call(page, 'GET', '/api/settings')).body.integrations;
  for (const id of SERVICES) {
    const entry = settings[id];
    const facts = await wizard(page).locator(`[data-service="${id}"] [data-fact]`).evaluateAll(nodes => nodes.map(node => node.dataset.fact));
    const expected = [entry?.configured ? 'configured' : 'unconfigured'];
    if (entry?.configured) expected.push(entry.verified ? 'verified' : 'unverified');
    if (!entry || entry.enabled === false) expected.push('disabled');
    expect(facts, id).toEqual(expected);
  }
  // Every one of the six opens its configuration here.
  for (const id of SERVICES) await expect(wizard(page).locator(`[data-service="${id}"]`)).toBeEnabled();
  await wizard(page).locator('[data-modal-accept]').click();
  await expect(wizard(page).locator('.dp-setup-getting-started')).toBeVisible();
  await call(page, 'DELETE', '/api/setup/rerun');
});

test('P02: a provider opens in the same dialog, as Easy shows it, for an Advanced reader whose Settings stays Advanced', async ({page}) => {
  await load(page);
  const presentation = (await call(page, 'GET', '/api/account/presentation')).body;
  await call(page, 'PUT', '/api/account/presentation', {presentation_mode: 'advanced'});
  await page.reload();
  await expect(page.locator('#sidebar .nav-item[data-view="dashboard"]')).toBeAttached();
  try {
    await premium(page);
    const overlays = await page.locator('.dp-modal-overlay').count();
    await wizard(page).locator('[data-service="alldebrid"]').click();
    const card = wizard(page).locator('.dp-setup-provider-surface .dp-settings-provider-card--alldebrid');
    await expect(card).toBeVisible();
    await expect(page.locator('.dp-modal-overlay')).toHaveCount(overlays);          // no second dialog
    await expect(wizard(page).locator('.dp-modal-title')).toHaveText('AllDebrid');
    await expect(card.locator('[data-setting="alldebrid_api_key"]')).toBeVisible();
    await expect(card.locator('.dp-settings-additional')).toHaveCount(0);          // Easy: no Additional Settings
    await expect(card.locator('[data-commit="changed-blur"]')).toHaveCount(0);     // its fields wait for Save
    expect((await call(page, 'GET', '/api/account/presentation')).body.presentation_mode).toBe('advanced');
    await wizard(page).locator('[data-modal-cancel]').click();
    await expect(wizard(page).locator('.dp-setup-premium')).toBeVisible();
    // Settings keeps the reader's Advanced presentation.
    await wizard(page).locator('[data-modal-cancel]').click();
    await page.locator('.dp-account-dialog [data-modal-cancel]').click();
    await page.evaluate(() => window.nav(document.querySelector('#sidebar .nav-item[data-view="settings"]')));
    await expect(page.locator('#view-settings .dp-settings-provider-card--alldebrid .dp-settings-additional')).toHaveCount(1);
    expect((await call(page, 'GET', '/api/account/presentation')).body.presentation_mode).toBe('advanced');
  } finally {
    await call(page, 'PUT', '/api/account/presentation', {presentation_mode: presentation.presentation_mode});
    await call(page, 'DELETE', '/api/setup/rerun');
  }
});

test('P03: Cancel discards a typed key; Test waits for Save', async ({page}) => {
  await load(page);
  const before = await integration(page, 'alldebrid');
  expect(before.configured, 'the open runtime holds no AllDebrid key').toBe(false);
  const writes = [];
  page.on('request', request => { if (request.url().includes('/api/integrations/')) writes.push(`${request.method()} ${new URL(request.url()).pathname}`); });
  await premium(page);
  await wizard(page).locator('[data-service="alldebrid"]').click();
  const field = wizard(page).locator('[data-setting="alldebrid_api_key"]');
  await field.fill(KEY);
  await expect(wizard(page).locator('.dp-settings-provider-test')).toBeDisabled();
  await expect(wizard(page).locator('[data-setup-test-note]')).toBeVisible();
  await field.blur();                                                              // no blur commit here
  await wizard(page).locator('[data-modal-cancel]').click();
  await expect(wizard(page).locator('.dp-setup-premium')).toBeVisible();
  expect(writes).toEqual([]);
  expect((await integration(page, 'alldebrid')).configured).toBe(false);
  await call(page, 'DELETE', '/api/setup/rerun');
});

test('P04: Save commits the key through the provider\'s canonical scope and the list shows it configured, not verified', async ({page}) => {
  await load(page);
  const before = await integration(page, 'alldebrid');
  expect(before.configured, 'the open runtime holds no AllDebrid key').toBe(false);
  try {
    await premium(page);
    await wizard(page).locator('[data-service="alldebrid"]').click();
    await wizard(page).locator('[data-setting="alldebrid_api_key"]').fill(KEY);
    const patch = page.waitForRequest(request => request.method() === 'PATCH'
      && new URL(request.url()).pathname === '/api/integrations/alldebrid/configuration');
    await wizard(page).locator('[data-modal-accept]').click();
    const sent = (await patch).postDataJSON();
    expect(sent.options).toEqual({api_key: KEY});
    await expect(wizard(page).locator('.dp-setup-premium')).toBeVisible();
    await expect(wizard(page).locator('[data-service="alldebrid"] [data-fact="configured"]')).toBeVisible();
    await expect(wizard(page).locator('[data-service="alldebrid"] [data-fact="unverified"]')).toBeVisible();
    const after = await integration(page, 'alldebrid');
    expect(after.configured).toBe(true);
    expect(after.verified).toBe(false);
    expect(JSON.stringify(after)).not.toContain(KEY);
  } finally {
    await restore(page, 'alldebrid', before);
  }
});

test('P05: a refused Save stays in the provider with a truthful error', async ({page}) => {
  await load(page);
  const before = await integration(page, 'debridlink');
  await page.route(url => url.pathname === '/api/integrations/debridlink/configuration', route => (route.request().method() === 'PATCH'
    ? route.fulfill({status: 409, contentType: 'application/json', body: JSON.stringify({detail: 'synthetic refusal', code: 'unclassified', params: {}})})
    : route.continue()));
  await premium(page);
  await wizard(page).locator('[data-service="debridlink"]').click();
  await wizard(page).locator('[data-setting="debridlink_api_key"]').fill(KEY);
  await wizard(page).locator('[data-modal-accept]').click();
  await expect(wizard(page).locator('.dp-setup-error')).toBeVisible();
  await expect(wizard(page).locator('.dp-setup-provider-surface .dp-settings-provider-card--debridlink')).toBeVisible();
  expect((await integration(page, 'debridlink')).configured).toBe(before.configured === true);
  await call(page, 'DELETE', '/api/setup/rerun');
});

test('P06: Enable acts at once and Cancel does not undo it', async ({page}) => {
  await load(page);
  const before = await integration(page, 'premiumize');
  try {
    await premium(page);
    await wizard(page).locator('[data-service="premiumize"]').click();
    const toggle = wizard(page).locator('[data-integration-enabled="premiumize"]');
    const was = await toggle.isChecked();
    await wizard(page).locator('.dp-setup-provider-surface .dp-settings-provider-card--premiumize label.toggle-row').click();
    await expect.poll(async () => (await integration(page, 'premiumize')).enabled !== false).toBe(!was);
    await wizard(page).locator('[data-modal-cancel]').click();
    await expect(wizard(page).locator('.dp-setup-premium')).toBeVisible();
    expect((await integration(page, 'premiumize')).enabled !== false).toBe(!was);
    await expect.poll(async () => wizard(page).locator('[data-service="premiumize"] [data-fact="disabled"]').count()).toBe(was ? 1 : 0);
  } finally {
    await restore(page, 'premiumize', before);
  }
});

test('P07: Test proves the saved configuration through the provider\'s own Test', async ({page}) => {
  await load(page);
  let tested = null;
  await page.route(url => url.pathname === '/api/settings/validate-premiumize', route => {
    tested = route.request().postDataJSON();
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true})});
  });
  await premium(page);
  await wizard(page).locator('[data-service="premiumize"]').click();
  const testButton = wizard(page).locator('.dp-settings-provider-test');
  await expect(testButton).toBeEnabled();
  await testButton.click();
  await expect.poll(() => tested).not.toBeNull();
  expect(tested).toEqual({api_key: ''});                                          // the saved key, no draft
  await wizard(page).locator('[data-modal-cancel]').click();
  await call(page, 'DELETE', '/api/setup/rerun');
});

test('P08: at 320px the provider fits the dialog', async ({page}) => {
  await load(page, {width: 320, height: 640});
  await premium(page);
  await wizard(page).locator('[data-service="debridlink"]').click();
  await expect(wizard(page).locator('.dp-setup-provider-surface .dp-settings-provider-card--debridlink')).toBeVisible();
  const fit = await wizard(page).evaluate(node => {
    const dialog = node.getBoundingClientRect();
    const out = [...node.querySelectorAll('.dp-setup-provider-surface *')]
      .filter(child => child.getClientRects().length && child.getBoundingClientRect().right > dialog.right + 1).length;
    return {left: dialog.left, right: dialog.right, out, page: document.documentElement.scrollWidth};
  });
  expect(fit.left).toBeGreaterThanOrEqual(0);
  expect(fit.right).toBeLessThanOrEqual(320);
  expect(fit.out).toBe(0);
  expect(fit.page).toBeLessThanOrEqual(320);
  await wizard(page).locator('[data-modal-cancel]').click();
  await call(page, 'DELETE', '/api/setup/rerun');
});

/* Services drawn with AllDebrid's card open, then this Admin's rerun at
 * Premium Services over it. */
async function servicesThenPremium(page) {
  await page.evaluate(() => window.nav(document.querySelector('#sidebar .nav-item[data-view="settings"]')));
  const card = page.locator('#view-settings .dp-settings-provider-card--alldebrid');
  await expect(card).toBeVisible();
  await card.locator('.dp-settings-disclosure').click();
  await expect(card.locator('[data-setting="alldebrid_api_key"]')).toBeVisible();
  return card;
}

test('P09: a Settings write of the same key still on its way decides the wizard\'s Save; refused, Save then saves', async ({page}) => {
  await load(page);
  const before = await integration(page, 'alldebrid');
  expect(before.configured, 'the open runtime holds no AllDebrid key').toBe(false);
  let release;
  const held = new Promise(resolve => { release = resolve; });
  let patches = 0;
  await page.route(url => url.pathname === '/api/integrations/alldebrid/configuration', async route => {
    if (route.request().method() !== 'PATCH') return route.continue();
    patches += 1;
    if (patches === 1) {
      await held;
      return route.fulfill({status: 409, contentType: 'application/json',
        body: JSON.stringify({detail: 'synthetic refusal', code: 'unclassified', params: {}})});
    }
    return route.continue();
  });
  try {
    const settingsCard = await servicesThenPremium(page);
    await settingsCard.locator('[data-setting="alldebrid_api_key"]').fill(KEY);
    await settingsCard.locator('[data-setting="alldebrid_api_key"]').press('Tab');       // Services' blur commit, held
    await expect.poll(() => patches).toBe(1);
    await premium(page);
    await wizard(page).locator('[data-service="alldebrid"]').click();
    await wizard(page).locator('[data-setting="alldebrid_api_key"]').fill(KEY);
    await wizard(page).locator('[data-modal-accept]').click();
    // The same draft is already on its way: no second write, and no answer yet.
    await page.waitForTimeout(800);
    expect(patches).toBe(1);
    await expect(wizard(page).locator('.dp-setup-provider-surface')).toBeVisible();
    await expect(wizard(page).locator('.dp-setup-error')).toBeHidden();
    // Refused: the wizard says so and stays, its own draft kept.
    release();
    await expect(wizard(page).locator('.dp-setup-error')).toBeVisible();
    await expect(wizard(page).locator('.dp-setup-provider-surface')).toBeVisible();
    await expect(wizard(page).locator('[data-setting="alldebrid_api_key"]')).toHaveValue(KEY);
    expect((await integration(page, 'alldebrid')).configured).toBe(false);
    // Save again: its own write, accepted.
    await wizard(page).locator('[data-modal-accept]').click();
    await expect(wizard(page).locator('.dp-setup-premium')).toBeVisible();
    expect(patches).toBe(2);
    expect((await integration(page, 'alldebrid')).configured).toBe(true);
  } finally {
    release();
    await restore(page, 'alldebrid', before);
  }
});

test('P10: the wizard\'s Test sends its own (saved) state, never an unsaved draft in Services\' card', async ({page}) => {
  await load(page);
  let tested = null;
  await page.route(url => url.pathname === '/api/settings/validate-alldebrid', route => {
    tested = route.request().postDataJSON();
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true})});
  });
  const settingsCard = await servicesThenPremium(page);
  // An unsaved draft in Services' field: present, never committed (no blur).
  await settingsCard.locator('[data-setting="alldebrid_api_key"]').evaluate(field => { field.value = 'synthetic-services-draft'; });
  await premium(page);
  await wizard(page).locator('[data-service="alldebrid"]').click();
  await wizard(page).locator('.dp-settings-provider-test').click();
  await expect.poll(() => tested).not.toBeNull();
  expect(tested).toEqual({api_key: ''});
  await wizard(page).locator('[data-modal-cancel]').click();
  await settingsCard.locator('[data-setting="alldebrid_api_key"]').evaluate(field => { field.value = ''; });
  await call(page, 'DELETE', '/api/setup/rerun');
});

/* WS3b: Real-Debrid, TorBox and Usenet, through the same owners.
 *
 * The account services (Real-Debrid, TorBox) connect on the provider's own
 * site: their Easy configuration is the Account Connection alone, and every
 * control on it acts at once. The provider's authorization endpoints are
 * emulated here (labelled mocks: no real provider is contacted); what is
 * proven is that the wizard drives Services' own connection owner. Usenet's
 * servers are the real local records; its fields wait for Save, which writes
 * an existing server field by field and creates a new one once, and a
 * server's own Test checks what it shows, saving nothing. */

/* Labelled mock of one account service's device authorization. */
async function emulateAuthorization(page, id, {connect = false} = {}) {
  const seen = [];
  const json = body => ({status: 200, contentType: 'application/json', body: JSON.stringify(body)});
  await page.route(url => url.pathname.startsWith(`/api/integrations/${id}/`), async route => {
    const path = new URL(route.request().url()).pathname.replace(`/api/integrations/${id}`, '');
    const method = route.request().method();
    if (path === '/authorization' && method === 'POST') {
      seen.push('authorize');
      return route.fulfill(json({user_code: 'SYNTH-CODE', verification_url: 'https://synthetic.invalid/device', interval: 5, expires_in: 600}));
    }
    if (path === '/authorization/poll') {
      seen.push('poll');
      if (!connect) return route.fulfill(json({state: 'pending', user_code: 'SYNTH-CODE', verification_url: 'https://synthetic.invalid/device', interval: 5}));
      const settings = await page.evaluate(() => window.debridPulseAuth.fetch('/api/settings').then(response => response.json()));
      const integration = {...settings.integrations[id], configured: true, verified: true};
      integration.options = {...integration.options, api_token_configured: true, client_id_configured: true};
      return route.fulfill(json({state: 'connected', integration_id: id, integration, email: 'synthetic@example.invalid', username: 'synthetic'}));
    }
    if (path === '/authorization' && method === 'DELETE') { seen.push('cancel'); return route.fulfill(json({ok: true})); }
    if (path === '/disconnect') {
      seen.push('disconnect');
      const settings = await page.evaluate(() => window.debridPulseAuth.fetch('/api/settings').then(response => response.json()));
      return route.fulfill(json({ok: true, integration_id: id, integration: settings.integrations[id]}));
    }
    return route.continue();
  });
  return seen;
}

test('P11: Real-Debrid and TorBox open in the same dialog as Easy shows them -- the Account Connection only -- for an Advanced reader', async ({page}) => {
  await load(page);
  const presentation = (await call(page, 'GET', '/api/account/presentation')).body;
  await call(page, 'PUT', '/api/account/presentation', {presentation_mode: 'advanced'});
  await page.reload();
  await expect(page.locator('#sidebar .nav-item[data-view="dashboard"]')).toBeAttached();
  const writes = [];
  page.on('request', request => { if (request.method() !== 'GET') writes.push(`${request.method()} ${new URL(request.url()).pathname}`); });
  try {
    await premium(page);
    const overlays = await page.locator('.dp-modal-overlay').count();
    for (const [id, name] of [['realdebrid', 'Real-Debrid'], ['torbox', 'TorBox']]) {
      await wizard(page).locator(`[data-service="${id}"]`).click();
      const card = wizard(page).locator(`.dp-setup-provider-surface .dp-settings-provider-card--${id}`);
      await expect(card).toBeVisible();
      await expect(page.locator('.dp-modal-overlay')).toHaveCount(overlays);
      await expect(wizard(page).locator('.dp-modal-title')).toHaveText(name);
      await expect(card.locator(`[data-${id}-connection] [data-action="connect-${id}"]`)).toBeVisible();
      await expect(card.locator('.dp-settings-additional')).toHaveCount(0);
      await expect(card.locator('[data-commit-key]:not([data-integration-enabled])')).toHaveCount(0);   // nothing to type
      await expect(wizard(page).locator('[data-i18n="setup.premium.immediate_account"]')).toBeVisible();
      // Nothing waits for Save: Save is the ordinary return to the list.
      await wizard(page).locator('[data-modal-accept]').click();
      await expect(wizard(page).locator('.dp-setup-premium')).toBeVisible();
    }
    expect(writes.filter(write => !write.endsWith('/api/setup/rerun'))).toEqual([]);
    expect((await call(page, 'GET', '/api/account/presentation')).body.presentation_mode).toBe('advanced');
    await wizard(page).locator('[data-modal-cancel]').click();
    await page.locator('.dp-account-dialog [data-modal-cancel]').click();
    await page.evaluate(() => window.nav(document.querySelector('#sidebar .nav-item[data-view="settings"]')));
    await expect(page.locator('#view-settings .dp-settings-provider-card--realdebrid .dp-settings-additional')).toHaveCount(1);
  } finally {
    await call(page, 'PUT', '/api/account/presentation', {presentation_mode: presentation.presentation_mode});
    await call(page, 'DELETE', '/api/setup/rerun');
  }
});

test('P12: Connect, the code, its Copy and Cancel, then a connection and Disconnect act at once through Services\' own owner (emulated provider)', async ({page}) => {
  await load(page);
  const seen = await emulateAuthorization(page, 'torbox', {connect: true});
  await page.evaluate(() => { window.__copied = []; navigator.clipboard.writeText = text => { window.__copied.push(text); return Promise.resolve(); }; });
  await premium(page);
  await wizard(page).locator('[data-service="torbox"]').click();
  const card = wizard(page).locator('.dp-setup-provider-surface .dp-settings-provider-card--torbox');
  await card.locator('[data-action="connect-torbox"]').click();
  await expect(card.locator('[data-torbox-code]')).toHaveValue('SYNTH-CODE');
  await card.locator('[data-action="copy-torbox-code"]').click();
  await expect.poll(() => page.evaluate(() => window.__copied)).toEqual(['SYNTH-CODE']);
  // Cancel the authorization (the island's own Cancel): immediate, the view stays.
  await card.locator('[data-action="cancel-torbox"]').click();
  await expect(card.locator('[data-action="connect-torbox"]')).toBeVisible();
  expect(seen).toEqual(['authorize', 'cancel']);
  // Connect again; the emulated provider authorizes at its first poll.
  await card.locator('[data-action="connect-torbox"]').click();
  await expect(card.locator('[data-action="disconnect-torbox"]')).toBeVisible({timeout: 15000});
  // Disconnect: Services' confirmation, then at once.
  await card.locator('[data-action="disconnect-torbox"]').click();
  await page.locator('.dp-modal-dialog[role="alertdialog"] [data-modal-accept]').click();
  await expect.poll(() => seen.includes('disconnect')).toBe(true);
  await expect(card.locator('[data-action="connect-torbox"]')).toBeVisible();
  await wizard(page).locator('[data-modal-cancel]').click();
  await expect(wizard(page).locator('.dp-setup-premium')).toBeVisible();
  await call(page, 'DELETE', '/api/setup/rerun');
});

/* One synthetic Usenet server, created through the real local API. */
async function usenetServer(page) {
  const created = await call(page, 'POST', '/api/usenet/servers', {host: 'news.synthetic.invalid', port: 563, ssl: true,
    username: 'synthetic', connections: 8, priority: 0, articles_per_request: 2, timeout_seconds: 60, enabled: true, display_name: ''});
  expect(created.status).toBe(200);
  return created.body.server_id;
}
const usenetServers = async page => ((await integration(page, 'usenet')).options?.servers) || [];
async function removeUsenetServers(page) {
  for (const server of await usenetServers(page)) await call(page, 'DELETE', `/api/usenet/servers/${server.id}`);
}

test('P13: a Usenet server\'s fields wait for Save: blur and Cancel write nothing, Save writes the changed field; a server action settles no draft', async ({page}) => {
  await load(page);
  expect(await usenetServers(page), 'the open runtime holds no Usenet server').toEqual([]);
  const id = await usenetServer(page);
  const writes = [];
  page.on('request', request => { if (/\/api\/usenet\//.test(request.url()) && request.method() !== 'GET') writes.push({method: request.method(), path: new URL(request.url()).pathname, body: request.postDataJSON()}); });
  try {
    await premium(page);
    await wizard(page).locator('[data-service="usenet"]').click();
    const server = wizard(page).locator(`.dp-setup-provider-surface [data-usenet-server-id="${id}"]`);
    await expect(server).toBeVisible();
    await expect(wizard(page).locator('[data-i18n="setup.premium.immediate_usenet"]')).toBeVisible();
    const username = server.locator('[data-usenet-field="username"]');
    await expect(username).toHaveAttribute('data-commit', 'explicit');
    await username.fill('synthetic-edited');
    await username.blur();
    // The provider's own Test (saved servers) waits; the server's Test does not.
    await expect(wizard(page).locator('[data-action="test-usenet"]')).toBeDisabled();
    await expect(server.locator('[data-usenet-action="test"]')).toBeEnabled();
    // An immediate act (SSL) carries only itself, never the username draft.
    await server.locator('.dp-usenet-ssl').click();
    await expect.poll(() => writes.length).toBe(1);
    expect(writes[0].method).toBe('PUT');
    expect(writes[0].body).toHaveProperty('ssl', false);
    expect(writes[0].body).not.toHaveProperty('username');
    await wizard(page).locator('[data-modal-cancel]').click();
    await expect(wizard(page).locator('.dp-setup-premium')).toBeVisible();
    expect(writes).toHaveLength(1);
    expect((await usenetServers(page))[0].username).toBe('synthetic');
    // Again, and Save: exactly that field, through the server's own record.
    await wizard(page).locator('[data-service="usenet"]').click();
    await server.locator('[data-usenet-field="username"]').fill('synthetic-saved');
    await wizard(page).locator('[data-modal-accept]').click();
    await expect(wizard(page).locator('.dp-setup-premium')).toBeVisible();
    expect(writes.slice(1)).toEqual([{method: 'PUT', path: `/api/usenet/servers/${id}`, body: {username: 'synthetic-saved'}}]);
    expect((await usenetServers(page))[0].username).toBe('synthetic-saved');
  } finally {
    await removeUsenetServers(page);
    await call(page, 'DELETE', '/api/setup/rerun');
  }
});

test('P14: a new Usenet server is created once by Save, with its credential sent once; without a Host, Save stays and says so', async ({page}) => {
  await load(page);
  expect(await usenetServers(page), 'the open runtime holds no Usenet server').toEqual([]);
  const writes = [];
  page.on('request', request => { if (/\/api\/usenet\//.test(request.url()) && request.method() !== 'GET') writes.push({method: request.method(), path: new URL(request.url()).pathname, body: request.postDataJSON()}); });
  try {
    await premium(page);
    await wizard(page).locator('[data-service="usenet"]').click();
    await wizard(page).locator('.dp-setup-provider-surface [data-usenet-action="add"]').click();
    const card = wizard(page).locator('.dp-setup-provider-surface [data-usenet-server-id=""]');
    await expect(card).toHaveCount(1);
    await expect(card.locator('[data-usenet-field="host"]')).toHaveAttribute('data-commit', 'explicit');
    await card.locator('[data-usenet-field="password"]').fill('synthetic-news-pass');
    await card.locator('[data-usenet-field="password"]').blur();
    expect(writes).toEqual([]);
    // No Host: nothing is sent, and the wizard stays and says so.
    await wizard(page).locator('[data-modal-accept]').click();
    await expect(wizard(page).locator('.dp-setup-error')).toBeVisible();
    await expect(card.locator('[data-usenet-validation]')).toBeVisible();
    expect(writes).toEqual([]);
    await card.locator('[data-usenet-field="host"]').fill('news2.synthetic.invalid');
    await card.locator('[data-usenet-field="username"]').fill('synthetic-new');
    await wizard(page).locator('[data-modal-accept]').click();
    await expect(wizard(page).locator('.dp-setup-premium')).toBeVisible();
    await expect(wizard(page).locator('[data-service="usenet"] [data-fact="configured"]')).toBeVisible();
    expect(writes).toHaveLength(1);
    expect(writes[0].method).toBe('POST');
    expect(writes[0].path).toBe('/api/usenet/servers');
    expect(writes[0].body).toMatchObject({host: 'news2.synthetic.invalid', username: 'synthetic-new', password: 'synthetic-news-pass'});
    const servers = await usenetServers(page);
    expect(servers.map(server => [server.host, server.password_configured])).toEqual([['news2.synthetic.invalid', true]]);
  } finally {
    await removeUsenetServers(page);
    await call(page, 'DELETE', '/api/setup/rerun');
  }
});

test('P15: a refused new server stays in the view with its draft; a server\'s Test checks the draft and saves nothing (emulated news server)', async ({page}) => {
  await load(page);
  expect(await usenetServers(page), 'the open runtime holds no Usenet server').toEqual([]);
  let refuse = true;
  let tested = null;
  await page.route(url => url.pathname === '/api/usenet/servers' || url.pathname === '/api/usenet/servers/test', route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/test')) {
      tested = route.request().postDataJSON();
      return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true, message: 'synthetic ok'})});
    }
    if (route.request().method() === 'POST' && refuse) {
      refuse = false;
      return route.fulfill({status: 409, contentType: 'application/json', body: JSON.stringify({detail: 'synthetic refusal', code: 'unclassified', params: {}})});
    }
    return route.continue();
  });
  try {
    await premium(page);
    await wizard(page).locator('[data-service="usenet"]').click();
    await wizard(page).locator('.dp-setup-provider-surface [data-usenet-action="add"]').click();
    const card = wizard(page).locator('.dp-setup-provider-surface .dp-usenet-server').last();
    await card.locator('[data-usenet-field="host"]').fill('news3.synthetic.invalid');
    await card.locator('[data-usenet-field="password"]').fill('synthetic-draft-pass');
    await card.locator('[data-usenet-action="test"]').click();
    await expect.poll(() => tested).not.toBeNull();
    expect(tested).toMatchObject({host: 'news3.synthetic.invalid', password: 'synthetic-draft-pass', server_id: null});
    expect(await usenetServers(page)).toEqual([]);                               // Test saved nothing
    await wizard(page).locator('[data-modal-accept]').click();
    await expect(wizard(page).locator('.dp-setup-error')).toBeVisible();
    await expect(card.locator('[data-usenet-field="host"]')).toHaveValue('news3.synthetic.invalid');
    expect(await usenetServers(page)).toEqual([]);
    await wizard(page).locator('[data-modal-accept]').click();
    await expect(wizard(page).locator('.dp-setup-premium')).toBeVisible();
    expect((await usenetServers(page)).map(server => server.host)).toEqual(['news3.synthetic.invalid']);
  } finally {
    await removeUsenetServers(page);
    await call(page, 'DELETE', '/api/setup/rerun');
  }
});
