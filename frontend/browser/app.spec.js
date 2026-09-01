const { test, expect } = require('@playwright/test');

const canonicalViews = [
  ['dashboard', 'Dashboard'],
  ['torrents', 'Downloads'],
  ['events', 'Activity Log'],
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

async function expectNoRetiredRuntime(runtime, page) {
  expect(await page.evaluate(() => window.__dpRetiredRuntimeHits || [])).toEqual([]);
  expect(runtime.requests.filter(url => retiredRequestFragments.some(fragment => url.toLowerCase().includes(fragment)))).toEqual([]);
}

test('cold load keeps only canonical owners and all six surfaces survive repeated, reverse, and rapid navigation', async ({ page }) => {
  await isolateExternalFonts(page);
  await installLegacyMutationWatch(page);
  const runtime = observeRuntime(page);

  await page.goto('/');
  await expect(page.locator('#view-dashboard')).toHaveClass(/\bactive\b/);
  await expectCanonicalShell(page);

  for (const [id, title] of canonicalViews) await openView(page, id, title);
  for (const [id, title] of canonicalViews.slice().reverse()) await openView(page, id, title);

  await page.evaluate(ids => {
    for (const id of ids) document.querySelector(`#sidebar .nav-item[data-view="${id}"]`)?.click();
  }, ['help', 'settings', 'stats', 'events', 'torrents', 'dashboard']);
  await expect(page.locator('#view-dashboard')).toHaveClass(/\bactive\b/);
  await expect(page.locator('#page-title')).toHaveText('Dashboard');

  await expectNoRetiredRuntime(runtime, page);
  expect(runtime.badResponses).toEqual([]);
  expect(runtime.errors).toEqual([]);
});

test('Dashboard runtime hydrates KPI and sparkline state and submission field preserves the five-line cap', async ({ page }) => {
  await isolateExternalFonts(page);
  const runtime = observeRuntime(page);
  await page.goto('/');

  await expect(page.locator('#view-dashboard')).toHaveClass(/\bactive\b/);
  await expect(page.locator('.dash-hero-stat')).toHaveCount(6);
  await expect(page.locator('.dp-card-spark')).toHaveCount(6);
  await expect(page.locator('.dp-card-spark-line')).toHaveCount(6);
  await expect(page.locator('#s-total')).not.toHaveText('—');
  await expect.poll(() => page.evaluate(() => Boolean(localStorage.getItem('debridpulse.dashboard.metric-history.v2')))).toBeTruthy();

  const input = page.locator('#q-transfer-input');
  await expect(input).toBeVisible();
  const initialHeight = await input.evaluate(node => node.clientHeight);
  await input.fill('https://example.com/1\nhttps://example.com/2\nhttps://example.com/3\nhttps://example.com/4\nhttps://example.com/5\nhttps://example.com/6');
  const expanded = await input.evaluate(node => ({clientHeight: node.clientHeight, scrollHeight: node.scrollHeight, value: node.value}));
  expect(expanded.clientHeight).toBeGreaterThan(initialHeight);
  expect(expanded.scrollHeight).toBeGreaterThan(expanded.clientHeight);
  expect(expanded.value.split('\n')).toHaveLength(6);
  await input.fill('https://example.com/only-one');
  expect(await input.inputValue()).toBe('https://example.com/only-one');
  await expect(page.locator('#btn-add-transfer')).toBeEnabled();
  expect(runtime.errors).toEqual([]);
});

test('Downloads, Statistics, and Settings expose their canonical interactive controls', async ({ page }) => {
  await isolateExternalFonts(page);
  const runtime = observeRuntime(page);
  await page.goto('/');

  await openView(page, 'torrents', 'Downloads');
  await expect(page.locator('#view-torrents .filter-tabs .ftab')).toHaveCount(7);
  await page.locator('#view-torrents .ftab[data-dp-status="error"]').click();
  await expect(page.locator('#view-torrents .ftab[data-dp-status="error"]')).toHaveAttribute('aria-selected', 'true');
  await page.locator('#torrent-search').fill('browser-smoke-query');
  expect(await page.locator('#torrent-search').inputValue()).toBe('browser-smoke-query');
  await expect(page.locator('#bulk-bar')).toBeVisible();
  await expect(page.locator('#bulk-bar .dp-downloads-bulk-action')).toHaveCount(5);
  await expect(page.locator('#chk-all')).toBeVisible();

  await openView(page, 'stats', 'Statistics');
  await expect(page.locator('.dp-statistics-master')).toBeVisible();
  await expect(page.locator('#detail-stat-cards [data-dp-stats-metric]')).toHaveCount(5);
  await page.locator('#stats-period-tabs .ftab[data-period="24h"]').click();
  await expect(page.locator('#stats-period-tabs .ftab[data-period="24h"]')).toHaveClass(/\bactive\b/);

  await openView(page, 'settings', 'Settings');
  await expect(page.locator('.dp-settings-tabs .stab')).toHaveCount(6);
  for (const tab of ['downloads', 'authentication', 'maintenance', 'sources']) {
    await page.locator(`.dp-settings-tabs .stab[data-tab="${tab}"]`).click();
    await expect(page.locator(`.dp-settings-tabs .stab[data-tab="${tab}"]`)).toHaveAttribute('aria-selected', 'true');
    await expect(page.locator(`.dp-settings-panel[data-panel="${tab}"]`)).toBeVisible();
  }

  expect(runtime.errors).toEqual([]);
});

test('hard reload after representative pages returns through a clean canonical bootstrap', async ({ page }) => {
  await isolateExternalFonts(page);
  await installLegacyMutationWatch(page);
  const runtime = observeRuntime(page);
  await page.goto('/');

  for (const [id, title] of [['settings', 'Settings'], ['stats', 'Statistics'], ['help', 'Help & License']]) {
    await openView(page, id, title);
    await page.reload();
    await expect(page.locator('#view-dashboard')).toHaveClass(/\bactive\b/);
    await expect(page.locator('#page-title')).toHaveText('Dashboard');
    await expectCanonicalShell(page);
  }

  await expectNoRetiredRuntime(runtime, page);
  expect(runtime.badResponses).toEqual([]);
  expect(runtime.errors).toEqual([]);
});

test('dark and light initialization persist across reload and canonical pages remain stable', async ({ page }) => {
  await isolateExternalFonts(page);
  const runtime = observeRuntime(page);
  await page.goto('/');

  expect(await page.evaluate(() => document.body.classList.contains('light') ? 'light' : 'dark')).toBe('dark');
  await page.locator('#theme-toggle').click();
  expect(await page.evaluate(() => ({
    applied: document.body.classList.contains('light') ? 'light' : 'dark',
    stored: localStorage.getItem('theme'),
  }))).toEqual({ applied: 'light', stored: 'light' });

  await page.reload();
  await expect(page.locator('#view-dashboard')).toHaveClass(/\bactive\b/);
  expect(await page.evaluate(() => ({
    applied: document.body.classList.contains('light') ? 'light' : 'dark',
    stored: localStorage.getItem('theme'),
  }))).toEqual({ applied: 'light', stored: 'light' });
  await openView(page, 'settings', 'Settings');
  await openView(page, 'stats', 'Statistics');
  await openView(page, 'dashboard', 'Dashboard');

  await page.locator('#theme-toggle').click();
  expect(await page.evaluate(() => ({
    applied: document.body.classList.contains('light') ? 'light' : 'dark',
    stored: localStorage.getItem('theme'),
  }))).toEqual({ applied: 'dark', stored: 'dark' });
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

  await openView(page, 'events', 'Activity Log');
  await expect.poll(() => injected).toBeGreaterThan(0);
  await expect(page.locator('.toast.error .dp-toast-copy')).toContainText('browser gate injected failure');
  await expect(page.locator('#view-events')).toHaveClass(/\bactive\b/);
  expect(runtime.badResponses.some(item => item.includes('503') && item.includes('/api/events?limit=500'))).toBeTruthy();
  const unexpectedErrors = runtime.errors.filter(
    error => error !== 'console: Failed to load resource: the server responded with a status of 503 (Service Unavailable)'
  );
  expect(unexpectedErrors).toEqual([]);
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

  await expectNoRetiredRuntime(runtime, page);
  expect(runtime.errors).toEqual([]);
  await context.close();
});
