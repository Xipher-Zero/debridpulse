const { test, expect } = require('@playwright/test');

/* The Settings master header's section navigation, desktop and tablet
 * (780px and wider): one row. The complete strip whenever the Tuning Deck
 * copy, the 16px row gap and the strip at their own widths fit the header
 * with the documented 4px to spare; otherwise the compact navigator -- First,
 * a window onto the same strip, Last. Either way the navigation keeps the
 * strip's own place: centred in the header (half the row gap toward the
 * copy's side) while the copy fits in the half beside it, otherwise right
 * after the copy, the row gap apart. The window holds as many sections as
 * fit, at least three wherever three can, and is a bounded sliding window:
 * at the list's first or last section it starts or ends there, between them
 * the selection is as centred as the cuts allow, an edge where sections
 * continue cuts one and fades outwards, and it slides (DPMotion) only when a
 * choice moves it. A part of a section too small to be a position is not
 * shown; at the 780px floor the first or last section may show with its
 * neighbour alone, both whole (the tabs giving up some side padding if they
 * must). Below 780px the handheld header keeps its wrapping strip.
 *
 * Fit, placement and feasibility are computed here from the rendered tabs'
 * and copy's own widths -- whatever fonts this browser drew -- never read
 * from the page's decision. */

const SPARE = 4;
const TARGET = 24;   // the least part of a section that is a position

// Two frames drawn, the navigator's window not sliding (DPMotion marks the
// strip from the choice until its slide is over) and no finite animation
// running: hit-testing and geometry are the page's own. (The panel's own
// mark is not waited on: on unchanged HEAD too, a rapid choice can leave it
// set after its slide has ended.)
const settle = async page => {
  await page.waitForFunction(() => !document.querySelector('#view-settings .dp-settings-tabs.dp-motion-surface') && document.getAnimations()
    .every(animation => animation.playState !== 'running' || animation.effect?.getTiming().iterations === Infinity));
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
};

async function resize(page, width) {
  await page.setViewportSize({width, height: 1000});
  await page.waitForFunction(() =>
    document.getElementById('sidebar').getAnimations().every(animation => animation.playState !== 'running'));
  await settle(page);
}

async function openSettings(page, width, {fallbackFonts = false} = {}) {
  if (fallbackFonts) {
    await page.route('https://fonts.googleapis.com/**', route =>
      route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  }
  /* Every frame in which the compact navigator's window is sliding (DPMotion
   * marks the strip while it runs): its direction and step, and where the
   * navigator's control and the header stand. */
  await page.addInitScript(() => {
    window.__dpNav = [];
    const tick = () => {
      const strip = document.querySelector('#view-settings .dp-settings-tabs.dp-motion-surface');
      if (strip) {
        const control = strip.parentElement.getBoundingClientRect();
        const header = strip.closest('.dp-settings-master-header').getBoundingClientRect();
        window.__dpNav.push({direction: strip.dataset.dpMotionDirection, step: strip.dataset.dpMotionStep,
          control: [control.left, control.top, control.width, control.height], header: [header.left, header.top, header.width, header.height],
          moving: document.getAnimations().some(a => a.effect?.target === strip && /\(root\)$/.test(a.effect?.pseudoElement || ''))});
      }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });
  await page.setViewportSize({width, height: 1000});
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]:not([data-title])').click();
  await expect(page.locator('#view-settings .dp-settings-tabs [data-tab]').first()).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await settle(page);
}

const selected = page => page.locator('#view-settings .dp-settings-tabs [aria-selected="true"]');

/* Everything about the header at this width, from geometry alone. `frame`
 * is the strip's own padding and border in the complete presentation. */
function header(page, frame = 0) {
  return page.evaluate(({frame, TARGET, SPARE}) => {
    const rect = node => node.getBoundingClientRect();
    const root = document.getElementById('view-settings');
    const headerNode = root.querySelector('.dp-settings-master-header');
    const style = getComputedStyle(headerNode);
    const box = rect(headerNode);
    const inner = {left: box.left + parseFloat(style.paddingLeft), right: box.right - parseFloat(style.paddingRight)};
    const gap = parseFloat(style.columnGap);
    const parts = node => [...node.children].reduce((sum, kid) => sum + rect(kid).width, 0)
      + (parseFloat(getComputedStyle(node).columnGap) || 0) * (node.children.length - 1);
    const copyNode = root.querySelector('.dp-settings-header-copy');
    const copy = rect(copyNode);
    const copyOwn = parts(copyNode);
    const strip = root.querySelector('.dp-settings-tabs');
    const s = rect(strip);
    const tabNodes = [...strip.querySelectorAll('[data-tab]')];
    const tabs = tabNodes.map(rect);
    const stripNatural = tabs.reduce((sum, tab) => sum + tab.width, 0);
    const ends = [...root.querySelectorAll('.dp-settings-nav-end')];
    const shownEnds = ends.filter(node => getComputedStyle(node).display !== 'none');
    const controlNode = strip.parentElement.matches('.dp-settings-nav-control') ? strip.parentElement : strip;
    const control = rect(controlNode);
    const index = tabNodes.findIndex(tab => tab.getAttribute('aria-selected') === 'true');
    const c = tabs[index];
    const label = tabNodes[index].querySelector('.dp-settings-tab-label');
    // What each section shows of itself in the window (nothing when hidden).
    const shown = tabs.map((t, k) => getComputedStyle(tabNodes[k]).opacity === '0' ? 0
      : Math.max(0, Math.min(t.right, s.right) - Math.max(t.left, s.left)));
    // Parts in the window that are not shown (a suppressed sliver).
    const hidden = tabs.map((t, k) => getComputedStyle(tabNodes[k]).opacity === '0'
      ? Math.max(0, Math.min(t.right, s.right) - Math.max(t.left, s.left)) : 0);
    const centred = (inner.right - inner.left - control.width - gap) / 2 >= copyOwn;
    const fadeOf = side => {
      const tab = tabNodes.find(node => node.dataset.navCut === side);
      return tab && getComputedStyle(tab).maskImage !== 'none'
        ? parseFloat(tab.style.getPropertyValue('--dp-settings-nav-cut-fade')) || 0 : 0;
    };
    return {
      width: innerWidth, height: box.height, copyLeft: copy.left - box.left, copyTop: copy.top - box.top,
      level: Math.abs((control.top + control.bottom) / 2 - (copy.top + copy.bottom) / 2),
      inside: control.left >= copy.left + copyOwn + gap - 0.5 && control.right <= inner.right + 0.5
        && control.top >= box.top && control.bottom <= box.bottom,
      // The strip's own place: centred (plus half the gap), else right after the copy.
      centred,
      placeOffset: centred ? (control.left + control.right) / 2 - ((inner.left + inner.right) / 2 + gap / 2)
        : control.left - (copy.left + copyOwn + gap),
      // >= 0: the complete strip fits beside the copy with the spare.
      side: inner.right - inner.left - (copyOwn + gap + stripNatural + frame + SPARE),
      compact: shownEnds.length > 0,
      endWidths: shownEnds.map(node => rect(node).width),
      allTabsInside: tabs.every(t => t.width > 0 && t.left >= s.left - 0.5 && t.right <= s.right + 0.5 && Math.abs(t.top - tabs[0].top) < 1),
      scrolls: strip.scrollWidth > strip.clientWidth + 1,
      index, order: tabNodes.map(tab => tab.dataset.tab),
      currentWhole: c.left >= s.left - 0.5 && c.right <= s.right + 0.5,
      labelWhole: label.scrollWidth <= label.clientWidth + 0.5 && rect(label).left >= c.left && rect(label).right <= c.right,
      // The window: what each section shows of itself, the positions (a part of
      // TARGET or more, or the selection), and the window's own width.
      shown, hidden, widths: tabs.map(t => t.width), window: s.width,
      padding: tabNodes.map(tab => parseFloat(getComputedStyle(tab).paddingLeft)),
      labels: tabNodes.map(tab => tab.textContent.trim()),
      glyphs: tabNodes.map(tab => tab.querySelectorAll('svg').length),
      fontSizes: tabNodes.map(tab => getComputedStyle(tab.querySelector('.dp-settings-tab-label')).fontSize),
      labelsWhole: tabNodes.map(tab => { const l = tab.querySelector('.dp-settings-tab-label'); return l.scrollWidth <= l.clientWidth + 0.5; }),
      cuts: tabNodes.map(tab => tab.dataset.navCut || ''),
      ends: ends.map(node => ({width: rect(node).width, height: rect(node).height, visibility: getComputedStyle(node).visibility})),
      positions: shown.filter((part, k) => part >= TARGET || k === index).length,
      atStart: c.left - s.left, atEnd: s.right - c.right, edge: tabs[0].left - (s.left - strip.scrollLeft),
      // The outward fade of the section each edge cuts (none where the list ends).
      fadeStart: fadeOf('start'), fadeEnd: fadeOf('end'),
      mask: tabNodes.filter(tab => tab.dataset.navCut).map(tab => getComputedStyle(tab).maskImage).join(' '),
      unmaskedSelection: getComputedStyle(tabNodes[index]).maskImage === 'none',
      control: {left: control.left, right: control.right, width: control.width},
    };
  }, {frame, TARGET, SPARE});
}

/* The one-row contract at this width. Returns the problems. */
function problems(h, reference) {
  const out = [];
  const borderline = Math.abs(h.side) < 0.5;
  if (h.level > 1) out.push('navigation not on the copy\'s row');
  if (!h.inside) out.push('navigation outside its room');
  if (Math.abs(h.height - reference.height) > 0.5) out.push(`header height ${h.height}`);
  if (Math.abs(h.copyLeft - reference.copyLeft) > 0.5 || Math.abs(h.copyTop - reference.copyTop) > 0.5) out.push('identity moved');
  if (Math.abs(h.placeOffset) > 0.5) out.push(`off the strip's place (${h.centred ? 'centred' : 'after the copy'}, ${h.placeOffset.toFixed(1)})`);
  if (!borderline && h.side >= 0 && h.compact) out.push('compact although the strip fits');
  if (!borderline && h.side < 0 && !h.compact) out.push('complete strip although it does not fit');
  if (!h.compact && (!h.allTabsInside || h.scrolls)) out.push('complete strip clipped or scrolling');
  if (h.compact) {
    if (!h.currentWhole) out.push('selected section not whole');
    if (!h.labelWhole) out.push('selected label clipped');
    if (!h.unmaskedSelection) out.push('selected section faded');
    if (h.endWidths.some(width => width < TARGET)) out.push('travel action too small');
    if (h.positions < 2) out.push(`${h.positions} position(s)`);
    if (h.shown.some(part => part > 0.5 && part < TARGET)) out.push('a sliver shown');
  }
  return out;
}

for (const fonts of ['web fonts', 'fallback fonts']) {
  test(`the Settings header is one row from 1920px to 780px with ${fonts}: the strip while it fits, else the compact navigator, both in the strip's own place`, async ({page}) => {
    await openSettings(page, 1920, {fallbackFonts: fonts === 'fallback fonts'});
    const first = await header(page);
    expect(first.compact).toBe(false);
    const frame = first.control.width - first.widths.reduce((sum, width) => sum + width, 0);   // the strip's padding and border
    expect(frame).toBeGreaterThan(0);
    expect(frame).toBeLessThan(12);
    const reference = await header(page, frame);
    // Every width's problems, reported together (an empty report passes).
    const report = {};
    const record = (width, found) => { if (found.length) report[`${width}px`] = found; };
    record(1920, problems(reference, reference));
    for (const width of [1800, 1700, 1600, 1560, 1559, 1440, 1300, 1200, 1100, 1024, 901, 900, 899, 850, 800, 781, 780]) {
      await resize(page, width);
      record(width, problems(await header(page, frame), reference));
    }
    expect(report).toEqual({});

    /* The threshold, from the measured parts: the header grows as the window
     * does within one sidebar width (full sidebar from 1560px, rail below). */
    const thresholds = [];
    for (const [probe, low, high] of [[1920, 1560, 1920], [1300, 901, 1559]]) {
      await resize(page, probe);
      const width = Math.ceil(probe - (await header(page, frame)).side);
      // (Outside its regime: that sidebar width is one presentation throughout,
      // which the widths above already checked.)
      if (width > low && width <= high) thresholds.push(width);
    }
    expect(thresholds.length).toBeGreaterThan(0);
    for (const threshold of thresholds) {
      for (const [width, compact] of [[threshold + 1, false], [threshold - 1, true], [threshold - 2, true],
        [threshold + 2, false], [threshold - 1, true], [threshold + 1, false]]) {
        await resize(page, width);
        const h = await header(page, frame);
        expect(problems(h, reference), `${width}px`).toEqual([]);
        expect(h.compact, `${width}px`).toBe(compact);
        await settle(page);
        expect((await header(page, frame)).compact, `${width}px, a frame later`).toBe(compact);
      }
      await resize(page, threshold);
      const at = await header(page, frame);
      expect(problems(at, reference), `${threshold}px`).toEqual([]);
      test.info().annotations.push({type: `threshold (${fonts})`, description: `complete strip from ${threshold}px: ${(at.side + SPARE).toFixed(1)}px to spare there`});
    }
  });
}

/* Choose a section through the page's own controls: a click where it shows,
 * else the keyboard from the selected tab (the window never shows a section
 * the list does not reach). */
async function choose(page, id) {
  const tab = page.locator(`#view-settings .dp-settings-tabs [data-tab="${id}"]`);
  const at = await header(page);
  const k = at.order.indexOf(id);
  if (at.shown[k] >= TARGET) {
    const box = await tab.boundingBox();
    const strip = await page.locator('#view-settings .dp-settings-tabs').boundingBox();
    const x = (Math.max(box.x, strip.x) + Math.min(box.x + box.width, strip.x + strip.width)) / 2;
    await page.mouse.click(x, box.y + box.height / 2);
  } else {
    await selected(page).focus();
    const key = k > at.index ? 'ArrowRight' : 'ArrowLeft';
    for (let step = 0; step < Math.abs(k - at.index); step += 1) await page.keyboard.press(key);
  }
  await expect(selected(page)).toHaveAttribute('data-tab', id);
  await expect(page.locator(`#view-settings [data-panel="${id}"]`)).toBeVisible();
  await settle(page);
}

test('the compact window holds as many sections as fit, fewer as the header narrows, three wherever three can fit, and every one it shows opens its section', async ({page}) => {
  await openSettings(page, 1920);
  const reference = await header(page);
  const frame = reference.control.width - reference.widths.reduce((sum, width) => sum + width, 0);
  // The widest compact width on the rail: one pixel below the strip's fit.
  await resize(page, 1300);
  const threshold = Math.ceil(1300 - (await header(page, frame)).side);
  const order = reference.order;
  const picks = [0, 1, Math.floor(order.length / 2), order.length - 2, order.length - 1];
  const rows = [];
  let previous = Infinity;
  for (const width of [threshold - 1, 1300, 1200, 1100, 1000, 900, 850, 800, 780]) {
    await resize(page, width);
    const row = {width, positions: [], window: 0};
    for (const k of picks) {
      await choose(page, order[k]);
      const h = await header(page, frame);
      expect(h.compact, `${width}px`).toBe(true);
      row.window = h.window;
      row.positions.push(h.positions);
      // Three positions wherever three can fit: at the list's first or last
      // section the selection, its whole neighbour and a part of TARGET; in
      // between the selection and a part of TARGET each side.
      const w = h.widths;
      const three = k === 0 ? h.edge + w[0] + w[1] + TARGET <= h.window
        : k === order.length - 1 ? h.edge + w[k] + w[k - 1] + TARGET <= h.window
        : w[k] + 2 * TARGET <= h.window;
      if (three) expect(h.positions, `${order[k]} at ${width}px`).toBeGreaterThanOrEqual(3);
      else test.info().annotations.push({type: 'limit', description: `${order[k]} at ${width}px: a ${h.window.toFixed(0)}px window cannot hold three positions (${h.positions} shown)`});
      // No part too small to be a position is shown.
      expect(h.shown.filter(part => part > 0.5 && part < TARGET), `${order[k]} at ${width}px`).toEqual([]);
      // Every section shown as a position opens itself where it shows.
      for (const [j, part] of h.shown.entries()) {
        if (part < TARGET || j === h.index) continue;
        const hit = await page.evaluate(j => {
          const strip = document.querySelector('#view-settings .dp-settings-tabs');
          const tab = strip.querySelectorAll('[data-tab]')[j];
          const s = strip.getBoundingClientRect(), t = tab.getBoundingClientRect();
          const x = (Math.max(t.left, s.left) + Math.min(t.right, s.right)) / 2;
          return document.elementFromPoint(x, (t.top + t.bottom) / 2)?.closest('[data-tab]')?.dataset.tab;
        }, j);
        expect(hit, `${order[j]} at ${width}px`).toBe(order[j]);
      }
    }
    // Never more sections as the header narrows.
    expect(Math.max(...row.positions), `${width}px`).toBeLessThanOrEqual(previous);
    previous = Math.max(...row.positions);
    rows.push(row);
  }
  // A wide compact navigator holds more than three.
  expect(Math.max(...rows[0].positions)).toBeGreaterThan(3);
  test.info().annotations.push({type: 'capacity', description: rows.map(row => `${row.width}px ${row.window.toFixed(0)}px [${row.positions.join(',')}]`).join('; ')});
});

test('the window is bounded: it starts at the first section, ends at the last, centres between them, fades only where sections continue; First and Last only away from their end', async ({page}) => {
  await openSettings(page, 1100);
  const order = (await header(page)).order;
  const first = page.locator('#view-settings .dp-settings-nav-end[data-settings-end="first"]');
  const last = page.locator('#view-settings .dp-settings-nav-end[data-settings-end="last"]');
  await expect(page.getByRole('tablist', {name: 'Settings sections'}).getByRole('tab')).toHaveCount(order.length);

  // The first section: the window starts with it; no First; the far edge fades.
  await choose(page, order[0]);
  let h = await header(page);
  expect(h.compact).toBe(true);
  expect(Math.abs(h.atStart - h.edge)).toBeLessThanOrEqual(1);
  expect(h.fadeStart).toBe(0);
  expect(h.fadeEnd).toBeGreaterThanOrEqual(TARGET);
  expect(h.mask).toContain('linear-gradient');
  await expect(first).toHaveCSS('visibility', 'hidden');
  await expect(last).toHaveCSS('visibility', 'visible');
  await expect(last).toHaveAccessibleName(`Last section: ${await page.locator(`#view-settings [data-tab="${order.at(-1)}"]`).textContent().then(t => t.trim())}`);
  await first.evaluate(node => node.focus());
  await expect(first).not.toBeFocused();   // a hidden action takes no focus
  const control = h.control;

  // Between them: the selection whole and both edges fading.
  const middle = Math.floor(order.length / 2);
  await choose(page, order[middle]);
  h = await header(page);
  expect(h.currentWhole).toBe(true);
  expect(h.fadeStart).toBeGreaterThanOrEqual(TARGET);
  expect(h.fadeEnd).toBeGreaterThanOrEqual(TARGET);
  expect(h.atStart).toBeGreaterThan(h.edge);
  expect(h.atEnd).toBeGreaterThan(h.edge);
  await expect(first).toHaveCSS('visibility', 'visible');
  await expect(last).toHaveCSS('visibility', 'visible');

  // The last section: the window ends with it; no Last; the near edge fades.
  await last.focus();
  await page.keyboard.press('Enter');
  await expect(selected(page)).toHaveAttribute('data-tab', order.at(-1));
  await expect(page.locator(`#view-settings [data-tab="${order.at(-1)}"]`)).toBeFocused();
  await settle(page);
  h = await header(page);
  expect(Math.abs(h.atEnd - h.edge)).toBeLessThanOrEqual(1);
  expect(h.fadeEnd).toBe(0);
  expect(h.fadeStart).toBeGreaterThanOrEqual(TARGET);
  await expect(last).toHaveCSS('visibility', 'hidden');
  // The navigator does not move or resize, wherever the selection is.
  for (const key of ['left', 'right']) expect(Math.abs(h.control[key] - control[key])).toBeLessThanOrEqual(0.5);
  await first.click();
  await expect(selected(page)).toHaveAttribute('data-tab', order[0]);
  await expect(page.locator(`#view-settings [data-tab="${order[0]}"]`)).toBeFocused();
  await settle(page);

  // The keyboard walks every section in order, the selection always whole.
  for (let k = 1; k < order.length; k += 1) {
    await page.keyboard.press('ArrowRight');
    await expect(page.locator(`#view-settings [data-tab="${order[k]}"]`)).toBeFocused();
    await settle(page);
    expect((await header(page)).currentWhole, order[k]).toBe(true);
  }
  await page.keyboard.press('Home');
  await expect(page.locator(`#view-settings [data-tab="${order[0]}"]`)).toBeFocused();
  const strip = page.locator('#view-settings .dp-settings-tabs');
  await expect(strip.locator('[tabindex="0"]')).toHaveCount(1);
  await expect(strip.locator('[aria-selected="true"]')).toHaveCount(1);

  // Representative images: 1100px start, middle, end; 780px, dark and light.
  for (const [width, k] of [[1100, 0], [1100, middle], [1100, order.length - 1], [780, middle]]) {
    await resize(page, width);
    await choose(page, order[k]);
    for (const theme of ['dark', 'light']) {
      if (width === 1100 && theme === 'light') continue;
      await page.evaluate(light => document.body.classList.toggle('light', light), theme === 'light');
      await settle(page);
      await page.locator('#view-settings .dp-settings-master-header')
        .screenshot({path: `test-results/checkpoint-settings-navigator-${width}-${order[k]}-${theme}.png`});
    }
    await page.evaluate(() => document.body.classList.remove('light'));
  }
});

for (const fonts of ['web fonts', 'fallback fonts']) {
  test(`at the 780px floor with ${fonts} the last section shows whole with its whole neighbour and no sliver; nearby widths and the other sections keep three positions`, async ({page}) => {
    await openSettings(page, 1920, {fallbackFonts: fonts === 'fallback fonts'});
    const reference = await header(page);
    expect(reference.compact).toBe(false);
    const order = reference.order;
    const n = order.length;
    // The tabs' own widths and side padding, from the complete strip.
    const natural = reference.widths;
    for (const width of [780, 781, 790, 800]) {
      await resize(page, width);
      let control = null;
      for (let k = 0; k < n; k += 1) {
        await choose(page, order[k]);
        const h = await header(page);
        const at = `${order[k]} at ${width}px`;
        expect(h.compact, at).toBe(true);
        expect(problems(h, reference), at).toEqual([]);
        // The navigator does not move or resize with the selection.
        control ??= h.control;
        for (const key of ['left', 'right']) expect(Math.abs(h.control[key] - control[key]), at).toBeLessThanOrEqual(0.5);
        // Text, glyphs, type and selection are the complete strip's own.
        expect(h.labels, at).toEqual(reference.labels);
        expect(h.glyphs, at).toEqual(reference.glyphs);
        expect(h.fontSizes, at).toEqual(reference.fontSizes);
        expect(h.labelsWhole.every(Boolean), at).toBe(true);
        await expect(page.locator('#view-settings .dp-settings-tabs [tabindex="0"]')).toHaveCount(1);
        await expect(page.locator(`#view-settings .dp-settings-tabs [data-tab="${order[k]}"]`)).toHaveAttribute('tabindex', '0');
        // First and Last keep their size; each hides (its place kept) only at its own end.
        for (const end of h.ends) {
          expect(end.width, at).toBeGreaterThanOrEqual(32);
          expect(end.height, at).toBeGreaterThanOrEqual(32);
        }
        expect(h.ends.map(end => end.visibility), at).toEqual([k === 0 ? 'hidden' : 'visible', k === n - 1 ? 'hidden' : 'visible']);
        // Side padding: the complete strip's unless an end section and its
        // whole neighbour would not otherwise fit, and then at least 5px.
        const pairs = Math.max(natural[0] + natural[1], natural[n - 2] + natural[n - 1]) + h.edge;
        for (const padding of h.padding) {
          if (pairs + 0.5 <= h.window) expect(padding, at).toBe(reference.padding[0]);
          else expect(padding, at).toBeGreaterThanOrEqual(5);
        }
        if (k === 0 || k === n - 1) {
          // The end section and its neighbour whole, unfaded, at the list's end.
          const pair = k === 0 ? [0, 1] : [n - 2, n - 1];
          for (const j of pair) {
            expect(Math.abs(h.shown[j] - h.widths[j]), `${order[j]} whole, ${at}`).toBeLessThanOrEqual(0.5);
            expect(h.cuts[j], `${order[j]} unfaded, ${at}`).toBe('');
          }
          expect(Math.abs((k === 0 ? h.atStart : h.atEnd) - h.edge), at).toBeLessThanOrEqual(1);
          // A third section only where at least TARGET of it fits; none at 780px.
          const room = h.window - h.edge - h.widths[pair[0]] - h.widths[pair[1]];
          expect(h.positions, at).toBe(room >= TARGET ? 3 : 2);
          if (room < TARGET) {
            const sliver = k === 0 ? 2 : n - 3;
            expect(h.shown[sliver], at).toBe(0);
            if (h.hidden[sliver] > 0) {
              // Not shown, and not a target for its section either.
              const hit = await page.evaluate(j => {
                const strip = document.querySelector('#view-settings .dp-settings-tabs');
                const t = strip.querySelectorAll('[data-tab]')[j].getBoundingClientRect(), s = strip.getBoundingClientRect();
                const x = (Math.max(t.left, s.left) + Math.min(t.right, s.right)) / 2;
                return document.elementFromPoint(x, (t.top + t.bottom) / 2)?.closest('[data-tab]')?.dataset.tab ?? null;
              }, sliver);
              expect(hit, at).not.toBe(order[sliver]);
            }
          }
        } else {
          expect(h.positions, at).toBeGreaterThanOrEqual(3);
        }
        if (width === 780 && k === n - 1) {
          test.info().annotations.push({type: `780px endpoint (${fonts})`, description:
            `window ${h.window.toFixed(1)}px; ${order[n - 2]} ${h.widths[n - 2].toFixed(1)}px + ${order[n - 1]} ${h.widths[n - 1].toFixed(1)}px whole; side padding ${h.padding[0]}px (complete strip ${reference.padding[0]}px); ${h.positions} positions`});
          for (const theme of ['dark', 'light']) {
            await page.evaluate(light => document.body.classList.toggle('light', light), theme === 'light');
            await settle(page);
            await page.locator('#view-settings .dp-settings-master-header').screenshot({
              path: `test-results/checkpoint-settings-navigator-780-endpoint-${fonts.split(' ')[0]}-${theme}.png`});
          }
          await page.evaluate(() => document.body.classList.remove('light'));
          await settle(page);
        }
      }
    }
  });
}

/* Whether the window moved for the last choice, and how it slid. */
async function slideOf(page, act) {
  await page.evaluate(() => { window.__dpNav = []; });
  const before = await page.evaluate(() => document.querySelector('#view-settings .dp-settings-tabs').scrollLeft);
  await act();
  await settle(page);
  const after = await page.evaluate(() => document.querySelector('#view-settings .dp-settings-tabs').scrollLeft);
  const frames = await page.evaluate(() => window.__dpNav);
  return {moved: after - before, frames, directions: [...new Set(frames.map(frame => frame.direction))],
    moving: frames.filter(frame => frame.moving).length,
    controls: new Set(frames.map(frame => frame.control.join())).size, headers: new Set(frames.map(frame => frame.header.join())).size};
}

const expectSlide = (slide, direction, label) => {
  expect(Math.sign(slide.moved), label).toBe(direction === 'forward' ? 1 : -1);
  expect(slide.directions, label).toEqual([direction]);
  expect(slide.moving, label).toBeGreaterThanOrEqual(3);       // drawn over several frames
  expect(slide.controls, label).toBe(1);                        // the control and the header stay where they are
  expect(slide.headers, label).toBe(1);
};

test('the window slides only when a choice moves it, the same way as the panel: neighbours, edges, First and Last, into either end', async ({page}) => {
  await openSettings(page, 1100);
  const order = (await header(page)).order;

  // In the window: the selection changes, the window stays, nothing slides.
  await choose(page, order[0]);
  let h = await header(page);
  const inside = h.shown.findIndex((part, k) => k > 0 && part >= h.widths[k] - 0.5);
  expect(inside).toBeGreaterThan(0);
  let slide = await slideOf(page, () => choose(page, order[inside]));
  expect(slide.moved).toBe(0);
  expect(slide.frames).toEqual([]);

  // A section cut at the edge: the window moves forward, and slides.
  h = await header(page);
  const cut = h.shown.findIndex((part, k) => part > 0 && part < h.widths[k] - 0.5 && k > h.index);
  expect(cut).toBeGreaterThan(h.index);
  slide = await slideOf(page, () => choose(page, order[cut]));
  expectSlide(slide, 'forward', 'cut section');

  // Last: forward into the end arrangement; First: back to the start.
  slide = await slideOf(page, () => page.locator('#view-settings .dp-settings-nav-end[data-settings-end="last"]').click());
  expectSlide(slide, 'forward', 'Last');
  h = await header(page);
  expect(Math.abs(h.atEnd - h.edge)).toBeLessThanOrEqual(1);
  slide = await slideOf(page, () => page.locator('#view-settings .dp-settings-nav-end[data-settings-end="first"]').click());
  expectSlide(slide, 'backward', 'First');
  h = await header(page);
  expect(Math.abs(h.atStart - h.edge)).toBeLessThanOrEqual(1);

  // Walking to the end one section at a time: each move that shifts the
  // window slides forward, the last of them into the end arrangement; the
  // others move nothing.
  let slid = 0;
  for (let k = 1; k < order.length; k += 1) {
    slide = await slideOf(page, () => choose(page, order[k]));
    if (slide.moved) { expectSlide(slide, 'forward', order[k]); slid += 1; }
    else expect(slide.frames, order[k]).toEqual([]);
  }
  expect(slid).toBeGreaterThan(0);
  h = await header(page);
  expect(Math.abs(h.atEnd - h.edge)).toBeLessThanOrEqual(1);

  // Rapid choices, interrupting each other: the last one wins, settled.
  await selected(page).focus();
  for (const key of ['Home', 'End', 'Home', 'ArrowRight', 'End']) await page.keyboard.press(key);
  await settle(page);
  await expect(selected(page)).toHaveAttribute('data-tab', order.at(-1));
  await expect(page.locator(`#view-settings [data-panel]:not([hidden])`)).toHaveAttribute('data-panel', order.at(-1));
  h = await header(page);
  expect(h.currentWhole).toBe(true);
  expect(Math.abs(h.atEnd - h.edge)).toBeLessThanOrEqual(1);
});

test('with reduced motion the window moves at once and never slides', async ({page}) => {
  await page.emulateMedia({reducedMotion: 'reduce'});
  await openSettings(page, 1100);
  const order = (await header(page)).order;
  const slide = await slideOf(page, () => page.locator('#view-settings .dp-settings-nav-end[data-settings-end="last"]').click());
  expect(slide.moved).toBeGreaterThan(0);
  expect(slide.frames).toEqual([]);
  const h = await header(page);
  expect(h.index).toBe(order.length - 1);
  expect(Math.abs(h.atEnd - h.edge)).toBeLessThanOrEqual(1);
});

test('the presentation changing keeps the section, its panel, an unsent edit and focus, and sends nothing', async ({page}) => {
  await openSettings(page, 1920);
  await page.locator('#view-settings [data-tab="authentication"]').click();
  await expect(page.locator('#view-settings [data-panel="authentication"]')).toBeVisible();
  const field = page.locator('#view-settings [data-panel="authentication"] input[type="text"]:visible, #view-settings [data-panel="authentication"] input[type="url"]:visible').first();
  await field.click();
  await field.fill('https://settings-navigation.example');
  await page.evaluate(() => { window.__dpPanel = document.querySelector('#view-settings [data-panel="authentication"]'); });
  const sent = [];
  // No write, and no re-read of the Settings document (a re-render).
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    // (The status bar's own aria2 probe, app.js checkConnections, is a POST that writes nothing.)
    if (path === '/api/settings/test-aria2') return;
    if (request.method() !== 'GET' || ['/api/settings', '/api/auth/config'].includes(path)) sent.push(`${request.method()} ${path}`);
  });
  for (const width of [1100, 780, 1440, 900, 1920]) {
    await resize(page, width);
    await expect(selected(page)).toHaveAttribute('data-tab', 'authentication');
    expect(await page.evaluate(() => window.__dpPanel === document.querySelector('#view-settings [data-panel]:not([hidden])'))).toBe(true);
    await expect(field).toHaveValue('https://settings-navigation.example');
    await expect(field).toBeFocused();
  }
  expect(sent).toEqual([]);
});

test('below 780px the handheld header keeps its wrapping, scrolling strip and no compact navigator', async ({page}) => {
  await openSettings(page, 1440);
  await resize(page, 779);
  const h = await page.evaluate(() => {
    const rect = node => node.getBoundingClientRect();
    const root = document.getElementById('view-settings');
    const copy = rect(root.querySelector('.dp-settings-header-copy'));
    const strip = root.querySelector('.dp-settings-tabs');
    return {beneath: rect(strip).top >= copy.bottom, scrolls: strip.scrollWidth > strip.clientWidth,
      ends: [...root.querySelectorAll('.dp-settings-nav-end')].map(node => getComputedStyle(node).display),
      surface: getComputedStyle(strip).borderTopWidth, tabs: strip.querySelectorAll('[data-tab]').length};
  });
  expect(h).toEqual({beneath: true, scrolls: true, ends: ['none', 'none'], surface: '1px', tabs: 7});
  await page.locator('#view-settings [data-tab="maintenance"]').click();
  await expect(page.locator('#view-settings [data-panel="maintenance"]')).toBeVisible();
});
