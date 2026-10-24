const { test, expect } = require('@playwright/test');

/* DP 1.0.14 desktop table geometry. The Dashboard's Recent Activity and the
 * Downloads table keep their full desktop composition through the whole
 * desktop (from 1390 CSS px, with the full sidebar from 1560px and the compact
 * rail below it): every Downloads column (Size and Date included), and the
 * Dashboard's fixed proportional tracks with Status and Date Added never cut.
 * Recent Activity's tracks are the table's own, not the Dashboard's six- or
 * three-card composition: they carry on through 1390/1389px and the tablet
 * (to 780px), and only on mobile does it size its columns to their content.
 * Below 1390px Downloads still sets Size and Date aside. Mocked rows: no
 * backend state is read or written. */

const DESKTOP = [1600, 1560, 1559, 1500, 1440, 1439, 1400, 1390];

function row(id, o) {
  return {
    id, name: `Row ${id}`, status: 'downloading', presentation_status: 'downloading',
    presentation_label: 'Downloading', presentation_badge_status: 'downloading', attention_required: false,
    progress: 40, active_execution_progress: null, active_execution_basis: null, size_bytes: 1000, source: 'manual',
    hash: 'a1b2c3d4e5f60718293a4b5c6d7e8f90', label: '', created_at: '2025-12-28T22:54:00Z',
    current_source_identity: {kind: 'link'}, providers: [], historical_providers: [], delivering_provider_ids: [],
    input_required: null, acquisition_eta: null, ...o,
  };
}

// Wide content: a long name, the widest provider names, large sizes, a year-bearing date, long
// estimates and badges. "Awaiting confirmation" is left out of the fit checks below: in the
// test's fallback font it is 3.6px wider than the 180px both tables give Status (as before).
const ITEMS = [
  row(9901, {name: 'The.Extremely.Long.Release.Name.That.Never.Ends.2025.2160p.UHD.BluRay.REMUX.HDR10.DV.HEVC.TrueHD.Atmos.7.1-GROUP.mkv',
    active_provider_id: 'media', active_provider_name: 'Media Downloads', source: 'alldebrid_existing', progress: 0,
    active_execution_progress: 100, active_execution_basis: 'bytes', size_bytes: 987.6 * 1024 ** 4,
    acquisition_eta: {seconds: 999 * 3600 + 59 * 60 + 59, held: false, estimated: true}}),
  row(9902, {name: 'Short.mkv', active_provider_id: 'general_http', active_provider_name: 'HTTP & HTTPS', source: 'direct_link',
    progress: 41, active_execution_progress: 76, active_execution_basis: 'bytes', size_bytes: 123.45 * 1024 ** 3,
    acquisition_eta: {seconds: 2318, held: true}}),
  row(9903, {name: 'Paused.Archive.part07.rar', status: 'paused', presentation_status: 'paused', presentation_label: 'Paused',
    presentation_badge_status: 'paused', active_provider_id: 'realdebrid', active_provider_name: 'Real-Debrid', progress: 67,
    size_bytes: 9.99 * 1024 ** 3}),
  row(9904, {name: 'Failed.Download.zip', status: 'error', presentation_status: 'error', presentation_label: 'Execution missing',
    presentation_badge_status: 'error', active_provider_id: 'debridlink', active_provider_name: 'Debrid-Link', progress: 3,
    size_bytes: 1.5 * 1024 ** 3}),
  row(9905, {name: 'Refreshing.Source.tar', status: 'queued', presentation_status: 'refresh_pending',
    presentation_label: 'Refreshing source', presentation_badge_status: 'refresh_pending', active_provider_id: 'premiumize',
    active_provider_name: 'Premiumize', progress: 0, size_bytes: 77 * 1024 ** 2}),
  row(9906, {name: 'Completed.Item.mkv', status: 'completed', presentation_status: 'completed', presentation_label: 'Completed',
    presentation_badge_status: 'completed', active_provider_id: 'torbox', active_provider_name: 'TorBox', progress: 100,
    size_bytes: 4.2 * 1024 ** 3}),
];

let requests = [];

async function open(page, view, {total = ITEMS.length} = {}) {
  requests = [];
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.route(url => url.pathname === '/api/torrents', route => {
    requests.push(new URL(route.request().url()).searchParams);
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({items: ITEMS, total})});
  });
  await page.setViewportSize({width: 1600, height: 1000});
  await page.goto('/');
  await page.evaluate(async view => {
    nav(document.querySelector(`#sidebar .nav-item[data-view="${view}"]`));
    if (view === 'torrents') await loadTorrents(); else await loadRecent();
  }, view);
  await expect(page.locator(`${view === 'torrents' ? '#t-tbody' : '#dash-tbody'} tr[data-torrent-id="9901"]`)).toBeVisible();
}

// The sidebar's width transition has finished (the real CSS transition, not a sleep).
async function resize(page, width) {
  await page.setViewportSize({width, height: 1000});
  await page.waitForFunction(() =>
    document.getElementById('sidebar').getAnimations().every(animation => animation.playState !== 'running'));
}

// One read of a table as it stands: its columns, and every content-fit fact the tests assert.
const geometry = (page, view) => page.evaluate(view => {
  const root = document.querySelector(view === 'torrents' ? '#view-torrents' : '#view-dashboard');
  const wrap = root.querySelector(view === 'torrents' ? '.dp-downloads-table-wrap' : '.dash-activity-table-wrap');
  const table = root.querySelector('.t-table');
  const heads = [...table.querySelectorAll('thead th')];
  const lines = node => {
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT), tops = new Set();
    for (let text; (text = walker.nextNode());) {
      if (!text.textContent.trim()) continue;
      const range = document.createRange();
      range.selectNodeContents(text);
      for (const rect of range.getClientRects()) if (rect.width > 0) tops.add(Math.round(rect.top));
    }
    return tops.size;
  };
  const columns = heads.map(th => ({
    label: (th.firstChild?.textContent || '').trim(), shown: getComputedStyle(th).display !== 'none',
    left: th.getBoundingClientRect().left, width: th.getBoundingClientRect().width,
  }));
  const misfits = [];
  for (const tr of table.querySelectorAll('tbody tr[data-torrent-id]')) {
    [...tr.children].forEach((td, index) => {
      if (index === 0 || getComputedStyle(td).display === 'none') return;
      const label = columns[index].label || 'select', style = getComputedStyle(td), cell = td.getBoundingClientRect();
      // Content inside the cell's padding; the selection checkbox, by design, only inside the cell.
      const select = td.classList.contains('dp-downloads-select-cell');
      const right = cell.right - (select ? 0 : parseFloat(style.paddingRight)) + 0.75;
      const left = cell.left + (select ? 0 : parseFloat(style.paddingLeft)) - 0.75;
      for (const node of td.querySelectorAll('*')) {
        const r = node.getBoundingClientRect();
        if (!r.width || node.closest('.dp-date-menu')) continue;
        if (r.right > right || r.left < left) { misfits.push(`${tr.dataset.torrentId} ${label}: outside its cell`); break; }
        if (node.scrollWidth > node.clientWidth + 1 && node.clientWidth > 0 && getComputedStyle(node).overflowX !== 'visible') {
          misfits.push(`${tr.dataset.torrentId} ${label}: cut`);
          break;
        }
      }
      if (/^(Size|Date|Date Added)$/.test(label) && lines(td) > 1) misfits.push(`${tr.dataset.torrentId} ${label}: wraps`);
      const badge = td.querySelector('.badge');
      if (badge && lines(badge) > 1) misfits.push(`${tr.dataset.torrentId} ${label}: badge wraps`);
    });
  }
  for (const th of heads) if (getComputedStyle(th).display !== 'none' && lines(th) > 1) misfits.push(`heading ${th.firstChild?.textContent.trim()} wraps`);
  const w = wrap.getBoundingClientRect(), t = table.getBoundingClientRect();
  return {columns, misfits, layout: getComputedStyle(table).tableLayout,
          wrap: {left: w.left, right: w.right, width: w.width}, tableRight: t.right,
          wrapScrolls: wrap.scrollWidth > wrap.clientWidth + 1,
          pageScrolls: document.documentElement.scrollWidth > innerWidth,
          sidebar: document.getElementById('sidebar').getBoundingClientRect().width};
}, view);

const shown = g => g.columns.filter(column => column.shown).map(column => column.label || 'select');

test('Downloads keeps every column, whole, through the desktop with either sidebar', async ({ page }) => {
  await open(page, 'torrents');
  for (const theme of ['dark', 'light']) {
    await page.evaluate(theme => document.body.classList.toggle('light', theme === 'light'), theme);
    for (const width of DESKTOP) {
      await resize(page, width);
      const g = await geometry(page, 'torrents'), where = `${theme} ${width}px`;
      expect(g.sidebar, where).toBe(width >= 1560 ? 240 : 72);
      expect(shown(g), where).toEqual(['Name', 'Provider / Source', 'Status', 'Progress', 'Size', 'Date', 'Actions', 'select']);
      expect(g.misfits, where).toEqual([]);
      expect(g.wrapScrolls || g.pageScrolls, where).toBe(false);
      expect(Math.abs(g.tableRight - g.wrap.right), where).toBeLessThanOrEqual(1);
      const [name, provider] = g.columns;
      expect(name.width, where).toBeGreaterThanOrEqual(150);       // Name and Provider never squeezed for the
      expect(provider.width, where).toBeGreaterThanOrEqual(160);   // secondary columns
    }
  }
  // The tablet band below is unchanged: Size and Date step aside from 1389px.
  await resize(page, 1389);
  expect(shown(await geometry(page, 'torrents'))).toEqual(['Name', 'Provider / Source', 'Status', 'Progress', 'Actions', 'select']);
});

test('Recent Activity keeps its fixed desktop tracks through the desktop, Status and Date Added never cut', async ({ page }) => {
  await open(page, 'dashboard');
  const {track, inset} = await page.evaluate(() => {
    const style = getComputedStyle(document.getElementById('view-dashboard'));
    return {track: parseFloat(style.getPropertyValue('--dp-dashboard-action-track')),
            inset: parseFloat(style.getPropertyValue('--dp-dashboard-action-inset'))};
  });
  for (const theme of ['dark', 'light']) {
    await page.evaluate(theme => document.body.classList.toggle('light', theme === 'light'), theme);
    for (const width of DESKTOP) {
      await resize(page, width);
      const g = await geometry(page, 'dashboard'), where = `${theme} ${width}px`;
      expect(g.layout, where).toBe('fixed');
      expect(shown(g), where).toEqual(['Name', 'Status', 'Progress', 'Size', 'Date Added', 'Action']);
      expect(g.misfits, where).toEqual([]);
      expect(g.wrapScrolls || g.pageScrolls, where).toBe(false);
      const [name, status, progress, size, date, action] = g.columns, W = g.wrap.width;
      // The proportions where they hold, the floors where they would cut, Name the rest.
      expect(Math.abs(status.width - Math.max(0.13 * W - 2.21, 180)), where).toBeLessThanOrEqual(1);
      expect(Math.abs(progress.width - (0.21 * W - 3.57)), where).toBeLessThanOrEqual(1);
      expect(Math.abs(size.width - (0.09 * W - 1.53)), where).toBeLessThanOrEqual(1);
      expect(Math.abs(date.width - Math.max(0.21 * W - track - 3.57, 158)), where).toBeLessThanOrEqual(1);
      expect(Math.abs(action.width - (track + inset)), where).toBeLessThanOrEqual(1);
      expect(Math.abs(name.left - g.wrap.left), where).toBeLessThanOrEqual(1);
      expect(Math.abs(action.left + action.width - g.wrap.right), where).toBeLessThanOrEqual(1);
      expect(name.width, where).toBeGreaterThanOrEqual(400);
    }
  }
  // The Dashboard's 1390/1389px composition change leaves the table alone:
  // the same fixed tracks, each column within the one pixel the window lost.
  await page.evaluate(() => document.body.classList.remove('light'));
  await resize(page, 1390);
  const at1390 = await geometry(page, 'dashboard');
  await resize(page, 1389);
  const at1389 = await geometry(page, 'dashboard');
  expect(at1389.layout).toBe('fixed');
  at1389.columns.forEach((column, index) => {
    expect(column.shown).toBe(true);
    expect(Math.abs(column.width - at1390.columns[index].width), column.label).toBeLessThanOrEqual(1);
  });
  // Through the tablet the same tracks hold -- proportions, floors, the action
  // track -- with every column present. Name, the complement, never goes
  // below its 280px floor (reached near a 1200px window); narrower, the
  // tracks together are wider than the card and the table overflows it,
  // left visible as the next column's fit limit. Where the table itself is
  // 987px or narrower its controls are compact (icon only): Status takes a
  // 63px track and the actions a 64px one, Date Added steps aside (heading,
  // cells and track), and Progress is the column that flexes -- all the room
  // left -- so Name rests at 280px. There the table's legacy 760px minimum
  // yields, so the table is the card's width all the way down to 780px.
  // (Whether each column's content still fits depends on the font -- this
  // file blocks the web font -- so that is measured with the production font.)
  for (const width of [1300, 1200, 1199, 1150, 1100, 1000, 950, 900, 878, 877, 850, 800, 780]) {
    await resize(page, width);
    const g = await geometry(page, 'dashboard'), where = `${width}px`, W = g.wrap.width;
    expect(g.layout, where).toBe('fixed');
    if (W > 987) {
      expect(shown(g), where).toEqual(['Name', 'Status', 'Progress', 'Size', 'Date Added', 'Action']);
      const [name, status, progress, size, date, action] = g.columns;
      const others = [Math.max(0.13 * W - 2.21, 180), 0.21 * W - 3.57, 0.09 * W - 1.53,
        Math.max(0.21 * W - track - 3.57, 158), track + inset];
      [status, progress, size, date, action].forEach((column, index) =>
        expect(Math.abs(column.width - others[index]), `${where} ${column.label}`).toBeLessThanOrEqual(1));
      const complement = W - others.reduce((sum, value) => sum + value, 0);
      expect(Math.abs(name.width - Math.max(280, complement)), where).toBeLessThanOrEqual(1);
      expect(g.wrapScrolls, where).toBe(complement < 279);
      continue;
    }
    expect(shown(g), where).toEqual(['Name', 'Status', 'Progress', 'Size', 'Action']);
    expect(g.wrapScrolls, where).toBe(false);
    const [name, status, progress, size, , action] = g.columns;
    expect(Math.abs(name.width - 280), where).toBeLessThanOrEqual(1);
    expect(Math.abs(status.width - 63), where).toBeLessThanOrEqual(1);
    expect(Math.abs(size.width - (0.09 * W - 1.53)), where).toBeLessThanOrEqual(1);
    expect(Math.abs(action.width - (64 + inset)), where).toBeLessThanOrEqual(1);
    expect(Math.abs(progress.width - (W - 280 - 63 - (0.09 * W - 1.53) - 64 - inset)), where).toBeLessThanOrEqual(1);
  }
  // Mobile keeps sizing its columns to their content.
  await resize(page, 779);
  expect((await geometry(page, 'dashboard')).layout).toBe('auto');
});

test('both tables resize continuously: no column jumps at 1440/1439, and the sidebar change only widens them', async ({ page }) => {
  for (const view of ['dashboard', 'torrents']) {
    await open(page, view);
    let previous = null;
    for (let width = 1600; width >= 1390; width -= 5) {
      await resize(page, width);
      const g = await geometry(page, view);
      const widths = g.columns.filter(column => column.shown).map(column => column.width);
      expect(g.misfits, `${view} ${width}px`).toEqual([]);
      if (previous) {
        const wrapDelta = g.wrap.width - previous.wrap;
        widths.forEach((value, index) => {
          const delta = value - previous.widths[index], where = `${view} ${previous.width}->${width}px column ${index}`;
          if (wrapDelta > 0) expect(delta, where).toBeGreaterThanOrEqual(-0.5);       // the rail only adds room
          else expect(Math.abs(delta), where).toBeLessThanOrEqual(Math.abs(wrapDelta) + 0.5);   // never more than the table moved
        });
        expect(widths.length, `${view} ${width}px`).toBe(previous.widths.length);
      }
      previous = {width, widths, wrap: g.wrap.width};
      if (width === 1560) width = 1564;   // the next step lands on 1559, across the sidebar change
    }
    // The former stale threshold specifically: 1440 and 1439 lay the table out alike.
    await resize(page, 1440);
    const at1440 = (await geometry(page, view)).columns.map(column => [column.shown, column.width]);
    await resize(page, 1439);
    const at1439 = (await geometry(page, view)).columns.map(column => [column.shown, column.width]);
    at1439.forEach(([visible, width], index) => {
      expect(visible).toBe(at1440[index][0]);
      expect(Math.abs(width - at1440[index][1])).toBeLessThanOrEqual(1.5);
    });
  }
});

test('at the narrowest desktop width the Downloads controls still work: date menu, selection, filters, pages', async ({ page }) => {
  await open(page, 'torrents', {total: 60});
  await resize(page, 1390);
  const trigger = page.locator('#view-torrents th .dp-date-menu-trigger');
  await trigger.click();
  const menu = page.locator('#view-torrents th .dp-date-menu');
  await expect(menu).toBeVisible();
  const menuBox = await menu.boundingBox();
  expect(menuBox.x).toBeGreaterThanOrEqual(0);
  expect(menuBox.x + menuBox.width).toBeLessThanOrEqual(1390);
  await page.keyboard.press('Escape');
  await page.locator('#chk-all').check();
  for (const id of ITEMS.map(item => item.id)) {
    await expect(page.locator(`#t-tbody tr[data-torrent-id="${id}"] .t-chk`)).toBeChecked();
  }
  await page.locator('#chk-all').uncheck();
  const remove = page.locator('#t-tbody tr[data-torrent-id="9902"] .actions button').first();
  await expect(remove).toBeVisible();
  const removeBox = await remove.boundingBox();
  expect(removeBox.x + removeBox.width).toBeLessThanOrEqual(1390);
  // Each filter asks for its own list. (Other owners read /api/torrents too --
  // the authorization prompt's input_required scan among them -- so the
  // filter's request is looked for among those since the click, not taken to
  // be the last one.)
  let since = requests.length;
  await page.locator('#view-torrents .ftab[data-dp-status="paused"]').click();
  await expect.poll(() => requests.slice(since).some(params => params.get('status') === 'paused')).toBe(true);
  since = requests.length;
  await page.locator('#view-torrents .ftab[data-dp-status=""]').click();
  await expect.poll(() => requests.slice(since).some(params => !params.get('status'))).toBe(true);
  const next = page.locator('#torrent-page-btns [aria-label="Next page"]');
  await expect(next).toBeVisible();
  await next.click();
  await expect(page.locator('#torrent-page-btns [aria-current="page"]')).toHaveText('2');
  expect(requests.some(params => Number(params.get('offset')) > 0 && params.get('offset') === params.get('limit'))).toBe(true);
});
