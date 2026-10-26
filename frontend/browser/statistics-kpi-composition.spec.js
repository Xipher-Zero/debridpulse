const { test, expect } = require('@playwright/test');

/* DP 1.0.14 Statistics KPI composition. As the page narrows, both KPI rows
 * drop from five to four, three and two cards together, each keeping its own
 * order and the cards that matter most; at 780px, the bottom of the tablet
 * range, two cards each. Below 780px the mobile presentation keeps every
 * card. The period selector has no visible "Period:" label and keeps its
 * accessible name. The page's own computed layout is read; the statistics are
 * the server's, made long on purpose (the longest realistic labels and
 * values), so the widths are exercised with what they must hold. */

const TOP = ['downloads', 'completed', 'progress', 'success', 'data'];
const SECOND = ['dp-stats-kpi-day', 'dp-stats-kpi-week', 'dp-stats-kpi-duration', 'dp-stats-kpi-success', 'dp-stats-kpi-size'];
const COMPOSITION = {
  5: {top: TOP, second: SECOND},
  4: {top: ['downloads', 'completed', 'success', 'data'],
    second: ['dp-stats-kpi-day', 'dp-stats-kpi-week', 'dp-stats-kpi-duration', 'dp-stats-kpi-size']},
  3: {top: ['downloads', 'completed', 'data'], second: ['dp-stats-kpi-week', 'dp-stats-kpi-duration', 'dp-stats-kpi-size']},
  2: {top: ['completed', 'data'], second: ['dp-stats-kpi-week', 'dp-stats-kpi-size']},
};

async function boot(page, width) {
  await page.setViewportSize({width, height: 1000});
  await page.route(url => url.pathname === '/api/stats/detail', route => route.fulfill({status: 200, contentType: 'application/json',
    body: JSON.stringify({period: new URL(route.request().url()).searchParams.get('period'),
      totals: {torrent_total: 123456, completed_count: 123456, partial_total: 12345, success_rate_pct: 99.9, completed_size: 999.9 * 1024 ** 4},
      torrent_status: [], file_status: [], event_levels: [], transfer_methods: [], daily_completions: []})}));
  await page.route(url => url.pathname === '/api/stats', async route => {
    const response = await route.fetch();
    const body = await response.json();
    route.fulfill({response, json: {...body, completed_last_24h: 9999, completed_last_7d: 99999, success_rate_pct: 99.9,
      avg_download_duration_seconds: 123.4 * 3600, avg_torrent_size_bytes: 999.9 * 1024 ** 3}});
  });
  await page.goto('/');
  await page.waitForFunction(() => window.DPStatisticsLifecycle);
  await page.evaluate(() => nav(document.querySelector('[data-view="stats"]')));
  await page.evaluate(() => window.DPStatisticsLifecycle.load('all'));
  await expect(page.locator('#detail-stat-cards [data-dp-stats-metric="data"] .metric-value')).toHaveText('999.9 TB');
  await expect(page.locator('#i-avg-size')).toHaveText('999.9 GB');
  // The widths are measured in the interface's own fonts; a substitute font is not what they promise.
  await page.evaluate(() => document.fonts.ready);
  expect(await page.evaluate(() => document.fonts.check('600 16px Outfit') && document.fonts.check('500 12px "JetBrains Mono"')),
    'Outfit and JetBrains Mono must be loaded').toBe(true);
}

const layout = page => page.evaluate(() => {
  const shown = node => getComputedStyle(node).display !== 'none';
  const row = (grid, id) => {
    const cards = [...grid.children].filter(card => card.matches('.metric-card, .dash-kpi'));
    const visible = cards.filter(shown);
    return {ids: visible.map(id),
      lines: new Set(visible.map(card => Math.round(card.getBoundingClientRect().top))).size,
      columns: getComputedStyle(grid).gridTemplateColumns.split(' ').length,
      // Every visible card's label and value on one line, inside the card, clear of its icon.
      whole: visible.every(card => {
        const box = card.getBoundingClientRect();
        const icon = card.querySelector('.dp-kpi-icon')?.getBoundingClientRect();
        return [...card.querySelectorAll('.metric-label, .metric-value, .dash-kpi-lbl, .dash-kpi-val')].every(text => {
          const range = document.createRange(); range.selectNodeContents(text);
          const r = range.getBoundingClientRect();
          const clearOfIcon = !icon || r.bottom <= icon.top || r.top >= icon.bottom || r.left >= icon.right;
          return r.left >= box.left && r.right <= box.right && text.scrollWidth <= text.clientWidth + 1 && range.getClientRects().length === 1 && clearOfIcon;
        });
      })};
  };
  return {
    top: row(document.getElementById('detail-stat-cards'), card => card.dataset.dpStatsMetric),
    second: row(document.querySelector('.dp-stats-history-grid'), card => [...card.classList].find(name => name.startsWith('dp-stats-kpi-'))),
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  };
});

// Each composition's own widths, either side of every change and at 780px.
for (const [width, cards] of [[1920, 5], [1880, 5], [1878, 4], [1440, 4], [1190, 4], [1188, 3], [910, 3], [908, 2], [780, 2]]) {
  test(`at ${width}px both KPI rows show the same ${cards} cards in their own order, whole and inside the page`, async ({page}) => {
    await boot(page, width);
    const shown = await layout(page);
    expect(shown.top).toEqual({ids: COMPOSITION[cards].top, lines: 1, columns: cards, whole: true});
    expect(shown.second).toEqual({ids: COMPOSITION[cards].second, lines: 1, columns: cards, whole: true});
    expect(shown.overflow).toBe(0);
  });
}

test('below 780px the mobile presentation keeps every KPI card', async ({page}) => {
  await boot(page, 779);
  const shown = await layout(page);
  expect(shown.top.ids).toEqual(TOP);
  expect(shown.second.ids).toEqual(SECOND);
  expect(shown.overflow).toBe(0);
});

test('a hidden KPI keeps its value current, and no card holds a focus stop', async ({page}) => {
  await boot(page, 800);
  await expect(page.locator('#detail-stat-cards [data-dp-stats-metric="downloads"]')).toBeHidden();
  await expect(page.locator('#detail-stat-cards [data-dp-stats-metric="downloads"] .metric-value')).toHaveText('123456');
  await expect(page.locator('#i-avg-duration')).toBeHidden();
  await expect(page.locator('#i-avg-duration')).not.toHaveText('—');
  expect(await page.locator('#detail-stat-cards [tabindex], .dp-stats-history-grid [tabindex], #detail-stat-cards :is(a, button, input), .dp-stats-history-grid :is(a, button, input)').count()).toBe(0);
});

test('the period selector has no visible "Period:" label and keeps its name, place and choices', async ({page}) => {
  for (const width of [1920, 1190, 780, 390]) {
    await boot(page, width);
    const header = page.locator('#view-stats > .dp-stats-master-header');
    await expect(header).not.toContainText('Period:');
    await expect(page.locator('#view-stats .dp-stats-period-label')).toHaveCount(0);
    const tabs = page.locator('#stats-period-tabs');
    await expect(tabs).toHaveAttribute('role', 'group');
    await expect(tabs).toHaveAttribute('aria-label', 'Statistics period');
    await expect(tabs.locator('.ftab')).toHaveText(['1Hour', '1Day', '7Days', '30Days', '1Year', 'All Time']);
    // Inside the header, which holds it.
    const [h, t] = await Promise.all([header.boundingBox(), tabs.boundingBox()]);
    expect(t.x >= h.x && t.x + t.width <= h.x + h.width + 0.5 && t.y >= h.y && t.y + t.height <= h.y + h.height + 0.5).toBe(true);
  }
  // A choice still selects its period and updates the figures.
  await page.locator('#stats-period-tabs .ftab[data-period="7d"]').click();
  await expect(page.locator('#stats-period-tabs .ftab[data-period="7d"]')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#detail-stat-cards [data-dp-stats-metric="completed"] .metric-sub')).toHaveText('Downloads completed during the last 7 days.');
});
