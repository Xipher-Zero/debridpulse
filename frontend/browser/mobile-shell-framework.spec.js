const { test, expect } = require('@playwright/test');

/* DP 1.0.14 handheld shell (below 780px): one header, the page as the one
 * scrolling workspace and one themed dock, for every role. Only the shell is
 * qualified here; the pages' own handheld layouts are later work. Every
 * account on the password-auth runtime is a synthetic fixture this file
 * creates under a run-unique name; runtime facts are routed, so nothing here
 * depends on a live transfer. */

const AUTH = process.env.DP_AUTH_BASE_URL;
const ADMIN = {username: 'browser', password: 'browser-secret'};
const run = Date.now().toString(36);
const adminSession = require('./acl-admin-session');

const noFonts = page => page.route(/fonts\.(googleapis|gstatic)\.com/, route => route.abort());
const noPulse = page => page.route('**/api/events/stream', () => {});
// The view region's slide (DPMotion) has ended: no surface in flight.
const still = page => page.waitForFunction(() => !document.querySelector('.dp-motion-surface'));
const dock = page => page.locator('#shell-dock > *').evaluateAll(items => items.map(item => ({
  tag: item.localName, label: item.querySelector('.dp-shell-label')?.textContent || '', name: item.getAttribute('aria-label'),
  view: item.dataset.view || '', current: item.getAttribute('aria-current'), active: item.classList.contains('active'),
  box: (({x, width, height}) => ({x, width, height}))(item.getBoundingClientRect()),
})));
const sheetOpen = (page, id) => page.evaluate(sheet => document.getElementById(sheet)?.matches(':popover-open') || false, id);
const title = page => page.locator('#page-title').textContent();
const view = page => page.evaluate(() => [...document.querySelectorAll('.view.active')].map(node => node.id));
// The header's interactive controls as actually hit: each one's own box, and
// whether the point at its centre is that control (no other region over it).
const headerHits = (page, ids) => page.evaluate(ids => ids.filter(id => document.querySelector(id)).map(id => {
  const node = document.querySelector(id);
  const {left, top, width, height, right, bottom} = node.getBoundingClientRect();
  const hit = document.elementFromPoint(left + width / 2, top + height / 2);
  return {id, left, top, width, height, right, bottom, own: !!hit && node.contains(hit)};
}), ids);
const HEADER_CONTROLS = ['#topbar-account-button', '#theme-toggle', '#theme-picker-toggle', '#runtime-cap-toggle'];
// The header is one row: every control and the title within its box, centred on one line.
const headerRow = page => page.evaluate(() => {
  const bar = document.getElementById('topbar').getBoundingClientRect();
  const parts = ['#page-title', '#topbar-account-button', '#runtime-speed-badge', '#theme-toggle', '#theme-picker-toggle']
    .map(id => document.querySelector(id)).filter(node => node?.getClientRects().length).map(node => node.getBoundingClientRect());
  const centres = parts.map(box => box.top + box.height / 2);
  return {inside: parts.every(box => box.top >= bar.top - 0.5 && box.bottom <= bar.bottom + 0.5),
    oneLine: Math.max(...centres) - Math.min(...centres) <= 2, height: Math.round(bar.height)};
});
// A menu's panel has finished its slide (its own transition, not a sleep).
const rested = (page, sel) => page.waitForFunction(selector => document.querySelector(selector)?.getAnimations().length === 0, sel);
const shown = (page, id) => page.evaluate(sel => { const node = document.querySelector(sel); return !!node && node.getBoundingClientRect().width > 1; }, id);

/* The shell menus: each one's own box is the page between the bars (its
 * scrim), its panel clipped to it. Sampled every frame while it opens or
 * closes: the box never reaches over the header or the dock, the header and
 * the dock stay the topmost element at their own points, and the panel
 * travels along its axis. */
const MENUS = {
  theme: {open: '#theme-picker-toggle', box: '#theme-picker', panel: '#theme-picker .dp-theme-picker-panel', from: -1},
  cap: {open: '#runtime-cap-toggle', box: '#runtime-cap-menu', panel: '#runtime-cap-menu .runtime-cap-panel', from: -1},
  more: {open: '#shell-dock .dp-shell-dock-more', box: '#shell-more', panel: '#shell-more .dp-shell-sheet-panel', from: 1},
  account: {open: '#topbar-account-button', box: '#topbar-account-sheet', panel: '#topbar-account-sheet .dp-shell-sheet-panel', from: -1},
};
const sample = (page, menu) => page.evaluate(([box, panel]) => {
  window.__frames = [];
  const end = performance.now() + 700;
  const tick = () => {
    const shell = document.querySelector(box), drawn = document.querySelector(panel);
    if (shell && getComputedStyle(shell).display !== 'none') {
      const r = node => node.getBoundingClientRect();
      const bar = r(document.getElementById('topbar')), dock = r(document.getElementById('shell-dock'));
      const title = r(document.getElementById('page-title')), item = r(document.querySelector('#shell-dock > button'));
      const top = (x, y, owner) => owner.contains(document.elementFromPoint(x, y));
      const translate = new DOMMatrixReadOnly(getComputedStyle(drawn).transform).m42;
      window.__frames.push({box: [r(shell).top, r(shell).bottom], panel: [r(drawn).top, r(drawn).bottom], header: bar.bottom, dock: dock.top,
        across: [r(shell).left, r(shell).width, r(drawn).left, r(drawn).width], height: r(drawn).height, layout: getComputedStyle(shell).flexDirection,
        clip: getComputedStyle(shell).overflow, translate, opacity: getComputedStyle(drawn).opacity,
        chrome: top(title.left + 4, title.top + title.height / 2, document.getElementById('topbar'))
          && top(item.left + item.width / 2, item.top + item.height / 2, document.getElementById('shell-dock'))});
    }
    if (performance.now() < end) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}, [MENUS[menu].box, MENUS[menu].panel]);
const frames = page => page.waitForFunction(() => window.__frames && performance.now() > 0, null, {timeout: 2000})
  .then(() => page.waitForTimeout(750)).then(() => page.evaluate(() => window.__frames));
function expectBounded(list, label, {moved = true, from = -1} = {}) {
  expect(list.length, `${label}: frames sampled`).toBeGreaterThan(3);
  for (const frame of list) {
    expect(frame.box[0], `${label}: the menu reaches over the header`).toBeGreaterThanOrEqual(frame.header - 0.5);
    expect(frame.box[1], `${label}: the menu reaches over the dock`).toBeLessThanOrEqual(frame.dock + 0.5);
    expect(frame.clip, `${label}: the panel is clipped to the menu's box`).toBe('hidden');
    expect(frame.chrome, `${label}: the header or the dock is covered`).toBe(true);
    // The panel keeps the menu's whole width and its place across it in every
    // frame, closing included: never narrowed to its content or moved aside.
    const [boxLeft, boxWidth, left, width] = frame.across;
    expect(Math.abs(left - boxLeft), `${label}: the panel moved aside`).toBeLessThanOrEqual(0.5);
    expect(Math.abs(width - boxWidth), `${label}: the panel narrowed`).toBeLessThanOrEqual(0.5);
  }
  // Its height is its own throughout (not stretched to the menu's box as it closes).
  const heights = list.map(frame => Math.round(frame.height));
  expect(Math.max(...heights) - Math.min(...heights), `${label}: the panel changed height`).toBeLessThanOrEqual(1);
  if (moved) {
    // The panel travels from its bar's edge: up (header menus) or down (More) of rest, never the other way.
    const travel = list.map(frame => frame.translate).filter(value => Math.abs(value) > 1);
    expect(travel.length, `${label}: the panel slid`).toBeGreaterThan(0);
    expect(travel.every(value => Math.sign(value) === from), `${label}: the panel slides from its bar`).toBe(true);
  }
}

async function expectHeaderTargets(page, width) {
  const hits = await headerHits(page, HEADER_CONTROLS);
  for (const hit of hits) {
    expect(hit.width, `${width}px ${hit.id} width`).toBeGreaterThanOrEqual(44);
    expect(hit.height, `${width}px ${hit.id} height`).toBeGreaterThanOrEqual(44);
    expect(hit.left, `${width}px ${hit.id}`).toBeGreaterThanOrEqual(0);
    expect(hit.right, `${width}px ${hit.id}`).toBeLessThanOrEqual(width);
    expect(hit.own, `${width}px ${hit.id} is hit at its centre`).toBe(true);
  }
  for (const [i, a] of hits.entries()) for (const b of hits.slice(i + 1)) {
    expect(a.right <= b.left + 0.5 || b.right <= a.left + 0.5 || a.bottom <= b.top + 0.5 || b.bottom <= a.top + 0.5, `${a.id} / ${b.id} overlap`).toBe(true);
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
  return hits;
}
// Where each click lands (its target), recorded before any handler runs.
const recordClicks = page => page.evaluate(() => {
  window.__clicks = [];
  document.addEventListener('click', event => window.__clicks.push(event.target.id || event.target.className), true);
});
const clicks = page => page.evaluate(() => window.__clicks.splice(0));

async function open(page, {width = 390, height = 844, path = '/'} = {}) {
  await noFonts(page);
  await noPulse(page);
  await page.setViewportSize({width, height});
  await page.goto(path);
  await page.waitForFunction(() => document.querySelector('#shell-dock > *') && window.DPTheme);
}

// The shell's one version datum at the end of the page's scroll: where its
// text is drawn against the active master surface, the workspace and the dock.
const versionAtEnd = page => page.evaluate(() => {
  const content = document.getElementById('content');
  content.scrollTop = content.scrollHeight;
  const label = document.getElementById('sidebar-version');
  const range = document.createRange();
  range.selectNodeContents(label);
  const text = range.getBoundingClientRect();
  const surface = document.querySelector('.view.active').getBoundingClientRect();
  const workspace = content.getBoundingClientRect();
  const bar = document.getElementById('shell-dock').getBoundingClientRect();
  return {
    labels: document.querySelectorAll('.dp-app-version').length, parent: label.parentElement.id, text: label.textContent,
    belowSurface: text.top >= surface.bottom - 0.5, rightAligned: Math.abs(text.right - surface.right) <= 7,
    insideWorkspace: text.top >= workspace.top && text.bottom <= workspace.bottom + 0.5,
    clearOfDock: bar.height === 0 || text.bottom <= bar.top + 0.5, atEnd: content.scrollTop + content.clientHeight >= content.scrollHeight - 1,
  };
});

test.describe('handheld shell, open runtime (every capability)', () => {
  test('the dock: five positions, Dashboard in the middle, More holding Activity Log and Help, following the active page', async ({page}) => {
    await open(page, {width: 320, height: 760});
    let items = await dock(page);
    expect(items.map(item => item.label)).toEqual(['Downloads', 'Stats', 'Dashboard', 'Settings', 'More']);
    expect(items.map(item => item.tag)).toEqual(Array(5).fill('button'));
    expect(items[1].name).toBe('Statistics');                         // the short label keeps the whole name
    expect(items[2].box.x + items[2].box.width / 2).toBeCloseTo(160, 0);
    for (const item of items) {
      expect(item.box.height).toBeGreaterThanOrEqual(48);
      expect(item.box.width).toBeGreaterThanOrEqual(60);
    }
    // Every label is drawn whole at 320px.
    expect(await page.locator('#shell-dock .dp-shell-label').evaluateAll(labels => labels.filter(l => l.scrollWidth > l.clientWidth + 0.5).length)).toBe(0);
    expect(items.filter(item => item.current === 'page').map(item => item.view)).toEqual(['dashboard']);
    await expect(page.locator('#sidebar')).toBeHidden();
    await expect(page.locator('#mobile-menu-btn, #mobile-overlay')).toHaveCount(0);

    // Each direct destination: its page, its title, its button current.
    for (const [label, id, heading] of [['Downloads', 'view-torrents', 'Downloads'], ['Stats', 'view-stats', 'Statistics'],
      ['Settings', 'view-settings', 'Settings'], ['Dashboard', 'view-dashboard', 'Dashboard']]) {
      await page.locator('#shell-dock .dp-shell-dock-item', {hasText: label}).click();
      await still(page);
      expect(await view(page)).toEqual([id]);
      expect(await title(page)).toBe(heading);
      items = await dock(page);
      expect(items.filter(item => item.current === 'page').map(item => item.label)).toEqual([label]);
    }

    // More is an action, not a page: opening and closing it changes nothing.
    const more = page.locator('#shell-dock .dp-shell-dock-more');
    await more.click();
    expect(await sheetOpen(page, 'shell-more')).toBe(true);
    expect(await page.locator('#shell-more .dp-shell-sheet-item').evaluateAll(rows => rows.map(row => row.textContent.trim())))
      .toEqual(['Activity Log', 'Help & License']);
    expect(await page.evaluate(() => document.activeElement.closest('#shell-more') !== null)).toBe(true);
    // A press on the scrim -- here over the page's first card -- closes the sheet and reaches nothing beneath.
    await recordClicks(page);
    await page.mouse.click(96, 200);
    expect(await clicks(page)).toEqual(['shell-more']);
    expect(await sheetOpen(page, 'shell-more')).toBe(false);
    expect(await view(page)).toEqual(['view-dashboard']);
    await expect(more).toBeFocused();
    await page.keyboard.press('Enter');
    expect(await sheetOpen(page, 'shell-more')).toBe(true);
    await page.keyboard.press('Escape');
    expect(await sheetOpen(page, 'shell-more')).toBe(false);
    expect(await view(page)).toEqual(['view-dashboard']);

    for (const [label, id] of [['Activity Log', 'view-events'], ['Help & License', 'view-help']]) {
      await more.click();
      await page.locator('#shell-more .dp-shell-sheet-item', {hasText: label}).click();
      await still(page);
      expect(await sheetOpen(page, 'shell-more')).toBe(false);
      expect(await view(page)).toEqual([id]);
      expect(await title(page)).toBe(label);
      expect(await more.evaluate(node => [node.classList.contains('active'), node.getAttribute('aria-label')]))
        .toEqual([true, `More, current page: ${label}`]);
      items = await dock(page);
      expect(items.filter(item => item.current === 'page')).toEqual([]);
    }
    // Help opens on Quick Start, as from the sidebar.
    await expect(page.locator('#view-help [role="tab"][aria-selected="true"]:not(.dp-motion-still *)')).toHaveText(/Quick Start/);
  });

  test('a dock choice slides the page across by dock order; the Downloads count is the sidebar badge value', async ({page}) => {
    await page.route('**/api/stats', async route => {
      const response = await route.fetch();
      const body = await response.json();
      body.by_status = {...(body.by_status || {}), downloading: 2, queued: 1, input_required: 1, error: 3};
      await route.fulfill({response, json: body});
    });
    await open(page, {width: 390, height: 844});
    await expect(page.locator('#shell-dock [data-dock-badge]')).toHaveText('4');
    expect(await page.locator('#nb-active').textContent()).toBe('4');
    await page.evaluate(() => {
      window.__slides = [];
      const slide = window.DPMotion.slide;
      // Observation only: the arguments each slide is made with.
      window.DPMotion = {...window.DPMotion, slide: (spec, commit) => { window.__slides.push({axis: spec.axis, direction: spec.direction, distance: spec.distance}); return slide(spec, commit); }};
    });
    await page.locator('#shell-dock .dp-shell-dock-item', {hasText: 'Downloads'}).click();
    await still(page);
    await page.locator('#shell-dock .dp-shell-dock-item', {hasText: 'Settings'}).click();
    await still(page);
    expect(await page.evaluate(() => window.__slides)).toEqual([{axis: 'x', direction: -1, distance: 2}, {axis: 'x', direction: 1, distance: 3}]);
  });

  test('the header: the page title, the live speed from the runtime fact, active/max and the cap; no Pause controls', async ({page}) => {
    let live = 5 * 1048576;
    await page.route('**/api/execution/throughput', route => route.fulfill({json: {download_bytes_per_second: live,
      max_download_bytes_per_second: 0, sustained_peak_bytes_per_second: live}}));
    await page.route('**/api/execution/runtime-status', route => route.fulfill({json: {active_execution_slots: 2}}));
    await open(page, {width: 320, height: 760});
    await expect(page.locator('#runtime-badge-speed')).toHaveText(/^5(\.0+)? MB\/s$/);
    live = 2 * 1048576;
    await expect(page.locator('#runtime-badge-speed')).toHaveText(/^2(\.0+)? MB\/s$/);
    await expect(page.locator('#runtime-badge-active')).toHaveText('2');
    await expect(page.locator('#runtime-badge-max')).not.toHaveText('');
    for (const id of ['#runtime-speed-badge', '#runtime-cap-toggle', '#theme-toggle', '#theme-picker-toggle', '#page-title']) {
      await expect(page.locator(id), id).toBeVisible();
    }
    for (const id of ['#topbar-actions', '#btn-pause-all', '#btn-resume-all', '#btn-resume-paused', '#page-subtitle']) {
      await expect(page.locator(id), id).toBeHidden();
    }
    // Every control within the header and the window, none over another.
    const boxes = await page.evaluate(() => ['#page-title', '#runtime-speed-badge', '#theme-toggle', '#theme-picker-toggle']
      .map(id => (({left, right, top, bottom}) => ({id, left, right, top, bottom}))(document.querySelector(id).getBoundingClientRect())));
    const header = await page.locator('#topbar').boundingBox();
    for (const box of boxes) {
      expect(box.left, box.id).toBeGreaterThanOrEqual(0);
      expect(box.right, box.id).toBeLessThanOrEqual(320);
      expect(box.bottom, box.id).toBeLessThanOrEqual(header.y + header.height);
    }
    for (const [i, a] of boxes.entries()) for (const b of boxes.slice(i + 1)) {
      expect(a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top, `${a.id} / ${b.id}`).toBe(true);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(320);
    // One header row at every handheld width, every control a whole 44 x 44 px
    // target; the speed reading gives way in its stages, the live speed never.
    await page.locator('#shell-dock .dp-shell-dock-item', {hasText: 'Downloads'}).click();
    await still(page);
    for (const width of [320, 375, 390, 430, 447, 448, 494, 495, 565, 566, 599, 600, 779]) {
      await page.setViewportSize({width, height: 760});
      expect(await headerRow(page), `${width}px`).toEqual({inside: true, oneLine: true, height: 61});
      expect((await expectHeaderTargets(page, width)).map(hit => hit.id)).toEqual(['#theme-toggle', '#theme-picker-toggle', '#runtime-cap-toggle']);
      expect(await page.locator('#page-title').evaluate(node => node.scrollWidth <= node.clientWidth + 0.5), `${width}px: Downloads whole`).toBe(true);
      expect({occupancy: await shown(page, '#runtime-badge-active'), cap: await shown(page, '#runtime-badge-limit'),
        speed: await shown(page, '#runtime-badge-speed'), split: await page.locator('#runtime-badge-speed').evaluate(node => node.getClientRects().length > 1 || node.getBoundingClientRect().height > 20)},
        `${width}px stage`).toEqual({occupancy: width >= 566, cap: width >= 495, speed: true, split: width <= 447});
    }
    // The cap's value, drawn or not, is the selector's accessible name.
    await expect(page.locator('#runtime-cap-toggle')).toHaveAccessibleName(/Unlimited|MB\/s/);
    await page.setViewportSize({width: 320, height: 760});
    // Every page title: its short name drawn where it has one, its whole name the heading's.
    for (const [label, whole, drawn] of [['Downloads', 'Downloads', 'Downloads'], ['Stats', 'Statistics', 'Stats'],
      ['Settings', 'Settings', 'Settings'], ['Dashboard', 'Dashboard', 'Dashboard']]) {
      await page.locator('#shell-dock .dp-shell-dock-item', {hasText: label}).click();
      await still(page);
      await expect(page.getByRole('heading', {level: 1})).toHaveAccessibleName(whole);
      expect(await page.locator('#page-title').evaluate(node => node.dataset.short || node.textContent)).toBe(drawn);
    }
    for (const [label, whole, drawn] of [['Activity Log', 'Activity Log', 'Activity'], ['Help & License', 'Help & License', 'Help']]) {
      await page.locator('#shell-dock .dp-shell-dock-more').click();
      await page.locator('#shell-more .dp-shell-sheet-item', {hasText: label}).click();
      await still(page);
      await expect(page.getByRole('heading', {level: 1})).toHaveAccessibleName(whole);
      expect(await page.locator('#page-title').evaluate(node => getComputedStyle(node, '::before').content)).toContain(`"${drawn}"`);
      expect(await headerRow(page)).toEqual({inside: true, oneLine: true, height: 61});
    }
  });

  test('the speed-cap sheet: over the page and the dock, inside the window, dismissed without reaching the page; presets, custom and refusals use the existing action', async ({page}) => {
    let cap = 0;
    await page.route('**/api/execution/throughput', route => route.fulfill({json: {download_bytes_per_second: 0,
      max_download_bytes_per_second: cap, sustained_peak_bytes_per_second: 0}}));
    const writes = [];
    await page.route('**/api/execution/runtime-limits', async route => {
      const body = route.request().postDataJSON();
      writes.push(body.max_download_bytes_per_second);
      if (body.max_download_bytes_per_second === 1048576) {
        await route.fulfill({status: 400, json: {detail: 'The speed limit was refused.'}});
        return;
      }
      cap = body.max_download_bytes_per_second;
      await route.fulfill({json: {ok: true, configured: {max_download_bytes_per_second: cap}}});
    });
    await open(page, {width: 320, height: 640});
    const menu = page.locator('#runtime-cap-menu');
    await page.locator('#runtime-cap-toggle').click();
    await expect(menu).toBeVisible();
    const box = await menu.boundingBox();
    const bars = await page.evaluate(() => [document.getElementById('topbar').getBoundingClientRect().bottom,
      document.getElementById('shell-dock').getBoundingClientRect().top]);
    expect([box.x, box.width]).toEqual([0, 320]);
    expect([box.y, box.y + box.height]).toEqual(bars);
    // The dock is never covered: its own button is the topmost element there.
    expect(await page.evaluate(() => document.elementFromPoint(160, 630)?.closest('#shell-dock') !== null)).toBe(true);
    // The panel is opaque: its own surface, not the page, behind the choices.
    expect(await page.locator('#runtime-cap-menu .runtime-cap-panel').evaluate(node => getComputedStyle(node).backgroundImage)).toContain('linear-gradient');
    await expect(page.locator('#runtime-cap-menu [data-cap-bps].active')).toBeFocused();
    await rested(page, '#runtime-cap-menu .runtime-cap-panel');
    // A press on the scrim below the panel closes it and reaches nothing beneath (here the page).
    const panel = await page.locator('#runtime-cap-menu .runtime-cap-panel').boundingBox();
    await recordClicks(page);
    await page.mouse.click(96, panel.y + panel.height + 20);
    expect(await clicks(page)).toEqual(['runtime-cap-menu']);
    await expect(menu).toBeHidden();
    expect(await view(page)).toEqual(['view-dashboard']);
    await page.locator('#runtime-cap-toggle').click();
    await page.keyboard.press('Escape');
    await expect(menu).toBeHidden();
    await expect(page.locator('#runtime-cap-toggle')).toBeFocused();

    // A refusal says why and keeps the sheet; a preset applies and closes it.
    await page.locator('#runtime-cap-toggle').click();
    await page.locator('#runtime-cap-menu [data-cap-bps="1048576"]').click();
    await expect(page.locator('#toasts')).toContainText('The speed limit was refused.');
    await expect(menu).toBeVisible();
    await page.locator('#runtime-cap-menu [data-cap-bps="10485760"]').click();
    await expect(menu).toBeHidden();
    await expect(page.locator('#runtime-badge-limit')).toHaveText(/^10(\.0+)? MB\/s$/);
    // Custom: an invalid value is refused locally; a valid one is sent.
    await page.locator('#runtime-cap-toggle').click();
    await page.locator('#runtime-cap-custom-mbps').fill('-1');
    await page.locator('#runtime-cap-menu .runtime-cap-custom .btn').click();
    await expect(page.locator('#toasts')).toContainText('Enter a speed cap of 0 MB/s or greater');
    await page.locator('#runtime-cap-custom-mbps').fill('2.5');
    await page.locator('#runtime-cap-menu .runtime-cap-custom .btn').click();
    await expect(menu).toBeHidden();
    expect(writes).toEqual([1048576, 10485760, Math.round(2.5 * 1048576)]);
  });

  test.describe('touch', () => {
    test.use({hasTouch: true});
    test('the theme control: a tap toggles once, a long press opens the palettes without toggling, the chevron opens the same sheet', async ({page}) => {
      await open(page, {width: 390, height: 844});
      const light = () => page.evaluate(() => document.body.classList.contains('light'));
      const before = await light();
      const toggle = await page.locator('#theme-toggle').boundingBox();
      const at = {x: toggle.x + toggle.width / 2, y: toggle.y + toggle.height / 2};
      await page.touchscreen.tap(at.x, at.y);
      expect(await light()).toBe(!before);
      expect(await sheetOpen(page, 'theme-picker')).toBe(false);
      await page.touchscreen.tap(at.x, at.y);
      expect(await light()).toBe(before);

      // Held: the toggle shows it is armed, and the lift opens the sheet; the mode never changes.
      const cdp = await page.context().newCDPSession(page);
      await cdp.send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: [{x: at.x, y: at.y}]});
      await expect(page.locator('#theme-toggle')).toHaveAttribute('data-theme-hold', 'armed');
      expect(await sheetOpen(page, 'theme-picker')).toBe(false);
      await cdp.send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []});
      await page.evaluate(() => new Promise(requestAnimationFrame));
      expect(await sheetOpen(page, 'theme-picker')).toBe(true);
      expect(await light()).toBe(before);
      const mode = before ? 'Light' : 'Dark';
      await expect(page.locator('#theme-picker-title')).toHaveText(`${mode} themes`);
      const bars = await page.evaluate(() => [document.getElementById('topbar').getBoundingClientRect().bottom,
        document.getElementById('shell-dock').getBoundingClientRect().top]);
      await rested(page, '#theme-picker .dp-theme-picker-panel');
      const sheet = await page.locator('#theme-picker .dp-theme-picker-panel').boundingBox();
      expect(sheet.y).toBeGreaterThanOrEqual(bars[0] - 0.5);
      expect(sheet.y + sheet.height).toBeLessThanOrEqual(bars[1] + 0.5);
      await expect(page.locator('#theme-picker [aria-checked="true"]')).toBeFocused();
      // The scrim: a tap below the panel closes it and reaches nothing beneath.
      await page.touchscreen.tap(195, sheet.y + sheet.height + 20);
      expect(await sheetOpen(page, 'theme-picker')).toBe(false);
      expect(await light()).toBe(before);
      // The header stays usable over an open menu: the mode toggle tapped while it
      // is open (after a long press it is the menu's opener) closes it and changes the mode once.
      await cdp.send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: [{x: at.x, y: at.y}]});
      await expect(page.locator('#theme-toggle')).toHaveAttribute('data-theme-hold', 'armed');
      await cdp.send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []});
      await expect.poll(() => sheetOpen(page, 'theme-picker')).toBe(true);
      await page.touchscreen.tap(at.x, at.y);
      expect(await sheetOpen(page, 'theme-picker')).toBe(false);
      expect(await light()).toBe(!before);
      await page.touchscreen.tap(at.x, at.y);
      expect(await light()).toBe(before);

      // Moving off the toggle cancels a press: no sheet, no toggle.
      await cdp.send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: [{x: at.x, y: at.y}]});
      await cdp.send('Input.dispatchTouchEvent', {type: 'touchMove', touchPoints: [{x: at.x - 40, y: at.y + 40}]});
      await page.waitForTimeout(700);                                  // past the 480 ms hold
      await expect(page.locator('#theme-toggle')).not.toHaveAttribute('data-theme-hold', /./);
      await cdp.send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []});
      expect(await sheetOpen(page, 'theme-picker')).toBe(false);
      expect(await light()).toBe(before);

      // The chevron: its own target, the same sheet; a choice applies to this mode only.
      const chevron = await page.locator('#theme-picker-toggle').boundingBox();
      expect(chevron.x).toBeGreaterThanOrEqual(toggle.x + toggle.width - 0.5);
      expect(chevron.height).toBeGreaterThanOrEqual(44);
      expect(chevron.width).toBeGreaterThanOrEqual(44);
      await page.touchscreen.tap(chevron.x + chevron.width / 2, chevron.y + chevron.height / 2);
      expect(await sheetOpen(page, 'theme-picker')).toBe(true);
      const other = before ? 'catppuccin-latte' : 'dracula';
      await page.locator(`#theme-picker [data-palette="${other}"]`).tap();
      expect(await sheetOpen(page, 'theme-picker')).toBe(false);
      expect(await page.evaluate(() => [document.body.getAttribute('data-dp-palette'), document.body.classList.contains('light')]))
        .toEqual([other, before]);
      await expect(page.locator('#theme-picker-toggle')).toBeFocused();
    });
  });

  test('the dock and sheets carry the sidebar theme tokens in dark, light and an alternative palette', async ({page}) => {
    await open(page, {width: 390, height: 844});
    for (const [light, palette] of [[false, null], [true, null], [false, 'dracula'], [true, 'catppuccin-latte']]) {
      await page.evaluate(([light, palette]) => {
        window.DPTheme.setMode(light);
        if (palette) window.DPTheme.choose(palette);
        document.dispatchEvent(new CustomEvent('debridpulse:theme-changed', {detail: {light}}));
      }, [light, palette]);
      const facts = await page.evaluate(() => {
        const probe = document.createElement('div');
        document.body.append(probe);
        const resolve = (property, value) => { probe.removeAttribute('style'); probe.style[property] = value; return getComputedStyle(probe)[property]; };
        const active = document.querySelector('#shell-dock .dp-shell-dock-item.active');
        const idle = document.querySelector('#shell-dock .dp-shell-dock-item:not(.active)');
        const bar = document.getElementById('shell-dock');
        const facts = {
          surface: getComputedStyle(bar).backgroundImage === resolve('backgroundImage', 'var(--dp-chrome-sidebar-surface)')
            || getComputedStyle(bar).backgroundColor === resolve('backgroundColor', 'var(--dp-chrome-sidebar-surface)'),
          rail: getComputedStyle(active, '::after').backgroundImage === resolve('backgroundImage', 'var(--dp-chrome-nav-rail)'),
          activeSurface: getComputedStyle(active, '::before').backgroundImage === resolve('backgroundImage', 'var(--dp-chrome-nav-active-surface)'),
          icon: getComputedStyle(active.querySelector('.icon')).color === resolve('color', 'var(--dp-chrome-nav-icon-active)'),
          idle: getComputedStyle(idle).color === resolve('color', 'var(--dp-chrome-nav-text)'),
          distinct: getComputedStyle(active).color !== getComputedStyle(idle).color,
          weight: [getComputedStyle(active).fontWeight, getComputedStyle(idle).fontWeight],
        };
        probe.remove();
        return facts;
      });
      expect(facts, `${light ? 'light' : 'dark'} ${palette || 'native'}`).toEqual({surface: true, rail: true, activeSurface: true,
        icon: true, idle: true, distinct: true, weight: ['650', '500']});
      await page.screenshot({path: `test-results/checkpoint-mobile-dock-${light ? 'light' : 'dark'}-${palette || 'native'}.png`,
        clip: {x: 0, y: 844 - 120, width: 390, height: 120}});
    }
  });

  test('every shell menu opens from its own bar into the page between them, frame by frame, in dark and light, and never covers the header or the dock', async ({page}) => {
    test.setTimeout(90_000);
    await open(page, {width: 320, height: 700});
    for (const width of [320, 390, 600, 779]) {
      await page.setViewportSize({width, height: 700});
      for (const light of [false, true]) {
        await page.evaluate(light => { window.DPTheme.setMode(light); document.dispatchEvent(new CustomEvent('debridpulse:theme-changed', {detail: {light}})); }, light);
        for (const menu of ['theme', 'cap', 'more']) {
          const label = `${width}px ${light ? 'light' : 'dark'} ${menu}`;
          await sample(page, menu);
          await page.locator(MENUS[menu].open).click();
          expectBounded(await frames(page), `${label} opening`, {from: MENUS[menu].from});
          await sample(page, menu);
          await page.keyboard.press('Escape');
          expectBounded(await frames(page), `${label} closing`, {from: MENUS[menu].from});
          expect(await page.evaluate(sel => getComputedStyle(document.querySelector(sel)).display, MENUS[menu].box), `${label} gone`).toBe('none');
          await expect(page.locator(MENUS[menu].open)).toBeFocused();
        }
      }
      await page.screenshot({path: `test-results/checkpoint-mobile-menus-${width}.png`});
    }
    // Reopened while it is still closing, and closed again: the same bounds and width in every frame.
    await page.setViewportSize({width: 390, height: 700});
    for (const menu of ['theme', 'more']) {
      await page.locator(MENUS[menu].open).click();
      await rested(page, MENUS[menu].panel);
      await sample(page, menu);
      await page.keyboard.press('Escape');
      await page.waitForFunction(sel => document.querySelector(sel).getAnimations().length > 0, MENUS[menu].panel);
      await page.locator(MENUS[menu].open).click();
      await rested(page, MENUS[menu].panel);
      await page.keyboard.press('Escape');
      expectBounded(await frames(page), `${menu} reopened while closing`, {moved: false});
      expect(await page.evaluate(sel => getComputedStyle(document.querySelector(sel)).display, MENUS[menu].box)).toBe('none');
    }
  });

  test('reduced motion: every shell menu opens and closes at once, in the same bounds', async ({page}) => {
    await page.emulateMedia({reducedMotion: 'reduce'});
    await open(page, {width: 390, height: 700});
    for (const menu of ['theme', 'cap', 'more']) {
      await sample(page, menu);
      await page.locator(MENUS[menu].open).click();
      const opened = await frames(page);
      expectBounded(opened, `${menu} opening`, {moved: false});
      expect(opened.every(frame => Math.abs(frame.translate) <= 0.5), `${menu}: no travel`).toBe(true);
      await page.keyboard.press('Escape');
      expect(await page.evaluate(sel => getComputedStyle(document.querySelector(sel)).display, MENUS[menu].box)).toBe('none');
    }
  });

  test('one shell menu at a time: another bar control switches to its menu, a dock choice closes More and navigates, quick repeats settle, no press reaches the page', async ({page}) => {
    await open(page, {width: 390, height: 760});
    const state = () => page.evaluate(() => ({theme: document.getElementById('theme-picker').matches(':popover-open'),
      cap: !document.getElementById('runtime-cap-menu').hidden, more: document.getElementById('shell-more').matches(':popover-open')}));
    await recordClicks(page);
    await page.locator('#theme-picker-toggle').click();
    await page.locator('#runtime-cap-toggle').click();
    expect(await state()).toEqual({theme: false, cap: true, more: false});
    await page.locator('#shell-dock .dp-shell-dock-more').click();
    expect(await state()).toEqual({theme: false, cap: false, more: true});
    await page.locator('#theme-picker-toggle').click();
    expect(await state()).toEqual({theme: true, cap: false, more: false});
    // The open menu's own control closes it.
    await page.locator('#theme-picker-toggle').click();
    expect(await state()).toEqual({theme: false, cap: false, more: false});
    // A dock destination while More is open: More closes and the page opens.
    await page.locator('#shell-dock .dp-shell-dock-more').click();
    await page.locator('#shell-dock .dp-shell-dock-item', {hasText: 'Stats'}).click();
    await still(page);
    expect(await state()).toEqual({theme: false, cap: false, more: false});
    expect(await view(page)).toEqual(['view-stats']);
    // Quick repeats end in one coherent state: open, then closed, nothing left over.
    for (let i = 0; i < 5; i += 1) await page.locator('#runtime-cap-toggle').click();
    expect(await state()).toEqual({theme: false, cap: true, more: false});
    await page.locator('#runtime-cap-toggle').click();
    await page.waitForFunction(() => getComputedStyle(document.getElementById('runtime-cap-menu')).display === 'none');
    expect(await page.evaluate(() => getComputedStyle(document.getElementById('runtime-cap-menu')).display)).toBe('none');
    expect((await clicks(page)).filter(target => typeof target === 'string' && /view-|card|dash/.test(target))).toEqual([]);
  });

  test('the dock carries the sidebar signal artwork once, faint, behind its buttons, taking no input and changing nothing', async ({page}) => {
    await open(page, {width: 390, height: 844});
    for (const [light, palette] of [[false, null], [true, null], [false, 'synthwave-84'], [true, 'gruvbox-light']]) {
      await page.evaluate(([light, palette]) => {
        window.DPTheme.setMode(light);
        if (palette) window.DPTheme.choose(palette);
        document.dispatchEvent(new CustomEvent('debridpulse:theme-changed', {detail: {light}}));
      }, [light, palette]);
      const art = await page.locator('#shell-dock').evaluate(dock => {
        const field = getComputedStyle(dock, '::before');
        const box = dock.getBoundingClientRect();
        const buttons = [...dock.querySelectorAll('button')];
        const turn = new DOMMatrixReadOnly(field.transform);
        return {image: (field.backgroundImage.match(/sidebar-wave-accent\.svg/g) || []).length, size: field.backgroundSize,
          ratio: Math.round(parseFloat(field.width) / parseFloat(field.height) * 1000) / 1000,
          degrees: Math.round(Math.atan2(turn.b, turn.a) * 180 / Math.PI),
          covers: parseFloat(field.left) < 0 && parseFloat(field.width) > box.width,
          events: field.pointerEvents, z: field.zIndex, opacity: Number(field.opacity), animation: field.animationName,
          height: Math.round(box.height), hits: buttons.every(b => { const r = b.getBoundingClientRect(); return b.contains(document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)); }),
          sidebar: getComputedStyle(document.getElementById('sidebar'), '::before').backgroundImage.includes('sidebar-wave-accent.svg')};
      });
      expect(art, `${light ? 'light' : 'dark'} ${palette || 'native'}`).toMatchObject({image: 1, size: '100% 100%', ratio: Math.round(320 / 300 * 1000) / 1000, degrees: 15, covers: true, events: 'none', z: '-2',
        animation: 'none', height: 59, hits: true, sidebar: true});
      expect(art.opacity).toBeLessThanOrEqual(light ? 0.2 : 0.34);   // faint: the artwork's own thin, translucent lines at about a third
      await page.screenshot({path: `test-results/checkpoint-mobile-dock-wave-${light ? 'light' : 'dark'}-${palette || 'native'}.png`,
        clip: {x: 0, y: 844 - 90, width: 390, height: 90}});
    }
  });

  test('the title keeps its pulse artwork beside it exactly where the rendered title, a compact gap and the artwork fit, never at the cost of the title or the row', async ({page}) => {
    await open(page, {width: 779, height: 760});
    // Where the artwork is drawn (the clipped one-line title does not scroll),
    // and whether it fits by the rendered title's own width.
    const fit = () => page.locator('#page-title').evaluate(heading => {
      const after = getComputedStyle(heading, '::after');
      const shown = heading.dataset.short ? getComputedStyle(heading, '::before') : getComputedStyle(heading);
      const probe = document.createElement('span');
      probe.style.cssText = `position:absolute;visibility:hidden;white-space:nowrap;font:${shown.font};letter-spacing:${shown.letterSpacing}`;
      probe.textContent = heading.dataset.short || heading.textContent;
      document.body.append(probe);
      const text = probe.getBoundingClientRect().width;
      probe.remove();
      const box = heading.getBoundingClientRect();
      return {drawn: heading.scrollHeight <= heading.clientHeight + 0.5,
        fits: text + parseFloat(after.marginLeft) + parseFloat(after.width) <= box.width + 0.5,
        whole: heading.scrollWidth <= heading.clientWidth + 0.5,
        proportion: Math.round(parseFloat(after.width) / parseFloat(after.height) * 100) / 100,
        size: Math.round(parseFloat(after.height) / parseFloat(shown.fontSize) * 100) / 100,
        gap: Math.round(parseFloat(after.marginLeft) * 10) / 10};
    });
    const seen = {};
    for (const [entry, view] of [['Downloads', 'torrents'], ['Dashboard', 'dashboard'], ['Settings', 'settings'], ['Stats', 'stats']]) {
      await page.setViewportSize({width: 779, height: 760});
      await page.locator('#shell-dock .dp-shell-dock-item', {hasText: entry}).click();
      await still(page);
      for (const width of [320, 375, 390, 430, 600, 779]) {
        await page.setViewportSize({width, height: 760});
        const facts = await fit();
        expect(facts.drawn, `${view} at ${width}px: drawn exactly where it fits`).toBe(facts.fits);
        expect(facts.whole, `${view} at ${width}px: the title whole`).toBe(true);
        expect([facts.proportion, facts.size], `${view}: the desktop proportion`).toEqual([5.53, 0.83]);
        expect(facts.gap).toBeLessThanOrEqual(3);
        expect(await headerRow(page), `${view} at ${width}px`).toEqual({inside: true, oneLine: true, height: 61});
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
        seen[`${view}@${width}`] = facts.drawn;
      }
    }
    // Every title keeps it at 779px; the longest give it up first.
    expect(['torrents', 'dashboard', 'settings', 'stats'].every(view => seen[`${view}@779`])).toBe(true);
    expect(seen['torrents@320']).toBe(false);
    expect(Object.keys(seen).filter(key => seen[key]).length).toBeGreaterThan(Object.keys(seen).filter(key => key.startsWith('stats') && seen[key]).length);
  });

  test('the one version datum stays below the active master surface, inside the workspace and clear of the dock, at every handheld width and at 780px', async ({page}) => {
    await open(page, {width: 320, height: 640});
    await page.waitForFunction(() => document.getElementById('sidebar-version').textContent !== 'v…');
    for (const width of [320, 375, 390, 430, 600, 779, 780]) {
      await page.setViewportSize({width, height: 640});
      for (const label of width < 780 ? ['Dashboard', 'Help'] : []) {
        const button = page.locator('#shell-dock .dp-shell-dock-item', {hasText: label});
        if (await button.count()) await button.click();
        else {
          await page.locator('#shell-dock .dp-shell-dock-more').click();
          await page.locator('#shell-more .dp-shell-sheet-item', {hasText: label}).click();
        }
        await still(page);
        expect(await versionAtEnd(page), `${width}px ${label}`).toEqual({labels: 1, parent: 'content', text: expect.stringMatching(/^v\d/),
          belowSurface: true, rightAligned: true, insideWorkspace: true, clearOfDock: true, atEnd: true});
      }
      if (width === 780) {
        expect(await versionAtEnd(page), '780px').toEqual({labels: 1, parent: 'content', text: expect.stringMatching(/^v\d/),
          belowSurface: true, rightAligned: true, insideWorkspace: true, clearOfDock: true, atEnd: true});
      }
      await page.screenshot({path: `test-results/checkpoint-mobile-version-${width}.png`});
    }
  });

  test('crossing 779/780px: one navigation at a time, and an open sheet closes rather than outliving its shell', async ({page}) => {
    await open(page, {width: 779, height: 800});
    const shown = () => page.evaluate(() => ({dock: !!document.getElementById('shell-dock').getClientRects().length,
      sidebar: !!document.getElementById('sidebar').getClientRects().length}));
    expect(await shown()).toEqual({dock: true, sidebar: false});
    for (const opener of [() => page.locator('#shell-dock .dp-shell-dock-more').click(), () => page.locator('#theme-picker-toggle').click(),
      () => page.locator('#runtime-cap-toggle').click()]) {
      await page.setViewportSize({width: 779, height: 800});
      await opener();
      await page.setViewportSize({width: 780, height: 800});
      expect(await shown()).toEqual({dock: false, sidebar: true});
      expect(await page.evaluate(() => ({more: document.getElementById('shell-more').matches(':popover-open'),
        theme: document.getElementById('theme-picker').matches(':popover-open'), cap: !document.getElementById('runtime-cap-menu').hidden})))
        .toEqual({more: false, theme: false, cap: false});
    }
    // The desktop shell at 780px: rail, topbar controls and Pause All, no handheld control.
    await expect(page.locator('#sidebar')).toHaveCSS('width', '72px');
    await expect(page.locator('#topbar-actions')).toBeVisible();
    await expect(page.locator('#shell-dock')).toBeHidden();
    await page.setViewportSize({width: 779, height: 800});
    expect(await shown()).toEqual({dock: true, sidebar: false});
  });

  test('a short window and a narrowed one (the keyboard): header and dock stay put, the page keeps its room, the cap sheet stays reachable', async ({page}) => {
    await open(page, {width: 320, height: 480});
    const layout = () => page.evaluate(() => {
      const box = id => document.getElementById(id).getBoundingClientRect();
      return {header: box('topbar').top, dock: box('shell-dock').bottom, room: box('content').height, height: innerHeight};
    });
    let facts = await layout();
    expect(facts.header).toBe(0);
    expect(facts.dock).toBe(480);
    expect(facts.room).toBeGreaterThan(200);
    await page.locator('#runtime-cap-toggle').click();
    await page.locator('#runtime-cap-custom-mbps').focus();
    // The keyboard opening (interactive-widget=resizes-content) narrows the layout viewport.
    await page.setViewportSize({width: 320, height: 300});
    facts = await layout();
    expect(facts.dock).toBe(300);
    // The sheet stays inside the window and scrolls on its own: the field and
    // its action are brought into view there, never stranded below it.
    const sheet = await page.locator('#runtime-cap-menu').boundingBox();
    const bars = await page.evaluate(() => [document.getElementById('topbar').getBoundingClientRect().bottom,
      document.getElementById('shell-dock').getBoundingClientRect().top]);
    expect([sheet.y, sheet.y + sheet.height]).toEqual(bars);
    for (const id of ['#runtime-cap-custom-mbps', '#runtime-cap-menu .runtime-cap-custom .btn']) {
      await page.locator(id).scrollIntoViewIfNeeded();
      const box = await page.locator(id).boundingBox();
      expect(box.y, id).toBeGreaterThanOrEqual(bars[0] - 0.5);
      expect(box.y + box.height, id).toBeLessThanOrEqual(bars[1] + 0.5);
    }
    expect((await layout()).header).toBe(0);
    await page.keyboard.press('Escape');
    expect(await page.evaluate(() => document.querySelector('meta[name="viewport"]').content)).toContain('interactive-widget=resizes-content');
  });
});

/* ── Roles: the dock and the header as each account's authority composes them ─ */
async function signIn(browser, {username, password}, viewport = {width: 390, height: 844}) {
  const shared = username === ADMIN.username;
  const reuse = shared && adminSession.get() !== null;
  const context = await browser.newContext({baseURL: AUTH, viewport, ...(reuse ? {storageState: adminSession.get()} : {})});
  await context.addInitScript(() => document.addEventListener('debridpulse:pulse-connected', () => { window.__dpPulseConnected = true; }));
  const page = await context.newPage();
  await noFonts(page);
  if (reuse) {
    await page.goto('/');
  } else {
    await page.goto('/login');
    await page.locator('#username').fill(username);
    await page.locator('#password').fill(password);
    await page.locator('button[type="submit"]').click();
  }
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page.locator('#topbar-account-button')).toBeVisible();
  if (shared && !reuse) adminSession.remember(await context.storageState());
  return {context, page};
}

const api = (page, method, path, body) => page.evaluate(async ([method, path, body]) => {
  const response = await window.debridPulseAuth.fetch(`/api${path}`, {
    method, headers: {'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : undefined});
  return {status: response.status, body: await response.json().catch(() => null)};
}, [method, path, body]);

async function person(admin, name, role) {
  const account = {username: `ms-${name}-${run}`, password: `ms-${name}-${run}-pass`};
  const created = await api(admin.page, 'POST', '/accounts', {kind: 'local', role, display_name: `${name} ${run}`, ...account});
  expect(created.status).toBe(200);
  return {...account, id: created.body.id, display: `${name} ${run}`};
}

async function accountSheet(page) {
  await page.locator('#topbar-account-button').click();
  expect(await sheetOpen(page, 'topbar-account-sheet')).toBe(true);
  return page.locator('#topbar-account-sheet').evaluate(sheet => ({
    name: sheet.querySelector('.dp-topbar-account-name').textContent, role: sheet.querySelector('.dp-topbar-account-role').textContent,
    actions: [...sheet.querySelectorAll('.dp-shell-sheet-item')].map(item => item.textContent.trim())}));
}

test('roles: Admin, User and ReadOnly each get exactly their authorized dock and header, and keep My Account and Log Out', async ({browser}) => {
  expect(AUTH, 'DP_AUTH_BASE_URL must be provided by browser CI').toBeTruthy();
  const admin = await signIn(browser, ADMIN);
  expect((await dock(admin.page)).map(item => item.label)).toEqual(['Downloads', 'Stats', 'Dashboard', 'Settings', 'More']);
  await expect(admin.page.locator('#runtime-cap-toggle')).toBeVisible();
  for (const width of [320, 390]) {
    await admin.page.setViewportSize({width, height: 844});
    expect(await headerRow(admin.page), `admin ${width}px`).toEqual({inside: true, oneLine: true, height: 61});
    expect((await expectHeaderTargets(admin.page, width)).map(hit => hit.id)).toEqual(['#topbar-account-button', '#theme-toggle', '#theme-picker-toggle', '#runtime-cap-toggle']);
    expect(await admin.page.locator('#page-title').evaluate(node => node.scrollWidth <= node.clientWidth + 0.5)).toBe(true);
  }
  const own = await accountSheet(admin.page);
  expect(own.actions).toEqual(['My Account', 'Log Out']);
  expect(own.role).toBe('Admin');
  await admin.page.locator('#topbar-account-sheet .dp-shell-sheet-item', {hasText: 'My Account'}).click();
  await expect(admin.page.locator('.dp-account-dialog')).toBeVisible();
  await admin.page.keyboard.press('Escape');
  await expect(admin.page.locator('.dp-account-dialog')).toHaveCount(0);

  const user = await person(admin, 'user', 'user');
  const reader = await person(admin, 'reader', 'readonly');

  // ── User: five direct destinations, Notifications in the Settings place; no More, no cap.
  const signed = await signIn(browser, user);
  let items = await dock(signed.page);
  expect(items.map(item => [item.label, item.name])).toEqual([['Downloads', null], ['Stats', 'Statistics'], ['Dashboard', null],
    ['Notifs', 'Notifications'], ['Help', 'Help & License']]);
  expect(items[2].box.x + items[2].box.width / 2).toBeCloseTo(195, 0);
  for (const id of ['#runtime-cap-toggle', '#runtime-badge-active', '#runtime-badge-max', '#topbar-actions', '#shell-dock .dp-shell-dock-more', '#view-events']) {
    await expect(signed.page.locator(id), id).toHaveCount(0);
  }
  await expect(signed.page.locator('#runtime-badge-speed')).toBeVisible();
  await signed.page.locator('#shell-dock .dp-shell-dock-item', {hasText: 'Notifs'}).click();
  await still(signed.page);
  expect(await title(signed.page)).toBe('Notifications');
  await expect(signed.page.locator('#view-settings.active')).toHaveAttribute('data-settings-context', 'self');
  expect((await dock(signed.page)).filter(item => item.current === 'page').map(item => item.name)).toEqual(['Notifications']);
  // At 320px, with the account control: every header target whole, the title whole.
  await signed.page.setViewportSize({width: 320, height: 760});
  expect((await expectHeaderTargets(signed.page, 320)).map(hit => hit.id)).toEqual(['#topbar-account-button', '#theme-toggle', '#theme-picker-toggle']);
  expect(await signed.page.locator('#page-title').evaluate(title => title.scrollWidth <= title.clientWidth)).toBe(true);
  expect((await dock(signed.page)).find(item => item.name === 'Notifications').label).toBe('Notifs');
  expect(await headerRow(signed.page)).toEqual({inside: true, oneLine: true, height: 61});
  for (const width of [390]) {
    await signed.page.setViewportSize({width, height: 760});
    expect(await headerRow(signed.page)).toEqual({inside: true, oneLine: true, height: 61});
    await expectHeaderTargets(signed.page, width);
  }
  // The account menu rolls down from the header, never over either bar.
  await sample(signed.page, 'account');
  await signed.page.locator('#topbar-account-button').click();
  expectBounded(await frames(signed.page), 'account opening', {from: -1});
  await signed.page.keyboard.press('Escape');
  await expect(signed.page.locator('#topbar-account-button')).toBeFocused();
  await signed.page.setViewportSize({width: 320, height: 760});
  await signed.page.setViewportSize({width: 390, height: 844});
  const userSheet = await accountSheet(signed.page);
  expect(userSheet).toEqual({name: user.display, role: 'User', actions: ['My Account', 'Log Out']});
  await signed.page.keyboard.press('Escape');
  await expect(signed.page.locator('#topbar-account-button')).toBeFocused();

  // The account's own theme: chosen from the header, kept by the account, each mode apart.
  await signed.page.locator('#theme-picker-toggle').click();
  await signed.page.locator('#theme-picker [data-palette="dracula"]').click();
  await expect.poll(async () => (await api(signed.page, 'GET', '/account/appearance')).body?.dark_palette).toBe('dracula');
  await signed.page.reload();
  await expect(signed.page.locator('body')).toHaveAttribute('data-dp-palette', 'dracula');
  expect((await api(signed.page, 'GET', '/account/appearance')).body.light_palette).toBe('debridpulse-light');

  // ── ReadOnly: four destinations, Dashboard still the middle of five, the fifth inert and empty.
  const readonly = await signIn(browser, reader);
  items = await dock(readonly.page);
  expect(items.map(item => [item.tag, item.label])).toEqual([['button', 'Downloads'], ['button', 'Stats'], ['button', 'Dashboard'],
    ['button', 'Help'], ['span', '']]);
  expect(items[2].box.x + items[2].box.width / 2).toBeCloseTo(195, 0);
  expect(await readonly.page.locator('#shell-dock .dp-shell-dock-free').evaluate(free => [free.getAttribute('aria-hidden'),
    getComputedStyle(free).pointerEvents, free.children.length, free.textContent])).toEqual(['true', 'none', 0, '']);
  for (const id of ['#runtime-cap-toggle', '#runtime-badge-active', '#view-settings', '#view-events', '#shell-more .dp-shell-sheet-item']) {
    await expect(readonly.page.locator(id), id).toHaveCount(0);
  }
  await readonly.page.setViewportSize({width: 320, height: 760});
  expect(await headerRow(readonly.page)).toEqual({inside: true, oneLine: true, height: 61});
  expect((await expectHeaderTargets(readonly.page, 320)).map(hit => hit.id)).toEqual(['#topbar-account-button', '#theme-toggle', '#theme-picker-toggle']);
  await expect(readonly.page.locator('#runtime-badge-speed')).toBeVisible();
  await readonly.page.setViewportSize({width: 390, height: 844});
  await readonly.page.locator('#shell-dock .dp-shell-dock-item', {hasText: 'Help'}).click();
  await still(readonly.page);
  expect(await title(readonly.page)).toBe('Help & License');
  expect((await accountSheet(readonly.page)).role).toBe('ReadOnly User');
  await readonly.page.screenshot({path: 'test-results/checkpoint-mobile-readonly-account.png'});
  await readonly.page.keyboard.press('Escape');

  // ── A changed authority recomposes the shell: the User demoted to ReadOnly loses Notifications.
  await expect.poll(() => signed.page.evaluate(() => window.__dpPulseConnected === true)).toBe(true);
  const reloaded = signed.page.waitForEvent('load');
  expect((await api(admin.page, 'PATCH', `/accounts/${user.id}`, {role: 'readonly'})).status).toBe(200);
  await reloaded;
  await signed.page.waitForFunction(() => document.querySelector('#shell-dock > *'));
  expect((await dock(signed.page)).map(item => item.label)).toEqual(['Downloads', 'Stats', 'Dashboard', 'Help', '']);

  // ── Log Out from the header's account sheet.
  await accountSheet(signed.page);
  await signed.page.locator('#topbar-account-sheet .dp-shell-sheet-item', {hasText: 'Log Out'}).click();
  await expect(signed.page).toHaveURL(/\/login/);

  for (const account of [user, reader]) await api(admin.page, 'DELETE', `/accounts/${account.id}`);
  for (const session of [admin, signed, readonly]) await session.context.close();
});
