const { test, expect } = require('@playwright/test');

/* DP 1.0.14 Statistics period selector and workspace. (The five-card KPI
 * composition this file first held -- two rows dropping from five to two
 * cards together -- is retired with those cards: Statistics now has two
 * period cards, covered by dashboard-statistics-mobile-workspaces.spec.js.)
 * From 780px the period selector has no visible "Period:" label and keeps its
 * accessible name, place and choices, and Statistics keeps one workspace from
 * 902px down to 780px. The page's own computed layout is read; the
 * statistics are the server's, made long on purpose. */

async function boot(page, width) {
  await page.setViewportSize({width, height: 1000});
  await page.route(url => url.pathname === '/api/stats/detail', route => route.fulfill({status: 200, contentType: 'application/json',
    body: JSON.stringify({period: new URL(route.request().url()).searchParams.get('period'),
      totals: {torrent_total: 123456, completed_count: 123456, partial_total: 12345, success_rate_pct: 99.9, completed_size: 999.9 * 1024 ** 4},
      torrent_status: [], file_status: [], event_levels: [], transfer_methods: [], daily_completions: []})}));
  await page.goto('/');
  await page.waitForFunction(() => window.DPStatisticsLifecycle);
  await page.evaluate(() => nav(document.querySelector('[data-view="stats"]')));
  await page.evaluate(() => window.DPStatisticsLifecycle.load('all'));
  await expect(page.locator('#detail-stat-cards [data-dp-stats-metric="data"] .metric-value')).toHaveText('999.9 TB');
  // The widths are measured in the interface's own fonts; a substitute font is not what they promise.
  await page.evaluate(() => document.fonts.ready);
  expect(await page.evaluate(() => document.fonts.check('600 16px Outfit') && document.fonts.check('500 12px "JetBrains Mono"')),
    'Outfit and JetBrains Mono must be loaded').toBe(true);
}

test('the period selector has no visible "Period:" label and keeps its name, place and choices', async ({page}) => {
  // 900/899px: either side of the global header's change, which must not move it. (Below
  // 780px the handheld period control stands for it: dashboard-statistics-mobile-workspaces.spec.js.)
  for (const width of [1920, 1190, 900, 899, 780]) {
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
    // Right-aligned in the header at every width, on the title's line.
    const contentRight = await header.evaluate(node => {
      const style = getComputedStyle(node);
      return node.getBoundingClientRect().right - parseFloat(style.paddingRight) - parseFloat(style.borderRightWidth);
    });
    expect(Math.abs(contentRight - (t.x + t.width)), `${width}px`).toBeLessThanOrEqual(0.5);
  }
  // A choice still selects its period and updates the figures.
  await page.locator('#stats-period-tabs .ftab[data-period="7d"]').click();
  await expect(page.locator('#stats-period-tabs .ftab[data-period="7d"]')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#detail-stat-cards [data-dp-stats-metric="downloads"] .metric-sub')).toHaveText('Downloads added during the last 7 days.');
});

// The global header changes at 900px; Statistics keeps one workspace from the desktop down to
// the tablet floor: the card fills the content and its body scrolls, the header stays centred
// with the selector right-aligned, and the chart keeps one sizing policy.
test('Statistics keeps one workspace from 902px down to 780px, whatever the global header does at 900px', async ({page}) => {
  await boot(page, 902);
  const workspace = () => page.evaluate(() => {
    const card = document.getElementById('view-stats'), header = card.querySelector('.dp-stats-master-header');
    const body = card.querySelector('.dp-stats-master-body'), content = document.getElementById('content');
    const tabs = document.getElementById('stats-period-tabs').getBoundingClientRect(), h = header.getBoundingClientRect();
    const chart = card.querySelector('.dp-stats-chart');
    return {
      cardFillsContent: Math.abs(card.getBoundingClientRect().bottom - content.getBoundingClientRect().bottom) <= 24 && getComputedStyle(card).display === 'flex',
      scrolls: [getComputedStyle(content).overflowY, getComputedStyle(body).overflowY],
      header: [Math.round(h.height), getComputedStyle(header).alignItems, Math.round(tabs.top - h.top)],
      chart: [getComputedStyle(chart).minHeight, Math.round(chart.getBoundingClientRect().height)],
    };
  });
  const reference = await workspace();
  expect(reference.cardFillsContent).toBe(true);
  expect(reference.scrolls).toEqual(['hidden', 'auto']);
  for (const width of [901, 900, 899, 850, 780]) {
    await page.setViewportSize({width, height: 1000});
    await page.waitForFunction(() => document.getAnimations().every(animation => !(animation instanceof CSSTransition) || animation.playState !== "running"));
    expect(await workspace(), `${width}px`).toEqual(reference);
  }
  // Below the tablet floor (1.0.14) the handheld card is bounded too: its body scrolls, not the page.
  await page.setViewportSize({width: 779, height: 1000});
  await page.waitForFunction(() => document.getAnimations().every(animation => !(animation instanceof CSSTransition) || animation.playState !== "running"));
  expect((await workspace()).scrolls).toEqual(['hidden', 'auto']);
});
