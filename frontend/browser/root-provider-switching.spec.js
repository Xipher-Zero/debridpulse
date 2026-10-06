const { test, expect } = require('@playwright/test');

// TASK3d-3 corrective: while one provider switch is outstanding the page stays
// live. Every surface keeps the COMMITTED provider and says "Switching to
// <provider>…" beside it, the transfer takes no second switch, navigation and
// the ordinary refresh loop keep their cadence, and the badge moves only when
// the backend answered. The transfer carries the live failure's volume: a
// 222-member decomposition.

const MEMBERS = 222;

async function ready(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({ status: 200, contentType: 'text/css', body: '' }));
  await page.goto('/');
  await page.waitForFunction(() => Boolean(window.DPRootProvider) && Boolean(window.DPDownloads));
}

function torrentItem(state) {
  return {
    id: 30, name: 'Root fixture 30', display_name: 'Root fixture 30', hash: 'magnet:30',
    status: 'downloading', progress: state.progress, active_execution_progress: 50, size_bytes: 4 * MEMBERS,
    created_at: '2026-10-05 10:00:00', current_source_identity: { kind: 'magnet', host: '' },
    request_kinds: ['magnet'], source: 'manual',
    current_provider_id: 'alldebrid', current_provider_name: 'AllDebrid',
    origin_provider_id: 'alldebrid', origin_provider_name: 'AllDebrid', provider_provenance_status: 'pending',
    route_provider_id: state.current[0], route_provider_name: state.current[1], route_switch_available: true,
    common_candidate_count: 0, group_remaining_count: 0, candidate_action_scope: 'none',
  };
}

function detail(state) {
  return {
    id: 30, name: 'Root fixture 30', status: 'downloading', progress: state.progress, size_bytes: 4 * MEMBERS,
    source: 'manual', label: '', hash: '', created_at: '2026-10-05T10:00:00Z',
    current_source_identity: { kind: 'magnet', host: '' }, request_kinds: ['magnet'],
    route_provider_id: state.current[0], route_provider_name: state.current[1],
    route_attempts: [], execution_attempts: [], executors: ['aria2'], source_outcomes: [], events: [],
    files: Array.from({ length: MEMBERS }, (_, index) => ({
      id: index + 1, filename: `Show/e${String(index).padStart(3, '0')}.bin`, size_bytes: 4,
      status: index < 20 ? 'completed' : index < 23 ? 'downloading' : 'queued', blocked: false })),
  };
}

function routeStatus(current) {
  const all = [['alldebrid', 'AllDebrid'], ['realdebrid', 'Real-Debrid'], ['debridlink', 'Debrid-Link']];
  return {
    transfer_id: 30, current_provider_id: current[0], current_provider_name: current[1], switchable: true,
    providers: all.map(([id, name]) => ({ provider_id: id, provider_name: name, readiness: null, reason: null,
      status: id === current[0] ? 'current' : id === 'realdebrid' ? 'prepared' : 'available',
      selectable: id !== current[0] })),
  };
}

async function apis(page, state) {
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.pathname.startsWith('/api/')) state.requests.push({ at: Date.now(), method: request.method(), path: url.pathname });
  });
  await page.route(url => /\/api\/torrents\/\d+\/route$/.test(url.pathname), async route => {
    if (route.request().method() !== 'POST') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(routeStatus(state.current)) });
    }
    state.posts.push(JSON.parse(route.request().postData() || '{}'));
    state.inflight += 1;
    state.maxInflight = Math.max(state.maxInflight, state.inflight);
    await state.answer;                                         // held: the switch is outstanding
    state.inflight -= 1;
    if (state.refuse) {
      return route.fulfill({ status: 409, contentType: 'application/json',
        body: JSON.stringify({ detail: { category: 'resource_state_conflict', message: 'Resource state conflict' } }) });
    }
    state.current = ['realdebrid', 'Real-Debrid'];
    return route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ transfer_id: 30, provider_id: 'realdebrid', previous_provider_id: 'alldebrid' }) });
  });
  await page.route(url => /\/api\/torrents\/\d+$/.test(url.pathname), route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(detail(state)) }));
  await page.route(url => url.pathname === '/api/torrents', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ items: [torrentItem(state)], total: 1 }) }));
  await page.route(url => url.pathname === '/api/execution/throughput', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ download_bytes_per_second: state.bps, max_download_bytes_per_second: 0 }) }));
}

function freshState() {
  const state = { posts: [], requests: [], inflight: 0, maxInflight: 0, refuse: false, progress: 40, bps: 1024,
    current: ['alldebrid', 'AllDebrid'] };
  state.answer = new Promise(resolve => { state.release = resolve; });
  return state;
}

function count(state, from, to, path) {
  return state.requests.filter(item => item.at >= from && item.at < to && (!path || item.path === path)).length;
}

async function window2s(page, state) {
  const from = Date.now();
  await page.waitForTimeout(2000);
  const to = Date.now();
  return { all: count(state, from, to), speed: count(state, from, to, '/api/execution/throughput') };
}

async function startSwitch(page) {
  const launcher = page.locator('#dash-tbody tr[data-torrent-id="30"] .dp-root-provider-launcher');
  await launcher.click();
  await page.locator('.dp-root-provider-menu [data-dp-provider-status="prepared"] .dp-root-provider-switch').click();
  return launcher;
}

test('while a switch is outstanding the page stays live, says it is switching and takes no second switch', async ({ page }) => {
  const state = freshState();
  await apis(page, state);
  await ready(page);
  const before = await window2s(page, state);

  const launcher = await startSwitch(page);
  const recent = page.locator('#dash-tbody tr[data-torrent-id="30"]');
  await expect(page.locator('.dp-root-provider-menu')).toBeHidden();       // never a modal wait
  await expect(launcher).toBeFocused();
  await expect(recent.locator('.dp-root-provider-switching')).toHaveText('Switching to Real-Debrid…');
  await expect(launcher).toHaveText('AllDebrid');                           // the committed route, not relabelled
  await expect(launcher).toHaveAttribute('aria-disabled', 'true');

  // Duplicate submission is blocked: the picker does not reopen, no second POST.
  await launcher.click({ force: true });                                    // aria-disabled: Playwright would refuse
  await expect(page.locator('.dp-root-provider-menu')).toBeHidden();
  expect(state.posts).toHaveLength(1);

  // The ordinary refresh loop keeps its cadence and its values reach the page.
  state.bps = 5 * 1024 * 1024;
  const pending = await window2s(page, state);
  await expect(page.locator('#runtime-badge-speed')).toContainText('MB');
  expect(pending.speed).toBeGreaterThan(0);
  expect(pending.speed).toBeLessThanOrEqual(before.speed + 2);              // never accelerated by the switch
  expect(pending.all).toBeLessThanOrEqual(before.all + 4);                  // no backlog accumulates

  // Unrelated navigation responds; every surface keeps saying it.
  await page.locator('[data-view="torrents"]').first().click();
  await expect(page.locator('#view-torrents')).toHaveClass(/active/);
  state.progress = 77;
  await page.evaluate(async () => { await loadTorrents(); });
  const row = page.locator('#t-tbody tr[data-torrent-id="30"]');
  await expect(row.locator('.dp-root-provider-launcher')).toHaveText('AllDebrid');
  await expect(row.locator('.dp-root-provider-switching')).toHaveText('Switching to Real-Debrid…');
  await page.evaluate(() => showDetail(30));                                 // Details of the 222-member transfer
  const mount = page.locator('#modal-body [data-dp-root-provider-mount]');
  await expect(mount.locator('.dp-root-provider-switching')).toHaveText('Switching to Real-Debrid…');
  await expect(page.locator('#modal-body .dp-detail-provider .dv')).toHaveText('AllDebrid');
  expect(state.maxInflight).toBe(1);

  state.release();                                                            // the backend commits
  await expect(page.locator('#modal-body .dp-detail-provider .dv')).toHaveText('Real-Debrid');
  await expect(page.locator('.dp-root-provider-switching')).toHaveCount(0);
  expect(state.posts).toEqual([{ provider_id: 'realdebrid', expected_provider_id: 'alldebrid' }]);
  console.log(`[8.10 browser] members=${MEMBERS} posts=${state.posts.length} max_inflight=${state.maxInflight} ` +
    `baseline_2s=${JSON.stringify(before)} pending_2s=${JSON.stringify(pending)}`);
});

test('a refused switch clears the switching state, keeps the committed provider and says why', async ({ page }) => {
  const state = freshState();
  state.refuse = true;
  await apis(page, state);
  await ready(page);
  const launcher = await startSwitch(page);
  await expect(page.locator('#dash-tbody tr[data-torrent-id="30"] .dp-root-provider-switching')).toBeVisible();
  state.release();
  await expect(page.locator('.toast, [role="alert"]').filter({
    hasText: 'Could not switch provider; transfer remains on AllDebrid.' }).first()).toBeVisible();
  await expect(page.locator('.toast, [role="alert"]').filter({
    hasText: 'The transfer changed meanwhile; nothing was switched.' }).first()).toBeVisible();
  await expect(page.locator('.dp-root-provider-switching')).toHaveCount(0);
  await expect(launcher).toHaveText('AllDebrid');
  await expect(launcher).not.toHaveAttribute('aria-disabled', 'true');
  await launcher.click();                                                     // switchable again
  await expect(page.locator('.dp-root-provider-menu')).toBeVisible();
});
