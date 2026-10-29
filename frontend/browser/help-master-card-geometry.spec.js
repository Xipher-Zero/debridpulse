const { test, expect } = require('@playwright/test');

/* Help & License is the bounded master card from the shell's 780px mode change
 * up, exactly as every main page is: the card ends where Downloads', the
 * Activity Log's, Statistics' and Settings' end, with no step at 900/901, the page
 * (#content) never scrolls, only the manual (.dp-help-scroll) does, the header
 * stays put while it scrolls, and the version beneath the view stays on
 * screen. Below 780px the handheld page scrolls, as before. Read from the
 * rendered page. */

const SIZES = [[1560, 932], [1180, 760], [900, 932], [780, 932], [780, 700]];

const settle = page => page.evaluate(() => new Promise(resolve => {
  const still = () => !document.querySelector('.dp-motion-surface') && document.getAnimations()
    .every(animation => animation.playState !== 'running' || animation.effect?.getTiming().iterations === Infinity);
  const wait = () => (still() ? requestAnimationFrame(() => requestAnimationFrame(resolve)) : setTimeout(wait, 30));
  wait();
}));

async function show(page, view) {
  await page.evaluate(view => nav(document.querySelector(`#sidebar .nav-item[data-view="${view}"]:not([data-title])`)), view);
  await expect(page.locator(`#view-${view}`)).toHaveClass(/\bactive\b/);
  await settle(page);
}

const geometry = (page, view) => page.evaluate(view => {
  const content = document.getElementById('content'), v = document.getElementById(`view-${view}`);
  const version = document.getElementById('sidebar-version'), range = document.createRange();
  range.selectNodeContents(version);
  const text = range.getBoundingClientRect();
  return {viewBottom: v.getBoundingClientRect().bottom, contentScrolls: content.scrollHeight > content.clientHeight + 1,
    pageScrolls: document.documentElement.scrollHeight > innerHeight + 1, version: {top: text.top, bottom: text.bottom, right: text.right},
    height: innerHeight, width: innerWidth};
}, view);

const MAIN = ['torrents', 'events', 'stats', 'settings'];
const WIDTHS = [780, 800, 899, 900, 901, 1024, 1440];

test('at every width from 780px Help ends on the main pages’ bottom datum, never steps at 900/901, and its navigator switches sections', async ({page}) => {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.setViewportSize({width: 1440, height: 932});
  await page.goto('/');
  await page.evaluate(() => document.fonts.ready);
  const cards = {};
  for (const width of WIDTHS) {
    await page.setViewportSize({width, height: 932});
    await page.waitForFunction(() => document.getElementById('sidebar').getAnimations().every(a => a.playState !== 'running'));
    const bottoms = [];
    for (const view of MAIN) {
      await show(page, view);
      bottoms.push((await geometry(page, view)).viewBottom);
    }
    await show(page, 'help');
    const help = await geometry(page, 'help');
    const at = `${width}x932`;
    for (const [index, bottom] of bottoms.entries()) {
      expect(Math.abs(help.viewBottom - bottom), `${at}: Help does not end where ${MAIN[index]} does`).toBeLessThanOrEqual(0.5);
    }
    expect([help.contentScrolls, help.pageScrolls], `${at}: the page scrolls`).toEqual([false, false]);
    expect(help.version.top, `${at}: the version is not beneath the card`).toBeGreaterThanOrEqual(help.viewBottom);
    expect(help.version.bottom, `${at}: the version is off screen`).toBeLessThanOrEqual(932);
    cards[width] = await page.locator('#view-help').evaluate(view => {
      const box = view.getBoundingClientRect(), scroller = view.querySelector('.dp-help-scroll');
      return {top: box.top, bottom: box.bottom, overflow: getComputedStyle(scroller).overflowY};
    });
    expect(cards[width].overflow, `${at}: the manual does not scroll inside the card`).toBe('auto');
    // The navigator switches sections at this width, and the card does not move.
    const tabs = page.locator('#view-help .dp-help-tabs [data-tab]');
    const target = await tabs.evaluateAll(nodes => nodes.find(node => node.getClientRects().length
      && node.getAttribute('aria-selected') !== 'true')?.dataset.tab);
    await page.evaluate(tab => document.querySelector(`#view-help .dp-help-tabs [data-tab="${tab}"]`).click(), target);
    await settle(page);
    await expect(page.locator(`#view-help .dp-help-tabs [data-tab="${target}"]`)).toHaveAttribute('aria-selected', 'true');
    expect(await page.locator('#view-help').evaluate(view => view.getBoundingClientRect().bottom), `${at}: switching moved the card`)
      .toBeCloseTo(cards[width].bottom, 1);
  }
  // No step across 899/900/901: the same card, top to bottom.
  expect([cards[900], cards[901]]).toEqual([cards[899], cards[899]]);
});

test('Help ends where Statistics ends, the page never scrolls, and the version stays on screen', async ({page}) => {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.setViewportSize({width: 1560, height: 932});
  await page.goto('/');
  await page.evaluate(() => document.fonts.ready);
  for (const [width, height] of SIZES) {
    await page.setViewportSize({width, height});
    await page.waitForFunction(() => document.getElementById('sidebar').getAnimations().every(a => a.playState !== 'running'));
    await show(page, 'stats');
    const stats = await geometry(page, 'stats');
    await show(page, 'help');
    const help = await geometry(page, 'help');
    const at = `${width}x${height}`;
    expect(Math.abs(help.viewBottom - stats.viewBottom), `${at}: Help's card does not end where Statistics' does`).toBeLessThanOrEqual(0.5);
    expect([help.contentScrolls, help.pageScrolls], `${at}: the page scrolls`).toEqual([false, false]);
    expect(help.version.bottom, `${at}: the version is off screen`).toBeLessThanOrEqual(height);
    expect(help.version.top, `${at}: the version is off screen`).toBeGreaterThanOrEqual(help.viewBottom);
    expect(Math.abs(help.version.right - stats.version.right), `${at}: the version moved`).toBeLessThanOrEqual(0.5);
  }
});

test('only the manual scrolls: the header stays put, every section reaches its end, a new section starts at its top, accordions grow inside', async ({page}) => {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.setViewportSize({width: 780, height: 932});
  await page.goto('/');
  await show(page, 'help');
  const tabs = await page.locator('#view-help .dp-help-tabs [data-tab]').evaluateAll(nodes => nodes.map(node => node.dataset.tab));
  expect(tabs.length).toBeGreaterThan(1);
  for (const tab of tabs) {
    await page.evaluate(tab => document.querySelector(`#view-help .dp-help-tabs [data-tab="${tab}"]`).click(), tab);
    await settle(page);
    const g = await page.evaluate(() => {
      const scroller = document.querySelector('#view-help .dp-help-scroll'), header = document.querySelector('#view-help .dp-help-master-header');
      const panel = document.querySelector('#view-help .dp-help-panel:not([hidden]):not(.dp-motion-still *)');
      const atTop = scroller.scrollTop, headerTop = header.getBoundingClientRect().top;
      scroller.scrollTop = scroller.scrollHeight;
      const end = panel.lastElementChild.getBoundingClientRect().bottom, bottom = scroller.getBoundingClientRect().bottom;
      return {atTop, owns: scroller.scrollHeight > scroller.clientHeight, reached: Math.abs(scroller.scrollTop + scroller.clientHeight - scroller.scrollHeight) <= 1,
        lastVisible: end <= bottom + 1, headerMoved: header.getBoundingClientRect().top - headerTop,
        contentScrolled: document.getElementById('content').scrollTop};
    });
    expect(g.atTop, `${tab}: did not start at its top`).toBe(0);
    expect(g.reached && g.lastVisible, `${tab}: its end is not reachable`).toBe(true);
    expect(g.headerMoved, `${tab}: the header moved`).toBe(0);
    expect(g.contentScrolled, `${tab}: the page scrolled`).toBe(0);
  }
  // Opening every accordion of a section grows the manual, never the card.
  await page.evaluate(tab => document.querySelector(`#view-help .dp-help-tabs [data-tab="${tab}"]`).click(), tabs[1]);
  await settle(page);
  const before = await page.locator('#view-help').evaluate(view => view.getBoundingClientRect().bottom);
  const opened = await page.evaluate(() => {
    const all = [...document.querySelectorAll('#view-help .dp-help-panel:not([hidden]) details.dp-help-accordion:not([open])')];
    all.forEach(details => { details.open = true; });
    return all.length;
  });
  expect(opened).toBeGreaterThan(0);
  await settle(page);
  expect(await page.locator('#view-help').evaluate(view => view.getBoundingClientRect().bottom)).toBeCloseTo(before, 1);
  expect(await page.evaluate(() => document.getElementById('content').scrollHeight <= document.getElementById('content').clientHeight + 1)).toBe(true);
});

test('below 780px the handheld page still scrolls, not the card', async ({page}) => {
  await page.setViewportSize({width: 700, height: 900});
  await page.goto('/');
  await show(page, 'help');
  const g = await page.evaluate(() => ({content: document.getElementById('content').scrollHeight > document.getElementById('content').clientHeight + 1,
    scroller: getComputedStyle(document.querySelector('#view-help .dp-help-scroll')).overflowY}));
  expect(g).toEqual({content: true, scroller: 'visible'});
});
