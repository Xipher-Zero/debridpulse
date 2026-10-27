const { test, expect } = require('@playwright/test');

/* The compact Settings and Help navigators' First and Last follow the window
 * as it is drawn, never which section is selected: where the window shows the
 * list's first section whole, First is gone and the window takes its slot;
 * where it shows the last whole, Last likewise; otherwise each is there, over
 * the strip's clipped end, never over a part of a section the window shows.
 * The control keeps its place and size whichever section is selected and
 * whatever ends are shown. Read from the rendered geometry, the clip and
 * hit-testing alone -- never from data-nav-at. */

const TARGET = 24;
const PAGES = {
  settings: {open: '#sidebar .nav-item[data-view="settings"]:not([data-title])', view: '#view-settings'},
  help: {open: '#sidebar .nav-item[data-view="help"]', view: '#view-help'},
};

const settle = async page => {
  await page.waitForFunction(() => !document.querySelector('.view.active .dp-section-nav .dp-motion-surface') && document.getAnimations()
    .every(animation => animation.playState !== 'running' || animation.effect?.getTiming().iterations === Infinity));
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
};

async function open(page, name, width, {fallbackFonts = false} = {}) {
  if (fallbackFonts) await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.setViewportSize({width, height: 900});
  await page.goto('/');
  await page.locator(PAGES[name].open).first().click();
  await expect(page.locator(`${PAGES[name].view}.active .dp-section-nav [role="tab"]`).first()).toBeAttached();
  await page.evaluate(() => document.fonts.ready);
  await settle(page);
}

async function resize(page, width) {
  await page.setViewportSize({width, height: 900});
  await page.waitForFunction(() => document.getElementById('sidebar').getAnimations().every(animation => animation.playState !== 'running'));
  await settle(page);
}

/* The navigator as drawn: its window (the strip less its clip), each
 * section's visible part, the ends shown, and what the pointer hits across
 * every visible part of every section. */
const state = page => page.evaluate(TARGET => {
  const nav = document.querySelector('.view.active .dp-section-nav');
  const compact = nav.parentElement.dataset.presentation === 'compact';
  const control = nav.querySelector('.dp-section-nav-control'), strip = nav.querySelector('.dp-section-nav-strip');
  const box = strip.getBoundingClientRect();
  const inset = (/inset\(([^)]+)\)/.exec(getComputedStyle(strip).clipPath)?.[1] || '0').split(/\s+/).map(parseFloat);
  const [top, right = top, , left = right] = inset;
  const win = {left: box.left + left, right: box.right - right};
  const tabNodes = [...strip.querySelectorAll('[role="tab"]')];
  const tabs = tabNodes.map((node, k) => {
    const r = node.getBoundingClientRect();
    const from = Math.max(r.left, win.left), to = Math.min(r.right, win.right);
    const shown = getComputedStyle(node).opacity === '0' ? 0 : Math.max(0, to - from);
    // Hit-test the visible part: both ends (inset 2px) and the middle.
    const hits = shown >= TARGET ? [from + 2, (from + to) / 2, to - 2].map(x =>
      document.elementFromPoint(x, (r.top + r.bottom) / 2)?.closest('[role="tab"]') === node) : [];
    return {k, left: r.left, right: r.right, width: r.width, shown, whole: shown >= r.width - 0.5, hits};
  });
  const ends = [...nav.querySelectorAll('.dp-section-nav-end')].map(node => {
    const shown = getComputedStyle(node).display !== 'none';
    const r = node.getBoundingClientRect();
    // Over a visible part of a section?
    const over = shown ? tabs.filter(t => t.shown > 0.5 && Math.min(r.right, t.right, win.right) - Math.max(r.left, t.left, win.left) > 0.5).map(t => t.k) : [];
    return {shown, left: r.left, right: r.right, over};
  });
  const c = control.getBoundingClientRect();
  return {compact, selected: tabNodes.findIndex(node => node.getAttribute('aria-selected') === 'true'), tabs, ends,
    control: [c.left, c.top, c.width, c.height].map(v => Math.round(v * 10) / 10), busy: control.hasAttribute('data-tooltip-busy'),
    names: tabNodes.map(node => node.textContent.trim().replace(/\s+/g, ' '))};
}, TARGET);

/* The endpoint contract for one drawn state; returns its problems. */
function problems(s, at) {
  const out = [];
  if (!s.compact) {
    if (s.ends.some(end => end.shown)) out.push(`${at}: First/Last on the complete strip`);
    return out;
  }
  const [first, last] = s.ends;
  const head = s.tabs[0], tail = s.tabs.at(-1);
  if (first.shown === head.whole) out.push(`${at}: First ${first.shown ? 'shown' : 'gone'} with the first section ${head.whole ? 'whole' : 'not whole'}`);
  if (last.shown === tail.whole) out.push(`${at}: Last ${last.shown ? 'shown' : 'gone'} with the last section ${tail.whole ? 'whole' : 'not whole'}`);
  for (const [name, end] of [['First', first], ['Last', last]]) if (end.over.length) out.push(`${at}: ${name} over ${end.over.map(k => s.names[k])}`);
  for (const t of s.tabs) if (t.hits.some(hit => !hit)) out.push(`${at}: ${s.names[t.k]} not its own target where it shows`);
  if (!s.tabs[s.selected].whole) out.push(`${at}: the selection not whole`);
  if (s.busy) out.push(`${at}: busy after settling`);
  return out;
}

/* Choose a section with the pointer where the window shows it, else with the
 * keyboard from the selected tab. Returns the pointer position used. */
async function choose(page, k) {
  const s = await state(page);
  const t = s.tabs[k];
  let point = null;
  if (t.shown >= TARGET) {
    const strip = await page.locator('.view.active .dp-section-nav-strip').boundingBox();
    const from = Math.max(t.left, s.ends[0].shown ? s.ends[0].right : strip.x), to = Math.min(t.right, s.ends[1].shown ? s.ends[1].left : strip.x + strip.width);
    point = {x: (from + to) / 2, y: strip.y + strip.height / 2};
    await page.mouse.click(point.x, point.y);
  } else {
    await page.locator('.view.active .dp-section-nav [role="tab"][aria-selected="true"]').focus();
    for (let step = s.selected; step !== k; step += Math.sign(k - s.selected)) await page.keyboard.press(k > s.selected ? 'ArrowRight' : 'ArrowLeft');
  }
  await settle(page);
  return point;
}

for (const name of Object.keys(PAGES)) {
  test(`${name}: at every compact width, walking every section both ways, First and Last follow the window drawn -- gone where its end section shows whole, never over a section it shows -- and the control never moves`, async ({page}) => {
    test.setTimeout(120_000);
    await open(page, name, 1100);
    const report = [];
    let widths = 0;
    for (const width of [780, 940, 1100, 1260]) {
      await resize(page, width);
      if (!(await state(page)).compact) continue;
      widths += 1;
      const count = (await state(page)).tabs.length;
      const order = [...Array(count).keys(), ...[...Array(count).keys()].reverse()];
      let control = null;
      for (const k of order) {
        await choose(page, k);
        const s = await state(page);
        report.push(...problems(s, `${width}px, ${s.names[k]}`));
        control ??= s.control;
        if (s.control.join() !== control.join()) report.push(`${width}px, ${s.names[k]}: control moved ${control} -> ${s.control}`);
      }
    }
    expect(widths).toBeGreaterThan(1);
    expect(report).toEqual([]);
  });

  test(`${name}: with the first section shown whole, choosing its neighbour keeps First gone, the first section whole and every section where it was; the same at the last section`, async ({page}) => {
    await open(page, name, 940);
    const count = (await state(page)).tabs.length;
    await choose(page, 0);
    const before = await state(page);
    expect(before.ends[0].shown).toBe(false);
    expect(before.tabs[0].whole).toBe(true);
    await choose(page, 1);
    const after = await state(page);
    expect(problems(after, 'second chosen')).toEqual([]);
    expect(after.ends[0].shown, 'First stays gone: the first section still shows whole').toBe(false);
    expect(after.tabs.map(t => Math.round(t.left)), 'no section moved').toEqual(before.tabs.map(t => Math.round(t.left)));
    expect(after.control).toEqual(before.control);

    await choose(page, count - 1);
    const end = await state(page);
    expect(end.ends[1].shown).toBe(false);
    await choose(page, count - 2);
    const back = await state(page);
    expect(problems(back, 'second to last chosen')).toEqual([]);
    expect(back.ends[1].shown, 'Last stays gone: the last section still shows whole').toBe(false);
    expect(back.tabs.map(t => Math.round(t.left))).toEqual(end.tabs.map(t => Math.round(t.left)));
    expect(back.control).toEqual(before.control);
  });

  test(`${name}: repeated edge clicks at one point both ways, a quick reversal, First and Last, and a font re-measure under a slide leave no stale action, no overlap and no busy mark`, async ({page}) => {
    await open(page, name, 860);
    const count = (await state(page)).tabs.length;
    await choose(page, 0);
    // The rightmost section shown, clicked again and again at one point.
    const report = [];
    let s = await state(page);
    const edge = s.tabs.filter(t => t.shown >= TARGET).at(-1);
    const point = await choose(page, edge.k);
    for (let n = 0; n < count && point; n += 1) {
      await page.mouse.click(point.x, point.y);
      await settle(page);
      report.push(...problems(await state(page), `forward click ${n + 1}`));
    }
    s = await state(page);
    const back = s.tabs.filter(t => t.shown >= TARGET)[0];
    const backPoint = await choose(page, back.k);
    for (let n = 0; n < count && backPoint; n += 1) {
      await page.mouse.click(backPoint.x, backPoint.y);
      await settle(page);
      report.push(...problems(await state(page), `back click ${n + 1}`));
    }
    // A quick reversal: forward and back before the first slide is over.
    await choose(page, Math.floor(count / 2));
    s = await state(page);
    const right = s.tabs.filter(t => t.shown >= TARGET).at(-1), left = s.tabs.filter(t => t.shown >= TARGET)[0];
    const strip = await page.locator('.view.active .dp-section-nav-strip').boundingBox();
    const y = strip.y + strip.height / 2;
    await page.mouse.click((Math.max(right.left, strip.x) + Math.min(right.right, strip.x + strip.width)) / 2 - 8, y);
    await page.mouse.click((Math.max(left.left, strip.x) + Math.min(left.right, strip.x + strip.width)) / 2 + 8, y);
    await settle(page);
    report.push(...problems(await state(page), 'quick reversal'));
    // First and Last themselves.
    for (const which of ['last', 'first']) {
      const end = page.locator('.view.active .dp-section-nav-end').nth(which === 'first' ? 0 : 1);
      if (await end.isVisible()) { await end.click(); await settle(page); }
      s = await state(page);
      report.push(...problems(s, `${which} action`));
      expect(s.selected).toBe(which === 'first' ? 0 : count - 1);
    }
    // A font re-measure while a slide runs.
    await choose(page, 1);
    s = await state(page);
    const far = s.tabs.filter(t => t.shown >= TARGET).at(-1);
    await page.mouse.click((Math.max(far.left, strip.x) + Math.min(far.right, strip.x + strip.width)) / 2, y);
    await page.evaluate(() => document.fonts.dispatchEvent(new Event('loadingdone')));
    await settle(page);
    report.push(...problems(await state(page), 'font re-measure under a slide'));
    expect(report).toEqual([]);
  });
}

test('the complete strip and the handheld header have no First or Last, on both pages', async ({page}) => {
  for (const name of Object.keys(PAGES)) {
    await open(page, name, 1920);
    const wide = await state(page);
    expect(wide.compact, name).toBe(false);
    expect(wide.ends.every(end => !end.shown), name).toBe(true);
    await resize(page, 779);
    expect((await state(page)).ends.every(end => !end.shown), name).toBe(true);
  }
});

test('with fallback fonts the Settings and Help endpoint contract holds at the 780px floor and at 1020px', async ({page}) => {
  for (const name of Object.keys(PAGES)) {
    await open(page, name, 780, {fallbackFonts: true});
    const report = [];
    for (const width of [780, 1020]) {
      await resize(page, width);
      if (!(await state(page)).compact) continue;
      const count = (await state(page)).tabs.length;
      for (const k of [...Array(count).keys(), ...[...Array(count).keys()].reverse()]) {
        await choose(page, k);
        report.push(...problems(await state(page), `${name} ${width}px #${k}`));
      }
    }
    expect(report).toEqual([]);
  }
});
