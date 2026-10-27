const { test, expect } = require('@playwright/test');

/* Downloads and the Activity Log, from the table viewport out:
 *  - the Downloads table is never wider than its viewport, at any width and
 *    for either role, every column and control inside it and its own target;
 *  - an ordinary Downloads page holds only the rows its viewport shows whole,
 *    the next row starting the next page, none skipped or repeated; a
 *    selection keeps its page;
 *  - from the desktop down to the 780px floor both pages are one workspace
 *    filling the content height, with no step at 900px; the Activity Log keeps
 *    its configured page and scrolls it.
 * Mocked rows and events only; everything is read from rendered geometry. */

function row(id, o) {
  return {
    id, name: `Row ${id}`, status: 'completed', presentation_status: 'completed', presentation_label: 'Done',
    presentation_badge_status: 'completed', attention_required: false, progress: 100, active_execution_progress: null,
    active_execution_basis: null, size_bytes: 1000, source: 'magnet', hash: 'a1b2c3d4e5f60718293a4b5c6d7e8f90', label: '',
    created_at: '2025-12-28T22:54:00Z', current_source_identity: {kind: 'magnet'}, providers: [], historical_providers: [],
    delivering_provider_ids: [], input_required: null, acquisition_eta: null, access: {destroy: true}, queue_member: false, ...o,
  };
}

// The demanding rows: a Debrid-Link download with its route selector and
// candidate launcher; a row showing Pause, Retry and Remove together; a
// promoted transfer; a failure with file selection and a label; a long name.
const DEMANDING = [
  row(9701, {name: 'GF191026-DSKNYTHC2-VC38 + 3 files', status: 'downloading', presentation_status: 'downloading',
    presentation_label: 'Downloading', presentation_badge_status: 'downloading', progress: 36, active_execution_progress: 36,
    active_execution_basis: 'bytes', size_bytes: 999.9 * 1024 ** 2, acquisition_eta: {seconds: 2318, held: false},
    route_provider_id: 'debridlink', route_provider_name: 'Debrid-Link', route_switch_available: true,
    common_candidate_count: 2, group_remaining_count: 1, candidate_action_scope: 'group', candidate_action_count: 2, queue_member: true}),
  row(9702, {name: 'Multi.File.Partial.Error.Season.Pack', status: 'error', presentation_status: 'downloading',
    presentation_label: 'Downloading', presentation_badge_status: 'downloading', progress: 12, active_execution_progress: 12,
    active_execution_basis: 'bytes', size_bytes: 987.6 * 1024 ** 3, acquisition_eta: {seconds: 2318, held: false},
    active_provider_id: 'alldebrid', active_provider_name: 'AllDebrid', queue_member: true}),
  row(9703, {name: 'Queued.Promoted.Transfer.mkv', status: 'queued', presentation_status: 'queued', presentation_label: 'Queued',
    presentation_badge_status: 'queued', progress: 0, size_bytes: 77 * 1024 ** 2, active_provider_id: 'premiumize',
    active_provider_name: 'Premiumize', queue_member: true, queue_restart_consent: true}),
  row(9704, {name: 'A.Very.Long.Release.Name.With.Many.Segments.2160p.UHD.BluRay.REMUX.HDR10.DV.TrueHD.Atmos.7.1-GROUP',
    status: 'error', presentation_status: 'failed', presentation_label: 'Awaiting confirmation',
    presentation_badge_status: 'error', progress: 3, size_bytes: 1.5 * 1024 ** 3, active_provider_id: 'torbox',
    active_provider_name: 'TorBox', file_selection_affordance: 'select', label: 'movies', queue_member: true}),
  row(9705, {name: 'Paused.Transfer.mkv', status: 'paused', presentation_status: 'paused', presentation_label: 'Paused',
    presentation_badge_status: 'paused', progress: 41, size_bytes: 123.5 * 1024 ** 3, active_provider_id: 'realdebrid',
    active_provider_name: 'Real-Debrid', current_source_identity: {kind: 'link'}, queue_member: true}),
];
// Uniform rows: one row height, so a viewport's capacity is exact.
const UNIFORM = Array.from({length: 80}, (_, k) => row(8000 + k, {name: `Uniform.Row.${String(k).padStart(2, '0')}.mkv`,
  active_provider_id: 'usenet', active_provider_name: 'Usenet', current_source_identity: {kind: 'link'}}));

async function openDownloads(page, items, {width = 1560, height = 1000, readOnly = false} = {}) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  if (readOnly) {
    const readonly = authority => ({...authority, role: 'readonly',
      capabilities: ['interface', 'session', 'statistics.read', 'statistics.summary', 'transfers.read']});
    await page.route(url => url.pathname === '/app.js', async route => {
      const body = await (await route.fetch()).text();
      const marker = 'window.debridPulseAuthority=Object.freeze(';
      const [head, rest] = body.split(marker);
      const end = rest.indexOf(');');
      return route.fulfill({status: 200, contentType: 'application/javascript',
        body: head + marker + JSON.stringify(readonly(JSON.parse(rest.slice(0, end)))) + rest.slice(end)});
    });
    await page.route('**/api/auth/session', async route => route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify(readonly(await (await route.fetch()).json()))}));
  }
  const gets = [];
  await page.route('**/api/torrents**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname !== '/api/torrents') return route.fallback();
    const limit = Number(url.searchParams.get('limit')) || 25, offset = Number(url.searchParams.get('offset')) || 0;
    if (url.searchParams.has('offset')) gets.push({limit, offset});
    return route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({items: items.slice(offset, offset + limit), total: items.length})});
  });
  await page.setViewportSize({width, height});
  await page.goto('/');
  await page.evaluate(async () => { nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]')); await loadTorrents(); });
  await expect(page.locator('#t-tbody tr[data-torrent-id]').first()).toBeVisible();
  await steady(page);
  return gets;
}

// The sidebar's own transition over, a capacity check settled (its 120ms
// debounce and any refetch it makes), and two frames drawn.
async function steady(page) {
  await page.waitForFunction(() => document.getElementById('sidebar').getAnimations().every(a => a.playState !== 'running'));
  await page.waitForTimeout(300);
  await page.waitForFunction(() => !document.querySelector('#t-tbody .dp-page-placeholder-row') && document.getAnimations()
    .every(a => a.playState !== 'running' || a.effect?.getTiming().iterations === Infinity));
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}

async function resize(page, width, height) {
  await page.setViewportSize({width, height});
  await steady(page);
}

/* The Downloads table viewport as drawn. */
const table = page => page.evaluate(() => {
  const wrap = document.querySelector('#view-torrents .dp-downloads-table-wrap');
  const w = wrap.getBoundingClientRect();
  const visible = {left: w.left + wrap.clientLeft, right: w.left + wrap.clientLeft + wrap.clientWidth,
    top: w.top + wrap.clientTop, bottom: w.top + wrap.clientTop + wrap.clientHeight};
  const head = wrap.querySelector('thead').getBoundingClientRect();
  const rows = [...wrap.querySelectorAll('tbody tr[data-torrent-id]')];
  const outside = [];
  for (const cell of wrap.querySelectorAll('thead th, tbody td')) {
    if (getComputedStyle(cell).display === 'none') continue;
    const c = cell.getBoundingClientRect();
    if (c.right > visible.right + 0.5) outside.push(`${cell.className} past the viewport`);
    for (const node of cell.querySelectorAll('button, input, [role="button"], .dp-queue-grip')) {
      if (!node.getClientRects().length) continue;
      const r = node.getBoundingClientRect();
      if (r.width <= 0) continue;
      if (r.left < c.left - 0.5 || r.right > c.right + 0.5) outside.push(`${node.className || node.tagName} outside its ${cell.className.split(' ').pop()}`);
      if (r.bottom <= visible.top || r.top >= visible.bottom) continue;
      const hit = document.elementFromPoint((r.left + r.right) / 2, (r.top + r.bottom) / 2);
      if (!node.contains(hit) && !hit?.contains(node)) outside.push(`${node.className || node.tagName} covered by ${hit?.className}`);
    }
  }
  return {over: wrap.scrollWidth - wrap.clientWidth, outside: [...new Set(outside)], height: wrap.clientHeight, bottom: visible.bottom,
    head: head.bottom, ids: rows.map(r => Number(r.dataset.torrentId)),
    rowBottoms: rows.map(r => r.getBoundingClientRect().bottom), rowHeight: Math.max(0, ...rows.map(r => r.getBoundingClientRect().height)),
    names: rows.map(r => r.querySelector('.dp-downloads-name-cell')).map(c => c.scrollWidth - c.clientWidth)};
});

for (const role of ['operator', 'read-only']) {
  test(`Downloads (${role}): the table is never wider than its viewport, from 1560px to 360px, every column and control inside it and its own target`, async ({page}) => {
    test.setTimeout(90_000);
    await openDownloads(page, DEMANDING, {readOnly: role === 'read-only'});
    const report = {};
    for (const width of [1560, 1300, 1167, 1165, 1100, 1000, 900, 882, 881, 800, 781, 780, 779, 700, 600, 480, 390, 360]) {
      await resize(page, width, 1000);
      const t = await table(page);
      const found = [...t.outside];
      if (t.over > 0.5) found.push(`scrolls ${t.over}px sideways`);
      if (t.names.some(spill => spill > 0.5)) found.push('a name cell spills');
      if (found.length) report[width] = found;
    }
    expect(report).toEqual({});
    // The Size beside the name (a table narrower than 763px, at tablet width) is whole in its cell.
    await resize(page, 800, 1000);
    const size = page.locator('#t-tbody .dp-downloads-size').first();
    await expect(size).toBeVisible();
    const cell = await page.locator('#t-tbody .dp-downloads-name-cell').first().boundingBox();
    const box = await size.boundingBox();
    expect(box.x + box.width).toBeLessThanOrEqual(cell.x + cell.width + 0.5);
  });
}

test('an ordinary Downloads page holds only the rows its viewport shows whole, at every height, below 901px as above it', async ({page}) => {
  test.setTimeout(120_000);
  await openDownloads(page, UNIFORM);
  const report = [];
  for (const width of [1560, 901, 900, 780]) {
    for (const height of [662, 673, 717, 728, 783, 838, 904, 959, 1000]) {
      await resize(page, width, height);
      const t = await table(page);
      const whole = t.rowBottoms.filter(bottom => bottom <= t.bottom + 0.5).length;
      if (whole !== t.ids.length) report.push(`${width}x${height}: ${t.ids.length} rows, ${whole} whole`);
      // And no room left for another whole row.
      if (t.bottom - (t.rowBottoms.at(-1) ?? t.head) >= t.rowHeight + 0.5) report.push(`${width}x${height}: room for another row`);
    }
  }
  expect(report).toEqual([]);
});

test('the row displaced by a smaller viewport opens the next page, and a resize keeps the first row shown: none skipped or repeated', async ({page}) => {
  await openDownloads(page, UNIFORM, {height: 1000});
  const first = await table(page);
  await page.locator('#torrent-page-btns [aria-label="Next page"]').click();
  await steady(page);
  const second = await table(page);
  expect(second.ids[0], 'the next page starts after the last row shown').toBe(first.ids.at(-1) + 1);
  expect(second.ids).toEqual(second.ids.map((_, k) => second.ids[0] + k));
  // A shorter window: the page shows the row that was first, and continues from it in order.
  const shown = second.ids[0];
  await resize(page, 1560, 760);
  const shorter = await table(page);
  expect(shorter.ids).toContain(shown);
  expect(shorter.ids).toEqual(shorter.ids.map((_, k) => shorter.ids[0] + k));
  await page.locator('#torrent-page-btns [aria-label="Next page"]').click();
  await steady(page);
  const after = await table(page);
  expect(after.ids[0]).toBe(shorter.ids.at(-1) + 1);
  // A search narrows the list: still only whole rows.
  await page.locator('#torrent-search').fill('Uniform.Row.1');
  await page.waitForTimeout(600);
  await steady(page);
  const searched = await table(page);
  expect(searched.rowBottoms.every(bottom => bottom <= searched.bottom + 0.5)).toBe(true);
});

test('a selection keeps its page: a smaller viewport scrolls it rather than repaginating, and clearing it pages whole rows again', async ({page}) => {
  const gets = await openDownloads(page, UNIFORM, {height: 1000});
  const before = await table(page);
  await page.locator('#t-tbody tr[data-torrent-id] .dp-downloads-select-cell input[type="checkbox"]').first().check();
  await steady(page);
  const asked = gets.length;
  await resize(page, 1560, 760);
  const held = await table(page);
  expect(gets.length, 'no page fetched while the selection holds it').toBe(asked);
  expect(held.ids).toEqual(before.ids);
  await page.evaluate(() => clearSelection());
  await steady(page);
  const cleared = await table(page);
  expect(gets.length).toBeGreaterThan(asked);
  expect(cleared.ids[0]).toBe(before.ids[0]);
  expect(cleared.rowBottoms.every(bottom => bottom <= cleared.bottom + 0.5)).toBe(true);
});

async function openActivity(page, {width, height}) {
  await page.route('**/api/events/stream', route => route.abort());
  const limits = [];
  await page.route('**/api/events?**', async route => {
    const q = Object.fromEntries(new URL(route.request().url()).searchParams);
    limits.push(q.limit);
    const limit = Number(q.limit) || 100, snapshot = q.snapshot ? Number(q.snapshot) : 260, top = q.before ? Number(q.before) - 1 : snapshot;
    const items = [];
    for (let id = top; id > 0 && items.length < limit; id--) items.push({id, severity: 'info', category: 'transfer', type: 'transfer.synthetic',
      message: `Event ${id}`, name: `fixture-${id}`, transfer_id: id, occurred_at: '2026-10-09T12:00:00Z'});
    const last = items.at(-1)?.id ?? 0;
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({items, limit, snapshot,
      has_more: last > 1, next_before: last > 1 ? last : null, more_after_next: last - 1 > limit, newer_available: false,
      window_start: null, search: {text: 'none', pending: 0, complete: true}})});
  });
  await page.setViewportSize({width, height});
  await page.goto('/');
  await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="events"]')));
  await expect(page.locator('#event-list .dp-activity-row').first()).toBeVisible();
  return limits;
}

/* A page's workspace: its card, its data viewport and the content box. */
const workspace = (page, view) => page.evaluate(view => {
  const v = document.getElementById(view), card = v.querySelector(':scope > .card');
  const data = v.querySelector(view === 'view-torrents' ? '.dp-downloads-table-wrap' : '.dp-activity-list');
  const content = document.getElementById('content'), cs = getComputedStyle(content);
  const c = card.getBoundingClientRect(), d = data.getBoundingClientRect(), box = content.getBoundingClientRect();
  return {cardTop: c.top, cardBottom: c.bottom, dataBottom: d.bottom, clientHeight: data.clientHeight, scrollHeight: data.scrollHeight,
    maxHeight: getComputedStyle(data).maxHeight, contentBottom: box.bottom - parseFloat(cs.paddingBottom),
    rows: data.querySelectorAll('.dp-activity-row').length};
}, view);

for (const height of [1000, 700]) {
  test(`Downloads and the Activity Log are one workspace from 902px to 780px at ${height}px high: no step at 900px, the card filling the content box`, async ({page}) => {
    test.setTimeout(90_000);
    await openDownloads(page, UNIFORM, {width: 902, height});
    const report = [];
    for (const [view, open] of [['view-torrents', null], ['view-events', async () => openActivity(page, {width: 902, height})]]) {
      if (open) await open();
      let reference = null;
      for (const width of [902, 901, 900, 899, 898, 850, 800, 781, 780]) {
        await resize(page, width, height);
        const w = await workspace(page, view);
        reference ??= w;
        const at = `${view} ${width}x${height}`;
        if (w.maxHeight !== 'none') report.push(`${at}: data viewport capped (${w.maxHeight})`);
        if (Math.abs(w.cardBottom - w.contentBottom) > 0.5) report.push(`${at}: card ends at ${w.cardBottom}, content at ${w.contentBottom}`);
        if (Math.abs(w.cardBottom - reference.cardBottom) > 0.5 || Math.abs(w.dataBottom - reference.dataBottom) > 0.5) report.push(`${at}: bottom moved`);
        if (view === 'view-events' && (w.rows !== 100 || w.scrollHeight <= w.clientHeight)) report.push(`${at}: ${w.rows} events, scrollable ${w.scrollHeight > w.clientHeight}`);
      }
      // The handheld page keeps its own bounded list.
      await resize(page, 779, height);
      expect((await workspace(page, view)).maxHeight, `${view} handheld`).not.toBe('none');
    }
    expect(report).toEqual([]);
  });
}

test('the Activity Log keeps its configured page size at every width: 50, 100 and 250 events, scrolled, never trimmed to the view', async ({page}) => {
  for (const size of [50, 250]) {
    await page.unrouteAll({behavior: 'ignoreErrors'});
    const limits = await openActivity(page, {width: 1300, height: 800});
    await page.evaluate(size => { settingsData.activity_log_page_size = size; }, size);
    await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]')));
    await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="events"]')));
    await expect.poll(() => limits.at(-1)).toBe(String(size));
    for (const width of [1300, 900, 780]) {
      await page.setViewportSize({width, height: 800});
      await page.waitForTimeout(400);
      const w = await workspace(page, 'view-events');
      expect(w.rows, `${size} at ${width}px`).toBe(size);
      expect(w.scrollHeight).toBeGreaterThan(w.clientHeight);
    }
  }
});
