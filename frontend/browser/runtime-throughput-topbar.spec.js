const { test, expect } = require('@playwright/test');

/* DP 1.0.13 work item G -- the topbar and the browser tab consume ONE neutral
 * aggregate download-throughput fact. Nothing generic polls an executor. */

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

/** Serve the neutral runtime-status payload and record every request path. */
async function runtimeStatus(page, value, seen) {
  await page.route(url => url.pathname.startsWith('/api/'), route => {
    const path = route.request().url().replace(/^https?:\/\/[^/]+/, '').split('?')[0];
    if (seen) seen.push(path);
    if (path === '/api/execution/runtime-status') {
      return route.fulfill({status: 200, contentType: 'application/json',
        body: JSON.stringify({ok: true, ...value})});
    }
    return route.fallback();
  });
}

test('the topbar shows the neutral aggregate speed, slots and cap', async ({page}) => {
  await isolateExternalFonts(page);
  const seen = [];
  await runtimeStatus(page, {download_bytes_per_second: 5 * 1024 * 1024,
    active_execution_slots: 2, max_download_bytes_per_second: 10 * 1024 * 1024}, seen);
  await page.goto('/');
  await expect(page.locator('#runtime-badge-speed')).toContainText('5', {timeout: 10000});
  await expect(page.locator('#runtime-badge-active')).toHaveText('2');
  await expect(page.locator('#runtime-badge-limit')).toContainText('10');
  // No generic presentation poll reaches an executor-specific route.
  expect(seen.filter(path => path.includes('/aria2/global-stat'))).toEqual([]);
  expect(seen.filter(path => path.includes('/aria2/global-options'))).toEqual([]);
});

test('the browser tab speed is the exact same neutral value as the topbar', async ({page}) => {
  await isolateExternalFonts(page);
  await runtimeStatus(page, {download_bytes_per_second: 3 * 1024 * 1024,
    active_execution_slots: 1, max_download_bytes_per_second: 0});
  await page.goto('/');
  // Give the title an active transfer to describe.
  await page.evaluate(() => updateOperatorTitle({by_status: {downloading: 1}, operator_active_progress_pct: 40}));
  await expect(page.locator('#runtime-badge-speed')).toContainText('3', {timeout: 10000});
  const badge = (await page.locator('#runtime-badge-speed').textContent()).replace(/\s+/g, '');
  await expect.poll(() => page.title()).toContain(badge);
  const state = await page.evaluate(() => _runtimeStatusState.liveBps);
  expect(state).toBe(3 * 1024 * 1024);
});

test('throughput settles to zero rather than retaining the last sampled speed', async ({page}) => {
  await isolateExternalFonts(page);
  let current = {download_bytes_per_second: 8 * 1024 * 1024,
    active_execution_slots: 3, max_download_bytes_per_second: 0};
  await page.route(url => url.pathname === '/api/execution/runtime-status',
    route => route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({ok: true, ...current})}));
  await page.goto('/');
  await expect(page.locator('#runtime-badge-speed')).toContainText('8', {timeout: 10000});
  current = {download_bytes_per_second: 0, active_execution_slots: 0, max_download_bytes_per_second: 0};
  await expect(page.locator('#runtime-badge-speed')).toContainText('0 KB/s', {timeout: 10000});
  await expect(page.locator('#runtime-badge-active')).toHaveText('0');
});

test('no generic presentation identifier is named after an executor', async ({page}) => {
  await isolateExternalFonts(page);
  await page.goto('/');
  const leaked = await page.evaluate(() => {
    const ids = [...document.querySelectorAll('#topbar [id]')].map(el => el.id);
    const classes = [...document.querySelectorAll('#topbar *')]
      .flatMap(el => [...el.classList]);
    return [...ids, ...classes].filter(name => /aria2|sab|nzb/i.test(name));
  });
  expect(leaked).toEqual([]);
  // The retired, executor-named global owner is gone -- not aliased.
  const retired = await page.evaluate(() => ['_aria2BadgeState', 'updateAria2TopbarBadge',
    'loadAria2TopbarStat', 'loadAria2SpeedLimit', 'loadAria2Runtime', '_setAria2Speed',
    'toggleAria2SpeedCapMenu', 'applyAria2TopbarSpeedCap']
    .filter(name => typeof window[name] !== 'undefined'));
  expect(retired).toEqual([]);
  // ...and the neutral owner that replaced it exists exactly once.
  expect(await page.evaluate(() => typeof window.updateRuntimeStatusBadge)).toBe('function');
});

test('the speed cap menu still writes through the neutral runtime-limit surface', async ({page}) => {
  await isolateExternalFonts(page);
  const writes = [];
  await page.route(url => url.pathname === '/api/execution/runtime-limits', route => {
    writes.push(route.request().method());
    return route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({ok: true, configured: {max_download_bytes_per_second: 1048576},
        effective: {max_download_bytes_per_second: 1048576}, last_apply_error: null})});
  });
  await page.goto('/');
  await page.locator('#runtime-cap-toggle').click();
  await page.locator('#runtime-cap-menu [data-cap-bps="1048576"]').click();
  await expect.poll(() => writes).toContain('PATCH');
});
