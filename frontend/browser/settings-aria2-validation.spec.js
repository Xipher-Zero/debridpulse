const { test, expect } = require('@playwright/test');

/* DP 1.0.13 aria2 configuration validation -- the Network Sources tuning
 * fields hold exactly what the managed aria2 accepts. A size field refuses a
 * malformed value at once, in its own words, without a request; Connections
 * per Server stops at aria2's 16; and whatever reaches the server is refused
 * there in plain words, the field returning to what the server holds.
 * Every case restores the aria2 options it changed. */

async function openDownloads(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.goto('/');
  await page.waitForFunction(() => typeof nav === 'function');
  await page.evaluate(() => nav(document.querySelector('[data-view="settings"]')));
  await page.locator('#view-settings [data-tab="downloads"]').click();
  const card = page.locator('[data-executor-tuning="direct"]');
  const disclosure = card.locator('.dp-settings-disclosure').first();
  if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.evaluate(button => button.click());
  await expect(card.locator('[data-setting="aria2_disk_cache"]')).toBeVisible();
  return card;
}

async function aria2(page) {
  const response = await page.request.get('/api/integrations/aria2/configuration');
  expect(response.ok()).toBeTruthy();
  return (await response.json()).options;
}

let restore = null;
test.beforeEach(async ({page}) => {
  const before = await aria2(page);
  restore = {max_connection_per_server: before.max_connection_per_server, min_split_size: before.min_split_size,
             disk_cache: before.disk_cache, lowest_speed_limit: before.lowest_speed_limit};
});
test.afterEach(async ({page}) => {
  await page.request.patch('/api/integrations/aria2/configuration', {data: {options: restore}});
});

test('a malformed size is refused at once in the field\'s words, with no request', async ({page}) => {
  const card = await openDownloads(page);
  const writes = [];
  page.on('request', request => {
    if (request.url().includes('/api/integrations/aria2/configuration') && request.method() === 'PATCH') writes.push(request);
  });
  for (const [key, text] of [
    ['aria2_disk_cache', 'Disk Cache is a whole number with an optional K or M suffix, such as 0, 512K or 64M.'],
    ['aria2_min_split_size', 'Minimum Split Size is a whole number with an optional K or M suffix, from 1M to 1024M.'],
    ['aria2_lowest_speed_limit', 'Lowest Speed Limit is a whole number with an optional K or M suffix, such as 0, 512K or 2M.'],
  ]) {
    const field = card.locator(`[data-setting="${key}"]`);
    const before = await field.inputValue();
    await field.fill('18EGGS');
    await field.blur();
    await expect(page.locator('.toast.error').last()).toHaveText(text);
    await expect(field).toHaveValue(before);
  }
  expect(writes).toHaveLength(0);
  expect((await aria2(page)).disk_cache).toBe(restore.disk_cache);
});

test('Connections per Server stops at 16, and a server refusal is shown in plain words', async ({page}) => {
  const card = await openDownloads(page);
  const connections = card.locator('[data-setting="aria2_max_connection_per_server"]');
  await expect(connections).toHaveAttribute('max', '16');
  const before = await connections.inputValue();
  // Above the control's own bound (the server's): refused in the field's words
  // before any request (Settings Batch 1, Item 3); the server refuses the same
  // value on its own (backend/tests/test_aria2_configuration_validation.py).
  const sent = [];
  const watch = request => { if (request.url().includes('/api/integrations/aria2/configuration')
    && request.method() === 'PATCH') sent.push(request.postData()); };
  page.on('request', watch);
  await connections.fill('20');
  await connections.blur();
  await expect(page.locator('.toast.error').last()).toHaveText('Connections per Server can be at most 16.');
  await expect(connections).toHaveValue(before);
  expect(sent).toEqual([]);
  page.off('request', watch);
  // A well-formed size outside aria2's range passes the field and is refused
  // by the server, in its words.
  const split = card.locator('[data-setting="aria2_min_split_size"]');
  const splitBefore = await split.inputValue();
  const outOfRange = page.waitForResponse(response => response.url().includes('/api/integrations/aria2/configuration')
    && response.request().method() === 'PATCH');
  await split.fill('2048M');
  await split.blur();
  expect((await outOfRange).status()).toBe(400);
  await expect(page.locator('.toast.error').last())
    .toHaveText('Minimum split size must be a whole number with an optional K or M suffix (for example 64M), from 1M to 1024M');
  await expect(split).toHaveValue(splitBefore);
  expect((await aria2(page)).max_connection_per_server).toBe(restore.max_connection_per_server);
});

test('a valid size saves as before and reports what took effect', async ({page}) => {
  const card = await openDownloads(page);
  const cache = card.locator('[data-setting="aria2_disk_cache"]');
  const target = restore.disk_cache === '32M' ? '48M' : '32M';
  const saved = page.waitForResponse(response => response.url().includes('/api/integrations/aria2/configuration')
    && response.request().method() === 'PATCH');
  await cache.fill(target);
  await cache.blur();
  const response = await saved;
  expect(response.status()).toBe(200);
  const body = await response.json();
  expect(body.options.disk_cache).toBe(target);
  // The daemon's own outcome rides with the save: never more than took effect.
  expect(body.runtime).toBeTruthy();
  expect(typeof body.runtime.running).toBe('boolean');
  await expect(cache).toHaveValue(target);
  expect((await aria2(page)).disk_cache).toBe(target);
});
