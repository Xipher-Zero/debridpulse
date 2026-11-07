const { test, expect } = require('@playwright/test');

/* The compact Settings and Help navigators' Previous and Next follow the
 * selected section, never the window as it is drawn: with the first section
 * selected Previous is gone and the window takes its slot; with the last,
 * Next likewise; otherwise each is there -- even where the window shows the
 * list's first or last section -- over the strip's clipped end, never over a
 * part of a section the window shows. Each press selects the section one
 * before or after the selected one. The control keeps its place and size
 * whichever section is selected. Read from the rendered geometry, the clip,
 * the selection and hit-testing alone -- never from data-nav-at. */

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

/* The travel contract for one drawn state; returns its problems. */
function problems(s, at) {
  const out = [];
  if (!s.compact) {
    if (s.ends.some(end => end.shown)) out.push(`${at}: Previous/Next on the complete strip`);
    return out;
  }
  const [previous, next] = s.ends;
  const atFirst = s.selected === 0, atLast = s.selected === s.tabs.length - 1;
  if (previous.shown === atFirst) out.push(`${at}: Previous ${previous.shown ? 'shown' : 'gone'} with section ${s.selected + 1} of ${s.tabs.length} selected`);
  if (next.shown === atLast) out.push(`${at}: Next ${next.shown ? 'shown' : 'gone'} with section ${s.selected + 1} of ${s.tabs.length} selected`);
  for (const [name, end] of [['Previous', previous], ['Next', next]]) if (end.over.length) out.push(`${at}: ${name} over ${end.over.map(k => s.names[k])}`);
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
  test(`${name}: at every compact width, walking every section both ways, Previous and Next follow the selection -- gone only at the first or last section, never over a section the window shows -- and the control never moves`, async ({page}) => {
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

  test(`${name}: with the first section selected Previous is gone; choosing its neighbour -- the first section still shown -- brings Previous back, usable; the same for Next at the last section`, async ({page}) => {
    await open(page, name, 940);
    const count = (await state(page)).tabs.length;
    await choose(page, 0);
    const before = await state(page);
    expect(before.ends[0].shown).toBe(false);
    expect(before.tabs[0].whole).toBe(true);
    await choose(page, 1);
    const after = await state(page);
    expect(problems(after, 'second chosen')).toEqual([]);
    expect(after.tabs[0].shown, 'the first section is still in the window').toBeGreaterThan(0);
    expect(after.ends[0].shown, 'Previous is back: the selection has a section before it').toBe(true);
    expect(after.control).toEqual(before.control);
    await page.locator('.view.active .dp-section-nav-end').first().click();
    await settle(page);
    expect((await state(page)).selected, 'Previous selects the section before').toBe(0);

    await choose(page, count - 1);
    const end = await state(page);
    expect(end.ends[1].shown).toBe(false);
    await choose(page, count - 2);
    const back = await state(page);
    expect(problems(back, 'second to last chosen')).toEqual([]);
    expect(back.tabs.at(-1).shown, 'the last section is still in the window').toBeGreaterThan(0);
    expect(back.ends[1].shown, 'Next is back: the selection has a section after it').toBe(true);
    expect(back.control).toEqual(before.control);
    await page.locator('.view.active .dp-section-nav-end').last().click();
    await settle(page);
    expect((await state(page)).selected, 'Next selects the section after').toBe(count - 1);
  });

  test(`${name}: repeated edge clicks at one point both ways, a quick reversal, Previous and Next pressed through every section, and a font re-measure under a slide leave no stale action, no overlap and no busy mark`, async ({page}) => {
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
    // Previous and Next themselves: each press one section, to the end and back.
    for (const which of ['next', 'previous']) {
      const end = page.locator('.view.active .dp-section-nav-end').nth(which === 'previous' ? 0 : 1);
      while (await end.isVisible()) {
        const was = (await state(page)).selected;
        await end.click();
        await settle(page);
        s = await state(page);
        report.push(...problems(s, `${which} action from #${was}`));
        expect(s.selected, `${which} from #${was}`).toBe(was + (which === 'next' ? 1 : -1));
      }
      expect(s.selected).toBe(which === 'previous' ? 0 : count - 1);
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

test('the complete strip has no Previous or Next; the handheld header has the same compact navigator with them, on both pages', async ({page}) => {
  for (const name of Object.keys(PAGES)) {
    await open(page, name, 1920);
    const wide = await state(page);
    expect(wide.compact, name).toBe(false);
    expect(wide.ends.every(end => !end.shown), name).toBe(true);
    // 1.0.14: below 780px the header is one row with the compact navigator (settings-help-mobile-navigator.spec.js).
    await resize(page, 779);
    const handheld = await state(page);
    expect(handheld.compact, `${name} 779px`).toBe(true);
    expect(handheld.ends.some(end => end.shown), `${name} 779px`).toBe(true);
  }
});

test('with fallback fonts the Settings and Help travel contract holds at the 780px floor and at 1020px', async ({page}) => {
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
