const { test, expect } = require('@playwright/test');

async function waitForFinalBatch(page) {
  await page.goto('/');
  await page.waitForFunction(() => Boolean(window.DPUICorrectionBatch1Final && window.DPUICorrectionBatch1));
}

test('batch1 final: Activity Log filters are labeled, server-backed, stable on refresh, and year-inclusive', async ({ page }) => {
  const requests = [];
  await page.route('**/api/events*', async route => {
    const url = new URL(route.request().url());
    requests.push(Object.fromEntries(url.searchParams.entries()));
    const search = url.searchParams.get('search') || '';
    const items = search === 'nothing'
      ? []
      : [{
          level: 'warn',
          message: 'Retry delayed',
          torrent_name: 'example.iso',
          created_at: '2026-09-06 08:30:00',
        }];
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({items, truncated: search !== 'nothing', limit: 500}),
    });
  });

  await waitForFinalBatch(page);
  await page.evaluate(() => nav(document.querySelector('[data-view="events"]')));
  await expect(page.locator('#ev-timeframe')).toBeAttached();

  await expect(page.locator('#view-events .dp-activity-filter-label')).toHaveText(['Timeframe', 'Severity']);
  await expect(page.locator('#ev-timeframe option')).toHaveText([
    'Last hour', 'Last 12 hours', 'Last day', 'Last 3 days',
    'Last week', 'Last 30 days', 'Available history',
  ]);
  await expect(page.locator('#ev-level option')).toHaveText(['All', 'Info', 'Warning', 'Error']);
  await expect(page.locator('#ev-timeframe')).toHaveValue('all');
  await expect(page.locator('#ev-level')).toHaveValue('');
  await expect(page.locator('label[for="ev-search"]')).toHaveCount(0);

  await page.locator('#ev-timeframe').selectOption('12h');
  await page.locator('#ev-level').selectOption('warn');
  await page.locator('#ev-search').fill('Retry');
  await page.waitForTimeout(350);
  await page.waitForFunction(() => document.querySelector('.dp-activity-message')?.textContent === 'Retry delayed');

  const filtered = requests[requests.length - 1];
  expect(filtered.limit).toBe('500');
  expect(filtered.timeframe).toBe('12h');
  expect(filtered.level).toBe('warn');
  expect(filtered.search).toBe('Retry');

  await expect(page.locator('.dp-activity-time')).toContainText('2026');
  await expect(page.locator('#dp-activity-result-note')).toBeVisible();
  await expect(page.locator('#dp-activity-result-note')).toContainText('Showing the latest 500 matching events');

  const beforeRefresh = requests.length;
  await page.locator('.dp-activity-refresh').click();
  await page.waitForFunction(count => window.__unused === undefined && count >= 0, beforeRefresh);
  await expect.poll(() => requests.length).toBeGreaterThan(beforeRefresh);
  const refreshed = requests[requests.length - 1];
  expect(refreshed.timeframe).toBe('12h');
  expect(refreshed.level).toBe('warn');
  expect(refreshed.search).toBe('Retry');

  await page.locator('#ev-search').fill('nothing');
  await page.waitForTimeout(350);
  await expect(page.locator('#event-list .empty')).toContainText('No events match the current filters.');
  await expect(page.locator('#dp-activity-result-note')).toBeHidden();
  await expect(page.locator('#view-events [id*="pagination"], #view-events [class*="pagination"]')).toHaveCount(0);
});

test('batch1 final: Archive Passwords use latched reveal and maintain one trailing append row', async ({ page }) => {
  await page.route('**/api/settings/extraction-passwords', async route => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({passwords: 'alpha\nbeta'}),
    });
  });

  await waitForFinalBatch(page);
  await page.evaluate(async () => {
    nav(document.querySelector('[data-view="settings"]'));
    await loadSettings();
  });
  await page.locator('#view-settings [data-tab="extraction"]').click();
  const editor = page.locator('.dp-settings-extraction-password-editor');
  await expect(editor).toBeVisible();
  await page.waitForFunction(() => document.querySelectorAll('.dp-settings-extraction-password-editor .dp-settings-password-line').length === 3);

  const rows = editor.locator('.dp-settings-password-line');
  await expect(rows).toHaveCount(3);
  await expect(rows.nth(0)).toHaveAttribute('type', 'password');
  await expect(rows.nth(1)).toHaveAttribute('type', 'password');
  await expect(rows.nth(2)).toHaveValue('');

  await rows.nth(0).focus();
  await expect(rows.nth(0)).toHaveAttribute('type', 'text');
  await expect(rows.nth(1)).toHaveAttribute('type', 'password');

  const eye = editor.locator('.dp-settings-password-eye');
  await expect(eye).toHaveAttribute('aria-pressed', 'false');
  await expect(eye).toHaveAttribute('aria-label', 'Show all passwords');
  await eye.click();
  await expect(eye).toHaveAttribute('aria-pressed', 'true');
  await expect(eye).toHaveAttribute('aria-label', 'Hide all passwords');
  for (let i = 0; i < 3; i += 1) await expect(rows.nth(i)).toHaveAttribute('type', 'text');

  await eye.click();
  await expect(eye).toHaveAttribute('aria-pressed', 'false');
  await expect(rows.nth(1)).toHaveAttribute('type', 'password');

  const tail = editor.locator('.dp-settings-password-line').last();
  await tail.fill('gamma');
  await expect(editor.locator('.dp-settings-password-line')).toHaveCount(4);
  await expect(editor.locator('.dp-settings-password-line').last()).toHaveValue('');

  const serialized = await page.locator('#view-settings [data-setting="extraction_password"]').inputValue();
  expect(serialized).toBe('alpha\nbeta\ngamma');
  await expect(page.locator('.dp-settings-extraction-password-field > .form-hint')).toContainText('Add one password per line');

  const geometry = await editor.evaluate(node => ({
    maxHeight: parseFloat(getComputedStyle(node).maxHeight),
    overflowY: getComputedStyle(node).overflowY,
    eyeRight: parseFloat(getComputedStyle(node.querySelector('.dp-settings-password-eye')).right),
    eyeBottom: parseFloat(getComputedStyle(node.querySelector('.dp-settings-password-eye')).bottom),
  }));
  expect(geometry.maxHeight).toBeGreaterThan(0);
  expect(['auto', 'scroll']).toContain(geometry.overflowY);
  expect(geometry.eyeRight).toBeGreaterThanOrEqual(0);
  expect(geometry.eyeBottom).toBeGreaterThanOrEqual(0);
});
