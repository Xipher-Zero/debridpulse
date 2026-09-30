const { test, expect } = require('@playwright/test');

/* DP 1.0.13: the topbar speed and the browser-tab speed follow the one neutral
 * throughput fact at presentation cadence. A slower runtime fact (execution-slot
 * occupancy) must never serialize the speed behind it: the speed is read from
 * its own cheap neutral projection, both surfaces read the one frontend state. */

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

const MIB = 1024 * 1024;

/** The slow projection (occupancy) answers after `slowMs`; the speed projection answers at once
 *  with a new value per request. Returns the per-route request log. */
async function serve(page, {slowMs}) {
  const log = {status: 0, speed: 0};
  await page.route(url => url.pathname === '/api/execution/runtime-status', async route => {
    log.status += 1;
    await new Promise(resolve => setTimeout(resolve, slowMs));
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true,
      download_bytes_per_second: (100 + log.status) * MIB, active_execution_slots: 2,
      max_download_bytes_per_second: 0})}).catch(() => {});
  });
  // One active download, so the tab title legitimately presents the speed.
  await page.route(url => url.pathname === '/api/stats', async route => {
    const response = await route.fetch();
    const stats = await response.json();
    return route.fulfill({response, json: {...stats, by_status: {...(stats.by_status || {}), downloading: 1},
      operator_active_progress_pct: 40}});
  });
  await page.route(url => url.pathname === '/api/execution/throughput', route => {
    log.speed += 1;
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true,
      download_bytes_per_second: log.speed * MIB, max_download_bytes_per_second: 0})});
  });
  return log;
}

/** Record every change of the topbar speed text and of the tab title, with timestamps. */
async function recordChanges(page, ms) {
  return page.evaluate(ms => new Promise(resolve => {
    const badge = document.getElementById('runtime-badge-speed');
    const changes = [];
    let lastBadge = badge.textContent, lastTitle = document.title;
    const started = performance.now();
    const observer = new MutationObserver(() => {
      const now = performance.now() - started;
      if (badge.textContent !== lastBadge) {
        lastBadge = badge.textContent;
        changes.push({at: now, badge: lastBadge, title: document.title, state: _runtimeStatusState.liveBps});
      }
      if (document.title !== lastTitle) lastTitle = document.title;
    });
    observer.observe(badge, {childList: true, characterData: true, subtree: true});
    setTimeout(() => { observer.disconnect(); resolve(changes); }, ms);
  }), ms);
}

// RED 1A/1B: a slow unrelated runtime fact does not stretch the speed cadence.
test('the speed keeps a sub-second cadence while the runtime-status projection is slow', async ({ page }) => {
  await isolateExternalFonts(page);
  await serve(page, {slowMs: 2500});
  await page.goto('/');
  await expect.poll(() => page.title()).toContain('DP |');
  const changes = await recordChanges(page, 4000);
  const gaps = changes.slice(1).map((change, index) => change.at - changes[index].at);
  expect(changes.length, `speed changed only ${changes.length} times in 4 s`).toBeGreaterThanOrEqual(4);
  expect(Math.max(...gaps), `largest speed gap ${Math.round(Math.max(...gaps))} ms`).toBeLessThanOrEqual(1000);
});

// RED 1C: both surfaces present the same sample from the one frontend state.
test('every topbar speed sample is the browser-tab speed and the one runtime state', async ({ page }) => {
  await isolateExternalFonts(page);
  await serve(page, {slowMs: 2500});
  await page.goto('/');
  await expect.poll(() => page.title()).toContain('DP |');
  const changes = await recordChanges(page, 2500);
  expect(changes.length).toBeGreaterThanOrEqual(2);
  for (const change of changes) {
    expect(change.title).toContain(change.badge.replace(/\s+/g, ''));
    expect(change.badge).toBe(await page.evaluate(bps => fmtSpeed(bps), change.state));
  }
});

test('the slower facts still arrive, through the same one state writer', async ({ page }) => {
  await isolateExternalFonts(page);
  const log = await serve(page, {slowMs: 300});
  await page.goto('/');
  await expect(page.locator('#runtime-badge-active')).toHaveText('2', {timeout: 5000});
  expect(log.status).toBeGreaterThan(0);
  // Speed is never written from the slow projection: its (100+n) MiB/s values never appear.
  await page.waitForTimeout(1500);
  const speed = await page.evaluate(() => _runtimeStatusState.liveBps);
  expect(speed).toBeLessThan(100 * 1024 * 1024);
});
