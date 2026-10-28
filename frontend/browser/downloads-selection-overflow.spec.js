const { test, expect } = require('@playwright/test');

/* Selecting transfers never makes the Downloads list scroll sideways.
 *
 * A selection's toolbar takes height from a page sized to the rows that fit,
 * so the list gains a vertical scrollbar while rows are selected (the page is
 * kept while a selection is held). Where scrollbars take room -- this browser
 * is started WITH its scrollbars, which headless runs otherwise hide -- the
 * table must still fit beside it: no horizontal overflow from no selection,
 * through one and several, to cleared, at widths around the 780px handoff and
 * wide; every row control, the checkbox and the toolbar stay reachable. */
test.use({launchOptions: {ignoreDefaultArgs: ['--hide-scrollbars']}});

function row(id, o) {
  return {
    id, name: `Selectable.Row.${id}.With.A.Long.Release.Name.2160p.mkv`, status: 'paused', presentation_status: 'paused',
    presentation_label: 'Paused', presentation_badge_status: 'paused', attention_required: false, progress: 40,
    active_execution_progress: null, active_execution_basis: null, size_bytes: 123456789000, source: 'magnet', hash: 'a1b2c3d4',
    label: '', created_at: '2025-12-28T22:54:00Z', current_source_identity: {kind: 'magnet'}, providers: [], historical_providers: [],
    delivering_provider_ids: [], input_required: null, acquisition_eta: null, access: {destroy: true}, queue_member: true, ...o,
  };
}
const ITEMS = Array.from({length: 60}, (_, index) => row(9801 + index));

async function open(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.route('**/api/torrents**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname !== '/api/torrents') return route.fallback();
    const limit = Number(url.searchParams.get('limit') || 50), offset = Number(url.searchParams.get('offset') || 0);
    return route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({items: ITEMS.slice(offset, offset + limit), total: ITEMS.length})});
  });
  await page.setViewportSize({width: 1300, height: 760});
  await page.goto('/');
  await page.evaluate(async () => {
    nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]'));
    await Promise.all([loadTorrents(), loadStats()]);
  });
  await expect(page.locator('#t-tbody tr[data-torrent-id]').first()).toBeVisible();
}

const viewport = page => page.evaluate(() => {
  const wrap = document.querySelector('#view-torrents .dp-downloads-table-wrap');
  const table = wrap.querySelector('table'), w = wrap.getBoundingClientRect();
  const controls = [...wrap.querySelectorAll('#t-tbody tr[data-torrent-id]:first-child :is(.t-chk, .actions button)')]
    .map(node => node.getBoundingClientRect());
  return {overflow: wrap.scrollWidth - wrap.clientWidth, table: table.getBoundingClientRect().width, client: wrap.clientWidth,
    vertical: wrap.scrollHeight > wrap.clientHeight, scrollbar: wrap.offsetWidth - wrap.clientWidth,
    controlsInside: controls.every(box => box.left >= w.left - 0.5 && box.right <= w.left + wrap.clientWidth + 0.5),
    selected: document.getElementById('bulk-count')?.textContent.trim() || ''};
});

test('no, one, several and cleared selections never scroll the list sideways', async ({page}) => {
  await open(page);
  const check = id => page.locator(`#t-tbody tr[data-torrent-id="${id}"] .t-chk`);
  for (const width of [780, 800, 900, 1300]) {
    await page.setViewportSize({width, height: 760});
    await page.waitForFunction(() => document.getElementById('sidebar').getAnimations().every(a => a.playState !== 'running'));
    await page.waitForTimeout(250);
    const first = await page.locator('#t-tbody tr[data-torrent-id]').first().getAttribute('data-torrent-id');
    const ids = await page.locator('#t-tbody tr[data-torrent-id]').evaluateAll(rows => rows.slice(0, 3).map(r => r.dataset.torrentId));
    const states = [['none', await viewport(page)]];
    await check(first).check();
    states.push(['one', await viewport(page)]);
    for (const id of ids.slice(1)) await check(id).check();
    states.push(['several', await viewport(page)]);
    for (const id of ids) await check(id).uncheck();
    states.push(['cleared', await viewport(page)]);
    for (const [label, state] of states) {
      const at = `${label} selected at ${width}px`;
      expect(state.overflow, `${at}: the list scrolls sideways (${state.table}px table, ${state.client}px room)`).toBeLessThanOrEqual(0);
      expect(state.controlsInside, `${at}: a row control is outside the list`).toBe(true);
    }
    // The case that used to overflow is present: the selection's toolbar
    // gave the list a vertical scrollbar that takes room.
    expect(states[2][1].selected).toBe('3 Selected');
    expect(states[2][1].vertical && states[2][1].scrollbar > 0, `${width}px: no scrollbar took room`).toBe(true);
    await expect(page.locator('#bulk-bar')).toBeHidden();
  }
});
