const { test, expect } = require('@playwright/test');

/* DP 1.0.14 handheld transfer lists (below 780px): Download Activity is its
 * Transfer Summary and Action; Downloads is its Transfer Summary, Actions
 * (which also holds Start Now there) and the selection column, last. The
 * summary is the Name cell's own three layers:
 * - row 1: the name, truncating, beside the primary value of the row's one
 *   progress projection;
 * - row 1.5: the thin slot, drawing that projection's primary track at the
 *   slot's 3px geometry (none for a terminal transfer);
 * - row 2: one metadata line -- the status glyph, the provider and the
 *   transfer's controls, then the size -- giving way in one order: the size,
 *   then a user label (its text, then the label), then the providers' short
 *   names, decided by the providers' own room; a queue grip stands beside
 *   the name only, the line taking the room beneath it.
 * From 780px both tables are the desktop's. Mocked rows: no backend state is
 * read or written. */

function row(id, o) {
  return {
    id, name: `Row ${id}`, status: 'completed', presentation_status: 'completed', presentation_label: 'Done',
    presentation_badge_status: 'completed', attention_required: false, progress: 100, active_execution_progress: null,
    active_execution_basis: null, size_bytes: 1000, source: 'magnet', hash: 'a1b2c3d4e5f60718293a4b5c6d7e8f90', label: '',
    created_at: '2025-12-28T22:54:00Z', current_source_identity: {kind: 'magnet'}, providers: [], historical_providers: [],
    delivering_provider_ids: [], input_required: null, acquisition_eta: null, access: {destroy: true}, queue_member: false, ...o,
  };
}
const downloading = o => ({status: 'downloading', presentation_status: 'downloading', presentation_label: 'Downloading', presentation_badge_status: 'downloading', ...o});
// The widest legitimate rows (as in downloads-unified-table.spec.js), and one of each progress state.
const ITEMS = [
  row(9701, downloading({name: 'GF191026-DSKNYTHC2-VC38 + 3 files', progress: 36, active_execution_progress: 36, active_execution_basis: 'bytes',
    size_bytes: 999.9 * 1024 ** 2, acquisition_eta: {seconds: 2318, held: false}, route_provider_id: 'debridlink', route_provider_name: 'Debrid-Link',
    route_switch_available: true, common_candidate_count: 2, group_remaining_count: 1, candidate_action_scope: 'group', candidate_action_count: 2, queue_member: true})),
  row(9702, downloading({name: 'Multi.File.Partial.Error.Season.Pack', status: 'error', progress: 12, active_execution_progress: 12,
    active_execution_basis: 'bytes', size_bytes: 987.6 * 1024 ** 3, active_provider_id: 'alldebrid', active_provider_name: 'AllDebrid', queue_member: true})),
  row(9703, {name: 'Queued.Promoted.Transfer.mkv', status: 'queued', presentation_status: 'queued', presentation_label: 'Queued',
    presentation_badge_status: 'queued', progress: 0, size_bytes: 77 * 1024 ** 2, active_provider_id: 'premiumize',
    active_provider_name: 'Premiumize', queue_member: true, queue_restart_consent: true}),
  row(9704, {name: 'Failed.Download.zip', status: 'error', presentation_status: 'failed', presentation_label: 'Failed',
    presentation_badge_status: 'error', progress: 3, size_bytes: 1.5 * 1024 ** 3, active_provider_id: 'torbox',
    active_provider_name: 'TorBox', file_selection_affordance: 'select', label: 'movies', queue_member: true}),
  row(9705, {name: 'Paused.Transfer.mkv', status: 'paused', presentation_status: 'paused', presentation_label: 'Paused',
    presentation_badge_status: 'paused', progress: 41, size_bytes: 123.5 * 1024 ** 3, active_provider_id: 'realdebrid',
    active_provider_name: 'Real-Debrid', current_source_identity: {kind: 'link'}, queue_member: true}),
  row(9706, {name: 'Completed.Item.mkv', size_bytes: 987.6 * 1024 ** 4, active_provider_id: 'usenet', active_provider_name: 'Usenet', current_source_identity: {kind: 'link'}}),
  row(9707, downloading({name: 'Media.Clip.webm', progress: 55, size_bytes: 300 * 1024 ** 2, active_provider_id: 'media', active_provider_name: 'Media Downloads',
    current_source_identity: {kind: 'link'}, queue_member: true})),
];
// The handheld short labels (the identity owner's, keyed by integration id).
const SHORT = {debridlink: 'DL', alldebrid: 'AD', premiumize: 'PM', realdebrid: 'RD', torbox: 'TB', media: 'Media'};
// Debrid-Link's route, as the route launcher's picker reads it.
const ROUTE = {transfer_id: 9701, current_provider_id: 'debridlink', current_provider_name: 'Debrid-Link', switchable: true,
  providers: [
    {provider_id: 'debridlink', provider_name: 'Debrid-Link', status: 'current', selectable: false, reason: null, readiness: null},
    {provider_id: 'alldebrid', provider_name: 'AllDebrid', status: 'available', selectable: true, reason: null, readiness: null}]};
const STATES = [
  row(9711, downloading({name: 'Execution.Only', progress: 0, active_execution_progress: 12.34, active_execution_basis: 'bytes'})),
  row(9712, downloading({name: 'Unknown.Total', progress: null, size_bytes: null})),
  row(9713, downloading({name: 'Estimated', progress: null, active_execution_progress: 94.6, active_execution_basis: 'estimated'})),
  row(9714, downloading({name: 'Processing', progress: 0, active_execution_progress: 100, active_execution_basis: 'processing'})),
  row(9715, downloading({name: 'Verified.Beside.Execution', progress: 31, active_execution_progress: 64, active_execution_basis: 'bytes'})),
  row(9717, {name: 'Failed.Retryable', status: 'error', presentation_status: 'failed', presentation_label: 'Failed', presentation_badge_status: 'error', progress: 3}),
  row(9716, {name: 'Cancelled', status: 'cancelled', presentation_status: 'cancelled', presentation_label: 'Cancelled', presentation_badge_status: 'cancelled', progress: 20}),
];
const UNIFORM = Array.from({length: 60}, (_, k) => row(9800 + k, downloading({name: `Uniform.Row.${k}.mkv`, progress: 20 + k % 50,
  size_bytes: 2 * 1024 ** 3, active_provider_id: 'alldebrid', active_provider_name: 'AllDebrid', queue_member: true, current_source_identity: {kind: 'link'}})));

// CI's fallback fonts by default; DP_WEB_FONTS=1 loads Outfit (the product's own face) for the same assertions.
async function open(page, items, {readOnly = false, width = 390, height = 844, view = 'torrents', log = {gets: 0, moves: []}} = {}) {
  if (!process.env.DP_WEB_FONTS) await page.route(/fonts\.(googleapis|gstatic)\.com/, route => route.abort());
  await page.route('**/api/events/stream', () => {});
  if (readOnly) {
    const readonly = authority => ({...authority, role: 'readonly', capabilities: ['interface', 'session', 'statistics.read', 'statistics.summary', 'transfers.read']});
    await page.route(url => url.pathname === '/app.js', async route => {
      const body = await (await route.fetch()).text();
      const marker = 'window.debridPulseAuthority=Object.freeze(';
      const [head, rest] = body.split(marker);
      const end = rest.indexOf(');');
      return route.fulfill({status: 200, contentType: 'application/javascript', body: head + marker + JSON.stringify(readonly(JSON.parse(rest.slice(0, end)))) + rest.slice(end)});
    });
    await page.route('**/api/auth/session', async route => route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify(readonly(await (await route.fetch()).json()))}));
  }
  await page.route('**/api/torrents**', async route => {
    const url = new URL(route.request().url());
    const move = url.pathname.match(/^\/api\/torrents\/(\d+)\/priority$/);
    if (move) {
      log.moves.push({id: Number(move[1]), ...route.request().postDataJSON()});
      return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({outcome: 'moved'})});
    }
    if (url.pathname === '/api/torrents/9701/route') return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(ROUTE)});
    if (url.pathname !== '/api/torrents') return route.fallback();
    log.gets++;
    const all = typeof items === 'function' ? items() : items;
    const limit = Number(url.searchParams.get('limit')) || 25, offset = Number(url.searchParams.get('offset')) || 0;
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({items: all.slice(offset, offset + limit), total: all.length})});
  });
  await page.setViewportSize({width, height});
  await page.goto('/');
  await expect(page.locator('#dash-tbody tr[data-torrent-id]').first()).toBeVisible();
  if (view === 'torrents') {
    await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]:not([data-title])')));
    await expect(page.locator('#t-tbody tr[data-torrent-id]').first()).toBeVisible();
  }
  await page.evaluate(() => document.fonts.ready);
  return log;
}
const settle = page => page.waitForTimeout(450);
const show = (page, view) => page.evaluate(v => nav(document.querySelector(`#sidebar .nav-item[data-view="${v}"]:not([data-title])`)), view);

/* One read of a table's handheld composition. */
const read = (page, view) => page.evaluate(view => {
  const tbody = document.getElementById(view === 'dashboard' ? 'dash-tbody' : 't-tbody');
  const table = tbody.closest('table'), wrap = table.parentElement;
  const shown = node => getComputedStyle(node).display !== 'none' && node.getClientRects().length > 0;
  const r = node => node.getBoundingClientRect();
  return {
    headings: [...table.querySelectorAll('thead th')].filter(shown).map(th => th.querySelector('#chk-all') ? 'select' : (th.firstChild?.textContent || '').trim()),
    over: wrap.scrollWidth - wrap.clientWidth,
    rows: [...tbody.querySelectorAll('tr[data-torrent-id]')].map(tr => {
      const cell = tr.children[0], meta = cell.querySelector('.dp-transfer-provider-meta'), main = meta.querySelector('.dp-transfer-meta-main');
      const size = meta.querySelector('.dp-recent-size, .dp-downloads-size'), name = cell.querySelector('.t-name');
      const value = cell.querySelector('.dp-transfer-summary-value'), slot = cell.querySelector('.dash-row-bar-slot');
      const track = cell.querySelector('.dp-transfer-summary-track .prog'), glyph = meta.querySelector('.dp-status-glyph');
      const badge = tr.querySelector('[data-role="transfer-status"] .badge');
      const essentials = [...main.children].filter(shown);
      const label = main.querySelector('.dp-provider-label, .dp-root-provider-name'), chip = label?.closest('.dp-provider-chip');
      const stage = meta.dataset.dpFit ?? '', userLabel = main.querySelector('.lbl-badge');
      // A text is cut when its run is wider than its box (an ellipsis is drawn for any overflow).
      const cut = node => { const range = document.createRange(); range.selectNodeContents(node); const style = getComputedStyle(node);
        return range.getBoundingClientRect().width > r(node).width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight)
          - parseFloat(style.borderLeftWidth) - parseFloat(style.borderRightWidth) + 0.05; };
      // The line at a given stage (the oracle re-lays it out, then restores the app's stage).
      const under = at => { if (at) meta.dataset.dpFit = at; else delete meta.dataset.dpFit;
        const shownLabel = userLabel && shown(userLabel), labelStyle = shownLabel && getComputedStyle(userLabel);
        const result = {provider: [...main.querySelectorAll('.dp-provider-label, .dp-root-provider-name')].some(cut),
          label: !!shownLabel && cut(userLabel), overflow: main.scrollWidth > main.clientWidth + 0.5,
          emptied: !!shownLabel && r(userLabel).width - parseFloat(labelStyle.paddingLeft) - parseFloat(labelStyle.paddingRight) < 2 * parseFloat(labelStyle.fontSize)};
        if (stage) meta.dataset.dpFit = stage; else delete meta.dataset.dpFit; return result; };
      const full = under(''), sized = under('size'), labelled = under(userLabel ? 'label' : 'size');
      // The provider's drawn text (its short label's, or its own), as painted: its run against its box, cut by every clipping ancestor.
      const painted = (() => { if (!label) return null;
        const short = stage === 'short' && label.dataset.shortLabel, style = getComputedStyle(label, short ? '::before' : null);
        const context = document.createElement('canvas').getContext('2d'); context.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`; context.letterSpacing = style.letterSpacing;
        const text = short || label.textContent, need = context.measureText(text).width, start = r(label).left + parseFloat(getComputedStyle(label).paddingLeft);
        let x0 = -Infinity, x1 = Infinity;
        for (let node = label; node !== tr; node = node.parentElement) { const box = getComputedStyle(node); if (box.overflowX === 'visible' || box.display === 'contents') continue;
          x0 = Math.max(x0, r(node).left + parseFloat(box.borderLeftWidth)); x1 = Math.min(x1, r(node).right - parseFloat(box.borderRightWidth)); }
        return {text, need, visible: Math.max(0, Math.min(x1, start + need) - Math.max(x0, start))}; })();
      const grip = cell.querySelector('.dp-queue-grip'), cellStyle = getComputedStyle(cell), launcher = main.querySelector('.dp-root-provider-launcher');
      const cellRight = r(cell).right - parseFloat(cellStyle.paddingRight) + 0.5;
      return {
        id: Number(tr.dataset.torrentId), height: Math.round(r(tr).height * 10) / 10,
        nameRight: r(name).right, valueLeft: value && shown(value) ? r(value).left : null, valueTop: value && shown(value) ? r(value).top : null, nameTop: r(name).top,
        valueMarkup: value && shown(value) ? [value.innerHTML, value.getAttribute('aria-label'), value.getAttribute('data-tooltip')] : null,
        columnValueMarkup: (pct => pct ? [pct.innerHTML, pct.getAttribute('aria-label'), pct.getAttribute('data-tooltip')] : null)(tr.querySelector('[data-role="transfer-progress"] .prog-pct')),
        trackMarkup: track ? track.outerHTML : null, columnTrackMarkup: tr.querySelector('[data-role="transfer-progress"] .prog')?.outerHTML ?? null,
        track: track && shown(track) ? {height: getComputedStyle(track).height, radius: getComputedStyle(track).borderRadius, width: r(track).width, slot: r(slot).width} : null,
        slot: {height: r(slot).height, shown: shown(slot)},
        legacyBar: shown(cell.querySelector('.dash-row-bar')),
        glyph: glyph && shown(glyph) ? {first: essentials[0] === glyph, name: glyph.getAttribute('aria-label')} : null,
        badgeLabel: badge?.getAttribute('data-tooltip') ?? null,
        sourceIcon: !!meta.querySelector('.dp-source-icon-slot') && shown(meta.querySelector('.dp-source-icon-slot')),
        metaHeight: Math.round(r(meta).height), sizeShown: shown(size) && r(size).top < r(meta).bottom - 0.5,
        stage, full, sized, labelled, painted, userLabel: userLabel ? (shown(userLabel) ? (cut(userLabel) ? 'truncated' : 'whole') : 'omitted') : null,
        grip: grip ? {gripLeft: r(grip).left, gripRight: r(grip).right, gripTop: r(grip).top, gripBottom: r(grip).bottom, cellLeft: r(cell).left,
          rowMiddle: (r(tr).top + r(tr).bottom) / 2, gripMiddle: (r(grip).top + r(grip).bottom) / 2, nameTop: r(name).top, metaBottom: r(meta).bottom,
          nameLeft: r(name).left, slotLeft: r(slot).left, metaLeft: r(meta).left, contentLeft: r(cell).left + parseFloat(cellStyle.paddingLeft)} : null,
        providerId: chip?.dataset.providerId || '', drawn: painted?.text, candidates: !!main.querySelector('.dp-candidate-chip'),
        launcher: launcher ? {padding: [getComputedStyle(launcher).paddingLeft, getComputedStyle(launcher).paddingRight, getComputedStyle(launcher).columnGap], height: r(launcher).height} : null,
        labelText: label?.textContent, labelTooltip: label?.getAttribute('data-tooltip') ?? null, labelShort: label?.dataset.shortLabel ?? null,
        actions: (cellNode => cellNode ? {width: r(cellNode).width, padding: [getComputedStyle(cellNode).paddingLeft, getComputedStyle(cellNode).paddingRight],
          buttons: [...cellNode.querySelectorAll('.actions .btn')].filter(shown).map(b => `${Math.round(r(b).width)}x${Math.round(r(b).height)}`),
          // Each button as drawn: its label physically absent (no box of its own, nothing painted), its glyph drawn, its name kept.
          drawn: [...cellNode.querySelectorAll('.actions .btn')].filter(shown).map(b => { const label = b.querySelector('[data-dp-icon-label]'), icon = b.querySelector('.dp-row-action-icon');
            return {labelGone: !label || (r(label).width <= 1 && r(label).height <= 1 && getComputedStyle(label).opacity === '0'), icon: !!icon && shown(icon) && r(icon).width >= 12,
              name: (b.getAttribute('aria-label') || label?.textContent || '').trim()}; }),
          gap: getComputedStyle(cellNode.querySelector('.actions') || cellNode).columnGap, last: tr.lastElementChild === cellNode || tr.lastElementChild.classList.contains('dp-downloads-select-cell')} : null)
          (tr.querySelector('.dp-downloads-action-cell') ?? (view === 'dashboard' && tr.children.length > 1 ? tr.lastElementChild : null)),
        // The columns as drawn: every one shown, its width; the selection cell's checkbox centre.
        columns: [...tr.children].filter(shown).map(td => r(td).width),
        checkbox: (box => box ? (r(box).left + r(box).right) / 2 - (r(box.closest('td')).left + r(box.closest('td')).right) / 2 : null)(tr.querySelector('.dp-downloads-select-cell input')),
        tableWidth: r(table).width,
        essentialsInside: essentials.every(node => r(node).left >= r(meta).left - 0.5 && r(node).right <= cellRight && r(node).top >= r(meta).top - 0.5 && r(node).bottom <= r(meta).bottom + 0.5),
        consentInActions: !!tr.querySelector('.dp-downloads-action-cell .dp-queue-consent-action') && shown(tr.querySelector('.dp-downloads-action-cell .dp-queue-consent-action')),
      };
    }),
  };
}, view);

test.describe('handheld transfer lists', () => {
  test('Download Activity is Summary | Action and Downloads Summary | Actions | selection, never wider than their viewports; one metadata line whose size gives way first; rows no taller; desktop from 780px', async ({page}) => {
    test.setTimeout(90_000);
    await open(page, ITEMS, {width: 779, view: 'dashboard'});
    for (const view of ['dashboard', 'torrents']) {
      await show(page, view);
      for (const width of [779, 600, 440, 414, 393, 390, 375, 362, 361, 360, 342, 341, 340, 336, 334, 330, 328, 326, 324, 322, 320]) {
        await page.setViewportSize({width, height: 900});
        await settle(page);
        const at = `${view} ${width}px`, t = await read(page, view);
        expect(t.headings, at).toEqual(view === 'dashboard' ? ['Name', 'Action'] : ['Name', 'Actions', 'select']);
        expect(t.over, `${at}: sideways scroll`).toBeLessThanOrEqual(0.5);
        for (const row of t.rows) {
          const where = `${at} #${row.id}`;
          expect(row.height, `${where}: taller than the 57px three-layer row`).toBeLessThanOrEqual(57.5);
          expect(row.metaHeight, `${where}: one metadata line`).toBe(20);
          // The order, each stage only where the one before does not fit: the size ("size"); a user label's text, then the
          // label once it has none ("label"); the providers' short names ("short") -- decided by the providers' own room.
          const needed = state => state.provider || state.overflow;
          expect(row.sizeShown, `${where}: the size shows exactly when no stage is taken`).toBe(row.stage === '');
          if (row.stage === '') expect(needed(row.full) || row.full.label, `${where}: the whole line fits`).toBe(false);
          if (row.stage) expect(needed(row.full) || row.full.label, `${where}: the size set aside only where the whole line does not fit`).toBe(true);
          if (row.stage === 'size') expect([needed(row.sized), row.sized.label && row.sized.emptied], `${where}: the line fits with the size aside`).toEqual([false, false]);
          if (row.stage === 'label') {
            expect(row.userLabel, `${where}: the label omitted`).toBe('omitted');
            expect(needed(row.sized) || row.sized.emptied, `${where}: a label omitted only where the provider needs its room or it has no text left`).toBe(true);
          }
          if (row.stage === 'short') {
            expect(needed(row.labelled), `${where}: shortened only where the provider itself has no room (never for a label)`).toBe(true);
            expect(row.drawn, `${where}: the short name`).toBe(SHORT[row.providerId]);
            expect([row.labelText.length > row.drawn.length, row.labelTooltip], `${where}: the full name stays its text and tooltip`).toEqual([true, row.labelText]);
          }
          if (['', 'size'].includes(row.stage) && row.userLabel) expect(row.userLabel, `${where}: the label shown`).not.toBe('omitted');
          // The provider's identity, as painted, whole: its every pixel, not merely its selection -- every row, every width.
          if (row.painted) expect(row.painted.visible + 0.5, `${where}: "${row.painted.text}" painted whole`).toBeGreaterThanOrEqual(row.painted.need);
          // The route launcher gives back padding (8px to 4px each side) and its name-caret gap (5px to 4px) only last: a
          // short-name line with a candidate launcher, in the narrowest Downloads table (below a 362px window). Its height
          // never changes.
          if (row.launcher) {
            const narrow = view === 'torrents' && width < 362 && row.stage === 'short' && row.candidates;
            expect([...row.launcher.padding, row.launcher.height], `${where}: route launcher padding`).toEqual(narrow ? ['4px', '4px', '4px', 20] : ['8px', '8px', '5px', 20]);
          }
          // A queue grip is the row's left-edge gutter: in the Name cell's left padding, from the cell's edge, over the whole
          // summary (the name's line to the metadata line's foot) and centred on the row; the name, slot and metadata line
          // start at the cell's content edge, as every row's do -- nothing overlaps it and it takes no width from them.
          if (row.grip) {
            const g = row.grip;
            expect([Math.abs(g.gripLeft - g.cellLeft) <= 0.5, g.gripRight <= g.contentLeft + 0.5], `${where}: grip in the left gutter`).toEqual([true, true]);
            expect([g.gripTop <= g.nameTop + 0.5, g.gripBottom >= g.metaBottom - 0.5, Math.abs(g.gripMiddle - g.rowMiddle) <= 1], `${where}: grip over the whole summary, centred on the row`).toEqual([true, true, true]);
            expect([g.nameLeft, g.slotLeft, g.metaLeft].map(x => Math.abs(x - g.contentLeft) <= 0.5), `${where}: summary at the content edge`).toEqual([true, true, true]);
          }
          // Every status, provider, candidate, file-selection and label control whole on the one line, 320px included.
          expect(row.essentialsInside, `${where}: an essential control off the line`).toBe(true);
          if (row.actions) {
            // Glyph-only 36px controls on both tables: the label physically absent, the glyph drawn, the name kept.
            expect(row.actions.buttons.every(size => size === '36x36'), `${where}: action buttons ${row.actions.buttons}`).toBe(true);
            expect(row.actions.drawn.every(b => b.labelGone && b.icon && b.name), `${where}: glyph-only actions ${JSON.stringify(row.actions.drawn)}`).toBe(true);
            expect([row.actions.gap, row.actions.last], `${where}: action gap (each table's own), Actions or selection last`).toEqual([view === 'dashboard' ? '4px' : '7px', true]);
            // 4px each side between the columns; Dashboard's outer edge keeps its 14px.
            expect(row.actions.padding, `${where}: Actions padding`).toEqual(view === 'dashboard' ? ['4px', '14px'] : ['4px', '4px']);
          }
          // The columns are the table: no phantom or dead track; Actions is its buttons and 8px (Downloads) or its one
          // button and its insets (Dashboard); the checkbox centred in its 36px track; Summary takes every other pixel.
          expect(Math.abs(row.columns.reduce((a, b) => a + b, 0) - row.tableWidth), `${where}: columns fill the table`).toBeLessThanOrEqual(0.6);
          if (row.actions && view === 'dashboard') expect(Math.round(row.actions.width), `${where}: Action track`).toBe(54);
          if (row.checkbox !== null) expect(Math.abs(row.checkbox), `${where}: checkbox centred`).toBeLessThanOrEqual(0.6);
          expect(row.valueLeft, `${where}: the value beside the name`).toBeGreaterThanOrEqual(row.nameRight);
          expect(Math.abs(row.valueTop - row.nameTop), `${where}: the value on the name's line`).toBeLessThanOrEqual(2);
          expect(row.glyph, `${where}: status leads the line`).toEqual({first: true, name: row.badgeLabel});
          expect([row.sourceIcon, row.legacyBar, row.slot.height], `${where}: no source icon, no legacy bar, the 3px slot`).toEqual([false, false, 3]);
          if (row.track) expect([row.track.height, row.track.radius, row.track.width], where).toEqual(['3px', '1px', row.track.slot]);
        }
        expect(t.rows.find(row => row.id === 9706)?.track ?? null, `${at}: a completed transfer draws no track`).toBe(null);
        if (view === 'torrents') expect(t.rows.find(row => row.id === 9703).consentInActions, `${at}: Start Now in Actions`).toBe(true);
      }
      // Back at 779px every row has room again: each size and label set aside at 320px returns. (Downloads sets some
      // aside at 320px in either face; Download Activity, with the Summary's whole width, may keep every one in Outfit.)
      if (view === 'torrents' || !process.env.DP_WEB_FONTS) expect((await read(page, view)).rows.some(row => !row.sizeShown), `${view} 320px: some size set aside`).toBe(true);
      await page.setViewportSize({width: 779, height: 900});
      await settle(page);
      expect((await read(page, view)).rows.every(row => row.sizeShown && row.userLabel !== 'omitted' && row.stage === ''), `${view} 779px: every size and label back`).toBe(true);
    }
    // From 780px: the desktop tables, their legacy name bar, no summary.
    await page.setViewportSize({width: 780, height: 900});
    await settle(page);
    for (const view of ['dashboard', 'torrents']) {
      await show(page, view);
      await settle(page);
      const t = await read(page, view);
      expect(t.headings, `${view} 780px`).toEqual(view === 'dashboard' ? ['Name', 'Status', 'Progress', 'Action'] : ['Name', 'Status', 'Progress', 'Actions', 'select']);
      expect(t.rows.every(row => row.valueMarkup === null && row.glyph === null && row.track === null && !row.consentInActions)).toBe(true);
    }
  });

  test('the summary value and thin track are the progress column\'s own projection, in every state; terminal transfers draw no track; a live update moves both alike', async ({page}) => {
    await open(page, [...STATES, ...ITEMS], {width: 390, height: 1100});
    for (const view of ['torrents', 'dashboard']) {
      await show(page, view);
      await settle(page);
      // The same row at 780px renders the progress column; below, the summary. Both come from one projection:
      // read the summary here, then the column from the same rows at 780px.
      const handheld = await read(page, view);
      await page.setViewportSize({width: 1440, height: 900});
      await settle(page);
      const desktop = await read(page, view);
      await page.setViewportSize({width: 390, height: 900});
      await settle(page);
      const both = handheld.rows.filter(row => desktop.rows.some(entry => entry.id === row.id));
      if (view === 'torrents') expect(both.map(row => row.id), 'every progress state compared').toEqual(expect.arrayContaining(STATES.map(state => state.id)));
      expect(both.length, view).toBeGreaterThanOrEqual(4);
      for (const row of both) {
        const column = desktop.rows.find(entry => entry.id === row.id);
        expect(row.valueMarkup, `${view} #${row.id}: value`).toEqual(column.columnValueMarkup);
        const terminal = ['completed', 'cancelled'].includes([...STATES, ...ITEMS].find(item => item.id === row.id).presentation_status);
        expect(row.trackMarkup, `${view} #${row.id}: track`).toBe(terminal ? null : column.columnTrackMarkup);
      }
    }
    // The figures themselves (Downloads): never a fabricated 0% or a rounded raw progress.
    await show(page, 'torrents');
    await settle(page);
    const figure = id => page.locator(`#t-tbody tr[data-torrent-id="${id}"] .dp-transfer-summary-value`);
    await expect(figure(9711)).toHaveText(/^12\.3%/);          // execution only: Math.round(progress) would say 0%
    await expect(figure(9712)).toHaveText('—');                 // unknown total
    await expect(figure(9713)).toHaveText(/^94\.6%/);
    await expect(figure(9714)).toHaveText('processing');
    await expect(figure(9715)).toHaveText(/^31%/);
    await expect(page.locator('#t-tbody tr[data-torrent-id="9712"] .dp-transfer-summary-track .prog-fill')).toHaveClass(/is-indeterminate/);
    await expect(page.locator('#t-tbody tr[data-torrent-id="9717"] .dp-transfer-summary-track .prog')).toHaveCount(1);   // failed is not terminal
    await expect(page.locator('#t-tbody tr[data-torrent-id="9716"] .dp-transfer-summary-track')).toHaveCount(0);
    // A live progress update (the SSE path) moves the summary as the full render would.
    await page.evaluate(() => patchProgressOnlyTransferEvent({progress_only: true, items: [{id: 9715, progress: 57, active_execution_progress: 80, active_execution_basis: 'bytes'}]}));
    const patched = await page.evaluate(() => {
      const tr = document.querySelector('#t-tbody tr[data-torrent-id="9715"]');
      return {value: tr.querySelector('.dp-transfer-summary-value').outerHTML, track: tr.querySelector('.dp-transfer-summary-track').innerHTML,
        expected: (() => { const parts = progressParts(57, 'downloading', 80, 'bytes', null); return [progressSummaryValue(parts), parts.track]; })()};
    });
    expect([patched.value, patched.track]).toEqual(patched.expected);
    await expect(figure(9715)).toHaveText(/^57%/);
  });

  test('one canonical status glyph per row, the same on both tables: no second mark, the same glyph, ink and box in dark and light, kept through a live change', async ({page}) => {
    let items = ITEMS.map(item => ({...item}));
    await open(page, () => items, {width: 390, height: 1400, view: 'dashboard'});
    const marks = view => page.evaluate(view => [...document.getElementById(view === 'dashboard' ? 'dash-tbody' : 't-tbody').querySelectorAll('tr[data-torrent-id]')].map(tr => {
      const cell = tr.children[0], shown = node => getComputedStyle(node).display !== 'none' && node.getClientRects().length > 0;
      const pseudo = node => ['::before', '::after'].map(p => getComputedStyle(node, p).content).filter(c => c && c !== 'none' && c !== 'normal');
      // Every status mark: a shown element carrying a status tone class, and any generated content on one or inside it.
      const status = [...cell.querySelectorAll('[class*="badge-"]')].filter(shown);
      const generated = status.flatMap(node => [node, ...node.querySelectorAll('*')]).flatMap(pseudo);
      const glyph = cell.querySelector('.dp-status-glyph'), svg = glyph?.querySelector('svg'), g = glyph && getComputedStyle(glyph), v = svg && getComputedStyle(svg);
      return {id: tr.dataset.torrentId, status: glyph?.dataset.dpStatus, count: status.length, generated, icon: svg?.dataset.dpLucide,
        look: glyph && [g.color, g.backgroundColor, g.backgroundImage, g.boxShadow, g.width, g.height, g.borderRadius, g.borderTopWidth, v.width, v.height, v.stroke, v.strokeWidth, v.filter].join(' | ')};
    }), view);
    for (const theme of ['dark', 'light']) {
      await page.evaluate(light => document.body.classList.toggle('light', light), theme === 'light');
      await settle(page);
      await show(page, 'dashboard'); await settle(page);
      const dashboard = await marks('dashboard');
      await show(page, 'torrents'); await settle(page);
      const downloads = await marks('torrents');
      for (const row of [...dashboard, ...downloads]) {
        expect([row.count, row.generated], `${theme} #${row.id}: one status mark, nothing generated`).toEqual([1, []]);
      }
      for (const row of dashboard) {
        const twin = downloads.find(entry => entry.id === row.id);
        if (!twin) continue;
        expect([row.status, row.icon, row.look], `${theme} #${row.id}: the same glyph on both tables`).toEqual([twin.status, twin.icon, twin.look]);
      }
    }
    // A live change of status: each table, refreshed through its own owner, still draws one mark of the new status.
    items = items.map(item => item.id === 9705 ? {...item, status: 'completed', presentation_status: 'completed', presentation_label: 'Done', presentation_badge_status: 'completed', progress: 100} : item);
    await page.evaluate(() => loadTorrents());
    await settle(page);
    await show(page, 'dashboard'); await page.evaluate(() => loadRecent()); await settle(page);
    for (const view of ['dashboard', 'torrents']) {
      if (view === 'torrents') { await show(page, 'torrents'); await settle(page); }
      const row = (await marks(view)).find(entry => entry.id === '9705');
      expect([row.count, row.generated, row.status], `${view} #9705 after a live change`).toEqual([1, [], 'completed']);
    }
  });

  test('Start Now from the handheld Actions column reaffirms the queue as before', async ({page}) => {
    const log = await open(page, ITEMS, {width: 390});
    const start = page.locator('#t-tbody tr[data-torrent-id="9703"] .dp-downloads-action-cell .dp-queue-consent-action');
    await expect(start).toBeVisible();
    await expect(start).toHaveAccessibleName('Start Now');
    await expect(page.locator('#t-tbody .dp-queue-consent')).toHaveCount(1);   // the desktop's, in Status, not shown here
    await start.click();
    await expect.poll(() => log.moves.length).toBeGreaterThan(0);
    expect(log.moves[0]).toMatchObject({id: 9703, reaffirm: true});
  });

  test('a viewer who cannot operate gets the Transfer Summary alone: no Action, Actions or selection column', async ({page}) => {
    await open(page, ITEMS, {width: 390, readOnly: true, view: 'dashboard'});
    for (const view of ['dashboard', 'torrents']) {
      await show(page, view);
      await settle(page);
      const t = await read(page, view);
      expect(t.headings, view).toEqual(['Name']);
      expect(t.over, view).toBeLessThanOrEqual(0.5);
    }
  });

  test('short names: the identity owner\'s, drawn only where needed at 320px, full again with room; the full name stays the text, tooltip and name; action focus is whole', async ({page}) => {
    await open(page, ITEMS, {width: 320, height: 1100});
    const names = view => page.evaluate(view => [...document.getElementById(view === 'dashboard' ? 'dash-tbody' : 't-tbody').querySelectorAll('tr[data-torrent-id]')].map(tr => {
      const meta = tr.querySelector('.dp-transfer-provider-meta'), label = meta.querySelector('.dp-provider-label, .dp-root-provider-name');
      return [Number(tr.dataset.torrentId), meta.dataset.dpFit === 'short' && label?.dataset.shortLabel ? label.dataset.shortLabel : label?.textContent];
    }), view);
    // 320px: a long catalog name shortened only where its own line has no room for it -- with the columns' 8px gutters,
    // only Debrid-Link beside the queue grip and its candidate launcher, and Media Downloads; Usenet is already short.
    expect(Object.fromEntries(await names('torrents'))).toEqual({9701: 'DL', 9702: 'AllDebrid', 9703: 'Premiumize', 9704: 'TorBox', 9705: 'Real-Debrid', 9706: 'Usenet', 9707: 'Media'});
    await expect(page.locator('#t-tbody tr[data-torrent-id="9704"] .lbl-badge')).toBeHidden();
    // Download Activity has room for every full name at 320px.
    await show(page, 'dashboard');
    await settle(page);
    expect((await names('dashboard')).every(([, name]) => !Object.values(SHORT).includes(name))).toBe(true);
    await show(page, 'torrents');
    await settle(page);
    // The short label's DP tooltip is the full name.
    await page.locator('#t-tbody tr[data-torrent-id="9707"] .dp-provider-label').hover();
    await expect(page.locator('#dp-tooltip')).toHaveText('Media Downloads');
    await page.mouse.move(0, 0);
    const launcher = page.locator('#t-tbody tr[data-torrent-id="9701"] .dp-root-provider-launcher');
    await expect(launcher).toHaveAccessibleName(/^Provider: Debrid-Link/);
    // At 320px, with its narrowed padding, the launcher still opens its picker and closes it, focus returning.
    await launcher.click();
    await expect(launcher).toHaveAttribute('aria-expanded', 'true');
    await expect(page.locator('[role="dialog"] .dp-root-provider-switch')).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(launcher).toHaveAttribute('aria-expanded', 'false');
    await expect(launcher).toBeFocused();
    // A row action, keyboard-focused at 320px: its focus ring lies inside its cell.
    const remove = page.locator('#t-tbody tr[data-torrent-id="9702"] .actions .btn').last();
    await remove.focus();
    await page.keyboard.press('Shift');
    const ring = await remove.evaluate(button => { const style = getComputedStyle(button), cell = button.closest('td').getBoundingClientRect(), box = button.getBoundingClientRect();
      const reach = (parseFloat(style.outlineWidth) || 0) + (parseFloat(style.outlineOffset) || 0);
      return {drawn: style.outlineStyle !== 'none' || style.boxShadow !== 'none', inside: box.left - reach >= cell.left - 0.5 && box.right + reach <= cell.right + 0.5}; });
    expect(ring).toEqual({drawn: true, inside: true});
    // Room again: full names return.
    await page.setViewportSize({width: 779, height: 1100});
    await settle(page);
    expect((await names('torrents')).every(([, name]) => !Object.values(SHORT).includes(name))).toBe(true);
    await expect(page.locator('#t-tbody tr[data-torrent-id="9704"] .lbl-badge')).toBeVisible();
  });

  test('the handheld queue: a drag from the grip and the keyboard reorder as on the desktop', async ({page}) => {
    for (const width of [390, 320]) {
      const log = await open(page, ITEMS, {width});
      const box = async selector => { let found = null; await expect.poll(async () => (found = await page.locator(selector).boundingBox()) !== null).toBe(true); return found; };
      const start = await box('#t-tbody tr[data-torrent-id="9704"] .dp-queue-grip');
      await page.mouse.move(start.x + start.width / 2, start.y + start.height / 2);
      await page.mouse.down();
      await page.mouse.move(start.x + start.width / 2, start.y + start.height / 2 + 12, {steps: 4});
      const end = await box('#t-tbody tr[data-torrent-id="9701"]');
      await page.mouse.move(end.x + end.width / 2, end.y + end.height * 0.25, {steps: 12});
      await page.mouse.up();
      await expect.poll(() => log.moves.length, `${width}px drag`).toBe(1);
      expect(log.moves[0], `${width}px drag`).toMatchObject({id: 9704, before_id: 9701});
      await expect(page.locator('#transfer-detail-modal.show, .modal.show')).toHaveCount(0);
      const cell = page.locator('#t-tbody tr[data-torrent-id="9705"] td[data-queue-drag="true"]');
      await cell.focus();
      await page.keyboard.press('Space');
      await page.keyboard.press('ArrowUp');
      await page.keyboard.press('Enter');
      await expect.poll(() => log.moves.length, `${width}px keyboard`).toBe(2);
      expect(log.moves[1].id, `${width}px keyboard`).toBe(9705);
      // Touch, as a finger's pointer: on the name it never picks the row up (it scrolls); on the grip it drags.
      const touch = (from, to) => page.evaluate(([from, to]) => {
        const a = document.querySelector(from), r = a.getBoundingClientRect(), b = document.querySelector(to).getBoundingClientRect();
        const fire = (target, type, x, y) => target.dispatchEvent(new PointerEvent(type, {bubbles: true, cancelable: true, pointerId: 7, pointerType: 'touch',
          isPrimary: true, button: 0, buttons: type === 'pointerup' ? 0 : 1, clientX: x, clientY: y}));
        const x = r.left + r.width / 2, y = r.top + r.height / 2, endY = b.top + b.height * 0.25;
        fire(a, 'pointerdown', x, y);
        for (let k = 1; k <= 12; k++) fire(document, 'pointermove', x, y + (endY - y) * k / 12);
        fire(document, 'pointerup', x, endY);
      }, [from, to]);
      await touch('#t-tbody tr[data-torrent-id="9704"] .t-name', '#t-tbody tr[data-torrent-id="9701"]');
      await page.waitForTimeout(300);
      expect(log.moves.length, `${width}px: a touch on the name moves nothing`).toBe(2);
      await touch('#t-tbody tr[data-torrent-id="9704"] .dp-queue-grip', '#t-tbody tr[data-torrent-id="9701"]');
      await expect.poll(() => log.moves.length, `${width}px touch on the grip`).toBe(3);
      expect(log.moves[2], `${width}px touch on the grip`).toMatchObject({id: 9704, before_id: 9701});
    }
    // The keyboard carries a held row across the page's lower edge, as on the desktop: past the last row, onto the next page.
    const log = await open(page, UNIFORM, {width: 390, height: 640});
    const ids = await page.locator('#t-tbody tr[data-torrent-id]').evaluateAll(rows => rows.map(row => Number(row.dataset.torrentId)));
    const last = ids.at(-1);
    await page.locator(`#t-tbody tr[data-torrent-id="${last}"] td[data-queue-drag="true"]`).focus();
    await page.keyboard.press('Space');
    await page.keyboard.press('ArrowDown');
    await expect.poll(async () => (await page.locator('#t-tbody tr[data-torrent-id]').first().getAttribute('data-torrent-id')), {timeout: 4000}).toBe(String(last + 1));
    await page.keyboard.press('Enter');
    await expect.poll(() => log.moves.length).toBe(1);
    expect(log.moves[0], 'cross-page keyboard move').toMatchObject({id: last, after_id: last + 1});
  });

  test('capacity: Downloads pages only the rows its viewport and one screen hold, its pager above the dock with no inner scroll; Download Activity fits one screen; stable', async ({page}) => {
    test.setTimeout(60_000);
    const log = await open(page, UNIFORM, {width: 390, view: 'dashboard'});
    for (const [width, height] of [[320, 568], [320, 640], [360, 640], [375, 667], [393, 852], [440, 956], [600, 900], [779, 1000]]) {
      await page.setViewportSize({width, height});
      for (const view of ['dashboard', 'torrents']) {
        await show(page, view);
        // Settled: no list fetch for a whole second (a capacity change refetches once), then none in the next.
        for (let last = -1, quiet = 0; quiet < 1000; ) { if (log.gets !== last) { last = log.gets; quiet = 0; } await page.waitForTimeout(100); quiet += 100; }
        const before = log.gets;
        await page.waitForTimeout(1000);
        expect(log.gets - before, `${view} ${width}x${height}: refetching after it settled`).toBe(0);
        const fit = await page.evaluate(view => {
          const content = document.getElementById('content'), space = getComputedStyle(content), box = content.getBoundingClientRect();
          content.scrollTop = 0;
          // The workspace's inset above the dock: the content box's bottom.
          const inset = box.top + content.clientTop + content.clientHeight - parseFloat(space.paddingBottom);
          const dock = document.getElementById('shell-dock').getBoundingClientRect();
          const card = view === 'dashboard' ? document.getElementById('dash-activity-card') : document.querySelector('#view-torrents > .card');
          const wrap = card.querySelector(view === 'dashboard' ? '.dash-activity-table-wrap' : '.dp-downloads-table-wrap');
          const rows = [...card.querySelectorAll('tbody tr[data-torrent-id]')], last = rows.at(-1)?.getBoundingClientRect();
          const rowHeight = rows[0]?.getBoundingClientRect().height ?? 0;
          const pager = view === 'dashboard' ? null : document.getElementById('torrent-pagination').getBoundingClientRect();
          return {cardBottom: card.getBoundingClientRect().bottom, inset, dockTop: dock.top, rows: rows.length, innerScroll: wrap.scrollHeight - wrap.clientHeight,
            headBottom: wrap.querySelector('thead').getBoundingClientRect().bottom, below: card.getBoundingClientRect().bottom - wrap.getBoundingClientRect().bottom,
            pageScroll: content.scrollHeight - content.clientHeight, lastBottom: last?.bottom ?? null, wrapBottom: wrap.getBoundingClientRect().bottom,
            slack: wrap.getBoundingClientRect().bottom - (last?.bottom ?? wrap.querySelector('thead').getBoundingClientRect().bottom), rowHeight,
            pagerBottom: pager?.bottom ?? null, count: document.getElementById('dash-activity-count')?.textContent ?? ''};
        }, view);
        const at = `${view} ${width}x${height}`;
        expect(fit.inset, `${at}: the inset above the dock`).toBeLessThanOrEqual(fit.dockTop + 0.5);
        expect(fit.innerScroll, `${at}: no inner scroll`).toBe(0);
        // The least the card can be: its own header and table head, and beneath them (Downloads) one row and its pager.
        const least = fit.headBottom + (view === 'dashboard' ? 0 : fit.rowHeight) + fit.below;
        if (least > fit.inset + 0.5) {
          // A screen too short for that alone: the page scrolls by exactly the overrun, nothing clipped; Download Activity
          // draws no row (none fits whole), Downloads its one.
          expect(fit.rows, `${at}: too short -- rows`).toBe(view === 'dashboard' ? 0 : 1);
          expect(Math.abs(fit.pageScroll - (fit.cardBottom - fit.inset)), `${at}: too short -- the page scrolls by the overrun`).toBeLessThanOrEqual(1);
          if (view === 'dashboard') expect(fit.count, `${at}: too short -- count`).toBe('0 downloads shown');
          continue;
        }
        // The card ends at the workspace's inset above the dock; every row whole and above it; no page scroll.
        expect(Math.abs(fit.cardBottom - fit.inset), `${at}: card bottom at the inset (${fit.cardBottom} vs ${fit.inset})`).toBeLessThanOrEqual(0.6);
        expect(fit.pageScroll, `${at}: no page scroll`).toBe(0);
        if (fit.rows) expect(fit.lastBottom, `${at}: last row whole`).toBeLessThanOrEqual(fit.wrapBottom + 0.5);
        // Only complete rows that fit: no room for another whole one.
        expect(fit.slack, `${at}: room left for another row`).toBeLessThan(fit.rowHeight);
        if (view === 'dashboard') expect(fit.count, `${at}: the count is the rows drawn`).toBe(`${fit.rows} download${fit.rows === 1 ? '' : 's'} shown`);
        else expect(fit.pagerBottom, `${at}: pager inside the card, above the dock`).toBeLessThanOrEqual(fit.cardBottom + 0.5);
      }
    }
  });
});
