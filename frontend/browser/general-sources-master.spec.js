const { test, expect } = require('@playwright/test');

/* DP 1.0.13 Items 5 and 10 -- the General Sources master gate, and every
 * Sources & Providers Enable toggle as an immediate canonical control.
 *
 * The master is an aggregate PARTICIPATION gate. It never edits a child's
 * stored preference, in either direction, and the group's Provider Status row
 * never disappears.
 */

const GROUP = 'direct_sources';
const CHILDREN = ['general_http', 'general_ftp'];

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

const canonical = page => page.request.get('/api/settings').then(r => r.json());

async function setChildren(page, http, ftp) {
  await page.request.patch('/api/integrations/general_http/configuration', {data: {enabled: http}});
  await page.request.patch('/api/integrations/general_ftp/configuration', {data: {enabled: ftp}});
}

async function setMaster(page, enabled) {
  const response = await page.request.patch(
    `/api/integration-groups/${GROUP}/configuration`, {data: {enabled}});
  expect(response.ok(), 'the generic group mutation route rejected the write').toBeTruthy();
  return response.json();
}

async function openSources(page) {
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await page.locator('#view-settings [data-tab="sources"]').click();
  await expect(page.locator('.dp-settings-panel[data-panel="sources"]')).toBeVisible();
}

const masterTrack = page => page.locator(
  `label[for="dp-settings-integration-group-${GROUP}-enabled"] .ttrack`);
const childTrack = (page, id) => page.locator(
  `label[for="dp-settings-integration-${id}-enabled"] .ttrack`);

const groupState = page => page.evaluate(group => {
  const node = document.querySelector(`#provider-status-list [data-provider-group="${group}"]`);
  if (!node) return null;
  return node.querySelector('.conn-row').dataset.providerState;
}, GROUP);

/* Render the status panel from one EXPLICIT canonical document.
 *
 * The aggregate colour is a property of the renderer, so it is driven with
 * metadata rather than by mutating the live installation: the Settings specs
 * run in parallel workers against one backend, and a spec that rewrites shared
 * integration state to prove a colour would be racing every other spec that
 * owns that same state. The durable semantics below still use the real API --
 * they are about durability and have to. */
async function renderStatus(page, {http, ftp, master}) {
  const live = await page.request.get('/api/settings').then(r => r.json());
  await page.goto('/');
  await page.evaluate(([entries, gates]) => {
    settingsData = {...(settingsData || {}), integrations: entries, integration_groups: gates};
  }, [
    {...live.integrations,
     general_http: {...live.integrations.general_http, enabled: http},
     general_ftp: {...live.integrations.general_ftp, enabled: ftp}},
    {[GROUP]: {enabled: master, label: 'General Sources', members: CHILDREN}},
  ]);
  await page.evaluate(() => window.DPProviderStatus.refresh());
}

/* This spec drives REAL canonical state, so it restores exactly what it found
 * rather than a state of its own choosing -- forcing "everything on" would be
 * a silent mutation that the next spec in the run inherits. */
let original = null;

test.beforeAll(async ({request}) => {
  const settings = await request.get('/api/settings').then(r => r.json());
  original = {
    http: settings.integrations.general_http.enabled,
    ftp: settings.integrations.general_ftp.enabled,
    master: settings.integration_groups?.[GROUP]?.enabled !== false,
  };
});

test.afterAll(async ({request}) => {
  if (!original) return;
  await request.patch('/api/integrations/general_http/configuration', {data: {enabled: original.http}});
  await request.patch('/api/integrations/general_ftp/configuration', {data: {enabled: original.ftp}});
  await request.patch(`/api/integration-groups/${GROUP}/configuration`, {data: {enabled: original.master}});
});

test.beforeEach(async ({page}) => {
  await isolateExternalFonts(page);
});

// --- state semantics ---------------------------------------------------------

test('an upgraded installation starts with the group master enabled', async ({page}) => {
  await setMaster(page, true);
  const settings = await canonical(page);
  expect(settings.integration_groups[GROUP].enabled).toBe(true);
  expect(settings.integration_groups[GROUP].label).toBe('General Sources');
});

test('master OFF does not rewrite either child preference', async ({page}) => {
  await setChildren(page, true, false);
  const before = await canonical(page);
  await setMaster(page, false);
  const after = await canonical(page);
  for (const child of CHILDREN) {
    expect(after.integrations[child].enabled,
      `${child} preference was rewritten by the master`).toBe(before.integrations[child].enabled);
  }
  expect(after.integrations.general_http.enabled).toBe(true);
  expect(after.integrations.general_ftp.enabled).toBe(false);
});

test('master ON restores participation from the surviving child preferences', async ({page}) => {
  await setChildren(page, true, false);
  await setMaster(page, false);
  await setMaster(page, true);
  const settings = await canonical(page);
  expect(settings.integrations.general_http.enabled).toBe(true);
  expect(settings.integrations.general_http.effective_enabled).toBe(true);
  expect(settings.integrations.general_ftp.enabled).toBe(false);
  expect(settings.integrations.general_ftp.effective_enabled).toBe(false);
});

test('a child toggled while the master is OFF changes only the child', async ({page}) => {
  await setChildren(page, true, false);
  await setMaster(page, false);
  await openSources(page);
  await childTrack(page, 'general_ftp').click();
  await expect.poll(async () => (await canonical(page)).integrations.general_ftp.enabled).toBe(true);
  const settings = await canonical(page);
  expect(settings.integration_groups[GROUP].enabled, 'the child toggle moved the master').toBe(false);
  expect(settings.integrations.general_ftp.effective_enabled).toBe(false);
});

// --- Provider Status aggregate ----------------------------------------------

test('green when the master is on and every child is enabled', async ({page}) => {
  await renderStatus(page, {http: true, ftp: true, master: true});
  await expect.poll(() => groupState(page)).toBe('healthy');
});

test('yellow when the master is on and one child is disabled', async ({page}) => {
  await renderStatus(page, {http: true, ftp: false, master: true});
  await expect.poll(() => groupState(page)).toBe('mixed');
});

test('red when the master is off, whatever the children prefer', async ({page}) => {
  await renderStatus(page, {http: true, ftp: true, master: false});
  await expect.poll(() => groupState(page)).toBe('disabled');
});

test('yellow when the master is on and EVERY child is disabled', async ({page}) => {
  // "one or more children disabled" does not stop being true when the number
  // reaches all of them. Reporting this as disabled/red would make an open
  // gate indistinguishable from a closed one.
  await renderStatus(page, {http: false, ftp: false, master: true});
  await expect.poll(() => groupState(page)).toBe('mixed');
});

test('red is reserved for the master gate, never for the children', async ({page}) => {
  for (const [http, ftp] of [[true, true], [true, false], [false, false]]) {
    await renderStatus(page, {http, ftp, master: true});
    await expect.poll(() => groupState(page),
      `master ON with http=${http} ftp=${ftp} must not report disabled`).not.toBe('disabled');
  }
  await renderStatus(page, {http: true, ftp: true, master: false});
  await expect.poll(() => groupState(page)).toBe('disabled');
});

test('the General Sources row renders, with a state, in every one of those states', async ({page}) => {
  const expected = {
    'true|true|true': 'healthy',
    'true|false|true': 'mixed',
    'false|false|true': 'mixed',
    'true|true|false': 'disabled',
    'false|false|false': 'disabled',
  };
  for (const [http, ftp, master] of [[true, true, true], [true, false, true],
                                     [false, false, true], [true, true, false], [false, false, false]]) {
    await renderStatus(page, {http, ftp, master});
    const row = page.locator(`#provider-status-list [data-provider-group="${GROUP}"]`);
    await expect(row, `http=${http} ftp=${ftp} master=${master}`).toHaveCount(1);
    await expect(row).toContainText('General Sources');
    await expect.poll(() => groupState(page),
      `http=${http} ftp=${ftp} master=${master}`).toBe(expected[`${http}|${ftp}|${master}`]);
  }
});

// --- immediate control semantics --------------------------------------------

test('the master toggle persists on change, with no Apply Settings', async ({page}) => {
  await setMaster(page, true);
  await openSources(page);
  await expect(masterTrack(page)).toHaveCount(1);
  await masterTrack(page).click();
  await expect.poll(async () => (await canonical(page)).integration_groups[GROUP].enabled).toBe(false);
  await page.reload();
  await openSources(page);
  await expect(page.locator(`[data-integration-group-enabled="${GROUP}"]`)).not.toBeChecked();
});

test('every Sources & Providers enable control is immediate', async ({page}) => {
  await openSources(page);
  const controls = await page.$$eval(
    '.dp-settings-panel[data-panel="sources"] [data-integration-enabled], ' +
    '.dp-settings-panel[data-panel="sources"] [data-integration-group-enabled]',
    nodes => nodes.map(n => n.dataset.integrationEnabled || n.dataset.integrationGroupEnabled));
  expect(new Set(controls)).toEqual(new Set(['alldebrid', 'usenet', 'general_http', 'general_ftp', GROUP]));
});

test('a later Apply Settings cannot replay a stale master or child value', async ({page}) => {
  await setChildren(page, true, true);
  await setMaster(page, true);
  await openSources(page);
  await masterTrack(page).click();
  await expect.poll(async () => (await canonical(page)).integration_groups[GROUP].enabled).toBe(false);
  await childTrack(page, 'general_ftp').click();
  await expect.poll(async () => (await canonical(page)).integrations.general_ftp.enabled).toBe(false);

  // An unrelated deferred edit, then the page-level write.
  await page.locator('#view-settings [data-tab="downloads"]').click();
  await page.locator('#view-settings [data-setting="min_free_disk_gb"]').fill('6');
  await page.locator('#view-settings [data-action="save"]').click();
  await expect.poll(async () => (await canonical(page)).min_free_disk_gb).toBe(6);

  const settings = await canonical(page);
  expect(settings.integration_groups[GROUP].enabled, 'Apply replayed a stale master').toBe(false);
  expect(settings.integrations.general_ftp.enabled, 'Apply replayed a stale child').toBe(false);
});

test('a rejected master write leaves the control on canonical state', async ({page}) => {
  await setMaster(page, true);
  await openSources(page);
  await page.route(url => url.pathname.startsWith('/api/integration-groups/'), route =>
    route.fulfill({status: 500, contentType: 'application/json', body: JSON.stringify({detail: 'nope'})}));
  await masterTrack(page).click();
  await expect(page.locator(`[data-integration-group-enabled="${GROUP}"]`)).toBeChecked();
  expect((await canonical(page)).integration_groups[GROUP].enabled).toBe(true);
});

test('an unknown group id is refused', async ({page}) => {
  const response = await page.request.patch(
    '/api/integration-groups/not_a_real_group/configuration', {data: {enabled: false}});
  expect(response.status()).toBe(404);
});
