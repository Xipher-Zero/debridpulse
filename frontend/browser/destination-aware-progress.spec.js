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
    if (current) Object.assign(current, {progress: update.progress, active_execution_progress: update.active_execution_progress,
                                         active_execution_basis: update.active_execution_basis ?? null});
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

// --- Progress presentation priority -----------------------------------------
// The renderer chooses visual priority among the two backend facts; it never
// creates a third. Every case below drives the one shared renderer.

const primary = row => row.locator('.prog-fill');

// A2: the observed SAB-shaped row -- no verified percentage, a trustworthy
// in-flight one -- leads with that percentage instead of an indeterminate stripe.
test('active-only execution progress is the primary bar, worded as not yet verified', async ({ page }) => {
  await isolateExternalFonts(page);
  await downloads(page, [item(9720, null, 94.6)]);
  const row = cell(page, 9720);
  await expect(row.locator('.prog-pct')).toHaveText('94.6% in progress');
  await expect(row.locator('[data-role="execution-unverified"]')).toHaveText('not yet verified');
  await expect(primary(row)).toHaveAttribute('style', /^width:94\.6%$/);
  await expect(primary(row)).toHaveAttribute('data-progress-basis', 'execution');
  await expect(primary(row)).not.toHaveClass(/done/);
  // One claim of the value, not three: no secondary lane or label repeats it.
  await expect(await lane(row)).toHaveCount(0);
  await expect(row.locator('[data-role="execution-progress"]')).toHaveCount(0);
});

// A1 and A5: a verified percentage keeps the primary bar, whatever the
// execution reports -- the two totals are never compared.
test('a verified percentage stays primary beside any execution value', async ({ page }) => {
  await isolateExternalFonts(page);
  await downloads(page, [item(9721, 63, null), item(9722, 41, 76), item(9723, 80, 12), item(9724, 41, 41)]);
  await expect(cell(page, 9721).locator('.prog-pct')).toHaveText('63%');
  await expect(await lane(cell(page, 9721))).toHaveCount(0);
  for (const [id, verified, active] of [[9722, 41, 76], [9723, 80, 12], [9724, 41, 41]]) {
    const row = cell(page, id);
    await expect(row.locator('.prog-pct')).toHaveText(`${verified}% verified`);
    await expect(primary(row)).toHaveAttribute('style', new RegExp(`^width:${verified}%$`));
    await expect(primary(row)).not.toHaveAttribute('data-progress-basis', /.*/);
    await expect(row.locator('[data-role="execution-progress"]')).toHaveText(`in progress ${active}.0%`);
    await expect(row.locator('[data-role="execution-unverified"]')).toHaveCount(0);
  }
});

// A7: a lingering execution value on a row that is not running is stale.
test('a non-running row never promotes or shows a stale execution value', async ({ page }) => {
  await isolateExternalFonts(page);
  const stale = (id, status, progress) => ({...item(id, progress, 67), status, presentation_status: status,
    presentation_badge_status: status});
  await downloads(page, [stale(9725, 'paused', 31), stale(9726, 'error', 31), stale(9727, 'cancelled', 31),
                         stale(9728, 'paused', null), stale(9729, 'queued', null)]);
  for (const id of [9725, 9726, 9727, 9728, 9729]) {
    const row = cell(page, id);
    await expect(row).not.toContainText('in progress');
    await expect(row.locator('[data-role="execution-unverified"], [data-role="execution-progress"]')).toHaveCount(0);
    await expect(primary(row)).not.toHaveAttribute('data-progress-basis', /.*/);
  }
  for (const id of [9725, 9726, 9727]) await expect(cell(page, id).locator('.prog-pct')).toHaveText('31%');
  for (const id of [9728, 9729]) await expect(cell(page, id).locator('.prog-pct')).toHaveText('—');
});

// A8: a replaced execution may move the dominant bar backward -- only
// verified material is durable, so nothing smooths it.
test('execution replacement may move the primary bar backward', async ({ page }) => {
  await isolateExternalFonts(page);
  await downloads(page, [item(9730, null, 67)]);
  const row = cell(page, 9730);
  await expect(row.locator('.prog-pct')).toHaveText('67.0% in progress');
  await event(page, [{id: 9730, status: 'downloading', progress: null, active_execution_progress: 5, status_changed: false}]);
  await expect(row.locator('.prog-pct')).toHaveText('5.0% in progress');
  await expect(primary(row)).toHaveAttribute('style', /^width:5%$/);
  // With verified material the replacement only moves the secondary lane.
  await event(page, [{id: 9730, status: 'downloading', progress: 31, active_execution_progress: 67, status_changed: false}]);
  await expect(row.locator('.prog-pct')).toHaveText('31% verified');
  await event(page, [{id: 9730, status: 'downloading', progress: 31, active_execution_progress: 5, status_changed: false}]);
  await expect(row.locator('.prog-pct')).toHaveText('31% verified');
  await expect(row.locator('[data-role="execution-progress"]')).toHaveText('in progress 5.0%');
});

// A9 and A11: a full execution is not completion; nothing known is not 0%.
test('a full execution on a running row is never Done, and no fact is never 0%', async ({ page }) => {
  await isolateExternalFonts(page);
  await downloads(page, [item(9731, null, 100), item(9732, null, null)]);
  const full = cell(page, 9731);
  await expect(full.locator('.prog-pct')).toHaveText('100.0% in progress');
  await expect(primary(full)).not.toHaveClass(/done/);
  await expect(full).not.toContainText(/Done|Completed|100% verified/);
  const none = cell(page, 9732);
  await expect(none.locator('.prog-pct')).toHaveText('—');
  await expect(none).not.toContainText('0%');
  await expect(primary(none)).toHaveAttribute('style', /repeating-linear-gradient/);
});

test('Recent Activity and Details follow the same priority', async ({ page }) => {
  await isolateExternalFonts(page);
  const errors = observeRuntime(page);
  const running = item(9733, null, 94.6);
  const paused = {...item(9734, 31, 67), status: 'paused', presentation_status: 'paused', presentation_badge_status: 'paused'};
  await listed(page, [running, paused]);
  await page.route(url => /^\/api\/torrents\/973[34]$/.test(url.pathname), route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify(new URL(route.request().url()).pathname.endsWith('9733') ? running : paused)}));
  await page.goto('/');
  await page.evaluate(async () => { await loadRecent(); });
  const recent = page.locator('#dash-tbody tr[data-torrent-id="9733"] [data-role="transfer-progress"]');
  await expect(recent.locator('.prog-pct')).toHaveText('94.6% in progress');
  await expect(recent.locator('[data-role="execution-unverified"]')).toHaveText('not yet verified');
  await page.evaluate(() => showDetail(9733));
  const detail = page.locator('#modal-body .dk', {hasText: /^Progress$/}).locator('..').locator('.dv');
  await expect(detail).toHaveText('in progress 94.6% (not yet verified)');
  await page.evaluate(() => showDetail(9734));
  await expect(detail).toHaveText('31.0%');
  expect(errors).toEqual([]);
});


// --- Nothing verified yet: acquisition leads, worded as such --------------------
// Usenet and Media Downloads acquire privately: nothing is verified material
// until import. While verified progress is exactly 0% (or unknown), the
// execution's acquisition percentage leads the primary bar as "in progress /
// not yet verified"; what it counts comes from ``active_execution_basis``
// (bytes, or completed parts such as media segments), never from an executor.

const at = (id, progress, active, basis, status = 'downloading') => ({
  ...item(id, progress, active), active_execution_basis: basis, status, presentation_status: status,
  presentation_badge_status: status,
});

test('at exactly 0% verified the acquisition leads the primary bar, not yet verified', async ({ page }) => {
  await isolateExternalFonts(page);
  const errors = observeRuntime(page);
  await downloads(page, [at(9740, 0, 42.5, 'bytes')]);
  const row = cell(page, 9740);
  await expect(row.locator('.prog-pct')).toHaveText('42.5% in progress');
  await expect(row.locator('[data-role="execution-unverified"]')).toHaveText('not yet verified');
  await expect(primary(row)).toHaveAttribute('style', /^width:42\.5%$/);
  await expect(primary(row)).toHaveAttribute('data-progress-basis', 'execution');
  await expect(await lane(row)).toHaveCount(0);                     // one claim of the value
  await event(page, [{id: 9740, progress: 0, active_execution_progress: 57, active_execution_basis: 'bytes'}]);
  await expect(row.locator('.prog-pct')).toHaveText('57.0% in progress');
  await expect(row).not.toContainText('verified 57');
  expect(errors).toEqual([]);
});

test('nonzero verified progress keeps priority over any acquisition figure', async ({ page }) => {
  await isolateExternalFonts(page);
  await downloads(page, [at(9748, 12, 60, 'bytes'), at(9749, 12, 37.5, 'units')]);
  await expect(cell(page, 9748).locator('.prog-pct')).toHaveText('12% verified');
  await expect(primary(cell(page, 9748))).toHaveAttribute('style', /^width:12%$/);
  await expect(cell(page, 9748).locator('[data-role="execution-progress"]')).toHaveText('in progress 60.0%');
  await expect(cell(page, 9749).locator('[data-role="execution-progress"]')).toHaveText('in progress 37.5% of parts');
});

test('completed parts are labelled as parts, never as bytes or completion', async ({ page }) => {
  await isolateExternalFonts(page);
  await downloads(page, [at(9750, null, 37.5, 'units'), at(9751, 0, 100, 'units')]);
  await expect(cell(page, 9750).locator('.prog-pct')).toHaveText('37.5% of parts');
  await expect(cell(page, 9750).locator('[data-role="execution-unverified"]')).toHaveText('not yet verified');
  await expect(cell(page, 9751).locator('.prog-pct')).toHaveText('100.0% of parts');
  await expect(primary(cell(page, 9751))).not.toHaveClass(/done/);
  await expect(cell(page, 9751)).not.toContainText(/Done|Completed|100% verified/);
});

test('acquisition hands over to processing, then to completion, without a verified rollback', async ({ page }) => {
  await isolateExternalFonts(page);
  await downloads(page, [at(9752, 0, 98, 'bytes')]);
  const row = cell(page, 9752);
  await expect(row.locator('.prog-pct')).toHaveText('98.0% in progress');
  // Repair, unpack, remux or finalization: the acquisition figure is retired.
  await event(page, [{id: 9752, progress: 0, active_execution_progress: null, active_execution_basis: 'processing'}]);
  await expect(row.locator('.prog-pct')).toHaveText('processing');
  await expect(row.locator('[data-role="execution-unverified"]')).toHaveText('not yet verified');
  await expect(primary(row)).toHaveClass(/is-indeterminate/);
  await expect(primary(row)).toHaveCSS('animation-name', 'dp-prog-indeterminate');
  await expect(row).not.toContainText(/0%|98/);
  // Processing beside verified material keeps the verified figure.
  await downloads(page, [at(9753, 40, null, 'processing'), at(9754, 100, null, null, 'completed')]);
  await expect(cell(page, 9753).locator('.prog-pct')).toHaveText('40% verified');
  await expect(cell(page, 9753).locator('[data-role="execution-processing"]')).toHaveText('processing');
  await expect(cell(page, 9754).locator('.prog-pct')).toHaveText('100%');
});

test('an unknown or untrustworthy total is indeterminate and never a fabricated 0%', async ({ page }) => {
  await isolateExternalFonts(page);
  // No basis and no percentage: mixed, changing or unknown totals project nothing.
  await downloads(page, [at(9741, null, null, null), at(9742, 0, null, null)]);
  await expect(cell(page, 9741).locator('.prog-pct')).toHaveText('—');
  await expect(primary(cell(page, 9741))).toHaveClass(/is-indeterminate/);
  // A known total with no bytes yet (an aria2 start) is a known 0%.
  await expect(cell(page, 9742).locator('.prog-pct')).toHaveText('0%');
  await expect(primary(cell(page, 9742))).toHaveClass(/is-indeterminate/);
});

test('determinate, paused, failed and completed rows are never indeterminate', async ({ page }) => {
  await isolateExternalFonts(page);
  await downloads(page, [at(9743, 40, null, null), at(9744, 0, 30, 'bytes', 'paused'), at(9745, 0, null, null, 'error'),
                         at(9746, 100, null, null, 'completed')]);
  // The aria2 baseline: verified material is the bar, unchanged.
  await expect(cell(page, 9743).locator('.prog-pct')).toHaveText('40%');
  await expect(primary(cell(page, 9743))).toHaveAttribute('style', /^width:40%$/);
  for (const id of [9743, 9744, 9745, 9746]) {
    await expect(primary(cell(page, id))).not.toHaveClass(/is-indeterminate/);
    await expect(primary(cell(page, id))).toHaveCSS('animation-name', 'none');
  }
  await expect(cell(page, 9744)).not.toContainText('in progress');
  await expect(cell(page, 9746).locator('.prog-pct')).toHaveText('100%');
});

test('reduced motion keeps the indeterminate state but stops its movement', async ({ page }) => {
  await isolateExternalFonts(page);
  await page.emulateMedia({reducedMotion: 'reduce'});
  await downloads(page, [at(9747, null, null, null)]);
  await expect(primary(cell(page, 9747))).toHaveClass(/is-indeterminate/);
  await expect(primary(cell(page, 9747))).toHaveCSS('animation-name', 'none');
});
