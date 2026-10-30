const { test, expect } = require('@playwright/test');

/* DP 1.0.14 Downloads card header and multi-selection toolbar, desktop to the
 * tablet (780px). The header stays one row: the seven filters keep their full
 * width and the card's identity yields to them in four presentations of the one
 * tracked count, chosen by the header's own content width -- the full hint at
 * 884px and wider, "N downloads tracked" from 760px, "N downloads" from 738px,
 * and the number alone in the title's place below that (13px, beside the same
 * icon at 30px, 6px from it, the filters' side paddings narrowed to 8px, so
 * every count through six digits fits at 780px). Each switch is where its
 * wording fits in the wider fallback face (DejaVu Sans). The toolbar shows each
 * action's glyph alone, and Clear Selections reads "Clear", where it is
 * narrower than 826px. The switches are the header's and the toolbar's, so each
 * case finds them by their own widths. Mocked rows, counts and commands: no
 * backend state is read or written. */

const FULL = 884;        // header content width: the full hint at this width and wider
const TRACKED = 760;     // "N downloads tracked" from this width
const DOWNLOADS = 738;   // "N downloads" from this width; the number alone below it
const BULK = 826;        // the toolbar is compact below this width

function row(id, o) {
  return {
    id, name: `Selectable.Row.${id}.mkv`, status: 'paused', presentation_status: 'paused', presentation_label: 'Paused',
    presentation_badge_status: 'paused', attention_required: false, progress: 40, active_execution_progress: null,
    active_execution_basis: null, size_bytes: 1000, source: 'magnet', hash: 'a1b2c3d4', label: '',
    created_at: '2025-12-28T22:54:00Z', current_source_identity: {kind: 'magnet'}, providers: [], historical_providers: [],
    delivering_provider_ids: [], input_required: null, acquisition_eta: null, access: {destroy: true}, queue_member: true, ...o,
  };
}

// Two unfinished queue members; a finished transfer (no place in the queue); an
// unfinished one this caller may not destroy; a finished one it may not destroy.
const DONE = {status: 'completed', presentation_status: 'completed', presentation_label: 'Done', presentation_badge_status: 'completed',
  progress: 100, queue_member: false};
const ITEMS = [
  row(9801), row(9802), row(9803, DONE), row(9804, {access: {destroy: false}}), row(9805, {...DONE, access: {destroy: false}}),
];

async function open(page, {role = 'admin', count = 618} = {}) {
  const state = {count, bulk: [], tops: [], hold: null};
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  if (role !== 'admin') {
    // A User: the server's authority without administration, in the bundle and the session.
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
  // The one authoritative count: /stats, as the application reads it.
  await page.route('**/api/stats', async route => {
    const stats = await (await route.fetch()).json();
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({...stats, by_status: {completed: state.count}})});
  });
  await page.route('**/api/torrents**', async route => {
    const request = route.request(), url = new URL(request.url());
    const reply = body => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(body)});
    if (url.pathname === '/api/torrents/bulk' && request.method() === 'POST') {
      state.bulk.push(request.postDataJSON());
      if (state.hold) await state.hold;
      return reply({ok: request.postDataJSON().ids.length, failed: 0});
    }
    if (url.pathname === '/api/torrents/move-to-top' && request.method() === 'POST') {
      state.tops.push(request.postDataJSON());
      return reply({ok: true, moved: request.postDataJSON().ids.length, ignored: 0, outcome: 'queued', preempting: []});
    }
    if (url.pathname !== '/api/torrents') return route.fallback();
    return reply({items: ITEMS, total: ITEMS.length});
  });
  await page.setViewportSize({width: 1560, height: 1000});
  await page.goto('/');
  await page.evaluate(async () => {
    nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]'));
    await Promise.all([loadTorrents(), loadStats()]);
  });
  await expect(page.locator('#t-tbody tr[data-torrent-id]').first()).toBeVisible();
  await expect(page.locator('#torrent-card-title')).toHaveAttribute('aria-label', new RegExp(`^On the Books\\. ${count} `));
  return state;
}

// The sidebar's width transition has finished (the real CSS transition, not a sleep).
async function resize(page, width) {
  await page.setViewportSize({width, height: 1000});
  await page.waitForFunction(() =>
    document.getElementById('sidebar').getAnimations().every(animation => animation.playState !== 'running'));
  await page.waitForTimeout(150);
}

async function setCount(page, state, count) {
  state.count = count;
  await page.evaluate(() => loadStats());
  await expect(page.locator('#torrent-card-title')).toHaveAttribute('aria-label', new RegExp(`^On the Books\\. ${count} `));
}

// One read of the header as it stands. ``clear``: the px the shown identity
// could still grow before it reaches the gap that separates it from the filters.
const header = page => page.evaluate(() => {
  const head = document.querySelector('#view-torrents > .card > .card-header');
  const title = head.querySelector('#torrent-card-title'), tabs = head.querySelector('.filter-tabs');
  const r = node => node.getBoundingClientRect(), s = getComputedStyle(head);
  const shown = node => getComputedStyle(node).display !== 'none' && Number(getComputedStyle(node).opacity) > 0.5;
  const textRight = node => { const range = document.createRange(); range.selectNodeContents(node); return range.getBoundingClientRect().right; };
  const copy = [...title.querySelectorAll('.dp-downloads-heading-copy > *')].filter(shown);
  const icon = title.querySelector('.dp-downloads-title-icon');
  return {
    content: head.clientWidth - parseFloat(s.paddingLeft) - parseFloat(s.paddingRight),
    height: r(head).height, icon: shown(icon) ? r(icon).width : 0,
    visible: copy.map(node => node.textContent),
    clear: r(tabs).left - parseFloat(s.columnGap) - Math.max(r(icon).right, ...copy.map(textRight)),
    oneLine: copy.every(node => r(node).height < 24),
    tabs: [...tabs.children].map(tab => tab.textContent),
    tabsWhole: tabs.scrollWidth <= tabs.clientWidth && r(tabs).right <= r(head).right - parseFloat(s.paddingRight) + 0.5
      && Math.abs((r(tabs).top + r(tabs).bottom) / 2 - (r(head).top + r(head).bottom) / 2) < 1,
    label: title.getAttribute('aria-label'), tooltip: title.getAttribute('data-tooltip'),
  };
});

// The window whose header content box is exactly ``content`` px wide.
async function headerAt(page, content) {
  let width = content + 152;
  for (let step = 0; step < 4; step++) {
    await resize(page, width);
    const now = (await header(page)).content;
    if (now === content) return width;
    width += content - now;
  }
  throw new Error(`no window gave a ${content}px header`);
}

const SEVEN = ['All', 'Downloading', 'Paused', 'Processing', 'Ready', 'Done', 'Error'];
const copy = n => n === 1
  ? {full: ['On the Books', '1 download tracked. It behaved.'], tracked: ['On the Books', '1 download tracked'],
    downloads: ['On the Books', '1 download'], count: ['1']}
  : {full: ['On the Books', `${n} downloads tracked. Most of them behaved.`], tracked: ['On the Books', `${n} downloads tracked`],
    downloads: ['On the Books', `${n} downloads`], count: [String(n)]};

test('the header keeps one row and its seven filters while its identity steps through four presentations, each switch at its own width', async ({page}) => {
  await open(page);
  const rest = (await header(page)).height;
  expect(rest).toBe(70);
  const expected = copy(618);
  const cases = [[FULL + 1, 'full'], [FULL, 'full'], [FULL - 1, 'tracked'], [TRACKED + 1, 'tracked'], [TRACKED, 'tracked'],
    [TRACKED - 1, 'downloads'], [DOWNLOADS + 1, 'downloads'], [DOWNLOADS, 'downloads'], [DOWNLOADS - 1, 'count']];
  for (const [content, stage] of cases) {
    const width = await headerAt(page, content);
    const state = await header(page);
    expect(state.visible, `${content}px header (${width}px window)`).toEqual(expected[stage]);
    expect(state, `${content}px header`).toMatchObject({height: rest, icon: stage === 'count' ? 30 : 51, oneLine: true, tabs: SEVEN, tabsWhole: true,
      label: 'On the Books. 618 downloads tracked. Most of them behaved.',
      tooltip: 'On the Books. 618 downloads tracked. Most of them behaved.'});
    expect(state.clear, `${content}px header`).toBeGreaterThanOrEqual(0);
  }
  for (const [width, stage] of [[1560, 'full'], [1389, 'full'], [780, 'count']]) {
    await resize(page, width);
    const state = await header(page);
    expect(state.visible, `${width}px`).toEqual(expected[stage]);
    expect(state, `${width}px`).toMatchObject({height: rest, icon: stage === 'count' ? 30 : 51, oneLine: true, tabs: SEVEN, tabsWhole: true});
    expect(state.clear, `${width}px`).toBeGreaterThanOrEqual(0);
  }
});

test('every presentation follows the one count /stats gives, singular and plural, under any active filter', async ({page}) => {
  const state = await open(page, {count: 0});
  const widths = {full: 1560, tracked: 800 + 152, downloads: DOWNLOADS + 10 + 152, count: 780};
  for (const count of [0, 1, 9999]) {
    await setCount(page, state, count);
    for (const [stage, width] of Object.entries(widths)) {
      await resize(page, width);
      const read = await header(page);
      expect(read.visible, `${count} at ${width}px`).toEqual(copy(count)[stage]);
      expect(read.clear, `${count} at ${width}px`).toBeGreaterThanOrEqual(0);
      expect(read.height).toBe(70);
    }
  }
  const full = 'On the Books. 1 download tracked. It behaved.';
  await setCount(page, state, 1);
  await expect(page.locator('#torrent-card-title')).toHaveAttribute('data-tooltip', full);
  // A filter's active state takes no width from the identity, and filtering keeps the count.
  for (const width of [FULL + 152, 780]) {
    await resize(page, width);
    for (const filter of ['downloading', 'processing', '']) {
      await page.locator(`#view-torrents .ftab[data-dp-status="${filter}"]`).click();
      await expect(page.locator(`#view-torrents .ftab[data-dp-status="${filter}"]`)).toHaveClass(/\bactive\b/);
      const read = await header(page);
      expect(read).toMatchObject({height: 70, tabs: SEVEN, tabsWhole: true, label: full});
      expect(read.clear).toBeGreaterThanOrEqual(0);
    }
  }
});

test('at 780px the number alone keeps clear of the filters for every count through six digits, in one 70px row', async ({page}) => {
  const state = await open(page);
  await resize(page, DOWNLOADS - 1 + 152);
  const wider = await page.evaluate(() => {
    const head = document.querySelector('#view-torrents > .card > .card-header'), icon = head.querySelector('.dp-downloads-title-icon');
    const r = node => node.getBoundingClientRect();
    return {left: r(icon).left - r(head).left, middle: (r(icon).top + r(icon).bottom) / 2 - r(head).top, src: icon.getAttribute('src')};
  });
  await resize(page, 780);
  // 0 and 1 are the widest figures in the production face, so 100000 is the widest six-digit count.
  for (const count of [618, 1000, 10000, 100000, 111111, 999999]) {
    await setCount(page, state, count);
    const read = await header(page);
    const terminal = await page.evaluate(() => {
      const head = document.querySelector('#view-torrents > .card > .card-header'), icon = head.querySelector('.dp-downloads-title-icon');
      const number = head.querySelector('.dp-downloads-count'), range = document.createRange();
      range.selectNodeContents(number);
      const r = node => node.getBoundingClientRect(), text = range.getBoundingClientRect();
      return {left: r(icon).left - r(head).left, middle: (r(icon).top + r(icon).bottom) / 2 - r(head).top, src: icon.getAttribute('src'),
        gap: Math.round(text.left - r(icon).right), size: getComputedStyle(number).fontSize,
        textMiddle: Math.abs((text.top + text.bottom) / 2 - (r(icon).top + r(icon).bottom) / 2) < 2};
    });
    expect(read, `${count}`).toMatchObject({visible: [String(count)], height: 70, icon: 30, oneLine: true, tabs: SEVEN, tabsWhole: true});
    // The spec's fonts are the wider fallback face: its reserve, measured at 21.9px for 100000.
    expect(read.clear, `${count}`).toBeGreaterThanOrEqual(12);
    expect(terminal).toEqual({left: wider.left, middle: wider.middle, src: wider.src, gap: 6, size: '13px', textMiddle: true});
  }
});

test('the full title and hint stay the title\'s name at every width, read once; a reduced header shows them in DP\'s tooltip', async ({page}) => {
  await open(page);
  const title = page.locator('#torrent-card-title'), tip = page.locator('#dp-tooltip');
  const full = 'On the Books. 618 downloads tracked. Most of them behaved.';
  // What assistive technology reads: the shorter presentations are hidden from it.
  const read = () => page.evaluate(() => {
    const parts = [];
    const walk = node => {
      if (node.nodeType === Node.TEXT_NODE) { if (node.textContent.trim()) parts.push(node.textContent.trim()); return; }
      if (node.getAttribute?.('aria-hidden') === 'true') return;
      node.childNodes.forEach(walk);
    };
    walk(document.getElementById('torrent-card-title'));
    return parts;
  });
  for (const width of [1560, FULL - 1 + 152, TRACKED - 1 + 152, 780]) {
    await resize(page, width);
    await expect(title).toHaveAttribute('aria-label', full);
    expect(await read(), `${width}px`).toEqual(['On the Books', '618 downloads tracked. Most of them behaved.']);
  }
  // The full header has nothing to add: no tooltip.
  await resize(page, 1560);
  await title.hover();
  await page.waitForTimeout(700);
  await expect(tip).toBeHidden();
  // Each reduced presentation: the full wording on hover and on keyboard focus,
  // never a browser tooltip.
  for (const width of [FULL - 1 + 152, TRACKED - 1 + 152, 780]) {
    await page.mouse.move(5, 995);
    await resize(page, width);
    await title.hover();
    await expect(tip).toBeVisible();
    await expect(tip).toHaveText(full);
    await page.mouse.move(5, 995);
    await expect(tip).toBeHidden();
    await page.locator('#torrent-search').focus();
    await page.keyboard.press('Shift');
    await title.focus();
    await expect(tip).toHaveText(full);
    await page.locator('#torrent-search').focus();
    await expect(tip).toBeHidden();
  }
  await expect(title).not.toHaveAttribute('title', /./);
});

// One read of the toolbar as it stands.
const toolbar = page => page.evaluate(() => {
  const bar = document.querySelector('#bulk-bar .dp-downloads-bulk-toolbar');
  const r = node => node.getBoundingClientRect(), s = getComputedStyle(bar);
  const actions = [...bar.querySelectorAll('.dp-downloads-bulk-actions > *')].filter(node => getComputedStyle(node).display !== 'none');
  const status = bar.querySelector('.dp-downloads-bulk-status'), clear = bar.querySelector('.dp-downloads-bulk-action--clear');
  const count = document.getElementById('bulk-count');
  // The text a control shows: every text node none of whose elements up to the control is hidden.
  const visibleText = node => {
    const parts = [], walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      let element = walker.currentNode.parentElement, seen = true;
      for (; element && element !== node.parentElement; element = element.parentElement) {
        const style = getComputedStyle(element);
        if (style.display === 'none' || Number(style.opacity) <= 0.5) seen = false;
      }
      if (seen && walker.currentNode.textContent.trim()) parts.push(walker.currentNode.textContent.trim());
    }
    return parts.join(' ');
  };
  const middle = node => (r(node).top + r(node).bottom) / 2;
  return {
    width: bar.clientWidth, height: r(bar).height,
    order: actions.map(node => node.classList.contains('dp-downloads-bulk-separator') ? '|' : node.dataset.defaultLabel),
    shown: actions.filter(node => node.tagName === 'BUTTON').map(node => visibleText(node)),
    glyphs: actions.filter(node => node.tagName === 'BUTTON').every(node => node.querySelector('svg') && r(node.querySelector('svg')).width === 16),
    sizes: actions.filter(node => node.tagName === 'BUTTON').map(node => [Math.round(r(node).width), Math.round(r(node).height)]),
    separator: actions.filter(node => !node.tagName.startsWith('B')).map(node => [r(node).width, r(node).height]),
    clear: visibleText(clear), count: count.textContent,
    oneRow: [...actions, count, clear].every(node => Math.abs(middle(node) - middle(bar)) < 1),
    apart: r(status).left - parseFloat(s.columnGap) - Math.max(...actions.map(node => r(node).right)),
    inside: r(status).right <= r(bar).right - parseFloat(s.paddingRight) + 0.5 && bar.scrollWidth <= bar.clientWidth,
    countLeftOfClear: r(count).right <= r(clear).left,
  };
});

async function barAt(page, width) {
  let window = width + 118;
  for (let step = 0; step < 4; step++) {
    await resize(page, window);
    const now = (await toolbar(page)).width;
    if (now === width) return window;
    window += width - now;
  }
  throw new Error(`no window gave a ${width}px toolbar`);
}

async function select(page, ids) {
  for (const id of ids) await page.locator(`#t-tbody tr[data-torrent-id="${id}"] .t-chk`).check();
  await expect(page.locator('#bulk-count')).toHaveText(`${ids.length} Selected`);
}

const ACTIONS = ['Pause', 'Resume', 'Reset', 'Move to Top', '|', 'Delete'];
const TOOLTIPS = {pause: 'Pause selected downloads', resume: 'Resume selected downloads', reset: 'Reset selected downloads',
  top: 'Move selected downloads to top', delete: 'Delete selected downloads'};

test('the toolbar keeps its full labels down to 826px; narrower, each action shows its glyph alone and Clear Selections reads Clear, in one row', async ({page}) => {
  await open(page);
  await select(page, [9801, 9802]);
  for (const width of [BULK + 1, BULK]) {
    await barAt(page, width);
    const state = await toolbar(page);
    expect(state, `${width}px toolbar`).toMatchObject({height: 54, order: ACTIONS, shown: ['Pause', 'Resume', 'Reset', 'Move to Top', 'Delete'],
      glyphs: true, clear: 'Clear Selections', count: '2 Selected', oneRow: true, inside: true, countLeftOfClear: true});
    expect(state.apart).toBeGreaterThanOrEqual(0);
  }
  // Full labels say the action; no tooltip repeats them.
  await page.locator('.dp-downloads-bulk-action--pause').hover();
  await page.waitForTimeout(700);
  await expect(page.locator('#dp-tooltip')).toBeHidden();
  await page.mouse.move(5, 995);
  for (const window of [null, 780]) {
    if (window) await resize(page, window); else await barAt(page, BULK - 1);
    const state = await toolbar(page);
    expect(state, `${state.width}px toolbar`).toMatchObject({height: 54, order: ACTIONS, shown: ['', '', '', '', ''], glyphs: true,
      sizes: [[36, 36], [36, 36], [36, 36], [36, 36], [36, 36]], separator: [[1, 24]], clear: 'Clear', count: '2 Selected',
      oneRow: true, inside: true, countLeftOfClear: true});
    expect(state.apart).toBeGreaterThanOrEqual(0);
    // Each keeps its name and says its action in DP's tooltip, by pointer and by keyboard.
    for (const [action, text] of Object.entries(TOOLTIPS)) {
      const button = page.locator(`.dp-downloads-bulk-action--${action}`);
      await expect(button).toHaveAccessibleName(await button.getAttribute('data-default-label'));
      await button.hover();
      await expect(page.locator('#dp-tooltip')).toHaveText(text);
      await expect(page.locator('#dp-tooltip')).toBeVisible();
    }
    await page.mouse.move(5, 995);
    await expect(page.locator('#dp-tooltip')).toBeHidden();
    await page.keyboard.press('Shift');
    await page.locator('.dp-downloads-bulk-action--resume').focus();
    await expect(page.locator('#dp-tooltip')).toHaveText(TOOLTIPS.resume);
    await expect(page.locator('.dp-downloads-bulk-action--resume')).toHaveAttribute('aria-describedby', /dp-tooltip/);
    await page.locator('#torrent-search').focus();
    const clear = page.locator('.dp-downloads-bulk-action--clear');
    await expect(clear).toHaveAccessibleName('Clear Selections');
    await clear.hover();
    await expect(page.locator('#dp-tooltip')).toHaveText('Clear Selections');
    await page.mouse.move(5, 995);
  }
});

test('compact: Move to Top and Delete keep their own conditions, the separator always stands before Delete and leaves with it', async ({page}) => {
  await open(page, {role: 'user'});
  await resize(page, 780);
  // A finished transfer selected: no Move to Top; Delete after its separator.
  await select(page, [9803]);
  let state = await toolbar(page);
  expect(state).toMatchObject({order: ['Pause', 'Resume', 'Reset', '|', 'Delete'], separator: [[1, 24]], oneRow: true, inside: true});
  // A transfer this User may not destroy: no Delete and no separator.
  await page.locator('.dp-downloads-bulk-action--clear').click();
  await select(page, [9804]);
  state = await toolbar(page);
  expect(state).toMatchObject({order: ['Pause', 'Resume', 'Reset', 'Move to Top'], separator: [], oneRow: true, inside: true,
    shown: ['', '', '', ''], clear: 'Clear', count: '1 Selected'});
  // Both absent.
  await page.locator('.dp-downloads-bulk-action--clear').click();
  await select(page, [9805]);
  state = await toolbar(page);
  expect(state).toMatchObject({order: ['Pause', 'Resume', 'Reset'], separator: [], oneRow: true, inside: true});
  // All five again; then the selection shrinks back.
  await page.locator('.dp-downloads-bulk-action--clear').click();
  await expect(page.locator('#bulk-bar')).not.toHaveClass(/\bvisible\b/);
  await select(page, [9801]);
  state = await toolbar(page);
  expect(state).toMatchObject({order: ACTIONS, separator: [[1, 24]], oneRow: true, inside: true});
  expect(state.apart).toBeGreaterThanOrEqual(0);
});

test('compact actions run their own commands; pending keeps the glyph; Clear ends the selection', async ({page}) => {
  const state = await open(page);
  await resize(page, 780);
  await select(page, [9801, 9802]);
  let release;
  state.hold = new Promise(resolve => { release = resolve; });
  const pause = page.locator('.dp-downloads-bulk-action--pause');
  await pause.click();
  await expect.poll(() => state.bulk.length).toBe(1);
  expect(state.bulk[0]).toEqual({ids: [9801, 9802], action: 'pause'});
  await expect(pause).toBeDisabled();
  await expect(pause).toHaveAttribute('aria-busy', 'true');
  await expect(pause.locator('svg[data-dp-lucide="pause"]')).toHaveCount(1);
  await expect(pause).toHaveAccessibleName('Pausing…');
  state.hold = null;
  release();
  await expect(page.locator('#bulk-bar')).not.toHaveClass(/\bvisible\b/);
  await expect(pause).toBeEnabled();
  await expect(pause.locator('svg[data-dp-lucide="pause"]')).toHaveCount(1);
  await expect(pause.locator('[data-dp-icon-label]')).toHaveText('Pause');
  await select(page, [9801]);
  expect((await toolbar(page)).shown).toEqual(['', '', '', '', '']);
  await page.locator('.dp-downloads-bulk-action--top').click();
  await expect.poll(() => state.tops.length).toBe(1);
  expect(state.tops[0].ids).toEqual([9801]);
  await select(page, [9802]);
  await page.locator('.dp-downloads-bulk-action--clear').click();
  await expect(page.locator('#bulk-bar')).not.toHaveClass(/\bvisible\b/);
  await expect(page.locator('#t-tbody .t-chk:checked')).toHaveCount(0);
});

test('mobile (779px) has its own header and toolbar: one header row with the status control in place of the filters, the full hint beside it, full action labels and Clear', async ({page}) => {
  // The handheld composition itself is downloads-mobile-header-bulk-filters.spec.js;
  // here, only that the desktop's header and toolbar decisions end at 780px.
  await open(page);
  await select(page, [9801, 9802]);
  await resize(page, 779);
  const state = await page.evaluate(() => {
    const head = document.querySelector('#view-torrents > .card > .card-header');
    const shown = node => getComputedStyle(node).display !== 'none' && Number(getComputedStyle(node).opacity) > 0.5;
    const r = node => node.getBoundingClientRect();
    // The text a control shows: every text node none of whose elements up to the control is hidden.
    const visibleText = node => {
      const parts = [], walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
      while (walker.nextNode()) {
        let seen = true;
        for (let element = walker.currentNode.parentElement; element && element !== node.parentElement; element = element.parentElement) {
          if (!shown(element) || getComputedStyle(element).visibility === 'hidden') seen = false;
        }
        if (seen && walker.currentNode.textContent.trim()) parts.push(walker.currentNode.textContent.trim());
      }
      return parts.join(' ');
    };
    const buttons = [...document.querySelectorAll('#bulk-bar .dp-downloads-bulk-action')].filter(shown);
    const control = document.getElementById('torrent-filter');
    return {
      visible: [...head.querySelectorAll('.dp-downloads-heading-copy > *')].filter(shown).map(node => node.textContent),
      filters: shown(head.querySelector('.filter-tabs')), control: visibleText(control),
      oneRow: Math.abs(r(control).top - r(head).top - (r(head).height - r(control).height) / 2) < 1 && r(head).height === 70,
      labels: buttons.map(visibleText),
      labelled: buttons.every(button => r(button).width > 36),
      count: r(document.getElementById('bulk-count')).width <= 1,
    };
  });
  expect(state).toEqual({visible: ['On the Books', '618 downloads tracked. Most of them behaved.'], filters: false, control: 'Status', oneRow: true,
    labels: ['Pause', 'Resume', 'Reset', 'Move to Top', 'Delete', 'Clear'], labelled: true, count: true});
});

// The keyboard stops from the header's speed-cap selector (in the topbar from
// 780px and in the handheld header below it; Pause All is desktop-only) to
// Search, each named by its id or class.
async function stopsToSearch(page) {
  await page.locator('#runtime-cap-toggle').focus();
  const stops = [];
  for (let step = 0; step < 30; step++) {
    await page.keyboard.press('Tab');
    const stop = await page.evaluate(() => {
      const node = document.activeElement;
      return node === document.body ? 'body' : node.id || `${node.tagName.toLowerCase()}.${[...node.classList].join('.')}`;
    });
    stops.push(stop);
    if (stop === 'torrent-search') return stops;
  }
  throw new Error(`Search was not reached: ${stops.join(' > ')}`);
}

test('the title is a keyboard stop from 780px, where its tooltip says the full wording; mobile (779px) keeps its tab order', async ({page}) => {
  await open(page);
  await resize(page, 779);
  const mobile = await stopsToSearch(page);
  expect(mobile).not.toContain('torrent-card-title');
  await expect(page.locator('#torrent-card-title')).not.toHaveAttribute('tabindex', /.*/);
  await resize(page, 780);
  const tablet = await stopsToSearch(page);
  // The shell's own controls are keyboard stops on both sides of 780px (the
  // handheld header keeps the theme toggle and its chevron); Downloads adds one
  // stop from 780px, the title, just before the filters, and the seven filters
  // stand where the handheld's one status control does. Nothing else moves.
  const SHELL = ['theme-toggle', 'theme-picker-toggle'];
  expect(SHELL.every(stop => tablet.includes(stop) && mobile.includes(stop))).toBe(true);
  const filters = tablet.filter(stop => stop.startsWith('div.ftab'));
  expect(filters).toHaveLength(7);
  expect(mobile.filter(stop => stop === 'torrent-filter')).toHaveLength(1);
  const firstFilter = tablet.indexOf(filters[0]);
  expect([...tablet.slice(0, firstFilter), 'torrent-filter', ...tablet.slice(firstFilter + 7)].filter(stop => stop !== 'torrent-card-title')).toEqual(mobile);
  expect(tablet.indexOf('torrent-card-title')).toBe(tablet.findIndex(stop => stop.startsWith('div.ftab')) - 1);
  await page.locator('#view-torrents .ftab').first().focus();
  await page.keyboard.press('Shift+Tab');
  await expect(page.locator('#torrent-card-title')).toBeFocused();
  await expect(page.locator('#dp-tooltip')).toHaveText('On the Books. 618 downloads tracked. Most of them behaved.');
  // Back to mobile: the stop leaves again.
  await page.locator('#torrent-search').focus();
  await resize(page, 779);
  expect(await stopsToSearch(page)).toEqual(mobile);
});
