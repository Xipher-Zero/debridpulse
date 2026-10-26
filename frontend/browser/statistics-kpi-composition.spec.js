const { test, expect } = require('@playwright/test');

/* DP 1.0.14 Statistics KPI composition. As the page narrows, both KPI rows
 * drop from five to four, three and two cards together, each keeping its own
 * order and the cards that matter most; at 780px, the bottom of the tablet
 * range, two cards each. Throughout, the established hierarchy holds: the
 * primary row spans the content, the secondary row is the smaller band
 * centred beneath it (83.333% of the primary row's width, as in the original
 * design). Five cards show wherever the original five-card layout fits -- down
 * to where a secondary title would start to run into its icon. Below 780px the
 * mobile presentation keeps every card. The period selector has no visible "Period:" label and keeps its
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
      // Every visible card's label and value on one line and inside the card.
      whole: visible.every(card => {
        const box = card.getBoundingClientRect();
        return [...card.querySelectorAll('.metric-label, .metric-value, .dash-kpi-lbl, .dash-kpi-val')].every(text => {
          const range = document.createRange(); range.selectNodeContents(text);
          const r = range.getBoundingClientRect();
          return r.left >= box.left && r.right <= box.right && text.scrollWidth <= text.clientWidth + 1 && range.getClientRects().length === 1;
        });
      }),
      // The smallest space between a title or value and the icon beside it (px).
      iconClearance: Math.min(Infinity, ...visible.flatMap(card => {
        const icon = card.querySelector('.dp-kpi-icon')?.getBoundingClientRect();
        if (!icon) return [];
        return [...card.querySelectorAll('.dash-kpi-lbl, .dash-kpi-val')].map(text => {
          const range = document.createRange(); range.selectNodeContents(text);
          const r = range.getBoundingClientRect();
          return r.bottom <= icon.top || r.top >= icon.bottom ? Infinity : r.left - icon.right;
        });
      }))};
  };
  // The rows' rendered spans: the secondary band against the primary row.
  const span = grid => {
    const cards = [...grid.children].filter(card => card.matches('.metric-card, .dash-kpi') && shown(card)).map(card => card.getBoundingClientRect());
    return {left: Math.min(...cards.map(c => c.left)), right: Math.max(...cards.map(c => c.right)),
      narrowest: Math.min(...cards.map(c => c.width)), widest: Math.max(...cards.map(c => c.width))};
  };
  const [primary, secondary] = [span(document.getElementById('detail-stat-cards')), span(document.querySelector('.dp-stats-history-grid'))];
  return {
    hierarchy: {
      spanRatio: (secondary.right - secondary.left) / (primary.right - primary.left),
      centreOffset: Math.abs((secondary.left + secondary.right) / 2 - (primary.left + primary.right) / 2),
      cardRatio: secondary.widest / primary.narrowest},
    top: row(document.getElementById('detail-stat-cards'), card => card.dataset.dpStatsMetric),
    second: row(document.querySelector('.dp-stats-history-grid'), card => [...card.classList].find(name => name.startsWith('dp-stats-kpi-'))),
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  };
});

// Each composition's own widths: the established desktop (1834px: the operator's
// reference), 1680 and 1600px, either side of every change, and 780px. Every change
// keeps the tightest secondary title at least 3px clear of its icon.
for (const [width, cards] of [[1920, 5], [1834, 5], [1816, 5], [1814, 4], [1680, 4], [1600, 4], [1340, 4], [1338, 3],
  [1026, 3], [1024, 2], [780, 2]]) {
  test(`at ${width}px both KPI rows show the same ${cards} cards in their own order, whole, inside the page and in the established hierarchy`, async ({page}) => {
    await boot(page, width);
    const shown = await layout(page);
    const {iconClearance, ...second} = shown.second;
    expect(shown.top).toMatchObject({ids: COMPOSITION[cards].top, lines: 1, columns: cards, whole: true});
    expect(second).toEqual({ids: COMPOSITION[cards].second, lines: 1, columns: cards, whole: true});
    expect(shown.overflow).toBe(0);
    // A positive clearance between each secondary title and its icon (3px at each change
    // in Chromium; 2.5px leaves room for subpixel rounding, never for an overlap).
    expect(iconClearance).toBeGreaterThanOrEqual(2.5);
    // The secondary row is the original band: 83.333% of the primary row's span, centred
    // beneath it, its cards narrower than the primary cards.
    expect(shown.hierarchy.spanRatio).toBeGreaterThan(0.828);
    expect(shown.hierarchy.spanRatio).toBeLessThan(0.839);
    expect(shown.hierarchy.centreOffset).toBeLessThan(1);
    expect(shown.hierarchy.cardRatio).toBeLessThan(0.86);
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
  // 900/899px: either side of the global header's change, which must not move it; 500px: wrapped below the title.
  for (const width of [1920, 1190, 900, 899, 780, 500, 390]) {
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
    // Right-aligned in the header at every width, on the title's line or its own.
    const contentRight = await header.evaluate(node => {
      const style = getComputedStyle(node);
      return node.getBoundingClientRect().right - parseFloat(style.paddingRight) - parseFloat(style.borderRightWidth);
    });
    expect(Math.abs(contentRight - (t.x + t.width)), `${width}px`).toBeLessThanOrEqual(0.5);
  }
  // A choice still selects its period and updates the figures.
  await page.locator('#stats-period-tabs .ftab[data-period="7d"]').click();
  await expect(page.locator('#stats-period-tabs .ftab[data-period="7d"]')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#detail-stat-cards [data-dp-stats-metric="completed"] .metric-sub')).toHaveText('Downloads completed during the last 7 days.');
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
  // Below the tablet floor the mobile page keeps scrolling as before.
  await page.setViewportSize({width: 779, height: 1000});
  await page.waitForFunction(() => document.getAnimations().every(animation => !(animation instanceof CSSTransition) || animation.playState !== "running"));
  expect((await workspace()).scrolls).toEqual(['auto', 'visible']);
});
