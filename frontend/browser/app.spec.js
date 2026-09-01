const { test, expect } = require('@playwright/test');

const canonicalViews = [
  ['dashboard', 'Dashboard'],
  ['torrents', 'Downloads'],
  ['events', 'Event Log'],
  ['stats', 'Statistics'],
  ['settings', 'Settings'],
  ['help', 'Help & License'],
];

const retiredIds = ['view-changelog', 'view-aria2queue', 'view-support'];
const retiredRequestFragments = [
  'ui-presentation-loader',
  'ui-page-finalization',
  'ui-dashboard-batch5',
  'ui-dashboard-polish',
  'ui-shell-runtime',
  'auth-polish',
  'auth-finalization',
];

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({
    status: 200,
    contentType: 'text/css',
    body: '',
  }));
}

function observeRuntime(page) {
  const errors = [];
  const requests = [];
  const badResponses = [];

  page.on('pageerror', error => errors.push(`pageerror: ${error.message}`));
  page.on('console', message => {
    if (message.type() === 'error') errors.push(`console: ${message.text()}`);
  });
  page.on('request', request => requests.push(request.url()));
  page.on('response', response => {
    const url = response.url();
    if (url.startsWith(page.url().split('/').slice(0, 3).join('/')) && response.status() >= 400) {
      badResponses.push(`${response.status()} ${url}`);
    }
  });

  return { errors, requests, badResponses };
}

async function installLegacyMutationWatch(page) {
  await page.addInitScript(({ ids, fragments }) => {
    window.__dpRetiredRuntimeHits = [];
    const idSet = new Set(ids);
    const lowered = fragments.map(value => value.toLowerCase());

    const inspect = node => {
      if (!node || node.nodeType !== Node.ELEMENT_NODE) return;
      const elements = [node, ...node.querySelectorAll('*')];
      for (const element of elements) {
        const id = String(element.id || '');
        if (idSet.has(id)) window.__dpRetiredRuntimeHits.push(`id:${id}`);
        for (const attr of ['src', 'href']) {
          const value = String(element.getAttribute?.(attr) || '').toLowerCase();
          if (value && lowered.some(fragment => value.includes(fragment))) {
            window.__dpRetiredRuntimeHits.push(`${attr}:${value}`);
          }
        }
      }
    };

    const observer = new MutationObserver(records => {
      for (const record of records) {
        if (record.type === 'attributes') inspect(record.target);
        for (const node of record.addedNodes) inspect(node);
      }
    });
    observer.observe(document, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ['id', 'src', 'href'],
    });
  }, { ids: retiredIds, fragments: retiredRequestFragments });
}

async function expectCanonicalShell(page) {
  const views = await page.locator('#sidebar .nav-item[data-view]').evaluateAll(nodes => nodes.map(node => node.dataset.view));
  expect(views).toEqual(canonicalViews.map(([id]) => id));
  for (const id of retiredIds) await expect(page.locator(`#${id}`)).toHaveCount(0);
}

async function openView(page, id, title) {
  await page.locator(`#sidebar .nav-item[data-view="${id}"]`).click();
  await expect(page.locator(`#view-${id}`)).toHaveClass(/\bactive\b/);
  await expect(page.locator('#page-title')).toHaveText(title);
  if (id === 'settings') await expect(page.locator('.dp-settings-master-card')).toBeVisible();
  if (id === 'help') await expect(page.locator('.dp-help-master-card')).toBeVisible();
}

test('cold load keeps only canonical owners and all six surfaces survive repeated navigation', async ({ page }) => {
  await isolateExternalFonts(page);
  await installLegacyMutationWatch(page);
  const runtime = observeRuntime(page);

  await page.goto('/');
  await expect(page.locator('#view-dashboard')).toHaveClass(/\bactive\b/);
  await expectCanonicalShell(page);

  for (const [id, title] of canonicalViews) await openView(page, id, title);
  for (const [id, title] of canonicalViews.slice().reverse()) await openView(page, id, title);
  await openView(page, 'dashboard', 'Dashboard');

  const mutationHits = await page.evaluate(() => window.__dpRetiredRuntimeHits || []);
  expect(mutationHits).toEqual([]);
  expect(runtime.requests.filter(url => retiredRequestFragments.some(fragment => url.toLowerCase().includes(fragment)))).toEqual([]);
  expect(runtime.badResponses).toEqual([]);
  expect(runtime.errors).toEqual([]);
});

test('theme choice survives reload and canonical pages remain stable', async ({ page }) => {
  await isolateExternalFonts(page);
  const runtime = observeRuntime(page);
  await page.goto('/');

  const before = await page.evaluate(() => {
    const explicit = document.documentElement.dataset.theme;
    if (explicit === 'light' || explicit === 'dark') return explicit;
    return matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  });
  await page.locator('#theme-toggle').click();
  const after = await page.evaluate(() => document.documentElement.dataset.theme);
  expect(after).toBe(before === 'light' ? 'dark' : 'light');

  await page.reload();
  await expect(page.locator('#view-dashboard')).toHaveClass(/\bactive\b/);
  expect(await page.evaluate(() => document.documentElement.dataset.theme)).toBe(after);
  await openView(page, 'settings', 'Settings');
  await openView(page, 'stats', 'Statistics');
  await openView(page, 'dashboard', 'Dashboard');
  expect(runtime.errors).toEqual([]);
});

test('handled Event Log API failure stays inside the UI error lifecycle', async ({ page }) => {
  await isolateExternalFonts(page);
  const runtime = observeRuntime(page);
  await page.goto('/');

  let injected = 0;
  await page.route('**/api/events?limit=500', route => {
    injected += 1;
    return route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'browser gate injected failure' }),
    });
  });

  await openView(page, 'events', 'Event Log');
  await expect.poll(() => injected).toBeGreaterThan(0);
  await expect(page.locator('.toast.error .dp-toast-copy')).toContainText('browser gate injected failure');
  await expect(page.locator('#view-events')).toHaveClass(/\bactive\b/);
  expect(runtime.errors).toEqual([]);
});

test('Help legal modal traps lifecycle and restores opener focus', async ({ page }) => {
  await isolateExternalFonts(page);
  const runtime = observeRuntime(page);
  await page.goto('/');
  await openView(page, 'help', 'Help & License');

  await page.locator('.dp-help-tab[data-tab="license"]').click();
  const opener = page.locator('[data-legal-document="gpl"]');
  await opener.focus();
  await opener.click();
  await expect(page.locator('.dp-help-legal-dialog')).toBeVisible();
  await expect(page.locator('.dp-help-legal-close')).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.locator('.dp-help-legal-dialog')).toHaveCount(0);
  await expect(opener).toBeFocused();
  expect(runtime.errors).toEqual([]);
});

test('password-authenticated cold start redirects to login and bootstraps canonical app after sign-in', async ({ browser }) => {
  const authBase = process.env.DP_AUTH_BASE_URL;
  expect(authBase, 'DP_AUTH_BASE_URL must be provided by browser CI').toBeTruthy();

  const context = await browser.newContext({ baseURL: authBase, viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  await isolateExternalFonts(page);
  await installLegacyMutationWatch(page);
  const runtime = observeRuntime(page);

  await page.goto('/');
  await expect(page).toHaveURL(/\/login(?:\?|$)/);
  await expect(page.locator('#username')).toBeVisible();
  await expect(page.locator('#password')).toBeVisible();
  await page.locator('#username').fill('browser');
  await page.locator('#password').fill('browser-secret');
  await page.locator('button[type="submit"]').click();
  await expect(page).toHaveURL(new RegExp(`${authBase.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}/?$`));
  await expect(page.locator('#view-dashboard')).toHaveClass(/\bactive\b/);
  await expectCanonicalShell(page);
  await openView(page, 'settings', 'Settings');
  await openView(page, 'help', 'Help & License');

  expect(await page.evaluate(() => window.__dpRetiredRuntimeHits || [])).toEqual([]);
  expect(runtime.errors).toEqual([]);
  await context.close();
});
