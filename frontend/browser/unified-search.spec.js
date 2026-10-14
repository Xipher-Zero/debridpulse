// The one search lifecycle (ui-search.js, DPSearch) on Downloads and the
// Activity Log: which answer is shown under which typed text, what the
// operator is told, that the search box keeps its value, focus and caret, and
// that searching never moves the page or repeats itself.
const { test, expect } = require('@playwright/test');
const { pauseClockBeforeBoot } = require('./page-clock');

const meta = (query, extra = {}) => ({
  query: query || null, mode: query ? 'text' : 'none', transfer_id: null, status: 'complete', pending: 0,
  finality: 'exact', notice: null, ...extra,
});
const invalid = message => ({status: 400, contentType: 'application/json',
  body: JSON.stringify({detail: {code: 'invalid_query', message}})});

function transfer(id, name) {
  return {id, name, display_name: name, hash: `hash-${id}`, status: 'completed', progress: 100, size_bytes: 1024,
    created_at: '2026-10-01T12:00:00Z', completed_at: '2026-10-01T12:05:00Z', source: 'direct_link', label: null,
    source_failure_count: 0};
}
const NAMES = ['alpha one', 'alpha two', 'bravo one', 'charlie one'];

async function ready(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.goto('/');
  await page.waitForFunction(() => window.DPSearch && window.DPActivityLog && typeof window.loadTorrents === 'function');
}

// Downloads' list read, answered by ``respond`` (which may hold an answer back).
async function routeDownloads(page, respond) {
  const requests = [];
  await page.route('**/api/torrents?*', async route => {
    const url = new URL(route.request().url());
    if (!url.searchParams.has('offset')) return route.fallback();
    const search = url.searchParams.get('search') || '';
    requests.push(search);
    return respond(route, search);
  });
  return requests;
}
function listAnswer(search) {
  const items = NAMES.map((name, index) => transfer(index + 1, name)).filter(item => item.name.includes(search));
  return {status: 200, contentType: 'application/json', body: JSON.stringify({items, total: items.length, search: meta(search)})};
}
async function openDownloads(page) {
  await page.evaluate(() => nav(document.querySelector('[data-view="torrents"]')));
  await expect(page.locator('#view-torrents')).toHaveClass(/\bactive\b/);
}
const rowNames = page => page.locator('#t-tbody .t-name').allTextContents();

test('Downloads shows only the newest answer, under the text that asked for it', async ({ page }) => {
  // The page's clock moves only when this test moves it, so no request
  // timeout can end the held answer: the newer search must not wait for it.
  await pauseClockBeforeBoot(page);
  await ready(page);
  const held = [];
  await routeDownloads(page, (route, search) => {
    if (search === 'alpha') return new Promise(resolve => held.push(() => resolve(route.fulfill(listAnswer(search)).catch(() => {}))));
    return route.fulfill(listAnswer(search));
  });
  await openDownloads(page);
  const box = page.locator('#torrent-search');
  await box.click();
  await box.pressSequentially('alpha', {delay: 20});
  await page.clock.runFor(250);
  await expect.poll(() => held.length).toBe(1);
  await box.fill('');
  await box.pressSequentially('bravo', {delay: 20});
  await page.clock.runFor(250);
  await expect.poll(() => rowNames(page)).toEqual(['bravo one']);
  // The slow, older answer, if it still arrives, is discarded.
  held.forEach(release => release());
  await page.waitForTimeout(300);
  expect(await rowNames(page)).toEqual(['bravo one']);
  await expect(page.locator('#torrent-page-info')).toHaveText('Showing 1 matching download');
  await expect(box).toBeFocused();
  await expect(box).toHaveValue('bravo');
  expect(await box.evaluate(input => input.selectionStart)).toBe(5);
});

test('Downloads: a refresh while typing keeps the committed query, the typing and the focus', async ({ page }) => {
  await ready(page);
  const requests = await routeDownloads(page, (route, search) => route.fulfill(listAnswer(search)));
  await openDownloads(page);
  const box = page.locator('#torrent-search');
  await box.fill('alpha');
  await expect.poll(() => rowNames(page)).toEqual(['alpha one', 'alpha two']);
  await box.click();
  await box.press('End');
  await box.press('Backspace');
  await box.pressSequentially('XYZ');
  // An incidental refresh (SSE, the 15 s poll) arrives mid-edit.
  const before = requests.length;
  await page.evaluate(() => window.loadTorrents());
  await expect.poll(() => requests.length).toBeGreaterThan(before);
  expect(requests[before]).toBe('alpha');
  await expect(box).toBeFocused();
  await expect(box).toHaveValue('alphXYZ');
  expect(await box.evaluate(input => input.selectionStart)).toBe(7);
  // Then the edit commits on its own.
  await expect.poll(() => requests.at(-1)).toBe('alphXYZ');
});

test('Downloads: an invalid query is explained and the last accepted results stay labelled', async ({ page }) => {
  await ready(page);
  const requests = await routeDownloads(page, (route, search) => (
    search === 'ab' ? route.fulfill(invalid('Search needs at least 3 characters, or a transfer ID like #580.'))
      : route.fulfill(listAnswer(search))));
  await openDownloads(page);
  const box = page.locator('#torrent-search');
  await box.fill('alpha');
  await expect.poll(() => rowNames(page)).toEqual(['alpha one', 'alpha two']);
  await box.fill('ab');
  await expect(page.locator('.toast', {hasText: 'Results are still for “alpha”.'})).toHaveCount(1);
  await expect(box).toHaveAttribute('aria-invalid', 'true');
  // Asked once, rejected, and the accepted query asked again.
  expect(requests.filter(search => search === 'ab')).toHaveLength(1);
  expect(requests.slice(requests.indexOf('ab') + 1).every(search => search === 'alpha')).toBe(true);
  expect(await rowNames(page)).toEqual(['alpha one', 'alpha two']);
  await expect(page.locator('#torrent-page-info')).toHaveText('Showing all 2 matching downloads');
  await expect(box).toHaveValue('ab');
  await expect(box).toBeFocused();
  // Typing again clears the rejection; Enter commits at once.
  await box.pressSequentially('c');
  await expect(box).toHaveAttribute('aria-invalid', 'false');
  await box.press('Enter');
  await expect.poll(() => requests.at(-1)).toBe('abc');
  await expect(box).toHaveAttribute('aria-invalid', 'false');
});

test('Activity Log shares the lifecycle: newest answer only, invalid explained, focus kept', async ({ page }) => {
  await ready(page);
  const held = [];
  const requests = [];
  await page.route('**/api/events?*', async route => {
    const search = new URL(route.request().url()).searchParams.get('search') || '';
    requests.push(search);
    if (search === 'ab') return route.fulfill(invalid('Search needs at least 3 characters, or a transfer ID like #580.'));
    const body = JSON.stringify({items: search ? [{id: search.length, occurred_at: '2026-10-01T12:00:00Z',
      category: 'transfer', type: 'transfer.synthetic', severity: 'info', message: `event for ${search}`}] : [],
      limit: 100, snapshot: 9, has_more: false, next_before: null, newer_available: false,
      search: meta(search, {finality: search ? 'bounded' : 'bounded'})});
    if (search === 'slow') return new Promise(resolve => held.push(() => resolve(route.fulfill({status: 200, contentType: 'application/json', body}))));
    return route.fulfill({status: 200, contentType: 'application/json', body});
  });
  await page.evaluate(() => nav(document.querySelector('[data-view="events"]')));
  const box = page.locator('#ev-search');
  await box.click();
  await box.pressSequentially('slow', {delay: 20});
  await expect.poll(() => held.length).toBe(1);
  await box.fill('quick');
  await expect(page.locator('#event-list .emsg')).toHaveText(['event for quick']);
  held.forEach(release => release());
  await page.waitForTimeout(300);
  await expect(page.locator('#event-list .emsg')).toHaveText(['event for quick']);
  await box.fill('ab');
  await expect(page.locator('.toast', {hasText: 'Results are still for “quick”.'})).toHaveCount(1);
  expect(requests.slice(-2)).toEqual(['ab', 'quick']);
  await expect(page.locator('#event-list .emsg')).toHaveText(['event for quick']);
  await expect(box).toBeFocused();
  await expect(box).toHaveValue('ab');
});

test('Both lists read the real server\'s one language', async ({ page }) => {
  await ready(page);
  await openDownloads(page);
  await page.locator('#torrent-search').fill('#0');
  await expect(page.locator('.toast', {hasText: 'Transfer IDs start at #1. Showing all downloads.'})).toHaveCount(1);
  await page.locator('#torrent-search').fill('58');
  await expect(page.locator('.toast', {hasText: 'Showing transfer #58 only'})).toHaveCount(1);
  await page.evaluate(() => nav(document.querySelector('[data-view="events"]')));
  await page.locator('#ev-search').fill('#0');
  await expect(page.locator('.toast', {hasText: 'Transfer IDs start at #1. Showing all events.'})).toHaveCount(1);
});

test('Searching never adds a line, moves the page or searches itself again', async ({ page }) => {
  await page.setViewportSize({width: 1440, height: 900});
  await ready(page);
  // Answers take a moment, as a real server's do.
  const requests = await routeDownloads(page, async (route, search) => {
    await new Promise(resolve => setTimeout(resolve, 150));
    return route.fulfill(listAnswer(search));
  });
  await openDownloads(page);
  await expect(page.locator('#torrent-search-status, #dp-activity-result-note, .dp-search-status')).toHaveCount(0);
  const geometry = () => page.evaluate(() => ['#torrent-search', '#view-torrents .dp-downloads-table-wrap', '#t-tbody']
    .map(selector => { const box = document.querySelector(selector).getBoundingClientRect(); return [Math.round(box.top), Math.round(box.height)]; }));
  await page.waitForTimeout(600);
  const before = await geometry();
  await page.locator('#torrent-search').fill('alpha');
  await expect.poll(() => rowNames(page)).toEqual(['alpha one', 'alpha two']);
  await page.waitForTimeout(600);
  const settled = requests.length;
  const shifts = await page.evaluate(() => new Promise(resolve => {
    const seen = [];
    const observer = new ResizeObserver(entries => entries.forEach(entry => seen.push(entry.contentRect.height)));
    observer.observe(document.querySelector('#view-torrents .dp-downloads-table-wrap'));
    setTimeout(() => { observer.disconnect(); resolve(seen.length); }, 3000);
  }));
  // Three quiet seconds with the text left in the box: no request, no resize
  // beyond the observer's first report.
  expect(requests.length).toBe(settled);
  expect(shifts).toBeLessThanOrEqual(1);
  const after = await geometry();
  expect(after[0]).toEqual(before[0]);
  expect(after[1][0]).toEqual(before[1][0]);
  // A refresh with nothing changed shows the same answer and asks once.
  await page.evaluate(() => window.loadTorrents());
  await expect.poll(() => requests.length).toBe(settled + 1);
  await page.waitForTimeout(1000);
  expect(requests.length).toBe(settled + 1);
  expect(await rowNames(page)).toEqual(['alpha one', 'alpha two']);
  await expect(page.locator('#torrent-page-info')).toHaveText('Showing all 2 matching downloads');
});

test('Activity Log tells an index still catching up apart from text search that is unavailable', async ({ page }) => {
  await ready(page);
  let status = 'index_incomplete';
  await page.route('**/api/events?*', route => {
    const url = new URL(route.request().url());
    const search = url.searchParams.get('search') || '';
    const items = Array.from({length: 3}, (_, index) => ({id: 30 - index, occurred_at: '2026-10-01T12:00:00Z',
      category: 'transfer', type: 'transfer.synthetic', severity: 'info', message: `event ${index}`}));
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({
      items, limit: 3, snapshot: 30, has_more: true, next_before: 28, more_after_next: true, newer_available: false,
      search: meta(search, {mode: search ? 'id_or_text' : 'none', status: search ? status : 'complete',
        pending: status === 'index_incomplete' ? 7 : 0, finality: search ? 'unproven' : 'bounded'})})});
  });
  await page.evaluate(() => nav(document.querySelector('[data-view="events"]')));
  const info = page.locator('#dp-activity-page-info');
  const last = page.locator('#dp-activity-page-btns button[aria-label^="Last page"]');
  await page.locator('#ev-search').fill('4242');
  await expect(info).toHaveText('Recent events are still being indexed; matches among them may be missing.');
  status = 'unavailable';
  await page.locator('#ev-search').fill('4243');
  await expect(info).toHaveText('Text search is unavailable on this installation; only the transfer ID was matched.');
  await expect(page.locator('.toast', {hasText: 'Text search is unavailable'})).toHaveCount(1);
  // Neither is described as the other, and neither line is a row of its own.
  await expect(info).not.toContainText('indexed');
  await expect(page.locator('#dp-activity-result-note, .dp-search-status')).toHaveCount(0);
  // Finality is its own fact: an unproven final page keeps Last unavailable either way.
  await expect(last).toBeDisabled();
  await expect(last).toHaveAttribute('aria-label', /^Last page unavailable: /);
  // A complete answer says neither.
  status = 'complete';
  await page.locator('#ev-search').fill('4244');
  await expect(info).toHaveText('');
});
