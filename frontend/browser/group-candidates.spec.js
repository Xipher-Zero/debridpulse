const { test, expect } = require('@playwright/test');

// Shared runtime + surface owners must all be live before a scenario runs.
const MARKERS = ['DPGroupCandidates', 'DPDownloadsPresentation', 'DPDashboardTransferPresentation'];
async function ready(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({ status: 200, contentType: 'text/css', body: '' }));
  await page.goto('/');
  await page.waitForFunction(markers => markers.every(marker => Boolean(window[marker])), MARKERS);
}

// ── Fixtures ──────────────────────────────────────────────────────────────

function sc(host, candidateId, { selected = false, eligible = false } = {}) {
  return { source_host: host, candidate_id: candidateId, is_selected: selected, switch_eligible: eligible };
}

function file(id, sourceCandidates, status = 'downloading') {
  return {
    id, filename: `file-${id}.rar`, size_bytes: 1024, status, blocked: false, block_reason: null,
    candidate_count: sourceCandidates.length,
    acquisition_candidates: sourceCandidates.length > 1 ? sourceCandidates.map(entry => ({
      candidate_id: entry.candidate_id, source_label: entry.source_host, provider_id: 'alldebrid',
      relationship: 'Original', dispositions: [], is_selected: entry.is_selected, is_active: entry.is_selected,
      is_delivering: false, switch_eligible: entry.switch_eligible,
    })) : undefined,
    source_candidates: sourceCandidates,
  };
}

function detail(id, files) {
  return {
    id, name: `Group fixture ${id}`, status: 'downloading', progress: 40, size_bytes: 4096,
    source: 'direct_link', label: '', hash: '', created_at: '2026-09-09T10:00:00Z',
    current_provider_id: 'alldebrid', current_provider_name: 'AllDebrid',
    route_attempts: [], execution_attempts: [], executors: ['aria2'], source_outcomes: [], events: [],
    files,
  };
}

function listItem(id, commonCount) {
  return {
    id, name: `Group fixture ${id}`, hash: `direct:${id}`, status: 'downloading', progress: 40,
    size_bytes: 4096, created_at: '2026-09-09 10:00:00', current_source_identity: { kind: 'host', host: 'rapidgator.net' },
    current_provider_id: 'alldebrid', current_provider_name: 'AllDebrid',
    delivering_provider_id: 'alldebrid', delivering_provider_name: 'AllDebrid',
    provider_provenance_status: 'recorded', source: 'direct_link',
    common_candidate_count: commonCount,
  };
}

async function routeDetail(page, holder) {
  await page.route(url => /\/api\/torrents\/\d+$/.test(url.pathname), route => {
    const id = Number(route.request().url().match(/\/api\/torrents\/(\d+)/)[1]);
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(holder[id] || detail(id, [])) });
  });
}

// ── Launcher visibility across all three surfaces (§6, §19.2-19.4, §19.12) ──

test('the group launcher follows the 0 / 1 / 2 common-host rule on Downloads and Recent', async ({ page }) => {
  const items = [listItem(10, 0), listItem(11, 1), listItem(12, 2)];
  await page.route('**/api/torrents*', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items, total: items.length }) }));
  await ready(page);

  // Dashboard Recent Items
  for (const [id, present] of [[10, 0], [11, 0], [12, 1]]) {
    await expect(page.locator(`#dash-tbody tr[data-torrent-id="${id}"] .dp-group-candidate-launcher`)).toHaveCount(present);
  }
  // Downloads
  await page.evaluate(async () => { nav(document.querySelector('[data-view="torrents"]')); await loadTorrents(); });
  for (const [id, present] of [[10, 0], [11, 0], [12, 1]]) {
    await expect(page.locator(`#t-tbody tr[data-torrent-id="${id}"] .dp-group-candidate-launcher`)).toHaveCount(present);
  }
  await expect(page.locator('#t-tbody tr[data-torrent-id="12"] .dp-group-candidate-launcher .dp-candidate-chip-count')).toHaveText('2');
});

test('the Details Files header launcher agrees with the list surfaces for the same transfer', async ({ page }) => {
  const holder = {
    20: detail(20, [file(201, [sc('rapidgator.net', 'a1', { selected: true }), sc('mega.nz', 'b1', { eligible: true })])]),
    21: detail(21, [file(211, [sc('rapidgator.net', 'a1', { selected: true }), sc('mega.nz', 'b1', { eligible: true })]),
                     file(212, [sc('rapidgator.net', 'a2', { selected: true }), sc('mega.nz', 'b2', { eligible: true })])]),
  };
  await routeDetail(page, holder);
  await page.route('**/api/torrents?*', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [listItem(21, 2)], total: 1 }) }));
  await ready(page);

  await page.evaluate(() => showDetail(21));
  await expect(page.locator('#overlay')).toHaveClass(/\bopen\b/);
  const launcher = page.locator('.dp-detail-files-group-slot .dp-group-candidate-launcher');
  await expect(launcher).toHaveCount(1);
  await expect(launcher.locator('.dp-candidate-chip-count')).toHaveText('2');

  // A single-artifact transfer with the same host set still has two common
  // hosts for its one file -> launcher present and consistent.
  await page.evaluate(() => closeModal());
  await page.evaluate(() => showDetail(20));
  await expect(page.locator('.dp-detail-files-group-slot .dp-group-candidate-launcher')).toHaveCount(1);
});

// ── Uniform active vs mixed (§7, §19.5, §19.6) ────────────────────────────

test('a uniform active source renders ACTIVE and the other common host renders Switch', async ({ page }) => {
  const holder = {
    30: detail(30, [
      file(301, [sc('rapidgator.net', 'a1', { selected: true }), sc('mega.nz', 'b1', { eligible: true })]),
      file(302, [sc('rapidgator.net', 'a2', { selected: true }), sc('mega.nz', 'b2', { eligible: true })]),
    ]),
  };
  await routeDetail(page, holder);
  await page.route('**/api/torrents?*', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], total: 0 }) }));
  await ready(page);
  await page.evaluate(() => showDetail(30));
  await page.locator('.dp-detail-files-group-slot .dp-group-candidate-launcher').click();

  const menu = page.locator('.dp-group-candidate-menu');
  await expect(menu).toBeVisible();
  await expect(menu.locator('.dp-group-candidate-row')).toHaveCount(2);
  const rgRow = menu.locator('.dp-group-candidate-row', { hasText: 'rapidgator.net' });
  const megaRow = menu.locator('.dp-group-candidate-row', { hasText: 'mega.nz' });
  await expect(rgRow.locator('.dp-group-candidate-active')).toHaveText('ACTIVE');
  await expect(megaRow.locator('.dp-group-candidate-switch')).toHaveText('Switch to this source');
});

test('mixed current sources produce no group ACTIVE host', async ({ page }) => {
  const holder = {
    31: detail(31, [
      file(311, [sc('rapidgator.net', 'a1', { selected: true }), sc('mega.nz', 'b1', { eligible: true })]),
      file(312, [sc('mega.nz', 'b2', { selected: true }), sc('rapidgator.net', 'a2', { eligible: true })]),
    ]),
  };
  await routeDetail(page, holder);
  await page.route('**/api/torrents?*', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], total: 0 }) }));
  await ready(page);
  await page.evaluate(() => showDetail(31));
  await page.locator('.dp-detail-files-group-slot .dp-group-candidate-launcher').click();

  const menu = page.locator('.dp-group-candidate-menu');
  await expect(menu.locator('.dp-group-candidate-active')).toHaveCount(0);
  await expect(menu.locator('.dp-group-candidate-switch')).toHaveCount(2);
});

// ── Membership vs. actionability (§3, §6, §12.1-12.2, §12.5, §12.8) ─────

test('a common source that is not currently switchable for every file stays visible with no switch action', async ({ page }) => {
  const holder = {
    32: detail(32, [
      file(321, [sc('rapidgator.net', 'a1', { selected: true }), sc('mega.nz', 'b1', { eligible: true })]),
      // completed: B is neither selected nor switch-eligible on this file, but
      // it is still a canonical candidate of this file -> B stays common.
      file(322, [sc('rapidgator.net', 'a2', { selected: true }), sc('mega.nz', 'b2', { eligible: false })], 'completed'),
    ]),
  };
  await routeDetail(page, holder);
  await page.route('**/api/torrents?*', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [listItem(32, 2)], total: 1 }) }));
  await ready(page);
  await page.evaluate(() => showDetail(32));
  const launcher = page.locator('.dp-detail-files-group-slot .dp-group-candidate-launcher');
  await expect(launcher.locator('.dp-candidate-chip-count')).toHaveText('2');  // still counted
  await launcher.click();

  const menu = page.locator('.dp-group-candidate-menu');
  await expect(menu.locator('.dp-group-candidate-row')).toHaveCount(2);  // both still shown
  const rgRow = menu.locator('.dp-group-candidate-row', { hasText: 'rapidgator.net' });
  const megaRow = menu.locator('.dp-group-candidate-row', { hasText: 'mega.nz' });
  await expect(rgRow.locator('.dp-group-candidate-active')).toHaveText('ACTIVE');
  // mega.nz: common, visible, but no Switch action.
  await expect(megaRow.locator('.dp-group-candidate-switch')).toHaveCount(0);
  await expect(megaRow.locator('.dp-group-candidate-active')).toHaveCount(0);
  await expect(megaRow).toContainText('Not switchable for every file');
});

test('two common hosts with zero actionable targets still show a launcher and a chooser with no switch actions', async ({ page }) => {
  const holder = {
    33: detail(33, [
      file(331, [sc('rapidgator.net', 'a1', { selected: true }), sc('mega.nz', 'b1', { eligible: false })], 'completed'),
      file(332, [sc('mega.nz', 'b2', { selected: true }), sc('rapidgator.net', 'a2', { eligible: false })], 'completed'),
    ]),
  };
  await routeDetail(page, holder);
  await page.route('**/api/torrents?*', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [listItem(33, 2)], total: 1 }) }));
  await ready(page);
  await page.evaluate(() => showDetail(33));
  const launcher = page.locator('.dp-detail-files-group-slot .dp-group-candidate-launcher');
  await expect(launcher.locator('.dp-candidate-chip-count')).toHaveText('2');
  await launcher.click();

  const menu = page.locator('.dp-group-candidate-menu');
  await expect(menu.locator('.dp-group-candidate-row')).toHaveCount(2);
  await expect(menu.locator('.dp-group-candidate-switch')).toHaveCount(0);
  await expect(menu.locator('.dp-group-candidate-active')).toHaveCount(0);
  await expect(menu.locator('.dp-group-candidate-unavailable')).toHaveCount(2);
});

// ── Convergence orchestration (§8, §19.7, §19.9) ─────────────────────────

test('choosing a host switches only the files not already on it, using each file\'s own candidate id', async ({ page }) => {
  const holder = {
    40: detail(40, [
      file(401, [sc('rapidgator.net', 'rg-401', { selected: true }), sc('mega.nz', 'mg-401', { eligible: true })]),
      file(402, [sc('rapidgator.net', 'rg-402', { selected: true }), sc('mega.nz', 'mg-402', { eligible: true })]),
      file(403, [sc('mega.nz', 'mg-403', { selected: true }), sc('rapidgator.net', 'rg-403', { eligible: true })]),
    ]),
  };
  await routeDetail(page, holder);
  await page.route('**/api/torrents?*', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], total: 0 }) }));

  const posts = [];
  await page.route(url => /\/api\/torrents\/40\/artifacts\/\d+\/candidate$/.test(url.pathname), async route => {
    const artifactId = Number(route.request().url().match(/artifacts\/(\d+)\//)[1]);
    posts.push({ artifactId, body: route.request().postDataJSON() });
    // After the switch, every file is on rapidgator.
    holder[40] = detail(40, [
      file(401, [sc('rapidgator.net', 'rg-401', { selected: true }), sc('mega.nz', 'mg-401', { eligible: true })]),
      file(402, [sc('rapidgator.net', 'rg-402', { selected: true }), sc('mega.nz', 'mg-402', { eligible: true })]),
      file(403, [sc('rapidgator.net', 'rg-403', { selected: true }), sc('mega.nz', 'mg-403', { eligible: true })]),
    ]);
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, transfer_id: 40, artifact_id: artifactId, filename: `file-${artifactId}.rar`, candidate_id: route.request().postDataJSON().candidate_id, source_host: 'rapidgator.net', provider_id: 'alldebrid' }) });
  });

  await ready(page);
  await page.evaluate(() => showDetail(40));
  await page.locator('.dp-detail-files-group-slot .dp-group-candidate-launcher').click();
  await page.locator('.dp-group-candidate-menu .dp-group-candidate-row', { hasText: 'rapidgator.net' }).locator('.dp-group-candidate-switch').click();

  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0]).toEqual({ artifactId: 403, body: { candidate_id: 'rg-403' } });
  await expect(page.locator('.toast', { hasText: 'switched' })).toContainText('Every file switched to rapidgator.net');
});

// ── Stale chooser revalidation distinguishes two failure modes (§7, §12.10, §12.11) ──

test('stale MEMBERSHIP: a host that loses a candidate entirely is revalidated and reported as no longer common', async ({ page }) => {
  const fresh = () => detail(50, [
    file(501, [sc('rapidgator.net', 'a1', { selected: true }), sc('mega.nz', 'b1', { eligible: true })]),
    file(502, [sc('rapidgator.net', 'a2', { selected: true }), sc('mega.nz', 'b2', { eligible: true })]),
  ]);
  const stale = () => detail(50, [
    file(501, [sc('rapidgator.net', 'a1', { selected: true }), sc('mega.nz', 'b1', { eligible: true })]),
    file(502, [sc('rapidgator.net', 'a2', { selected: true })]),  // mega.nz gone entirely for file 502
  ]);
  const holder = { detail: fresh() };
  await page.route(url => url.pathname === '/api/torrents/50', route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(holder.detail) }));
  await page.route('**/api/torrents?*', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], total: 0 }) }));
  let posted = false;
  await page.route(url => /\/api\/torrents\/50\/artifacts\/\d+\/candidate$/.test(url.pathname), route => { posted = true; return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' }); });

  await ready(page);
  await page.evaluate(() => showDetail(50));
  await page.locator('.dp-detail-files-group-slot .dp-group-candidate-launcher').click();
  const menu = page.locator('.dp-group-candidate-menu');
  await expect(menu.locator('.dp-group-candidate-row')).toHaveCount(2);
  // Authoritative truth changes while the chooser is open.
  holder.detail = stale();
  await menu.locator('.dp-group-candidate-row', { hasText: 'mega.nz' }).locator('.dp-group-candidate-switch').click();

  await expect(page.locator('.toast', { hasText: 'mega.nz' })).toContainText('mega.nz is no longer common to the entire transfer');
  expect(posted).toBe(false);
});

test('stale ACTIONABILITY: a host that stays common but loses eligibility is revalidated and reported as not switchable, and remains visible', async ({ page }) => {
  const fresh = () => detail(51, [
    file(511, [sc('rapidgator.net', 'a1', { selected: true }), sc('mega.nz', 'b1', { eligible: true })]),
    file(512, [sc('rapidgator.net', 'a2', { selected: true }), sc('mega.nz', 'b2', { eligible: true })]),
  ]);
  const stale = () => detail(51, [
    file(511, [sc('rapidgator.net', 'a1', { selected: true }), sc('mega.nz', 'b1', { eligible: true })]),
    // Still has mega.nz as a candidate (still COMMON) but can no longer switch
    // to it (no longer ACTIONABLE).
    file(512, [sc('rapidgator.net', 'a2', { selected: true }), sc('mega.nz', 'b2', { eligible: false })], 'completed'),
  ]);
  const holder = { detail: fresh() };
  await page.route(url => url.pathname === '/api/torrents/51', route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(holder.detail) }));
  await page.route('**/api/torrents?*', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], total: 0 }) }));
  let posted = false;
  await page.route(url => /\/api\/torrents\/51\/artifacts\/\d+\/candidate$/.test(url.pathname), route => { posted = true; return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' }); });

  await ready(page);
  await page.evaluate(() => showDetail(51));
  await page.locator('.dp-detail-files-group-slot .dp-group-candidate-launcher').click();
  const menu = page.locator('.dp-group-candidate-menu');
  await expect(menu.locator('.dp-group-candidate-row')).toHaveCount(2);
  holder.detail = stale();
  await menu.locator('.dp-group-candidate-row', { hasText: 'mega.nz' }).locator('.dp-group-candidate-switch').click();

  await expect(page.locator('.toast', { hasText: 'mega.nz' })).toContainText('mega.nz is currently not switchable for every file');
  expect(posted).toBe(false);
  // The host stays visible (still common) but its action is gone.
  const megaRow = menu.locator('.dp-group-candidate-row', { hasText: 'mega.nz' });
  await expect(megaRow).toBeVisible();
  await expect(megaRow.locator('.dp-group-candidate-switch')).toHaveCount(0);
});

// ── Partial failure (§8, §19.14) ────────────────────────────────────────

test('a partial group failure does not fake convergence and reports how far it got', async ({ page }) => {
  const holder = {
    60: detail(60, [
      file(601, [sc('mega.nz', 'm-601', { selected: true }), sc('rapidgator.net', 'r-601', { eligible: true })]),
      file(602, [sc('mega.nz', 'm-602', { selected: true }), sc('rapidgator.net', 'r-602', { eligible: true })]),
    ]),
  };
  await routeDetail(page, holder);
  await page.route('**/api/torrents?*', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], total: 0 }) }));
  const seen = [];
  await page.route(url => /\/api\/torrents\/60\/artifacts\/\d+\/candidate$/.test(url.pathname), route => {
    const artifactId = Number(route.request().url().match(/artifacts\/(\d+)\//)[1]);
    seen.push(artifactId);
    if (artifactId === 601) {
      holder[60].files[0].source_candidates = [sc('rapidgator.net', 'r-601', { selected: true }), sc('mega.nz', 'm-601', { eligible: true })];
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, filename: 'file-601.rar', candidate_id: 'r-601', source_host: 'rapidgator.net' }) });
    }
    return route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ detail: { category: 'provider_unavailable', message: 'Selected provider is unavailable' } }) });
  });

  await ready(page);
  await page.evaluate(() => showDetail(60));
  await page.locator('.dp-detail-files-group-slot .dp-group-candidate-launcher').click();
  await page.locator('.dp-group-candidate-menu .dp-group-candidate-row', { hasText: 'rapidgator.net' }).locator('.dp-group-candidate-switch').click();

  const groupToast = page.locator('.toast', { hasText: 'converge' });
  await expect(groupToast).toContainText('Group did not fully converge on rapidgator.net');
  await expect(groupToast).toContainText('1 of 2 files switched');
  expect(seen).toEqual([601, 602]);
});

// ── Keyboard + non-regression ──────────────────────────────────────────

test('the chooser is keyboard reachable, Escape closes it and returns focus to the launcher', async ({ page }) => {
  const holder = {
    70: detail(70, [
      file(701, [sc('rapidgator.net', 'a1', { selected: true }), sc('mega.nz', 'b1', { eligible: true })]),
      file(702, [sc('rapidgator.net', 'a2', { selected: true }), sc('mega.nz', 'b2', { eligible: true })]),
    ]),
  };
  await routeDetail(page, holder);
  await page.route('**/api/torrents?*', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], total: 0 }) }));
  await ready(page);
  await page.evaluate(() => showDetail(70));
  const launcher = page.locator('.dp-detail-files-group-slot .dp-group-candidate-launcher');
  await launcher.click();
  const menu = page.locator('.dp-group-candidate-menu');
  await expect(menu).toBeVisible();
  await expect(menu.locator('.dp-group-candidate-switch').first()).toBeFocused();
  await menu.locator('.dp-group-candidate-switch').first().press('Escape');
  await expect(menu).toBeHidden();
  await expect(launcher).toBeFocused();
  await expect(launcher).toHaveAttribute('aria-expanded', 'false');
});

test('the per-file candidate disclosure is untouched by the group wrapper', async ({ page }) => {
  const holder = {
    80: detail(80, [
      file(801, [sc('rapidgator.net', 'a1', { selected: true }), sc('mega.nz', 'b1', { eligible: true }), sc('turbobit.net', 'c1', { eligible: true })]),
      file(802, [sc('rapidgator.net', 'a2', { selected: true }), sc('mega.nz', 'b2', { eligible: true })]),
    ]),
  };
  await routeDetail(page, holder);
  await page.route('**/api/torrents?*', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], total: 0 }) }));
  await ready(page);
  await page.evaluate(() => showDetail(80));

  // Group exposes the intersection (2 common hosts).
  await expect(page.locator('.dp-detail-files-group-slot .dp-group-candidate-launcher .dp-candidate-chip-count')).toHaveText('2');
  // The individual file still exposes its own 3-candidate disclosure.
  const disclosure = page.locator('tr[data-dp-artifact-id="801"] .dp-detail-candidate-disclosure');
  await expect(disclosure).toHaveCount(1);
  await expect(disclosure.locator('.dp-candidate-chip-count')).toHaveText('3');
  await disclosure.click();
  await expect(page.locator('tr[data-dp-candidate-owner="801"]')).toBeVisible();
  await expect(page.locator('tr[data-dp-candidate-owner="801"] .dp-detail-candidate-item')).toHaveCount(3);
});
