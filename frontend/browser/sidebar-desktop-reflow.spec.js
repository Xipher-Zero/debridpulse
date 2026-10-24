const { test, expect } = require('@playwright/test');

/* DP 1.0.14 universal desktop sidebar reflow.
 *
 * One width decides the desktop shell for every page: from 1560 CSS px the
 * full 240px sidebar, from 901 to 1559px the compact 72px icon rail (the same
 * sidebar, its labels faded in place). The rail keeps its section headings,
 * its icons, the brand glyph, Provider Status as indicators and the session
 * controls on one icon rail; DP's own tooltips name what is no longer written.
 * Its mid-height handle shows the same sidebar at full width above the page
 * for the moment without moving the page. Settings and Help keep their title
 * and complete tab strip on one header row through the desktop; the Dashboard
 * keeps its six cards on one row down to 1390px. */

const EXPANDED = 1560;
const COMPACT = 1559;
const RAIL = 72;
const FULL = 240;

async function open(page, width, {height = 1000} = {}) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.setViewportSize({width, height});
  await page.goto('/');
  await expect(page.locator('#sidebar .nav-item[data-view="dashboard"]')).toBeVisible();
}

// The sidebar's width transition has finished (the real CSS transition, not a sleep).
const settledShell = page => page.waitForFunction(() =>
  document.getElementById('sidebar').getAnimations().every(animation => animation.playState !== 'running'));

async function resize(page, width, height = 1000) {
  await page.setViewportSize({width, height});
  await settledShell(page);
}

const box = (page, selector) => page.locator(selector).first().evaluate(node => {
  const r = node.getBoundingClientRect();
  return {left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height};
});

/* What the desktop rail must keep, as a list of what is wrong with it (empty
 * when it is right): its width, the main region beside it, the three section
 * headings visible and centred on the icon rail inside the sidebar, every
 * icon on that rail, the brand glyph on it, and Provider Status shown. */
function railProblems(page) {
  return page.evaluate(({RAIL}) => {
    const problems = [];
    const rect = node => node.getBoundingClientRect();
    const sidebar = rect(document.getElementById('sidebar'));
    const main = rect(document.getElementById('main'));
    if (Math.abs(sidebar.width - RAIL) > 0.5) problems.push(`sidebar ${sidebar.width}`);
    if (Math.abs(main.left - RAIL) > 0.5 || Math.abs(main.width - (innerWidth - RAIL)) > 0.5) problems.push('main');
    const icons = [...document.querySelectorAll('#sidebar nav .nav-item .icon svg')].map(rect);
    const rail = icons[0].left + icons[0].width / 2;
    if (Math.abs(rail - RAIL / 2) > 0.5) problems.push(`rail ${rail}`);
    icons.forEach((icon, i) => { if (Math.abs(icon.left + icon.width / 2 - rail) > 0.5) problems.push(`icon ${i}`); });
    const glyph = rect(document.querySelector('#sidebar .logo-icon'));
    if (Math.abs(glyph.left + glyph.width / 2 - rail) > 0.5) problems.push('glyph');
    const headings = [...document.querySelectorAll('#sidebar nav .nav-group')];
    if (headings.map(h => h.textContent.trim().toUpperCase()).join() !== 'QUEUE,REVIEW,SYSTEM') problems.push('headings');
    headings.forEach(heading => {
      const r = rect(heading);
      const style = getComputedStyle(heading);
      if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) < 0.99 || !r.width) {
        problems.push(`heading ${heading.textContent} hidden`);
        return;
      }
      if (r.left < 0 || r.right > sidebar.right) problems.push(`heading ${heading.textContent} clipped`);
      // The letters (the start padding equals the trailing tracking) centre on the rail.
      const padding = parseFloat(style.paddingLeft);
      if (Math.abs(r.left + padding + (r.width - padding) / 2 - rail) > 1.5) problems.push(`heading ${heading.textContent} off rail`);
    });
    const footer = document.querySelector('#sidebar .sidebar-footer');
    if (footer) {
      const f = rect(footer);
      if (getComputedStyle(footer).display === 'none' || f.left < sidebar.left || f.right > sidebar.right + 0.5) problems.push('provider status');
      if (!footer.querySelectorAll('.dot').length) problems.push('provider indicators');
    }
    return problems;
  }, {RAIL});
}

test('one universal width: the full sidebar from 1560px, the compact rail below it, and back', async ({page}) => {
  await open(page, 1600);
  for (const width of [1600, EXPANDED]) {
    await resize(page, width);
    expect((await box(page, '#sidebar')).width).toBe(FULL);
    expect(await box(page, '#main')).toMatchObject({left: FULL, width: width - FULL});
    await expect(page.locator('#sidebar-handle')).toBeHidden();
    await expect(page.locator('#sidebar .nav-item[data-view="dashboard"] .nav-label')).toHaveCSS('opacity', '1');
  }
  for (const width of [COMPACT, 1440, 1000]) {
    await resize(page, width);
    expect(await railProblems(page)).toEqual([]);
    await expect(page.locator('#sidebar-handle')).toBeVisible();
    // The labels are faded in place: still each entry's accessible name.
    await expect(page.locator('#sidebar .nav-item[data-view="dashboard"] .nav-label')).toHaveCSS('opacity', '0');
    await expect(page.locator('#sidebar .nav-item[data-view="torrents"]')).toHaveAccessibleName('Downloads');
    await expect(page.locator('#sidebar .nav-item[data-view="help"]')).toHaveAccessibleName('Help & License');
  }
  // The rail gives the page back what the sidebar gave up.
  await resize(page, EXPANDED);
  const expandedMain = (await box(page, '#main')).width;
  await resize(page, COMPACT);
  expect((await box(page, '#main')).width - expandedMain).toBe(FULL - RAIL - 1);
  await resize(page, 1600);
  expect((await box(page, '#sidebar')).width).toBe(FULL);
  await expect(page.locator('#sidebar')).not.toHaveClass(/sidebar-overlay-open/);
});

test('negative control: the rail check fails when a section heading or Provider Status goes missing', async ({page}) => {
  await open(page, 1440);
  expect(await railProblems(page)).toEqual([]);
  const review = page.locator('#sidebar nav .nav-group', {hasText: 'Review'});
  await review.evaluate(heading => { heading.style.display = 'none'; });
  expect(await railProblems(page)).toEqual(['heading Review hidden']);
  await review.evaluate(heading => { heading.style.display = ''; });
  await page.addStyleTag({content: '#sidebar .sidebar-footer { display: none !important; }'});
  expect(await railProblems(page)).toEqual(['provider status']);
});

/* Every animation frame from now until `act` has finished and the sidebar's
 * change is over (its width settled and its reflow mark gone): the window
 * width, the sidebar's drawn width and mark, how far its edge passes over the
 * page and over the page's content (the page's clear inset is #content's
 * leading padding), and -- on Settings or Help -- whether the tab strip is on
 * a second row, the header's own genuine-fit rule, and whether every tab is
 * inside the header. */
async function framesDuring(page, view, act) {
  await page.evaluate(view => {
    const rect = node => node.getBoundingClientRect();
    window.__dpFrames = [];
    window.__dpStop = false;
    const sample = () => {
      const sidebarNode = document.getElementById('sidebar');
      const sidebar = rect(sidebarNode);
      const main = rect(document.getElementById('main'));
      const content = document.getElementById('content');
      const inset = parseFloat(getComputedStyle(content).paddingLeft);
      const frame = {vw: innerWidth, sidebar: sidebar.width, mainLeft: main.left, mainWidth: main.width,
        overlap: sidebar.right - main.left, contentCovered: sidebar.right - (rect(content).left + inset),
        mark: sidebarNode.classList.contains('sidebar-reflowing') ? sidebarNode.dataset.sidebarReflow || 'unnamed' : '',
        labelOpacity: Number(getComputedStyle(sidebarNode.querySelector('.nav-label')).opacity)};
      if (view) {
        const root = document.getElementById(`view-${view}`);
        const header = root.querySelector(`.dp-${view}-master-header`);
        const style = getComputedStyle(header);
        const box = rect(header);
        const inner = {left: box.left + parseFloat(style.paddingLeft), right: box.right - parseFloat(style.paddingRight)};
        const copyNode = root.querySelector(`.dp-${view}-header-copy`);
        const copy = rect(copyNode);
        const strip = root.querySelector(`.dp-${view}-tabs`);
        const s = rect(strip);
        const tabs = [...strip.querySelectorAll('[data-tab]')].map(rect);
        const stripStyle = getComputedStyle(strip);
        const natural = [...copyNode.children].reduce((sum, child) => sum + rect(child).width, 0)
          + parseFloat(getComputedStyle(copyNode).columnGap) * (copyNode.children.length - 1);
        const stripNatural = tabs.reduce((sum, tab) => sum + tab.width, 0) + parseFloat(stripStyle.columnGap || 0) * (tabs.length - 1)
          + ['paddingLeft', 'paddingRight', 'borderLeftWidth', 'borderRightWidth'].reduce((sum, key) => sum + parseFloat(stripStyle[key]), 0);
        Object.assign(frame, {
          second: s.top > copy.bottom - 4,
          fits: natural + parseFloat(style.columnGap) + stripNatural <= inner.right - inner.left + 0.5,
          inside: s.left >= inner.left - 0.5 && s.right <= inner.right + 0.5
            && tabs.every(tab => tab.left >= s.left - 0.5 && tab.right <= s.right + 0.5 && Math.abs(tab.top - tabs[0].top) < 1),
          copyCovered: sidebar.right - copy.left,
        });
      }
      window.__dpFrames.push(frame);
      if (!window.__dpStop) requestAnimationFrame(sample);
    };
    requestAnimationFrame(sample);
  }, view);
  await act();
  await page.waitForFunction(() => {
    const sidebar = document.getElementById('sidebar');
    return !sidebar.classList.contains('sidebar-reflowing') && sidebar.dataset.sidebarReflow === undefined
      && sidebar.getAnimations({subtree: true}).every(animation => animation.playState !== 'running' || animation.effect.getTiming().iterations === Infinity);
  });
  return page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(() => {
    window.__dpStop = true;
    resolve(window.__dpFrames);
  }))));
}

// The header's tab strip is on a second row (Settings or Help), at rest.
const restRow = (page, view) => page.evaluate(view => {
  const root = document.getElementById(`view-${view}`);
  return root.querySelector(`.dp-${view}-tabs`).getBoundingClientRect().top
    > root.querySelector(`.dp-${view}-header-copy`).getBoundingClientRect().bottom - 4;
}, view);

const changes = values => values.reduce((count, value, i) => count + (i && value !== values[i - 1] ? 1 : 0), 0);

// The window moving a frame's width at a time, as a dragged window edge does.
async function drag(page, from, to, step) {
  const sign = Math.sign(to - from);
  for (let width = from; sign * (to - width) >= 0; width += sign * step) {
    await page.setViewportSize({width, height: 1000});
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)));
  }
}

test('a window dragged across 1560px animates the persistent change, the page keeping its room, and reduced motion makes it immediate', async ({page}) => {
  await open(page, 1600);
  const transition = await page.evaluate(() => getComputedStyle(document.getElementById('sidebar')).transitionProperty);
  expect(transition).toContain('width');
  for (const [from, to] of [[1600, 1500], [1500, 1600]]) {
    const frames = await framesDuring(page, null, () => drag(page, from, to, 8));
    // Animated: the sidebar passes through the widths between, and the change
    // is the persistent one throughout (never taken at once).
    expect(frames.some(frame => frame.sidebar > RAIL + 1 && frame.sidebar < FULL - 1)).toBe(true);
    expect(frames.filter(frame => frame.mark === 'immediate')).toEqual([]);
    for (const frame of frames) {
      // The page never has less room than at the narrowest resting width the
      // window passes through (1560px with the full sidebar) ...
      expect(frame.mainWidth).toBeGreaterThanOrEqual(Math.min(frame.vw - (frame.vw >= EXPANDED ? FULL : RAIL), EXPANDED - FULL) - 0.5);
      // ... and the moving edge passes over the page by no more than the
      // window has lost below 1560px (how much of the page that reaches
      // depends on the drag's speed: the Gate 9 packet measures it).
      expect(frame.overlap, JSON.stringify(frame)).toBeLessThanOrEqual(Math.max(0, EXPANDED - frame.vw) + 0.5);
      expect(frame.mainLeft).toBeGreaterThanOrEqual(RAIL - 0.5);
    }
    expect(frames.at(-1).sidebar).toBe(to >= EXPANDED ? FULL : RAIL);
  }
  expect((await box(page, '#main')).left).toBe(FULL);

  // Reduced motion: every change is immediate, and nothing is marked or left behind.
  await page.emulateMedia({reducedMotion: 'reduce'});
  for (const act of [() => page.setViewportSize({width: 1440, height: 1000}), () => drag(page, 1440, 1600, 16), () => page.setViewportSize({width: 1440, height: 1000})]) {
    const frames = await framesDuring(page, null, act);
    expect(frames.filter(frame => frame.sidebar !== (frame.vw >= EXPANDED ? FULL : RAIL) || frame.mark)).toEqual([]);
    expect(await page.evaluate(() => document.getElementById('sidebar').getAnimations().length)).toBe(0);
  }
});

/* A window that changes by more than the page's clear inset at once (a snap,
 * maximise or restore) takes the new sidebar width in that same frame, either
 * way: every frame is the page's resting layout at that frame's window width
 * -- the header's row included -- and the sidebar never passes over the page. */
for (const view of ['settings', 'help']) {
  test(`abrupt window changes take the ${view} page straight to its resting layout, frame by frame`, async ({page}) => {
    await open(page, 1600);
    await page.locator(`#sidebar .nav-item[data-view="${view}"]:not([data-title])`).click();
    await expect(page.locator(`#view-${view} .dp-${view}-tabs [data-tab]`).first()).toBeVisible();
    const cases = [[1600, 1559], [1700, 1540], [1600, 1500], [1600, 1440], [1920, 1400]];
    const rest = {};
    for (const width of new Set(cases.flat())) {
      await resize(page, width);
      rest[width] = await restRow(page, view);
    }
    for (const [from, to] of [...cases, ...cases.map(([a, b]) => [b, a])]) {
      await resize(page, from);
      const frames = await framesDuring(page, view, () => page.setViewportSize({width: to, height: 1000}));
      const label = `${view} ${from}->${to}`;
      const after = frames.filter(frame => frame.vw === to);
      expect(after.length, label).toBeGreaterThan(3);
      for (const frame of frames) {
        expect(frame.sidebar, label).toBe(frame.vw >= EXPANDED ? FULL : RAIL);
        expect(frame.overlap, label).toBeLessThanOrEqual(0.5);
        expect(frame.copyCovered, label).toBeLessThanOrEqual(0);
        expect(frame.inside, label).toBe(true);
        expect(frame.second, `${label} at ${frame.vw}`).toBe(rest[frame.vw]);
      }
      expect(after.some(frame => frame.mark === 'immediate'), label).toBe(true);
      expect(changes([rest[from], ...frames.map(frame => frame.second), rest[to]]), label).toBe(rest[from] === rest[to] ? 0 : 1);
      expect(await restRow(page, view), label).toBe(rest[to]);
    }
    expect((await box(page, '#main')).left).toBe(FULL);   // 1920: the full sidebar
  });
}

/* A further abrupt change while the sidebar is still moving -- narrowing on,
 * widening back, or narrowing again during an expansion -- takes the new
 * width in that frame too, and the change's mark is gone once it is over. */
test('an abrupt change while the sidebar is moving settles at once in either direction and leaves nothing behind', async ({page}) => {
  await open(page, 1600);
  await page.locator('#sidebar .nav-item[data-view="settings"]:not([data-title])').click();
  await expect(page.locator('#view-settings .dp-settings-tabs [data-tab]').first()).toBeVisible();
  for (const [start, cross, then] of [[1568, 1552, 1400], [1568, 1552, 1600], [1552, 1568, 1920], [1552, 1568, 1440]]) {
    await resize(page, start);
    const label = `${start}->${cross}->${then}`;
    const frames = await framesDuring(page, 'settings', async () => {
      await page.setViewportSize({width: cross, height: 1000});
      // The sidebar is moving: part-way between its two widths.
      await page.waitForFunction(() => document.getElementById('sidebar').getAnimations()
        .some(animation => animation.transitionProperty === 'width' && animation.playState === 'running' && animation.currentTime > 100));
      await page.setViewportSize({width: then, height: 1000});
    });
    const moving = frames.filter(frame => frame.vw === cross && frame.sidebar > RAIL + 1 && frame.sidebar < FULL - 1);
    expect(moving.length, label).toBeGreaterThan(0);
    expect(moving.every(frame => frame.mark === 'persistent'), label).toBe(true);
    const settled = frames.filter(frame => frame.vw === then);
    expect(settled.length, label).toBeGreaterThan(3);
    for (const frame of settled) expect(frame.sidebar, label).toBe(then >= EXPANDED ? FULL : RAIL);
    for (const frame of frames) {
      expect(frame.contentCovered, label).toBeLessThanOrEqual(0.5);
      expect(frame.inside, label).toBe(true);
    }
    expect(await page.evaluate(() => {
      const sidebar = document.getElementById('sidebar');
      return {mark: sidebar.className.includes('sidebar-reflowing'), data: sidebar.dataset.sidebarReflow ?? null,
        margin: getComputedStyle(sidebar).marginRight, transition: getComputedStyle(sidebar).transitionProperty.includes('width')};
    }), label).toEqual({mark: false, data: null, margin: '0px', transition: true});
  }
});

/* Continuous drags at 8 and 16px a frame, both directions, Settings and
 * Help: the accepted motion. The header follows its own genuine fit in every
 * frame (no premature second row), every tab stays inside it, the page never
 * has less room than at 1560px, and the moving edge passes over the page by
 * no more than the window has lost below 1560px. (How far that reaches into
 * the header depends on the drag's real speed, which a headless browser's
 * frames do not fix; the Gate 9 packet measures it.) */
for (const view of ['settings', 'help']) {
  test(`continuous drags across 1560px keep the ${view} header's row and the accepted motion`, async ({page}) => {
    await open(page, 1640);
    await page.locator(`#sidebar .nav-item[data-view="${view}"]:not([data-title])`).click();
    await expect(page.locator(`#view-${view} .dp-${view}-tabs [data-tab]`).first()).toBeVisible();
    for (const step of [8, 16]) {
      for (const [from, to] of [[1640, 1480], [1480, 1640]]) {
        await resize(page, from);
        const label = `${view} ${from}->${to} at ${step}px a frame`;
        const frames = await framesDuring(page, view, () => drag(page, from, to, step));
        expect(frames.filter(frame => frame.mark === 'immediate'), label).toEqual([]);
        expect(frames.some(frame => frame.sidebar > RAIL + 1 && frame.sidebar < FULL - 1), label).toBe(true);
        for (const frame of frames) {
          expect(frame.second, `${label} at ${frame.vw}`).toBe(!frame.fits);
          expect(frame.inside, label).toBe(true);
          expect(frame.mainWidth, label).toBeGreaterThanOrEqual(EXPANDED - FULL - 0.5);
          expect(frame.overlap, label).toBeLessThanOrEqual(Math.max(0, EXPANDED - frame.vw) + 0.5);
        }
      }
    }
  });
}

/* The overlay is not the persistent change: an abrupt window change while it
 * is open, or the overlay opened right after one, never moves the page, and
 * the overlay keeps its own animation. */
test('the overlay keeps its motion and the page its place around abrupt window changes', async ({page}) => {
  await open(page, 1600);
  await page.setViewportSize({width: 1440, height: 1000});
  // Opened while the abrupt change's mark is still there: animated, the page unmoved.
  const opening = await framesDuring(page, null, () => page.locator('#sidebar-handle').click());
  expect(opening.some(frame => frame.sidebar > RAIL + 1 && frame.sidebar < FULL - 1)).toBe(true);
  for (const frame of opening) expect([frame.mainLeft, frame.mainWidth]).toEqual([RAIL, 1440 - RAIL]);
  const during = await framesDuring(page, null, () => page.setViewportSize({width: 1300, height: 1000}));
  for (const frame of during) {
    expect([frame.sidebar, frame.mainLeft, frame.mainWidth]).toEqual([FULL, RAIL, frame.vw - RAIL]);
    expect(['', 'overlay']).toContain(frame.mark);
  }
  await expect(page.locator('#sidebar')).toHaveClass(/sidebar-overlay-open/);
  const closing = await framesDuring(page, null, () => page.locator('#sidebar-handle').click());
  expect(closing.some(frame => frame.sidebar > RAIL + 1 && frame.sidebar < FULL - 1)).toBe(true);
  for (const frame of closing) expect([frame.mainLeft, frame.mainWidth]).toEqual([RAIL, 1300 - RAIL]);
  expect(await railProblems(page)).toEqual([]);
});

/* The rail's other edge, 900px, is not the persistent change: the tablet
 * sidebar becomes the rail with its accepted motion, dragged or at once, the
 * page beside it in every frame (it only gains room there). */
test('crossing into the rail from the tablet sidebar keeps its motion and never covers the page', async ({page}) => {
  await open(page, 880);
  for (const act of [() => drag(page, 880, 928, 12), () => page.setViewportSize({width: 800, height: 1000}), () => page.setViewportSize({width: 1051, height: 1000})]) {
    const frames = await framesDuring(page, null, act);
    for (const frame of frames) {
      expect(frame.overlap, JSON.stringify(frame)).toBeLessThanOrEqual(0.5);
      expect(['', 'tablet']).toContain(frame.mark);
    }
    if (frames.at(-1).vw > 900) expect(frames.some(frame => frame.sidebar > RAIL + 1 && frame.sidebar < 279)).toBe(true);
  }
  expect(await railProblems(page)).toEqual([]);
});

/* The labels: faded out on the rail and in on the full sidebar, through the
 * accepted fade when dragged and at once on an abrupt change. */
test('the labels fade with a dragged change and follow an abrupt one at once', async ({page}) => {
  await open(page, 1600);
  const dragged = await framesDuring(page, null, () => drag(page, 1576, 1544, 8));
  expect(dragged.some(frame => frame.labelOpacity > 0.01 && frame.labelOpacity < 0.99)).toBe(true);
  expect(dragged.at(-1).labelOpacity).toBe(0);
  const snapped = await framesDuring(page, null, () => page.setViewportSize({width: 1700, height: 1000}));
  for (const frame of snapped.filter(frame => frame.vw === 1700)) expect(frame.labelOpacity).toBe(1);
});

/* Negative control: with the abrupt change animated as before (the immediate
 * mark ignored), the frame check sees the cover and the premature row. */
test('negative control: the abrupt-change check fails when a snap animates the sidebar', async ({page}) => {
  await open(page, 1600);
  await page.locator('#sidebar .nav-item[data-view="settings"]:not([data-title])').click();
  await expect(page.locator('#view-settings .dp-settings-tabs [data-tab]').first()).toBeVisible();
  await page.addStyleTag({content: `@media (min-width: 901px) and (max-width: 1559px) {
    #sidebar.sidebar-settling:not(.sidebar-overlay-open) {
      transition: width 200ms cubic-bezier(.4,0,.2,1) 60ms, min-width 200ms cubic-bezier(.4,0,.2,1) 60ms; }
    #sidebar.sidebar-reflowing[data-sidebar-reflow="immediate"]:not(.sidebar-overlay-open) {
      animation: dp-sidebar-yield 200ms cubic-bezier(.4,0,.2,1) 60ms both; } }`});
  const frames = await framesDuring(page, 'settings', () => page.setViewportSize({width: 1440, height: 1000}));
  expect(frames.some(frame => frame.vw === 1440 && frame.sidebar > RAIL + 1)).toBe(true);
  expect(Math.max(...frames.map(frame => frame.copyCovered))).toBeGreaterThan(0);
});

test('the Dashboard keeps its six cards on one row through the desktop, down to 1390px', async ({page}) => {
  await open(page, 1600);
  const rows = () => page.evaluate(() => new Set([...document.querySelectorAll('#view-dashboard .dash-hero-stat')]
    .map(card => Math.round(card.getBoundingClientRect().top))).size);
  for (const width of [1600, EXPANDED, COMPACT, 1440, 1390]) {
    await resize(page, width);
    await expect(page.locator('#view-dashboard .dash-hero-stat')).toHaveCount(6);
    expect(await rows(), `${width}px`).toBe(1);
  }
  await resize(page, 1389);
  expect(await rows()).toBe(2);
});

test('every page keeps its desktop content on both sides of the boundary', async ({page}) => {
  await open(page, EXPANDED);
  for (const width of [EXPANDED, COMPACT]) {
    await resize(page, width);
    for (const view of ['dashboard', 'torrents', 'events', 'stats', 'settings', 'help']) {
      await page.locator(`#sidebar .nav-item[data-view="${view}"]:not([data-title])`).click();
      await expect(page.locator(`#view-${view}`)).toHaveClass(/\bactive\b/);
      const facts = await page.evaluate(() => ({
        page: document.documentElement.scrollWidth - innerWidth,
        main: document.getElementById('main').scrollWidth - document.getElementById('main').clientWidth,
        controls: [...document.querySelectorAll('#topbar-actions button, #topbar-actions #runtime-speed-badge')]
          .filter(node => node.getClientRects().length).length,
      }));
      expect(facts.page, `${view} at ${width}px`).toBeLessThanOrEqual(0);
      expect(facts.main, `${view} at ${width}px`).toBeLessThanOrEqual(0);
      expect(facts.controls, `${view} at ${width}px`).toBeGreaterThan(0);
    }
  }
});

/* The master header's identity copy and its tab strip. The strip is never
 * stretched, never scrolls and never loses a tab; whenever the copy at its own
 * width, the row gap and the strip at its own width fit the header, they are
 * one row (every tab on one line, the strip level with the copy, after it,
 * inside the header). Only when they genuinely do not fit -- which depends on
 * the fonts the browser has: this suite blocks the web fonts, whose real
 * metrics fit through the whole desktop (the Gate 9 packet measures them) --
 * may the strip sit beneath the copy, still at its own width. Returns
 * {fits, problems}. */
function header(page, view) {
  return page.evaluate(view => {
    const root = document.getElementById(`view-${view}`);
    const headerNode = root.querySelector(`.dp-${view}-master-header`);
    const box = headerNode.getBoundingClientRect();
    const style = getComputedStyle(headerNode);
    const inner = box.width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const copyNode = root.querySelector(`.dp-${view}-header-copy`);
    const copy = copyNode.getBoundingClientRect();
    const natural = [...copyNode.children].reduce((sum, child) => sum + child.getBoundingClientRect().width, 0)
      + parseFloat(getComputedStyle(copyNode).columnGap) * (copyNode.children.length - 1);
    const strip = root.querySelector(`.dp-${view}-tabs`);
    const s = strip.getBoundingClientRect();
    const tabs = [...strip.querySelectorAll('[data-tab]')].map(tab => tab.getBoundingClientRect());
    const own = tabs.reduce((sum, tab) => sum + tab.width, 0);
    const stripStyle = getComputedStyle(strip);
    const stripNatural = own + parseFloat(stripStyle.columnGap || 0) * (tabs.length - 1)
      + ['paddingLeft', 'paddingRight', 'borderLeftWidth', 'borderRightWidth'].reduce((sum, key) => sum + parseFloat(stripStyle[key]), 0);
    const fits = natural + parseFloat(style.columnGap) + stripNatural <= inner + 0.5;
    const problems = [];
    if (new Set(tabs.map(tab => Math.round(tab.top))).size !== 1) problems.push('tabs wrap');
    if (s.right > box.right + 0.5 || tabs.some(tab => tab.left < s.left - 0.5 || tab.right > s.right + 0.5)) problems.push('clipped');
    if (strip.scrollWidth > strip.clientWidth + 1) problems.push('strip scrolls');
    if (s.width > own + 40) problems.push('strip stretched');
    const level = Math.abs((s.top + s.bottom) / 2 - (copy.top + copy.bottom) / 2) <= 12;
    if (fits && !level) problems.push('second row');
    if (fits && s.left < copy.right) problems.push('tabs not beside the copy');
    if (!fits && level) problems.push('overflowing row');
    if (fits && Math.round(box.height) > Math.round(Math.max(copy.height, s.height)
      + parseFloat(style.paddingTop) + parseFloat(style.paddingBottom) + 1)
      && Math.round(box.height) > parseFloat(style.minHeight) + 0.5) problems.push('extra line');
    return {fits, problems};
  }, view);
}

for (const view of ['settings', 'help']) {
  test(`the ${view} master header keeps its title and complete tab strip on one row whenever they fit`, async ({page}) => {
    await open(page, 1920);
    await page.locator(`#sidebar .nav-item[data-view="${view}"]:not([data-title])`).click();
    await expect(page.locator(`#view-${view} .dp-${view}-tabs [data-tab]`).first()).toBeVisible();
    await expect(page.locator(`#view-${view} .dp-${view}-tabs [data-tab]`)).toHaveCount(view === 'settings' ? 7 : 8);
    // Wide: one row, in the accepted centred presentation.
    expect(await header(page, view)).toEqual({fits: true, problems: []});
    const centre = await page.evaluate(view => {
      const header = document.querySelector(`#view-${view} .dp-${view}-master-header`);
      const box = header.getBoundingClientRect();
      const strip = document.querySelector(`#view-${view} .dp-${view}-tabs`).getBoundingClientRect();
      const copy = document.querySelector(`#view-${view} .dp-${view}-header-copy`).getBoundingClientRect();
      // Centred while there is room; when the copy (its width is the font's) is
      // wider than its half, the strip sits right after it, the row gap apart.
      const centred = (box.left + box.right) / 2 - strip.width / 2;
      return strip.left - Math.max(centred, copy.right + parseFloat(getComputedStyle(header).columnGap));
    }, view);
    // (To within half the 16px row gap, which sits on the copy's side.)
    expect(Math.abs(centre)).toBeLessThanOrEqual(8.5);
    // Both sides of the former 1699px stacking rule, the expanded minimum, the
    // rail just below it, and the compact desktop down to 1390px (the former
    // 1180px rule lies below the desktop fit range and no longer exists).
    for (const width of [1700, 1699, 1600, EXPANDED, COMPACT, 1440, 1390]) {
      await resize(page, width);
      expect((await header(page, view)).problems, `${view} at ${width}px`).toEqual([]);
    }
    for (const theme of ['light', 'dark']) {
      await page.evaluate(light => document.body.classList.toggle('light', light), theme === 'light');
      for (const width of [EXPANDED, COMPACT]) {
        await resize(page, width);
        await page.locator(`#view-${view} .dp-${view}-master-header`).screenshot({path: `test-results/checkpoint-${view}-header-${width}-${theme}.png`});
      }
    }
    // A tab still opens its panel through its own owner.
    await resize(page, 1920);
    const second = page.locator(`#view-${view} .dp-${view}-tabs [data-tab]`).nth(1);
    await second.click();
    await expect(second).toHaveAttribute('aria-selected', 'true');
    expect(await header(page, view)).toEqual({fits: true, problems: []});
  });
}

test('negative control: the header check fails when the premature second row returns', async ({page}) => {
  await open(page, 1920);
  await page.locator('#sidebar .nav-item[data-view="settings"]:not([data-title])').click();
  await expect(page.locator('#view-settings .dp-settings-tabs [data-tab]').first()).toBeVisible();
  expect(await header(page, 'settings')).toEqual({fits: true, problems: []});
  // The former <=1699px rule: one column, the strip stretched beneath the copy.
  await page.addStyleTag({content: `#view-settings .dp-settings-master-header { display: grid; grid-template-columns: minmax(0, 1fr); }
    #view-settings .dp-settings-tabs { width: 100%; }`});
  const {problems} = await header(page, 'settings');
  expect(problems).toContain('second row');
  expect(problems).toContain('strip stretched');
});

/* Main region geometry, exactly. */
const mainGeometry = page => page.evaluate(() => ['main', 'content', 'topbar'].map(id => {
  const r = document.getElementById(id).getBoundingClientRect();
  return [r.left, r.top, r.width, r.height];
}));

test('the handle opens the same sidebar above the page without moving the page, and every dismissal closes it', async ({page}) => {
  await open(page, 1440);
  const handle = page.locator('#sidebar-handle');
  const border = await page.locator('#sidebar').evaluate(node => parseFloat(getComputedStyle(node).borderRightWidth));
  const attached = async () => {
    const [h, s] = [await box(page, '#sidebar-handle'), await box(page, '#sidebar')];
    expect(Math.abs(h.right - (s.right - border))).toBeLessThan(0.5);            // on the right edge, against its border
    expect(Math.abs((h.top + h.bottom) / 2 - (s.top + s.bottom) / 2)).toBeLessThan(0.5);   // at mid-height
    return s.width;
  };
  await expect(handle).toHaveAttribute('aria-expanded', 'false');
  await expect(handle).toHaveAccessibleName('Expand sidebar');
  await expect(handle).toHaveAttribute('aria-controls', 'sidebar');
  expect(await attached()).toBe(RAIL);
  const before = await mainGeometry(page);
  const chevron = () => handle.locator('svg').evaluate(svg => getComputedStyle(svg).transform);

  const openIt = async () => {
    await handle.click();
    await expect(handle).toHaveAttribute('aria-expanded', 'true');
    // The handle follows the edge while it moves.
    const moving = await page.evaluate(border => new Promise(resolve => requestAnimationFrame(() => {
      const h = document.getElementById('sidebar-handle').getBoundingClientRect();
      const s = document.getElementById('sidebar').getBoundingClientRect();
      resolve(Math.abs(h.right - (s.right - border)));
    })), border);
    expect(moving).toBeLessThan(0.5);
    await settledShell(page);
    expect(await attached()).toBe(FULL);
    expect(await mainGeometry(page)).toEqual(before);
  };

  const flat = await chevron();
  await openIt();
  expect(await chevron()).not.toBe(flat);
  await expect(handle).toHaveAccessibleName('Collapse sidebar');
  await expect(page.locator('#sidebar .nav-item[data-view="dashboard"] .nav-label')).toHaveCSS('opacity', '1');
  await expect(page.locator('#sidebar .logo-text')).toHaveCSS('opacity', '1');
  await expect(page.locator('#sidebar .dp-provider-status-heading')).toHaveCSS('opacity', '1');
  await expect(page.locator('#sidebar')).toHaveCount(1);
  await expect(page.locator('#provider-status-list')).toHaveCount(1);
  await page.screenshot({path: 'test-results/checkpoint-sidebar-overlay-dark.png'});

  // The handle again: closed, focus stays on it.
  await handle.click();
  await expect(handle).toHaveAttribute('aria-expanded', 'false');
  await expect(handle).toBeFocused();
  await settledShell(page);
  expect(await attached()).toBe(RAIL);
  expect(await chevron()).toBe(flat);
  expect(await mainGeometry(page)).toEqual(before);

  // Escape: closed, focus returned to the handle.
  await openIt();
  await page.locator('#sidebar .nav-item[data-view="stats"]').focus();
  await page.keyboard.press('Escape');
  await expect(handle).toHaveAttribute('aria-expanded', 'false');
  await expect(handle).toBeFocused();
  await settledShell(page);

  // A press outside the sidebar.
  await openIt();
  await page.mouse.click(900, 600);
  await expect(handle).toHaveAttribute('aria-expanded', 'false');
  await settledShell(page);

  // Choosing a page: navigated, and closed.
  await openIt();
  await page.locator('#sidebar .nav-item[data-view="stats"]').click();
  await expect(page.locator('#view-stats')).toHaveClass(/\bactive\b/);
  await expect(handle).toHaveAttribute('aria-expanded', 'false');
  await settledShell(page);
  expect(await attached()).toBe(RAIL);

  // Keyboard: the handle is a button.
  await handle.focus();
  await page.keyboard.press('Enter');
  await expect(handle).toHaveAttribute('aria-expanded', 'true');
  await page.keyboard.press('Space');
  await expect(handle).toHaveAttribute('aria-expanded', 'false');
});

test('leaving the compact range clears the overlay: narrow, wide, narrow leaves one closed sidebar', async ({page}) => {
  await open(page, 1440);
  await page.locator('#sidebar-handle').click();
  await expect(page.locator('#sidebar')).toHaveClass(/sidebar-overlay-open/);
  await resize(page, 1600);
  await expect(page.locator('#sidebar')).not.toHaveClass(/sidebar-overlay-open/);
  await expect(page.locator('#sidebar-handle')).toHaveAttribute('aria-expanded', 'false');
  expect((await box(page, '#main')).left).toBe(FULL);
  await resize(page, 1440);
  await expect(page.locator('#sidebar')).not.toHaveClass(/sidebar-overlay-open|open/);
  expect(await railProblems(page)).toEqual([]);
});

test('negative control: the overlay invariant fails if the overlay takes the page\'s width', async ({page}) => {
  await open(page, 1440);
  const before = await mainGeometry(page);
  await page.addStyleTag({content: '#sidebar.sidebar-overlay-open { margin-right: 0 !important; transition: none !important; }'});
  await page.locator('#sidebar-handle').click();
  await settledShell(page);
  const after = await mainGeometry(page);
  expect(after).not.toEqual(before);
  expect(after[0][2]).toBe(before[0][2] - (FULL - RAIL));
});

test('the rail names what it no longer writes with DP tooltips and stable accessible names, never native titles', async ({page}) => {
  await open(page, 1440);
  await expect(page.locator('#sidebar [title]')).toHaveCount(0);
  const tooltip = page.locator('#dp-tooltip');
  await page.locator('#sidebar .nav-item[data-view="torrents"]').hover();
  await expect(tooltip).toHaveText('Downloads');
  await expect(tooltip).toBeVisible();
  // Keyboard focus shows it at once.
  await page.mouse.move(900, 600);
  await expect(tooltip).toBeHidden();
  await page.locator('#sidebar .nav-item[data-view="dashboard"]').focus();
  await page.keyboard.press('Tab');
  await expect(tooltip).toHaveText('Downloads');
  await page.keyboard.press('Escape');

  // Provider Status as indicators, from real canonical metadata: a premium
  // tier with an unhealthy member, and a general tier.
  await page.evaluate(() => {
    settingsData = {...(settingsData || {}), integrations: {
      alldebrid: {enabled: true, kind: 'provider', configured: true, presentation: {status_name: 'AllDebrid', premium: true,
        static_status: 'healthy', display_order: 10, status_tier: 'premium_service', status_tier_label: 'Premium Services'}},
      torbox: {enabled: true, kind: 'provider', configured: true, presentation: {status_name: 'TorBox', premium: true,
        static_status: 'unhealthy', display_order: 20, status_tier: 'premium_service', status_tier_label: 'Premium Services'}},
      general_http: {enabled: true, kind: 'provider', configured: true, presentation: {status_name: 'HTTP(S)', premium: false,
        static_status: 'healthy', display_order: 910, status_group: 'direct_sources', status_group_label: 'Network Sources',
        status_tier: 'general_family', status_tier_label: 'Standard Services'}},
    }};
  });
  await page.evaluate(() => window.DPProviderStatus.refresh());
  const tiers = page.locator('#provider-status-list .dp-provider-status-tier');
  await expect(tiers).toHaveCount(2);
  const torbox = page.locator('#provider-status-list [data-provider-id="torbox"]');
  await expect(torbox.locator('.dot')).toHaveClass(/\berror\b/);
  await expect(torbox.locator('.dot')).toHaveAccessibleName('Unhealthy');
  await expect(torbox.locator('.dp-provider-status-name')).toHaveText('TorBox');
  await expect(torbox.locator('.dp-provider-status-name')).toHaveCSS('opacity', '0');
  const layout = await page.evaluate(() => {
    const list = [...document.querySelectorAll('#provider-status-list .dp-provider-status-tier')];
    const footer = document.querySelector('#sidebar .sidebar-footer').getBoundingClientRect();
    return {
      dots: list.map(tier => tier.querySelectorAll('.dot').length),
      separators: list.map(tier => getComputedStyle(tier).borderTopStyle),
      inside: [...document.querySelectorAll('#provider-status-list .dot')].every(dot => {
        const r = dot.getBoundingClientRect();
        return r.left >= footer.left && r.right <= footer.right;
      }),
      headingsShown: list.some(tier => getComputedStyle(tier.querySelector('.dp-provider-status-tier-label')).opacity !== '0'),
    };
  });
  expect(layout).toEqual({dots: [2, 1], separators: ['none', 'solid'], inside: true, headingsShown: false});
  await torbox.hover();
  await expect(tooltip).toHaveText('TorBox: Unhealthy');
  await page.locator('#provider-status-list [data-provider-group="direct_sources"] .conn-row').hover();
  await expect(tooltip).toHaveText('Network Sources: Healthy');

  // No general tier: no separator is left behind.
  await page.evaluate(() => { delete settingsData.integrations.general_http; return window.DPProviderStatus.refresh(); });
  await expect(tiers).toHaveCount(1);
  expect(await tiers.first().evaluate(tier => getComputedStyle(tier).borderTopStyle)).toBe('none');

  // The crown alone for a premium account, its account in the tooltip.
  await page.evaluate(() => document.dispatchEvent(new CustomEvent('debridpulse:provider-status', {detail: {entries: [
    {id: 'alldebrid', name: 'AllDebrid', state: 'healthy', status: {account: {service_class: 'premium', entitlement: 'ready',
      plan: 'Premium', expires_at: Date.now() / 1000 + 82.5 * 86400}}}]}})));
  const crown = page.locator('#premium-row');
  await expect(crown).toBeVisible();
  await expect(page.locator('#lbl-premium')).toHaveCSS('opacity', '0');
  await crown.hover();
  await expect(tooltip).toContainText('AllDebrid Premium until');
  await expect(tooltip).toContainText('(83 days remaining)');

  // An aria2 or DB exception stays visible as its indicator.
  await page.evaluate(() => setDot('aria2', 'error', 'aria2: offline'));
  const aria2 = page.locator('#sidebar .conn-row:has(#dot-aria2)');
  await expect(aria2).toBeVisible();
  await aria2.hover();
  await expect(tooltip).toHaveText('aria2: offline');
  await page.screenshot({path: 'test-results/checkpoint-sidebar-compact-dark.png'});
  await page.evaluate(() => document.body.classList.add('light'));
  await page.screenshot({path: 'test-results/checkpoint-sidebar-compact-light.png'});

  // With the full sidebar the written labels speak for themselves: no tooltip.
  await page.evaluate(() => document.body.classList.remove('light'));
  await resize(page, 1600);
  await page.mouse.move(900, 600);
  await page.locator('#sidebar .nav-item[data-view="torrents"]').hover();
  await page.waitForTimeout(700);                 // past the tooltip owner's 450 ms hover delay
  await expect(tooltip).toBeHidden();
});

test('the mobile drawer is unchanged and never takes the desktop handle or overlay', async ({page}) => {
  await open(page, 600, {height: 900});
  await expect(page.locator('#sidebar-handle')).toBeHidden();
  await page.locator('#mobile-menu-btn').click();
  await expect(page.locator('#sidebar')).toHaveClass(/\bopen\b/);
  await expect(page.locator('#mobile-overlay')).toHaveClass(/\bopen\b/);
  await expect(page.locator('#sidebar')).not.toHaveClass(/sidebar-overlay-open/);
  await expect(page.locator('#sidebar .nav-item[data-view="dashboard"] .nav-label')).toHaveCSS('opacity', '1');
  await page.locator('#mobile-overlay').click({position: {x: 560, y: 450}});
  await expect(page.locator('#sidebar')).not.toHaveClass(/\bopen\b/);
  // The desktop overlay cannot be forced open below the desktop range.
  await page.evaluate(() => setSidebarOverlay(true));
  await expect(page.locator('#sidebar')).not.toHaveClass(/sidebar-overlay-open/);
});

/* Statistics: the same chart instance takes its settled container's size
 * across both directions of the persistent change, its data unchanged. */
test('the Statistics chart follows the sidebar transition in both directions without being rebuilt', async ({page}) => {
  await page.route('**/api/events/stream', () => {});
  await page.route(url => url.pathname === '/api/stats/detail', route => route.fulfill({status: 200, contentType: 'application/json',
    body: JSON.stringify({period: '7d', totals: {torrent_total: 9, completed_count: 7, partial_total: 1, success_rate_pct: 78, completed_size: 0},
      torrent_status: [{status: 'completed', count: 7}], file_status: [{status: 'completed', count: 7}],
      event_levels: [{level: 'info', count: 3}], transfer_methods: [{method: 'Debrid', count: 7}],
      daily_completions: [0, 1, 2, 3, 4, 5, 6].map(i => ({date: `2026-10-${String(17 + i).padStart(2, '0')}`, count: i + 1,
        premium: i, network: 1, unattributed: 0}))})}));
  await open(page, 1600);
  await page.waitForFunction(() => window.DPStatisticsLifecycle && typeof Chart !== 'undefined');
  await page.locator('#sidebar .nav-item[data-view="stats"]').click();
  const settledChart = () => page.waitForFunction(() => { const chart = document.getElementById('daily-chart')?._ci;
    return chart && !Chart.animator.running(chart) && chart.data.labels.length === 7; });
  await settledChart();
  await page.evaluate(() => { window.__chart = document.getElementById('daily-chart')._ci; });
  const fit = () => page.evaluate(() => {
    const chart = document.getElementById('daily-chart')._ci;
    const parent = chart.canvas.parentElement;
    const style = getComputedStyle(parent);
    const inner = parent.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    return {same: chart === window.__chart, labels: chart.data.labels.length,
      data: chart.data.datasets.map(set => set.data.join()).join('|'), gap: Math.abs(chart.width - inner)};
  });
  const start = await fit();
  expect(start.gap).toBeLessThan(1);
  // Expanded -> compact (the page gains the 168px the rail gives back) -> expanded.
  for (const width of [COMPACT, 1600]) {
    await resize(page, width);
    await expect.poll(async () => (await fit()).gap).toBeLessThan(1);
    expect(await fit()).toEqual({...start, gap: expect.any(Number)});
  }
});

test('negative control: an unresized chart fails the Statistics fit check', async ({page}) => {
  await page.route('**/api/events/stream', () => {});
  await open(page, 1600);
  await page.waitForFunction(() => window.DPStatisticsLifecycle && typeof Chart !== 'undefined');
  await page.locator('#sidebar .nav-item[data-view="stats"]').click();
  await page.waitForFunction(() => document.getElementById('daily-chart')?._ci);
  // A chart whose resize never happens (the defect the positive case excludes).
  await page.evaluate(() => { const chart = document.getElementById('daily-chart')._ci; chart.resize = chart._resize = () => {}; });
  const before = await page.evaluate(() => document.getElementById('daily-chart')._ci.width);
  await resize(page, COMPACT);
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const after = await page.evaluate(() => {
    const chart = document.getElementById('daily-chart')._ci;
    return {width: chart.width, parent: chart.canvas.parentElement.clientWidth};
  });
  expect(after.width).toBe(before);
  expect(Math.abs(after.parent - after.width)).toBeGreaterThan(50);
});
