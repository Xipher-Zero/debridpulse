const { test, expect } = require('@playwright/test');

/* DP 1.0.13: every operator-forced source switch -- the Details per-file
 * disclosure, and the shared chooser launched from Details, Dashboard Recent
 * Activity and Downloads (artifact and group scope) -- speaks ONE discard
 * protocol (ui-group-candidates.js). A switch that would discard downloaded
 * progress is refused first (409 discard_material) with nothing changed;
 * Cancel switches nothing, Confirm retries the exact candidate with the
 * confirmed consequence. A group operation previews every move BEFORE any
 * move and asks once. All backend responses are mocked here. */

const MIB = 1024 * 1024;

async function ready(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({ status: 200, contentType: 'text/css', body: '' }));
  await page.goto('/');
  await page.waitForFunction(() => Boolean(window.DPGroupCandidates && window.DPDownloads && window.DPSettingsModal));
}

function sc(host, candidateId, { selected = false, eligible = false } = {}) {
  return { source_host: host, candidate_id: candidateId, is_selected: selected, switch_eligible: eligible };
}

function file(id, sourceCandidates) {
  return {
    id, filename: `file-${id}.mkv`, size_bytes: 8 * MIB, status: 'downloading', blocked: false, block_reason: null,
    candidate_count: sourceCandidates.length,
    acquisition_candidates: sourceCandidates.map(entry => ({
      candidate_id: entry.candidate_id, source_label: entry.source_host, provider_id: 'alldebrid',
      relationship: 'Original', dispositions: entry.is_selected ? ['Active'] : [], is_selected: entry.is_selected,
      is_active: entry.is_selected, is_delivering: false, switch_eligible: entry.switch_eligible,
    })),
    source_candidates: sourceCandidates,
  };
}

function detail(id, files) {
  return {
    id, name: `Discard fixture ${id}`, status: 'downloading', progress: 40, size_bytes: 8 * MIB * files.length,
    source: 'direct_link', label: '', hash: '', created_at: '2026-09-27T10:00:00Z',
    current_provider_id: 'alldebrid', current_provider_name: 'AllDebrid',
    route_attempts: [], execution_attempts: [], executors: ['aria2'], source_outcomes: [], events: [], files,
  };
}

function scopedItem(id, scope, opts = {}) {
  return {
    id, name: `Discard fixture ${id}`, display_name: `Discard fixture ${id}`, hash: `direct:${id}`,
    status: 'downloading', progress: 40, size_bytes: 8 * MIB, created_at: '2026-09-27 10:00:00',
    current_source_identity: { kind: 'host', host: 'rapidgator.net' },
    current_provider_id: 'alldebrid', current_provider_name: 'AllDebrid',
    delivering_provider_id: 'alldebrid', delivering_provider_name: 'AllDebrid',
    provider_provenance_status: 'recorded', source: 'direct_link',
    common_candidate_count: opts.commonCount || 0, group_remaining_count: opts.remaining || 0,
    candidate_action_scope: scope, candidate_action_count: opts.count || 0,
    candidate_action_artifact_id: opts.artifactId != null ? opts.artifactId : null,
  };
}

const refusal = (discarded, retained = 0, generation = 3, changed = false) => ({
  status: 409, contentType: 'application/json',
  body: JSON.stringify({ detail: {
    confirmation: 'discard_material', discarded_bytes: discarded, retained_bytes: retained,
    material_generation: generation, changed,
    message: changed ? 'Downloaded progress changed after the switch was confirmed; nothing was switched.'
      : 'Switching to this source discards downloaded progress.',
  } }),
});
const switched = (artifactId, candidateId, host) => ({
  status: 200, contentType: 'application/json',
  body: JSON.stringify({ ok: true, artifact_id: artifactId, filename: `file-${artifactId}.mkv`, candidate_id: candidateId, source_host: host }),
});

// One transfer, list rows + fresh Details, and a recorder for every preview
// and switch request in the order they were issued.
async function fixture(page, transferId, items, files, onPost, previews = {}) {
  const log = [];
  await page.route('**/api/torrents*', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items, total: items.length }) }));
  await page.route(url => url.pathname === `/api/torrents/${transferId}`, route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(detail(transferId, files)) }));
  await page.route(url => /\/candidate\/preview$/.test(url.pathname), route => {
    const artifactId = Number(route.request().url().match(/artifacts\/(\d+)\//)[1]);
    const candidateId = new URL(route.request().url()).searchParams.get('candidate_id');
    log.push({ kind: 'preview', artifactId, candidateId });
    const [discarded, retained] = previews[artifactId] || [0, 0];
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
      transfer_id: transferId, artifact_id: artifactId, candidate_id: candidateId, filename: `file-${artifactId}.mkv`,
      discarded_bytes: discarded, retained_bytes: retained, material_generation: 7,
    }) });
  });
  await page.route(url => /\/artifacts\/\d+\/candidate$/.test(url.pathname), route => {
    const artifactId = Number(route.request().url().match(/artifacts\/(\d+)\//)[1]);
    const body = route.request().postDataJSON();
    const posts = log.filter(entry => entry.kind === 'post');
    log.push({ kind: 'post', artifactId, body });
    return route.fulfill(onPost(artifactId, body, posts.length));
  });
  return log;
}

const posts = log => log.filter(entry => entry.kind === 'post');
const dialog = page => page.locator('.dp-modal-overlay .dp-modal-dialog');

async function toDownloads(page) {
  await page.evaluate(async () => { nav(document.querySelector('[data-view="torrents"]')); await loadTorrents(); });
}

const oneArtifact = [file(5001, [sc('rapidgator.net', 'rg-1', { selected: true }), sc('mega.nz', 'mg-1', { eligible: true })])];

// ── Details per-file candidate disclosure ─────────────────────────────────

test('Details disclosure: Cancel changes nothing; Confirm retries the exact candidate with the confirmed consequence', async ({ page }) => {
  const log = await fixture(page, 500, [], oneArtifact, (id, body, n) => (n === 0 || n === 1)
    ? refusal(3 * MIB) : switched(id, body.candidate_id, 'mega.nz'));
  await ready(page);
  await page.evaluate(() => showDetail(500));
  await page.locator('tr[data-dp-artifact-id="5001"] .dp-detail-candidate-disclosure').click();
  const switchButton = page.locator('tr[data-dp-candidate-owner="5001"] .dp-detail-candidate-switch');

  await switchButton.click();
  await expect(dialog(page)).toContainText('Discard downloaded progress?');
  await expect(dialog(page)).toContainText('file-5001.mkv');
  await page.locator('.dp-modal-overlay [data-modal-cancel]').click();
  await expect(dialog(page)).toHaveCount(0);
  expect(posts(log)).toEqual([{ kind: 'post', artifactId: 5001, body: { candidate_id: 'mg-1' } }]);
  await expect(page.locator('.toast.error, .toast-error')).toHaveCount(0);

  await page.locator('tr[data-dp-candidate-owner="5001"] .dp-detail-candidate-switch').click();
  await page.locator('.dp-modal-overlay [data-modal-accept]').click();
  await expect.poll(() => posts(log).length).toBe(3);
  expect(posts(log)[2].body).toEqual({
    candidate_id: 'mg-1', discard_confirmed: true, discard_confirmation: { material_generation: 3, retained_bytes: 0 },
  });
  await expect(page.locator('.toast')).toContainText('file-5001.mkv file source switched to mega.nz');
});

// ── Artifact-scoped chooser: Dashboard Recent Activity and Downloads ──────

test('Dashboard Recent artifact chooser: one confirmation naming the file and amount, then the exact confirmed retry', async ({ page }) => {
  const items = [scopedItem(510, 'artifact', { count: 2, artifactId: 5001 })];
  const log = await fixture(page, 510, items, oneArtifact, (id, body, n) => n === 0
    ? refusal(3 * MIB, MIB, 4) : switched(id, body.candidate_id, 'mega.nz'));
  await ready(page);
  await page.locator('#dash-tbody tr[data-torrent-id="510"] .dp-group-candidate-launcher').click();
  await page.locator('.dp-group-candidate-menu .dp-group-candidate-switch').click();

  await expect(dialog(page)).toHaveCount(1);
  await expect(dialog(page)).toContainText('file-5001.mkv');
  await expect(dialog(page)).toContainText('keeps the first');
  await page.locator('.dp-modal-overlay [data-modal-accept]').click();
  await expect.poll(() => posts(log).length).toBe(2);
  expect(posts(log).map(entry => entry.body)).toEqual([
    { candidate_id: 'mg-1' },
    { candidate_id: 'mg-1', discard_confirmed: true, discard_confirmation: { material_generation: 4, retained_bytes: MIB } },
  ]);
  await expect(page.locator('.toast', { hasText: 'switched' })).toContainText('mega.nz');
});

test('Downloads artifact chooser: Cancel switches nothing and reports no error', async ({ page }) => {
  const items = [scopedItem(520, 'artifact', { count: 2, artifactId: 5001 })];
  const log = await fixture(page, 520, items, oneArtifact, () => refusal(2 * MIB));
  await ready(page);
  await toDownloads(page);
  await page.locator('#t-tbody tr[data-torrent-id="520"] .dp-group-candidate-launcher').click();
  await page.locator('.dp-group-candidate-menu .dp-group-candidate-switch').click();
  await expect(dialog(page)).toHaveCount(1);
  await page.locator('.dp-modal-overlay [data-modal-cancel]').click();
  await expect(dialog(page)).toHaveCount(0);
  await page.waitForTimeout(300);
  expect(posts(log)).toHaveLength(1);
  await expect(page.locator('.toast', { hasText: 'Unable to switch' })).toHaveCount(0);
});

test('Downloads artifact chooser: a zero-discard switch never prompts', async ({ page }) => {
  const items = [scopedItem(530, 'artifact', { count: 2, artifactId: 5001 })];
  const log = await fixture(page, 530, items, oneArtifact, (id, body) => switched(id, body.candidate_id, 'mega.nz'));
  await ready(page);
  await toDownloads(page);
  await page.locator('#t-tbody tr[data-torrent-id="530"] .dp-group-candidate-launcher').click();
  await page.locator('.dp-group-candidate-menu .dp-group-candidate-switch').click();
  await expect(page.locator('.toast', { hasText: 'switched' })).toContainText('mega.nz');
  expect(posts(log).map(entry => entry.body)).toEqual([{ candidate_id: 'mg-1' }]);
  await expect(dialog(page)).toHaveCount(0);
});

test('an ordinary failure stays an ordinary error and never opens the discard dialog', async ({ page }) => {
  const items = [scopedItem(540, 'artifact', { count: 2, artifactId: 5001 })];
  const log = await fixture(page, 540, items, oneArtifact, () => ({
    status: 409, contentType: 'application/json',
    body: JSON.stringify({ detail: { category: 'RESOURCE_STATE_CONFLICT', message: 'The artifact is busy.' } }),
  }));
  await ready(page);
  await page.locator('#dash-tbody tr[data-torrent-id="540"] .dp-group-candidate-launcher').click();
  await page.locator('.dp-group-candidate-menu .dp-group-candidate-switch').click();
  await expect(page.locator('.toast', { hasText: 'Unable to switch' })).toContainText('The artifact is busy.');
  await expect(dialog(page)).toHaveCount(0);
  expect(posts(log)).toHaveLength(1);
});

test('a confirmed retry whose consequence changed fails safely: no second prompt, nothing switched', async ({ page }) => {
  const items = [scopedItem(550, 'artifact', { count: 2, artifactId: 5001 })];
  const log = await fixture(page, 550, items, oneArtifact, (id, body, n) => n === 0
    ? refusal(3 * MIB) : refusal(5 * MIB, 0, 4, true));
  await ready(page);
  await page.locator('#dash-tbody tr[data-torrent-id="550"] .dp-group-candidate-launcher').click();
  await page.locator('.dp-group-candidate-menu .dp-group-candidate-switch').click();
  await page.locator('.dp-modal-overlay [data-modal-accept]').click();
  await expect(page.locator('.toast', { hasText: 'Unable to switch' })).toContainText('changed after the switch was confirmed');
  await expect(dialog(page)).toHaveCount(0);
  expect(posts(log)).toHaveLength(2);
});

// ── True group / common-source switching ──────────────────────────────────

const threeFiles = [
  file(6001, [sc('rapidgator.net', 'rg-1', { selected: true }), sc('mega.nz', 'mg-1', { eligible: true })]),
  file(6002, [sc('rapidgator.net', 'rg-2', { selected: true }), sc('mega.nz', 'mg-2', { eligible: true })]),
  file(6003, [sc('rapidgator.net', 'rg-3', { selected: true }), sc('mega.nz', 'mg-3', { eligible: true })]),
];
const twoDestructive = { 6001: [3 * MIB, 0], 6003: [2 * MIB, MIB] };

async function chooseGroupHost(page, launcher) {
  await launcher.click();
  await page.locator('.dp-group-candidate-menu .dp-group-candidate-row', { hasText: 'mega.nz' })
    .locator('.dp-group-candidate-switch').click();
}

test('Downloads group: every move is previewed before any mutation, one confirmation covers several destructive moves, Cancel switches zero', async ({ page }) => {
  const items = [scopedItem(600, 'group', { count: 2, commonCount: 2, remaining: 3 })];
  const log = await fixture(page, 600, items, threeFiles, (id, body) => switched(id, body.candidate_id, 'mega.nz'), twoDestructive);
  let dialogsOpened = 0;
  await page.exposeFunction('dpCountDialog', () => { dialogsOpened += 1; });
  await ready(page);
  await page.evaluate(() => new MutationObserver(records => records.forEach(record => record.addedNodes.forEach(node => {
    if (node.classList && node.classList.contains('dp-modal-overlay')) window.dpCountDialog();
  }))).observe(document.body, { childList: true }));
  await toDownloads(page);
  await chooseGroupHost(page, page.locator('#t-tbody tr[data-torrent-id="600"] .dp-group-candidate-launcher'));

  await expect(dialog(page)).toHaveCount(1);
  expect(log.map(entry => `${entry.kind}:${entry.artifactId}`)).toEqual(['preview:6001', 'preview:6002', 'preview:6003']);
  await expect(dialog(page)).toContainText('across 2 files');
  await expect(dialog(page)).toContainText('file-6001.mkv');
  await expect(dialog(page)).toContainText('file-6003.mkv');
  await expect(dialog(page)).not.toContainText('file-6002.mkv');
  await page.locator('.dp-modal-overlay [data-modal-cancel]').click();
  await expect(dialog(page)).toHaveCount(0);
  await page.waitForTimeout(300);
  expect(posts(log)).toHaveLength(0);
  expect(dialogsOpened).toBe(1);
});

test('Dashboard Recent group: confirmed switching uses each file\'s exact candidate and carries confirmation only for confirmed moves', async ({ page }) => {
  const items = [scopedItem(610, 'group', { count: 2, commonCount: 2, remaining: 3 })];
  const log = await fixture(page, 610, items, threeFiles, (id, body) => switched(id, body.candidate_id, 'mega.nz'), twoDestructive);
  await ready(page);
  await chooseGroupHost(page, page.locator('#dash-tbody tr[data-torrent-id="610"] .dp-group-candidate-launcher'));
  await page.locator('.dp-modal-overlay [data-modal-accept]').click();
  await expect(page.locator('.toast', { hasText: 'switched' })).toContainText('Remaining files switched to mega.nz');
  expect(posts(log).map(entry => [entry.artifactId, entry.body])).toEqual([
    [6001, { candidate_id: 'mg-1', discard_confirmed: true, discard_confirmation: { material_generation: 7, retained_bytes: 0 } }],
    [6002, { candidate_id: 'mg-2' }],
    [6003, { candidate_id: 'mg-3', discard_confirmed: true, discard_confirmation: { material_generation: 7, retained_bytes: MIB } }],
  ]);
  const firstPost = log.findIndex(entry => entry.kind === 'post');
  expect(log.slice(0, firstPost).map(entry => entry.kind)).toEqual(['preview', 'preview', 'preview']);
});

test('group: a zero-discard operation previews and never prompts', async ({ page }) => {
  const items = [scopedItem(620, 'group', { count: 2, commonCount: 2, remaining: 3 })];
  const log = await fixture(page, 620, items, threeFiles, (id, body) => switched(id, body.candidate_id, 'mega.nz'));
  await ready(page);
  await chooseGroupHost(page, page.locator('#dash-tbody tr[data-torrent-id="620"] .dp-group-candidate-launcher'));
  await expect(page.locator('.toast', { hasText: 'switched' })).toContainText('Remaining files switched to mega.nz');
  expect(posts(log).map(entry => entry.body)).toEqual([{ candidate_id: 'mg-1' }, { candidate_id: 'mg-2' }, { candidate_id: 'mg-3' }]);
  await expect(dialog(page)).toHaveCount(0);
});

test('group: a move whose consequence changed after confirmation fails safely and reports partial convergence', async ({ page }) => {
  const items = [scopedItem(630, 'group', { count: 2, commonCount: 2, remaining: 3 })];
  const log = await fixture(page, 630, items, threeFiles, (id, body) => id === 6002
    ? refusal(MIB) : switched(id, body.candidate_id, 'mega.nz'), twoDestructive);
  await ready(page);
  await chooseGroupHost(page, page.locator('#dash-tbody tr[data-torrent-id="630"] .dp-group-candidate-launcher'));
  await page.locator('.dp-modal-overlay [data-modal-accept]').click();
  // 6002 was previewed as keeping everything; it now discards: refused, the
  // operation stops there, and no second dialog is ever opened.
  await expect(page.locator('.toast', { hasText: 'did not fully converge' })).toContainText('1 of 3 files switched');
  await expect(dialog(page)).toHaveCount(0);
  expect(posts(log).map(entry => entry.artifactId)).toEqual([6001, 6002]);
});

test('group: a failed preflight switches nothing', async ({ page }) => {
  const items = [scopedItem(640, 'group', { count: 2, commonCount: 2, remaining: 3 })];
  const log = await fixture(page, 640, items, threeFiles, (id, body) => switched(id, body.candidate_id, 'mega.nz'));
  await page.route(url => url.pathname === '/api/torrents/640/artifacts/6002/candidate/preview', route =>
    route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ detail: { category: 'SOURCE_NOT_FOUND' } }) }));
  await ready(page);
  await chooseGroupHost(page, page.locator('#dash-tbody tr[data-torrent-id="640"] .dp-group-candidate-launcher'));
  await expect(page.locator('.toast', { hasText: 'Nothing was changed' })).toBeVisible();
  await expect(dialog(page)).toHaveCount(0);
  expect(posts(log)).toHaveLength(0);
});
