const { test, expect } = require('@playwright/test');

// Scheduler capacity is universal transfer policy. The topbar denominator is
// rendered by app.js's updateRuntimeStatusBadge() from
// settingsData.transfer_policy.max_concurrent_executions and nothing else: no
// wrapper runtime, no flat alias, and no value reported by the aria2 daemon.

async function ready(page) {
  await page.setViewportSize({width: 1280, height: 720});
  await page.goto('/');
  await page.waitForFunction(() => Boolean(window.DPProcessingPresentation && typeof updateRuntimeStatusBadge === 'function'));
}

async function useSettings(page, {concurrency = 7, limit = 0} = {}) {
  await page.evaluate(({concurrency, limit}) => {
    settingsData = {
      integrations: {aria2: {options: {}}},
      transfer_policy: {max_concurrent_executions: concurrency},
      execution_runtime_limits: {max_download_bytes_per_second: limit},
      paused: false,
    };
    updateRuntimeStatusBadge({active: 3, liveBps: 1048576});
  }, {concurrency, limit});
}

const maxText = page => page.locator('#runtime-badge-max');

test('the retired topbar wrapper runtime is gone and updateRuntimeStatusBadge is the app.js function itself', async ({ page }) => {
  await ready(page);
  await expect(page.locator('script[src*="ui-topbar-concurrency"]')).toHaveCount(0);
  expect((await page.request.get('/ui-topbar-concurrency.js')).status()).not.toBe(200);
  const shape = await page.evaluate(() => ({
    wrapperGlobal: typeof window.DPTopbarConcurrency,
    source: Function.prototype.toString.call(window.updateRuntimeStatusBadge).slice(0, 44),
    sameBinding: window.updateRuntimeStatusBadge === updateRuntimeStatusBadge,
  }));
  expect(shape.wrapperGlobal).toBe('undefined');
  expect(shape.source).toContain('function updateRuntimeStatusBadge(patch)');
  expect(shape.sameBinding).toBe(true);
});

test('denominator is transfer_policy.max_concurrent_executions', async ({ page }) => {
    await ready(page);
    await useSettings(page, {concurrency: 7});
    await expect(page.locator('#runtime-badge-active')).toHaveText('3');
    await expect(maxText(page)).toHaveText('7');

    const visible = await page.evaluate(() => {
      const max = document.getElementById('runtime-badge-max');
      const badge = document.getElementById('runtime-speed-badge');
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
      const badge = document.getElementById('runtime-speed-badge');
      badge.style.display = 'none';
      return getComputedStyle(badge).display;
    });
    expect(hidden).toBe('none');
});

test('a stale or native maximum in a badge patch cannot replace scheduler capacity', async ({ page }) => {
  await ready(page);
  await useSettings(page, {concurrency: 7});
  await page.evaluate(() => updateRuntimeStatusBadge({maxDl: 3}));           // the retired stale-fallback shape
  await expect(maxText(page)).toHaveText('7');
  await page.evaluate(() => updateRuntimeStatusBadge({maxDl: 99}));          // a native aria2 value
  await expect(maxText(page)).toHaveText('7');
});

test('the neutral runtime-status response cannot override universal scheduler capacity', async ({ page }) => {
  // DP 1.0.13 work item G: the indicator no longer polls an executor at all.
  // The denominator still comes from canonical transfer policy, so even a
  // runtime-status payload claiming otherwise cannot replace it.
  await ready(page);
  await useSettings(page, {concurrency: 7});
  await page.route('**/api/execution/runtime-status', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ok: true, download_bytes_per_second: 0, active_execution_slots: 2,
                          max_download_bytes_per_second: 0, max_concurrent_downloads: 2}),
  }));
  await page.evaluate(() => loadRuntimeStatus());
  await expect(maxText(page)).toHaveText('7');
  expect(await page.evaluate(() => settingsData.transfer_policy.max_concurrent_executions)).toBe(7);
});

test('an unknown capacity renders a dash instead of a manufactured default', async ({ page }) => {
  await ready(page);
  await page.evaluate(() => { settingsData = {}; updateRuntimeStatusBadge({active: 1}); });
  await expect(maxText(page)).toHaveText('—');
});

test('no flat alias is read or written when the operator applies a bandwidth cap', async ({ page }) => {
  await ready(page);
  await useSettings(page, {concurrency: 4, limit: 0});
  let patched = null;
  await page.route('**/api/execution/runtime-limits', route => {
    patched = route.request().postDataJSON();
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({
      ok: true, configured: {max_download_bytes_per_second: 2097152},
      effective: {max_download_bytes_per_second: 2097152}, last_apply_error: null})});
  });
  const applied = await page.evaluate(() => _setDownloadSpeedCap(2097152));
  expect(applied).toBe(true);
  expect(patched).toEqual({max_download_bytes_per_second: 2097152});
  const state = await page.evaluate(() => ({
    canonical: settingsData.execution_runtime_limits.max_download_bytes_per_second,
    alias: 'aria2_max_download_limit' in settingsData,
    capacity: settingsData.transfer_policy.max_concurrent_executions,
  }));
  expect(state).toEqual({canonical: 2097152, alias: false, capacity: 4});
});

test('committing a Downloads field adopts the canonical policy and never writes a flat alias or namespace through the broad document', async ({ page }) => {
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
  // One aria2: the Downloads panel offers no topology control, visible or hidden.
  const downloads = page.locator('#view-settings [data-panel="downloads"]');
  for (const retired of ['aria2_mode', 'aria2_url', 'aria2_secret', 'aria2_download_path']) {
    await expect(downloads.locator(`[data-setting="${retired}"]`)).toHaveCount(0);
  }
  await expect(downloads.locator('[data-download-path-mode], [data-builtin-only-tuning], [data-clear-secret^="aria2"]')).toHaveCount(0);
  // DP 1.0.13: Downloads is a field-boundary persistence surface. There is no
  // Apply on it at all -- each control commits itself, through its own
  // canonical namespace, carrying only the field that changed.
  await expect(page.locator('#view-settings button[data-action="save"]')).toBeHidden();
  await expect(page.locator('#view-settings .dp-settings-save-hint')).toBeHidden();

  await page.locator('#view-settings [data-setting="aria2_max_active_downloads"]').fill('5');
  await page.locator('#view-settings [data-setting="aria2_max_active_downloads"]').blur();
  await expect.poll(() => captured.patches.filter(p => p.id === 'transfer-policy').length).toBe(1);

  // Advanced direct-transfer tuning lives in the collapsed Network Sources
  // child card of the Executor Tuning master card.
  await page.locator('#view-settings [data-executor-tuning="direct"] .dp-settings-disclosure').click();
  await page.locator('#view-settings [data-setting="aria2_split"]').fill('8');
  await page.locator('#view-settings [data-setting="aria2_split"]').blur();
  await expect.poll(() => captured.patches.filter(p => p.id === 'aria2').length).toBe(1);

  // Each scoped write carries exactly ONE field of exactly one namespace.
  const policyPatch = captured.patches.find(p => p.id === 'transfer-policy').body;
  expect(Object.keys(policyPatch)).toEqual(['max_concurrent_executions']);
  expect(policyPatch.max_concurrent_executions).toBe(5);
  const aria2 = captured.patches.find(p => p.id === 'aria2').body;
  expect(Object.keys(aria2)).toEqual(['options']);
  expect(Object.keys(aria2.options)).toEqual(['split']);
  expect(aria2.options.split).toBe(8);

  // A settings-document control of the same panel commits through its own
  // scope: read canonical truth, write it back, with no flat alias and no
  // canonical namespace inside the broad document.
  await page.locator('#view-settings [data-setting="min_free_disk_gb"]').fill('2');
  await page.locator('#view-settings [data-setting="min_free_disk_gb"]').blur();
  await expect.poll(() => captured.put).not.toBeNull();
  expect(captured.put.min_free_disk_gb).toBe(2);
  for (const flat of ['max_concurrent_downloads', 'aria2_max_active_downloads', 'aria2_max_download_limit',
    'aria2_split', 'alldebrid_api_key', 'poll_interval_seconds', 'stuck_download_timeout_hours',
    'upload_fail_retry_count', 'integrations', 'transfer_policy', 'execution_runtime_limits']) {
    expect(Object.keys(captured.put)).not.toContain(flat);
  }
  // The canonical cache carries the accepted value, and the topbar renders it.
  await expect.poll(() => page.evaluate(() => settingsData.transfer_policy.max_concurrent_executions)).toBe(5);
  await page.evaluate(() => updateRuntimeStatusBadge({}));
  await expect(maxText(page)).toHaveText('5');
});
