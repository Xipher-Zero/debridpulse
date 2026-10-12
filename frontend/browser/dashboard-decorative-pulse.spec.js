const { test, expect } = require('@playwright/test');

/* DP 1.0.13 decorative Dashboard heartbeat. Each KPI card rests on a dim flat
 * baseline; after its own random 1-3 s interval one angular ECG beat enters
 * from the right edge and crosses the card at one fixed speed, and its trail
 * fades back into the baseline just before that card's next beat. Beats run
 * only while the Dashboard is active, visible and motion is allowed. The test
 * owns time (page.clock) for the intervals; each beat's own motion is read
 * from its animation. */

const PULSE = '#view-dashboard .dp-card-spark-pulse';
const STEP = 50;

async function boot(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.clock.install();
  await page.goto('/');
  await expect(page.locator('#s-total')).not.toHaveText('—');
  await expect(page.locator(PULSE)).toHaveCount(6);
  // From here only the test moves time, so measured intervals carry no real-time drift.
  await page.clock.pauseAt(await page.evaluate(() => Date.now() + 50));
  await page.evaluate(() => { window.__beats = []; });
}

/* Advance fake time in small steps, recording each newly started beat once:
 * the card, when it started, its duration, the instant (ms) its complex has
 * crossed the card, and its keyframes. */
async function advance(page, ms, step = STEP) {
  for (let t = 0; t < ms; t += step) {
    await page.clock.runFor(step);
    await page.evaluate(selector => {
      document.querySelectorAll(selector).forEach((path, card) => {
        const running = path.getAnimations();
        if (running.length > 1) window.__beats.push({card, overlap: running.length});
        for (const animation of running) {
          if (animation.id) continue;
          animation.id = `beat-${window.__beats.length}`;
          const frames = animation.effect.getKeyframes();
          const duration = animation.effect.getComputedTiming().duration;
          window.__beats.push({card, at: Date.now(), duration, crossedAt: frames[1].computedOffset * duration,
            easing: [animation.effect.getTiming().easing, frames[0].easing],
            frames: frames.map(f => ({x: f.transform, opacity: Number(f.opacity)}))});
        }
      });
    }, PULSE);
  }
  return page.evaluate(() => window.__beats);
}

const running = page => page.locator(PULSE).evaluateAll(paths => paths.flatMap(path => path.getAnimations()).length);
const geometry = page => page.locator('#view-dashboard .dp-card-spark').evaluateAll(svgs => svgs.map(svg =>
  ['.dp-card-spark-line', '.dp-card-spark-pulse'].map(sel => svg.querySelector(sel).getAttribute('d'))));

test('each card beats after its own 1-3 s interval: the ECG enters from the right, crosses at one speed and its trail fades out before the next beat', async ({page}) => {
  await boot(page);
  // At rest: a flat baseline, the beat invisible, no animation.
  const shapes = await geometry(page);
  expect(shapes.every(([line]) => line === 'M 0 16 H 100')).toBe(true);
  expect(await running(page)).toBe(0);
  expect(await page.locator(PULSE).evaluateAll(paths => paths.map(path => getComputedStyle(path).opacity))).toEqual(Array(6).fill('0'));
  expect(await page.locator('#view-dashboard .dp-card-spark').evaluateAll(svgs => svgs.every(svg => svg.getAttribute('aria-hidden') === 'true'))).toBe(true);

  // A bright leading beat whose trail fades along its length.
  const stops = await page.locator('#dp-card-spark-beat-1 stop').evaluateAll(nodes => nodes.map(node => Number(getComputedStyle(node).stopOpacity)));
  expect(stops).toEqual([1, 0.9, 0]);

  // Measure from a fresh entry on the paused clock; stats refreshes and a
  // repeated Dashboard selection then add no second schedule.
  await page.locator('#sidebar .nav-item[data-view="torrents"]').click();
  await page.evaluate(() => { window.__beats = []; });
  const start = await page.evaluate(() => Date.now());
  await page.locator('#sidebar .nav-item[data-view="dashboard"]').click();
  await page.evaluate(() => Promise.all([loadStats(), loadStats()]));
  await page.locator('#sidebar .nav-item[data-view="dashboard"]').click();
  const beats = await advance(page, 9000);

  expect(beats.filter(b => b.overlap)).toEqual([]);
  const cards = [0, 1, 2, 3, 4, 5].map(card => beats.filter(b => b.card === card));
  const first = cards.map(list => list[0].at - start);
  for (const at of first) {
    expect(at).toBeGreaterThanOrEqual(1000);
    expect(at).toBeLessThanOrEqual(3000 + STEP);
  }
  expect(new Set(first).size).toBeGreaterThan(1);          // cards are not synchronized
  for (const list of cards) {
    expect(list.length).toBeGreaterThanOrEqual(3);
    list.slice(1).forEach((beat, i) => {
      const gap = beat.at - list[i].at;                       // interval between beat starts
      expect(gap).toBeGreaterThanOrEqual(1000 - STEP);
      expect(gap).toBeLessThanOrEqual(3000 + STEP);
      expect(list[i].duration + 60).toBeGreaterThan(gap - STEP);  // the trail fades out just before the next beat ...
      expect(list[i].duration).toBeLessThan(gap + STEP);          // ... never after it
    });
    expect(new Set(list.slice(1).map((beat, i) => beat.at - list[i].at)).size).toBeGreaterThan(1);  // intervals vary
  }
  for (const beat of beats) {
    expect(beat.frames.map(f => f.x)).toEqual(['translateX(100px)', 'translateX(-24px)', 'translateX(-24px)']);  // right to left
    expect(beat.crossedAt).toBeCloseTo(800, 6);             // one fixed travel time over one fixed distance ...
    expect(beat.easing).toEqual(['linear', 'linear']);       // ... travelled linearly: one constant speed
    expect(beat.frames.map(f => f.opacity)).toEqual([1, 1, 0]);  // the trail ends at the baseline
  }

  await page.evaluate(() => loadStats());
  expect(await geometry(page)).toEqual(shapes);
  expect(await page.evaluate(() => localStorage.getItem('debridpulse.dashboard.metric-history.v2'))).toBeNull();
});

test('leaving the Dashboard, hiding the document or reducing motion stops every beat, and each return schedules afresh', async ({page}) => {
  await boot(page);
  const stopsAndResumes = async (leave, back) => {
    await advance(page, 3100);
    await leave();
    // (A media-query change reaches the page at its next rendering step.)
    await expect.poll(() => running(page)).toBe(0);
    const before = (await page.evaluate(() => window.__beats)).length;
    expect((await advance(page, 8000, 500)).length).toBe(before);
    const returned = await page.evaluate(() => Date.now());
    await back();
    await expect.poll(() => page.evaluate(() => !document.hidden && !matchMedia('(prefers-reduced-motion: reduce)').matches)).toBe(true);
    const resumed = (await advance(page, 3100)).slice(before);
    expect(new Set(resumed.map(b => b.card)).size).toBe(6);
    expect(Math.min(...resumed.map(b => b.at - returned))).toBeGreaterThanOrEqual(1000);
  };

  await stopsAndResumes(
    () => page.locator('#sidebar .nav-item[data-view="torrents"]').click(),
    () => page.locator('#sidebar .nav-item[data-view="dashboard"]').click());
  const visibility = hidden => page.evaluate(hidden => {
    Object.defineProperty(document, 'hidden', {configurable: true, get: () => hidden});
    document.dispatchEvent(new Event('visibilitychange'));
  }, hidden);
  await stopsAndResumes(() => visibility(true), () => visibility(false));
  // The reduced-motion preference applies live, without a reload; the baseline stays.
  await stopsAndResumes(
    () => page.emulateMedia({reducedMotion: 'reduce'}),
    () => page.emulateMedia({reducedMotion: 'no-preference'}));
});

test('reduced motion from the start shows only the still baseline', async ({page}) => {
  await page.emulateMedia({reducedMotion: 'reduce'});
  await boot(page);
  expect((await geometry(page)).every(([line]) => line === 'M 0 16 H 100')).toBe(true);
  expect(await page.locator('#view-dashboard .dp-card-spark-line').evaluateAll(lines => lines.map(line => getComputedStyle(line).opacity))).toEqual(Array(6).fill('0.4'));
  expect(await advance(page, 10000, 1000)).toEqual([]);
  await page.locator('#sidebar .nav-item[data-view="torrents"]').click();
  await page.locator('#sidebar .nav-item[data-view="dashboard"]').click();
  expect(await advance(page, 10000, 1000)).toEqual([]);
});
