/* The page clock under the test's control BEFORE the application exists.
 *
 * Playwright's installed clock runs in real time from install() until it is
 * paused, and pauseAt(target) refuses a target that real time has already
 * passed. Reading the page's time and then pausing a little ahead of it, after
 * the application booted, is therefore a race against real time: on a loaded
 * runner the pause arrives late and throws "Cannot fast-forward to the past".
 *
 * Paused here, on the still-blank page, nothing is scheduled yet, so the pause
 * may land far enough ahead to be certain its target is still in the future
 * and fires nothing. The headroom exceeds every case's timeout, so it is never
 * a bound on real time a case could meet. The application then boots on a
 * paused clock, and only the test moves its time. */
const HEADROOM_MS = 10 * 60 * 1000;

async function pauseClockBeforeBoot(page) {
  await page.clock.install();
  const blank = await page.evaluate(() => Date.now());
  await page.clock.pauseAt(blank + HEADROOM_MS);
}

module.exports = {pauseClockBeforeBoot};
