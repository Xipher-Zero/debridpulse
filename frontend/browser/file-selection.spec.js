const { test, expect } = require('@playwright/test');

// Universal file-selection modal — browser contract (specification section 61).
// The Universal Transfer Core owns every policy decision and deadline; these
// tests only exercise the presentation owner (window.DPFileSelection) against
// mocked authoritative API state.

const MANIFEST_A = 'manifest-aaaa1111';
const ENTRIES = [
  {entry_id: 'e-s1e01', name: 'e01.mkv', relative_path: 'Season 1/e01.mkv', size_bytes: 1073741824},
  {entry_id: 'e-s1e02', name: 'e02.mkv', relative_path: 'Season 1/e02.mkv', size_bytes: 1181116006},
  {entry_id: 'e-extras', name: 'behind.mkv', relative_path: 'Extras/behind.mkv', size_bytes: 524288000},
  {entry_id: 'e-nfo', name: 'info.nfo', relative_path: 'info.nfo', size_bytes: 0},
];

function selectionView(overrides) {
  return Object.assign({
    eligible: true,
    mutable: true,
    manifest_id: MANIFEST_A,
    decision: 'pending',
    decision_reason: null,
    file_count: ENTRIES.length,
    total_size_bytes: ENTRIES.reduce((s, e) => s + e.size_bytes, 0),
    entries: ENTRIES,
    selected_entry_ids: [],
    auto_offer: true,
    auto_offer_until: 1060.0,
    decision_deadline: 1120.0,
    initially_available: true,
    server_now: 1000.0,
  }, overrides || {});
}

async function stub(page, state) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.route(url => url.pathname === '/api/file-selections/offers', route =>
    route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({offers: state.offers || []})}));
  await page.route(url => url.pathname === '/api/torrents/7/file-selection', route =>
    route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify(state.view)}));
  await page.route(url => url.pathname === '/api/torrents/7', route =>
    route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({id: 7, name: 'Some.Show.S01', files: [], events: [], route_attempts: [],
        execution_attempts: [], executors: [], source_outcomes: []})}));
  await page.route(url => url.pathname === '/api/torrents/7/file-selection/confirm', route => {
    state.confirmBody = route.request().postDataJSON();
    if (state.confirmStatus === 409) {
      return route.fulfill({status: 409, contentType: 'application/json',
        body: JSON.stringify({detail: 'Executable manifest already committed'})});
    }
    state.view = selectionView({decision: 'explicit', mutable: true,
      selected_entry_ids: state.confirmBody.entry_ids, auto_offer: false, decision_deadline: null});
    return route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({ok: true, decision: 'explicit', manifest_id: MANIFEST_A, detail: 'confirmed'})});
  });
  await page.route(url => url.pathname === '/api/torrents/7/file-selection/dismiss', route => {
    state.dismissBody = route.request().postDataJSON();
    return route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({ok: true, decision: 'all', manifest_id: MANIFEST_A, detail: 'closed'})});
  });
  await page.route(url => url.pathname === '/api/torrents/7/cancel', route => {
    state.cancelHit = (state.cancelHit || 0) + 1;
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true})});
  });
}

async function boot(page) {
  await page.goto('/');
  await page.waitForFunction(() => Boolean(window.DPFileSelection && window.DPModal));
}

async function openViaEvent(page) {
  await page.evaluate(() => document.dispatchEvent(
    new CustomEvent('debridpulse:file-selection-available', {detail: {transfer_id: 7}})));
  await expect(page.locator('#overlay')).toHaveClass(/\bopen\b/);
  await expect(page.locator('#modal[data-dp-modal-mode="file-selection"]')).toBeVisible();
}

test('SSE file_selection_available opens the selector and renders the folder tree', async ({page}) => {
  const state = {view: selectionView()};
  await stub(page, state);
  await boot(page);
  await openViaEvent(page);

  await expect(page.locator('#modal-title')).toHaveText('Select files');
  await expect(page.locator('.dp-fs-tree .dp-fs-row--folder')).toHaveCount(2);   // Season 1, Extras
  await expect(page.locator('.dp-fs-tree .dp-fs-check--file')).toHaveCount(4);
  await expect(page.locator('.dp-fs-subtitle')).toHaveText('Some.Show.S01');
  await expect(page.locator('#modal-footer')).toBeVisible();
  await expect(page.locator('#modal-footer .dp-fs-foot-note'))
    .toContainText('Confirm downloads only the selected files. Cancel stops the entire transfer.');
});

test('cold-load offers query recovers an active offer', async ({page}) => {
  const state = {view: selectionView(), offers: [
    {transfer_id: 7, manifest_id: MANIFEST_A, file_count: 4, decision_deadline: 1120.0, auto_offer_until: 1060.0},
  ]};
  await stub(page, state);
  await boot(page);
  await page.evaluate(() => window.DPFileSelection.pollOffers());
  await expect(page.locator('#modal[data-dp-modal-mode="file-selection"]')).toBeVisible();
  await expect(page.locator('.dp-fs-tree .dp-fs-check--file')).toHaveCount(4);
});

test('default draft is ALL; folder tri-state, Select all / Deselect all and counts track the draft', async ({page}) => {
  const state = {view: selectionView()};
  await stub(page, state);
  await boot(page);
  await openViaEvent(page);

  const count = page.locator('.dp-fs-count');
  await expect(count).toHaveText('4 of 4 files selected');
  await expect(page.locator('.dp-fs-toggle-all')).toHaveText('Deselect all');

  // Uncheck one file in "Season 1" → that folder becomes indeterminate and the
  // state-aware toggle flips to "Select all".
  await page.locator('.dp-fs-check--file[data-entry-id="e-s1e01"]').uncheck();
  await expect(count).toHaveText('3 of 4 files selected');
  const seasonFolder = page.locator('.dp-fs-check--folder[data-folder-path="Season 1"]');
  await expect(seasonFolder).toHaveJSProperty('indeterminate', true);
  await expect(page.locator('.dp-fs-toggle-all')).toHaveText('Select all');

  // Select all → 4/4, then Deselect all → 0/4, Confirm disabled.
  await page.locator('.dp-fs-toggle-all').click();
  await expect(count).toHaveText('4 of 4 files selected');
  await expect(page.locator('.dp-fs-toggle-all')).toHaveText('Deselect all');
  await page.locator('.dp-fs-toggle-all').click();
  await expect(count).toHaveText('0 of 4 files selected');
  await expect(page.locator('#modal-footer .dp-fs-confirm')).toBeDisabled();
  await expect(page.locator('.dp-fs-toggle-all')).toHaveText('Select all');

  // Folder checkbox selects its whole subtree.
  await page.locator('.dp-fs-check--folder[data-folder-path="Season 1"]').check();
  await expect(count).toHaveText('2 of 4 files selected');
  await expect(page.locator('.dp-fs-check--file[data-entry-id="e-s1e02"]')).toBeChecked();
  await expect(page.locator('#modal-footer .dp-fs-confirm')).toBeEnabled();
});

test('folder expand/collapse toggles visibility only, never selection', async ({page}) => {
  const state = {view: selectionView()};
  await stub(page, state);
  await boot(page);
  await openViaEvent(page);

  const caret = page.locator('.dp-fs-row--folder', {hasText: 'Season 1'}).locator('.dp-fs-caret');
  const child = page.locator('.dp-fs-check--file[data-entry-id="e-s1e01"]');
  await expect(child).toBeVisible();
  await expect(page.locator('.dp-fs-count')).toHaveText('4 of 4 files selected');
  await caret.click();
  await expect(child).toBeHidden();
  await expect(page.locator('.dp-fs-count')).toHaveText('4 of 4 files selected');   // unchanged
  await caret.click();
  await expect(child).toBeVisible();
});

test('Confirm posts manifest_id + selected entry_ids only, then closes with no policy leak', async ({page}) => {
  const state = {view: selectionView()};
  await stub(page, state);
  await boot(page);
  await openViaEvent(page);

  await page.locator('.dp-fs-check--file[data-entry-id="e-extras"]').uncheck();
  await page.locator('.dp-fs-check--file[data-entry-id="e-nfo"]').uncheck();
  await page.locator('#modal-footer .dp-fs-confirm').click();

  await expect(page.locator('#overlay')).not.toHaveClass(/\bopen\b/);
  expect(state.confirmBody).toEqual({manifest_id: MANIFEST_A, entry_ids: ['e-s1e01', 'e-s1e02']});
  expect(Object.keys(state.confirmBody).sort()).toEqual(['entry_ids', 'manifest_id']);
  await expect(page.locator('.toast')).toContainText('2 files selected for download');
});

test('a stale 409 refreshes authoritative state and never shows success', async ({page}) => {
  const state = {view: selectionView(), confirmStatus: 409};
  await stub(page, state);
  await boot(page);
  await openViaEvent(page);

  await page.locator('#modal-footer .dp-fs-confirm').click();
  await expect(page.locator('.toast')).toContainText(/selection changed|already started/i);
  await expect(page.locator('.toast')).not.toContainText('selected for download');
});

test('the 120s countdown renders from the server deadline and is presentation-only', async ({page}) => {
  const state = {view: selectionView({server_now: 1000.0, decision_deadline: 1120.0})};
  await stub(page, state);
  await boot(page);
  await openViaEvent(page);

  const clock = page.locator('#modal-footer .dp-fs-clock');
  await expect(page.locator('#modal-footer .dp-fs-foot-countdown')).toBeVisible();
  await expect(clock).toHaveText(/^(1:5\d|2:00)$/);   // ~120s remaining at open
});

test('a PREPARING-origin offer (initially_available false, active decision_deadline) opens identically to a cached one', async ({page}) => {
  // Specification section 9: the browser must accept initially_available === false
  // with a non-null decision_deadline as a fully valid actionable selector, and
  // must never gate presentation on initially_available.
  const state = {view: selectionView({initially_available: false, server_now: 1000.0, decision_deadline: 1120.0}),
    offers: [{transfer_id: 7, manifest_id: MANIFEST_A, file_count: 4, decision_deadline: 1120.0, auto_offer_until: 1060.0}]};
  await stub(page, state);
  await boot(page);

  // Cold-load recovery through the offers query opens the selector even though
  // initially_available is false — presentation is gated only on the offer +
  // an active decision_deadline, never on initially_available.
  await page.evaluate(() => window.DPFileSelection.pollOffers());
  await expect(page.locator('#modal[data-dp-modal-mode="file-selection"]')).toBeVisible();
  await expect(page.locator('#modal-title')).toHaveText('Select files');
  await expect(page.locator('.dp-fs-tree .dp-fs-check--file')).toHaveCount(4);
  await expect(page.locator('#modal-footer .dp-fs-foot-countdown')).toBeVisible();
  await expect(page.locator('#modal-footer .dp-fs-clock')).toHaveText(/^(1:5\d|2:00)$/);
});

test('reaching zero refreshes authoritative state; an expired hold closes the selector', async ({page}) => {
  const state = {view: selectionView({server_now: 1119.0, decision_deadline: 1120.0})};
  await stub(page, state);
  await boot(page);
  await openViaEvent(page);
  // Backend has since settled to ALL.
  state.view = selectionView({mutable: false, decision: 'all', decision_reason: 'decision_timeout',
    auto_offer: false, decision_deadline: null});
  await expect(page.locator('#overlay')).not.toHaveClass(/\bopen\b/, {timeout: 6000});
  await expect(page.locator('.toast')).toContainText(/window closed|will download/i);
});

test('Close and X share identical semantics and both POST dismiss', async ({page}) => {
  for (const closer of ['#modal-footer .dp-fs-close', '#modal .modal-close']) {
    const state = {view: selectionView()};
    await stub(page, state);
    await boot(page);
    await openViaEvent(page);
    await page.locator(closer).click();
    await expect(page.locator('#overlay')).not.toHaveClass(/\bopen\b/);
    await expect.poll(() => state.dismissBody).toEqual({manifest_id: MANIFEST_A});
    await page.unrouteAll({behavior: 'ignoreErrors'}).catch(() => {});
  }
});

test('Cancel Transfer routes to the existing transfer cancel endpoint', async ({page}) => {
  const state = {view: selectionView()};
  await stub(page, state);
  await boot(page);
  await openViaEvent(page);
  await page.locator('#modal-footer .dp-fs-cancel').click();
  await expect(page.locator('#overlay')).not.toHaveClass(/\bopen\b/);
  await expect.poll(() => state.cancelHit).toBe(1);
});

test('a single-file resource never opens the selector', async ({page}) => {
  const only = [ENTRIES[3]];
  const state = {view: selectionView({entries: only, file_count: 1, decision: 'all',
    decision_reason: 'single_file', auto_offer: false})};
  await stub(page, state);
  await boot(page);
  await page.evaluate(() => document.dispatchEvent(
    new CustomEvent('debridpulse:file-selection-available', {detail: {transfer_id: 7}})));
  await page.waitForTimeout(400);
  await expect(page.locator('#overlay')).not.toHaveClass(/\bopen\b/);
});

test('a manifest that appears after the 60s window (auto_offer false) never auto-opens', async ({page}) => {
  const state = {view: selectionView({auto_offer: false, auto_offer_until: 1060.0, decision_deadline: null,
    initially_available: false})};
  await stub(page, state);
  await boot(page);
  await page.evaluate(() => document.dispatchEvent(
    new CustomEvent('debridpulse:file-selection-available', {detail: {transfer_id: 7}})));
  await page.waitForTimeout(400);
  await expect(page.locator('#overlay')).not.toHaveClass(/\bopen\b/);
});

test('the selector reuses the one shared #overlay / #modal shell', async ({page}) => {
  const state = {view: selectionView()};
  await stub(page, state);
  await boot(page);
  await openViaEvent(page);
  expect(await page.locator('#overlay').count()).toBe(1);
  expect(await page.locator('#modal').count()).toBe(1);
  expect(await page.locator('#modal-footer').count()).toBe(1);
  await expect(page.locator('#modal #modal-body .dp-fs')).toBeVisible();
});

// ── Details manual entry point (specification section 44) ───────────────────

test('Details exposes "Select files" while mutable and pending, and opens the selector', async ({page}) => {
  const state = {view: selectionView({auto_offer: false})};
  await stub(page, state);
  await boot(page);
  await page.evaluate(() => showDetail(7));
  await expect(page.locator('#overlay')).toHaveClass(/\bopen\b/);
  const entry = page.locator('#dp-detail-actions .dp-file-selection-entry');
  await expect(entry).toHaveText('Select files');
  await entry.click();
  await expect(page.locator('#modal[data-dp-modal-mode="file-selection"]')).toBeVisible();
  await expect(page.locator('.dp-fs-tree .dp-fs-check--file')).toHaveCount(4);
});

test('Details exposes "Change file selection" once an explicit subset is confirmed', async ({page}) => {
  const state = {view: selectionView({decision: 'explicit', auto_offer: false, decision_deadline: null,
    selected_entry_ids: ['e-s1e01', 'e-s1e02']})};
  await stub(page, state);
  await boot(page);
  await page.evaluate(() => showDetail(7));
  await expect(page.locator('#dp-detail-actions .dp-file-selection-entry')).toHaveText('Change file selection');
});

test('a locked selection shows a passive summary and no mutation control', async ({page}) => {
  const state = {view: selectionView({decision: 'explicit', mutable: false, auto_offer: false,
    decision_deadline: null, selected_entry_ids: ['e-s1e01', 'e-s1e02']})};
  await stub(page, state);
  await boot(page);
  await page.evaluate(() => showDetail(7));
  await expect(page.locator('#dp-detail-actions .dp-file-selection-summary')).toHaveText('2 of 4 files selected');
  await expect(page.locator('#dp-detail-actions .dp-file-selection-entry')).toHaveCount(0);
});

test('a non-eligible transfer shows no file-selection control in Details', async ({page}) => {
  const state = {view: {eligible: false}};
  await stub(page, state);
  await boot(page);
  await page.evaluate(() => showDetail(7));
  await expect(page.locator('#overlay')).toHaveClass(/\bopen\b/);
  await expect(page.locator('#dp-detail-actions')).toBeEmpty();
});
