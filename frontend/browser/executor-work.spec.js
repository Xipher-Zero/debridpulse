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
  await expect(card(page)).toContainText(
    'This is an advanced recovery surface. Use Downloads for normal management, '
    + 'and these controls only for troubleshooting or recovery.');
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
  // No native endpoint is reachable from this surface at all.
  const requested = [];
  page.on('request', request => requested.push(new URL(request.url()).pathname));
  await card(page).locator('[data-dp-executor-work-refresh]').click();
  await expect.poll(() => requested.filter(p => p.startsWith('/api/executor-work')).length)
    .toBeGreaterThan(0);
  expect(requested.filter(p => /aria2|sab|queue|nzb/.test(p))).toEqual([]);
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
