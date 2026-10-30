const { test, expect } = require('@playwright/test');

/* DP 1.0.14 Downloads: one table composition from the desktop to the tablet
 * (780px), Download Activity's, with Downloads' own columns. One Name column
 * (the name, its secondary bar, and its source, provider and candidate
 * controls beneath; no Provider / Source column), Status, Progress, Size,
 * Added, Actions and the selection column last. Where the table itself is
 * narrower than 1166px its badges and row actions show their glyph alone
 * (DP tooltips say them) and Added steps aside; narrower than 763px, Size
 * moves to the right end of the name's metadata row. The switches are the
 * table's, so each case finds them by the table's own width. Mocked rows: no
 * backend state is read or written. */

const COMPACT = 1166;     // the table is compact below this width
const RELOCATE = 763;     // and Size moves beside the name below this one

function row(id, o) {
  return {
    id, name: `Row ${id}`, status: 'completed', presentation_status: 'completed', presentation_label: 'Done',
    presentation_badge_status: 'completed', attention_required: false, progress: 100, active_execution_progress: null,
    active_execution_basis: null, size_bytes: 1000, source: 'magnet', hash: 'a1b2c3d4e5f60718293a4b5c6d7e8f90', label: '',
    created_at: '2025-12-28T22:54:00Z', current_source_identity: {kind: 'magnet'}, providers: [], historical_providers: [],
    delivering_provider_ids: [], input_required: null, acquisition_eta: null, access: {destroy: true}, queue_member: false, ...o,
  };
}

// The demanding rows: an active Debrid-Link download with its route selector, a
// candidate launcher and its name bar; a row showing every button its states allow
// together (a failed transfer one of whose files still downloads: Pause, Retry and
// Remove); a promoted transfer with Start Now; a failure with file selection and a label.
const ITEMS = [
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
  row(9704, {name: 'Failed.Download.zip', status: 'error', presentation_status: 'failed', presentation_label: 'Failed',
    presentation_badge_status: 'error', progress: 3, size_bytes: 1.5 * 1024 ** 3, active_provider_id: 'torbox',
    active_provider_name: 'TorBox', file_selection_affordance: 'select', label: 'movies', queue_member: true}),
  row(9705, {name: 'Paused.Transfer.mkv', status: 'paused', presentation_status: 'paused', presentation_label: 'Paused',
    presentation_badge_status: 'paused', progress: 41, size_bytes: 123.5 * 1024 ** 3, active_provider_id: 'realdebrid',
    active_provider_name: 'Real-Debrid', current_source_identity: {kind: 'link'}, queue_member: true}),
  row(9706, {name: 'Completed.Item.mkv', size_bytes: 987.6 * 1024 ** 4, active_provider_id: 'usenet', active_provider_name: 'Usenet',
    current_source_identity: {kind: 'link'}}),
];

const ROUTE = {transfer_id: 9701, current_provider_id: 'debridlink', current_provider_name: 'Debrid-Link', switchable: true,
  providers: [
    {provider_id: 'debridlink', provider_name: 'Debrid-Link', status: 'current', selectable: false, reason: null, readiness: null},
    {provider_id: 'alldebrid', provider_name: 'AllDebrid', status: 'available', selectable: true, reason: null, readiness: null}]};

// Serves ``items`` (a function, read on every request) as the server-ordered list, paged by limit/offset.
async function open(page, items, {total, readOnly = false, log = {gets: [], moves: [], details: []}, hold} = {}) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  if (readOnly) {
    // A ReadOnly User: the server's own role and capabilities for it, in the
    // authority it prepends to the application bundle and in the session.
    const readonly = authority => ({...authority, role: 'readonly',
      capabilities: ['interface', 'session', 'statistics.read', 'statistics.summary', 'transfers.read']});
    await page.route(url => url.pathname === '/app.js', async route => {
      const body = await (await route.fetch()).text();
      const marker = 'window.debridPulseAuthority=Object.freeze(';
      const [head, rest] = body.split(marker);
      const end = rest.indexOf(');');
      const authority = readonly(JSON.parse(rest.slice(0, end)));
      return route.fulfill({status: 200, contentType: 'application/javascript',
        body: head + marker + JSON.stringify(authority) + rest.slice(end)});
    });
    await page.route('**/api/auth/session', async route => route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify(readonly(await (await route.fetch()).json()))}));
  }
  await page.route('**/api/torrents**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/torrents/9701/route') {
      return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(ROUTE)});
    }
    const move = url.pathname.match(/^\/api\/torrents\/(\d+)\/priority$/);
    if (move) {
      log.moves.push({id: Number(move[1]), ...route.request().postDataJSON()});
      return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({outcome: 'moved'})});
    }
    if (url.pathname !== '/api/torrents') return route.fallback();
    const all = items();
    const limit = Number(url.searchParams.get('limit')) || 25, offset = Number(url.searchParams.get('offset')) || 0;
    // Only the Downloads list asks for a page by offset (Download Activity and
    // the input-required scan read the same path).
    if (url.searchParams.has('offset')) log.gets.push({limit, offset});
    if (hold) await hold(offset);
    return route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({items: all.slice(offset, offset + limit), total: total ?? all.length})});
  });
  await page.setViewportSize({width: 1560, height: 1000});
  await page.goto('/');
  await page.evaluate(async () => {
    nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]'));
    await loadTorrents();
  });
  await expect(page.locator('#t-tbody tr[data-torrent-id]').first()).toBeVisible();
  return log;
}

// The sidebar's width transition has finished (the real CSS transition, not a sleep),
// and the capacity check a resize schedules has settled.
async function resize(page, width) {
  await page.setViewportSize({width, height: 1000});
  await page.waitForFunction(() =>
    document.getElementById('sidebar').getAnimations().every(animation => animation.playState !== 'running'));
  await page.waitForTimeout(200);
}

// The window at which the table first measures narrower than ``limit``, and the one just wider.
async function edge(page, limit, from, to) {
  let wide = null;
  for (let width = from; width >= to; width--) {
    await resize(page, width);
    const table = await page.evaluate(() => document.querySelector('#view-torrents .dp-downloads-table-wrap').clientWidth);
    if (table < limit) {
      if (wide === null) throw new Error(`the table is already narrower than ${limit}px at ${from}px`);
      return {wide, narrow: width};
    }
    wide = width;
  }
  throw new Error(`no window between ${from} and ${to}px gave a table narrower than ${limit}px`);
}

// One read of the table as it stands.
const read = page => page.evaluate(() => {
  const r = node => node.getBoundingClientRect();
  const shown = node => !!node && getComputedStyle(node).display !== 'none' && r(node).width > 0;
  const wrap = document.querySelector('#view-torrents .dp-downloads-table-wrap'), table = wrap.querySelector('table');
  const box = cell => { const b = r(cell), s = getComputedStyle(cell); return {left: b.left + parseFloat(s.paddingLeft), right: b.right - parseFloat(s.paddingRight)}; };
  const centre = node => { const b = r(node); return b.left + b.width / 2; };
  const textCentre = node => { const range = document.createRange(); range.selectNodeContents(node.querySelector('span') || node); const b = range.getBoundingClientRect(); return b.left + b.width / 2; };
  const heads = [...table.querySelectorAll('thead th')];
  const column = name => heads.find(th => th.classList.contains(`dp-downloads-${name}-cell`));
  const visible = heads.filter(shown);
  return {
    wrap: wrap.clientWidth, overflow: wrap.scrollWidth - wrap.clientWidth,
    headings: visible.map(th => th.classList.contains('dp-downloads-select-cell') ? 'select' : (th.firstChild?.textContent || '').trim()),
    columns: Object.fromEntries(['name', 'status', 'progress', 'size', 'date', 'action', 'select'].map(name =>
      [name, column(name) && shown(column(name)) ? r(column(name)).width : 0])),
    statusHeading: column('status') ? textCentre(column('status')) - (() => { const b = r(column('status')); return b.left + b.width / 2; })() : null,
    actionHeading: column('action') ? textCentre(column('action')) - (() => { const b = r(column('action')); return b.left + b.width / 2; })() : null,
    rows: [...table.querySelectorAll('tbody tr[data-torrent-id]')].map(tr => {
      const cells = [...tr.children];
      const name = tr.querySelector('.dp-downloads-name-cell'), meta = name.querySelector('.dp-transfer-provider-meta');
      const size = meta.querySelector('.dp-downloads-size'), sizeCell = tr.querySelector('.dp-downloads-size-cell');
      const status = tr.querySelector('.dp-downloads-status-cell'), badge = status.querySelector('.badge');
      // The drawn row actions (the handheld Start Now copy is not drawn from 780px).
      const action = tr.querySelector('.dp-downloads-action-cell'), buttons = action ? [...action.querySelectorAll('.btn')].filter(shown) : [];
      // The metadata items as laid out: the essentials group is display:contents from 780px, so its
      // children are the line's items; the handheld status glyph is not drawn there; the size is kept.
      const items = [...meta.querySelectorAll(':scope > .dp-transfer-meta-main > *, :scope > :not(.dp-transfer-meta-main)')]
        .filter(node => node === size || shown(node) || node.classList.contains('dp-source-icon-slot'));
      const controls = items.filter(node => node !== size && shown(node));
      const nameBox = box(name), title = getComputedStyle(name.querySelector('.t-name')), type = getComputedStyle(size);
      const bar = name.querySelector('.dash-row-bar-slot');
      const progressCell = tr.querySelector('.dp-downloads-progress-cell');
      const select = tr.querySelector('.dp-downloads-select-cell');
      return {
        id: Number(tr.dataset.torrentId), cells: cells.length, height: r(tr).height,
        classes: cells.map(td => [...td.classList].find(name => /^dp-downloads-.*-cell$/.test(name)) || ''),
        last: cells.at(-1).classList.contains('dp-downloads-select-cell'),
        meta: items.map(node => (node.getAttribute('class') || '').split(' ')[0]),
        controls: controls.map(node => [Math.round(r(node).left - nameBox.left), Math.round(r(node).width)]),
        metaInside: controls.every(node => r(node).right <= nameBox.right + 0.5),
        metaHeight: r(meta).height,
        bar: shown(bar) ? {bottom: r(bar).bottom, metaTop: r(meta).top, fill: name.querySelector('.dash-row-bar.is-empty') ? 0 : r(bar).width} : null,
        hashes: tr.querySelectorAll('.t-hash, .dp-transfer-source-label').length,
        sizes: [shown(size), shown(sizeCell)].filter(Boolean).length,
        relocated: shown(size) ? {value: size.textContent, cell: sizeCell.textContent.trim(), rightGap: nameBox.right - r(size).right,
          clear: r(size).left - Math.max(...controls.map(node => r(node).right)),
          sameType: ['fontFamily', 'fontSize', 'fontWeight', 'fontStyle', 'color', 'letterSpacing'].every(key => title[key] === type[key]),
          lines: new Set([...(() => { const g = document.createRange(); g.selectNodeContents(size); return g.getClientRects(); })()].map(q => Math.round(q.top))).size} : null,
        badge: {offset: centre(badge) - (box(status).left + box(status).right) / 2, width: r(badge).width,
          labelShown: Number(getComputedStyle(badge.querySelector('.dp-status-label')).opacity) > 0.5},
        consent: status.querySelector('.dp-queue-consent') ? (() => { const c = status.querySelector('.dp-queue-consent');
          return {offset: centre(c) - (box(status).left + box(status).right) / 2, inside: r(c).left >= box(status).left - 0.5 && r(c).right <= box(status).right + 0.5,
            labelShown: Number(getComputedStyle(c.querySelector('[data-dp-icon-label]')).opacity) > 0.5}; })() : null,
        actions: buttons.map(button => ({label: button.dataset.defaultLabel, width: r(button).width,
          labelShown: Number(getComputedStyle(button.querySelector('[data-dp-icon-label]')).opacity) > 0.5,
          glyph: shown(button.querySelector('.dp-row-action-icon')) ? button.querySelector('.dp-row-action-icon').dataset.dpLucide : null})),
        actionOffset: buttons.length ? (r(buttons[0]).left + r(buttons.at(-1)).right) / 2 - (box(action).left + box(action).right) / 2 : null,
        actionsInside: buttons.every(button => r(button).left >= box(action).left - 0.5 && r(button).right <= box(action).right + 0.5),
        separate: buttons.every((button, index) => index === 0 || r(button).left >= r(buttons[index - 1]).right + 4),
        progress: shown(progressCell) ? {bar: r(progressCell.querySelector('.prog')).width,
          spill: progressCell.scrollWidth > progressCell.clientWidth + 1} : null,
        checkbox: select ? (() => { const c = r(select.querySelector('.t-chk')), s = r(select); return c.width > 0 && c.left >= s.left && c.right <= s.right; })() : null,
      };
    }),
  };
});

const byId = (state, id) => state.rows.find(entry => entry.id === id);

test('the full table: one Name column with its controls beneath, centred Status and Actions, every button, selection last', async ({page}) => {
  await open(page, () => ITEMS);
  for (const theme of ['dark', 'light']) {
    await page.evaluate(theme => document.body.classList.toggle('light', theme === 'light'), theme);
    for (const width of [1560, 1390]) {
      await resize(page, width);
      const state = await read(page), where = `${theme} ${width}px`;
      expect(state.wrap, where).toBeGreaterThanOrEqual(COMPACT);
      expect(state.headings, where).toEqual(['Name', 'Status', 'Progress', 'Size', 'Date', 'Actions', 'select']);
      expect(state.overflow, where).toBeLessThanOrEqual(0);
      expect(Math.abs(state.statusHeading), where).toBeLessThanOrEqual(1);
      expect(Math.abs(state.actionHeading), where).toBeLessThanOrEqual(1);
      expect(state.columns.name, where).toBeGreaterThanOrEqual(279.5);
      for (const entry of state.rows) {
        const at = `${where} row ${entry.id}`;
        expect(entry.classes, at).toEqual(['dp-downloads-name-cell', 'dp-downloads-status-cell', 'dp-downloads-progress-cell',
          'dp-downloads-size-cell', 'dp-downloads-date-cell', 'dp-downloads-action-cell', 'dp-downloads-select-cell']);
        expect(entry.hashes, at).toBe(0);                          // no request/hash caption, no source description
        expect(entry.meta.at(-1), at).toBe('dp-downloads-size');   // the relocatable size closes the row, hidden here
        expect([entry.sizes, entry.relocated], at).toEqual([1, null]);
        expect(entry.metaInside, at).toBe(true);
        expect(Math.abs(entry.badge.offset), at).toBeLessThanOrEqual(0.75);
        expect(entry.badge.labelShown, at).toBe(true);
        expect(Math.abs(entry.actionOffset), at).toBeLessThanOrEqual(0.75);
        expect(entry.actionsInside && entry.separate, at).toBe(true);
        expect(entry.actions.every(action => action.labelShown && action.glyph === null), at).toBe(true);
        expect(entry.last && entry.checkbox, at).toBe(true);
        expect(entry.progress && !entry.progress.spill, at).toBe(true);
      }
      // The demanding Name: source, Debrid-Link selector and candidate launcher on the second line.
      expect(byId(state, 9701).meta.slice(0, 3), where).toEqual(['dp-source-icon-slot', 'dp-provider-chip', 'dp-candidate-chip']);
      expect(byId(state, 9701).bar.fill, where).toBeGreaterThan(0);
      expect(byId(state, 9701).bar.bottom, where).toBeLessThanOrEqual(byId(state, 9701).bar.metaTop + 0.5);
      expect(byId(state, 9704).meta, where).toContain('lbl-badge');
      // Every button a row's states allow, side by side.
      expect(byId(state, 9702).actions.map(action => action.label), where).toEqual(['Pause', 'Retry', 'Remove']);
      expect(byId(state, 9705).actions.map(action => action.label), where).toEqual(['Resume', 'Remove']);
      expect(byId(state, 9706).actions.map(action => action.label), where).toEqual(['Remove']);
      expect(byId(state, 9703).consent, where).toMatchObject({inside: true, labelShown: true});
      expect(Math.abs(byId(state, 9703).consent.offset), where).toBeLessThanOrEqual(0.75);
    }
  }
  await expect(page.locator('#view-torrents thead th', {hasText: /Provider/})).toHaveCount(0);
});

test('a compact table shows Status and each action by its glyph, says them in DP tooltips, and sets Added aside', async ({page}) => {
  await open(page, () => ITEMS);
  const {wide, narrow} = await edge(page, COMPACT, 1320, 1240);
  await resize(page, wide);
  const before = await read(page);
  await resize(page, narrow);
  const after = await read(page);
  expect(before.headings).toEqual(['Name', 'Status', 'Progress', 'Size', 'Date', 'Actions', 'select']);
  expect(after.headings).toEqual(['Name', 'Status', 'Progress', 'Size', 'Actions', 'select']);
  expect(after.overflow).toBeLessThanOrEqual(0);
  expect(after.columns.progress).toBeGreaterThan(before.columns.progress);      // Progress takes the room
  expect(Math.abs(after.statusHeading)).toBeLessThanOrEqual(1);
  expect(Math.abs(after.actionHeading)).toBeLessThanOrEqual(1);
  for (const entry of after.rows) {
    const at = `row ${entry.id}`;
    expect(entry.badge.labelShown, at).toBe(false);
    expect(Math.abs(entry.badge.offset), at).toBeLessThanOrEqual(0.75);
    expect(entry.actions.every(action => !action.labelShown && Math.round(action.width) === 36), at).toBe(true);
    expect(Math.abs(entry.actionOffset), at).toBeLessThanOrEqual(0.75);
    expect(entry.actionsInside && entry.separate && entry.checkbox, at).toBe(true);
  }
  expect(byId(after, 9702).actions.map(action => action.glyph)).toEqual(['pause', 'refresh', 'trash2']);
  expect(byId(after, 9705).actions.map(action => action.glyph)).toEqual(['play', 'trash2']);
  expect(byId(after, 9703).consent).toMatchObject({inside: true, labelShown: false});

  // Each control keeps its own name, and DP's tooltip says it on hover and keyboard focus (never a native title).
  const tooltip = page.locator('#dp-tooltip');
  const triple = page.locator('#t-tbody tr[data-torrent-id="9702"] .actions .btn');
  await expect(triple).toHaveCount(3);
  for (const [index, name, words] of [[0, 'Pause', 'Pause this download'], [1, 'Retry', 'Retry this download'], [2, 'Remove', 'Remove this download']]) {
    await expect(triple.nth(index)).toHaveAccessibleName(name);
    await expect(triple.nth(index)).not.toHaveAttribute('title', /.*/);
    await triple.nth(index).focus();
    await expect(tooltip).toHaveText(words);
  }
  await page.locator('#torrent-search').focus();
  const badge = page.locator('#t-tbody tr[data-torrent-id="9704"] .badge');
  await badge.hover();
  await expect(tooltip).toHaveText('Failed');
  const consent = page.locator('#t-tbody tr[data-torrent-id="9703"] .dp-queue-consent');
  await expect(consent).toHaveAccessibleName('Start Now');
  await consent.hover();
  await expect(tooltip).toHaveText(/^Start Now/);
});

test('a narrower table moves Size beside the name, in its type, clear of its controls, and gives Progress the room', async ({page}) => {
  await open(page, () => ITEMS);
  const {wide, narrow} = await edge(page, RELOCATE, 900, 860);
  await resize(page, wide);
  const before = await read(page);
  await resize(page, narrow);
  const after = await read(page);
  expect(before.headings).toEqual(['Name', 'Status', 'Progress', 'Size', 'Actions', 'select']);
  expect(after.headings).toEqual(['Name', 'Status', 'Progress', 'Actions', 'select']);
  expect(after.columns.progress).toBeGreaterThan(before.columns.progress);
  expect(after.overflow).toBeLessThanOrEqual(0);
  after.rows.forEach((entry, index) => {
    const at = `row ${entry.id}`, was = before.rows[index];
    expect([was.sizes, was.relocated], at).toEqual([1, null]);
    expect(entry.sizes, at).toBe(1);
    expect(entry.relocated.value, at).toBe(entry.relocated.cell);          // the same formatted value
    expect(entry.relocated.sameType && entry.relocated.lines === 1, at).toBe(true);
    expect(Math.abs(entry.relocated.rightGap), at).toBeLessThanOrEqual(0.5);
    expect(entry.relocated.clear, at).toBeGreaterThan(0);
    expect(entry.controls, at).toEqual(was.controls);                       // the controls where they were
    expect(entry.metaHeight, at).toBeCloseTo(was.metaHeight, 1);
  });
  // Down to the tablet's edge: every column, control and checkbox on the card, Progress shown.
  for (const width of [800, 780]) {
    await resize(page, width);
    const state = await read(page);
    expect(state.overflow, `${width}px`).toBeLessThanOrEqual(0);
    expect(state.columns.name, `${width}px`).toBeGreaterThanOrEqual(279.5);
    for (const entry of state.rows) {
      const at = `${width}px row ${entry.id}`;
      expect(entry.progress.bar, at).toBeGreaterThan(60);
      expect(entry.progress.spill, at).toBe(false);
      expect(entry.metaInside && entry.actionsInside && entry.checkbox, at).toBe(true);
      expect(entry.relocated.clear, at).toBeGreaterThan(0);
    }
  }
  // The Debrid-Link selector in the narrow Name still opens its picker.
  const launcher = page.locator('#t-tbody tr[data-torrent-id="9701"] .dp-root-provider-launcher');
  await launcher.click();
  await expect(page.locator('.dp-root-provider-menu')).toBeVisible();
  await expect(launcher).toHaveAttribute('aria-expanded', 'true');
  await expect.poll(() => page.locator('#overlay.open').count()).toBe(0);  // and opened no Details
});

test('a state change re-renders its badge, tooltip and actions; a pending compact action keeps its glyph', async ({page}) => {
  let items = ITEMS.slice(4, 5);
  await open(page, () => items);
  await resize(page, 1000);
  const tooltip = page.locator('#dp-tooltip');
  const rowOf = () => page.locator('#t-tbody tr[data-torrent-id="9705"]');
  await expect(rowOf().locator('.actions .btn')).toHaveCount(2);
  await rowOf().locator('.badge').hover();
  await expect(tooltip).toHaveText('Paused');
  items = [{...ITEMS[4], status: 'downloading', presentation_status: 'downloading', presentation_label: 'Downloading',
    presentation_badge_status: 'downloading', acquisition_eta: {seconds: 60, held: false}}];
  await page.mouse.move(0, 0);
  await page.evaluate(() => loadTorrents());
  await expect(rowOf().locator('.actions .btn').first()).toHaveAccessibleName('Pause');
  await expect(rowOf().locator('.actions .btn').first().locator('svg')).toHaveAttribute('data-dp-lucide', 'pause');
  await rowOf().locator('.badge').hover();
  await expect(tooltip).toHaveText('Downloading');
  // Retry, pending: the glyph and the square stay, the label says what is happening.
  items = [ITEMS[3]];
  await page.mouse.move(0, 0);
  await page.evaluate(() => loadTorrents());
  // The answer is held until released; ``reached`` is the request arriving here
  // (the button shows its pending state before it does).
  let release, arrive;
  const reached = new Promise(resolve => { arrive = resolve; });
  await page.route('**/api/torrents/9704/retry', async route => { await new Promise(resolve => { release = resolve; arrive(); }); return route.fulfill({status: 200, contentType: 'application/json', body: '{}'}); });
  const retry = page.locator('#t-tbody tr[data-torrent-id="9704"] .actions .btn', {hasText: 'Retry'});
  await retry.click();
  await expect(retry).toHaveAttribute('aria-busy', 'true');
  await expect(retry.locator('[data-dp-icon-label]')).toHaveText('Retrying…');
  await expect(retry.locator('svg[data-dp-lucide="refresh"]')).toBeVisible();
  expect(Math.round((await retry.boundingBox()).width)).toBe(36);
  await reached;
  release();
  await expect(retry).not.toHaveAttribute('aria-busy', 'true');
});

test('pressing a control in the Name cell never picks the row up; the Name and its grip still reorder, by pointer and keyboard', async ({page}) => {
  const log = await open(page, () => ITEMS);
  for (const width of [1560, 1000]) {
    await resize(page, width);
    const launcher = page.locator('#t-tbody tr[data-torrent-id="9701"] .dp-root-provider-launcher');
    const start = await launcher.boundingBox();
    await page.mouse.move(start.x + start.width / 2, start.y + start.height / 2);
    await page.mouse.down();
    await page.mouse.move(start.x + start.width / 2, start.y + 90, {steps: 8});
    await expect(page.locator('.dp-queue-drag-ghost')).toHaveCount(0);
    await page.mouse.up();
    expect(log.moves, `${width}px`).toEqual([]);
    await page.keyboard.press('Escape');
    for (const control of ['.dp-candidate-chip', '.dp-file-selection-chip']) {
      const node = page.locator(`#t-tbody tr[data-torrent-id="${control === '.dp-candidate-chip' ? 9701 : 9704}"] ${control}`);
      const box = await node.boundingBox();
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.mouse.down();
      await page.mouse.move(box.x + box.width / 2, box.y + 90, {steps: 8});
      await expect(page.locator('.dp-queue-drag-ghost')).toHaveCount(0);
      await page.mouse.up();
      await page.keyboard.press('Escape');
    }
    expect(log.moves, `${width}px`).toEqual([]);
  }
  // The name itself drags: Paused (9705) moves above the Debrid-Link row.
  await resize(page, 1000);
  const name = page.locator('#t-tbody tr[data-torrent-id="9705"] .t-name');
  const from = await name.boundingBox(), to = await page.locator('#t-tbody tr[data-torrent-id="9701"]').boundingBox();
  await page.mouse.move(from.x + 10, from.y + from.height / 2);
  await page.mouse.down();
  await page.mouse.move(from.x + 10, from.y + from.height / 2 + 12, {steps: 4});
  await page.mouse.move(to.x + 40, to.y + to.height * 0.25, {steps: 12});
  await page.mouse.up();
  await expect.poll(() => log.moves).toEqual([{id: 9705, before_id: 9701, after_id: null}]);
  // The keyboard: pick up, move, cancel -- nothing moves; then pick up, move, drop.
  const cell = page.locator('#t-tbody tr[data-torrent-id="9704"] td[data-queue-drag="true"]');
  await cell.focus();
  await page.keyboard.press('Space');
  await expect(page.locator('#dp-queue-status')).toContainText('Picked up');
  await page.keyboard.press('ArrowUp');
  // Cancelling reloads the list and then restores focus where the move had it
  // (the gap's row); that lands after this step, so wait for it before taking
  // focus back to 9704, or the restore can take it away again.
  const reloaded = page.waitForResponse(response => new URL(response.url()).pathname === '/api/torrents');
  await page.keyboard.press('Escape');
  await expect(page.locator('#dp-queue-status')).toContainText('Nothing was moved');
  await reloaded;
  await page.waitForFunction(() => document.activeElement?.matches('#t-tbody td[data-queue-drag="true"]'));
  expect(log.moves.length).toBe(1);
  await page.locator('#t-tbody tr[data-torrent-id="9704"] td[data-queue-drag="true"]').focus();
  await page.keyboard.press('Enter');
  await page.keyboard.press('ArrowUp');
  await page.keyboard.press('Enter');
  await expect.poll(() => log.moves.length).toBe(2);
  expect(log.moves[1]).toMatchObject({id: 9704, before_id: 9703});
});

test('selection: each row, select all and its partial state, the bulk toolbar, in the compact table', async ({page}) => {
  const log = await open(page, () => ITEMS);
  await resize(page, 1000);
  const boxes = page.locator('#t-tbody .t-chk'), all = page.locator('#chk-all');
  await boxes.nth(1).check();
  await expect(all).toHaveJSProperty('indeterminate', true);
  await expect(page.locator('#bulk-bar')).toHaveClass(/\bvisible\b/);
  await expect(page.locator('#bulk-count')).toHaveText('1 Selected');
  await all.check();
  await expect(all).toHaveJSProperty('indeterminate', false);
  await expect(boxes).toHaveCount(ITEMS.length);
  for (let index = 0; index < ITEMS.length; index++) await expect(boxes.nth(index)).toBeChecked();
  await expect(page.locator('#bulk-count')).toHaveText(`${ITEMS.length} Selected`);
  await expect(page.locator('#bulk-bar .dp-downloads-bulk-action--delete')).toHaveCount(1);
  await expect(page.locator('#bulk-bar .dp-downloads-bulk-action--top')).toBeVisible();
  // A checkbox never opens Details or starts a move.
  expect(log.moves).toEqual([]);
  await expect(page.locator('#overlay.open')).toHaveCount(0);
  await page.locator('#bulk-bar .dp-downloads-bulk-action--clear').click();
  await expect(all).not.toBeChecked();
  await expect(page.locator('#bulk-bar')).not.toHaveClass(/\bvisible\b/);
});

test('a page turn draws its placeholder in the table\'s own columns, and a resize settles without fetching again', async ({page}) => {
  const many = Array.from({length: 60}, (_, index) => ({...ITEMS[index % ITEMS.length], id: 9800 + index, name: `Paged ${index}`}));
  let held = null;
  const log = await open(page, () => many, {hold: async offset => { if (held && offset > 0) await held; }});
  for (const width of [1560, 1000, 840]) {
    await resize(page, width);
    await expect.poll(async () => {
      const count = log.gets.length;
      await page.waitForTimeout(700);
      return log.gets.length === count;
    }, {timeout: 8000}).toBe(true);
    const settled = log.gets.length;
    await page.waitForTimeout(800);
    expect(log.gets.length, `${width}px: no fetch after the page settles`).toBe(settled);
    // Turn to page 2 while its answer is held: the placeholder stands in the same columns.
    let release;
    held = new Promise(resolve => { release = resolve; });
    await page.evaluate(() => goToTorrentPage(2, {turn: true}));
    await expect(page.locator('#t-tbody tr.dp-page-placeholder-row').first()).toBeVisible();
    const match = await page.evaluate(() => {
      // The table's own headings -- not those of the inert copy the page turn's slide draws beside it.
      const heads = [...document.getElementById('t-tbody').closest('table').tHead.rows[0].cells];
      const cells = [...document.querySelector('#t-tbody tr.dp-page-placeholder-row').children];
      const shown = node => getComputedStyle(node).display !== 'none';
      return {count: [heads.length, cells.length],
        classes: cells.every((td, index) => [...heads[index].classList].filter(name => name.startsWith('dp-downloads-'))
          .every(name => td.classList.contains(name))),
        shown: cells.every((td, index) => shown(td) === shown(heads[index])),
        widths: cells.every((td, index) => !shown(td) || Math.abs(td.getBoundingClientRect().width - heads[index].getBoundingClientRect().width) <= 1)};
    });
    expect(match, `${width}px`).toEqual({count: [7, 7], classes: true, shown: true, widths: true});
    release();
    held = null;
    await expect(page.locator('#t-tbody tr.dp-page-placeholder-row')).toHaveCount(0);
    await expect(page.locator('#torrent-page-btns [aria-current="page"]')).toHaveText('2');
    await expect(page.locator('#torrent-page-info')).toHaveText('Showing 60 Retained Items');
    await page.evaluate(() => goToTorrentPage(1));
    await expect(page.locator('#t-tbody tr[data-torrent-id="9800"]')).toBeVisible();
  }
});

test('a viewer who cannot operate gets no Actions or selection column: headings, rows, placeholder and empty state align', async ({page}) => {
  let items = ITEMS;
  await open(page, () => items, {readOnly: true});
  await expect(page.locator('#view-torrents thead th')).toHaveCount(5);
  for (const width of [1560, 1000, 780]) {
    await resize(page, width);
    const state = await read(page), where = `${width}px`;
    expect(state.overflow, where).toBeLessThanOrEqual(0);
    expect(state.columns.action + state.columns.select, where).toBe(0);
    expect(state.headings.slice(0, 2), where).toEqual(['Name', 'Status']);
    expect(state.headings).not.toContain('Actions');
    for (const entry of state.rows) {
      expect(entry.cells, `${where} row ${entry.id}`).toBe(5);
      expect(entry.actions, `${where} row ${entry.id}`).toEqual([]);
      expect(entry.checkbox, `${where} row ${entry.id}`).toBeNull();
    }
    const sum = Object.values(state.columns).reduce((a, b) => a + b, 0);
    expect(Math.abs(sum - state.wrap), where).toBeLessThanOrEqual(1);
  }
  await expect(page.locator('#t-tbody .t-chk, #t-tbody .actions, #t-tbody .dp-queue-consent')).toHaveCount(0);
  // A page turn's placeholder has the same five columns, each as wide as its heading.
  await resize(page, 1000);
  items = Array.from({length: 60}, (_, index) => ({...ITEMS[index % ITEMS.length], id: 9900 + index}));
  await page.evaluate(() => loadTorrents());
  await expect(page.locator('#torrent-page-btns [aria-label="Next page"]')).toBeVisible();
  let release;
  const held = new Promise(resolve => { release = resolve; });
  await page.route(url => url.pathname === '/api/torrents' && Number(url.searchParams.get('offset')) > 0,
    async route => { await held; return route.fallback(); });
  await page.evaluate(() => goToTorrentPage(2, {turn: true}));
  await expect(page.locator('#t-tbody tr.dp-page-placeholder-row').first()).toBeVisible();
  expect(await page.evaluate(() => {
    // The table's own headings -- not those of the inert copy the page turn's slide draws beside it.
    const heads = [...document.getElementById('t-tbody').closest('table').tHead.rows[0].cells];
    const cells = [...document.querySelector('#t-tbody tr.dp-page-placeholder-row').children];
    return [heads.length, cells.length, cells.every((td, index) => getComputedStyle(td).display === 'none'
      || Math.abs(td.getBoundingClientRect().width - heads[index].getBoundingClientRect().width) <= 1)];
  })).toEqual([5, 5, true]);
  release();
  await expect(page.locator('#t-tbody tr.dp-page-placeholder-row')).toHaveCount(0);
  // The empty state spans the five.
  items = [];
  await page.evaluate(() => goToTorrentPage(1));
  await expect(page.locator('#t-tbody td[colspan="5"] .empty')).toHaveCount(1);
});

test('the Downloads header has no Refresh control at any width; its title, filters, search and order keep their places', async ({page}) => {
  await open(page, () => ITEMS);
  const header = () => page.evaluate(() => {
    const card = document.querySelector('#view-torrents > .card'), head = card.querySelector('.card-header');
    const box = node => { const r = node.getBoundingClientRect(); return {left: r.left, right: r.right, top: r.top, bottom: r.bottom}; };
    const inside = node => { const b = box(node), h = box(head); return b.left >= h.left - 0.5 && b.right <= h.right + 0.5; };
    const refresh = [...card.querySelectorAll('button, [role="button"]')].filter(node =>
      /refresh/i.test(`${node.textContent} ${node.getAttribute('aria-label') || ''} ${node.className}`));
    // Below 780px the one status control stands for the filters (downloads-mobile-header-bulk-filters.spec.js).
    const filters = getComputedStyle(head.querySelector('.filter-tabs')).display !== 'none' ? head.querySelector('.filter-tabs') : document.getElementById('torrent-filter');
    return {refresh: refresh.length, controls: [...head.children].map(node => node.id || node.className.split(' ')[0]),
      filters: inside(filters), filter: filters.id || filters.className, title: inside(head.querySelector('#torrent-card-title')),
      search: getComputedStyle(document.getElementById('torrent-search')).display !== 'none',
      order: getComputedStyle(document.getElementById('torrent-order')).display !== 'none',
      overflow: head.scrollWidth > head.clientWidth + 1};
  });
  for (const width of [1560, 1389, 1000, 780, 779, 390]) {
    await resize(page, width);
    const state = await header();
    expect(state, `${width}px`).toEqual({refresh: 0, controls: ['torrent-card-title', 'filter-tabs', 'torrent-filter', 'torrent-filter-menu'],
      filters: true, filter: width >= 780 ? 'filter-tabs' : 'torrent-filter', title: true, search: true, order: true, overflow: false});
  }
  // The list still refreshes itself through its one path.
  const log = {before: 0};
  await page.route('**/api/torrents?*', route => { log.before += 1; return route.fallback(); });
  await page.evaluate(() => loadTorrents());
  expect(log.before).toBeGreaterThan(0);
});

// 1.0.14 handheld Transfer Summary (formerly a BLOCKED MOBILE case: it held
// the superseded handheld columns Name/Status/Date/Actions/select, no name bar
// and labelled controls). Below 780px the table is the Transfer Summary,
// Actions and the selection column; the name's thin slot draws the row's
// primary track, and its metadata line closes with the size where it fits
// (transfer-summary-mobile.spec.js qualifies the whole composition).
test('mobile (779px) is the Transfer Summary, Actions and selection: the name bar is the progress track, the size closes the metadata line, glyph-only actions', async ({page}) => {
  await open(page, () => ITEMS);
  await resize(page, 779);
  const state = await page.evaluate(() => {
    const shown = node => getComputedStyle(node).display !== 'none';
    const heads = [...document.querySelectorAll('#view-torrents thead th')];
    return {headings: heads.filter(shown).map(th => th.classList.contains('dp-downloads-select-cell') ? 'select' : (th.firstChild?.textContent || '').trim()),
      tracks: [...document.querySelectorAll('#t-tbody .dp-transfer-summary-track')].filter(shown).length,
      legacyBars: [...document.querySelectorAll('#t-tbody .dash-row-bar')].filter(shown).length,
      sizes: [...document.querySelectorAll('#t-tbody .dp-downloads-size')].filter(shown).length,
      actionLabels: [...document.querySelectorAll('#t-tbody .actions [data-dp-icon-label]')].every(node => Number(getComputedStyle(node).opacity) <= 0.5)};
  });
  // Six rows, one of them completed (no track); every size has room at 779px.
  expect(state).toEqual({headings: ['Name', 'Actions', 'select'], tracks: 5, legacyBars: 0, sizes: 6, actionLabels: true});
});
