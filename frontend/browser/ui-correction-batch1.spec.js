const { test, expect } = require('@playwright/test');

async function waitForBatch(page) {
  await page.goto('/');
  await page.waitForFunction(() => Boolean(window.DPUICorrectionBatch1));
}

test('Batch 1 removes Quick Add Import and keeps neutral recovery as sole header action', async ({ page }) => {
  await waitForBatch(page);
  const quick = page.locator('.dp-dashboard-quick-add');
  await expect(quick.locator('#btn-import-existing')).toHaveCount(0);
  await expect(quick.locator('#btn-recover-all')).toHaveCount(1);
  await expect(quick.locator('.dp-card-header-actions button')).toHaveCount(1);
  await expect(quick.locator('#btn-recover-all')).toHaveAttribute('title', 'Check transfers for recoverable work');
});

test('authoritative pause state owns both Quick Add and Downloads pause surfaces', async ({ page }) => {
  await waitForBatch(page);
  await page.evaluate(() => {
    settingsData = settingsData || {};
    settingsData.paused = true;
    renderTopbarActions();
  });

  const quickPause = page.locator('.dp-global-pause-center');
  await expect(quickPause).toHaveClass(/is-visible/);
  await expect(quickPause).toContainText('PROCESSING PAUSED');
  await expect(quickPause).toContainText('New downloads can still be added. They will remain queued until processing is resumed.');

  await page.evaluate(() => nav(document.querySelector('[data-view="torrents"]')));
  const shim = page.locator('.dp-downloads-pause-shim');
  await expect(shim).toHaveClass(/is-visible/);
  await expect(shim).toHaveText('Processing paused. Queued and newly added downloads will not start until processing is resumed.');

  await page.evaluate(() => {
    settingsData.paused = false;
    renderTopbarActions();
  });
  await expect(shim).not.toHaveClass(/is-visible/);
});

test('pager keeps three physical slots and current page stays centered at both boundaries', async ({ page }) => {
  await waitForBatch(page);
  await page.evaluate(() => nav(document.querySelector('[data-view="torrents"]')));

  const buttons = page.locator('#torrent-page-btns');
  await page.evaluate(() => renderTorrentPagination(30, 10, 0));
  await expect(buttons.locator('.dp-pager-slot')).toHaveCount(2);
  await expect(buttons.locator('.dp-pager-placeholder')).toHaveCount(1);
  await expect(buttons.locator('.dp-pager-current')).toHaveText('1');

  const firstCenter = await buttons.locator('.dp-pager-current').boundingBox();
  const firstBox = await buttons.boundingBox();

  await page.evaluate(() => renderTorrentPagination(30, 10, 20));
  await expect(buttons.locator('.dp-pager-placeholder')).toHaveCount(1);
  await expect(buttons.locator('.dp-pager-current')).toHaveText('3');
  const lastCenter = await buttons.locator('.dp-pager-current').boundingBox();
  const lastBox = await buttons.boundingBox();

  expect(Math.abs((firstCenter.x + firstCenter.width / 2) - (firstBox.x + firstBox.width / 2))).toBeLessThan(1);
  expect(Math.abs((lastCenter.x + lastCenter.width / 2) - (lastBox.x + lastBox.width / 2))).toBeLessThan(1);
});

test('date menu is an options control and host icons use domain-boundary matching', async ({ page }) => {
  await waitForBatch(page);
  await page.evaluate(() => nav(document.querySelector('[data-view="torrents"]')));
  const trigger = page.locator('.dp-date-menu-trigger');
  await expect(trigger).toHaveCount(1);
  await expect(trigger).toHaveAttribute('aria-haspopup', 'menu');
  await trigger.click();
  await expect(page.getByRole('menuitemradio', { name: 'Friendly' })).toBeVisible();
  await expect(page.getByRole('menuitemradio', { name: 'International' })).toBeVisible();
  await expect(page.getByRole('menuitemradio', { name: 'ISO' })).toBeVisible();
  await expect(page.getByRole('menuitemradio', { name: '24-hour' })).toBeVisible();
  await expect(page.getByRole('menuitemradio', { name: '12-hour' })).toBeVisible();

  const assets = await page.evaluate(() => ({
    exact: DPUICorrectionBatch1.hostAsset('rapidgator.net'),
    subdomain: DPUICorrectionBatch1.hostAsset('cdn.rapidgator.net'),
    boundary: DPUICorrectionBatch1.hostAsset('notrapidgator.net'),
    unknown: DPUICorrectionBatch1.hostAsset('example.invalid'),
  }));
  expect(assets.exact).toBe('/icons/hosts/rapidgator.png');
  expect(assets.subdomain).toBe('/icons/hosts/rapidgator.png');
  expect(assets.boundary).toBe('');
  expect(assets.unknown).toBe('');
});

test('adaptive toast duration scales and focus pause preserves remaining lifetime', async ({ page }) => {
  await waitForBatch(page);
  const durations = await page.evaluate(() => ({
    short: DPToastDuration('Saved', 'info'),
    medium: DPToastDuration('This is a medium notification with enough words to require a little more reading time.', 'info'),
    error: DPToastDuration('Request failed', 'error'),
  }));
  expect(durations.short).toBeGreaterThanOrEqual(3500);
  expect(durations.medium).toBeGreaterThan(durations.short);
  expect(durations.medium).toBeLessThanOrEqual(12000);
  expect(durations.error).toBeGreaterThanOrEqual(4500);

  await page.evaluate(() => toast('A focused toast remains available while the user is reading it.', 'info'));
  const toastNode = page.locator('#toasts .toast').last();
  await toastNode.focus();
  await page.waitForTimeout(3800);
  await expect(toastNode).toBeVisible();
  await toastNode.locator('.dp-toast-close').click();
  await expect(toastNode).toHaveCount(0);
});

for (const height of [760, 980]) {
  test(`Downloads measured capacity can request fewer than fifteen rows at ${height}px`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height });
    await waitForBatch(page);
    await page.evaluate(() => nav(document.querySelector('[data-view="torrents"]')));

    await page.evaluate(() => {
      const body = document.getElementById('t-tbody');
      body.innerHTML = Array.from({ length: 8 }, (_, index) =>
        `<tr data-torrent-id="${index + 1}" style="height:58px"><td colspan="8">row ${index + 1}</td></tr>`
      ).join('');
      torrentPage = 1;
      torrentPageSize = 25;
      DPUICorrectionBatch1.recalculateDownloadsCapacity();
    });
    await page.waitForTimeout(350);
    const size = await page.evaluate(() => torrentPageSize);
    expect(size).toBeGreaterThanOrEqual(1);
    expect(size).toBeLessThan(15);
  });
}
