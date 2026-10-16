const { test, expect } = require('@playwright/test');

/* DP 1.0.13 navigation and disclosure motion (ui-motion.js / ui-motion.css).
 *
 * A pager or tab slides its own surface across, the sidebar slides the main
 * view region up or down, and a Settings disclosure rolls its real height.
 * Every frame of a slide is recorded from the browser's own view-transition
 * pseudo-elements (their computed transforms), so these cases prove the travel
 * itself, not that a function was called. Downloads, Activity Log and
 * Statistics data are answered by the page (mocks); the rest is the real
 * application. Motion is the browser default (no reduced motion) except in the
 * case that is about reduced motion. */

const PAGES = 10;

async function boot(page, {width = 1440, height = 900} = {}) {
  await page.setViewportSize({width, height});
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.route('**/api/events/stream', () => {});              // held: no live data moves anything here
  await page.addInitScript(() => {
    window.__frames = [];
    // Which surface a scoped slide moves (ui-motion.js marks it while it runs).
    const scopeOf = el => el.id === 'content' ? 'main' : el.id === 'event-list' ? 'activity'
      : el.classList.contains('dp-downloads-table-wrap') ? 'downloads' : el.classList.contains('dp-settings-scroll') ? 'settings'
      : el.classList.contains('dp-help-scroll') ? 'help' : el.className;
    const style = (el, pseudo, key) => { try { return getComputedStyle(el, pseudo)[key]; } catch (_) { return null; } };
    const tick = () => {
      const surface = document.querySelector('.dp-motion-surface');
      if (surface) {
        const motion = document.getAnimations().filter(a => a.effect?.target === surface && /\(root\)$/.test(a.effect?.pseudoElement || ''));
        window.__frames.push({
          t: performance.now(), scope: scopeOf(surface), axis: surface.dataset.dpMotionAxis,
          direction: surface.dataset.dpMotionDirection, step: surface.dataset.dpMotionStep,
          old: style(surface, '::view-transition-old(root)', 'transform'), new: style(surface, '::view-transition-new(root)', 'transform'),
          oldOpacity: Number(style(surface, '::view-transition-old(root)', 'opacity') ?? 1),
          clip: style(surface, '::view-transition-image-pair(root)', 'overflow'),
          durations: [...new Set(motion.map(a => a.effect.getTiming().duration))],
          panels: [...document.querySelectorAll('.view.active [data-panel]:not([hidden]), .view.active .dp-help-panel:not([hidden])')].map(p => p.dataset.panel),
          overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
          topbar: document.getElementById('topbar')?.getBoundingClientRect().top,
          sidebar: document.getElementById('sidebar')?.getBoundingClientRect().left,
        });
      }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });
}

// matrix(a, b, c, d, tx, ty) -> {x, y}; 'none' is no travel.
const shift = value => {
  const m = /matrix\(([^)]+)\)/.exec(value || '');
  if (!m) return {x: 0, y: 0};
  const n = m[1].split(',').map(Number);
  return {x: n[4], y: n[5]};
};

const clear = page => page.evaluate(() => { window.__frames = []; });
const settled = page => page.waitForFunction(() => !document.querySelector('.dp-motion-surface'));
/* The slide that just ran: its scope, direction, step, timing and how far its
 * old and new content travelled (largest displacement seen on each axis). */
async function lastSlide(page) {
  await settled(page);
  const frames = await page.evaluate(() => window.__frames);
  if (!frames.length) return null;
  const scope = frames.at(-1).scope;
  const mine = frames.filter(frame => frame.scope === scope);
  const extreme = (key, axis) => mine.map(frame => shift(frame[key])[axis]).reduce((a, b) => (Math.abs(b) > Math.abs(a) ? b : a), 0);
  return {
    scope, axis: mine[0].axis, direction: mine[0].direction, step: mine[0].step,
    durations: [...new Set(mine.flatMap(frame => frame.durations))],
    old: {x: extreme('old', 'x'), y: extreme('old', 'y')}, new: {x: extreme('new', 'x'), y: extreme('new', 'y')},
    minOldOpacity: Math.min(...mine.map(frame => frame.oldOpacity)),
    moving: mine.filter(frame => frame.old !== 'none' || frame.new !== 'none').length,
    panels: [...new Set(mine.flatMap(frame => frame.panels))],
    overflow: Math.max(...mine.map(frame => frame.overflow)),
    topbar: [...new Set(mine.map(frame => frame.topbar))], sidebar: [...new Set(mine.map(frame => frame.sidebar))],
    clip: [...new Set(mine.map(frame => frame.clip))],
  };
}
const noSlide = async page => {
  await page.waitForTimeout(0);
  expect(await page.evaluate(() => window.__frames.length)).toBe(0);
};

function expectSlide(slide, {scope, axis, direction, step}) {
  expect(slide, `a ${scope} slide`).not.toBeNull();
  expect({scope: slide.scope, axis: slide.axis, direction: slide.direction, step: slide.step}).toEqual({scope, axis, direction, step});
  expect(slide.durations).toEqual([step === 'jump' ? 170 : 220]);
  expect(slide.moving).toBeGreaterThanOrEqual(3);                 // drawn travel over several frames
  expect(slide.clip).toEqual(['hidden']);                          // clipped to its own surface
  const sign = direction === 'forward' ? -1 : 1;                    // forward: the old leaves toward the start
  expect(Math.sign(slide.old[axis])).toBe(sign);
  expect(Math.sign(slide.new[axis])).toBe(-sign);                  // and the new arrives from the end
  const other = axis === 'x' ? 'y' : 'x';
  expect(slide.old[other]).toBe(0);
  if (step === 'jump') expect(slide.minOldOpacity).toBeLessThan(0.5);   // the condensed skip fades
  else expect(slide.minOldOpacity).toBe(1);
}

// ── Downloads ──────────────────────────────────────────────────────────────
async function tenPagesOfDownloads(page) {
  const requests = [];
  await page.route('**/api/torrents**', route => {
    const url = new URL(route.request().url());
    if (url.pathname !== '/api/torrents' || !url.searchParams.has('offset')) return route.fallback();
    const limit = Math.max(1, Number(url.searchParams.get('limit')) || 25);
    const offset = Math.max(0, Number(url.searchParams.get('offset')) || 0);
    requests.push({limit, offset, status: url.searchParams.get('status') || ''});
    const total = PAGES * limit;
    const items = Array.from({length: Math.max(0, Math.min(limit, total - offset))}, (_, index) => {
      const id = offset + index + 1;
      return {id, name: `Transfer ${id}`, hash: `hash-${id}`, status: 'downloading', progress: 10, size_bytes: 1024,
              created_at: '2026-10-01T12:00:00Z', source: 'direct_link', label: null, current_provider_id: 'general_http',
              current_provider_name: 'HTTP & HTTPS', provider_provenance_status: 'known', extraction_status: null, source_failure_count: 0};
    });
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({items, total})});
  });
  return requests;
}

test('Downloads: a page turn slides only the list, across, by direction and distance; nothing else slides', async ({page}) => {
  await boot(page);
  const requests = await tenPagesOfDownloads(page);
  await page.goto('/');
  await page.evaluate(() => nav(document.querySelector('[data-view="torrents"]')));
  const pager = page.locator('#torrent-page-btns'), current = pager.locator('.dp-pager-current');
  await expect(current).toHaveText('1');
  const rows = page.locator('#t-tbody tr[data-torrent-id]');

  await clear(page);
  let asked = requests.length;
  await pager.locator('[aria-label="Next page"]').click();
  await expect(current).toHaveText('2');
  const next = await lastSlide(page);
  expectSlide(next, {scope: 'downloads', axis: 'x', direction: 'forward', step: 'adjacent'});
  expect(requests.length - asked).toBe(1);                            // one request, for the destination only
  await expect(rows.first()).toHaveAttribute('data-torrent-id', String(requests.at(-1).offset + 1));

  await clear(page);
  await pager.locator('[aria-label="Previous page"]').click();
  await expect(current).toHaveText('1');
  expectSlide(await lastSlide(page), {scope: 'downloads', axis: 'x', direction: 'backward', step: 'adjacent'});

  await clear(page);
  asked = requests.length;
  await pager.locator('[aria-label="Last page"]').click();
  await expect(current).toHaveText('10');
  expectSlide(await lastSlide(page), {scope: 'downloads', axis: 'x', direction: 'forward', step: 'jump'});
  expect(requests.slice(asked).map(r => r.offset / r.limit + 1)).toEqual([10]);   // no page in between

  await clear(page);
  await pager.locator('[aria-label="First page"]').click();
  await expect(current).toHaveText('1');
  expectSlide(await lastSlide(page), {scope: 'downloads', axis: 'x', direction: 'backward', step: 'jump'});

  // A filter, a refresh and a programmatic page change render in place.
  await clear(page);
  await page.locator('#view-torrents .ftab[data-dp-status="downloading"]').click();
  await expect.poll(() => requests.at(-1).status).toBe('downloading');
  await page.evaluate(() => loadTorrents());
  await page.evaluate(() => goToTorrentPage(4));
  await expect(current).toHaveText('4');
  await noSlide(page);
});

// ── Activity Log ───────────────────────────────────────────────────────────
function journal(limit) {
  const total = (PAGES - 1) * limit + 7, snapshot = total;
  const ids = Array.from({length: total}, (_, index) => total - index);
  const event = id => ({id, severity: 'info', category: 'transfer', type: 'transfer.synthetic', message: `Event ${id}`,
                        transfer_id: id, occurred_at: '2026-10-09T12:00:00Z'});
  return {answer(q) {
    let start = 0, page;
    if (q.last) { page = PAGES; start = (PAGES - 1) * limit; }
    else if (q.after) { const above = ids.filter(id => id > Number(q.after)); start = Math.max(0, above.length - limit); }
    else if (q.before) start = ids.findIndex(id => id < Number(q.before));
    const items = ids.slice(start, start + limit).map(event);
    const older = ids.length - (start + items.length);
    return {items, limit, snapshot, has_more: older > 0, next_before: older > 0 ? items.at(-1).id : null,
            more_after_next: older > limit, newer_available: false, ...(q.after || q.last ? {has_newer: start > 0} : {}),
            ...(q.last ? {page} : {}), search: {text: 'none', pending: 0, complete: true}, window_start: null};
  }};
}

test('Activity Log: a page turn slides the list across; Refresh and live updates never do', async ({page}) => {
  await boot(page);
  const requests = [];
  await page.route('**/api/events?**', route => {
    const q = Object.fromEntries(new URL(route.request().url()).searchParams);
    requests.push(q);
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(journal(Number(q.limit)).answer(q))});
  });
  await page.goto('/');
  await page.evaluate(() => { settingsData.activity_log_page_size = 50; });
  await page.evaluate(() => nav(document.querySelector('[data-view="events"]')));
  const pager = page.locator('#dp-activity-page-btns'), current = pager.locator('.dp-pager-current');
  await expect(current).toHaveText('1');

  await clear(page);
  await pager.locator('[aria-label="Next page"]').click();
  await expect(current).toHaveText('2');
  expectSlide(await lastSlide(page), {scope: 'activity', axis: 'x', direction: 'forward', step: 'adjacent'});
  expect(requests.at(-1)).toMatchObject({snapshot: '457', before: '408'});    // the snapshot's own cursor

  await clear(page);
  await pager.locator('[aria-label="Last page"]').click();
  await expect(current).toHaveText('10');
  expectSlide(await lastSlide(page), {scope: 'activity', axis: 'x', direction: 'forward', step: 'jump'});

  await clear(page);
  await pager.locator('[aria-label="Previous page"]').click();
  await expect(current).toHaveText('9');
  expectSlide(await lastSlide(page), {scope: 'activity', axis: 'x', direction: 'backward', step: 'adjacent'});

  await clear(page);
  await pager.locator('[aria-label="First page"]').click();
  await expect(current).toHaveText('1');
  expectSlide(await lastSlide(page), {scope: 'activity', axis: 'x', direction: 'backward', step: 'jump'});

  // Refresh and an Auto Refresh re-read are new investigations, shown in place.
  await clear(page);
  const before = requests.length;
  await page.locator('.dp-activity-refresh').click();
  await expect.poll(() => requests.length).toBe(before + 1);
  await page.evaluate(() => document.documentElement.dataset.dpPulse = 'connected');
  await page.locator('.dp-activity-live').click();
  await expect.poll(() => requests.length).toBe(before + 2);
  await page.evaluate(() => document.dispatchEvent(new CustomEvent('debridpulse:activity-journal-changed', {detail: {latest: 999}})));
  await expect.poll(() => requests.length).toBe(before + 3);
  await expect(current).toHaveText('1');
  await noSlide(page);
});

// ── Settings and Help tabs ─────────────────────────────────────────────────
const settingsTab = (page, id) => page.locator(`#view-settings.active .dp-settings-tabs [data-tab="${id}"]`);

test('Settings tabs: click and keyboard slide only the panel, by tab order, never through a skipped tab', async ({page}) => {
  await boot(page);
  await page.goto('/');
  await page.evaluate(() => nav(document.querySelector('[data-view="settings"]')));
  await expect(settingsTab(page, 'sources')).toHaveAttribute('aria-selected', 'true');

  await clear(page);
  await settingsTab(page, 'downloads').click();
  await expect(page.locator('.view.active [data-panel="downloads"]')).toBeVisible();
  let slide = await lastSlide(page);
  expectSlide(slide, {scope: 'settings', axis: 'x', direction: 'forward', step: 'adjacent'});
  expect(slide.panels.every(panel => ['sources', 'downloads'].includes(panel))).toBe(true);
  expect(slide.topbar.length).toBe(1);

  await clear(page);
  await settingsTab(page, 'maintenance').click();
  await expect(page.locator('.view.active [data-panel="maintenance"]')).toBeVisible();
  slide = await lastSlide(page);
  expectSlide(slide, {scope: 'settings', axis: 'x', direction: 'forward', step: 'jump'});
  expect(slide.panels.every(panel => ['downloads', 'maintenance'].includes(panel))).toBe(true);   // no tab in between

  // Keyboard: Home is a jump back, ArrowRight one step on; focus and ARIA follow the strip at once.
  await clear(page);
  await settingsTab(page, 'maintenance').focus();
  await page.keyboard.press('Home');
  await expect(settingsTab(page, 'sources')).toBeFocused();
  await expect(settingsTab(page, 'sources')).toHaveAttribute('aria-selected', 'true');
  expectSlide(await lastSlide(page), {scope: 'settings', axis: 'x', direction: 'backward', step: 'jump'});
  await clear(page);
  await page.keyboard.press('ArrowRight');
  await expect(settingsTab(page, 'downloads')).toBeFocused();
  expectSlide(await lastSlide(page), {scope: 'settings', axis: 'x', direction: 'forward', step: 'adjacent'});
  const strip = await page.locator('#view-settings.active .dp-settings-tabs [data-tab]').evaluateAll(tabs =>
    tabs.map(tab => [tab.dataset.tab, tab.getAttribute('aria-selected'), tab.tabIndex]).filter(([, selected]) => selected === 'true'));
  expect(strip).toEqual([['downloads', 'true', 0]]);
  expect(await page.locator('.view.active [data-panel]:not([hidden])').evaluateAll(p => p.map(x => x.dataset.panel))).toEqual(['downloads']);

  // The same tab: nothing moves.
  await clear(page);
  await settingsTab(page, 'downloads').click();
  await noSlide(page);
});

test('Help tabs: the same contract; its accordions keep their state', async ({page}) => {
  await boot(page);
  await page.goto('/');
  await page.evaluate(() => nav(document.querySelector('[data-view="help"]')));
  const tabs = page.locator('#view-help .dp-help-tab');
  await expect(tabs.first()).toHaveAttribute('aria-selected', 'true');
  const ids = await tabs.evaluateAll(list => list.map(tab => tab.dataset.tab));
  const accordion = page.locator(`#dp-help-panel-${ids[0]} details`).first();
  const hasAccordion = await accordion.count();
  if (hasAccordion) await accordion.locator('summary').click();

  await clear(page);
  await tabs.nth(1).click();
  await expect(page.locator(`#dp-help-panel-${ids[1]}`)).toBeVisible();
  expectSlide(await lastSlide(page), {scope: 'help', axis: 'x', direction: 'forward', step: 'adjacent'});

  await clear(page);
  await tabs.nth(1).focus();
  await page.keyboard.press('End');
  await expect(tabs.last()).toBeFocused();
  const slide = await lastSlide(page);
  expectSlide(slide, {scope: 'help', axis: 'x', direction: 'forward', step: 'jump'});
  expect(slide.panels.every(panel => [ids[1], ids.at(-1)].includes(panel))).toBe(true);

  await clear(page);
  await page.keyboard.press('Home');
  await expect(tabs.first()).toBeFocused();
  expectSlide(await lastSlide(page), {scope: 'help', axis: 'x', direction: 'backward', step: 'jump'});
  if (hasAccordion) expect(await accordion.evaluate(node => node.open)).toBe(true);   // untouched by the slide
  expect(await tabs.evaluateAll(list => list.filter(tab => tab.tabIndex === 0).map(tab => tab.dataset.tab))).toEqual([ids[0]]);
});

// ── Sidebar ────────────────────────────────────────────────────────────────
const entry = (page, view) => page.locator(`#sidebar .nav-item[data-view="${view}"]`).first();

test('Sidebar: the main view slides up or down by the displayed order; chrome stays; no in-view slide joins it', async ({page}) => {
  await boot(page);
  await page.goto('/');
  const order = await page.locator('#sidebar .nav-item[data-view]').evaluateAll(items => items.map(item => item.dataset.view));
  expect(order).toEqual(['dashboard', 'torrents', 'events', 'stats', 'settings', 'help']);   // Admin, open mode

  await clear(page);
  await entry(page, 'torrents').click();
  await expect(page.locator('#view-torrents')).toHaveClass(/\bactive\b/);
  let slide = await lastSlide(page);
  expectSlide(slide, {scope: 'main', axis: 'y', direction: 'forward', step: 'adjacent'});
  expect(slide.topbar.length).toBe(1);
  expect(slide.sidebar.length).toBe(1);
  expect((await page.evaluate(() => window.__frames)).every(frame => frame.scope === 'main')).toBe(true);   // no page slide joined

  await clear(page);
  await entry(page, 'dashboard').click();
  await expect(page.locator('#view-dashboard')).toHaveClass(/\bactive\b/);
  expectSlide(await lastSlide(page), {scope: 'main', axis: 'y', direction: 'backward', step: 'adjacent'});

  await clear(page);
  await entry(page, 'help').click();                  // over Downloads, Activity Log, Statistics and Settings
  await expect(page.locator('#view-help')).toHaveClass(/\bactive\b/);
  slide = await lastSlide(page);
  expectSlide(slide, {scope: 'main', axis: 'y', direction: 'forward', step: 'jump'});
  expect(await page.locator('.view.active').evaluateAll(views => views.map(view => view.id))).toEqual(['view-help']);

  // Keyboard activation slides too; the active entry, a programmatic change and a reload do not.
  await clear(page);
  await entry(page, 'stats').focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('#view-stats')).toHaveClass(/\bactive\b/);
  expectSlide(await lastSlide(page), {scope: 'main', axis: 'y', direction: 'backward', step: 'jump'});
  await clear(page);
  await entry(page, 'stats').click();
  await page.evaluate(() => nav(document.querySelector('[data-view="torrents"]')));
  await expect(page.locator('#view-torrents')).toHaveClass(/\bactive\b/);
  await noSlide(page);
  await page.reload();
  await expect(page.locator('#view-dashboard')).toHaveClass(/\bactive\b/);
  await noSlide(page);

  // Rapid choices: the last one wins, with one active view and entry.
  await clear(page);
  await entry(page, 'events').click();
  await entry(page, 'settings').click();
  await settled(page);
  await expect(page.locator('#view-settings.active')).toHaveCount(1);
  expect(await page.locator('#sidebar .nav-item.active').evaluateAll(items => items.map(item => item.dataset.view))).toEqual(['settings']);
  expect(await page.locator('.view.active').count()).toBe(1);
});

test('Dashboard: the heartbeat route is read from settled cards on arrival; its clock and beats carry on', async ({page}) => {
  await boot(page);
  await page.goto('/');
  await page.waitForFunction(() => _dashboardHeartbeat.running && _dashboardHeartbeat.route.cards.length === 6);
  const route = () => page.evaluate(() => _dashboardHeartbeat.route.cards.map(card => [card.left, card.right, card.top].map(Math.round)));
  const settledRoute = await route();
  const clock = await page.evaluate(() => ({running: _dashboardHeartbeat.running, born: _dashboardHeartbeat.beats.map(beat => beat.born)}));

  await entry(page, 'torrents').click();
  await settled(page);
  await clear(page);
  await entry(page, 'dashboard').click();
  // During the slide itself: the strip was measured from the live cards, which never move.
  await page.waitForFunction(() => document.getElementById('content').classList.contains('dp-motion-surface') && document.getElementById('view-dashboard').classList.contains('active'));
  const during = await route();
  const live = await page.evaluate(() => [...document.querySelectorAll('#view-dashboard .dp-card-spark')]
    .map(svg => { const box = svg.getBoundingClientRect(); return [box.left, box.right, box.top].map(Math.round); }));
  const slide = await lastSlide(page);
  expectSlide(slide, {scope: 'main', axis: 'y', direction: 'backward', step: 'adjacent'});
  const sorted = list => [...list].sort((a, b) => a[2] - b[2] || a[0] - b[0]);
  expect(during).toEqual(settledRoute);
  expect(sorted(live)).toEqual(sorted(settledRoute));                // the live cards themselves never moved
  expect(await route()).toEqual(settledRoute);                       // and again once settled
  const after = await page.evaluate(() => ({running: _dashboardHeartbeat.running, born: _dashboardHeartbeat.beats.map(beat => beat.born)}));
  expect(after.running).toBe(true);
  // No restart: every beat still on the strip was born before, or after, without a reset of the ones before.
  const kept = clock.born.filter(born => after.born.includes(born));
  expect(after.born.slice(0, kept.length)).toEqual(kept);

  // A jump in, from Help.
  await entry(page, 'help').click();
  await settled(page);
  await clear(page);
  await entry(page, 'dashboard').click();
  expectSlide(await lastSlide(page), {scope: 'main', axis: 'y', direction: 'backward', step: 'jump'});
  expect(await route()).toEqual(settledRoute);
});

test('Statistics: the chart takes its settled size on arrival, and a live re-read mid-slide keeps that chart', async ({page}) => {
  await boot(page);
  let version = 1;
  await page.route(url => url.pathname === '/api/stats/detail', route => {
    const period = new URL(route.request().url()).searchParams.get('period');
    const daily = [0, 1, 2].map(index => ({date: `2026-10-1${index}`, count: version + index, premium: 0, network: 0, unattributed: version + index}));
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({period, totals: {torrent_total: version},
      torrent_status: [], file_status: [], event_levels: [], transfer_methods: [], daily_completions: daily})});
  });
  await page.goto('/');
  const fit = () => page.evaluate(() => {
    const canvas = document.getElementById('daily-chart'), chart = canvas._ci, box = canvas.parentElement;
    return {chart: chart.width, height: chart.height, container: box.clientWidth, containerHeight: box.clientHeight,
            canvas: Math.round(canvas.getBoundingClientRect().width)};
  });
  for (const [from, step] of [['events', 'adjacent'], ['dashboard', 'jump']]) {
    await page.evaluate(view => nav(document.querySelector(`[data-view="${view}"]`)), from);
    await clear(page);
    await page.evaluate(() => {
      window.__chart = null;
      document.addEventListener('debridpulse:statistics-rendered', function entered(event) {
        if (event.detail?.live) return;
        window.__chart = document.getElementById('daily-chart')._ci;    // the chart this entry drew
        document.removeEventListener('debridpulse:statistics-rendered', entered);
      });
    });
    await entry(page, 'stats').click();
    await page.waitForFunction(() => window.__chart);
    version++;
    // A live change arrives while the view is still sliding in.
    await page.evaluate(() => document.dispatchEvent(new CustomEvent('debridpulse:statistics-invalidated')));
    expectSlide(await lastSlide(page), {scope: 'main', axis: 'y', direction: step === 'jump' ? 'forward' : 'forward', step});
    await page.waitForFunction(() => !Chart.animator.running(document.getElementById('daily-chart')._ci));
    await expect.poll(() => page.evaluate(() => document.getElementById('daily-chart')._ci.data.datasets[0].data[0])).toBe(version);
    expect(await page.evaluate(() => document.getElementById('daily-chart')._ci === window.__chart)).toBe(true);
    const size = await fit();
    expect(Math.abs(size.chart - size.container)).toBeLessThanOrEqual(1);
    expect(Math.abs(size.canvas - size.container)).toBeLessThanOrEqual(1);
    expect(size.height).toBeGreaterThan(50);
    await page.waitForTimeout(2100);                               // past the live re-read spacing for the next round
  }
});

// ── Settings disclosures ───────────────────────────────────────────────────
/* Every frame from now until the disclosure body's own motion has started and
 * finished (at most `cap` ms): the body's drawn height and the top and height
 * of the element observed beside it. */
async function track(page, body, next, cap = 1500) {
  return page.evaluate(([bodySelector, nextSelector, limit]) => new Promise(resolve => {
    const frames = [], end = performance.now() + limit;
    let moved = false;
    const sample = () => {
      const b = document.querySelector(bodySelector), n = document.querySelector(nextSelector);
      const running = !!b && b.getAnimations().length > 0;
      moved = moved || running;
      frames.push({height: b && !b.hidden ? b.getBoundingClientRect().height : 0, next: n.getBoundingClientRect().top,
                   span: n.getBoundingClientRect().height});
      if ((moved && !running) || performance.now() > end) resolve(frames); else requestAnimationFrame(sample);
    };
    requestAnimationFrame(sample);
  }), [body, next, cap]);
}
// Several distinct frames strictly between the first and the last: it moved, it did not jump.
function travelled(frames, key) {
  const first = frames[0][key], last = frames.at(-1)[key];
  const between = frames.filter(frame => frame[key] > Math.min(first, last) + 1 && frame[key] < Math.max(first, last) - 1);
  return {first, last, between: new Set(between.map(frame => Math.round(frame[key]))).size};
}

async function openSettingsServices(page) {
  await page.evaluate(() => nav(document.querySelector('[data-view="settings"]')));
  await expect(page.locator('#view-settings.active [data-panel="sources"]')).toBeVisible();
}

test('Settings disclosures: every mechanism rolls its real height, and what follows moves with it', async ({page}) => {
  await boot(page);
  await page.goto('/');
  await openSettingsServices(page);
  // 1. The canonical disclosure chip (provider, group, subsection and tuning cards).
  const bodyId = await page.locator('.view.active .dp-settings-provider-card .dp-settings-disclosure[aria-expanded="false"]').first().getAttribute('aria-controls');
  const chip = page.locator(`.view.active .dp-settings-disclosure[aria-controls="${bodyId}"]`);
  const card = `#${bodyId}`;
  const following = await page.evaluate(id => {
    const node = document.getElementById(id).closest('.dp-settings-provider-card');
    let next = node.nextElementSibling;
    while (next && !next.getClientRects().length) next = next.nextElementSibling;
    if (!next) return null;
    next.dataset.motionProbe = 'next';
    return '[data-motion-probe="next"]';
  }, bodyId);
  expect(following).not.toBeNull();
  const opening = track(page, card, following);
  await chip.click();
  let frames = await opening;
  await expect(chip).toHaveAttribute('aria-expanded', 'true');
  const grow = travelled(frames, 'next');
  expect(grow.last).toBeGreaterThan(grow.first + 20);
  expect(grow.between).toBeGreaterThanOrEqual(3);
  expect(travelled(frames, 'height').between).toBeGreaterThanOrEqual(3);
  expect(await page.locator(card).evaluate(node => node.style.height || node.style.overflow || '')).toBe('');

  // Closing with focus inside: focus returns to the chip, the body ends hidden and not inert.
  await page.locator(`${card} input, ${card} select, ${card} button`).first().focus();
  const closing = track(page, card, following);
  await chip.evaluate(node => node.click());
  await expect(chip).toBeFocused();
  frames = await closing;
  const shrink = travelled(frames, 'next');
  expect(shrink.last).toBeLessThan(shrink.first - 20);
  expect(shrink.between).toBeGreaterThanOrEqual(3);
  await expect(page.locator(card)).toBeHidden();
  expect(await page.locator(card).evaluate(node => [node.inert, node.style.height, node.getAnimations().length])).toEqual([false, '', 0]);

  // Reversal mid-motion: the last choice wins, with no animation or inline size left behind.
  await chip.click();
  await page.waitForTimeout(80);
  await chip.click();
  await expect(page.locator(card)).toBeHidden();
  await expect.poll(() => page.locator(card).evaluate(node => node.getAnimations().length)).toBe(0);
  await expect(chip).toHaveAttribute('aria-expanded', 'false');

  // 2. Additional Settings (native <details>) inside an opened provider card.
  await chip.click();
  await expect(page.locator(card)).toBeVisible();
  const details = page.locator(`${card} details.dp-settings-additional`).first();
  if (await details.count()) {
    await details.evaluate(node => {
      let next = node.nextElementSibling;
      while (next && !next.getClientRects().length) next = next.nextElementSibling;
      (next || node.parentElement.nextElementSibling).dataset.motionProbe = 'after-details';
    });
    const dBody = `${card} details.dp-settings-additional > .dp-settings-additional-body`;
    const opening2 = track(page, dBody, '[data-motion-probe="after-details"]');
    await details.locator('summary').click();
    frames = await opening2;
    expect(await details.evaluate(node => node.open)).toBe(true);
    expect(travelled(frames, 'next').between).toBeGreaterThanOrEqual(3);
    const closing2 = page.evaluate(selector => new Promise(resolve => {
      const node = document.querySelector(selector), seen = [];
      const end = performance.now() + 400;
      const sample = () => {
        seen.push({open: node.open, closing: node.dataset.dpClosing === '1',
                   chevron: getComputedStyle(node.querySelector('summary'), '::after').transform});
        if (performance.now() < end) requestAnimationFrame(sample); else resolve(seen);
      };
      requestAnimationFrame(sample);
    }), `${card} details.dp-settings-additional`);
    await details.locator('summary').click();
    const states = await closing2;
    expect(await details.evaluate(node => node.open)).toBe(false);
    // While it rolls up it is still open, but its chevron has already turned back.
    const rolling = states.filter(state => state.open && state.closing);
    expect(rolling.length).toBeGreaterThanOrEqual(3);
    expect(new Set(rolling.slice(-2).map(state => state.chevron)).has('none') || rolling.slice(-1)[0].chevron.startsWith('matrix(1, 0')).toBe(true);
  }
  await chip.click();
  await expect(page.locator(card)).toBeHidden();

  // 3. A Usenet server's Advanced panel (its own owner).
  const usenet = page.locator('.view.active .dp-settings-provider-card--usenet .dp-settings-disclosure');
  if ((await usenet.getAttribute('aria-expanded')) !== 'true') await usenet.click();
  // A new, local blank card: nothing is committed, so no server is created and
  // the Usenet collection other specs own is left alone.
  await page.locator('.view.active [data-usenet-collection] [data-usenet-action="add"]').click();
  const server = page.locator('.view.active [data-usenet-server-id=""]').last();
  const toggle = server.locator('[data-usenet-advanced-toggle]');
  const advancedId = await toggle.getAttribute('aria-controls');
  // Server cards sit side by side: the card's own height is what grows with its panel.
  await server.evaluate(node => { node.dataset.motionProbe = 'server'; });
  const opening3 = track(page, `#${advancedId}`, '[data-motion-probe="server"]');
  await toggle.click();
  frames = await opening3;
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  // The panel rolls down; the card grows with it. (Its action pair moving to
  // a row of its own is the card's existing open layout, applied at once.)
  const down = travelled(frames, 'span');
  expect(down.last).toBeGreaterThan(down.first + 20);
  expect(travelled(frames, 'height').between).toBeGreaterThanOrEqual(3);
  const closing3 = track(page, `#${advancedId}`, '[data-motion-probe="server"]');
  await toggle.click();
  frames = await closing3;
  const up = travelled(frames, 'span');
  expect(up.last).toBeLessThan(up.first - 20);
  expect(travelled(frames, 'height').between).toBeGreaterThanOrEqual(3);
  await expect(page.locator(`#${advancedId}`)).toBeHidden();
});

test('Settings disclosures: a render or reconciliation shows the final state at once, without motion', async ({page}) => {
  await boot(page);
  await page.goto('/');
  await openSettingsServices(page);
  const chip = page.locator('.view.active .dp-settings-provider-card .dp-settings-disclosure').first();
  await chip.click();
  await expect(chip).toHaveAttribute('aria-expanded', 'true');
  await page.waitForFunction(() => !document.querySelector('.view.active .dp-settings-provider-card .card-body')?.getAnimations().length);
  await page.evaluate(() => loadSettings());
  await page.waitForFunction(() => document.querySelector('.view.active .dp-settings-tabs'));
  // Nothing in Settings is animating after a render; every body is in its final state.
  expect(await page.evaluate(() => [...document.querySelectorAll('#view-settings.active *')]
    .filter(node => node.getAnimations().some(a => a instanceof CSSAnimation === false && a.effect?.getKeyframes().some(k => 'height' in k))).length)).toBe(0);
  await noSlide(page);
});

test('Reduced motion: every navigation and disclosure lands at once, correctly', async ({page}) => {
  await page.emulateMedia({reducedMotion: 'reduce'});
  await boot(page);
  const requests = await tenPagesOfDownloads(page);
  await page.goto('/');
  await clear(page);
  await entry(page, 'torrents').click();
  await expect(page.locator('#view-torrents')).toHaveClass(/\bactive\b/);
  await expect(page.locator('#torrent-page-btns .dp-pager-current')).toHaveText('1');
  await page.locator('#torrent-page-btns [aria-label="Next page"]').click();
  await expect(page.locator('#torrent-page-btns .dp-pager-current')).toHaveText('2');
  expect(requests.length).toBeGreaterThan(0);
  await entry(page, 'settings').click();
  await settingsTab(page, 'maintenance').click();
  await expect(page.locator('.view.active [data-panel="maintenance"]')).toBeVisible();
  await settingsTab(page, 'sources').click();
  const bodyId = await page.locator('.view.active .dp-settings-provider-card .dp-settings-disclosure[aria-expanded="false"]').first().getAttribute('aria-controls');
  const chip = page.locator(`.view.active .dp-settings-disclosure[aria-controls="${bodyId}"]`);
  const body = page.locator(`#${bodyId}`);
  await chip.click();
  expect(await body.evaluate(node => [node.hidden, node.getAnimations().length])).toEqual([false, 0]);
  await chip.click();
  expect(await body.evaluate(node => [node.hidden, node.getAnimations().length])).toEqual([true, 0]);
  await noSlide(page);
});

test('Narrow and light: a slide stays inside its surface; nothing overflows the page', async ({page}) => {
  await boot(page, {width: 390, height: 844});
  await page.goto('/');
  await page.evaluate(() => { document.body.classList.add('light'); document.dispatchEvent(new CustomEvent('debridpulse:theme-changed')); });
  await page.evaluate(() => nav(document.querySelector('[data-view="settings"]')));
  await expect(settingsTab(page, 'sources')).toHaveAttribute('aria-selected', 'true');
  await clear(page);
  await settingsTab(page, 'extraction').evaluate(node => node.click());
  const slide = await lastSlide(page);
  expectSlide(slide, {scope: 'settings', axis: 'x', direction: 'forward', step: 'jump'});
  expect(slide.overflow).toBeLessThanOrEqual(0);
  expect(slide.clip).toEqual(['hidden']);                          // the travelling content is clipped to its surface
});
