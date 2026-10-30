const { test, expect } = require('@playwright/test');

/* DP 1.0.13 Statistics, live while shown. GET /stats/detail is answered by the
 * page itself (a MOCK whose figures are a function of the period and of a
 * version the test moves, so every figure says which answer it came from), and
 * the application's one stream is held; invalidations, heartbeats and stream
 * restores are the document events app.js emits for them. One case lets app.js
 * itself forward a real stream frame. Nothing is written to the shared backend. */

const PERIOD_INDEX = {'1h': 1, '24h': 2, '7d': 3, '30d': 4, '1y': 5, all: 6};

function answer(period, version) {
  const mark = version * 10 + PERIOD_INDEX[period];
  const day = index => `2026-10-${String(10 + index).padStart(2, '0')}`;
  return {
    period,
    totals: {torrent_total: mark, completed_count: mark + 1, partial_total: mark + 2, success_rate_pct: 50, completed_size: mark},
    torrent_status: [{status: 'completed', count: mark}],
    file_status: [{status: 'completed', count: mark}],
    event_levels: [{level: 'error', count: mark}, {level: 'info', count: 1}],
    transfer_methods: [{method: 'Debrid', count: mark}],
    // From version 8 the window has moved on a day: the first bucket left, a new one arrived.
    daily_completions: (version >= 8 ? [1, 2, 3] : [0, 1, 2]).map(index => ({date: day(index), count: mark + index * version,
                                                premium: index, network: 0, unattributed: mark + index * version - index})),
  };
}

async function open(page, {delay = () => 0, stream} = {}) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.route('**/api/events/stream', stream || (() => {}));          // held: the test speaks for the stream
  const state = {version: 1, requests: [], open: 0, maxOpen: 0, fail: false};
  await page.route(url => url.pathname === '/api/stats/detail', async route => {
    const period = new URL(route.request().url()).searchParams.get('period');
    const entry = {period, version: state.version};
    state.requests.push(entry);
    state.open++;
    state.maxOpen = Math.max(state.maxOpen, state.open);
    const wait = delay(entry);
    if (wait) await new Promise(resolve => setTimeout(resolve, wait));
    state.open--;
    if (state.fail) return route.fulfill({status: 503, contentType: 'application/json', body: '{"detail":"busy"}'});
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(answer(period, entry.version))});
  });
  await page.goto('/');
  await page.waitForFunction(() => window.DPStatisticsLifecycle && typeof Chart !== 'undefined');
  await page.evaluate(() => nav(document.querySelector('[data-view="stats"]')));
  await expect.poll(() => figures(page)).toMatchObject({downloads: String(13)});
  await settled(page);
  return state;
}

const invalidate = page => page.evaluate(() => document.dispatchEvent(new CustomEvent('debridpulse:statistics-invalidated')));
const settled = page => page.waitForFunction(() => { const chart = document.getElementById('daily-chart')._ci; return chart && !Chart.animator.running(chart); });

// Every figure the page shows, from all three groups.
const figures = page => page.evaluate(() => {
  const metric = key => document.querySelector(`#detail-stat-cards > [data-dp-stats-metric="${key}"] .metric-value, #detail-stat-cards > [data-dp-stats-metric="${key}"] .stat-value`)?.textContent;
  const first = id => document.querySelector(`#${id} .kv-val`)?.textContent;
  const chart = document.getElementById('daily-chart')._ci;
  // 1.0.14: the two period cards, Downloads Added and the data figure.
  return {downloads: metric('downloads'), data: metric('data'),
          disposition: first('detail-torrent-status'), files: first('detail-file-status'), errors: first('detail-event-levels'),
          methods: first('detail-sources'), bars: chart ? chart.data.datasets[0].data.slice() : null,
          tooltip: chart ? chart.options.plugins.tooltip.callbacks.label({dataIndex: 0})[0] : null};
});
const expected = (period, version) => {
  const a = answer(period, version);
  return {downloads: String(a.totals.torrent_total), data: `${a.totals.completed_size.toFixed(1)} B`,
          disposition: String(a.torrent_status[0].count), files: String(a.file_status[0].count), errors: String(a.event_levels[0].count),
          methods: String(a.transfer_methods[0].count), bars: a.daily_completions.map(item => item.count),
          tooltip: `Total completions: ${a.daily_completions[0].count}`};
};

test('a change while Statistics is shown refreshes every group from one answer, and the same chart moves its bars', async ({page}) => {
  const state = await open(page);
  expect(await figures(page)).toEqual(expected('7d', 1));
  await page.evaluate(() => { window.__chart = document.getElementById('daily-chart')._ci; });
  const before = await page.evaluate(() => window.__chart.getDatasetMeta(0).data.map(bar => bar.getProps(['y']).y));

  // Every frame's drawn top of the third bar, from before the change until well after it.
  await page.evaluate(() => {
    window.__frames = [];
    const end = performance.now() + 3000;
    const sample = () => {
      const bar = window.__chart.getDatasetMeta(0).data[2];
      window.__frames.push({t: performance.now(), y: bar.y});
      if (performance.now() < end) requestAnimationFrame(sample);
    };
    requestAnimationFrame(sample);
  });
  state.version = 2;                                          // counts rise
  await invalidate(page);
  await expect.poll(() => state.requests.length).toBe(2);
  expect(state.requests[1]).toEqual({period: '7d', version: 2});
  await page.waitForTimeout(3100);
  await settled(page);
  const after = await page.evaluate(() => window.__chart.getDatasetMeta(0).data.map(bar => bar.getProps(['y']).y));
  expect(await page.evaluate(() => document.getElementById('daily-chart')._ci === window.__chart)).toBe(true);   // one instance
  // The bar rose (its value), and was drawn moving there from where it stood: several frames
  // strictly between its old and new positions over ~350 ms, never beyond either.
  expect(await page.evaluate(() => window.__chart.data.datasets[0].data[2])).toBeGreaterThan(15);
  const frames = await page.evaluate(() => window.__frames);
  const low = Math.min(before[2], after[2]), high = Math.max(before[2], after[2]);
  expect(high - low).toBeGreaterThan(3);
  const between = frames.filter(frame => frame.y > low + 0.5 && frame.y < high - 0.5);
  expect(between.length).toBeGreaterThanOrEqual(3);
  // Its duration, bracketed by drawn frames whatever their cadence: no longer than from the last
  // frame at the old place to the first at the new, no shorter than from the first moved frame
  // to the last not yet there (~350 ms; a slow runner widens the bracket, never moves it).
  const firstMoved = frames.findIndex(frame => frame.y !== before[2]);
  const firstThere = frames.findIndex((frame, i) => i > firstMoved && frame.y === after[2]);
  expect(firstMoved).toBeGreaterThan(0);
  expect(firstThere).toBeGreaterThan(firstMoved);
  expect(frames[firstThere].t - frames[firstMoved - 1].t).toBeGreaterThan(150);
  expect(frames[firstThere - 1].t - frames[firstMoved].t).toBeLessThan(600);
  frames.forEach(frame => { expect(frame.y).toBeGreaterThan(low - 0.5); expect(frame.y).toBeLessThan(high + 0.5); });
  expect(await figures(page)).toEqual(expected('7d', 2));
  // The family stack and tooltip read the bucket the chart holds now.
  expect(await page.evaluate(() => window.__chart.data.datasets[0].dpFamilies.map(item => item.count))).toEqual(expected('7d', 2).bars);

  state.version = 1;                                          // counts fall: the bar contracts
  await page.waitForTimeout(2100);                             // past the measured spacing
  await invalidate(page);
  await expect.poll(() => state.requests.length).toBe(3);
  await settled(page);
  expect(await page.evaluate(() => window.__chart.data.datasets[0].data[2])).toBe(15);              // contracted
  expect(await page.evaluate(() => document.getElementById('daily-chart')._ci === window.__chart)).toBe(true);
  expect(await figures(page)).toEqual(expected('7d', 1));
});

test('the last period chosen wins: a slow 7 Days answer never overwrites 1 Hour', async ({page}) => {
  let slow = true;
  const state = await open(page, {delay: entry => (entry.period === '7d' && slow ? 700 : 0)});
  state.version = 2;
  // Both chosen at once, neither awaited: the slow 7 Days answer arrives after 1 Hour was chosen.
  await page.evaluate(() => { void window.DPStatisticsLifecycle.load('7d'); });          // slow
  await page.evaluate(() => {
    document.querySelectorAll('#stats-period-tabs .ftab').forEach(tab => tab.classList.toggle('active', tab.dataset.period === '1h'));
    void window.DPStatisticsLifecycle.load('1h');
  });
  await invalidate(page);                                     // a change while 1 Hour is pending is not lost
  await page.waitForTimeout(1500);
  slow = false;
  await settled(page);
  expect(await figures(page)).toEqual(expected('1h', 2));     // every group from the 1 Hour answer
  expect(state.maxOpen).toBe(1);                              // the 1 Hour request waited for the 7 Days one
  expect(state.requests.at(-1).period).toBe('1h');
  await expect(page.locator('#chart-title')).toHaveText('Completed downloads in the last hour.');
});

test('a burst of changes is one request at a time, spaced, and ends on the last committed state', async ({page}) => {
  const state = await open(page, {delay: () => 300});
  const start = state.requests.length;
  for (let index = 0; index < 40; index++) {                  // far faster than the answer takes
    state.version = 2 + index;
    await invalidate(page);
    await page.waitForTimeout(50);
  }
  await page.waitForTimeout(3500);                            // the burst has subsided
  const issued = state.requests.length - start;
  expect(state.maxOpen).toBe(1);
  expect(issued).toBeGreaterThanOrEqual(1);
  expect(issued).toBeLessThanOrEqual(3);                       // ~2.3 s of burst, re-reads >= 2 s apart
  await settled(page);
  expect(await figures(page)).toEqual(expected('7d', state.version));        // nothing of the burst lost
});

test('hidden, Statistics asks for nothing; re-entry and a restored stream each re-read once', async ({page}) => {
  const state = await open(page);
  await page.evaluate(() => nav(document.querySelector('[data-view="dashboard"]')));
  const away = state.requests.length;
  state.version = 3;
  await invalidate(page);
  await page.evaluate(() => document.dispatchEvent(new CustomEvent('debridpulse:pulse-heartbeat')));
  await page.waitForTimeout(2500);
  expect(state.requests.length).toBe(away);
  await page.evaluate(() => nav(document.querySelector('[data-view="stats"]')));
  await expect.poll(() => state.requests.length).toBe(away + 1);
  await expect.poll(() => figures(page)).toEqual(expected('7d', 3));
  await expect(page.locator('#stats-period-tabs .ftab.active')).toHaveAttribute('data-period', '7d');

  state.version = 4;
  await page.waitForTimeout(2100);
  await page.evaluate(() => document.dispatchEvent(new CustomEvent('debridpulse:pulse-connected')));
  await expect.poll(() => state.requests.length).toBe(away + 2);
  await settled(page);
  expect(await figures(page)).toEqual(expected('7d', 4));
});

test('a heartbeat re-reads only an answer old enough to have aged', async ({page}) => {
  const state = await open(page);
  const fresh = state.requests.length;
  await page.evaluate(() => document.dispatchEvent(new CustomEvent('debridpulse:pulse-heartbeat')));
  await page.waitForTimeout(2500);
  expect(state.requests.length).toBe(fresh);                   // just read: nothing has aged
  // 26 s later (the shown answer's age, moved on rather than waited for).
  await page.evaluate(() => { const now = Date.now; Date.now = () => now() + 26000; });
  state.version = 5;                                           // e.g. a completion left the window
  await page.evaluate(() => document.dispatchEvent(new CustomEvent('debridpulse:pulse-heartbeat')));
  await expect.poll(() => state.requests.length).toBe(fresh + 1);
  await settled(page);
  expect(await figures(page)).toEqual(expected('7d', 5));
});

test('a failed live re-read keeps the page as it was and raises no toast', async ({page}) => {
  const state = await open(page);
  const shown = await figures(page);
  state.version = 6;
  state.fail = true;
  await invalidate(page);
  await expect.poll(() => state.requests.length).toBe(2);
  await page.waitForTimeout(500);
  expect(await figures(page)).toEqual(shown);
  await expect(page.locator('#toasts .toast')).toHaveCount(0);
});

test('with reduced motion a live change lands without a transition, on the same chart', async ({page}) => {
  await page.emulateMedia({reducedMotion: 'reduce'});
  const state = await open(page);
  await page.evaluate(() => { window.__chart = document.getElementById('daily-chart')._ci; });
  state.version = 2;
  await invalidate(page);
  await expect.poll(() => figures(page)).toEqual(expected('7d', 2));
  expect(await page.evaluate(() => Chart.animator.running(window.__chart))).toBe(false);
  expect(await page.evaluate(() => document.getElementById('daily-chart')._ci === window.__chart)).toBe(true);
});

test('app.js forwards the stream\'s statistics_changed and heartbeat', async ({page}) => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await page.addInitScript(() => {
    window.__beats = 0;
    document.addEventListener('debridpulse:pulse-heartbeat', () => { window.__beats++; });
  });
  const state = await open(page, {stream: async route => {
    await gate;
    await route.fulfill({status: 200, contentType: 'text/event-stream', body: 'event: connected\ndata: {}\n\n'
      + 'event: statistics_changed\ndata: {}\n\nevent: ping\ndata: {}\n\n'});
  }});
  const before = state.requests.length;
  state.version = 7;
  await page.waitForTimeout(2100);
  release();
  // connected and statistics_changed: re-reads, never two at once, ending on the newest state.
  await expect.poll(() => figures(page)).toEqual(expected('7d', 7));
  expect(state.requests.length).toBeGreaterThan(before);
  expect(state.maxOpen).toBe(1);
  // The ping is forwarded as the heartbeat. (1.0.14: it no longer re-reads /stats -- the
  // Overall & Rolling row that aged with it is retired.)
  await expect.poll(() => page.evaluate(() => window.__beats)).toBeGreaterThanOrEqual(1);
});

test('when buckets roll the same chart keeps labels, family stacks and tooltips index-aligned', async ({page}) => {
  const state = await open(page);
  await page.evaluate(() => { window.__chart = document.getElementById('daily-chart')._ci; });
  state.version = 8;
  await invalidate(page);
  await expect.poll(() => state.requests.length).toBe(2);
  await settled(page);
  const a = answer('7d', 8);
  const chart = await page.evaluate(() => {
    const instance = window.__chart;
    return {same: document.getElementById('daily-chart')._ci === instance,
            labels: instance.data.labels.slice(), data: instance.data.datasets[0].data.slice(),
            families: instance.data.datasets[0].dpFamilies.map(item => item.date),
            tooltips: instance.data.labels.map((_, index) => instance.options.plugins.tooltip.callbacks.label({dataIndex: index})[0]),
            elements: instance.getDatasetMeta(0).data.length};
  });
  const labels = await page.evaluate(dates => dates.map(date => {
    const [year, month, day] = date.split('-');
    return window.downloadsCalendarLabel({year, month, day});
  }), a.daily_completions.map(item => item.date));
  expect(chart).toEqual({same: true, labels, data: a.daily_completions.map(item => item.count),
                         families: a.daily_completions.map(item => item.date),
                         tooltips: a.daily_completions.map(item => `Total completions: ${item.count}`), elements: 3});
});
