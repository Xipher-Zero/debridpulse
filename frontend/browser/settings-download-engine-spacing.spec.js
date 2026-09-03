const { test, expect } = require('@playwright/test');

async function openDownloadsSettings(page) {
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  const downloads = page.locator('.dp-settings-tabs .stab[data-tab="downloads"]');
  await downloads.click();
  await expect(downloads).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('.dp-settings-download-engine-row')).toBeVisible();
}

async function expectCanonicalSpacing(page) {
  const row = page.locator('.dp-settings-download-engine-row');
  await expect.poll(() => row.evaluate(node => getComputedStyle(node).marginTop)).toBe('0px');
}

test('Download Engine spacing is canonical in dark, light, and narrower responsive Settings', async ({ page }) => {
  const correctionRequests = [];
  page.on('request', request => {
    if (request.url().includes('ui-settings-download-engine-spacing.css')) correctionRequests.push(request.url());
  });

  await openDownloadsSettings(page);
  await expectCanonicalSpacing(page);

  await page.locator('#theme-toggle').click();
  await expect.poll(() => page.evaluate(() => document.body.classList.contains('light'))).toBeTruthy();
  await expectCanonicalSpacing(page);

  await page.setViewportSize({ width: 900, height: 900 });
  await expect(page.locator('.dp-settings-download-engine-row')).toBeVisible();
  await expectCanonicalSpacing(page);

  expect(correctionRequests).toEqual([]);
});
