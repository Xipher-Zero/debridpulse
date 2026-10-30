const { test, expect } = require('@playwright/test');

/* DP 1.0.14 Activity Log handheld header and filter control (320-779px only).
 * - The card header is one row at every handheld width: the artwork, the title
 *   over its hint, Auto Refresh and Refresh. Where it is short it gives way in
 *   one order -- the title and the hint each the longest approved wording drawn
 *   whole (the hint none at all where even its shortest is not), then Refresh as
 *   its glyph alone, then Auto Refresh out of the row -- each step only where the
 *   title would otherwise be shorter than "Activity".
 * - Out of the row, Auto Refresh is on, whatever the tab session's choice, which
 *   it never writes; back in the row that choice holds.
 * - The filter control reads Filter / Clear in one compact footprint; Search
 *   takes the rest. From 780px all of it is the desktop's, unchanged.
 * Engine: Chromium (Playwright), CSS viewport sizes; the web face with
 * DP_WEB_FONTS=1, else the fallback face. The journal is answered here. */

const WEB_FONTS = process.env.DP_WEB_FONTS === '1';
const TITLES = ['For the Record', 'The Records', 'Activity', 'Logs'];
const HINTS = ['Everything DebridPulse thought was worth mentioning.', 'Things worth writing down.', 'Worth noting.', ''];

function journal() {
  const state = {top: 457};
  const event = id => ({id, severity: 'info', category: 'transfer', type: 'transfer.synthetic',
    message: `Event ${id}`, transfer_id: id, occurred_at: '2026-10-09T12:00:00Z'});
  state.answer = q => {
    const snapshot = q.snapshot !== undefined ? Number(q.snapshot) : state.top, limit = Number(q.limit) || 50;
    let upper = snapshot;
    if (q.before) upper = Math.min(snapshot, Number(q.before) - 1);
    const ids = [];
    for (let id = upper; id >= 1 && ids.length < limit + 1; id--) ids.push(id);
    const items = ids.slice(0, limit).map(event), more = ids.length > limit;
    return {items, limit, snapshot, has_more: more, next_before: more ? items.at(-1).id : null, more_after_next: more && items.at(-1).id - 1 > limit,
      newer_available: state.top > snapshot, window_start: null, search: {text: 'none', pending: 0, complete: true}};
  };
  return state;
}

async function open(page, {width = 390, height = 844, startOff = false} = {}) {
  if (startOff) await page.addInitScript(() => {
    if (sessionStorage.getItem('dp-activity-auto-refresh') === null) sessionStorage.setItem('dp-activity-auto-refresh', 'off');
  });
  if (!WEB_FONTS) await page.route(/fonts\.(googleapis|gstatic)\.com/, route => route.abort());
  await page.route('**/api/events/stream', route => route.abort());
  const log = journal(), requests = [];
  await page.route('**/api/events?**', route => {
    const q = Object.fromEntries(new URL(route.request().url()).searchParams);
    requests.push(q);
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(log.answer(q))});
  });
  await page.setViewportSize({width, height});
  await page.goto('/');
  await page.evaluate(() => { settingsData.activity_log_page_size = 50; });
  await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="events"]')));
  await expect(page.locator('#event-list .dp-activity-row').first()).toHaveText(/Event 457/);
  await page.evaluate(() => document.fonts.ready);
  return {log, requests};
}

async function resize(page, width, height = 844) {
  await page.setViewportSize({width, height});
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}

// The header as drawn: its arrangement, one row or not, the wordings shown, whether each is
// whole, and whether the next longer wording would have been whole in the same arrangement.
const header = page => page.evaluate(({TITLES, HINTS}) => {
  const h = document.querySelector('#view-events > .dp-activity-card > .card-header'), box = node => node.getBoundingClientRect();
  const parts = ['.dp-activity-title-icon', '.dp-activity-heading-copy', '.dp-activity-live', '.dp-activity-refresh']
    .map(selector => h.querySelector(selector)).filter(node => node.getClientRects().length);
  const middle = node => (box(node).top + box(node).bottom) / 2;
  const line = (node, list) => {
    const kept = node.dataset.dpCopy, shown = kept ?? node.textContent, at = list.indexOf(shown);
    const whole = shown === '' || node.scrollWidth <= node.clientWidth;
    let longer = false;
    if (at > 0) {
      if (at - 1 === 0) delete node.dataset.dpCopy; else node.dataset.dpCopy = list[at - 1];
      longer = node.scrollWidth <= node.clientWidth;
      if (kept === undefined) delete node.dataset.dpCopy; else node.dataset.dpCopy = kept;
    }
    return {shown, whole, longer};
  };
  const live = h.querySelector('.dp-activity-live'), refresh = h.querySelector('.dp-activity-refresh');
  return {fit: h.dataset.dpFit ?? '', oneRow: parts.every(node => Math.abs(middle(node) - middle(parts[0])) < 14),
    inside: parts.every(node => box(node).left >= box(h).left && box(node).right <= box(h).right),
    title: line(h.querySelector('.dp-activity-heading'), TITLES), hint: line(h.querySelector('.dp-activity-subtitle'), HINTS),
    artwork: box(h.querySelector('.dp-activity-title-icon')).width, refresh: box(refresh).width,
    refreshText: getComputedStyle(refresh.querySelector('span')).clipPath === 'inset(50%)' ? 'glyph' : 'label',
    liveShown: live.getClientRects().length > 0, overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth};
}, {TITLES, HINTS});

test('one header row at every handheld width: the longest approved wordings drawn whole, Refresh and the artwork always there', async ({page}) => {
  await open(page, {width: 320});
  let seenGlyph = false;
  for (const width of [320, 360, 390, 414, 430, 480, 560, 600, 700, 779]) {
    await resize(page, width);
    const h = await header(page);
    const at = `${width}px [${h.fit}]`;
    expect(h.oneRow, `${at}: one row`).toBe(true);
    expect(h.inside && h.overflow === 0, `${at}: inside the card, no sideways scroll`).toBe(true);
    expect(h.artwork, `${at}: the artwork`).toBeGreaterThan(30);
    expect(h.refresh, `${at}: Refresh`).toBeGreaterThan(30);
    expect(TITLES).toContain(h.title.shown);
    expect(HINTS).toContain(h.hint.shown);
    for (const [name, line] of [['title', h.title], ['hint', h.hint]]) {
      expect(line.whole, `${at}: the ${name} "${line.shown}" drawn whole`).toBe(true);
      expect(line.longer, `${at}: the ${name} is the longest wording that fits`).toBe(false);
    }
    // Each step only where the title would otherwise be shorter than "Activity": at these widths it never is.
    expect(TITLES.indexOf(h.title.shown), `${at}: an intelligible title`).toBeLessThanOrEqual(2);
    expect(h.refreshText === 'glyph', `${at}: Refresh is its glyph exactly when the arrangement says so`).toBe(h.fit.includes('icon'));
    expect(h.liveShown, `${at}: Auto Refresh in the row exactly when the arrangement keeps it`).toBe(!h.fit.includes('quiet'));
    seenGlyph ||= h.refreshText === 'glyph';
    if (width === 779) expect([h.title.shown, h.hint.shown, h.fit], '779px: the full wordings, every control').toEqual([TITLES[0], HINTS[0], '']);
  }
  await page.screenshot({path: 'test-results/checkpoint-activity-mobile-header.png', clip: {x: 0, y: 0, width: 779, height: 260}});
  // 780px and up: the desktop header, nothing of this applied.
  await resize(page, 780);
  expect(await page.evaluate(() => {
    const h = document.querySelector('#view-events > .dp-activity-card > .card-header');
    return [h.dataset.dpFit, h.querySelector('.dp-activity-heading').dataset.dpCopy, h.querySelector('.dp-activity-subtitle').dataset.dpCopy, h.querySelector('.dp-activity-refresh').textContent.trim()];
  })).toEqual([undefined, undefined, undefined, 'Refresh']);
});

test('Refresh as its glyph alone keeps its name, tooltip and keyboard focus, and still refreshes', async ({page}) => {
  const {requests} = await open(page, {width: 480});
  // The first width (narrowing) where the arrangement draws Refresh as its glyph.
  let width = 480;
  for (; width >= 320; width -= 2) { await resize(page, width); if ((await header(page)).fit.includes('icon')) break; }
  expect(width, 'a width where Refresh is its glyph').toBeGreaterThanOrEqual(320);
  const refresh = page.locator('#view-events .dp-activity-refresh');
  await expect(refresh).toHaveAccessibleName('Refresh activity log');
  await expect(refresh).toHaveAttribute('data-tooltip', 'Refresh activity log');
  await refresh.focus();
  await expect(refresh).toBeFocused();
  const before = requests.length;
  await page.keyboard.press('Enter');
  await expect.poll(() => requests.length).toBeGreaterThan(before);
});

test('Auto Refresh out of the row is on, never writing the session choice; back in the row, the choice (off) holds', async ({page}) => {
  const {log, requests} = await open(page, {width: 779, startOff: true});
  const toggle = page.locator('#ev-live');
  const hint = latest => page.evaluate(value => document.dispatchEvent(new CustomEvent('debridpulse:activity-journal-changed', {detail: {latest: value}})), latest);
  await expect(toggle).not.toBeChecked();
  // Off, in the row: a journal hint brings nothing.
  log.top = 460;
  let before = requests.length;
  await hint(460);
  await page.waitForTimeout(1200);                      // longer than the owner's coalescing window
  expect(requests.length).toBe(before);
  // Narrowed until Auto Refresh leaves the row: on, following the journal; the choice still off.
  await resize(page, 320);
  expect((await header(page)).liveShown).toBe(false);
  await expect(page.locator('#view-events .dp-activity-live')).toBeHidden();
  expect(await toggle.evaluate(node => node.checkVisibility())).toBe(false);   // nothing to reach or toggle
  before = requests.length;
  log.top = 463;
  await hint(463);
  await expect.poll(() => requests.length).toBe(before + 1);   // one new investigation, no storm
  await expect(page.locator('#event-list .dp-activity-row').first()).toHaveText(/Event 463/);
  expect(await page.evaluate(() => sessionStorage.getItem('dp-activity-auto-refresh'))).toBe('off');
  // Back in the row: off again, as chosen.
  await resize(page, 779);
  await expect(page.locator('#view-events .dp-activity-live')).toBeVisible();
  await expect(toggle).not.toBeChecked();
  before = requests.length;
  log.top = 466;
  await hint(466);
  await page.waitForTimeout(1200);
  expect(requests.length).toBe(before);
  // And on desktop, the same choice.
  await resize(page, 1024);
  await expect(toggle).not.toBeChecked();
});

test('Auto Refresh left on (the default) stays on in and out of the row, one request per hint', async ({page}) => {
  const {log, requests} = await open(page, {width: 779});
  const toggle = page.locator('#ev-live');
  for (const width of [779, 320, 779]) {
    await resize(page, width);
    await expect(toggle).toBeChecked();
    const before = requests.length;
    log.top += 2;
    await page.evaluate(value => document.dispatchEvent(new CustomEvent('debridpulse:activity-journal-changed', {detail: {latest: value}})), log.top);
    await expect.poll(() => requests.length, `${width}px`).toBe(before + 1);
    await page.waitForTimeout(1200);
    expect(requests.length, `${width}px: no further requests`).toBe(before + 1);
  }
  expect(await page.evaluate(() => sessionStorage.getItem('dp-activity-auto-refresh'))).not.toBe('off');
});

test('the filter control reads Filter / Clear in one compact footprint below 780px, Search taking the rest; Filter Results / Clear Filters at 140px from 780px', async ({page}) => {
  await open(page, {width: 320});
  const read = () => page.evaluate(() => {
    const button = document.getElementById('ev-filter'), b = button.getBoundingClientRect(), s = document.getElementById('ev-search').getBoundingClientRect();
    return {text: button.textContent.trim(), name: button.getAttribute('aria-describedby') ? document.getElementById('ev-filter-applied').textContent : '',
      tooltip: button.dataset.tooltip ?? null, width: Math.round(b.width * 100) / 100, search: Math.round(s.width * 100) / 100, fits: button.scrollWidth <= button.clientWidth,
      // The room Search could take: the row's content width less the control and the row's gap.
      room: (() => { const row = button.parentElement, cs = getComputedStyle(row); return Math.round((row.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight) - b.width - parseFloat(cs.columnGap)) * 100) / 100; })()};
  });
  const apply = async () => {
    await page.locator('#ev-filter').click();
    await page.locator('#ev-filter-panel input[name="ev-filter-level"][value="error"]').check({force: true});
    await page.locator('#ev-filter-panel [data-filter-action="apply"]').click();
  };
  for (const width of [320, 390]) {
    await resize(page, width);
    const filter = await read();
    await apply();
    await expect(page.locator('#ev-filter-label')).toHaveText('Clear');
    const clear = await read();
    await page.locator('#ev-filter').click();
    await expect(page.locator('#ev-filter-label')).toHaveText('Filter');
    const again = await read();
    expect([filter.text, clear.text, again.text], `${width}px`).toEqual(['Filter', 'Clear', 'Filter']);
    expect(filter.width, `${width}px: the compact footprint`).toBe(96);
    expect([clear.width, again.width, clear.search, again.search], `${width}px: one footprint, Search never moves`).toEqual([96, 96, filter.search, filter.search]);
    expect(filter.fits && clear.fits, `${width}px: each label whole`).toBe(true);
    // What Clear clears stays said: its description and tooltip name what is applied.
    expect(clear.name).toContain('Error');
    expect(clear.tooltip).toBe(`Clear filters — ${clear.name}`);
    // Search takes all the room the control leaves (44px more than beside the desktop's 140px footprint).
    expect(Math.abs(filter.search - filter.room), `${width}px: Search takes the rest`).toBeLessThanOrEqual(0.5);
  }
  await resize(page, 780);
  const desk = await read();
  await apply();
  await expect(page.locator('#ev-filter-label')).toHaveText('Clear Filters');
  const deskClear = await read();
  expect([desk.text, desk.width, deskClear.width]).toEqual(['Filter Results', 140, 140]);
  // Back to a handheld width with a restriction applied: the short label at once.
  await resize(page, 390);
  await expect(page.locator('#ev-filter-label')).toHaveText('Clear');
});
