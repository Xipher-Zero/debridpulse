const { test, expect } = require('@playwright/test');

test('topbar visibly renders canonical scheduler concurrency without stale first-paint masking', async ({ page }) => {
  await page.setViewportSize({width: 1280, height: 720});
  await page.goto('/');
  await page.waitForFunction(() => !!window.DPTopbarConcurrency);

  const runtimeScript = page.locator('script[src="/ui-topbar-concurrency.js?v=1"]');
  await expect(runtimeScript).toHaveCount(1);

  await page.evaluate(() => {
    settingsData.transfer_policy = {
      ...(settingsData.transfer_policy || {}),
      max_concurrent_executions: 7,
    };
    settingsData.max_concurrent_downloads = 0;
    settingsData.aria2_max_active_downloads = 0;

    updateAria2TopbarBadge({
      active: 3,
      maxDl: 0,
      liveBps: 1048576,
    });
  });

  await expect(page.locator('#aria2-badge-active')).toHaveText('3');
  await expect(page.locator('#aria2-badge-max')).toHaveText('7');

  const visibleProjection = await page.evaluate(() => {
    const max = document.getElementById('aria2-badge-max');
    const badge = document.getElementById('aria2-speed-badge');
    return {
      text: max?.textContent,
      fontSize: max ? window.getComputedStyle(max).fontSize : null,
      pseudoContent: max ? window.getComputedStyle(max, '::after').content : null,
      badgeDisplay: badge ? window.getComputedStyle(badge).display : null,
    };
  });
  expect(visibleProjection.text).toBe('7');
  expect(parseFloat(visibleProjection.fontSize || '0')).toBeGreaterThan(0);
  expect(visibleProjection.pseudoContent).not.toBe('"0"');
  expect(visibleProjection.badgeDisplay).not.toBe('none');

  // The canonical runtime must retain display authority. The retired first-paint
  // stylesheet used !important and could keep this badge visible after app.js
  // deliberately hid it for a stopped/unavailable built-in engine.
  const hiddenDisplay = await page.evaluate(() => {
    const badge = document.getElementById('aria2-speed-badge');
    badge.style.display = 'none';
    return window.getComputedStyle(badge).display;
  });
  expect(hiddenDisplay).toBe('none');

  await page.evaluate(() => {
    document.getElementById('aria2-speed-badge').style.display = 'flex';
  });

  // Reproduce the inherited fallback that used to manufacture a positive 3
  // after seeing zero flat aliases. It must not displace canonical policy 7.
  await page.evaluate(() => {
    settingsData.max_concurrent_downloads = 0;
    settingsData.aria2_max_active_downloads = 0;
    updateAria2TopbarBadge({maxDl: 3});
  });
  await expect(page.locator('#aria2-badge-max')).toHaveText('7');

  // An operator-applied positive value is accepted when the cached aliases have
  // already been updated by that mutation path, and is synchronized canonically.
  const projected = await page.evaluate(() => {
    settingsData.max_concurrent_downloads = 5;
    settingsData.aria2_max_active_downloads = 5;
    updateAria2TopbarBadge({maxDl: 5});
    return {
      rendered: document.getElementById('aria2-badge-max')?.textContent,
      canonical: settingsData.transfer_policy.max_concurrent_executions,
    };
  });
  expect(projected).toEqual({rendered: '5', canonical: 5});
});
