const { test, expect } = require('@playwright/test');

/* DP 1.0.14: Network Sources tuning also reaches existing downloads.
 *
 * Connections per Server, Segments per File and Minimum Split Size: while
 * DebridPulse's own downloads are running, saving one asks first (they will
 * briefly reconnect); Cancel saves nothing and the field returns. What the
 * save did to existing downloads is reported apart from the save, from what
 * the server observed per download. Other fields never ask. The running count
 * comes from /api/aria2/downloads (DebridPulse's own downloads only); these
 * cases substitute that listing and the per-download outcome the save
 * returns -- the save itself is the real server's. Every case restores the
 * options it changed. */

const NOTICE = 'Changes also apply to existing downloads. Active downloads may briefly reconnect; their progress is preserved.';

async function openDownloads(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.goto('/');
  await page.waitForFunction(() => typeof nav === 'function');
  await page.evaluate(() => nav(document.querySelector('[data-view="settings"]')));
  await page.locator('#view-settings [data-tab="downloads"]').click();
  const card = page.locator('[data-executor-tuning="direct"]');
  const disclosure = card.locator('.dp-settings-disclosure').first();
  if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.evaluate(button => button.click());
  await expect(card.locator('[data-setting="aria2_split"]')).toBeVisible();
  return card;
}

async function aria2(page) {
  const response = await page.request.get('/api/integrations/aria2/configuration');
  expect(response.ok()).toBeTruthy();
  return (await response.json()).options;
}

/* The listing the confirmation reads: ``active`` running downloads, or a
 * failure when ``active`` is null. */
async function running(page, active) {
  const seen = [];
  await page.route('**/api/aria2/downloads', route => {
    seen.push(route.request().method());
    return active === null
      ? route.fulfill({status: 502, contentType: 'application/json', body: '{"detail":"aria2 unavailable"}'})
      : route.fulfill({status: 200, contentType: 'application/json',
                       body: JSON.stringify({ok: true, items: [], groups: {active: [], waiting: [], stopped: []},
                                             summary: {active, waiting: 0, stopped: 0, download_speed: 0, remaining_length: 0}})});
  });
  return seen;
}

/* The real save, answered with ``outcome`` as its per-download result. */
async function savedWith(page, outcome) {
  const sent = [];
  await page.route('**/api/integrations/aria2/configuration', async route => {
    if (route.request().method() !== 'PATCH') return route.continue();
    sent.push(route.request().postDataJSON());
    const response = await route.fetch();
    const body = await response.json();
    return route.fulfill({response, json: outcome === undefined ? body : {...body, job_tuning: outcome}});
  });
  return sent;
}

let restore = null;
test.beforeEach(async ({page}) => {
  const before = await aria2(page);
  restore = {split: before.split, max_connection_per_server: before.max_connection_per_server,
             min_split_size: before.min_split_size, disk_cache: before.disk_cache};
});
test.afterEach(async ({page}) => {
  await page.unroute('**/api/integrations/aria2/configuration');
  await page.request.patch('/api/integrations/aria2/configuration', {data: {options: restore}});
});

test('while downloads run, a tuning change asks first; Cancel saves nothing and the field returns', async ({page}) => {
  const card = await openDownloads(page);
  const listed = await running(page, 2);
  const sent = await savedWith(page);
  const split = card.locator('[data-setting="aria2_split"]');
  const before = await split.inputValue();
  await split.fill(String(Number(before) === 7 ? 6 : 7));
  await split.blur();

  const dialog = page.getByRole('alertdialog');
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('Apply to existing downloads?');
  await expect(dialog.locator('.dp-modal-message')).toHaveText(`${NOTICE} 2 running downloads will briefly reconnect.`);
  await dialog.getByRole('button', {name: 'Cancel'}).click();

  await expect(dialog).toBeHidden();
  await expect(split).toHaveValue(before);
  expect(listed).toEqual(['GET']);
  expect(sent).toEqual([]);
  await expect(page.locator('.toast.error')).toHaveCount(0);
  expect((await aria2(page)).split).toBe(restore.split);
});

test('confirmed, the save is made and a partial outcome is told as one', async ({page}) => {
  const card = await openDownloads(page);
  await running(page, 1);
  const sent = await savedWith(page, {daemon: 'running', selected: 4, applied: 1, unchanged: 0, refused: 1,
                                       uncertain: 1, interrupted: 0, gone: 0, unavailable: 0, not_reached: 0,
                                       superseded: 1});
  const connections = card.locator('[data-setting="aria2_max_connection_per_server"]');
  const value = Number(await connections.inputValue()) === 5 ? '6' : '5';
  await connections.fill(value);
  await connections.blur();
  const dialog = page.getByRole('alertdialog');
  await expect(dialog.locator('.dp-modal-message')).toHaveText(`${NOTICE} 1 running download will briefly reconnect.`);
  await dialog.getByRole('button', {name: 'Save and Apply'}).click();

  await expect(page.locator('.toast').last()).toHaveText(
    'Saved. Applied to 1 of 4 existing downloads. aria2 refused the change for 1; it keeps its earlier values. '
    + '1 could not be confirmed and may still use the earlier values. '
    + '1 was left to a newer change of these settings.');
  // A partial outcome is never the green "applied" message.
  await expect(page.locator('.toast.success')).toHaveCount(0);
  expect(sent).toEqual([{options: {max_connection_per_server: Number(value)}}]);
  await expect(connections).toHaveValue(value);
  expect((await aria2(page)).max_connection_per_server).toBe(Number(value));
});

test('with nothing running there is no question, and a full outcome is confirmed', async ({page}) => {
  const card = await openDownloads(page);
  await running(page, 0);
  const sent = await savedWith(page, {daemon: 'running', selected: 2, applied: 2, unchanged: 0, refused: 0,
                                       uncertain: 0, interrupted: 0, gone: 0, unavailable: 0, not_reached: 0});
  const minimum = card.locator('[data-setting="aria2_min_split_size"]');
  const value = (await minimum.inputValue()) === '32M' ? '64M' : '32M';
  await minimum.fill(value);
  await minimum.blur();
  await expect(page.locator('.toast.success').last()).toHaveText('Applied to 2 existing downloads.');
  await expect(page.getByRole('alertdialog')).toHaveCount(0);
  expect(sent).toEqual([{options: {min_split_size: value}}]);
});

test('when the save cannot check existing downloads in time, it says so rather than reporting none', async ({page}) => {
  const card = await openDownloads(page);
  await running(page, 0);
  await savedWith(page, {daemon: 'running', selection: 'unavailable'});
  const minimum = card.locator('[data-setting="aria2_min_split_size"]');
  const value = (await minimum.inputValue()) === '32M' ? '64M' : '32M';
  await minimum.fill(value);
  await minimum.blur();
  await expect(page.locator('.toast.warning, .toast.warn').last()).toHaveText(
    'Saved. Existing downloads could not be checked in time, so they may still use the earlier values.');
  await expect(page.locator('.toast.success')).toHaveCount(0);
});

test('when running downloads cannot be read nothing is assumed idle; other fields never ask', async ({page}) => {
  const card = await openDownloads(page);
  const listed = await running(page, null);
  const sent = await savedWith(page);
  // Disk Cache is not per-download tuning: saved without a question or a listing.
  const cache = card.locator('[data-setting="aria2_disk_cache"]');
  const cacheValue = (await cache.inputValue()) === '32M' ? '48M' : '32M';
  await cache.fill(cacheValue);
  await cache.blur();
  await expect.poll(() => sent.length).toBe(1);
  expect(listed).toEqual([]);
  await expect(page.getByRole('alertdialog')).toHaveCount(0);

  const split = card.locator('[data-setting="aria2_split"]');
  const before = await split.inputValue();
  await split.fill(String(Number(before) === 7 ? 6 : 7));
  await split.blur();
  const dialog = page.getByRole('alertdialog');
  await expect(dialog.locator('.dp-modal-message')).toHaveText(
    `${NOTICE} Downloads that are running now will briefly reconnect.`);
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(split).toHaveValue(before);
  expect(sent).toHaveLength(1);
});

test('at 390px the question is reached and answered from the keyboard, in light and dark', async ({page}) => {
  await page.setViewportSize({width: 390, height: 844});
  const card = await openDownloads(page);
  await running(page, 3);
  const sent = await savedWith(page, {daemon: 'running', selected: 3, applied: 3, unchanged: 0, refused: 0,
                                       uncertain: 0, interrupted: 0, gone: 0, unavailable: 0, not_reached: 0});
  const split = card.locator('[data-setting="aria2_split"]');
  const value = Number(await split.inputValue()) === 9 ? '8' : '9';
  for (const pass of ['first', 'second']) {
    await split.focus();
    await split.fill(pass === 'first' ? value : String(Number(value) + 1));
    await page.keyboard.press('Tab');
    const dialog = page.getByRole('alertdialog');
    await expect(dialog).toBeVisible();
    // Fits the narrow viewport, focus inside the dialog.
    const box = await dialog.boundingBox();
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(390);
    expect(await dialog.evaluate(node => node.contains(document.activeElement))).toBe(true);
    await page.screenshot({path: test.info().outputPath(`live-tuning-390-${pass}.png`)});
    if (pass === 'first') {
      await page.keyboard.press('Escape');
      await expect(dialog).toBeHidden();
      // The other theme for the second pass.
      await page.locator('#theme-toggle').click();
      continue;
    }
    await dialog.getByRole('button', {name: 'Save and Apply'}).focus();
    await page.keyboard.press('Enter');
    await expect(page.locator('.toast.success').last()).toHaveText('Applied to 3 existing downloads.');
  }
  expect(sent).toEqual([{options: {split: Number(value) + 1}}]);
});
