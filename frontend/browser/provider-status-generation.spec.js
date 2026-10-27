const { test, expect } = require('@playwright/test');

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({
    status: 200, contentType: 'text/css', body: '',
  }));
}

async function bootstrap(page) {
  await isolateExternalFonts(page);
  // These cases count exactly the observations they start themselves. A live
  // pulse legitimately re-observes Provider Status (on connect, and whenever
  // the shared backend announces an integration status change -- which other
  // spec files cause), so this page is kept off the pulse.
  await page.route('**/api/events/stream', route => route.abort());
  await page.goto('/');
  await expect.poll(() => page.evaluate(() => !!window.DPProviderStatus)).toBeTruthy();
  await expect(page.locator('#provider-status-list')).toBeVisible();
  // These cases observe AllDebrid's probe, and only an enabled integration is
  // probed. A fresh installation switches every premium service off, so the
  // page's own settings document enables AllDebrid -- page-local, never the
  // shared backend (this is an injected-document spec).
  await expect.poll(() => page.evaluate(() => !!settingsData?.integrations?.alldebrid)).toBeTruthy();
  await page.evaluate(() => { settingsData.integrations.alldebrid.enabled = true; });
}

async function controlledStatus(page) {
  const pending = [];
  await page.route('**/api/integration-status/alldebrid', route => pending.push(route));
  const start = () => page.evaluate(() => window.DPProviderStatus.refresh());
  const count = async n => expect.poll(() => pending.length).toBe(n);
  const resolve = async (index, body, status = 200) => pending[index].fulfill({
    status, contentType: 'application/json', body: JSON.stringify(body),
  });
  return {pending, start, count, resolve};
}

const alldebrid = page => page.locator('#provider-status-list [data-provider-id="alldebrid"]');

test('UISTATE-001-A older healthy response cannot overwrite newer disabled state', async ({ page }) => {
  await bootstrap(page);
  const c = await controlledStatus(page);
  const r1 = c.start(); await c.count(1);
  const r2 = c.start(); await c.count(2);
  await c.resolve(1, {state:'disabled'}); await r2;
  await expect(alldebrid(page)).toHaveCount(0);
  await c.resolve(0, {state:'healthy', username:'old-user'}); await r1;
  await expect(alldebrid(page)).toHaveCount(0);
});

test('UISTATE-001-B configuration generation wins over older unconfigured response', async ({ page }) => {
  await bootstrap(page);
  const c = await controlledStatus(page);
  const r1 = c.start(); await c.count(1);
  await page.evaluate(() => window.DPProviderStatus.invalidate());
  const r2 = c.start(); await c.count(2);
  await c.resolve(1, {state:'healthy', username:'configured'}); await r2;
  await c.resolve(0, {state:'unconfigured'}); await r1;
  await expect(alldebrid(page)).toHaveAttribute('data-provider-state', 'healthy');
  await expect(alldebrid(page)).toContainText('AllDebrid');
});

test('UISTATE-001-C rapid overlapping polling permits only newest generation to render', async ({ page }) => {
  await bootstrap(page);
  const c = await controlledStatus(page);
  const r1 = c.start(); await c.count(1);
  const r2 = c.start(); await c.count(2);
  const r3 = c.start(); await c.count(3);
  await c.resolve(2, {state:'healthy', username:'newest'}); await r3;
  await c.resolve(1, {state:'disabled'}); await r2;
  await c.resolve(0, {state:'unhealthy'}); await r1;
  await expect(alldebrid(page)).toHaveAttribute('data-provider-state', 'healthy');
});

// Provider Status is sidebar-global: a page transition changes no provider
// fact, so it never discards the observation in flight -- the answer renders
// without waiting for another trigger (the 60 s safety net among them).
test('UISTATE-001-D navigation does not invalidate the sidebar-global observation', async ({ page }) => {
  await bootstrap(page);
  const c = await controlledStatus(page);
  const r1 = c.start(); await c.count(1);
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await c.resolve(0, {state:'healthy', username:'after-nav'}); await r1;
  await expect(alldebrid(page)).toHaveAttribute('data-provider-state', 'healthy');
  expect(c.pending).toHaveLength(1);                                  // no second observation was needed
});

test('UISTATE-001-E settings-generation invalidation defeats pre-save observation', async ({ page }) => {
  await bootstrap(page);
  const c = await controlledStatus(page);
  const r1 = c.start(); await c.count(1);
  await page.evaluate(() => window.DPProviderStatus.invalidate());
  const r2 = c.start(); await c.count(2);
  await c.resolve(1, {state:'disabled'}); await r2;
  await c.resolve(0, {state:'healthy', username:'pre-save'}); await r1;
  await expect(alldebrid(page)).toHaveCount(0);
});

test('UISTATE-001-F obsolete request error cannot replace newer provider truth', async ({ page }) => {
  await bootstrap(page);
  const c = await controlledStatus(page);
  const r1 = c.start(); await c.count(1);
  const r2 = c.start(); await c.count(2);
  await c.resolve(1, {state:'healthy', username:'authoritative'}); await r2;
  await c.resolve(0, {detail:'old failure'}, 503); await r1;
  await expect(alldebrid(page)).toHaveAttribute('data-provider-state', 'healthy');
});

// Live provider routing corrective: a provider whose status endpoint did not
// answer this time keeps its last answer while that answer is still valid;
// only with neither a current observation nor a valid last-known-good is it
// unresolved. The ordinary cadence -- not a provider switch -- brings it back.
async function answering(page) {
  const endpoint = {mode: 'healthy', asked: 0};
  await page.route('**/api/integration-status/alldebrid', route => {
    endpoint.asked += 1;
    if (endpoint.mode === 'down') return route.abort('timedout');
    return route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({state: 'healthy', checked: true})});
  });
  return endpoint;
}

test('UISTATE-002-A a missed observation keeps the valid last-known-good, never yellow', async ({ page }) => {
  await page.clock.install();
  const endpoint = await answering(page);
  await bootstrap(page);
  await page.evaluate(() => window.DPProviderStatus.refresh());
  await expect(alldebrid(page)).toHaveAttribute('data-provider-state', 'healthy');

  endpoint.mode = 'down';                                       // the next probe gets no answer
  const asked = endpoint.asked;
  await page.evaluate(() => window.DPProviderStatus.refresh());
  expect(endpoint.asked).toBeGreaterThan(asked);
  await expect(alldebrid(page)).toHaveAttribute('data-provider-state', 'healthy');
  await expect(alldebrid(page).locator('.dot')).toHaveClass(/\bok\b/);

  // Past the last-known-good's validity, with still no answer: unresolved.
  await page.clock.fastForward('05:01');
  await page.evaluate(() => window.DPProviderStatus.refresh());
  await expect(alldebrid(page)).toHaveAttribute('data-provider-state', 'unknown');

  // The ordinary 60 s refresh restores it once the endpoint answers again.
  endpoint.mode = 'healthy';
  await page.clock.fastForward('01:01');
  await expect(alldebrid(page)).toHaveAttribute('data-provider-state', 'healthy');
});

test('UISTATE-002-B with no earlier answer a missed observation is unresolved', async ({ page }) => {
  const endpoint = await answering(page);
  endpoint.mode = 'down';
  await bootstrap(page);
  await page.evaluate(() => window.DPProviderStatus.refresh());
  await expect(alldebrid(page)).toHaveAttribute('data-provider-state', 'unknown');
});

test('UISTATE-002-C an obsolete answer finishing last never becomes the last-known-good', async ({ page }) => {
  await bootstrap(page);
  const c = await controlledStatus(page);
  const r1 = c.start(); await c.count(1);
  const r2 = c.start(); await c.count(2);
  await c.resolve(1, {state:'unhealthy'}); await r2;               // newer truth wins the render
  await expect(alldebrid(page)).toHaveAttribute('data-provider-state', 'unhealthy');
  await c.resolve(0, {state:'healthy', username:'obsolete'}); await r1;
  await expect(alldebrid(page)).toHaveAttribute('data-provider-state', 'unhealthy');
  const r3 = c.start(); await c.count(3);
  await c.pending[2].abort('timedout'); await r3;                    // the next probe gets no answer
  await expect(alldebrid(page)).toHaveAttribute('data-provider-state', 'unhealthy');
});

test('UISTATE-002-D an observation begun before a settings save never seeds the last-known-good', async ({ page }) => {
  const pending = [];                                               // held from page load: no answer ever seeds it
  await page.route('**/api/integration-status/alldebrid', route => pending.push(route));
  await bootstrap(page);
  const base = pending.length;
  const start = () => page.evaluate(() => window.DPProviderStatus.refresh());
  const r1 = start(); await expect.poll(() => pending.length).toBe(base + 1);
  await page.evaluate(() => window.DPProviderStatus.invalidate());  // the settings save
  await pending[base].fulfill({status: 200, contentType: 'application/json',
    body: JSON.stringify({state: 'healthy', username: 'pre-save'})}); await r1;
  const r2 = start(); await expect.poll(() => pending.length).toBe(base + 2);
  await pending[base + 1].abort('timedout'); await r2;
  await expect(alldebrid(page)).toHaveAttribute('data-provider-state', 'unknown');
});

// Accepted participation is published at once; a slow probe defers only its
// own provider's health. Settings, its PATCH and every status endpoint are
// held under the page's control: the document is built once from what the
// page booted with, then served -- and every enable answered -- from the
// page's own state, so nothing reaches the shared backend's settings (an
// injected-document spec, test_browser_settings_key_ownership.py).
const DAY = 86400;
const premium = () => ({state:'healthy', checked:true, account:{service_class:'premium', entitlement:'ready',
  functional:'ready', plan:'Premium', expires_at:Math.floor(Date.now() / 1000) + 30 * DAY}});

async function acceptedSettings(page, overrides) {
  await bootstrap(page);
  await expect.poll(() => page.evaluate(() => !!settingsData?.integrations?.realdebrid)).toBeTruthy();
  const doc = await page.evaluate(() => JSON.parse(JSON.stringify(settingsData)));
  for (const [id, over] of Object.entries(overrides)) Object.assign(doc.integrations[id], over);
  const ctl = {doc, hold:new Set(), pending:[]};
  await page.route('**/api/settings', route => route.fulfill({
    status:200, contentType:'application/json', body:JSON.stringify(ctl.doc)}));
  await page.route(/\/api\/integrations\/[^/]+\/configuration$/, route => {
    const id = route.request().url().split('/integrations/')[1].split('/')[0];
    ctl.doc.integrations[id].enabled = route.request().postDataJSON().enabled;
    return route.fulfill({status:200, contentType:'application/json', body:JSON.stringify({ok:true, ...ctl.doc.integrations[id]})});
  });
  await page.route(/\/api\/integration-status\/[^/]+$/, route => {
    const id = route.request().url().split('/integration-status/')[1];
    const answer = () => route.fulfill({status:200, contentType:'application/json',
      body:JSON.stringify(id === 'media' ? {state:'healthy'} : premium())});
    if (ctl.hold.has(id)) ctl.pending.push({id, answer});
    else return answer();
  });
  ctl.release = async id => {
    const due = ctl.pending.filter(item => item.id === id);
    ctl.pending = ctl.pending.filter(item => item.id !== id);
    for (const item of due) await item.answer();
  };
  return ctl;
}

async function openSources(page) {
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await page.locator('#view-settings [data-tab="sources"]').click();
  await expect(page.locator('.dp-settings-panel[data-panel="sources"]')).toBeVisible();
}

const provider = (page, id) => page.locator(`#provider-status-list [data-provider-id="${id}"]`);
const directSources = page => page.locator('#provider-status-list [data-provider-group="direct_sources"] [data-provider-state]');
const premiumLine = page => page.locator('#lbl-premium');
const accept = (page, integration) => page.locator(`label[for="dp-settings-integration-${integration}-enabled"]`).click();
const configured = {configured:true, verified:true};
const ENABLED = {alldebrid:{enabled:true, ...configured}, realdebrid:{enabled:true, ...configured}};

test('UISTATE-003-A an accepted disable is immediate while an unrelated probe is held', async ({ page }) => {
  const ctl = await acceptedSettings(page, ENABLED);
  await bootstrap(page);
  await expect(provider(page, 'realdebrid')).toHaveAttribute('data-provider-state', 'healthy');
  await expect(premiumLine(page)).toContainText('Real-Debrid');
  await openSources(page);
  ctl.hold.add('alldebrid');
  await accept(page, 'realdebrid');
  await expect.poll(() => ctl.pending.length).toBe(1);
  // Real-Debrid's accepted participation and its premium line are gone, the
  // group untouched, while AllDebrid's probe is still outstanding.
  await expect(provider(page, 'realdebrid')).toHaveCount(0);
  await expect(premiumLine(page)).not.toContainText('Real-Debrid');
  await expect(provider(page, 'alldebrid')).toHaveAttribute('data-provider-state', 'healthy');
  await expect(directSources(page)).toHaveAttribute('data-provider-state', 'healthy');
  // A member's accepted disable changes the aggregate in the same immediate snapshot.
  // (Its tile is inside Network Sources, closed on arrival: opening it is
  // local presentation, no request.)
  await page.locator('.dp-settings-general-sources .dp-settings-disclosure').click();
  await accept(page, 'media');
  await expect.poll(() => ctl.pending.length).toBe(2);
  await expect(directSources(page)).toHaveAttribute('data-provider-state', 'mixed');
  expect(ctl.pending.map(item => item.id)).toEqual(['alldebrid', 'alldebrid']);
});

test('UISTATE-003-B an accepted enable is Checking until its own answer, whatever else is held', async ({ page }) => {
  const ctl = await acceptedSettings(page, {...ENABLED, realdebrid:{enabled:false, ...configured}});
  await bootstrap(page);
  await expect(provider(page, 'alldebrid')).toHaveAttribute('data-provider-state', 'healthy');
  await openSources(page);
  ctl.hold.add('alldebrid'); ctl.hold.add('realdebrid');
  await accept(page, 'realdebrid');
  await expect.poll(() => ctl.pending.length).toBe(2);
  await expect(provider(page, 'realdebrid')).toHaveAttribute('data-provider-state', 'checking');
  await expect(premiumLine(page)).not.toContainText('Real-Debrid');
  // Unchanged AllDebrid keeps its valid observation while it is rechecked.
  await expect(provider(page, 'alldebrid')).toHaveAttribute('data-provider-state', 'healthy');
  await ctl.release('realdebrid');
  await expect(provider(page, 'realdebrid')).toHaveAttribute('data-provider-state', 'healthy');
  await expect(premiumLine(page)).toContainText('Real-Debrid');
  expect(ctl.pending.map(item => item.id)).toEqual(['alldebrid']);  // still held
});

test('UISTATE-003-C status announcements cannot hide committed participation', async ({ page }) => {
  const ctl = await acceptedSettings(page, ENABLED);
  await bootstrap(page);
  await expect(provider(page, 'alldebrid')).toHaveAttribute('data-provider-state', 'healthy');
  await openSources(page);
  ctl.hold.add('alldebrid');
  await accept(page, 'realdebrid');
  await expect.poll(() => ctl.pending.length).toBe(1);
  for (let n = 2; n <= 3; n += 1) {
    await page.evaluate(() => document.dispatchEvent(new CustomEvent('debridpulse:integration-status-changed')));
    await expect.poll(() => ctl.pending.length).toBe(n);
    await expect(provider(page, 'realdebrid')).toHaveCount(0);
    await expect(provider(page, 'alldebrid')).toHaveAttribute('data-provider-state', 'healthy');
  }
  await ctl.release('alldebrid');
  await expect(provider(page, 'realdebrid')).toHaveCount(0);
  await expect(provider(page, 'alldebrid')).toHaveAttribute('data-provider-state', 'healthy');
});
