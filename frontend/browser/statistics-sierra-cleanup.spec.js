const { test, expect } = require('@playwright/test');

/* DP 1.0.13 Statistics cleanup: copy and card titles, the page still fitting
 * where it did, Completions bucket labels in the viewer's date preference,
 * whole-number count ticks, Transfer Methods as the server names them, and
 * Events Severity in its fixed order. (1.0.14 retired the overall/rolling KPI
 * row and its separator: their cases now assert that they are gone.) */

const PREF_KEY = 'debridpulse.downloads.date-presentation.v1';
const bucket = (date, count) => ({date, count, premium: 0, network: 0, unattributed: count});
const PERIODS = {
  '1h': [bucket('09:05', 1), bucket('14:06', 2)],
  '24h': [bucket('00:00', 1), bucket('13:00', 3)],
  '7d': [bucket('2025-10-05', 2), bucket('2025-12-31', 1)],
  '30d': [bucket('2025-10-05', 2), bucket('2025-12-31', 1)],
  '1y': [bucket('2025-11', 4), bucket('2025-12', 1)],
  all: [bucket('2025-10-05', 2), bucket('2025-12-31', 1)],
};
const BREAKDOWN = {
  torrent_status: [{status: 'completed', count: 9}, {status: 'deleted', count: 2}],
  file_status: [{status: 'completed', count: 9}],
  // In the query's own order, which is not the display order.
  event_levels: [{level: 'error', count: 2}, {level: 'info', count: 4}, {level: 'warning', count: 3}],
  transfer_methods: [{method: 'Debrid', count: 6}, {method: 'Media Downloads', count: 4}, {method: 'HTTP(S)', count: 3},
                     {method: 'Unattributed', count: 2}, {method: 'Usenet', count: 1}],
};

// Every acquisition method at once: "Media Downloads" wraps in the narrow two-column card.
const ALL_METHODS = [['Debrid', 4], ['HTTP(S)', 4], ['FTP / SFTP', 2], ['Media Downloads', 2], ['SCP', 1],
  ['Unattributed', 1], ['Usenet', 1], ['WebDAV', 1], ['rsync', 1]].map(([method, count]) => ({method, count}));

async function boot(page, {width = 1440, height = 1000, pref, timezone = 'UTC', daily, methods} = {}) {
  await page.setViewportSize({width, height});
  if (pref) await page.addInitScript(([key, value]) => localStorage.setItem(key, JSON.stringify(value)), [PREF_KEY, pref]);
  await page.route(url => url.pathname === '/api/stats/detail', route => {
    const period = new URL(route.request().url()).searchParams.get('period');
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({
      period, totals: {torrent_total: 12, completed_count: 9}, ...BREAKDOWN,
      ...(methods ? {transfer_methods: methods} : {}), daily_completions: daily || PERIODS[period]})});
  });
  await page.goto('/');
  await page.waitForFunction(() => window.DPStatisticsLifecycle && typeof Chart !== 'undefined' && typeof window.downloadsCalendarLabel === 'function');
  await page.evaluate(zone => { settingsData = {...(settingsData || {}), timezone: zone}; }, timezone);
  await page.evaluate(() => nav(document.querySelector('[data-view="stats"]')));
}
const show = async (page, period) => {
  await page.evaluate(value => window.DPStatisticsLifecycle.load(value), period);
  await page.waitForFunction(() => { const chart = document.getElementById('daily-chart')._ci; return chart && !Chart.animator.running(chart); });
};
// The axis labels and, for each bucket, the tooltip title Chart.js shows for it.
const labels = page => page.evaluate(() => {
  const chart = document.getElementById('daily-chart')._ci;
  return chart.data.labels.map((label, index) => {
    chart.tooltip.setActiveElements([{datasetIndex: 0, index}], {x: 0, y: 0});
    chart.update('none');
    return {axis: label, tooltip: chart.tooltip.title.join(' '), body: chart.tooltip.body.flatMap(item => item.lines)};
  });
});

test('copy and card titles', async ({ page }) => {
  await boot(page);
  await show(page, '7d');
  await expect(page.locator('#view-stats .dash-kpi')).toHaveCount(0);
  const titles = await page.locator('#view-stats .dp-stats-breakdown-grid .card-title').allTextContents();
  expect(titles).toEqual(['Transfer Disposition', 'File Status', 'Events Severity', 'Transfer Methods']);
  const view = await page.locator('#view-stats').innerText();
  for (const old of ['LIFE-TIME', 'Top Sources', 'Download Status', 'recorded finished']) expect(view).not.toContain(old);
});

const rowsOf = (page, id) => page.locator(`#${id} .kv-row`).evaluateAll(nodes => nodes.map(node => [
  node.querySelector('.kv-key').textContent, node.querySelector('.kv-val').textContent]));

test('Transfer Methods lists the methods the server names, in its ranking', async ({ page }) => {
  await boot(page);
  await show(page, '7d');
  expect(await rowsOf(page, 'detail-sources')).toEqual(
    [['Debrid', '6'], ['Media Downloads', '4'], ['HTTP(S)', '3'], ['Unattributed', '2'], ['Usenet', '1']]);
});

for (const width of [1440, 900, 390]) {
  test(`every Transfer Methods label is read whole, its count on its first line (${width}px)`, async ({ page }) => {
    await boot(page, {width, methods: ALL_METHODS});
    await show(page, '7d');
    const rows = await page.locator('#detail-sources .kv-row').evaluateAll(nodes => nodes.map(node => {
      const [key, value] = [node.querySelector('.kv-key'), node.querySelector('.kv-val')];
      const [k, v, card] = [key.getBoundingClientRect(), value.getBoundingClientRect(),
                            node.closest('.list-card').getBoundingClientRect()];
      return {label: key.textContent, whole: key.scrollWidth <= key.clientWidth + 0.5,
              // The count's line centres on the label's first line, however many lines the label takes.
              firstLine: v.top + v.height / 2 > k.top && v.top + v.height / 2 < k.top + 16,
              apart: v.left >= k.right, inside: v.right <= card.right};
    }));
    expect(rows).toHaveLength(9);
    for (const row of rows) expect(row, row.label).toMatchObject({whole: true, firstLine: true, apart: true, inside: true});
    expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBe(0);
  });
}

test('Events Severity is always Errors, Warnings, Informational', async ({ page }) => {
  await boot(page);
  await show(page, '7d');
  expect(await rowsOf(page, 'detail-event-levels')).toEqual([['Errors', '2'], ['Warnings', '3'], ['Informational', '4']]);
  // A severity with no events is simply absent; the others keep their order.
  await page.unroute(url => url.pathname === '/api/stats/detail');
  await page.route(url => url.pathname === '/api/stats/detail', route => route.fulfill({status: 200,
    contentType: 'application/json', body: JSON.stringify({period: '30d', totals: {}, ...BREAKDOWN,
      event_levels: [{level: 'info', count: 9}, {level: 'error', count: 1}], daily_completions: PERIODS['30d']})}));
  await show(page, '30d');
  expect(await rowsOf(page, 'detail-event-levels')).toEqual([['Errors', '1'], ['Informational', '9']]);
});

test('no separator and no overall/rolling row: the period choice drives the two cards, then the chart', async ({ page }) => {
  await boot(page);
  await show(page, '7d');
  await expect(page.locator('#view-stats [role="separator"]')).toHaveCount(0);
  await expect(page.locator('#view-stats .dp-stats-history-grid')).toHaveCount(0);
  expect(await page.evaluate(() => document.getElementById('detail-stat-cards').nextElementSibling.className)).toContain('dp-stats-chart');
  await page.locator('#stats-period-tabs .ftab[data-period="30d"]').click();
  await expect(page.locator('#detail-stat-cards [data-dp-stats-metric="downloads"] .metric-sub')).toHaveText('Downloads added during the last 30 days.');
  await expect(page.locator('#detail-stat-cards [data-dp-stats-metric="data"] .metric-sub')).toHaveText('Total data downloaded during the last 30 days.');
});

// The whole chart -- plot, zero line and x-axis -- lies inside its card.
const chartInsideCard = page => page.evaluate(() => {
  const chart = document.getElementById('daily-chart')._ci;
  const card = document.querySelector('#view-stats .dp-stats-chart').getBoundingClientRect();
  const canvas = chart.canvas.getBoundingClientRect();
  return {canvasWithinCard: canvas.bottom <= card.bottom + 0.5, axisWithinCard: canvas.top + chart.scales.x.bottom <= card.bottom + 0.5,
          plot: chart.chartArea.bottom - chart.chartArea.top};
});

// 1440x914 is the shortest desktop height at which the page fit before the
// (since retired) separator; 950 lies in the band where the x-axis used to be cut off. (With
// the 22px desktop inset, af5496ef, no method's label wraps at these widths;
// the wrapped label is covered at 780px below.)
for (const [width, height] of [[1440, 914], [1440, 950], [1440, 1000], [1920, 914], [1920, 1080]]) {
  test(`the page still fits at ${width}x${height}, and the whole chart shows`, async ({ page }) => {
    await boot(page, {width, height, methods: ALL_METHODS});
    await show(page, '7d');
    const boxes = await page.evaluate(() => ({
      heights: [...document.querySelectorAll('#view-stats .dp-stats-breakdown-grid > .list-card')]
        .map(card => Math.round(card.getBoundingClientRect().height)),
      wrapped: [...document.querySelectorAll('#detail-sources .kv-key')]
        .some(key => key.getBoundingClientRect().height > 20)}));
    // The four boxes keep one equal height, wrapped label or not.
    expect(new Set(boxes.heights).size).toBe(1);
    const fit = await page.evaluate(() => {
      const body = document.querySelector('#view-stats > .dp-stats-master-body');
      const doc = document.documentElement;
      return {body: body.scrollHeight - body.clientHeight, doc: doc.scrollHeight - doc.clientHeight};
    });
    expect(fit).toEqual({body: 0, doc: 0});
    const chart = await chartInsideCard(page);
    expect(chart.canvasWithinCard).toBe(true);
    expect(chart.axisWithinCard).toBe(true);
    expect(chart.plot).toBeGreaterThan(60);
  });
}

// At the 780px floor, with every acquisition method listed, "Media Downloads"
// wraps (measured in the web face and the fallback face alike; from 800px up no
// label wraps), and the four boxes still keep one equal height.
test('at the 780px floor a wrapped method label leaves the four boxes one equal height', async ({ page }) => {
  await boot(page, {width: 780, height: 914, methods: ALL_METHODS});
  await show(page, '7d');
  const boxes = await page.evaluate(() => ({
    heights: [...document.querySelectorAll('#view-stats .dp-stats-breakdown-grid > .list-card')]
      .map(card => Math.round(card.getBoundingClientRect().height)),
    wrapped: [...document.querySelectorAll('#detail-sources .kv-key')]
      .filter(key => key.getBoundingClientRect().height > 20).map(key => key.textContent.trim())}));
  expect(boxes.wrapped).toContain('Media Downloads');
  expect(boxes.heights).toHaveLength(4);
  expect(new Set(boxes.heights).size).toBe(1);
});

test('on a phone the card\'s body scrolls, not the page, and the whole chart shows', async ({ page }) => {
  await boot(page, {width: 390, height: 844});
  await show(page, '7d');
  const layout = await page.evaluate(() => {
    const content = document.getElementById('content'), body = document.querySelector('#view-stats > .dp-stats-master-body');
    return {page: content.scrollHeight > content.clientHeight + 1, body: body.scrollHeight > body.clientHeight + 1};
  });
  expect(layout).toEqual({page: false, body: true});
  await page.evaluate(() => document.querySelector('#view-stats > .dp-stats-master-body').scrollTo(0, 99999));
  const chart = await chartInsideCard(page);
  expect(chart.canvasWithinCard).toBe(true);
  expect(chart.axisWithinCard).toBe(true);
});

const EXPECTED = {
  iso24: {'1h': ['09:05', '14:06'], '24h': ['00:00', '13:00'], '7d': ['2025-10-05', '2025-12-31'], '1y': ['2025-11', '2025-12']},
  us12: {'1h': ['09:05 AM', '02:06 PM'], '24h': ['12:00 AM', '01:00 PM'], '7d': ['10/05/2025', '12/31/2025'], '1y': ['11/2025', '12/2025']},
  intl24: {'1h': ['09:05', '14:06'], '24h': ['00:00', '13:00'], '7d': ['05/10/2025', '31/12/2025'], '1y': ['11/2025', '12/2025']},
  friendly12: {'1h': ['09:05 AM', '02:06 PM'], '24h': ['12:00 AM', '01:00 PM'], '7d': ['Oct 5, 2025', 'Dec 31, 2025'], '1y': ['Nov 2025', 'Dec 2025']},
};
const PREFS = {iso24: {format: 'iso', hour12: false}, us12: {format: 'us', hour12: true},
               intl24: {format: 'international', hour12: false}, friendly12: {format: 'friendly', hour12: true}};
for (const [name, pref] of Object.entries(PREFS)) {
  test(`Completions labels and tooltips follow the ${name} preference on all six periods`, async ({ page }) => {
    // A time zone far from UTC: the buckets are the server's own, never shifted.
    await boot(page, {pref, timezone: 'Asia/Tokyo'});
    for (const period of ['1h', '24h', '7d', '30d', '1y', 'all']) {
      await show(page, period);
      const expected = EXPECTED[name][{'30d': '7d', all: '7d'}[period] || period];
      const drawn = await labels(page);
      expect(drawn.map(item => item.axis), period).toEqual(expected);
      expect(drawn.map(item => item.tooltip), period).toEqual(expected);
      // Counts and their tooltip lines are untouched.
      expect(drawn.map(item => item.body[0]), period).toEqual(PERIODS[period].map(item => `Total completions: ${item.count}`));
    }
  });
}

test('friendly dates name today and yesterday, and a month of this year without its year', async ({ page }) => {
  await boot(page, {pref: {format: 'friendly', hour12: false}, timezone: 'Asia/Tokyo'});
  const today = await page.evaluate(() => {
    const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', {timeZone: 'Asia/Tokyo', year: 'numeric', month: '2-digit', day: '2-digit'})
      .formatToParts(new Date()).map(part => [part.type, part.value]));
    const yesterday = new Date(Date.UTC(+parts.year, +parts.month - 1, +parts.day) - 86400000);
    return {day: `${parts.year}-${parts.month}-${parts.day}`, yesterday: yesterday.toISOString().slice(0, 10),
            month: `${parts.year}-${parts.month}`, monthName: new Intl.DateTimeFormat('en-US', {timeZone: 'UTC', month: 'short'}).format(yesterday)};
  });
  await page.evaluate(([day, yesterday, month]) => {
    window.__dpDaily = {'7d': [{date: yesterday, count: 1}, {date: day, count: 2}], '1y': [{date: month, count: 3}]};
  }, [today.day, today.yesterday, today.month]);
  await page.unroute(url => url.pathname === '/api/stats/detail');
  await page.route(url => url.pathname === '/api/stats/detail', async route => {
    const period = new URL(route.request().url()).searchParams.get('period');
    const daily = await page.evaluate(value => window.__dpDaily[value], period);
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({
      period, totals: {}, ...BREAKDOWN, daily_completions: daily.map(item => bucket(item.date, item.count))})});
  });
  await show(page, '7d');
  expect((await labels(page)).map(item => item.axis)).toEqual(['Yesterday', 'Today']);
  await show(page, '1y');
  const month = (await labels(page))[0].axis;
  expect(month).not.toMatch(/\d{4}/);
  expect(month).toMatch(/^[A-Z][a-z]{2}$/);
});

for (const [name, counts, ticks] of [
  ['empty', [], [0, 1]], ['all zero', [0, 0], [0, 1]], ['one', [1], [0, 1]], ['two', [2, 0], [0, 1, 2]],
  ['sparse', [0, 3, 0], [0, 1, 2, 3]], ['several', [7, 13], null],
]) {
  test(`the count axis has whole-number ticks, never a step below one: ${name}`, async ({ page }) => {
    await boot(page, {daily: counts.map((count, index) => bucket(`2025-10-0${index + 1}`, count))});
    await show(page, '7d');
    const values = await page.evaluate(() => document.getElementById('daily-chart')._ci.scales.y.ticks.map(tick => tick.value));
    values.forEach(value => expect(Number.isInteger(value)).toBe(true));
    values.slice(1).forEach((value, index) => expect(value - values[index]).toBeGreaterThanOrEqual(1));
    expect(values[0]).toBe(0);
    if (ticks) expect(values).toEqual(ticks);
    else expect(values[values.length - 1]).toBeGreaterThanOrEqual(Math.max(...counts));
  });
}
