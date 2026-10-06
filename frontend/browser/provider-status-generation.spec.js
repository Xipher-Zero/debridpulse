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

test('UISTATE-001-D navigation invalidates observation started by prior surface', async ({ page }) => {
  await bootstrap(page);
  const c = await controlledStatus(page);
  const r1 = c.start(); await c.count(1);
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await c.resolve(0, {state:'healthy', username:'stale-nav'}); await r1;
  await expect(alldebrid(page)).toHaveCount(0);
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
