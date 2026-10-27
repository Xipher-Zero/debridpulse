const { test, expect } = require('@playwright/test');

/* The compact Settings and Help navigators' section index: the one tooltip
 * owner (ui-tooltip.js) shows, on hover over the navigator or keyboard focus
 * within it, the current section, its place and the count over every section
 * in its order, the current one emphasised -- read from the navigator's own
 * tabs and selection (ui-section-nav.js marks the compact control). The
 * complete strip shows every section and has no index. The index is on one
 * line wherever the viewport holds it, else wraps between whole sections; it
 * is never over the navigator and never takes a click. */

const PAGES = [
  {view: 'settings', label: 'Settings', count: 7},
  {view: 'help', label: 'Help', count: 8},
];

const settle = async page => {
  await page.waitForFunction(() => !document.querySelector('.dp-motion-surface') && document.getAnimations()
    .every(animation => animation.playState !== 'running' || animation.effect?.getTiming().iterations === Infinity));
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
};

async function resize(page, width) {
  await page.setViewportSize({width, height: 900});
  await page.waitForFunction(() => document.getElementById('sidebar').getAnimations().every(animation => animation.playState !== 'running'));
  await settle(page);
}

async function open(page, view, width, fallbackFonts) {
  if (fallbackFonts) await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.setViewportSize({width, height: 900});
  await page.goto('/');
  await page.locator(`#sidebar .nav-item[data-view="${view}"]:not([data-title])`).click();
  await expect(page.locator(`#view-${view} .dp-${view}-tabs [data-tab]`).first()).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await settle(page);
}

const shownTip = page => page.waitForFunction(() => {
  const tip = document.getElementById('dp-tooltip');
  return tip?.classList.contains('is-shown') && getComputedStyle(tip).opacity === '1';
});

const awayFrom = page => page.mouse.move(2, 890);

/* The index as drawn, with the navigator's own tabs and geometry. */
function index(page, view) {
  return page.evaluate(view => {
    const tip = document.getElementById('dp-tooltip');
    const strip = document.querySelector(`#view-${view} .dp-${view}-tabs`);
    const control = strip.parentElement;
    const tabs = [...strip.querySelectorAll('[data-tab]')];
    const name = node => node.textContent.trim().replace(/\s+/g, ' ');
    const shown = !!tip && !tip.hidden && tip.classList.contains('is-shown');
    const result = {shown, described: control.getAttribute('data-tooltip'), tabs: tabs.map(name),
      selected: tabs.findIndex(tab => tab.getAttribute('aria-selected') === 'true')};
    if (!shown) return result;
    const t = tip.getBoundingClientRect(), c = control.getBoundingClientRect();
    const list = tip.querySelector('.dp-tooltip-index-list');
    const entries = list ? [...list.children] : [];
    const line = parseFloat(getComputedStyle(list).lineHeight);
    return {...result, isIndex: tip.classList.contains('is-index'),
      head: [...(tip.querySelector('.dp-tooltip-index-head')?.children || [])].map(name),
      entries: entries.map(name), emphasised: entries.filter(node => node.tagName === 'STRONG').map(name),
      weights: entries.map(node => getComputedStyle(node).fontWeight),
      separators: [...list.childNodes].filter(node => node.nodeType === 3).map(node => node.textContent),
      lines: new Set(entries.map(node => Math.round(node.getBoundingClientRect().top))).size,
      entryLines: entries.map(node => Math.round(node.getBoundingClientRect().height / line)),
      entriesInside: entries.every(node => { const r = node.getBoundingClientRect(); return r.left >= t.left - 0.5 && r.right <= t.right + 0.5; }),
      overflow: tip.scrollWidth - tip.clientWidth,
      inViewport: t.left >= -0.5 && t.right <= innerWidth + 0.5 && t.top >= -0.5 && t.bottom <= innerHeight + 0.5,
      overControl: !(t.bottom <= c.top + 0.5 || t.top >= c.bottom - 0.5),
      pointer: getComputedStyle(tip).pointerEvents, fontSize: getComputedStyle(list).fontSize,
      describedBy: control.getAttribute('aria-describedby') || ''};
  }, view);
}

function expectIndex(i, at, count) {
  expect(i.shown, at).toBe(true);
  expect(i.isIndex, at).toBe(true);
  // Every section, in the navigator's own order and words.
  expect(i.entries, at).toEqual(i.tabs);
  expect(i.entries, at).toHaveLength(count);
  expect(i.separators.every(text => text === ' · '), at).toBe(true);
  // The current section: named, placed, counted and emphasised -- the selection's own.
  expect(i.head, at).toEqual(['Current section', i.tabs[i.selected], `${i.selected + 1} of ${count}`]);
  expect(i.emphasised, at).toEqual([i.tabs[i.selected]]);
  expect(Number(i.weights[i.selected]), at).toBeGreaterThan(Number(i.weights[(i.selected + 1) % count]));
  // Never over the navigator, never a target, inside the viewport, whole.
  expect(i.overControl, at).toBe(false);
  expect(i.pointer, at).toBe('none');
  expect(i.inViewport, at).toBe(true);
  expect(i.entriesInside, at).toBe(true);
  expect(i.overflow, at).toBeLessThanOrEqual(0);
  expect(i.entryLines.every(lines => lines === 1), `${at}: no section broken`).toBe(true);
  expect(i.fontSize, at).toBe('11px');
  expect(i.describedBy.split(/\s+/), at).toContain('dp-tooltip');
}

for (const {view, label, count} of PAGES) {
  for (const fonts of ['web fonts', 'fallback fonts']) {
    test(`with ${fonts}, the compact ${label} navigator shows its whole section index on hover and on keyboard focus, on one line, and the complete strip has none`, async ({page}) => {
      await open(page, view, 1920, fonts === 'fallback fonts');
      const control = page.locator(`#view-${view} .dp-${view}-tabs`);
      // The complete strip: every section is already shown; no index.
      const full = await index(page, view);
      expect(full.described).toBeNull();
      expect(await control.evaluate(strip => strip.closest('[data-tooltip]'))).toBeNull();

      const rows = [];
      for (const width of [1100, 780]) {
        await resize(page, width);
        await expect(page.locator(`#view-${view} .dp-${view}-master-header`)).toHaveAttribute('data-presentation', 'compact');
        // Hover over the navigator.
        const box = await control.boundingBox();
        await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
        await shownTip(page);
        let i = await index(page, view);
        expectIndex(i, `hover at ${width}px`, count);
        expect(i.lines, `the index on one line at ${width}px`).toBe(1);
        rows.push(`${width}px: ${i.entries.length} sections on ${i.lines} line`);
        await page.locator('#dp-tooltip').screenshot({path: `test-results/checkpoint-${view}-index-${fonts.split(' ')[0]}-${width}.png`});
        await awayFrom(page);
        await expect(page.locator('#dp-tooltip.is-shown')).toHaveCount(0);

        // Keyboard: focus within the navigator shows it, and it follows the selection.
        const selected = page.locator(`#view-${view} .dp-${view}-tabs [aria-selected="true"]`);
        await selected.focus();
        await page.keyboard.press('ArrowRight');
        await settle(page);
        await shownTip(page);
        i = await index(page, view);
        expectIndex(i, `keyboard at ${width}px`, count);
        await page.keyboard.press('End');
        await settle(page);
        await shownTip(page);
        i = await index(page, view);
        expectIndex(i, `End at ${width}px`, count);
        expect(i.selected).toBe(count - 1);
        await page.keyboard.press('Home');
        await settle(page);
        await shownTip(page);
        expect((await index(page, view)).selected).toBe(0);
        await page.evaluate(() => document.activeElement.blur());
        await expect(page.locator('#dp-tooltip.is-shown')).toHaveCount(0);
      }
      test.info().annotations.push({type: `${label} index (${fonts})`, description: rows.join('; ')});
    });
  }

  test(`the ${label} index follows First and Last, and takes no click: the edge preview, clicked again and again at one point, still chooses the next section`, async ({page}) => {
    await open(page, view, 1100, false);
    const strip = `#view-${view} .dp-${view}-tabs`;
    const last = page.locator(`#view-${view} .dp-${view}-nav-end`).last();
    await last.click();
    await settle(page);
    // The press dismissed it; hovering again shows the new selection.
    await awayFrom(page);
    const box = await page.locator(strip).boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await shownTip(page);
    let i = await index(page, view);
    expectIndex(i, 'after Last', count);
    expect(i.selected).toBe(count - 1);
    await awayFrom(page);
    await page.locator(`#view-${view} .dp-${view}-nav-end`).first().click();
    await settle(page);
    await awayFrom(page);
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await shownTip(page);
    expect((await index(page, view)).selected).toBe(0);

    // The rightmost preview's point, with the index shown: what is there is
    // the tab, and each click there chooses the next section.
    const point = await page.evaluate(sel => {
      const s = document.querySelector(sel), b = s.getBoundingClientRect();
      const inset = (/inset\(([^)]+)\)/.exec(getComputedStyle(s).clipPath)?.[1] || '0').split(/\s+/).map(parseFloat);
      const [top, right = top, , left = right] = inset;
      const win = {left: b.left + left, right: b.right - right};
      const tabs = [...s.querySelectorAll('[data-tab]')];
      let k = -1;
      tabs.forEach((tab, j) => {
        const r = tab.getBoundingClientRect();
        if (tab.getAttribute('aria-selected') !== 'true' && getComputedStyle(tab).opacity !== '0'
          && Math.min(r.right, win.right) - Math.max(r.left, win.left) >= 24) k = j;
      });
      const r = tabs[k].getBoundingClientRect();
      return {x: (Math.max(r.left, win.left) + Math.min(r.right, win.right)) / 2, y: (r.top + r.bottom) / 2, k};
    }, strip);
    await page.mouse.move(point.x, point.y);
    await shownTip(page);
    const under = () => page.evaluate(([x, y]) => document.elementFromPoint(x, y)?.closest('[data-tab]')?.dataset.tab ?? null, [point.x, point.y]);
    const order = (await index(page, view)).tabs;
    const ids = await page.locator(`${strip} [data-tab]`).evaluateAll(tabs => tabs.map(tab => tab.dataset.tab));
    let expected = point.k;
    for (let click = 0; click < 3 && expected < count; click += 1) {
      expect(await under(), `click ${click + 1}: the tab, not the index, is under the pointer`).toBe(ids[expected]);
      await page.mouse.click(point.x, point.y);
      await settle(page);
      expect((await index(page, view)).selected, `click ${click + 1} chose ${order[expected]}`).toBe(expected);
      expected += 1;
    }
  });

  test(`where the ${label} index cannot hold one line it wraps between whole sections, inside the viewport`, async ({page}) => {
    await open(page, view, 780, true);
    // Spaced out until it cannot fit 780px on one line (the tooltip's own
    // type unchanged): the wrapping, not the fonts, is what is tested.
    await page.addStyleTag({content: '#dp-tooltip .dp-tooltip-index-list { letter-spacing: 6px; }'});
    const box = await page.locator(`#view-${view} .dp-${view}-tabs`).boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await shownTip(page);
    const i = await index(page, view);
    expectIndex(i, 'wrapped at 780px', count);
    expect(i.lines, 'wraps').toBeGreaterThan(1);
    await page.locator('#dp-tooltip').screenshot({path: `test-results/checkpoint-${view}-index-wrapped-780.png`});
  });
}

test('below 780px neither handheld header has an index', async ({page}) => {
  for (const {view} of PAGES) {
    await open(page, view, 1100, false);
    await resize(page, 779);
    expect((await index(page, view)).described).toBeNull();
  }
});
