const { test, expect } = require('@playwright/test');

/* The desktop shell from its 780px mode change up, read from the rendered page:
 * one 22px horizontal inset on both sides; every page's title at the one 23px
 * desktop title size, on one line, with its pulse artwork 4px beside it; and
 * the operational controls (Pause All, Resume All, Resume Paused) glyph-only at
 * and below 1023px, labelled above, on one row clear of the title. With Resume
 * Paused beside Pause All below 788px the artwork is drawn 10% smaller, never
 * hidden. Below 780px the handheld shell keeps its own inset, title and
 * controls. */

const DESKTOP = [1920, 1560, 1440, 1181, 1180, 1179, 1024, 1023, 900, 788, 787, 780];
const VIEWS = ['dashboard', 'torrents', 'events', 'stats', 'settings', 'help'];
const COMPACT_MAX = 1023;

const settle = page => page.evaluate(() => new Promise(resolve => {
  const still = () => !document.querySelector('.dp-motion-surface') && document.getAnimations()
    .every(animation => animation.playState !== 'running' || animation.effect?.getTiming().iterations === Infinity);
  const wait = () => (still() ? requestAnimationFrame(() => requestAnimationFrame(resolve)) : setTimeout(wait, 30));
  wait();
}));

async function open(page, width) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.setViewportSize({width, height: 900});
  await page.goto('/');
  await page.evaluate(() => document.fonts.ready);
}

async function resize(page, width) {
  await page.setViewportSize({width, height: 900});
  await page.waitForFunction(() => document.getElementById('sidebar').getAnimations().every(a => a.playState !== 'running'));
}

async function show(page, view) {
  await page.evaluate(view => nav(document.querySelector(`#sidebar .nav-item[data-view="${view}"]:not([data-title])`)), view);
  await settle(page);
}

/* The topbar as drawn. `worst` puts both Resume Paused (12) and Pause All on
 * the rail and fills the speed badge with its widest text, in the same task as
 * the measurement. The title's text is measured where it is rendered: the
 * element's own text, or Activity Log's ::before. */
const measure = (page, view, worst) => page.evaluate(([view, worst]) => {
  processingPaused = false;
  pausedTransferCount = worst ? 12 : 0;
  renderTopbarActions();
  if (worst) {
    for (const [id, text] of [['runtime-badge-active', '10'], ['runtime-badge-max', '10'], ['runtime-badge-speed', '999.99 MB/s'], ['runtime-badge-limit', '100.00 MB/s']]) {
      document.getElementById(id).textContent = text;
    }
  }
  const box = element => element.getBoundingClientRect();
  const content = document.getElementById('content'), style = getComputedStyle(content), frame = box(content), viewBox = box(document.getElementById(`view-${view}`));
  const title = document.getElementById('page-title'), titleBox = box(title), drawn = getComputedStyle(title, '::before');
  const text = drawn.content && drawn.content !== 'none' && drawn.content !== 'normal' ? drawn : getComputedStyle(title);
  const topbar = document.getElementById('topbar'), bar = box(topbar);
  const controls = [...topbar.children].filter(element => !element.matches('.dp-page-heading, template') && element.getClientRects().length)
    .flatMap(element => (element.id === 'topbar-actions' ? [...element.children].filter(child => child.getClientRects().length) : [element]));
  const buttons = [...document.querySelectorAll('#topbar-actions .btn')].filter(button => button.getClientRects().length);
  const chevron = document.querySelector('.topbar-theme-control .dp-theme-picker-trigger');
  return {shellX: getComputedStyle(document.documentElement).getPropertyValue('--dp-shell-x').trim(),
    padding: [parseFloat(style.paddingLeft), parseFloat(style.paddingRight)], inset: [viewBox.left - frame.left, frame.right - viewBox.right],
    art: getComputedStyle(title, '::after').display, artSize: [parseFloat(getComputedStyle(title, '::after').width), parseFloat(getComputedStyle(title, '::after').marginLeft)], fontSize: parseFloat(text.fontSize), lineHeight: parseFloat(text.fontSize) * parseFloat(text.lineHeight),
    titleHeight: titleBox.height, headingRight: box(title.closest('.dp-page-heading')).right, titleRight: titleBox.right, row: [getComputedStyle(title).display, getComputedStyle(title).flexWrap, getComputedStyle(title).whiteSpace],
    clearance: Math.min(...controls.map(control => box(control).left)) - titleBox.right,
    rows: new Set(controls.map(control => Math.round(box(control).top))).size,
    inBar: controls.every(control => box(control).top >= bar.top && box(control).bottom <= bar.bottom),
    lastRight: Math.max(...controls.map(control => box(control).right)), width: innerWidth,
    chevronRight: chevron && chevron.getClientRects().length ? box(chevron).right : null,
    buttons: buttons.map(button => ({id: button.id, width: box(button).width, glyph: !!button.querySelector('svg'),
      labelShown: [...button.children].some(child => child.matches('[data-dp-icon-label]') && Number(getComputedStyle(child).opacity) > 0.5), tooltip: button.dataset.tooltip})),
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth};
}, [view, worst]);

test('from 780px up: a 22px inset, one 23px one-line title beside its artwork on every page, clear of the controls', async ({page}) => {
  test.setTimeout(120_000);
  await open(page, 1920);
  for (const width of [...DESKTOP, 779]) {
    await resize(page, width);
    for (const view of VIEWS) {
      await show(page, view);
      const g = await measure(page, view, false), at = `${view} at ${width}px`;
      expect(g.overflow, `${at}: the page overflows`).toBe(0);
      if (width === 779) {
        expect([g.shellX, g.art], `${at}: the handheld shell changed`).toEqual(['max(10px, 0px)', 'none']);
        continue;
      }
      expect(g.shellX, `${at}: --dp-shell-x`).toBe('22px');
      expect(g.padding, `${at}: the content padding`).toEqual([22, 22]);
      g.inset.forEach(inset => expect(Math.abs(inset - 22), `${at}: the view is not inset 22px`).toBeLessThanOrEqual(0.5));
      expect(g.fontSize, `${at}: the rendered title size`).toBe(23);
      expect(g.art, `${at}: the artwork is hidden`).not.toBe('none');
      expect(g.artSize, `${at}: the artwork's size or lead`).toEqual([105, 4]);
      expect(g.row, `${at}: the title row can wrap`).toEqual(['inline-flex', 'nowrap', 'nowrap']);
      expect(g.titleHeight, `${at}: the rendered title takes more than one line`).toBeLessThanOrEqual(g.lineHeight + 1.5);
      expect(g.clearance, `${at}: the title or its artwork reaches the controls`).toBeGreaterThan(0);
      expect([g.rows, g.inBar], `${at}: the controls left their one row`).toEqual([1, true]);
    }
  }
});

test('at and below 1023px the operational controls are glyph-only; above it they are labelled; the widest state keeps one clear row', async ({page}) => {
  test.setTimeout(120_000);
  await open(page, 1440);
  for (const width of [1920, 1440, 1180, 1024, 1023, 900, 788, 787, 780]) {
    await resize(page, width);
    for (const view of VIEWS) {
      await show(page, view);
      const g = await measure(page, view, true), at = `${view} at ${width}px (widest state)`;
      expect(g.buttons.map(button => button.id), at).toEqual(['btn-resume-paused', 'btn-pause-all']);
      for (const button of g.buttons) {
        expect(button.glyph, `${at}: ${button.id} lost its glyph`).toBe(true);
        expect(button.labelShown, `${at}: ${button.id} label`).toBe(width > COMPACT_MAX);
        if (width <= COMPACT_MAX) expect(button.width, `${at}: ${button.id} is not one glyph square`).toBeCloseTo(37, 0);
      }
      expect(g.buttons[0].tooltip, at).toBe('Resume Paused (12)');
      expect(g.art, `${at}: the artwork is hidden`).not.toBe('none');
      // 95px beside both controls below 788px, else 105px -- unless the title
      // fills its heading to the controls' gap (the longest title in the wider
      // fallback face): then the artwork yields that room, never below 48px.
      const art = width <= 787 ? 95 : 105;
      expect(g.artSize[1], `${at}: the artwork's lead`).toBe(4);
      if (g.artSize[0] < art - 0.5) {
        expect(g.artSize[0], `${at}: the artwork is drawn below its floor`).toBeGreaterThanOrEqual(48);
        expect(Math.abs(g.titleRight - g.headingRight), `${at}: the artwork yields while the heading has room`).toBeLessThanOrEqual(0.5);
      } else {
        expect(g.artSize[0], `${at}: the artwork's size`).toBe(art);
      }
      expect(g.titleHeight, `${at}: the rendered title takes more than one line`).toBeLessThanOrEqual(g.lineHeight + 1.5);
      expect(g.clearance, `${at}: the title or its artwork reaches the controls`).toBeGreaterThan(0);
      expect([g.rows, g.inBar], `${at}: the controls left their one row`).toEqual([1, true]);
      expect(Math.abs(g.width - 22 - g.lastRight), `${at}: the controls left the 22px inset`).toBeLessThanOrEqual(0.5);
      // The theme chevron sits in the inset strip by design, on screen.
      if (g.chevronRight !== null) expect(g.chevronRight, `${at}: the chevron is off screen`).toBeLessThanOrEqual(g.width);
      expect(g.overflow, `${at}: the page overflows`).toBe(0);
    }
  }
});

test('a glyph-only control keeps its full name, count, tooltip, keyboard reach and pending glyph', async ({page}) => {
  await open(page, 900);
  await show(page, 'dashboard');
  await page.evaluate(() => { processingPaused = false; pausedTransferCount = 12; renderTopbarActions(); });
  await expect(page.getByRole('button', {name: 'Resume Paused (12)', exact: true})).toBeVisible();
  await expect(page.getByRole('button', {name: 'Pause All', exact: true})).toBeVisible();
  await page.hover('#btn-resume-paused');
  await expect(page.locator('#dp-tooltip')).toHaveText('Resume Paused (12)');
  await page.mouse.move(5, 880);
  await page.focus('#btn-pause-all');
  expect(await page.evaluate(() => document.activeElement.id)).toBe('btn-pause-all');
  const pending = await page.evaluate(() => {
    const button = document.getElementById('btn-pause-all');
    setButtonPending(button, true, 'Pausing…');
    const during = {width: button.getBoundingClientRect().width, glyph: !!button.querySelector('svg'), busy: button.getAttribute('aria-busy'), label: button.querySelector('[data-dp-icon-label]').textContent};
    setButtonPending(button, false);
    return {during, after: {glyph: !!button.querySelector('svg'), label: button.querySelector('[data-dp-icon-label]').textContent}};
  });
  expect(pending.during.width).toBeCloseTo(37, 0);
  expect(pending.during).toMatchObject({glyph: true, busy: 'true', label: 'Pausing…'});
  expect(pending.after).toEqual({glyph: true, label: 'Pause All'});
  // The narrowest rail draws the artwork 10% smaller only while Resume Paused is
  // beside Pause All; it returns to its size as Resume Paused leaves.
  await resize(page, 780);
  const art = () => page.evaluate(() => parseFloat(getComputedStyle(document.getElementById('page-title'), '::after').width));
  await page.evaluate(() => { processingPaused = false; pausedTransferCount = 12; renderTopbarActions(); });
  expect(await art()).toBe(95);
  await page.evaluate(() => { pausedTransferCount = 0; renderTopbarActions(); });
  expect(await art()).toBe(105);
  await page.evaluate(() => { pausedTransferCount = 12; renderTopbarActions(); });
  await resize(page, 788);
  expect(await art()).toBe(105);
  // Labelled (above 1023px) the tooltip would only repeat the label, so none is shown.
  await resize(page, 1024);
  await page.hover('#btn-resume-paused');
  await page.waitForTimeout(800);
  await expect(page.locator('#dp-tooltip')).toBeHidden();
});
