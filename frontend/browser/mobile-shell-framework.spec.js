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
    // Every header control is a whole 44 x 44 px target at every handheld width.
    for (const width of [320, 390, 600, 779]) {
      await page.setViewportSize({width, height: 760});
      expect((await expectHeaderTargets(page, width)).map(hit => hit.id)).toEqual(['#theme-toggle', '#theme-picker-toggle', '#runtime-cap-toggle']);
    }
    await page.setViewportSize({width: 320, height: 760});
    // Every page title, in full in the accessible tree.
    for (const label of ['Downloads', 'Stats', 'Settings', 'Dashboard']) {
      await page.locator('#shell-dock .dp-shell-dock-item', {hasText: label}).click();
      await still(page);
      const heading = await title(page);
      expect(await page.getByRole('heading', {level: 1}).textContent()).toBe(heading);
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
    expect(box.y).toBeGreaterThanOrEqual(0);
    expect(box.y + box.height).toBeLessThanOrEqual(640.5);
    expect([box.x, box.width]).toEqual([0, 320]);
    // Above the dock: the topmost element over a dock button is the sheet's scrim.
    expect(await page.evaluate(() => document.elementFromPoint(160, 630)?.id)).toBe('runtime-cap-menu');
    await expect(page.locator('#runtime-cap-menu [data-cap-bps].active')).toBeFocused();
    // A press beside the sheet closes it and reaches nothing beneath (here the page).
    await recordClicks(page);
    await page.mouse.click(96, Math.max(4, box.y - 30));
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
      const sheet = await page.locator('#theme-picker .dp-theme-picker-head').boundingBox();
      const list = await page.locator('#theme-picker-list').boundingBox();
      expect(sheet.y).toBeGreaterThan(0);
      expect(list.y + list.height).toBeLessThanOrEqual(844.5);
      await expect(page.locator('#theme-picker [aria-checked="true"]')).toBeFocused();
      // The scrim: a tap above the sheet closes it and reaches nothing beneath (the toggle itself).
      await page.touchscreen.tap(at.x, at.y);
      expect(await sheetOpen(page, 'theme-picker')).toBe(false);
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
    expect(sheet.y).toBeGreaterThanOrEqual(0);
    expect(sheet.y + sheet.height).toBeLessThanOrEqual(300.5);
    for (const id of ['#runtime-cap-custom-mbps', '#runtime-cap-menu .runtime-cap-custom .btn']) {
      await page.locator(id).scrollIntoViewIfNeeded();
      const box = await page.locator(id).boundingBox();
      expect(box.y, id).toBeGreaterThanOrEqual(0);
      expect(box.y + box.height, id).toBeLessThanOrEqual(300.5);
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
