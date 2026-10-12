// The one search lifecycle (ui-search.js, DPSearch) on Downloads and the
// Activity Log: which answer is shown under which typed text, what the status
// line says, and that the search box keeps its value, focus and caret.
const { test, expect } = require('@playwright/test');

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
  await ready(page);
  const held = [];
  await routeDownloads(page, (route, search) => {
    if (search === 'alpha') return new Promise(resolve => held.push(() => resolve(route.fulfill(listAnswer(search)))));
    return route.fulfill(listAnswer(search));
  });
  await openDownloads(page);
  const box = page.locator('#torrent-search');
  await box.click();
  await box.pressSequentially('alpha', {delay: 20});
  await expect.poll(() => held.length).toBe(1);
  await box.fill('');
  await box.pressSequentially('bravo', {delay: 20});
  await expect.poll(() => rowNames(page)).toEqual(['bravo one']);
  // The slow, older answer arrives last and is discarded.
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
  const status = page.locator('#torrent-search-status');
  await expect(status).toHaveText('Search needs at least 3 characters, or a transfer ID like #580. Results below are for “alpha”.');
  await expect(box).toHaveAttribute('aria-invalid', 'true');
  // Asked once, rejected, and the accepted query asked again.
  expect(requests.filter(search => search === 'ab')).toHaveLength(1);
  expect(requests.slice(requests.indexOf('ab') + 1).every(search => search === 'alpha')).toBe(true);
  expect(await rowNames(page)).toEqual(['alpha one', 'alpha two']);
  await expect(page.locator('#torrent-page-info')).toHaveText('Showing all 2 matching downloads');
  await expect(box).toHaveValue('ab');
  await expect(box).toBeFocused();
  // Typing again clears the explanation; Enter commits at once.
  await box.pressSequentially('c');
  await expect(status).toBeHidden();
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
  await expect(page.locator('#dp-activity-result-note')).toHaveText(
    'Search needs at least 3 characters, or a transfer ID like #580. Results below are for “quick”.');
  expect(requests.slice(-2)).toEqual(['ab', 'quick']);
  await expect(page.locator('#event-list .emsg')).toHaveText(['event for quick']);
  await expect(box).toBeFocused();
  await expect(box).toHaveValue('ab');
});

test('Both lists read the real server\'s one language', async ({ page }) => {
  await ready(page);
  await openDownloads(page);
  await page.locator('#torrent-search').fill('#0');
  await expect(page.locator('#torrent-search-status')).toHaveText('Transfer IDs start at #1. Showing all downloads.');
  await page.locator('#torrent-search').fill('58');
  await expect(page.locator('#torrent-search-status')).toHaveText('Showing transfer #58 only: text search needs at least 3 characters.');
  await page.evaluate(() => nav(document.querySelector('[data-view="events"]')));
  await page.locator('#ev-search').fill('#0');
  await expect(page.locator('#dp-activity-result-note')).toHaveText('Transfer IDs start at #1. Showing all events.');
  await page.locator('#ev-search').fill('58');
  await expect(page.locator('#dp-activity-result-note')).toHaveText('Showing transfer #58 only: text search needs at least 3 characters.');
});
