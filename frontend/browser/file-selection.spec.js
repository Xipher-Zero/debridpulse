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
    // The selector auto-presents for the life of the decision hold; the core
    // now reports the same absolute deadline for both.
    auto_offer_until: 1120.0,
    decision_deadline: 1120.0,
    initially_available: true,
    server_now: 1000.0,
    // Mirrors the canonical backend domain function
    // (transfers.file_selection.file_selection_affordance) for the default
    // mutable/pending/multi-file scenario; callers override it alongside
    // decision/mutable when they override those fields.
    file_selection_affordance: 'choose',
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
        execution_attempts: [], executors: [], source_outcomes: [],
        // Durable submission-kind fact (DP 1.0.12 Workstream B): the Files
        // header (and its file-selection mount) renders even with files:[]
        // for a torrent/magnet source, matching the real manifest-pending
        // window (§6.5) where no artifact exists yet.
        current_source_identity: {kind: 'magnet'}})}));
  await page.route(url => url.pathname === '/api/torrents/7/file-selection/confirm', route => {
    state.confirmBody = route.request().postDataJSON();
    if (state.confirmStatus === 409) {
      return route.fulfill({status: 409, contentType: 'application/json',
        body: JSON.stringify({detail: 'Executable manifest already committed'})});
    }
    state.view = selectionView({decision: 'explicit', mutable: true,
      selected_entry_ids: state.confirmBody.entry_ids, auto_offer: false, decision_deadline: null,
      file_selection_affordance: 'change'});
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

test('default draft is ALL; folder tri-state, Select All / Deselect All and counts track the draft', async ({page}) => {
  const state = {view: selectionView()};
  await stub(page, state);
  await boot(page);
  await openViaEvent(page);

  const count = page.locator('.dp-fs-count');
  await expect(count).toHaveText('4 of 4 files selected');
  await expect(page.locator('.dp-fs-toggle-all')).toHaveText('Deselect All');

  // Uncheck one file in "Season 1" → that folder becomes indeterminate and the
  // state-aware toggle flips to "Select All".
  await page.locator('.dp-fs-check--file[data-entry-id="e-s1e01"]').uncheck();
  await expect(count).toHaveText('3 of 4 files selected');
  const seasonFolder = page.locator('.dp-fs-check--folder[data-folder-path="Season 1"]');
  await expect(seasonFolder).toHaveJSProperty('indeterminate', true);
  await expect(page.locator('.dp-fs-toggle-all')).toHaveText('Select All');

  // Select All → 4/4, then Deselect All → 0/4, Confirm disabled.
  await page.locator('.dp-fs-toggle-all').click();
  await expect(count).toHaveText('4 of 4 files selected');
  await expect(page.locator('.dp-fs-toggle-all')).toHaveText('Deselect All');
  await page.locator('.dp-fs-toggle-all').click();
  await expect(count).toHaveText('0 of 4 files selected');
  await expect(page.locator('#modal-footer .dp-fs-confirm')).toBeDisabled();
  await expect(page.locator('.dp-fs-toggle-all')).toHaveText('Select All');

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

test('after Confirm the settled state never recreates a countdown or re-opens the selector', async ({page}) => {
  // TASK_File_Selection_Hold_Release_Correction §16 — the corrected backend
  // returns decision:"explicit"/auto_offer:false/decision_deadline:null for a
  // settled selection. Given that authoritative response the browser must not
  // reconstruct the 120s countdown or wait it out before reflecting the
  // transfer, even if a stale SSE offer notification arrives afterwards.
  const state = {view: selectionView({server_now: 1000.0, decision_deadline: 1120.0})};
  await stub(page, state);
  await boot(page);
  await openViaEvent(page);
  await expect(page.locator('#modal-footer .dp-fs-foot-countdown')).toBeVisible();

  await page.locator('#modal-footer .dp-fs-confirm').click();
  await expect(page.locator('#overlay')).not.toHaveClass(/\bopen\b/);
  // stub() flipped state.view to the settled explicit read model.
  expect(state.view.decision).toBe('explicit');
  expect(state.view.decision_deadline).toBeNull();
  expect(state.view.auto_offer).toBe(false);

  // A late/duplicate SSE offer for the same transfer must be a no-op now.
  await page.evaluate(() => document.dispatchEvent(
    new CustomEvent('debridpulse:file-selection-available', {detail: {transfer_id: 7}})));
  await page.waitForTimeout(400);
  await expect(page.locator('#overlay')).not.toHaveClass(/\bopen\b/);
  await expect(page.locator('#modal-footer .dp-fs-foot-countdown')).toHaveCount(0);

  // Re-opening Details on the settled selection shows a passive summary, no timer.
  await page.evaluate(() => window.DPFileSelection.pollOffers());
  await page.waitForTimeout(200);
  await expect(page.locator('#modal[data-dp-modal-mode="file-selection"]')).toHaveCount(0);
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
    auto_offer: false, decision_deadline: null, file_selection_affordance: 'none'});
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
    decision_reason: 'single_file', auto_offer: false, file_selection_affordance: 'none'})};
  await stub(page, state);
  await boot(page);
  await page.evaluate(() => document.dispatchEvent(
    new CustomEvent('debridpulse:file-selection-available', {detail: {transfer_id: 7}})));
  await page.waitForTimeout(400);
  await expect(page.locator('#overlay')).not.toHaveClass(/\bopen\b/);
});

test('the browser never auto-opens the selector when the core reports auto_offer=false', async ({page}) => {
  // The Universal Transfer Core is the sole authority for whether the selector
  // may auto-present. The browser presentation owner obeys auto_offer and never
  // re-derives a window of its own.
  const state = {view: selectionView({auto_offer: false, auto_offer_until: null, decision_deadline: null,
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

function detailFileSelectionMount(page) {
  return page.locator('[data-dp-file-selection-mount][data-dp-transfer-id="7"]');
}

test('Details Files header exposes "Choose Files" while mutable and pending, and opens the selector', async ({page}) => {
  const state = {view: selectionView({auto_offer: false})};
  await stub(page, state);
  await boot(page);
  await page.evaluate(() => showDetail(7));
  await expect(page.locator('#overlay')).toHaveClass(/\bopen\b/);
  // The action lives in the Files-section header, right side -- the same
  // slot pattern the group-switch launcher uses -- never the old generic
  // top-of-Details host.
  await expect(page.locator('#dp-detail-actions')).toHaveCount(0);
  const entry = detailFileSelectionMount(page).locator('.dp-file-selection-entry');
  await expect(entry).toHaveText('Choose Files');
  await entry.click();
  await expect(page.locator('#modal[data-dp-modal-mode="file-selection"]')).toBeVisible();
  await expect(page.locator('.dp-fs-tree .dp-fs-check--file')).toHaveCount(4);
});

test('Details Files header exposes "Change Files" once an explicit subset is confirmed', async ({page}) => {
  const state = {view: selectionView({decision: 'explicit', auto_offer: false, decision_deadline: null,
    selected_entry_ids: ['e-s1e01', 'e-s1e02'], file_selection_affordance: 'change'})};
  await stub(page, state);
  await boot(page);
  await page.evaluate(() => showDetail(7));
  await expect(detailFileSelectionMount(page).locator('.dp-file-selection-entry')).toHaveText('Change Files');
});

test('a locked selection shows a passive summary and no mutation control', async ({page}) => {
  const state = {view: selectionView({decision: 'explicit', mutable: false, auto_offer: false,
    decision_deadline: null, selected_entry_ids: ['e-s1e01', 'e-s1e02'], file_selection_affordance: 'none'})};
  await stub(page, state);
  await boot(page);
  await page.evaluate(() => showDetail(7));
  await expect(detailFileSelectionMount(page).locator('.dp-file-selection-summary')).toHaveText('2 of 4 files selected');
  await expect(detailFileSelectionMount(page).locator('.dp-file-selection-entry')).toHaveCount(0);
});

test('a non-eligible transfer shows no file-selection control in Details', async ({page}) => {
  const state = {view: {eligible: false}};
  await stub(page, state);
  await boot(page);
  await page.evaluate(() => showDetail(7));
  await expect(page.locator('#overlay')).toHaveClass(/\bopen\b/);
  await expect(detailFileSelectionMount(page)).toBeEmpty();
});

// DP 1.0.13 collection acquisition: a collection of independent members
// (``explicit_only``) starts with every displayed entry checked, but that draft
// is not consent -- only Confirm commits it. There is no ALL countdown, the
// backdrop and Escape do nothing, and X, Close and Cancel Transfer all cancel
// the transfer. A source larger than its bounded snapshot is stated.
const MEMBERS = [
  {entry_id: 'm-1', name: 'Song 1 [a1].webm', relative_path: 'Song 1 [a1].webm', size_bytes: 0},
  {entry_id: 'm-2', name: 'Song 2 [b2].webm', relative_path: 'Song 2 [b2].webm', size_bytes: 0},
  {entry_id: 'm-3', name: 'Song 3 [c3].webm', relative_path: 'Song 3 [c3].webm', size_bytes: 0},
];

function collectionView(overrides) {
  return selectionView(Object.assign({entries: MEMBERS, file_count: MEMBERS.length, total_size_bytes: 0,
    explicit_only: true, source_truncated: true, source_total: null,
    decision_deadline: null, auto_offer_until: null}, overrides || {}));
}

for (const theme of ['dark', 'light']) {
  test(`a collection starts all checked without committing anything, has no countdown, and ignores backdrop, Escape and time (${theme})`,
    async ({page}) => {
      const state = {view: collectionView()};
      await stub(page, state);
      await boot(page);
      if (theme === 'light') await page.evaluate(() => document.body.classList.add('light'));
      await openViaEvent(page);
      await expect(page.locator('#modal-title')).toHaveText('Select entries');
      await expect(page.locator('.dp-fs-tree')).toHaveAttribute('aria-label', 'Entries in this collection');
      await expect(page.locator('.dp-fs-check--file:checked')).toHaveCount(3);
      await expect(page.locator('.dp-fs-count')).toHaveText('3 of 3 entries selected');
      await expect(page.locator('.dp-fs-toggle-all')).toHaveText('Deselect All');
      await expect(page.locator('#modal-footer .dp-fs-confirm')).toBeEnabled();
      await expect(page.locator('#modal-footer .dp-fs-foot-countdown')).toBeHidden();
      const note = page.locator('#modal-footer .dp-fs-foot-note');
      await expect(note).toHaveText('Confirm downloads only the selected entries. Nothing downloads until you '
        + 'confirm; Close or Cancel Transfer cancels the entire transfer.');
      await expect(note).toHaveCSS('text-align', 'center');
      const frame = await page.locator('#modal').boundingBox();
      const noteBox = await note.boundingBox();
      expect(Math.abs((noteBox.x + noteBox.width / 2) - (frame.x + frame.width / 2))).toBeLessThanOrEqual(1);
      const notice = page.locator('.dp-fs-notice');
      await expect(notice).toHaveText('Only the first 3 entries are shown and can be selected. This collection has '
        + 'more entries; its total length is unknown; later entries are not shown or downloaded.');
      await expect(notice).toHaveAttribute('role', 'note');
      // Blocking: the backdrop, Escape and waiting decide nothing and send nothing.
      await page.locator('#overlay').click({position: {x: 4, y: 4}});
      await page.keyboard.press('Escape');
      await page.waitForTimeout(2500);
      await expect(page.locator('#overlay')).toHaveClass(/\bopen\b/);
      await expect(page.locator('.dp-fs-check--file:checked')).toHaveCount(3);
      expect(state.confirmBody).toBeUndefined();
      expect(state.dismissBody).toBeUndefined();
      expect(state.cancelHit).toBeUndefined();
    });
}

for (const [name, closer] of [['X', '#modal .modal-close'], ['Close', '#modal-footer .dp-fs-close'],
  ['Cancel Transfer', '#modal-footer .dp-fs-cancel']]) {
  test(`${name} cancels a pending collection transfer once, committing nothing and leaving nothing pending`, async ({page}) => {
    const state = {view: collectionView()};
    await stub(page, state);
    await boot(page);
    await openViaEvent(page);
    await page.locator(closer).dblclick();
    await expect(page.locator('#overlay')).not.toHaveClass(/\bopen\b/);
    await expect.poll(() => state.cancelHit).toBe(1);
    await expect(page.locator('.toast')).toContainText('Transfer cancelled');
    await page.waitForTimeout(300);
    expect(state.cancelHit).toBe(1);
    expect(state.confirmBody).toBeUndefined();
    expect(state.dismissBody).toBeUndefined();
  });
}

test('a collection confirms exactly the entries chosen, keeps edits across a re-render, and a stated source total is shown', async ({page}) => {
  const state = {view: collectionView({source_total: 342}), confirmStatus: 409};
  await stub(page, state);
  await boot(page);
  await openViaEvent(page);
  await expect(page.locator('.dp-fs-notice')).toContainText('This collection has 342 entries');
  await page.locator('.dp-fs-check--file[data-entry-id="m-2"]').uncheck();
  await expect(page.locator('.dp-fs-count')).toHaveText('2 of 3 entries selected');
  // A refused Confirm re-reads authority and re-renders: the deliberate edit stays.
  await page.locator('#modal-footer .dp-fs-confirm').click();
  await expect(page.locator('.toast')).toContainText(/selection changed|already started/i);
  await expect(page.locator('#modal[data-dp-modal-mode="file-selection"]')).toBeVisible();
  await expect(page.locator('.dp-fs-check--file[data-entry-id="m-2"]')).not.toBeChecked();
  await expect(page.locator('.dp-fs-count')).toHaveText('2 of 3 entries selected');
  await expect(page.locator('.dp-fs-toggle-all')).toHaveText('Select All');
  state.confirmStatus = 200;
  await page.locator('#modal-footer .dp-fs-confirm').click();
  await expect.poll(() => state.confirmBody).toEqual({manifest_id: MANIFEST_A, entry_ids: ['m-1', 'm-3']});
  await expect(page.locator('.toast').last()).toContainText('2 entries selected for download');
});

test('a collection needs a positive explicit selection: no entries checked disables Confirm', async ({page}) => {
  const state = {view: collectionView({entries: [MEMBERS[0]], file_count: 1, source_truncated: false})};
  await stub(page, state);
  await boot(page);
  await openViaEvent(page);
  await expect(page.locator('.dp-fs-notice')).toHaveCount(0);
  await expect(page.locator('.dp-fs-check--file:checked')).toHaveCount(1);
  await page.locator('.dp-fs-toggle-all').click();
  await expect(page.locator('.dp-fs-count')).toHaveText('0 of 1 entries selected');
  await expect(page.locator('#modal-footer .dp-fs-confirm')).toBeDisabled();
  await page.locator('#modal-footer .dp-fs-confirm').click({force: true});
  await page.waitForTimeout(300);
  expect(state.confirmBody).toBeUndefined();
});

test('after a reload a pending collection is offered afresh: all checked again, nothing committed by the reload', async ({page}) => {
  const state = {view: collectionView(), offers: [{transfer_id: 7, manifest_id: MANIFEST_A, file_count: 3}]};
  await stub(page, state);
  await boot(page);
  await expect(page.locator('#modal[data-dp-modal-mode="file-selection"]')).toBeVisible();
  await page.locator('.dp-fs-check--file[data-entry-id="m-1"]').uncheck();
  await page.reload();
  await page.waitForFunction(() => Boolean(window.DPFileSelection && window.DPModal));
  await expect(page.locator('#modal[data-dp-modal-mode="file-selection"]')).toBeVisible();
  await expect(page.locator('.dp-fs-check--file:checked')).toHaveCount(3);
  expect(state.confirmBody).toBeUndefined();
  expect(state.dismissBody).toBeUndefined();
  expect(state.cancelHit).toBeUndefined();
});

// The one shared Select All / Deselect All, for both selectors: every
// selectable entry checked reads Deselect All and unchecks them; otherwise it
// reads Select All and checks them. One box for both labels, in its place.
for (const kind of ['torrent', 'collection']) {
  test(`the shared toggle tracks the selection and never resizes or moves (${kind})`, async ({page}) => {
    const state = {view: kind === 'torrent' ? selectionView() : collectionView()};
    await stub(page, state);
    await boot(page);
    await openViaEvent(page);
    const toggle = page.locator('.dp-fs-toolbar > .dp-fs-toggle-all');
    const checks = page.locator('.dp-fs-check--file');
    const total = await checks.count();
    const boxes = [];
    const measure = async () => boxes.push(JSON.stringify(await toggle.boundingBox()));
    await expect(toggle).toHaveText('Deselect All');
    await measure();
    await checks.first().uncheck();                                             // partial
    await expect(toggle).toHaveText('Select All');
    await measure();
    await checks.first().check();                                               // all again
    await expect(toggle).toHaveText('Deselect All');
    await toggle.click();                                                       // → none
    await expect(page.locator('.dp-fs-check--file:checked')).toHaveCount(0);
    await expect(toggle).toHaveText('Select All');
    await measure();
    await toggle.click();                                                       // → all
    await expect(page.locator('.dp-fs-check--file:checked')).toHaveCount(total);
    await expect(toggle).toHaveText('Deselect All');
    await measure();
    expect(new Set(boxes).size).toBe(1);
  });
}

test('a disabled row is never counted or changed by the toggle', async ({page}) => {
  const state = {view: collectionView()};
  await stub(page, state);
  await boot(page);
  await openViaEvent(page);
  const toggle = page.locator('.dp-fs-toggle-all');
  const blocked = page.locator('.dp-fs-check--file[data-entry-id="m-3"]');
  await blocked.uncheck();
  // No current source renders an ineligible row; the rule is the HTML one --
  // a disabled checkbox is not selectable -- so the case is made in the page.
  await blocked.evaluate(node => { node.disabled = true; });
  await page.locator('.dp-fs-check--file[data-entry-id="m-1"]').uncheck();
  await page.locator('.dp-fs-check--file[data-entry-id="m-1"]').check();
  await expect(toggle).toHaveText('Deselect All');                              // every eligible row checked
  await toggle.click();
  await expect(page.locator('.dp-fs-check--file:checked')).toHaveCount(0);
  await expect(toggle).toHaveText('Select All');
  await toggle.click();
  await expect(page.locator('.dp-fs-check--file:checked')).toHaveCount(2);
  await expect(blocked).not.toBeChecked();
  await expect(toggle).toHaveText('Deselect All');
});

test('the collection selector is keyboard-complete: modal semantics, focus moves in and stays in, Escape is inert', async ({page}) => {
  const state = {view: collectionView({entries: [MEMBERS[0]], file_count: 1, source_truncated: false})};
  await stub(page, state);
  await boot(page);
  await openViaEvent(page);
  const modalNode = page.locator('#modal');
  await expect(modalNode).toHaveAttribute('role', 'dialog');
  await expect(modalNode).toHaveAttribute('aria-modal', 'true');
  await expect(modalNode).toHaveAttribute('aria-labelledby', 'modal-title');
  await expect(modalNode).toBeFocused();
  await expect(page.locator('#modal .modal-close')).toHaveAttribute('aria-label', 'Cancel transfer');
  const order = [];
  for (let step = 0; step < 8; step += 1) {
    await page.keyboard.press('Tab');
    order.push(await page.evaluate(() => {
      const node = document.activeElement;
      if (!node.closest('#modal')) return 'OUTSIDE';
      return node.getAttribute('aria-label') || node.dataset.entryId || node.textContent.trim();
    }));
  }
  expect(order).toEqual(['Cancel transfer', 'Deselect All', 'm-1', 'Cancel Transfer', 'Close', 'Confirm',
    'Cancel transfer', 'Deselect All']);
  await page.keyboard.press('Shift+Tab');
  await expect(page.locator('#modal .modal-close')).toBeFocused();
  await page.keyboard.press('Shift+Tab');                                      // wraps backwards
  await expect(page.locator('#modal-footer .dp-fs-confirm')).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.locator('#overlay')).toHaveClass(/\bopen\b/);
  await page.locator('#modal-footer .dp-fs-cancel').focus();
  await page.keyboard.press('Enter');
  await expect.poll(() => state.cancelHit).toBe(1);
  await expect(page.locator('#overlay')).not.toHaveClass(/\bopen\b/);
  expect(await page.locator('#modal').getAttribute('tabindex')).toBeNull();
});

test('the torrent selector keeps its lifecycle: all checked, countdown, dismissal by backdrop / X / Close, no focus capture', async ({page}) => {
  const state = {view: selectionView()};
  await stub(page, state);
  await boot(page);
  await openViaEvent(page);
  await expect(page.locator('.dp-fs-check--file:checked')).toHaveCount(4);
  await expect(page.locator('#modal-footer .dp-fs-foot-countdown')).toBeVisible();
  await expect(page.locator('#modal-footer .dp-fs-foot-note')).not.toHaveCSS('text-align', 'center');
  await expect(page.locator('#modal .modal-close')).toHaveAttribute('aria-label', 'Close file selection');
  expect(await page.locator('#modal').getAttribute('tabindex')).toBeNull();
  await expect(page.locator('#modal')).not.toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.locator('#overlay')).toHaveClass(/\bopen\b/);             // unchanged: Escape was never wired
  await page.locator('#overlay').click({position: {x: 4, y: 4}});
  await expect(page.locator('#overlay')).not.toHaveClass(/\bopen\b/);
  await expect.poll(() => state.dismissBody).toEqual({manifest_id: MANIFEST_A});
  expect(state.cancelHit).toBeUndefined();
});
