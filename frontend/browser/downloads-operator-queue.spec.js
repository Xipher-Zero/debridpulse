// Downloads operator queue: Name-cell drag vs Details click, cross-page drag
// on the bounded table, view gating, the destructive-preemption confirmation,
// the selection column after Actions, and the quiet unhovered Remove.
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
async function installQueue(page, initial, {victims = {}, consent = {}} = {}) {
  let items = clone(initial);
  const log = {gets: [], moves: [], details: []};
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
  await drag(page, row(page, 1).locator('td').nth(2), row(page, 4));
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
  const head = page.locator('#view-torrents thead th');
  await expect(head).toHaveCount(8);
  await expect(head.nth(0)).toHaveText(/Name/);
  await expect(head.nth(6)).toHaveText('Actions');
  await expect(head.nth(7).locator('#chk-all')).toHaveCount(1);
  await expect(row(page, 1).locator('td').nth(7).locator('.t-chk')).toHaveCount(1);
  await expect(row(page, 1).locator('td').nth(6).locator('.actions')).toHaveCount(1);

  const visibleColumns = () => row(page, 1).locator('td').evaluateAll(cells => cells
    .map((cell, index) => (getComputedStyle(cell).display === 'none' ? null : index)).filter(index => index !== null));
  for (const [width, expected] of [[1500, [0, 1, 2, 3, 4, 5, 6, 7]], [1300, [0, 1, 2, 3, 6, 7]],
                                   [1000, [0, 1, 2, 6, 7]], [600, [0, 1, 2, 5, 6, 7]]]) {
    await page.setViewportSize({width, height: 900});
    await expect.poll(visibleColumns).toEqual(expected);
    const actions = await row(page, 1).locator('td').nth(6).boundingBox();
    const select = await row(page, 1).locator('td').nth(7).boundingBox();
    expect(select.x).toBeGreaterThanOrEqual(actions.x + actions.width - 1);
    expect(select.width).toBeGreaterThanOrEqual(24);
  }

  await page.setViewportSize({width: 1500, height: 900});
  const remove = row(page, 1).locator('button', {hasText: 'Remove'});
  const name = row(page, 1).locator('.t-name');
  const styles = async () => remove.evaluate(n => {
    const s = getComputedStyle(n);
    return {color: s.color, background: s.backgroundColor, border: s.borderTopColor};
  });
  const quiet = await styles();
  expect(quiet.color).toBe(await name.evaluate(n => getComputedStyle(n).color));
  const danger = await page.evaluate(() => {
    const probe = document.createElement('span');
    probe.style.color = 'var(--dp-state-error)';
    document.body.appendChild(probe);
    const color = getComputedStyle(probe).color;
    probe.remove();
    return color;
  });
  expect(quiet.color).not.toBe(danger);
  await remove.hover();
  // The settled hover state (the shared button material transitions colour).
  await expect.poll(async () => (await styles()).color).toBe(danger);
  const hovered = await styles();
  // Only the row Remove changes: the bulk Delete keeps its red text.
  const bulk = await page.locator('.dp-downloads-bulk-action--delete').evaluate(n => getComputedStyle(n).color);
  expect(bulk).toBe(hovered.color);
});
