const { test, expect } = require('@playwright/test');

/* The Help & License master header's section navigation, desktop and tablet
 * (780px and wider): one row, through the same section navigator as Settings
 * (ui-section-nav.js). The complete eight-section strip whenever the Field
 * Manual copy, the 16px row gap and the strip at their own widths fit the
 * header with the documented 4px to spare; otherwise the compact navigator
 * -- First, a window onto the same eight tabs, Last. Either way the
 * navigation keeps the strip's own place: centred in the header (half the
 * row gap toward the copy's side) while the copy fits in the half beside it,
 * otherwise right after the copy, the row gap apart. The window holds as
 * many sections as fit, at least three wherever three can, and is a bounded
 * sliding window: at Quick Start it starts there, at License it ends there,
 * between them the selection is as centred as the cuts allow, an edge where
 * sections continue cuts one and fades outwards, and it slides (DPMotion)
 * only when a choice moves it, the way the choice goes, as the panel does.
 * First and Last are gone at their own end, taking no room: the window grows
 * into their place while the control stays where it is. A part of a section
 * too small to be a position is not shown; at the 780px floor Quick Start or
 * License may show with its neighbour alone: the selection always whole, the
 * neighbour whole where the two fit (the tabs giving up some side padding if
 * they must), otherwise cut at the window's edge, faded and still a target.
 * Below 780px the handheld header keeps its wrapping strip. Help keeps its
 * own sections, chips, colours, keyboard, ARIA, entry on Quick Start and
 * content.
 *
 * Fit, placement and feasibility are computed here from the rendered tabs'
 * and copy's own widths -- whatever fonts this browser drew -- never read
 * from the page's decision. */

const SPARE = 4;
const TARGET = 24;   // the least part of a section that is a position
const SECTIONS = [
  ['quickstart', 'Quick Start'], ['using', 'Everyday Use'], ['howitworks', 'How It Works'], ['services', 'Services'],
  ['engines', 'Engines'], ['settings', 'Settings'], ['trouble', 'Troubleshooting'], ['license', 'License'],
];

// Two frames drawn, the navigator's window not sliding (DPMotion marks the
// strip from the choice until its slide is over) and no finite animation
// running: hit-testing and geometry are the page's own.
const settle = async page => {
  await page.waitForFunction(() => !document.querySelector('#view-help .dp-help-tabs.dp-motion-surface') && document.getAnimations()
    .every(animation => animation.playState !== 'running' || animation.effect?.getTiming().iterations === Infinity));
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
};

async function resize(page, width) {
  await page.setViewportSize({width, height: 1000});
  await page.waitForFunction(() =>
    document.getElementById('sidebar').getAnimations().every(animation => animation.playState !== 'running'));
  await settle(page);
}

async function openHelp(page, width, {fallbackFonts = false} = {}) {
  if (fallbackFonts) {
    await page.route('https://fonts.googleapis.com/**', route =>
      route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  }
  /* Every frame in which the compact navigator's window is sliding (DPMotion
   * marks the strip while it runs): its direction and step, where the
   * navigator's control and the header stand, and the way the strip's and
   * the panel's arriving content moves. And in every frame: a First or Last
   * that is shown stands over the window's end, one that is gone has left
   * its place to the window -- never both, never neither (__dpEnds counts
   * the frames that break it). */
  await page.addInitScript(() => {
    window.__dpNav = [];
    window.__dpContent = [];
    window.__dpEnds = [];
    // Where the destination stands now: + arriving from the
    // right (forward), - from the left (backward), 0 none.
    // (The destination is the surface's live content, moved by the slide;
    // its first drawn part says where it stands.)
    const arriving = node => {
      const host = node && (node.querySelector('[data-dp-motion-outgoing]')?.parentElement || node);
      const live = host && [...host.children].find(part => part.getClientRects().length
        && !part.classList.contains('dp-motion-still') && !part.hasAttribute('data-dp-motion-outgoing'));
      const match = live && /matrix\(([^)]+)\)/.exec(getComputedStyle(live).transform);
      return match ? Math.sign(Math.round(+match[1].split(',')[4])) : 0;
    };
    // Whether the slide of `node` is travelling: an animation of its content's place.
    const travelling = node => document.getAnimations().some(a => a.playState === 'running' && a.effect?.target instanceof Node
      && node.contains(a.effect.target) && a.effect.target !== node && a.effect.getKeyframes().some(k => k.transform));
    const tick = () => {
      // The content viewport's own slide, recorded whether or not the selector slides.
      const content = document.querySelector('#view-help .dp-help-scroll.dp-motion-surface');
      if (content) window.__dpContent.push({direction: content.dataset.dpMotionDirection, arriving: arriving(content),
        moving: travelling(content)});
      const strip = document.querySelector('#view-help .dp-help-tabs.dp-motion-surface');
      if (strip) {
        const control = strip.parentElement.getBoundingClientRect();
        const header = strip.closest('.dp-help-master-header').getBoundingClientRect();
        const copy = document.querySelector('#view-help .dp-help-header-copy').getBoundingClientRect();
        window.__dpNav.push({direction: strip.dataset.dpMotionDirection, step: strip.dataset.dpMotionStep,
          control: [control.left, control.top, control.width, control.height], header: [header.left, header.top, header.width, header.height],
          copy: [copy.left, copy.top, copy.width, copy.height],
          moving: travelling(strip),
          strip: arriving(strip), panel: arriving(document.querySelector('#view-help .dp-help-scroll.dp-motion-surface'))});
      }
      const compact = document.querySelector('#view-help .dp-help-master-header[data-presentation="compact"] .dp-help-tabs');
      if (compact && innerWidth >= 780) {
        const inset = (/inset\(([^)]+)\)/.exec(getComputedStyle(compact).clipPath)?.[1] || '0').split(/\s+/).map(parseFloat);
        const [top, right = top, , left = right] = inset;
        for (const [end, clip] of [['first', left], ['last', right]]) {
          const node = compact.parentElement.querySelector(`.dp-help-nav-end[data-help-end="${end}"]`);
          const there = getComputedStyle(node).display !== 'none';
          if (there !== clip > 0 || (there && Math.abs(node.getBoundingClientRect().width - clip) > 0.5)) window.__dpEnds.push(`${end}: ${there ? 'shown' : 'gone'}, clip ${clip}`);
        }
      }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });
  await page.setViewportSize({width, height: 1000});
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="help"]:not([data-title])').click();
  await expect(page.locator('#view-help .dp-help-tabs [data-tab]').first()).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await settle(page);
}

// The live strip: while the window slides, its outgoing copy (inert, hidden
// from assistive technology) stands beside it with the same classes.
const selected = page => page.locator('#view-help .dp-help-tabs:not(.dp-motion-still) [aria-selected="true"]');

/* Everything about the header at this width, from geometry alone. `frame`
 * is the strip's own padding and border in the complete presentation. */
function header(page, frame = 0) {
  return page.evaluate(({frame, TARGET, SPARE}) => {
    const rect = node => node.getBoundingClientRect();
    const root = document.getElementById('view-help');
    const headerNode = root.querySelector('.dp-help-master-header');
    const style = getComputedStyle(headerNode);
    const headerBox = rect(headerNode);
    const inner = {left: headerBox.left + parseFloat(style.paddingLeft), right: headerBox.right - parseFloat(style.paddingRight)};
    const gap = parseFloat(style.columnGap);
    const parts = node => [...node.children].reduce((sum, kid) => sum + rect(kid).width, 0)
      + (parseFloat(getComputedStyle(node).columnGap) || 0) * (node.children.length - 1);
    const copyNode = root.querySelector('.dp-help-header-copy');
    const copy = rect(copyNode);
    const copyOwn = parts(copyNode);
    const strip = root.querySelector('.dp-help-tabs');
    const box = rect(strip);
    // The window: the strip clipped to it (compact), or the strip.
    const inset = (/inset\(([^)]+)\)/.exec(getComputedStyle(strip).clipPath)?.[1] || '0').split(/\s+/).map(parseFloat);
    const [clipTop, clipRight = clipTop, , clipLeft = clipRight] = inset;
    const s = {left: box.left + clipLeft, right: box.right - clipRight, width: box.width - clipLeft - clipRight};
    const tabNodes = [...strip.querySelectorAll('[data-tab]')];
    const tabs = tabNodes.map(rect);
    const stripNatural = tabs.reduce((sum, tab) => sum + tab.width, 0);
    const ends = [...root.querySelectorAll('.dp-help-nav-end')];
    const shownEnds = ends.filter(node => getComputedStyle(node).display !== 'none');
    const controlNode = root.querySelector('.dp-help-nav-control');
    const control = rect(controlNode);
    const index = tabNodes.findIndex(tab => tab.getAttribute('aria-selected') === 'true');
    const c = tabs[index];
    const label = tabNodes[index].querySelector('.dp-help-tab-label');
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
        ? parseFloat(tab.style.getPropertyValue('--dp-section-nav-cut-fade')) || 0 : 0;
    };
    return {
      width: innerWidth, height: headerBox.height, minHeight: parseFloat(style.minHeight),
      copyLeft: copy.left - headerBox.left, copyTop: copy.top - headerBox.top,
      level: Math.abs((control.top + control.bottom) / 2 - (copy.top + copy.bottom) / 2),
      inside: control.left >= copy.left + copyOwn + gap - 0.5 && control.right <= inner.right + 0.5
        && control.top >= headerBox.top && control.bottom <= headerBox.bottom,
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
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      index, order: tabNodes.map(tab => tab.dataset.tab),
      currentWhole: c.left >= s.left - 0.5 && c.right <= s.right + 0.5,
      labelWhole: label.scrollWidth <= label.clientWidth + 0.5 && rect(label).left >= c.left && rect(label).right <= c.right,
      shown, hidden, widths: tabs.map(t => t.width), window: s.width,
      padding: tabNodes.map(tab => parseFloat(getComputedStyle(tab).paddingLeft)),
      labels: tabNodes.map(tab => tab.textContent.trim()),
      glyphs: tabNodes.map(tab => tab.querySelector('.dp-help-tab-chip img')?.getAttribute('src')),
      chips: tabNodes.map(tab => getComputedStyle(tab.querySelector('.dp-help-tab-chip')).borderTopColor),
      fontSizes: tabNodes.map(tab => getComputedStyle(tab.querySelector('.dp-help-tab-label')).fontSize),
      labelsWhole: tabNodes.map(tab => { const l = tab.querySelector('.dp-help-tab-label'); return l.scrollWidth <= l.clientWidth + 0.5; }),
      cuts: tabNodes.map(tab => tab.dataset.navCut || ''),
      faded: tabNodes.map(tab => getComputedStyle(tab).maskImage !== 'none'),
      ends: ends.map(node => ({width: rect(node).width, height: rect(node).height, left: rect(node).left, right: rect(node).right,
        shown: getComputedStyle(node).display !== 'none'})),
      positions: shown.filter((part, k) => part >= TARGET || k === index).length,
      atStart: c.left - s.left, atEnd: s.right - c.right,
      edge: tabs[0].left - (box.left + parseFloat(getComputedStyle(strip).paddingLeft) - strip.scrollLeft),
      strip: {left: box.left, right: box.right, width: box.width}, clip: [clipLeft, clipRight],
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
  if (h.height > h.minHeight + 0.5) out.push(`header taller than its ${h.minHeight}px row`);
  if (Math.abs(h.copyLeft - reference.copyLeft) > 0.5 || Math.abs(h.copyTop - reference.copyTop) > 0.5) out.push('identity moved');
  if (Math.abs(h.placeOffset) > 0.5) out.push(`off the strip's place (${h.centred ? 'centred' : 'after the copy'}, ${h.placeOffset.toFixed(1)})`);
  if (!borderline && h.side >= 0 && h.compact) out.push('compact although the strip fits');
  if (!borderline && h.side < 0 && !h.compact) out.push('complete strip although it does not fit');
  if (!h.compact && (!h.allTabsInside || h.scrolls)) out.push('complete strip clipped or scrolling');
  if (h.overflow > 0) out.push('page scrolls sideways');
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

/* A section the window cuts is never presented as whole: it is marked cut
 * and fades (or, under TARGET, is not shown at all). */
function expectNoHiddenCut(h, at) {
  h.shown.forEach((part, j) => {
    if (part <= 0.5 || part >= h.widths[j] - 0.5) return;
    expect(h.cuts[j], `${h.order[j]} cut (${part.toFixed(1)}/${h.widths[j].toFixed(1)}px), ${at}`).not.toBe('');
    expect(h.faded[j], `${h.order[j]} cut and faded, ${at}`).toBe(true);
  });
}

/* Quick Start or License selected: the selection comes first. It is whole,
 * unfaded, at the list's end; its neighbour gives up only what the
 * selection leaves no room for -- whole wherever the two fit, otherwise cut
 * at the window's edge, faded, at least TARGET, still named and a target
 * for its own section. More sections only where TARGET of one fits; a
 * smaller part is neither shown nor a target. Returns what the neighbour
 * shows. */
async function expectEndpoint(page, h, at, reference) {
  const n = h.order.length;
  const first = h.index === 0;
  const [near, far, next] = first ? [0, 1, 2] : [n - 1, n - 2, n - 3];
  const hitAt = j => page.evaluate(j => {
    const strip = document.querySelector('#view-help .dp-help-tabs');
    const t = strip.querySelectorAll('[data-tab]')[j].getBoundingClientRect();
    const box = strip.getBoundingClientRect();
    const inset = (/inset\(([^)]+)\)/.exec(getComputedStyle(strip).clipPath)?.[1] || '0').split(/\s+/).map(parseFloat);
    const [top, right = top, , left = right] = inset;
    const x = (Math.max(t.left, box.left + left) + Math.min(t.right, box.right - right)) / 2;
    return document.elementFromPoint(x, (t.top + t.bottom) / 2)?.closest('[data-tab]')?.dataset.tab ?? null;
  }, j);
  expect(Math.abs(h.shown[near] - h.widths[near]), `${h.order[near]} whole, ${at}`).toBeLessThanOrEqual(0.5);
  expect(h.cuts[near], `${h.order[near]} unfaded, ${at}`).toBe('');
  expect(h.faded[near], `${h.order[near]} unfaded, ${at}`).toBe(false);
  expect(h.labelWhole, `${h.order[near]} label whole, ${at}`).toBe(true);
  expect(Math.abs((first ? h.atStart : h.atEnd) - h.edge), at).toBeLessThanOrEqual(1);
  const left = h.window - h.edge - h.widths[near];
  expect(Math.abs(h.shown[far] - Math.min(h.widths[far], left)),
    `${h.order[far]} gives up only what ${h.order[near]} needs, ${at}`).toBeLessThanOrEqual(0.5);
  // Named in full and reachable whatever it shows.
  await expect(page.getByRole('tab', {name: reference.labels[far], exact: true}), at).toHaveCount(1);
  if (left >= h.widths[far] - 0.5) {
    expect(h.cuts[far], `${h.order[far]} whole and unfaded, ${at}`).toBe('');
    const room = left - h.widths[far];
    if (room >= TARGET) expect(h.positions, at).toBeGreaterThanOrEqual(3);
    else {
      expect(h.positions, at).toBe(2);
      expect(h.shown[next], at).toBe(0);
      // Not shown, and not a target for its section either.
      if (h.hidden[next] > 0) expect(await hitAt(next), at).not.toBe(h.order[next]);
    }
  } else {
    expect(h.shown[far], `${h.order[far]} a preview, ${at}`).toBeGreaterThanOrEqual(TARGET);
    expect(h.cuts[far], `${h.order[far]} cut at the window's edge, ${at}`).toBe(first ? 'end' : 'start');
    expect(h.faded[far], `${h.order[far]} faded, ${at}`).toBe(true);
    expect(await hitAt(far), `${h.order[far]} a target, ${at}`).toBe(h.order[far]);
    expect(h.positions, at).toBe(2);
    expect(h.shown[next] + h.hidden[next], at).toBe(0);
  }
  return h.shown[far];
}

/* Choose a section through the page's own controls: a click where it shows,
 * else the keyboard from the selected tab (the window never shows a section
 * the list does not reach). */
async function choose(page, id) {
  const tab = page.locator(`#view-help .dp-help-tabs:not(.dp-motion-still) [data-tab="${id}"]`);
  const at = await header(page);
  const k = at.order.indexOf(id);
  if (k === at.index) return;
  if (at.shown[k] >= TARGET) {
    // The middle of what the window shows of it (the strip is clipped to the window).
    const box = await tab.boundingBox();
    const x = (Math.max(box.x, at.strip.left + at.clip[0]) + Math.min(box.x + box.width, at.strip.right - at.clip[1])) / 2;
    await page.mouse.click(x, box.y + box.height / 2);
  } else {
    await selected(page).focus();
    const key = k > at.index ? 'ArrowRight' : 'ArrowLeft';
    for (let step = 0; step < Math.abs(k - at.index); step += 1) await page.keyboard.press(key);
  }
  await expect(selected(page)).toHaveAttribute('data-tab', id);
  await expect(page.locator(`#dp-help-panel-${id}`)).toBeVisible();
  await settle(page);
}

for (const fonts of ['web fonts', 'fallback fonts']) {
  test(`the Help header is one row from 1920px to 780px with ${fonts}: the strip while it fits, else the compact navigator, both in the strip's own place`, async ({page}) => {
    await openHelp(page, 1920, {fallbackFonts: fonts === 'fallback fonts'});
    const first = await header(page);
    expect(first.compact).toBe(false);
    expect(first.order).toEqual(SECTIONS.map(([id]) => id));
    expect(first.labels).toEqual(SECTIONS.map(([, label]) => label));
    const frame = first.control.width - first.widths.reduce((sum, width) => sum + width, 0);   // the strip's padding and border
    expect(frame).toBeGreaterThan(0);
    expect(frame).toBeLessThan(12);
    const reference = await header(page, frame);
    expect(reference.height).toBe(76);
    // Every width's problems, reported together (an empty report passes).
    const report = {};
    const record = (width, found) => { if (found.length) report[`${width}px`] = found; };
    record(1920, problems(reference, reference));
    let compact = 0;
    for (const width of [1800, 1700, 1600, 1560, 1559, 1440, 1389, 1300, 1200, 1181, 1180, 1100, 1024, 901, 900, 899, 850, 800, 781, 780]) {
      await resize(page, width);
      const h = await header(page, frame);
      record(width, problems(h, {...reference, copyLeft: h.copyLeft, copyTop: h.copyTop}));
      // The identity copy is where it was (its icon is the page's own size at each width).
      if (Math.abs(h.copyLeft - reference.copyLeft) > 0.5) record(width, ['identity moved']);
      compact += h.compact;
    }
    expect(report).toEqual({});
    expect(compact, 'compact widths').toBeGreaterThan(0);

    /* The threshold, from the measured parts: the header grows as the window
     * does within one sidebar width (full sidebar from 1560px, rail below). */
    const thresholds = [];
    for (const [probe, low, high] of [[1920, 1560, 1920], [1300, 901, 1559]]) {
      await resize(page, probe);
      const width = Math.ceil(probe - (await header(page, frame)).side);
      if (width > low && width <= high) thresholds.push(width);
    }
    expect(thresholds.length).toBeGreaterThan(0);
    for (const threshold of thresholds) {
      for (const [width, compact] of [[threshold + 1, false], [threshold - 1, true], [threshold - 2, true],
        [threshold + 2, false], [threshold - 1, true], [threshold + 1, false]]) {
        await resize(page, width);
        const h = await header(page, frame);
        expect(problems(h, {...reference, copyLeft: h.copyLeft, copyTop: h.copyTop}), `${width}px`).toEqual([]);
        expect(h.compact, `${width}px`).toBe(compact);
        await settle(page);
        expect((await header(page, frame)).compact, `${width}px, a frame later`).toBe(compact);
      }
      await resize(page, threshold);
      const at = await header(page, frame);
      test.info().annotations.push({type: `threshold (${fonts})`, description: `complete strip from ${threshold}px: ${(at.side + SPARE).toFixed(1)}px to spare there`});
    }
    // Representative images: wide (complete), the threshold's compact side, 1100px; dark and light.
    for (const width of [1920, thresholds.at(-1) - 1, 1100]) {
      await resize(page, width);
      for (const theme of ['dark', 'light']) {
        await page.evaluate(light => document.body.classList.toggle('light', light), theme === 'light');
        await settle(page);
        await page.locator('#view-help .dp-help-master-header').screenshot({
          path: `test-results/checkpoint-help-header-${fonts.split(' ')[0]}-${width}-${theme}.png`});
      }
      await page.evaluate(() => document.body.classList.remove('light'));
    }
  });
}

test('the compact window holds as many Help sections as fit, fewer as the header narrows, three wherever three can fit, and every one it shows opens its section', async ({page}) => {
  // Nine widths x five sections, each chosen, verified and settled: 32-45s on
  // hosted runners when it passes (CI logs, Oct 2026), at the suite's 45s
  // default -- the sweep's own budget, not a wait for anything.
  test.setTimeout(90_000);
  await openHelp(page, 1920);
  const reference = await header(page);
  const frame = reference.control.width - reference.widths.reduce((sum, width) => sum + width, 0);
  await resize(page, 1300);
  const threshold = Math.min(1300, Math.ceil(1300 - (await header(page, frame)).side));
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
      const w = h.widths;
      const three = k === 0 ? h.edge + w[0] + w[1] + TARGET <= h.window
        : k === order.length - 1 ? h.edge + w[k] + w[k - 1] + TARGET <= h.window
        : w[k] + 2 * TARGET <= h.window;
      if (three) expect(h.positions, `${order[k]} at ${width}px`).toBeGreaterThanOrEqual(3);
      else test.info().annotations.push({type: 'limit', description: `${order[k]} at ${width}px: a ${h.window.toFixed(0)}px window cannot hold three positions (${h.positions} shown)`});
      expect(h.shown.filter(part => part > 0.5 && part < TARGET), `${order[k]} at ${width}px`).toEqual([]);
      for (const [j, part] of h.shown.entries()) {
        if (part < TARGET || j === h.index) continue;
        const hit = await page.evaluate(j => {
          const strip = document.querySelector('#view-help .dp-help-tabs');
          const tab = strip.querySelectorAll('[data-tab]')[j];
          const s = strip.getBoundingClientRect(), t = tab.getBoundingClientRect();
          const x = (Math.max(t.left, s.left) + Math.min(t.right, s.right)) / 2;
          return document.elementFromPoint(x, (t.top + t.bottom) / 2)?.closest('[data-tab]')?.dataset.tab;
        }, j);
        expect(hit, `${order[j]} at ${width}px`).toBe(order[j]);
      }
    }
    expect(Math.max(...row.positions), `${width}px`).toBeLessThanOrEqual(previous);
    previous = Math.max(...row.positions);
    rows.push(row);
  }
  expect(Math.max(...rows[0].positions)).toBeGreaterThan(3);
  test.info().annotations.push({type: 'capacity', description: rows.map(row => `${row.width}px ${row.window.toFixed(0)}px [${row.positions.join(',')}]`).join('; ')});
});

test('the Help window is bounded: it starts at Quick Start, ends at License, centres between them, fades only where sections continue; First and Last only away from their end, named for their section', async ({page}) => {
  await openHelp(page, 1100);
  const order = (await header(page)).order;
  const first = page.locator('#view-help .dp-help-nav-end[data-help-end="first"]');
  const last = page.locator('#view-help .dp-help-nav-end[data-help-end="last"]');
  // One tablist of the eight real tabs, each with its own panel.
  const tablist = page.getByRole('tablist', {name: 'Help sections'});
  await expect(tablist.getByRole('tab')).toHaveCount(8);
  for (const [id, label] of SECTIONS) {
    const tab = tablist.getByRole('tab', {name: label, exact: true});
    await expect(tab).toHaveAttribute('id', `dp-help-tab-${id}`);
    await expect(tab).toHaveAttribute('aria-controls', `dp-help-panel-${id}`);
    await expect(page.locator(`#dp-help-panel-${id}`)).toHaveAttribute('aria-labelledby', `dp-help-tab-${id}`);
    await expect(page.locator(`#dp-help-panel-${id}`)).toHaveAttribute('role', 'tabpanel');
  }
  await expect(page.locator('#view-help [role="tablist"]')).toHaveCount(1);
  await expect(page.locator('#view-help [data-tab]')).toHaveCount(8);

  // Quick Start: the window starts with it; no First. The far edge fades
  // where it cuts a section; where it falls (within less than TARGET) on a
  // section boundary, that part is not shown (the approved rule).
  let h = await header(page);
  expect(h.index).toBe(0);
  expect(h.compact).toBe(true);
  expect(Math.abs(h.atStart - h.edge)).toBeLessThanOrEqual(1);
  expect(h.fadeStart).toBe(0);
  const after = h.shown.findLastIndex(part => part > 0);
  if (h.fadeEnd) {
    expect(h.fadeEnd).toBeGreaterThanOrEqual(TARGET);
    expect(h.mask).toContain('linear-gradient');
  } else {
    expect(Math.abs(h.shown[after] - h.widths[after]), 'the last section shown is whole').toBeLessThanOrEqual(0.5);
    expect(h.hidden[after + 1], 'the part beyond it is a hidden sliver').toBeLessThan(TARGET);
    test.info().annotations.push({type: 'Quick Start edge at 1100px', description: `${h.order[after + 1]} ${h.hidden[after + 1].toFixed(1)}px in the window, under ${TARGET}px: not shown`});
  }
  expect(h.shown.filter(part => part > 0.5 && part < TARGET)).toEqual([]);
  await expect(first).toHaveCSS('display', 'none');
  await expect(last).toBeVisible();
  await expect(last).toHaveAccessibleName('Last section: License');
  await expect(first).toHaveAttribute('aria-label', 'First section: Quick Start');   // gone: no accessible node here
  await first.evaluate(node => node.focus());
  await expect(first).not.toBeFocused();   // a hidden action takes no focus
  const control = h.control;

  // Between them: the selection whole, away from both ends; each edge fades
  // where it cuts a section, or falls (within less than TARGET) on a section
  // boundary, that part not shown (the window keeps where it is drawn, the
  // operator's targets under the pointer, before centring the selection).
  const middle = Math.floor(order.length / 2);
  await choose(page, order[middle]);
  h = await header(page);
  expect(h.currentWhole).toBe(true);
  for (const [fade, k] of [[h.fadeStart, h.shown.findIndex(part => part > 0)], [h.fadeEnd, h.shown.findLastIndex(part => part > 0)]]) {
    if (fade) expect(fade).toBeGreaterThanOrEqual(TARGET);
    else expect(Math.abs(h.shown[k] - h.widths[k]), `${h.order[k]} whole at the edge`).toBeLessThanOrEqual(0.5);
  }
  expect(h.shown.filter(part => part > 0.5 && part < TARGET)).toEqual([]);
  expect(h.atStart).toBeGreaterThan(h.edge);
  expect(h.atEnd).toBeGreaterThan(h.edge);
  await expect(first).toBeVisible();
  await expect(last).toBeVisible();
  await expect(first).toHaveAccessibleName('First section: Quick Start');
  await expect(last).toHaveAccessibleName('Last section: License');
  // The actions are actions, never tabs.
  await expect(page.getByRole('tablist', {name: 'Help sections'}).getByRole('button')).toHaveCount(0);
  for (const key of ['left', 'right']) expect(Math.abs(h.control[key] - control[key])).toBeLessThanOrEqual(0.5);

  // License: the window ends with it; no Last; the near edge fades.
  await last.focus();
  await page.keyboard.press('Enter');
  await expect(selected(page)).toHaveAttribute('data-tab', 'license');
  await expect(page.locator('#dp-help-tab-license')).toBeFocused();
  await settle(page);
  h = await header(page);
  expect(Math.abs(h.atEnd - h.edge)).toBeLessThanOrEqual(1);
  expect(h.fadeEnd).toBe(0);
  const before = h.shown.findIndex(part => part > 0);
  if (h.fadeStart) expect(h.fadeStart).toBeGreaterThanOrEqual(TARGET);
  else expect(Math.abs(h.shown[before] - h.widths[before]), 'the first section shown is whole').toBeLessThanOrEqual(0.5);
  expect(h.shown.filter(part => part > 0.5 && part < TARGET)).toEqual([]);
  await expect(last).toHaveCSS('display', 'none');
  for (const key of ['left', 'right']) expect(Math.abs(h.control[key] - control[key])).toBeLessThanOrEqual(0.5);
  await first.click();
  await expect(selected(page)).toHaveAttribute('data-tab', 'quickstart');
  await expect(page.locator('#dp-help-tab-quickstart')).toBeFocused();
  await settle(page);

  // The keyboard walks every section in order, the selection always whole,
  // and wraps at either end; Home and End reach the ends.
  for (let k = 1; k < order.length; k += 1) {
    await page.keyboard.press('ArrowRight');
    await expect(page.locator(`#dp-help-tab-${order[k]}`)).toBeFocused();
    await settle(page);
    expect((await header(page)).currentWhole, order[k]).toBe(true);
  }
  await page.keyboard.press('ArrowRight');
  await expect(page.locator('#dp-help-tab-quickstart')).toBeFocused();
  await page.keyboard.press('ArrowLeft');
  await expect(page.locator('#dp-help-tab-license')).toBeFocused();
  await page.keyboard.press('Home');
  await expect(page.locator('#dp-help-tab-quickstart')).toBeFocused();
  await page.keyboard.press('End');
  await expect(page.locator('#dp-help-tab-license')).toBeFocused();
  await expect(page.locator('#dp-help-panel-license')).toBeVisible();
  await settle(page);
  const strip = page.locator('#view-help .dp-help-tabs:not(.dp-motion-still)');
  await expect(strip.locator('[tabindex="0"]')).toHaveCount(1);
  await expect(strip.locator('[aria-selected="true"]')).toHaveCount(1);
  // The keyboard never scrolled the page.
  expect(await page.evaluate(() => [scrollX, scrollY, document.getElementById('content').scrollLeft])).toEqual([0, 0, 0]);

  // Representative images: 1100px start, middle, end; 780px middle; dark and light.
  for (const [width, k] of [[1100, 0], [1100, middle], [1100, order.length - 1], [780, middle]]) {
    await resize(page, width);
    await choose(page, order[k]);
    for (const theme of ['dark', 'light']) {
      await page.evaluate(light => document.body.classList.toggle('light', light), theme === 'light');
      await settle(page);
      await page.locator('#view-help .dp-help-master-header')
        .screenshot({path: `test-results/checkpoint-help-navigator-${width}-${order[k]}-${theme}.png`});
    }
    await page.evaluate(() => document.body.classList.remove('light'));
  }
});

for (const fonts of ['web fonts', 'fallback fonts']) {
  test(`at the 780px floor with ${fonts} Quick Start and License show whole, the neighbour whole where both fit and otherwise cut, faded and a target, and no sliver; nearby widths and the other sections keep three positions`, async ({page}) => {
    await openHelp(page, 1920, {fallbackFonts: fonts === 'fallback fonts'});
    const reference = await header(page);
    expect(reference.compact).toBe(false);
    const order = reference.order;
    const n = order.length;
    const natural = reference.widths;
    for (const width of [780, 781, 790, 800]) {
      await resize(page, width);
      let control = null;
      let identity = null;
      for (let k = 0; k < n; k += 1) {
        await choose(page, order[k]);
        const h = await header(page);
        const at = `${order[k]} at ${width}px`;
        expect(h.compact, at).toBe(true);
        // The identity stays where it is at this width, whatever is selected.
        identity ??= {copyLeft: h.copyLeft, copyTop: h.copyTop};
        expect(problems(h, {...reference, ...identity}), at).toEqual([]);
        control ??= h.control;
        for (const key of ['left', 'right']) expect(Math.abs(h.control[key] - control[key]), at).toBeLessThanOrEqual(0.5);
        // Text, chips, glyphs, type and selection are the complete strip's own.
        expect(h.labels, at).toEqual(reference.labels);
        expect(h.glyphs, at).toEqual(reference.glyphs);
        expect(h.chips, at).toEqual(reference.chips);
        expect(h.fontSizes, at).toEqual(reference.fontSizes);
        expect(h.labelsWhole.every(Boolean), at).toBe(true);
        await expect(page.locator('#view-help .dp-help-tabs [tabindex="0"]')).toHaveCount(1);
        await expect(page.locator(`#dp-help-tab-${order[k]}`)).toHaveAttribute('tabindex', '0');
        for (const end of h.ends.filter(end => end.shown)) {
          expect(end.width, at).toBeGreaterThanOrEqual(32);
          expect(end.height, at).toBeGreaterThanOrEqual(32);
        }
        // Each of First and Last is gone exactly where the window shows its end
        // section whole (1.0.14, 053c1f60; section-navigator-endpoints).
        const whole = i => h.shown[i] >= h.widths[i] - 0.5;
        expect(h.ends.map(end => end.shown), at).toEqual([!whole(0), !whole(n - 1)]);
        // Side padding: the complete strip's unless an end section and its
        // whole neighbour would not otherwise fit the end section's own room,
        // and then at least 5px.
        const slot = Math.max(...h.ends.map(end => end.width));
        const pairs = Math.max(natural[0] + natural[1], natural[n - 2] + natural[n - 1]) + h.edge;
        for (const padding of h.padding) {
          if (pairs + 0.5 <= h.strip.width - slot) expect(padding, at).toBe(reference.padding[0]);
          else expect(padding, at).toBeGreaterThanOrEqual(5);
        }
        expectNoHiddenCut(h, at);
        if (k === 0 || k === n - 1) await expectEndpoint(page, h, at, reference);
        else expect(h.positions, at).toBeGreaterThanOrEqual(3);
        if (width === 780 && (k === 0 || k === n - 1)) {
          test.info().annotations.push({type: `780px endpoint (${fonts})`, description:
            `${order[k]}: window ${h.window.toFixed(1)}px; ${h.order.map((id, j) => h.shown[j] > 0.5 ? `${id} ${h.shown[j].toFixed(1)}/${h.widths[j].toFixed(1)}${h.cuts[j] ? ` (${h.cuts[j]})` : ''}` : '').filter(Boolean).join(', ')}; side padding ${h.padding[0]}px (complete strip ${reference.padding[0]}px); ${h.positions} positions`});
          for (const theme of ['dark', 'light']) {
            await page.evaluate(light => document.body.classList.toggle('light', light), theme === 'light');
            await settle(page);
            await page.locator('#view-help .dp-help-master-header').screenshot({
              path: `test-results/checkpoint-help-navigator-780-${order[k]}-${fonts.split(' ')[0]}-${theme}.png`});
          }
          await page.evaluate(() => document.body.classList.remove('light'));
          await settle(page);
        }
      }
    }
  });
}

/* Where the fonts drawn leave License and its neighbour too wide for its
 * room even with the padding trim, the selected section still comes first.
 * Recreated on any host: the labels are spaced out (the tabs' widths are all
 * a font changes here) until the pair is short even after the trim, and the
 * page re-measures as it does when fonts finish loading. */
test('at the 780px floor, where the end pair cannot fit even trimmed, License stays whole and its neighbour gives up only the rest: cut, faded, named and a target', async ({page}) => {
  await openHelp(page, 1920, {fallbackFonts: true});
  const reference = await header(page);
  const order = reference.order;
  const n = order.length;
  await resize(page, 780);
  const short = h => h.widths[n - 2] + h.widths[n - 1] + 4 * (reference.padding[0] - h.padding[0]) + h.edge - h.window;
  const chars = reference.labels[n - 2].length + reference.labels[n - 1].length;
  const goal = 4 * 5 + 2 * TARGET;   // past the most trim, by more than a preview
  let spacing = 0;
  let h;
  for (;;) {
    await page.evaluate(spacing => {
      let style = document.getElementById('dp-test-short-pair');
      if (!style) document.head.append(style = Object.assign(document.createElement('style'), {id: 'dp-test-short-pair'}));
      style.textContent = `#view-help .dp-help-tab-label { letter-spacing: ${spacing}px; }`;
      document.fonts.dispatchEvent(new Event('loadingdone'));
    }, spacing);
    await settle(page);
    await choose(page, order[n - 1]);
    h = await header(page);
    if (short(h) >= goal) break;
    spacing += Math.max(0.05, (goal - short(h)) / chars);
    expect(spacing, 'the recreated shortfall').toBeLessThan(10);
  }
  const at = `License at 780px, ${short(h).toFixed(1)}px short`;
  const identity = {copyLeft: h.copyLeft, copyTop: h.copyTop};
  expect(h.compact, at).toBe(true);
  expect(problems(h, {...reference, ...identity}), at).toEqual([]);
  expect(h.widths[n - 2] + h.widths[n - 1] + h.edge - h.window, at).toBeGreaterThan(0.5);
  for (const padding of h.padding) expect(padding, at).toBeGreaterThanOrEqual(5);
  expectNoHiddenCut(h, at);
  const shown = await expectEndpoint(page, h, at, reference);
  expect(shown, at).toBeLessThan(h.widths[n - 2] - 0.5);
  const control = h.control;
  for (let k = 0; k < n - 1; k += 1) {
    await choose(page, order[k]);
    const other = await header(page);
    const where = `${order[k]} at 780px (short pair)`;
    expect(problems(other, {...reference, ...identity}), where).toEqual([]);
    expectNoHiddenCut(other, where);
    for (const key of ['left', 'right']) expect(Math.abs(other.control[key] - control[key]), where).toBeLessThanOrEqual(0.5);
    expect(other.ends.map(end => end.shown), where).toEqual([k !== 0, true]);
    if (k === 0) await expectEndpoint(page, other, where, reference);
  }
  // The preview opens its section; the neighbour, now selected, is whole.
  await choose(page, order[n - 1]);
  await page.locator(`#view-help .dp-help-tabs:not(.dp-motion-still) [data-tab="${order[n - 2]}"]`).click();
  await expect(selected(page)).toHaveAttribute('data-tab', order[n - 2]);
  await settle(page);
  expect((await header(page)).currentWhole).toBe(true);
  await choose(page, order[n - 1]);
  test.info().annotations.push({type: '780px endpoint (short pair, recreated)', description:
    `labels spaced ${spacing.toFixed(2)}px; window ${h.window.toFixed(1)}px; License ${h.shown[n - 1].toFixed(1)}/${h.widths[n - 1].toFixed(1)}px whole; ${order[n - 2]} ${shown.toFixed(1)}/${h.widths[n - 2].toFixed(1)}px cut and faded; side padding ${h.padding[0]}px`});
  for (const theme of ['dark', 'light']) {
    await page.evaluate(light => document.body.classList.toggle('light', light), theme === 'light');
    await settle(page);
    await page.locator('#view-help .dp-help-master-header').screenshot({
      path: `test-results/checkpoint-help-navigator-780-short-pair-${theme}.png`});
  }
  await page.evaluate(() => document.body.classList.remove('light'));
});

for (const fonts of ['web fonts', 'fallback fonts']) {
  test(`with ${fonts}, a Help First or Last that is gone takes no room: the window grows into its place, the list fills it, and the control stays where it is`, async ({page}) => {
    await openHelp(page, 1920, {fallbackFonts: fonts === 'fallback fonts'});
    const order = (await header(page)).order;
    const n = order.length;
    for (const width of [780, 900, 1100, 1300]) {
      await resize(page, width);
      const at = k => `${order[k]} at ${width}px`;
      await choose(page, order[Math.floor(n / 2)]);
      // (Placed afresh, as a render places it: a choice leaves the window
      // where it was drawn, which may still show an end section whole -- its
      // action then gone.)
      await page.evaluate(() => document.fonts.dispatchEvent(new Event('loadingdone')));
      await settle(page);
      const inner = await header(page);
      expect(inner.compact, `${width}px`).toBe(true);
      const [first, last] = inner.ends;
      expect(first.shown && last.shown, `${width}px`).toBe(true);
      const slot = first.width;
      expect(Math.abs(first.left - inner.strip.left), `${width}px`).toBeLessThanOrEqual(0.5);
      expect(Math.abs(last.right - inner.strip.right), `${width}px`).toBeLessThanOrEqual(0.5);
      expect(first.right, `${width}px`).toBeLessThanOrEqual(inner.strip.left + inner.clip[0] + 0.5);
      expect(last.left, `${width}px`).toBeGreaterThanOrEqual(inner.strip.right - inner.clip[1] - 0.5);
      expect(Math.abs(inner.window - (inner.strip.width - 2 * slot)), `${width}px`).toBeLessThanOrEqual(0.5);
      for (const k of [0, n - 1]) {
        await choose(page, order[k]);
        const h = await header(page);
        const gone = k === 0 ? 0 : 1;
        expect(h.ends[gone].shown, at(k)).toBe(false);
        expect(h.ends[gone].width, at(k)).toBe(0);
        expect(h.ends[1 - gone].shown, at(k)).toBe(true);
        expect(h.ends[1 - gone].width, at(k)).toBe(slot);
        for (const key of ['left', 'right', 'width']) expect(Math.abs(h.control[key] - inner.control[key]), at(k)).toBeLessThanOrEqual(0.5);
        expect(Math.abs(h.window - (inner.window + slot)), at(k)).toBeLessThanOrEqual(0.5);
        expect(Math.abs((k === 0 ? h.atStart : h.atEnd) - h.edge), at(k)).toBeLessThanOrEqual(1);
        const shown = h.shown.reduce((sum, part) => sum + part, 0) + h.hidden.reduce((sum, part) => sum + part, 0);
        expect(Math.abs(shown + h.edge - h.window), at(k)).toBeLessThanOrEqual(1);
        const hit = await page.evaluate(([x, k]) => {
          const tab = document.querySelectorAll('#view-help .dp-help-tabs [data-tab]')[k].getBoundingClientRect();
          return document.elementFromPoint(x, (tab.top + tab.bottom) / 2)?.closest('[data-tab]')?.dataset.tab;
        }, [k === 0 ? h.strip.left + h.edge + slot / 2 : h.strip.right - h.edge - slot / 2, k]);
        expect(hit, at(k)).toBe(order[k]);
        expect(problems(h, inner), at(k)).toEqual([]);
      }
      if (width === 780 || width === 1100) {
        for (const k of [0, Math.floor(n / 2), n - 1]) {
          await choose(page, order[k]);
          await page.locator('#view-help .dp-help-master-header').screenshot({
            path: `test-results/checkpoint-help-navigator-reclaim-${fonts.split(' ')[0]}-${width}-${order[k]}.png`});
        }
      }
    }
    expect(await page.evaluate(() => window.__dpEnds)).toEqual([]);
  });
}

/* Whether the window moved for the last choice, and how it slid. */
async function slideOf(page, act) {
  await page.evaluate(() => { window.__dpNav = []; window.__dpContent = []; });
  const before = await page.evaluate(() => document.querySelector('#view-help .dp-help-tabs').scrollLeft);
  await act();
  await settle(page);
  const after = await page.evaluate(() => document.querySelector('#view-help .dp-help-tabs').scrollLeft);
  const frames = await page.evaluate(() => window.__dpNav);
  const content = await page.evaluate(() => window.__dpContent);
  return {moved: after - before, frames,
    // The content viewport's own slide: its direction and the way it arrives.
    content: {directions: [...new Set(content.map(frame => frame.direction))], arriving: [...new Set(content.map(frame => frame.arriving).filter(Boolean))],
      moving: content.filter(frame => frame.moving).length}, directions: [...new Set(frames.map(frame => frame.direction))],
    moving: frames.filter(frame => frame.moving).length,
    strip: [...new Set(frames.map(frame => frame.strip).filter(Boolean))],
    panel: [...new Set(frames.map(frame => frame.panel).filter(Boolean))],
    controls: new Set(frames.map(frame => frame.control.join())).size, headers: new Set(frames.map(frame => frame.header.join())).size,
    copies: new Set(frames.map(frame => frame.copy.join())).size};
}

const expectSlide = (slide, direction, label) => {
  expect(slide.moved, label).not.toBe(0);
  expect(slide.directions, label).toEqual([direction]);
  // Seen, not only asked for: the strip's destination arrives from the
  // right going forward, from the left going back -- as the panel's does.
  expect(slide.strip, label).toEqual([direction === 'forward' ? 1 : -1]);
  expect(slide.panel, label).toEqual(slide.strip);
  expect(slide.moving, label).toBeGreaterThanOrEqual(3);       // drawn over several frames
  expect(slide.controls, label).toBe(1);                        // the control, the header and the identity stay
  expect(slide.headers, label).toBe(1);
  expect(slide.copies, label).toBe(1);
};

/* The first or last section chosen where the window already shows it: the
 * selector is in its end arrangement at once -- never slid -- while
 * the content viewport slides as for any choice, the way the choice goes. */
const expectSnap = (slide, direction, label) => {
  // (Its scroll may already be the end arrangement's: then only the travel
  // action goes and the window takes its place.)
  expect(slide.frames, `${label}: no selector slide`).toEqual([]);
  expect(slide.content.directions, `${label}: the content slides`).toEqual([direction]);
  expect(slide.content.arriving, label).toEqual([direction === 'forward' ? 1 : -1]);
  expect(slide.content.moving, label).toBeGreaterThanOrEqual(3);
};

test('the Help window slides only when a choice moves it, the same way as the panel: neighbours, edges, First and Last, into either end, and a quick return turns back', async ({page}) => {
  await openHelp(page, 1100);
  const order = (await header(page)).order;

  // A whole section the window already shows: where the window stays,
  // nothing slides (the panel still turns); where it moves, it slides the
  // way the choice goes.
  let h;
  let slide;
  let stayed = 0;
  for (const width of [1300, 1100]) {
    await resize(page, width);
    for (let a = 1; a < order.length - 1 && !stayed; a += 1) {
      await choose(page, order[a]);
      h = await header(page);
      const whole = h.shown.map((part, k) => k).filter(k => k !== a && k > 0 && k < order.length - 1 && h.shown[k] >= h.widths[k] - 0.5);
      for (const b of whole.slice(0, 2)) {
        slide = await slideOf(page, () => choose(page, order[b]));
        if (slide.moved) expectSlide(slide, b > a ? 'forward' : 'backward', `${order[a]} to ${order[b]} at ${width}px`);
        else {
          expect(slide.frames, `${order[a]} to ${order[b]} at ${width}px`).toEqual([]);
          stayed += 1;
          test.info().annotations.push({type: 'in-window choice', description: `${order[a]} to ${order[b]} at ${width}px: the window stayed, no selector slide`});
        }
        await choose(page, order[a]);
      }
    }
  }
  expect(stayed, 'choices the window already holds').toBeGreaterThan(0);
  await resize(page, 1100);
  await choose(page, order[0]);

  // The first section the window does not show whole (cut at the edge, or
  // beyond it): the window moves forward, and slides.
  h = await header(page);
  const cut = h.shown.findIndex((part, k) => k > h.index && part < h.widths[k] - 0.5);
  expect(cut).toBeGreaterThan(h.index);
  slide = await slideOf(page, () => choose(page, order[cut]));
  expectSlide(slide, 'forward', `${order[cut]}, beyond the window's edge`);

  // Last: forward into the end arrangement; First: back to the start.
  slide = await slideOf(page, () => page.locator('#view-help .dp-help-nav-end[data-help-end="last"]').click());
  expectSlide(slide, 'forward', 'Last');
  h = await header(page);
  expect(Math.abs(h.atEnd - h.edge)).toBeLessThanOrEqual(1);
  slide = await slideOf(page, () => page.locator('#view-help .dp-help-nav-end[data-help-end="first"]').click());
  expectSlide(slide, 'backward', 'First');
  h = await header(page);
  expect(Math.abs(h.atStart - h.edge)).toBeLessThanOrEqual(1);

  // Walking to the end one section at a time and back: each move that
  // shifts the window slides the way it goes; the others move nothing.
  let slid = 0;
  for (let k = 1; k < order.length; k += 1) {
    const exposed = (await header(page)).shown[k] >= TARGET;
    slide = await slideOf(page, () => choose(page, order[k]));
    if (k === order.length - 1 && exposed) expectSnap(slide, 'forward', `${order[k]}, shown: snapped`);
    else if (slide.moved) { expectSlide(slide, 'forward', order[k]); slid += 1; }
    else expect(slide.frames, order[k]).toEqual([]);
  }
  expect(slid).toBeGreaterThan(0);
  slid = 0;
  for (let k = order.length - 2; k >= 0; k -= 1) {
    const exposed = (await header(page)).shown[k] >= TARGET;
    slide = await slideOf(page, () => choose(page, order[k]));
    if (k === 0 && exposed) expectSnap(slide, 'backward', `${order[k]}, shown: snapped`);
    else if (slide.moved) { expectSlide(slide, 'backward', order[k]); slid += 1; }
    else expect(slide.frames, order[k]).toEqual([]);
  }
  expect(slid).toBeGreaterThan(0);
  expect(await page.evaluate(() => window.__dpEnds)).toEqual([]);

  // A quick return: forward and straight back, in one task -- the window
  // and the panel turn back with it, never finishing the forward turn.
  await choose(page, order[3]);
  await selected(page).focus();
  const placed = await page.evaluate(() => document.querySelector('#view-help .dp-help-tabs').scrollLeft);
  slide = await slideOf(page, () => page.evaluate(() => {
    const press = key => document.activeElement.dispatchEvent(new KeyboardEvent('keydown', {key, bubbles: true, cancelable: true}));
    press('ArrowRight');
    press('ArrowLeft');
  }));
  await expect(selected(page)).toHaveAttribute('data-tab', order[3]);
  await expect(page.locator('#view-help .dp-help-panel:not([hidden])')).toHaveAttribute('data-panel', order[3]);
  expect(await page.evaluate(() => document.querySelector('#view-help .dp-help-tabs').scrollLeft)).toBe(placed);
  expect(slide.strip.filter(way => way > 0), 'no forward turn of the window').toEqual([]);
  expect(slide.panel.filter(way => way > 0), 'no forward turn of the panel').toEqual([]);

  // Rapid choices, interrupting each other: the last one wins, settled.
  await selected(page).focus();
  for (const key of ['Home', 'End', 'Home', 'ArrowRight', 'End']) await page.keyboard.press(key);
  await settle(page);
  await expect(selected(page)).toHaveAttribute('data-tab', 'license');
  await expect(page.locator('#view-help .dp-help-panel:not([hidden])')).toHaveAttribute('data-panel', 'license');
  h = await header(page);
  expect(h.currentWhole).toBe(true);
  expect(Math.abs(h.atEnd - h.edge)).toBeLessThanOrEqual(1);
});

/* Stable edge targets. Choosing the rightmost section the window shows moves
 * it on by just as much as puts the next section where the chosen one was;
 * choosing the leftmost, the previous one. So the operator clicks the edge
 * preview again and again at one point, without moving the pointer, and
 * each click chooses the next section -- read from what is under that point
 * before each click, not from scroll offsets. The selection is whole after
 * every move. Where the window then shows the first or last section, choosing
 * it puts the selector in its end arrangement at once (no selector slide;
 * its travel action gone, its place the window's) while the content
 * viewport slides as for any choice. */
for (const fonts of ['web fonts', 'fallback fonts']) {
  test(`with ${fonts}, the Help edge preview clicked again and again at one point chooses the next section each time, both ways, and the end section snaps while the content slides`, async ({page}) => {
    await openHelp(page, 1920, {fallbackFonts: fonts === 'fallback fonts'});
    const order = (await header(page)).order;
    const n = order.length;
    const under = point => page.evaluate(([x, y]) => document.elementFromPoint(x, y)?.closest('[data-tab]')?.dataset.tab ?? null, [point.x, point.y]);
    const rows = [];
    for (const width of [780, 1100]) {
      await resize(page, width);
      for (const dir of ['forward', 'backward']) {
        const step = dir === 'forward' ? 1 : -1;
        await choose(page, order[dir === 'forward' ? 0 : n - 1]);
        let h = await header(page);
        expect(h.compact, `${width}px`).toBe(true);
        // The edge preview: the rightmost (leftmost) part of TARGET or more.
        const shown = h.shown.map((part, k) => (part >= TARGET && k !== h.index ? k : -1)).filter(k => k >= 0);
        let expected = dir === 'forward' ? shown.at(-1) : shown[0];
        const point = await page.evaluate(([sel, k]) => {
          const strip = document.querySelector(sel);
          const box = strip.getBoundingClientRect();
          const inset = (/inset\(([^)]+)\)/.exec(getComputedStyle(strip).clipPath)?.[1] || '0').split(/\s+/).map(parseFloat);
          const [top, right = top, , left = right] = inset;
          const t = strip.querySelectorAll('[data-tab]')[k].getBoundingClientRect();
          return {x: (Math.max(t.left, box.left + left) + Math.min(t.right, box.right - right)) / 2, y: (t.top + t.bottom) / 2};
        }, ['#view-help .dp-help-tabs', expected]);
        const trail = [];
        for (;;) {
          const label = `${dir} at ${width}px, click ${trail.length + 1} at x=${point.x.toFixed(1)}`;
          expect(await under(point), `${label}: the next section is under the pointer`).toBe(order[expected]);
          const atEnd = expected === 0 || expected === n - 1;
          const slide = await slideOf(page, () => page.mouse.click(point.x, point.y));
          await expect(selected(page), label).toHaveAttribute('data-tab', order[expected]);
          h = await header(page);
          expect(h.currentWhole, `${label}: the selection whole`).toBe(true);
          expect(h.labelWhole, label).toBe(true);
          expect(problems(h, h), label).toEqual([]);
          expectNoHiddenCut(h, label);
          trail.push(order[expected]);
          if (atEnd) {
            expectSnap(slide, dir, label);
            // Its travel action gone, the window in its place, the section at its end.
            expect(h.ends.map(end => end.shown), label).toEqual([expected !== 0, expected !== n - 1]);
            expect(Math.abs((expected === 0 ? h.atStart : h.atEnd) - h.edge), label).toBeLessThanOrEqual(1);
            break;
          }
          if (slide.moved) expectSlide(slide, dir, label);
          else expect(slide.frames, label).toEqual([]);
          expected += step;
        }
        rows.push(`${dir} ${width}px at x=${point.x.toFixed(1)}: ${trail.join(' > ')}`);
        if (width === 780) await page.locator('#view-help .dp-help-master-header').screenshot({
          path: `test-results/checkpoint-help-navigator-repeat-${fonts.split(' ')[0]}-${dir}-780.png`});
      }
    }
    expect(await page.evaluate(() => window.__dpEnds)).toEqual([]);
    test.info().annotations.push({type: `one point, repeated (${fonts})`, description: rows.join('; ')});
  });
}

/* A quick choice of a shown end section, made while a slide of the selector
 * is still to run, ends it: the selector is drawn in its end arrangement and
 * no older slide plays over it. */
test('a Help end section chosen straight after a move that slides the selector snaps, and no older slide plays over it', async ({page}) => {
  await openHelp(page, 1100);
  const order = (await header(page)).order;
  const n = order.length;
  await choose(page, order[n - 1]);
  const h0 = await header(page);
  const leftmost = h0.shown.findIndex((part, k) => part >= TARGET && k !== h0.index);
  expect(h0.shown[n - 1]).toBeGreaterThanOrEqual(TARGET);
  const slide = await slideOf(page, () => page.evaluate(([sel, k]) => {
    const tabs = [...document.querySelectorAll(`${sel} [data-tab]`)];
    tabs[k].click();          // backward: the selector would slide
    tabs.at(-1).click();      // the shown last section, in the same task
  }, ['#view-help .dp-help-tabs', leftmost]));
  await expect(selected(page)).toHaveAttribute('data-tab', order[n - 1]);
  expect(slide.moving, 'no selector slide drawn').toBe(0);
  expect(slide.strip, 'no selector slide drawn').toEqual([]);
  const h = await header(page);
  expect(h.currentWhole).toBe(true);
  expect(Math.abs(h.atEnd - h.edge)).toBeLessThanOrEqual(1);
  expect(h.ends.map(end => end.shown)).toEqual([true, false]);
  expect(slide.moved).toBe(0);                                   // where it was, in its end arrangement
  expect(await page.evaluate(sel => document.querySelector(sel).classList.contains('dp-motion-surface'), '#view-help .dp-help-tabs')).toBe(false);
});

test('with reduced motion the Help window moves at once and never slides', async ({page}) => {
  await page.emulateMedia({reducedMotion: 'reduce'});
  await openHelp(page, 1100);
  const order = (await header(page)).order;
  const slide = await slideOf(page, () => page.locator('#view-help .dp-help-nav-end[data-help-end="last"]').click());
  expect(slide.moved).toBeGreaterThan(0);
  expect(slide.frames).toEqual([]);
  const h = await header(page);
  expect(h.index).toBe(order.length - 1);
  expect(Math.abs(h.atEnd - h.edge)).toBeLessThanOrEqual(1);
});

test('the Help presentation changing keeps the section, its panel and its open accordion, the content in place and focus; entering Help again opens Quick Start without a slide; a choice scrolls the content to its top', async ({page}) => {
  await openHelp(page, 1920);
  await page.locator('#dp-help-tab-howitworks').click();
  await expect(page.locator('#dp-help-panel-howitworks')).toBeVisible();
  await settle(page);
  await page.evaluate(() => document.querySelector('#dp-help-panel-howitworks details.dp-help-accordion:not([open])').classList.add('dp-test-opened'));
  await page.locator('#dp-help-panel-howitworks details.dp-test-opened > summary').click();
  const opened = page.locator('#dp-help-panel-howitworks details.dp-test-opened');
  await expect(opened).toHaveAttribute('open', '');
  await settle(page);
  await page.locator('#dp-help-tab-howitworks').focus();
  await page.evaluate(() => { window.__dpPanel = document.getElementById('dp-help-panel-howitworks'); });
  for (const width of [1100, 780, 1440, 900, 1920]) {
    await resize(page, width);
    await expect(selected(page)).toHaveAttribute('data-tab', 'howitworks');
    expect(await page.evaluate(() => window.__dpPanel === document.querySelector('#view-help .dp-help-panel:not([hidden])'))).toBe(true);
    await expect(opened).toHaveAttribute('open', '');
    await expect(page.locator('#dp-help-tab-howitworks')).toBeFocused();
  }

  // A choice opens its section at the top of the content.
  await resize(page, 1100);
  await page.evaluate(() => { const scroller = document.querySelector('#view-help .dp-help-scroll'); scroller.scrollTop = scroller.scrollHeight; });
  expect(await page.evaluate(() => document.querySelector('#view-help .dp-help-scroll').scrollTop)).toBeGreaterThan(0);
  await choose(page, 'services');
  expect(await page.evaluate(() => document.querySelector('#view-help .dp-help-scroll').scrollTop)).toBe(0);

  // Leaving and entering Help again: Quick Start, its window at the start, no slide.
  await choose(page, 'license');
  await page.locator('#sidebar .nav-item[data-view="dashboard"]:not([data-title])').click();
  await expect(page.locator('#view-help')).not.toHaveClass(/\bactive\b/);
  await page.evaluate(() => { window.__dpNav = []; });
  await page.locator('#sidebar .nav-item[data-view="help"]:not([data-title])').click();
  await expect(selected(page)).toHaveAttribute('data-tab', 'quickstart');
  await expect(page.locator('#dp-help-panel-quickstart')).toBeVisible();
  await settle(page);
  const h = await header(page);
  expect(h.compact).toBe(true);
  expect(Math.abs(h.atStart - h.edge)).toBeLessThanOrEqual(1);
  expect(await page.evaluate(() => window.__dpNav)).toEqual([]);
});

test('below 780px the Help handheld header is one row with the same compact navigator', async ({page}) => {
  // 1.0.14: the former handheld header (the complete strip, the header's width, scrolling beneath the
  // copy, no compact navigator) is retired; the identity gives way instead (settings-help-mobile-navigator.spec.js).
  await openHelp(page, 1440);
  await resize(page, 779);
  const h = await page.evaluate(() => {
    const rect = node => node.getBoundingClientRect();
    const root = document.getElementById('view-help');
    const header = root.querySelector('.dp-help-master-header'), control = root.querySelector('.dp-help-nav-control');
    const copy = root.querySelector('.dp-help-header-copy');
    const strip = root.querySelector('.dp-help-tabs');
    const middle = node => (rect(node).top + rect(node).bottom) / 2;
    return {presentation: header.dataset.presentation, oneRow: !copy.getClientRects().length || Math.abs(middle(copy) - middle(control)) < 14,
      ends: [...root.querySelectorAll('.dp-help-nav-end')].some(node => getComputedStyle(node).display !== 'none'),
      tabs: strip.querySelectorAll('[data-tab]').length, overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth};
  });
  expect(h).toEqual({presentation: 'compact', oneRow: true, ends: true, tabs: 8, overflow: 0});
  await page.locator('#dp-help-tab-license').click();
  await expect(page.locator('#dp-help-panel-license')).toBeVisible();
  await page.locator('#view-help .dp-help-master-header').screenshot({path: 'test-results/checkpoint-help-header-779.png'});
});
