const { test, expect } = require('@playwright/test');

/* DP 1.0.13 Items 5 and 10 -- the Network Sources master gate, and every
 * Services Enable toggle as an immediate canonical control.
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

/* Services cards arrive COLLAPSED: expansion is local presentation
 * state, never a projection of enabled/configured/verified state. A General
 * Sources member toggle lives inside that group's body, so operating one means
 * opening the card first -- exactly what the operator does, through the one
 * canonical disclosure. The master's own toggle is in the header and is always
 * reachable. */
async function reveal(page, id) {
  const label = page.locator(`label[for="dp-settings-integration-${id}-enabled"]`);
  if (await label.isVisible()) return;
  const disclosure = page.locator('.dp-settings-general-sources .dp-settings-disclosure');
  if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click();
  await expect(label).toBeVisible();
}

/** Operate one member toggle, opening the group card if it is still closed. */
async function flipChild(page, id) {
  await reveal(page, id);
  await childTrack(page, id).click();
}

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
    {[GROUP]: {enabled: master, label: 'Network Sources', members: CHILDREN}},
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
  expect(settings.integration_groups[GROUP].label).toBe('Network Sources');
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
  await flipChild(page, 'general_ftp');
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

test('red when the master is on and EVERY child is disabled', async ({page}) => {
  // An open gate with no participating member cannot acquire anything, so
  // yellow ("some still work") was untrue. It reports red through its own
  // neutral zero-participant state rather than by pretending the master is
  // off, which remains a different and still-meaningful thing.
  await renderStatus(page, {http: false, ftp: false, master: true});
  await expect.poll(() => groupState(page)).toBe('unavailable');
  expect(await page.locator(`#provider-status-list [data-provider-group="${GROUP}"] .dot`)
    .getAttribute('class')).toContain('error');
});

test('the closed gate and the empty open gate stay distinguishable', async ({page}) => {
  // Both are red, and neither is allowed to impersonate the other.
  await renderStatus(page, {http: false, ftp: false, master: true});
  await expect.poll(() => groupState(page)).toBe('unavailable');
  await renderStatus(page, {http: true, ftp: true, master: false});
  await expect.poll(() => groupState(page)).toBe('disabled');
  for (const [http, ftp] of [[true, true], [true, false]]) {
    await renderStatus(page, {http, ftp, master: true});
    await expect.poll(() => groupState(page),
      `master ON with http=${http} ftp=${ftp} must not report the gate closed`).not.toBe('disabled');
  }
});

test('the Network Sources row renders, with a state, in every one of those states', async ({page}) => {
  const expected = {
    'true|true|true': 'healthy',
    'true|false|true': 'mixed',
    'false|false|true': 'unavailable',
    'true|true|false': 'disabled',
    'false|false|false': 'disabled',
  };
  for (const [http, ftp, master] of [[true, true, true], [true, false, true],
                                     [false, false, true], [true, true, false], [false, false, false]]) {
    await renderStatus(page, {http, ftp, master});
    const row = page.locator(`#provider-status-list [data-provider-group="${GROUP}"]`);
    await expect(row, `http=${http} ftp=${ftp} master=${master}`).toHaveCount(1);
    await expect(row).toContainText('Network Sources');
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

test('every Services enable control is immediate', async ({page}) => {
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
  await flipChild(page, 'general_ftp');
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


// --- immediate Provider Status convergence (no Apply Settings) --------------

/* The REAL operator controls against the REAL backend.
 *
 * The renderer-unit cases above drive one explicit canonical document, which
 * proves the colour rule but not the convergence: an accepted scoped mutation
 * used to update only the Settings page's own copy, so the status renderer --
 * which reads the one global document -- kept serving pre-mutation state until
 * an unrelated Apply Settings happened to perform a fresh GET. */
test.describe.serial('immediate Network Sources status convergence', () => {
  test('master and member toggles converge the sidebar status with no Apply Settings', async ({page}) => {
    await setChildren(page, true, true);
    await setMaster(page, true);
    await openSources(page);
    await expect.poll(() => groupState(page)).toBe('healthy');

    await flipChild(page, 'general_ftp');
    await expect.poll(async () => (await canonical(page)).integrations.general_ftp.enabled).toBe(false);
    await expect.poll(() => groupState(page), 'one member off did not converge').toBe('mixed');

    await flipChild(page, 'general_http');
    await expect.poll(async () => (await canonical(page)).integrations.general_http.enabled).toBe(false);
    await expect.poll(() => groupState(page), 'all members off did not converge').toBe('unavailable');

    await masterTrack(page).click();
    await expect.poll(async () => (await canonical(page)).integration_groups[GROUP].enabled).toBe(false);
    await expect.poll(() => groupState(page), 'the closed gate did not converge').toBe('disabled');

    // Nothing above went through the page-level write.
    await expect(page.locator('#view-settings [data-action="save"]')).toBeVisible();
  });

  test('the accepted mutation is published to the one settings document', async ({page}) => {
    await setChildren(page, true, true);
    await setMaster(page, true);
    await openSources(page);
    await flipChild(page, 'general_http');
    await expect.poll(async () => (await canonical(page)).integrations.general_http.enabled).toBe(false);
    // Both frontend references to Settings state agree, because there is one
    // synchronisation owner and the acceptance helpers converge through it.
    await expect.poll(() => page.evaluate(() => {
      try { return settingsData?.integrations?.general_http?.enabled; } catch (_) { return 'unreadable'; }
    })).toBe(false);
  });
});
