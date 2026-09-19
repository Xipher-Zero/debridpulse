const { test, expect } = require('@playwright/test');

// Scheduler capacity is universal transfer policy. The topbar denominator is
// rendered by app.js's updateAria2TopbarBadge() from
// settingsData.transfer_policy.max_concurrent_executions and nothing else: no
// wrapper runtime, no flat alias, and no value reported by the aria2 daemon.

async function ready(page) {
  await page.setViewportSize({width: 1280, height: 720});
  await page.goto('/');
  await page.waitForFunction(() => Boolean(window.DPProcessingPresentation && typeof updateAria2TopbarBadge === 'function'));
}

async function useSettings(page, {mode = 'builtin', concurrency = 7, limit = 0} = {}) {
  await page.evaluate(({mode, concurrency, limit}) => {
    settingsData = {
      integrations: {aria2: {options: {mode}}},
      transfer_policy: {max_concurrent_executions: concurrency},
      execution_runtime_limits: {max_download_bytes_per_second: limit},
      paused: false,
    };
    updateAria2TopbarBadge({active: 3, liveBps: 1048576});
  }, {mode, concurrency, limit});
}

const maxText = page => page.locator('#aria2-badge-max');

test('the retired topbar wrapper runtime is gone and updateAria2TopbarBadge is the app.js function itself', async ({ page }) => {
  await ready(page);
  await expect(page.locator('script[src*="ui-topbar-concurrency"]')).toHaveCount(0);
  expect((await page.request.get('/ui-topbar-concurrency.js')).status()).not.toBe(200);
  const shape = await page.evaluate(() => ({
    wrapperGlobal: typeof window.DPTopbarConcurrency,
    source: Function.prototype.toString.call(window.updateAria2TopbarBadge).slice(0, 44),
    sameBinding: window.updateAria2TopbarBadge === updateAria2TopbarBadge,
  }));
  expect(shape.wrapperGlobal).toBe('undefined');
  expect(shape.source).toContain('function updateAria2TopbarBadge(patch)');
  expect(shape.sameBinding).toBe(true);
});

for (const mode of ['builtin', 'external']) {
  test(`denominator is transfer_policy.max_concurrent_executions in ${mode} mode`, async ({ page }) => {
    await ready(page);
    await useSettings(page, {mode, concurrency: 7});
    await expect(page.locator('#aria2-badge-active')).toHaveText('3');
    await expect(maxText(page)).toHaveText('7');

    const visible = await page.evaluate(() => {
      const max = document.getElementById('aria2-badge-max');
      const badge = document.getElementById('aria2-speed-badge');
      return {
        fontSize: parseFloat(getComputedStyle(max).fontSize),
        pseudo: getComputedStyle(max, '::after').content,
        display: getComputedStyle(badge).display,
      };
    });
    expect(visible.fontSize).toBeGreaterThan(0);
    expect(visible.pseudo).not.toBe('"0"');
    expect(visible.display).not.toBe('none');

    // The runtime keeps display authority: nothing re-shows a badge app.js hid.
    const hidden = await page.evaluate(() => {
      const badge = document.getElementById('aria2-speed-badge');
      badge.style.display = 'none';
      return getComputedStyle(badge).display;
    });
    expect(hidden).toBe('none');
  });
}

test('a stale or native maximum in a badge patch cannot replace scheduler capacity', async ({ page }) => {
  await ready(page);
  await useSettings(page, {concurrency: 7});
  await page.evaluate(() => updateAria2TopbarBadge({maxDl: 3}));           // the retired stale-fallback shape
  await expect(maxText(page)).toHaveText('7');
  await page.evaluate(() => updateAria2TopbarBadge({maxDl: 99}));          // a native aria2 value
  await expect(maxText(page)).toHaveText('7');
});

test('the aria2 global-options response cannot override universal scheduler capacity', async ({ page }) => {
  await ready(page);
  await useSettings(page, {concurrency: 7});
  await page.route('**/api/aria2/global-options', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ok: true, mode: 'builtin', global_options_read_only: false,
      max_download_speed: 0, max_upload_speed: 0, max_concurrent_downloads: 2}),
  }));
  await page.evaluate(() => loadAria2SpeedLimit());
  await expect(maxText(page)).toHaveText('7');
  expect(await page.evaluate(() => settingsData.transfer_policy.max_concurrent_executions)).toBe(7);
});

test('an unknown capacity renders a dash instead of a manufactured default', async ({ page }) => {
  await ready(page);
  await page.evaluate(() => { settingsData = {}; updateAria2TopbarBadge({active: 1}); });
  await expect(maxText(page)).toHaveText('—');
});

test('no flat alias is read or written when the operator applies a bandwidth cap', async ({ page }) => {
  await ready(page);
  await useSettings(page, {mode: 'builtin', concurrency: 4, limit: 0});
  let patched = null;
  await page.route('**/api/execution/runtime-limits', route => {
    patched = route.request().postDataJSON();
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({
      ok: true, configured: {max_download_bytes_per_second: 2097152},
      effective: {max_download_bytes_per_second: 2097152}, last_apply_error: null})});
  });
  const applied = await page.evaluate(() => _setAria2Speed(2097152));
  expect(applied).toBe(true);
  expect(patched).toEqual({max_download_bytes_per_second: 2097152});
  const state = await page.evaluate(() => ({
    canonical: settingsData.execution_runtime_limits.max_download_bytes_per_second,
    alias: 'aria2_max_download_limit' in settingsData,
    capacity: settingsData.transfer_policy.max_concurrent_executions,
  }));
  expect(state).toEqual({canonical: 2097152, alias: false, capacity: 4});
});

test('an external aria2 daemon keeps the bandwidth cap read-only', async ({ page }) => {
  await ready(page);
  await useSettings(page, {mode: 'external', concurrency: 4, limit: 500});
  let requests = 0;
  await page.route('**/api/execution/runtime-limits', route => { requests += 1; return route.abort(); });
  const applied = await page.evaluate(() => _setAria2Speed(1048576));
  expect(applied).toBe(false);
  expect(requests).toBe(0);
  expect(await page.evaluate(() => settingsData.execution_runtime_limits.max_download_bytes_per_second)).toBe(500);
  await expect(page.locator('#aria2-speed-badge')).toHaveClass(/external-control/);
});

test('saving Settings adopts the canonical policy and never writes a flat alias or namespace through the broad document', async ({ page }) => {
  await ready(page);
  const policy = {max_concurrent_executions: 5, execution_retry_count: 3, execution_retry_delay_seconds: 60,
    resolution_retry_count: 3, resolution_retry_delay_minutes: 5, execution_poll_interval_seconds: 2,
    provider_poll_interval_seconds: 30, stalled_timeout_hours: 6, resolution_concurrency: 3};
  const captured = {patches: [], put: null};
  const document_ = await page.evaluate(() => JSON.parse(JSON.stringify(settingsData)));

  await page.route('**/api/integrations/*/configuration', route => {
    const id = route.request().url().split('/integrations/')[1].split('/')[0];
    captured.patches.push({id, body: route.request().postDataJSON()});
    const entry = {ok: true, ...(document_.integrations[id] || {enabled: true, priority: 0, options: {}})};
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(entry)});
  });
  await page.route('**/api/transfer-policy', route => {
    const body = route.request().postDataJSON();
    captured.patches.push({id: 'transfer-policy', body});
    Object.assign(policy, body);
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true, last_apply_error: null, ...policy})});
  });
  await page.route('**/api/settings', route => {
    if (route.request().method() !== 'PUT') return route.fallback();
    captured.put = route.request().postDataJSON();
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({
      ...document_, transfer_policy: {...policy}, ok: true})});
  });

  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await page.locator('#view-settings [data-tab="downloads"]').click();
  await page.locator('#view-settings [data-setting="aria2_max_active_downloads"]').fill('5');
  await page.locator('#view-settings button[data-action="save"]:visible').first().click();

  await expect.poll(() => captured.put).not.toBeNull();
  expect(captured.patches.find(p => p.id === 'transfer-policy').body.max_concurrent_executions).toBe(5);
  for (const flat of ['max_concurrent_downloads', 'aria2_max_active_downloads', 'aria2_max_download_limit',
    'aria2_mode', 'aria2_split', 'alldebrid_api_key', 'poll_interval_seconds', 'stuck_download_timeout_hours',
    'upload_fail_retry_count', 'integrations', 'transfer_policy', 'execution_runtime_limits']) {
    expect(Object.keys(captured.put)).not.toContain(flat);
  }
  // The canonical cache carries the accepted value, and the topbar renders it.
  await expect.poll(() => page.evaluate(() => settingsData.transfer_policy.max_concurrent_executions)).toBe(5);
  await page.evaluate(() => updateAria2TopbarBadge({}));
  await expect(maxText(page)).toHaveText('5');
});
