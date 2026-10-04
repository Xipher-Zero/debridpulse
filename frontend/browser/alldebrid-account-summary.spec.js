const { test, expect } = require('@playwright/test');

/* DP 1.0.13 AllDebrid account-summary parity.
 *
 * AllDebrid's Settings card states its connected account the way every other
 * API-key account card does: one line above the key island, worded by the one
 * neutral premium-account owner (window.DPPremiumAccount) from the neutral
 * `status.account` truth Provider Status reads -- "Premium until DD.MM.YYYY
 * (N days remaining)", "Free account", or nothing while that truth cannot say.
 *
 * The settings document, the provider status and every credential write are
 * answered here, so this file writes no canonical state and owns no settings
 * key (spec files share one backend and run concurrently). */

const EXPIRES = Date.parse('2027-03-14T12:00:00Z') / 1000;
const PREMIUM = {entitlement: 'ready', service_class: 'premium', functional: 'usable', plan: 'Premium',
                 request_types: ['direct_link'], expires_at: EXPIRES};
const FREE = {entitlement: 'ready', service_class: 'standard', functional: 'usable', plan: 'Free',
              request_types: ['direct_link'], expires_at: null};
const UNRESOLVED = {entitlement: 'unresolved', service_class: null, functional: 'unresolved', plan: '',
                    request_types: [], expires_at: null};

const healthy = account => ({integration: 'alldebrid', state: 'healthy', checked: true, username: 'alice',
                             ...(account ? {account} : {})});

/* Serve one settings document in which AllDebrid (and Debrid-Link, the
 * control) is enabled and keyed, and answer both providers' status and
 * configuration writes. `status.alldebrid` is what the next AllDebrid status
 * read returns; `gate`, when set, holds that read until it resolves. */
async function prepare(page, {alldebrid, debridlink = null}) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  const keyed = {alldebrid: true, debridlink: debridlink !== null};
  const live = {document: null};
  const entry = id => {
    const current = live.document.integrations[id] || {};
    return {...current, enabled: true, configured: keyed[id], verified: keyed[id],
            options: {...(current.options || {}), api_key: '', api_key_configured: keyed[id]}};
  };
  await page.route('**/api/settings', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    live.document = await (await route.fetch()).json();
    live.document.integrations = {...live.document.integrations,
      alldebrid: entry('alldebrid'), debridlink: entry('debridlink')};
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(live.document)});
  });
  const status = {alldebrid, debridlink, gate: null};
  await page.route(url => /^\/api\/integration-status\/(alldebrid|debridlink)$/.test(url.pathname), async route => {
    const id = new URL(route.request().url()).pathname.split('/').pop();
    if (id === 'alldebrid' && status.gate) await status.gate;
    const body = status[id] ? {...status[id], integration: id} : {integration: id, state: 'unconfigured'};
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(body)});
  });
  const writes = [];
  await page.route(url => url.pathname === '/api/integrations/alldebrid/configuration', route => {
    const body = route.request().postDataJSON();
    writes.push(body);
    if ((body.clear_secrets || []).includes('api_key')) keyed.alldebrid = false;
    else if (body.options?.api_key) keyed.alldebrid = true;
    return route.fulfill({status: 200, contentType: 'application/json',
                          body: JSON.stringify({ok: true, ...entry('alldebrid')})});
  });
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await page.locator('#view-settings [data-tab="sources"]').click();
  const card = page.locator('.dp-settings-provider-card--alldebrid');
  const disclosure = card.locator('.dp-settings-disclosure');
  if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click();
  await expect(card.locator(':scope > .card-body')).toBeVisible();
  return {card, region: card.locator('[data-alldebrid-account]'), status, writes};
}

// The wording the ONE premium-account owner gives this account, computed in
// the page from the same neutral facts.
const premiumWording = page => page.evaluate(expires => {
  const text = window.DPPremiumAccount.describe(new Date(expires * 1000), 'Premium');
  return `${text.until} ${text.days}`;
}, EXPIRES);

test('a finite premium AllDebrid account states the shared premium line above its key island', async ({page}) => {
  const {card, region} = await prepare(page, {alldebrid: healthy(PREMIUM)});
  const expected = await premiumWording(page);
  expect(expected).toMatch(/^Premium until 14\.03\.2027 \(\d+ days remaining\)$/);
  await expect(region.locator('.dp-settings-provider-status-line .dp-settings-account-expiry')).toHaveText(expected);
  // Provider Status reads the same truth through the same owner.
  await expect(page.locator('#lbl-premium')).toContainText(`AllDebrid ${expected.split(' (')[0]}`);
  // The line sits directly above the key island.
  const order = await card.evaluate(node => {
    const line = node.querySelector('[data-alldebrid-account]').getBoundingClientRect();
    const key = node.querySelector('.dp-settings-alldebrid-key-row').getBoundingClientRect();
    return key.top - line.bottom;
  });
  expect(order).toBeGreaterThanOrEqual(0);
});

test('a standard AllDebrid account reads "Free account"', async ({page}) => {
  const {region} = await prepare(page, {alldebrid: healthy(FREE)});
  await expect(region.locator('.dp-settings-account-expiry')).toHaveText('Free account');
});

for (const [label, account] of [['unresolved account truth', UNRESOLVED], ['no account truth', null]]) {
  test(`${label} states nothing, and leaves no blank row`, async ({page}) => {
    const {region} = await prepare(page, {alldebrid: healthy(account)});
    // Provider Status has observed AllDebrid before the line is judged.
    await expect(page.locator('#provider-status-list [data-provider-id="alldebrid"]')).toHaveCount(1);
    await expect(region.locator('*')).toHaveCount(0);
  });
}

test('removing the key withdraws the account line with the accepted state', async ({page}) => {
  const {card, region, status, writes} = await prepare(page, {alldebrid: healthy(PREMIUM)});
  await expect(region.locator('.dp-settings-account-expiry')).toHaveText(await premiumWording(page));
  status.alldebrid = {integration: 'alldebrid', state: 'unconfigured'};
  await card.locator('[data-action="clear-alldebrid-key"]').click();
  const accepted = page.waitForResponse(response =>
    new URL(response.url()).pathname === '/api/integrations/alldebrid/configuration');
  await page.locator('.dp-modal-dialog [data-modal-accept]').click();
  await accepted;
  expect(writes).toEqual([{options: {}, clear_secrets: ['api_key']}]);
  await expect(region.locator('*')).toHaveCount(0);
  await expect(card.locator('[data-action="clear-alldebrid-key"]')).toBeDisabled();
  // The credential row converged exactly as before: no key present.
  await expect(card.locator('.dp-settings-alldebrid-key-row')).not.toHaveClass(/\bis-configured\b/);
});

test('a replaced key never keeps the previous account line', async ({page}) => {
  const {region, status, writes} = await prepare(page, {alldebrid: healthy(PREMIUM)});
  await expect(region.locator('.dp-settings-account-expiry')).toHaveText(await premiumWording(page));
  // The new key belongs to a free account; its status read is held so the
  // moment between the accepted write and the new truth is observable.
  let release;
  status.gate = new Promise(resolve => { release = resolve; });
  status.alldebrid = healthy(FREE);
  const field = page.locator('#dp-settings-field-alldebrid-api-key');
  await field.fill('replacement-key');
  await field.blur();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0].options.api_key).toBe('replacement-key');
  await expect(region.locator('*')).toHaveCount(0);
  status.gate = null;
  release();
  await expect(region.locator('.dp-settings-account-expiry')).toHaveText('Free account');
});

test('Debrid-Link keeps its own account line through the same owner', async ({page}) => {
  await prepare(page, {alldebrid: healthy(FREE),
                       debridlink: {state: 'healthy', checked: true, username: 'bob', account: PREMIUM}});
  const card = page.locator('.dp-settings-provider-card--debridlink');
  const disclosure = card.locator('.dp-settings-disclosure');
  if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click();
  await expect(card.locator('[data-debridlink-account] .dp-settings-account-expiry'))
    .toHaveText(await premiumWording(page));
  await expect(page.locator('[data-alldebrid-account] .dp-settings-account-expiry')).toHaveText('Free account');
});
