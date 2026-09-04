const { test, expect } = require('@playwright/test');

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({
    status: 200, contentType: 'text/css', body: '',
  }));
}

async function bootstrap(page) {
  await isolateExternalFonts(page);
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
  await expect(alldebrid(page)).not.toHaveAttribute('data-provider-state', 'healthy');
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
