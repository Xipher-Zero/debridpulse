const { test, expect } = require('@playwright/test');

/* DP 1.0.13 decorative Dashboard ECG strip. The six KPI cards' lines form one
 * virtual strip: rows top to bottom, each row right to left, gaps and row
 * breaks travelled unseen at their real distance. One owner starts the
 * heartbeats, 7 BPM with no throughput up to 90 BPM as download throughput
 * reaches the installation's sustained peak; each travels the whole strip
 * right to left at 0.24 px/ms, so several can be on it at once. Each card's
 * single line is the signal: a flat baseline deformed into the ECG complex
 * where a heartbeat is, its brightness fading behind it back to the baseline
 * within 416 px and always before the following heartbeat. The test owns time
 * (page.clock) for timers and animation frames, and the throughput facts:
 * unless a case says otherwise they hold the cadence at 30 BPM (one heartbeat
 * every 2 s), 23/83 of the sustained peak. */

const SPEED = 0.24;          // px per ms
const INTERVAL = 2000;       // ms between heartbeats
const STEP = 50;
const R_OFFSET = 22;         // the R peak (y=2) sits 22 px behind the complex's leading edge
const TRAIL = 416;           // px: the longest trail

async function boot(page, {width = 1600, height = 900, reducedMotion = 'no-preference', throughput = {bps: 23, peak: 83}} = {}) {
  await page.setViewportSize({width, height});
  await page.emulateMedia({reducedMotion});
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  // The throughput facts: {bps, peak}, read at every poll.
  await page.route('**/api/execution/throughput', route => route.fulfill({status: 200, contentType: 'application/json',
    body: JSON.stringify({ok: true, download_bytes_per_second: throughput.bps, sustained_peak_bytes_per_second: throughput.peak,
      max_download_bytes_per_second: 0})}));
  await page.clock.install();
  const facts = page.waitForResponse('**/api/execution/throughput');
  await page.goto('/');
  await expect(page.locator('#s-total')).not.toHaveText('—');
  await facts;
  // From here only the test moves time.
  await page.clock.pauseAt(await page.evaluate(() => Date.now() + 50));
  // Enter the Dashboard afresh, its cadence starting from those facts.
  await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]')));
  await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="dashboard"]')));
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
  expect(busy.stops.map(s => s.opacity).slice(0, 7)).toEqual([0.4, 0.4, 1, 1, 0.9, 0.6, 0.4]);

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

/* Each heartbeat on the strip in strip coordinates (px along it): its leading
 * edge and where its trail reaches rest, read from every card showing it (one
 * trail across card boundaries); `rows` lists the rows it was seen on, and
 * `broken` whether a trail reaches back past the start of its row into the
 * unseen row break behind it (a break is longer than any trail). */
function stripBeats(cards) {
  const {rows, entries} = route(cards);
  const beats = [];
  cards.forEach((card, i) => groups(card).forEach(group => {
    const lead = entries[i] + card.width - group[0] - 4, end = entries[i] + card.width - group[5];
    const row = rows.findIndex(r => r.includes(i));
    const broken = end < Math.min(...rows[row].map(j => entries[j]));
    const known = beats.find(beat => Math.abs(beat.lead - lead) < 0.5);
    if (!known) return beats.push({lead, end, cards: 1, broken});
    expect(end).toBeCloseTo(known.end, 0);
    known.cards++;
    known.broken ||= broken;
  }));
  return beats.sort((a, b) => b.lead - a.lead);           // furthest along first
}

/* No heartbeat ever runs into a trail; a trail is never longer than 416 px,
 * and shorter than that its next heartbeat (heartbeats are at least 160 px
 * apart) meets rest 16 px behind where it ends. Returns the spacing-limited
 * trails checked against a seen follower, and how many were at the cap. */
function expectClearTrails(beats) {
  const checked = [];
  let capped = 0;
  beats.forEach((leader, i) => {
    beats.slice(i + 1).forEach(other => expect(other.lead).toBeLessThanOrEqual(leader.end - 16 + 0.5));
    const trail = leader.lead - 48 - leader.end;
    expect(trail).toBeLessThanOrEqual(TRAIL + 0.5);
    if (trail > TRAIL - 0.5) return void capped++;
    const follower = beats[i + 1];
    if (!follower || leader.end - 16 - follower.lead > 150) return;
    expect(leader.end - 16 - follower.lead).toBeCloseTo(0, 0);
    checked.push(trail);
  });
  return {checked, capped};
}

/* sRGB bytes -> OKLCH lightness, chroma and hue (degrees). */
function oklch([r, g, b]) {
  const lin = v => (v /= 255) <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  [r, g, b] = [r, g, b].map(lin);
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  const a = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s, bb = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s;
  return {l: 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s, c: Math.hypot(a, bb), h: (Math.atan2(bb, a) * 180 / Math.PI + 360) % 360};
}

test('the heartbeat is its own card\'s accent at full strength in both themes, never whitened, and the quiet Errors card does not dim it', async ({page}) => {
  test.setTimeout(60000);
  // No errors: the Errors card is in its quiet state.
  await page.route('**/api/stats', async route => {
    const response = await route.fetch();
    route.fulfill({response, json: {...await response.json(), error_count: 0}});
  });
  await boot(page);
  const cards = await strip(page);
  await page.clock.runFor(INTERVAL + Math.max(...route(cards).entries) / SPEED + cards[0].width / SPEED);
  for (const theme of ['dark', 'light']) {
    await page.evaluate(light => document.body.classList.toggle('light', light), theme === 'light');
    // Each card's accent and lead colour as rendered sRGB bytes.
    const colours = await page.evaluate(() => {
      const ctx = Object.assign(document.createElement('canvas'), {width: 1, height: 1}).getContext('2d', {willReadFrequently: true});
      const bytes = colour => { ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = colour; ctx.fillRect(0, 0, 1, 1); return [...ctx.getImageData(0, 0, 1, 1).data].slice(0, 3); };
      return [...document.querySelectorAll('#view-dashboard .dash-hero-stat')].map(card => {
        const svg = card.querySelector('.dp-card-spark');
        return {card: card.dataset.c, accent: bytes(getComputedStyle(svg).color),
          lead: bytes(getComputedStyle(svg.querySelector('.dp-card-spark-lead')).stopColor)};
      });
    });
    expect(colours.length).toBe(6);
    for (const {card, accent, lead} of colours) {
      const a = oklch(accent), l = oklch(lead), label = `${theme} ${card}`;
      const hue = Math.abs(((l.h - a.h) % 360 + 540) % 360 - 180);
      expect(hue, label).toBeLessThanOrEqual(8);                              // the card's own hue
      expect(l.c, label).toBeGreaterThanOrEqual(a.c - 0.01);                  // at least the accent's chroma: not washed out
      expect(Math.min(...lead) / Math.max(...lead), label).toBeLessThanOrEqual(Math.min(...accent) / Math.max(...accent) + 0.05);   // no white in it
      if (theme === 'dark') expect(l.l, label).toBeGreaterThanOrEqual(a.l - 0.005);   // at least as light, not mixed toward white
      else {
        expect(l.l, label).toBeLessThanOrEqual(a.l);                          // deepened for contrast ...
        expect(l.l, label).toBeGreaterThanOrEqual(a.l - 0.1);                 // ... not toward black
      }
    }
  }
  // The quiet Errors card recedes -- content, edges, resting baseline -- but
  // nothing between its heartbeat and the page is dimmed.
  const quiet = await page.evaluate(() => {
    const card = document.getElementById('dash-error-card');
    const opacities = [];
    for (let e = card.querySelector('.dp-card-spark-line'); e; e = e.parentElement) opacities.push(Number(getComputedStyle(e).opacity));
    return {quiet: card.classList.contains('dash-hero-stat--quiet'), opacities,
      body: Number(getComputedStyle(card.querySelector('.dhs-body')).opacity),
      rest: Number(getComputedStyle(card.querySelector('.dp-card-spark-rest')).stopOpacity),
      lead: Number(getComputedStyle(card.querySelector('.dp-card-spark-lead')).stopOpacity)};
  });
  expect(quiet).toEqual({quiet: true, opacities: quiet.opacities.map(() => 1), body: 0.6, rest: 0.24, lead: 1});
});

const IDLE = 60000 / 7, FULL = 60000 / 90;   // ms between heartbeats at no throughput and at the sustained peak

for (const [label, throughput, interval] of [['7 BPM', {bps: 0, peak: 42e6}, IDLE], ['90 BPM', {bps: 42e6, peak: 42e6}, FULL]]) {
  test(`at ${label} each trail fades to rest in strip distance, within 416 px and just before the following heartbeat`, async ({page}) => {
    test.setTimeout(120000);
    await boot(page, {throughput});
    await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]')));
    await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="dashboard"]')));
    const seen = await births(page, 3 * interval + 3000);
    expect(seen.times.length).toBeGreaterThanOrEqual(3);
    intervals(seen.times).forEach(gap => expect(Math.abs(gap - interval)).toBeLessThanOrEqual(QUANTUM));
    // Every trail spans the spacing to the next heartbeat less the complex and
    // its 16 px clearance, up to 416 px; where two meet in one card, rest
    // resumes 12 px before the follower's leading edge (its own rest stop, 4 px
    // ahead of it).
    const spacing = SPEED * interval, pairs = [];
    for (let t = 0; t < 3000; t += 100) {
      await page.clock.runFor(100);
      let lit = 0;
      for (const card of await strip(page)) {
        const here = groups(card);
        for (const group of here) {
          expect(group[5] - group[3]).toBeCloseTo(Math.min(TRAIL, spacing - 48 - 16), 0);
          lit += Math.max(0, Math.min(card.width, group[5]) - Math.max(0, group[0]));
        }
        here.slice(1).forEach((group, i) => pairs.push(group[0] - here[i][5]));
        expectOneContinuousLine([card]);
      }
      // At idle one heartbeat lights at most its complex and trail; the rest of the strip rests.
      if (interval === IDLE) expect(lit).toBeLessThanOrEqual(4 + 48 + TRAIL + 0.5);
    }
    if (interval === FULL) {
      expect(pairs.length).toBeGreaterThan(0);
      pairs.forEach(gap => expect(gap).toBeCloseTo(12, 0));
    }
  });
}

test('while the cadence changes and across row breaks every trail reaches rest in strip distance just before its follower', async ({page}) => {
  test.setTimeout(120000);
  const throughput = {bps: 23, peak: 83};
  await boot(page, {width: 560, height: 1400, throughput});
  await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]')));
  await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="dashboard"]')));
  await page.clock.runFor(5000);                          // heartbeats at 30 BPM on the strip
  throughput.bps = 83;                                    // the cadence now eases towards 90 BPM
  const trails = [];
  let capped = 0, spanned = 0, broken = 0;
  for (let t = 0; t < 16000; t += 100) {
    await page.clock.runFor(100);
    const cards = await strip(page);
    expect(route(cards).rows.length).toBe(3);
    const beats = stripBeats(cards);
    spanned += beats.filter(beat => beat.cards > 1).length;
    broken += beats.filter(beat => beat.broken).length;
    const clear = expectClearTrails(beats);
    trails.push(...clear.checked);
    capped += clear.capped;
  }
  const pairs = trails.length;
  expect(pairs).toBeGreaterThan(20);
  expect(spanned).toBeGreaterThan(0);                    // trails continuing from card to card
  expect(broken).toBeGreaterThan(0);                     // and back across a row break
  expect(capped).toBeGreaterThan(0);                     // 416 px at 30 BPM as the change began ...
  expect(new Set(trails.map(Math.round)).size).toBeGreaterThan(3);   // ... then shortening ...
  expect(Math.min(...trails)).toBeLessThan(110);         // ... to 96 px near 90 BPM
});

test('the cadence eases to the throughput between 7 and 90 BPM without bunching, resetting or changing speed', async ({page}) => {
  test.setTimeout(300000);
  const throughput = {bps: 0, peak: 42e6};
  await boot(page, {throughput});
  await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]')));
  await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="dashboard"]')));
  const rest = await births(page, 2 * IDLE + 1500);                // idle: one heartbeat per 8571 ms
  expect(rest.times.length).toBeGreaterThanOrEqual(2);
  intervals(rest.times).forEach(gap => expect(Math.abs(gap - IDLE)).toBeLessThanOrEqual(QUANTUM));

  throughput.bps = 42e6;                                  // full sustained throughput: towards 90 BPM
  const rising = await births(page, 22000);
  const up = intervals([rest.times[rest.times.length - 1], ...rising.times]);
  up.slice(1).forEach((gap, i) => expect(gap).toBeLessThanOrEqual(up[i] + 2 * QUANTUM));   // never slower again
  up.forEach(gap => expect(gap).toBeGreaterThanOrEqual(FULL - QUANTUM));                   // never bunched
  expect(Math.abs(up[up.length - 1] - FULL)).toBeLessThanOrEqual(QUANTUM + 20);

  throughput.bps = 21e6;                                  // half of it: 48.5 BPM, one beat per 1237 ms
  const half = await births(page, 22000);
  expect(Math.abs(intervals(half.times).pop() - 60000 / 48.5)).toBeLessThanOrEqual(QUANTUM + 20);

  throughput.bps = 0;                                     // idle again: back towards 7 BPM
  const falling = await births(page, 22000);
  const down = intervals([half.times[half.times.length - 1], ...falling.times]);
  down.slice(1).forEach((gap, i) => expect(gap).toBeGreaterThanOrEqual(down[i] - 2 * QUANTUM));
  expect(down[down.length - 1]).toBeGreaterThan(6000);

  // Whatever the cadence did, travelling heartbeats kept one speed.
  const all = [...rest.leads, ...rising.leads, ...half.leads, ...falling.leads];
  all.slice(1).forEach((now, i) => {
    if (now.length !== 1 || all[i].length !== 1 || now[0] > all[i][0]) return;
    const moved = all[i][0] - now[0];
    expect(moved).toBeGreaterThanOrEqual(SPEED * (STEP - 16) - 0.01);
    expect(moved).toBeLessThanOrEqual(SPEED * (STEP + 16) + 0.01);
  });
});

test('throughput arriving just after an idle heartbeat brings the next one forward at once, and the cadence rises smoothly', async ({page}) => {
  test.setTimeout(120000);
  const throughput = {bps: 0, peak: 42e6};
  await boot(page, {throughput});
  const seen = {times: [], leads: []};
  while (!seen.times.length) await births(page, STEP, seen);     // an idle heartbeat has just started
  throughput.bps = 42e6;                                        // full sustained throughput from now
  await births(page, 20000, seen);
  const gaps = intervals(seen.times);
  console.log('idle -> peak intervals (ms):', gaps.join(', '));
  expect(gaps[0]).toBeLessThan(3500);                           // not the 8571 ms idle interval
  gaps.slice(1).forEach((gap, i) => {
    expect(gap).toBeLessThanOrEqual(gaps[i] + 2 * QUANTUM);     // never slower again
    expect(gap).toBeGreaterThanOrEqual(gaps[i] * 0.45);         // no abrupt jump
  });
  gaps.forEach(gap => expect(gap).toBeGreaterThanOrEqual(FULL - QUANTUM));   // never bunched
  expect(Math.abs(gaps[gaps.length - 1] - FULL)).toBeLessThanOrEqual(QUANTUM + 20);
});

test('throughput falling to nothing slows the cadence smoothly to idle, never postponing a heartbeat past the idle interval', async ({page}) => {
  test.setTimeout(120000);
  const throughput = {bps: 42e6, peak: 42e6};
  await boot(page, {throughput});
  const seen = await births(page, 5000);
  throughput.bps = 0;
  await births(page, 30000, seen);
  const gaps = intervals(seen.times);
  console.log('peak -> idle intervals (ms):', gaps.join(', '));
  gaps.slice(1).forEach((gap, i) => {
    expect(gap).toBeGreaterThanOrEqual(gaps[i] - 2 * QUANTUM);  // never faster again
    expect(gap).toBeLessThanOrEqual(gaps[i] * 2 + QUANTUM);     // no abrupt jump
  });
  gaps.forEach(gap => expect(gap).toBeLessThanOrEqual(IDLE + QUANTUM));      // never postponed
  expect(gaps[gaps.length - 1]).toBeGreaterThan(6000);
});

test('repeatedly changing throughput re-times the pending heartbeat without bursts, and every trail still clears its follower', async ({page}) => {
  test.setTimeout(180000);
  const throughput = {bps: 0, peak: 42e6};
  await boot(page, {throughput});
  const levels = [42e6, 0, 21e6, 42e6, 5e6, 0, 30e6, 42e6, 0, 42e6];
  const born = [];
  let checked = 0;
  for (let t = 0; t < 26000; t += 100) {
    throughput.bps = levels[Math.floor(t / 1300) % levels.length];
    await page.clock.runFor(100);
    const now = await page.evaluate(() => Date.now());
    const beats = stripBeats(await strip(page));
    checked += expectClearTrails(beats).checked.length;
    // Each heartbeat's birth, from where it is on the strip (a frame is 16 ms).
    for (const beat of beats) {
      const at = now - beat.lead / SPEED;
      if (!born.some(b => Math.abs(b - at) < 40)) born.push(at);
    }
  }
  born.sort((a, b) => a - b);
  const gaps = intervals(born);
  console.log('changing throughput intervals (ms):', gaps.map(Math.round).join(', '));
  expect(gaps.length).toBeGreaterThan(10);
  gaps.forEach(gap => {
    expect(gap).toBeGreaterThanOrEqual(FULL - 40);              // no burst or doubled heartbeat
    expect(gap).toBeLessThanOrEqual(IDLE + 40);                 // none postponed
  });
  expect(checked).toBeGreaterThan(20);
});

test('without a recorded sustained peak the heartbeat rests at 7 BPM whatever the live speed', async ({page}) => {
  test.setTimeout(120000);
  await boot(page, {throughput: {bps: 500e6, peak: null}});
  await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]')));
  await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="dashboard"]')));
  const seen = await births(page, 3 * IDLE + 500);
  expect(seen.times.length).toBeGreaterThanOrEqual(3);
  intervals(seen.times).forEach(gap => expect(Math.abs(gap - IDLE)).toBeLessThanOrEqual(QUANTUM));
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
