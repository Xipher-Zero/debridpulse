const { test, expect } = require('@playwright/test');

/* DP 1.0.13 Activity Log Auto Refresh. The journal is answered by the page
 * itself (the GET /events cursor contract over a journal whose newest edge the
 * test moves), and the application's one live stream is either held or answered
 * here; nothing is written to the shared backend. A hint is what app.js
 * forwards from the stream (debridpulse:activity-journal-changed); the stream's
 * state is what app.js publishes (data-dp-pulse and the pulse events). */

const LIMIT = 50;

function journal() {
  const state = {top: 457};
  const event = id => ({id, severity: 'info', category: 'transfer', type: 'transfer.synthetic',
                         message: `Event ${id}`, transfer_id: id, occurred_at: '2026-10-09T12:00:00Z'});
  state.answer = q => {
    const snapshot = q.snapshot !== undefined ? Number(q.snapshot) : state.top;
    const limit = Number(q.limit) || LIMIT;
    let upper = snapshot;
    if (q.before) upper = Math.min(snapshot, Number(q.before) - 1);
    const ids = [];
    for (let id = upper; id >= 1 && ids.length < limit + 1; id--) ids.push(id);
    const items = ids.slice(0, limit).map(event);
    const more = ids.length > limit;
    return {items, limit, snapshot, has_more: more, next_before: more ? items.at(-1).id : null,
            more_after_next: more && items.at(-1).id - 1 > limit, newer_available: state.top > snapshot,
            window_start: null, search: {text: 'none', pending: 0, complete: true}};
  };
  return state;
}

/* `startOff`: the operator switched Auto Refresh off earlier in this tab
 * session (its sessionStorage choice), so the case starts from OFF; a choice
 * made during the case is kept across its own reloads. */
async function open(page, {stream, startOff = false} = {}) {
  if (startOff) await page.addInitScript(() => {
    if (sessionStorage.getItem('dp-activity-auto-refresh') === null) sessionStorage.setItem('dp-activity-auto-refresh', 'off');
  });
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.route('**/api/events/stream', stream || (route => route.abort()));
  const log = journal(), requests = [];
  await page.route('**/api/events?**', route => {
    const q = Object.fromEntries(new URL(route.request().url()).searchParams);
    requests.push(q);
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(log.answer(q))});
  });
  await page.goto('/');
  await page.evaluate(() => { settingsData.activity_log_page_size = 50; });
  await page.locator('#sidebar .nav-item[data-view="events"]').click();
  await expect(page.locator('#event-list .dp-activity-row').first()).toHaveText(/Event 457/);
  return {log, requests};
}

// What app.js does when its one stream connects, loses its connection, or forwards a hint.
const connected = page => page.evaluate(() => {
  document.documentElement.dataset.dpPulse = 'connected';
  document.dispatchEvent(new CustomEvent('debridpulse:pulse-connected'));
});
const lost = page => page.evaluate(() => {
  document.documentElement.dataset.dpPulse = 'lost';
  document.dispatchEvent(new CustomEvent('debridpulse:pulse-lost'));
});
const hint = (page, latest) => page.evaluate(value =>
  document.dispatchEvent(new CustomEvent('debridpulse:activity-journal-changed', {detail: {latest: value}})), latest);
// Longer than the owner's coalescing window: anything it would fetch has been asked for.
const settle = page => page.waitForTimeout(1200);
const rows = page => page.locator('#event-list .dp-activity-row');
const ids = async page => (await rows(page).locator('.dp-activity-message').allTextContents())
  .map(text => Number(text.replace('Event ', '')));

async function geometry(page) {
  return page.evaluate(() => {
    const box = node => node.getBoundingClientRect();
    const island = document.querySelector('.dp-activity-live'), refresh = document.querySelector('.dp-activity-refresh');
    const header = document.querySelector('#view-events .dp-activity-card > .card-header');
    const style = getComputedStyle(island);
    // Where the title's text is actually drawn, overflow included -- not its box.
    const text = document.createRange();
    text.selectNodeContents(header.querySelector('.dp-activity-card-title'));
    const apart = (a, b) => a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top;
    const drawn = [...text.getClientRects()].filter(rect => rect.width && rect.height);
    return {island: box(island).toJSON(), refresh: box(refresh).toJSON(),
            titleClear: drawn.every(rect => apart(rect, box(island)) && apart(rect, box(refresh))),
            adjacent: island.parentElement === refresh.parentElement && island.nextElementSibling === refresh,
            border: `${style.borderTopWidth} ${style.borderTopStyle}`, radius: style.borderTopLeftRadius,
            borderColor: style.borderTopColor,
            divider: (() => { const probe = document.createElement('div'); probe.style.color = 'var(--dp-divider)';
              header.appendChild(probe); const color = getComputedStyle(probe).color; probe.remove(); return color; })(),
            overflow: header.scrollWidth - header.clientWidth,
            label: island.querySelector('.dp-activity-live-label').textContent};
  });
}

test('Auto Refresh: one bordered island left of Refresh, Refresh\'s height, on in a new tab session, a keyboard switch',
  async ({page}) => {
    const {requests} = await open(page);
    const toggle = page.locator('#ev-live');
    await expect(toggle).toBeChecked();                        // a new tab session starts on
    await expect(page.getByRole('switch', {name: 'Auto Refresh'})).toHaveCount(1);
    await expect(page.locator('.dp-activity-refresh')).toHaveText('Refresh');
    for (const [width, theme] of [[1440, 'dark'], [390, 'light']]) {
      await page.setViewportSize({width, height: 900});
      await page.evaluate(light => document.body.classList.toggle('light', light), theme === 'light');
      const g = await geometry(page);
      expect(g.adjacent).toBe(true);
      expect(g.label).toBe('Auto Refresh');
      expect(Math.abs(g.island.height - g.refresh.height)).toBeLessThan(0.5);
      expect(Math.abs(g.island.top - g.refresh.top)).toBeLessThan(0.5);
      expect(g.island.right).toBeLessThan(g.refresh.left);
      expect(g.border).toBe('1px solid');
      expect(g.radius).toBe('12px');
      expect(g.borderColor).toBe(g.divider);                     // the theme's own divider token
      expect(g.overflow).toBeLessThanOrEqual(0);
      expect(g.titleClear).toBe(true);                           // the title never runs under the controls
    }
    await page.setViewportSize({width: 1440, height: 1000});
    await page.evaluate(() => document.body.classList.remove('light'));

    await toggle.focus();
    await page.keyboard.press('Space');
    await expect(toggle).not.toBeChecked();
    expect(await page.locator('.dp-activity-live .ttrack').evaluate(node => getComputedStyle(node).outlineStyle))
      .toBe('solid');                                          // visible focus
    await page.keyboard.press('Space');
    await expect(toggle).toBeChecked();
    expect(requests.length).toBeGreaterThanOrEqual(1);
  });

test('Auto Refresh: the operator\'s choice lasts for the tab session -- other views, inactivity, reloads -- and a new tab session starts on',
  async ({page}) => {
    const {requests} = await open(page);
    const toggle = page.locator('#ev-live');
    await expect(toggle).toBeChecked();
    await page.locator('.dp-activity-live').click();           // the operator switches it off
    await expect(toggle).not.toBeChecked();
    await page.locator('#sidebar .nav-item[data-view="dashboard"]').click();
    const before = requests.length;
    await hint(page, 500);
    await settle(page);
    expect(requests.length).toBe(before);                      // nothing fetched while away
    await page.locator('#sidebar .nav-item[data-view="events"]').click();
    await expect(toggle).not.toBeChecked();                    // back: still off
    // In the background (the document hidden, then shown again): still off, however long it was away.
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await expect(toggle).not.toBeChecked();
    await page.reload();
    await page.locator('#sidebar .nav-item[data-view="events"]').click();
    await expect(page.locator('#ev-live')).not.toBeChecked();  // reloaded, same tab session: still off
    await page.locator('.dp-activity-live').click();           // and switched back on
    await page.locator('#sidebar .nav-item[data-view="dashboard"]').click();
    await page.locator('#sidebar .nav-item[data-view="events"]').click();
    await expect(page.locator('#ev-live')).toBeChecked();
    await page.locator('.dp-activity-live').click();           // off once more, then a new tab session
    expect(await page.evaluate(() => sessionStorage.getItem('dp-activity-auto-refresh'))).toBe('off');
    const other = await page.context().newPage();
    try {
      await open(other);
      await expect(other.locator('#ev-live')).toBeChecked();   // a new tab session: on again
      await expect(page.locator('#ev-live')).not.toBeChecked(); // the first tab keeps its own choice
    } finally { await other.close(); }
  });

test('Auto Refresh on from entry: the newest page follows the journal, and switching off freezes it while Refresh still reads', async ({page}) => {
  const {log, requests} = await open(page);
  await connected(page);
  await expect(page.locator('.dp-activity-live')).toHaveAttribute('data-state', 'live');
  log.top = 466;
  await hint(page, 466);                                       // on by default: one newest page from the journal
  await expect(rows(page).first()).toHaveText(/Event 466/);
  const asked = requests.length;
  await page.locator('.dp-activity-live').click();             // off: the page holds still
  log.top = 480;
  await hint(page, 480);
  await settle(page);
  expect(requests.length).toBe(asked);
  expect((await ids(page))[0]).toBe(466);
  await page.locator('.dp-activity-refresh').click();          // Refresh still reads the journal
  await expect(rows(page).first()).toHaveText(/Event 480/);
  // A live update never slides the list.
  expect(await page.evaluate(() => !!document.querySelector('.dp-motion-surface'))).toBe(false);
});

test('Auto Refresh: off changes nothing; on, a burst becomes one newest page from the journal', async ({page}) => {
  const {log, requests} = await open(page, {startOff: true});
  await connected(page);
  log.top = 470;
  await hint(page, 470);                                       // OFF: the page stays as it is
  await settle(page);
  expect(requests).toHaveLength(1);
  expect((await ids(page))[0]).toBe(457);

  await page.locator('.dp-activity-live').click();                      // ON: a new investigation at the newest event
  await expect.poll(() => requests.length).toBe(2);
  expect(requests[1].snapshot).toBeUndefined();
  await expect(rows(page).first()).toHaveText(/Event 470/);
  await expect(page.locator('.dp-activity-live')).toHaveAttribute('data-state', 'live');

  log.top = 470 + 3 * LIMIT;                                   // a burst larger than the page
  for (const latest of [480, 560, log.top]) await hint(page, latest);
  await settle(page);
  expect(requests).toHaveLength(3);                            // coalesced into one request
  expect(requests[2]).toMatchObject({limit: String(LIMIT), timeframe: 'all'});
  expect(requests[2].snapshot).toBeUndefined();
  const first = await ids(page);
  expect(first).toHaveLength(LIMIT);                           // bounded
  expect(first[0]).toBe(log.top);
  await hint(page, log.top);                                   // a repeated hint: already shown
  await settle(page);
  expect(requests).toHaveLength(3);

  await page.locator('#dp-activity-page-btns [aria-label="Next page"]').click();
  await expect(page.locator('#dp-activity-page-btns .dp-pager-current')).toHaveText('2');
  expect(requests[3]).toMatchObject({snapshot: String(log.top), before: String(first.at(-1))});
  expect((await ids(page))[0]).toBe(first.at(-1) - 1);          // no hole, no duplicate

  const refreshed = requests.length;
  await page.locator('.dp-activity-refresh').click();          // Refresh: today's action, and Auto Refresh stays on
  await expect.poll(() => requests.length).toBe(refreshed + 1);
  expect(requests.at(-1).snapshot).toBeUndefined();
  await expect(page.locator('#dp-activity-page-btns .dp-pager-current')).toHaveText('1');
  await expect(page.locator('#ev-live')).toBeChecked();
});

test('Auto Refresh: an older page, or the newest page read below its top, is never replaced', async ({page}) => {
  const {log, requests} = await open(page, {startOff: true});
  await connected(page);
  await page.locator('.dp-activity-live').click();
  await expect.poll(() => requests.length).toBe(2);

  await page.locator('#dp-activity-page-btns [aria-label="Next page"]').click();
  await expect(page.locator('#dp-activity-page-btns .dp-pager-current')).toHaveText('2');
  const frozen = await ids(page), asked = requests.length;
  await expect(page.locator('#ev-newest')).toBeHidden();
  log.top = 470;
  await hint(page, 470);
  await settle(page);
  expect(requests).toHaveLength(asked);                        // the frozen page is not fetched again
  expect(await ids(page)).toEqual(frozen);
  await expect(page.locator('#dp-activity-page-btns .dp-pager-current')).toHaveText('2');
  await expect(page.locator('#ev-newest')).toBeVisible();      // newer events exist
  await page.locator('#ev-newest').click();                    // deliberately back to the newest
  await expect(rows(page).first()).toHaveText(/Event 470/);
  await expect(page.locator('#dp-activity-page-btns .dp-pager-current')).toHaveText('1');

  const list = page.locator('#event-list');
  await list.evaluate(node => { node.scrollTop = 600; });
  const reading = await list.evaluate(node => node.scrollTop);
  expect(reading).toBeGreaterThan(100);
  const before = requests.length, shown = await ids(page);
  log.top = 480;
  await hint(page, 480);
  await settle(page);
  expect(requests).toHaveLength(before);                       // nothing moves under the reader
  expect(await ids(page)).toEqual(shown);
  expect(await list.evaluate(node => node.scrollTop)).toBe(reading);
  await expect(page.locator('#ev-newest')).toBeVisible();
  await list.evaluate(node => { node.scrollTop = 0; node.dispatchEvent(new Event('scroll')); });
  await expect.poll(() => requests.length).toBe(before + 1);   // back at the top: followed again
  await expect(rows(page).first()).toHaveText(/Event 480/);
});

test('Auto Refresh: a lost stream is said, a reconnect is caught up from the journal, and stale or late work never lands',
  async ({page}) => {
    const {log, requests} = await open(page, {startOff: true});
    const island = page.locator('.dp-activity-live');
    await connected(page);
    await page.locator('.dp-activity-live').click();
    await expect.poll(() => requests.length).toBe(2);
    await expect(island).toHaveAttribute('data-state', 'live');

    await lost(page);
    await expect(island).toHaveAttribute('data-state', 'unavailable');
    await expect(page.locator('#ev-live')).toHaveAccessibleDescription(/not connected/);
    log.top = 475;                                             // committed while the stream was down
    await settle(page);
    expect(requests).toHaveLength(2);                          // no polling of its own meanwhile
    await connected(page);
    await expect.poll(() => requests.length).toBe(3);          // reconnect: caught up from the journal
    await expect(rows(page).first()).toHaveText(/Event 475/);
    await expect(island).toHaveAttribute('data-state', 'live');

    log.top = 480;
    await hint(page, 480);
    await page.locator('.dp-activity-live').click();                  // off before the burst settles: nothing fetched
    await settle(page);
    expect(requests).toHaveLength(3);
    await expect(rows(page).first()).toHaveText(/Event 475/);

    await page.locator('.dp-activity-live').click();
    await expect.poll(() => requests.length).toBe(4);
    await page.locator('#ev-level').selectOption('error');     // a filter change is the newest request
    await hint(page, 490);
    await settle(page);
    const after = requests.slice(4);
    expect(after.length).toBeGreaterThanOrEqual(1);
    expect(after.every(q => q.level === 'error')).toBe(true);  // no old-filter answer replaces it
  });

test('Auto Refresh: app.js forwards the stream\'s journal hint from the one EventSource', async ({page}) => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await page.addInitScript(() => {
    window.__hints = [];
    document.addEventListener('debridpulse:activity-journal-changed', event => window.__hints.push(event.detail));
  });
  const {requests} = await open(page, {startOff: true, stream: async route => {
    await gate;
    await route.fulfill({status: 200, contentType: 'text/event-stream',
      body: 'event: connected\ndata: {}\n\nevent: activity_journal_changed\ndata: {"latest": 457}\n\n'});
  }});
  await page.locator('.dp-activity-live').click();
  release();
  await expect.poll(() => page.evaluate(() => window.__hints)).toEqual([{latest: 457}]);
  // The stream then closes: the switch stops claiming to be live.
  await expect(page.locator('.dp-activity-live')).toHaveAttribute('data-state', 'unavailable');
  expect(requests.every(q => q.limit === String(LIMIT))).toBe(true);
});

test('Auto Refresh: a refresh that fails keeps the page, its pager and its snapshot, and raises no toast', async ({page}) => {
  const {log, requests} = await open(page, {startOff: true});
  await connected(page);
  await page.locator('.dp-activity-live').click();
  await expect.poll(() => requests.length).toBe(2);
  const shown = await ids(page);
  await page.route('**/api/events?**', route => {
    requests.push(Object.fromEntries(new URL(route.request().url()).searchParams));
    return route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({detail: 'unavailable'})});
  });
  log.top = 470;
  await hint(page, 470);
  await expect.poll(() => requests.length).toBe(3);
  await settle(page);
  expect(await ids(page)).toEqual(shown);                      // the page stays
  await expect(page.locator('#toasts .toast')).toHaveCount(0);
  await page.unroute('**/api/events?**');
  await page.route('**/api/events?**', route => {
    const q = Object.fromEntries(new URL(route.request().url()).searchParams);
    requests.push(q);
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(log.answer(q))});
  });
  await page.locator('#dp-activity-page-btns [aria-label="Next page"]').click();   // its pager still works
  await expect(page.locator('#dp-activity-page-btns .dp-pager-current')).toHaveText('2');
  expect(requests.at(-1)).toMatchObject({snapshot: '457', before: String(shown.at(-1))});
});
