const { test, expect } = require('@playwright/test');

/* DP 1.0.13 -- Statistics -> Completions: on 1 Hour, 1 Day, 7 Days and 1 Year
 * each bar is the bucket's total, stacked from Premium Sources, Network
 * Sources and the unattributed remainder in the chart's own purple; 30 Days and
 * All Time stay single bars. The legend follows the period. */

const bucket = (date, premium, network, unattributed) => ({date, count: premium + network + unattributed, premium, network, unattributed});
const STACKED = {
  '7d': [bucket('2026-10-10', 1, 0, 2), bucket('2026-10-11', 2, 2, 1), bucket('2026-10-12', 3, 0, 0)],
  '1y': [bucket('2026-09', 0, 1, 0), bucket('2026-10', 5, 4, 4)],
  '24h': [], '1h': [],
};
const PLAIN = {'30d': [{date: '2026-10-11', count: 4}, {date: '2026-10-12', count: 9}], all: [{date: '2026-10-12', count: 14}]};

async function boot(page) {
  await page.route(url => url.pathname === '/api/stats/detail', route => {
    const period = new URL(route.request().url()).searchParams.get('period');
    const stacked = period in STACKED;
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({
      period, totals: {}, torrent_status: [], file_status: [], event_levels: [], sources: [],
      daily_completions: stacked ? STACKED[period] : PLAIN[period], completions_by_family: stacked})});
  });
  await page.goto('/');
  await page.waitForFunction(() => window.DPStatisticsLifecycle && typeof Chart !== 'undefined');
  await page.evaluate(() => nav(document.querySelector('[data-view="stats"]')));
}
const show = (page, period) => page.evaluate(value => window.DPStatisticsLifecycle.load(value), period);
const legend = page => page.locator('#completions-legend .dp-stats-chart-legend-item').allTextContents();
const chart = page => page.evaluate(() => {
  const instance = document.getElementById('daily-chart')._ci;
  return {
    stacked: instance.options.scales.y.stacked === true,
    datasets: instance.data.datasets.map(set => ({label: set.label, role: set.dpRole, data: set.data})),
    // Only the stacked chart sets its own tooltip; the single bars keep Chart.js's.
    customTooltip: instance.options.plugins.tooltip.mode === 'index',
    tooltips: instance.options.plugins.tooltip.mode === 'index'
      ? instance.data.labels.map((_, index) => instance.options.plugins.tooltip.callbacks.label({dataIndex: index})) : [],
  };
});

for (const period of ['7d', '1y']) {
  test(`${period} stacks Premium, Network and the unattributed remainder into each unchanged total`, async ({ page }) => {
    await boot(page);
    await show(page, period);
    expect(await legend(page)).toEqual(['Daily Completions', 'Premium Sources', 'Network Sources']);
    const drawn = await chart(page);
    expect(drawn.stacked).toBe(true);
    // Fixed order whatever the values; the purple remainder is the total's identity, not a family.
    expect(drawn.datasets.map(set => set.role)).toEqual(['premium', 'network', 'total']);
    STACKED[period].forEach((item, index) => {
      expect(drawn.datasets.reduce((sum, set) => sum + set.data[index], 0)).toBe(item.count);
      const lines = drawn.tooltips[index];
      expect(lines.slice(0, 3)).toEqual([`Total completions: ${item.count}`, `Premium Sources: ${item.premium}`,
                                         `Network Sources: ${item.network}`]);
      expect(lines.includes(`Unattributed: ${item.unattributed}`)).toBe(item.unattributed > 0);
      expect(lines.some(line => /Unattributed/.test(line))).toBe(item.unattributed > 0);
    });
    if (period === '1y') expect(await page.evaluate(() => document.getElementById('daily-chart')._ci.data.labels)).toEqual(['2026-09', '2026-10']);
  });
}

for (const period of ['30d', 'all']) {
  test(`${period} keeps its single total bars and total-only legend`, async ({ page }) => {
    await boot(page);
    await show(page, '7d');
    await show(page, period);
    expect(await legend(page)).toEqual(['Daily Completions']);
    const drawn = await chart(page);
    expect(drawn.stacked).toBe(false);
    expect(drawn.datasets).toEqual([{label: 'Completions', role: 'total', data: PLAIN[period].map(item => item.count)}]);
    expect(drawn.customTooltip).toBe(false);
  });
}

test('an empty stacked period keeps its three-item legend; swatches follow the theme', async ({ page }) => {
  await boot(page);
  await show(page, '24h');
  expect(await legend(page)).toEqual(['Daily Completions', 'Premium Sources', 'Network Sources']);
  const swatches = () => page.locator('#completions-legend .dp-stats-chart-legend-swatch')
    .evaluateAll(nodes => nodes.map(node => getComputedStyle(node).backgroundColor));
  const dark = await swatches();
  expect(new Set(dark).size).toBe(3);
  await page.evaluate(() => { document.body.classList.add('light'); document.dispatchEvent(new CustomEvent('debridpulse:theme-changed')); });
  const light = await swatches();
  expect(new Set(light).size).toBe(3);
  expect(light).not.toEqual(dark);
});
