const { test, expect } = require('@playwright/test');

// A destination-aware reconstruction (for example rsync continuing an aria2
// partial) reports its in-flight progress beside DP-valid progress: the bar
// and percentage stay the verified material, the activity is labelled apart.

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

function observeRuntime(page) {
  const errors = [];
  page.on('pageerror', error => errors.push(`pageerror: ${error.message}`));
  page.on('console', message => { if (message.type() === 'error') errors.push(`console: ${message.text()}`); });
  return errors;
}

function item(id, progress, active) {
  return {
    id, name: `Reconstruction ${id}`, status: 'downloading', presentation_status: 'downloading',
    presentation_label: 'Downloading', presentation_badge_status: 'downloading', attention_required: false,
    progress, active_execution_progress: active, retained_bytes: 310, size_bytes: 1000, source: 'manual',
    hash: '', label: '', created_at: '2026-09-28T00:00:00Z', current_source_identity: {kind: 'link'},
    providers: [], historical_providers: [], delivering_provider_ids: [], input_required: null,
  };
}

async function listed(page, items) {
  await page.route(url => url.pathname === '/api/torrents', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({items, total: items.length}),
  }));
}

test('Downloads keeps verified progress primary and labels the reconstruction apart', async ({ page }) => {
  await isolateExternalFonts(page);
  const errors = observeRuntime(page);
  await listed(page, [item(9701, 31, 67), item(9702, 40, null)]);
  await page.goto('/');
  await page.evaluate(async () => {
    nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]'));
    await loadTorrents();
  });
  const row = page.locator('#t-tbody tr[data-torrent-id="9701"] [data-role="transfer-progress"]');
  await expect(row.locator('.prog-pct')).toHaveText('31%');
  await expect(row.locator('.prog-fill')).toHaveAttribute('style', /width:31%/);
  await expect(row.locator('[data-role="execution-progress"]')).toHaveText('reconstructing 67%');
  const plain = page.locator('#t-tbody tr[data-torrent-id="9702"] [data-role="transfer-progress"]');
  await expect(plain.locator('.prog-pct')).toHaveText('40%');
  await expect(plain.locator('[data-role="execution-progress"]')).toHaveCount(0);

  // A live progress-only event moves the activity; the verified figure holds.
  await page.evaluate(() => patchProgressOnlyTransferEvent({progress_only: true, items: [
    {id: 9701, status: 'downloading', progress: 31, active_execution_progress: 88, status_changed: false}]}));
  await expect(row.locator('.prog-pct')).toHaveText('31%');
  await expect(row.locator('[data-role="execution-progress"]')).toHaveText('reconstructing 88%');
  expect(errors).toEqual([]);
});

test('Recent Activity shows the same two truths', async ({ page }) => {
  await isolateExternalFonts(page);
  const errors = observeRuntime(page);
  await listed(page, [item(9703, 31, 67)]);
  await page.goto('/');
  await page.evaluate(async () => { await loadRecent(); });
  const row = page.locator('#dash-tbody tr[data-torrent-id="9703"]');
  await expect(row.locator('[data-role="transfer-progress"] .prog-pct')).toHaveText('31%');
  await expect(row.locator('[data-role="execution-progress"]')).toHaveText('reconstructing 67%');
  await expect(row.locator('.dash-row-bar-fill')).toHaveAttribute('style', /width:31%/);
  expect(errors).toEqual([]);
});
