const { test, expect } = require('@playwright/test');

// DP 1.0.12 Workstream B — cross-surface file-selection affordance regression
// (task Section 13). Exercises the bounded list hint (file_selection_affordance
// on /api/torrents rows) plus the fresh-click authority read
// (/api/torrents/{id}/file-selection) across all three surfaces: Details Files
// header, Dashboard Recent, and Downloads.

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

const ENTRIES = [
  {entry_id: 'e1', name: 'e01.mkv', relative_path: 'e01.mkv', size_bytes: 1000},
  {entry_id: 'e2', name: 'e02.mkv', relative_path: 'e02.mkv', size_bytes: 2000},
  {entry_id: 'e3', name: 'e03.mkv', relative_path: 'e03.mkv', size_bytes: 3000},
];

function baseRow(overrides) {
  return Object.assign({
    id: 501,
    name: 'Example Torrent',
    display_name: 'Example Torrent',
    hash: 'hash-501',
    status: 'downloading',
    progress: 10,
    size_bytes: 1024 * 1024,
    created_at: '2026-09-01T00:00:00Z',
    completed_at: null,
    source: 'magnet',
    label: null,
    current_provider_id: 'alldebrid',
    current_provider_name: 'AllDebrid',
    delivering_provider_id: null,
    delivering_provider_name: null,
    provider_provenance_status: 'pending',
    extraction_status: null,
    source_failure_count: 0,
    current_source_identity: {kind: 'magnet'},
    common_candidate_count: 0,
    group_remaining_count: 0,
    candidate_action_scope: 'none',
    candidate_action_count: 0,
    candidate_action_artifact_id: null,
    file_selection_affordance: 'none',
  }, overrides || {});
}

async function installListFixture(page, row) {
  await page.route('**/api/torrents**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname !== '/api/torrents' || request.method() !== 'GET') return route.fallback();
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({items: [row], total: 1}),
    });
  });
}

async function installSelectionFixture(page, transferId, state) {
  await page.route(url => url.pathname === `/api/torrents/${transferId}/file-selection`, route =>
    route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(state.view)}));
}

async function installDetailFixture(page, transferId, row) {
  await page.route(url => url.pathname === `/api/torrents/${transferId}`, route =>
    route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(Object.assign({
      files: [], events: [], route_attempts: [], execution_attempts: [], executors: [], source_outcomes: [],
    }, row))}));
}

function pendingView() {
  return {eligible: true, mutable: true, manifest_id: null, decision: 'pending',
    decision_reason: null, file_count: 0, total_size_bytes: 0, entries: [],
    selected_entry_ids: [], auto_offer: false, auto_offer_until: null,
    decision_deadline: null, initially_available: true, server_now: 1000.0};
}
function chooseView() {
  return {eligible: true, mutable: true, manifest_id: 'm-1', decision: 'pending',
    decision_reason: null, file_count: ENTRIES.length,
    total_size_bytes: ENTRIES.reduce((s, e) => s + e.size_bytes, 0), entries: ENTRIES,
    selected_entry_ids: [], auto_offer: false, auto_offer_until: null,
    decision_deadline: 1120.0, initially_available: true, server_now: 1000.0};
}
function changeView() {
  return Object.assign(chooseView(), {decision: 'explicit', selected_entry_ids: ['e1', 'e2']});
}
function lockedView() {
  return Object.assign(chooseView(), {decision: 'explicit', mutable: false,
    selected_entry_ids: ['e1', 'e2'], decision_deadline: null});
}

async function boot(page) {
  await page.goto('/');
  await page.waitForFunction(() => Boolean(window.DPFileSelection && window.DPModal));
}

function dashboardChip(page, id) {
  return page.locator(`#dash-tbody tr[data-torrent-id="${id}"] [data-dp-file-selection-chip]`);
}
function downloadsChip(page, id) {
  return page.locator(`#t-tbody tr[data-torrent-id="${id}"] [data-dp-file-selection-chip]`);
}

// ── Case A: manifest available, initial/default selection ──────────────────

test('Case A: Dashboard and Downloads render a glyph-only "Choose Files" chip', async ({page}) => {
  const row = baseRow({file_selection_affordance: 'choose'});
  await isolateExternalFonts(page);
  await installListFixture(page, row);
  await installSelectionFixture(page, 501, {view: chooseView()});
  await installDetailFixture(page, 501, row);
  await boot(page);

  const dashChip = dashboardChip(page, 501);
  await expect(dashChip).toHaveCount(1);
  await expect(dashChip).toHaveJSProperty('tagName', 'BUTTON');
  await expect(dashChip).toHaveAttribute('title', 'Choose Files');
  await expect(dashChip).toHaveAttribute('aria-label', 'Choose Files');
  await expect(dashChip).toHaveText('');   // no visible text
  await expect(dashChip.locator('.dp-candidate-chip-count')).toHaveCount(0);   // no count

  await page.locator('.nav-item[data-view="torrents"]').click();
  await expect(page.locator('#view-torrents')).toHaveClass(/active/);
  await expect(downloadsChip(page, 501)).toHaveCount(1);
  const dlChip = downloadsChip(page, 501);
  await expect(dlChip).toHaveAttribute('title', 'Choose Files');
  await expect(dlChip).toHaveText('');

  // Details Files header shows the exact short-form label.
  await page.evaluate(() => showDetail(501));
  const detailEntry = page.locator('[data-dp-file-selection-mount][data-dp-transfer-id="501"] .dp-file-selection-entry');
  await expect(detailEntry).toHaveText('Choose Files');
});

// ── Case B: explicit subset already exists, still mutable ──────────────────

test('Case B: "Change Files" wording on all three surfaces', async ({page}) => {
  const row = baseRow({file_selection_affordance: 'change'});
  await isolateExternalFonts(page);
  await installListFixture(page, row);
  await installSelectionFixture(page, 501, {view: changeView()});
  await installDetailFixture(page, 501, row);
  await boot(page);

  const dashChip = dashboardChip(page, 501);
  await expect(dashChip).toHaveAttribute('title', 'Change Files');
  await expect(dashChip).toHaveAttribute('aria-label', 'Change Files');
  await expect(dashChip).toHaveText('');

  await page.evaluate(() => showDetail(501));
  await expect(page.locator('[data-dp-file-selection-mount][data-dp-transfer-id="501"] .dp-file-selection-entry'))
    .toHaveText('Change Files');
});

// ── Case C: manifest not available yet ──────────────────────────────────────

test('Case C: pending-manifest click shows the approved informational copy, no tree/Confirm/countdown', async ({page}) => {
  const row = baseRow({file_selection_affordance: 'pending_manifest'});
  await isolateExternalFonts(page);
  await installListFixture(page, row);
  const state = {view: pendingView()};
  await installSelectionFixture(page, 501, state);
  await installDetailFixture(page, 501, row);
  await boot(page);

  const dashChip = dashboardChip(page, 501);
  await expect(dashChip).toHaveCount(1);
  await dashChip.click();

  await expect(page.locator('#overlay')).toHaveClass(/\bopen\b/);
  await expect(page.locator('#modal-title')).toHaveText('File list not available yet');
  await expect(page.locator('#modal-body')).toContainText(
    'DebridPulse is still waiting for the file list for this torrent. ' +
    'The transfer will continue preparing in the background. Try again shortly.');
  await expect(page.locator('.dp-fs-tree')).toHaveCount(0);
  await expect(page.locator('#modal-footer')).toBeHidden();
  await expect(page.locator('.dp-fs-cancel')).toHaveCount(0);
  await expect(page.locator('.dp-fs-clock')).toHaveCount(0);
});

// ── Case D: manifest becomes available after a pending state ───────────────

test('Case D: after the manifest arrives, the same affordance opens the real selector', async ({page}) => {
  const row = baseRow({file_selection_affordance: 'pending_manifest'});
  await isolateExternalFonts(page);
  await installListFixture(page, row);
  const state = {view: pendingView()};
  await installSelectionFixture(page, 501, state);
  await installDetailFixture(page, 501, row);
  await boot(page);

  await dashboardChip(page, 501).click();
  await expect(page.locator('#modal-title')).toHaveText('File list not available yet');
  await page.locator('#modal .modal-close').click();
  await expect(page.locator('#overlay')).not.toHaveClass(/\bopen\b/);

  // The manifest has since arrived -- the fresh authoritative read now
  // reports a usable, mutable, multi-file manifest.
  state.view = chooseView();
  await dashboardChip(page, 501).click();
  await expect(page.locator('#modal[data-dp-modal-mode="file-selection"]')).toBeVisible();
  await expect(page.locator('.dp-fs-tree .dp-fs-check--file')).toHaveCount(3);
});

// ── Case E: selection locked/materialized ───────────────────────────────────

test('Case E: a locked selection shows no actionable control on any surface', async ({page}) => {
  const row = baseRow({file_selection_affordance: 'none', status: 'downloading'});
  await isolateExternalFonts(page);
  await installListFixture(page, row);
  await installSelectionFixture(page, 501, {view: lockedView()});
  await installDetailFixture(page, 501, row);
  await boot(page);

  await expect(dashboardChip(page, 501)).toHaveCount(0);
  await page.evaluate(() => showDetail(501));
  const mount = page.locator('[data-dp-file-selection-mount][data-dp-transfer-id="501"]');
  await expect(mount.locator('.dp-file-selection-entry')).toHaveCount(0);
  // A truthful passive summary is acceptable (§6.9) -- never a dead button.
  await expect(mount.locator('.dp-file-selection-summary')).toHaveText('2 of 3 files selected');
});

// ── Case F: single-file torrent/magnet ──────────────────────────────────────

test('Case F: a single-file torrent has no file-selection action anywhere', async ({page}) => {
  const row = baseRow({file_selection_affordance: 'none'});
  await isolateExternalFonts(page);
  await installListFixture(page, row);
  await installSelectionFixture(page, 501, {
    view: {eligible: true, mutable: false, manifest_id: 'm-1', decision: 'all',
      decision_reason: 'single_file', file_count: 1, total_size_bytes: 1000,
      entries: [ENTRIES[0]], selected_entry_ids: ['e1'], auto_offer: false,
      auto_offer_until: null, decision_deadline: null, initially_available: true, server_now: 1000.0},
  });
  await installDetailFixture(page, 501, row);
  await boot(page);

  await expect(dashboardChip(page, 501)).toHaveCount(0);
  await page.evaluate(() => showDetail(501));
  const mount = page.locator('[data-dp-file-selection-mount][data-dp-transfer-id="501"]');
  await expect(mount.locator('.dp-file-selection-entry')).toHaveCount(0);
  await expect(mount.locator('.dp-file-selection-summary')).toHaveCount(0);
});

// ── Case G: stale list hint ──────────────────────────────────────────────────

test('Case G: a stale "choose" hint does not open a stale picker once locked server-side', async ({page}) => {
  const row = baseRow({file_selection_affordance: 'choose'});
  await isolateExternalFonts(page);
  await installListFixture(page, row);
  const state = {view: lockedView()};   // authoritative state has already moved on
  await installSelectionFixture(page, 501, state);
  await installDetailFixture(page, 501, row);
  await boot(page);

  const chip = dashboardChip(page, 501);
  await expect(chip).toHaveCount(1);   // the bounded hint still says "choose"
  await chip.click();

  // Fresh click authority: never opens the picker on stale hint truth.
  await expect(page.locator('#modal[data-dp-modal-mode="file-selection"]')).toHaveCount(0);
  await expect(page.locator('.toast')).toContainText('no longer available');
});

// ── Case H: row-click ownership ─────────────────────────────────────────────

test('Case H: clicking the folder chip in Recent/Downloads opens only file selection, never Details', async ({page}) => {
  const row = baseRow({file_selection_affordance: 'choose'});
  await isolateExternalFonts(page);
  await installListFixture(page, row);
  await installSelectionFixture(page, 501, {view: chooseView()});
  await installDetailFixture(page, 501, row);
  await boot(page);

  await dashboardChip(page, 501).click();
  await expect(page.locator('#modal[data-dp-modal-mode="file-selection"]')).toBeVisible();
  // Not the generic Details modal.
  await expect(page.locator('#modal[data-dp-modal-mode="details"]')).toHaveCount(0);
});

// ── Case I: Candidates isolation ────────────────────────────────────────────

test('Case I: a file-selection row never renders the Candidates launcher, and vice versa', async ({page}) => {
  const fileSelectionRow = baseRow({file_selection_affordance: 'choose', common_candidate_count: 0});
  await isolateExternalFonts(page);
  await installListFixture(page, fileSelectionRow);
  await installSelectionFixture(page, 501, {view: chooseView()});
  await installDetailFixture(page, 501, fileSelectionRow);
  await boot(page);

  await expect(dashboardChip(page, 501)).toHaveCount(1);
  await expect(page.locator('#dash-tbody tr[data-torrent-id="501"] .dp-candidate-chip')).toHaveCount(0);
});

test('Case I (converse): a generic candidate-capable row never gains a file-selection chip', async ({page}) => {
  const genericRow = baseRow({
    id: 777, current_source_identity: {kind: 'host', host: '1fichier.com'},
    file_selection_affordance: 'none', common_candidate_count: 2, group_remaining_count: 1,
    candidate_action_scope: 'group', candidate_action_count: 2,
  });
  await isolateExternalFonts(page);
  await installListFixture(page, genericRow);
  await boot(page);

  await expect(dashboardChip(page, 777)).toHaveCount(0);
});
