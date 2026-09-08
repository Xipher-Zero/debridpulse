const { test, expect } = require('@playwright/test');

async function ready(page) {
  await page.goto('/');
  await page.waitForFunction(() => Boolean(window.DPDownloadsPresentation));
}

function torrentPayload(withPresentation = true) {
  const item = {
    id: 991,
    name: 'Pause truth mismatch',
    status: withPresentation ? 'downloading' : 'paused',
    progress: 37,
    size_bytes: 1024,
    created_at: '2026-09-08 07:00:00',
    source: 'manual',
  };
  if (withPresentation) {
    item.presentation_status = 'paused';
    item.presentation_label = 'Paused';
    item.presentation_badge_status = 'paused';
  }
  return { items: [item], total: 1, limit: 25, offset: 0 };
}

async function routeTorrents(page, payload) {
  await page.route('**/api/torrents?*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(payload),
  }));
}

test('canonical paused presentation controls Downloads and Recent Activity', async ({ page }) => {
  await routeTorrents(page, torrentPayload(true));
  await ready(page);

  await page.evaluate(async () => {
    nav(document.querySelector('[data-view="torrents"]'));
    await loadTorrents();
  });
  const downloads = page.locator('#t-tbody tr[data-torrent-id="991"]');
  await expect(downloads.locator('[data-role="transfer-status"]')).toContainText('Paused');
  await expect(downloads.locator('button[data-default-label="Resume"]')).toHaveCount(1);
  await expect(downloads.locator('button[data-default-label="Pause"]')).toHaveCount(0);

  await page.evaluate(async () => {
    nav(document.querySelector('[data-view="dashboard"]'));
    await loadRecent();
  });
  const recent = page.locator('#dash-tbody tr[data-torrent-id="991"]');
  await expect(recent).toHaveAttribute('data-status', 'downloading');
  await expect(recent).toHaveAttribute('data-presentation-status', 'paused');
  await expect(recent.locator('[data-role="transfer-status"]')).toContainText('Paused');
  await expect(recent.locator('button[data-default-label="Resume"]')).toHaveCount(1);
  await expect(recent.locator('button[data-default-label="Pause"]')).toHaveCount(0);
});

test('legacy paused status remains the fallback when presentation_status is absent', async ({ page }) => {
  await routeTorrents(page, torrentPayload(false));
  await ready(page);
  await page.evaluate(async () => {
    nav(document.querySelector('[data-view="torrents"]'));
    await loadTorrents();
  });
  const downloads = page.locator('#t-tbody tr[data-torrent-id="991"]');
  await expect(downloads.locator('[data-role="transfer-status"]')).toContainText('Paused');
  await expect(downloads.locator('button[data-default-label="Resume"]')).toHaveCount(1);
  await expect(downloads.locator('button[data-default-label="Pause"]')).toHaveCount(0);
});
