const { test, expect } = require('@playwright/test');

/* DP 1.0.14 Settings and Help handheld master headers (320-779px only). The
 * header is one row: the page's identity -- as much of it as leaves the
 * section navigator whole (shorter approved wordings, then no hint, no
 * artwork, none) -- and the SAME compact navigator as from 780px (ui-section-
 * nav.js: First, a window onto the one tablist, Last), centred in the whole
 * header while the identity fits beside it, else right after it, centred again
 * with no identity; a choice slides the navigator's window and the content
 * viewport by DPMotion as from 780px. From 780px nothing changes.
 * Engine: Chromium (Playwright), CSS viewport sizes; the web face with
 * DP_WEB_FONTS=1, else the fallback face. */

const WEB_FONTS = process.env.DP_WEB_FONTS === '1';
const PAGES = {
  settings: {view: '#view-settings', head: '.dp-settings-master-header', copy: '.dp-settings-header-copy', nav: '.dp-settings-nav', strip: '.dp-settings-tabs',
    scroll: '.dp-settings-scroll', icon: '.dp-settings-header-icon', title: '.dp-settings-header-title', hint: '.dp-settings-header-subtitle', first: 'sources',
    levels: [['Tuning Deck', 'Your rules, your defaults.', true], ['Tuning Deck', 'Your rules.', true], ['Tuning', 'Your rules.', true], ['Tune', '', true], ['Tune', '', false], ['', '', false]]},
  help: {view: '#view-help', head: '.dp-help-master-header', copy: '.dp-help-header-copy', nav: '.dp-help-nav', strip: '.dp-help-tabs',
    scroll: '.dp-help-scroll', icon: '.dp-help-title-icon', title: '.dp-help-header-title', hint: '.dp-help-header-subtitle', first: 'quickstart',
    levels: [['Field Manual', 'When intuition fails.', true], ['Field Manual', 'When stuck.', true], ['Manual', 'When stuck.', true], ['Help', '', true], ['Help', '', false], ['', '', false]]},
};

async function open(page, name, width) {
  if (!WEB_FONTS) await page.route(/fonts\.(googleapis|gstatic)\.com/, route => route.abort());
  await page.route('**/api/events/stream', route => route.abort());
  // Every frame in which a navigator window or content viewport slides: its direction.
  await page.addInitScript(() => {
    window.__slides = [];
    const tick = () => {
      for (const node of document.querySelectorAll('.dp-motion-surface')) {
        const kind = node.matches('[role="tablist"]') ? 'strip' : node.matches('.dp-settings-scroll, .dp-help-scroll') ? 'content' : null;
        if (kind) window.__slides.push(`${kind}:${node.dataset.dpMotionDirection}`);
      }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });
  await page.setViewportSize({width, height: 844});
  await page.goto('/');
  await page.evaluate(view => nav(document.querySelector(`#sidebar .nav-item[data-view="${view}"]:not([data-title])`)), name);
  await expect(page.locator(`${PAGES[name].view} ${PAGES[name].strip} [role="tab"]`).first()).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await settle(page);
}

const settle = async page => {
  await page.waitForFunction(() => !document.querySelector('[role="tablist"].dp-motion-surface') && document.getAnimations()
    .every(animation => animation.playState !== 'running' || animation.effect?.getTiming().iterations === Infinity));
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
};

async function resize(page, width) {
  await page.setViewportSize({width, height: 844});
  await settle(page);
}

// The header as drawn: which identity level, one row or not, and where the navigator stands.
const header = (page, name) => page.evaluate(p => {
  const v = document.querySelector(p.view), h = v.querySelector(p.head), box = node => node.getBoundingClientRect();
  const shown = node => node && node.getClientRects().length > 0;
  const copy = h.querySelector(p.copy), navigation = h.querySelector(p.nav), control = navigation.querySelector('.dp-section-nav-control');
  const style = getComputedStyle(h), inner = {left: box(h).left + parseFloat(style.paddingLeft), right: box(h).right - parseFloat(style.paddingRight)};
  const middle = node => (box(node).top + box(node).bottom) / 2;
  const level = [shown(copy) && shown(h.querySelector(p.title)) ? h.querySelector(p.title).textContent : '',
    shown(copy) && shown(h.querySelector(p.hint)) ? h.querySelector(p.hint).textContent : '', shown(copy) && shown(h.querySelector(p.icon))];
  const drawn = [copy, ...copy.querySelectorAll(':scope > *')].filter(shown);
  const copyRight = drawn.length ? Math.max(...drawn.map(node => box(node).right)) : null;
  return {presentation: h.dataset.presentation ?? null, marks: [h.dataset.dpIdentity ?? null], level, levelIndex: p.levels.findIndex(l => JSON.stringify(l) === JSON.stringify(level)),
    oneRow: !shown(copy) || Math.abs(middle(copy) - middle(control)) < 14, controlInside: box(control).left >= inner.left - 0.5 && box(control).right <= inner.right + 0.5,
    offCentre: (box(control).left + box(control).right) / 2 - (inner.left + inner.right) / 2, afterCopy: copyRight === null ? null : box(control).left - copyRight,
    gap: parseFloat(style.columnGap), ends: [...navigation.querySelectorAll('.dp-section-nav-end')].filter(shown).length,
    clipped: [...h.querySelectorAll(`${p.title}, ${p.hint}`)].filter(shown).some(node => node.scrollWidth > node.clientWidth + 0.5),
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth, height: box(h).height};
}, PAGES[name]);

for (const name of Object.keys(PAGES)) {
  test(`${name}: one handheld header row with the compact navigator; the identity gives way in its approved order and returns as room does; centred when unpressed`, async ({page}) => {
    await open(page, name, 320);
    let previous = Infinity;
    for (const width of [320, 360, 390, 430, 480, 560, 640, 700, 779]) {
      await resize(page, width);
      const h = await header(page, name);
      const at = `${name} ${width}px`;
      expect(h.presentation, `${at}: the shared navigator draws it`).not.toBe(null);
      expect(h.oneRow && h.controlInside && h.overflow === 0, `${at}: one row, the navigator whole inside the header`).toBe(true);
      expect(h.levelIndex, `${at}: an approved identity level ${JSON.stringify(h.level)}`).toBeGreaterThanOrEqual(0);
      expect(h.levelIndex, `${at}: the identity never shorter at a wider width`).toBeLessThanOrEqual(previous);
      previous = h.levelIndex;
      expect(h.clipped, `${at}: title and hint drawn whole`).toBe(false);
      if (h.presentation === 'compact') expect(h.ends, `${at}: First or Last there`).toBeGreaterThanOrEqual(1);
      // Centred in the whole header, else right after the identity (never past it); centred with none.
      if (h.levelIndex === PAGES[name].levels.length - 1) expect(Math.abs(h.offCentre), `${at}: no identity, centred`).toBeLessThanOrEqual(0.5);
      else if (Math.abs(h.offCentre) > 0.5) {
        expect(h.offCentre, `${at}: pushed right, never left`).toBeGreaterThan(0);
        expect(h.afterCopy, `${at}: never over the identity`).toBeGreaterThanOrEqual(h.gap - 0.5);
      }
    }
    // (In the web face the full identity fits beside the navigator at 779px; in the fallback face a shorter level may.)
    if (WEB_FONTS) expect(previous, `${name}: the full identity at 779px`).toBe(0);
    await page.screenshot({path: `test-results/checkpoint-${name}-handheld-header.png`, clip: {x: 0, y: 0, width: 779, height: 200}});
    // 780px: the desktop header, nothing of this owner's handheld marks.
    await resize(page, 780);
    const desk = await header(page, name);
    expect([desk.marks, desk.level, desk.levelIndex]).toEqual([[null], PAGES[name].levels[0], 0]);
    // And back: the handheld level again, the same as before.
    await resize(page, 390);
    const back = await header(page, name);
    await resize(page, 391);
    await resize(page, 390);
    expect((await header(page, name)).level).toEqual(back.level);
  });

  test(`${name}: on a handheld the compact navigator chooses as from 780px -- tabs, First/Last, keyboard -- sliding its window and the content the choice's way; re-entry starts at the first section`, async ({page}) => {
    await open(page, name, 390);
    const p = PAGES[name];
    const tabs = page.locator(`${p.view} ${p.strip}:not(.dp-motion-still) [role="tab"]`);
    const count = await tabs.count();
    const order = await tabs.evaluateAll(nodes => nodes.map(node => node.dataset.tab || node.dataset.htab || node.id));
    const state = () => page.evaluate(({p}) => {
      const v = document.querySelector(p.view), strip = v.querySelector(`${p.strip}:not(.dp-motion-still)`);
      const selected = strip.querySelector('[aria-selected="true"]'), sb = strip.getBoundingClientRect(), tb = selected.getBoundingClientRect();
      const panels = [...v.querySelectorAll('[data-panel]:not(.dp-motion-still *)')].filter(panel => !panel.hidden).map(panel => panel.dataset.panel);
      return {selected: selected.dataset.tab || selected.dataset.htab || selected.id, panels, whole: tb.left >= sb.left - 0.5 && tb.right <= sb.right + 0.5,
        tabStop: [...strip.querySelectorAll('[role="tab"]')].filter(tab => tab.tabIndex === 0).length};
    }, {p});
    // The next section (the window's right edge): one forward choice -- the content slides forward.
    await page.evaluate(() => { window.__slides.length = 0; });
    await tabs.nth(1).click();
    await settle(page);
    let s = await state();
    expect(s.whole && s.tabStop === 1, `${name}: the selection whole in the window, one tab stop`).toBe(true);
    let slides = await page.evaluate(() => [...new Set(window.__slides)]);
    expect(slides, `${name}: the content viewport slid forward`).toContain('content:forward');
    // Last: the window and the content travel forward to the last section.
    await page.evaluate(() => { window.__slides.length = 0; });
    await page.locator(`${p.view} .dp-section-nav-end:last-child`).click();
    await settle(page);
    s = await state();
    expect(s.selected).toBe(order.at(-1));
    expect(s.whole).toBe(true);
    slides = await page.evaluate(() => [...new Set(window.__slides)]);
    expect(slides).toEqual(expect.arrayContaining(['strip:forward', 'content:forward']));
    // First: back, the other way.
    await page.evaluate(() => { window.__slides.length = 0; });
    await page.locator(`${p.view} .dp-section-nav-end:first-child`).click();
    await settle(page);
    s = await state();
    expect(s.selected).toBe(order[0]);
    slides = await page.evaluate(() => [...new Set(window.__slides)]);
    expect(slides).toEqual(expect.arrayContaining(['strip:backward', 'content:backward']));
    // Keyboard: the arrow keys move between the tabs; focus stays on a shown tab.
    await tabs.nth(0).focus();
    await page.keyboard.press('ArrowRight');
    await settle(page);
    s = await state();
    expect(s.selected).toBe(order[1]);
    expect(await page.evaluate(() => { const a = document.activeElement; return a.getAttribute('role') === 'tab' && a.getClientRects().length > 0 && a.getAttribute('aria-selected'); })).toBe('true');
    // Quick successive choices settle on the last one, the content agreeing.
    for (let k = 2; k < Math.min(count, 5); k += 1) await tabs.nth(k).click();
    await settle(page);
    s = await state();
    expect(s.panels).toEqual([s.selected]);
    // Leaving and returning starts again at the first section, the header as it was.
    const before = await header(page, name);
    await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="dashboard"]')));
    await page.evaluate(view => nav(document.querySelector(`#sidebar .nav-item[data-view="${view}"]:not([data-title])`)), name);
    await settle(page);
    s = await state();
    expect(s.selected).toBe(p.first);
    expect((await header(page, name)).level).toEqual(before.level);
  });
}

test('reduced motion on a handheld: a choice lands at once, without a slide, and the selection and content agree', async ({page}) => {
  await page.emulateMedia({reducedMotion: 'reduce'});
  await open(page, 'settings', 390);
  await page.evaluate(() => { window.__slides.length = 0; });
  await page.locator('#view-settings .dp-section-nav-end:last-child').click();
  await settle(page);
  expect(await page.evaluate(() => window.__slides.length)).toBe(0);
  expect(await page.evaluate(() => {
    const v = document.getElementById('view-settings'), tab = v.querySelector('.dp-settings-tabs [aria-selected="true"]');
    return [tab.dataset.tab, [...v.querySelectorAll('[data-panel]')].filter(panel => !panel.hidden).map(panel => panel.dataset.panel)];
  })).toEqual(['maintenance', ['maintenance']]);
});
