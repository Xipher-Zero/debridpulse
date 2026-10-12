const { test, expect } = require('@playwright/test');

/* DP 1.0.13 decorative Dashboard ECG strip. The six KPI cards' lines form one
 * virtual strip: rows top to bottom, each row right to left, gaps and row
 * breaks travelled unseen at their real distance. One owner starts a
 * heartbeat every 2 s; each travels the whole strip right to left at
 * 0.24 px/ms, so several can be on it at once. Each card's single line is the
 * signal: a flat baseline deformed into the ECG complex where a heartbeat is.
 * The test owns time (page.clock) for timers and animation frames. */

const SPEED = 0.24;          // px per ms
const INTERVAL = 2000;       // ms between heartbeats
const STEP = 50;
const R_OFFSET = 22;         // the R peak (y=2) sits 22 px behind the complex's leading edge

async function boot(page, {width = 1600, height = 900, reducedMotion = 'no-preference'} = {}) {
  await page.setViewportSize({width, height});
  await page.emulateMedia({reducedMotion});
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.clock.install();
  await page.goto('/');
  await expect(page.locator('#s-total')).not.toHaveText('—');
  // From here only the test moves time.
  await page.clock.pauseAt(await page.evaluate(() => Date.now() + 50));
}

/* Each card in DOM order: its geometry, line points, path count and gradient stops. */
const strip = page => page.evaluate(() => [...document.querySelectorAll('#view-dashboard .dp-card-spark')].map(svg => {
  const d = svg.querySelector('.dp-card-spark-line').getAttribute('d');
  const box = svg.getBoundingClientRect();
  return {width: box.width, left: box.left, right: box.right, middle: (box.top + box.bottom) / 2,
    points: [...d.matchAll(/(-?[\d.]+) (-?[\d.]+)/g)].map(m => [Number(m[1]), Number(m[2])]),
    paths: svg.querySelectorAll('path').length,
    stops: [...svg.querySelectorAll('stop')].map(stop => ({role: stop.getAttribute('class'), offset: Number(stop.getAttribute('offset')),
      opacity: Number(getComputedStyle(stop).stopOpacity)}))};
}));
const leads = card => card.points.filter(([, y]) => y === 2).map(([x]) => x - R_OFFSET);
const flat = card => card.points.every(([, y]) => y === 16);

/* The expected route, from the rendered rectangles: each card's entry (px). */
function route(cards) {
  const rows = [];
  [...cards.keys()].sort((a, b) => cards[a].middle - cards[b].middle).forEach(i => {
    const row = rows[rows.length - 1];
    if (row && Math.abs(cards[i].middle - cards[row[0]].middle) < 8) row.push(i); else rows.push([i]);
  });
  const entries = [];
  let previous = null, entry = 0;
  rows.forEach(row => row.sort((a, b) => cards[b].right - cards[a].right).forEach(i => {
    const card = cards[i];
    if (previous) entry += previous.row === row ? previous.card.right - card.right
      : previous.card.width + Math.hypot(card.right - previous.card.left, card.middle - previous.card.middle);
    entries[i] = entry;
    previous = {card, row};
  }));
  return {rows, entries};
}

/* Enter the Dashboard afresh and sample the strip every STEP ms for `ms`. */
async function enterAndSample(page, ms) {
  // Through the navigation owner itself: at phone widths the sidebar is collapsed.
  await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]')));
  await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="dashboard"]')));
  const samples = [];
  for (let t = 0; t < ms; t += STEP) {
    await page.clock.runFor(STEP);
    samples.push({t: t + STEP, cards: await strip(page)});
  }
  return samples;
}

function expectOneContinuousLine(cards) {
  for (const card of cards) {
    expect(card.paths).toBe(1);                                         // no second path over the baseline
    const xs = card.points.map(([x]) => x);
    expect(xs).toEqual([...xs].sort((a, b) => a - b));                  // never doubles back: no doubled line
    expect(card.points[0][1]).toBe(16);
    expect(card.points[0][0]).toBeLessThanOrEqual(0);                   // the line spans the card edge to edge ...
    const last = card.points[card.points.length - 1];
    expect(last[1]).toBe(16);
    expect(last[0]).toBeGreaterThanOrEqual(card.width - 0.01);          // ... without a gap
    const offsets = card.stops.map(stop => stop.offset);
    expect(offsets).toEqual([...offsets].sort((a, b) => a - b));
  }
}

async function expectStripFollowsLayout(page, {rowsExpected}) {
  const cards = await strip(page);
  const {rows, entries} = route(cards);
  // Long enough for the first heartbeat to reach the last card in the route.
  const samples = await enterAndSample(page, INTERVAL + Math.max(...entries) / SPEED + 1000);
  expect(rows.length).toBe(rowsExpected);

  // Resting until the first heartbeat, two seconds after entry.
  for (const sample of samples.filter(s => s.t < INTERVAL)) expect(sample.cards.every(flat)).toBe(true);

  // The first heartbeat reaches each card when the strip says it should: in
  // route order, right to left within a row, row by row, gaps included.
  for (const [i, entry] of entries.entries()) {
    const seen = samples.find(s => leads(s.cards[i]).some(x => x < cards[i].width));
    const due = INTERVAL + entry / SPEED;
    expect(seen.t - due, `card ${i}`).toBeGreaterThan(0);
    expect(seen.t - due, `card ${i}`).toBeLessThanOrEqual(STEP + 17);
  }

  // Right to left at one constant speed, slower than the former ~0.31 px/ms.
  const first = rows[0][0];
  const path = samples.map(s => leads(s.cards[first])).filter(l => l.length === 1).map(l => l[0]).slice(0, 8);
  expect(path.length).toBe(8);
  // (Frames land on 16 ms ticks, so one STEP shows 48 or 64 ms of travel.)
  path.slice(1).forEach((x, i) => {
    expect(path[i] - x).toBeGreaterThanOrEqual(SPEED * (STEP - 16) - 0.01);
    expect(path[i] - x).toBeLessThanOrEqual(SPEED * (STEP + 16) + 0.01);
  });
  expect(Math.abs((path[0] - path[7]) / (7 * STEP) - SPEED)).toBeLessThanOrEqual(SPEED * 16 / (7 * STEP) + 1e-6);

  // Several heartbeats travel the strip at once, every card one continuous line.
  const counts = samples.map(s => s.cards.reduce((n, c) => n + leads(c).filter(x => x > -48 && x < c.width).length, 0));
  expect(Math.max(...counts)).toBeGreaterThanOrEqual(2);
  for (const sample of samples) expectOneContinuousLine(sample.cards);
  return samples;
}

test('one heartbeat strip crosses the single row right to left at one speed, the line itself deforming', async ({page}) => {
  await boot(page);
  const before = await page.evaluate(() => [...document.querySelectorAll('.dhs-val')].map(v => v.textContent));
  const samples = await expectStripFollowsLayout(page, {rowsExpected: 1});

  // The deformed region: rest, a brighter lead, the body, then rest again behind it.
  const busy = samples.flatMap(s => s.cards).find(c => leads(c).some(x => x > 0 && x < c.width - 48));
  expect(busy.stops.map(s => s.role).slice(0, 5)).toEqual(['dp-card-spark-rest', 'dp-card-spark-rest', 'dp-card-spark-lead', 'dp-card-spark-body', 'dp-card-spark-rest']);
  expect(busy.stops.map(s => s.opacity).slice(0, 5)).toEqual([0.4, 0.4, 1, 1, 0.4]);

  // A stats refresh or Dashboard reselection neither resets nor jolts the heartbeats on the strip.
  const mid = await strip(page);
  await page.evaluate(() => Promise.all([loadStats(), loadStats()]));
  await page.locator('#sidebar .nav-item[data-view="dashboard"]').click();
  await page.clock.runFor(STEP);
  const after = await strip(page);
  for (const [i, card] of mid.entries()) {
    const now = leads(after[i]);
    for (const x of leads(card).filter(x => x - SPEED * STEP > -48)) {
      expect(now.some(y => Math.abs(y - (x - SPEED * STEP)) < 0.5)).toBe(true);
    }
  }
  expect(await page.evaluate(() => [...document.querySelectorAll('.dhs-val')].map(v => v.textContent))).toEqual(before);
  expect(await page.evaluate(() => localStorage.getItem('debridpulse.dashboard.metric-history.v2'))).toBeNull();
});

for (const [width, height, rows] of [[1000, 900, 2], [560, 1400, 3]]) {
  test(`wrapped into ${rows} rows the cards are one strip: rows top to bottom, each right to left, row breaks travelled unseen`, async ({page}) => {
    test.setTimeout(90000);
    await boot(page, {width, height});
    await expectStripFollowsLayout(page, {rowsExpected: rows});
  });
}

test('a reflow re-measures the strip under the heartbeats already on it, without restarting or doubling them', async ({page}) => {
  await boot(page);
  // When heartbeats enter the route's first card (fake ms), sampled every STEP.
  const entries = async ms => {
    const times = [];
    for (let t = 0; t < ms; t += STEP) {
      const cards = await strip(page);
      const card = cards[route(cards).rows[0][0]];
      const inside = leads(card).filter(x => x < card.width);
      await page.clock.runFor(STEP);
      const now = await strip(page);
      // Inside the card now, and not one step ago (it has moved at most SPEED * (STEP + 16) since).
      const entered = leads(now[route(now).rows[0][0]]).filter(x => x < card.width
        && !inside.some(previous => previous > x && previous - x <= SPEED * (STEP + 16) + 0.01));
      if (entered.length) times.push(await page.evaluate(() => Date.now()));
    }
    return times;
  };
  const [beforeReflow] = await entries(INTERVAL + 100);
  await page.setViewportSize({width: 1000, height: 900});
  await expect.poll(async () => route(await strip(page)).rows.length).toBe(2);
  await expect.poll(() => page.evaluate(() => [...document.querySelectorAll('#view-dashboard .dp-card-spark')]
    .every(svg => Math.abs(Number(svg.getAttribute('viewBox').split(' ')[2]) - svg.getBoundingClientRect().width) < 0.01))).toBe(true);
  expectOneContinuousLine(await strip(page));
  // The heartbeat cadence carries straight on through the reflow: the next
  // entries fall on the same 2 s phase (no restart), one per 2 s (no second timer).
  const after = await entries(2 * INTERVAL);
  expect(after.length).toBe(2);
  after.forEach(at => {
    const phase = ((at - beforeReflow) % INTERVAL + INTERVAL) % INTERVAL;
    expect(Math.min(phase, INTERVAL - phase)).toBeLessThanOrEqual(STEP + 16);
  });
});

test('leaving the Dashboard, hiding the document or reducing motion flattens every line, and each return starts afresh', async ({page}) => {
  await boot(page);
  const stopsAndResumes = async (leave, back) => {
    await page.clock.runFor(INTERVAL + 1500);
    expect((await strip(page)).some(c => !flat(c))).toBe(true);
    await leave();
    // (A media-query change reaches the page at its next rendering step.)
    await expect.poll(async () => (await strip(page)).every(flat)).toBe(true);
    await page.clock.runFor(8000);
    expect((await strip(page)).every(flat)).toBe(true);
    await back();
    await expect.poll(() => page.evaluate(() => !document.hidden && !matchMedia('(prefers-reduced-motion: reduce)').matches)).toBe(true);
    await page.clock.runFor(INTERVAL - 100);
    expect((await strip(page)).every(flat)).toBe(true);                // a fresh start: the first heartbeat is 2 s away
  };
  await stopsAndResumes(
    () => page.locator('#sidebar .nav-item[data-view="torrents"]').click(),
    () => page.locator('#sidebar .nav-item[data-view="dashboard"]').click());
  const visibility = hidden => page.evaluate(hidden => {
    Object.defineProperty(document, 'hidden', {configurable: true, get: () => hidden});
    document.dispatchEvent(new Event('visibilitychange'));
  }, hidden);
  await stopsAndResumes(() => visibility(true), () => visibility(false));
  await stopsAndResumes(
    () => page.emulateMedia({reducedMotion: 'reduce'}),
    () => page.emulateMedia({reducedMotion: 'no-preference'}));
});

test('reduced motion from the start shows only the still flat baselines', async ({page}) => {
  await boot(page, {reducedMotion: 'reduce'});
  expect(await page.locator('#view-dashboard .dp-card-spark').evaluateAll(svgs => svgs.every(svg => svg.getAttribute('aria-hidden') === 'true'))).toBe(true);
  await page.clock.runFor(10000);
  expect((await strip(page)).every(flat)).toBe(true);
  await page.locator('#sidebar .nav-item[data-view="torrents"]').click();
  await page.locator('#sidebar .nav-item[data-view="dashboard"]').click();
  await page.clock.runFor(10000);
  expect((await strip(page)).every(flat)).toBe(true);
});
