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

/* The index for the pointer is a deliberate pause, never a by-product of
 * using the navigator: it shows only after the pointer has rested over the
 * compact navigator (moving no more than 4px) for 2s, with nothing in it
 * still moving; moving off that rest, using it or leaving it starts or ends
 * the wait; keyboard focus stays prompt. Every frame records whether the
 * index is shown, so each case reads WHEN it appeared against what the
 * pointer did -- real time, since the wait itself is the subject. */
const DWELL = 1000;

async function openLogged(page, view, width = 1100) {
  await page.addInitScript(() => {
    window.__index = [];
    const tick = () => {
      const t = document.getElementById('dp-tooltip');
      const on = !!t && !t.hidden && t.classList.contains('is-shown') && t.classList.contains('is-index');
      if (on !== !!window.__indexOn) { window.__indexOn = on; window.__index.push({t: performance.now(), on}); }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });
  await open(page, view, width, false);
  await awayFrom(page);
}

const now = page => page.evaluate(() => performance.now());
const until = (page, t) => page.waitForFunction(t => performance.now() >= t, t);
// Times (ms after `since`) at which the index appeared.
const appeared = (page, since) => page.evaluate(since => window.__index.filter(e => e.on && e.t >= since).map(e => e.t - since), since);
const shownNow = page => page.evaluate(() => !!window.__indexOn);
const indexShown = page => page.waitForFunction(() => window.__indexOn === true, null, {timeout: 6000});
const settled = page => page.waitForFunction(() => !document.querySelector('.dp-motion-surface')
  && !document.querySelector('[data-tooltip-busy]'));

async function navigatorPoint(page, view, which = 'middle') {
  return page.evaluate(([sel, which]) => {
    const s = document.querySelector(sel), b = s.getBoundingClientRect();
    const inset = (/inset\(([^)]+)\)/.exec(getComputedStyle(s).clipPath)?.[1] || '0').split(/\s+/).map(parseFloat);
    const [top, right = top, , left = right] = inset;
    const win = {left: b.left + left, right: b.right - right};
    if (which === 'middle') return {x: (win.left + win.right) / 2, y: (b.top + b.bottom) / 2, win};
    const tabs = [...s.querySelectorAll('[data-tab]')];
    const shown = tabs.map((tab, k) => { const r = tab.getBoundingClientRect(); return {k, r, part: Math.min(r.right, win.right) - Math.max(r.left, win.left),
      selected: tab.getAttribute('aria-selected') === 'true', hidden: getComputedStyle(tab).opacity === '0'}; })
      .filter(e => e.part >= 24 && !e.selected && !e.hidden);
    const e = which === 'right' ? shown.at(-1) : shown[0];
    return {x: (Math.max(e.r.left, win.left) + Math.min(e.r.right, win.right)) / 2, y: (e.r.top + e.r.bottom) / 2, win};
  }, [`#view-${view} .dp-${view}-tabs`, which]);
}

for (const {view, label} of PAGES) {
  test(`the ${label} index waits for the pointer to rest 2s; moving, leaving and coming back start the wait again; a little jitter does not`, async ({page}) => {
    await openLogged(page, view);
    const p = await navigatorPoint(page, view);
    // 1-2. Enter and rest: nothing before 2s, the index at 2s.
    let t0 = await now(page);
    await page.mouse.move(p.x, p.y);
    await indexShown(page);
    let at = await appeared(page, t0);
    expect(at[0], 'rested: not before the dwell').toBeGreaterThanOrEqual(DWELL);
    expect(at[0], 'rested: at the dwell').toBeLessThan(DWELL + 600);
    // 6. Shown, then a meaningful move: gone at once ...
    await page.mouse.move(p.x + 20, p.y);
    await page.waitForFunction(() => window.__indexOn === false, null, {timeout: 500});
    // 4. ... and back only a full dwell after the pointer stops.
    t0 = await now(page);
    await indexShown(page);
    expect((await appeared(page, t0))[0], 'after a move, a fresh dwell').toBeGreaterThanOrEqual(DWELL - 50);
    // 7. Leaving during the wait: no late appearance.
    await page.mouse.move(p.x - 20, p.y);
    await expect.poll(() => shownNow(page)).toBe(false);
    t0 = await now(page);
    await until(page, t0 + DWELL * 0.45);
    await awayFrom(page);
    await until(page, t0 + DWELL + 600);
    expect(await appeared(page, t0), 'left during the wait').toEqual([]);
    // 8. Coming back: the whole dwell again.
    t0 = await now(page);
    await page.mouse.move(p.x, p.y);
    await indexShown(page);
    expect((await appeared(page, t0))[0], 're-entered').toBeGreaterThanOrEqual(DWELL);
    await awayFrom(page);
    // 3. Moving all the time for 3s: never.
    t0 = await now(page);
    for (let step = 0; step < 30; step += 1) {
      await page.mouse.move(p.win.left + 20 + (step % 10) * 20, p.y);
      await until(page, t0 + (step + 1) * 100);
    }
    expect(await appeared(page, t0), 'moving continuously').toEqual([]);
    // 5. Jitter of 3px at rest, through three quarters of the dwell: the
    // dwell still completes.
    await awayFrom(page);
    t0 = await now(page);
    await page.mouse.move(p.x, p.y);
    for (let step = 1; step <= 6; step += 1) {
      await until(page, t0 + step * DWELL * 0.125);
      await page.mouse.move(p.x + (step % 2 ? 3 : 0), p.y + (step % 2 ? 0 : 2));
    }
    await indexShown(page);
    at = await appeared(page, t0);
    expect(at[0], 'jitter is still a rest').toBeLessThan(DWELL + 600);
  });

  test(`using the ${label} navigator never brings the index up: repeated edge clicks both ways, First and Last, an end snap and a quick reversal; only a fresh rest after the use does`, async ({page}) => {
    await openLogged(page, view);
    const strip = `#view-${view} .dp-${view}-tabs`;
    // 9 and 11. The right edge clicked again and again at one point (the
    // window sliding beneath the still pointer), well inside a dwell apart: no index.
    await page.evaluate(sel => document.querySelector(`${sel} [data-tab]`).click(), strip);
    await settled(page);
    let p = await navigatorPoint(page, view, 'right');
    await page.mouse.move(p.x, p.y);
    let t0 = await now(page);
    let lastUse = t0;
    for (let click = 0; click < 5; click += 1) {
      lastUse = await now(page);
      await page.mouse.click(p.x, p.y);
      await until(page, t0 + (click + 1) * DWELL * 0.35);
    }
    expect(await appeared(page, t0), 'right-edge clicks').toEqual([]);
    // 15. Then still: the index only a full dwell after the last use (which,
    // at the list's end, moves nothing).
    await settled(page);
    let rest = lastUse;
    await indexShown(page);
    expect((await appeared(page, rest))[0], 'after the clicks settled').toBeGreaterThanOrEqual(DWELL - 50);
    // 10. The left edge, the same way.
    await awayFrom(page);
    await page.evaluate(sel => document.querySelector(`${sel} [data-tab]:last-child`).click(), strip);
    await settled(page);
    p = await navigatorPoint(page, view, 'left');
    await page.mouse.move(p.x, p.y);
    t0 = await now(page);
    for (let click = 0; click < 5; click += 1) {
      await page.mouse.click(p.x, p.y);
      await until(page, t0 + (click + 1) * DWELL * 0.35);
    }
    expect(await appeared(page, t0), 'left-edge clicks').toEqual([]);
    // 12. First and Last: the index goes at the press and returns only a
    // full dwell after the window has settled.
    for (const end of ['last', 'first']) {
      await awayFrom(page);
      const action = page.locator(`#view-${view} .dp-section-nav-end`)[end === 'first' ? 'first' : 'last']();
      if (!(await action.isVisible())) continue;
      const box = await action.boundingBox();
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await indexShown(page);
      t0 = await now(page);
      await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
      await page.waitForFunction(() => window.__indexOn === false, null, {timeout: 500});
      await settled(page);
      rest = await now(page);
      await indexShown(page);
      expect((await appeared(page, rest))[0], `${end}: a fresh dwell after it settled`).toBeGreaterThanOrEqual(DWELL - 50);
    }
    // 13. An end section snapped to where it is shown: no immediate return.
    await awayFrom(page);
    await page.evaluate(sel => document.querySelector(`${sel} [data-tab]`).click(), strip);
    await settled(page);
    const ids = await page.locator(`${strip} [data-tab]`).evaluateAll(tabs => tabs.map(tab => tab.dataset.tab));
    await page.evaluate(([sel, id]) => document.querySelector(`${sel} [data-tab="${id}"]`).click(), [strip, ids.at(-2)]);
    await settled(page);
    const lastTab = await page.evaluate(sel => {
      const s = document.querySelector(sel), b = s.getBoundingClientRect();
      const inset = (/inset\(([^)]+)\)/.exec(getComputedStyle(s).clipPath)?.[1] || '0').split(/\s+/).map(parseFloat);
      const [top, right = top] = inset;
      const r = s.querySelector('[data-tab]:last-child').getBoundingClientRect();
      return {x: (r.left + Math.min(r.right, b.right - right)) / 2, y: (r.top + r.bottom) / 2};
    }, strip);
    await page.mouse.move(lastTab.x, lastTab.y);
    t0 = await now(page);
    await page.mouse.click(lastTab.x, lastTab.y);
    await expect(page.locator(`${strip} [aria-selected="true"]`)).toHaveAttribute('data-tab', ids.at(-1));
    await until(page, t0 + DWELL * 0.75);
    expect(await appeared(page, t0), 'no index straight after the snap').toEqual([]);
    // 14. A quick reversal (right edge, then left edge, 150ms apart): no
    // stale index; it comes only after a full rest.
    await awayFrom(page);
    await page.evaluate(([sel, id]) => document.querySelector(`${sel} [data-tab="${id}"]`).click(), [strip, ids[2]]);
    await settled(page);
    const right = await navigatorPoint(page, view, 'right');
    const left = await navigatorPoint(page, view, 'left');
    t0 = await now(page);
    await page.mouse.click(right.x, right.y);
    await until(page, t0 + 150);
    await page.mouse.click(left.x, left.y);
    await settled(page);
    rest = await now(page);
    await indexShown(page);
    expect(await appeared(page, t0), 'nothing before the rest').toEqual([(await appeared(page, rest))[0] + (rest - t0)]);
    expect((await appeared(page, rest))[0], 'a full rest after the reversal').toBeGreaterThanOrEqual(DWELL - 50);
  });
}

/* A window slide still running when fonts finish loading (the navigator
 * re-measures, replacing its fit): the slide's own completion still clears
 * the busy mark and says the navigator is ready, so a still pointer gets the
 * index a full dwell after the slide settled -- never a stuck wait. */
for (const {view, label} of PAGES) {
  test(`a ${label} window slide interrupted by a font re-measure still clears its busy mark, and a still pointer gets the index a full dwell after it settled`, async ({page}) => {
    await openLogged(page, view);
    const strip = `#view-${view} .dp-${view}-tabs`;
    await page.evaluate(sel => document.querySelector(`${sel} [data-tab]`).click(), strip);
    await settled(page);
    const p = await navigatorPoint(page, view, 'right');
    // The instant the page settles -- no slide surface and no busy mark --
    // stamped in the page on the index log's clock (a later read from here
    // would add the runner's own latency to the dwell measured from it).
    await page.evaluate(() => {
      window.__settledAt = null;
      const check = () => {
        const settled = !document.querySelector('.dp-motion-surface') && !document.querySelector('[data-tooltip-busy]');
        if (!settled) window.__settledAt = null;
        else if (window.__settledAt === null) window.__settledAt = performance.now();
      };
      new MutationObserver(check).observe(document.body, {subtree: true, attributes: true, attributeFilter: ['class', 'data-tooltip-busy']});
    });
    await page.mouse.move(p.x, p.y);
    await page.mouse.click(p.x, p.y);
    // The click's slide is running; the fonts finish loading now.
    const running = await page.evaluate(() => {
      const busy = !!document.querySelector('[data-tooltip-busy]');
      document.fonts.dispatchEvent(new Event('loadingdone'));
      return busy;
    });
    expect(running, 'the window was sliding when the fonts re-measured').toBe(true);
    await page.waitForFunction(() => !document.querySelector('.dp-motion-surface'));
    await page.waitForFunction(() => !document.querySelector('[data-tooltip-busy]'), null, {timeout: 3000});
    const rest = await page.evaluate(() => window.__settledAt);
    expect(rest, 'the settle was stamped').not.toBeNull();
    await indexShown(page);
    const at = (await appeared(page, rest))[0];
    expect(at, 'a full dwell after the slide settled').toBeGreaterThanOrEqual(DWELL - 50);
    expect(at, 'and no later').toBeLessThan(DWELL + 600);
  });
}

test('keyboard focus brings the index promptly and Escape dismisses it; the index rests on the same policy on both pages', async ({page}) => {
  for (const {view} of PAGES) {
    await openLogged(page, view);
    await expect(page.locator(`#view-${view} .dp-section-nav-control`)).toHaveAttribute('data-tooltip-dwell', '1000');
    const selected = page.locator(`#view-${view} .dp-${view}-tabs [aria-selected="true"]`);
    await selected.focus();
    const t0 = await now(page);
    await page.keyboard.press('ArrowRight');
    await indexShown(page);
    expect((await appeared(page, t0))[0], `${view}: keyboard is prompt`).toBeLessThan(500);
    await page.keyboard.press('Escape');
    await page.waitForFunction(() => window.__indexOn === false, null, {timeout: 500});
    await page.evaluate(() => document.activeElement.blur());
  }
});
