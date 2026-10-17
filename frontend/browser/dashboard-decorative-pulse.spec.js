const { test, expect } = require('@playwright/test');
const { pauseClockBeforeBoot } = require('./page-clock');

/* DP 1.0.13 decorative Dashboard ECG strip. The six KPI cards' lines form one
 * virtual strip: rows top to bottom, each row right to left, gaps and row
 * breaks travelled unseen at their real distance. One owner starts the
 * heartbeats, 3.5 BPM with no throughput up to 90 BPM as download throughput
 * reaches the installation's sustained peak; each travels the whole strip
 * right to left at 0.24 px/ms, so several can be on it at once. Each card's
 * single line is the signal: a flat baseline deformed into the ECG complex
 * where a heartbeat is, its brightness fading behind it back to the baseline
 * within 416 px and always before the following heartbeat. The test owns time
 * (page.clock) for timers and animation frames, and the throughput facts:
 * unless a case says otherwise they hold the cadence at 30 BPM (one heartbeat
 * every 2 s): 3.5 + 86.5 x 26.5/86.5 of the sustained peak. */

const SPEED = 0.24;          // px per ms
const INTERVAL = 2000;       // ms between heartbeats
const STEP = 50;
const COMPLEX_POINTS = 15;   // points in one complex, its R peak the 7th (the only point above y=6)
const TRAIL = 416;           // px: the longest trail

async function boot(page, {width = 1600, height = 900, reducedMotion = 'no-preference', throughput = {bps: 26.5, peak: 86.5}, pause, events} = {}) {
  await page.setViewportSize({width, height});
  // The strip's clock runs for the life of the page once motion is allowed on
  // the shown Dashboard; it is held still until the test owns time.
  await page.emulateMedia({reducedMotion: 'reduce'});
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  // Deterministic heartbeat shapes: a seeded Math.random.
  await page.addInitScript(() => {
    let a = 20261012;
    Math.random = () => {
      a = (a + 0x6D2B79F5) >>> 0;
      let t = Math.imul(a ^ (a >>> 15), a | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  });
  // The authoritative global pause state, when the case owns it: {paused,
  // transfersPaused} read at every GET /stats.
  if (pause) await page.route('**/api/stats', async route => {
    const response = await route.fetch();
    const stats = await response.json();
    route.fulfill({response, json: {...stats, paused: pause.paused,
      by_status: {...stats.by_status, paused: pause.transfersPaused ?? stats.by_status?.paused ?? 0}}});
  });
  // The application event stream, when the case owns it: each events.emit()
  // delivers one stats_changed, as a change made in another session would.
  if (events) {
    let release = () => {};
    events.emit = () => release();
    await page.route('**/api/events/stream', async route => {
      await new Promise(resolve => { release = resolve; });
      route.fulfill({status: 200, contentType: 'text/event-stream',
        body: 'event: connected\ndata: {}\n\nevent: stats_changed\ndata: {}\n\n'});
    });
  }
  // The throughput facts: {bps, peak}, read at every poll ({fail: true}: the poll fails, as when the server is unreachable).
  await page.route('**/api/execution/throughput', route => throughput.fail
    ? route.fulfill({status: 503, contentType: 'application/json', body: '{"detail":"unavailable"}'})
    : route.fulfill({status: 200, contentType: 'application/json',
    body: JSON.stringify({ok: true, download_bytes_per_second: throughput.bps, sustained_peak_bytes_per_second: throughput.peak,
      max_download_bytes_per_second: 0})}));
  // Only the test moves time, from before the application exists (page-clock.js).
  await pauseClockBeforeBoot(page);
  const facts = page.waitForResponse('**/api/execution/throughput');
  await page.goto('/');
  await expect(page.locator('#s-total')).not.toHaveText('—');
  await facts;
  // The strip starts afresh now, its cadence from those facts.
  await page.emulateMedia({reducedMotion});
  await expect.poll(() => page.evaluate(() => _dashboardHeartbeat.running)).toBe(reducedMotion !== 'reduce');
}

/* Each card in DOM order: its geometry, line points, path count and gradient stops. */
const strip = page => page.evaluate(() => [...document.querySelectorAll('#view-dashboard .dp-card-spark')].map(svg => {
  const d = svg.querySelector('.dp-card-spark-line').getAttribute('d');
  const box = svg.getBoundingClientRect();
  const gradient = svg.querySelector('linearGradient[id^="dp-card-spark-signal-"]');
  return {width: box.width, left: box.left, right: box.right, middle: (box.top + box.bottom) / 2,
    from: Number(gradient.getAttribute('x1')), to: Number(gradient.getAttribute('x2')),
    points: [...d.matchAll(/(-?[\d.]+) (-?[\d.]+)/g)].map(m => [Number(m[1]), Number(m[2])]),
    paths: svg.querySelectorAll('path[stroke]').length,
    stops: [...gradient.querySelectorAll('stop')].map(stop => ({role: stop.getAttribute('class'), offset: Number(stop.getAttribute('offset')),
      opacity: Number(getComputedStyle(stop).stopOpacity)}))};
}));
/* Each complex drawn on a card's line: its leading edge and its points
 * relative to it (x px, y units). */
const complexes = card => card.points.flatMap(([, y], i) => (y < 6 ? [i - 6] : []))
  .map(start => card.points.slice(start, start + COMPLEX_POINTS))
  .map(points => ({lead: points[0][0], points: points.map(([x, y]) => [x - points[0][0], y])}));
const leads = card => complexes(card).map(complex => complex.lead);
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

/* Sample the strip every STEP ms for `ms`, from a fresh start (boot). */
async function sampleFromStart(page, ms) {
  const samples = [];
  for (let t = 0; t < ms; t += STEP) {
    await page.clock.runFor(STEP);
    samples.push({t: t + STEP, cards: await strip(page)});
  }
  return samples;
}

function expectOneContinuousLine(cards) {
  for (const card of cards) {
    expect(card.paths).toBe(1);                                         // one stroked line: no second over the baseline
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
  const samples = await sampleFromStart(page, INTERVAL + Math.max(...entries) / SPEED + 1000);
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
    // (A complex is drawn until its own width has left the card.)
    for (const {lead: x, points} of complexes(card).filter(({lead, points}) => lead - SPEED * STEP + points[COMPLEX_POINTS - 1][0] > 0)) {
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
    const width = group[3] - group[0] - 4;                       // the heartbeat's own complex
    const row = rows.findIndex(r => r.includes(i));
    const broken = end < Math.min(...rows[row].map(j => entries[j]));
    const known = beats.find(beat => Math.abs(beat.lead - lead) < 0.5);
    const envelope = group.map(x => x - group[0]);                // its stops, from its own rest stop
    if (!known) return beats.push({lead, end, width, envelope, cards: 1, broken});
    expect(end).toBeCloseTo(known.end, 0);
    known.cards++;
    known.broken ||= broken;
  }));
  return beats.sort((a, b) => b.lead - a.lead);           // furthest along first
}

/* No heartbeat ever runs into a trail; a trail is never longer than 416 px,
 * and shorter than that its next heartbeat (heartbeats are at least 160 px
 * apart) meets rest 16 px behind where it ends, whatever the leader's width. Returns the spacing-limited
 * trails checked against a seen follower, and how many were at the cap. */
function expectClearTrails(beats, {held = false} = {}) {
  const checked = [];
  let capped = 0;
  beats.forEach((leader, i) => {
    beats.slice(i + 1).forEach(other => expect(other.lead).toBeLessThanOrEqual(leader.end - 16 + 0.5));
    const trail = leader.lead - leader.width - leader.end;
    expect(trail).toBeLessThanOrEqual(TRAIL + 0.5);
    if (trail > TRAIL - 0.5) return void capped++;
    const follower = beats[i + 1];
    if (!follower || leader.end - 16 - follower.lead > 150 || held) return;   // (a held trail may end sooner)
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

const IDLE = 60000 / 3.5, FULL = 60000 / 90; // ms between heartbeats at no throughput (17,143) and at the sustained peak

for (const [label, throughput, interval] of [['3.5 BPM', {bps: 0, peak: 42e6}, IDLE], ['90 BPM', {bps: 42e6, peak: 42e6}, FULL]]) {
  test(`at ${label} each trail fades to rest in strip distance, within 416 px and just before the following heartbeat`, async ({page}) => {
    test.setTimeout(120000);
    await boot(page, {throughput});
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
          expect(group[5] - group[3]).toBeCloseTo(Math.min(TRAIL, spacing - (group[3] - group[0] - 4) - 16), 0);
          lit += Math.max(0, Math.min(card.width, group[5]) - Math.max(0, group[0]));
        }
        here.slice(1).forEach((group, i) => pairs.push(group[0] - here[i][5]));
        expectOneContinuousLine([card]);
      }
      // At idle one heartbeat lights at most its complex and trail; the rest of the strip rests.
      if (interval === IDLE) expect(lit).toBeLessThanOrEqual(4 + 48 * 1.32 + TRAIL + 0.5);
    }
    if (interval === FULL) {
      expect(pairs.length).toBeGreaterThan(0);
      pairs.forEach(gap => expect(gap).toBeCloseTo(12, 0));
    }
  });
}

test('while the cadence changes and across row breaks every trail reaches rest in strip distance just before its follower', async ({page}) => {
  test.setTimeout(120000);
  const throughput = {bps: 26.5, peak: 86.5};
  await boot(page, {width: 560, height: 1400, throughput});
  await page.clock.runFor(5000);                          // heartbeats at 30 BPM on the strip
  throughput.bps = 86.5;                                  // the cadence now eases towards 90 BPM
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

test('the cadence eases to the throughput between 3.5 and 90 BPM without bunching, resetting or changing speed', async ({page}) => {
  test.setTimeout(300000);
  const throughput = {bps: 0, peak: 42e6};
  await boot(page, {throughput});
  const rest = await births(page, 2 * IDLE + 1500);                // idle: one heartbeat per 17,143 ms
  expect(rest.times.length).toBeGreaterThanOrEqual(2);
  intervals(rest.times).forEach(gap => expect(Math.abs(gap - IDLE)).toBeLessThanOrEqual(QUANTUM));

  throughput.bps = 42e6;                                  // full sustained throughput: towards 90 BPM
  const rising = await births(page, 22000);
  const up = intervals([rest.times[rest.times.length - 1], ...rising.times]);
  up.slice(1).forEach((gap, i) => expect(gap).toBeLessThanOrEqual(up[i] + 2 * QUANTUM));   // never slower again
  up.forEach(gap => expect(gap).toBeGreaterThanOrEqual(FULL - QUANTUM));                   // never bunched
  expect(Math.abs(up[up.length - 1] - FULL)).toBeLessThanOrEqual(QUANTUM + 20);

  throughput.bps = 21e6;                                  // half of it: 3.5 + 86.5 / 2 = 46.75 BPM, one beat per 1283 ms
  const half = await births(page, 22000);
  expect(Math.abs(intervals(half.times).pop() - 60000 / (3.5 + 86.5 / 2))).toBeLessThanOrEqual(QUANTUM + 20);

  throughput.bps = 0;                                     // idle again: back towards 3.5 BPM
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

/* The documented cadence law (app.js): the cadence eases toward its target with
 * a 4 s time constant from the first reading of the new throughput (at most one
 * 500 ms poll after it changed), and a heartbeat comes when its phase completes
 * -- so each interval is the inverse of the mean cadence over it, which lies
 * between the cadence at its two ends. A pending heartbeat is re-timed only at
 * a reading, so it may land up to one poll off that. Every interval must lie in
 * that band: no restart, jump, double or postponed heartbeat. */
const EASE_MS = 4000, POLL_MS = 500;
function expectEasedIntervals(times, changed, from, to) {
  const cadence = (t, start) => (t <= start ? from : to + (from - to) * Math.exp(-(t - start) / EASE_MS));
  times.slice(1).forEach((end, i) => {
    const begin = times[i], gap = end - begin;
    const ends = [changed, changed + POLL_MS].flatMap(start => [cadence(begin, start), cadence(end, start)]);
    expect(gap, `interval ${i}: ${Math.round(gap)} ms`).toBeGreaterThanOrEqual(60000 / Math.max(...ends) - POLL_MS - QUANTUM);
    expect(gap, `interval ${i}: ${Math.round(gap)} ms`).toBeLessThanOrEqual(60000 / Math.min(...ends) + POLL_MS + QUANTUM);
  });
}

test('throughput arriving just after an idle heartbeat brings the next one forward at once, and the cadence rises smoothly', async ({page}) => {
  test.setTimeout(120000);
  const throughput = {bps: 0, peak: 42e6};
  await boot(page, {throughput});
  const seen = {times: [], leads: []};
  while (!seen.times.length) await births(page, STEP, seen);     // an idle heartbeat has just started
  throughput.bps = 42e6;                                        // full sustained throughput from now
  const changed = await page.evaluate(() => Date.now());
  await births(page, 20000, seen);
  const gaps = intervals(seen.times);
  console.log('idle -> peak intervals (ms):', gaps.join(', '));
  expect(gaps[0]).toBeLessThan(3500);                           // not the 17,143 ms idle interval
  gaps.slice(1).forEach((gap, i) => expect(gap).toBeLessThanOrEqual(gaps[i] + 2 * QUANTUM));   // never slower again
  expectEasedIntervals(seen.times, changed, 3.5, 90);           // no abrupt jump: each interval as the cadence law allows
  gaps.forEach(gap => expect(gap).toBeGreaterThanOrEqual(FULL - QUANTUM));   // never bunched
  expect(Math.abs(gaps[gaps.length - 1] - FULL)).toBeLessThanOrEqual(QUANTUM + 20);
});

test('throughput falling to nothing slows the cadence smoothly to idle, never postponing a heartbeat past the idle interval', async ({page}) => {
  test.setTimeout(120000);
  const throughput = {bps: 42e6, peak: 42e6};
  await boot(page, {throughput});
  const seen = await births(page, 5000);
  throughput.bps = 0;
  const changed = await page.evaluate(() => Date.now());
  await births(page, 30000, seen);
  const gaps = intervals(seen.times);
  console.log('peak -> idle intervals (ms):', gaps.join(', '));
  gaps.slice(1).forEach((gap, i) => expect(gap).toBeGreaterThanOrEqual(gaps[i] - 2 * QUANTUM));   // never faster again
  expectEasedIntervals(seen.times, changed, 90, 3.5);           // no abrupt jump: each interval as the cadence law allows
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

test('without a recorded sustained peak the heartbeat rests at 3.5 BPM whatever the live speed', async ({page}) => {
  test.setTimeout(120000);
  await boot(page, {throughput: {bps: 500e6, peak: null}});
  const seen = await births(page, 3 * IDLE + 500);
  expect(seen.times.length).toBeGreaterThanOrEqual(3);
  intervals(seen.times).forEach(gap => expect(Math.abs(gap - IDLE)).toBeLessThanOrEqual(QUANTUM));
});

/* Every complex on the strip now, with when its heartbeat was born (from
 * where it is: the strip distance it has travelled at 0.24 px/ms). */
async function observe(page) {
  const now = await page.evaluate(() => Date.now());
  const cards = await strip(page);
  const {entries} = route(cards);
  const seen = cards.flatMap((card, i) => complexes(card).map(complex =>
    ({...complex, card: i, born: now - (entries[i] + card.width - complex.lead) / SPEED})));
  return {now, cards, seen};
}

/* The heartbeats in a series of observations, keyed by birth (one frame is 16 ms). */
function heartbeats(observations) {
  const found = [];
  for (const {seen} of observations) for (const complex of seen) {
    const known = found.find(beat => Math.abs(beat.born - complex.born) <= 20);
    if (known) known.sightings.push(complex); else found.push({born: complex.born, sightings: [complex]});
  }
  return found.sort((a, b) => a.born - b.born);
}

test('each heartbeat has its own bounded complex, kept unchanged across every card and frame', async ({page}) => {
  test.setTimeout(120000);
  await boot(page, {throughput: {bps: 42e6, peak: 42e6}});
  const base = await page.evaluate(() => DASHBOARD_HEARTBEAT_COMPLEX);
  const observations = [];
  for (let t = 0; t < 9000; t += 50) {
    await page.clock.runFor(50);
    observations.push(await observe(page));
  }
  const beats = heartbeats(observations).filter(beat => new Set(beat.sightings.map(s => s.card)).size >= 3);
  expect(beats.length).toBeGreaterThan(5);
  const shapes = new Set(), widths = [], peaks = [];
  for (const beat of beats) {
    const [first] = beat.sightings;
    // Unchanged in every card and every frame it was seen in.
    for (const sighting of beat.sightings) {
      sighting.points.forEach(([x, y], i) => {
        expect(x).toBeCloseTo(first.points[i][0], 1);
        expect(y).toBeCloseTo(first.points[i][1], 1);
      });
    }
    shapes.add(first.points.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' '));
    // In order, on the baseline at both ends, each deflection the same way
    // as the established complex and within its bound.
    const xs = first.points.map(([x]) => x);
    expect(xs).toEqual([...xs].sort((a, b) => a - b));
    const width = xs[xs.length - 1];
    widths.push(width);
    expect(width).toBeGreaterThanOrEqual(48 * 0.7267 - 0.05);
    expect(width).toBeLessThanOrEqual(48 * 1.3151 + 0.05);
    // The P (0-16), QRS (16-29) and T-U (29-48) stretches each scale evenly within their bounds.
    for (const [from, to, low, high] of [[0, 16, 0.7216, 1.3216], [16, 29, 0.792, 1.232], [29, 48, 0.6864, 1.3664]]) {
      const inside = base.map(([bx], i) => i).filter(i => base[i][0] >= from && base[i][0] <= to);
      const a = inside[0], b = inside[inside.length - 1];
      const scale = (first.points[b][0] - first.points[a][0]) / (base[b][0] - base[a][0]);
      expect(scale).toBeGreaterThanOrEqual(low - 0.01);
      expect(scale).toBeLessThanOrEqual(high + 0.01);
      inside.forEach(i => expect(first.points[i][0]).toBeCloseTo(first.points[a][0] + (base[i][0] - base[a][0]) * scale, 1));
    }
    first.points.forEach(([x, y], i) => {
      const [, by] = base[i];
      if (by === 16) return expect(y).toBeCloseTo(16, 2);
      const factor = (y - 16) / (by - 16);                           // > 0: same polarity
      const [low, high] = by === 2 ? [0.7225, 1.3225] : by === 23 ? [0.697, 1.357] : [0.663, 1.403];
      expect(factor).toBeGreaterThanOrEqual(low - 0.01);
      expect(factor).toBeLessThanOrEqual(high + 0.01);
    });
    peaks.push(16 - first.points[6][1]);
    expect(first.points[6][1]).toBeLessThan(Math.min(...first.points.filter((_, i) => i !== 6).map(([, y]) => y)));   // R always the tallest
    expect(Math.max(...first.points.map(([, y]) => y))).toBeLessThanOrEqual(25.5 + 0.01);   // clear of the card's floor
  }
  expect(shapes.size).toBe(beats.length);                          // every heartbeat its own
  // ... and visibly so: lengths and R heights spread well apart.
  expect(Math.max(...widths) - Math.min(...widths)).toBeGreaterThan(8);
  expect(Math.max(...peaks) - Math.min(...peaks)).toBeGreaterThan(2);
});

/* How far the strip's signal stands off its baseline now (the largest
 * deflection of any card's line, in viewBox units; 0 is flat), and whether the
 * red paused presentation is on. */
const signal = page => page.evaluate(() => ({now: performance.now(),
  height: Math.max(0, ...[...document.querySelectorAll('#view-dashboard .dp-card-spark-line')]
    .flatMap(line => [...line.getAttribute('d').matchAll(/(-?[\d.]+) (-?[\d.]+)/g)].map(m => Math.abs(Number(m[2]) - 16)))),
  flatline: document.getElementById('dash-hero').classList.contains('dash-hero--flatline'),
  born: _dashboardHeartbeat.beats.map(beat => beat.born)}));
const SETTLE = 700;

test('Global Pause All from another session settles the signal flat where it stands in about 700 ms, then all six lines breathe one red; resuming starts a heartbeat at once', async ({page}) => {
  test.setTimeout(180000);
  const pause = {paused: false}, events = {};
  await boot(page, {pause, events});
  await page.clock.runFor(INTERVAL + 2500);                          // heartbeats on the strip, mid-route
  const accents = await page.evaluate(() => [...document.querySelectorAll('#view-dashboard .dp-card-spark')].map(svg => getComputedStyle(svg).color));
  const edges = await page.evaluate(() => [...document.querySelectorAll('#view-dashboard .dash-hero-stat')].map(card => getComputedStyle(card, '::before').backgroundColor));
  const flatline = () => page.evaluate(() => document.getElementById('dash-hero').classList.contains('dash-hero--flatline'));

  pause.paused = true;
  events.emit();                                                    // stats_changed: loadStats reads the pause
  await expect.poll(() => page.evaluate(() => processingPaused)).toBe(true);
  const confirmed = await signal(page);
  const inFlight = heartbeats([await observe(page)]);
  expect(inFlight.length).toBeGreaterThan(0);                       // complexes stand on the strip, mid-route
  expect(confirmed.height).toBeGreaterThan(10);
  expect(confirmed.flatline).toBe(false);

  // Frame by frame: it settles from the first frame, never rising, still in the
  // cards' colours, travelling on; flat at about 700 ms, and only then red.
  const frames = [];
  let landed = null;
  for (let t = 0; t < 2000 && landed === null; t += 16) {
    await page.clock.runFor(16);
    const frame = await signal(page);
    frames.push({...frame, t: frame.now - confirmed.now});
    if (frame.flatline) landed = frames.at(-1);
    else expect(await page.evaluate(() => [...document.querySelectorAll('#view-dashboard .dp-card-spark')].map(svg => getComputedStyle(svg).color))).toEqual(accents);
  }
  expect(frames[0].height).toBeLessThan(confirmed.height * 0.99);  // it began at once, visibly ...
  frames.slice(1).forEach((frame, i) => expect(frame.height).toBeLessThanOrEqual(frames[i].height + 1e-6));   // ... and never rose
  expect(frames.filter(frame => frame.height > 0.05).every(frame => !frame.flatline)).toBe(true);   // no red while the signal stands
  expect(landed).not.toBeNull();
  expect(landed.height).toBe(0);                                   // red only once it is flat ...
  expect(landed.t).toBeGreaterThanOrEqual(SETTLE - 20);            // ... at about 700 ms,
  expect(landed.t).toBeLessThanOrEqual(SETTLE + 20);               // not waiting for the strip to drain
  // Mid-collapse the complexes kept travelling where their age puts them -- no teleport, no clearing.
  const mid = frames.find(frame => frame.t >= SETTLE / 2);
  expect(mid.height).toBeGreaterThan(0);
  expect(mid.born).toEqual(confirmed.born);
  frames.forEach(frame => expect(frame.born.every(born => born < confirmed.now)).toBe(true));   // none started after the pause

  // Paused: flat, one saturated red on all six, breathing in step; nothing starts.
  await expect.poll(flatline).toBe(true);
  const red = await page.evaluate(() => [...document.querySelectorAll('#view-dashboard .dp-card-spark')].map(svg => {
    const line = svg.querySelector('.dp-card-spark-line');
    const [animation] = line.getAnimations();
    return {color: getComputedStyle(svg).color, rest: Number(getComputedStyle(svg.querySelector('.dp-card-spark-rest')).stopOpacity),
      name: animation?.animationName, start: animation?.startTime};
  }));
  expect(new Set(red.map(r => r.color)).size).toBe(1);
  const [r, g, b] = red[0].color.match(/\d+/g).map(Number);
  expect(r).toBeGreaterThan(200);
  expect(Math.max(g, b)).toBeLessThan(60);                         // saturated red, not pink or white
  red.forEach(line => expect(line).toMatchObject({rest: 1, name: 'dp-card-spark-flatline', start: red[0].start}));
  expect(await page.evaluate(() => [...document.querySelectorAll('#view-dashboard .dash-hero-stat')].map(card => getComputedStyle(card, '::before').backgroundColor))).toEqual(edges);
  for (let t = 0; t < 5000; t += 250) {
    await page.clock.runFor(250);
    expect((await strip(page)).every(flat => flat.points.every(([, y]) => y === 16))).toBe(true);
    expect((await signal(page)).born).toEqual([]);
  }

  // Resumed (the event stream reconnects 10 s after its last event): colours
  // back, and a heartbeat at once, then the cadence.
  await page.clock.runFor(10000);
  pause.paused = false;
  events.emit();
  await expect.poll(() => page.evaluate(() => processingPaused)).toBe(false);
  const resumed = await page.evaluate(() => Date.now());
  expect(await flatline()).toBe(false);
  expect(await page.evaluate(() => [...document.querySelectorAll('#view-dashboard .dp-card-spark')].map(svg => getComputedStyle(svg).color))).toEqual(accents);
  const after = await births(page, 2 * INTERVAL + 500);
  expect(after.times[0] - resumed).toBeLessThanOrEqual(QUANTUM);
  intervals(after.times).forEach(gap => expect(Math.abs(gap - INTERVAL)).toBeLessThanOrEqual(QUANTUM));
});

test('pause, resume and pause again in quick succession: the signal turns back from where it is each time, nothing doubles, no stale red', async ({page}) => {
  test.setTimeout(120000);
  const pause = {paused: false};
  await boot(page, {pause});
  await page.clock.runFor(INTERVAL + 2500);
  const set = async paused => { pause.paused = paused; await page.evaluate(() => loadStats()); expect(await page.evaluate(() => processingPaused)).toBe(paused); };
  await set(true);
  const start = await signal(page);
  await page.clock.runFor(300);
  const low = await signal(page);
  expect(low.height).toBeLessThan(start.height);
  expect(low.height).toBeGreaterThan(0);
  await set(false);                                                 // released before it is flat
  const rising = [];
  for (let t = 0; t < 400; t += 16) { await page.clock.runFor(16); rising.push(await signal(page)); }
  rising.slice(1).forEach((frame, i) => expect(frame.height).toBeGreaterThanOrEqual(rising[i].height - 1e-6));   // it rises again ...
  expect(rising.at(-1).height).toBeGreaterThan(low.height);
  rising.forEach(frame => expect(frame.flatline).toBe(false));      // ... never red
  await set(true);                                                  // and paused once more, from where it stands
  const again = await signal(page);
  let flat = null;
  for (let t = 0; t < 1500 && flat === null; t += 16) {
    await page.clock.runFor(16);
    const frame = await signal(page);
    if (frame.flatline) { flat = frame.now - again.now; expect(frame.height).toBe(0); }   // red only once flat
    else if (frame.height > 0.05) expect(frame.flatline).toBe(false);
  }
  expect(flat).not.toBeNull();
  expect(flat).toBeLessThanOrEqual(SETTLE + 20);                    // never longer than a full settle
  // Nothing born while paused, at most the one a release starts at once while it was released.
  const born = (await signal(page)).born;
  expect(born).toEqual([]);
  await page.clock.runFor(3000);
  expect((await signal(page)).flatline).toBe(true);
  expect((await signal(page)).born).toEqual([]);
});

test('hidden or navigated away while it settles, the Dashboard comes back already flat and red, nothing replayed or born', async ({page}) => {
  test.setTimeout(120000);
  const pause = {paused: false};
  await boot(page, {pause});
  await page.clock.runFor(INTERVAL + 2500);
  pause.paused = true;
  await page.evaluate(() => loadStats());
  const paused = await signal(page);
  expect(paused.height).toBeGreaterThan(10);
  await page.clock.runFor(200);                                      // part-way down
  await setHidden(page, true);
  expect(await page.evaluate(() => [_dashboardHeartbeat.timer, _dashboardHeartbeat.frame])).toEqual([0, 0]);
  await page.clock.runFor(3000);
  const back = await setHidden(page, false);                         // the first frame back
  expect(back.lines.every(d => [...d.matchAll(/(-?[\d.]+) (-?[\d.]+)/g)].every(m => Number(m[2]) === 16))).toBe(true);
  const shown = await signal(page);
  expect([shown.flatline, shown.born]).toEqual([true, []]);
  // Paused while hidden, settled unseen, then resumed while still hidden: nothing from before rises again.
  pause.paused = false;
  await page.evaluate(() => loadStats());
  await page.clock.runFor(INTERVAL + 500);
  pause.paused = true;
  await setHidden(page, true);
  await page.evaluate(() => loadStats());
  const pausedHidden = (await signal(page)).now;
  await page.clock.runFor(2000);                                     // settled, unseen
  pause.paused = false;
  await page.evaluate(() => loadStats());                            // resumed, still hidden
  const shownAgain = await setHidden(page, false);
  const stillFlat = shownAgain.lines.every(d => [...d.matchAll(/(-?[\d.]+) (-?[\d.]+)/g)].every(m => Number(m[2]) === 16));
  expect(stillFlat).toBe(true);                                      // the settled heartbeats stay gone
  expect((await signal(page)).born.every(born => born > pausedHidden)).toBe(true);
  pause.paused = true;
  await page.evaluate(() => loadStats());
  await page.clock.runFor(1000);
  // Resumed, then paused again and left for another view mid-settle.
  pause.paused = false;
  await page.evaluate(() => loadStats());
  await page.clock.runFor(INTERVAL + 500);
  expect((await signal(page)).height).toBeGreaterThan(10);
  pause.paused = true;
  await page.evaluate(() => loadStats());
  const pausedAgain = await signal(page);
  await page.clock.runFor(150);
  await leaveDashboard(page);
  await page.clock.runFor(2000);
  const first = await returnAndRead(page);
  expect(first.flatline).toBe(true);
  expect(first.lines.every(d => [...d.matchAll(/(-?[\d.]+) (-?[\d.]+)/g)].every(m => Number(m[2]) === 16))).toBe(true);
  expect((await signal(page)).born.every(born => born < pausedAgain.now)).toBe(true);
});

test('with reduced motion a confirmed Global Pause All shows the still red flat lines at once, and resuming the plain baselines', async ({page}) => {
  test.setTimeout(90000);
  const pause = {paused: false};
  await boot(page, {pause, reducedMotion: 'reduce'});
  expect(await page.evaluate(() => _dashboardHeartbeat.running)).toBe(false);
  pause.paused = true;
  await page.evaluate(() => loadStats());
  const red = await signal(page);
  expect([red.flatline, red.height]).toEqual([true, 0]);              // at once, no settle to animate
  expect(await page.locator('#view-dashboard .dp-card-spark-line').first().evaluate(line => getComputedStyle(line).animationName)).toBe('none');
  pause.paused = false;
  await page.evaluate(() => loadStats());
  expect((await signal(page)).flatline).toBe(false);
});

/* Every heartbeat on the strip now, in strip coordinates, with its gradient
 * envelope; and a check that each one seen before is where it travelled to
 * with the very same envelope -- no trail lengthened, shortened or brightened. */
const snapshot = async page => ({now: await page.evaluate(() => Date.now()), beats: stripBeats(await strip(page))});
function expectSameTrails(before, after) {
  let matched = 0;
  for (const beat of before.beats) {
    const lead = beat.lead + SPEED * (after.now - before.now);
    const same = after.beats.find(b => Math.abs(b.lead - lead) < 4);   // (frames land on 16 ms ticks)
    if (!same) continue;                                        // (left the strip, or is in a gap)
    matched++;
    same.envelope.forEach((x, i) => expect(x).toBeCloseTo(beat.envelope[i], 1));
  }
  return matched;
}

test('Global Pause All at high throughput keeps each heartbeat\'s short trail exactly as it was while the signal settles, and resuming keeps it too', async ({page}) => {
  test.setTimeout(120000);
  const pause = {paused: false};
  await boot(page, {pause, throughput: {bps: 42e6, peak: 42e6}});
  await page.clock.runFor(14000);                                  // settled near 90 BPM
  let before = await snapshot(page);
  const short = before.beats.filter(beat => beat.envelope[5] - beat.envelope[3] < 150);
  expect(short.length).toBeGreaterThan(0);                         // short trails, laid out for close followers

  pause.paused = true;
  await page.evaluate(() => loadStats());                          // what a stats_changed event runs
  expect(await page.evaluate(() => processingPaused)).toBe(true);
  await page.clock.runFor(16);                                     // the first frame held
  let after = await snapshot(page);
  expect(expectSameTrails({...before, beats: short}, after)).toBe(short.length);   // the short trails, unchanged
  // (Including the newest heartbeat, whatever frame it was born in: none of
  // the trails, read from their gradient stops, lengthens.)
  const shortTrails = snap => snap.beats.forEach(beat => expect(beat.envelope[5] - beat.envelope[3]).toBeLessThan(150));
  shortTrails(after);
  // Settling: every heartbeat keeps its envelope (where its trail lies along the strip), frame after frame.
  for (let t = 0; t < 400; t += 50) {
    before = after;
    await page.clock.runFor(50);
    after = await snapshot(page);
    expectSameTrails(before, after);
    shortTrails(after);
    expect(after.beats.length).toBeGreaterThan(0);
  }
  // Resumed while still settling (past the heartbeat that was due): one starts
  // at once, the trails in flight carry on unchanged, each clear of its follower.
  pause.paused = false;
  await page.evaluate(() => loadStats());
  const resumed = await page.evaluate(() => Date.now());
  const observations = [];
  for (let t = 0; t < 2000; t += 50) {
    before = after;
    await page.clock.runFor(50);
    after = await snapshot(page);
    expect(expectSameTrails(before, after)).toBeGreaterThan(0);
    expectClearTrails(after.beats, {held: true});
    observations.push(await observe(page));
  }
  const fresh = heartbeats(observations).filter(beat => beat.born > resumed - 20);
  expect(fresh.length).toBeGreaterThan(0);
  expect(fresh[0].born - resumed).toBeLessThanOrEqual(20);
});

test('resuming before the held trail allows waits only for that trail\'s clearance, never erasing, restarting or reshaping a heartbeat', async ({page}) => {
  test.setTimeout(90000);
  const pause = {paused: false};
  await boot(page, {pause});
  await page.clock.runFor(INTERVAL + 2500);                         // 30 BPM: last heartbeat 500 ms ago
  pause.paused = true;
  await page.evaluate(() => loadStats());
  let after = await snapshot(page);
  const last = after.beats.reduce((a, b) => (a.lead < b.lead ? a : b));
  const lastBorn = after.now - last.lead / SPEED;
  // The earliest a follower may start: where the held trail leaves it 16 px.
  const clearance = lastBorn + (last.lead - last.end + 16) / SPEED;
  await page.clock.runFor(300);
  pause.paused = false;
  await page.evaluate(() => loadStats());
  const resumed = await page.evaluate(() => Date.now());
  expect(resumed).toBeLessThan(clearance);                          // resumed before the trail allows a follower
  const observations = [];
  let before;
  for (let t = 0; t < 3000; t += 50) {
    before = after;
    await page.clock.runFor(50);
    after = await snapshot(page);
    expect(expectSameTrails(before, after)).toBeGreaterThan(0);     // not erased, restarted or reshaped
    expectClearTrails(after.beats, {held: true});
    observations.push(await observe(page));
    expect(await page.evaluate(() => document.getElementById('dash-hero').classList.contains('dash-hero--flatline'))).toBe(false);
  }
  const fresh = heartbeats(observations).filter(beat => beat.born > resumed - 20);
  expect(fresh.length).toBeGreaterThan(0);
  expect(Math.abs(fresh[0].born - clearance)).toBeLessThanOrEqual(20);
});

test('only a confirmed Global Pause All holds the strip: a failed or pending request, paused transfers or idle throughput do not', async ({page}) => {
  test.setTimeout(120000);
  const pause = {paused: false, transfersPaused: 4};                // every transfer paused individually
  let answer = null, answered = 0;
  await page.route('**/api/processing/pause', async route => {
    const reply = await new Promise(resolve => { answer = resolve; });
    await route.fulfill(reply);
    answered++;
  });
  const throughput = {bps: 0, peak: 42e6};
  await boot(page, {pause, throughput});
  const flatline = () => page.evaluate(() => document.getElementById('dash-hero').classList.contains('dash-hero--flatline'));
  const idle = await births(page, IDLE + 500);
  expect(idle.times.length).toBe(1);                                 // the 3.5 BPM heartbeat runs
  expect(await flatline()).toBe(false);

  await page.evaluate(() => { pauseProcessing(); });                // the Pause All control
  await expect.poll(() => answer !== null).toBe(true);
  answer({status: 500, contentType: 'application/json', body: JSON.stringify({detail: 'refused'})});
  await expect.poll(() => answered).toBe(1);
  await expect.poll(() => page.evaluate(() => document.getElementById('btn-pause-all')?.dataset.pending !== '1')).toBe(true);
  expect(await page.evaluate(() => processingPaused)).toBe(false);
  expect(await flatline()).toBe(false);
  answer = null;
  throughput.bps = 42e6;                                             // a busy strip (90 BPM) ...
  await page.evaluate(() => { pauseProcessing(); });
  await expect.poll(() => answer !== null).toBe(true);               // ... and a request pending, unconfirmed
  const pending = await births(page, 4000);                          // (inside the request's 8 s timeout)
  expect(pending.times.length).toBeGreaterThanOrEqual(2);            // heartbeats still start (easing up from 3.5 BPM)
  expect(await flatline()).toBe(false);

  pause.paused = true;                                               // now confirmed
  answer({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true, paused: true, count: 0, failed: 0})});
  await expect.poll(() => page.evaluate(() => processingPaused)).toBe(true);
  await page.clock.runFor(12000);
  await expect.poll(flatline).toBe(true);
});

test('entering the Dashboard while paused shows the red flat lines at once, still under reduced motion, and plain baselines once resumed', async ({page}) => {
  test.setTimeout(90000);
  const pause = {paused: true};
  await boot(page, {pause});
  const flatline = () => page.evaluate(() => document.getElementById('dash-hero').classList.contains('dash-hero--flatline'));
  await expect.poll(flatline).toBe(true);
  for (let t = 0; t < 10000; t += 500) {
    await page.clock.runFor(500);
    expect((await strip(page)).every(flat)).toBe(true);              // nothing replayed, nothing started
  }
  await page.emulateMedia({reducedMotion: 'reduce'});
  await expect.poll(() => page.evaluate(() => document.querySelector('#view-dashboard .dp-card-spark-line').getAnimations().length)).toBe(0);
  expect(await flatline()).toBe(true);                               // still red, still
  pause.paused = false;
  await page.evaluate(() => loadStats());
  expect(await flatline()).toBe(false);
  await page.clock.runFor(10000);
  expect((await strip(page)).every(flat)).toBe(true);                // still baselines, no motion
});

/* Each card's glow beneath its line, against the line and its stroke gradient. */
const glow = page => page.evaluate(() => [...document.querySelectorAll('#view-dashboard .dash-hero-stat')].map(card => {
  const svg = card.querySelector('.dp-card-spark'), fill = svg.querySelector('.dp-card-spark-fill');
  const line = svg.querySelector('.dp-card-spark-line'), rect = svg.querySelector('mask rect');
  const fillGradient = svg.querySelector(fill.getAttribute('fill').slice(4, -1));
  const box = svg.getBoundingClientRect(), cardBox = card.getBoundingClientRect();
  const view = Number(svg.getAttribute('viewBox').split(' ')[3]);
  const text = Math.max(...[...card.querySelectorAll('.dhs-body *, .dhs-icon')].map(e => e.getBoundingClientRect().bottom));
  return {
    elements: [svg.querySelectorAll('path').length, svg.querySelectorAll('mask').length, svg.querySelectorAll('linearGradient').length, svg.querySelectorAll('mask rect').length],
    lineD: line.getAttribute('d'), fillD: fill.getAttribute('d'), view,
    stroke: line.getAttribute('stroke'), maskedBy: rect.getAttribute('fill'), mask: getComputedStyle(fill).mask,
    fillOpacity: Number(getComputedStyle(fill).opacity), fillAnimations: fill.getAnimations().length,
    glowColours: [...fillGradient.querySelectorAll('stop')].map(stop => getComputedStyle(stop).stopColor),
    glowOpacities: [...fillGradient.querySelectorAll('stop')].map(stop => Number(getComputedStyle(stop).stopOpacity)),
    glowFrom: Number(fillGradient.getAttribute('y1')), glowTo: Number(fillGradient.getAttribute('y2')),
    accent: getComputedStyle(card).getPropertyValue('--c').trim(), line: getComputedStyle(svg).color,
    rest: Number(getComputedStyle(svg).getPropertyValue('--dp-card-spark-rest')),
    // css px above the card's floor: the baseline, the highest a heartbeat can reach (R at y=-2.51), and the text
    baseline: cardBox.bottom - (box.top + 16 * box.height / view),
    peak: cardBox.bottom - (box.top - 2.51 * box.height / view), text: cardBox.bottom - text, card: [cardBox.width, cardBox.height],
  };
}));

/* The glow is the area under the line itself, down to the line box's floor. */
function expectGlowFollowsLine(cards) {
  for (const card of cards) {
    expect(card.elements).toEqual([2, 1, 2, 1]);                    // one fill, one line; nothing doubled
    const start = card.lineD.match(/^M (-?[\d.]+) 16/)[1];
    expect(card.fillD).toBe(`${card.lineD} V ${card.view} H ${start} Z`);
    expect(card.maskedBy).toBe(card.stroke);                         // masked by the line's own stroke gradient
  }
}

test('each card glows in its accent beneath a baseline raised 7 px, the glow the area under that very line, brightening and fading with it', async ({page}) => {
  test.setTimeout(90000);
  await boot(page, {throughput: {bps: 42e6, peak: 42e6}});
  const rest = await glow(page);
  for (const card of rest) {
    expect(card.baseline).toBeCloseTo(17.33, 1);                     // 10.33 px before
    expect(card.text - card.peak).toBeGreaterThan(3 + 3);            // the tallest heartbeat and its 3 px glow clear the text
    expect(card.glowFrom).toBe(16);                                  // from the baseline ...
    expect(card.glowTo).toBe(card.view);                             // ... down to the floor
    expect(card.glowOpacities[0]).toBeGreaterThan(card.glowOpacities[1]);   // fading down
    card.glowColours.forEach(colour => expect(colour).toBe(card.line));    // the card's accent
  }
  expect(new Set(rest.map(card => card.baseline.toFixed(2))).size).toBe(1);  // all six aligned
  expectGlowFollowsLine(rest);
  // Every frame at 90 BPM, several heartbeats on the strip: the glow is still
  // the area under the line as drawn, masked by its one gradient.
  let shaped = 0;
  for (let t = 0; t < 4000; t += 50) {
    await page.clock.runFor(50);
    const cards = await glow(page);
    expectGlowFollowsLine(cards);
    shaped += cards.filter(card => /L [\d.-]+ [\d.]+ L/.test(card.lineD) && card.lineD.split(' L ').length > 3).length;
  }
  expect(shaped).toBeGreaterThan(20);
});

test('fully paused, the red lines breathe over each card\'s still accent glow at its resting level, and nothing is doubled on re-entry or reflow', async ({page}) => {
  test.setTimeout(90000);
  const pause = {paused: true};
  await boot(page, {pause});
  await expect.poll(() => page.evaluate(() => document.getElementById('dash-hero').classList.contains('dash-hero--flatline'))).toBe(true);
  for (const card of await glow(page)) {
    expect(card.line).not.toBe(card.glowColours[0]);                // the line is red ...
    card.glowColours.forEach(colour => expect(colour).not.toBe(card.line));   // ... the glow is not
    expect(card.mask).toBe('none');
    expect(card.fillOpacity).toBe(card.rest);                        // the resting level
    expect(card.fillAnimations).toBe(0);                             // still: it does not breathe
  }
  // Re-entry, reflow and stats refreshes add nothing.
  for (let i = 0; i < 3; i++) {
    await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]')));
    await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="dashboard"]')));
    await page.evaluate(() => loadStats());
  }
  await page.setViewportSize({width: 1000, height: 900});
  await expect.poll(async () => route(await strip(page)).rows.length).toBe(2);
  pause.paused = false;
  await page.evaluate(() => loadStats());
  await page.clock.runFor(3000);
  const resumed = await glow(page);
  expectGlowFollowsLine(resumed);
  resumed.forEach(card => {
    expect(card.mask).not.toBe('none');
    card.glowColours.forEach(colour => expect(colour).toBe(card.line));
  });
});

/* The heartbeats on the strip, by birth time relative to the strip's start. */
const heartbeatState = page => page.evaluate(() => ({
  born: _dashboardHeartbeat.beats.map(beat => beat.born), timer: _dashboardHeartbeat.timer,
  frame: _dashboardHeartbeat.frame, phase: _dashboardHeartbeat.phase, now: performance.now()}));
const lines = page => page.evaluate(() => [...document.querySelectorAll('#view-dashboard .dp-card-spark-line')].map(line => line.getAttribute('d')));
const leaveDashboard = page => page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]')));
/* Return to the Dashboard and read the strip in the same task: what the first painted frame shows. */
const returnAndRead = page => page.evaluate(() => {
  nav(document.querySelector('#sidebar .nav-item[data-view="dashboard"]'));
  return {flatline: document.getElementById('dash-hero').classList.contains('dash-hero--flatline'),
    lines: [...document.querySelectorAll('#view-dashboard .dp-card-spark-line')].map(line => line.getAttribute('d'))};
});
const allLeads = cards => cards.map(card => leads(card).map(x => Math.round(x)));
/* Two pages' strips agree: the same heartbeats on every card, each within one
 * animation frame of travel (each page's frames tick on its own clock origin). */
const expectSameStrip = (a, b) => {
  expect(a.map(card => card.length)).toEqual(b.map(card => card.length));
  a.flat().forEach((x, i) => expect(Math.abs(x - b.flat()[i])).toBeLessThanOrEqual(SPEED * 16 + 1));
};

for (const [label, away, resize] of [['a short stay', 3000, null], ['a long stay', 30000, null],
                                     ['a stay during which the cards wrap', 6000, {width: 560, height: 1400}]]) {
  test(`leaving the Dashboard for another view keeps its clock: after ${label} the strip is exactly the one that never left`, async ({browser}) => {
    test.setTimeout(120000);
    const stayed = await browser.newPage(), left = await browser.newPage();
    try {
      await boot(stayed);
      await boot(left);
      // Each page's own clock origin: births compared from each strip's start.
      const origin = {stayed: (await heartbeatState(stayed)).now, left: (await heartbeatState(left)).now};
      const bornSince = async (page, key) => (await heartbeatState(page)).born.map(born => Math.round(born - origin[key]));
      await stayed.clock.runFor(INTERVAL + 1500);
      await left.clock.runFor(INTERVAL + 1500);
      await leaveDashboard(left);
      const unseen = await lines(left);
      for (let t = 0; t < away; t += 500) {
        if (resize && t === 1000) await Promise.all([stayed.setViewportSize(resize), left.setViewportSize(resize)]);
        await stayed.clock.runFor(500);
        await left.clock.runFor(500);
        // Off screen: nothing is painted, no frame is pending, only the heartbeats still on the strip are kept.
        const state = await heartbeatState(left);
        expect(state.frame).toBe(0);
        expect(state.born.length).toBeLessThanOrEqual(8);
        expect(await lines(left)).toEqual(unseen);
      }
      const first = await returnAndRead(left);
      // The same heartbeats, born at the same instants: nothing reset, replayed or doubled.
      expect(await bornSince(left, 'left')).toEqual(await bornSince(stayed, 'stayed'));
      // The first frame back is the strip at this very instant (the stayed page last drew one
      // frame earlier), so one step on it has moved exactly one step's travel -- never a fresh
      // start or a stale strip -- and both pages draw the same strip from then on.
      const firstLeads = allLeads(first.lines.map(d => ({points: [...d.matchAll(/(-?[\d.]+) (-?[\d.]+)/g)]
        .map(m => [Number(m[1]), Number(m[2])])})));
      await stayed.clock.runFor(STEP);
      await left.clock.runFor(STEP);
      const next = allLeads(await strip(left));
      expectSameStrip(next, allLeads(await strip(stayed)));
      expect(next.map(card => card.length)).toEqual(firstLeads.map(card => card.length));
      next.flat().forEach((x, i) => expect(Math.abs(firstLeads.flat()[i] - x - SPEED * STEP)).toBeLessThanOrEqual(5));
      await stayed.clock.runFor(INTERVAL);
      await left.clock.runFor(INTERVAL);
      expectSameStrip(allLeads(await strip(left)), allLeads(await strip(stayed)));
    } finally {
      await stayed.close();
      await left.close();
    }
  });
}

test('Global Pause All while another view is shown: the first frame back is already the drained red flatline', async ({page}) => {
  test.setTimeout(90000);
  const pause = {paused: false}, events = {};
  await boot(page, {pause, events});
  await page.clock.runFor(INTERVAL + 1500);
  expect((await strip(page)).some(c => !flat(c))).toBe(true);
  await leaveDashboard(page);
  // Paused in another session; the event stream brings it to this page whatever view it shows.
  pause.paused = true;
  events.emit();
  await expect.poll(() => page.evaluate(() => processingPaused)).toBe(true);
  await page.clock.runFor(20000);                                     // every heartbeat has left the strip
  const refreshed = page.waitForResponse('**/api/stats');            // the Dashboard's own refresh on entry
  const first = await returnAndRead(page);
  expect(first.flatline).toBe(true);
  expect(first.lines.every(d => [...d.matchAll(/(-?[\d.]+) (-?[\d.]+)/g)].every(m => Number(m[2]) === 16))).toBe(true);
  await refreshed;
  expect(await page.evaluate(() => document.getElementById('dash-hero').classList.contains('dash-hero--flatline'))).toBe(true);
});

// ── A hidden document: the strip shown again is the one that was never hidden ──
/* Hide or show the document (the test stands in for the browser's own tab
 * switch: document.hidden and its visibilitychange), and read the strip in the
 * same task -- what the first painted frame after it shows. */
const setHidden = (page, hidden) => page.evaluate(hidden => {
  Object.defineProperty(document, 'hidden', {configurable: true, get: () => hidden});
  document.dispatchEvent(new Event('visibilitychange'));
  return {now: performance.now(), lines: [...document.querySelectorAll('#view-dashboard .dp-card-spark-line')].map(line => line.getAttribute('d'))};
}, hidden);
const parse = d => ({points: [...d.matchAll(/(-?[\d.]+) (-?[\d.]+)/g)].map(m => [Number(m[1]), Number(m[2])])});
/* Where each heartbeat born at `born` (ms, the page's own clock) must stand at
 * `now` on each card: its leading edge, from its age alone (the strip's route
 * from the rendered cards; only complexes wholly inside a card, clear of its
 * edges, are compared). */
function expectedLeads(cards, born, now) {
  const {entries} = route(cards);
  return cards.map((card, i) => born.map(b => card.width - ((now - b) * SPEED - entries[i]))
    .filter(x => x > 4 && x + 64 < card.width).sort((p, q) => p - q));
}
const drawnLeads = (cards, lines) => lines.map((d, i) => leads(parse(d)).filter(x => x > 4 && x + 64 < cards[i].width).sort((p, q) => p - q));
/* The same birth instants, each within the 1 ms a timer fires on (a heartbeat
 * born while hidden takes its due instant exactly; a running timer fires on
 * the following whole millisecond) -- 0.24 px of travel. */
function expectSameBirths(actual, expected) {
  expect(actual.length, `${actual} vs ${expected}`).toBe(expected.length);
  actual.forEach((born, i) => expect(Math.abs(born - expected[i]), `${actual} vs ${expected}`).toBeLessThanOrEqual(1));
}
function expectLeadsAt(actual, expected) {
  expect(actual.map(card => card.length)).toEqual(expected.map(card => card.length));
  actual.flat().forEach((x, i) => expect(Math.abs(x - expected.flat()[i])).toBeLessThanOrEqual(0.6));
}
/* Two pages, one watched throughout and one hidden for a while, under the same
 * clock steps and facts: their heartbeats are born at the same instants. */
async function watchedAndHidden(browser, options = {}) {
  const watched = await browser.newPage(), hidden = await browser.newPage();
  const facts = {watched: {...(options.throughput || {bps: 26.5, peak: 86.5})}, hidden: {...(options.throughput || {bps: 26.5, peak: 86.5})}};
  await boot(watched, {...options, throughput: facts.watched});
  await boot(hidden, {...options, throughput: facts.hidden});
  const origin = {watched: (await heartbeatState(watched)).now, hidden: (await heartbeatState(hidden)).now};
  const bornSince = async key => (await heartbeatState(key === 'watched' ? watched : hidden)).born.map(born => Math.round(born - origin[key]));
  const run = async ms => { for (let t = 0; t < ms; t += 500) { await watched.clock.runFor(500); await hidden.clock.runFor(500); } };
  return {watched, hidden, facts, origin, bornSince, run, close: async () => { await watched.close(); await hidden.close(); }};
}

for (const [label, away, expired] of [['before any heartbeat leaves the strip', 1000, false], ['long enough for some to leave it', 5000, true],
                                      ['longer than the whole strip', 30000, true]]) {
  test(`hidden ${label}: the first frame back holds exactly the heartbeats a watched strip holds, each where its age puts it`, async ({browser}) => {
    test.setTimeout(120000);
    const pair = await watchedAndHidden(browser);
    try {
      await pair.run(2 * INTERVAL + 1500);                                // two heartbeats on the strip (at 2 s and 4 s)
      const before = await pair.bornSince('hidden');
      expectSameBirths(before, [2000, 4000]);
      await setHidden(pair.hidden, true);
      expect(await pair.hidden.evaluate(() => [_dashboardHeartbeat.timer, _dashboardHeartbeat.frame])).toEqual([0, 0]);
      await pair.run(away);
      const first = await setHidden(pair.hidden, false);
      const cards = await strip(pair.hidden);
      // Independently: at a steady 30 BPM a heartbeat is born every 2 s from the strip's start; those whose
      // trails have not yet left the strip are on it, each at the place its age carries it to.
      const elapsed = Math.round(first.now - pair.origin.hidden);
      const lattice = Array.from({length: Math.floor(elapsed / INTERVAL)}, (_, k) => (k + 1) * INTERVAL);
      const kept = await pair.bornSince('hidden');
      expectSameBirths(kept, lattice.filter(born => born >= kept[0] - 1));   // born on time, none skipped, none doubled
      if (expired) expect(kept[0]).toBeGreaterThan(before[0] + 1); else expect(kept.slice(0, 2)).toEqual(before);
      expectSameBirths(kept, await pair.bornSince('watched'));           // exactly the watched strip's heartbeats
      expectLeadsAt(drawnLeads(cards, first.lines), expectedLeads(cards, kept.map(b => b + pair.origin.hidden), first.now));
      expect(drawnLeads(cards, first.lines).flat().length).toBeGreaterThan(0);
      // And it goes on from there: one step later every heartbeat has moved exactly one step's travel.
      await pair.watched.clock.runFor(STEP);
      await pair.hidden.clock.runFor(STEP);
      expectSameStrip(allLeads(await strip(pair.hidden)), allLeads(await strip(pair.watched)));
    } finally { await pair.close(); }
  });
}

test('hidden while the cadence changes, polls still arriving: the heartbeats due meanwhile are born at the watched strip\'s instants', async ({browser}) => {
  test.setTimeout(120000);
  const pair = await watchedAndHidden(browser);
  try {
    await pair.run(INTERVAL + 500);
    await setHidden(pair.hidden, true);
    pair.facts.watched.bps = pair.facts.hidden.bps = 86.5;               // towards 90 BPM while hidden
    await pair.run(6000);
    const first = await setHidden(pair.hidden, false);
    const kept = await pair.bornSince('hidden');
    expectSameBirths(kept, await pair.bornSince('watched'));
    expect(new Set(kept).size).toBe(kept.length);
    expect(intervals(kept).every(gap => gap >= 60000 / 90 - 1)).toBe(true);
    const cards = await strip(pair.hidden);
    expectLeadsAt(drawnLeads(cards, first.lines), expectedLeads(cards, kept.map(b => b + pair.origin.hidden), first.now));
  } finally { await pair.close(); }
});

test('hidden while the throughput cannot be read: only the heartbeats the clock\'s last facts make, none invented', async ({browser}) => {
  test.setTimeout(120000);
  const pair = await watchedAndHidden(browser);
  try {
    await pair.run(INTERVAL + 500);
    await setHidden(pair.hidden, true);
    pair.facts.watched.fail = pair.facts.hidden.fail = true;           // unreachable for both, watched or not
    await pair.run(7000);
    const first = await setHidden(pair.hidden, false);
    const kept = await pair.bornSince('hidden');
    expectSameBirths(kept, await pair.bornSince('watched'));
    // Still the steady 30 BPM of the last facts read: one every 2 s, nothing filled in.
    expect(intervals(kept).every(gap => Math.abs(gap - INTERVAL) <= 1)).toBe(true);
    const cards = await strip(pair.hidden);
    expectLeadsAt(drawnLeads(cards, first.lines), expectedLeads(cards, kept.map(b => b + pair.origin.hidden), first.now));
  } finally { await pair.close(); }
});

test('repeated hide and show, with readings and a stats change arriving around each return: no drift, no doubled heartbeat', async ({browser}) => {
  test.setTimeout(150000);
  const pair = await watchedAndHidden(browser);
  try {
    await pair.run(INTERVAL);
    // 2.5 s hidden, 0.5 s shown: the 3 s cycle walks across the 2 s cadence, so heartbeats fall due both while
    // hidden and while shown.
    for (let cycle = 0; cycle < 12; cycle += 1) {
      await setHidden(pair.hidden, true);
      await pair.run(2500);
      // The return, a throughput reading and a stats re-read (a live change) in the same instant, then frames.
      await pair.hidden.evaluate(() => { loadRuntimeSpeed(); loadStats(); });
      await setHidden(pair.hidden, false);
      await pair.run(500);
    }
    const kept = await pair.bornSince('hidden');
    expectSameBirths(kept, await pair.bornSince('watched'));
    expect(new Set(kept).size).toBe(kept.length);
    // Still on the 30 BPM lattice after 12 cycles: no drift accumulates.
    kept.forEach(born => expect(Math.min(born % INTERVAL, INTERVAL - born % INTERVAL)).toBeLessThanOrEqual(1));
    expectSameStrip(allLeads(await strip(pair.hidden)), allLeads(await strip(pair.watched)));
  } finally { await pair.close(); }
});

test('hidden on another view, then shown and back to the Dashboard by the sidebar: the first frame is the watched strip', async ({browser}) => {
  test.setTimeout(120000);
  const pair = await watchedAndHidden(browser);
  try {
    await pair.run(INTERVAL + 1500);
    await leaveDashboard(pair.hidden);
    await setHidden(pair.hidden, true);
    await pair.run(5000);
    await setHidden(pair.hidden, false);
    await pair.run(500);
    const first = await returnAndRead(pair.hidden);
    const now = await pair.hidden.evaluate(() => performance.now());
    const kept = await pair.bornSince('hidden');
    expectSameBirths(kept, await pair.bornSince('watched'));
    const cards = await strip(pair.hidden);
    expectLeadsAt(drawnLeads(cards, first.lines), expectedLeads(cards, kept.map(b => b + pair.origin.hidden), now));
  } finally { await pair.close(); }
});

test('Global Pause All arriving while hidden: the heartbeats due before it are born, none after; the return shows the watched strip', async ({browser}) => {
  test.setTimeout(120000);
  const watched = await browser.newPage(), hidden = await browser.newPage();
  const pauses = {watched: {paused: false}, hidden: {paused: false}};
  try {
    await boot(watched, {pause: pauses.watched});
    await boot(hidden, {pause: pauses.hidden});
    const origin = {watched: (await heartbeatState(watched)).now, hidden: (await heartbeatState(hidden)).now};
    const run = async ms => { for (let t = 0; t < ms; t += 500) { await watched.clock.runFor(500); await hidden.clock.runFor(500); } };
    await run(INTERVAL + 500);
    await setHidden(hidden, true);
    await run(3000);                                                     // a heartbeat is due at 4 s, while hidden
    pauses.watched.paused = pauses.hidden.paused = true;
    await Promise.all([watched.evaluate(() => loadStats()), hidden.evaluate(() => loadStats())]);
    // Read at the hold, before the settle clears the strip: the births due before it, and none after.
    const born = async (page, key) => (await heartbeatState(page)).born.map(b => Math.round(b - origin[key]));
    const kept = await born(hidden, 'hidden');
    expectSameBirths(kept, await born(watched, 'watched'));
    expect(kept.some(b => Math.abs(b - 4000) <= 1)).toBe(true);         // due while hidden, before the hold
    expect(kept.every(b => b <= 4001)).toBe(true);                       // none once held
    await run(2500);
    const back = await setHidden(hidden, false);                         // shown again: settled flat and red, nothing born
    expect(back.lines.every(d => [...d.matchAll(/(-?[\d.]+) (-?[\d.]+)/g)].every(m => Number(m[2]) === 16))).toBe(true);
    expect(await hidden.evaluate(() => document.getElementById('dash-hero').classList.contains('dash-hero--flatline'))).toBe(true);
    expect(await born(hidden, 'hidden')).toEqual([]);
    expect(await born(watched, 'watched')).toEqual([]);
  } finally { await watched.close(); await hidden.close(); }
});

test('hidden for 24 hours at the full 90 BPM, nothing advancing the clock meanwhile: a bounded reconstruction, the strip of this instant, the cadence carrying on', async ({page}) => {
  test.setTimeout(120000);
  const facts = {bps: 83, peak: 83};                                     // at the sustained peak: 90 BPM
  await boot(page, {throughput: facts});
  await page.clock.runFor(20000);                                        // the cadence has eased to 90 BPM
  await setHidden(page, true);
  facts.fail = true;                                                     // no reading reaches the clock while hidden
  await page.clock.fastForward(24 * 3600 * 1000);                        // one jump: no intermediate advance
  expect(await page.evaluate(() => _dashboardHeartbeat.advancedAt)).toBeLessThan(await page.evaluate(() => performance.now()) - 86000000);
  // The return does the whole reconstruction; its cost is read with the real clock around it.
  const idle = Date.now(); await page.evaluate(() => 0); const overhead = Date.now() - idle;
  const started = Date.now();
  const first = await setHidden(page, false);
  const spent = Date.now() - started - overhead;
  test.info().annotations.push({type: 'reconstruction-ms', description: String(spent)});
  console.log(`24 h reconstruction: ${spent} ms (evaluate overhead ${overhead} ms)`);
  expect(spent).toBeLessThan(250);
  const state = await heartbeatState(page);
  const cards = await strip(page);
  // Bounded: only heartbeats whose trail can still be on the strip -- a few strip transits' worth, never the day's.
  const end = await page.evaluate(() => _dashboardHeartbeat.route.end);
  const transit = (end + 2 * 48 + TRAIL) / SPEED;
  expect(state.born.length).toBeGreaterThan(0);
  expect(state.born.length).toBeLessThanOrEqual(Math.ceil(transit / (60000 / 90)) + 1);
  expect(state.born.every(born => first.now - born <= transit)).toBe(true);
  // On the 90 BPM cadence: one every 666.7 ms, the last no more than one interval ago, none in the future.
  intervals(state.born).forEach(gap => expect(Math.abs(gap - 60000 / 90)).toBeLessThanOrEqual(0.01));
  expect(first.now - state.born.at(-1)).toBeLessThan(60000 / 90);
  expect(first.now - state.born.at(-1)).toBeGreaterThanOrEqual(0);
  // The first frame: each heartbeat where its age puts it.
  expectLeadsAt(drawnLeads(cards, first.lines), expectedLeads(cards, state.born, first.now));
  expect(drawnLeads(cards, first.lines).flat().length).toBeGreaterThan(0);
  // And the cadence carries on from there, its timer firing on the whole millisecond after each due instant.
  facts.fail = false;
  await page.clock.runFor(3000);
  const after = (await heartbeatState(page)).born.filter(born => born > state.born.at(-1));
  expect(after.length).toBeGreaterThanOrEqual(4);
  intervals([state.born.at(-1), ...after]).forEach(gap => expect(Math.abs(gap - 60000 / 90)).toBeLessThanOrEqual(1.01));
});

test('reduced motion while hidden: shown again, only the still flat baselines', async ({page}) => {
  test.setTimeout(90000);
  await boot(page);
  await page.clock.runFor(INTERVAL + 1500);
  await setHidden(page, true);
  await page.emulateMedia({reducedMotion: 'reduce'});
  await page.clock.runFor(4000);
  await setHidden(page, false);
  await expect.poll(async () => (await strip(page)).every(flat)).toBe(true);
  expect(await page.evaluate(() => [_dashboardHeartbeat.running, _dashboardHeartbeat.beats.length])).toEqual([false, 0]);
});

test('repeated navigation adds no clock, frame loop or heartbeat', async ({page}) => {
  test.setTimeout(90000);
  await boot(page);
  await page.evaluate(() => {
    for (let i = 0; i < 25; i++) {
      nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]'));
      nav(document.querySelector('#sidebar .nav-item[data-view="dashboard"]'));
    }
  });
  const seen = await births(page, 5 * INTERVAL + 500);
  expect(seen.times.length).toBe(5);
  intervals(seen.times).forEach(gap => expect(Math.abs(gap - INTERVAL)).toBeLessThanOrEqual(QUANTUM));
});

test('reducing motion flattens every line and stops the clock; allowing it again starts afresh', async ({page}) => {
  await boot(page);
  await page.clock.runFor(INTERVAL + 1500);
  expect((await strip(page)).some(c => !flat(c))).toBe(true);
  await page.emulateMedia({reducedMotion: 'reduce'});
  // (A media-query change reaches the page at its next rendering step.)
  await expect.poll(async () => (await strip(page)).every(flat)).toBe(true);
  await page.clock.runFor(8000);
  expect((await strip(page)).every(flat)).toBe(true);
  await page.emulateMedia({reducedMotion: 'no-preference'});
  await expect.poll(() => page.evaluate(() => _dashboardHeartbeat.running)).toBe(true);
  await page.clock.runFor(INTERVAL - 100);
  expect((await strip(page)).every(flat)).toBe(true);                // a fresh start: the first heartbeat is 2 s away
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
