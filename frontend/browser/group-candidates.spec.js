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

test('a common source that is not currently switchable for every remaining-work file stays visible with no switch action', async ({ page }) => {
  const holder = {
    // Genuinely non-actionable regardless of the completed-file exclusion
    // (DP 1.0.12 Defect 4): the only remaining-work file (321) is itself not
    // switch-eligible for mega.nz, so mega.nz correctly stays common but
    // without an action. A prior version of this fixture relied on the now-
    // corrected all-files actionability assumption (a completed file's own
    // ineligibility vetoing the group) to reach the same visible result; see
    // 'a completed file does not veto remaining-work actionability...' below
    // for that corrected boundary instead.
    32: detail(32, [
      file(321, [sc('rapidgator.net', 'a1', { selected: true }), sc('mega.nz', 'b1', { eligible: false })]),
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

// This fixture's two files are BOTH completed -- with no remaining-work
// participants at all, Section 8's "all files completed" rule requires zero
// actionable hosts (never vacuously true over an empty remaining-work set),
// which is exactly what this test already proves (Defect 4 / §14 Case G).
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
    // Still has mega.nz as a candidate (still COMMON) but this remaining-work
    // file can no longer switch to it (no longer ACTIONABLE for remaining
    // work) -- independent of the completed-file exclusion (DP 1.0.12
    // Defect 4): a prior version of this fixture used a newly-completed file
    // to reach the same visible result, which the corrected actionability
    // rule no longer treats as a loss of actionability by itself.
    file(512, [sc('rapidgator.net', 'a2', { selected: true }), sc('mega.nz', 'b2', { eligible: false })]),
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
  await expect(groupToast).toContainText('convergence incomplete');
  expect(seen).toEqual([601, 602]);

  // §12/§14 Case J: the chooser itself is refreshed from authoritative
  // truth, not left showing a fabricated ACTIVE state or a stuck busy panel.
  const menu = page.locator('.dp-group-candidate-menu');
  await expect(menu).toBeVisible();
  await expect(menu).not.toHaveAttribute('aria-busy', 'true');
  await expect(menu.locator('.dp-group-candidate-active')).toHaveCount(0);
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

// ── Compact list launchers vs. labeled Details launcher (Defect 1, §5, §14 Case A/B) ──

test('Dashboard Recent and Downloads render a compact glyph+count launcher with no visible "Candidates" text', async ({ page }) => {
  const items = [listItem(90, 3)];
  await page.route('**/api/torrents*', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items, total: items.length }) }));
  await ready(page);

  const recentLauncher = page.locator('#dash-tbody tr[data-torrent-id="90"] .dp-group-candidate-launcher');
  await expect(recentLauncher).toHaveCount(1);
  await expect(recentLauncher.locator('.dp-candidate-chip-count')).toHaveText('3');
  await expect(recentLauncher).not.toContainText('Candidates');
  await expect(recentLauncher).toHaveAttribute('aria-label', /common sources/);
  await expect(recentLauncher).toHaveAttribute('title', /common to every file/);

  await page.evaluate(async () => { nav(document.querySelector('[data-view="torrents"]')); await loadTorrents(); });
  const downloadsLauncher = page.locator('#t-tbody tr[data-torrent-id="90"] .dp-group-candidate-launcher');
  await expect(downloadsLauncher).toHaveCount(1);
  await expect(downloadsLauncher.locator('.dp-candidate-chip-count')).toHaveText('3');
  await expect(downloadsLauncher).not.toContainText('Candidates');
  await expect(downloadsLauncher).toHaveAttribute('aria-label', /common sources/);
});

test('the Details Files-header launcher renders exactly "<count> Candidates" after the glyph', async ({ page }) => {
  const holder = {
    91: detail(91, [
      file(911, [sc('rapidgator.net', 'a1', { selected: true }), sc('mega.nz', 'b1', { eligible: true }), sc('turbobit.net', 'c1', { eligible: true })]),
    ]),
  };
  await routeDetail(page, holder);
  await page.route('**/api/torrents?*', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [listItem(91, 3)], total: 1 }) }));
  await ready(page);
  await page.evaluate(() => showDetail(91));
  const launcher = page.locator('.dp-detail-files-group-slot .dp-group-candidate-launcher');
  await expect(launcher).toHaveCount(1);
  await expect(launcher).toHaveText('3 Candidates');
});

// ── Click ownership: Recent launcher vs. row navigation (Defect 2, §6, §14 Case C/D/K) ──

test('Recent: clicking the group launcher opens only the chooser, never Details; the row itself still opens Details', async ({ page }) => {
  const items = [listItem(92, 2)];
  await page.route('**/api/torrents*', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items, total: items.length }) }));
  await page.route(url => /\/api\/torrents\/\d+$/.test(url.pathname), route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(detail(92, [file(921, [sc('rapidgator.net', 'a1', { selected: true }), sc('mega.nz', 'b1', { eligible: true })])])) }));
  await ready(page);

  const row = page.locator('#dash-tbody tr[data-torrent-id="92"]');
  await row.locator('.dp-group-candidate-launcher').click();
  await expect(page.locator('.dp-group-candidate-menu')).toBeVisible();
  await expect(page.locator('#overlay')).not.toHaveClass(/\bopen\b/);
  await expect(page.locator('.dp-group-candidate-menu .dp-group-candidate-row')).toHaveCount(2);

  await page.keyboard.press('Escape');
  await expect(page.locator('.dp-group-candidate-menu')).toBeHidden();

  await row.locator('.t-name').click();
  await expect(page.locator('#overlay')).toHaveClass(/\bopen\b/);
});

test('Downloads: clicking the group launcher opens only the chooser, never Details (regression protection)', async ({ page }) => {
  const items = [listItem(94, 2)];
  await page.route('**/api/torrents*', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items, total: items.length }) }));
  await page.route(url => /\/api\/torrents\/\d+$/.test(url.pathname), route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(detail(94, [file(941, [sc('rapidgator.net', 'a1', { selected: true }), sc('mega.nz', 'b1', { eligible: true })])])) }));
  await ready(page);
  await page.evaluate(async () => { nav(document.querySelector('[data-view="torrents"]')); await loadTorrents(); });

  const row = page.locator('#t-tbody tr[data-torrent-id="94"]');
  await row.locator('.dp-group-candidate-launcher').click();
  await expect(page.locator('.dp-group-candidate-menu')).toBeVisible();
  await expect(page.locator('#overlay')).not.toHaveClass(/\bopen\b/);

  await page.keyboard.press('Escape');
  await expect(page.locator('.dp-group-candidate-menu')).toBeHidden();

  await row.locator('.t-name').click();
  await expect(page.locator('#overlay')).toHaveClass(/\bopen\b/);
});

test('keyboard: Enter on either list launcher opens only the chooser, never Details; Enter on the Downloads row itself still opens Details', async ({ page }) => {
  const items = [listItem(95, 2)];
  await page.route('**/api/torrents*', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items, total: items.length }) }));
  await page.route(url => /\/api\/torrents\/\d+$/.test(url.pathname), route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(detail(95, [file(951, [sc('rapidgator.net', 'a1', { selected: true }), sc('mega.nz', 'b1', { eligible: true })])])) }));
  await ready(page);

  await page.locator('#dash-tbody tr[data-torrent-id="95"] .dp-group-candidate-launcher').focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('.dp-group-candidate-menu')).toBeVisible();
  await expect(page.locator('#overlay')).not.toHaveClass(/\bopen\b/);
  await page.keyboard.press('Escape');

  await page.evaluate(async () => { nav(document.querySelector('[data-view="torrents"]')); await loadTorrents(); });
  await page.locator('#t-tbody tr[data-torrent-id="95"] .dp-group-candidate-launcher').focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('.dp-group-candidate-menu')).toBeVisible();
  await expect(page.locator('#overlay')).not.toHaveClass(/\bopen\b/);
  await page.keyboard.press('Escape');

  // Regression protection: the Downloads row's own existing keyboard
  // activation (tabindex + onkeydown, unrelated to this fix) still opens
  // Details.
  await page.locator('#t-tbody tr[data-torrent-id="95"]').focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('#overlay')).toHaveClass(/\bopen\b/);
});

// ── Completed files never veto remaining-work actionability (Defect 4, §8, §14 Case E/F/G) ──

test('a completed file does not veto remaining-work actionability for a host it cannot itself switch to', async ({ page }) => {
  const holder = {
    100: detail(100, [
      file(1001, [sc('rapidgator.net', 'a1', { selected: true }), sc('mega.nz', 'b1', { eligible: false })], 'completed'),
      file(1002, [sc('rapidgator.net', 'a2', { selected: true }), sc('mega.nz', 'b2', { eligible: true })], 'downloading'),
      file(1003, [sc('rapidgator.net', 'a3', { selected: true }), sc('mega.nz', 'b3', { eligible: true })], 'paused'),
    ]),
  };
  await routeDetail(page, holder);
  await page.route('**/api/torrents?*', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [listItem(100, 2)], total: 1 }) }));
  await ready(page);
  await page.evaluate(() => showDetail(100));
  const launcher = page.locator('.dp-detail-files-group-slot .dp-group-candidate-launcher');
  await expect(launcher.locator('.dp-candidate-chip-count')).toHaveText('2');
  await launcher.click();

  const menu = page.locator('.dp-group-candidate-menu');
  await expect(menu.locator('.dp-group-candidate-row')).toHaveCount(2);
  const megaRow = menu.locator('.dp-group-candidate-row', { hasText: 'mega.nz' });
  await expect(megaRow.locator('.dp-group-candidate-switch')).toHaveText('Switch to this source');
  await expect(megaRow.locator('.dp-group-candidate-unavailable')).toHaveCount(0);
});

test('switching the group only POSTs for unfinished files needing movement, never the completed file or an already-on-target file', async ({ page }) => {
  const holder = {
    101: detail(101, [
      file(1011, [sc('rapidgator.net', 'a1', { selected: true }), sc('mega.nz', 'b1', { eligible: false })], 'completed'),
      file(1012, [sc('rapidgator.net', 'a2', { selected: true }), sc('mega.nz', 'b2', { eligible: true })], 'downloading'),
      file(1013, [sc('mega.nz', 'b3', { selected: true }), sc('rapidgator.net', 'a3', { eligible: true })], 'paused'),
    ]),
  };
  await routeDetail(page, holder);
  await page.route('**/api/torrents?*', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], total: 0 }) }));
  const posts = [];
  await page.route(url => /\/api\/torrents\/101\/artifacts\/\d+\/candidate$/.test(url.pathname), route => {
    posts.push(Number(route.request().url().match(/artifacts\/(\d+)\//)[1]));
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) });
  });
  await ready(page);
  await page.evaluate(() => showDetail(101));
  await page.locator('.dp-detail-files-group-slot .dp-group-candidate-launcher').click();
  await page.locator('.dp-group-candidate-menu .dp-group-candidate-row', { hasText: 'mega.nz' }).locator('.dp-group-candidate-switch').click();

  await expect(page.locator('.toast', { hasText: 'switched' })).toBeVisible();
  expect(posts).toEqual([1012]);
});

test('when every file is completed, common hosts remain visible but no switch action exists (no remaining work)', async ({ page }) => {
  const holder = {
    102: detail(102, [
      file(1021, [sc('rapidgator.net', 'a1', { selected: true }), sc('mega.nz', 'b1', { eligible: false })], 'completed'),
      file(1022, [sc('rapidgator.net', 'a2', { selected: true }), sc('mega.nz', 'b2', { eligible: false })], 'completed'),
    ]),
  };
  await routeDetail(page, holder);
  await page.route('**/api/torrents?*', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [listItem(102, 2)], total: 1 }) }));
  await ready(page);
  await page.evaluate(() => showDetail(102));
  const launcher = page.locator('.dp-detail-files-group-slot .dp-group-candidate-launcher');
  await expect(launcher.locator('.dp-candidate-chip-count')).toHaveText('2');
  await launcher.click();

  const menu = page.locator('.dp-group-candidate-menu');
  await expect(menu.locator('.dp-group-candidate-row')).toHaveCount(2);
  await expect(menu.locator('.dp-group-candidate-switch')).toHaveCount(0);
  const rgRow = menu.locator('.dp-group-candidate-row', { hasText: 'rapidgator.net' });
  await expect(rgRow.locator('.dp-group-candidate-active')).toHaveText('ACTIVE');
});

// ── Switch-progress UX (Defect 3, §10, §14 Case H) ──────────────────────

test('a multi-file group switch shows visible progress that increments as each file resolves, and stays busy throughout', async ({ page }) => {
  const holder = {
    103: detail(103, [
      file(1031, [sc('mega.nz', 'm1', { selected: true }), sc('rapidgator.net', 'r1', { eligible: true })]),
      file(1032, [sc('mega.nz', 'm2', { selected: true }), sc('rapidgator.net', 'r2', { eligible: true })]),
      file(1033, [sc('mega.nz', 'm3', { selected: true }), sc('rapidgator.net', 'r3', { eligible: true })]),
    ]),
  };
  await routeDetail(page, holder);
  await page.route('**/api/torrents?*', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], total: 0 }) }));

  const gate = { resolvers: [] };
  await page.route(url => /\/api\/torrents\/103\/artifacts\/\d+\/candidate$/.test(url.pathname), async route => {
    await new Promise(resolve => { gate.resolvers.push(resolve); });
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) });
  });

  await ready(page);
  await page.evaluate(() => showDetail(103));
  await page.locator('.dp-detail-files-group-slot .dp-group-candidate-launcher').click();
  await page.locator('.dp-group-candidate-menu .dp-group-candidate-row', { hasText: 'rapidgator.net' }).locator('.dp-group-candidate-switch').click();

  const menu = page.locator('.dp-group-candidate-menu');
  await expect(menu).toHaveAttribute('aria-busy', 'true');
  await expect(menu).toContainText('Switching to rapidgator.net');
  await expect(menu).toContainText('0 of 3 files');
  await expect(menu.locator('.dp-group-candidate-switch:not([disabled])')).toHaveCount(0);

  await expect.poll(() => gate.resolvers.length).toBeGreaterThan(0);
  gate.resolvers.shift()();
  await expect(menu).toContainText('1 of 3 files');
  await expect(menu).toHaveAttribute('aria-busy', 'true');

  await expect.poll(() => gate.resolvers.length).toBeGreaterThan(0);
  gate.resolvers.shift()();
  await expect(menu).toContainText('2 of 3 files');

  await expect.poll(() => gate.resolvers.length).toBeGreaterThan(0);
  gate.resolvers.shift()();
  await expect(page.locator('.toast', { hasText: 'switched' })).toBeVisible();
  await expect(menu).not.toHaveAttribute('aria-busy', 'true');
});

// ── Authoritative post-success refresh (§11, §14 Case I) ────────────────

test('a successful uniform remaining-work switch converges membership onto one common source and shows ACTIVE', async ({ page }) => {
  const holder = {
    104: detail(104, [
      file(1041, [sc('mega.nz', 'm1', { selected: true }), sc('rapidgator.net', 'r1', { eligible: true })], 'downloading'),
      file(1042, [sc('rapidgator.net', 'r2', { selected: true }), sc('mega.nz', 'm2', { eligible: false })], 'completed'),
    ]),
  };
  await routeDetail(page, holder);
  await page.route('**/api/torrents?*', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], total: 0 }) }));
  await page.route(url => /\/api\/torrents\/104\/artifacts\/\d+\/candidate$/.test(url.pathname), route => {
    holder[104] = detail(104, [
      file(1041, [sc('rapidgator.net', 'r1', { selected: true }), sc('mega.nz', 'm1', { eligible: true })], 'downloading'),
      file(1042, [sc('rapidgator.net', 'r2', { selected: true }), sc('mega.nz', 'm2', { eligible: false })], 'completed'),
    ]);
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) });
  });
  await ready(page);
  await page.evaluate(() => showDetail(104));
  await page.locator('.dp-detail-files-group-slot .dp-group-candidate-launcher').click();
  await page.locator('.dp-group-candidate-menu .dp-group-candidate-row', { hasText: 'rapidgator.net' }).locator('.dp-group-candidate-switch').click();

  const menu = page.locator('.dp-group-candidate-menu');
  await expect(page.locator('.toast', { hasText: 'switched' })).toBeVisible();
  await expect(menu).not.toHaveAttribute('aria-busy', 'true');
  await expect(menu.locator('.dp-group-candidate-row')).toHaveCount(2);
  const rgRow = menu.locator('.dp-group-candidate-row', { hasText: 'rapidgator.net' });
  await expect(rgRow.locator('.dp-group-candidate-active')).toHaveText('ACTIVE');
});

test('a successful remaining-work switch does not fabricate ACTIVE when a completed file truthfully stays on its original host', async ({ page }) => {
  const holder = {
    105: detail(105, [
      file(1051, [sc('mega.nz', 'm1', { selected: true }), sc('rapidgator.net', 'r1', { eligible: true })], 'downloading'),
      file(1052, [sc('mega.nz', 'm2', { selected: true }), sc('rapidgator.net', 'r2', { eligible: false })], 'completed'),
    ]),
  };
  await routeDetail(page, holder);
  await page.route('**/api/torrents?*', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], total: 0 }) }));
  await page.route(url => /\/api\/torrents\/105\/artifacts\/\d+\/candidate$/.test(url.pathname), route => {
    // Only the remaining-work file (1051) actually moves; the completed file
    // (1052) truthfully stays on mega.nz and must never receive a POST.
    holder[105] = detail(105, [
      file(1051, [sc('rapidgator.net', 'r1', { selected: true }), sc('mega.nz', 'm1', { eligible: true })], 'downloading'),
      file(1052, [sc('mega.nz', 'm2', { selected: true }), sc('rapidgator.net', 'r2', { eligible: false })], 'completed'),
    ]);
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) });
  });
  await ready(page);
  await page.evaluate(() => showDetail(105));
  await page.locator('.dp-detail-files-group-slot .dp-group-candidate-launcher').click();
  await page.locator('.dp-group-candidate-menu .dp-group-candidate-row', { hasText: 'rapidgator.net' }).locator('.dp-group-candidate-switch').click();

  const menu = page.locator('.dp-group-candidate-menu');
  await expect(page.locator('.toast', { hasText: 'switched' })).toBeVisible();
  await expect(menu.locator('.dp-group-candidate-row')).toHaveCount(2);
  await expect(menu.locator('.dp-group-candidate-active')).toHaveCount(0);
});
