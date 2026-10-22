const { test, expect } = require('@playwright/test');

// B1 (1.0.13 release correction): the provider badge of a transfer of several
// links opens the transfer's best-effort provider picker directly, on Recent
// Activity and Downloads alike -- never Details. Each provider row says how
// many links it may take now and why the others take no part; choosing one
// sends ONE request naming the provider seen on each eligible link, and the
// result is reported link by link. A file's own switch in Details says
// "Switching to …" beside the files of that one link only.

async function ready(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({ status: 200, contentType: 'text/css', body: '' }));
  await page.goto('/');
  await page.waitForFunction(() => Boolean(window.DPRootProvider) && Boolean(window.DPDownloads));
}

// Three links: r1 (AllDebrid, files 1 and 3), r2 (Premiumize, file 2), r3
// (TorBox, expanded into file 4: never switchable in a transfer of several).
function listItem() {
  return {
    id: 40, name: 'Several links 40', display_name: 'Several links 40', hash: 'request:40',
    status: 'downloading', progress: 30, size_bytes: 4096, created_at: '2026-10-21 08:45:42',
    current_source_identity: { kind: 'host', host: 'rapidgator.net' }, request_kinds: ['https'], source: 'direct_link',
    active_provider_id: 'alldebrid', active_provider_name: 'AllDebrid', active_provider_basis: 'execution',
    current_provider_id: 'alldebrid', current_provider_name: 'AllDebrid',
    route_switch_available: true, route_switch_scope: 'transfer',
    common_candidate_count: 0, group_remaining_count: 0, candidate_action_scope: 'none',
  };
}

function file(id, root, provider, name) {
  return { id, filename: `part0${id}.rar`, size_bytes: 4, status: 'downloading', blocked: false,
    route_root: { root_id: root, provider_id: provider, provider_name: name, switch_available: root !== 'r3' } };
}

function detail() {
  return {
    id: 40, name: 'Several links 40', status: 'downloading', progress: 30, size_bytes: 4096, source: 'direct_link',
    label: '', hash: '', created_at: '2026-10-21T08:45:42Z', current_source_identity: { kind: 'host', host: 'rapidgator.net' },
    request_kinds: ['https'], active_provider_id: 'alldebrid', active_provider_name: 'AllDebrid',
    route_attempts: [], execution_attempts: [], executors: ['aria2'], source_outcomes: [], events: [],
    files: [file(1, 'r1', 'alldebrid', 'AllDebrid'), file(2, 'r2', 'premiumize', 'Premiumize'),
      file(3, 'r1', 'alldebrid', 'AllDebrid'), file(4, 'r3', 'torbox', 'TorBox')],
  };
}

const ROOTS = { r1: ['alldebrid', [1, 3]], r2: ['premiumize', [2]], r3: ['torbox', [4]] };
const NAMES = { alldebrid: 'AllDebrid', premiumize: 'Premiumize', realdebrid: 'Real-Debrid', torbox: 'TorBox' };
// What each provider can do for each link: the backend's per-root statuses.
const SHAPE = {
  alldebrid: { r1: 'already_selected', r2: 'eligible' },
  premiumize: { r1: 'eligible', r2: 'already_selected' },
  realdebrid: { r1: 'eligible', r2: 'unsupported' },
  torbox: { r1: 'unsupported', r2: 'unsupported' },
};

function transferStatus() {
  const providers = Object.keys(SHAPE).map(provider => {
    const roots = Object.keys(ROOTS).map(root => ({
      root_id: root, status: root === 'r3' ? 'decomposed' : SHAPE[provider][root], reason: null,
      current_provider_id: ROOTS[root][0], current_provider_name: NAMES[ROOTS[root][0]], artifact_ids: ROOTS[root][1] }));
    const eligible = roots.filter(root => root.status === 'eligible').length;
    return { provider_id: provider, provider_name: NAMES[provider], eligible, participants: 3, selectable: eligible > 0,
      already_selected: roots.filter(root => root.status === 'already_selected').length, roots };
  });
  return { transfer_id: 40, scope: 'transfer', participants: 3, providers, switchable: true };
}

function rootStatus() {
  return {
    transfer_id: 40, root_id: 'r1', current_provider_id: 'alldebrid', current_provider_name: 'AllDebrid', switchable: true,
    providers: [
      { provider_id: 'alldebrid', provider_name: 'AllDebrid', status: 'current', selectable: false, reason: null, readiness: null },
      { provider_id: 'realdebrid', provider_name: 'Real-Debrid', status: 'available', selectable: true, reason: null, readiness: null },
    ],
  };
}

async function apis(page, state) {
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.pathname.startsWith('/api/torrents/40')) state.requests.push(`${request.method()} ${url.pathname}${url.search}`);
  });
  await page.route(url => url.pathname === '/api/torrents', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ items: [state.item || listItem()], total: 1 }) }));
  await page.route(url => url.pathname === '/api/torrents/40', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(detail()) }));
  await page.route(url => url.pathname === '/api/torrents/40/route/transfer', async route => {
    if (route.request().method() !== 'POST') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(transferStatus()) });
    }
    state.posts.push(JSON.parse(route.request().postData() || '{}'));
    if (state.result) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(state.result) });
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
      transfer_id: 40, provider_id: 'premiumize', result: 'partial', participants: 3,
      switched: 1, already_selected: 1, ineligible: 1, refused: 0, roots: [
        { root_id: 'r1', outcome: 'switched', reason: null, previous_provider_id: 'alldebrid', artifact_ids: [1, 3] },
        { root_id: 'r2', outcome: 'already_selected', reason: null, previous_provider_id: 'premiumize', artifact_ids: [2] },
        { root_id: 'r3', outcome: 'ineligible', reason: 'decomposed', previous_provider_id: 'torbox', artifact_ids: [4] },
      ] }) });
  });
  await page.route(url => url.pathname === '/api/torrents/40/route', async route => {
    if (route.request().method() !== 'POST') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(rootStatus()) });
    }
    state.posts.push(JSON.parse(route.request().postData() || '{}'));
    await state.held;                                         // the switch stays outstanding until released
    return route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ transfer_id: 40, provider_id: 'realdebrid', root_id: 'r1' }) });
  });
}

function freshState() {
  const state = { posts: [], requests: [] };
  state.held = new Promise(resolve => { state.release = resolve; });
  return state;
}

const detailReads = state => state.requests.filter(item => item === 'GET /api/torrents/40').length;

async function expectTransferPicker(page, launcher, state) {
  await launcher.click();
  const menu = page.locator('.dp-root-provider-menu');
  await expect(menu).toBeVisible();
  await expect(menu.locator('.dp-root-provider-title')).toHaveText('Provider for this transfer');
  await expect(launcher).toHaveAttribute('aria-expanded', 'true');
  expect(detailReads(state)).toBe(0);                                         // never Details
  await expect(page.locator('#modal-body .dp-detail-file-row')).toHaveCount(0);
  const row = name => menu.locator('.dp-root-provider-row', { hasText: name });
  await expect(row('Premiumize').locator('.dp-root-provider-status')).toHaveText(
    'Can take 1 of 3 links — 1 already on it · 1 expanded into several files');
  await expect(row('Real-Debrid').locator('.dp-root-provider-status')).toHaveText(
    'Can take 1 of 3 links — 1 cannot use this link · 1 expanded into several files');
  await expect(row('TorBox').locator('.dp-root-provider-switch')).toBeDisabled();
  await expect(row('TorBox').locator('.dp-root-provider-status')).toContainText('No link can switch');
  return menu;
}

test('the Recent Activity badge opens the transfer picker with truthful counts, and choosing reports link by link', async ({ page }) => {
  const state = freshState();
  await apis(page, state);
  await ready(page);
  const launcher = page.locator('#dash-tbody tr[data-torrent-id="40"] .dp-root-provider-launcher');
  await expect(launcher).toHaveText('AllDebrid');
  await expect(launcher).toHaveAttribute('data-dp-scope', 'transfer');
  const menu = await expectTransferPicker(page, launcher, state);

  await menu.locator('.dp-root-provider-row', { hasText: 'Premiumize' }).locator('.dp-root-provider-switch').click();
  await expect(menu).toBeHidden();
  // Exactly what the picker showed: the one link Premiumize may take, with the provider seen on it.
  await expect.poll(() => state.posts).toEqual([{ provider_id: 'premiumize', expected_routes: { r1: 'alldebrid' } }]);
  await expect(page.locator('.toast, [role="status"], [role="alert"]').filter({
    hasText: 'Switched 1 of 3 links to Premiumize.' }).first()).toBeVisible();
  await expect(page.locator('.toast, [role="status"], [role="alert"]').filter({
    hasText: '1 link already on Premiumize; 1 link not switched (expanded into several files).' }).first()).toBeVisible();
  // The badge still names the authoritative provider; no collection-wide identity is invented.
  await expect(launcher).toHaveText('AllDebrid');
  expect(detailReads(state)).toBe(0);
});

test('the Downloads badge opens the same picker, and the rest of the row still opens Details', async ({ page }) => {
  const state = freshState();
  await apis(page, state);
  await ready(page);
  await page.evaluate(async () => { nav(document.querySelector('[data-view="torrents"]')); await loadTorrents(); });
  const launcher = page.locator('#t-tbody tr[data-torrent-id="40"] .dp-root-provider-launcher');
  const menu = await expectTransferPicker(page, launcher, state);
  await page.keyboard.press('Escape');
  await expect(menu).toBeHidden();
  await expect(launcher).toBeFocused();

  // Ordinary row navigation is unchanged: outside the badge the row opens Details.
  await page.locator('#dash-tbody tr[data-torrent-id="40"], #t-tbody tr[data-torrent-id="40"]').filter({ visible: true })
    .locator('.t-name, td').first().click();
  await expect(page.locator('#modal-body .dp-detail-file-row')).toHaveCount(4);
  expect(detailReads(state)).toBeGreaterThan(0);
  expect(state.posts).toEqual([]);
});

test('a file\'s own switch in Details says "Switching to" beside that link\'s files only', async ({ page }) => {
  const state = freshState();
  await apis(page, state);
  await ready(page);
  await page.evaluate(() => showDetail(40));
  const rowOf = id => page.locator(`#modal-body tr.dp-detail-file-row[data-dp-artifact-id="${id}"]`);
  await expect(rowOf(1).locator('.dp-root-provider-launcher')).toHaveText('AllDebrid');
  await rowOf(1).locator('.dp-root-provider-launcher').click();
  await page.locator('.dp-root-provider-menu .dp-root-provider-row', { hasText: 'Real-Debrid' })
    .locator('.dp-root-provider-switch').click();
  await expect.poll(() => state.posts).toEqual([
    { provider_id: 'realdebrid', expected_provider_id: 'alldebrid', artifact_id: 1, expected_root_id: 'r1' }]);

  // While it is outstanding: files 1 and 3 (link r1) say so; file 2 (r2) and the list badge do not.
  await expect(rowOf(1).locator('.dp-root-provider-switching')).toHaveText('Switching to Real-Debrid…');
  await expect(rowOf(3).locator('.dp-root-provider-switching')).toHaveText('Switching to Real-Debrid…');
  await expect(rowOf(2).locator('.dp-root-provider-switching')).toHaveCount(0);
  await expect(page.locator('#dash-tbody tr[data-torrent-id="40"] .dp-root-provider-switching')).toHaveCount(0);
  // The transfer still takes no second switch meanwhile.
  await expect(rowOf(2).locator('.dp-root-provider-launcher')).toHaveAttribute('aria-disabled', 'true');
  state.release();
  await expect(page.locator('#modal-body .dp-root-provider-switching')).toHaveCount(0);
  await expect(rowOf(2).locator('.dp-root-provider-launcher')).not.toHaveAttribute('aria-disabled', 'true');
});

// Option A (transfer 1082): a provider that must first acquire an equivalent
// source is accepted as a PENDING choice. The badge keeps naming the committed
// provider; the pending preference is shown beside it, never as it; and the
// outcome says the files will switch when that provider's copy is ready.
test('a pending provider choice is shown beside the committed provider, never as it', async ({ page }) => {
  const state = freshState();
  state.result = { transfer_id: 40, provider_id: 'torbox', result: 'pending', participants: 1,
    switched: 0, pending: 1, already_selected: 0, ineligible: 0, refused: 0, roots: [
      { root_id: 'r1', outcome: 'pending', reason: null, previous_provider_id: 'alldebrid', artifact_ids: [1, 3],
        files: [{ artifact_id: 1, via: 'alternate', outcome: 'pending', changed: true }] }] };
  await apis(page, state);
  await ready(page);
  const launcher = page.locator('#dash-tbody tr[data-torrent-id="40"] .dp-root-provider-launcher');
  await expect(launcher).toHaveText('AllDebrid');
  await expect(page.locator('#dash-tbody tr[data-torrent-id="40"] .dp-root-provider-pending')).toHaveCount(0);

  await launcher.click();
  const menu = page.locator('.dp-root-provider-menu');
  await expect(menu).toBeVisible();
  await menu.locator('.dp-root-provider-row', { hasText: 'Premiumize' }).locator('.dp-root-provider-switch').click();
  await expect(page.locator('.toast, [role="status"], [role="alert"]').filter({
    hasText: 'preparing Premiumize for 1 link.' }).first()).toBeVisible();
  await expect(page.locator('.toast, [role="status"], [role="alert"]').filter({
    hasText: "1 link will switch when Premiumize's copy is ready." }).first()).toBeVisible();

  // The list now reports the pending preference: shown beside the badge, while
  // the badge still names the committed provider.
  state.item = Object.assign(listItem(), { pending_provider_ids: ['torbox'], pending_provider_names: ['TorBox'] });
  await page.evaluate(() => loadRecent());
  const hint = page.locator('#dash-tbody tr[data-torrent-id="40"] .dp-root-provider-pending');
  await expect(hint).toHaveText('Preparing TorBox…');
  await expect(hint).toHaveAttribute('data-dp-pending-providers', 'torbox');
  await expect(page.locator('#dash-tbody tr[data-torrent-id="40"] .dp-root-provider-launcher')).toHaveText('AllDebrid');
});
