const { test, expect } = require('@playwright/test');

/* DP 1.0.13 decorative Dashboard KPI pulse. Each card's waveform is fixed
 * artwork; one short pulse travels left to right along it after an
 * independent random 3-10 s pause, lasting 1-1.5 s, never two at once on one
 * card, and only while the Dashboard is active, visible and motion is allowed.
 * The test owns time (page.clock) for the pauses; each pulse's own timing is
 * read from its animation. */

const PULSE = '#view-dashboard .dp-card-spark-pulse';

async function boot(page, {reducedMotion = 'no-preference'} = {}) {
  await page.emulateMedia({reducedMotion});
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.clock.install();
  // Registered before app.js's own DOMContentLoaded listener, which first schedules the pulses.
  await page.addInitScript(() => document.addEventListener('DOMContentLoaded', () => { window.__booted = Date.now(); }));
  await page.goto('/');
  await expect(page.locator('#s-total')).not.toHaveText('—');
  await expect(page.locator(PULSE)).toHaveCount(6);
  // Every pulse animation ever started, tagged with the fake time it began.
  await page.evaluate(() => { window.__pulses = []; });
}

/* Advance fake time in small steps, recording each newly started pulse once. */
async function advance(page, ms, step = 100) {
  for (let t = 0; t < ms; t += step) {
    await page.clock.runFor(step);
    await page.evaluate(selector => {
      document.querySelectorAll(selector).forEach((path, card) => {
        const running = path.getAnimations();
        if (running.length > 1) window.__pulses.push({card, overlap: running.length});
        for (const animation of running) {
          if (animation.id) continue;
          animation.id = `pulse-${window.__pulses.length}`;
          const timing = animation.effect.getComputedTiming();
          const frames = animation.effect.getKeyframes();
          window.__pulses.push({card, at: Date.now(), duration: timing.duration,
            from: Number(frames[0].strokeDashoffset), to: Number(frames[frames.length - 1].strokeDashoffset)});
        }
      });
    }, PULSE);
  }
  return page.evaluate(() => window.__pulses);
}

/* Each card's pulses in order: never two at once, at least 3 s apart (the
 * next pause starts only after the last pulse ends). */
function perCard(pulses) {
  expect(pulses.filter(p => p.overlap)).toEqual([]);
  const cards = [0, 1, 2, 3, 4, 5].map(card => pulses.filter(p => p.card === card).map(p => p.at));
  for (const times of cards) times.slice(1).forEach((at, i) => expect(at - times[i]).toBeGreaterThanOrEqual(3000));
  return cards;
}

const geometry = page => page.locator('#view-dashboard .dp-card-spark').evaluateAll(svgs => svgs.map(svg =>
  ['.dp-card-spark-line', '.dp-card-spark-fill', '.dp-card-spark-pulse'].map(sel => svg.querySelector(sel).getAttribute('d'))));

test('each card pulses along its fixed waveform after its own 3-10 s pause, and stats refreshes change neither', async ({page}) => {
  await boot(page);
  const shapes = await geometry(page);
  expect(new Set(shapes.map(([line]) => line)).size).toBe(6);
  for (const [line, fill, pulse] of shapes) {
    expect(pulse).toBe(line);
    expect(fill).toBe(`${line} L 100 24 L 0 24 Z`);
  }
  expect(await page.locator('#view-dashboard .dp-card-spark').evaluateAll(svgs => svgs.every(svg => svg.getAttribute('aria-hidden') === 'true'))).toBe(true);

  const start = await page.evaluate(() => window.__booted);
  // Stats refreshes and repeated Dashboard entries add no second schedule.
  await page.evaluate(() => Promise.all([loadStats(), loadStats()]));
  await page.locator('#sidebar .nav-item[data-view="dashboard"]').click();
  const pulses = await advance(page, 10300);
  const first = perCard(pulses).map(times => times[0] - start);
  for (const at of first) {
    expect(at).toBeGreaterThanOrEqual(3000);
    expect(at).toBeLessThanOrEqual(10300);
  }
  expect(new Set(first).size).toBeGreaterThan(1);   // cards are not synchronized
  for (const p of pulses) {
    expect(p.duration).toBeGreaterThanOrEqual(1000);
    expect(p.duration).toBeLessThanOrEqual(1500);
    expect(p.from).toBeGreaterThan(p.to);          // dash offset falls: the dash moves from the path's start to its end
    expect(p.to).toBe(-100);                        // and leaves past the right endpoint (pathLength 100)
  }

  // Each finished pulse schedules that card's next one; the waveform never changes.
  // (Animations run on the real timeline, timers on the test's clock: end the
  // running pulses here rather than waiting for them in real time.)
  await page.evaluate(selector => Promise.all([...document.querySelectorAll(selector)]
    .flatMap(path => path.getAnimations().map(animation => { animation.finish(); return animation.finished; }))), PULSE);
  const later = perCard(await advance(page, 20000));
  expect(later.every(times => times.length >= 2)).toBe(true);
  await page.evaluate(() => loadStats());
  expect(await geometry(page)).toEqual(shapes);
  expect(await page.evaluate(() => localStorage.getItem('debridpulse.dashboard.metric-history.v2'))).toBeNull();
});

test('leaving the Dashboard or hiding the document stops pulses, and re-entry schedules afresh', async ({page}) => {
  await boot(page);
  await advance(page, 10100);
  await page.locator('#sidebar .nav-item[data-view="torrents"]').click();
  expect(await page.locator(PULSE).evaluateAll(paths => paths.flatMap(path => path.getAnimations()).length)).toBe(0);
  const before = (await page.evaluate(() => window.__pulses)).length;
  expect((await advance(page, 20000, 1000)).length).toBe(before);

  const entered = await page.evaluate(() => Date.now());
  await page.locator('#sidebar .nav-item[data-view="dashboard"]').click();
  const resumed = perCard((await advance(page, 10300)).slice(before));
  expect(resumed.every(times => times.length >= 1)).toBe(true);
  expect(Math.min(...resumed.map(times => times[0] - entered))).toBeGreaterThanOrEqual(3000);

  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
    document.dispatchEvent(new Event('visibilitychange'));
  });
  expect(await page.locator(PULSE).evaluateAll(paths => paths.flatMap(path => path.getAnimations()).length)).toBe(0);
  const hidden = (await page.evaluate(() => window.__pulses)).length;
  expect((await advance(page, 20000, 1000)).length).toBe(hidden);
});

test('reduced motion shows the still waveform and schedules no pulse', async ({page}) => {
  await boot(page, {reducedMotion: 'reduce'});
  expect((await geometry(page)).every(([line]) => line.startsWith('M 0.00 '))).toBe(true);
  expect(await advance(page, 20000, 1000)).toEqual([]);
  await page.locator('#sidebar .nav-item[data-view="torrents"]').click();
  await page.locator('#sidebar .nav-item[data-view="dashboard"]').click();
  expect(await advance(page, 20000, 1000)).toEqual([]);
});
