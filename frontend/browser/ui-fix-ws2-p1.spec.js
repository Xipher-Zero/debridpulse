const { test, expect } = require('@playwright/test');

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({
    status: 200,
    contentType: 'text/css',
    body: '',
  }));
}

function transfer(id, name, status = 'downloading', progress = 10) {
  return {
    id,
    name,
    hash: `fixture-hash-${id}-0123456789abcdef`,
    status,
    progress,
    size_bytes: 1024 * 1024 * id,
    created_at: '2026-09-04T12:00:00Z',
    completed_at: null,
    source: 'direct_link',
    label: null,
    current_provider_id: 'general_http',
    current_provider_name: 'HTTP & HTTPS',
    delivering_provider_id: null,
    delivering_provider_name: null,
    provider_provenance_status: 'known',
    extraction_status: null,
    source_failure_count: 0,
  };
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

async function installDownloadsFixture(page, initialDownloads) {
  let downloads = clone(initialDownloads);
  let bulkFailIds = new Set();
  let singleFailIds = new Set();
  const requests = {bulk: [], singleDelete: []};

  await page.route('**/api/torrents**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();

    if (url.pathname === '/api/torrents/bulk' && method === 'POST') {
      const body = request.postDataJSON() || {};
      const ids = Array.isArray(body.ids) ? body.ids.map(Number) : [];
      requests.bulk.push({ids: [...ids], action: body.action});
      if (body.action === 'delete') {
        const failed = ids.filter(id => bulkFailIds.has(id));
        const succeeded = ids.filter(id => !bulkFailIds.has(id));
        downloads = downloads.filter(item => !succeeded.includes(Number(item.id)));
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ok: succeeded.length, failed: failed.length}),
        });
      }
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ok: ids.length, failed: 0}),
      });
    }

    const itemMatch = url.pathname.match(/^\/api\/torrents\/(\d+)$/);
    if (itemMatch && method === 'DELETE') {
      const id = Number(itemMatch[1]);
      requests.singleDelete.push(id);
      if (singleFailIds.has(id)) {
        return route.fulfill({
          status: 500,
          contentType: 'application/json',
          body: JSON.stringify({detail: 'fixture removal failure'}),
        });
      }
      downloads = downloads.filter(item => Number(item.id) !== id);
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ok: true}),
      });
    }

    if (itemMatch && method === 'GET') {
      const id = Number(itemMatch[1]);
      const item = downloads.find(candidate => Number(candidate.id) === id);
      return route.fulfill({
        status: item ? 200 : 404,
        contentType: 'application/json',
        body: JSON.stringify(item ? clone(item) : {detail: 'not found'}),
      });
    }

    if (url.pathname === '/api/torrents' && method === 'GET') {
      const status = String(url.searchParams.get('status') || '').trim();
      const search = String(url.searchParams.get('search') || '').trim().toLowerCase();
      const limit = Math.max(1, Number(url.searchParams.get('limit')) || 25);
      const offset = Math.max(0, Number(url.searchParams.get('offset')) || 0);
      let filtered = downloads.filter(item => !status || item.status === status);
      if (search) filtered = filtered.filter(item => String(item.name || '').toLowerCase().includes(search));
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({items: clone(filtered.slice(offset, offset + limit)), total: filtered.length}),
      });
    }

    return route.fallback();
  });

  return {
    setDownloads(value) { downloads = clone(value); },
    setBulkFailures(ids) { bulkFailIds = new Set(ids.map(Number)); },
    setSingleFailures(ids) { singleFailIds = new Set(ids.map(Number)); },
    snapshot() { return {downloads: clone(downloads), requests: clone(requests)}; },
  };
}

async function openDownloads(page) {
  await page.locator('#sidebar .nav-item[data-view="torrents"]').click();
  await expect(page.locator('#view-torrents')).toHaveClass(/\bactive\b/);
  await expect(page.locator('#page-title')).toHaveText('Downloads');
  await expect(page.locator('#t-tbody .dp-downloads-detail-row').first()).toBeVisible();
}

const rowCheckbox = (page, id) => page.locator(`.t-chk[data-id="${id}"]`);
const row = (page, id) => page.locator(`.dp-downloads-detail-row[data-torrent-id="${id}"]`);
const accept = page => page.locator('[data-confirm-accept]');
const cancel = page => page.locator('[data-confirm-cancel]');

async function selectedIds(page) {
  return page.evaluate(() => [..._selectedIds].sort((a, b) => a - b));
}

async function installNativeDialogWatch(page) {
  const dialogs = [];
  page.on('dialog', dialog => {
    dialogs.push(`${dialog.type()}:${dialog.message()}`);
    dialog.dismiss().catch(() => {});
  });
  return dialogs;
}

async function refreshDownloads(page) {
  await page.evaluate(() => loadTorrents());
  await expect(page.locator('#view-torrents')).toHaveClass(/\bactive\b/);
}

async function expectModalInsideViewport(page) {
  const box = await page.locator('.dp-settings-confirm-dialog').boundingBox();
  const viewport = page.viewportSize();
  expect(box).not.toBeNull();
  expect(viewport).not.toBeNull();
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.y).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(viewport.width);
  expect(box.y + box.height).toBeLessThanOrEqual(viewport.height);
}

test('WS2-P1 stable selection survives refresh, object replacement, status/progress changes, reorder, and partial disappearance', async ({ page }) => {
  await isolateExternalFonts(page);
  const fixture = await installDownloadsFixture(page, [
    transfer(1, 'Alpha', 'downloading', 10),
    transfer(2, 'Beta', 'paused', 0),
    transfer(3, 'Gamma', 'ready', 0),
  ]);
  await page.goto('/');
  await openDownloads(page);

  await rowCheckbox(page, 1).check();
  await rowCheckbox(page, 2).check();
  expect(await selectedIds(page)).toEqual([1, 2]);
  await expect(page.locator('#bulk-count')).toHaveText('2 Selected');
  expect(await page.locator('#chk-all').evaluate(el => el.indeterminate)).toBe(true);

  fixture.setDownloads([
    transfer(3, 'Gamma', 'ready', 0),
    transfer(1, 'Alpha', 'paused', 37),
    transfer(2, 'Beta', 'downloading', 51),
  ]);
  await refreshDownloads(page);
  await expect(rowCheckbox(page, 1)).toBeChecked();
  await expect(rowCheckbox(page, 2)).toBeChecked();
  await expect(rowCheckbox(page, 3)).not.toBeChecked();

  fixture.setDownloads([
    transfer(2, 'Beta', 'paused', 0),
    transfer(3, 'Gamma', 'ready', 0),
  ]);
  await refreshDownloads(page);
  await expect(row(page, 1)).toHaveCount(0);
  await expect(rowCheckbox(page, 2)).toBeChecked();
  expect(await selectedIds(page)).toEqual([2]);
  await expect(page.locator('#bulk-count')).toHaveText('1 Selected');
});

test('WS2-P1 select-all and individual changes mutate the stable-ID owner, while filter and search reset scope', async ({ page }) => {
  await isolateExternalFonts(page);
  await installDownloadsFixture(page, [
    transfer(1, 'Alpha'),
    transfer(2, 'Beta', 'paused', 0),
    transfer(3, 'Gamma'),
  ]);
  await page.goto('/');
  await openDownloads(page);

  await page.locator('#chk-all').check();
  expect(await selectedIds(page)).toEqual([1, 2, 3]);
  await expect(page.locator('#chk-all')).toBeChecked();
  await rowCheckbox(page, 2).uncheck();
  expect(await selectedIds(page)).toEqual([1, 3]);
  expect(await page.locator('#chk-all').evaluate(el => el.indeterminate)).toBe(true);

  await page.locator('#view-torrents .ftab[data-dp-status="paused"]').click();
  expect(await selectedIds(page)).toEqual([]);
  await expect(rowCheckbox(page, 2)).not.toBeChecked();

  await page.locator('#view-torrents .ftab[data-dp-status=""]').click();
  await rowCheckbox(page, 1).check();
  await page.locator('#torrent-search').fill('Beta');
  await expect.poll(() => selectedIds(page)).toEqual([]);
  await expect(rowCheckbox(page, 2)).toBeVisible();
});

test('WS2-P1 bulk Remove uses the canonical app modal, restores focus, supports Escape/themes, and never opens a native dialog', async ({ page }) => {
  await isolateExternalFonts(page);
  const fixture = await installDownloadsFixture(page, [transfer(1, 'Alpha')]);
  const dialogs = await installNativeDialogWatch(page);
  await page.setViewportSize({width: 1280, height: 800});
  await page.goto('/');
  await openDownloads(page);
  await rowCheckbox(page, 1).check();
  const remove = page.locator('.dp-downloads-bulk-action--delete');

  await remove.click();
  await expect(page.locator('.dp-settings-confirm-overlay')).toBeVisible();
  await expect(accept(page)).toHaveClass(/\bbtn-danger\b/);
  await expect(accept(page)).toHaveText('Remove');
  await expect(cancel(page)).toHaveText('Cancel');
  await expectModalInsideViewport(page);
  await cancel(page).click();
  await expect(page.locator('.dp-settings-confirm-overlay')).toHaveCount(0);
  await expect(remove).toBeFocused();
  expect(fixture.snapshot().requests.bulk).toEqual([]);

  await remove.click();
  await page.keyboard.press('Escape');
  await expect(page.locator('.dp-settings-confirm-overlay')).toHaveCount(0);
  await expect(remove).toBeFocused();

  await page.locator('#theme-toggle').click();
  expect(await page.evaluate(() => document.body.classList.contains('light'))).toBe(true);
  await page.setViewportSize({width: 1440, height: 900});
  await remove.click();
  await expectModalInsideViewport(page);
  await cancel(page).click();
  expect(dialogs).toEqual([]);
});

test('WS2-P1 bulk Remove captures its stable targets once, rejects double-confirm, and does not leak targets between repeated removals', async ({ page }) => {
  await isolateExternalFonts(page);
  const fixture = await installDownloadsFixture(page, [transfer(1, 'Alpha'), transfer(2, 'Beta')]);
  const dialogs = await installNativeDialogWatch(page);
  await page.goto('/');
  await openDownloads(page);

  await rowCheckbox(page, 1).check();
  await page.locator('.dp-downloads-bulk-action--delete').click();
  await expect(page.locator('.dp-settings-confirm-overlay')).toBeVisible();
  await page.evaluate(() => {
    const alpha = document.querySelector('.t-chk[data-id="1"]');
    const beta = document.querySelector('.t-chk[data-id="2"]');
    alpha.checked = false;
    onCheckboxChange(alpha);
    beta.checked = true;
    onCheckboxChange(beta);
  });
  expect(await selectedIds(page)).toEqual([2]);

  await accept(page).evaluate(button => {
    button.click();
    button.click();
  });
  await expect.poll(() => fixture.snapshot().requests.bulk.length).toBe(1);
  expect(fixture.snapshot().requests.bulk[0]).toEqual({ids: [1], action: 'delete'});
  await expect(row(page, 1)).toHaveCount(0);
  await expect(rowCheckbox(page, 2)).toBeChecked();

  await page.locator('.dp-downloads-bulk-action--delete').click();
  await accept(page).click();
  await expect.poll(() => fixture.snapshot().requests.bulk.length).toBe(2);
  expect(fixture.snapshot().requests.bulk[1]).toEqual({ids: [2], action: 'delete'});
  await expect(row(page, 2)).toHaveCount(0);
  expect(await selectedIds(page)).toEqual([]);
  expect(dialogs).toEqual([]);
});

test('WS2-P1 failed bulk Remove preserves the failed stable selection for retry', async ({ page }) => {
  await isolateExternalFonts(page);
  const fixture = await installDownloadsFixture(page, [transfer(1, 'Alpha')]);
  fixture.setBulkFailures([1]);
  await page.goto('/');
  await openDownloads(page);

  await rowCheckbox(page, 1).check();
  await page.locator('.dp-downloads-bulk-action--delete').click();
  await accept(page).click();
  await expect.poll(() => fixture.snapshot().requests.bulk.length).toBe(1);
  await expect(rowCheckbox(page, 1)).toBeChecked();
  expect(await selectedIds(page)).toEqual([1]);
  await expect(page.locator('#bulk-count')).toHaveText('1 Selected');
});

test('WS2-P1 single-row Remove uses the same canonical modal and existing DELETE only after confirmation', async ({ page }) => {
  await isolateExternalFonts(page);
  const fixture = await installDownloadsFixture(page, [transfer(1, 'Alpha')]);
  const dialogs = await installNativeDialogWatch(page);
  await page.goto('/');
  await openDownloads(page);

  const remove = row(page, 1).locator('button.btn-danger');
  await remove.click();
  await expect(page.locator('.dp-settings-confirm-overlay')).toBeVisible();
  await cancel(page).click();
  expect(fixture.snapshot().requests.singleDelete).toEqual([]);
  await expect(remove).toBeFocused();

  await remove.click();
  await accept(page).click();
  await expect.poll(() => fixture.snapshot().requests.singleDelete).toEqual([1]);
  await expect(row(page, 1)).toHaveCount(0);
  expect(dialogs).toEqual([]);
});
