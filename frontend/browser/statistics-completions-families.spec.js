const { test, expect } = require('@playwright/test');

/* DP 1.0.13 -- Statistics -> Completions: on 1 Hour, 1 Day, 7 Days and 1 Year
 * each bucket is its purple completion bar, with a narrower Premium/Network
 * stack inset in it on the same scale; unattributed completions are only the
 * purple above that stack. 30 Days and All Time stay single bars. The legend
 * follows the period. Read from the drawn canvas itself. */

const bucket = (date, premium, network, unattributed) => ({date, count: premium + network + unattributed, premium, network, unattributed});
const CASES = [
  bucket('mixed', 4, 3, 3), bucket('premium-only', 5, 0, 0), bucket('network-only', 0, 4, 0),
  bucket('all-attributed', 3, 3, 0), bucket('unattributed-only', 0, 0, 4), bucket('empty', 0, 0, 0),
  bucket('one', 1, 0, 0), bucket('two', 1, 1, 0),
];
const STACKED = {'7d': CASES, '1y': [bucket('2026-09', 0, 1, 0), bucket('2026-10', 5, 4, 4)], '24h': [], '1h': []};
const PLAIN = {'30d': [{date: '2026-10-11', count: 4}, {date: '2026-10-12', count: 9}], all: [{date: '2026-10-12', count: 14}]};

async function boot(page, {width = 1440, height = 900, light = false} = {}) {
  await page.setViewportSize({width, height});
  await page.route(url => url.pathname === '/api/stats/detail', route => {
    const period = new URL(route.request().url()).searchParams.get('period');
    const stacked = period in STACKED;
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({
      period, totals: {}, torrent_status: [], file_status: [], event_levels: [], sources: [],
      daily_completions: stacked ? STACKED[period] : PLAIN[period], completions_by_family: stacked})});
  });
  await page.goto('/');
  await page.waitForFunction(() => window.DPStatisticsLifecycle && typeof Chart !== 'undefined');
  if (light) await page.evaluate(() => { document.body.classList.add('light'); document.dispatchEvent(new CustomEvent('debridpulse:theme-changed')); });
  await page.evaluate(() => nav(document.querySelector('[data-view="stats"]')));
}
const show = async (page, period) => {
  await page.evaluate(value => window.DPStatisticsLifecycle.load(value), period);
  // The bars as drawn at their final size: once the chart's own animation has finished.
  await page.waitForFunction(() => { const chart = document.getElementById('daily-chart')._ci; return chart && !Chart.animator.running(chart); });
};
const legend = page => page.locator('#completions-legend .dp-stats-chart-legend-item').allTextContents();

/* Each bucket's drawn geometry and RGBA samples (0-255) at named points. */
const drawn = page => page.evaluate(() => {
  const chart = document.getElementById('daily-chart')._ci;
  const ratio = chart.currentDevicePixelRatio;
  const pixel = (x, y) => [...chart.ctx.getImageData(Math.round(x * ratio), Math.round(y * ratio), 1, 1).data];
  const scale = chart.scales.y;
  return chart.getDatasetMeta(0).data.map((bar, index) => {
    const {x, y, base, width} = bar.getProps(['x', 'y', 'base', 'width'], true);
    const item = chart.data.datasets[0].dpFamilies?.[index] || {};
    const left = x - width / 2;
    const attributedTop = scale.getPixelForValue((item.premium || 0) + (item.network || 0));
    const premiumTop = scale.getPixelForValue(item.premium || 0);
    return {
      date: chart.data.labels[index], x, top: y, base, width, attributedTop, premiumTop,
      totalHeight: base - y,
      sideMargin: pixel(left + 1, (base + Math.max(attributedTop, y + 6)) / 2),
      premiumCentre: pixel(x, (base + premiumTop) / 2),
      networkCentre: pixel(x, (premiumTop + attributedTop) / 2),
      aboveStack: pixel(x, (y + attributedTop) / 2),
      justBelowTop: pixel(x, y + 2),
      // Off the integer gridlines, which the translucent purple lets through.
      centre: pixel(x, scale.getPixelForValue((item.count || 0) * 0.6 + 0.1)),
      plotMiddle: pixel(x, (chart.chartArea.top + chart.chartArea.bottom) / 2),
    };
  });
});
// Purple (the bar's own gradient, deep in dark, pale lavender in light): blue above red above
// green, red well below blue -- unlike the magentas, whose red nearly equals their blue, or the cyan.
const purple = ([r, g, b, a]) => a > 30 && b > g + 20 && r > g && r < 0.9 * b;
// Exactly the family colour at a translucent alpha: nothing of the purple mixed in.
const family = (rgb, [r, g, b, a]) => a >= 0.28 * 255 && a <= 0.67 * 255
  && Math.abs(r - rgb[0]) <= 3 && Math.abs(g - rgb[1]) <= 3 && Math.abs(b - rgb[2]) <= 3;
const rgbOf = (page, role) => page.evaluate(role => {
  if (role === 'premium') return document.body.classList.contains('light') ? [186, 36, 196] : [234, 88, 242];
  const hex = getComputedStyle(document.body).getPropertyValue('--dp-accent-cyan').trim().replace('#', '');
  const v = parseInt(hex, 16);
  return [v >> 16 & 255, v >> 8 & 255, v & 255];
}, role);

for (const light of [false, true]) {
  test(`${light ? 'light' : 'dark'}: each purple bar keeps its silhouette with a narrower Premium/Network stack inset on the same scale`, async ({ page }) => {
    await boot(page, {light});
    await show(page, '7d');
    expect(await legend(page)).toEqual(['Daily Completions', 'Premium Sources', 'Network Sources']);
    const premium = await rgbOf(page, 'premium'), network = await rgbOf(page, 'network');
    const bars = Object.fromEntries((await drawn(page)).map(bar => [bar.date, bar]));
    // One unit of height, from the scale: the purple bar is the total, the stack only P + N.
    const unit = bars['unattributed-only'].totalHeight / 4;
    for (const item of CASES.filter(item => item.count)) {
      expect(Math.abs(bars[item.date].totalHeight - unit * item.count)).toBeLessThanOrEqual(1);
    }
    // Mixed: purple at the side and above the stack; each family its own colour, nothing purple in it.
    const mixed = bars.mixed;
    expect(purple(mixed.sideMargin)).toBe(true);
    expect(purple(mixed.aboveStack)).toBe(true);
    expect(mixed.attributedTop - mixed.top).toBeGreaterThanOrEqual(unit * 3 - 1);   // the 3 unattributed
    expect(family(premium, mixed.premiumCentre)).toBe(true);
    expect(family(network, mixed.networkCentre)).toBe(true);
    // One family alone keeps the same inset.
    expect(family(premium, bars['premium-only'].premiumCentre)).toBe(true);
    expect(purple(bars['premium-only'].sideMargin)).toBe(true);
    expect(family(network, bars['network-only'].networkCentre)).toBe(true);
    expect(purple(bars['network-only'].sideMargin)).toBe(true);
    // Everything attributed: still a purple margin at the top.
    expect(purple(bars['all-attributed'].justBelowTop)).toBe(true);
    expect(family(network, bars['all-attributed'].networkCentre)).toBe(true);
    // Unattributed alone: the purple bar with nothing in it. Empty: no bar.
    expect(purple(bars['unattributed-only'].centre)).toBe(true);
    expect(bars.empty.totalHeight).toBe(0);
    expect(bars.empty.plotMiddle[3]).toBe(0);
    // Small bars degrade without losing the purple or the family.
    for (const small of ['one', 'two']) {
      expect(purple(bars[small].sideMargin)).toBe(true);
      expect(family(premium, bars[small].premiumCentre)).toBe(true);
    }
    expect(family(network, bars.two.networkCentre)).toBe(true);
  });
}

test('only the uppermost family is rounded, inside the bar, with no gap between the families', async ({ page }) => {
  await boot(page);
  await show(page, '7d');
  const samples = await page.evaluate(() => {
    const chart = document.getElementById('daily-chart')._ci;
    const ratio = chart.currentDevicePixelRatio;
    const pixel = (x, y) => [...chart.ctx.getImageData(Math.round(x * ratio), Math.round(y * ratio), 1, 1).data];
    const index = chart.data.labels.indexOf('all-attributed');
    const {x, y, base, width} = chart.getDatasetMeta(0).data[index].getProps(['x', 'y', 'base', 'width'], true);
    const side = Math.max(2, Math.min(10, Math.round(width * 0.18))), left = x - width / 2 + side;
    // Scan down the stack's left edge column and its centre column from just above the stack top.
    const premiumTop = chart.scales.y.getPixelForValue(3);
    const column = px => Array.from({length: Math.ceil(base - y)}, (_, i) => pixel(px, y + i + 0.5));
    return {top: y, base, premiumTop, edge: column(left + 0.5), centre: column(x)};
  });
  const coloured = ([r, g, b, a]) => !purple([r, g, b, a]) && a > 60;
  const firstColoured = column => column.findIndex(coloured);
  // The rounded corner: on the stack's first rows its top-left pixel is not yet the pure family
  // colour (the arc cuts it), while the centre already is; lower down the edge is.
  const network = await rgbOf(page, 'network');
  const stackTop = firstColoured(samples.centre);
  expect(family(network, samples.centre[stackTop + 1])).toBe(true);
  expect(family(network, samples.edge[stackTop])).toBe(false);
  expect(family(network, samples.edge[stackTop + 4])).toBe(true);
  // Network straight onto Premium: at most one blended row between them, never purple or empty,
  // where the scale puts the Premium top.
  const premium = await rgbOf(page, 'premium');
  const lastNetwork = samples.centre.findLastIndex(pixel => family(network, pixel));
  const firstPremium = samples.centre.findIndex(pixel => family(premium, pixel));
  expect(firstPremium - lastNetwork).toBeGreaterThanOrEqual(1);
  expect(firstPremium - lastNetwork).toBeLessThanOrEqual(2);
  // A blended row lies channel by channel between the two family colours, which the
  // container's purple (its green far below both) never does.
  const between = pixel => [0, 1, 2].every(c => pixel[c] >= Math.min(network[c], premium[c]) - 3
    && pixel[c] <= Math.max(network[c], premium[c]) + 3);
  for (let i = lastNetwork + 1; i < firstPremium; i++) {
    expect(between(samples.centre[i])).toBe(true);
    expect(samples.centre[i][3]).toBeGreaterThan(60);
  }
  expect(Math.abs(samples.top + firstPremium - samples.premiumTop)).toBeLessThanOrEqual(1.5);
  // The stack never reaches the bar's own top edge.
  expect(firstColoured(samples.centre)).toBeGreaterThanOrEqual(2);
});

test('1 Year keeps its monthly buckets and stacks; the tooltip gives the total and each share', async ({ page }) => {
  await boot(page);
  await show(page, '1y');
  expect(await legend(page)).toEqual(['Daily Completions', 'Premium Sources', 'Network Sources']);
  const chart = await page.evaluate(() => {
    const instance = document.getElementById('daily-chart')._ci;
    return {labels: instance.data.labels, datasets: instance.data.datasets.map(set => ({role: set.dpRole, data: set.data})),
      lines: instance.data.labels.map((_, index) => instance.options.plugins.tooltip.callbacks.label({dataIndex: index}))};
  });
  expect(chart.labels).toEqual(['2026-09', '2026-10']);
  // One bar per bucket: the total. Unattributed is never a dataset of its own.
  expect(chart.datasets).toEqual([{role: 'total', data: [1, 13]}]);
  expect(chart.lines).toEqual([
    ['Total completions: 1', 'Premium Sources: 0', 'Network Sources: 1'],
    ['Total completions: 13', 'Premium Sources: 5', 'Network Sources: 4', 'Unattributed: 4'],
  ]);
});

for (const period of ['30d', 'all']) {
  test(`${period} keeps its single purple bars, default tooltip and total-only legend`, async ({ page }) => {
    await boot(page);
    await show(page, '7d');
    await show(page, period);
    expect(await legend(page)).toEqual(['Daily Completions']);
    const chart = await page.evaluate(() => {
      const instance = document.getElementById('daily-chart')._ci;
      return {datasets: instance.data.datasets.map(set => ({label: set.label, role: set.dpRole, data: set.data, families: set.dpFamilies})),
        plugins: instance.config.plugins.map(plugin => plugin.id), customTooltip: instance.options.plugins.tooltip.mode === 'index'};
    });
    expect(chart.datasets).toEqual([{label: 'Completions', role: 'total', data: PLAIN[period].map(item => item.count), families: null}]);
    expect(chart.plugins).not.toContain('dpCompletionFamilies');
    expect(chart.customTooltip).toBe(false);
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
