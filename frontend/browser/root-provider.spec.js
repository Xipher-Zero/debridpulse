const { test, expect } = require('@playwright/test');

// TASK3d-3: the torrent root provider is a live route-state badge on Recent
// Activity, Downloads and the Details Files header, and all three switch
// through ONE action (POST /api/torrents/{id}/route).

async function ready(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({ status: 200, contentType: 'text/css', body: '' }));
  await page.goto('/');
  await page.waitForFunction(() => Boolean(window.DPRootProvider) && Boolean(window.DPDownloads));
}

function torrentItem(id, opts = {}) {
  return {
    id, name: `Root fixture ${id}`, display_name: `Root fixture ${id}`, hash: `magnet:${id}`,
    status: 'downloading', progress: 40, size_bytes: 4096, created_at: '2026-10-05 10:00:00',
    current_source_identity: { kind: 'magnet', host: '' }, request_kinds: ['magnet'], source: 'manual',
    current_provider_id: 'alldebrid', current_provider_name: 'AllDebrid',
    origin_provider_id: 'alldebrid', origin_provider_name: 'AllDebrid', provider_provenance_status: 'pending',
    route_provider_id: opts.provider || 'alldebrid', route_provider_name: opts.name || 'AllDebrid',
    route_switch_available: opts.switchable !== false,
    common_candidate_count: 0, group_remaining_count: 0, candidate_action_scope: 'none',
  };
}

function hosterItem(id) {
  const item = torrentItem(id);
  delete item.route_provider_id; delete item.route_provider_name; delete item.route_switch_available;
  item.current_source_identity = { kind: 'host', host: 'rapidgator.net' };
  item.request_kinds = ['https'];
  return item;
}

function detail(id, provider = ['alldebrid', 'AllDebrid']) {
  return {
    id, name: `Root fixture ${id}`, status: 'downloading', progress: 40, size_bytes: 4096, source: 'manual',
    label: '', hash: '', created_at: '2026-10-05T10:00:00Z', current_source_identity: { kind: 'magnet', host: '' },
    request_kinds: ['magnet'], route_provider_id: provider[0], route_provider_name: provider[1],
    route_attempts: [], execution_attempts: [], executors: ['aria2'], source_outcomes: [], events: [],
    files: [{ id: 1, filename: 'one.bin', size_bytes: 4, status: 'downloading', blocked: false }],
  };
}

function routeStatus(current = ['alldebrid', 'AllDebrid']) {
  const all = [
    { provider_id: 'alldebrid', provider_name: 'AllDebrid' },
    { provider_id: 'realdebrid', provider_name: 'Real-Debrid' },
    { provider_id: 'debridlink', provider_name: 'Debrid-Link' },
    { provider_id: 'torbox', provider_name: 'TorBox' },
  ];
  const shape = {
    alldebrid: { status: 'available', selectable: true, reason: null },
    realdebrid: { status: 'prepared', selectable: true, reason: null },
    debridlink: { status: 'available', selectable: true, reason: null },
    torbox: { status: 'unavailable', selectable: false, reason: 'disabled' },
  };
  return {
    transfer_id: 30, current_provider_id: current[0], current_provider_name: current[1], switchable: true,
    providers: all.map(item => ({ ...item, readiness: null, ...(item.provider_id === current[0]
      ? { status: 'current', selectable: false, reason: null } : shape[item.provider_id]) })),
  };
}

async function routeApis(page, state) {
  await page.route(url => /\/api\/torrents\/\d+\/route$/.test(url.pathname), async route => {
    if (route.request().method() === 'POST') {
      state.posts.push(JSON.parse(route.request().postData() || '{}'));
      if (state.refuse) {
        return route.fulfill({ status: 409, contentType: 'application/json',
          body: JSON.stringify({ detail: { category: 'concurrency_limited', message: 'Concurrency limited' } }) });
      }
      const target = state.status.providers.find(item => item.provider_id === state.posts.at(-1).provider_id);
      state.current = [target.provider_id, target.provider_name];
      state.status = routeStatus(state.current);
      return route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ transfer_id: 30, provider_id: target.provider_id }) });
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(state.status) });
  });
  await page.route(url => /\/api\/torrents\/\d+$/.test(url.pathname), route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(detail(30, state.current)) }));
  await page.route(url => url.pathname === '/api/torrents', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ items: [
      torrentItem(30, { provider: state.current[0], name: state.current[1] }),
      torrentItem(31, { switchable: false }), hosterItem(32)], total: 3 }) }));
}

function freshState() {
  return { posts: [], refuse: false, current: ['alldebrid', 'AllDebrid'], status: routeStatus() };
}

test('Recent and Downloads show the committed root provider; a launcher only where a real alternative exists', async ({ page }) => {
  const state = freshState();
  await routeApis(page, state);
  await ready(page);
  const recent = page.locator('#dash-tbody');
  await expect(recent.locator('tr[data-torrent-id="30"] .dp-root-provider-launcher')).toHaveText('AllDebrid');
  await expect(recent.locator('tr[data-torrent-id="31"] .dp-root-provider-launcher')).toHaveCount(0);
  await expect(recent.locator('tr[data-torrent-id="31"] .dp-root-provider-badge')).toHaveText('AllDebrid');
  // A non-torrent transfer keeps its existing chip, untouched.
  await expect(recent.locator('tr[data-torrent-id="32"] .dp-root-provider-badge, tr[data-torrent-id="32"] .dp-root-provider-launcher')).toHaveCount(0);
  await expect(recent.locator('tr[data-torrent-id="32"] .dp-provider-chip')).toHaveCount(1);

  await page.evaluate(async () => { nav(document.querySelector('[data-view="torrents"]')); await loadTorrents(); });
  await expect(page.locator('#t-tbody tr[data-torrent-id="30"] .dp-root-provider-launcher')).toHaveText('AllDebrid');
  await expect(page.locator('#t-tbody tr[data-torrent-id="31"] .dp-root-provider-launcher')).toHaveCount(0);
});

test('the picker distinguishes every provider state and disables what cannot be chosen', async ({ page }) => {
  const state = freshState();
  await routeApis(page, state);
  await ready(page);
  const launcher = page.locator('#dash-tbody tr[data-torrent-id="30"] .dp-root-provider-launcher');
  await launcher.click();
  const menu = page.locator('.dp-root-provider-menu');
  await expect(menu).toBeVisible();
  await expect(menu).toHaveAttribute('role', 'dialog');
  await expect(launcher).toHaveAttribute('aria-expanded', 'true');
  const rows = menu.locator('.dp-root-provider-row');
  await expect(rows).toHaveCount(4);
  await expect(menu.locator('[data-dp-provider-status="current"] .dp-root-provider-current')).toHaveText('CURRENT');
  await expect(menu.locator('[data-dp-provider-status="prepared"] .dp-root-provider-status')).toContainText('Prepared');
  await expect(menu.locator('[data-dp-provider-status="available"] .dp-root-provider-status')).toContainText('Available');
  const disabled = menu.locator('[data-dp-provider-status="unavailable"]');
  await expect(disabled.locator('.dp-root-provider-status')).toContainText('Disabled');
  await expect(disabled.locator('button.dp-root-provider-switch')).toBeDisabled();

  // Keyboard: focus starts on the first action; arrows move; Escape closes and returns focus.
  await expect(menu.locator('.dp-root-provider-switch:not(:disabled)').first()).toBeFocused();
  await page.keyboard.press('ArrowDown');
  await expect(menu.locator('.dp-root-provider-switch:not(:disabled)').nth(1)).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(menu).toBeHidden();
  await expect(launcher).toBeFocused();
  expect(state.posts).toEqual([]);
});

test('every surface switches through the one action, and the badge follows only the committed route', async ({ page }) => {
  const state = freshState();
  await routeApis(page, state);
  await ready(page);
  await page.locator('#dash-tbody tr[data-torrent-id="30"] .dp-root-provider-launcher').click();
  await page.locator('.dp-root-provider-menu [data-dp-provider-status="prepared"] .dp-root-provider-switch').click();
  await expect(page.locator('.dp-root-provider-menu')).toBeHidden();      // closes before the request answers
  await expect.poll(() => state.posts).toEqual([{ provider_id: 'realdebrid', expected_provider_id: 'alldebrid' }]);
  await expect(page.locator('#dash-tbody tr[data-torrent-id="30"] .dp-root-provider-launcher')).toHaveText('Real-Debrid');

  await page.evaluate(async () => { nav(document.querySelector('[data-view="torrents"]')); await loadTorrents(); });
  await expect(page.locator('#t-tbody tr[data-torrent-id="30"] .dp-root-provider-launcher')).toHaveText('Real-Debrid');
  await page.locator('#t-tbody tr[data-torrent-id="30"] .dp-root-provider-launcher').click();
  await page.locator('.dp-root-provider-menu .dp-root-provider-row', { hasText: 'Debrid-Link' })
    .locator('.dp-root-provider-switch').click();
  await expect.poll(() => state.posts.at(-1)).toEqual({ provider_id: 'debridlink', expected_provider_id: 'realdebrid' });
  await expect(page.locator('#t-tbody tr[data-torrent-id="30"] .dp-root-provider-launcher')).toHaveText('Debrid-Link');
});

test('a refused switch leaves the provider where it was and says why', async ({ page }) => {
  const state = freshState();
  state.refuse = true;
  await routeApis(page, state);
  await ready(page);
  await page.locator('#dash-tbody tr[data-torrent-id="30"] .dp-root-provider-launcher').click();
  await page.locator('.dp-root-provider-menu [data-dp-provider-status="prepared"] .dp-root-provider-switch').click();
  await expect(page.locator('.toast, [role="status"], [role="alert"]').filter({
    hasText: 'Could not switch provider; transfer remains on AllDebrid.' }).first()).toBeVisible();
  await expect(page.locator('#dash-tbody tr[data-torrent-id="30"] .dp-root-provider-launcher')).toHaveText('AllDebrid');
});

test('Details shows the provider control in the Files header, right aligned, and switches through the same action', async ({ page }) => {
  const state = freshState();
  await routeApis(page, state);
  await ready(page);
  await page.evaluate(() => showDetail(30));
  const header = page.locator('#modal-body .dp-detail-files-header');
  const launcher = header.locator('.dp-detail-files-header-actions [data-dp-root-provider-mount] .dp-root-provider-launcher');
  await expect(launcher).toHaveText('AllDebrid');
  const headerBox = await header.boundingBox();
  const launcherBox = await launcher.boundingBox();
  expect(launcherBox.x).toBeGreaterThan(headerBox.x + headerBox.width / 2);          // right side
  await expect(page.locator('#modal-body .dp-detail-provider .dv')).toHaveText('AllDebrid');

  await launcher.click();
  await page.locator('.dp-root-provider-menu [data-dp-provider-status="prepared"] .dp-root-provider-switch').click();
  await expect.poll(() => state.posts).toEqual([{ provider_id: 'realdebrid', expected_provider_id: 'alldebrid' }]);
  await expect(page.locator('#modal-body .dp-detail-provider .dv')).toHaveText('Real-Debrid');
  await expect(page.locator('#modal-body [data-dp-root-provider-mount] .dp-root-provider-launcher')).toHaveText('Real-Debrid');
});

test('at a narrow width the Files header wraps instead of hiding or overlapping the provider control', async ({ page }) => {
  const state = freshState();
  await page.setViewportSize({ width: 420, height: 860 });
  await routeApis(page, state);
  await ready(page);
  await page.evaluate(() => showDetail(30));
  const header = page.locator('#modal-body .dp-detail-files-header');
  const title = header.locator('.card-title');
  const launcher = header.locator('.dp-root-provider-launcher');
  await expect(launcher).toBeVisible();
  const [titleBox, launcherBox] = [await title.boundingBox(), await launcher.boundingBox()];
  const overlap = !(launcherBox.x >= titleBox.x + titleBox.width || launcherBox.y >= titleBox.y + titleBox.height ||
                    titleBox.x >= launcherBox.x + launcherBox.width || titleBox.y >= launcherBox.y + launcherBox.height);
  expect(overlap).toBe(false);
  expect(launcherBox.x + launcherBox.width).toBeLessThanOrEqual(420);
});

test('a list refresh while the picker is open keeps it open on the re-rendered row', async ({ page }) => {
  const state = freshState();
  await routeApis(page, state);
  await ready(page);
  await page.evaluate(async () => { nav(document.querySelector('[data-view="torrents"]')); await loadTorrents(); });
  await page.locator('#t-tbody tr[data-torrent-id="30"] .dp-root-provider-launcher').click();
  const menu = page.locator('.dp-root-provider-menu');
  await expect(menu).toBeVisible();
  await page.evaluate(async () => { await loadTorrents(); });        // the ordinary refresh re-renders the row
  await expect(menu).toBeVisible();
  await expect(page.locator('#t-tbody tr[data-torrent-id="30"] .dp-root-provider-launcher')).toHaveAttribute('aria-expanded', 'true');
  await menu.locator('.dp-root-provider-row', { hasText: 'Debrid-Link' }).locator('.dp-root-provider-switch').click();
  await expect.poll(() => state.posts).toEqual([{ provider_id: 'debridlink', expected_provider_id: 'alldebrid' }]);
});
