// Downloads operator queue: Name-cell drag vs Details click, cross-page drag
// on the bounded table, view gating, the destructive-preemption confirmation,
// the selection column after Actions, the quiet unhovered Remove, Move to Top
// (row menu and bulk action, one request), the context-menu policy, and a
// selection that never repaginates the page it was made on.
const { test, expect } = require('@playwright/test');

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function transfer(id, overrides = {}) {
  return {
    id,
    name: `Queue item ${String(id).padStart(2, '0')}`,
    hash: `queue-${id}-0123456789abcdef`,
    status: 'queued',
    progress: 0,
    size_bytes: 1024 * 1024 * id,
    created_at: `2026-09-${String(10 + (id % 18)).padStart(2, '0')}T12:00:00Z`,
    completed_at: null,
    source: 'direct_link',
    label: null,
    queue_member: true,
    current_provider_id: 'general_http',
    current_provider_name: 'HTTP & HTTPS',
    provider_provenance_status: 'known',
    extraction_status: null,
    source_failure_count: 0,
    ...overrides,
  };
}

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({
    status: 200, contentType: 'text/css', body: '',
  }));
}

/* A server-ordered queue: ``items`` is the queue order then history; a move
 * is applied by stable identity against the whole collection. */
async function installQueue(page, initial, {victims = {}, consent = {}, topVictims = []} = {}) {
  let items = clone(initial);
  const log = {gets: [], moves: [], details: [], tops: [], bulk: []};
  let bulk = {error: false, hold: null};
  const respond = (route, status, body) => route.fulfill({
    status, contentType: 'application/json', body: JSON.stringify(body),
  });
  await page.route('**/api/torrents**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    const move = url.pathname.match(/^\/api\/torrents\/(\d+)\/priority$/);
    const detail = url.pathname.match(/^\/api\/torrents\/(\d+)$/);
    if (url.pathname === '/api/torrents' && method === 'GET') {
      log.gets.push(Object.fromEntries(url.searchParams));
      const order = url.searchParams.get('order') || '';
      const status = url.searchParams.get('status') || '';
      const search = (url.searchParams.get('search') || '').toLowerCase();
      const limit = Math.max(1, Number(url.searchParams.get('limit')) || 25);
      const offset = Math.max(0, Number(url.searchParams.get('offset')) || 0);
      let view = order === 'queue' ? items : [...items].sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id - a.id);
      if (status) view = view.filter(item => item.status === status);
      if (search) view = view.filter(item => item.name.toLowerCase().includes(search));
      return respond(route, 200, {items: clone(view.slice(offset, offset + limit)), total: view.length});
    }
    if (move && method === 'PATCH') {
      const id = Number(move[1]);
      const body = request.postDataJSON() || {};
      log.moves.push({id, ...body});
      const queue = items.filter(item => item.queue_member && item.id !== id);
      const history = items.filter(item => !item.queue_member);
      let index = body.before_id != null ? queue.findIndex(item => item.id === body.before_id)
        : queue.findIndex(item => item.id === body.after_id) + 1;
      if (body.before_id == null && body.after_id == null) index = 0;
      const position = items.filter(item => item.queue_member).findIndex(item => item.id === id);
      // Going up overtakes the restart-only victims it passes; going down,
      // the moved transfer is itself overtaken; a reaffirmation renews the
      // restarts its standing promotion still needs.
      const passed = body.reaffirm ? [] : position > index ? queue.slice(index).filter(item => victims[item.id])
        : index > position && victims[id] ? [{id}] : [];
      const confirmed = new Set(body.confirm_restart || []);
      const needed = body.reaffirm ? (consent[id] ? [consent[id]] : []) : passed.map(item => victims[item.id]);
      if (needed.length && !needed.every(victim => victim.attempt_ids.every(attempt => confirmed.has(attempt)))) {
        return respond(route, 409, {detail: {
          confirmation: 'restart_required', changed: confirmed.size > 0,
          transfers: needed.map(victim => clone(victim)),
          message: 'Reordering the queue interrupts transfers that cannot resume.',
        }});
      }
      const moved = items.find(item => item.id === id);
      if (body.reaffirm) {
        moved.queue_restart_consent = false;
        return respond(route, 200, {ok: true, torrent_id: id, priority: 0, moved: false,
                                    outcome: needed.length ? 'preempting' : 'queued', preempting: []});
      }
      queue.splice(index, 0, moved);
      items = [...queue, ...history];
      return respond(route, 200, {ok: true, torrent_id: id, priority: queue.length - index,
                                  outcome: needed.length ? 'preempting' : 'queued', preempting: []});
    }
    if (url.pathname === '/api/torrents/move-to-top' && method === 'POST') {
      // The server derives the block from its own queue: the requested
      // members, in their current queue order, ahead of every other member.
      const body = request.postDataJSON() || {};
      log.tops.push(body);
      const wanted = new Set(body.ids || []);
      const members = items.filter(item => item.queue_member);
      const block = members.filter(item => wanted.has(item.id));
      if (!block.length) {
        return respond(route, 409, {detail: {reason: 'not_queued', message: 'Only unfinished transfers have a place in the queue.'}});
      }
      const confirmed = new Set(body.confirm_restart || []);
      if (topVictims.length && !topVictims.every(victim => victim.attempt_ids.every(attempt => confirmed.has(attempt)))) {
        return respond(route, 409, {detail: {
          confirmation: 'restart_required', changed: confirmed.size > 0, transfers: clone(topVictims),
          message: 'Reordering the queue interrupts transfers that cannot resume.',
        }});
      }
      items = [...block, ...members.filter(item => !wanted.has(item.id)), ...items.filter(item => !item.queue_member)];
      return respond(route, 200, {ok: true, moved: block.length, ignored: wanted.size - block.length,
                                  outcome: topVictims.length ? 'preempting' : 'queued', preempting: []});
    }
    if (url.pathname === '/api/torrents/bulk' && method === 'POST') {
      const body = request.postDataJSON() || {};
      log.bulk.push(body);
      if (bulk.hold) await bulk.hold;
      if (bulk.error) return respond(route, 500, {detail: 'Bulk action failed'});
      const ids = new Set(body.ids || []);
      if (body.action === 'delete') items = items.filter(item => !ids.has(item.id));
      else items.forEach(item => { if (ids.has(item.id)) item.status = body.action === 'pause' ? 'paused' : 'queued'; });
      return respond(route, 200, {ok: ids.size, failed: 0});
    }
    if (detail && method === 'GET') {
      const id = Number(detail[1]);
      log.details.push(id);
      return respond(route, 200, {...clone(items.find(item => item.id === id) || transfer(id)), files: []});
    }
    return route.fallback();
  });
  return {
    log,
    // The Downloads page's own list requests (it always pages by offset).
    page: () => log.gets.filter(request => 'offset' in request).at(-1),
    order: () => items.map(item => item.id),
    setVictims(next) { victims = next; },
    setBulk(next) { bulk = {...bulk, ...next}; },
  };
}

async function openDownloads(page) {
  await page.locator('#sidebar .nav-item[data-view="torrents"]').click();
  await expect(page.locator('#view-torrents')).toHaveClass(/\bactive\b/);
  await expect(page.locator('#t-tbody .dp-downloads-detail-row').first()).toBeVisible();
}

async function boot(page, items, options) {
  await isolateExternalFonts(page);
  const queue = await installQueue(page, items, options);
  await page.goto('/');
  await openDownloads(page);
  await settled(page, queue);
  return queue;
}

/* The list renders once more after it measures its capacity: the page is
 * settled when the latest list request is unchanged over several readings and
 * the rows it asked for are the ones rendered -- only then is a row's cell a
 * stable focus target. */
async function settled(page, queue) {
  let previous = '';
  let stable = 0;
  await expect.poll(async () => {
    const request = queue.page();
    if (!request) return false;
    const count = await page.locator('#t-tbody tr[data-torrent-id]').count();
    const signature = `${queue.log.gets.length}:${request.limit}:${request.offset}:${count}`;
    stable = signature === previous ? stable + 1 : 0;
    previous = signature;
    return stable >= 3 && count === Math.min(Number(request.limit), queue.order().length - Number(request.offset));
  }, {timeout: 4000, intervals: [100, 100, 100, 100, 100, 200, 200]}).toBe(true);
}

const row = (page, id) => page.locator(`#t-tbody tr[data-torrent-id="${id}"]`);
const nameCell = (page, id) => row(page, id).locator('td').first();

async function renderedIds(page) {
  return page.locator('#t-tbody tr[data-torrent-id]').evaluateAll(rows => rows.map(r => Number(r.dataset.torrentId)));
}

/* A real pointer drag: press inside ``from`` at (dx, dy), travel in steps. */
// The list re-renders once after it measures its capacity: a box is read
// from the settled DOM, never from a row that is being replaced.
async function box(locator) {
  let found = null;
  await expect.poll(async () => (found = await locator.boundingBox()) !== null).toBe(true);
  return found;
}

async function drag(page, from, to, {dx = 0.85, dy = 0.5, toY = 0.25, hold} = {}) {
  const start = await box(from);
  await page.mouse.move(start.x + start.width * dx, start.y + start.height * dy);
  await page.mouse.down();
  await page.mouse.move(start.x + start.width * dx, start.y + start.height * dy + 12, {steps: 4});
  if (hold) await hold();
  const end = await box(to);
  await page.mouse.move(end.x + end.width * 0.5, end.y + end.height * toY, {steps: 12});
  await page.mouse.up();
}

test('U1 a Name click opens Details once; a Name-cell drag reorders and never opens Details', async ({ page }) => {
  const queue = await boot(page, [1, 2, 3, 4].map(id => transfer(id)).concat(
    [transfer(5, {status: 'completed', progress: 100, queue_member: false})]));
  expect(queue.page().order).toBe('queue');

  // The grip lives inside the Name cell, at its left edge.
  const grip = nameCell(page, 3).locator('.dp-queue-grip svg[data-dp-lucide="grip-vertical"]');
  await expect(grip).toBeVisible();
  const offset = await nameCell(page, 3).evaluate(cell =>
    cell.querySelector('.dp-queue-grip svg').getBoundingClientRect().left - cell.getBoundingClientRect().left);
  expect(offset).toBeLessThan(24);
  // History is not a queue entry: no grip, no drag. The grip adds no height.
  await expect(nameCell(page, 5).locator('.dp-queue-grip')).toHaveCount(0);
  const heights = await page.locator('#t-tbody tr[data-torrent-id]').evaluateAll(rows =>
    rows.map(r => Math.round(r.getBoundingClientRect().height)));
  expect(new Set(heights).size).toBe(1);

  await row(page, 2).locator('.t-chk').check();
  await nameCell(page, 2).locator('.t-name').click();
  await expect.poll(() => queue.log.details).toEqual([2]);
  await expect(page.locator('#overlay')).toHaveClass(/\bopen\b/);
  await page.evaluate(() => closeModal());
  await expect(page.locator('#overlay')).not.toHaveClass(/\bopen\b/);
  // Details covers the page until it has faded away; the raw drag below waits for that.
  await expect(page.locator('#overlay')).toBeHidden();

  // Drag from the blank space of the Name cell: one move, no Details.
  await drag(page, nameCell(page, 4), row(page, 1), {dx: 0.9});
  await expect.poll(() => queue.log.moves).toEqual([{id: 4, before_id: 1, after_id: null}]);
  await expect.poll(() => renderedIds(page)).toEqual([4, 1, 2, 3, 5]);
  // The grip itself drags too.
  await drag(page, nameCell(page, 3).locator('.dp-queue-grip'), row(page, 1), {dx: 0.5, toY: 0.75});
  await expect.poll(() => queue.log.moves.length).toBe(2);
  expect(queue.log.moves[1]).toEqual({id: 3, before_id: 2, after_id: 1});
  expect(queue.log.details).toEqual([2]);
  // Selection survives the reorders; checkbox and action clicks never drag.
  await expect(row(page, 2).locator('.t-chk')).toBeChecked();
  await drag(page, row(page, 1).locator('.t-chk'), row(page, 4));
  await drag(page, row(page, 1).locator('[data-role="transfer-status"]'), row(page, 4));
  expect(queue.log.moves.length).toBe(2);
  expect(queue.log.details).toEqual([2]);
});

test('U2 a drag crosses page boundaries to the global anchor; partial views never drag', async ({ page }) => {
  await page.setViewportSize({width: 1500, height: 640});
  const items = Array.from({length: 30}, (_, index) => transfer(index + 1));
  const queue = await boot(page, items);
  const firstPage = await renderedIds(page);
  expect(firstPage[0]).toBe(1);
  expect(firstPage.length).toBeLessThan(30);
  await page.locator('#torrent-page-btns [aria-label="Next page"]').click();
  await expect.poll(async () => (await renderedIds(page))[0]).toBe(firstPage.length + 1);
  // The page turn slides the list in; its rows take a gesture once they have
  // arrived (the slide moves the surface's content, so its own end is the fence).
  await page.waitForFunction(() => !document.querySelector('#view-torrents .dp-motion-surface'));
  const moved = (await renderedIds(page))[1];

  // Hold the drag over the previous-page zone: page one replaces page two
  // while the same gesture continues, then drop before the first transfer.
  await drag(page, nameCell(page, moved), page.locator('#t-tbody tr[data-torrent-id]').first(), {
    hold: async () => {
      const zone = page.locator('.dp-queue-page-zone[data-direction="previous"]');
      await expect(zone).toBeVisible();
      const box = await zone.boundingBox();
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, {steps: 6});
      await expect.poll(async () => (await renderedIds(page))[0], {timeout: 4000}).toBe(1);
      // A refresh during the gesture never replaces the rows it is held over.
      await page.locator('#t-tbody tr[data-torrent-id]').first().evaluate(node => { node.dataset.marker = 'held'; });
      await page.evaluate(() => window.loadTorrents());
      await expect(page.locator('#t-tbody tr[data-marker="held"]')).toHaveCount(1);
    },
    toY: 0.25,
  });
  await expect.poll(() => queue.log.moves).toEqual([{id: moved, before_id: 1, after_id: null}]);
  await expect.poll(() => queue.order()[0]).toBe(moved);
  await expect.poll(async () => (await renderedIds(page))[0]).toBe(moved);

  // Newest, a status filter and a search are partial views: nothing drags.
  await page.locator('#torrent-order').selectOption('newest');
  await expect.poll(() => queue.page().order).toBeUndefined();
  await expect(page.locator('#t-tbody .dp-queue-grip')).toHaveCount(0);
  const newestFirst = (await renderedIds(page))[0];
  await drag(page, nameCell(page, newestFirst), page.locator('#t-tbody tr[data-torrent-id]').nth(3));
  await page.locator('#torrent-order').selectOption('queue');
  await expect.poll(() => queue.page().order).toBe('queue');
  await page.locator('#torrent-search').fill('item 0');
  await expect.poll(() => queue.page().search).toBe('item 0');
  await expect(page.locator('#t-tbody .dp-queue-grip')).toHaveCount(0);
  expect(queue.log.moves.length).toBe(1);
});

test('U2 the keyboard reorders through the same move, across a page boundary', async ({ page }) => {
  await page.setViewportSize({width: 1500, height: 640});
  const queue = await boot(page, Array.from({length: 30}, (_, index) => transfer(index + 1)));
  const perPage = (await renderedIds(page)).length;
  const last = (await renderedIds(page)).at(-1);
  await nameCell(page, last).focus();
  await expect(nameCell(page, last)).toBeFocused();
  await page.keyboard.press('Space');
  await expect(page.locator('#dp-queue-status')).toContainText(/picked up/i);
  await page.keyboard.press('ArrowDown');
  await expect.poll(async () => (await renderedIds(page))[0], {timeout: 4000}).toBe(perPage + 1);
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Enter');
  // One position per arrow: past the first row of the next page, then the second.
  await expect.poll(() => queue.log.moves).toEqual([{id: last, before_id: perPage + 3, after_id: perPage + 2}]);
  // Escape abandons a held transfer without any request.
  const another = (await renderedIds(page))[2];
  await nameCell(page, another).focus();
  await page.keyboard.press('Space');
  await page.keyboard.press('ArrowUp');
  await page.keyboard.press('Escape');
  await expect(page.locator('#dp-queue-status')).toContainText(/cancel/i);
  expect(queue.log.moves.length).toBe(1);
});

test('U3 a reorder that would restart active work asks first; Cancel changes nothing', async ({ page }) => {
  const victim = {transfer_id: 1, name: 'Queue item 01', attempt_ids: ['attempt-1'], completed_bytes: 2097152,
                  total_bytes: 4194304, estimated_percentage: null};
  const queue = await boot(page, [transfer(1, {status: 'downloading'}), transfer(2), transfer(3)],
    {victims: {1: victim}});
  const before = await renderedIds(page);

  await drag(page, nameCell(page, 3), row(page, 1));
  const dialog = page.locator('.dp-modal-dialog.dp-queue-restart-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.locator('.dp-modal-title')).toHaveText('Transfer Restart Required');
  const hints = dialog.locator('.dp-modal-message--center');
  await expect(hints).toHaveCount(2);
  await expect(hints.first()).toHaveText(
    'Reordering the queue will interrupt 1 active transfer that cannot resume its current download.');
  await expect(hints.last()).toHaveText(
    'Downloaded progress for this transfer will be lost. Are you sure you want to proceed?');
  for (const hint of await hints.all()) expect(await hint.evaluate(n => getComputedStyle(n).textAlign)).toBe('center');
  const entry = dialog.locator('.dp-queue-restart-item');
  await expect(entry).toHaveCount(1);
  await expect(entry).toContainText('Queue item 01');
  await expect(entry).toContainText('This transfer will restart downloading from the beginning when it becomes eligible again.');
  for (const node of [dialog.locator('.dp-modal-title'), entry]) {
    expect(['left', 'start']).toContain(await node.evaluate(n => getComputedStyle(n).textAlign));
  }
  const footer = await dialog.locator('.dp-modal-footer').evaluate(n => getComputedStyle(n).justifyContent);
  expect(footer).toBe('flex-end');
  await expect(dialog.locator('[data-modal-accept]')).toHaveText('Confirm Reorder');
  await expect(dialog.locator('[data-modal-accept]')).toHaveClass(/\bbtn-warn\b/);

  await dialog.locator('[data-modal-cancel]').click();
  await expect(dialog).toBeHidden();
  expect(queue.log.moves).toEqual([{id: 3, before_id: 1, after_id: null}]);
  await expect.poll(() => renderedIds(page)).toEqual(before);
  await expect(nameCell(page, 3)).toBeFocused();

  // Confirm sends the same move with the reviewed attempt; a changed victim
  // is shown again before anything moves.
  queue.setVictims({1: {...victim, attempt_ids: ['attempt-1b']}});
  await drag(page, nameCell(page, 3), row(page, 1));
  await expect(dialog).toBeVisible();
  queue.setVictims({1: {...victim, attempt_ids: ['attempt-1c']}});
  await dialog.locator('[data-modal-accept]').click();
  await expect.poll(() => queue.log.moves.length).toBe(3);
  expect(queue.log.moves[2]).toEqual({id: 3, before_id: 1, after_id: null, confirm_restart: ['attempt-1b']});
  await expect(dialog).toBeVisible();
  await dialog.locator('[data-modal-accept]').click();
  await expect.poll(() => queue.log.moves.length).toBe(4);
  expect(queue.log.moves[3].confirm_restart).toEqual(['attempt-1c']);
  await expect(dialog).toBeHidden();
  await expect.poll(() => renderedIds(page)).toEqual([3, 1, 2]);
});

test('U5 dragging a restart-only download below waiting work asks first, like dragging the waiting one up', async ({ page }) => {
  const victim = {transfer_id: 1, name: 'Queue item 01', attempt_ids: ['attempt-1'], completed_bytes: 2097152,
                  total_bytes: 4194304, estimated_percentage: null};
  const queue = await boot(page, [transfer(1, {status: 'downloading'}), transfer(2), transfer(3)],
    {victims: {1: victim}});
  const before = await renderedIds(page);
  await drag(page, nameCell(page, 1), row(page, 2), {toY: 0.85});
  const dialog = page.locator('.dp-modal-dialog.dp-queue-restart-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.locator('.dp-queue-restart-item')).toContainText('Queue item 01');
  await dialog.locator('[data-modal-cancel]').click();
  await expect(dialog).toBeHidden();
  expect(queue.log.moves).toEqual([{id: 1, before_id: 3, after_id: 2}]);
  await expect.poll(() => renderedIds(page)).toEqual(before);

  await drag(page, nameCell(page, 1), row(page, 2), {toY: 0.85});
  await expect(dialog).toBeVisible();
  await dialog.locator('[data-modal-accept]').click();
  await expect.poll(() => queue.log.moves.length).toBe(3);
  expect(queue.log.moves[2]).toEqual({id: 1, before_id: 3, after_id: 2, confirm_restart: ['attempt-1']});
  await expect.poll(() => renderedIds(page)).toEqual([2, 1, 3]);
});

test('U6 a promoted transfer waiting on an unconfirmed restart offers Start Now through the same confirmation', async ({ page }) => {
  const victim = {transfer_id: 2, name: 'Queue item 02', attempt_ids: ['attempt-2b'], completed_bytes: 1048576,
                  total_bytes: 4194304, estimated_percentage: null};
  const queue = await boot(page, [transfer(1, {queue_restart_consent: true}), transfer(2, {status: 'downloading'}),
    transfer(3)], {consent: {1: victim}});
  const before = await renderedIds(page);
  await expect(page.locator('#t-tbody .dp-queue-consent')).toHaveCount(1);
  const start = row(page, 1).locator('td[data-role="transfer-status"] .dp-queue-consent');
  await expect(start).toHaveText('Start Now');

  await start.click();
  const dialog = page.locator('.dp-modal-dialog.dp-queue-restart-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.locator('.dp-queue-restart-item')).toContainText('Queue item 02');
  await dialog.locator('[data-modal-cancel]').click();
  await expect(dialog).toBeHidden();
  expect(queue.log.moves).toEqual([{id: 1, reaffirm: true}]);
  expect(queue.log.details).toEqual([]);
  await expect.poll(() => renderedIds(page)).toEqual(before);
  await expect(start).toBeVisible();

  await start.click();
  await expect(dialog).toBeVisible();
  await dialog.locator('[data-modal-accept]').click();
  await expect.poll(() => queue.log.moves.length).toBe(3);
  expect(queue.log.moves[2]).toEqual({id: 1, reaffirm: true, confirm_restart: ['attempt-2b']});
  await expect(dialog).toBeHidden();
  await expect(page.locator('#t-tbody .dp-queue-consent')).toHaveCount(0);
  expect(queue.log.details).toEqual([]);
  await expect.poll(() => renderedIds(page)).toEqual(before);
});

test('U4 selection sits after Actions at every width; an unhovered Remove is quiet', async ({ page }) => {
  await boot(page, [transfer(1, {status: 'downloading', progress: 40}), transfer(2)]);
  // 1.0.14: seven columns (the provider identity is Name's second line), the
  // selection column last, after Actions, by position and by name.
  const head = page.locator('#view-torrents thead th');
  await expect(head).toHaveCount(7);
  await expect(head.nth(0)).toHaveText(/Name/);
  await expect(head.nth(5)).toHaveText('Actions');
  await expect(head.nth(5)).toHaveClass(/\bdp-downloads-action-cell\b/);
  await expect(head.nth(6).locator('#chk-all')).toHaveCount(1);
  await expect(head.nth(6)).toHaveClass(/\bdp-downloads-select-cell\b/);
  await expect(row(page, 1).locator('td').nth(6).locator('.t-chk')).toHaveCount(1);
  await expect(row(page, 1).locator('td').nth(5).locator('.actions')).toHaveCount(1);

  const visibleColumns = () => row(page, 1).locator('td').evaluateAll(cells => cells
    .map((cell, index) => (getComputedStyle(cell).display === 'none' ? null : index)).filter(index => index !== null));
  // The full table while it holds every column (1500, 1300); the compact
  // table sets Added aside (1000); mobile is the Transfer Summary, Actions and
  // selection (600; 1.0.14 handheld recomposition -- formerly a BLOCKED MOBILE
  // case expecting Name, Status, Date, Actions and selection there).
  for (const [width, expected] of [[1500, [0, 1, 2, 3, 4, 5, 6]], [1300, [0, 1, 2, 3, 4, 5, 6]],
                                   [1000, [0, 1, 2, 3, 5, 6]], [600, [0, 5, 6]]]) {
    await page.setViewportSize({width, height: 900});
    await expect.poll(visibleColumns).toEqual(expected);
    // Both cells measured in ONE read of the row as it stands: a live list
    // refresh between two separate reads could replace the row in between.
    // The row is looked up inside that same read: a locator resolves its node
    // in one round trip and evaluates it in the next, and the capacity refresh
    // a resize schedules replaces every row in between -- a detached row
    // measures 0 x 0 at x 0, which is no rendering anyone sees.
    const {actions, select} = await page.evaluate(() => {
      const node = document.querySelector('#t-tbody tr[data-torrent-id="1"]');
      const box = cell => { const r = cell.getBoundingClientRect(); return {x: r.x, width: r.width}; };
      const cells = node.querySelectorAll(':scope > td');
      return {actions: box(cells[5]), select: box(cells[6])};
    });
    expect(select.x).toBeGreaterThanOrEqual(actions.x + actions.width - 1);
    expect(select.width).toBeGreaterThanOrEqual(24);
  }

  await page.setViewportSize({width: 1500, height: 900});
  const remove = row(page, 1).locator('button', {hasText: 'Remove'});
  // Read the Remove the page shows now (looked up in the same read, as above):
  // the capacity refresh that follows the resize back replaces the row.
  const styles = async () => page.evaluate(() => {
    const n = Array.from(document.querySelectorAll('#t-tbody tr[data-torrent-id="1"] button'))
      .find(button => button.textContent.includes('Remove'));
    const s = getComputedStyle(n);
    return {color: s.color, background: s.backgroundColor, border: s.borderTopColor, width: n.offsetWidth,
            height: n.offsetHeight, transition: s.transitionProperty};
  });
  const token = name => page.evaluate(variable => {
    const probe = document.createElement('span');
    probe.style.color = `var(${variable})`;
    document.body.appendChild(probe);
    const color = getComputedStyle(probe).color;
    probe.remove();
    return color;
  }, name);
  for (const light of [false, true]) {
    await page.evaluate(on => document.body.classList.toggle('light', on), light);
    await page.mouse.move(0, 0);
    await page.evaluate(() => document.activeElement?.blur());
    const danger = await token('--dp-state-error');
    // Rest: the softer secondary text; only the colour differs from before.
    await expect.poll(async () => (await styles()).color).toBe(await token('--dp-text-secondary'));
    const quiet = await styles();
    expect(quiet.color).not.toBe(await token('--dp-text-primary'));
    expect(quiet.color).not.toBe(danger);
    await remove.hover();
    // The settled hover state (the shared button material transitions colour).
    await expect.poll(async () => (await styles()).color).toBe(danger);
    const hovered = await styles();
    expect([hovered.width, hovered.height, hovered.transition]).toEqual([quiet.width, quiet.height, quiet.transition]);
    // Only the row Remove changes: the bulk Delete keeps its red text.
    const bulk = await page.locator('.dp-downloads-bulk-action--delete').evaluate(n => getComputedStyle(n).color);
    expect(bulk).toBe(hovered.color);
    // Keyboard focus is as loud as hover.
    await page.mouse.move(0, 0);
    await row(page, 1).locator('button', {hasText: /Pause|Resume/}).first().focus();
    await page.keyboard.press('Tab');
    await expect(remove).toBeFocused();
    await expect.poll(async () => (await styles()).color).toBe(danger);
  }
});

/* The browser's own context menu is suppressed only where nothing native is
 * lost; ``prevented`` reads what reached the window. */
async function recordContextMenus(page) {
  await page.evaluate(() => {
    window.__menus = [];
    window.addEventListener('contextmenu', event => {
      window.__menus.push({prevented: event.defaultPrevented, tag: event.target.tagName});
    });
  });
  return async () => page.evaluate(() => window.__menus.map(entry => entry.prevented));
}
const rowMenu = page => page.locator('.dp-row-menu');

test('U7 right-click anywhere on an unfinished row offers Move to Top: one global move, back to the queue top', async ({ page }) => {
  await page.setViewportSize({width: 1500, height: 640});
  const items = Array.from({length: 30}, (_, index) => transfer(index + 1));
  items.push(transfer(31, {status: 'completed', progress: 100, queue_member: false}));
  const queue = await boot(page, items);
  const prevented = await recordContextMenus(page);
  const perPage = (await renderedIds(page)).length;
  await page.locator('#torrent-page-btns [aria-label="Next page"]').click();
  await expect.poll(async () => (await renderedIds(page))[0]).toBe(perPage + 1);
  const target = (await renderedIds(page))[2];
  await row(page, target).locator('.t-chk').check();
  const gets = queue.log.gets.length;

  // Outside the Name cell: the Size cell.
  await row(page, target).locator('.dp-downloads-size-cell').click({button: 'right'});
  await expect(rowMenu(page)).toHaveCount(1);
  await expect(rowMenu(page)).toBeVisible();
  await expect(rowMenu(page)).toHaveAttribute('role', 'menu');
  const items_ = rowMenu(page).locator('[role="menuitem"]');
  await expect(items_).toHaveCount(1);
  await expect(items_).toHaveText('Move to Top');
  expect(await prevented()).toEqual([true]);
  // No Details, no checkbox change, no drag.
  expect(queue.log.details).toEqual([]);
  await expect(row(page, target).locator('.t-chk')).toBeChecked();
  await expect(page.locator('#t-tbody .dp-queue-drag-source')).toHaveCount(0);
  // The menu stays inside the viewport.
  const menu = await box(rowMenu(page));
  expect(menu.x + menu.width).toBeLessThanOrEqual(1500);
  expect(menu.y + menu.height).toBeLessThanOrEqual(640);

  await items_.click();
  await expect.poll(() => queue.log.tops).toEqual([{ids: [target]}]);
  await expect(rowMenu(page)).toHaveCount(0);
  expect(queue.log.moves).toEqual([]);
  // The unfiltered Priority view returns to its first page, where it now leads.
  await expect.poll(() => queue.page().offset).toBe('0');
  await expect.poll(async () => (await renderedIds(page))[0]).toBe(target);
  expect(queue.log.gets.length).toBeGreaterThan(gets);

  // A settled row has no queue command and no browser menu.
  await page.locator('#torrent-order').selectOption('newest');
  await expect.poll(() => queue.page().order).toBeUndefined();
  // The settled row is on whichever page the measured capacity puts it in
  // the Newest order: turn to it.
  for (let turns = 0; await row(page, 31).count() === 0; turns++) {
    expect(turns).toBeLessThan(10);
    const first = (await renderedIds(page))[0];
    await page.locator('#torrent-page-btns [aria-label="Next page"]').click();
    await expect.poll(async () => (await renderedIds(page))[0]).not.toBe(first);
  }
  await row(page, 31).locator('.dp-downloads-size-cell').click({button: 'right'});
  await expect(rowMenu(page)).toHaveCount(0);
  expect((await prevented()).at(-1)).toBe(true);
  // Plain chrome: suppressed, no DP menu.
  await page.locator('#view-torrents .dp-downloads-bulk-count, #view-torrents h2, #view-torrents .card').first().click({button: 'right', position: {x: 4, y: 4}});
  await expect(rowMenu(page)).toHaveCount(0);
  expect((await prevented()).at(-1)).toBe(true);
});

test('U8 native menus stay for editing, selected text and links; the row menu is keyboard-operable', async ({ page }) => {
  const queue = await boot(page, [transfer(1, {status: 'downloading', progress: 40}), transfer(2), transfer(3),
    transfer(4, {status: 'completed', progress: 100, queue_member: false})]);
  const prevented = await recordContextMenus(page);

  await page.locator('#torrent-search').click({button: 'right'});
  await page.evaluate(() => {
    const host = document.querySelector('#view-torrents .card');
    host.insertAdjacentHTML('afterbegin', '<div id="probe-edit" contenteditable="true">editable <b id="probe-edit-child">child</b></div><textarea id="probe-text">t</textarea><a id="probe-link" href="https://example.invalid/help"><span id="probe-link-child">link</span></a>');
  });
  for (const selector of ['#probe-edit-child', '#probe-text', '#probe-link-child']) {
    await page.locator(selector).click({button: 'right'});
  }
  expect(await prevented()).toEqual([false, false, false, false]);
  await expect(rowMenu(page)).toHaveCount(0);
  await page.evaluate(() => document.querySelectorAll('#probe-edit,#probe-text,#probe-link').forEach(n => n.remove()));
  // The probes resized the card: let the list settle its capacity first.
  await settled(page, queue);

  // Selected text in the row: the native Copy menu.
  // Right-click on the selected text itself (the draggable Name cell is not
  // selectable; the Size cell is).
  const text = await row(page, 2).locator('.dp-downloads-size-cell').evaluate(node => {
    const range = document.createRange();
    range.selectNodeContents(node);
    getSelection().removeAllRanges();
    getSelection().addRange(range);
    const rect = range.getBoundingClientRect();
    return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
  });
  await page.mouse.click(text.x, text.y, {button: 'right'});
  await expect(rowMenu(page)).toHaveCount(0);
  expect((await prevented()).at(-1)).toBe(false);
  // Text selected elsewhere never disables the row menu.
  await page.locator('#view-torrents .dp-downloads-tracked, #view-torrents h2, #bulk-count').first().evaluate(node => {
    const range = document.createRange(); range.selectNodeContents(node); getSelection().removeAllRanges(); getSelection().addRange(range);
  });
  await row(page, 2).locator('[data-role="transfer-progress"]').click({button: 'right'});
  await expect(rowMenu(page)).toBeVisible();
  expect((await prevented()).at(-1)).toBe(true);
  // Escape closes it and returns focus to the row.
  await page.keyboard.press('Escape');
  await expect(rowMenu(page)).toHaveCount(0);
  await expect(row(page, 2)).toBeFocused();

  // Keyboard: Shift+F10 and the Context Menu key on a focused row.
  for (const key of ['Shift+F10', 'ContextMenu']) {
    await row(page, 3).focus();
    await page.keyboard.press(key);
    await expect(rowMenu(page)).toBeVisible();
    await expect(rowMenu(page).locator('[role="menuitem"]')).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(rowMenu(page)).toHaveCount(0);
    await expect(row(page, 3)).toBeFocused();
  }
  // Outside pointer and scrolling dismiss; only one menu ever exists.
  await row(page, 3).locator('.dp-downloads-size-cell').click({button: 'right'});
  await row(page, 2).locator('.dp-downloads-size-cell').click({button: 'right'});
  await expect(rowMenu(page)).toHaveCount(1);
  await page.locator('#torrent-pagination').click({position: {x: 2, y: 2}});
  await expect(rowMenu(page)).toHaveCount(0);
  await row(page, 3).locator('.dp-downloads-size-cell').click({button: 'right'});
  await page.evaluate(() => window.dispatchEvent(new Event('scroll')));
  await expect(rowMenu(page)).toHaveCount(0);
  // Enter activates the command from the keyboard; the row never opened Details.
  await row(page, 3).focus();
  await page.keyboard.press('Shift+F10');
  await page.keyboard.press('Enter');
  await expect.poll(() => queue.log.tops).toEqual([{ids: [3]}]);
  expect(queue.log.details).toEqual([]);
  // The Name-cell keyboard drag is unaffected.
  await nameCell(page, 1).focus();
  await page.keyboard.press('Space');
  await expect(page.locator('#dp-queue-status')).toContainText(/picked up/i);
  await page.keyboard.press('Escape');
});

test('U11 right-clicking a selection checkbox opens the row menu and leaves the selection as it was', async ({ page }) => {
  const queue = await boot(page, [transfer(1, {status: 'downloading', progress: 40}), transfer(2), transfer(3)]);
  const prevented = await recordContextMenus(page);
  await row(page, 2).locator('.t-chk').check();
  for (const [id, checked] of [[2, true], [3, false]]) {
    const box_ = row(page, id).locator('.t-chk');
    await box_.click({button: 'right'});
    await expect(rowMenu(page)).toBeVisible();
    await expect(rowMenu(page).locator('[role="menuitem"]')).toHaveText('Move to Top');
    expect((await prevented()).at(-1)).toBe(true);
    if (checked) await expect(box_).toBeChecked(); else await expect(box_).not.toBeChecked();
    await page.keyboard.press('Escape');
    await expect(rowMenu(page)).toHaveCount(0);
  }
  await expect(page.locator('#bulk-count')).toHaveText('1 Selected');
  // A text field keeps the browser's own menu.
  await page.locator('#torrent-search').click({button: 'right'});
  expect((await prevented()).at(-1)).toBe(false);
  await expect(rowMenu(page)).toHaveCount(0);
  expect(queue.log.tops).toEqual([]);
});

test('U9 Move to Top on the bulk toolbar: eligible selection only, one request, sits before Delete', async ({ page }) => {
  const items = [1, 2, 3, 4, 5, 6].map(id => transfer(id, id === 1 ? {status: 'downloading', progress: 30} : {}));
  items.push(transfer(7, {status: 'completed', progress: 100, queue_member: false}),
             transfer(8, {status: 'completed', progress: 100, queue_member: false}));
  const queue = await boot(page, items);
  const actions = page.locator('.dp-downloads-bulk-actions > *');
  const order = await actions.evaluateAll(nodes => nodes.map(node => node.dataset.defaultLabel || node.className));
  expect(order).toEqual(['Pause', 'Resume', 'Reset', 'Move to Top', 'dp-downloads-bulk-separator', 'Delete']);
  const button = page.locator('.dp-downloads-bulk-action--top');
  await expect(button.locator('svg[data-dp-lucide="arrow-up-to-line"]')).toHaveCount(1);

  // Settled-only selection: no Move to Top.
  await row(page, 7).locator('.t-chk').check();
  await row(page, 8).locator('.t-chk').check();
  await expect(page.locator('#bulk-bar')).toHaveClass(/\bvisible\b/);
  await expect(button).toBeHidden();
  // Mixed: F, B, D (checked in that order) and two settled.
  for (const id of [6, 2, 4]) await row(page, id).locator('.t-chk').check();
  await expect(button).toBeVisible();
  const heights = await actions.evaluateAll(nodes => nodes.filter(n => n.tagName === 'BUTTON').map(n => n.offsetHeight));
  expect(new Set(heights).size).toBe(1);
  await button.click();
  await expect.poll(() => queue.log.tops.length).toBe(1);
  expect(new Set(queue.log.tops[0].ids)).toEqual(new Set([2, 4, 6]));
  expect(queue.log.moves).toEqual([]);
  await expect.poll(() => renderedIds(page)).toEqual([2, 4, 6, 1, 3, 5, 7, 8]);
  // The settled rows are untouched and nothing was reset.
  expect(queue.order().slice(-2)).toEqual([7, 8]);
});

test('U10 Move to Top keeps a search, filter or Newest view and asks once for every restart it needs', async ({ page }) => {
  // The victims are not on this page; each is named as Downloads names it.
  const victims = [
    {transfer_id: 41, name: 'watch', display_name: 'Victim One [abc].webm', attempt_ids: ['attempt-1'], completed_bytes: 2097152, total_bytes: 4194304, estimated_percentage: null},
    {transfer_id: 42, name: 'watch', display_name: 'Victim Two [def].webm', attempt_ids: ['attempt-2'], completed_bytes: 1048576, total_bytes: 4194304, estimated_percentage: null},
  ];
  const queue = await boot(page, [transfer(1, {status: 'downloading'}), transfer(2, {status: 'downloading'}),
    transfer(3), transfer(4)], {topVictims: victims});
  await page.locator('#torrent-order').selectOption('newest');
  await page.locator('#torrent-search').fill('item 0');
  await expect.poll(() => queue.page().search).toBe('item 0');
  await settled(page, queue);
  for (const id of [4, 3]) await row(page, id).locator('.t-chk').check();
  await page.locator('.dp-downloads-bulk-action--top').click();

  const dialog = page.locator('.dp-modal-dialog.dp-queue-restart-dialog');
  await expect(dialog).toHaveCount(1);
  await expect(dialog).toBeVisible();
  await expect(dialog.locator('.dp-queue-restart-item')).toHaveCount(2);
  await expect(dialog.locator('.dp-queue-restart-name')).toHaveText(['Victim One [abc].webm', 'Victim Two [def].webm']);
  await expect(page.locator('#t-tbody tr[data-torrent-id="41"], #t-tbody tr[data-torrent-id="42"]')).toHaveCount(0);
  await dialog.locator('[data-modal-cancel]').click();
  await expect(dialog).toBeHidden();
  expect(queue.log.tops.length).toBe(1);
  expect(queue.order()).toEqual([1, 2, 3, 4]);
  await expect(row(page, 3).locator('.t-chk')).toBeChecked();

  await page.locator('.dp-downloads-bulk-action--top').click();
  await expect(dialog).toBeVisible();
  await dialog.locator('[data-modal-accept]').click();
  await expect.poll(() => queue.log.tops.length).toBe(3);
  expect(queue.log.tops[2].confirm_restart).toEqual(['attempt-1', 'attempt-2']);
  await expect(dialog).toBeHidden();
  expect(queue.order()).toEqual([3, 4, 1, 2]);
  // The completed bulk move ends the selection; the operator's view is kept
  // and the outcome is said, not shown by clearing it.
  await expect(page.locator('#bulk-bar')).not.toHaveClass(/\bvisible\b/);
  await expect(page.locator('#t-tbody .t-chk:checked')).toHaveCount(0);
  await expect(page.locator('#torrent-order')).toHaveValue('newest');
  await expect(page.locator('#torrent-search')).toHaveValue('item 0');
  await expect(page.locator('#dp-queue-status')).toContainText(/top of the queue/i);
  expect(queue.log.moves).toEqual([]);
});

/* The page as the operator sees it: its number, the size of the last list
 * read, and the rows in order. */
async function pageState(page, queue) {
  return {page: await page.locator('#torrent-page-btns [aria-current="page"]').textContent(),
          limit: queue.page().limit, offset: queue.page().offset, ids: await renderedIds(page)};
}
/* Let the toolbar's resize notification and any refresh it causes run out. */
async function quiet(page, queue) {
  await page.waitForTimeout(400);
  await settled(page, queue);
}

test('B1-B4 a selection holds its page: the toolbar never repaginates, live changes still show', async ({ page }) => {
  await page.setViewportSize({width: 1500, height: 640});
  const queue = await boot(page, Array.from({length: 30}, (_, index) => transfer(index + 1)));
  await page.locator('#torrent-page-btns [aria-label="Next page"]').click();
  const perPage = Number(queue.page().limit);
  await expect.poll(async () => (await renderedIds(page))[0]).toBe(perPage + 1);
  await quiet(page, queue);
  const before = await pageState(page, queue);
  const first = before.ids[0];

  // B1: the first check shows the toolbar; nothing moves.
  await row(page, first).locator('.t-chk').check();
  await expect(page.locator('#bulk-bar')).toHaveClass(/\bvisible\b/);
  await quiet(page, queue);
  expect(await pageState(page, queue)).toEqual(before);
  await expect(row(page, first).locator('.t-chk')).toBeChecked();

  // B2: more checks, select all, one unchecked: still the same page and rows;
  // the last row stays a member, reached by the table's own scrolling.
  await row(page, before.ids[1]).locator('.t-chk').check();
  await page.locator('#chk-all').check();
  await row(page, before.ids[2]).locator('.t-chk').uncheck();
  await expect(page.locator('#chk-all')).toHaveJSProperty('indeterminate', true);
  await quiet(page, queue);
  expect(await pageState(page, queue)).toEqual(before);
  const last = row(page, before.ids.at(-1));
  await last.scrollIntoViewIfNeeded();
  await expect(last).toBeInViewport();
  await expect(last.locator('.t-chk')).toBeChecked();
  // A window resize during the selection does not repaginate either.
  await page.setViewportSize({width: 1500, height: 560});
  await quiet(page, queue);
  expect(await pageState(page, queue)).toEqual(before);

  // B4: a real change on the server still arrives through the list.
  await page.evaluate(([id, anchor]) => api('PATCH', `/torrents/${id}/priority`, {before_id: anchor}),
                      [before.ids.at(-1), before.ids[0]]);
  await page.evaluate(() => window.loadTorrents());
  await expect.poll(async () => (await renderedIds(page))[0]).toBe(before.ids.at(-1));
  expect((await pageState(page, queue)).limit).toBe(before.limit);
  await expect(row(page, first).locator('.t-chk')).toBeChecked();

  // B3: clearing the selection lets the page measure itself again ...
  await page.locator('.dp-downloads-bulk-action--clear').click();
  await expect(page.locator('#bulk-bar')).not.toHaveClass(/\bvisible\b/);
  await expect.poll(() => queue.page().limit, {timeout: 4000}).not.toBe(before.limit);
  // ... and explicit navigation keeps its own rule: a new page, no selection.
  await quiet(page, queue);
  const current = await renderedIds(page);
  await row(page, current[0]).locator('.t-chk').check();
  await page.locator('#torrent-page-btns [aria-label="Previous page"]').click();
  await expect.poll(() => queue.page().offset).toBe('0');
  await expect(page.locator('#bulk-bar')).not.toHaveClass(/\bvisible\b/);
});

test('N1 Details is headed by the transfer display name, the submitted name only as fallback', async ({ page }) => {
  const queue = await boot(page, [transfer(1, {name: 'watch', display_name: 'A Real Video Title [abc123].webm'}),
                                  transfer(2, {name: 'watch'})]);
  await expect(row(page, 1).locator('.t-name')).toHaveText('A Real Video Title [abc123].webm');
  await row(page, 1).locator('.t-name').click();
  await expect.poll(() => queue.log.details).toEqual([1]);
  await expect(page.locator('#modal-title')).toHaveText('A Real Video Title [abc123].webm');
  await page.evaluate(() => closeModal());
  await row(page, 2).locator('.t-name').click();
  await expect.poll(() => queue.log.details).toEqual([1, 2]);
  await expect(page.locator('#modal-title')).toHaveText('watch');
});

async function selectionCleared(page) {
  await expect(page.locator('#bulk-bar')).not.toHaveClass(/\bvisible\b/);
  await expect(page.locator('#bulk-count')).toHaveText('');
  await expect(page.locator('#t-tbody .t-chk:checked')).toHaveCount(0);
  await expect(page.locator('#chk-all')).not.toBeChecked();
  await expect(page.locator('#chk-all')).toHaveJSProperty('indeterminate', false);
}

test('C1 a completed bulk action ends the selection and shows its result', async ({ page }) => {
  const queue = await boot(page, [1, 2, 3, 4, 5, 6].map(id => transfer(id)));
  for (const [action, ids, status] of [['pause', [2, 3], 'paused'], ['resume', [2], 'queued'], ['reset', [3], 'queued']]) {
    for (const id of ids) await row(page, id).locator('.t-chk').check();
    await page.locator(`.dp-downloads-bulk-action--${action}`).click();
    await expect.poll(() => queue.log.bulk.at(-1)?.action).toBe(action);
    await selectionCleared(page);
    for (const id of ids) await expect(row(page, id)).toHaveAttribute('data-status', status);
  }
  // Delete: the removed rows are gone, nothing stays selected, focus is not stranded.
  for (const id of [4, 5]) await row(page, id).locator('.t-chk').check();
  await page.locator('.dp-downloads-bulk-action--delete').click();
  await page.locator('.dp-modal-dialog [data-modal-accept]').click();
  await expect.poll(() => renderedIds(page)).toEqual([1, 2, 3, 6]);
  await selectionCleared(page);
  await expect(page.locator('#torrent-search')).toBeFocused();
  // Move to Top on page 1: the selection ends too.
  await row(page, 6).locator('.t-chk').check();
  await page.locator('.dp-downloads-bulk-action--top').click();
  await expect.poll(() => renderedIds(page)).toEqual([6, 1, 2, 3]);
  await selectionCleared(page);
  // A later refresh never brings a stale selection back.
  await page.evaluate(() => window.loadTorrents());
  await settled(page, queue);
  await selectionCleared(page);
});

test('C2 cancel and failure keep the selection; a pending action holds the page; completion releases it', async ({ page }) => {
  await page.setViewportSize({width: 1500, height: 640});
  const queue = await boot(page, Array.from({length: 30}, (_, index) => transfer(index + 1)));
  await page.locator('#torrent-page-btns [aria-label="Next page"]').click();
  await expect.poll(() => queue.page().offset).not.toBe('0');
  await quiet(page, queue);
  const before = await pageState(page, queue);
  const [first, second] = before.ids;
  for (const id of [first, second]) await row(page, id).locator('.t-chk').check();

  // Cancelled confirmation: nothing sent, selection kept.
  await page.locator('.dp-downloads-bulk-action--delete').click();
  await page.locator('.dp-modal-dialog [data-modal-cancel]').click();
  expect(queue.log.bulk).toEqual([]);
  // A failed request: selection kept for a retry.
  queue.setBulk({error: true});
  await page.locator('.dp-downloads-bulk-action--pause').click();
  await expect.poll(() => queue.log.bulk.length).toBe(1);
  for (const id of [first, second]) await expect(row(page, id).locator('.t-chk')).toBeChecked();
  await expect(page.locator('#bulk-bar')).toHaveClass(/\bvisible\b/);
  await expect(page.locator('#bulk-count')).toHaveText('2 Selected');

  // Pending: the page holds; completion ends the selection on the same page.
  let release;
  queue.setBulk({error: false, hold: new Promise(resolve => { release = resolve; })});
  await page.locator('.dp-downloads-bulk-action--pause').click();
  await expect.poll(() => queue.log.bulk.length).toBe(2);
  await quiet(page, queue);
  expect(await pageState(page, queue)).toEqual(before);
  release();
  queue.setBulk({hold: null});
  await expect(row(page, first)).toHaveAttribute('data-status', 'paused');
  await selectionCleared(page);
  expect((await pageState(page, queue)).offset).toBe(before.offset);
  // Capacity measurement runs again once the selection is gone.
  await page.setViewportSize({width: 1500, height: 520});
  await expect.poll(() => queue.page().limit, {timeout: 4000}).not.toBe(before.limit);

  // A bulk Move to Top from a deeper unfiltered Priority page shows the new top.
  await quiet(page, queue);
  const deep = (await renderedIds(page))[1];
  await row(page, deep).locator('.t-chk').check();
  await page.locator('.dp-downloads-bulk-action--top').click();
  await expect.poll(() => queue.page().offset).toBe('0');
  await expect.poll(async () => (await renderedIds(page))[0]).toBe(deep);
  await selectionCleared(page);
});
