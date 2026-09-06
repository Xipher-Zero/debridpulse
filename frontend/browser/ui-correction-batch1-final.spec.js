const { test, expect } = require('@playwright/test');

async function waitForP4Repair(page) {
  await page.goto('/');
  await page.waitForFunction(() => Boolean(
    window.DPUICorrectionBatch1Final &&
    window.DPUICorrectionBatch1 &&
    window.DPUICorrectionP4Repair
  ));
}

test('P4 repair: Activity Log filter presentation, options, server filtering, reset, and refresh are exact', async ({ page }) => {
  const requests = [];
  await page.route('**/api/events*', async route => {
    const url = new URL(route.request().url());
    requests.push(Object.fromEntries(url.searchParams.entries()));
    const search = url.searchParams.get('search') || '';
    const items = search === 'nothing'
      ? []
      : [{
          level: 'warning',
          message: 'Retry delayed',
          torrent_name: 'example.iso',
          created_at: '2026-09-06 08:30:00',
        }];
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({items, truncated: search === 'Retry', limit: 500}),
    });
  });

  await waitForP4Repair(page);
  await page.evaluate(() => nav(document.querySelector('[data-view="events"]')));
  await expect(page.locator('#ev-timeframe')).toBeAttached();

  await expect(page.locator('#ev-timeframe option')).toHaveText([
    'Last hour',
    'Last 12 hours',
    'Last day',
    'Last 3 days',
    'Last week',
    'Last 30 days',
    'Available history',
  ]);
  await expect(page.locator('#ev-level option')).toHaveText(['All', 'Info', 'Warning', 'Error']);
  await expect(page.locator('#ev-timeframe')).toHaveValue('all');
  await expect(page.locator('#ev-level')).toHaveValue('');
  await expect(page.locator('#ev-reset')).toBeHidden();

  const presentation = await page.evaluate(() => {
    const rect = node => node.getBoundingClientRect();
    const centerY = node => {
      const box = rect(node);
      return box.top + box.height / 2;
    };
    const search = document.getElementById('ev-search');
    const timeframe = document.getElementById('ev-timeframe');
    const severity = document.getElementById('ev-level');
    const timeField = timeframe.closest('.dp-activity-filter-field--time');
    const severityField = severity.closest('.dp-activity-filter-field--severity');
    const timeLabel = timeField?.querySelector('.dp-activity-filter-label');
    const severityLabel = severityField?.querySelector('.dp-activity-filter-label');
    const timeTrigger = timeframe._dpDropdownTrigger;
    const severityTrigger = severity._dpDropdownTrigger;
    return {
      timeLines: Array.from(timeLabel?.children || []).map(node => node.textContent),
      severityText: severityLabel?.textContent || '',
      searchCenter: centerY(search),
      timeLabelCenter: centerY(timeLabel),
      timeTriggerCenter: centerY(timeTrigger),
      severityLabelCenter: centerY(severityLabel),
      severityTriggerCenter: centerY(severityTrigger),
      timeLabelRight: rect(timeLabel).right,
      timeTriggerLeft: rect(timeTrigger).left,
      severityLabelRight: rect(severityLabel).right,
      severityTriggerLeft: rect(severityTrigger).left,
      timeGrouped: Boolean(timeField?.contains(timeTrigger)),
      severityGrouped: Boolean(severityField?.contains(severityTrigger)),
      rowDisplay: getComputedStyle(search.parentElement).display,
    };
  });
  expect(presentation.timeLines).toEqual(['Time', 'Window']);
  expect(presentation.severityText).toBe('Severity');
  expect(presentation.timeGrouped).toBe(true);
  expect(presentation.severityGrouped).toBe(true);
  expect(presentation.rowDisplay).toBe('flex');
  for (const center of [
    presentation.timeLabelCenter,
    presentation.timeTriggerCenter,
    presentation.severityLabelCenter,
    presentation.severityTriggerCenter,
  ]) {
    expect(Math.abs(center - presentation.searchCenter)).toBeLessThanOrEqual(3);
  }
  expect(presentation.timeLabelRight).toBeLessThan(presentation.timeTriggerLeft);
  expect(presentation.severityLabelRight).toBeLessThan(presentation.severityTriggerLeft);
  await page.screenshot({ path: 'test-results/checkpoint-activity-default.png', fullPage: true });

  await page.locator('#ev-timeframe').selectOption('72h');
  await page.locator('#ev-level').selectOption('warning');
  await page.locator('#ev-search').fill('Retry');
  await page.waitForTimeout(325);
  await page.waitForFunction(() => document.querySelector('.dp-activity-message')?.textContent === 'Retry delayed');

  const filtered = requests[requests.length - 1];
  expect(filtered.limit).toBe('500');
  expect(filtered.include_meta).toBe('true');
  expect(filtered.timeframe).toBe('72h');
  expect(filtered.level).toBe('warning');
  expect(filtered.search).toBe('Retry');
  await expect(page.locator('#ev-reset')).toBeVisible();
  await expect(page.locator('.dp-activity-time')).toContainText('2026');
  await expect(page.locator('#dp-activity-result-note')).toBeVisible();
  await expect(page.locator('#dp-activity-result-note')).toContainText('Showing the latest 500 matching events');
  await page.screenshot({ path: 'test-results/checkpoint-activity-filtered.png', fullPage: true });

  const beforeRefresh = requests.length;
  await page.locator('.dp-activity-refresh').click();
  await expect.poll(() => requests.length).toBeGreaterThan(beforeRefresh);
  const refreshed = requests[requests.length - 1];
  expect(refreshed.timeframe).toBe('72h');
  expect(refreshed.level).toBe('warning');
  expect(refreshed.search).toBe('Retry');

  await page.locator('#ev-reset').click();
  await expect(page.locator('#ev-search')).toHaveValue('');
  await expect(page.locator('#ev-timeframe')).toHaveValue('all');
  await expect(page.locator('#ev-level')).toHaveValue('');
  await expect(page.locator('#ev-reset')).toBeHidden();

  await page.locator('#ev-search').fill('nothing');
  await page.waitForTimeout(325);
  await expect(page.locator('#event-list .empty')).toContainText('No events match your filters.');
  await expect(page.locator('#dp-activity-result-note')).toBeHidden();
  await page.locator('#ev-reset').click();
  await expect(page.locator('#ev-reset')).toBeHidden();
  await expect(page.locator('#view-events [id*="pagination"], #view-events [class*="pagination"]')).toHaveCount(0);
});

test('P4 repair: Archive Passwords render the themed ghost Show all/Hide all control and preserve line editing', async ({ page }) => {
  await page.route('**/api/settings/extraction-passwords', async route => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({passwords: 'alpha\nbeta'}),
    });
  });

  await waitForP4Repair(page);
  await page.evaluate(async () => {
    nav(document.querySelector('[data-view="settings"]'));
    await loadSettings();
  });
  await page.locator('#view-settings [data-tab="extraction"]').click();
  const editor = page.locator('.dp-settings-extraction-password-editor');
  await expect(editor).toBeVisible();
  await page.waitForFunction(() => document.querySelectorAll('.dp-settings-extraction-password-editor .dp-settings-password-line').length === 3);

  let rows = editor.locator('.dp-settings-password-line');
  await expect(rows).toHaveCount(3);
  await expect(rows.nth(0)).toHaveAttribute('type', 'password');
  await expect(rows.nth(1)).toHaveAttribute('type', 'password');
  await expect(rows.nth(2)).toHaveValue('');

  const eye = editor.locator('.dp-settings-password-eye');
  await expect(eye).toBeVisible();
  await expect(eye).toHaveClass(/dp-settings-password-eye--ghost/);
  await expect(eye).toHaveAttribute('aria-pressed', 'false');
  await expect(eye).toHaveAttribute('aria-label', 'Show all passwords');
  await expect(eye).toContainText('Show all');
  await expect(eye.locator('img')).toHaveCount(0);
  await expect(eye.locator('svg[data-lucide="eye"]')).toBeVisible();
  await expect(eye.locator('svg')).toHaveAttribute('stroke', 'currentColor');

  const darkGeometry = await eye.evaluate(button => {
    const editor = button.closest('.dp-settings-extraction-password-editor');
    const buttonRect = button.getBoundingClientRect();
    const editorRect = editor.getBoundingClientRect();
    const style = getComputedStyle(button);
    const svgStyle = getComputedStyle(button.querySelector('svg'));
    return {
      width: buttonRect.width,
      height: buttonRect.height,
      insideRight: buttonRect.right <= editorRect.right + 1,
      insideBottom: buttonRect.bottom <= editorRect.bottom + 1,
      borderStyle: style.borderStyle,
      borderWidth: style.borderWidth,
      backgroundColor: style.backgroundColor,
      color: style.color,
      svgStroke: svgStyle.stroke,
      editorPaddingBottom: getComputedStyle(editor).paddingBottom,
    };
  });
  expect(darkGeometry.width).toBeGreaterThan(80);
  expect(darkGeometry.height).toBeGreaterThanOrEqual(30);
  expect(darkGeometry.insideRight).toBe(true);
  expect(darkGeometry.insideBottom).toBe(true);
  expect(darkGeometry.borderStyle).toBe('solid');
  expect(parseFloat(darkGeometry.borderWidth)).toBeGreaterThan(0);
  expect(darkGeometry.backgroundColor).toBe('rgba(0, 0, 0, 0)');
  expect(darkGeometry.color).not.toBe('rgba(0, 0, 0, 0)');
  expect(darkGeometry.svgStroke).toBe(darkGeometry.color);
  expect(parseFloat(darkGeometry.editorPaddingBottom)).toBeGreaterThanOrEqual(48);
  await page.screenshot({ path: 'test-results/checkpoint-archive-passwords-masked.png', fullPage: true });

  await rows.nth(0).focus();
  await expect(rows.nth(0)).toHaveAttribute('type', 'text');
  await expect(rows.nth(1)).toHaveAttribute('type', 'password');
  await rows.nth(0).fill('changed');
  await rows.nth(0).press('Escape');
  await expect(editor.locator('.dp-settings-password-line').nth(0)).toHaveValue('alpha');

  await eye.click();
  await expect(eye).toHaveAttribute('aria-pressed', 'true');
  await expect(eye).toHaveAttribute('aria-label', 'Hide all passwords');
  await expect(eye).toContainText('Hide all');
  await expect(eye.locator('img')).toHaveCount(0);
  await expect(eye.locator('svg[data-lucide="eye-off"]')).toBeVisible();
  rows = editor.locator('.dp-settings-password-line');
  for (let i = 0; i < 3; i += 1) await expect(rows.nth(i)).toHaveAttribute('type', 'text');
  await page.screenshot({ path: 'test-results/checkpoint-archive-passwords-revealed.png', fullPage: true });

  await page.evaluate(() => document.body.classList.add('light'));
  const lightTheme = await eye.evaluate(button => {
    const style = getComputedStyle(button);
    const svgStyle = getComputedStyle(button.querySelector('svg'));
    return {color: style.color, svgStroke: svgStyle.stroke, borderColor: style.borderColor};
  });
  expect(lightTheme.color).not.toBe('rgba(0, 0, 0, 0)');
  expect(lightTheme.svgStroke).toBe(lightTheme.color);
  expect(lightTheme.borderColor).not.toBe('rgba(0, 0, 0, 0)');
  await page.screenshot({ path: 'test-results/checkpoint-archive-passwords-light.png', fullPage: true });
  await page.evaluate(() => document.body.classList.remove('light'));

  await eye.click();
  await expect(eye).toHaveAttribute('aria-pressed', 'false');
  await expect(eye).toContainText('Show all');
  await expect(editor.locator('.dp-settings-password-line').nth(1)).toHaveAttribute('type', 'password');

  const tail = editor.locator('.dp-settings-password-line').last();
  await tail.fill('gamma');
  await expect(editor.locator('.dp-settings-password-line')).toHaveCount(4);
  await expect(editor.locator('.dp-settings-password-line').last()).toHaveValue('');

  const middle = editor.locator('.dp-settings-password-line').nth(1);
  await middle.focus();
  await middle.fill('');
  await middle.blur();
  await expect(editor.locator('.dp-settings-password-line')).toHaveCount(3);

  const serialized = await page.locator('#view-settings [data-setting="extraction_password"]').inputValue();
  expect(serialized).toBe('alpha\ngamma');
  await expect(page.locator('.dp-settings-extraction-password-field > .form-hint')).toContainText('Add one password per line');

  const geometry = await editor.evaluate(node => ({
    maxHeight: getComputedStyle(node).maxHeight,
    overflowY: getComputedStyle(node).overflowY,
  }));
  expect(geometry.maxHeight).toBe('none');
  expect(['visible', 'clip']).toContain(geometry.overflowY);
});
