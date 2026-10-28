const { test, expect } = require('@playwright/test');

/* DP 1.0.13 Executor Work.
 *
 * One neutral projection, one renderer, two unrelated executors. Every
 * assertion below is about what the browser does with DebridPulse facts: it
 * must render rows it has never seen the shape of before, offer exactly the
 * controls the projection declared legal, address them by the DURABLE attempt
 * id, and know nothing whatsoever about a GID, an NZO id, a native status name
 * or a native action URL.
 */

// One payload, covering a per-execution copier and an aggregate-throughput
// acquisition service. Nothing in it is executor-specific except the display
// identity of the executor that holds each row.
const PAYLOAD = {
  ok: true,
  items: [
    {
      attempt_id: 'attempt-direct-1', transfer_id: 11, artifact_id: 101,
      name: 'ubuntu-desktop.iso', executor_id: 'aria2', executor_name: 'aria2',
      state: 'running', filter_group: 'active', progress: 42.5,
      completed_bytes: 425000000, total_bytes: 1000000000, remaining_bytes: 575000000,
      bytes_per_second: 1258291, speed_measured_per_execution: true,
      error: null, controls: ['pause', 'cancel'],
    },
    {
      attempt_id: 'attempt-usenet-1', transfer_id: 12, artifact_id: 102,
      name: 'Some.Collection.S01', executor_id: 'sabnzbd', executor_name: 'Usenet',
      state: 'paused', filter_group: 'paused', progress: 10,
      completed_bytes: 104857600, total_bytes: null, remaining_bytes: null,
      bytes_per_second: null, speed_measured_per_execution: false,
      error: null, controls: ['resume', 'cancel'],
    },
    {
      attempt_id: 'attempt-direct-2', transfer_id: 13, artifact_id: 103,
      name: 'waiting-payload.bin', executor_id: 'aria2', executor_name: 'aria2',
      state: 'queued', filter_group: 'waiting', progress: 0,
      completed_bytes: 0, total_bytes: 4096, remaining_bytes: 4096,
      bytes_per_second: 0, speed_measured_per_execution: true,
      error: null, controls: ['pause', 'cancel'],
    },
  ],
  summary: {
    total: 3,
    download_speed: 2516582,
    // One row's size is unknown, so there is no honest aggregate remaining.
    remaining_bytes: null,
    counts: {active: 1, waiting: 1, paused: 1, stopped: 0},
  },
};

const card = page => page.locator('[data-dp-executor-work-card="1"]');
const rows = page => card(page).locator('.dp-executor-work-item');
const rowFor = (page, name) => rows(page).filter({hasText: name});

async function installExecutorWork(page, payload = PAYLOAD) {
  const actions = [];
  const state = {payload, actions, fail: false};
  await page.route('**/api/executor-work**', async route => {
    const url = new URL(route.request().url());
    if (route.request().method() === 'POST') {
      const parts = url.pathname.split('/').filter(Boolean);
      actions.push({attempt: decodeURIComponent(parts.at(-2)), action: parts.at(-1)});
      if (state.fail) {
        await route.fulfill({status: 409, contentType: 'application/json',
          body: JSON.stringify({detail: 'That action is not currently available for this execution'})});
        return;
      }
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true})});
      return;
    }
    await route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify(state.payload)});
  });
  return state;
}

async function openDownloads(page) {
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await page.locator('#view-settings [data-tab="downloads"]').click();
  await expect(page.locator('#view-settings [data-panel="downloads"]')).toBeVisible();
  await expect(card(page)).toBeVisible();
  await expect(rows(page)).toHaveCount(3);
}

test('two unrelated executors render from one neutral payload', async ({page}) => {
  await installExecutorWork(page);
  await openDownloads(page);

  // DP 1.0.13 Settings consolidation: operator-facing break-glass presentation.
  // The generic projection and action routing beneath it are unchanged; only
  // what the operator is told changed, and it names no internal concept.
  await expect(card(page)).toContainText('Download Engine Activity');
  await expect(card(page)).toContainText(
    'View current download engine jobs and intervene when something is stuck.');
  // Two lines in the Database Reset caution's own hierarchy: what the surface
  // is, then when to use it.
  const warning = card(page).locator('.dp-settings-caution.dp-executor-work-context');
  await expect(warning.locator(':scope > b')).toHaveText('This is an advanced recovery surface.');
  await expect(warning.locator(':scope > span')).toHaveText(
    'Use Downloads for normal management. These controls are only for troubleshooting or recovery.');
  expect((await card(page).innerText()).toLowerCase()).not.toContain('executor');

  // Each row states which executor holds it, from the projection's own
  // display identity.
  await expect(rowFor(page, 'ubuntu-desktop.iso').locator('.dp-executor-work-owner')).toHaveText('aria2');
  await expect(rowFor(page, 'Some.Collection.S01').locator('.dp-executor-work-owner')).toHaveText('Usenet');

  // The rows are structurally identical: one grammar, not two.
  const shapes = await rows(page).evaluateAll(items => items.map(item =>
    Array.from(item.querySelectorAll('.dp-executor-work-k')).map(k => k.textContent.trim()).join('|')));
  expect(new Set(shapes).size).toBe(1);
});

test('the browser holds no native executor identity, status or action', async ({page}) => {
  const state = await installExecutorWork(page);
  await openDownloads(page);

  const markup = (await card(page).innerHTML()).toLowerCase();
  for (const native of ['gid', 'nzo', 'native', 'correlation', 'apikey', '/aria2/', '/sab']) {
    expect(markup, `native token ${native} reached the browser`).not.toContain(native);
  }
  // No native endpoint is reachable from this surface at all: the live
  // re-read it makes when execution activity changes asks only the projection.
  const requested = [];
  page.on('request', request => requested.push(new URL(request.url()).pathname));
  await page.evaluate(() => document.dispatchEvent(new CustomEvent('debridpulse:execution-activity-changed')));
  await expect.poll(() => requested.filter(p => p.startsWith('/api/executor-work')).length, {timeout: 12000})
    .toBeGreaterThan(0);
  // (The window now spans the spaced re-read, so it also sees the shell's own
  // Admin engine-health check, app.js checkConnections -> POST
  // /api/settings/test-aria2, which retries while no aria2 answers; that is
  // the shell's request, not this surface's, and is the only one excluded.)
  expect(requested.filter(p => /aria2|sab|queue|nzb/.test(p) && p !== '/api/settings/test-aria2')).toEqual([]);
  expect(state.actions).toEqual([]);
});

test('speed and remaining are shown only where the projection states them', async ({page}) => {
  await installExecutorWork(page);
  await openDownloads(page);

  const facts = async (name, key) => rowFor(page, name).locator('.dp-executor-work-facts > div')
    .filter({has: page.locator('.dp-executor-work-k', {hasText: key})})
    .locator('.dp-executor-work-v').innerText();

  // The aggregate-throughput executor publishes no per-job rate.
  expect((await facts('Some.Collection.S01', 'Speed')).trim()).toBe('—');
  // Unknown size means there is no remaining figure to state.
  expect((await facts('Some.Collection.S01', 'Remaining')).trim()).toBe('—');
  // The per-execution executor's own rate is shown.
  expect((await facts('ubuntu-desktop.iso', 'Speed')).trim()).not.toBe('—');
  expect((await facts('ubuntu-desktop.iso', 'Remaining')).trim()).not.toBe('—');

  // The card-level totals come from the projection's summary, and an
  // incomplete remaining total is never manufactured.
  await expect(card(page).locator('[data-dp-executor-work-remaining]')).toHaveText('— Remaining');
  await expect(card(page).locator('[data-dp-executor-work-speed]')).not.toHaveText('0 KB/s');
});

test('filters group rows by the neutral execution state alone', async ({page}) => {
  await installExecutorWork(page);
  await openDownloads(page);

  const visible = () => rows(page).evaluateAll(items =>
    items.filter(item => !item.hidden).map(item => item.dataset.executorGroup));

  for (const [filter, expected] of [['active', ['active']], ['waiting', ['waiting']],
                                    ['paused', ['paused']], ['stopped', []]]) {
    await card(page).locator(`[data-executor-filter="${filter}"]`).click();
    expect(await visible(), `filter ${filter}`).toEqual(expected);
  }
  await card(page).locator('[data-executor-filter="all"]').click();
  expect((await visible()).length).toBe(3);
});

test('controls are exactly what the projection declared legal', async ({page}) => {
  await installExecutorWork(page);
  await openDownloads(page);

  const controlsOf = name => rowFor(page, name).locator('[data-executor-action]')
    .evaluateAll(buttons => buttons.map(b => b.dataset.executorAction));

  expect(await controlsOf('ubuntu-desktop.iso')).toEqual(['pause', 'cancel']);
  expect(await controlsOf('Some.Collection.S01')).toEqual(['resume', 'cancel']);
});

test('an action is addressed by the durable attempt id and dispatched generically', async ({page}) => {
  const state = await installExecutorWork(page);
  await openDownloads(page);

  await rowFor(page, 'ubuntu-desktop.iso').locator('[data-executor-action="pause"]').click();
  await expect.poll(() => state.actions).toEqual([{attempt: 'attempt-direct-1', action: 'pause'}]);

  await rowFor(page, 'Some.Collection.S01').locator('[data-executor-action="resume"]').click();
  await expect.poll(() => state.actions.length).toBe(2);
  expect(state.actions[1]).toEqual({attempt: 'attempt-usenet-1', action: 'resume'});
});

test('termination is gated by the canonical Settings confirmation, never a browser confirm', async ({page}) => {
  const state = await installExecutorWork(page);
  // A native confirm() would auto-dismiss to false here AND would be recorded.
  const nativeDialogs = [];
  page.on('dialog', dialog => { nativeDialogs.push(dialog.type()); dialog.dismiss(); });
  await openDownloads(page);

  await rowFor(page, 'ubuntu-desktop.iso').locator('[data-executor-action="cancel"]').click();

  const dialog = page.locator('.dp-modal-dialog, [role="alertdialog"]').first();
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('Terminate');
  expect(nativeDialogs).toEqual([]);
  // Exactly one dialog owner: no second modal or focus trap.
  await expect(page.locator('[role="alertdialog"]')).toHaveCount(1);

  // Declining performs no mutation at all.
  await dialog.locator('[data-modal-cancel]').click();
  await expect(dialog).toHaveCount(0);
  await page.waitForTimeout(400);
  expect(state.actions).toEqual([]);

  // Accepting dispatches the generic action for that durable attempt.
  await rowFor(page, 'ubuntu-desktop.iso').locator('[data-executor-action="cancel"]').click();
  const confirmDialog = page.locator('[role="alertdialog"]').first();
  await expect(confirmDialog).toBeVisible();
  await confirmDialog.locator('[data-modal-accept]').click();
  await expect.poll(() => state.actions).toEqual([{attempt: 'attempt-direct-1', action: 'cancel'}]);
});

test('a refused action is reported and the surface re-reads canonical truth', async ({page}) => {
  const state = await installExecutorWork(page);
  await openDownloads(page);
  state.fail = true;

  await rowFor(page, 'ubuntu-desktop.iso').locator('[data-executor-action="pause"]').click();
  await expect(page.locator('#toasts .toast').last()).toContainText('Executor work');
  await expect.poll(() => state.actions.length).toBe(1);
  // The rows are still the projection's, re-read rather than locally mutated.
  await expect(rows(page)).toHaveCount(3);
});

test('an empty projection says so, and offers nothing to control', async ({page}) => {
  await installExecutorWork(page, {
    ok: true, items: [],
    summary: {total: 0, download_speed: 0, remaining_bytes: null,
              counts: {active: 0, waiting: 0, paused: 0, stopped: 0}},
  });
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await page.locator('#view-settings [data-tab="downloads"]').click();
  await expect(card(page)).toBeVisible();
  await expect(card(page).locator('.empty'))
    .toHaveText('No download engine activity right now.');
  await expect(card(page).locator('[data-executor-action]')).toHaveCount(0);
});


test('the execution state badge is the DP 6px rounded rectangle, and pill controls keep their shape', async ({page}) => {
  await installExecutorWork(page);
  await openDownloads(page);
  const badge = rowFor(page, 'ubuntu-desktop.iso').locator('.badge').first();
  await expect(badge).toBeVisible();
  expect(await badge.evaluate(node => getComputedStyle(node).borderRadius)).toBe('6px');
  // A real pill control (a toggle track) is not a status badge and keeps its geometry.
  const track = page.locator('#view-settings .toggle .ttrack').first();
  expect(await track.evaluate(node => getComputedStyle(node).borderRadius)).not.toBe('6px');
});

/* DP 1.0.14: Download Engine Activity follows the application's one stream.
 *
 * SIMULATED: the stream's signals are dispatched as the document events app.js
 * bridges them to (debridpulse:execution-activity-changed, -pulse-connected),
 * and the authoritative projection is the mocked GET /executor-work, whose
 * answer is the payload committed when each request STARTS -- so a held
 * request answers with what was true when it was asked. Nothing here is live
 * executor truth. */
async function liveExecutorWork(page, payload = PAYLOAD) {
  const state = {payload, starts: [], inflight: 0, maxInflight: 0, held: [], hold: false};
  await page.route('**/api/executor-work', async route => {
    const body = JSON.stringify(state.payload);
    state.starts.push(Date.now());
    state.inflight += 1;
    state.maxInflight = Math.max(state.maxInflight, state.inflight);
    if (state.hold) await new Promise(resolve => state.held.push(resolve));
    state.inflight -= 1;
    await route.fulfill({status: 200, contentType: 'application/json', body});
  });
  state.release = () => state.held.splice(0).forEach(resolve => resolve());
  return state;
}

const signal = (page, name = 'debridpulse:execution-activity-changed') =>
  page.evaluate(event => document.dispatchEvent(new CustomEvent(event)), name);
const names = page => rows(page).evaluateAll(items => items.map(item =>
  item.querySelector('.dp-executor-work-name').textContent.trim()));
const withItems = (items, total = items.length) => ({...PAYLOAD, items, summary: {...PAYLOAD.summary, total}});
const [DIRECT, USENET, WAITING] = PAYLOAD.items;
const SPACING_MS = 2000;
const fact = (page, name, key) => rowFor(page, name).locator('.dp-executor-work-facts > div')
  .filter({has: page.locator('.dp-executor-work-k', {hasText: key})}).locator('.dp-executor-work-v').innerText();

test('added, updated, finished and removed work appears by itself; there is no Refresh and one stream', async ({page}) => {
  await page.addInitScript(() => {
    const Native = window.EventSource;
    window.__dpStreams = 0;
    window.EventSource = function (...args) { window.__dpStreams += 1; return new Native(...args); };
    window.EventSource.prototype = Native.prototype;
  });
  const state = await liveExecutorWork(page);
  await openDownloads(page);
  await expect(card(page).getByRole('button', {name: /Refresh/})).toHaveCount(0);
  await expect(card(page).locator('[data-dp-executor-work-refresh]')).toHaveCount(0);
  const done = await fact(page, 'ubuntu-desktop.iso', 'Done');

  // Usenet's job is removed, the waiting one finished, the running one moved
  // on, and a new one was added -- signalled, never fetched by hand.
  const added = {...WAITING, attempt_id: 'attempt-direct-3', transfer_id: 14, name: 'new-arrival.bin',
    state: 'running', filter_group: 'active', progress: 5};
  state.payload = withItems([{...DIRECT, progress: 77.5, completed_bytes: 775000000},
    {...WAITING, state: 'succeeded', filter_group: 'stopped', progress: 100, controls: []}, added]);
  await signal(page);
  await expect.poll(() => names(page), {timeout: SPACING_MS + 4000})
    .toEqual(['new-arrival.bin', 'ubuntu-desktop.iso', 'waiting-payload.bin']);
  expect(await fact(page, 'ubuntu-desktop.iso', 'Done')).not.toBe(done);
  await expect(rowFor(page, 'waiting-payload.bin').locator('.badge')).toHaveText('Completed');
  await expect(rowFor(page, 'Some.Collection.S01')).toHaveCount(0);
  expect(await page.evaluate(() => window.__dpStreams)).toBe(1);
  expect(state.maxInflight).toBe(1);
});

test('a burst during a re-read is ONE trailing re-read, spaced, and ends at the latest answer', async ({page}) => {
  const state = await liveExecutorWork(page);
  await openDownloads(page);
  await expect.poll(() => state.starts.length).toBe(1);

  state.hold = true;
  await signal(page);
  await expect.poll(() => state.starts.length, {timeout: SPACING_MS + 4000}).toBe(2);   // in flight, held
  // Twenty signals and two newer commits while that request is held.
  state.payload = withItems([{...DIRECT, name: 'intermediate.bin'}]);
  for (let index = 0; index < 20; index += 1) await signal(page);
  state.payload = withItems([{...DIRECT, name: 'latest.bin'}, USENET]);
  await page.waitForTimeout(300);
  expect(state.starts.length, 'a second request started while one was in flight').toBe(2);
  state.hold = false;
  state.release();

  await expect.poll(() => names(page), {timeout: SPACING_MS + 4000}).toEqual(['latest.bin', 'Some.Collection.S01']);
  expect(state.starts.length, 'the burst was not one trailing re-read').toBe(3);
  expect(state.starts[2] - state.starts[1], 'the trailing re-read was not spaced').toBeGreaterThanOrEqual(SPACING_MS - 50);
  expect(state.maxInflight).toBe(1);
  // Nothing else is pending: no further request without a further signal.
  await page.waitForTimeout(SPACING_MS + 500);
  expect(state.starts.length).toBe(3);
});

test('an answer that belongs to an earlier arrival on the card is never shown', async ({page}) => {
  const state = await liveExecutorWork(page);
  await openDownloads(page);
  await page.evaluate(() => {
    window.__dpStaleSeen = false;
    const list = document.querySelector('[data-dp-executor-work-list="1"]');
    new MutationObserver(() => { if (list.textContent.includes('stale-row.bin')) window.__dpStaleSeen = true; })
      .observe(list, {childList: true, subtree: true, characterData: true});
  });
  await page.waitForTimeout(SPACING_MS + 100);
  state.hold = true;
  state.payload = withItems([{...DIRECT, name: 'stale-row.bin'}]);
  await signal(page);
  await expect.poll(() => state.starts.length).toBe(2);                     // held: answers stale-row.bin
  // Leave the card and come back while it is in flight; the truth moved on.
  await page.locator('#view-settings [data-tab="extraction"]').click();
  state.payload = withItems([{...DIRECT, name: 'fresh-row.bin'}]);
  state.hold = false;
  await page.locator('#view-settings [data-tab="downloads"]').click();
  state.release();
  await expect.poll(() => names(page), {timeout: SPACING_MS + 4000}).toEqual(['fresh-row.bin']);
  expect(await page.evaluate(() => window.__dpStaleSeen)).toBe(false);
  expect(state.maxInflight).toBe(1);
});

test('nothing is read while the card is off screen or for a non-Admin; arriving and reconnecting re-read once', async ({page}) => {
  const state = await liveExecutorWork(page);
  await openDownloads(page);
  await page.waitForTimeout(SPACING_MS + 100);
  const baseline = state.starts.length;
  const quiet = async label => {
    for (const name of ['debridpulse:execution-activity-changed', 'debridpulse:pulse-connected']) {
      for (let index = 0; index < 5; index += 1) await signal(page, name);
    }
    await page.waitForTimeout(SPACING_MS + 300);
    expect(state.starts.length, label).toBe(baseline);
  };
  // Another Settings section, then another page.
  await page.locator('#view-settings [data-tab="extraction"]').click();
  await quiet('read while another Settings section was shown');
  await page.locator('#sidebar .nav-item[data-view="dashboard"]').click();
  await expect(page.locator('#view-dashboard')).toHaveClass(/\bactive\b/);
  await quiet('read while another page was shown');
  // Arriving again re-reads once.
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await page.locator('#view-settings [data-tab="downloads"]').click();
  await expect.poll(() => state.starts.length).toBe(baseline + 1);
  // A reconnected stream re-reads once.
  await page.waitForTimeout(SPACING_MS + 100);
  await signal(page, 'debridpulse:pulse-connected');
  await expect.poll(() => state.starts.length, {timeout: SPACING_MS + 2000}).toBe(baseline + 2);
  // Without the Admin's authority (the application's own predicate) nothing
  // is read, however the stream signals.
  await page.evaluate(() => { window.dpCan = () => false; });
  const before = state.starts.length;
  for (let index = 0; index < 5; index += 1) await signal(page);
  await page.waitForTimeout(SPACING_MS + 300);
  expect(state.starts.length).toBe(before);
});

test('a live re-read keeps the focused control, the untouched rows and the chosen filter', async ({page}) => {
  const state = await liveExecutorWork(page);
  await openDownloads(page);
  await page.waitForTimeout(SPACING_MS + 100);
  const pause = rowFor(page, 'ubuntu-desktop.iso').locator('[data-executor-action="pause"]');
  await pause.focus();
  const speed = await fact(page, 'ubuntu-desktop.iso', 'Speed');
  await page.evaluate(() => {
    document.activeElement.__dpKept = true;
    document.querySelector('[data-attempt="attempt-usenet-1"]').__dpKept = true;
  });
  state.payload = withItems([{...DIRECT, progress: 51.25, completed_bytes: 512500000, bytes_per_second: 2097152}, USENET, WAITING]);
  await signal(page);
  await expect.poll(() => fact(page, 'ubuntu-desktop.iso', 'Speed'), {timeout: SPACING_MS + 4000}).not.toBe(speed);
  expect(await page.evaluate(() => document.activeElement?.__dpKept === true
    && document.activeElement.dataset.executorAction === 'pause')).toBe(true);
  expect(await page.evaluate(() => document.querySelector('[data-attempt="attempt-usenet-1"]').__dpKept === true)).toBe(true);
  // The chosen filter survives a re-read.
  await card(page).locator('[data-executor-filter="paused"]').click();
  await page.waitForTimeout(SPACING_MS + 100);
  state.payload = withItems([{...DIRECT, progress: 60}, USENET, WAITING]);
  await signal(page);
  await expect.poll(() => state.starts.length, {timeout: SPACING_MS + 2000}).toBeGreaterThan(2);
  await expect(card(page).locator('[data-executor-filter="paused"]')).toHaveAttribute('aria-selected', 'true');
  expect(await rows(page).evaluateAll(items => items.filter(item => !item.hidden).map(item => item.dataset.executorGroup)))
    .toEqual(['paused']);
});

/* DP 1.0.14: a row's first line at every width. The name is one line, cut
 * with an ellipsis and whole in its DP tooltip, with the executor beneath it;
 * the row's actions stay on that first line, side by side, at the card's
 * trailing inset -- the name yields width, never the actions. */
test('each row keeps one line: a truncated name and its actions together at the trailing edge, at every width', async ({page}) => {
  const long = 'The.Extraordinarily.Long.Release.Name.Of.A.Documentary.Series.S01E01.2160p.UHD.BluRay.REMUX.HDR10.HEVC.DTS-HD.MA.5.1-SYNTHETIC.mkv';
  const unbroken = 'Another_Very_Long_Archive_Name_Without_Any_Spaces_To_Break_On_part0001_of_0042_synthetic_fixture.rar';
  await installExecutorWork(page, {...PAYLOAD, items: [
    {...PAYLOAD.items[0], name: long}, {...PAYLOAD.items[1], name: unbroken}, {...PAYLOAD.items[2], name: 'short.iso', controls: ['cancel']}]});
  await openDownloads(page);
  let previous = {};
  for (const width of [1440, 1180, 1024, 900, 780, 760, 700, 600, 480, 390]) {
    // Below 780px the shell's sidebar stops taking width, so the card -- and
    // the name's room -- grows once there; within one shell mode it only shrinks.
    if (width === 760) previous = {};
    await page.setViewportSize({width, height: 1000});
    await page.waitForFunction(() => document.getElementById('sidebar').getAnimations().every(a => a.playState !== 'running'));
    const rows = await rowsOf(page);
    for (const row of rows) {
      const at = `${row.name.slice(0, 12)} at ${width}px`;
      expect(row.lines, `${at}: the name wraps`).toBe(1);
      expect(row.ownerBelow, `${at}: the executor is not beneath the name`).toBe(true);
      expect(row.buttons, at).toBeGreaterThan(0);
      expect(row.oneRow && row.firstLine, `${at}: the actions left the first line`).toBe(true);
      expect(Math.abs(row.trailing), `${at}: the actions are off the trailing inset`).toBeLessThanOrEqual(0.5);
      expect(row.clear, `${at}: the name runs under the actions`).toBeGreaterThanOrEqual(9.5);
      expect(row.overflow, `${at}: overflow`).toBeLessThanOrEqual(0);
      expect(row.tooltip, `${at}: the tooltip is not the whole name`).toBe(true);
      // Narrower, the name only ever gets less room.
      if (previous[row.name] !== undefined) expect(row.nameWidth, at).toBeLessThanOrEqual(previous[row.name] + 0.5);
      previous[row.name] = row.nameWidth;
    }
    if (width <= 1180) expect(rows.find(row => row.name === long).truncated, `${width}px: the long name is not cut`).toBe(true);
    if (width === 1440) expect(rows.find(row => row.name === 'short.iso').truncated).toBe(false);
  }
  // The actions are still the keyboard's: focus reaches Pause.
  await rowFor(page, long.slice(0, 20)).locator('[data-executor-action="pause"]').focus();
  await expect(rowFor(page, long.slice(0, 20)).locator('[data-executor-action="pause"]')).toBeFocused();
});

const rowsOf = page => page.evaluate(() => [...document.querySelectorAll('.dp-executor-work-item')].map(item => {
  const r = n => n.getBoundingClientRect(), st = getComputedStyle(item), ib = r(item);
  const content = ib.right - parseFloat(st.borderRightWidth) - parseFloat(st.paddingRight);
  const name = item.querySelector('.dp-executor-work-name'), nb = r(name), ob = r(item.querySelector('.dp-executor-work-owner'));
  const buttons = [...item.querySelectorAll('.dp-executor-work-actions .btn')].map(r);
  const line = parseFloat(getComputedStyle(name).lineHeight) || 1.2 * parseFloat(getComputedStyle(name).fontSize);
  return {name: name.textContent, lines: Math.round(nb.height / line), nameWidth: nb.width, truncated: name.scrollWidth > name.clientWidth + 0.5,
    tooltip: name.dataset.tooltip === name.textContent, ownerBelow: ob.top >= nb.bottom - 1 && Math.abs(ob.left - nb.left) < 1,
    buttons: buttons.length, oneRow: buttons.every(b => Math.abs(b.top - buttons[0].top) < 1), firstLine: buttons.every(b => b.top < nb.bottom),
    trailing: content - Math.max(...buttons.map(b => b.right)), clear: Math.min(...buttons.map(b => b.left)) - nb.right,
    overflow: Math.max(item.scrollWidth - item.clientWidth, item.parentElement.scrollWidth - item.parentElement.clientWidth)};
}));
