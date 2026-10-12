const { test, expect } = require('@playwright/test');

/* DP 1.0.13 UI consistency: the one DP pager (Downloads and the Activity Log),
 * Activity Log First/Last within a frozen snapshot, and sidebar entry resets.
 * Every collection here is answered by the page itself; nothing is written to
 * the shared backend. */

const PAGES = 10;

async function isolate(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.route('**/api/events/stream', route => route.abort());
}

// Downloads: always exactly ten pages of whatever page size the view measured.
async function tenPagesOfDownloads(page) {
  const requests = [];
  await page.route('**/api/torrents**', route => {
    const url = new URL(route.request().url());
    if (url.pathname !== '/api/torrents' || !url.searchParams.has('offset')) return route.fallback();
    const limit = Math.max(1, Number(url.searchParams.get('limit')) || 25);
    const offset = Math.max(0, Number(url.searchParams.get('offset')) || 0);
    requests.push({limit, offset});
    const total = PAGES * limit;
    const items = Array.from({length: Math.max(0, Math.min(limit, total - offset))}, (_, index) => {
      const id = offset + index + 1;
      return {id, name: `Transfer ${id}`, hash: `hash-${id}`, status: 'downloading', progress: 10,
              size_bytes: 1024, created_at: '2026-10-01T12:00:00Z', source: 'direct_link', label: null,
              current_provider_id: 'general_http', current_provider_name: 'HTTP & HTTPS',
              provider_provenance_status: 'known', extraction_status: null, source_failure_count: 0};
    });
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({items, total})});
  });
  return requests;
}

const downloadsPager = page => page.locator('#torrent-page-btns');
const activityPager = page => page.locator('#dp-activity-page-btns');

/* The five positions, left to right: each the label of the control shown
 * there, '.' for an empty reserved slot, or the current page number. */
async function slots(pager) {
  return pager.evaluate(group => [...group.children].map(slot => {
    const child = slot.firstElementChild;
    if (!child || child.classList.contains('dp-pager-placeholder')) return '.';
    if (child.getAttribute('aria-current') === 'page') return child.textContent;
    return child.getAttribute('aria-label');
  }));
}

const EXPECTED = {
  1: ['.', '.', '1', 'Next page', 'Last page'],
  2: ['.', 'Previous page', '2', 'Next page', 'Last page'],
  5: ['First page', 'Previous page', '5', 'Next page', 'Last page'],
  9: ['First page', 'Previous page', '9', 'Next page', '.'],
  10: ['First page', 'Previous page', '10', '.', '.'],
};

async function centre(pager) {
  return pager.evaluate(group => {
    const box = group.getBoundingClientRect(), current = group.querySelector('.dp-pager-current').getBoundingClientRect();
    return {groupCentre: Math.round(box.left + box.width / 2), currentCentre: Math.round(current.left + current.width / 2),
            width: Math.round(current.width), height: Math.round(current.height)};
  });
}

async function boundaryGlyphs(pager) {
  return pager.evaluate(group => Object.fromEntries([...group.querySelectorAll('button.dp-pager-btn')]
    .map(button => [button.dataset.dpPager, button.querySelector('svg')?.innerHTML || ''])));
}

test('Downloads: one five-slot pager, contextual controls, a fixed centre, and First/Last by page arithmetic',
  async ({page}) => {
    await isolate(page);
    const requests = await tenPagesOfDownloads(page);
    await page.goto('/');
    await page.locator('#sidebar .nav-item[data-view="torrents"]').click();
    const pager = downloadsPager(page);
    await expect(pager.locator('.dp-pager-current')).toHaveText('1');
    expect(await slots(pager)).toEqual(EXPECTED[1]);
    const anchor = await centre(pager);
    expect(anchor).toMatchObject({width: 36, height: 34});
    expect(anchor.currentCentre).toBe(anchor.groupCentre);

    await pager.locator('[aria-label="Last page"]').click();
    await expect(pager.locator('.dp-pager-current')).toHaveText('10');
    expect(requests.at(-1).offset).toBe(9 * requests.at(-1).limit);
    expect(await slots(pager)).toEqual(EXPECTED[10]);
    await pager.locator('[aria-label="Previous page"]').click();
    await expect(pager.locator('.dp-pager-current')).toHaveText('9');
    expect(await slots(pager)).toEqual(EXPECTED[9]);
    const glyphs = await boundaryGlyphs(pager);
    expect(glyphs.first).toContain('M7 6v12');                       // Lucide chevron-first
    await page.evaluate(() => goToTorrentPage(5));
    await expect(pager.locator('.dp-pager-current')).toHaveText('5');
    expect(await slots(pager)).toEqual(EXPECTED[5]);
    expect(glyphs.last ?? (await boundaryGlyphs(pager)).last).toContain('M17 6v12');  // Lucide chevron-last
    expect((await centre(pager)).currentCentre).toBe(anchor.currentCentre);
    await pager.locator('[aria-label="First page"]').click();
    await expect(pager.locator('.dp-pager-current')).toHaveText('1');
    expect(requests.at(-1).offset).toBe(0);
    await page.evaluate(() => goToTorrentPage(2));
    await expect(pager.locator('.dp-pager-current')).toHaveText('2');
    expect(await slots(pager)).toEqual(EXPECTED[2]);
    // A status badge on these rows is the DP 6px rounded rectangle.
    expect(await page.locator('#t-tbody .badge').first().evaluate(node => getComputedStyle(node).borderRadius)).toBe('6px');
  });

/* The journal for one frozen snapshot: 10 pages of `limit`, with a short last
 * page, answered by the same cursor contract as GET /events. */
function journal(limit) {
  const total = (PAGES - 1) * limit + 7, snapshot = total;
  const ids = Array.from({length: total}, (_, index) => total - index);   // newest first
  const event = id => ({id, severity: 'info', category: 'transfer', type: 'transfer.synthetic',
                         message: `Event ${id}`, transfer_id: id, occurred_at: '2026-10-09T12:00:00Z'});
  return {snapshot, answer(q) {
    let start = 0, items, page;
    if (q.last) { page = PAGES; start = (PAGES - 1) * limit; }
    else if (q.after) { const above = ids.filter(id => id > Number(q.after)); start = Math.max(0, above.length - limit); }
    else if (q.before) start = ids.findIndex(id => id < Number(q.before));
    items = ids.slice(start, start + limit).map(event);
    const older = ids.length - (start + items.length);
    return {items, limit, snapshot, has_more: older > 0, next_before: older > 0 ? items.at(-1).id : null,
            more_after_next: older > limit, newer_available: false, ...(q.after || q.last ? {has_newer: start > 0} : {}),
            ...(q.last ? {page} : {}), search: {text: 'none', pending: 0, complete: true},
            // The cutoff a relative window was measured with: the first page's, echoed back.
            window_start: q.timeframe && q.timeframe !== 'all' ? Number(q.window_start ?? 1760000000.25) : null};
  }};
}

async function activity(page, {hold = false, refuseLast = false} = {}) {
  const requests = [], held = [];
  await page.route('**/api/events?**', async route => {
    const q = Object.fromEntries(new URL(route.request().url()).searchParams);
    requests.push(q);
    const reply = () => {
      if (q.last && refuseLast) {
        return route.fulfill({status: 409, contentType: 'application/json', body: JSON.stringify({detail: {
          code: 'last_page_unavailable', message: 'This history is too large to count for a direct jump to the last page.'}})});
      }
      return route.fulfill({status: 200, contentType: 'application/json',
        body: JSON.stringify(journal(Number(q.limit)).answer(q))});
    };
    if (hold && requests.length > 1) held.push(reply); else await reply();
  });
  await page.goto('/');
  await page.evaluate(() => { settingsData.activity_log_page_size = 50; });
  await page.locator('#sidebar .nav-item[data-view="events"]').click();
  await expect(activityPager(page).locator('.dp-pager-current')).toHaveText('1');
  return {requests, held};
}

test('Activity Log: the same pager; Last and First stay in the frozen snapshot, Previous walks back from Last',
  async ({page}) => {
    await isolate(page);
    const {requests} = await activity(page);
    const pager = activityPager(page), rows = page.locator('#event-list .dp-activity-row');
    const snapshot = requests[0].snapshot ?? null;
    expect(snapshot).toBeNull();
    expect(await slots(pager)).toEqual(EXPECTED[1]);
    await expect(page.locator('#ev-older, #ev-newer')).toHaveCount(0);       // no parallel old controls
    const geometry = await centre(pager);
    expect(geometry).toMatchObject({width: 36, height: 34});
    expect(geometry.currentCentre).toBe(geometry.groupCentre);

    await pager.locator('[aria-label="Last page"]').click();
    await expect(pager.locator('.dp-pager-current')).toHaveText('10');
    expect(requests.at(-1)).toMatchObject({last: 'true', snapshot: '457'});
    await expect(rows).toHaveCount(7);                                        // the remainder, not the oldest 50
    expect(await slots(pager)).toEqual(EXPECTED[10]);
    await expect(page.locator('#dp-activity-page-info')).toHaveText('Page 10 of 10');

    await pager.locator('[aria-label="Previous page"]').click();
    await expect(pager.locator('.dp-pager-current')).toHaveText('9');
    expect(requests.at(-1)).toMatchObject({after: '7', snapshot: '457'});
    await expect(rows.first().locator('.dp-activity-message')).toHaveText('Event 57');
    expect(await slots(pager)).toEqual(EXPECTED[9]);
    await pager.locator('[aria-label="Next page"]').click();
    await expect(pager.locator('.dp-pager-current')).toHaveText('10');
    expect(requests.at(-1)).toMatchObject({before: '8', snapshot: '457'});

    await pager.locator('[aria-label="First page"]').click();
    await expect(pager.locator('.dp-pager-current')).toHaveText('1');
    const first = requests.at(-1);
    expect(first.snapshot).toBe('457');                                       // the same snapshot, not a new one
    expect(first.before ?? first.after ?? first.last).toBeUndefined();
    await expect(rows.first().locator('.dp-activity-message')).toHaveText('Event 457');

    // "Show newest events" is a new investigation, outside the pager.
    await page.evaluate(() => { document.getElementById('ev-newest').hidden = false; });
    await page.locator('#ev-newest').click();
    await expect.poll(() => requests.at(-1).snapshot).toBeUndefined();
    await expect(pager.locator('#ev-newest')).toHaveCount(0);
  });

test('Activity Log: navigation is inert while a page is unresolved, and an unprovable Last says why', async ({page}) => {
  await isolate(page);
  const {requests, held} = await activity(page, {hold: true, refuseLast: true});
  const pager = activityPager(page);
  await pager.locator('[aria-label="Next page"]').click();
  await expect.poll(() => held.length).toBe(1);
  await expect(pager.locator('[aria-label="Next page"]')).toBeDisabled();
  await pager.locator('[aria-label="Next page"]').click({force: true});
  await pager.locator('[aria-label="Last page"]').click({force: true});
  expect(requests).toHaveLength(2);                                           // the first navigation only
  await held.shift()();
  await expect(pager.locator('.dp-pager-current')).toHaveText('2');
  await expect(pager.locator('[aria-label="Next page"]')).toBeEnabled();

  await pager.locator('[aria-label="Last page"]').click();
  await expect.poll(() => held.length).toBe(1);
  await held.shift()();                                                       // refused: 409
  const last = pager.locator('[data-dp-pager="last"]');
  await expect(last).toBeDisabled();
  await expect(last).toHaveAttribute('aria-label', /^Last page unavailable: This history is too large/);
  await expect(pager.locator('.dp-pager-current')).toHaveText('2');          // no fabricated page
  await expect(page.locator('#event-list .dp-activity-row').first().locator('.dp-activity-message')).toHaveText('Event 407');
  await pager.locator('[aria-label="Previous page"]').click();
  await held.shift()?.();
  await expect(pager.locator('.dp-pager-current')).toHaveText('1');
});

test('every sidebar entry opens Downloads page 1, Settings Services and Help Quick Start; a refresh keeps the page',
  async ({page}) => {
    await isolate(page);
    await tenPagesOfDownloads(page);
    await page.goto('/');
    const nav = view => page.locator(`#sidebar .nav-item[data-view="${view}"]`);
    const current = downloadsPager(page).locator('.dp-pager-current');

    await nav('torrents').click();
    await page.evaluate(() => goToTorrentPage(4));
    await expect(current).toHaveText('4');
    await page.evaluate(() => loadTorrents());                               // an ordinary refresh
    await expect(current).toHaveText('4');
    await nav('torrents').click();                                            // reselected
    await expect(current).toHaveText('1');
    await page.evaluate(() => goToTorrentPage(4));
    await expect(current).toHaveText('4');
    await nav('dashboard').click();
    await nav('torrents').click();                                            // re-entered
    await expect(current).toHaveText('1');

    const settingsTab = () => page.locator('#view-settings .stab[aria-selected="true"]');
    await nav('settings').click();
    await page.locator('#view-settings [data-tab="downloads"]').click();
    await expect(settingsTab()).toHaveAttribute('data-tab', 'downloads');
    await nav('settings').click();
    await expect(settingsTab()).toHaveAttribute('data-tab', 'sources');
    await page.locator('#view-settings [data-tab="downloads"]').click();
    await nav('dashboard').click();
    await nav('settings').click();
    await expect(settingsTab()).toHaveAttribute('data-tab', 'sources');

    const helpTab = () => page.locator('#view-help .dp-help-tab[aria-selected="true"]');
    await nav('help').click();
    await page.locator('#view-help .dp-help-tab[data-tab="license"]').click();
    await expect(helpTab()).toHaveAttribute('data-tab', 'license');
    await nav('help').click();
    await expect(helpTab()).toHaveAttribute('data-tab', 'quickstart');
    await page.locator('#view-help .dp-help-tab[data-tab="license"]').click();
    await nav('dashboard').click();
    await nav('help').click();
    await expect(helpTab()).toHaveAttribute('data-tab', 'quickstart');
  });

test('re-entering Settings keeps a write already in flight', async ({page}) => {
  await isolate(page);
  let release, completed = false, failed = false;
  await page.route('**/api/settings', async route => {
    if (route.request().method() !== 'PUT') return route.fallback();
    const sent = route.request().postDataJSON();
    await new Promise(resolve => { release = resolve; });
    const current = await (await route.fetch({method: 'GET'})).json();
    await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({...current, ...sent})});
  });
  page.on('requestfinished', request => { if (request.method() === 'PUT') completed = true; });
  page.on('requestfailed', request => { if (request.method() === 'PUT') failed = true; });
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await page.locator('#view-settings [data-tab="downloads"]').click();
  const field = page.locator('#dp-settings-field-min-free-disk-gb');
  await field.fill(String((Number(await field.inputValue()) || 0) + 1));
  await field.blur();
  await expect.poll(() => typeof release).toBe('function');
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings .stab[aria-selected="true"]')).toHaveAttribute('data-tab', 'sources');
  release();
  await expect.poll(() => completed).toBe(true);
  expect(failed).toBe(false);
});

test('Activity Log: a relative time window keeps the first page\'s cutoff on every page; Refresh measures anew',
  async ({page}) => {
    await isolate(page);
    const {requests} = await activity(page);
    const pager = activityPager(page);
    await page.locator('#ev-timeframe').selectOption('24h');
    await expect.poll(() => requests.at(-1).timeframe).toBe('24h');
    expect(requests.at(-1).window_start).toBeUndefined();                    // a new investigation measures it
    await expect(pager.locator('.dp-pager-current')).toHaveText('1');
    await pager.locator('[aria-label="Next page"]').click();
    await expect(pager.locator('.dp-pager-current')).toHaveText('2');
    expect(requests.at(-1)).toMatchObject({timeframe: '24h', window_start: '1760000000.25'});
    await pager.locator('[aria-label="Last page"]').click();
    await expect(pager.locator('.dp-pager-current')).toHaveText('10');
    expect(requests.at(-1)).toMatchObject({last: 'true', window_start: '1760000000.25'});
    await pager.locator('[aria-label="Previous page"]').click();
    await expect(pager.locator('.dp-pager-current')).toHaveText('9');
    expect(requests.at(-1)).toMatchObject({window_start: '1760000000.25'});
    await page.locator('.dp-activity-refresh').click();
    await expect(pager.locator('.dp-pager-current')).toHaveText('1');
    expect(requests.at(-1).window_start).toBeUndefined();
    expect(requests.at(-1).snapshot).toBeUndefined();
  });
