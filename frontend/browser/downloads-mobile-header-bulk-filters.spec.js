const { test, expect } = require('@playwright/test');

/* DP 1.0.14 Downloads controls on a handheld (below 780px):
 * - the card header is one row: the identity (icon, On the Books and one hint
 *   line) and, at its right, one status filter control standing for the seven
 *   filters -- Status (the Lucide funnel, its label and the DP chevron) opens a
 *   menu of the filters' own choices; with a status chosen it is Clear (the
 *   funnel-x), which returns to All. Both states have one footprint. The hint
 *   is the longest of the full hint, "N downloads tracked." and "N downloads"
 *   whose drawn line fits beside the control;
 * - Search and the Priority/Newest selector share one row;
 * - the selection toolbar hides its "N Selected", reads Clear, and shows every
 *   label while the whole toolbar fits with its widest set, else every glyph
 *   alone at once -- six on one row at 320px.
 * The header's own fit is checked from 320px in Outfit (the product's face) and
 * from 340px in the fallback face: there "On the Books" (118px in DejaVu Sans)
 * and the control need 18px more than a 320px window gives -- a recorded limit
 * of the fallback face, not compensated in the presentation. Every other case
 * holds at 320px in either face.
 * From 780px the desktop's header, filters and toolbar are unchanged (their own
 * specs). CI's fallback fonts by default; DP_WEB_FONTS=1 loads Outfit for the
 * same assertions. Mocked rows, counts and commands: no backend state is read or
 * written. */

const NARROWEST_HEADER = process.env.DP_WEB_FONTS ? 320 : 340;
const SEVEN = ['All', 'Downloading', 'Paused', 'Processing', 'Ready', 'Done', 'Error'];
const STATUSES = ['', 'downloading', 'paused', 'processing', 'ready', 'completed', 'error'];

function row(id, o) {
  return {
    id, name: `Selectable.Row.${id}.mkv`, status: 'paused', presentation_status: 'paused', presentation_label: 'Paused',
    presentation_badge_status: 'paused', attention_required: false, progress: 40, active_execution_progress: null,
    active_execution_basis: null, size_bytes: 1000, source: 'magnet', hash: 'a1b2c3d4', label: '',
    created_at: '2025-12-28T22:54:00Z', current_source_identity: {kind: 'magnet'}, providers: [], historical_providers: [],
    delivering_provider_ids: [], input_required: null, acquisition_eta: null, access: {destroy: true}, queue_member: true, ...o,
  };
}
const DONE = {status: 'completed', presentation_status: 'completed', presentation_label: 'Done', presentation_badge_status: 'completed',
  progress: 100, queue_member: false};
// Two unfinished queue members (Move to Top), a finished one (none), one this
// caller may not destroy, a finished one it may not destroy, then plain rows
// enough to fill a tall page.
const ITEMS = [row(9801), row(9802), row(9803, DONE), row(9804, {access: {destroy: false}}), row(9805, {...DONE, access: {destroy: false}}),
  ...Array.from({length: 40}, (_, k) => row(9900 + k))];

async function open(page, {role = 'admin', count = 618, width = 390, height = 844} = {}) {
  const state = {count, requests: [], bulk: [], tops: []};
  if (!process.env.DP_WEB_FONTS) await page.route(/fonts\.(googleapis|gstatic)\.com/, route => route.abort());
  await page.route('**/api/events/stream', () => {});
  if (role !== 'admin') {
    const user = authority => ({...authority, role: 'user',
      capabilities: authority.capabilities.filter(capability => capability !== 'administer')});
    await page.route(url => url.pathname === '/app.js', async route => {
      const body = await (await route.fetch()).text();
      const marker = 'window.debridPulseAuthority=Object.freeze(';
      const [head, rest] = body.split(marker);
      const end = rest.indexOf(');');
      return route.fulfill({status: 200, contentType: 'application/javascript',
        body: head + marker + JSON.stringify(user(JSON.parse(rest.slice(0, end)))) + rest.slice(end)});
    });
    await page.route('**/api/auth/session', async route => route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify(user(await (await route.fetch()).json()))}));
  }
  await page.route('**/api/stats', async route => {
    const stats = await (await route.fetch()).json();
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({...stats, by_status: {completed: state.count}})});
  });
  await page.route('**/api/torrents**', async route => {
    const request = route.request(), url = new URL(request.url());
    const reply = body => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(body)});
    if (url.pathname === '/api/torrents/bulk' && request.method() === 'POST') {
      state.bulk.push(request.postDataJSON());
      return reply({ok: request.postDataJSON().ids.length, failed: 0});
    }
    if (url.pathname === '/api/torrents/move-to-top' && request.method() === 'POST') {
      state.tops.push(request.postDataJSON());
      return reply({ok: true, moved: request.postDataJSON().ids.length, ignored: 0, outcome: 'queued', preempting: []});
    }
    if (url.pathname !== '/api/torrents') return route.fallback();
    // The list's own pages (another owner polls Input Required with no offset).
    if (url.searchParams.has('offset')) state.requests.push(Object.fromEntries(url.searchParams));
    const limit = Number(url.searchParams.get('limit')) || 25, offset = Number(url.searchParams.get('offset')) || 0;
    return reply({items: ITEMS.slice(offset, offset + limit), total: ITEMS.length});
  });
  await page.setViewportSize({width, height});
  await page.goto('/');
  await page.evaluate(async () => {
    nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]'));
    await Promise.all([loadTorrents(), loadStats()]);
    await document.fonts.ready;
  });
  await expect(page.locator('#t-tbody tr[data-torrent-id]').first()).toBeVisible();
  await expect(page.locator('#torrent-card-title')).toHaveAttribute('aria-label', new RegExp(`^On the Books\\. ${count} `));
  return state;
}

async function resize(page, width, height = 844) {
  await page.setViewportSize({width, height});
  await page.waitForFunction(() =>
    document.getElementById('sidebar').getAnimations().every(animation => animation.playState !== 'running'));
  await page.waitForTimeout(200);
}

async function setCount(page, state, count) {
  state.count = count;
  await page.evaluate(() => loadStats());
  await expect(page.locator('#torrent-card-title')).toHaveAttribute('aria-label', new RegExp(`^On the Books\\. ${count} `));
}

// The text a control shows: every text node none of whose elements up to the
// control is hidden (display, visibility or opacity).
const VISIBLE_TEXT = `(node => {
  const seen = element => { const s = getComputedStyle(element); return s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity) > 0.5; };
  const parts = [], walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    let shown = true;
    for (let element = walker.currentNode.parentElement; element && element !== node.parentElement; element = element.parentElement) if (!seen(element)) shown = false;
    if (shown && walker.currentNode.textContent.trim()) parts.push(walker.currentNode.textContent.trim());
  }
  return parts.join(' ');
})`;

// One read of the handheld header as it stands, with the oracle for its hint:
// the drawn width of every presentation (canvas, in the line's own computed
// font) against the room the identity's column has before the control.
const header = page => page.evaluate(VISIBLE => {
  const visibleText = eval(VISIBLE);
  const head = document.querySelector('#view-torrents > .card > .card-header');
  const title = head.querySelector('#torrent-card-title'), copy = title.querySelector('.dp-downloads-heading-copy');
  const trigger = document.getElementById('torrent-filter'), tabs = head.querySelector('.filter-tabs');
  const r = node => node.getBoundingClientRect(), s = getComputedStyle(head);
  const shown = node => getComputedStyle(node).display !== 'none' && Number(getComputedStyle(node).opacity) > 0.5;
  const textRight = node => { const range = document.createRange(); range.selectNodeContents(node); return range.getBoundingClientRect().right; };
  const lines = [...copy.children].filter(shown);
  const heading = copy.querySelector('.dp-downloads-heading'), hint = lines.find(node => node !== heading);
  const context = document.createElement('canvas').getContext('2d');
  const drawn = text => { const cs = getComputedStyle(head.querySelector('.dp-downloads-subtitle')); context.font = `${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`; return context.measureText(text).width; };
  const count = head.querySelector('.dp-downloads-count').textContent;
  const word = count === '1' ? 'download' : 'downloads';
  const middle = node => (r(node).top + r(node).bottom) / 2;
  const glyphs = [...trigger.querySelectorAll('svg')].filter(svg => getComputedStyle(svg).visibility !== 'hidden' && r(svg).width > 0);
  const chevron = trigger.querySelector('.dp-dropdown__chevron');
  return {
    height: r(head).height, tabs: shown(tabs), triggerShown: shown(trigger),
    visible: lines.map(node => node.textContent + (getComputedStyle(node, '::after').content === '"."' ? '.' : '')),
    room: r(trigger).left - parseFloat(s.columnGap) - r(copy).left,
    widths: {full: drawn(head.querySelector('.dp-downloads-subtitle').textContent), tracked: drawn(`${count} ${word} tracked.`), downloads: drawn(`${count} ${word}`)},
    clear: r(trigger).left - parseFloat(s.columnGap) - Math.max(...lines.map(textRight)),
    headingWhole: heading.scrollWidth <= heading.clientWidth && textRight(heading) <= r(trigger).left - parseFloat(s.columnGap) + 0.5,
    hintWhole: hint ? textRight(hint) <= r(trigger).left - parseFloat(s.columnGap) + 0.5 : false,
    oneRow: Math.abs(middle(trigger) - middle(head)) < 1 && Math.abs(middle(title) - middle(head)) < 1,
    inside: r(trigger).right <= r(head).right - parseFloat(s.paddingRight) + 0.5 && head.scrollWidth <= head.clientWidth,
    trigger: {text: visibleText(trigger), glyphs: glyphs.map(svg => svg.dataset.dpLucide), glyphSize: glyphs.map(svg => [r(svg).width, r(svg).height]),
      glyphLeftOfText: glyphs.length === 1 && [...trigger.querySelectorAll('.dp-downloads-filter-text')].filter(node => getComputedStyle(node).visibility !== 'hidden')
        .every(node => r(glyphs[0]).right <= textRight(node) - r(node).width + 0.5),
      chevron: getComputedStyle(chevron).visibility !== 'hidden', box: [r(trigger).left, r(trigger).top, r(trigger).width, r(trigger).height],
      name: trigger.getAttribute('aria-label'), tooltip: trigger.getAttribute('data-tooltip'), popover: trigger.getAttribute('popovertarget'),
      expanded: trigger.getAttribute('aria-expanded')},
  };
}, VISIBLE_TEXT);

// The presentation shown is the longest that fits: the shown line is drawn
// whole beside the control (hintWhole, by layout), and every longer one would
// not fit the room (its canvas width, within 1px of measurement tolerance).
const FORMS = ['full', 'tracked', 'downloads'];
const shownForm = (state, count) => FORMS.find(form => state.visible[1] === wording(count, form));
const longestFitting = (state, count) => {
  const form = shownForm(state, count);
  return FORMS.slice(0, FORMS.indexOf(form)).every(longer => state.widths[longer] > state.room - 1) ? form : `not the longest: ${form}`;
};
const wording = (count, form) => {
  const one = count === 1;
  return {full: one ? '1 download tracked. It behaved.' : `${count} downloads tracked. Most of them behaved.`,
    tracked: one ? '1 download tracked.' : `${count} downloads tracked.`, downloads: one ? '1 download' : `${count} downloads`}[form];
};
const STATUS_TRIGGER = {text: 'Status', glyphs: ['funnel'], glyphSize: [[15, 15]], glyphLeftOfText: true, chevron: true,
  name: 'Status filter', tooltip: null, popover: 'torrent-filter-menu'};

test('the handheld header is one row: identity, hint and the Status control, the hint the longest presentation that fits, at every width and count', async ({page}) => {
  const state = await open(page, {width: 320});
  const forms = new Set();
  for (const count of [1, 618, 99999]) {
    await setCount(page, state, count);
    for (const width of [NARROWEST_HEADER, 375, 390, 440, 600, 779]) {
      await resize(page, width);
      const now = await header(page);
      const form = longestFitting(now, count);
      forms.add(form);
      expect(now, `${count} at ${width}px`).toMatchObject({height: 70, tabs: false, triggerShown: true, oneRow: true, inside: true,
        headingWhole: true, hintWhole: true, visible: ['On the Books', wording(count, form)], trigger: STATUS_TRIGGER});
      expect(now.trigger.box[3]).toBe(36);
      expect(now.clear, `${count} at ${width}px`).toBeGreaterThanOrEqual(-0.5);
      await expect(page.locator('#torrent-card-title')).toHaveAttribute('aria-label', `On the Books. ${wording(count, 'full')}`);
    }
  }
  // Each presentation was exercised; at the narrowest a long count reads "N downloads", wider the short and the full hint return.
  expect([...forms].sort()).toEqual(['downloads', 'full', 'tracked']);
  await resize(page, NARROWEST_HEADER);
  expect((await header(page)).visible).toEqual(['On the Books', '99999 downloads']);
  await resize(page, 779);
  expect((await header(page)).visible).toEqual(['On the Books', '99999 downloads tracked. Most of them behaved.']);
  // A reduced hint: the full wording in DP's tooltip, never a browser one.
  await resize(page, NARROWEST_HEADER);
  await page.locator('#torrent-card-title').hover();
  await expect(page.locator('#dp-tooltip')).toHaveText('On the Books. 99999 downloads tracked. Most of them behaved.');
  await expect(page.locator('#torrent-card-title')).not.toHaveAttribute('title', /./);
});

test('Status opens the filters\' own choices; a choice is the filter transition, the control becomes Clear, and Clear returns to All', async ({page}) => {
  const state = await open(page, {width: 320});
  const trigger = page.locator('#torrent-filter'), menu = page.locator('#torrent-filter-menu');
  const options = menu.locator('.dp-dropdown__option');
  const before = (await header(page)).trigger.box;
  // A selection and a later page: the filter transition clears and resets both.
  await page.locator('#t-tbody tr[data-torrent-id="9801"] .t-chk').check();
  await page.evaluate(() => goToTorrentPage(2));
  await expect.poll(() => state.requests.at(-1)?.offset).not.toBe('0');
  await page.locator('#t-tbody tr[data-torrent-id] .t-chk').first().check();

  await trigger.click();
  await expect(menu).toBeVisible();
  await expect(trigger).toHaveAttribute('aria-expanded', 'true');
  await expect(options).toHaveText(SEVEN);
  expect(await options.evaluateAll(nodes => nodes.map(node => node.dataset.dpStatus))).toEqual(STATUSES);
  await expect(options.first()).toHaveAttribute('aria-selected', 'true');
  await expect(options.first()).toBeFocused();
  // Anchored beneath the control, inside the window, at least as wide as it, its rows whole.
  const placed = await page.evaluate(() => {
    const t = document.getElementById('torrent-filter').getBoundingClientRect(), m = document.getElementById('torrent-filter-menu').getBoundingClientRect();
    return {below: m.top - t.bottom, right: Math.abs(m.right - t.right), inside: m.left >= 8 && m.right <= document.documentElement.clientWidth - 8,
      wide: m.width >= t.width - 0.5, whole: document.getElementById('torrent-filter-menu').scrollHeight <= document.getElementById('torrent-filter-menu').clientHeight};
  });
  expect(placed).toEqual({below: 6, right: 0, inside: true, wide: true, whole: true});
  // The keyboard walks the choices; Enter takes one.
  await page.keyboard.press('ArrowDown');
  await expect(options.nth(1)).toBeFocused();
  await page.keyboard.press('End');
  await expect(options.last()).toBeFocused();
  await page.keyboard.press('Home');
  await page.keyboard.press('ArrowDown');
  const asked = state.requests.length;
  await page.keyboard.press('Enter');
  await expect(menu).toBeHidden();
  // The one transition: page 1, the status, the selection cleared, one request.
  await expect.poll(() => state.requests.slice(asked)).toEqual([expect.objectContaining({status: 'downloading', offset: '0'})]);
  await expect(page.locator('#view-torrents .ftab.active')).toHaveAttribute('data-dp-status', 'downloading');
  await expect(page.locator('#t-tbody .t-chk:checked')).toHaveCount(0);
  await expect(page.locator('#bulk-bar')).not.toHaveClass(/\bvisible\b/);
  await expect(trigger).toBeFocused();
  let now = await header(page);
  expect(now.trigger).toMatchObject({text: 'Clear', glyphs: ['funnel-x'], glyphSize: [[15, 15]], glyphLeftOfText: true, chevron: false,
    name: 'Clear Downloading filter', tooltip: 'Clear Downloading filter', popover: null, expanded: null});
  // The same footprint in both states: nothing moves.
  expect(now.trigger.box).toEqual(before);
  await expect(trigger).toHaveAccessibleName('Clear Downloading filter');
  await trigger.hover();
  await expect(page.locator('#dp-tooltip')).toHaveText('Clear Downloading filter');
  await expect(trigger).not.toHaveAttribute('title', /./);

  // Clear opens nothing: it returns to All at once.
  const cleared = state.requests.length;
  await page.keyboard.press('Enter');
  await expect(menu).toBeHidden();
  await expect.poll(() => state.requests.slice(cleared)).toHaveLength(1);
  expect(state.requests.at(-1).status).toBeUndefined();
  expect(state.requests.at(-1).offset).toBe('0');
  await expect(page.locator('#view-torrents .ftab.active')).toHaveAttribute('data-dp-status', '');
  await expect(trigger).toBeFocused();
  now = await header(page);
  expect(now.trigger).toMatchObject(STATUS_TRIGGER);
  expect(now.trigger.box).toEqual(before);

  // Escape closes the menu with no change, focus back on the control.
  const quiet = state.requests.length;
  await trigger.click();
  await expect(menu).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(menu).toBeHidden();
  await expect(trigger).toBeFocused();
  await expect(trigger).toHaveAttribute('aria-expanded', 'false');
  // Tab out of the menu closes it (never a trap): focus goes on to Search.
  await trigger.press('Enter');
  await expect(menu).toBeVisible();
  await page.keyboard.press('Tab');
  await expect(menu).toBeHidden();
  await expect(page.locator('#torrent-search')).toBeFocused();
  // A press outside closes it too, with no change.
  await trigger.click();
  await expect(menu).toBeVisible();
  const outside = await page.locator('#torrent-page-info').boundingBox();
  await page.mouse.click(outside.x + 2, outside.y + 2);
  await expect(menu).toBeHidden();
  // A pointer choice: Error.
  await trigger.click();
  await options.last().click();
  await expect(page.locator('#view-torrents .ftab.active')).toHaveAttribute('data-dp-status', 'error');
  await expect(trigger).toHaveAccessibleName('Clear Error filter');
  expect(state.requests.slice(quiet).map(request => request.status)).toEqual(['error']);
  await page.waitForTimeout(300);
  expect(state.requests.slice(quiet)).toHaveLength(1);
});

test('one operative filter surface per side of 780px: the control below, the seven filters from 780px, sharing one filter state', async ({page}) => {
  await open(page, {width: 390});
  // Below 780px: the control is a keyboard stop, no filter is.
  const stops = async () => {
    await page.locator('#runtime-cap-toggle').focus();
    const seen = [];
    for (let step = 0; step < 30; step++) {
      await page.keyboard.press('Tab');
      const stop = await page.evaluate(() => document.activeElement.id || document.activeElement.className);
      seen.push(stop);
      if (stop === 'torrent-search') return seen;
    }
    throw new Error(seen.join(' > '));
  };
  let mobile = await stops();
  expect(mobile.filter(stop => /ftab/.test(stop))).toEqual([]);
  expect(mobile.filter(stop => stop === 'torrent-filter')).toHaveLength(1);
  // A filter chosen on the desktop is the handheld control's Clear, and back.
  await resize(page, 1440, 900);
  await expect(page.locator('#torrent-filter')).toBeHidden();
  await expect(page.locator('#view-torrents .filter-tabs')).toBeVisible();
  await page.locator('#view-torrents .ftab[data-dp-status="paused"]').click();
  await resize(page, 390);
  await expect(page.locator('#torrent-filter')).toHaveAccessibleName('Clear Paused filter');
  await page.locator('#torrent-filter').click();
  await resize(page, 1440, 900);
  await expect(page.locator('#view-torrents .ftab.active')).toHaveAttribute('data-dp-status', '');
  // An open menu closes when the page crosses to 780px.
  await resize(page, 390);
  await page.locator('#torrent-filter').click();
  await expect(page.locator('#torrent-filter-menu')).toBeVisible();
  await resize(page, 780, 900);
  await expect(page.locator('#torrent-filter-menu')).toBeHidden();
  await expect(page.locator('#torrent-filter')).toBeHidden();
  expect(await page.evaluate(() => 'dpFit' in document.querySelector('#view-torrents > .card > .card-header').dataset)).toBe(false);
});

test('Search and Priority/Newest share one row from 320 to 779px: Search takes the room, both centred, each working', async ({page}) => {
  const state = await open(page, {width: 320});
  for (const width of [320, 375, 600, 779]) {
    await resize(page, width);
    const now = await page.evaluate(() => {
      const r = node => node.getBoundingClientRect();
      const row = document.querySelector('#view-torrents .ev-search-row'), search = document.getElementById('torrent-search');
      const order = document.getElementById('torrent-order').nextElementSibling;
      const mid = node => (r(node).top + r(node).bottom) / 2;
      const s = getComputedStyle(search), context = document.createElement('canvas').getContext('2d');
      context.font = `${s.fontWeight} ${s.fontSize} ${s.fontFamily}`;
      return {sameRow: Math.abs(mid(search) - mid(order)) < 0.5 && Math.abs(mid(search) - mid(row)) < 0.5,
        fills: Math.abs(r(search).left - r(row).left) < 0.5 && Math.abs(r(order).right - r(row).right) < 0.5,
        apart: r(order).left - r(search).right, order: [r(order).width, r(order).height], height: r(row).height,
        room: search.clientWidth - parseFloat(s.paddingLeft) - parseFloat(s.paddingRight),
        full: context.measureText('Search downloads…').width, drawn: context.measureText(search.placeholder).width,
        placeholder: search.placeholder, name: search.getAttribute('aria-label'), orderText: order.textContent.trim()};
    });
    // The full placeholder wherever it is drawn whole, else "Search…"; the name is always the full one.
    expect(now.drawn, `${width}px`).toBeLessThanOrEqual(now.room);
    expect(now, `${width}px`).toMatchObject({sameRow: true, fills: true, apart: 8, order: [112, 42], height: 42, name: 'Search downloads',
      placeholder: now.full <= now.room ? 'Search downloads…' : 'Search…', orderText: 'Priority'});
  }
  await resize(page, 320);
  // Typed text is the search's own: one committed request carries it.
  await page.locator('#torrent-search').fill('Row.9801');
  await expect.poll(() => state.requests.at(-1)?.search).toBe('Row.9801');
  await expect(page.locator('#torrent-search')).toHaveValue('Row.9801');
  await page.locator('#torrent-search').fill('');
  await expect.poll(() => state.requests.at(-1)?.search).toBeUndefined();
  // The order: Newest, then Priority again, each one request from page 1.
  await page.locator('#torrent-order + .dp-dropdown-shell .dp-dropdown__trigger').click();
  await page.locator('#dp-dropdown-layer .dp-dropdown__option', {hasText: 'Newest'}).click();
  await expect.poll(() => state.requests.at(-1)).toEqual(expect.objectContaining({offset: '0'}));
  expect(state.requests.at(-1).order).toBeUndefined();
  await expect(page.locator('#torrent-order')).toHaveValue('newest');
  await page.locator('#torrent-order + .dp-dropdown-shell .dp-dropdown__trigger').click();
  await page.locator('#dp-dropdown-layer .dp-dropdown__option', {hasText: 'Priority'}).click();
  await expect.poll(() => state.requests.at(-1)?.order).toBe('queue');
});

// One read of the toolbar as it stands.
const toolbar = page => page.evaluate(VISIBLE => {
  const visibleText = eval(VISIBLE);
  const bar = document.querySelector('#bulk-bar .dp-downloads-bulk-toolbar');
  const r = node => node.getBoundingClientRect(), s = getComputedStyle(bar);
  const items = [...bar.querySelectorAll('.dp-downloads-bulk-actions > *, .dp-downloads-bulk-action--clear')].filter(node => getComputedStyle(node).display !== 'none');
  const buttons = items.filter(node => node.tagName === 'BUTTON');
  const count = document.getElementById('bulk-count');
  const middle = node => (r(node).top + r(node).bottom) / 2;
  return {
    width: document.getElementById('bulk-bar').clientWidth, height: r(bar).height,
    order: items.map(node => node.classList.contains('dp-downloads-bulk-separator') ? '|' : node.dataset.defaultLabel),
    shown: buttons.map(node => visibleText(node)),
    glyphs: buttons.every(node => node.querySelector('svg') && r(node.querySelector('svg')).width === 16),
    sizes: buttons.map(node => [Math.round(r(node).width), Math.round(r(node).height)]),
    oneRow: items.every(node => Math.abs(middle(node) - middle(bar)) < 1),
    inside: Math.max(...items.map(node => r(node).right)) <= r(bar).right - parseFloat(s.paddingRight) + 0.5
      && Math.min(...items.map(node => r(node).left)) >= r(bar).left + parseFloat(s.paddingLeft) - 0.5 && bar.scrollWidth <= bar.clientWidth,
    disjoint: items.every((node, i) => i === 0 || r(node).left >= r(items[i - 1]).right - 0.5),
    count: count.textContent, countDrawn: r(count).width * r(count).height,
    names: buttons.map(node => node.getAttribute('aria-label') || node.textContent.trim()),
  };
}, VISIBLE_TEXT);

async function barAt(page, width) {
  let window = width + 22;
  for (let step = 0; step < 4; step++) {
    await resize(page, window);
    const now = (await toolbar(page)).width;
    if (now === width) return window;
    window += width - now;
  }
  throw new Error(`no window gave a ${width}px toolbar`);
}

const SIX = ['Pause', 'Resume', 'Reset', 'Move to Top', '|', 'Delete', 'Clear Selections'];
const GLYPHS = {shown: ['', '', '', '', '', ''], sizes: Array(6).fill([36, 36])};
const LABELS = {shown: ['Pause', 'Resume', 'Reset', 'Move to Top', 'Delete', 'Clear']};

test('at 320px the widest toolbar -- Pause, Resume, Reset, Move to Top, Delete and Clear -- is six glyphs on one row, no counter drawn, each working', async ({page}) => {
  const state = await open(page, {width: 320});
  await page.locator('#t-tbody tr[data-torrent-id="9801"] .t-chk').check();
  await page.locator('#t-tbody tr[data-torrent-id="9802"] .t-chk').check();
  const now = await toolbar(page);
  expect(now).toMatchObject({height: 54, order: SIX, ...GLYPHS, glyphs: true, oneRow: true, inside: true, disjoint: true,
    count: '2 Selected', countDrawn: 1,
    names: ['Pause', 'Resume', 'Reset', 'Move to Top', 'Delete', 'Clear Selections']});
  // Each says its action in DP's tooltip; Clear says the whole operation.
  for (const [action, text] of [['pause', 'Pause selected downloads'], ['delete', 'Delete selected downloads'], ['clear', 'Clear Selections']]) {
    await page.locator(`.dp-downloads-bulk-action--${action}`).hover();
    await expect(page.locator('#dp-tooltip')).toHaveText(text);
  }
  await page.locator('.dp-downloads-bulk-action--top').click();
  await expect.poll(() => state.tops.length).toBe(1);
  expect(state.tops[0].ids).toEqual([9801, 9802]);
  await page.locator('#t-tbody tr[data-torrent-id="9801"] .t-chk').check();
  await page.locator('.dp-downloads-bulk-action--pause').click();
  await expect.poll(() => state.bulk.length).toBe(1);
  expect(state.bulk[0]).toEqual({ids: [9801], action: 'pause'});
  await page.locator('#t-tbody tr[data-torrent-id="9802"] .t-chk').check();
  await page.locator('.dp-downloads-bulk-action--clear').click();
  await expect(page.locator('#bulk-bar')).not.toHaveClass(/\bvisible\b/);
});

test('every toolbar button changes together at one width, whichever actions are mounted, back and forth, in either face', async ({page}) => {
  await open(page, {role: 'user', width: 779});
  const mixed = state => state.shown.some(text => text) && state.shown.some(text => !text);
  // The widest set (a User who may destroy the queue member): labels while it fits.
  await page.locator('#t-tbody tr[data-torrent-id="9801"] .t-chk').check();
  await barAt(page, 648);
  let state = await toolbar(page);
  expect(state).toMatchObject({order: SIX, ...LABELS, oneRow: true, inside: true, disjoint: true, countDrawn: 1});
  // One px narrower: every button its glyph alone, Clear too.
  await barAt(page, 647);
  state = await toolbar(page);
  expect(state).toMatchObject({order: SIX, ...GLYPHS, oneRow: true, inside: true});
  // Actions mounting and leaving at either side of the switch never mix the modes.
  for (const width of [647, 648, 700, 647, 320, 648]) {
    await barAt(page, width);
    const glyphs = width < 648;
    for (const ids of [[9801], [9803], [9804], [9805], [9801]]) {
      await page.evaluate(() => clearSelection());
      for (const id of ids) await page.locator(`#t-tbody tr[data-torrent-id="${id}"] .t-chk`).check();
      state = await toolbar(page);
      const expectedOrder = {9801: SIX, 9803: ['Pause', 'Resume', 'Reset', '|', 'Delete', 'Clear Selections'],
        9804: ['Pause', 'Resume', 'Reset', 'Move to Top', 'Clear Selections'], 9805: ['Pause', 'Resume', 'Reset', 'Clear Selections']}[ids.join(',')];
      expect(state.order, `${ids} at ${width}px`).toEqual(expectedOrder);
      expect(mixed(state), `${ids} at ${width}px`).toBe(false);
      expect(state.shown.every(text => !text), `${ids} at ${width}px`).toBe(glyphs);
      expect(state, `${ids} at ${width}px`).toMatchObject({oneRow: true, inside: true, disjoint: true, height: 54});
    }
  }
});

test('the reclaimed rows are the table\'s: the card still ends 10px above the dock, whole rows only, the pager inside, no nested scroll', async ({page}) => {
  await open(page, {width: 390, height: 844});
  for (const [width, height] of [[320, 568], [390, 844], [779, 1000]]) {
    await resize(page, width, height);
    await page.waitForTimeout(400);
    await expect.poll(() => page.evaluate(() => {
      const content = document.getElementById('content'), card = document.querySelector('#view-torrents > .card');
      const wrap = card.querySelector('.dp-downloads-table-wrap'), head = wrap.querySelector('thead');
      const rows = [...wrap.querySelectorAll('tbody tr[data-torrent-id]')], r = node => node.getBoundingClientRect();
      const inset = parseFloat(getComputedStyle(content).paddingBottom);
      const pager = document.getElementById('torrent-pager') || document.getElementById('torrent-pagination');
      const rowHeight = Math.max(...rows.map(node => r(node).height));
      const room = r(wrap).height - r(head).height;
      return {cardEnd: Math.round(r(content).bottom - inset - r(card).bottom) <= 0.5 ? 'at inset' : r(content).bottom - inset - r(card).bottom,
        pagerInside: r(pager).bottom <= r(card).bottom + 0.5, whole: rows.length * rowHeight <= room + 0.5,
        noRoomForAnother: room - rows.length * rowHeight < rowHeight, scroll: wrap.scrollHeight <= wrap.clientHeight + 0.5};
    }), {timeout: 5000}).toEqual({cardEnd: 'at inset', pagerInside: true, whole: true, noRoomForAnother: true, scroll: true});
  }
});

test('the control in light and dark: the standard ghost button\'s colours, hover and focus ring, unchanged, its glyph in its text colour', async ({page}) => {
  await open(page, {width: 390});
  const read = () => page.evaluate(() => {
    const trigger = document.getElementById('torrent-filter'), s = getComputedStyle(trigger);
    const glyph = [...trigger.querySelectorAll('svg')].find(svg => getComputedStyle(svg).visibility !== 'hidden');
    return {color: s.color, background: s.backgroundColor, border: s.borderColor, shadow: s.boxShadow, outline: s.outlineStyle,
      glyph: getComputedStyle(glyph).color, height: trigger.getBoundingClientRect().height};
  });
  // The same button material another ghost button on the page draws (Activity Log's Refresh), in the same theme.
  const reference = () => page.evaluate(() => {
    const probe = document.createElement('button');
    probe.className = 'btn btn-ghost';
    probe.textContent = 'Probe';
    document.querySelector('#view-torrents > .card > .card-header').appendChild(probe);
    const s = getComputedStyle(probe);
    const out = {color: s.color, background: s.backgroundColor, border: s.borderColor};
    probe.remove();
    return out;
  });
  for (const theme of ['dark', 'light']) {
    await page.evaluate(light => { document.body.classList.toggle('light', light); }, theme === 'light');
    await page.mouse.move(2, 2);
    // Settled (the button's colours transition with the theme).
    const settled = await reference();
    await expect.poll(async () => { const now = await read(); return {color: now.color, background: now.background, border: now.border}; }, theme).toEqual(settled);
    const rest = await read();
    expect(rest, theme).toMatchObject({glyph: rest.color, height: 36});
    await page.locator('#torrent-filter').hover();
    await expect.poll(async () => (await read()).border, theme).not.toBe(rest.border);
    await page.mouse.move(2, 2);
    await page.locator('#torrent-search').focus();
    await page.keyboard.press('Shift+Tab');
    await expect(page.locator('#torrent-filter')).toBeFocused();
    const focused = await read();
    expect(focused.shadow !== 'none' || focused.outline !== 'none', theme).toBe(true);
    await page.locator('#torrent-search').focus();
  }
});

test.describe('touch', () => {
  test.use({hasTouch: true});
  test('the menu by touch: a tap opens it, a tap on a choice filters, a tap outside dismisses it, the toolbar\'s buttons answer taps', async ({page}) => {
    const state = await open(page, {width: 320});
    const trigger = page.locator('#torrent-filter'), menu = page.locator('#torrent-filter-menu');
    await trigger.tap();
    await expect(menu).toBeVisible();
    const outside = await page.locator('#torrent-page-info').boundingBox();
    await page.touchscreen.tap(outside.x + 2, outside.y + 2);
    await expect(menu).toBeHidden();
    expect(state.requests.filter(request => request.status)).toEqual([]);
    await trigger.tap();
    await menu.locator('.dp-dropdown__option', {hasText: 'Ready'}).tap();
    await expect(menu).toBeHidden();
    await expect(trigger).toHaveAccessibleName('Clear Ready filter');
    await expect.poll(() => state.requests.at(-1)?.status).toBe('ready');
    await trigger.tap();
    await expect(menu).toBeHidden();
    await expect(trigger).toHaveAccessibleName('Status filter');
    await page.locator('#t-tbody tr[data-torrent-id="9801"] .t-chk').tap();
    await page.locator('.dp-downloads-bulk-action--resume').tap();
    await expect.poll(() => state.bulk.at(-1)).toEqual({ids: [9801], action: 'resume'});
  });
});
