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

function cell(page, id) {
  return page.locator(`#t-tbody tr[data-torrent-id="${id}"] [data-role="transfer-progress"]`);
}

let served = [];

async function downloads(page, items) {
  served = items;
  await listed(page, items);
  await page.goto('/');
  await page.evaluate(async () => {
    nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]'));
    await loadTorrents();
  });
}

/** A live progress-only event; the served list moves with it, so a later
 *  list refresh presents the same backend truth the event carried. */
async function event(page, items) {
  for (const update of items) {
    const current = served.find(entry => entry.id === update.id);
    if (current) Object.assign(current, {progress: update.progress, active_execution_progress: update.active_execution_progress});
  }
  await page.evaluate(items => patchProgressOnlyTransferEvent({progress_only: true, items}), items);
}

async function lane(row) {
  return row.locator('[data-role="execution-progress-lane"]');
}

test('Downloads keeps verified progress primary and shows the reconstruction as its own lane', async ({ page }) => {
  await isolateExternalFonts(page);
  const errors = observeRuntime(page);
  await downloads(page, [item(9701, 31, 67), item(9702, 40, null)]);
  const row = cell(page, 9701);
  await expect(row.locator('.prog-pct')).toHaveText('31% verified');
  await expect(row.locator('.prog-fill')).toHaveAttribute('style', /width:31%/);
  await expect(row.locator('[data-role="execution-progress"]')).toHaveText('in progress 67.0%');
  await expect(await lane(row)).toHaveAttribute('aria-valuenow', '67');
  const plain = cell(page, 9702);
  await expect(plain.locator('.prog-pct')).toHaveText('40%');
  await expect(plain.locator('[data-role="execution-progress"]')).toHaveCount(0);
  await expect(await lane(plain)).toHaveCount(0);

  // A live progress-only event moves the activity; the verified figure holds.
  await event(page, [{id: 9701, status: 'downloading', progress: 31, active_execution_progress: 88, status_changed: false}]);
  await expect(row.locator('.prog-pct')).toHaveText('31% verified');
  await expect(row.locator('[data-role="execution-progress"]')).toHaveText('in progress 88.0%');
  expect(errors).toEqual([]);
});

// RED 2A: durable verified progress is never blended with private reconstruction.
test('verified progress stays at 95% while the private reconstruction advances', async ({ page }) => {
  await isolateExternalFonts(page);
  await downloads(page, [item(9711, 95, 40)]);
  const row = cell(page, 9711);
  for (const active of [40, 55.5, 72.25, 99.9]) {
    await event(page, [{id: 9711, status: 'downloading', progress: 95, active_execution_progress: active, status_changed: false}]);
    await expect(row.locator('.prog-fill')).toHaveAttribute('style', /width:95%/);
    await expect(row.locator('.prog-pct')).toHaveText('95% verified');
  }
  // Nothing in the cell claims 96-100% verified material.
  await expect(row.locator('.prog-pct')).not.toHaveText(/9[6-9]%|100%/);
});

// RED 2B: the reconstruction is a distinct, quantified, moving lane.
test('the reconstruction is a distinct quantified lane that visibly moves', async ({ page }) => {
  await isolateExternalFonts(page);
  await downloads(page, [item(9712, 95, 40)]);
  const row = cell(page, 9712);
  const activity = await lane(row);
  await expect(activity).toHaveCount(1);
  await expect(activity).toHaveAttribute('role', 'progressbar');
  await expect(activity).toHaveAttribute('aria-valuenow', '40');
  await expect(activity.locator('.prog-lane-fill')).toHaveAttribute('style', /width:40%/);
  await expect(row.locator('[data-role="execution-progress"]')).toHaveText('in progress 40.0%');
  // The lane is visibly distinct from the verified bar: its own box, below it, thinner.
  const boxes = await page.evaluate(() => {
    const cell = document.querySelector('#t-tbody tr[data-torrent-id="9712"] [data-role="transfer-progress"]');
    const rect = element => { const r = element.getBoundingClientRect(); return {y: r.y, height: r.height, width: r.width}; };
    return {verified: rect(cell.querySelector('.prog')), secondary: rect(cell.querySelector('[data-role="execution-progress-lane"]'))};
  });
  expect(boxes.secondary.y).toBeGreaterThan(boxes.verified.y);
  expect(boxes.secondary.height).toBeGreaterThan(0);
  expect(boxes.secondary.width).toBeGreaterThan(0);
  expect(boxes.secondary.height).toBeLessThan(boxes.verified.height);
  // A sub-percent advance on a large reconstruction is visible.
  await event(page, [{id: 9712, status: 'downloading', progress: 95, active_execution_progress: 40.3, status_changed: false}]);
  await expect(row.locator('[data-role="execution-progress"]')).toHaveText('in progress 40.3%');
  await expect(activity.locator('.prog-lane-fill')).toHaveAttribute('style', /width:40.3%/);
});

// RED 2C: the lane never outlives the execution that reported it.
test('the lane clears when the reconstruction stops for any reason', async ({ page }) => {
  await isolateExternalFonts(page);
  await downloads(page, [item(9713, 95, 40), item(9714, 95, 40), item(9715, 95, 40)]);
  // The backend's one read reports no active execution once the attempt is
  // retired (failure, cancellation, failover, executor replacement).
  await event(page, [{id: 9713, status: 'downloading', progress: 95, active_execution_progress: null, status_changed: false}]);
  await expect(await lane(cell(page, 9713))).toHaveCount(0);
  await expect(cell(page, 9713).locator('[data-role="execution-progress"]')).toHaveCount(0);
  await expect(cell(page, 9713).locator('.prog-pct')).toHaveText('95%');
  // A row no longer downloading never shows a stale lane even if a value lingers.
  for (const [id, status] of [[9714, 'error'], [9715, 'paused']]) {
    Object.assign(served.find(entry => entry.id === id), {status, presentation_status: status,
      presentation_badge_status: status, active_execution_progress: 40});
  }
  await page.evaluate(async () => { await loadTorrents(); });
  for (const id of [9714, 9715]) {
    await expect(cell(page, id).locator('.prog-fill')).toHaveCount(1);
    await expect(await lane(cell(page, id))).toHaveCount(0);
    await expect(cell(page, id).locator('[data-role="execution-progress"]')).toHaveCount(0);
  }
});

// RED 2D: verified completion is 100% exactly once, with no lane.
test('verified completion shows 100% and no reconstruction lane', async ({ page }) => {
  await isolateExternalFonts(page);
  await downloads(page, [{...item(9716, 100, null), status: 'completed', presentation_status: 'completed',
    presentation_label: 'Completed', presentation_badge_status: 'completed'}]);
  const row = cell(page, 9716);
  await expect(row.locator('.prog-pct')).toHaveText('100%');
  await expect(row.locator('.prog-fill')).toHaveClass(/done/);
  await expect(await lane(row)).toHaveCount(0);
});

// RED 2E: executor-neutral -- the presentation depends only on the two facts.
test('any executor reporting private progress renders identically', async ({ page }) => {
  await isolateExternalFonts(page);
  const a = {...item(9717, 95, 40), current_source_identity: {kind: 'link'}, executor_id: 'rsync'};
  const b = {...item(9718, 95, 40), current_source_identity: {kind: 'host', value: 'example.test'},
    executor_id: 'fixture-private-executor'};
  await downloads(page, [a, b]);
  await expect(await lane(cell(page, 9717))).toHaveCount(1);
  const html = id => page.evaluate(id => document.querySelector(
    `#t-tbody tr[data-torrent-id="${id}"] [data-role="transfer-progress"]`).innerHTML, id);
  expect(await html(9717)).toBe(await html(9718));
});

test('Recent Activity shows the same two truths', async ({ page }) => {
  await isolateExternalFonts(page);
  const errors = observeRuntime(page);
  await listed(page, [item(9703, 31, 67)]);
  await page.goto('/');
  await page.evaluate(async () => { await loadRecent(); });
  const row = page.locator('#dash-tbody tr[data-torrent-id="9703"]');
  await expect(row.locator('[data-role="transfer-progress"] .prog-pct')).toHaveText('31% verified');
  await expect(row.locator('[data-role="execution-progress"]')).toHaveText('in progress 67.0%');
  await expect(row.locator('[data-role="execution-progress-lane"]')).toHaveAttribute('aria-valuenow', '67');
  await expect(row.locator('.dash-row-bar-fill')).toHaveAttribute('style', /width:31%/);
  expect(errors).toEqual([]);
});
