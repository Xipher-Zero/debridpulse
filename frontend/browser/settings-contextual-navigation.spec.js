const { test, expect } = require('@playwright/test');

/* The Settings master header's section navigation, desktop and tablet
 * (780px and wider): one row. The complete strip keeps its original place,
 * centred in the whole header (half the 16px row gap toward the copy's
 * side), and is shown only while that place leaves the Tuning Deck copy its
 * own width with the documented 4px to spare; otherwise the compact
 * navigator (First, a faded part of the previous tab, the current tab, a
 * faded part of the next, Last) is centred in the room between the copy and
 * the card's right edge. Below 780px the handheld header keeps its wrapping
 * strip.
 *
 * Whether the complete strip fits is computed here from the rendered tabs'
 * own parts (chip, gap, label, padding, border) and the copy's own parts --
 * whatever fonts this browser drew -- not read from the page's decision. */

const SPARE = 4;

// Two frames drawn, and no finite animation (the page's navigation and panel
// motion) still running: hit-testing and geometry are the page's own.
const settle = async page => {
  await page.waitForFunction(() => document.getAnimations()
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
  return page.evaluate(({SPARE, frame}) => {
    const rect = node => node.getBoundingClientRect();
    const root = document.getElementById('view-settings');
    const headerNode = root.querySelector('.dp-settings-master-header');
    const style = getComputedStyle(headerNode);
    const box = rect(headerNode);
    const inner = {left: box.left + parseFloat(style.paddingLeft), right: box.right - parseFloat(style.paddingRight)};
    const gap = parseFloat(style.columnGap);
    const copyNode = root.querySelector('.dp-settings-header-copy');
    const copy = rect(copyNode);
    const parts = node => {
      const kids = [...node.children];
      return kids.reduce((sum, kid) => sum + rect(kid).width, 0)
        + (parseFloat(getComputedStyle(node).columnGap) || 0) * (kids.length - 1);
    };
    const copyNatural = parts(copyNode);
    const strip = root.querySelector('.dp-settings-tabs');
    const s = rect(strip);
    const tabNodes = [...strip.querySelectorAll('[data-tab]')];
    const natural = tab => {
      const st = getComputedStyle(tab);
      return parts(tab.querySelector('.dp-settings-tab-face') || tab)
        + ['paddingLeft', 'paddingRight', 'borderLeftWidth', 'borderRightWidth'].reduce((sum, key) => sum + parseFloat(st[key]), 0);
    };
    const naturals = tabNodes.map(natural);
    const stripNatural = naturals.reduce((sum, width) => sum + width, 0);
    const ends = [...root.querySelectorAll('.dp-settings-nav-end')];
    const shownEnds = ends.filter(node => getComputedStyle(node).display !== 'none');
    const control = rect(strip.parentElement.matches('.dp-settings-nav-control') ? strip.parentElement : strip);
    const current = strip.querySelector('[aria-selected="true"]');
    const c = rect(current);
    const label = current.querySelector('.dp-settings-tab-label');
    const centre = (control.left + control.right) / 2;
    return {
      width: innerWidth, height: box.height, copyLeft: copy.left - box.left, copyTop: copy.top - box.top,
      level: Math.abs((control.top + control.bottom) / 2 - (copy.top + copy.bottom) / 2),
      inside: control.left >= copy.left + copyNatural + gap - 0.5 && control.right <= inner.right + 0.5
        && control.top >= box.top && control.bottom <= box.bottom,
      // The complete strip's original place: the header's centre plus half the gap.
      headerOffset: centre - ((inner.left + inner.right) / 2 + gap / 2),
      // The compact navigator's place: the centre of the room after the copy.
      roomOffset: centre - (copy.left + copyNatural + gap + inner.right) / 2,
      // Centred in the header, the space left on the copy's side beyond the
      // copy's own width and the spare (>= 0: the complete strip fits).
      side: (inner.right - inner.left - stripNatural - frame - gap) / 2 - copyNatural - SPARE,
      stripNatural,
      clearance: control.left - (copy.left + copyNatural),
      compact: shownEnds.length > 0,
      endWidths: shownEnds.map(node => rect(node).width),
      allTabsInside: tabNodes.every(tab => {
        const t = rect(tab);
        return t.width > 0 && t.left >= s.left - 0.5 && t.right <= s.right + 0.5 && Math.abs(t.top - s.top - (s.height - t.height) / 2) < 1.5;
      }),
      scrolls: strip.scrollWidth > strip.clientWidth + 1,
      currentInside: c.left >= s.left - 0.5 && c.right <= s.right + 0.5,
      currentCentred: Math.abs((c.left + c.right) / 2 - centre),
      currentWidth: c.width, widest: Math.max(...naturals),
      labelWhole: label.scrollWidth <= label.clientWidth + 0.5 && rect(label).left >= c.left && rect(label).right <= c.right,
      control: {left: control.left, right: control.right, width: control.width},
      current: {left: c.left, right: c.right},
      slots: [c.left - s.left, s.right - c.right],
    };
  }, {SPARE, frame});
}

/* The one-row contract at this width. Returns the problems. */
function problems(h, reference) {
  const out = [];
  const borderline = Math.abs(h.side) < 0.5;
  if (h.level > 1) out.push('navigation not on the copy\'s row');
  if (!h.inside) out.push('navigation outside its room');
  if (Math.abs(h.height - reference.height) > 0.5) out.push(`header height ${h.height}`);
  if (Math.abs(h.copyLeft - reference.copyLeft) > 0.5 || Math.abs(h.copyTop - reference.copyTop) > 0.5) out.push('identity moved');
  if (!borderline && h.side >= 0 && h.compact) out.push('compact although the centred strip fits');
  if (!borderline && h.side < 0 && !h.compact) out.push('complete strip although it would meet the copy');
  if (!h.compact) {
    if (!h.allTabsInside || h.scrolls) out.push('complete strip clipped or scrolling');
    if (Math.abs(h.headerOffset) > 0.5) out.push(`complete strip off its centred place (${h.headerOffset.toFixed(1)})`);
    if (h.clearance < 16 + SPARE - 0.5) out.push(`complete strip ${h.clearance.toFixed(1)}px from the copy`);
  } else {
    if (Math.abs(h.roomOffset) > 1) out.push(`compact navigator not centred in its room (${h.roomOffset.toFixed(1)})`);
    if (!h.currentInside || h.currentCentred > 1) out.push('current tab not whole in the centre');
    if (h.currentWidth < h.widest - 0.5) out.push('centre slot narrower than the widest tab');
    if (!h.labelWhole) out.push('current label clipped');
    if (h.endWidths.some(width => width < 24)) out.push('travel action too small');
    if (Math.abs(h.slots[0] - h.slots[1]) > 1) out.push('unequal neighbour slots');
  }
  return out;
}

for (const fonts of ['web fonts', 'fallback fonts']) {
  test(`the Settings header is one row from 1920px to 780px with ${fonts}: the complete strip at its original centred place while it clears the copy, the compact navigator from the point it would not`, async ({page}) => {
    await openSettings(page, 1920, {fallbackFonts: fonts === 'fallback fonts'});
    const first = await header(page);
    expect(first.compact).toBe(false);
    const frame = first.control.width - first.stripNatural;   // the strip's padding and border
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

    /* The threshold, from the measured parts: the space beside the centred
     * strip grows by half of each pixel the window gains, within one sidebar
     * width (full sidebar from 1560px, rail below). */
    const thresholds = [];
    for (const [probe, low, high] of [[1920, 1560, 1920], [1300, 901, 1559]]) {
      await resize(page, probe);
      const at = await header(page, frame);
      const width = Math.ceil(probe - 2 * at.side);
      // (Outside its regime: that sidebar width is compact throughout, which
      // the widths above already checked.)
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
      test.info().annotations.push({type: `threshold (${fonts})`,
        description: `complete strip from ${threshold}px: ${(at.side + SPARE).toFixed(1)}px beside the copy there (spare ${SPARE}px), clearance ${at.clearance.toFixed(1)}px`});
    }
  });
}

test('the compact navigator: the current tab centred, outward-fading neighbours that open their tab, First and Last, the keyboard', async ({page}) => {
  await openSettings(page, 1100);
  const strip = page.locator('#view-settings .dp-settings-tabs');
  const order = await strip.locator('[data-tab]').evaluateAll(tabs => tabs.map(tab => tab.dataset.tab));
  const names = await strip.locator('[data-tab]').evaluateAll(tabs => tabs.map(tab => tab.textContent.trim()));
  expect(order.length).toBeGreaterThan(2);
  const first = page.locator('#view-settings .dp-settings-nav-end[data-settings-end="first"]');
  const last = page.locator('#view-settings .dp-settings-nav-end[data-settings-end="last"]');
  await expect(last).toHaveAccessibleName(`Last section: ${names.at(-1)}`);
  // Actions, not tabs: the tablist holds exactly the sections.
  await expect(page.getByRole('tablist', {name: 'Settings sections'}).getByRole('tab')).toHaveCount(order.length);

  /* What the navigator shows now: the selected tab, its neighbours' visible
   * parts (within the strip's window) and their fades, and the two actions. */
  const view = () => page.evaluate(() => {
    const rect = node => node.getBoundingClientRect();
    const strip = document.querySelector('#view-settings .dp-settings-tabs');
    const s = rect(strip);
    const current = strip.querySelector('[aria-selected="true"]');
    const neighbour = node => {
      if (!node) return null;
      const t = rect(node);
      const face = node.querySelector('.dp-settings-tab-face');
      const visible = Math.max(0, Math.min(t.right, s.right) - Math.max(t.left, s.left));
      return {visible, face: rect(face).width, mask: getComputedStyle(face).maskImage,
        natural: parseFloat(node.style.getPropertyValue('--dp-settings-tab-natural')),
        point: {x: (Math.max(t.left, s.left) + Math.min(t.right, s.right)) / 2, y: (t.top + t.bottom) / 2}};
    };
    const end = name => {
      const node = document.querySelector(`#view-settings .dp-settings-nav-end[data-settings-end="${name}"]`);
      return {visibility: getComputedStyle(node).visibility, ...JSON.parse(JSON.stringify(rect(node)))};
    };
    const c = rect(current);
    return {tab: current.dataset.tab, panel: document.querySelector('#view-settings [data-panel]:not([hidden])')?.dataset.panel,
      current: {left: c.left, right: c.right}, control: JSON.parse(JSON.stringify(rect(strip.parentElement))),
      previous: neighbour(current.previousElementSibling), next: neighbour(current.nextElementSibling),
      first: end('first'), last: end('last')};
  });
  const opened = async tab => {
    await expect(selected(page)).toHaveAttribute('data-tab', tab);
    await expect(page.locator(`#view-settings [data-panel="${tab}"]`)).toBeVisible();
    await expect(page.locator('#view-settings [data-panel]:not([hidden])')).toHaveCount(1);
    await settle(page);
  };

  // At the first section: no First, no previous neighbour; the next one shows.
  await opened(order[0]);
  const atFirst = await view();
  expect(atFirst.first.visibility).toBe('hidden');
  expect(atFirst.last.visibility).toBe('visible');
  expect(atFirst.previous).toBeNull();
  expect(atFirst.next.visible).toBeGreaterThan(30);
  expect(atFirst.next.mask).toContain('linear-gradient');
  await expect(first).not.toBeFocused();
  await first.evaluate(node => node.focus());
  await expect(first).not.toBeFocused();   // a hidden action takes no focus

  // A neighbour opens its own tab, through the canonical choice.
  await page.mouse.click(atFirst.next.point.x, atFirst.next.point.y);
  await opened(order[1]);
  await expect(first).toHaveAccessibleName(`First section: ${names[0]}`);
  const middle = await view();
  for (const side of ['previous', 'next']) {
    const part = middle[side];
    expect(part.mask, side).toContain('linear-gradient');
    // About 70% of its own width, never more than its slot.
    expect(part.face + 10, side).toBeLessThanOrEqual(Math.min(part.natural * 0.7, part.visible) + 0.5);
    expect(part.face + 10, side).toBeGreaterThanOrEqual(Math.min(part.natural * 0.7, part.visible) - 1);
  }
  expect(Math.abs(middle.previous.visible - middle.next.visible)).toBeLessThanOrEqual(1);
  // The navigator and its centre slot do not move whichever tab is current.
  for (const key of ['left', 'right']) {
    expect(Math.abs(middle.current[key] - atFirst.current[key])).toBeLessThanOrEqual(0.5);
    expect(Math.abs(middle.control[key] - atFirst.control[key])).toBeLessThanOrEqual(0.5);
  }
  await page.mouse.click(middle.previous.point.x, middle.previous.point.y);
  await opened(order[0]);

  // Last and First reach the ends, and focus follows to the tab they open.
  await last.focus();
  await page.keyboard.press('Enter');
  await opened(order.at(-1));
  await expect(page.locator(`#view-settings [data-tab="${order.at(-1)}"]`)).toBeFocused();
  const atLast = await view();
  expect(atLast.last.visibility).toBe('hidden');
  expect(atLast.first.visibility).toBe('visible');
  expect(atLast.next).toBeNull();
  for (const key of ['left', 'right']) {
    expect(Math.abs(atLast.current[key] - atFirst.current[key])).toBeLessThanOrEqual(0.5);
    expect(Math.abs(atLast.control[key] - atFirst.control[key])).toBeLessThanOrEqual(0.5);
    expect(Math.abs(atLast.first[key] - atFirst.first[key])).toBeLessThanOrEqual(0.5);
  }
  await first.click();
  await opened(order[0]);
  await expect(page.locator(`#view-settings [data-tab="${order[0]}"]`)).toBeFocused();

  // The keyboard walks every section in order, the current one centred.
  await page.keyboard.press('ArrowRight');
  await opened(order[1]);
  await expect(page.locator(`#view-settings [data-tab="${order[1]}"]`)).toBeFocused();
  await page.keyboard.press('End');
  await opened(order.at(-1));
  await page.keyboard.press('Home');
  await opened(order[0]);
  await page.keyboard.press('ArrowLeft');
  await opened(order.at(-1));
  await expect(page.locator(`#view-settings [data-tab="${order.at(-1)}"]`)).toBeFocused();
  // One tab stop: the selected tab; the neighbours keep their whole names.
  await expect(strip.locator('[tabindex="0"]')).toHaveCount(1);
  await expect(strip.locator('[aria-selected="true"]')).toHaveCount(1);
  await expect(page.getByRole('tab', {name: names.at(-2), exact: true})).toHaveAttribute('aria-selected', 'false');

  // A middle section at the 780px floor, dark and light.
  await page.keyboard.press('Home');
  await page.keyboard.press('ArrowRight');
  await page.keyboard.press('ArrowRight');
  await opened(order[2]);
  await resize(page, 780);
  for (const theme of ['dark', 'light']) {
    await page.evaluate(light => document.body.classList.toggle('light', light), theme === 'light');
    await settle(page);
    await page.locator('#view-settings .dp-settings-master-header')
      .screenshot({path: `test-results/checkpoint-settings-navigator-780-${theme}.png`});
  }
  const floor = await view();
  expect(floor.previous.visible).toBeGreaterThan(40);
  expect(Math.abs(floor.previous.visible - floor.next.visible)).toBeLessThanOrEqual(1);
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
