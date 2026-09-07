const { test, expect } = require('@playwright/test');

test('topbar denominator renders canonical scheduler concurrency and rejects zero projection', async ({ page }) => {
  await page.goto('/');
  await page.waitForFunction(() => !!window.DPTopbarConcurrency);

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
