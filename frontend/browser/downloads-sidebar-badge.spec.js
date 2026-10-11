const { test, expect } = require('@playwright/test');

/* The Downloads sidebar badge counts the distinct transfers still in the
 * ordinary download lifecycle: /stats `by_status` (one torrents row per
 * logical transfer, so never artifacts or attempts). Failed transfers awaiting
 * the operator and terminal ones are not counted; zero hides the badge. It is
 * not Dashboard Active Now, which keeps its own aggregate. */

async function withStats(page, byStatus) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.route('**/api/stats', route => route.fulfill({status: 200, contentType: 'application/json',
    body: JSON.stringify({by_status: byStatus, active_operations: 99})}));
  await page.goto('/');
  await page.evaluate(() => loadStats());
}

const badge = page => page.locator('#nb-active');

test('the badge counts every unfinished lifecycle state once, and nothing failed or terminal', async ({page}) => {
  await withStats(page, {
    pending: 1, processing: 2, input_required: 1, ready: 1, queued: 3, downloading: 2,
    paused: 1, verifying: 1, extracting: 1,                                        // 13 unfinished
    error: 4, completed: 10, cancelled: 2, consolidated: 3, deleted: 5, uploading: 2, // not counted
  });
  await expect(badge(page)).toBeVisible();
  await expect(badge(page)).toHaveText('13');
  await expect(page.locator('#s-active')).toHaveText('99');                        // Active Now unchanged
});

test('with nothing unfinished the badge is hidden', async ({page}) => {
  await withStats(page, {error: 2, completed: 7, cancelled: 1, consolidated: 1, deleted: 3});
  await expect(badge(page)).toBeHidden();
});
