const { test, expect } = require('@playwright/test');
const { pauseClockBeforeBoot } = require('./page-clock');

/* DP 1.0.13 -- Statistics -> Completions: on every period each bucket is its
 * purple completion bar, with a narrower Premium/Network stack inset in it on
 * the same scale; unattributed completions are only the purple above that
 * stack. Read from the drawn canvas itself. */

const bucket = (date, premium, network, unattributed) => ({date, count: premium + network + unattributed, premium, network, unattributed});
const CASES = [
  bucket('mixed', 4, 3, 3), bucket('premium-only', 5, 0, 0), bucket('network-only', 0, 4, 0),
  bucket('all-attributed', 3, 3, 0), bucket('unattributed-only', 0, 0, 4), bucket('empty', 0, 0, 0),
  bucket('one', 1, 0, 0), bucket('two', 1, 1, 0),
];
const PERIODS = {'7d': CASES, '30d': CASES, all: CASES, '1y': [bucket('2026-09', 0, 1, 0), bucket('2026-10', 5, 4, 4)],
                 '24h': [], '1h': []};

async function boot(page, {width = 1440, height = 900, light = false, clock = false} = {}) {
  await page.setViewportSize({width, height});
  // Only the test moves time, from before the application exists (page-clock.js).
  if (clock) await pauseClockBeforeBoot(page);
  await page.route(url => url.pathname === '/api/stats/detail', route => {
    const period = new URL(route.request().url()).searchParams.get('period');

    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({
      period, totals: {}, torrent_status: [], file_status: [], event_levels: [], transfer_methods: [],
      daily_completions: PERIODS[period]})});
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
// 1 Year's two months as the viewer's date preference labels them.
const MONTHS = page => page.evaluate(() => ['09', '10'].map(month => window.downloadsCalendarLabel({year: '2026', month})));

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
// The canvas keeps colour premultiplied by alpha, so reading it back at a low alpha rounds
// each channel by up to 255/alpha: the tolerance follows the alpha.
const channelTolerance = a => Math.max(3, Math.ceil(2 * 255 / Math.max(1, a)));
// Exactly the family colour at a translucent alpha: nothing of the purple mixed in.
const family = (rgb, [r, g, b, a]) => a >= 0.18 * 255 && a <= 0.57 * 255
  && [r, g, b].every((value, c) => Math.abs(value - rgb[c]) <= channelTolerance(a));
const rgbOf = (page, role) => page.evaluate(role => {
  if (role === 'premium') return document.body.classList.contains('light') ? [194, 5, 212] : [245, 56, 255];
  const hex = getComputedStyle(document.body).getPropertyValue('--dp-accent-cyan').trim().replace('#', '');
  const v = parseInt(hex, 16);
  return [v >> 16 & 255, v >> 8 & 255, v & 255];
}, role);

for (const period of ['7d', '30d', 'all']) for (const light of [false, true]) {
  test(`${period} ${light ? 'light' : 'dark'}: each purple bar keeps its silhouette with a narrower Premium/Network stack inset on the same scale`, async ({ page }) => {
    await boot(page, {light});
    await show(page, period);
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
    // Each family fades within itself: about 55% just below its own top, about 20% just above
    // its own bottom -- never one gradient across the stack, never reversed.
    const alphaAt = (x, y) => page.evaluate(([x, y]) => {
      const chart = document.getElementById('daily-chart')._ci;
      const ratio = chart.currentDevicePixelRatio;
      return chart.ctx.getImageData(Math.round(x * ratio), Math.round(y * ratio), 1, 1).data[3] / 255;
    }, [x, y]);
    const near = (value, target) => Math.abs(value - target) <= 0.05;
    const premiumTop = mixed.premiumTop, networkTop = mixed.attributedTop;
    for (const [segmentTop, segmentBottom] of [[premiumTop, mixed.base], [networkTop, premiumTop]]) {
      const top = await alphaAt(mixed.x, segmentTop + 2), bottom = await alphaAt(mixed.x, segmentBottom - 2);
      expect(top).toBeGreaterThan(bottom);
      expect(near(top, 0.55)).toBe(true);
      expect(near(bottom, 0.2)).toBe(true);
    }
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
  const coloured = ([r, g, b, a]) => !purple([r, g, b, a]) && a > 40;
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
    expect(samples.centre[i][3]).toBeGreaterThan(40);
  }
  expect(Math.abs(samples.top + firstPremium - samples.premiumTop)).toBeLessThanOrEqual(1.5);
  // The stack never reaches the bar's own top edge.
  expect(firstColoured(samples.centre)).toBeGreaterThanOrEqual(2);
});

/* One frame of the chart as drawn now: per bucket, the bar's current (animated)
 * geometry and its centre and side pixel columns. */
const frame = page => page.evaluate(() => {
  const chart = document.getElementById('daily-chart')._ci;
  const ratio = chart.currentDevicePixelRatio;
  const pixel = (x, y) => [...chart.ctx.getImageData(Math.round(x * ratio), Math.round(y * ratio), 1, 1).data];
  return chart.getDatasetMeta(0).data.map((bar, index) => {
    const {x, y, base, width} = bar.getProps(['x', 'y', 'base', 'width']);
    const final = bar.getProps(['y'], true).y;
    const column = px => Array.from({length: Math.max(0, Math.ceil(base - y) + 2)}, (_, i) => [y - 1 + i, pixel(px, y - 1 + i + 0.5)]);
    return {item: chart.data.datasets[0].dpFamilies[index], x, y, base, width, final, centre: column(x), side: column(x - width / 2 + 1)};
  });
});

for (const period of ['7d', '30d', 'all']) for (const light of [false, true]) {
  test(`${period} ${light ? 'light' : 'dark'}: each inset stack grows with its purple bar through the entrance animation, as one object`, async ({ page }) => {
    await boot(page, {light, clock: true});
    await page.evaluate(value => window.DPStatisticsLifecycle.load(value), period);
    await expect.poll(() => page.evaluate(() => !!document.getElementById('daily-chart')._ci)).toBe(true);
    const premium = await rgbOf(page, 'premium'), network = await rgbOf(page, 'network');
    let previous = null, sawGrowing = false;
    for (let step = 0; step < 16; step++) {
      await page.clock.runFor(80);
      const bars = await frame(page);
      for (const bar of bars.filter(bar => bar.item.count > 0)) {
        const {item, base, y} = bar;
        const height = base - y;
        if (Math.abs(bar.final - y) > 2) sawGrowing = true;
        // Never past its final height, never shrinking between frames.
        expect(y).toBeGreaterThanOrEqual(bar.final - 0.5);
        if (previous) expect(height).toBeGreaterThanOrEqual(previous[bars.indexOf(bar)] - 0.5);
        const rowsOf = (column, rgb) => column.filter(([, px]) => family(rgb, px)).map(([row]) => row);
        const premiumRows = rowsOf(bar.centre, premium), networkRows = rowsOf(bar.centre, network);
        const familyRows = [...premiumRows, ...networkRows];
        if (!item.premium && !item.network) { expect(familyRows).toEqual([]); continue; }
        if (height * (item.premium + item.network) / item.count < 6) continue;      // too short to read rows yet
        // Inside the bar: below its top, above its base, purple at the side beside it.
        expect(Math.min(...familyRows)).toBeGreaterThan(y);
        expect(Math.max(...familyRows)).toBeLessThan(base + 1);
        // (Rows are the bar's own fractional pixel rows: centre and side share them by index.)
        const firstIndex = bar.centre.findIndex(([row]) => row === Math.min(...familyRows));
        const lastIndex = bar.centre.findIndex(([row]) => row === Math.max(...familyRows));
        expect(purple(bar.side[Math.round((firstIndex + lastIndex) / 2)][1])).toBe(true);
        // The families' shares of this frame's bar, on the same progress as the bar.
        // (The uppermost family gives up the established top inset -- at most 5 px, at most a
        // quarter of the stack -- where the purple above it would otherwise be thinner.)
        if (item.premium) {
          const share = height * item.premium / item.count;
          const inset = item.network ? 0 : Math.min(5, height * (item.premium + item.network) / item.count / 4);
          const drawn = base - Math.min(...premiumRows);
          expect(drawn).toBeLessThanOrEqual(share + 2);
          expect(drawn).toBeGreaterThanOrEqual(share - inset - 2);
        }
        const stackHeight = base - Math.min(...familyRows);
        expect(stackHeight).toBeLessThanOrEqual(height * (item.premium + item.network) / item.count + 1.5);
        expect(stackHeight).toBeGreaterThanOrEqual(height * (item.premium + item.network) / item.count - 6);
        // Unattributed: purple between the bar's top and the stack.
        if (item.unattributed) {
          // Every row from the bar's top to the stack is purple but the two anti-aliased edge rows.
          const gap = bar.centre.filter(([row]) => row >= y - 0.5 && row < Math.min(...familyRows));
          // (Whole sampled rows against a fractional share.)
          expect(gap.length).toBeGreaterThanOrEqual(Math.floor(height * item.unattributed / item.count) - 1);
          expect(gap.filter(([, px]) => !purple(px)).length).toBeLessThanOrEqual(2);
        }
        // One connected stack: no purple or empty row between its families.
        // One connected stack: every row a family colour or the blend between the two (never the
        // container's purple, whose green is far below both, and never empty).
        const between = px => [0, 1, 2].every(c => px[c] >= Math.min(network[c], premium[c]) - channelTolerance(px[3])
          && px[c] <= Math.max(network[c], premium[c]) + channelTolerance(px[3]));
        for (let index = firstIndex; index <= lastIndex; index++) {
          const px = bar.centre[index][1];
          expect(px[3]).toBeGreaterThan(40);
          expect(family(premium, px) || family(network, px) || between(px)).toBe(true);
        }
      }
      previous = bars.map(bar => bar.base - bar.y);
    }
    expect(sawGrowing).toBe(true);
    // The last frame is the static design.
    const settled = await frame(page);
    settled.forEach(bar => expect(Math.abs(bar.final - bar.y)).toBeLessThanOrEqual(0.5));
  });
}

test('changing period mid-animation starts the new chart from the baseline, with no stale stack', async ({ page }) => {
  await boot(page, {clock: true});
  await page.evaluate(() => window.DPStatisticsLifecycle.load('7d'));
  await expect.poll(() => page.evaluate(() => !!document.getElementById('daily-chart')._ci)).toBe(true);
  await page.clock.runFor(300);
  await page.evaluate(() => window.DPStatisticsLifecycle.load('1y'));
  const months = (await MONTHS(page)).join();
  await expect.poll(() => page.evaluate(() => document.getElementById('daily-chart')._ci.data.labels.join())).toBe(months);
  await page.clock.runFor(32);
  const early = await frame(page);
  // The new bars begin near the baseline, and nothing of the old chart remains drawn above them.
  early.forEach(bar => expect(bar.base - bar.y).toBeLessThan((bar.base - bar.final) * 0.5));
  const premium = await rgbOf(page, 'premium'), network = await rgbOf(page, 'network');
  for (const bar of early) {
    const stray = bar.centre.filter(([row, px]) => row < bar.y - 1 && (family(premium, px) || family(network, px)));
    expect(stray).toEqual([]);
  }
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
  expect(chart.labels).toEqual(await MONTHS(page));
  // One bar per bucket: the total. Unattributed is never a dataset of its own.
  expect(chart.datasets).toEqual([{role: 'total', data: [1, 13]}]);
  expect(chart.lines).toEqual([
    ['Total completions: 1', 'Premium Sources: 0', 'Network Sources: 1'],
    ['Total completions: 13', 'Premium Sources: 5', 'Network Sources: 4', 'Unattributed: 4'],
  ]);
});

for (const period of ['30d', 'all']) {
  test(`${period} now carries the same legend, one total bar per bucket and the family tooltip`, async ({ page }) => {
    await boot(page);
    await show(page, '7d');
    await show(page, period);
    expect(await legend(page)).toEqual(['Daily Completions', 'Premium Sources', 'Network Sources']);
    const chart = await page.evaluate(() => {
      const instance = document.getElementById('daily-chart')._ci;
      return {datasets: instance.data.datasets.map(set => ({label: set.label, role: set.dpRole, data: set.data})),
        plugins: instance.config.plugins.map(plugin => plugin.id),
        lines: instance.data.labels.map((_, index) => instance.options.plugins.tooltip.callbacks.label({dataIndex: index}))};
    });
    expect(chart.datasets).toEqual([{label: 'Completions', role: 'total', data: CASES.map(item => item.count)}]);
    expect(chart.plugins).toEqual(['dpCompletionFamilies']);
    expect(chart.lines[0]).toEqual(['Total completions: 10', 'Premium Sources: 4', 'Network Sources: 3', 'Unattributed: 3']);
    expect(chart.lines[3]).toEqual(['Total completions: 6', 'Premium Sources: 3', 'Network Sources: 3']);
  });
}

test('an empty period keeps its three-item legend; swatches follow the theme', async ({ page }) => {
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
