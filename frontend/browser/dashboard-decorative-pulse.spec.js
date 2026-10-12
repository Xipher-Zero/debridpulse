const { test, expect } = require('@playwright/test');

/* DP 1.0.13 decorative Dashboard ECG strip. The six KPI cards' lines form one
 * virtual strip: rows top to bottom, each row right to left, gaps and row
 * breaks travelled unseen at their real distance. One owner starts a
 * heartbeat every 2 s at rest (30 BPM), up to 75 BPM as download throughput
 * reaches the installation's sustained peak; each travels the whole strip
 * right to left at 0.24 px/ms, so several can be on it at once. Each card's
 * single line is the signal: a flat baseline deformed into the ECG complex
 * where a heartbeat is, its brightness fading behind it back to the baseline
 * just before the following heartbeat. The test owns time (page.clock) for
 * timers and animation frames, and the throughput facts where it says so. */

const SPEED = 0.24;          // px per ms
const INTERVAL = 2000;       // ms between heartbeats
const STEP = 50;
const R_OFFSET = 22;         // the R peak (y=2) sits 22 px behind the complex's leading edge

async function boot(page, {width = 1600, height = 900, reducedMotion = 'no-preference', throughput} = {}) {
  await page.setViewportSize({width, height});
  await page.emulateMedia({reducedMotion});
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  // The throughput facts, when the test sets them: {bps, peak} read at every poll.
  if (throughput) await page.route('**/api/execution/throughput', route => route.fulfill({status: 200, contentType: 'application/json',
    body: JSON.stringify({ok: true, download_bytes_per_second: throughput.bps, sustained_peak_bytes_per_second: throughput.peak,
      max_download_bytes_per_second: 0})}));
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
  const gradient = svg.querySelector('linearGradient');
  return {width: box.width, left: box.left, right: box.right, middle: (box.top + box.bottom) / 2,
    from: Number(gradient.getAttribute('x1')), to: Number(gradient.getAttribute('x2')),
    points: [...d.matchAll(/(-?[\d.]+) (-?[\d.]+)/g)].map(m => [Number(m[1]), Number(m[2])]),
    paths: svg.querySelectorAll('path').length,
    stops: [...svg.querySelectorAll('stop')].map(stop => ({role: stop.getAttribute('class'), offset: Number(stop.getAttribute('offset')),
      opacity: Number(getComputedStyle(stop).stopOpacity)}))};
}));
const leads = card => card.points.filter(([, y]) => y === 2).map(([x]) => x - R_OFFSET);
const flat = card => card.points.every(([, y]) => y === 16);
/* Each heartbeat's gradient group in px: [rest, lead, lead, body, tail, rest]. */
const groups = card => {
  const out = [];
  for (let i = 1; i + 6 <= card.stops.length; i += 6) {
    const offsets = card.stops.slice(i, i + 6).map(stop => stop.offset);
    if (offsets.every(offset => offset === 1)) continue;          // an unused group
    out.push(offsets.map(offset => card.from + offset * (card.to - card.from)));
  }
  return out.sort((a, b) => a[0] - b[0]);
};

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
  expect(busy.stops.map(s => s.role).slice(0, 7)).toEqual(['rest', 'rest', 'lead', 'lead', 'body', 'tail', 'rest'].map(role => `dp-card-spark-${role}`));
  expect(busy.stops.map(s => s.opacity).slice(0, 7)).toEqual([0.4, 0.4, 1, 1, 1, 0.62, 0.4]);

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

/* Heartbeat births, read where each one enters the route's first card,
 * sampled every STEP ms for `ms`; also each sample's first-card leads. */
async function births(page, ms, into = {times: [], leads: []}) {
  for (let t = 0; t < ms; t += STEP) {
    const cards = await strip(page);
    const first = cards[route(cards).rows[0][0]];
    const inside = leads(first).filter(x => x < first.width);
    await page.clock.runFor(STEP);
    const now = (await strip(page))[route(cards).rows[0][0]];
    const entered = leads(now).filter(x => x < first.width
      && !inside.some(previous => previous > x && previous - x <= SPEED * (STEP + 16) + 0.01));
    for (const _ of entered) into.times.push(await page.evaluate(() => Date.now()));
    into.leads.push(leads(now).filter(x => x < now.width));
  }
  return into;
}
const intervals = times => times.slice(1).map((at, i) => at - times[i]);
const QUANTUM = STEP + 16;          // a birth is read within one sample and one frame

test('the leading QRS is more intense than the body, in both themes, and the trail fades through the tail', async ({page}) => {
  await boot(page);
  const luminance = color => {
    const [r, g, b] = color.match(/[\d.]+/g).slice(0, 3).map(Number).map(v => (v > 1 ? v / 255 : v));
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  await page.clock.runFor(INTERVAL + 200);
  for (const theme of ['dark', 'light']) {
    await page.evaluate(light => document.body.classList.toggle('light', light), theme === 'light');
    const colours = await page.evaluate(() => [...document.querySelectorAll('#view-dashboard .dp-card-spark')]
      .filter(svg => svg.querySelector('.dp-card-spark-lead'))
      .map(svg => ({lead: getComputedStyle(svg.querySelector('.dp-card-spark-lead')).stopColor,
                    body: getComputedStyle(svg.querySelector('.dp-card-spark-body')).stopColor})));
    expect(colours.length).toBeGreaterThan(0);
    for (const {lead, body} of colours) {
      if (theme === 'dark') expect(luminance(lead) - luminance(body)).toBeGreaterThan(0.15);   // lit towards white on dark
      else expect(luminance(body) - luminance(lead)).toBeGreaterThan(0.03);                    // deepened on light
    }
  }
});

for (const [label, throughput, interval] of [['30 BPM', {bps: 0, peak: 42e6}, 2000], ['75 BPM', {bps: 42e6, peak: 42e6}, 800]]) {
  test(`at ${label} each trail fades to rest in strip distance just before the following heartbeat`, async ({page}) => {
    test.setTimeout(90000);
    await boot(page, {throughput});
    await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]')));
    await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="dashboard"]')));
    const seen = await births(page, 4 * interval + 3000);
    intervals(seen.times).forEach(gap => expect(Math.abs(gap - interval)).toBeLessThanOrEqual(QUANTUM));
    // Every trail spans the spacing to the next heartbeat less the complex and
    // its 16 px clearance; where two meet in one card, rest resumes 12 px
    // before the follower's leading edge (its own rest stop, 4 px ahead of it).
    const spacing = SPEED * interval, pairs = [];
    for (let t = 0; t < 3000; t += 100) {
      await page.clock.runFor(100);
      for (const card of await strip(page)) {
        const here = groups(card);
        for (const group of here) expect(group[5] - group[3]).toBeCloseTo(spacing - 48 - 16, 0);
        here.slice(1).forEach((group, i) => pairs.push(group[0] - here[i][5]));
        expectOneContinuousLine([card]);
      }
    }
    expect(pairs.length).toBeGreaterThan(0);
    pairs.forEach(gap => expect(gap).toBeCloseTo(12, 0));
  });
}

test('while the cadence changes and across row breaks every trail reaches rest in strip distance just before its follower', async ({page}) => {
  test.setTimeout(120000);
  const throughput = {bps: 0, peak: 42e6};
  await boot(page, {width: 560, height: 1400, throughput});
  await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]')));
  await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="dashboard"]')));
  await page.clock.runFor(5000);                          // heartbeats at 30 BPM on the strip
  throughput.bps = 42e6;                                  // the cadence now eases towards 75 BPM
  const trails = [];
  let pairs = 0, spanned = 0, broken = 0;
  for (let t = 0; t < 16000; t += 100) {
    await page.clock.runFor(100);
    const cards = await strip(page);
    const {rows, entries} = route(cards);
    expect(rows.length).toBe(3);
    // Each heartbeat in strip coordinates (px along the strip): its leading
    // edge and where its trail reaches rest, read from every card showing it.
    const beats = [];
    cards.forEach((card, i) => groups(card).forEach(group => {
      const lead = entries[i] + card.width - group[0] - 4, end = entries[i] + card.width - group[5];
      // A trail reaching back past the start of its row runs on into the
      // unseen row break behind it (a break is longer than any trail).
      const rowStart = Math.min(...rows.find(r => r.includes(i)).map(j => entries[j]));
      if (end < rowStart) broken++;
      const known = beats.find(beat => Math.abs(beat.lead - lead) < 0.5);
      if (!known) return beats.push({lead, end, cards: 1});
      expect(end).toBeCloseTo(known.end, 0);              // one trail across card boundaries
      known.cards++;
    }));
    spanned += beats.filter(beat => beat.cards > 1).length;
    beats.sort((a, b) => b.lead - a.lead);                // furthest along first
    beats.forEach((leader, i) => {
      // No later heartbeat ever runs into a trail ...
      beats.slice(i + 1).forEach(other => expect(other.lead).toBeLessThanOrEqual(leader.end - 16 + 0.5));
      // ... and the next one (heartbeats are at least 192 px apart) meets rest
      // 16 px behind where the trail ends, whatever spacing the cadence gave it.
      const follower = beats[i + 1];
      if (!follower || leader.end - 16 - follower.lead > 150) return;
      expect(leader.end - 16 - follower.lead).toBeCloseTo(0, 0);
      trails.push(leader.lead - 48 - leader.end);
      pairs++;
    });
  }
  expect(pairs).toBeGreaterThan(20);
  expect(spanned).toBeGreaterThan(0);                    // trails continuing from card to card
  expect(broken).toBeGreaterThan(0);                     // and back across a row break
  expect(Math.max(...trails)).toBeGreaterThan(300);      // long spacing as the change began ...
  expect(Math.min(...trails)).toBeLessThan(160);         // ... short near 75 BPM (128 px)
});

test('the cadence eases to the throughput between 30 and 75 BPM without bunching, resetting or changing speed', async ({page}) => {
  test.setTimeout(180000);
  const throughput = {bps: 0, peak: 42e6};
  await boot(page, {throughput});
  await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]')));
  await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="dashboard"]')));
  const rest = await births(page, 6500);
  intervals(rest.times).forEach(gap => expect(Math.abs(gap - 2000)).toBeLessThanOrEqual(QUANTUM));

  throughput.bps = 42e6;                                  // full sustained throughput: towards 75 BPM
  const rising = await births(page, 18000);
  const up = intervals([rest.times[rest.times.length - 1], ...rising.times]);
  up.slice(1).forEach((gap, i) => expect(gap).toBeLessThanOrEqual(up[i] + 2 * QUANTUM));   // never slower again
  up.forEach(gap => expect(gap).toBeGreaterThanOrEqual(800 - QUANTUM));                    // never bunched
  expect(Math.abs(up[up.length - 1] - 800)).toBeLessThanOrEqual(QUANTUM + 20);

  throughput.bps = 21e6;                                  // half of it: 52.5 BPM, one beat per 1143 ms
  const half = await births(page, 22000);
  expect(Math.abs(intervals(half.times).pop() - 60000 / 52.5)).toBeLessThanOrEqual(QUANTUM + 20);

  throughput.bps = 0;                                     // idle again: back towards 30 BPM
  const falling = await births(page, 22000);
  const down = intervals([half.times[half.times.length - 1], ...falling.times]);
  down.slice(1).forEach((gap, i) => expect(gap).toBeGreaterThanOrEqual(down[i] - 2 * QUANTUM));
  expect(down[down.length - 1]).toBeGreaterThan(1850);

  // Whatever the cadence did, travelling heartbeats kept one speed.
  const all = [...rest.leads, ...rising.leads, ...half.leads, ...falling.leads];
  all.slice(1).forEach((now, i) => {
    if (now.length !== 1 || all[i].length !== 1 || now[0] > all[i][0]) return;
    const moved = all[i][0] - now[0];
    expect(moved).toBeGreaterThanOrEqual(SPEED * (STEP - 16) - 0.01);
    expect(moved).toBeLessThanOrEqual(SPEED * (STEP + 16) + 0.01);
  });
});

test('without a recorded sustained peak the heartbeat rests at 30 BPM whatever the live speed', async ({page}) => {
  test.setTimeout(60000);
  await boot(page, {throughput: {bps: 500e6, peak: null}});
  await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]')));
  await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="dashboard"]')));
  const seen = await births(page, 9000);
  expect(seen.times.length).toBeGreaterThanOrEqual(3);
  intervals(seen.times).forEach(gap => expect(Math.abs(gap - 2000)).toBeLessThanOrEqual(QUANTUM));
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
