const { test, expect } = require('@playwright/test');

/* DP 1.0.13 minor cleanup: Transfer Details dates use the Downloads list's
 * own date owner (format, timezone, year); the unfiltered Downloads footer
 * counts Retained Items; and three exact copy corrections. */

const PREF_KEY = 'debridpulse.downloads.date-presentation.v1';
// 23:30 UTC on New Year's Eve is already the next year in Tokyo.
const ADDED = '2025-12-31T23:30:00Z';
const transfer = (id, extra) => ({
  id, name: `Transfer ${id}`, display_name: `Transfer ${id}`, status: 'completed', progress: 100, size_bytes: 1024,
  source: 'direct_link', hash: `hash-${id}`, created_at: ADDED, completed_at: null, files: [], source_outcomes: [],
  executors: ['aria2'], route_attempts: [], execution_attempts: [],
  events: [{level: 'info', message: 'Added', created_at: ADDED}], ...extra,
});

async function boot(page, {pref, items = [transfer(7)], total = items.length} = {}) {
  if (pref) await page.addInitScript(([key, value]) => localStorage.setItem(key, JSON.stringify(value)), [PREF_KEY, pref]);
  await page.route(url => /^\/api\/torrents\/\d+$/.test(url.pathname),
    route => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(transfer(7))}));
  await page.route(url => url.pathname === '/api/torrents', route => {
    const query = new URL(route.request().url()).searchParams;
    const limit = Number(query.get('limit')) || items.length, offset = Number(query.get('offset')) || 0;
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({
      items: items.slice(offset, offset + limit), total, search: {mode: 'none', status: 'complete'}})});
  });
  await page.goto('/');
  await page.waitForFunction(() => typeof showDetail === 'function' && typeof window.downloadsDateMarkup === 'function');
  await page.evaluate(() => { settingsData = {...(settingsData || {}), timezone: 'Asia/Tokyo'}; });
}
const listDate = async page => {
  await page.evaluate(() => nav(document.querySelector('[data-view="torrents"]')));
  return page.locator('#t-tbody tr[data-torrent-id="7"] .dp-downloads-date-value').innerText();
};
const detailsDates = async page => {
  await page.evaluate(() => showDetail(7));
  await expect(page.locator('#modal-body .detail-grid')).toBeVisible();
  const value = label => page.locator('#modal-body .detail-grid > div').filter({has: page.locator('.dk', {hasText: new RegExp(`^${label}$`)})}).locator('.dv');
  return {added: await value('Added').innerText(), completed: await value('Completed').innerText(),
          event: await page.locator('.dp-detail-events-list .etime').first().innerText()};
};

for (const [format, expected] of [['iso', '2026-01-01 08:30'], ['us', '01/01/2026 08:30']]) {
  test(`Details dates match the Downloads list in the ${format} preference, timezone and year included`, async ({ page }) => {
    await boot(page, {pref: {format, hour12: false}});
    const list = await listDate(page);
    expect(list).toBe(expected);
    const details = await detailsDates(page);
    expect(details).toEqual({added: expected, completed: '—', event: expected});
  });
}

test('the unfiltered footer counts Retained Items: none, one, many and a later page', async ({ page }) => {
  const many = Array.from({length: 153}, (_, index) => transfer(1000 + index));
  await boot(page, {items: many});
  const info = page.locator('#torrent-page-info');
  await page.evaluate(() => nav(document.querySelector('[data-view="torrents"]')));
  await expect(info).toHaveText('Showing 153 Retained Items');
  await page.evaluate(() => goToTorrentPage(2));
  await expect(info).toHaveText('Showing 153 Retained Items');
  // The historical KPI subtitle keeps its own count, only its wording changed.
  await page.evaluate(() => updateDownloadsTrackedCopy(9000));
  await expect(page.locator('#torrent-card-title .dp-downloads-subtitle')).toHaveText('9000 downloads tracked. Most of them behaved.');
});

for (const [items, text] of [[[], 'No Retained Items'], [[transfer(7)], 'Showing 1 Retained Item']]) {
  test(`the unfiltered footer reads "${text}"`, async ({ page }) => {
    await boot(page, {items});
    await page.evaluate(() => nav(document.querySelector('[data-view="torrents"]')));
    await expect(page.locator('#torrent-page-info')).toHaveText(text);
  });
}

test('filtered footers keep their own descriptors', async ({ page }) => {
  await boot(page);
  await page.evaluate(() => nav(document.querySelector('[data-view="torrents"]')));
  await page.locator('#view-torrents .ftab[data-dp-status="completed"]').click();
  await expect(page.locator('#torrent-page-info')).toHaveText('1 Download Completed');
});

test('the three copy corrections, static and dynamic, with search unchanged', async ({ page }) => {
  await boot(page);
  const title = page.locator('#torrent-card-title');
  // As first painted (the static markup), and as the owner rewrites it.
  const html = await page.evaluate(() => document.documentElement.outerHTML);
  expect(html).not.toContain('followed instructions');
  await page.evaluate(() => updateDownloadsTrackedCopy(1));
  await expect(title.locator('.dp-downloads-subtitle')).toHaveText('1 download tracked. It behaved.');
  await expect(title).toHaveAttribute('aria-label', 'On the Books. 1 download tracked. It behaved.');
  await page.evaluate(() => updateDownloadsTrackedCopy(0));
  await expect(title).toHaveAttribute('aria-label', 'On the Books. 0 downloads tracked. Most of them behaved.');
  await expect(page.locator('.dp-card-subtitle', {hasText: 'Add links'})).toHaveText('Add links, magnets, torrent, or NZB files to the queue.');
  const placeholder = await page.locator('#ev-search').getAttribute('placeholder');
  expect(placeholder).toBe('Search events, names, or transfer ID…');
  expect(placeholder).not.toContain('#');
});
